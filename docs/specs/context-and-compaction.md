# 上下文与压缩规格

> 文档类型：现行规格

## 目标与范围

上下文系统从 canonical messages 构建一次 Provider 请求视图，在不改写历史的前提下应用系统提示词、
Skills 指令、恢复投影、媒体整理、摘要压缩和模型 token 预算。

压缩是框架的请求准备机制，对用户消息与 Agent 执行轨迹构成的会话记录做上下文重构。框架负责预算判断、
历史帧选择、结果校验与激活；内置或插件贡献的策略负责生成继续执行所需的背景上下文，正文形式由策略决定。
规划只依赖会话材料、完整帧边界和预算，不依赖具体插件或 Run 的完成状态。

## ContextView

`ContextView` 拥有一个最终 `provider_context` 值、对应 Manifest、估算指标、摘要输入成本和请求 metadata。
`ProviderContext` 包含 system prompt、按顺序排列的 messages 和 tools。`ContextBuilder` 是无 I/O 的构建器，
在构建时一次完成模型可见内容转换，并对该值估算；不执行 Provider、文件或 SessionStore I/O。
`provider_context` 是数据字段，读取它不再次执行投影。`RuntimeAdapter` 直接将其中的值交给 Provider。
Manifest 与该值在同一次构建中产生；来源限定为本次已读取的 canonical 活动消息。未变内容按消息身份和块位置引用，
工具参数及 opaque continuation 按所属消息引用；已变内容保存实际字面值，不通过正文 hash 或重新投影决定引用。

准备服务在一次 `session_read` 中读取 Session 当前选择、活动摘要与未覆盖尾部；读取结束后才执行动态
Context Provider 和投影。Core 的摘要投影接收已经选定的尾部，不接收或切片完整 canonical 历史。
再次摘要只处理本次尾部，生成的覆盖终点仍使用 canonical 位置；准备完成后释放本次已覆盖材料。
动态 Provider 的 `message_count` 仍为 Session 的规范消息总数，自定义 estimator 仍接收完整实际请求。

自动摘要复用本次已读取尾部和初始 `ContextView`，摘要激活返回已校验的最终视图。准备服务只读当前 Session 小头，
当历史代次、消息数量及选择仍对应这份材料时直接使用该值；摘要期间发生追加或选择变化时重新读取活动尾部并构建。
标题等无关字段不触发重复准备。没有可摘要帧时保留原准备值，不重新投影；这些值仅在当前调用内存活。
摘要策略仍取得单独物化的 canonical 材料，因为恢复标注、工具帧过滤和 Provider continuation 筛选后的 Agent 视图
不是同一语义。媒体读取仍受原有只读寿命保护，策略与网络调用不持有存储锁。

`ContextBuilder` 接收注册工具定义，根据 `batch_behavior="exclusive"` 为请求副本的 description 追加统一的
单独调用约束，说明混合调用会在执行前整批拒绝。其他工具的描述保持原样；`execution_mode` 是宿主调度设置，
不生成额外模型指令。投影不修改注册定义、参数 schema 或执行契约哈希。
`ContextView.provider_context.tools` 保存已呈现的工具定义，token 估算、Provider 请求和请求记录使用同一份内容。
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
请求准备重试按 Run、model round、提供方及片段位置构成的 delivery key 点查最后一次持久值。
内容、角色、scaffold 与语义标签未变时不重复追加，改变时追加新事实；被摘要覆盖不使此重试身份失效。

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
媒体整理有独立的选择边界；默认策略只在容量不足时推进，调用方可显式调整当前选择。

## 当前上下文选择

