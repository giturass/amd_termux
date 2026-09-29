"""Run the upstream wrapper-lite guest with Termux's native QEMU."""

import argparse
from contextlib import contextmanager
import fcntl
import getpass
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile


ASSET_URL = (
    "https://nightly.link/WorldObservationLog/wrapper/"
    "workflows/build-lite/lite/qemu-assets.zip"
)
KERNEL = "vmlinuz-lite-qemu"
INITRD = "lite-initramfs.cpio.gz"

# Keep the upstream network/module setup. This tail gives the host a shutdown
# channel, keeps login arguments off the persistent disk, and reports login's
# actual exit status (QEMU itself exits successfully even when guest login fails).
GUEST_TAIL = r'''/bin/busybox chmod +x /system/bin/lite /system/bin/linker64
if [ ! -s /lite-qemu-args ]; then
    echo "[amd-lite] guest arguments missing"
    /bin/busybox poweroff -f
    exit 1
fi
/bin/busybox stty -F /dev/ttyS1 raw -echo clocal -hupcl
LITE_ARGS_FILE=/lite-qemu-args /system/bin/lite </dev/ttyS0 &
lite_pid=$!
(
    while IFS= read -r command; do
        if [ "$command" = shutdown ]; then
            echo "[amd-lite] stopping guest"
            /bin/busybox kill -TERM "$lite_pid"
            break
        fi
    done
) </dev/ttyS1 &
shutdown_pid=$!
wait "$lite_pid"
lite_status=$?
/bin/busybox kill "$shutdown_pid" 2>/dev/null || true
/bin/busybox rm -f /lite-qemu-args
echo "[amd-lite] guest exit: $lite_status"
/bin/busybox sync
/bin/busybox umount /data
/bin/busybox poweroff -f
'''


class LiteError(RuntimeError):
    pass


def init_from_cpio(path):
    """Read just /init from the upstream gzip/newc initramfs, without extracting it."""
    with gzip.open(path, "rb") as stream:
        while True:
            header = stream.read(110)
            if len(header) != 110 or header[:6] not in (b"070701", b"070702"):
                raise LiteError("上游 initramfs 格式不兼容，安装已停止")
            fields = [int(header[i:i + 8], 16) for i in range(6, 110, 8)]
            size, name_size = fields[6], fields[11]
            if not 0 < name_size <= 4096:
                raise LiteError("上游 initramfs 文件名无效")
            name = stream.read(name_size).rstrip(b"\0")
            stream.read(-(110 + name_size) % 4)
            if name in (b"init", b"./init"):
                return stream.read(size).decode("utf-8")
            if name == b"TRAILER!!!":
                raise LiteError("上游 initramfs 缺少 /init")
            stream.seek(size + (-size % 4), os.SEEK_CUR)


def cpio_entry(name, data, mode=0o100755):
    name = name.encode() + b"\0"
    fields = [1, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name), 0]
    header = b"070701" + b"".join(f"{field:08x}".encode() for field in fields)
    entry = header + name
    return entry + b"\0" * (-len(entry) % 4) + data + b"\0" * (-len(data) % 4)


def adapt_initramfs(path):
    init = init_from_cpio(path)
    marker = "/bin/busybox chmod +x /system/bin/lite"
    mount = '/bin/busybox mount -t ext4 /dev/vda /data 2>/dev/null || echo "[init] data disk mount failed"'
    if init.count(marker) != 1 or mount not in init or "/data/.lite-qemu-args" not in init:
        raise LiteError("上游 /init 启动流程已变化，需要更新 amd_termux 后再安装")
    init = init.split(marker, 1)[0].replace("/data/.lite-qemu-args", "/lite-qemu-args")
    init = init.replace(mount, '/bin/busybox mount -t ext4 /dev/vda /data || {\n'
                        '    echo "[amd-lite] data disk mount failed"\n'
                        '    /bin/busybox poweroff -f\n    exit 1\n}')
    # Linux supports concatenated compressed cpio archives. The last /init wins;
    # the upstream kernel, libraries, and wrapper binary remain byte-for-byte intact.
    overlay = cpio_entry("init", (init + GUEST_TAIL).encode())
    overlay += cpio_entry("TRAILER!!!", b"", 0)
    with path.open("ab") as output:
        output.write(gzip.compress(overlay, mtime=0))


def qemu_value(value):
    # QEMU's key=value option parser uses doubled commas for literal commas.
    return str(value).replace(",", ",,")


