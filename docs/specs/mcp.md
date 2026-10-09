# MCP 客户端规格

> 文档类型：现行规格

## 边界与依赖

`ouro.mcp` 是 MCP Python SDK 的唯一适配边界。SDK 的 session、capability、tool、content 和异常对象不得
进入 core、application 或 `ouro.tools`。边界内使用 `MCPServerInfo`、`MCPRemoteTool` 和
`MCPRemoteToolResult` 完成转换，边界外只暴露 OURO Tool/ToolResult 和生命周期记录。

读取配置和构造 Manager 不加载 SDK。默认客户端 factory 在连接 owner 中按需加载，导入工作在线程中执行；
加载期间停止的 server 不再打开 transport。`ouro.mcp.SDKMCPClientFactory` 保留显式导入入口，测试和其他
部署可注入 `MCPClientFactory`，无需依赖默认 SDK 的生命周期。

生产依赖使用官方 MCP Python SDK 1.x 稳定线，并限制 `<2`，避免 2.x 预发布及未来稳定版在未迁移时改变
transport 契约。stdio 和 Streamable HTTP 使用 SDK transport；legacy SSE 的 GET/POST 数据通道由
`ouro.mcp.sse_transport` 适配为 SDK session stream，协议编解码与 session 仍由 SDK 负责。该边界保证 SSE
任一方向失败都进入 OURO 生命周期，而不是由依赖直接写进程日志。新配置优先使用 Streamable HTTP。

## 配置与身份

`MCPServerConfig` 按 transport 严格校验：

- stdio 必须提供 command，只允许 args/cwd/env；
- HTTP/SSE 必须提供 http(s) URL，不接受进程字段；
- stdio 只继承显式白名单环境变量；`credential_refs` 的 key 是注入的环境变量名；
- HTTP/SSE 的公开 headers 与凭据 header 分离，`credential_refs` 的 key 是 header 名；
- timeout、健康检查、重连次数和指数退避均有正数或上界校验；
- MCP 工具默认使用 `approval` 权限，不把 server annotation 当作可信授权依据。
- `auto_start` 默认为 false；只有显式启用的 server 才进入默认 Host 的工具投影，避免读取配置即执行进程或
  建立网络连接。

配置持久化在 `config/mcp.json` 的 `mcp.servers`，由 `MCPConfigStore` 完整校验后原子替换，与其他配置域
独立保存。注册表键是 OURO 自动分配并持久化的内部 ID，名称变更不改变该 ID。用户配置只接受名称与连接、
权限等业务字段；名称去除首尾空白后必须非空且唯一，按区分大小写的精确名称匹配。配置项中的 `server_id`
字段不被接受。公开配置输出不包含内部 ID，且必须隐藏 inline credential；运行错误和 diagnostic 必须用已解析值脱敏。

`MCPConfigStore.update` 接受类型化的 `MCPServerConfigUpdate`；内部 ID 为 `None` 时创建并分配 ID，
否则只更新已有记录。`set_by_name` 在同一原子操作内按名称查找并创建或更新。两者负责完整候选校验、
名称唯一性检查和单次持久化；校验失败不写入文件。集合更新采用整体替换语义，显式空集合表示清空；
stdio 与远程 transport 互换时，由配置层清除旧 transport 的进程字段、URL/header 和 credential refs，
CLI 不重复实现该规则。校验错误只公开字段位置与
错误消息，不包含 Pydantic 输入值。

CLI 管理面为 `mcp list/show/set/remove/check`，操作参数使用显示名称，`set --name` 设置新名称；不提供 ID
查找或配置入口。TUI 复用分组内联编辑组件，在 DETAILS 分组编辑 Display name，菜单、确认框与通知使用
该名称。应用视图保留 ID 供进程内关联，序列化时排除该字段。所有配置展示统一经过
`mcp_config_to_public_dict`；`check` 用临时的 enabled、无重连副本执行单次真实握手和工具发现，
不修改 durable 配置，并在返回前关闭 Manager。
检查结果中的工具列表使用服务端工具原名。配置错误退出码为 2，连接检查失败为 3。