`SessionRecord.context_selection` 保存当前摘要身份和媒体选择边界，独立于最近一次实际请求。
摘要激活通过 Session 原子提交更新 `compaction_id`。媒体边界保存稳定消息 ID 和规范 sequence；
显式切换摘要只能引用本 Session 当前保留的记录，清除摘要选择则允许重新使用历史材料。
关系在提交或快照发布时校验，普通投影依据位置判断已选消息，不扫描旧前缀定位边界。
下一次选择可以改变，最近请求仍保留当时实际准备的输入；不能从其 metadata 反向恢复当前策略状态。
Host/调用方通过 `SessionService.set_context_selection` 设置这一有限选择值，复用会话设置的执行所有权与原生
提交边界，不直接替换完整 Session。切回较早摘要时读取其覆盖终点后的材料；清除摘要及媒体边界后可重新使用
保存的历史与 Resource 字节，无须重新打开原工作区文件。清除摘要是明确的策略选择，后续撤销不自动激活仍保留
的摘要；只有当前已选摘要失效时，才沿原规则选择保留的摘要。模型能力、容量和完整工具协议检查仍适用。

## 媒体容量与历史整理

媒体处理与全文压缩是独立的投影机制。媒体处理只限制编码载荷、替换历史媒体块，不删除消息、
移动分析文字、生成摘要或触发 LLM 请求；全文压缩负责 token 预算、摘要生成及消息覆盖范围，
摘要激活同时接受其实际媒体选择。两者复用通用消息帧分组，由请求准备层组合各自的结果。

图片、音频、视频、内联内容和资源附件共用 `20 × 1024 × 1024` 编码字符的请求容量 `B`。
data URI 按 Base64 正文长度计量，外部 URI 按 URI 长度计量；当前模态或格式不支持的媒体不占容量。
未观察资源先识别格式和统计大小，确定保留范围后才载入完整资源并编码。已观察的附件和工具媒体使用固定 Resource 引用，
活动历史读取不物化其字节；准备服务按媒体类型和资源大小计算容量，最终保留的媒体才读取并编码。
已省略或不支持的媒体不打开内容文件。最终物化在会话只读边界内完成，不在 Provider 网络请求期间持有存储锁。
重复引用同一媒体的不同消息各自占用容量，不因出现较新的引用而改写旧媒体表示。

媒体选择保存在 `context_selection.media_elided_through`，包含 `message_id` 和 `sequence`，
覆盖该位置及之前的媒体。位置来自 canonical 消息事实，不使用摘要投影下标，也不要求边界消息仍处于活动输入中。
默认容量策略沿现有边界向前整理；这是策略行为，不是禁止调用方重新选择媒体的存储限制。原始内容与文件不变。

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

媒体预算规划只产生候选选择。准备完成后由 Session 提交接受选择，最近请求按自己的发布边界保存；
两者不通过 metadata 互相恢复。提交核对所依据的选择和历史代次，竞争选择变更不能被旧准备结果覆盖，
无关标题变更不阻止提交。容量检查失败不接受候选；已接受选择不因 Provider 发送失败回滚。
手动摘要在摘要激活事务中接受选择，不改写最近请求。摘要试算失败不会推进选择。

重启、正常追加、摘要和模型切换继承当前选择；已压缩前缀无需重新读出。撤销只将越过切口的媒体边界收缩到
最后保留消息，不重新打开更早媒体；空历史清除边界。分支重映射并收缩选择，跨后端转存保留选择和最近请求原值。
调用方显式清除或前移边界后，可从保存的事实重新选择媒体；模型能力和容量检查仍适用。

媒体边界不负责文本资源的预览和去重，不保证其他上下文变更的前缀稳定性。

## 最近一次 Provider 请求快照

最近请求快照记录已准备请求的事实。追加响应或更新 Harness 状态不会使它失效，进行中的请求仍使用原身份
结算 Provider 用量。后续请求准备时替换快照；撤销历史则清除属于已撤回历史的快照。

