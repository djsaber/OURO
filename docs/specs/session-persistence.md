# 会话与持久化规格

> 文档类型：现行规格

## 目标与范围

持久化层保存可恢复的 canonical Session 数据，并提供可替换的 `SessionStore` 端口。默认实现是
`LocalSessionStore`；application 不依赖本地文件路径和原子替换细节。

## 数据布局

```text
OURO_HOME/
  layout.json
  config/config.json
  config/providers.json
  config/mcp.json
  config/agents.json
  config/prompts.json
  skills/
  locks/sessions/
  sessions/
    index.json
    <session_id>/
      inputs/records/<input_key>.json
      inputs/states/<input_key>.json
      messages.jsonl
      session-data.sqlite3
      request-content/<context_id>.json
      delegations/<delegation_id>.json
      delegation-index/requests/<request_key>
      delegation-index/children/<child_session_id>
      delegation-index/parents/<parent_run_id>/<delegation_id>
      resources/files/<filename>
      resources/.staging/
      harness/<version_id>.json
      commit.json  # 仅存在于未完成的跨文件提交期间
  trash/sessions/<trash_id>/
```

当前 `data_layout_version` 为 41，SQLite schema 为 26；各持久记录的版本见下文 Canonical 数据与投影。
SQLite 同样使用会话文件目录保存附件，资源元信息保存在数据库；跨后端传输使用统一归档。
初始化只接受当前格式；不支持或缺失的版本抛出 `UnsupportedDataVersionError`，不扫描或转换历史记录。
空数据目录/数据库初始化为当前格式。已有数据不自动清空，由用户选择新目录或清理失效测试数据。

SQLite 初始化在数据库旁 `.<数据库文件名>.locks/initialize.lock` 下取得跨进程短锁，等待上限为 5 秒。
schema 检查、WAL 设置和版本记录创建在同一初始化所有权内完成，防止并发启动重复创建版本；后续 Session
业务读写仍使用原有 SQLite 事务和 Session 执行锁。

当前格式的未完成提交仍按原生决定恢复，不重新运行插件、转换 schema 或重写审计事实。
Harness、资源列表、事件高水位、事件分页和 sequence 追赶同样使用只读边界；Files 查询发现未决决定时
报告该会话需恢复。事件与执行记录共同存于原生数据库，高水位和分页直接通过索引查询，不扫描正文或重建索引。
owner 业务状态由相应执行契约读取，不支持的 schema 明确拒绝；没有状态迁移命令。
当前格式的 Session、Run、消息与 Harness 可在插件变化后继续读取。ToolInvocation 保存 resumable 声明及
实际执行所需的插件契约或 Host 工具定义摘要；恢复核对保存的执行契约、当前定义及授权，不能推断未知私有状态。
历史 Catalog/generation 摘要仅用于审计；队列接受凭据不授权恢复旧环境。

## Canonical 数据与投影

Canonical 数据包括：

- `inputs/records/<input_key>.json` 的已接受输入，以及 `inputs/states/<input_key>.json` 的小生命周期状态；
- `session-data.sqlite3` 的 Session 头、Run、有序事件、独立调用、实际执行开始、确定结果事实、资源元信息、可变 Call 状态、有序摘要记录、请求小头、当前请求定位与 Draft；
- `messages.jsonl` 的有序消息事实；
- root Session 下的 `delegations/*.json`；
- `latest_contexts` 指向的唯一 `request-content/<context_id>.json` Manifest；
- 所属会话的资源元信息与不可变附件文件；
- 不可变 Harness 版本、Session 的当前版本引用和消息前版本锚点；
- 输入接受与 Run 结算事实，以及文件后端的短期未完成提交决定。

Files 将消息自己拥有的内容及领域引用顺序存入 `messages.jsonl`，使用 UTF-8 和 LF，偏移按字节计算。
`session-data.sqlite3` 保存消息 ID、位置、正文偏移和业务查询键，并唯一拥有 Session 头、Run、事件、Call、started、Outcome、资源元信息、Call 状态与摘要。
消息位置的查询键使用原生列与索引；资源 ID 列表单独编码。
相关工具事实、调用状态、摘要和消息位置在一个原生事务中关联和更新；Files 的后端选择和领域接口保持不变。
摘要按身份点查或按创建顺序一次读取，前驱、覆盖终点和展示锚点由覆盖索引提供；撤销只读写需调整锚点的摘要正文。
消息点查按 ID 取得偏移并读取选中的正文行。
消息的持久位置由有序成员索引保存，独立于传入的 sequence，不在正文重复保存；点查核对正文身份。
分页、倒序窗口和完整历史直接读顺序正文；撤销按位置选择小成员记录，只解析首条移除和最后保留的边界事实，
不读取待移除的全部正文。成员、Call 关联及 Run 小计数随会话数据事务更新；正文与数据库共同由已有
`commit.json` 决定发布。数据库事务前中断时重放文件后缀并提交事务；Session 的 `last_commit_id` 与事实、事件
同事务生效，已完成的决定只清理过期 Manifest 和决定槽位，不重复插入事实或事件。查询只读索引，不建立全历史内存表。
完整快照在未发布目录中单次编码、顺序写入正文。Files 空 schema 初始化独立于会话发布，失败重试可复用空 schema；
消息位置、工具事实、状态、摘要、请求头、资源元信息与 Session 头在同一个数据事务中发布。
原生数据发布失败时不会留下部分会话，已预留的不可变资源仍可用于重试。
持久编码直接使用已验证消息的自有内容和固定引用，不构造仅用于序列化的 MessageFact 副本。
需要跨内存所有权边界传递事实时，`as_fact()` 仍生成独立的事实对象。
内容读取直接解码为 MessageRecord，并执行持久内容归属检查；没有引用的正文直接成为查询独占的结果。带引用的消息解析相应 Input/Call/Outcome，并验证解析后的关联及消息内容。
工具消息在绑定 Call/Outcome 时完成关联验证；Input 正文物化后仍检查消息自有附件的位置。
显式窗口、分页、Run 和完整历史查询按有界消息批次联接所选 Call/Outcome；不读取范围外事实，不保留全历史缓存。
Outcome 的 `structured` 与其他结果字段由同一原生行的不同列保存，随事实一起提交且各存一份。
消息投影不读取结构化结果列；显式 Outcome 读取、恢复、完整快照和转存返回完整结构化结果，
不会以消息视图替代原始观察。工具正文、结构化结果和结算反馈的既有语义保持独立。
一个批次中重复引用同一事实时，只有会被多个消息视图使用的可变部分需要复制，各返回消息保持独立。
惰性历史迭代仍按调用方需求读取下一条，不提前解析额外消息或媒体。
请求准备使用倒序窗口的 `include_media=False`，仍解析 Input/Call/Outcome 的内容和身份，但保留固定媒体引用；
它是显式的读取选项，不是隐式加载对象。普通查询默认物化媒体，最终请求选择通过资源准备边界读取保留的字节。
Files 索引按 Run、Input 等业务身份定位成员。Run 查询只读该 Run 的引用，并复用同一个正文文件句柄定位消息；
输入查询读取其最新成员。SQLite 后端对应查询保持表和索引定位；输入重复键查询
按历史顺序取最新保留消息。输入领域自身的一次接受约束仍由接受记录负责。

