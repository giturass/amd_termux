#!/usr/bin/env bash
set -euo pipefail

AMD_DIR="${AMD_DIR:-$HOME/AppleMusicDecrypt}"
AMD_REPO="https://github.com/WorldObservationLog/AppleMusicDecrypt.git"
AMD_BRANCH="v3"
AMD_TMP_DIR="${AMD_TMP_DIR:-$HOME/tmp}"
GLIBC_BIN="$PREFIX/glibc/bin"
AMD_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
AMD_LITE_DIR="${AMD_LITE_DIR:-$HOME/wrapper-lite}"
AMD_MANAGER_DIR="${AMD_MANAGER_DIR:-$PREFIX/share/amd-termux}"

C_BOLD_BLUE="" C_BOLD_GREEN="" C_BOLD_YELLOW="" C_BOLD_RED=""
C_BOLD_CYAN="" C_BOLD_MAGENTA="" C_BOLD_GRAY=""
C_BOLD_ORANGE="" C_BOLD_LIME="" C_RESET=""
if [ -t 1 ] && [ "${TERM:-}" != dumb ] && [ -z "${NO_COLOR:-}" ]; then
    C_BOLD_BLUE='\033[1;34m'
    C_BOLD_GREEN='\033[1;32m'
    C_BOLD_YELLOW='\033[1;33m'
    C_BOLD_RED='\033[1;31m'
    C_BOLD_CYAN='\033[1;36m'
    C_BOLD_MAGENTA='\033[1;35m'
    C_BOLD_GRAY='\033[1;30m'
    C_BOLD_ORANGE='\033[38;5;208m'
    C_BOLD_LIME='\033[38;5;118m'
    C_RESET='\033[0m'
fi

menu_line() {
    printf '%b%s%b\n' "$1" "$2" "$C_RESET"
}

menu_divider() {
    menu_line "$C_BOLD_BLUE" '====================================='
}

menu_header() {
    menu_divider
    menu_line "$C_BOLD_MAGENTA" "$1"
    menu_divider
    lite_status
    menu_divider
}

menu_prompt() {
    menu_divider
    printf '%b请选择 (0-5): %b' "$C_BOLD_CYAN" "$C_RESET"
}

menu() {
    menu_header '=== AppleMusicDecrypt v3 管理 ==='
    menu_line "$C_BOLD_GREEN" '1) 安装 AMD'
    menu_line "$C_BOLD_YELLOW" '2) 更新 AMD'
    menu_line "$C_BOLD_LIME" '3) 启动 AMD'
    menu_line "$C_BOLD_ORANGE" '4) 修改 AMD 配置文件'
    menu_line "$C_BOLD_CYAN" '5) 管理本地 wrapper-lite Server'
    menu_line "$C_BOLD_GRAY" '0) 退出'
    menu_prompt
}

ensure_pkg() {
    local p package_info installed_version candidate_version
    for p in "$@"; do
        # dpkg -s 对仅剩配置文件的包也可能成功，需检查实际安装状态。
        # shellcheck disable=SC2016 # 由 dpkg-query 展开字段名。
        package_info=$(dpkg-query -W -f='${db:Status-Status} ${Version}' "$p" 2>/dev/null) || package_info=""
        if [[ "$package_info" == installed\ * ]]; then
            installed_version=${package_info#installed }
            if [ "$p" = qemu-system-x86-64-headless ]; then
                # 只读本地软件源记录；无新版时不运行 pkg，不触发下载。
                candidate_version=$(LC_ALL=C apt-cache policy "$p" 2>/dev/null |
                    awk '$1 == "Candidate:" { print $2; exit }') || candidate_version=""
                if [ -n "$candidate_version" ] && [ "$candidate_version" != '(none)' ] &&
                   dpkg --compare-versions "$candidate_version" gt "$installed_version"; then
                    echo "[update] $p $installed_version -> $candidate_version"
                    pkg install -y "$p"
                    continue
                fi
            fi
            echo "[skip] $p 已安装 ($installed_version)，跳过下载"
        else
            echo "[install] $p"
            pkg install -y "$p"
        fi
    done
}

# grun 会把参数重新拼成 shell 命令；脚本内容和路径变量写入临时文件，
# 避免引号、空格及换行在 grun 的参数处理中丢失。
run_in_glibc() (
    mkdir -p "$AMD_TMP_DIR"
    amd_grun_tmp=$(mktemp "$AMD_TMP_DIR/amd-grun.XXXXXX.sh")
    trap 'rm -f -- "$amd_grun_tmp"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    {
        printf 'set -euo pipefail\n'
        printf 'export AMD_DIR=%q\n' "$AMD_DIR"
        printf 'export TMPDIR=%q\n' "$AMD_TMP_DIR"
        # shellcheck disable=SC2016 # 在生成的脚本中展开 PATH。
        printf 'export PATH=%q:"$PATH"\n' "$GLIBC_BIN"
        cat
    } >"$amd_grun_tmp"
    grun -s bash "$amd_grun_tmp"
)