请求完成通过独立小头结算，不读取或重写准备好的输入；替换后到达的旧完成回调不能覆盖新请求。
头只保留计量引用，查询需要数值时读取原始账本；Provider done、本地中断和响应提交分别解释，
不会由原始用量推断完整响应成功。`response_message_id` 随正式或 partial 消息原子发布，
不将 Provider done 视为消息已提交。恢复与只读查询边界见[用量规格](usage-and-billing.md#请求结算与恢复)。
头、输入、替换清理与原生恢复的精确契约见[会话持久化](session-persistence.md#canonical-数据与投影)。

最近请求由 `ModelRequestHeader` 与 `RequestManifestRecord` 保存：前者包含身份、metrics、路由和收尾状态，
后者包含最终消息骨架、不可变消息内容引用、请求独占字面值与模态能力。
`ContextSnapshotRecord` 是显式查询物化后的完整值；它不作为另一份持久输入正文。
没有长期事实来源的请求专属媒体由 Manifest 内的实际字面值拥有，随这一代输入替换或失效清理；
不转成永久附件。已有事实媒体继续使用固定 Resource 引用。长期记录没有指向请求私有字面值的引用类型，
已返回的完整查询值也不依赖随后可能被替换的存储路径。
准备提交同时接受选择变化并记录 Run 的当前请求身份；随后 `started_at` 与请求计数同提交。
仅记录存在只证明已准备，`started_at` 也不证明 Provider 已收到请求或已完成响应。
`session context` 读取该记录的 metrics；`session context --content` 与 TUI 的上下文详情通过
`SessionService.context` 在一致读边界内取得骨架和所引用消息，返回完整 `ProviderContext`。没有准备过 Provider 请求时应明确返回
“尚无已准备上下文”，不能用 prospective projection 冒充真实请求。

查询不执行 Context Provider、Observer 或自动压缩，也不写入 canonical history。手动压缩保存摘要自身的小请求记录，并激活新的
`CompactionRecord`，不得替换最近普通请求的输入；新摘要会在下一次模型请求准备后自然进入新的快照。

工具描述按请求准备时的完整文本保存。快照读取直接使用该文本，不根据当前契约呈现逻辑追加或更新说明；
没有附加说明的已保存描述也原样保留。重复读取和协议编码不得重复追加约束。

Manifest schema 10 保存最终 provider-neutral 骨架，独立请求头 schema 11 记录准备、可能开始调用及收尾，与输入内容分别归属。语义标签与技能的渲染结果
在构建时固定，查询不调用当前渲染器，也不通过重建后比较 hash 证明相等；结构与请求身份仍校验。
记录固定实际文本、状态、图片、音频和视频内容，以及当时端点的三种输入能力；查询只读取已归属的媒体资源，
不依据当前模型或阈值重新投影。未变消息内容通过固定引用取得，实际生成的预览、路径表和媒体转换值保存为本请求字面值；
查询不打开工作区原文件。工具参数与结果经固定消息引用解引用到 Call/Outcome，未完成调用反馈保持其消息字面值。
附件骨架和正文按共享输入批次进入同一压缩帧，详见[附件规格](attachments.md)。

## Token 预算

`ContextBudget` 只保存一次请求的 `context_window` 与可空 `max_output_tokens`。输出预留为有限上限或 0，输入容量为窗口减预留；容量必须大于 0。主会话、独立生成、摘要及 TUI 消费同一模型，不另设预留配置。空值只表示 OURO 不配置输出上限。

`ContextMetrics` 保存 `estimated_input_tokens`、`estimated_fixed_tokens`、可选 `budget` 及 estimator 身份。输入预算、剩余容量、利用率和 `over_budget` 是派生属性，不重复持久化。请求快照中的 `budget` 保存当时最终请求的窗口和上限；无预算的纯投影可以仅记录输入估算。

估算覆盖 system prompt、message content、typed tool fields 和 tool definitions。默认 estimator 是 Provider 无关的保守近似。原生协议附件先按当前 provider/model 和执行 history_epoch 筛选，再进入估算；不遍历或解码私有 blocks。不匹配附件从请求投影移除，canonical 正文、工具帧和持久化附件保持完整。协议及认证 scope 的兼容性由 codec 在实际请求边界判断。

内置 estimator 复用最近一次构建中仍在活动输入内的不可变消息单元成本。键来自同次构建的 Manifest：
Session/message/block 身份、最终 role/工具结果小字段及 Call/continuation 成员引用；不保存正文或参数，不散列正文。
每次构建替换此小成本表，移出的成员不再保留。协议成员变化重新估算；纯媒体使用内置固定成本，与媒体字节无关。
变换后的字面文本、动态片段、提示和工具定义均重新估算。只有明确采用内置 estimator 才使用这一复用；
自定义文本 estimator 仍按原契约处理当前全部组成字符串，自定义构建器的整请求处理入口和参数保持不变。
复用不省略最终输入的构建与发送，也不改变动态 Context Provider 调用次数。

`over_budget` 只判断本次输入估算是否超过输入容量，不预测生成长度。上下文超预算时不能静默截断 message 或拆开工具交换。`RunBudgetPolicy` 负责整个 Run 的累计软额度，用实际用量记账；其输出额度不作为单次请求或摘要的输出参数。

## 原子上下文帧

- 同一 `turn_id` 的 assistant 工具请求与结果属于一个模型回合帧。
- 其前的用户消息加入随后的 identified frame。
- 无 `turn_id` 的普通消息按用户请求分组。
- 含未完成工具请求的后缀整体受保护。

压缩计划只能覆盖 canonical 前缀，并保留最新帧及未完成工具交换所在的整个后缀。已完成的旧帧可来自当前
Run；Run 是执行归属，不是压缩边界。摘要覆盖不删除或改写 canonical 消息，也不改变工具执行和恢复记录。
摘要有效性由提交时的覆盖终点和实际前置条件确定；规划器只接收尚未覆盖的请求投影，按帧及预算选择本次材料。
摘要以固定覆盖终点和前置摘要身份校验原始历史；图片降级等请求投影不参与历史一致性判断，不累计覆盖 ID 或计算旧前缀哈希。

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

1. 读取当前活动摘要，选择本次新增覆盖的 canonical 材料并保留受保护帧。
2. 按 `输入预算 × target_ratio - 固定内容估算 - 已有摘要投影估算` 规划尾部容量，最低为 0；目标比例是软目标。
3. 调用生成策略，得到非空摘要正文。
4. 将实际摘要和保留内容重新投影为完整主上下文，校验输入容量。
5. 校验生成期间的历史代次、前置摘要及固定消息引用后，原子提交并激活 `CompactionRecord`。

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

当前记录 schema 为 3。记录至少包含：

- compaction/session/previous identity；
- strategy id/version 和 prompt version；
- trigger；
- summary；
- `covered_message_count` 与 `covered_end_message_id`，标识 canonical 覆盖前缀的长度和终点；
- `retained_skill_message_ids`，标识被压缩后仍需提供技能内容的固定来源消息；
- `history_boundary_message_id`，标识摘要在历史展示中的位置；
- usage、model route 和 metadata。

摘要生成前捕获原 Run 的 `history_epoch` 和前置摘要身份；提交时读取当前 Session，拒绝旧代次或竞争激活。
普通追加和无关状态变化不使候选失效。覆盖终点必须位于声明的位置，展示锚点不得早于覆盖终点，技能来源必须位于
覆盖内且属于同一 Session。校验只读取这些固定引用，正文与 active 指针由同一 Session 提交发布。
`compaction_completed` 随激活提交保存，引用摘要身份和本次指标；提交后只通知，丢失通知可由 sequence 追赶。
开始、生成进度、跳过与失败事件仍用于诊断，不承担摘要激活或恢复的权威。
快照导入在发布前校验同一组引用、摘要身份唯一性和前置关系；消息的规范位置由快照顺序建立。

有效性由激活和撤销边界维护。正常投影直接使用摘要与未覆盖尾部，不重读、序列化或 hash 旧前缀，也不保存完整覆盖 ID 列表。
撤销从小引用读取前驱、覆盖终点和展示锚点，只读取需要移动锚点的摘要正文；其余保留正文不参与撤销决定或重写。
仅撤销尾部时保留原摘要；撤销覆盖终点时移除它及依赖它的后续摘要。展示锚点被撤销时移到最后保留消息，并清除
基于旧尾部计算的 `context_metrics_after`，摘要正文和覆盖不变。分支采用相同的覆盖判断，重映射有限引用；
有效摘要可以继承到早于原展示锚点的分支。已采用摘要不因后来无关的历史代次变化自动过期。

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
