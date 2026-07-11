# OURO Agent 第一阶段核心运行规格

- Date: 2026-07-07
- Status: Accepted / Phase 1 implemented
- Related: ../../docs/OURO_AGENT_项目概述_V0707.md

> Compaction boundary update (2026-07-11): the Phase 1 truncation implementation described in
> this document has been removed. Current behavior is defined by
> `2026-07-11-built-in-summary-compaction.md`: OURO persists and reuses built-in LLM summaries,
> supports typed strategy replacement, and never falls back to history truncation.

> Implementation boundary update (2026-07-11): 第一阶段实际 dispatch 并承诺合并语义的 hooks 是
> `after_context_build`、`before_tool_call`、`after_tool_call` 和 `on_event`。其他已声明 HookName 是
> 后续扩展保留名，不应解读为第一阶段已执行。第二阶段见
> `2026-07-11-phase-2-extensions-official-capabilities.md`。

## 1. Motivation

OURO 第一阶段需要先跑通一个可恢复、可观察、可扩展的本地智能体运行闭环。

OURO 第一阶段采用自研基础运行设施，不基于外部 Agent 框架实现核心运行闭环。核心运行规格的职责是定义并稳定 OURO 自己的概念：会话、运行、回合、消息、事件、状态、上下文视图、工具调用和人工介入。

## 2. Goals

- 提供一个 UI 无关的核心运行层。
- 支持 Interactive 和 Print / JSON 共享同一运行逻辑。
- 支持会话、运行、回合的稳定边界。
- 支持模型流式输出、工具调用、工具结果回灌。
- 支持中止、审批、挂起、恢复。
- 支持完整事件流输出和持久化钩子。
- 支持内置、可持久恢复的 LLM 摘要压缩。
- 定义稳定的运行扩展点，让插件通过 OURO Runtime Hooks 接入核心链路。
- 通过 OURO 自研 RuntimeAdapter、Provider、Tool、Event、Persistence 和 Hooks 边界组织运行闭环。

## 3. Non-goals

- 不在第一阶段实现 Desktop。
- 不在第一阶段冻结 SDK/RPC 外部协议。
- 不把摘要压缩做进核心运行层。
- 不把子智能体、浏览器自动化、复杂任务编排做进核心运行层。
- 不在核心运行层实现复杂权限系统。
- 不要求核心运行层知道最终文件存储目录结构，持久化由存储层负责。
- 不允许插件直接修改核心内部对象或依赖外部框架类型。

## 4. Module Boundary

建议模块边界：

```
ouro/
  application/
    runtime.py        # Outer Loop 应用服务，编排 session、lock、abort 与恢复入口
    runs.py           # run/session 状态转换与消息、checkpoint 的持久化边界
    events.py         # 运行事件持久化、on_event hook 和 hook failure 事件化
    session.py        # 会话应用服务，封装 SessionStore 的用例入口
    followups.py      # active run 期间收到的 follow-up input 持久化队列
  core/
    inner_loop.py     # 单个 active run 内的模型回合与工具结果回灌
    provider.py       # Provider 协议、模型元数据、流事件与 usage 契约
    results.py        # 运行结果契约
    context.py        # 最终上下文视图构建、估算与硬预算校验
    events.py         # OURO 事件类型和事件总线
    messages.py       # OURO 消息类型
    state.py          # 会话状态、运行状态、审批状态
    hooks.py          # 运行扩展点和 Hook 调度
    adapter.py        # OURO 自研 RuntimeAdapter，连接上下文视图与 Provider 协议
    errors.py         # 统一错误类型
```

这些模块名不是最终 API 承诺，但职责边界应保持。应用编排入口必须位于 `application` 层；`core` 不提供 `RuntimeService` / `SessionService` 兼容 shim，也不依赖 application、persistence、具体 provider 实现或提示词包。Provider 的抽象契约归 `core` 所有，HTTP 传输、供应商编解码和配置归外层实现。

核心层依赖：