ensure_glibc_runner() {
    if dpkg -s glibc-runner >/dev/null 2>&1 && command -v grun >/dev/null 2>&1; then
        echo "[skip] glibc-runner 已安装"
        return 0
    fi
    ensure_pkg glibc-repo
    pkg update -y
    ensure_pkg glibc-runner
    command -v grun >/dev/null 2>&1 || { echo "grun 不可用，安装失败" >&2; return 1; }
}

ensure_environment() {
    case "$(dpkg --print-architecture)" in
        aarch64|x86_64) ;;
        *) echo "v3 的 Temari 预编译库需要 aarch64 或 x86_64 Termux" >&2; return 1 ;;
    esac
    ensure_glibc_runner
    ensure_pkg python-glibc python-pip-glibc libsqlite-glibc git
}

sync_repository() {
    if [ ! -e "$AMD_DIR" ]; then
        git clone --branch "$AMD_BRANCH" --single-branch "$AMD_REPO" "$AMD_DIR"
        return
    fi
    [ -d "$AMD_DIR/.git" ] || { echo "$AMD_DIR 不是 AMD Git 仓库" >&2; return 1; }
    if ! git -C "$AMD_DIR" diff --quiet || ! git -C "$AMD_DIR" diff --cached --quiet; then
        echo "AMD 源码有本地修改，请先提交或备份后再更新: $AMD_DIR" >&2
        return 1
    fi
    # 显式 fetch refspec 也适用于旧版 --single-branch / 浅克隆。
    git -C "$AMD_DIR" fetch origin "+refs/heads/$AMD_BRANCH:refs/remotes/origin/$AMD_BRANCH"
    if git -C "$AMD_DIR" show-ref --verify --quiet "refs/heads/$AMD_BRANCH"; then
        git -C "$AMD_DIR" checkout "$AMD_BRANCH"
    else
        git -C "$AMD_DIR" checkout --no-track -b "$AMD_BRANCH" "refs/remotes/origin/$AMD_BRANCH"
    fi
    git -C "$AMD_DIR" merge --ff-only "refs/remotes/origin/$AMD_BRANCH"
}

