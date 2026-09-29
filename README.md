# amd_termux

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Checks](https://github.com/giturass/amd_termux/actions/workflows/checks.yml/badge.svg)](https://github.com/giturass/amd_termux/actions/workflows/checks.yml)

在 Android Termux 中安装、运行和更新 [AppleMusicDecrypt v3](https://github.com/WorldObservationLog/AppleMusicDecrypt/tree/v3)，并管理可选的本地 wrapper-lite 服务。无需 root 或 proot。

`amd_termux` 提供 Termux 环境适配和管理入口；音乐处理能力来自 AppleMusicDecrypt，账号登录与 HTTP 服务来自 wrapper-lite。

- **一个入口**：输入 `amd` 打开中文彩色菜单，安装、更新、启动、编辑配置和管理服务。
- **glibc 运行环境**：通过 `glibc-runner` 使用 glibc Python 3.11+ 与独立 `.venv`，安装上游锁定的依赖。
- **AMD v3 支持**：使用 Temari 本地解密、Python 封装与标签处理，安装流程无需 Bento4、FFmpeg、GPAC 或 Poetry。
- **本地服务可选**：使用 Termux 原生 QEMU 运行 wrapper-lite，支持交互登录、后台启停、状态和日志。
- **保留已有数据**：更新保留 AMD 配置和服务登录磁盘；源码存在未提交修改或分支分叉时停止更新。

## 目录

- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [命令速查](#命令速查)
- [本地 Wrapper Server](#本地-wrapper-server)
- [更新与配置](#更新与配置)
- [目录与运行方式](#目录与运行方式)
- [常见问题](#常见问题)
- [开发与贡献](#开发与贡献)
- [致谢与许可证](#致谢与许可证)

## 环境要求

- Android 上的 [Termux](https://github.com/termux/termux-app#installation)，架构为 **aarch64 或 x86_64**。32 位 ARM / x86 不受支持。
- 已执行 `termux-setup-storage` 并授予共享存储访问权限。
- 可以访问 GitHub、Termux 软件源和 PyPI；安装本地服务还需要访问 nightly.link。
- 可用的 **wrapper-lite HTTP 实例**或兼容的 wrapper-manager。可以使用已有实例，也可以在手机上部署本地服务并登录自己的 Apple Music 账号。

AMD v3 使用 HTTP API，与旧版 gRPC / ADB 后端不兼容。旧地址可连接不代表可以直接用于 v3。

## 快速开始

### 1. 获取管理脚本

在 Termux 中执行：

```bash
pkg update
pkg install git
termux-setup-storage
git clone https://github.com/giturass/amd_termux.git ~/amd_termux
cd ~/amd_termux
bash ./amd.sh
```

请保留完整仓库，`amd.sh` 需要同目录的 `lite.py`。首次运行会安装全局命令 `amd` 并打开菜单，此时无需预先安装 AppleMusicDecrypt。

```text
=====================================
=== AppleMusicDecrypt v3 管理 ===
=====================================
Wrapper Server 状态：未安装
=====================================
1) 安装 AMD
2) 更新 AMD
3) 启动 AMD
4) 修改 AMD 配置文件
5) 管理本地 wrapper-lite Server
0) 退出
```

### 2. 安装并连接实例

主菜单选择 **1** 安装 AMD，完成后选择实例接入方式：

| 接入方式 | 操作 |
| --- | --- |
| 已有 HTTP 实例 | 准备实例地址，并确认服务端已登录 Apple Music |
| 手机本地实例 | 进入主菜单 **5**，依次选择 **1 安装 / 更新**、**2 登录 Apple ID**、**3 后台启动服务** |

回到主菜单选择 **4** 编辑 `config.toml`，在已有配置中调整对应字段。以下是本地实例配置片段，**不要用它覆盖整个配置文件**：

```toml
[instance]
url = "127.0.0.1:12340"
secure = false

[localInstance]
enable = false
```

外部实例的 `url` 填实际主机与端口，**不带 `http://` 或 `https://`**；HTTPS 实例设置 `secure = true`。本地服务由 `amd` 管理，因此 `[localInstance].enable` 保持 `false`。

### 3. 开始使用

选择主菜单 **3**，或直接执行：

```bash
amd start
# 小屏终端可使用简洁界面
amd start --legacy-ui
```

进入 AMD 后可以使用 `dl <Apple Music URL>`、`qa <Apple Music URL>`、`status` 等命令，具体功能见 [AMD v3 上游文档](https://github.com/WorldObservationLog/AppleMusicDecrypt/tree/v3#readme)。通过菜单启动时，AMD 退出、操作失败或取消后会返回菜单。

## 命令速查

| 命令 | 用途 |
| --- | --- |
| `amd` / `amd menu` | 打开主菜单，自动显示本地服务状态 |
| `amd install` | 安装 AMD v3 |
| `amd update` | 更新 AMD v3 源码和依赖 |
| `amd start [参数]` | 直接启动 AMD，传递上游启动参数 |
| `amd config` | 编辑 AMD 配置 |
| `amd wrapper` | 打开本地服务管理菜单 |
| `amd wrapper install` | 安装或更新本地服务 |
| `amd wrapper login` | 交互登录 Apple ID |
| `amd wrapper start` / `amd wrapper stop` | 启动 / 停止本地服务 |
| `amd wrapper status` / `amd wrapper logs` | 查看服务状态 / 最近 60 行日志 |
| `amd --help` | 显示管理命令帮助 |

兼容原有 `amd --legacy-ui` 用法。本地服务安装后也提供 `wrapper` 兼容命令，推荐统一使用 `amd wrapper`。

## 本地 Wrapper Server

### 安装、登录和启动

```bash
amd wrapper install
amd wrapper login
amd wrapper start
amd wrapper status
```

安装会通过 `pkg` 安装原生 `python`、`curl` 和 `qemu-system-x86-64-headless`，并下载上游 [wrapper lite 构建产物](https://nightly.link/WorldObservationLog/wrapper/workflows/build-lite/lite)中的 `qemu-assets.zip`。aarch64 和 x86_64 手机均使用 x86_64 来宾与 TCG 模拟，默认分配 **512 MB 来宾内存、2 个虚拟 CPU**，HTTP 端口仅绑定 **`127.0.0.1:12340`**。

登录必须在交互终端中执行。按提示输入 Apple ID 和密码，密码不回显；需要两步验证时，在 `2FA code` 提示后输入验证码。登录完成后虚拟机会退出，随后执行 `amd wrapper start`。重新登录或更新服务前先执行 `amd wrapper stop`。

登录状态保存在 `~/wrapper-lite/data.img`，更新会保留此文件。备份时先停止服务，再复制登录磁盘；该文件包含账号登录数据，不应上传到仓库或 Issue。

### 启动参数与后台运行

```bash
amd wrapper start --port 12345 --memory 768 --smp 2 --timeout 300
amd wrapper logs
amd wrapper stop
```

`--memory` 单位为 MB。自定义端口后，同步修改 AMD 配置中的 `[instance].url`。这些选项只作用于当次启动，后续启动需要重新传入。

`start` 默认等待 HTTP 接口最多 180 秒；超时或启动失败时会停止本次虚拟机并保留日志。`status` 显示服务状态及登录地区；地区为空时仍需登录。`stop` 最多等待来宾关机 30 秒，无响应时通过 QEMU 管理接口退出。

Android 可能回收后台 Termux。长时间使用时，可以执行 `termux-wake-lock` 并调整系统电池优化设置；使用结束后执行 `termux-wake-unlock`。

### 使用手机上的代理

QEMU 来宾中的 `10.0.2.2` 指向手机宿主。登录和每次启动服务时分别传入代理：

```bash
amd wrapper login --proxy socks5h://10.0.2.2:1080
amd wrapper start --proxy socks5h://10.0.2.2:1080
```

将 `1080` 改为手机代理应用的 SOCKS 端口；HTTP 代理可使用 `http://10.0.2.2:端口`。管理程序访问本地 `/status` 时直接连接，不受环境代理变量影响。

### 手动下载构建产物

nightly.link 下载受限时，可以先下载上游 `qemu-assets.zip`，再安装：

```bash
amd wrapper install --archive ~/storage/downloads/qemu-assets.zip
```

构建产物过期时需要重新获取可用产物。安装会校验所需文件与上游 `/init` 结构，不兼容时停止更新，已有登录磁盘会保留。安装和更新服务不会修改 AMD 配置。

## 更新与配置

**管理脚本、AMD 和本地服务分别更新：**

```bash
# 更新本仓库与全局 amd 入口；路径改为自己的仓库目录
cd ~/amd_termux
git pull --ff-only
bash ./amd.sh setup

# 更新 AMD v3
amd update

# 更新本地服务（仅在已安装时需要）
amd wrapper stop
amd wrapper install
amd wrapper start
```

`amd update` 拉取上游 `v3` 分支，支持从旧版单分支克隆迁移，只执行快进合并。存在未提交源码修改或分支分叉时会停止，请先自行提交或备份修改。

AMD 配置不存在时才从上游 `config.example.toml` 复制；已有配置保持原样。从旧版升级或上游配置变化后，请对照新模板手动调整实例地址、下载目录和并发等选项。

`amd config` 默认使用 `vi`，缺少时安装 `vim`；也可以通过 `EDITOR` 指定已安装的编辑器，例如 `EDITOR=nano amd config`。

安装通过 `pkg` 获取 `glibc-repo`、`glibc-runner`、`python-glibc`、`python-pip-glibc`、`libsqlite-glibc` 和 `git`。Python 依赖使用 glibc 侧的 `python3 -m pip --python .venv` 安装到 `.venv`，虚拟环境本身无需包含 pip。更新会复用有效环境；环境损坏、Python 来源不匹配或主 / 次版本变化时，先备份为 `.venv.bak-时间戳` 再重建。安装结束会检查依赖一致性并加载 Temari 动态库。

本地 QEMU 软件包仅在未安装或本地软件源记录中存在更新版本时下载。需要刷新候选版本时，先运行 `pkg update`。

## 目录与运行方式

| 路径 | 内容 |
| --- | --- |
| `~/amd_termux/` | 本仓库，用于获取和更新管理程序 |
| `~/AppleMusicDecrypt/` | 上游 v3 源码、`.venv`、`config.toml` 和 `run.sh` |
| `$PREFIX/bin/amd` | 全局管理入口 |
| `$PREFIX/share/amd-termux/` | 已安装的 `amd.sh` 和 `lite.py` |
| `~/wrapper-lite/` | 本地服务、`qemu/` 镜像、`data.img` 登录磁盘及 `server.log` |
| `$PREFIX/bin/wrapper` | 本地服务的兼容管理入口 |
| `~/tmp/amd-lite-*` | 服务锁、QEMU 管理套接字、安装暂存与参数临时文件 |
| `~/tmp/amd-menu.*`、`~/tmp/amd-grun.*`、`~/tmp/amd-run.*` | 菜单更新及 glibc 启动暂存，退出后清理 |

源码、虚拟环境和服务磁盘应放在 **Termux 私有存储**中。首次安装前可以指定目录，或通过 `setup` 更新入口记录的路径：

```bash
AMD_DIR="$HOME/AppleMusicDecrypt" AMD_LITE_DIR="$HOME/wrapper-lite" bash ./amd.sh setup
```

该命令记录路径，不会搬移已有数据。全局入口使用安装后的脚本副本，因此移动本仓库后仍可运行 `amd`。

源码由 Termux Git 管理，AMD 的 Python 环境创建和执行通过 `grun` 完成。启动器将参数转义后写入临时脚本，以保留空格、引号和标准输入。

本地服务直接调用原生 QEMU 与系统固件。安装时在上游 initramfs 后追加 `/init` 覆盖层，保留网络、驱动和 wrapper 二进制；登录参数放在来宾内存中，通过第二串口接收关机请求，退出时同步并卸载数据盘。

## 常见问题

| 问题 | 处理方式 |
| --- | --- |
| 共享存储不可访问 | 执行 `termux-setup-storage` 并授予权限后重试 |
| `grun` 不可用 | 检查网络和 glibc 软件源，运行 `pkg update`、`pkg install glibc-runner` |
| Python 版本过旧 | 运行 `pkg upgrade python-glibc python-pip-glibc`，再执行 `amd update` |
| 旧脚本报 `No module named pip` | 更新本仓库并运行 `bash ./amd.sh setup`，再执行 `amd update`；无需删除 `.venv` |
| Temari 无法加载 | 使用 `amd start` 启动；检查 `TEMARI_LIB` 是否指向错误架构或 Android 原生库，移除错误覆盖后重试 |
| `/status` 返回 HTTP 403 或 `non-JSON body` | 检查实例是否被 Cloudflare 浏览器验证拦截；可更换网络或代理，或使用可供客户端访问的 v3 HTTP API 实例 |
| 无法连接 wrapper-lite | 核对 `[instance]` 地址、HTTP / HTTPS 设置和登录状态；本地服务运行 `amd wrapper status`、`amd wrapper logs` 排查 |
| 服务启动超时 | 先查看日志，必要时用 `amd wrapper start --timeout 300` 延长等待 |
| 上游 `/init` 启动流程已变化 | 更新本仓库后重试；仍不兼容时提交 Issue 并附构建产物来源 |

## 开发与贡献

欢迎通过 [Issues](https://github.com/giturass/amd_termux/issues) 反馈问题，或提交 Pull Request。问题报告请包含 Termux / Android 版本、设备架构、执行命令和去除账号信息后的错误日志，并说明使用本地还是外部实例。

在仓库目录执行：

```bash
pkg install python shellcheck git
bash -n amd.sh
shellcheck amd.sh
python -B -m unittest discover -s tests -v
```

测试仅使用 Python 标准库，临时数据写入 `~/tmp`。覆盖菜单分发、失败与 Ctrl+C 处理、Git 分支升级、无 pip 虚拟环境、参数与标准输入传递，以及本地服务安装、登录结果、并发锁、端口冲突、健康检查和启停。

测试使用本地 Git 仓库、模拟 `grun` 和提供真实本机套接字的 QEMU 替身，不安装软件或连接 Apple Music。GitHub Actions 执行同一套自动检查；真实 glibc / QEMU 镜像兼容性与账号登录仍需在 Termux 中验证。

## 致谢与许可证

- [fii6/amd_termux](https://github.com/fii6/amd_termux)：项目原始仓库，本仓库保留已有提交历史。
- [AppleMusicDecrypt](https://github.com/WorldObservationLog/AppleMusicDecrypt/tree/v3)：AMD v3 主程序。
- [wrapper](https://github.com/WorldObservationLog/wrapper/tree/lite)：wrapper-lite HTTP 服务及 QEMU 来宾构建产物。
- [Termux](https://termux.dev/)、[termux-glibc](https://github.com/termux-pacman/glibc-packages) 与 [QEMU](https://www.qemu.org/)：Android 终端、glibc 环境与虚拟化工具。

本仓库代码采用 **[Apache License 2.0](LICENSE)**。安装时下载的上游项目、依赖和来宾镜像分别遵循各自的许可证；本仓库许可证不替代这些许可证。
