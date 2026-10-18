# 上下文与压缩规格

> 文档类型：现行规格

## 目标与范围

上下文系统从 canonical messages 构建一次 Provider 请求视图，在不改写历史的前提下应用系统提示词、
Skills 指令、恢复投影、摘要压缩和模型 token 预算。

## ContextView

`ContextView` 包含：system prompt、messages、tools、token estimate、budget、overflow 状态和 metadata。
`ContextBuilder` 是纯构建器，不执行 Provider、文件或 SessionStore I/O。

`ContextBuilder` 接收注册工具定义，根据 `batch_behavior="exclusive"` 为请求副本的 description 追加统一的
单独调用约束，说明混合调用会在执行前整批拒绝。其他工具的描述保持原样；`execution_mode` 是宿主调度设置，
不生成额外模型指令。投影不修改注册定义、参数 schema 或执行契约哈希。
`ContextView.tool_definitions` 保存已呈现的工具定义，token 估算、Provider 请求和请求快照使用同一份内容。
OpenAI Chat Completions、OpenAI Responses 与 Anthropic Messages 均由公共上下文路径取得描述，codec 只转换协议格式，
不生成调用规则。工具目录中的结构化契约保持原样。

基础系统提示词保持原始内容；有 Skills 指令时使用稳定空行拼接。Context Provider 返回
`target="system_prompt"` 的 ephemeral fragment 时，只有 fragment 显式提供 `semantic_tag`，Core 才使用统一
semantic envelope renderer 包裹原始正文；未提供标签时直接拼接正文。投影按稳定顺序位于基础提示词和 Skills
之后，进入 token 估算和请求快照，但不构造消息。恢复投影只在模型上下文中标记 incomplete assistant 尝试，
不把它升级为 canonical 完成消息。

## 最近一次 Provider 请求快照

最近请求快照记录已准备请求的事实。追加响应或更新 Harness 状态不会使它失效，进行中的请求仍使用原身份
结算 Provider 用量。后续请求准备时替换快照；撤销历史则清除属于已撤回历史的快照。

`ContextSnapshotRecord` 保存最近一次真正准备并交给 Provider 的请求，包括 system prompt、请求级 ephemeral
Context Provider 注入、压缩/恢复后的 messages、tools、metrics、路由和实际 Provider 内容 digest。
`session context` 读取该记录的 metrics；`session context --content` 与 TUI 的上下文详情通过
`SessionService.context` 还原并校验同一记录的完整 `ProviderContext`。没有发生过 Provider 请求时应明确返回
“尚无已准备上下文”，不能用 prospective projection 冒充真实请求。

查询不执行 Context Provider、Observer 或自动压缩，也不写入 canonical history。手动压缩只激活新的
`CompactionRecord`，不得改写最近请求快照；新摘要会在下一次模型请求准备后自然进入新的快照。

工具描述按请求准备时的完整文本保存。快照读取直接使用该文本，不根据当前契约呈现逻辑追加或更新说明；
没有附加说明的已保存描述也原样保留。重复读取和协议编码不得重复追加约束。

`ContextSnapshotRecord` 只保存实际 Provider 请求的单一当前投影。语义包络完全由 source message/fragment 上是否
存在 `semantic_tag` 决定；Store 不猜测旧投影方式。
schema 5 固定实际发送的文本、状态、图片、音频和视频内容，以及当时端点的三种输入能力；查询不重新读取文件，
不依据当前模型或阈值重新投影。文件预览和原生媒体直接保存在骨架 content 中，文件变化不修改过去的内容；
工具读取结果保存在各自的 tool_result 中。附件骨架和正文按共享输入批次进入同一压缩帧，详见[附件规格](attachments.md)。

## Token 预算

`ContextBudget` 只保存一次请求的 `context_window` 与可空 `max_output_tokens`。输出预留为有限上限或 0，输入容量为窗口减预留；容量必须大于 0。主会话、独立生成、摘要及 TUI 消费同一模型，不另设预留配置。空值只表示 OURO 不配置输出上限。

`ContextMetrics` 保存 `estimated_input_tokens`、`estimated_fixed_tokens`、可选 `budget` 及 estimator 身份。输入预算、剩余容量、利用率和 `over_budget` 是派生属性，不重复持久化。请求快照中的 `budget` 保存当时最终请求的窗口和上限；无预算的纯投影可以仅记录输入估算。

估算覆盖 system prompt、message content、typed tool fields 和 tool definitions。默认 estimator 是 Provider 无关的保守近似。原生协议附件先按当前 provider/model 和执行 history_epoch 筛选，再进入估算；不遍历或解码私有 blocks。不匹配附件从请求投影移除，canonical 正文、工具帧和持久化附件保持完整。协议及认证 scope 的兼容性由 codec 在实际请求边界判断。

`over_budget` 只判断本次输入估算是否超过输入容量，不预测生成长度。上下文超预算时不能静默截断 message 或拆开工具交换。`RunBudgetPolicy` 负责整个 Run 的累计软额度，用实际用量记账；其输出额度不作为单次请求或摘要的输出参数。

## 原子上下文帧

- 同一 `turn_id` 的 assistant 工具请求与结果属于一个模型回合帧。
- 其前的用户消息加入随后的 identified frame。
- 无 `turn_id` 的普通消息按用户请求分组。
- 含未完成工具请求的后缀整体受保护。

压缩计划只能覆盖 canonical 前缀，并至少保留最新完整帧。