sync_python() {
    run_in_glibc <<'EOF'
cd "$AMD_DIR"
python3 - <<'PY'
import ctypes
import json
from datetime import datetime
from pathlib import Path
import subprocess
import sys

if sys.version_info < (3, 11) or not hasattr(ctypes.CDLL(None), "gnu_get_libc_version"):
    raise SystemExit("需要 glibc Python 3.11+；请更新 python-glibc 后重试")

venv = Path(".venv")
if venv.exists() or venv.is_symlink():
    probe = "import json, sys; print(json.dumps([list(sys.version_info[:2]), sys.base_prefix]))"
    try:
        result = subprocess.run([str(venv / "bin/python"), "-c", probe],
                                capture_output=True, text=True, check=True)
        compatible = json.loads(result.stdout) == [list(sys.version_info[:2]), sys.base_prefix]
    except (OSError, subprocess.SubprocessError, ValueError):
        compatible = False
    if not compatible:
        backup = venv.with_name(".venv.bak-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        venv.rename(backup)
        print(f"旧 Python 环境已备份到 {backup}，将重新创建 .venv")
PY
# Termux 的 pip 会跳过安装 pip 自身（即使指定 --python）。
# 始终由 python-pip-glibc 管理无 pip 的 .venv，所有包仍安装到虚拟环境中。
python3 -m venv --without-pip .venv
python3 -m pip --python .venv install --upgrade setuptools wheel
python3 -m pip --python .venv install -r requirements.txt
python3 -m pip --python .venv check
.venv/bin/python - <<'PY'
import temari
library = temari.load()
print(f"Temari 库加载成功: {library.path}")
PY
EOF
}

init_config() {
    if [ ! -e "$AMD_DIR/config.toml" ]; then
        cp -- "$AMD_DIR/config.example.toml" "$AMD_DIR/config.toml"
    fi
}

write_launcher() {
    {
        printf '#!/usr/bin/env bash\nset -euo pipefail\n'
        printf 'cd -- %q\n' "$AMD_DIR"
        printf 'export TMPDIR=%q\n' "$AMD_TMP_DIR"
        # shellcheck disable=SC2016 # 在生成的脚本中展开 TMPDIR。
        printf 'mkdir -p "$TMPDIR"\n'
        printf 'exec .venv/bin/python main.py "$@"\n'
    } >"$AMD_DIR/run.sh"
    chmod +x "$AMD_DIR/run.sh"
    install_manager
}

# 菜单与 AMD 安装独立；首次运行仓库脚本即可注册 amd 命令。
# 先暂存再替换，更新正在运行的菜单时不会截断当前脚本。
install_manager() (
    [ -f "$AMD_SCRIPT_DIR/lite.py" ] || { echo "缺少 lite.py，请使用完整的 amd_termux 仓库" >&2; return 1; }
    mkdir -p "$AMD_TMP_DIR" "$AMD_MANAGER_DIR" "$PREFIX/bin"
    amd_manager_stage=$(mktemp -d "$AMD_TMP_DIR/amd-menu.XXXXXX")
    trap 'rm -rf -- "$amd_manager_stage"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    cp -- "$AMD_SCRIPT_DIR/amd.sh" "$AMD_SCRIPT_DIR/lite.py" "$amd_manager_stage/"
    {
        printf '#!/usr/bin/env bash\nset -euo pipefail\n'
        printf 'export PREFIX=%q\n' "$PREFIX"
        printf 'export AMD_DIR=%q\n' "$AMD_DIR"
        printf 'export AMD_LITE_DIR=%q\n' "$AMD_LITE_DIR"
        printf 'export AMD_TMP_DIR=%q\n' "$AMD_TMP_DIR"
        printf 'export AMD_MANAGER_DIR=%q\n' "$AMD_MANAGER_DIR"
        # shellcheck disable=SC2016 # 在入口执行时展开 PATH 和参数。
        printf 'export PATH="$PATH":%q\n' "$PREFIX/bin"
        # shellcheck disable=SC2016
        printf 'exec bash %q "$@"\n' "$AMD_MANAGER_DIR/amd.sh"
    } >"$amd_manager_stage/amd"
    chmod 755 "$amd_manager_stage/amd" "$amd_manager_stage/amd.sh"
    mv -f -- "$amd_manager_stage/amd.sh" "$AMD_MANAGER_DIR/amd.sh"
    mv -f -- "$amd_manager_stage/lite.py" "$AMD_MANAGER_DIR/lite.py"
    mv -f -- "$amd_manager_stage/amd" "$PREFIX/bin/amd"
    echo "菜单命令已就绪：amd"
)

start_amd() (
    if [ ! -f "$AMD_DIR/main.py" ] || [ ! -x "$AMD_DIR/.venv/bin/python" ]; then
        echo "尚未完成 AMD 安装，请先选择菜单 1 安装或菜单 2 更新" >&2
        return 1
    fi
    command -v grun >/dev/null 2>&1 || { echo "缺少 grun，请先安装或更新 AMD" >&2; return 1; }
    mkdir -p "$AMD_TMP_DIR"
    amd_run_tmp=$(mktemp "$AMD_TMP_DIR/amd-run.XXXXXX.sh")
    trap 'rm -f -- "$amd_run_tmp"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    # 不依赖旧版 run.sh，让已有安装也能正确传递 --legacy-ui 等参数。
    {
        printf 'set -euo pipefail\n'
        printf 'cd -- %q\n' "$AMD_DIR"
        printf 'export TMPDIR=%q\n' "$AMD_TMP_DIR"
        printf 'exec .venv/bin/python main.py'
        for arg in "$@"; do
            printf ' %q' "$arg"
        done
        printf '\n'
    } >"$amd_run_tmp"
    export PATH="$PATH:$PREFIX/bin"
    grun -s bash "$amd_run_tmp"
)

deploy_amd() {
    ensure_environment
    sync_repository
    sync_python
    init_config
    write_launcher
    echo "AMD v3 已就绪，运行 amd 后选择 3 启动（直接启动：amd start）"
    echo "请在 $AMD_DIR/config.toml 的 [instance] 中配置 wrapper-lite HTTP 实例"
}

install_amd() {
    if [ ! -r /sdcard ] || [ ! -w /sdcard ]; then
        echo "请先执行: termux-setup-storage" >&2
        return 1
    fi
    deploy_amd
}

update_amd() {
    [ -d "$AMD_DIR/.git" ] || { echo "尚未安装，请先选择安装 AMD" >&2; return 1; }
    deploy_amd
}

edit_config() {
    [ -f "$AMD_DIR/config.toml" ] || { echo "尚未完成安装，请先安装或更新 AMD v3" >&2; return 1; }
    local editor="${EDITOR:-vi}"
    if [ "$editor" = vi ] && ! command -v vi >/dev/null 2>&1; then
        ensure_pkg vim
    fi
    "$editor" "$AMD_DIR/config.toml"
}

install_lite() {
    case "$(dpkg --print-architecture)" in
        aarch64|x86_64) ;;
        *) echo "本地 Wrapper Server 需要 aarch64 或 x86_64 Termux" >&2; return 1 ;;
    esac
    ensure_pkg python curl qemu-system-x86-64-headless
    "$PREFIX/bin/python" -B "$AMD_SCRIPT_DIR/lite.py" --dir "$AMD_LITE_DIR" install "$@"
    {
        printf '#!/usr/bin/env bash\nset -euo pipefail\n'
        printf 'exec %q -B %q --dir %q "$@"\n' \
            "$PREFIX/bin/python" "$AMD_LITE_DIR/lite.py" "$AMD_LITE_DIR"
    } >"$PREFIX/bin/wrapper"
    chmod +x "$PREFIX/bin/wrapper"
    install_manager
    echo "本地服务可从 amd 菜单 5 管理；也可继续使用 wrapper 命令"
}

