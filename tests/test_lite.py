import argparse
from contextlib import redirect_stdout
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import lite


PROJECT = Path(__file__).resolve().parents[1]
# A minimal upstream-format guest init. Real image compatibility is also checked
# by booting the actual upstream artifact on Termux during manual validation.
UPSTREAM_INIT = '''#!/bin/busybox sh
/bin/busybox mount -t ext4 /dev/vda /data 2>/dev/null || echo "[init] data disk mount failed"
/bin/busybox cat /sys/firmware/qemu_fw_cfg/by_name/lite_args/raw > /data/.lite-qemu-args
/bin/busybox chmod +x /system/bin/lite /system/bin/linker64
LITE_ARGS_FILE=/data/.lite-qemu-args /system/bin/lite
/bin/busybox poweroff -f
'''


class LiteTests(unittest.TestCase):
    def setUp(self):
        temp = Path.home() / "tmp"
        temp.mkdir(exist_ok=True)
        scratch = tempfile.TemporaryDirectory(prefix="al-", dir=temp)
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.directory = self.root / "server user's,files"
        qemu = self.root / "qemu"
        qemu.write_text("#!" + sys.executable + "\n" + (PROJECT / "tests/fake_qemu.py").read_text())
        qemu.chmod(0o755)
        self.server = lite.LiteServer(self.directory, tmp=self.root, qemu=qemu)
        self.output = io.StringIO()
        self.capture = redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)
        self.addCleanup(self.shutdown)
        self.archive = self.bundle()
        with self.server.lock():
            self.server.install(self.archive)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.options = argparse.Namespace(port=port, memory=512, smp=2, proxy=None, timeout=5)

    def shutdown(self):
        with self.server.lock():
            self.server.stop(timeout=1)

    def bundle(self, kernel=b"guest kernel", init=UPSTREAM_INIT):
        archive = self.root / "assets.zip"
        ramdisk = lite.cpio_entry("./init", init.encode()) + lite.cpio_entry("TRAILER!!!", b"", 0)
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(lite.KERNEL, kernel)
            bundle.writestr(lite.INITRD, gzip.compress(ramdisk))
            bundle.writestr("data.img", b"fresh account disk")
            bundle.writestr("LICENSE", "upstream MIT license")
        return archive

    def start(self):
        with self.server.lock():
            self.server.start(self.options)

    def test_update_keeps_account_disk_and_existing_amd_config(self):
        self.server.disk.write_bytes(b"existing login tokens")
        config = self.root / "config.toml"
        config.write_text('[instance]\nurl = "working.example"\n')
        self.bundle(kernel=b"new guest kernel")
        with self.server.lock():
            self.server.install(self.archive)
        self.assertEqual(self.server.disk.read_bytes(), b"existing login tokens")
        self.assertEqual((self.server.assets / lite.KERNEL).read_bytes(), b"new guest kernel")
        self.assertEqual(config.read_text(), '[instance]\nurl = "working.example"\n')
        self.assertEqual(self.server.disk.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.glob("amd-lite-install-*")), [])

    def test_incomplete_download_does_not_replace_working_files(self):
        old_init = (self.server.assets / lite.INITRD).read_bytes()
        with zipfile.ZipFile(self.archive, "w") as bundle:
            bundle.writestr(lite.KERNEL, "incomplete update")
        with self.server.lock(), self.assertRaises(lite.LiteError):
            self.server.install(self.archive)
        self.assertEqual((self.server.assets / lite.KERNEL).read_bytes(), b"guest kernel")
        self.assertEqual((self.server.assets / lite.INITRD).read_bytes(), old_init)
        self.assertEqual(self.server.disk.read_bytes(), b"fresh account disk")

    def test_changed_upstream_init_stops_update_before_replacing_files(self):
        self.bundle(kernel=b"new kernel", init="#!/bin/sh\nexit 0\n")
        with self.server.lock(), self.assertRaisesRegex(lite.LiteError, "/init"):
            self.server.install(self.archive)
        self.assertEqual((self.server.assets / lite.KERNEL).read_bytes(), b"guest kernel")
        self.assertEqual(list(self.root.glob("amd-lite-install-*")), [])

    def test_daemon_lifecycle_and_direct_health_probe(self):
        self.start()
        command = json.loads((self.directory / "command.json").read_text())
        args = command["argv"]
        self.assertEqual(args[args.index("-accel") + 1], "tcg")
        self.assertEqual(args[args.index("-nic") + 1],
                         f"user,model=e1000,hostfwd=tcp:127.0.0.1:{self.options.port}-:12340")
        self.assertIn("file=" + lite.qemu_value(self.server.disk), args[args.index("-drive") + 1])
        self.assertEqual(command["mode"], 0o600)
        self.assertEqual(list(self.server.runtime.glob("args-*")), [])
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "no_proxy": ""}):
            self.assertEqual(self.server.status_data(), ["us"])
        self.assertEqual(self.server.status(), 0)
        old_pid = (self.directory / "daemon.pid").read_text()
        self.start()
        self.assertEqual((self.directory / "daemon.pid").read_text(), old_pid)
        with self.server.lock(), self.assertRaisesRegex(lite.LiteError, "正在运行"):
            self.server.install(self.archive)
        with self.server.lock():
            self.server.stop(timeout=2)
        self.assertFalse(self.server.active())
        self.assertEqual((self.directory / "shutdown.received").read_text(), "graceful\n")
        self.assertEqual(self.server.status(), 3)
        self.assertFalse(self.server.state.exists())

    def test_failed_qemu_start_cleans_arguments_and_state(self):
        with patch.dict(os.environ, {"AMD_TEST_QEMU_MODE": "fail"}):
            with self.assertRaisesRegex(lite.LiteError, "启动失败"):
                self.start()
        self.assertIn("startup failure", self.server.log.read_text())
        self.assertFalse(self.server.active())
        self.assertFalse(self.server.state.exists())
        self.assertEqual(list(self.server.runtime.glob("args-*")), [])

    def test_non_json_health_response_times_out_and_stops_vm(self):
        self.options.timeout = 1
        with patch.dict(os.environ, {"AMD_TEST_QEMU_MODE": "bad-http"}):
            with self.assertRaisesRegex(lite.LiteError, "启动超时"):
                self.start()
        self.assertFalse(self.server.active())
        self.assertTrue((self.directory / "shutdown.received").exists())
        self.assertFalse(self.server.state.exists())

    def test_occupied_port_is_left_alone(self):
        with socket.socket() as other:
            other.bind(("127.0.0.1", self.options.port))
            other.listen()
            with self.assertRaisesRegex(lite.LiteError, "端口"):
                self.start()
            self.assertEqual(other.getsockname()[1], self.options.port)
        self.assertFalse((self.directory / "command.json").exists())

    def test_stale_socket_does_not_prevent_restart(self):
        for path in (self.server.qmp_socket, self.server.control_socket):
            with socket.socket(socket.AF_UNIX) as old:
                old.bind(str(path))
        self.assertFalse(self.server.active())
        self.start()
        self.assertTrue(self.server.active())

    def test_concurrent_operations_are_rejected(self):
        with self.server.lock(), self.assertRaisesRegex(lite.LiteError, "另一个"):
            with self.server.lock():
                self.fail("a second operation acquired the disk lock")

    def test_unresponsive_guest_uses_qmp_quit(self):
        with patch.dict(os.environ, {"AMD_TEST_QEMU_MODE": "ignore-shutdown"}):
            self.start()
        with self.server.lock():
            self.server.stop(timeout=0)
        self.assertFalse(self.server.active())

    def test_login_uses_private_file_and_guest_exit_status(self):
        for mode, success in (("normal", True), ("login-fail", False)):
            with self.subTest(mode=mode):
                output = io.TextIOWrapper(io.BytesIO())
                with patch("sys.stdin.isatty", return_value=True), \
                        patch("builtins.input", return_value="account@example.invalid"), \
                        patch("getpass.getpass", return_value="pa'ss$`word"), \
                        patch.dict(os.environ, {"AMD_TEST_QEMU_MODE": mode}), \
                        redirect_stdout(output), self.server.lock():
                    if success:
                        self.server.login(self.options)
                    else:
                        with self.assertRaisesRegex(lite.LiteError, "登录未完成"):
                            self.server.login(self.options)
                output.close()
                command = json.loads((self.directory / "command.json").read_text())
                self.assertNotIn("pa'ss$`word", " ".join(command["argv"]))
                self.assertIn("account@example.invalid:pa'ss$`word", command["guest"])
                self.assertEqual(command["mode"], 0o600)
                self.assertEqual(list(self.server.runtime.glob("args-*")), [])

    def test_noninteractive_login_does_not_prompt_for_password(self):
        with patch("sys.stdin.isatty", return_value=False), patch("getpass.getpass") as password:
            with self.server.lock(), self.assertRaisesRegex(lite.LiteError, "交互终端"):
                self.server.login(self.options)
        password.assert_not_called()

    def test_shell_installer_creates_native_launcher_with_quoted_paths(self):
        prefix = self.root / "prefix"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin/python").symlink_to(sys.executable)
        env = dict(os.environ, PREFIX=str(prefix), AMD_LITE_DIR=str(self.directory),
                   AMD_TEST_ARCHIVE=str(self.archive))
        # Emulate Termux's architecture name even on an Ubuntu CI host (amd64).
        result = subprocess.run(
            ["bash", "-c", 'source "$1"\n'
             'dpkg() { [ "$*" = --print-architecture ] || return 99; echo x86_64; }\n'
             'ensure_pkg() { :; }\ninstall_lite --archive "$AMD_TEST_ARCHIVE"',
             "amd-test", str(PROJECT / "amd.sh")],
            env=env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(shutil.rmtree, lite.LiteServer(self.directory).runtime, True)
        result = subprocess.run(["bash", str(prefix / "bin/wrapper"), "status"],
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("Wrapper Server 未运行", result.stdout)
        self.assertNotIn("grun", (prefix / "bin/wrapper").read_text())


if __name__ == "__main__":
    unittest.main()
