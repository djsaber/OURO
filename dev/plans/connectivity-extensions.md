# 连接与扩展计划

状态：进行中（MCP transport 已完成；插件生命周期未开始）

## 目标

在现有 Tool、Hook、Plugin contribution 和 MCP 状态边界上，实现真实 MCP 连接和可审计插件生命周期。
不提供不可信代码沙箱承诺。

## 前置条件

- Application 配置门面能够管理扩展配置和 credential refs。
- RuntimeSupervisor 为长生命周期 server、订阅和关闭提供进程所有权。
- ToolExecutor、HookRegistry 和插件系统当前测试保持绿色。

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

权威契约见[插件系统目标规格](../specs/plugin-system.md)。以下里程碑必须依次建立证据；后续里程碑不能
通过临时 import 或直接 registry mutation 绕过前一阶段。

### B1. 包、来源与不可变 Store

- [ ] 新增 `.ouro-plugin/plugin.json` schema v1；安全字段拒绝 unknown。
- [ ] 严格校验 plugin id、SemVer、OURO/Plugin API range、entrypoint、相对路径、文件类型和 capability。
- [ ] 实现 local/Git/archive source provider；ref 固定为 commit，来源记录由 installer 生成且不可自报。
- [ ] materialize 受文件数、单文件/总大小、压缩比限制；拒绝 symlink、reparse、越界路径和安装脚本。
- [ ] 规范化文件树 digest，同文件系统 staging/rename；store 以 digest 不可变保存，数据目录独立。
- [ ] application/CLI 完成 validate/list/show/install/remove 和 dry-run；此阶段绝不 import/启动插件。

### B2. 授权、配置与 contribution ownership

- [ ] 将 capability、Host-service permission 和 code trust 建模为三个独立记录和 fingerprint。
- [ ] 权限请求包含稳定 id、scope 和 reason；credential 只保存 ref，公开 DTO 全部脱敏。
- [ ] 未满足配置、code trust 或 grant 时不得 import entrypoint、启动 worker 或 plugin MCP。
- [ ] Tool/Hook/command/compaction registry 支持 owner-aware replace/unregister，builtin identity 永久保留。
- [ ] application `ContributionCoordinator` 预校验完整 contribution set 并原子提交/回滚。
- [ ] permission/capability/runtime/source 扩大使旧授权失效；per-tool auto grant 与插件授权分离。

### B3. In-process runtime 生命周期

- [ ] 建立独立 `ouro-plugin-sdk`/`ouro.sdk.plugins.v1`，不把 core/application 内部类型暴露为 API。
- [ ] adapter 负责 import/factory；`prepare` 不发布副作用，`start/stop` 由 plugin owner 管理且 stop 幂等。
- [ ] `PluginContext` 只提供按 grant 裁剪的 service ports、私有数据、受监督 task 和脱敏 diagnostic。
- [ ] `python-inprocess` 在授权 UI/DTO 中明确 full trust，不声称权限声明可限制直接 Python/OS 调用。
- [ ] `LocalApplication` 在接受 Run 前启动已授权插件，并在 runtime/Supervisor 关闭时按依赖逆序回收。
- [ ] enable/disable/restart/check 完整撤销 contribution、task、MCP、listener 和连接；插件失败不阻止 Host。

### B4. Durable generation、更新与回滚

- [ ] 每次成功 activation 产生不可变 generation 和 owner lease；新 Run 获取 extension catalog snapshot。
- [ ] 工具调用、approval、Hook/compaction selection 持久化 plugin/version/digest/generation/contract hash。
- [ ] side-by-side 安装候选；权限 diff 未授权时旧 generation 继续服务，新版本不激活。
- [ ] 更新只影响新 Run；旧 generation drain 后关闭，不能原地 reload 到运行中 Run。
- [ ] rollback 使用本地 immutable digest；数据迁移前备份并验证 rollback compatibility。
- [ ] exact digest 缺失的 durable recovery 返回稳定错误，绝不调用同名新工具替代。

### B5. Worker runtime 与社区兼容

- [ ] 实现协商版本的双向 JSON-RPC/stdio worker protocol，stdout 只承载协议，stderr 限流脱敏。
- [ ] 每个 digest 独立环境；Python 仅接受带 hash 的预构建 wheel，不执行 sdist/build/lifecycle script。
- [ ] worker 请求具备 timeout、cancel、output limit、restart backoff/circuit breaker 和进程树关闭。
- [ ] 文档/UI 明确 worker 提供崩溃/依赖隔离而非 OS sandbox。
- [ ] Agent Skills 与 MCP 原生复用；Claude/Codex 包显式导入可映射资源并输出 CompatibilityReport。
- [ ] Pi/OpenCode 只提供 Skills/MCP 提取或 worker/MCP porting report，不透明执行未知 JS Hook。

### B6. 依赖与 Catalog

- [ ] plugin dependency resolver 固定版本/digest lock graph，拒绝 cycle，按拓扑激活和逆序关闭。
- [ ] active dependent 和 durable lease 阻止 remove；orphan dependency/store GC 不默认删除 plugin data。
- [ ] Catalog 只解析为不可变来源，不自动构成 trust root；publisher/signature policy 与来源索引分离。
- [ ] 只有出现真实分发和多设备需求后才产品化 Catalog/签名，不阻塞 local/Git 基础闭环。

## 测试矩阵

- stdio server 正常握手、工具调用、timeout、崩溃和取消；
- HTTP 正常/错误响应、断线和重连；
- schema 不合法、重复工具和 server identity 冲突；
- 插件 manifest 错误、未授权、entrypoint import 失败；
- traversal/symlink/archive bomb、安装中断、digest mismatch、更新回滚和移除失败；
- 未授权前 import side effect 证明代码从未执行；权限扩大使旧 grant 失效；
- Hook/Tool contribution 任一必需项失败时 owner 零发布，且不破坏其他插件或主运行时；
- 更新期间旧 Run 固定旧 generation，新 Run 使用新 generation；pending tool 只恢复 exact digest；
- worker crash、协议污染、反复重启、取消和强制关闭；
- community import 对支持、转换、忽略和不兼容资源生成类型化报告；
- shutdown 后无遗留进程、task 或连接。

## 风险与控制

- 协议对象泄漏：所有转换停留在 `ouro.mcp`。
- 插件权限名存实亡：未授权前绝不 import；工具仍经过 ToolExecutor。
- 进程隔离被误解为安全沙箱：in-process 明示 full trust；worker 只承诺崩溃/依赖隔离。
- 生命周期分散：transport 和插件资源统一注册到可关闭 owner。
- Run 中途版本漂移：所有扩展通过 immutable generation snapshot 和 durable contract hash 解析。
- 伪社区兼容：只复用 Skills/MCP 等共同协议，其余输出 CompatibilityReport，不静默加载。

## 退出条件

- 至少一个 stdio 和一个 HTTP 测试 server 完整通过发现、调用、取消和关闭。
- 插件可安装、授权、加载、停用、升级和回滚，provenance/digest/generation 可查询。
- 未授权代码从未 import；禁用/撤权/卸载撤销 owner 的全部 contribution 和长生命周期资源。
- 更新不改变进行中 Run 的插件版本，durable recovery 不以同名新 contribution 代替旧版本。
- Agent Skills/MCP 兼容子集有真实 round-trip；不可兼容社区资源具有逐项诊断。
- MCP/插件错误不改变 core 类型和 RuntimeService 事务边界。
- 文档明确当前信任与安全限制。