run_lite() {
    [ -x "$PREFIX/bin/python" ] || { echo "请先从菜单 5 安装本地 Wrapper Server" >&2; return 1; }
    "$PREFIX/bin/python" -B "$AMD_SCRIPT_DIR/lite.py" --dir "$AMD_LITE_DIR" "$@"
}

# 不把操作函数放进 if / ||：那会让 Bash 忽略函数内部的 set -e，
# 例如安装依赖失败后仍继续部署。子 shell 保持失败即停，菜单负责接收结果。
menu_action() {
    local status
    trap ':' INT
    set +e
    (
        trap - INT
        set -e
        "$@"
    )
    status=$?
    set -e
    trap - INT
    if [ "$status" -eq 130 ]; then
        printf '\n操作已取消，已返回菜单。\n'
    elif [ "$status" -ne 0 ]; then
        printf '\n操作未完成（退出码 %s），请查看上方提示。\n' "$status" >&2
    fi
    return 0
}

lite_status() {
    local status_text status_color="$C_BOLD_YELLOW"
    if [ ! -s "$AMD_LITE_DIR/qemu/vmlinuz-lite-qemu" ] ||
       [ ! -s "$AMD_LITE_DIR/qemu/lite-initramfs.cpio.gz" ] ||
       [ ! -s "$AMD_LITE_DIR/data.img" ]; then
        status_text='未安装'
    elif [ ! -x "$PREFIX/bin/python" ]; then
        status_text='无法检测（缺少 Python，请安装 / 更新 Wrapper Server）'
    elif status_text=$(run_lite status 2>&1); then
        status_color="$C_BOLD_GREEN"
    else
        case "$?" in
            3) status_color="$C_BOLD_RED" ;;
        esac
    fi
    printf 'Wrapper Server 状态：%b%s%b\n' "$status_color" "${status_text#Wrapper Server }" "$C_RESET"
}

