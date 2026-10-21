# 运行与存储选择

> 文档类型：使用指南

```bash
# 默认全屏 TUI
uv run --frozen ouro --home ./.ouro-dev
uv run --frozen ouro --home ./.ouro-dev --approval-mode auto
uv run --frozen ouro --home ./.ouro-dev --afk
uv run --frozen ouro --home ./.ouro-dev --session <session_id>

# 单次运行
uv run --frozen ouro run --print --home ./.ouro-dev "hello"
uv run --frozen ouro run --json --home ./.ouro-dev "hello"
uv run --frozen ouro run --json --home ./.ouro-dev --approval-mode approval "review first"
uv run --frozen ouro run --json --home ./.ouro-dev --afk "continue autonomously"

# 继续会话
uv run --frozen ouro run --json --home ./.ouro-dev --session <session_id> "continue"

# 选择 SQLite Session 后端
uv run --frozen ouro run --json --home ./.ouro-dev --session-backend sqlite "hello"
uv run --frozen ouro session list --home ./.ouro-dev --session-backend sqlite

# 子智能体定义与运行策略
uv run --frozen ouro agent definition list --home ./.ouro-dev
uv run --frozen ouro agent definition tools --home ./.ouro-dev
uv run --frozen ouro config agents --home ./.ouro-dev
# 以下 revision=0 只适用于初始配置；已有配置使用上一条查询返回的当前 revision
uv run --frozen ouro config agents --home ./.ouro-dev --expected-revision 0 \
  --allow-agent general --allow-model '<provider_id>/*' \
  --execution-mode sync --execution-mode async --no-network

# 基于上面一次 completed Run 返回的 session_id/run_id 派遣无人值守 child
uv run --frozen ouro agent spawn <session_id> <run_id> general "inspect one bounded task" \
  --description "Bounded inspection" --request-id cli/example-1 --home ./.ouro-dev
uv run --frozen ouro agent tree <session_id> --home ./.ouro-dev
```

未指定 `--home` 时，OURO 使用 `OURO_HOME`；两者都未设置时使用 `~/.ouro`。
Session 后端默认是 `files`，也可通过 `OURO_SESSION_BACKEND=sqlite` 设置当前 Host 的默认值；命令行
`--session-backend files|sqlite` 优先于环境变量。交互、run、session、resource 和审批命令使用同一选择结果。
切换后端不会混读数据，文件和 SQLite 是两个独立数据集；当前格式的跨后端转移通过显式、可验证、可回滚的导入完成。

模型许可模式中的 `<provider_id>` 使用实际生成的 Provider ID，可从 `provider list` 取得；显示名称和产品类型不能代替该 ID。

运行时只读取现行数据格式。TUI 启动遇到数据版本错误时，会关闭启动中的服务并自动升级受支持数据，
显示阶段进度，成功后重新加载主页；失败时提供需单独危险确认的清空初始化兜底。
命令行升级、支持范围和恢复操作见[数据升级指南](data-upgrade.md)。

自动任务配置与执行记录也支持 `files`（默认）和 `sqlite`，通过 `--task-backend files|sqlite` 或
`OURO_TASK_BACKEND` 选择，命令行优先。文件数据位于 `<home>/tasks/`，SQLite 数据位于 `<home>/tasks.sqlite3`；
切换时不混读或自动迁移，已有 SQLite 任务可选择 `sqlite` 继续使用。任务会话始终走上述统一 Session 后端，
不会因任务记录使用 SQLite 而切换会话存储。配置与恢复边界见[自动任务规格](../specs/automated-tasks.md)。

## TUI 工作区与偏好

TUI 欢迎页可通过 `/workspace <path>` 为尚未创建的会话选择工作空间；输入命令后回车可从最近 20 条
历史目录中选择，也可继续输入其他目录。会话一旦创建，工作空间即固定，需使用 `/new` 返回新会话欢迎页后
再选择其他目录；`/workspace clear` 只清空最近工作空间记录，不改变当前会话的工作空间。Actions 的
`Settings` 分组统一提供主题、思考内容展开方式、输入草稿记忆、排队输入交付方式和上下文压缩策略；关闭草稿
记忆会清空已保存草稿。排队输入交付方式与压缩策略的更新在下一次启动时生效。

### 欢迎页主题

通过 `Ctrl+P → Theme` 菜单前面的 `Welcome style` 分组选择欢迎页主题，后面的 `Theme color`
分组选择界面配色。基础安装默认使用 Still 静态字标，包含完整的 TUI 功能。

安装可选主题拓展（PowerShell、bash 或 zsh）：

```bash
uv tool install --python 3.14 "ouro-agent[themes]"
```

使用 pip 时，在 OURO 所在虚拟环境执行 `python -m pip install "ouro-agent[themes]"`。
uv tool 使用独立环境，向系统 Python 或其他虚拟环境安装依赖不会改变 OURO 的可用主题。

点阵主题 Horizon、Bloom、Tide、Iris、Prism 需要 `numpy>=2.3.3,<3`。
更新主题包后，应在同一 OURO 环境中重新安装 `ouro-agent[themes]` 以同步依赖。
基础安装无需该依赖。数组运算使用 NumPy 自带的原生实现，无需额外 JIT、GPU 或计算服务。

TUI 每次启动检测主题所需依赖，只列出可用主题；安装依赖后需要重启。安装拓展只增加可选项，不自动
切换主题。已保存的主题暂不可用时，本次显示 Still 并提示原因，保留原选择；恢复依赖后再次启动即可
恢复该主题。使用同一 OURO 环境中手动安装的兼容依赖，也能启用对应主题。

主题选择立即保存，独立于界面配色。动画由各主题自行实现，TUI 菜单不提供主题内部设置。
所选作品立即显示在菜单后方，菜单打开期间暂停动画。自定义主题接入见
[欢迎页样式包开发](../development/welcome-styles.md)。
