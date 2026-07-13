# 连接与扩展计划

状态：进行中（MCP transport 已完成；插件生命周期未开始）

## 目标

在现有 Tool、Hook、Plugin contribution 和 MCP 状态边界上，实现真实 MCP 连接和可审计插件生命周期。
不提供不可信代码沙箱承诺。

## 前置条件

- Application 配置门面能够管理扩展配置和 credential refs。
- RuntimeSupervisor 为长生命周期 server、订阅和关闭提供进程所有权。
- ToolExecutor、HookRegistry 和 PluginLoader 当前测试保持绿色。

## 工作流 A：MCP transport

### A1. 协议与 transport 分层

- [x] 定义 MCP client port、capabilities、server session 和 protocol error。
- [x] 实现 stdio transport，明确进程、stdin/stdout、stderr 和关闭所有权。
- [x] 实现 HTTP transport；按协议需要接入 streaming/SSE。
- [x] 所有请求支持 timeout、cancel 和 request id correlation。

### A2. 握手与工具发现

- [x] initialize/version/capability negotiation。
- [x] tools list 与分页/变化通知。
- [x] MCP schema 到 `ToolDefinition` 的严格转换。
- [x] server/tool identity 冲突规则。

### A3. 调用与错误

- [x] ToolInvocation 到 MCP call；content/result 到 `ToolResult`。
- [x] 协议错误、server error、timeout、disconnect 和 invalid schema 分类。
- [x] 取消本地请求任务并终止等待；协议请求与 transport 取消由官方 SDK 管理。
- [x] 未知副作用遵循现有 tool batch recovery 规则。

### A4. 生命周期

- [x] start/stop/restart/reconnect backoff。
- [x] credential resolution 和环境变量白名单。
- [x] stderr/diagnostic 日志脱敏。
- [x] Supervisor shutdown 关闭所有 transport 和子进程树。

### A5. Host 装配

- [x] 默认 Run 与 Interactive 从 `mcp.servers` 装配 auto-start server 到共享 ToolRegistry。
- [x] durable 工具审批恢复前重新连接对应 MCP server。
- [x] JSON Host 保持 stdout 机器输出纯净，启动失败只写脱敏 stderr warning。
- [x] Host 退出移除动态工具并关闭 transport/stdio 子进程。

### A6. 配置管理 CLI

- [x] `mcp list/show/set/remove` 通过类型化配置更新 API 管理 server。
- [x] transport 切换、集合替换/清空和 credential 公开输出具备明确契约。
- [x] `mcp check` 执行一次真实连接、握手与工具发现，并在结束后关闭资源。
- [x] 配置错误、连接失败与 JSON/文本输出具有稳定退出码和脱敏行为。

## 工作流 B：插件生命周期

### B1. Manifest 与来源

- [ ] 收紧 plugin id、version、entrypoint 和 capability 校验。
- [ ] 定义 local/official/third-party 来源记录和安装 metadata。
- [ ] 权限声明覆盖 filesystem、network、process 和贡献类型。

### B2. 安装与授权

- [ ] 本地/Git 来源解析、staging、文件限制和符号链接策略。
- [ ] 安装前展示权限与配置需求。
- [ ] 未授权插件保持 `needs_authorization`，不能 import entrypoint。
- [ ] update/remove 具备 backup 和回滚。

### B3. Entrypoint 加载

- [ ] 独立 adapter 负责 import/instantiate，不让 import 细节进入 registry。
- [ ] contribution 构建与注册分开；单项失败可诊断。
- [ ] 资源关闭协议覆盖监听器、MCP server 和后台任务。
- [ ] builtin id、工具名、命令名和策略 id 冲突规则稳定。

### B4. 配置与 CLI

- [ ] application DTO：discover/list/show/install/authorize/enable/disable/update/remove。
- [ ] CLI 只调用 facade。
- [ ] JSON 输出不暴露 entrypoint 环境、密钥或完整异常 payload。

## 测试矩阵

- stdio server 正常握手、工具调用、timeout、崩溃和取消；
- HTTP 正常/错误响应、断线和重连；
- schema 不合法、重复工具和 server identity 冲突；
- 插件 manifest 错误、未授权、entrypoint import 失败；
- 安装中断、更新回滚和移除失败；
- Hook/Tool contribution 失败不破坏其他插件或主运行时；
- shutdown 后无遗留进程、task 或连接。

## 风险与控制

- 协议对象泄漏：所有转换停留在 `ouro.mcp`。
- 插件权限名存实亡：未授权前绝不 import；工具仍经过 ToolExecutor。
- 生命周期分散：transport 和插件资源统一注册到可关闭 owner。
- 过早沙箱化：先明确可信同进程模型，不引入不完整 sandbox。

## 退出条件

- 至少一个 stdio 和一个 HTTP 测试 server 完整通过发现、调用、取消和关闭。
- 插件可安装、授权、加载、停用、升级和回滚，且 provenance 可查询。
- MCP/插件错误不改变 core 类型和 RuntimeService 事务边界。
- 文档明确当前信任与安全限制。
