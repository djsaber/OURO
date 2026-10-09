# 连接 MCP Server

> 文档类型：使用指南

MCP server 配置保存在 `OURO_HOME/config/mcp.json` 的 `mcp.servers`。启用且 `auto_start` 的 server 会随
Host 启动在后台连接，不阻塞交互模式、`run` 或工具审批恢复。连接成功后，发现的工具会原子地
加入当前工具注册表；连接前或连接失败时其他 Agent 功能保持可用，Host 退出时统一关闭。
server 必须显式设置 `auto_start: true` 才会进入默认 Host，MCP 工具权限默认为 `approval`。

TUI 中按 `Ctrl+P` 打开操作菜单，`MCPs` 分组提供 `Manage MCPs` 和 `+ mcp`。
管理列表按连接类型分组，显示配置名称和连接状态，末尾的 `+` 可新增配置。配置页在 DETAILS 分组中
提供 Display name，名称必须非空且唯一，可包含中文和空格。OURO 自动管理内部标识，改名保持连接与工具的内部关联。选择一项后可编辑、启用或禁用、检查连接、
连接、刷新工具、重连、断开或删除；不可用的操作保持可见并置灰。`Esc` 逐级返回，列表同步反映配置和连接操作结果。

配置可通过 CLI 完整管理，无需直接修改 JSON。命令按去除首尾空白后的完整显示名称匹配，区分大小写；
含空格的名称需加引号。`set` 创建或更新同名配置，`--name` 用于指定新名称：

```bash
# stdio；以 --arg=... 形式传入以 "--" 开头的子进程参数
uv run --frozen ouro mcp set "Filesystem" --home ./.ouro-dev \
  --command npx --arg=-y --arg=@modelcontextprotocol/server-filesystem --arg=. \
  --auto-start

# Streamable HTTP；凭据引用优先使用环境变量
uv run --frozen ouro mcp set "Remote MCP" --home ./.ouro-dev \
  --transport http --url https://mcp.example.com/mcp \
  --credential Authorization=env:MCP_AUTHORIZATION --auto-start

uv run --frozen ouro mcp list --home ./.ouro-dev
uv run --frozen ouro mcp show "Remote MCP" --home ./.ouro-dev --json
uv run --frozen ouro mcp check "Remote MCP" --home ./.ouro-dev --json
uv run --frozen ouro mcp set "Remote MCP" --name "Remote tools" --home ./.ouro-dev
uv run --frozen ouro mcp remove "Remote tools" --home ./.ouro-dev
```

重复的 `--arg`、`--env`、`--inherit-env`、`--header` 和 `--credential` 会整体替换对应集合；使用
`--clear-args`、`--clear-env`、`--clear-inherit-env`、`--clear-headers` 或 `--clear-credentials` 显式清空。
stdio 与 HTTP/SSE 之间切换时，CLI 会原子移除旧 transport 的不兼容字段和凭据引用。`mcp check` 只做一次
临时连接检查，即使持久配置为 disabled 也不会改写它；连接失败退出码为 3，配置错误为 2。

stdio 中 `credential_refs` 的 key 是注入子进程的环境变量名；HTTP/SSE 中则是请求 header 名。环境变量的
值应包含完整 header 值，例如 `MCP_AUTHORIZATION="Bearer ..."`。MCP 状态由单一连接 owner 实时维护为
`starting`、`connected`、`reconnecting`、`failed` 或 `stopped`；运行中 Host 的 MCP 状态查询读取同一实时记录。
TUI 会在自身生命周期内同步该内存快照，实时更新连接中、已连接、失败和当前可用工具数量。
单个 MCP 连接失败不会进入同步启动失败，也不会阻止没有依赖该 server 的普通 Run。`inline:` 引用会以明文保存在
本地配置中，公开 CLI 输出统一显示为
`inline:***`，因此生产配置应优先使用 `env:`。