委派结果收集通过 `load_run_result_references` 读取当前保留历史的首条匹配委派输入、最后一条 assistant
身份及 assistant/tool_result 的有序去重资源 ID，不展开整个 Run 正文。Files 的已有成员引用保存委派输入键和
资源 ID；SQLite 保存对应列并使用覆盖索引。确定工具结果的资源 ID 在提交前从固定 Outcome 解析，随消息发布
维护，MessageFact 不复制 Outcome 的资源列表或正文。未知反馈发布后，迟到 Outcome 不改变该成员引用。
撤销同步移除成员，完整转存从物化快照重建引用。此查询仍遍历该 Run 的小成员记录，内存只保留所需结果 ID 与
唯一资源 ID，不声称资源列表或 Run 成员数无限增长时为固定成本。

assistant 回包按 Session、Run、turn 定位，包含带 Call 引用的模型响应；持久 Context Provider 片段按
Session、Run、delivery key 定位最后值。Files 索引保存这两类查询键及对应成员引用，身份作为 SQL 参数传递；
没有对应身份的消息不增加这两类索引。SQLite 使用相同业务身份的复合表达式索引。
查询只物化选中的一条事实，不建立 Run 全消息缓存；撤销截断索引后，片段查询自然回到仍保留的最后值。

续跑所需的 Run 成员存在性、user/system 输入存在性只查询成员引用，不读取正文；最后 assistant 查询只物化所选一条事实。
Files 和 SQLite 均使用对应的 Run 索引与 role 部分索引，无须对成员身份进行哈希。
scaffold 不影响这里的输入存在性语义，
助手 Call 帧仍属于最后 assistant 查询结果。这些索引同样随追加、快照创建和撤销的原生提交维护。

continuation 的执行事实读取 `RunMessageCounts`，不加载整 Run 消息。工具数按已发布消息中的
Run/turn/tool_call_id 去重，声明和结果只计一次；未发布 Call 不计入，Driver 的结果消息也计入。
自动续跑标记只统计带 `session_input_mode` 的已消费输入：无输入为未知，全部带 continuation 决定身份为 true，
否则为 false；策略反馈不作为输入。这保持原有结算语义，不改变插件是否继续的决策。
Files 用 Call 成员引用判断首次发布和最后撤销，并保存每 Run 小计数；每个 Call 成员不重复整批 Call 列表。
SQLite 使用 `messages.run_id/input_origin`、`message_call_references` 与 `run_message_counts`，按关系和索引定位。
这些都是消息派生索引，随消息追加/撤销在原生提交中更新，完整导入从所选消息重建；没有第二份正文或新 Run 业务状态。
计数点读及新消息追加不读取旧消息正文；撤销处理实际移除的后缀，显式完整转存仍处理完整选中数据。
SQLite 在一个原生事务中批量写入消息及其成员关系，每批只更新一次各受影响 Run 的计数。
同一消息的 Call 成员整体查询和发布，不对每个成员重复定位消息行。
同一 Call 的请求与结果消息仍只贡献一次工具调用计数，增量汇总仅保留到当前提交结束。

这些消息查询引用与正文、顺序和 Session 头由同一跨文件决定维护。
追加决定记录各目标引用日志的原偏移；撤销决定记录待删除的消息引用和受影响日志的截断偏移。
恢复重复写入所决定的后缀，并删除撤销正文，能覆盖部分引用行和清理中断；不扫描其他历史或重放业务回调。
按 ID、Run、输入、分页、尾部和倒序窗口查询使用只读边界，存在未决决定时明确要求先完成原生恢复。
turn 回包和 Context Provider 片段点查遵守同一只读与归属契约。
快照/导入按输入顺序将消息事实的 sequence 设为从 1 开始的规范位置，不依赖输入 payload 已经赋值。
完整导出仍允许读取完整历史。