- 模型供应商接口。
- 工具注册表与工具执行器。
- 持久化接口。
- 配置读取接口。
- 事件输出接口。
- Runtime Hooks 注册表。

核心层不依赖：

- Textual。
- PyWebview。
- FastAPI。
- 具体命令行渲染。
- 任何外部 Agent 框架的内部类型。

## 5. Core Concepts

### Session

会话是用户与 OURO 的持续对话空间。

最小字段：

- session_id。
- title。
- created_at。
- updated_at。
- workspace。
- current_model。
- status。
- archived。
- message_count。
- last_run_id。
- metadata。

会话负责长期保存用户可见历史、配置、状态、分支和运行记录。

### Run

运行是一次执行单元，从用户输入、恢复点或队列消息开始，到完成、挂起、出错或中止为止。

最小字段：

- run_id。
- session_id。
- started_at。
- ended_at。
- status。
- trigger。
- active_model。
- current_turn。
- pending_approval_id。
- abort_requested。
- error。

运行状态：

- queued。
- running。
- waiting_user。
- waiting_approval。
- completed。
- aborted。
- failed。
- recoverable。

同一会话同一时间只允许一个主运行。未来子智能体可以有子运行，但第一阶段不实现。

### Turn

回合是一次模型调用及其后续工具执行。

一个运行可以包含多个回合。若模型输出工具调用，工具执行完成后结果回灌，进入下一回合。若模型不再调用工具，运行结束。

### Message

消息是会话的用户可见历史。

第一阶段消息类型：

- user。
- assistant。
- tool_result。
- system_synthetic。

system_synthetic 必须显式标记，不能在 UI 中伪装成用户消息。

### Event

事件是运行过程的实时记录和回放依据。

事件必须包含：

- event_id。
- event_type。
- session_id。
- run_id。
- turn_id。
- timestamp。
- sequence。
- payload。

sequence 在同一个 run 内单调递增，用于流式渲染和回放排序。

## 6. Runtime Flow

运行层采用双层 Agent Loop：

- Outer Loop 由 application 层 `RuntimeService` 承担，负责会话创建/加载、run lock、active run 检测、中止信号、审批/AskUser 恢复入口和队列串行 drain。消息、checkpoint、usage、等待、完成和失败等持久化状态转换集中由 `RunLifecycle` 执行，避免编排服务同时承担状态机细节。active run 期间收到的 follow-up input 由 application 层 `FollowUpQueue` 维护。运行事件由 application 层 `RuntimeEventRecorder` 维护，统一处理事件持久化、`on_event` hook 分发以及 hook failure 的可观测事件。
- Inner Loop 由 `InnerAgentLoop` 承担，负责一个 active run 内的模型请求、流式增量事件、provider usage 记录、工具调用请求、工具结果回灌、多轮 provider/tool continuation、空响应/stream 异常/轮数上限失败判断。
- Inner Loop 不直接管理 session 生命周期、不获取 run lock、不读取 follow-up 队列，也不直接依赖具体存储实现；需要事件、持久化、暂停状态保存、工具执行或完成/失败处理时，通过显式 ports 交回 Outer Loop。

一次普通用户输入的流程：

1. 宿主提交用户输入和 session_id。
2. application `SessionService` 加载或创建会话。
3. 如果该会话已有 active run，返回现有运行，不创建第二个主运行。
4. 持久化用户消息。
5. 创建 run_id，并写入运行状态。
6. 发布 run_started 事件。
7. 组合根发现 Skills 并只渲染可用技能的元数据说明；ContextBuilder 通过显式
   `skill_instructions` 输入把它与 system prompt 组合，不能把 Skills 降格为匿名
   `base_context` 或普通会话消息。