class LiteServer:
    def __init__(self, directory, tmp=None, qemu=None):
        self.directory = Path(directory).expanduser().resolve()
        self.tmp = Path(tmp) if tmp else Path.home() / "tmp"
        identity = hashlib.sha256(os.fsencode(self.directory)).hexdigest()[:12]
        self.runtime = self.tmp / ("amd-lite-" + identity)
        self.assets = self.directory / "qemu"
        self.disk = self.directory / "data.img"
        self.log = self.directory / "server.log"
        self.state = self.runtime / "instance.json"
        self.qmp_socket = self.runtime / "qmp.sock"
        self.control_socket = self.runtime / "control.sock"
        prefix = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
        self.qemu = str(qemu or prefix / "bin/qemu-system-x86_64")
        self.firmware = prefix / "share/qemu"

    @contextmanager
    def lock(self):
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.runtime.mkdir(mode=0o700, exist_ok=True)
        self.runtime.chmod(0o700)
        with (self.runtime / "lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LiteError("另一个 Wrapper Server 操作正在进行，请稍后重试") from exc
            yield

    def qmp(self, command):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(self.qmp_socket))
            with connection.makefile("rwb") as stream:
                def reply():
                    while True:
                        line = stream.readline(65536)
                        if not line:
                            raise ConnectionResetError("QEMU 管理连接已断开")
                        message = json.loads(line)
                        if "error" in message:
                            raise LiteError("QEMU: " + str(message["error"]))
                        if "return" in message or "QMP" in message:
                            return message

                if "QMP" not in reply():
                    raise LiteError("无效的 QEMU 管理接口")
                for operation in ("qmp_capabilities", command):
                    stream.write(json.dumps({"execute": operation}).encode() + b"\n")
                    stream.flush()
                    result = reply()
                return result["return"]

    def active(self):
        for attempt in range(3):
            try:
                self.qmp("query-status")
                return True
            except (FileNotFoundError, ConnectionRefusedError):
                return False
            except (ConnectionResetError, BrokenPipeError):
                # During shutdown QEMU may close an accepted connection before
                # removing its socket. Retry before treating it as a live VM.
                if attempt == 2:
                    raise
                time.sleep(0.05)

    def require_stopped(self):
        if self.active():
            raise LiteError("Wrapper Server 正在运行，请先执行 amd wrapper stop")

    def require_installed(self):
        for path in (self.assets / KERNEL, self.assets / INITRD, self.disk):
            if not path.is_file() or path.stat().st_size == 0:
                raise LiteError("尚未安装完整的 Wrapper Server，请先从菜单安装")
        if not os.access(self.qemu, os.X_OK):
            raise LiteError("缺少原生 QEMU，请执行 pkg install qemu-system-x86-64-headless")
        if len(os.fsencode(self.control_socket)) >= 104:
            raise LiteError("临时目录路径过长，无法创建 QEMU 管理套接字")

    def install(self, archive=None):
        self.require_stopped()
        with tempfile.TemporaryDirectory(prefix="amd-lite-install-", dir=self.tmp) as scratch:
            staging = Path(scratch)
            if archive is None:
                archive = staging / "qemu-assets.zip"
                subprocess.run(
                    ["curl", "-fL", "--retry", "3", "--connect-timeout", "15",
                     "--max-time", "600", "-o", str(archive), ASSET_URL], check=True,
                )
            with zipfile.ZipFile(archive) as bundle:
                for name in (KERNEL, INITRD, "data.img", "LICENSE"):
                    member = next((item for item in (name, "qemu/" + name)
                                   if item in bundle.namelist()), None)
                    if member is None or bundle.getinfo(member).file_size == 0:
                        raise LiteError(f"压缩包缺少 {name}，需要上游 qemu-assets.zip")
                    # Extract only named files, never archive-controlled paths or symlinks.
                    with bundle.open(member) as source, (staging / name).open("wb") as target:
                        shutil.copyfileobj(source, target)
            adapt_initramfs(staging / INITRD)
            shutil.copyfile(__file__, staging / "lite.py")
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.directory.chmod(0o700)
            self.assets.mkdir(exist_ok=True)
            for name in (KERNEL, INITRD, "LICENSE"):
                os.replace(staging / name, self.assets / name)
            # Account storage is independent from the replaceable guest assets.
            if not self.disk.exists() and not self.disk.is_symlink():
                os.replace(staging / "data.img", self.disk)
            self.disk.chmod(0o600)
            os.replace(staging / "lite.py", self.directory / "lite.py")
        print(f"Wrapper Server 已安装到 {self.directory}")
        print("首次使用请执行 amd wrapper login，然后执行 amd wrapper start")

    def port(self):
        try:
            return int(json.loads(self.state.read_text())["port"])
        except (FileNotFoundError, KeyError, ValueError, TypeError):
            return 12340

    def status_data(self):
        # A localhost health probe must never inherit HTTP(S)_PROXY/ALL_PROXY.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{self.port()}/status", timeout=2) as response:
            result = json.loads(response.read(65536))
        if (not isinstance(result, dict) or result.get("code") != 0
                or not isinstance(result.get("data"), dict)
                or not isinstance(result["data"].get("regions"), list)
                or not all(isinstance(region, str) for region in result["data"]["regions"])):
            raise LiteError("/status 没有返回 wrapper-lite 的有效响应")
        return result["data"]["regions"]

    def status(self):
        if not self.active():
            print("Wrapper Server 未运行")
            return 3
        try:
            regions = self.status_data()
        except (OSError, ValueError, urllib.error.URLError, LiteError):
            print("QEMU 运行中，HTTP 接口尚未就绪；可用 amd wrapper logs 查看日志")
            return 1
        print(f"Wrapper Server 运行中: http://127.0.0.1:{self.port()}")
        print("登录地区: " + ", ".join(regions) if regions else "尚无登录地区，请先执行 amd wrapper stop，再执行 amd wrapper login")
        return 0

    def clean_runtime(self):
        for path in (self.qmp_socket, self.control_socket, self.state):
            path.unlink(missing_ok=True)

    @contextmanager
    def arguments(self, options, credentials=None):
        guest = ["--base-dir", "/data", "--host", "0.0.0.0", "--port", "12340"]
        if options.proxy:
            guest += ["--proxy", options.proxy]
        if credentials is not None:
            guest += ["--login", credentials]
        if any(any(char in value for char in "\r\n\0") for value in guest):
            raise LiteError("登录信息和代理地址不能包含换行或空字符")
        with tempfile.NamedTemporaryFile(mode="w", prefix="args-", dir=self.runtime) as handle:
            handle.write("\n".join(guest) + "\n")
            handle.flush()
            yield handle.name

    def command(self, options, arguments, login=False):
        return [
            self.qemu, "-L", str(self.firmware), "-name", "amd-lite",
            "-accel", "tcg", "-cpu", "max", "-m", str(options.memory), "-smp", str(options.smp),
            "-kernel", str(self.assets / KERNEL), "-initrd", str(self.assets / INITRD),
            "-append", "console=ttyS0 quiet net.ifnames=0 biosdevname=0",
            "-display", "none", "-monitor", "none", "-no-reboot",
            "-serial", "stdio" if login else "file:server.log",
            "-chardev", "socket,id=shutdown,path=" + qemu_value(self.control_socket) + ",server=on,wait=off",
            "-serial", "chardev:shutdown",
            "-qmp", "unix:" + qemu_value(self.qmp_socket) + ",server=on,wait=off",
            "-nic", f"user,model=e1000,hostfwd=tcp:127.0.0.1:{options.port}-:12340",
            "-drive", "file=" + qemu_value(self.disk) + ",format=raw,if=virtio",
            "-fw_cfg", "name=lite_args,file=" + qemu_value(arguments),
        ]

    def prepare(self, options):
        self.require_installed()
        self.require_stopped()
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", options.port))
            except OSError as exc:
                raise LiteError(f"本机端口 {options.port} 不可用: {exc}") from exc
        self.clean_runtime()
        self.state.write_text(json.dumps({"port": options.port}))

    def stop(self, timeout=30):
        if not self.active():
            self.clean_runtime()
            print("Wrapper Server 已停止")
            return
        # Keep the serial connection open until shutdown; disconnecting just
        # after sendall can drop queued UART input before the guest reads it.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(2)
            try:
                channel.connect(str(self.control_socket))
                channel.sendall(b"shutdown\n")
            except (FileNotFoundError, ConnectionRefusedError, socket.timeout):
                pass
            deadline = time.monotonic() + timeout
            while self.active() and time.monotonic() < deadline:
                time.sleep(0.2)
            if self.active():
                print("来宾未及时关机，正在退出 QEMU")
                try:
                    self.qmp("quit")
                except (ConnectionResetError, BrokenPipeError):
                    pass  # QEMU may exit before sending the quit reply.
                deadline = time.monotonic() + 5
                while self.active() and time.monotonic() < deadline:
                    time.sleep(0.1)
                if self.active():
                    raise LiteError("QEMU 尚未退出，请查看日志后重试停止")
        self.clean_runtime()
        print("Wrapper Server 已停止")

    def start(self, options):
        self.require_installed()
        if self.active():
            print(f"Wrapper Server 已在运行: http://127.0.0.1:{self.port()}")
            return
        self.prepare(options)
        try:
            with self.arguments(options) as arguments:
                result = subprocess.run(
                    self.command(options, arguments) + ["-daemonize"],
                    cwd=self.directory, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                )
                if result.stderr:
                    with self.log.open("ab") as output:
                        output.write(result.stderr)
                if result.returncode:
                    raise LiteError("QEMU 启动失败，请执行 amd wrapper logs 查看日志")
            print("QEMU 已启动，等待 Wrapper Server HTTP 接口...", flush=True)
            deadline = time.monotonic() + options.timeout
            next_notice = time.monotonic() + 10
            while time.monotonic() < deadline:
                if not self.active():
                    raise LiteError("QEMU 已退出，请执行 amd wrapper logs 查看日志")
                try:
                    self.status_data()
                    self.status()
                    return
                except (OSError, ValueError, urllib.error.URLError, LiteError):
                    pass
                if time.monotonic() >= next_notice:
                    print("仍在等待 HTTP 接口，可在另一终端用 amd wrapper logs 查看启动日志...", flush=True)
                    next_notice = time.monotonic() + 10
                time.sleep(1)
            raise LiteError("Wrapper Server 启动超时，请查看日志；可用 --timeout 增加等待秒数")
        except BaseException:
            self.stop(timeout=5)
            raise

    def login(self, options):
        self.require_installed()
        self.require_stopped()
        if not sys.stdin.isatty():
            raise LiteError("请在 Termux 交互终端中执行 amd wrapper login")
        username = input("Apple ID: ").strip()
        password = getpass.getpass("密码（不回显）: ")
        if not username or not password or ":" in username:
            raise LiteError("Apple ID 或密码为空，或 Apple ID 包含冒号")
        self.prepare(options)
        print("需要两步验证时，在下方 2FA code 提示后输入验证码。按 Ctrl+C 可取消。", flush=True)
        try:
            with self.arguments(options, username + ":" + password) as arguments:
                del password
                process = subprocess.Popen(
                    self.command(options, arguments, login=True), cwd=self.directory,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
                tail = b""
                try:
                    while chunk := os.read(process.stdout.fileno(), 4096):
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                        tail = (tail + chunk)[-8192:]
                    code = process.wait()
                finally:
                    try:
                        if process.poll() is None:
                            try:
                                self.stop(timeout=5)
                            finally:
                                if process.poll() is None:
                                    process.terminate()
                                    try:
                                        process.wait(timeout=5)
                                    except subprocess.TimeoutExpired:
                                        process.kill()
                                        process.wait()
                    finally:
                        process.stdout.close()
                if code or b"[amd-lite] guest exit: 0" not in tail:
                    raise LiteError("登录未完成，请检查上方来宾输出后重试")
        finally:
            self.clean_runtime()
        print("登录完成，可执行 amd wrapper start")

    def logs(self):
        if not self.log.exists():
            print("尚无服务日志")
            return
        with self.log.open("rb") as stream:
            stream.seek(max(0, self.log.stat().st_size - 65536))
            print(b"\n".join(stream.read().splitlines()[-60:]).decode(errors="replace"))


def number(low, high):
    def parse(value):
        try:
            result = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("需要整数") from exc
        if not low <= result <= high:
            raise argparse.ArgumentTypeError(f"取值范围为 {low} 到 {high}")
        return result
    return parse


def main(argv=None):
    os.umask(0o077)

    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)

    # Let finally blocks remove login arguments and stop a VM whose startup or
    # login is cancelled when the Termux terminal/session is closed.
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    parser = argparse.ArgumentParser(prog="amd wrapper", description="Termux 本地 wrapper-lite QEMU 服务管理")
    parser.add_argument("--dir", default=os.environ.get("AMD_LITE_DIR", str(Path.home() / "wrapper-lite")))
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", help="安装或更新上游 QEMU 来宾，保留登录磁盘")
    install.add_argument("--archive", type=Path, help="使用已下载的上游 qemu-assets.zip")
    for name in ("start", "login"):
        command = commands.add_parser(name, help="后台启动服务" if name == "start" else "交互登录 Apple ID")
        command.add_argument("--port", type=number(1024, 65535), default=12340)
        command.add_argument("--memory", type=number(256, 65536), default=512, help="来宾内存，单位 MB")
        command.add_argument("--smp", type=number(1, 32), default=2)
        command.add_argument("--proxy", help="来宾代理，例如 socks5h://10.0.2.2:1080")
        if name == "start":
            command.add_argument("--timeout", type=number(1, 3600), default=180, help="等待 HTTP 接口的秒数")
    for name, help_text in (("stop", "停止服务"), ("status", "查看服务和登录地区"),
                            ("logs", "查看最近 60 行日志")):
        commands.add_parser(name, help=help_text)
    options = parser.parse_args(argv)
    server = LiteServer(options.dir)
    try:
        if options.command in ("status", "logs"):
            return getattr(server, options.command)() or 0
        with server.lock():
            if options.command == "install":
                server.install(options.archive)
            elif options.command == "stop":
                server.stop()
            else:
                getattr(server, options.command)(options)
        return 0
    except KeyboardInterrupt:
        print("\n操作已取消", file=sys.stderr)
        return 130
    except (LiteError, OSError, EOFError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        print(f"Wrapper Server: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