工具注册表与模型调用协议使用 `<server_id>__<remote_tool_name>` 作为内部工具地址，保证不同 server 的
同名工具不依赖连接顺序，且 server 改名不改变调用关联。工具显示名称由 server 配置名称与工具标题
（缺省为工具原名）组成。非法名称、重复名称、过长工具地址和非法 JSON Schema 使该 server 启动或刷新失败。

## 生命周期所有权

每个 server 由一个 asyncio owner task 独占 transport context。只有 owner task 打开和退出 SDK/AnyIO context，
避免 `start` 与 `stop` 来自不同 Host task 时跨任务退出取消域。状态机为：

```text
configured/disabled
       |
       v
   starting ----failure----> reconnecting ----exhausted----> failed
       |                          |
       v                          |
   connected <--------------------+
       |
       v
    stopping -> stopped
```

- `start` 幂等等待同一个启动结果；活动 server 必须 stop 后才能重配；
- initialize 完成版本/能力协商，随后完整消费 tools/list pagination；
- list-changed notification 只唤醒 owner，由 owner 发起发现，不能在 SDK receive loop 内等待响应；
- ToolRegistry 使用原子 contribution replacement，刷新失败保留刷新前的完整 registry；
- SDK receive loop 的连接丢失、health ping 或工具连接错误都会唤醒 owner 并触发有界指数退避重连；
  legacy SSE 的读取或 POST 状态失败也进入同一 session stream。长连接读取没有被动 read timeout，存活性统一
  由 owner 的有界 ping 监督；初始消息端点等待和每次 POST 仍受请求 timeout 约束。显式 reconnect 执行完整
  stop/start；
- `stop`、`close` 和 RuntimeSupervisor shutdown 退出 session、HTTP client、stdio 管道与子进程；
- legacy SSE transport 是两对 AnyIO memory stream 的创建者，退出时必须关闭全部 send/receive 端点，不能把
  接收端留给 SDK 对象析构器回收；
- `MCPRuntimeResource` 是 MCPManager 到 RuntimeSupervisor 通用受管资源协议的适配器。

## Host 装配

`MCPRuntimeResource.from_config(root, registry)` 是配置到运行时的权威装配入口：读取 `mcp.servers`，把所有
server 配置交给 Manager，并把发现工具投影到 RuntimeService 正在使用的同一个 ToolRegistry。

- CLI `run --print` / `run --json` 在单次 Run 外持有 MCP resource context；
- TUI 由 LocalApplication 持有 MCP context，退出时等待资源关闭；
- `approve` 在恢复 durable MCP 工具批次前重新装配并连接 server；
- RuntimeSupervisor Host 把 MCPRuntimeResource 作为 `RuntimeManagedResource` 注入；
- 单个 auto-start server 连接失败保留 failed record 并输出脱敏 warning，不阻止其他 server 或普通 Run；
- Host 退出后动态 MCP 工具从 registry 移除，不留下失效 handler。

只调用同步的 `build_default_runtime()` 会构建 RuntimeService 本体；执行型 Host 还必须通过
`build_default_mcp_runtime(runtime)` 或等价的 MCPRuntimeResource/Supervisor 装配管理异步资源。

## 调用、取消与错误

ToolInvocation 只把远程原名和 arguments 交给 MCP session。SDK 提供 JSON-RPC request id correlation 和
request timeout；OURO 在 ToolExecutionContext abort 时取消本地等待任务，ToolExecutor 将其作为 aborted
结果处理。远端 `isError` 保留为 ToolResult error；连接、协议、timeout、disconnect 和 schema 错误在 MCP
边界分类，之后由工具执行边界隔离，不能改变 durable Run 事务。

文本 content 合并为模型可读文本，structuredContent 保存在 `ToolResult.structured`。图片和音频二进制不复制
到文本上下文，只保留省略标记和 content block 计数，避免无界放大。

## 验收证据

- `tests/mcp/test_configuration.py`
- `tests/mcp/test_manager.py`
- `tests/mcp/test_sse_transport.py`
- `tests/mcp/test_sdk_integration.py`
- `tests/tools/test_tools.py`
- `tests/application/test_supervisor.py`
- `tests/cli/test_run_cli.py`
- `tests/cli/test_approval_commands.py`
- `tests/cli/test_mcp_commands.py`