8. 触发 after_context_build hooks，允许插件追加上下文。
9. application context preparation 在达到阈值时生成并激活摘要，再构建发送视图。
10. 调用模型供应商流式接口。
11. 将模型流式增量转换为 OURO 事件，并触发 on_event hooks。
12. 若模型请求工具，先持久化带 tool_calls 元数据的 assistant 消息，再进入工具执行流程。
13. 触发 before_tool_call hooks。
14. 若工具需要审批，运行进入 waiting_approval。
15. 审批通过后执行工具。
16. 触发 after_tool_call hooks，允许插件观察工具结果。
17. 工具结果写入消息和事件。
18. 模型完成后记录 usage，发布 model_message_completed 事件。
19. 若模型继续调用工具，进入下一回合。
20. 模型完成后写入最终 assistant 消息，运行进入 completed。
21. 发布 run_completed 事件。

## 7. Context View

上下文视图是发送给模型的数据，不等同于持久化消息历史。

输入：

- 系统提示。
- 会话消息历史。
- 当前工作区信息。
- 用户选择的资源引用。
- 技能注入内容。
- 插件上下文提供器输出。
- 当前运行状态。
- 最小本地记忆插件输出，若已启用。

输出：

- model_messages。
- system_prompt。
- tool_definitions。
- context_metadata。

`system_prompt` 是 ContextView 和 Provider 请求的原生字段，不是持久化消息，也不得由
RuntimeAdapter 临时伪装成 `system_synthetic` 消息。Provider 契约必须显式接收 system prompt，
具体 provider 再负责映射为自身协议。

Skills 注入遵循渐进披露：ContextBuilder 只接收组合根已经渲染好的可用技能元数据说明，并追加到
system prompt；完整 `SKILL.md` 仍由模型在匹配任务时按需读取。插件 `added_context` 保持为动态
synthetic context，不与系统提示和 Skills 静态能力混用。

当前内置摘要压缩遵循：

- 保留必要系统提示与最近开放用户回合。
- coverage 不得落在 assistant tool-call 与其连续 tool_result 之间。
- 不修改原始消息历史，摘要保存为独立不可变记录。
- active summary 持续作用于后续轮次和恢复后的会话。

`context_metadata` 至少记录 active compaction、coverage、token estimates、是否注入 Skills 和硬预算状态。技能发现、配置解析等
外层能力不得注入 `ContextBuilder`；组合根只向其提供已经渲染好的 `skill_instructions`，插件产生
的动态内容仍通过 hook 输入。

插件可贡献替代 `CompactionStrategy`，默认摘要实现由 OURO 内置提供。

## 8. Runtime Hooks

Runtime Hooks 是 OURO 核心暴露给插件和官方能力的稳定扩展点。它承担中间件职责，但使用 OURO 自己的概念和数据结构，不暴露外部框架 hook。

第一阶段需要定义 Hook 调度模型，即使只实现其中一部分。

### Hook Types

建议第一阶段声明以下 hook：

- before_run：运行创建后、模型调用前触发。
- after_run：运行完成、中止或失败后触发。
- before_context_build：上下文视图构建前触发。
- after_context_build：上下文视图构建后触发。
- before_model_call：调用模型前触发。
- after_model_response：模型完成一个消息后触发。
- before_tool_call：工具执行或审批前触发。
- after_tool_call：工具执行完成后触发。
- on_approval_requested：审批请求创建后触发。
- on_event：事件发布后触发。
- on_error：运行错误被捕获后触发。

### Hook Capabilities

不同 hook 允许的能力不同。第一阶段已实现并承诺的合并语义保持保守：

- after_context_build 可以追加上下文片段。
- before_tool_call 和 after_tool_call 可以观察工具调用与工具结果。
- on_event 可以观察事件。
- hook 可以返回 diagnostics，但第一阶段不允许通过 hook 覆盖工具结果、终止运行或修改模型请求。

### Hook Ordering

Hook 调度顺序：

1. 按插件来源分组：official、third_party、ai_generated。
2. 同来源内按 priority 从小到大执行。
3. 同 priority 按 plugin_id 稳定排序。

第一阶段可以先只支持 priority 默认值，但排序规则需要提前定义，避免后续插件行为不稳定。

### Failure Policy

Hook 失败不能破坏核心可恢复性。

