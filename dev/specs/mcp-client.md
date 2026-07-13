# MCP 客户端规格

状态：当前

## 边界与依赖

`ouro.mcp` 是 MCP Python SDK 的唯一适配边界。SDK 的 session、capability、tool、content 和异常对象不得
进入 core、application 或 `ouro.tools`。边界内使用 `MCPServerInfo`、`MCPRemoteTool` 和
`MCPRemoteToolResult` 完成转换，边界外只暴露 OURO Tool/ToolResult 和生命周期记录。

生产依赖使用官方 MCP Python SDK 1.x 稳定线，并限制 `<2`，避免 2.x 预发布及未来稳定版在未迁移时改变
transport 契约。stdio、Streamable HTTP 和兼容性 SSE 均由 SDK 实现；SSE 是 legacy transport，新配置优先
使用 Streamable HTTP。

## 配置与身份

`MCPServerConfig` 按 transport 严格校验：

- stdio 必须提供 command，只允许 args/cwd/env；
- HTTP/SSE 必须提供 http(s) URL，不接受进程字段；
- stdio 只继承显式白名单环境变量；`credential_refs` 的 key 是注入的环境变量名；
- HTTP/SSE 的公开 headers 与凭据 header 分离，`credential_refs` 的 key 是 header 名；
- timeout、健康检查、重连次数和指数退避均有正数或上界校验；
- MCP 工具默认使用 `approval` 权限，不把 server annotation 当作可信授权依据。

配置持久化在共享配置文件的 `mcp.servers`，由 `MCPConfigStore` 完整校验后原子替换，且不覆盖其他配置
分区。公开配置输出必须隐藏 inline credential；运行错误和 diagnostic 必须用已解析值脱敏。

远程工具公开名固定为 `<server_id>__<remote_tool_name>`。名称始终带 server 身份，因此不同 server 的同名
工具不依赖连接顺序；非法名称、重复名称、过长 identity 和非法 JSON Schema 使该 server 启动或刷新失败。

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
- health ping 或工具连接错误触发有界指数退避重连；显式 reconnect 执行完整 stop/start；
- `stop`、`close` 和 RuntimeSupervisor shutdown 退出 session、HTTP client、stdio 管道与子进程；
- `MCPRuntimeResource` 是 MCPManager 到 RuntimeSupervisor 通用受管资源协议的适配器。

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
- `tests/mcp/test_sdk_integration.py`
- `tests/tools/test_tools.py`
- `tests/application/test_supervisor.py`