Files 的活动摘要读取按 Session 的 active 指针点查一个记录，不扫描摘要顺序索引或消息历史。摘要激活通过
`SessionCommit.compaction` 同时发布正文、顺序成员和 Session active 指针，未决决定记录索引原偏移；
SQLite 在同一事务中插入摘要并更新 Session。查询使用只读边界，Files 未决提交要求先由显式写入/恢复完成，
不在查询中回放。撤销在同一原生提交内删除失效摘要并调整仍有效摘要的展示锚点。引用规则见
[上下文与压缩](context-and-compaction.md#compactionrecord)。
撤销只遍历摘要的小依赖引用，判定删除项与需移动展示锚点的项；保留且未受影响的摘要不读取、不序列化、
不重写正文。Files 决定保存小引用列表、删除身份和真正改变的摘要；SQLite 使用覆盖索引读取依赖列，并只
更新或删除受影响记录。显式撤销仍有 O(摘要数) 的引用扫描，不声称该维护操作为常量成本；普通活动摘要点查不变。

Session 的 `pending_input_ids` 只保存待处理输入的 FIFO 身份。`InputRecord` 保存接受时的原文、固定技能内容、
附件、来源与执行归属；`InputState` 保存路由字段及 pending/consumed/cancelled/withdrawn 状态和已提交消息身份。
SQLite 使用 `inputs`、`input_states` 两张按 Session/input ID 定位的表实现同一契约。
Files 的 `input_key` 是原 input ID UTF-8 字节的 SHA-256 十六进制值，只用于确定文件名；原 ID 在记录中完整保留并校验。
Host 身份中的分隔符、Unicode 和长身份不会成为路径，也不要求扫描映射表。这里不对输入正文或提交提案计算身份摘要。
接受只写一次正文，消费或取消只更新对应小状态与 Session 待处理身份，不读取或重写其他已接受输入正文。
消费与消息追加共用原生决定；撤销保留输入原文及终态，不允许旧身份复活。
普通点查缺失或损坏时明确报告，不隐式移除队列成员。Session 头及 Harness 成对查询同样使用只读边界，
未决 Files 决定必须通过显式 mutation 入口完成后再读取。
完整导出包含全部接受事实和终态，分支只复制所选历史引用的输入、重映射消息身份并复制其资源闭包；不复制 pending 授权。

消息的 `input_ref` 固定引用一个已接受 Input 的正文或附件观察。正文由 Input 中接受时的 prompt 和技能单元物化，
附件意图由同一 Input 读取；附件说明中的实际文件文本、媒体引用及生成的资源表由消息拥有。
图片、音频和视频观察的二进制由所属 Resource 保存，媒体块用 `resource_id` 引用；上传复用原资源，
工作区观察发布内部资源。持久 MessageFact 和 Manifest 不同时保存该引用的 data URI；
消息、分页和最近请求的显式读取在边界内物化所选字节，返回值可携带 URI 与原资源身份。
`MessageFact` 是持久化形态，拒绝同时内嵌已引用的正文或附件意图；`MessageRecord` 是显式物化的单条执行/查询值，
不含 loader 或隐式读取。`as_fact()` 在写入边界保留引用与消息自有内容，不把物化副本落盘。
引用绑定必须与 Input 消费处于同一决定，角色、scaffold 与语义标签必须符合已接受意图；缺失或跨 Session 的 Input 明确报错。
原始输入只含 resource_refs 时，消费生成的附件元信息属于消息自有内容，不冒充接受时已保存的附件意图。
附件观察和正文共享输入身份及原子消费单元；输入回执返回正文消息，只有附件时返回附件消息。
消息 metadata 不接受 `queued_input_id`，读取不猜测旧关联字段。

归档消息同样使用 `MessageFact` 并携带其 Input 闭包，导入只解析已保存的固定事实，不调用技能解析器或读取原始附件路径。
分支重映射消息位置而保留输入原意；撤销返回显式物化的原始输入，原始文件或技能已变化不影响还原。

两个后端的 `events` 表以一份完整 JSON 唯一保存事件；可选空字段和空的默认值不重复编码，schema_version 始终保留。
查询身份列由 SQLite 从该 JSON 生成，`(session_id, sequence)`、Session/Run 行游标索引用于高水位、追赶和分页。
身份列通过原生表达式取得。索引由同一插入事务维护，高水位直接读取索引值，不解析事件 JSON。
分页游标是后端私有的位置，不是事件 sequence。查询只解码选中的事件，水位读取不解码事件正文；
不维护额外事件文件、派生游标文件、内存事件索引或回退扫描。Session、Run、调用事实、请求头、摘要和事件
共享同一原生事务。没有消息正文、输入文件、Harness 或 Manifest 文件变更时直接提交事务，不创建 `commit.json`。

共享预算的 `load_run_request_counts` 只读取 Run 身份、model_round_count 和 summary_request_count。
这些数值由 Run 拥有：两后端在原生 Run 行中同步投影两列并通过覆盖索引读取。读取不解析 Run 正文；完整转存按 Run 重建投影。该读取随相关 Session 的
Run 数增长，不承诺与 Run 数无关。

`load_latest_context_header` 只返回请求身份、时间和 metrics，不物化 provider 参数、消息或媒体正文。
请求头是 canonical 状态，不从输入派生，也不在输入中重复保存。两后端使用复合 Session/context_id 主键的
`request_headers`，`latest_contexts` 定位最近普通请求。Files 的原生表只保存最近身份，正文在 request-content；
SQLite 的最近输入行同时保存 Manifest 正文。完成更新只写请求小头，不重写大正文。
`load_request_header` 可定向读取普通或摘要请求的小头；替换只清理旧输入，保留请求身份和计量收尾。
完整查询在一个读取边界物化头和正文，校验两者身份后返回独立值；不向调用方交出可能被替换清理的文件引用。

准备新请求使用 `SessionCommit.request_header/request_manifest`，与已接受的 ContextSelection 和 Run 的
`current_request_id` 同提交。Files 先发布不可变 Manifest，现有 Session 决定只保存小头及上一请求身份；
决定将新头、当前请求定位及 Draft 生命周期放入同一个原生事务，再完成 Run、选择及旧正文清理；
SQLite 在一个原生事务提交相同的领域效果。
恢复不读取 Manifest 正文、不扫描旧请求。网络和 Observer 均在提交结束后执行。
在调用 Provider 前，另一个小提交同时记录头的 `started_at` 与 Run 请求计数；它只表示可能开始调用，
不证明远端收到。仅准备完成或准备后的 Observer 失败不增加请求计数；取消不能启动新的 Provider reader。
已发布身份不能被另一份输入覆盖。决定之前失败可能留下未被接受的孤立输入，普通读取与恢复不扫描该类文件。
撤销的原生决定清除最近输入指针及对应正文，保留小请求记录；Files 决定保存待删身份，重复恢复仍能清理正文。

`settle_request_header` 只读取匹配 context_id 的头，不访问输入正文。头保存互斥的 provider_done_at 或 interrupted_at，
前者表示已确认 Provider done，后者表示本地中断收尾；二者都不证明完整响应消息已提交。
`response_message_id` 只在对应 Run/turn 的正式或 partial assistant 消息进入原生提交时写入；工具响应沿用
消息、Call 和 Run 的同一提交。该引用不由 Provider done 或账本记录推断，消息的 incomplete 标记仍决定 partial 语义。
内置摘要在派发前提交 purpose=compaction 的小请求头、Run.current_request_id 与 summary_request_count，
不替换最近普通请求的 Manifest。摘要激活按生成结果的请求身份，在同一提交写入 response_compaction_id
和 response_closed_at；Provider done 或账本记录本身不证明摘要已采用。
usage_entry_id 只引用原始账本事实，不保存 Provider token 数值。引用必须匹配本请求的 Run、turn、用途和模型路由身份。
重复收尾保留首次时间，不能将已记录的中断改写成 Provider done；后续原始计量确认可以给原状态补齐引用。
已有引用不可清除或替换。迟到的计量收尾只更新原身份的小头，不替换当前输入或重新发布旧响应。账本与 Session 的提交、恢复和查询边界见
[用量规格](usage-and-billing.md#请求结算与恢复)。
Files 的 `session_read` 遇到未决 `commit.json` 抛出 `SessionRecoveryRequiredError`，不执行恢复写入；显式进入
`session_mutation` 完成原生存储决定后可再读。Delegation 点查、按 ID/child/idempotency 查找及列表查询
使用同一只读边界，不隐式恢复；创建和 CAS 更新仍显式进入 mutation。其余旧查询入口继续按执行恢复职责审查。

Session、Run、Message、Event、Compaction、ContextSnapshot 和 Delegation 是独立持久化根记录。每个 payload
必须显式携带各自当前 `schema_version`：Session 为 5，Event 为 2，Run 为 10，ModelRequestHeader 为 11、Manifest 为 10，Message 为 8，Compaction 为 3，Delegation 为 2，InputRecord/InputState/ToolCallRecord/ToolCallStart/ToolCallState 为 1，ToolOutcomeRecord 为 2；ResourceRecord 为 3。

最近请求通过统一 Session 提交接收小头和 `RequestManifestRecord`，不接受完整查询 DTO 作为持久正文。
`load_latest_request` 返回已存骨架；完整 `load_latest_context` 在同一个 Session 读边界内点读其固定消息引用，
每个被引用消息只物化一次，不扫描其他历史或调用当前投影逻辑。Files 保持读锁到物化结束，SQLite 的骨架与消息查询
共享同一读事务。缺失来源、越界块或不同 Session 身份明确报错，不能转而读取当前工作区重建。
转存将全部小请求记录、唯一最近骨架及其消息引用闭包直接纳入目标快照创建，不伪造一次新的执行提交或改写源 Session 时间；
不重新写入完整 ProviderContext 副本。

Session 的 `context_selection` 拥有当前摘要身份及媒体边界，替代根级 `active_compaction_id`；Host 状态 DTO
仍可提供同名摘要字段，但不拥有另一份选择。选择更新通过 Session 提交校验新媒体边界的固定消息及规范位置，
快照按输入顺序建立位置后校验同一关系。当前媒体选择属于 ContextSelection，转存保留已准备请求的 metadata。
缺失、非整数、旧版本或未来版本均由共享 versioned decoder 在 adapter
读取边界抛出 `UnsupportedDataVersionError`。领域模型校验各自版本，未知版本不能被验证为当前记录。
Store 不通过字段默认值猜测旧格式，也不提供历史版本转换。

`data_layout_version`、SQLite schema version 和插件 manifest/state schema 是独立版本域，不能因为当前数字相同
而共用版本判定。Session 资源元信息逐记录使用 ResourceRecord 版本；Session catalog 是 layout-bound 文档，不冒充核心 record 版本。

`sessions/index.json` 与 SQLite `session_catalog` 表是可重建查询投影，不属于 canonical transaction。Catalog
查询是严格只读的投影查询：只读取并校验索引自身的结构，不抢 catalog lock、不打开 Session 目录/表，也不因为
索引缺失、过期或损坏而隐式扫描 canonical 数据。指定 Session 的 `open/messages/runs` 查询才读取并严格校验该
Session 的 canonical records；一个坏 Session 不能把列表查询升级成全库数据质量检查。

Catalog 行自身的结构错误使用独立 `catalog_error` 表达，此时 `health=None`，缺失的 `session_kind` 保持未知；
`health=corrupt | unsupported_version` 只表示构建投影时实际读取 canonical 数据所得的健康结论。Host 可以对
`catalog_error` 使用失败语义色并提示重建，但只要有可定位的 `session_id`，就必须允许用户尝试打开；打开成功或失败
只由目标 Session 的 canonical snapshot/observation 决定。Catalog 状态不得成为 `open` 的前置授权条件，也不得为了
补齐未知身份在列表路径逐项读取 canonical Session。

Canonical mutation 成功后，adapter 在独立 catalog 边界只重算受影响的一个条目；删除则移除该条目。该刷新最多
读取目标 Session 的有界 `latest + count` 投影，不扫描无关 Session。刷新、索引写入或 catalog lock 失败时 canonical
写入仍保持成功，application 返回可恢复 diagnostic，列表可暂时读取旧快照。全量扫描只属于显式
`rebuild_catalog` 维护，不得从 `read_catalog`、Host 会话选择器或进程启动的常规路径
触发。已有 canonical Session 但索引缺失/损坏时，列表明确报告需要重建；不能用空列表或同步自愈掩盖故障。

Session 头的 `message_count`、`run_count`、`resource_count` 是原生提交维护的成员投影。Run 首次保存增加计数，
更新已有 Run 不增加；撤销保留 Run 审计记录，因此不扣减。资源首次挂接增加、移除减少，同身份重提不重复累计，
内部媒体资源也计入已挂接成员。普通 Session 设置写入不能覆盖这些存储管理字段。
两后端的独立 Run 保存、资源挂接/移除均由原生事务维护成员与 Session 头计数；
资源元信息由会话原生数据库拥有。
快照创建按实际资源清单建立计数，Run 从零随导入记录建立，不能继承源会话未选入的成员数量。
Catalog 刷新直接使用这些字段，不枚举 Run/资源目录，也不执行全表成员计数；预览只解析消息或 Input 文本。
Files 的 Session 头和最新 Run 在同一个原生读事务中读取；执行期间复用当前所有者的连接，独立刷新结束即关闭连接。
计数不是授权、执行状态或外部副作用结论；资源发布只允许改变相应计数字段，不推进 Session 业务状态。

## 工具事实存储

独立存储身份为 Session 内的 `(run_id, batch_id, tool_call_id)`；不能把 Provider call ID 当作跨批次唯一键。
调用身份是经过类型校验的不可变值，复制工作集可共享身份；可变参数、生产者和结果正文仍按各自所有权隔离。
`ToolCallRecord` 保存原始请求、source_index、producer、turn、history_epoch 和原执行 Session；
`ToolCallStart` 按 attempt 保存已固定的执行绑定。实际参数与原请求相同时引用原 Call，仅变更后的参数单独保存。
`ToolOutcomeRecord` 保存一次确定观察的 content、structured、错误、资源、时间和退出码；这些字段有独立语义，不互相替代。
工具自定义 structured 字段不用于猜测框架的 unknown_outcome 状态。

工具媒体的有效 data URI 在结果提交边界解码并发布为所属 Session 的内部 Resource，Outcome 只保存固定媒体引用；
原媒体类型及必要的 data URI 头保留在块中。远程 URL 和不能解码的 opaque URI 原样保留，不访问网络或推断字节。
独立结果提交与普通批次提交共用这一捕获路径，重提同一观察复用已有资源身份。原生事实和转存快照不保存可物化的 Base64 副本，
显式 Outcome 查询在读取寿命内补全媒体内容，分支写回引用形态。迟到结果的资源归属不依赖结果是否已发布进当前历史。

新 Call 与 started 必须属于当前活动 Run、当前 epoch，不能借复制的历史归属重新获得执行许可。
正数 attempt 的 Outcome 必须引用已存在的 started；attempt 0 只表示尚未进入工具的确定预检拒绝。
同身份、同内容的事实重提不产生新记录；已有原始参数、实际参数和确定结果不得被另一份内容覆盖。
后续执行许可和等待恢复由调用用例负责，单独存在 started 不是自动重跑授权。

`commit_tool_outcome` 只保存原 Call 的确定观察，不读取 Harness、Run 正文或历史，不发布消息或应用业务意图。
媒体字节先发布为不可变文件，Outcome、资源元信息与 Session 头的派生 `resource_count` 在同一原生事务内提交；
其他 Session 字段保持不变。回滚后不会留下可见资源登记或 Outcome，可能保留尚未被引用的媒体文件。
普通消息提交需要跨文件决定时，该决定同时携带本次新资源元信息，恢复时与 Outcome 一起写入原生事务。
它允许原 Run 已终止或 epoch 已撤回后的观察，不允许重建已删除的 Session；复制的历史 Call 不能接收新的执行事实。
此入口只接受无业务提案的 `ToolResultValue`。带控制或 Harness 提案的 `ToolResult` 必须通过应用层结算边界处理，
不能被隐式裁掉字段后当作已经接受的状态变更。

Files 在会话自身的 `session-data.sqlite3` 保存 Call、started、Outcome 和 Call 状态，使用原身份三元组与 attempt 定位。
它与 SQLite 后端复用原生事实读写实现，正文只有一份。点查校验所选正文的身份、版本和 attempt；不扫描其他 Call。
Call 的完整 payload 拥有原参数，同一行另存不含 arguments 的小元数据投影；两者由同一次 INSERT 和事务写入。
投影不独立更新，不复制参数正文，也不按消息或读取次数累积。结果点查、只含结果引用的消息批次，以及不比较参数的
提交准备使用该投影。包含 assistant Call 引用的批次读取完整 payload；原 Call 查询、执行、参数比较和转存快照始终
保留完整参数，包括空对象与显式空参数覆盖的区别。structured 结果仍由显式 Outcome 查询读取。
一次 Session 读作用域共用原生连接与读事务，涵盖消息定位、调用事实、摘要、请求头及嵌套查询。
Files 的同会话嵌套读取共用最外层作用域：只在进入边界时取得锁、检查未决提交并观察目录身份，
退出边界时统一释放资源。SQLite 的同会话嵌套读取复用当前资源锁；切换会话仍取得对应资源锁。
读取范围不含工具引用时不查询 Call/Outcome；返回对象独立解码，不跨查询保留正文或共享可变观察结果。
Files 提交准备同样在一个读事务中完成 Session、Run、所选事实、消息位置和计数读取；该事务在文件发布和
原生写事务之前结束。独立 Outcome 的准备与写入共用其写事务连接。连接由读作用域或既有 Run 执行所有权释放，
不跨借用保留事务，不增加全历史缓存。
独立 Outcome 直接提交原生事务；失败完整回滚，提交成功但调用方未收到确认时，可通过事实查询和同内容重提确认结果。
同一 Session 提交中的消息位置、工具事实、Call 状态、Session 头和事件在一个事务中生效；涉及消息日志等文件时
才使用跨文件决定恢复。已有事实的同内容重提由领域准备阶段识别；原生插入不忽略唯一键冲突。
SQLite 使用 tool_calls、tool_call_starts、
tool_outcomes 的复合键，在原生事务内保存同一语义；Outcome 失败不改变此前的 Call、started 或 Session 业务状态。
普通点查不执行恢复，Files 未决决定明确返回需恢复；不扫描其他 Call，不建立全历史身份映射。

Run 的 `active_tool_batch` 是 `ToolBatchHead`，只保存当前批次身份、阶段、顺序 Call 身份和生产者等小头。
独立 `ToolCallState` 保存单个调用的审批、输入等待、子任务等待、attempt、Outcome 引用及结果消息身份。
审批元信息不重复保存修改参数；尚未启动的修改值由 prepared Call 状态拥有，启动后转交 immutable started。
待接受的 Harness 与 Session control 提案属于 Call 状态，确定 Outcome 不持有业务提案。
Agent control 使用独立类型化意图；实际返回结果进入相同观察存储，等待 child 使用 Call 的 agent_wait。

两后端均使用原生 `tool_call_states` 关系保存调用状态。同一次 Session 提交中的事实、
Call 状态和 Run 小头共用原生决定。Call 状态必须引用存在的原 Call、started 和确定 Outcome；
当前待处理状态只能属于当前 epoch 的活动 Run。已发布的未知反馈可以没有 Outcome 引用，迟到 Outcome
单独归档不更新该状态或历史反馈。

执行层通过显式 `load_tool_batch` 读取所选 Call，`ToolCallBatchRecord` 仅为该工作集的内存值。
普通 Run 解码不隐式解引用；只需等待元信息的消费者读取 `load_batch_states`。
模型调用接受时，assistant 消息、原 Call、初始 Call 状态及 Run 小头在同一决定中保存；
每次实际工具或 resume handler 执行前先持久化 started。执行检查点只提交本次改变的调用状态和新事实。

MessageFact 的 `call_refs` 按 source_index 引用一个模型批次的原 Call；`outcome_ref` 固定选择一个
Call 的确定 attempt，或表明本条消息自有的未知/中断反馈。持久消息不复制原参数、结果身份、确定结果正文、资源或时间。
读取只解析所选引用，物化后的 MessageRecord 继续提供模型协议、Host 和历史处理所需的 typed tool_calls/tool_result。
消息与 Call 的 Session、Run、turn、producer 关联和 Outcome attempt 在提交及读取时校验；不从可变 CallState 重建消息。
已发布未知反馈使用固定的 Call 引用与当时字面内容，迟到确定 Outcome 不改变该反馈。

`ToolResultFeedback` 仅拥有框架控制返回或结算错误，不复制工具观察。发布前由 CallState.feedback 暂存；
发布时转交固定消息的 tool_feedback，并与 proposals 一起从 Call 状态清除。反馈必须引用一个确定 Outcome，
不能附着在未知字面反馈上。物化消息将反馈与原观察组合，不查询当前 CallState 决定历史语义。
`load_message_fact` 只点读固定消息骨架；已发布调用的工作集读取可据此取得反馈，而不重复加载 Outcome 正文。

`publish_tool_batch` 一次提交整个 source_index 顺序帧、每个 Call 的结果消息身份及待接受 Harness 变化，
同时把相应 Driver checkpoint 从 pending 改为 ready。冲突提案拒绝整个发布，已经保存的确定 Outcome 保留。
重提已发布批次通过 CallState 的消息身份定向读取完整帧；不存在逐条补齐缺失后缀的发布路径。
中断恢复先读取原 Call/Outcome，必要时构造明确的未知或未执行反馈，再通过同一提交边界发布；不恢复旧业务提案。
Files 未决帧决定使消息、Call 状态与 Run 点查明确报需恢复，显式 mutation 完成决定；SQLite 失败回滚整帧。

完整快照、归档、转存及回收站保留事实与 Call 状态；导入校验消息引用、Call/started/Outcome 闭包、所属 Run 与原执行来源。
分支复制所选消息依赖的事实，保留原执行来源，不复制原 CallState、Run 或执行授权；原参数和确定结果仍各有唯一正文。
正常观察保存、业务回调及中断时反馈的执行约束见[工具规格](tools-and-interactions.md#恢复)。

## 会话标题

标题由 Session 保存，Catalog 和 Host 只投影该值。未指定标题的应用层创建使用英文占位标题
`New conversation`，来源为 `placeholder`。显式传入的标题按调用者声明的来源保存，默认来源为
`fallback`；即使文字等于占位标题，也不视为未命名。

`title_source` 表达标题的来源：

| 来源 | 含义 |
|---|---|
| `placeholder` | 尚未取得可命名的用户输入 |
| `fallback` | 从用户输入提取，或由创建者提供的初始标题 |
| `generated` | 由应用层注入的生成器提供 |
| `user` | 用户显式命名，`title_locked=true` |

标题策略位于 Core，负责空白归一化、长度校验和输入提取；应用层负责创建、显式命名、生成器调用及写入所有权。
消息提交的领域准备阶段只对 `placeholder` 且未锁定的 Session 提取标题，将结果与消息写入同一个
`SessionCommit` 决定。File/SQLite adapter 不解释标题内容，不另建标题更新事务；读取列表不触发补全。

提取只检查本次提交中 `role=user` 且 `scaffold=false` 的消息，选择第一条非空文字，合并连续空白后取前
60 个字符；无文字的显式技能调用使用 `Skill: <name>`。附件回执、附件正文、助手回复和其他脚手架不提供标题。
仅发送附件时保留占位标题，之后首次提交有效文字再补全。上传、文件引用、直接输入和队列消费使用同一规则，
不以附件类型、会话创建时刻或消息计数判断是否命名。已有 `fallback/generated/user` 标题不随后续输入改写。
撤销消息不撤销 Session 标题；分支继承标题及其来源和锁定标记。

所有赋值入口使用同一校验：合并连续空白后，标题必须包含 1–160 个字符。手动重命名将来源设为 `user`
并锁定标题。`SessionService.generate_title` 接受异步 `SessionTitleGenerator`，向其提供 Session 视图和
最早至多 20 条 canonical 消息，不加载全部消息、事件或 Run 历史。生成器返回标题文字；应用层负责校验与提交。
当前没有自动生成调度或内置模型生成器。

生成期间不持有 Session 执行锁。结果写入时，在执行锁和 mutation 边界内重新检查用户命名与 `history_epoch`：
用户命名优先；历史撤销或运行占用使写入延期并返回 diagnostic。生成失败或返回非法标题保留已有标题并返回
diagnostic。消息提取只遍历本次提交批次，不增加历史读取或独立持久化开销。

## 会话摘要查询

`SessionService.summaries(session_id)` 返回按持久化顺序排列的全部保留摘要，类型为
`tuple[SessionSummary, ...]`。每项包含 Session id、Compaction id、生成时间、触发方式、完整正文和
`active` 标记；标记来自同一 Session read 边界读取的当前生效指针，不以最后一条记录推断。
正文来自已持久化的 `CompactionRecord.summary`，不包含失败请求或分块压缩的中间模型输出。

查询不创建 Run、不调用模型、不恢复执行状态，也不读取完整消息或事件历史。空历史返回空元组；会话不存在、
摘要记录损坏或 active 指针引用缺失记录时明确报错。File/SQLite 使用同一接口。
CLI/TUI 自行决定参数、文字和展示样式，框架只返回类型化摘要事实。

## 写入规则

- JSON 快照使用同目录临时文件、flush/fsync 和原子 replace。
- Windows sharing conflict 仅在本地 I/O 原语中有限重试。
- JSONL 追加后执行 flush/fsync。
- `append_messages` 是有序消息批次的持久化边界：SQLite 在当前 mutation transaction 中插入整批；文件后端把
  完整批次编码后以一次 JSONL append/flush/fsync 写入。业务输入通过 `SessionCommit` 将消息、锚点、
  队列消费和 Session 投影一起提交，不在追加成功后另行确认队列。
- 正常模型流不写入永久 delta 事件。当前请求的 text/reasoning 按顺序追加 Draft 后再通知观察者；工具参数只作实时通知。
  两后端的 `response_streams` 保存当前请求身份，`response_drafts` 以请求内位置、channel、content 原生列保存片段。
  Files 使用会话自身的 `session-data.sqlite3`；追加使用 `synchronous=NORMAL` 事务，已有 FULL 业务事务内不得降级。
  请求 started 提交创建空容器，关闭、替换或撤销删除容器及其片段；用量结算不截断已有前缀。
  单条追加语句只向仍属于该请求的容器写入，不创建迟到请求目标，也不逐片重读请求头。
  观察读事务固定当前前缀，WAL 允许追加与读取并行；Files 消息与 Harness 文件仍受原 Session 读取边界保护。
  活动读取直接使用该容器；容器不存在时按请求头区分尚未 started 的空响应与已关闭或被替换的请求。
- 正式/partial assistant 消息、请求头 `response_closed_at/response_message_id`、Draft 清理与
  `model_response_committed` 引用事件同提交。终态无正文也关闭 Draft；请求替换和撤销清除当前 Draft。
  永久响应事件只保存 context/message 身份，观察时在读边界物化正文。关闭后的旧 Draft 游标转向正式事实。
  SQLite 在同一事务中先释放 Draft 再安装正式消息，使最终正文复用已有页；事务失败仍恢复原前缀。
  Files 的正式正文位于 JSONL，其原生数据库使用 FULL auto-vacuum 在提交时回收空闲尾页，
  避免永久保留已关闭 Draft 的最大体积。WAL 检查点和活动读取寿命仍决定物理文件何时完成回收。
- 进程退出后恢复已提交的 Draft 前缀；未提交的原生事务不可见，已提交的非法片段在读取时报告错误。
  真实掉电可能丢失弱同步尾部；进程退出测试不能证明掉电耐久性。done 不是正式响应已经提交的证明。
- 临时文件和 staging 失败必须清理；无法安全回滚时错误中必须包含保留数据位置。
- record id 在参与路径拼接前限制为安全字符和长度。

## SessionStore 契约

端口覆盖：创建 snapshot、Session/Run 保存、消息/事件追加、摘要原子激活、Delegation repository、加载、
索引、回收站发布以及 session lock；部署初始化和 catalog 维护不属于该端口。新的后端必须保持这些行为语义，
而不只是复制方法签名。

`MessageRecord` 显式保存封闭的 `role=system|user|assistant|tool_result`、独立的 `scaffold` 布尔值和可选
`semantic_tag`。这些消息语义字段原样往返，不从其他字段猜测；tool_calls/tool_result 按前述固定事实引用物化。旧字段
严格拒绝，不提供转换或兼容读取。

Harness 是 Session 内可撤销状态，统一包含原生 controls 与按 owner 绑定的 JSON 状态。`harness_version`
指向唯一当前版本；`controls` 与 extension 查询只是该版本的投影，没有独立可写副本。状态变更创建新版本，
无变化读取/回调不创建版本。每条新消息的 `before_harness_version` 表达该消息及后缀撤回时应恢复的状态。
Session 原生头保存当前 `controls` 读取投影，与 `harness_version` 在同一提交中发布；快照创建、撤销和分支
均从选中 Harness 版本取得该投影。它只有当前一份，空间随活动控制状态变化，不随历史消息或检查点数累积。
`load_session_record` 直接读取该头，不展开插件私有状态；需要 Harness 的调用者显式读取版本或使用成对查询。

`SessionRepository.load_session_harness(session_id)` 在一个一致性边界内返回 Session 及其引用的
`HarnessVersion`。文件后端使用 Session 只读边界，未决提交明确报需恢复；SQLite 沿用已有读事务，
处于 mutation 中时复用该事务连接。版本身份、归属、缺失与损坏检查在读取边界保留。
Session 的可变 `controls` 投影与返回的 Harness 控制状态隔离，修改读取结果不会影响另一份视图或后续读取。
此查询不引入跨操作缓存，不加载消息或事件历史，也不创建版本或回执。

两后端的原生连接由现有 Run 执行锁保留：运行期间独占借用空闲连接，每次借用结束必须退出事务；
读取与写入复用普通原生句柄，使最后退出的观察连接也能完成 WAL 收尾；Files 独立读取也使用能完成该收尾的句柄。
只读业务范围仍使用短读事务，不执行领域状态更新或恢复；已有 mutation 中的读取共享当前写事务。
最后一个执行所有者释放时关闭空闲连接，仍在借用的连接归还时关闭。
无执行所有者的普通查询在结束时关闭连接。空闲连接数取决于本次执行期间的并发借用峰值，不随历史长度增长。
不跨事务保留领域模型或历史正文，不改变 FULL 业务提交和 NORMAL Draft 写入的持久化边界。
存储读取将新解码、无其他持有者的事实直接组成查询结果，避免 dump/validate 往返；调用方仍持有原事实时，
纯投影接口返回独立深拷贝，修改视图不得影响源事实或后续存储读取。
消息的原生存储编码省略默认空集合、空文本及空可选字段，但显式保留 schema、身份和时间；
任意 metadata、provider payload 内的 null、空集合、false 和零不被删除。公开 JSON 投影与转存格式不变。
已在存储边界解码的成员直接组成 Session 查询容器，不再次遍历历史执行成员验证；引用闭包检查仍在对应读取/写入边界执行。
SQLite 查询逐行解码，避免同时保留完整的 JSON 行列表和完整模型列表。

Session 单记录查询只解码当前头；当前 Harness 查询和应用层阶段快照使用成对查询，
每次只读取一次所需检查点；显式版本查询按指定版本读取。
提交准备按效果读取：新消息或 Harness 变化读取当前完整检查点，验证消息前版本锚点；
撤销读取所选边界的恢复版本；仅变更请求、Run、事件、输入状态等记录时保留 Session 头的版本和 controls，
不展开无关私有状态。所有提交仍检查调用者观察的版本、history_epoch 和消息头。
文件决定完成/恢复仍独立读取并验证持久检查点，不能复用提交准备的内存值跳过核验。

`SessionCommit` 在同一 mutation 边界检查 history_epoch、版本、消息头和实际使用的队列/控制前置条件，
有序提交状态变化、消息、Run/队列效果与事件，不持久化完整返回值，也不计算整提案摘要。
文件后端先保存版本，再持久化 `commit.json` 决定并用现有类型值完成效果；仅恢复路径读取、解码未完成决定。恢复只完成该决定，
不执行插件或外部工具。SQLite 在一个事务中提交等价效果。提交成功后才通知 Host，派生 Catalog 不进入决定。

RunLifecycle 的预留、完成、失败、暂停及取消使用此提交边界保存 Run 和 Session。预留配置与 queued Run
一起生效，child 派遣无需修补分步写入留下的活动指针。完成或失败的最终 assistant 消息与状态一起提交，
保留当前 Harness 锚点；已先行提交的响应只结算终态。消息位置与 Session 头由提交边界维护，收尾不读取历史正文。
Files 决定未完成时，普通 Session/Run 查询报告需恢复，显式 mutation 完成决定；SQLite 失败时回滚同批状态与消息。

`SessionCommitResult` 仅为本次写入的内存返回值；Files 原生决定可临时包含它，决定完成即清理。
不存在永久 operations 目录、SQLite session_commits 表、历史提交结果查询或完整回执导出。
Session 的 `last_commit_id` 只保存最近一次提交身份，供拥有同一 mutation 边界的调用方在丢失返回时确认结果。
它不是 revision/CAS 前置条件，不因无关字段更新而拒绝提案，也不承诺历史操作的任意重试；
当前身份重复提交明确拒绝，长期幂等由 Input 接受事实、Run continuation 等实际领域身份负责。
SQLite 已失败的嵌套事务禁止读取未提交状态作为成功依据，退出外层边界后回滚全部效果。

命令与控制器的显示输出、取消摘要和接受身份由本次调用保留，取消等待其拥有的写入任务完成并返回实际结果；
不为任意输出建立永久回执。撤销在同一 mutation 边界确认提交并从 Session 头取得结果。
Run continuation 结算后保存 action、选中控制器、reason 和 diagnostics，清除 settlement_plan 中的正文及暂存 Harness；
已结算记录不得继续携带该方案。进程重启通过这些领域状态判断已接受效果，不重新执行已接受的策略或命令回调。

任意 canonical 消息节点都可作为撤回起点。撤销保留此前消息、恢复目标消息前版本并递增 history_epoch，
清除 active/last Run 与失效上下文投影。Run、事件、usage、资源审计与已经接受的输入回执仍保留；原文件
副作用不回滚。文件后端按索引/尾部定位原地裁剪，不能复制保留前缀；SQLite 按索引定位并删除范围。

Delegation 以 root Session 为聚合边界，使用 `(root_session_id, idempotency_key)` 唯一约束、child Session 唯一
反向查询和 revision compare-and-swap。运行路径携带 root identity；已知父 Run 时，列表同时按 `parent_run_id`
定向定位。共享预算、父终态绑定子任务结算及父 ToolCall 导航不读取同一 root 的其他父 Run 委派。
按全局 Delegation ID 导航可先定位所属 root，再执行该 root 的只读查询；Files 冷定位只枚举匹配文件路径。
显式 root 全列表仍读取全部委派，其损坏不影响其他 root 或不涉及该记录的定向查询。
Files 保存 canonical `delegations/<id>.json`，request/child 指针及 parent Run 成员与记录在同一有限
`commit.json` 决定中安装；中断由显式 mutation 完成，完成后清理决定。索引不拥有委派正文，查询不重建索引。
opaque 幂等请求键仅为路径定位编码为 SHA-256 文件名，不散列正文；业务 ID 沿用路径隔离校验。
SQLite 在原生事务中维护 payload 和 root/request、child、root/parent Run 索引。完整转存从委派记录重建索引。
CAS 冲突抛出 `DelegationRevisionConflictError`，不能与普通持久化损坏混为一类。
CAS 还保护已接受的冻结定义、权限上限、路径约束、交互模式及预算不被生命周期更新替换；字段范围见
[子智能体权限契约](subagents.md#权限与能力继承)。

运行路径使用 `RuntimeSessionSnapshot`，只包含 Session、当前 epoch 的待处理 Run 和 active/last 指针指向的 Run，
不包含完整 Run 历史、消息、摘要或事件。按明确身份执行审批、回答、委派恢复时直接点读目标 Run。

`load_pending_runs` 选择仍 active、可恢复且批次未发布完成，或 agent continuation 尚未结算的当前代 Run。
两后端在 Run 行保存查询用的 history_epoch、needs_recovery 列及部分索引，由原生事务同时更新。
独立 Run 保存与 Session 的 run_count 增量在同一数据库事务内完成；无独立计数文件、pending 成员文件或 Run 决定槽。
Files 普通 Session 提交在同一原生事务内更新 Session、Run、消息位置和相关事实；涉及 JSONL 的有限决定仍由
commit.json 协调。运行所有权持有期间复用空闲原生连接，查询和写入的事务不跨调用保留；目录替换或移入回收站前，
持有 Session mutation 的调用者关闭空闲连接，避免连接继续指向旧数据库。
撤销在同一提交中将当前待处理 Run 的未结算 continuation 置为 resolved/pause，原因为 `session_history_withdrawn`，
释放其 settlement plan 并移除旧代成员；保留原 Run 及调用审计状态，不执行控制器或插件回调。
随后更新旧 Run 审计状态不会重新进入当前执行集合。无索引重建缓存或第二套日志。
存在未决文件决定时仍需显式恢复；Run 原生行与其正文身份不匹配时明确报错，不静默忽略。
Session 的 active/last 指针由显式恢复定向核对，失效指针清除。
`SessionSnapshot` 是包含完整历史的查询与回放视图；`SessionObservationState` 保留显式观察所需的完整消息、
摘要与高水位，不含活动 Run 的历史事件。Application 的窗口观察只读指定消息范围，并从当前 Call/Draft/请求
恢复交互与输出；具体契约见[会话观察](session-observation.md#session-snapshot-与事件游标)。
application 只有在检查、导出、回放或重建活动投影时才能请求完整快照，普通 Run、工具恢复、上下文
准备和队列消费不得为了 Session 状态或 event sequence 加载完整事件日志。event sequence 由 Store 通过独立
轻量端口提供。只需要 Run 元数据的检查（如插件 durable generation 引用扫描）使用独立的 Run 记录只读端口，
不为此加载消息或事件日志。

消息查询提供 `load_run_messages`、`load_message` 与 `find_queued_input_message`，分别按 Run、message id 和输入
identity 定位。文件后端按 ID 读取事实、按 Run/input 读取所属成员引用；SQLite 使用对应的 Run 和 Input
表达式索引。点查、结果交付与等待回执验证不得为此加载完整 runtime session，具体布局见前述消息事实契约。

续跑通过 Run 成员查询确认旧代次仍有保留消息，并通过输入成员查询确认历史存在 user/system 输入，包含 scaffold。
消息角色属于 Files 的成员元数据；最后 assistant 回复使用按 Run 分组的成员索引，SQLite 使用对应部分索引。
这两个查询不物化无关正文；索引与消息事实沿用同一提交和撤销边界，不形成另一份正文来源。
Run 结果查询只读取指定 Run 和其最后一条 assistant 回复。Host 已消费回执按 Input 身份定位消息，
插件输入批次通过 InputState 确认消费。
Files 的完整快照、执行快照与 Run 列表查询使用只读边界；有未决决定时报告局部需恢复，由显式入口完成。

Host 观察使用 `SessionObservationState` 在一个 Session mutation 边界内取得 canonical transcript、active Run
事件和 event high-water。Local adapter 在稳定路径从 per-Run byte offset 读取 active Run，不扫描 terminal Run；
`load_events_after` 通过稀疏 sequence checkpoint 将前向扫描限制为 checkpoint 间隔加实际返回前缀。旧会话首次
缺少 Run offset 时可以扫描一次 canonical journal 并写入派生索引。SQLite 必须以 `(session_id, run_id)` 和
`(session_id, sequence)` 索引完成等价查询；前者包含 `row_id` 以直接满足稳定顺序，后者可由 canonical sequence
唯一约束提供，不建立重复索引。

## 锁与并发

- session run lock 由 OS descriptor 持有。
- 锁文件只保存诊断信息，不作为存活事实。
- 同一 session 的运行、模型切换、分支敏感操作必须尊重 active Run 和 lock。
- catalog 使用独立锁，不能阻塞 canonical Session 打开。
- root Session mutation lock 串行化同一 root 的 Delegation 创建、CAS 更新和查询线性化点；process-local
  Agent 生命周期锁只缩小竞态窗口，不能替代 durable revision。

## 恢复

显式恢复命令只处理当前 history_epoch 中无 live owner 的 queued/running Run；Supervisor 已接管的 job
不进入恢复。queued 尚未取得执行锁不构成中断证据，Host 选择会话时不据此触发恢复。满足恢复条件时：

- Run 转为 `recoverable`；
- Session 清除 active run 并恢复可操作状态；
- 活动工具批次中 running 调用转为 `unknown_outcome`；
- 最后模型轮次的 text/reasoning delta 可投影为 incomplete assistant message；
- 只有随 durable assistant 工具消息提交的完整 `provider_continuation` 才可恢复；流中未提交的 opaque
  continuation 不从文本事件猜测或重建；
- 已有 durable assistant/tool message 的模型轮次不能重复投影；
- 恢复事件只写入一次。

恢复的 partial 消息、Run 状态、Call 未知状态、Session 投影与恢复事件属于同一个 Session 提交。
消息序号、总数与末尾身份由该提交基于当前 Session 头分配；恢复不从完整历史列表重建消息头。
是否已有正式模型回包按 Session/Run/turn 点查，包含 assistant 工具调用消息。
只有当前代次、无 live owner 的活动 Run 才读取 Call 状态，已终结 Run 的批次不参与中断解释。

恢复不重放工具调用，不把 partial assistant 输出视为完成答案。
无未完成 Run 的常规加载不读取 events；只有检测到需要恢复的中断 Run 时，Store 才加载恢复所需的模型流
事件。该读取属于冷恢复路径，不能污染后续 Run 的正常成本。

进程启动不是 Session 恢复边界。LocalApplication 和 RuntimeSupervisor 启动不得枚举 Session、Run、消息、事件或
Delegation；历史数量和单个历史记录损坏不能改变服务启动成本或可用性。显式执行或恢复入口针对目标 Session，
只读观察不运行恢复。若目标属于 Agent aggregate，则恢复范围最多扩展到同一 `agent_root_session_id` 下由 Delegation 明确引用的
child Session。目标记录错误只使该目标操作 fail-closed，新 Session 和其他 primary Session 仍可运行。

目标 Session/Run 恢复之后，Agent reconciliation 按单向提交阶段核对 Delegation、child Session backlink、child Run
provenance 和冻结 contract。只有唯一可证明归属的记录才补齐；active child 转 `recoverable`，unknown outcome
不自动重放。terminal child 可重建缺失的 DelegationResult，pending delivery 通过稳定 delivery identity 对账，
不会重复写父消息或启动父 Run。无法证明一致的半提交关系 fail-closed，并保留全部 canonical 证据。

## 会话查询与维护

application 提供：

- catalog 搜索、状态/归档/置顶/工作区/模型/标签筛选与排序；
- show、rename、archive、pin、tag；
- messages、events、export 和 replay；
- branch 与 trash。

Catalog 与 Host DTO 均不保存完整 transcript；文本查询只覆盖 title、至多 160 字符的最后消息预览、workspace、
model 和 tags。最后消息预览只选择消息提供方明确声明为 `scaffold=false` 的 user/assistant 消息；不得从 role、
semantic tag 或 producer provenance 猜测脚手架属性。全文搜索是独立未来能力，不能借 disposable Catalog 复制
canonical 对话。

root Session 有 active 或无法判定的 Agent descendant 时不能归档。trash 与归档语义不同：正常生命周期路径通过
`SessionRecord.parent_session_id` 解析 owning primary 和传递 subagent scope；Host 已取得用户明确删除确认、但 canonical
记录无法可靠解码时，Application 可改用 Catalog 中稳定的 `session_id/root_session_id/parent_session_id` 确定可见树范围。
两条路径都不得通过 Run 或 Delegation 反推删除 scope；`session_kind`、Catalog health 和 canonical 可读性不是删除授权条件。

## Branch

- 分支点必须存在于 canonical messages。
- 工具交换必须完整；请求或结果中间不能作为孤立边界。
- 分支消息获得新 id、清除 run/turn id，并记录 source identity。
- 分支 lineage 写入 root/parent/fork 字段。
- 含 `resource_refs` 的分支通过 ResourceService 验证存在性，并与消息一起原子发布引用关系。
- 分支当前 Harness 取所选消息之后的边界；分支头取当前版本。`copy/reset/reject` 一致作用于保留消息
  锚点引用的全部版本。reset 移除 owner 状态和声明的脚手架投影，不能经分支内撤销复活；reject 不能被当前
  空状态绕过。分支点若是必须 reset 的投影则拒绝。
- 重映射 Session、消息与版本身份。activation identity 按 Session 归属，与不透明 owner payload 一起保留，
  不单独重写其中一侧引用。
- 完整 snapshot/export 包含 canonical Session、Harness 版本、Input 事实与生命周期状态；File/SQLite 转换保留锚点、代次
  与去重事实。SQLite 完整快照采用一个只读事务。普通消息分页和 Catalog 不暴露 owner 私有状态。

## 回收站

正常生命周期删除先对整树中确有 live owner 的 Run 复用 canonical abort transition，等待运行方观察 durable abort
并释放锁；超时则 fail-closed。无法解码 canonical 的显式强制删除不猜测 Run 状态，直接按稳定顺序尝试取得范围内
全部 Session 锁，遇到 live owner 同样 fail-closed；持久化为 running/waiting 但没有 live owner 的旧 Run 直接随树封存。
Local backend 使用 tree staging 后原子发布到
`trash/sessions/<trash_id>/sessions/<session_id>`；SQLite backend 在一个事务中封存原始 rows 并删除整树。发布失败
必须回滚全部 Session，或明确报告数据保留位置。初始化负责完成可判定安全的中断 tree publish。
Harness 版本与 Input 接受事实随同一 Session tree 或外键事务封存，不依赖 Observer 做事后状态清理。

附件随所属会话回收：文件后端将资源目录包含在上述会话目录内；SQLite 将资源文件移至
`trash/sessions/<trash_id>/<session_id>/resources`，并在数据库封存资源记录。普通事务失败恢复已移动的目录；
文件与数据库不承诺掉电级联合事务。其他会话拥有的独立副本保持可用。

Host 可以继续用“已归档或存在索引/canonical 错误”限制普通删除菜单的可见范围；一旦用户明确确认删除，确认本身就是
破坏性操作授权。Application 的 Catalog-scope trash 不读取目标 canonical observation，也不以 `catalog_error`、health
或其新旧程度重新授权；这些可重建语义信息不能阻止底层按稳定 ID 执行真实删除。实体存在性、范围内 live-owner 锁与
后端原子发布仍是必须保留的结构约束。

## 验收证据

- `tests/core/test_persistence.py`
- `tests/sessions/test_store_contract.py`
- `tests/sessions/test_local_files.py`
- `tests/sessions/test_local_io.py`
- `tests/sessions/test_catalog_projection.py`
- `tests/application/test_sessions.py`
- `tests/application/test_session_models.py`
- `tests/sessions/test_delegations.py`
- `tests/application/test_agent_recovery.py`
- `tests/plugins/test_session_state.py`
- `tests/cli/test_session_commands.py`