- 官方插件 hook 失败：记录事件，按 hook 类型决定是否降级。
- 第三方和 AI 生成插件 hook 失败：记录事件，禁用本次运行中的该 hook。
- 摘要策略失败：保留旧 active compaction，记录失败并中止本次模型请求。
- on_event hook 失败：只记录诊断，不影响事件持久化。

Hook 失败必须带 plugin_id、hook_name、session_id、run_id。

### Plugin Integration Contract

插件不得直接访问 Runtime 内部可变对象。核心传给 hook 的上下文对象应是受控视图，插件返回结构化结果，由核心决定是否合并。

第一阶段稳定返回：

- added_context。
- diagnostics。

modified_view_metadata、tool_result_override、resource_refs、terminate_run 等更强能力留给后续 Runtime Hooks 详细规格，避免插件接口提前承诺未实现语义。

所有 hook 产生的可见影响都应进入事件流。

## 9. Adapter Boundary

核心运行层通过 OURO RuntimeAdapter 访问模型供应商和运行适配边界。

适配器职责：

- 把 OURO 消息转换为 provider 消息。
- 把 OURO 工具定义转换为 provider 工具定义。
- 把 provider 输出转换为 OURO 事件。
- 把 provider 工具调用请求交回 OURO 工具执行器。
- 把 provider 错误转换为 OURO 错误。
- 原样传递 ContextView 的 system prompt、messages、tools 和端点级 ModelMetadata；不得在此层重新
  发现 Skills、读取配置或推断模型能力。

适配器不得要求宿主界面理解 provider 或工具执行内部细节。

第一阶段实现 OURO 自研 RuntimeAdapter。未来如增加其他 provider 或运行后端，只要保持 OURO 核心接口不变，上层不应改动。

## 10. Error and Retry

错误分类：

- model_error。
- provider_auth_error。
- provider_rate_limit。
- tool_error。
- permission_denied。
- user_abort。
- context_too_large。
- persistence_error。
- runtime_error。

临时性模型或网络错误可以重试，但必须满足：

- 重试事件可见。
- 用户可以中止重试。
- 重试不隐藏原始错误。
- 重试次数和退避策略由配置控制。

第一阶段可以采用保守默认：

- 自动重试 0 次或 1 次。
- 只有明确标记为临时性错误才重试。
- 用户可通过命令手动重试失败运行。

## 11. Acceptance

完成本规格后，应满足：

- 可以创建会话并发送用户消息。
- 可以流式显示模型输出。
- 可以执行至少一个需要审批的工具调用。
- 可以拒绝工具调用，并把拒绝结果返回给模型。
- 可以中止运行，运行结束但会话可继续。
- 同一会话已有 active run 时，不会创建第二个主运行；queued follow-up input 会持久化并在当前运行完成后串行处理。
- 可以输出完整事件流，事件带 session_id、run_id、sequence。
- 插件可以通过 Runtime Hooks 追加上下文或观察工具结果，不依赖外部框架类型。
- Hook 失败会产生可观察事件，并按策略降级或隔离。
- 可以在不修改宿主的前提下替换底层适配器。
- 摘要后仍超出硬限制时返回明确 context overflow，不静默裁剪历史。
- provider usage 会进入运行记录，JSON 模式会输出 usage。

## 12. Resolved Decisions

- queued follow-up input 使用会话 metadata 中的持久化队列，当前运行完成后串行处理。队列消费采用 peek/ack：只有对应 user message 持久化后才确认删除，并通过 queue_id 检测崩溃窗口中的已写消息，避免队列项在消息落盘前丢失或在恢复时重复写入用户消息。
- run recoverable 状态第一阶段恢复到“可解释失败并允许重新运行”，不自动重放到工具审批点。
- 系统提示默认内容由配置层提供，核心只保留 system_prompt 注入边界。
- Skills 元数据说明通过 ContextBuilder 的显式 `skill_instructions` 边界进入 system prompt；
  `base_context` 不再作为原生 Skills 接口。
- Runtime Hooks 第一阶段只合并 added_context 和 diagnostics，更强返回能力留给后续规格，且不得绕过工具权限。
