# 上下文与压缩规格

> 文档类型：现行规格

## 目标与范围

上下文系统从 canonical messages 构建一次 Provider 请求视图，在不改写历史的前提下应用系统提示词、
Skills 指令、恢复投影、媒体整理、摘要压缩和模型 token 预算。

压缩是框架的请求准备机制，对用户消息与 Agent 执行轨迹构成的会话记录做上下文重构。框架负责预算判断、
历史帧选择、结果校验与激活；内置或插件贡献的策略负责生成继续执行所需的背景上下文，正文形式由策略决定。
规划只依赖会话材料、完整帧边界和预算，不依赖具体插件或 Run 的完成状态。

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

脚手架输入、Context Fragment 和续跑决策默认使用 `role=user`，提供方可显式声明其他角色。
基础指令与 Skills 继续组装为 system prompt；Agent Context 的 AGENTS.md 指令和运行环境信息显式声明
`role=system, target=system_prompt`，只进入系统提示词，不构造 user 消息。
内置 Plan 与 Goal 的 Context Provider 使用 `persistence=message`，在模型请求准备边界将 user 脚手架追加到
canonical history；模式和目标状态不并入 system prompt。子 Agent 指导和异步结果也以 user 脚手架进入普通输入队列。
这些消息保留来源和语义标签，user 角色不表示真人输入，也不改变执行权限。

系统提示词、补充提示词、Skills 目录与请求级注入不被压缩改写。压缩前后使用同一次请求已解析的固定上下文，
并将其完整计入输入预算。Agent 系统指令同时作为背景提供给生成策略，帮助理解会话，但不成为摘要模型的系统指令。

Agent 的有效系统上下文为固定系统上下文加最新压缩结果。最新结果通过唯一的
`role=system, semantic_tag=conversation-summary` 投影进入 Provider 的 system/instructions；原系统提示词完整保留。
再次压缩只替换这一结果，不累计追加旧摘要。canonical history 保留原始消息，投影使用最新结果及未覆盖尾部。

显式用户技能激活正文保存在 canonical 输入中。请求准备层根据压缩记录的 `history_boundary_message_id`
确定该边界所属 Run，恢复其中已被覆盖的技能正文为 ephemeral user context；仍在尾部的激活不重复注入。
正文从已接受的激活块重建，不重新读取文件、不提升角色、不追加 canonical 消息，并计入预算与请求快照。
这部分背景固定到下一次压缩，不随当前执行 Run 切换而移除，也不依赖运行实例的内存状态。

压缩激活后的第一次 Agent 请求使用重构后的上下文。此后直到下次压缩，摘要覆盖投影保持已有前缀不变，
只在保留尾部之后追加新消息；关闭运行实例后恢复也从相同持久化记录重建。显式提示词、技能目录或消息编辑
按各自契约生效，不属于压缩引起的变化。请求边界对不完整消息帧的安全过滤仍然适用，不与只追加约束冲突。
媒体整理有独立的容量边界，只有推进该边界时才替换其覆盖的媒体块。

## 媒体容量与历史整理

媒体处理与全文压缩是独立的投影机制。媒体处理只限制编码载荷、替换历史媒体块，不删除消息、
移动分析文字、生成摘要或触发 LLM 请求；全文压缩负责 token 预算、摘要生成及消息覆盖范围，
不修改媒体整理状态。两者复用通用消息帧分组，由请求准备层组合各自的结果。

图片、音频、视频、内联内容和资源附件共用 `20 × 1024 × 1024` 编码字符的请求容量 `B`。
data URI 按 Base64 正文长度计量，外部 URI 按 URI 长度计量；当前模态或格式不支持的媒体不占容量。
资源媒体先识别格式和统计大小，确定保留范围后才载入完整资源并编码。已观察的附件复用持久化内容。
重复引用同一媒体的不同消息各自占用容量，不因出现较新的引用而改写旧媒体表示。

媒体阶段只持久化 `context_metadata.media_projection` 中的 `history_epoch` 与
`media_elided_through_message_id`。边界指向原始历史中的稳定消息 ID，覆盖该消息及之前所有媒体；
位置判断使用原始历史，不使用摘要投影中的下标。边界只向前推进，原始内容与文件不变。

应用已保存边界后，剩余载荷不超过 `B` 时保持边界不变。超过时按历史顺序整理最旧的完整分组，
直到剩余载荷不超过 `max(0.6 × B, 受保护载荷)`。只替换分组中的媒体块，不修改其他内容。
受保护范围从最新正常完成模型回复之后的待处理输入开始，并包含未完成工具交互所在的整个后缀；
与该范围相交的分组整体保留。最新工具结果与随后加入的用户输入都受保护，无可靠完成边界时保守保留。
工具调用与结果齐全不表示结果已经被模型处理。

受保护媒体自身超过 `B` 时抛出 `MediaCapacityError`，明确要求缩小或拆分输入，
不发送该次模型请求、不提交候选边界，不部分发送或静默省略新媒体。单项媒体仍遵循工具和附件容量限制。

被整理的媒体在原位置替换为按类型固定的 `Historical image/audio/video` 说明：当前不提供媒体数据，
文件信息与文字记录保留但不保证覆盖全部细节；优先使用已有观察，需要核实时才重新读取可用来源，
原文件可能已经更新。说明不声称内容已经被模型看过，不包含轮次、时间戳或预算等变化值。

