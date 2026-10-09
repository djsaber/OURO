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

当前实现只支持现行数据格式，不提供旧格式的自动兼容或迁移。遇到版本不符时，保留原数据并选择新的
`OURO_HOME`，或按明确的数据维护流程处理；程序不会自动清空会话。

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

通过 `Ctrl+P → Theme` 菜单前面的 `Welcome style` 分组直接选择欢迎页样式，后面的 `Theme color`
分组选择界面配色。内置 Still 静态字标与 Horizon、Bloom、Labyrinth、Tide、Iris、Prism 六种动态点阵艺术包，
默认使用 Horizon。`Animation` 可暂停或恢复动画。样式与播放偏好立即保存，独立于界面配色；
所选作品立即显示在菜单后方，菜单打开期间暂停动画。自定义包接入见[欢迎页样式包开发](../development/welcome-styles.md)。
