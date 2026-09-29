import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        tmp = Path.home() / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        self.scratch = tempfile.TemporaryDirectory(prefix="amd-termux-test-", dir=tmp)
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)


class ShellTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.amd = self.root / "AMD user's files"
        self.prefix = self.root / "prefix user's files"
        self.bin = self.prefix / "bin"
        self.bin.mkdir(parents=True)
        self.tmp = self.root / "tmp"
        self.tmp.mkdir()
        self.env = os.environ.copy()
        self.env.update(
            PREFIX=str(self.prefix),
            AMD_DIR=str(self.amd),
            AMD_LITE_DIR=str(self.root / "local wrapper user's files"),
            AMD_MANAGER_DIR=str(self.prefix / "share/amd-termux"),
            AMD_TMP_DIR=str(self.tmp),
            AMD_TEST_PREFIX=str(self.prefix),
            AMD_TEST_TMP=str(self.tmp),
            PATH=str(self.bin) + os.pathsep + self.env["PATH"],
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
        )
        (self.bin / "python").symlink_to(sys.executable)
        # Reproduce grun's command-string reconstruction, including its lost quoting.
        self.write_executable(
            self.bin / "grun",
            "#!" + shutil.which("bash") + '\n[ "$1" = "-s" ] || exit 90\nshift\nexec bash -c "$*"\n',
        )

    @staticmethod
    def write_executable(path, contents):
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def shell(self, command, **kwargs):
        setup = 'source "$1"\nPREFIX="$AMD_TEST_PREFIX"\nAMD_TMP_DIR="$AMD_TEST_TMP"\nGLIBC_BIN="$PREFIX/glibc/bin"\n'
        return subprocess.run(
            ["bash", "-c", setup + command, "amd-test", str(PROJECT / "amd.sh")],
            cwd=self.root, env=self.env, text=True, capture_output=True, **kwargs,
        )

    def git(self, directory, *args):
        return subprocess.run(
            ["git", "-c", "user.name=AMD Test", "-c", "user.email=amd-test@example.invalid",
             "-c", "commit.gpgsign=false", "-C", str(directory), *args],
            env=self.env, check=True, text=True, capture_output=True,
        ).stdout.strip()

    def make_origin(self):
        origin = self.root / "origin"
        origin.mkdir()
        self.git(origin, "init", "--initial-branch=v2")
        (origin / "main.py").write_text("v2\n")
        (origin / ".gitignore").write_text("config.toml\n.venv/\nrun.sh\n")
        self.git(origin, "add", ".")
        self.git(origin, "commit", "-m", "v2")
        self.git(origin, "checkout", "-b", "v3")
        (origin / "main.py").write_text("v3\n")
        self.git(origin, "add", ".")
        self.git(origin, "commit", "-m", "v3")
        self.env["AMD_TEST_ORIGIN"] = str(origin)
        return origin

    def clone_old(self, origin):
        self.git(self.root, "clone", "--depth=1", "--single-branch", "--branch", "v2",
                 origin.as_uri(), str(self.amd))

    def test_new_install_clones_v3(self):
        self.make_origin()
        result = self.shell('AMD_REPO="$AMD_TEST_ORIGIN"\nsync_repository\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.amd, "branch", "--show-current"), "v3")
        self.assertEqual((self.amd / "main.py").read_text(), "v3\n")

    def test_old_shallow_single_branch_clone_upgrades_and_updates(self):
        origin = self.make_origin()
        self.clone_old(origin)
        original_config = 'version = "0.0.10"\n'
        (self.amd / "config.toml").write_text(original_config)
        result = self.shell("sync_repository\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.amd, "branch", "--show-current"), "v3")
        self.assertEqual((self.amd / "config.toml").read_text(), original_config)
        (origin / "main.py").write_text("new v3\n")
        self.git(origin, "commit", "-am", "v3 update")
        result = self.shell("sync_repository\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.amd, "rev-parse", "HEAD"), self.git(origin, "rev-parse", "v3"))

    def test_uncommitted_source_is_preserved(self):
        origin = self.make_origin()
        self.clone_old(origin)
        (self.amd / "main.py").write_text("my source changes\n")
        result = self.shell("sync_repository\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.amd / "main.py").read_text(), "my source changes\n")
        self.assertEqual(self.git(self.amd, "branch", "--show-current"), "v2")

    def test_staged_source_is_preserved(self):
        origin = self.make_origin()
        self.clone_old(origin)
        (self.amd / "main.py").write_text("staged changes\n")
        self.git(self.amd, "add", "main.py")
        result = self.shell("sync_repository\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.amd, "diff", "--cached", "--name-only"), "main.py")
        self.assertEqual(self.git(self.amd, "branch", "--show-current"), "v2")

    def test_diverged_branch_is_not_reset(self):
        origin = self.make_origin()
        self.assertEqual(self.shell('AMD_REPO="$AMD_TEST_ORIGIN"\nsync_repository\n').returncode, 0)
        (self.amd / "main.py").write_text("local v3\n")
        self.git(self.amd, "commit", "-am", "local commit")
        previous_head = self.git(self.amd, "rev-parse", "HEAD")
        (origin / "main.py").write_text("upstream v3\n")
        self.git(origin, "commit", "-am", "upstream commit")
        result = self.shell("sync_repository\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.amd, "rev-parse", "HEAD"), previous_head)
        self.assertEqual((self.amd / "main.py").read_text(), "local v3\n")

    def test_glibc_script_failure_propagates_and_cleans_up(self):
        result = self.shell("run_in_glibc <<'EOF'\nexit 29\nEOF\necho UNEXPECTED_SUCCESS\n")
        self.assertEqual(result.returncode, 29, result.stderr)
        self.assertNotIn("UNEXPECTED_SUCCESS", result.stdout)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_failed_environment_does_not_continue_deployment(self):
        result = self.shell(
            'ensure_environment() { return 19; }\n'
            'sync_repository() { echo UNEXPECTED_FETCH; }\n'
            'deploy_amd\n'
        )
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertNotIn("UNEXPECTED_FETCH", result.stdout)

    def test_qemu_package_only_installs_when_missing_or_newer(self):
        scenarios = (
            ("installed 1:11.0.3", "1:11.0.3", False, 0),
            ("installed 1:11.0.3", "1:11.0.4", True, 0),
            ("installed 1:11.0.3", "11.0.4", False, 0),
            ("installed 1:11.0.3", "(none)", False, 0),
            ("installed 1:11.0.3", "", False, 0),
            ("config-files 1:11.0.3", "1:11.0.3", True, 0),
            ("", "1:11.0.3", True, 0),
            ("installed 1:11.0.3", "1:11.0.4", True, 19),
        )
        for installed, candidate, downloads, exit_code in scenarios:
            with self.subTest(installed=installed, candidate=candidate, exit_code=exit_code):
                self.env.update(AMD_TEST_PACKAGE_INFO=installed, AMD_TEST_CANDIDATE=candidate,
                                AMD_TEST_PKG_EXIT=str(exit_code))
                result = self.shell(
                    'dpkg-query() { printf "%s" "$AMD_TEST_PACKAGE_INFO"; '
                    '[ -n "$AMD_TEST_PACKAGE_INFO" ]; }\n'
                    'apt-cache() { printf "  Candidate: %s\\n" "$AMD_TEST_CANDIDATE"; '
                    '[ -n "$AMD_TEST_CANDIDATE" ]; }\n'
                    'pkg() { printf "PKG_CALL: %s\\n" "$*"; return "$AMD_TEST_PKG_EXIT"; }\n'
                    'ensure_pkg qemu-system-x86-64-headless\n'
                    'echo PACKAGE_CHECK_COMPLETE\n'
                )
                self.assertEqual(result.returncode, exit_code, result.stderr)
                self.assertEqual("PKG_CALL:" in result.stdout, downloads)
                if downloads:
                    self.assertIn("PKG_CALL: install -y qemu-system-x86-64-headless", result.stdout)
                else:
                    self.assertIn("跳过下载", result.stdout)
                self.assertEqual("PACKAGE_CHECK_COMPLETE" in result.stdout, exit_code == 0)

    def test_dependency_sync_works_without_pip_in_venv(self):
        self.amd.mkdir()
        (self.amd / "requirements.txt").write_text("example-dependency==1.0\n")
        venv_bin = self.amd / ".venv/bin"
        venv_bin.mkdir(parents=True)
        self.write_executable(
            venv_bin / "python",
            "#!" + sys.executable + "\n"
            "import sys\n"
            "if sys.argv[1:3] == ['-m', 'pip']:\n"
            "    raise SystemExit('No module named pip')\n"
            "sys.stdin.read()\n",
        )
        glibc_bin = self.prefix / "glibc/bin"
        glibc_bin.mkdir(parents=True)
        self.env["AMD_TEST_PIP_LOG"] = str(self.root / "pip.jsonl")
        self.write_executable(
            glibc_bin / "python3",
            "#!" + sys.executable + "\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "if args == ['-']:\n"
            "    sys.stdin.read()\n"
            "elif args[:2] == ['-m', 'venv']:\n"
            "    pass  # A --without-pip venv deliberately has no pip module.\n"
            "elif args[:2] == ['-m', 'pip']:\n"
            "    if args[2:4] != ['--python', '.venv']:\n"
            "        raise SystemExit('Refusing to change system packages')\n"
            "    # Termux skips pip itself when other packages are requested.\n"
            "    with open(os.environ['AMD_TEST_PIP_LOG'], 'a') as log:\n"
            "        log.write(json.dumps(args[4:]) + '\\n')\n"
            "else:\n"
            "    raise SystemExit('Unexpected Python invocation')\n",
        )
        result = self.shell("sync_python\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in Path(self.env["AMD_TEST_PIP_LOG"]).read_text().splitlines()]
        self.assertTrue(any("-r" in call and "requirements.txt" in call for call in calls))
        self.assertIn(["check"], calls)
        self.assertFalse(any("pip" in call for call in calls))
        # Completing dependency management does not require installing venv pip.
        pip_probe = subprocess.run(
            [str(venv_bin / "python"), "-m", "pip", "--version"],
            text=True, capture_output=True,
        )
        self.assertNotEqual(pip_probe.returncode, 0)
        self.assertIn("No module named pip", pip_probe.stderr)

    def prepare_launcher(self):
        venv_bin = self.amd / ".venv/bin"
        venv_bin.mkdir(parents=True)
        (self.amd / "main.py").write_text("# simulated AMD entry\n")
        self.env["AMD_TEST_RESULT"] = str(self.root / "args.json")
        self.write_executable(
            venv_bin / "python",
            "#!" + sys.executable + "\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['AMD_TEST_RESULT']).write_text(json.dumps({\n"
            "    'args': sys.argv[1:], 'cwd': os.getcwd(), 'tmp': os.environ['TMPDIR']}))\n"
            "raise SystemExit(int(os.environ.get('AMD_TEST_EXIT', '0')))\n",
        )
        result = self.shell("write_launcher\n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_launcher_preserves_arguments_working_directory_and_exit_code(self):
        self.prepare_launcher()
        arguments = ["--legacy-ui", "has space", "", "中文", "*", "'quoted'", "$(printf INJECTED)", "`printf INJECTED`"]
        self.env["AMD_TEST_EXIT"] = "17"
        result = subprocess.run(
            ["bash", str(self.bin / "amd"), "start", *arguments],
            cwd=self.root, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        captured = json.loads(Path(self.env["AMD_TEST_RESULT"]).read_text())
        self.assertEqual(captured["args"], ["main.py", *arguments])
        self.assertEqual(captured["cwd"], str(self.amd))
        self.assertEqual(captured["tmp"], str(self.tmp))
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_start_without_arguments_adds_no_empty_argument(self):
        self.prepare_launcher()
        subprocess.run(["bash", str(self.bin / "amd"), "start"], env=self.env, check=True, capture_output=True)
        captured = json.loads(Path(self.env["AMD_TEST_RESULT"]).read_text())
        self.assertEqual(captured["args"], ["main.py"])
        self.assertEqual(list(self.tmp.iterdir()), [])


    def test_launcher_without_arguments_opens_menu(self):
        self.prepare_launcher()
        result = subprocess.run(
            ["bash", str(self.bin / "amd")], input="0\n",
            cwd=self.root, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("3) 启动 AMD", result.stdout)
        self.assertIn("5) 管理本地 wrapper-lite Server", result.stdout)
        self.assertIn("Wrapper Server 状态：未安装", result.stdout)
        self.assertLess(result.stdout.index("Wrapper Server 状态："), result.stdout.index("1) 安装 AMD"))
        self.assertNotIn("5) 启动本地", result.stdout)
        self.assertFalse(Path(self.env["AMD_TEST_RESULT"]).exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_first_menu_install_works_before_amd_and_after_checkout_moves(self):
        checkout = self.root / "checkout user's files"
        checkout.mkdir()
        for name in ("amd.sh", "lite.py"):
            shutil.copyfile(PROJECT / name, checkout / name)
        result = subprocess.run(
            ["bash", str(checkout / "amd.sh")], input="0\n",
            cwd=self.root, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        checkout.rename(self.root / "moved checkout")
        result = subprocess.run(
            ["bash", str(self.bin / "amd")], input="3\n5\n5\n0\n0\n",
            cwd=self.root, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("尚未完成 AMD 安装", result.stderr)
        self.assertIn("尚无服务日志", result.stdout)
        self.assertIn("Wrapper Server 状态：未安装", result.stdout)
        self.assertNotIn("查看状态", result.stdout)
        self.assertNotIn("显示 AMD 配置示例", result.stdout)
        self.assertFalse(self.amd.exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_menu_returns_after_amd_exits_or_fails(self):
        self.prepare_launcher()
        for code in (0, 17, 130):
            with self.subTest(exit_code=code):
                self.env["AMD_TEST_EXIT"] = str(code)
                result = subprocess.run(
                    ["bash", str(self.bin / "amd")], input="3\n0\n",
                    cwd=self.root, env=self.env, text=True, capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count("=== AppleMusicDecrypt v3 管理 ==="), 2)
                captured = json.loads(Path(self.env["AMD_TEST_RESULT"]).read_text())
                self.assertEqual(captured["args"], ["main.py"])
                if code == 17:
                    self.assertIn("退出码 17", result.stderr)
                if code == 130:
                    self.assertIn("操作已取消", result.stdout)
                self.assertEqual(list(self.tmp.iterdir()), [])

    def test_failed_menu_deployment_stops_before_fetch_and_returns(self):
        result = self.shell(
            'ensure_environment() { return 19; }\n'
            'sync_repository() { echo UNEXPECTED_FETCH; }\n'
            'install_amd() { deploy_amd; }\n'
            'main\n',
            input="1\n0\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("UNEXPECTED_FETCH", result.stdout)
        self.assertIn("退出码 19", result.stderr)
        self.assertEqual(result.stdout.count("=== AppleMusicDecrypt v3 管理 ==="), 2)

    def test_local_server_actions_and_failures_stay_in_menu(self):
        calls = self.root / "lite-calls"
        self.env["AMD_TEST_CALLS"] = str(calls)
        directory = Path(self.env["AMD_LITE_DIR"])
        (directory / "qemu").mkdir(parents=True)
        for name in ("qemu/vmlinuz-lite-qemu", "qemu/lite-initramfs.cpio.gz", "data.img"):
            (directory / name).write_text("installed")
        result = self.shell(
            'run_lite() {\n'
            '    printf "%s\\n" "$*" >> "$AMD_TEST_CALLS"\n'
            '    case "$1" in start) return 23 ;; status) echo "Wrapper Server 未运行"; return 3 ;; esac\n'
            '}\n'
            'install_lite() { printf "install\\n" >> "$AMD_TEST_CALLS"; }\n'
            'main\n',
            input="5\n1\n2\n3\n4\n5\n0\n0\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = calls.read_text().splitlines()
        self.assertEqual([command for command in commands if command != "status"],
                         ["install", "login", "start", "stop", "logs"])
        self.assertEqual(commands.count("status"), 8)
        self.assertEqual(result.stdout.count("Wrapper Server 状态：未运行"), 8)
        self.assertEqual(result.stderr.count("退出码 23"), 1)
        self.assertNotIn("退出码 3", result.stderr)
        self.assertEqual(result.stdout.count("=== AppleMusicDecrypt v3 管理 ==="), 2)

    def test_menu_edits_the_configured_amd_file(self):
        self.amd.mkdir()
        config = self.amd / "config.toml"
        config.write_text('[instance]\nurl = "old.example"\n')
        editor = self.root / "editor user's files"
        self.write_executable(
            editor,
            "#!" + sys.executable + "\n"
            "import sys\nfrom pathlib import Path\n"
            "path = Path(sys.argv[1])\n"
            "path.write_text(path.read_text().replace('old.example', 'new.example'))\n",
        )
        self.env["EDITOR"] = str(editor)
        self.assertEqual(self.shell("install_manager\n").returncode, 0)
        result = subprocess.run(
            ["bash", str(self.bin / "amd")], input="4\n0\n",
            cwd=self.root, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_text(), '[instance]\nurl = "new.example"\n')
        self.assertEqual(result.stdout.count("=== AppleMusicDecrypt v3 管理 ==="), 2)

    def test_ctrl_c_during_start_returns_to_menu_and_cleans_up(self):
        self.prepare_launcher()
        ready = self.root / "ready"
        self.env["AMD_TEST_READY"] = str(ready)
        self.write_executable(
            self.amd / ".venv/bin/python",
            "#!" + sys.executable + "\n"
            "import os, time\nfrom pathlib import Path\n"
            "Path(os.environ['AMD_TEST_READY']).touch()\n"
            "while True:\n    time.sleep(1)\n",
        )
        with subprocess.Popen(
            ["bash", str(self.bin / "amd")], cwd=self.root, env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        ) as process:
            try:
                process.stdin.write("3\n")
                process.stdin.flush()
                deadline = time.monotonic() + 5
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "simulated AMD did not start")
                os.killpg(process.pid, signal.SIGINT)
                output, errors = process.communicate("0\n", timeout=10)
                self.assertEqual(process.returncode, 0, errors)
                self.assertIn("操作已取消", output)
                self.assertEqual(output.count("=== AppleMusicDecrypt v3 管理 ==="), 2)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_direct_start_keeps_standard_input(self):
        self.prepare_launcher()
        self.write_executable(
            self.amd / ".venv/bin/python",
            "#!" + sys.executable + "\n"
            "import os, sys\nfrom pathlib import Path\n"
            "Path(os.environ['AMD_TEST_RESULT']).write_text(sys.stdin.read())\n",
        )
        result = subprocess.run(
            ["bash", str(self.bin / "amd"), "start"], input="interactive AMD input\n",
            cwd=self.root, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(self.env["AMD_TEST_RESULT"]).read_text(), "interactive AMD input\n")

    def test_legacy_ui_shortcut_still_starts_amd(self):
        self.prepare_launcher()
        result = subprocess.run(
            ["bash", str(self.bin / "amd"), "--legacy-ui"],
            cwd=self.root, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        captured = json.loads(Path(self.env["AMD_TEST_RESULT"]).read_text())
        self.assertEqual(captured["args"], ["main.py", "--legacy-ui"])

    def test_help_and_unknown_command_do_not_install_or_start(self):
        for command, code in (("--help", 0), ("unknown-command", 2)):
            with self.subTest(command=command):
                result = subprocess.run(
                    ["bash", str(PROJECT / "amd.sh"), command],
                    cwd=self.root, env=self.env, text=True, capture_output=True,
                )
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertFalse((self.bin / "amd").exists())
                self.assertFalse(self.amd.exists())


if __name__ == "__main__":
    unittest.main()