媒体预算规划只产生候选投影。最终请求准备完成时，边界与请求快照一起原子保存；发送失败不回滚边界，
相同输入重试继承相同决定。摘要试算、手动压缩和失败的请求准备都不提交媒体状态。
同一会话和 `history_epoch` 下重启、追加和全文压缩继承边界，即使摘要已覆盖边界消息，仍保留尾部的整理结果。
撤回、分支、导入以及缺少整理状态的快照开始新媒体阶段。模型切换保留边界，模型能力和协议变化按其自身契约生效。

媒体边界不负责文本资源的预览和去重，不保证其他上下文变更的前缀稳定性。

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

压缩计划只能覆盖 canonical 前缀，并保留最新帧及未完成工具交换所在的整个后缀。已完成的旧帧可来自当前
Run；Run 是执行归属，不是压缩边界。摘要覆盖不删除或改写 canonical 消息，也不改变工具执行和恢复记录。
历史身份与内容哈希在原始 canonical 记录上校验；规划器只接收尚未覆盖的请求投影，按帧及预算选择本次材料。
提交时从原始前缀生成累计覆盖身份与内容哈希，图片降级等请求投影不参与历史一致性判断。

## 压缩配置

本地组合的专用压缩 adapter 在请求边界经共享 model catalog 解析，认证引用、驱动和连接配置变化会生效；
普通凭据刷新复用现有 adapter。已经绑定的路由失效以及带结构化错误码的服务失败为 terminal，
不切换模型、账户或 fallback strategy；managed 未分类传输/响应失败也提供通用错误码。
相应契约见[请求认证规格](request-authentication.md)。

`CompactionConfig` 包含 primary/fallback strategy、触发与目标比例，以及内置摘要策略的模型路由与请求参数。摘要的 `max_output_tokens` 缺失或为 `null` 均表示不限制，可以在当前 Session 模型、默认模型或专用端点上配置。专用端点在启动时尚未解析成功可回退当前 Session runtime endpoint，并在 metadata 中记录原因。

所有摘要路由应用同一份摘要请求配置。端点输出配置不是摘要上限的能力边界；摘要空值不继承端点或主请求上限。路由选定后创建独立 adapter 副本，不修改共享模型。有限上限必须为该路由留下输入空间，协议负责拒绝无法编码的请求。

## 摘要执行

框架与生成策略的契约是输入材料和继续执行的背景上下文。`CompactionRequest` 提供会话/运行身份、Harness 视图、
触发方式、`agent_system_prompt`、已有压缩结果及本次会话材料；不传入主请求预算或生成额度。
`agent_system_prompt` 是本次已解析的完整固定系统背景，不含旧压缩结果，后者由 `previous_summary` 单独传递。
策略通过 `CompactionArtifact.summary` 返回非空上下文，可附带用量、请求审计与策略 metadata。
插件可自行选择策略、提示词、模型和响应格式，框架不要求结果必须是自然语言摘要。

框架执行顺序：

1. 验证 active compaction lineage，选择可覆盖的 canonical 前缀并保留受保护帧。
2. 按 `输入预算 × target_ratio - 固定内容估算 - 已有摘要投影估算` 规划尾部容量，最低为 0；目标比例是软目标。
3. 调用生成策略，得到非空摘要正文。
4. 将实际摘要和保留内容重新投影为完整主上下文，校验输入容量。
5. 校验历史前缀和代次后原子提交并激活 `CompactionRecord`。

提示词、原始输出格式、解析、分块和格式相关重试由生成策略负责。框架不检查 `<summary>` 标签，不依据输出参数预测摘要正文大小，不要求插件沿用内置提示词。

内置 `builtin.llm_summary` 位于 `ouro/application/context/summary.py`，使用独立模型请求，不进入主 Agent 循环，
不创建持久化 Session，不携带普通工具。摘要模型的 system 仅含其压缩指令；Agent 系统背景以
`role=user, semantic_tag=agent-system-context` 脚手架传入，已有摘要与待压缩会话以
`role=user, semantic_tag=conversation-to-summarize` 传入。两者都是待理解的材料，不能混入摘要模型的系统指令。
每个分块及重试均保留 Agent 背景，估算覆盖全部脚手架、标签、转义正文、已有摘要及压缩指令。
其提示词要求完整简洁的替代摘要，不复述固定系统指令，不依赖数值输出预算模板。
当前内置解析器提取非空 `<summary>` 正文，格式规则与提示词一起维护。

每次滚动摘要使用自身请求的输入容量。选块按完整 frame 进行，对候选内容最多序列化一次，用与默认 estimator 一致的可加字符度量累计容量，保持线性复杂度。单帧或压缩指令、Agent 背景与已有摘要无法容纳时明确失败。内置策略仅对现有可识别的未闭合、被截断摘要执行一次缩块重试，不循环扩大额度。

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

- 每次主模型请求前都估算完整输入；达到 trigger ratio 时自动尝试压缩。工具轮次完成后发起的下一次请求
  使用相同入口，因此单个 Run 内可以多次进行增量压缩。
- 自动压缩沿用当前 Run 的锁、取消信号、事件及用量归属；成功后以新摘要和保留尾部重建上下文，再继续
  同一轮模型请求。它不创建新的 Session 或 Run，不重放已经执行的工具。
- 手动 `session compact` 强制尝试覆盖至少一个旧帧。
- 无可压缩前缀时产生 skipped 结果，不创建空记录。
- 手动压缩使用独立的压缩 Run，复用相同的规划、策略执行和提交路径。
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
- `tests/application/test_run_compaction.py`
- `tests/plugins/test_sdk_compaction.py`
- `tests/cli/test_compaction_config_commands.py`
- `tests/cli/test_session_commands.py`
