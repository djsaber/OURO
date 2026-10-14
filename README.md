# OURO

[![PyPI](https://img.shields.io/pypi/v/ouro-agent?color=2563eb)](https://pypi.org/project/ouro-agent/)
[![Python 3.14+](https://img.shields.io/badge/Python-3.14%2B-3776ab)](https://www.python.org/downloads/)
[![MIT License](https://img.shields.io/badge/License-MIT-16a34a)](https://github.com/djsaber/OURO/blob/main/LICENSE)

**本地优先、可灵活拓展的个人智能体。**

OURO 让你在终端中与模型协作，处理文件、调用工具、管理会话和执行持续任务。它提供全屏交互界面，也可以通过命令行完成单次任务或输出 JSON。会话与配置保存在本地，模型服务由你选择。

插件是 OURO 的核心拓展机制：除了工具、Skills 和 MCP，还可以拓展模型接入、智能体运行方式、后台任务及插件间协作，按需安装、启用和停用。

![OURO v0.2.0 的终端交互界面](https://raw.githubusercontent.com/djsaber/OURO/v0.2.0/docs/assets/tui-welcome.png)

*OURO TUI · 可选 Horizon 欢迎页主题 · 尚未配置模型的状态*

[使用文档](https://github.com/djsaber/OURO/blob/main/docs/README.md) · [版本记录](https://github.com/djsaber/OURO/blob/main/CHANGELOG.md) · [源码](https://github.com/djsaber/OURO) · [问题反馈](https://github.com/djsaber/OURO/issues)

## 为你的工作方式而拓展

| 能力 | 你可以做什么 |
| --- | --- |
| 终端协作 | 在全屏 TUI 中交互，或通过 CLI 完成单次任务、输出 JSON。 |
| 本地资料 | 读取、编辑和搜索文件，使用附件及模型支持的图片、音频或视频输入。 |
| 持续任务 | 保存和恢复会话，通过子智能体、Goal / Plan 拆分任务、管理目标与计划。 |
| 模型与工具 | 配置多家模型供应商、兼容接口和 MCP 服务，为任务选择合适能力。 |
| 可组合插件 | 按需拓展工具、Skills、模型接入、智能体循环、插件协作与后台调度。 |
| 执行控制 | 通过权限、人工审批、取消操作和运行预算控制执行，查看用量与历史。 |

## 安装

OURO 需要 **Python 3.14 或更高版本**。建议使用支持 Unicode 的现代终端。

### 使用 uv 安装（推荐）

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后执行：

```bash
uv tool install --python 3.14 ouro-agent
ouro --version
```

uv 会为 OURO 创建独立环境，并按需安装所需 Python。若终端提示找不到 `ouro`，执行 `uv tool update-shell` 后重新打开终端。

### 使用 pip 安装

如果已经有 Python 3.14+ 环境，也可以安装到自己的虚拟环境中：

```bash
python -m pip install ouro-agent
ouro --version
```

PyPI 包名是 `ouro-agent`，安装后的命令是 `ouro`。

### TUI 欢迎页主题拓展

基础安装包含完整 TUI 和默认的 Still 欢迎页。可选主题通过 `themes` extra 安装：

```bash
uv tool install --python 3.14 "ouro-agent[themes]"
```

使用 pip 时，在 OURO 所在虚拟环境执行 `python -m pip install "ouro-agent[themes]"`。
重启 TUI 后，在 `Ctrl+P → Theme → Welcome style` 中选择可用主题。安装拓展不会改变已有选择。
点阵主题依赖 `arrayfunc`；Linux 源码构建的工具链要求与安装参数见
[欢迎页主题指南](https://github.com/djsaber/OURO/blob/main/docs/guides/running.md#欢迎页主题)。

## 第一次使用

### 1. 配置模型

使用真实模型需要对应供应商的账户与凭据。OURO 不附带模型权重或 API 额度；尚未配置供应商时，内置 Faux 仅提供离线演示，不是真实模型。

下面以 OpenAI API 为例。先在当前终端设置自己的 API Key：

**Windows PowerShell：**

```powershell
$env:OPENAI_API_KEY = "替换为你的 API Key"
```

**macOS / Linux（bash 或 zsh）：**

```bash
export OPENAI_API_KEY="替换为你的 API Key"
```

再添加供应商并设为默认：

```bash
ouro provider add "OpenAI" --type openai --api-key-env OPENAI_API_KEY --default
```

以上环境变量设置只对当前终端及其子进程生效。重新打开终端后，需要重新设置，或通过系统环境变量管理保存。OURO 配置中记录的是变量名。

你也可以通过 `ouro provider templates` 查看其他内置供应商模板，或在交互界面按 `Ctrl+P`，进入 `Manage providers` 管理供应商。模型是否可用取决于你的账户权限和供应商支持范围。

### 2. 启动交互界面

在想要处理的工作目录中执行：

```bash
ouro
```

输入任务即可开始。`Ctrl+P` 打开操作菜单，`/help` 查看快捷键与命令帮助。

### 3. 尝试命令行任务

```bash
# 文本输出
ouro run --print "说明当前目录的用途，并给出整理建议"

# JSON 输出，便于脚本集成
ouro run --json "总结本次任务"

# 查看帮助
ouro --help
```

只想先检查安装是否正常，可以在独立数据目录运行离线演示，无需 API Key：

```bash
ouro run --print --home ./.ouro-demo "hello"
```

## 按需增加能力

OURO 提供稳定的运行框架，插件作为独立拓展包接入。你可以从工具和 Skills 开始，也可以通过插件定制智能体的执行方式、模型接入、服务协作和后台任务。

插件管理入口可通过 `ouro plugin --help` 查看。请只安装并启用你信任的插件；插件和工具可能读写文件、执行程序或连接外部服务。

## 数据保存在哪里

默认数据目录为用户主目录下的 `.ouro`，用于保存配置、会话及运行记录。可以用 `--home` 为不同用途选择独立目录：

```bash
ouro --home ./.ouro-work
ouro session list --home ./.ouro-work
```

目录选择顺序为：`--home`、环境变量 `OURO_HOME`、默认的 `~/.ouro`。本地保存不代表所有处理都离线；模型请求会发送到你配置的服务。

升级前建议备份数据。关闭使用同一 home 的进程后，可用 `ouro-upgrade --home <path> --check` 检查、再去掉 `--check` 原地升级受支持的核心格式，详见[数据升级指南](https://github.com/djsaber/OURO/blob/main/docs/guides/data-upgrade.md)。范围外格式明确拒绝，不会自动清空原数据。

## 升级

使用 uv 安装：

```bash
uv tool upgrade ouro-agent
```

使用 pip 安装：

```bash
python -m pip install --upgrade ouro-agent
```

使用 pip 且需要主题拓展时，升级命令中的包名使用 `"ouro-agent[themes]"`。

## 文档与源码

完整源码位于 [GitHub](https://github.com/djsaber/OURO)，安装包和源码包发布在 [PyPI](https://pypi.org/project/ouro-agent/)。

- [文档入口](https://github.com/djsaber/OURO/blob/main/docs/README.md)：使用指南、插件开发、配置说明与运行契约。
- [更新记录](https://github.com/djsaber/OURO/blob/main/CHANGELOG.md)：各版本的主要改进与兼容性说明。
- [贡献指南](https://github.com/djsaber/OURO/blob/main/CONTRIBUTING.md)：开发流程与验证要求。
- [问题反馈](https://github.com/djsaber/OURO/issues)：报告问题或提出改进建议。

参与开发：

```bash
git clone https://github.com/djsaber/OURO.git
cd OURO
uv sync --frozen --all-groups
uv run --frozen ouro
```

## 开源协议

OURO 是完全开源的项目，采用 [MIT License](https://github.com/djsaber/OURO/blob/main/LICENSE)，允许使用、修改与分发。欢迎参与改进，也欢迎构建适合自己的插件。
