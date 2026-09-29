"""Small QEMU stand-in: real daemon, Unix QMP/control sockets, and HTTP port."""
import http.server
import json
import os
from pathlib import Path
import re
import signal
import socket
import sys
import threading
import time


def option(flag):
    return sys.argv[sys.argv.index(flag) + 1]


def field(value, key):
    for part in re.split(r"(?<!,),(?!,)", value):
        if part.startswith(key + "="):
            return part[len(key) + 1:].replace(",,", ",")
    raise ValueError(key)


mode = os.environ.get("AMD_TEST_QEMU_MODE", "normal")
if mode == "fail":
    print("simulated QEMU startup failure", file=sys.stderr)
    raise SystemExit(27)
args_file = Path(field(option("-fw_cfg"), "file"))
guest = args_file.read_text().splitlines()
record = {"argv": sys.argv[1:], "guest": guest, "mode": args_file.stat().st_mode & 0o777}
Path("command.json").write_text(json.dumps(record))
if "--login" in guest:
    print("2FA code: ")
    print("[amd-lite] guest exit: " + ("1" if mode == "login-fail" else "0"))
    raise SystemExit(0)

qmp = Path(option("-qmp")[len("unix:"):].split(",server=", 1)[0].replace(",,", ","))
control = Path(field(option("-chardev"), "path"))
port = int(option("-nic").split("hostfwd=tcp:127.0.0.1:")[1].split("-:")[0])
pid = os.fork()
if pid:
    for _ in range(200):
        if qmp.exists() and control.exists():
            raise SystemExit(0)
        if os.waitpid(pid, os.WNOHANG)[0]:
            raise SystemExit(1)
        time.sleep(0.01)
    os.kill(pid, signal.SIGTERM)
    raise SystemExit(1)

os.setsid()
with open(os.devnull, "r+b", buffering=0) as null:
    for fd in (0, 1, 2):
        os.dup2(null.fileno(), fd)
Path("daemon.pid").write_text(str(os.getpid()))
Path("server.log").write_text("fake guest boot\n")
stopped = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stopped.set())


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        if mode == "bad-http":
            self.wfile.write(b"<html>not the wrapper API</html>")
        else:
            self.wfile.write(b'{"code":0,"msg":"SUCCESS","data":{"regions":["us"]}}')

    def log_message(self, *_):
        pass


http = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
listeners = []


def listen(path, handler):
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen()
    listener.settimeout(0.1)
    listeners.append(listener)

    def serve():
        while not stopped.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection:
                handler(connection)

    threading.Thread(target=serve, daemon=True).start()


def manage(connection):
    with connection.makefile("rwb") as stream:
        stream.write(b'{"QMP":{"version":{},"capabilities":[]}}\n')
        stream.flush()
        while line := stream.readline():
            command = json.loads(line)["execute"]
            # Real QMP interleaves asynchronous events with command replies.
            stream.write(b'{"event":"TEST_EVENT"}\n')
            stream.write(json.dumps({"return": {"running": True, "status": "running"}
                                    if command == "query-status" else {}}).encode() + b"\n")
            stream.flush()
            if command == "quit":
                stopped.set()
                break


def shutdown(connection):
    if connection.recv(100) == b"shutdown\n":
        # A UART can lose queued bytes if the host disconnects immediately.
        # Only acknowledge shutdown while the sender keeps its connection open.
        connection.settimeout(0.05)
        try:
            if connection.recv(1) == b"":
                return
        except socket.timeout:
            pass
        Path("shutdown.received").write_text("graceful\n")
        if mode != "ignore-shutdown":
            stopped.set()


threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
listen(qmp, manage)
listen(control, shutdown)
stopped.wait()
http.shutdown()
http.server_close()
for listener in listeners:
    listener.close()
qmp.unlink(missing_ok=True)
control.unlink(missing_ok=True)
os._exit(0)
