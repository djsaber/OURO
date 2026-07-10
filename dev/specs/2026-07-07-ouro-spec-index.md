# OURO Agent 规格文档索引

- Date: 2026-07-07
- Status: Accepted / Phase 1 audited; Phase 2 proposed
- Related: ../../docs/OURO_AGENT_项目概述_V0707.md

## 1. 目的

本文档是 OURO Agent 第一批开发规格的索引和阅读顺序说明。

规格文档的目标不是重复项目概述，而是把项目概述中的产品边界转化为后续实现可以遵循的模块职责、数据边界、事件边界和验收标准。

2026-07-11 完成了第一阶段实现审计。第一阶段规格中曾提前描述、但 Resolved Decisions 和代码只保留
框架的 resource、完整 MCP 和插件 entrypoint 能力，已明确划入第二阶段。审计记录见
`../audits/2026-07-11-phase-1-completion-audit.md`。

## 2. 第一批规格

建议按以下顺序阅读：

1. 2026-07-07-phase-1-core-runtime.md
   - 定义第一阶段核心运行层。
   - 说明会话、运行、回合、事件、上下文和 OURO 自研运行边界。

2. 2026-07-07-local-persistence-and-events.md
   - 定义本地可阅读文件持久化。
   - 说明消息、事件、会话状态、资源引用、恢复和数据维护。

3. 2026-07-07-tools-permissions-hitl.md
   - 定义基础工具、工具权限、人工审批、AskUser、取消和恢复。

4. 2026-07-07-models-credentials-config.md
   - 定义模型供应商抽象、凭据引用、配置优先级和连通性检查。

5. 2026-07-07-plugins-mcp-skills.md
   - 定义插件、Runtime Hooks、MCP 生命周期、Agent Skills 和官方能力扩展边界。

6. 2026-07-07-interactive-print-json.md
   - 定义第一阶段运行模式：Interactive 和 Print / JSON。
   - 明确 SDK、RPC、Desktop 后置，但必须共享核心运行层。

## 3. 第一阶段范围

第一阶段实现目标：

- 单用户本地运行。
- Interactive 基础交互入口。
- Print / JSON 一次性运行模式。
- 基础模型供应商和凭据配置。
- Read / Write / Edit / Bash / AskUser 基础工具。
- 工具审批、中止和恢复。
- assistant tool-call 消息持久化，保证工具结果回灌历史链路完整。
- 本地可阅读文件持久化。
- 完整事件流长期保存。
- provider usage 进入运行记录和 JSON 输出。
- 内置最简会话截断压缩。
- 基础插件加载与一致授权模型。
- Runtime Hooks 的稳定扩展点。
- 已有 active run 时不创建第二个主运行；queued follow-up input 持久化并串行处理。
- 会话基础元数据和列表展示。

第一阶段不实现：

- Desktop。
- SDK/RPC 稳定协议。
- 子智能体。
- 浏览器自动化。
- 沙箱隔离。
- 企业级权限隔离。
- 云同步。
- 插件市场。
- 摘要压缩核心内置。

摘要压缩可以作为官方插件后续实现；插件失败、不可用或超时时，核心降级为会话截断压缩。

## 4. 全局设计约束

1. OURO 自研基础设施

   OURO 第一阶段自主构建 RuntimeAdapter、Provider、Tool、Event、Persistence 和 Hooks 等基础运行设施。所有上层模块必须依赖 OURO 自己的稳定接口。

2. 本地可阅读优先

   第一阶段默认持久化使用 JSON、YAML、JSONL 等本地可阅读文件。SQLite 是可选同等职责后端，不作为第一阶段默认依赖。

3. 完整事件默认保存

   事件流默认长期完整保存，不做额外脱敏、采样或敏感内容过滤。数据清理由用户主动触发。

4. 权限模型保持简单

   工具权限区分自动执行和用户批准。插件权限区分读写能力和网络访问能力。不引入复杂组织、角色或策略语言。

5. 插件属于 OURO 系统

   不存在项目插件或全局插件划分。插件来源只分为官方插件、第三方插件、AI 自主生成插件。

6. 宿主共享核心

   Interactive、Print / JSON、未来 Desktop、SDK、RPC 都必须共享同一套核心运行逻辑。

## 5. 文档格式约定

每篇规格尽量包含：

- Motivation：为什么需要这个规格。
- Goals：本规格要解决什么。
- Non-goals：本规格明确不解决什么。
- Design：模块、数据、流程或接口设计。
- Acceptance：实现完成后应满足的验收标准。
- Open questions：需要后续确认的问题。

这些文档是开发规格，不是用户文档。可以包含模块名、数据形状和事件名，但不要求冻结最终 Python API。

## 6. 后续深化候选

后续可以继续补充：

- Desktop 多会话交互规格（第二阶段子规格已覆盖）。
- SDK/RPC 协议规格。
- 官方摘要压缩插件规格（第二阶段先定义最小版本，后续可独立深化）。
- 最小本地记忆插件规格（第二阶段先定义最小版本，后续可独立深化）。
- 会话分支和回放规格（第二阶段子规格已覆盖）。
- 资源、附件和生成文件管理规格（第二阶段子规格已覆盖）。
- 插件开发与 AI 生成插件确认规格。
- Runtime Hooks 详细接口规格。
- 子智能体官方插件规格。

## 7. 第二阶段规格

第二阶段已形成可执行规格和计划，当前状态为 Proposed / not started：

1. `2026-07-11-phase-2-personal-workflow.md`
   - 第二阶段总范围、application facade、RuntimeSupervisor、迁移、配置优先级与跨模块验收。
2. `2026-07-11-phase-2-sessions-resources-desktop.md`
   - session catalog、搜索、branch/replay/export、ResourceRecord、Textual Interactive 和
     PyWebview/FastAPI Desktop。
3. `2026-07-11-phase-2-extensions-official-capabilities.md`
   - workspace trust、插件、完整 hooks、真实 MCP、Skills/model UX、工具批次和官方插件。
4. `../plans/2026-07-11-phase-2-implementation-plan.md`
   - 按依赖、测试门禁和提交边界拆分的实施计划。