## 压缩配置

本地组合的专用压缩 adapter 在请求边界经共享 model catalog 解析，认证引用、驱动和连接配置变化会生效；
普通凭据刷新复用现有 adapter。已经绑定的路由失效以及带结构化错误码的服务失败为 terminal，
不切换模型、账户或 fallback strategy；managed 未分类传输/响应失败也提供通用错误码。
相应契约见[请求认证规格](request-authentication.md)。

`CompactionConfig` 包含 primary/fallback strategy、触发与目标比例，以及内置摘要策略的模型路由与请求参数。摘要的 `max_output_tokens` 缺失或为 `null` 均表示不限制，可以在当前 Session 模型、默认模型或专用端点上配置。专用端点在启动时尚未解析成功可回退当前 Session runtime endpoint，并在 metadata 中记录原因。

所有摘要路由应用同一份摘要请求配置。端点输出配置不是摘要上限的能力边界；摘要空值不继承端点或主请求上限。路由选定后创建独立 adapter 副本，不修改共享模型。有限上限必须为该路由留下输入空间，协议负责拒绝无法编码的请求。

## 摘要执行

框架与生成策略的契约是输入材料和规范化的摘要正文。`CompactionRequest` 提供会话/运行身份、Harness 视图、触发方式、已有摘要及本次材料；不传入主请求预算或生成额度。策略返回 `CompactionArtifact.summary`，可附带用量、请求审计与策略 metadata。插件可自行选择提示词、模型和响应格式，再将结果转换为摘要正文。

框架执行顺序：

1. 验证 active compaction lineage，选择可覆盖的 canonical 前缀并保留受保护帧。
2. 按 `输入预算 × target_ratio - 固定内容估算 - 已有摘要投影估算` 规划尾部容量，最低为 0；目标比例是软目标。
3. 调用生成策略，得到非空摘要正文。
4. 将实际摘要和保留内容重新投影为完整主上下文，校验输入容量。
5. 校验历史前缀和代次后原子提交并激活 `CompactionRecord`。

提示词、原始输出格式、解析、分块和格式相关重试由生成策略负责。框架不检查 `<summary>` 标签，不依据输出参数预测摘要正文大小，不要求插件沿用内置提示词。

内置 `builtin.llm_summary` 位于 `application/summary_compaction.py`，将已有摘要与新增材料序列化为独立模型请求，不进入主 agent 循环，不创建持久化 Session，不携带普通工具。输入使用 `role=user, semantic_tag=conversation-to-summarize`；估算包括提示词、标签、转义后的正文及已有摘要。其提示词要求完整简洁的摘要，不依赖数值输出预算模板。当前内置解析器提取非空 `<summary>` 正文，格式规则与提示词一起维护。

每次滚动摘要使用自身请求的输入容量。选块按完整 frame 进行，对候选内容最多序列化一次，用与默认 estimator 一致的可加字符度量累计容量，保持线性复杂度。单帧或提示词与已有摘要无法容纳时明确失败。内置策略仅对现有可识别的未闭合、被截断摘要执行一次缩块重试，不循环扩大额度。

公共模型执行负责完成判定：`length`、过滤、缺少完成事件等失败不能因摘要标签闭合而视为成功。通用请求审计记录输出参数（可以为 `None`）、原始正文和思考、完成原因、用量及错误；不包含标签开闭状态。已发生的用量仍归属原 Session/Run 并只记账一次；取消和失败不激活摘要。

## CompactionRecord

当前记录 schema 为 2。记录至少包含：

- compaction/session/previous identity；
- strategy id/version 和 prompt version；
- trigger；
- summary；
- covered message ids；
- source digest；
- usage、model route 和 metadata。

激活时 message id 前缀和 digest 必须同时匹配当前 canonical history，否则抛出 `CompactionStateError`。

## 自动与手动压缩

- 自动压缩在上下文达到 trigger ratio 时运行。
- 手动 `session compact` 强制尝试覆盖至少一个旧帧。
- 无可压缩前缀时产生 skipped 结果，不创建空记录。
- 压缩 Run 使用同一 session lock、RunLifecycle、事件和 abort signal。
- TUI 把 `started/progress/completed/failed/skipped` 事件投影为运行活动和压缩时间线；压缩核心逻辑不依赖终端实现。

## 失败语义

- 摘要 Provider、格式或持久化失败产生 `compaction_failed`，不得激活部分记录。
- 失败后 canonical history 保持不变。
- 压缩后仍超预算时抛出 `ContextOverflowError` 或将 Run 以 `context_blocked` 暂停。
- 配置的 primary/fallback strategy 都失败时聚合明确错误。

## 撤销后的请求投影

消息撤销支持任意节点，canonical 前缀不受压缩帧粒度限制。若前缀保留了不完整工具交换，统一 Provider
投影过滤未闭合交换；不伪造结果，也不为协议合法性再次修改 canonical 消息。

撤销只保留覆盖消息仍完整可达的 compaction，清除失效的活动摘要和最近请求快照。Context Provider 等
只读准备阶段共享一次捕获的 Harness 视图，不能直接写入状态。撤销后请求使用当前版本与新代次，不使用
旧模型轮次缓存恢复激活关系或旧执行许可。

## 验收证据

- `tests/core/test_context.py`
- `tests/core/test_compaction.py`
- `tests/application/test_compaction.py`
- `tests/cli/test_compaction_config_commands.py`
- `tests/cli/test_session_commands.py`