lite_menu() {
    local choice
    while true; do
        menu_header '=== 本地 wrapper-lite Server ==='
        menu_line "$C_BOLD_GREEN" '1) 安装 / 更新 Wrapper Server'
        menu_line "$C_BOLD_YELLOW" '2) 登录 Apple ID'
        menu_line "$C_BOLD_LIME" '3) 后台启动服务'
        menu_line "$C_BOLD_RED" '4) 停止服务'
        menu_line "$C_BOLD_ORANGE" '5) 查看日志'
        menu_line "$C_BOLD_GRAY" '0) 返回'
        menu_prompt
        read -r choice || return 0
        case "$choice" in
            1) menu_action install_lite ;;
            2) menu_action run_lite login ;;
            3) menu_action run_lite start ;;
            4) menu_action run_lite stop ;;
            5) menu_action run_lite logs ;;
            0) return 0 ;;
            *) menu_line "$C_BOLD_RED" '无效选择' ;;
        esac
    done
}

main() {
    local c
    while true; do
        menu
        read -r c || return 0
        case "$c" in
            1) menu_action install_amd ;;
            2) menu_action update_amd ;;
            3) menu_action start_amd ;;
            4) menu_action edit_config ;;
            5) lite_menu ;;
            0) return 0 ;;
            *) menu_line "$C_BOLD_RED" '无效选择' ;;
        esac
    done
}

usage() {
    cat <<'EOF'
用法：amd [命令]
  不带参数 / menu       打开管理菜单
  install              安装 AMD
  update               更新 AMD
  start [AMD 参数]      启动 AMD，例如 amd start --legacy-ui
  config               编辑 AMD 配置文件
  wrapper              打开本地 wrapper-lite Server 子菜单
  wrapper <命令>       install / login / start / stop / status / logs
  setup                安装或更新 amd 菜单命令
  --help               显示帮助
EOF
}

cli() {
    local command="${1:-menu}"
    if [ "$#" -gt 0 ]; then
        shift
    fi
    case "$command" in
        menu|setup|install|update|config)
            if [ "$#" -gt 0 ]; then
                echo "$command 不接受额外参数" >&2
                return 2
            fi ;;
    esac
    case "$command" in
        menu)
            if [[ "$AMD_SCRIPT_DIR" != "$AMD_MANAGER_DIR" ]]; then
                install_manager
            fi
            main ;;
        setup) install_manager ;;
        install) install_amd ;;
        update) update_amd ;;
        start) start_amd "$@" ;;
        config) edit_config ;;
        wrapper)
            if [ "$#" -eq 0 ]; then
                lite_menu
            elif [[ "$1" == install ]]; then
                shift
                install_lite "$@"
            else
                run_lite "$@"
            fi ;;
        --legacy-ui) start_amd --legacy-ui "$@" ;;
        --help|-h|help) usage ;;
        *) echo "未知命令：$command；运行 amd --help 查看用法" >&2; return 2 ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    cli "$@"
fi
