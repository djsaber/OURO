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
      session.json
      messages.jsonl
      events.jsonl
      event-index/sequences.bin
      event-index/runs/<run_id>.json
      compactions.jsonl
      context.json
      context-index.json
      runs/<run_id>.json
      delegations/<delegation_id>.json
      resources/index.json
      resources/files/<filename>
      resources/.staging/
      harness/<version_id>.json
      operations/<operation_id>.json
      commit.json  # 仅存在于未完成的原生提交期间
  trash/sessions/<trash_id>/
```

当前 `data_layout_version` 为 9，SQLite schema 为 7；各持久记录的版本见下文 Canonical 数据与投影。
SQLite 同样使用会话文件目录保存附件，资源元信息保存在数据库；跨后端传输使用统一归档。
初始化只接受当前格式；不支持或缺失的版本抛出 `UnsupportedDataVersionError`，不扫描或转换历史记录。
空数据目录/数据库初始化为当前格式。已有数据不自动清空，由用户选择新目录或清理失效测试数据。

SQLite 初始化在数据库旁 `.<数据库文件名>.locks/initialize.lock` 下取得跨进程短锁，等待上限为 5 秒。
schema 检查、WAL 设置和版本记录创建在同一初始化所有权内完成，防止并发启动重复创建版本；后续 Session
业务读写仍使用原有 SQLite 事务和 Session 执行锁。

当前格式的未完成提交仍按原生决定恢复，不重新运行插件、转换 schema 或重写审计事实。
owner 业务状态由相应执行契约读取，不支持的 schema 明确拒绝；没有状态迁移命令。
当前格式的 Session、Run、消息与 Harness 可在插件变化后继续读取。ToolInvocation 保存 resumable 声明及
实际执行所需的插件契约或 Host 工具定义摘要；恢复核对保存的执行契约、当前定义及授权，不能推断未知私有状态。
历史 Catalog/generation 摘要仅用于审计；队列接受凭据不授权恢复旧环境。

## Canonical 数据与投影

Canonical 数据包括：

- `session.json`；
- `messages.jsonl`；
- `events.jsonl`；
- `runs/*.json`；
- root Session 下的 `delegations/*.json`；
- `compactions.jsonl`；
- 最新模型请求的 `context.json`；
- 所属会话的资源元信息与不可变附件文件；
- 不可变 Harness 版本、Session 的当前版本引用和消息前版本锚点；
- 原生提交回执及文件后端的未完成提交决定。

`event-index/` 是 Local adapter 私有、可丢弃的查询加速器，不属于 canonical 数据，也不推进 data layout version。
`event-index/runs/<run_id>.json` 保存首次可能 event byte offset，`sequences.bin` 保存固定宽度的稀疏
sequence→byte checkpoint。
缺失、截断或损坏时读取必须回退到 `events.jsonl`，且可在目标 Session mutation 边界内惰性重建；索引绝不能使
canonical Session 不可读或隐藏事件。派生索引维护是 best-effort 且不执行 fsync，失败不能阻断 canonical append；
offset 0 由索引缺失隐式表达，只有游标能跳过非空历史前缀时才落盘。

`load_latest_context_header` 只返回请求身份、时间和 metrics，不物化 provider 参数、消息或媒体正文。
文件后端在写入 `context.json` 后，以同一 Session 锁维护可丢弃的 `context-index.json`；索引记录文件身份、大小和
修改时间，读取时逐项核对。缺失、损坏或失效时读取并校验 canonical context，查询不写入索引；索引写入失败不改变
canonical 写入结果。SQLite 的 `latest_context_headers` 覆盖表达式索引与 context 行在同一事务更新，查询直接读取
去除 provider 参数后的投影。派生索引不改变持久化版本，完整 context 查询、导出和 digest 校验仍读取 canonical 数据。

Session、Run、Message、Event、Compaction、ContextSnapshot 和 Delegation 是独立持久化根记录。每个 payload
必须显式携带各自当前 `schema_version`：Session/Event 为 2，Run/ContextSnapshot 为 5，Message 为 4，Compaction 为 2，Delegation 为 1。
缺失、非整数、旧版本或未来版本均由共享 versioned decoder 在 adapter
读取边界抛出 `UnsupportedDataVersionError`。领域模型校验各自版本，未知版本不能被验证为当前记录。
Store 不通过字段默认值猜测旧格式，也不提供历史版本转换。

`data_layout_version`、SQLite schema version 和插件 manifest/state schema 是独立版本域，不能因为当前数字相同
而共用版本判定。Session resource reference index 与 Session catalog 是 layout-bound 文档，不冒充核心 record 版本。

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
`active` 标记；标记来自同一 Session mutation 边界读取的当前生效指针，不以最后一条记录推断。
正文来自已持久化的 `CompactionRecord.summary`，不包含失败请求或分块压缩的中间模型输出。

查询不创建 Run、不调用模型、不恢复执行状态，也不读取完整消息或事件历史。空历史返回空元组；会话不存在、
摘要记录损坏或 active 指针引用缺失记录时明确报错。File/SQLite 使用同一接口，不新增持久化格式。
CLI/TUI 自行决定参数、文字和展示样式，框架只返回类型化摘要事实。

## 写入规则

- JSON 快照使用同目录临时文件、flush/fsync 和原子 replace。
- Windows sharing conflict 仅在本地 I/O 原语中有限重试。
- JSONL 追加后执行 flush/fsync。
- `append_messages` 是有序消息批次的持久化边界：SQLite 在当前 mutation transaction 中插入整批；文件后端把
  完整批次编码后以一次 JSONL append/flush/fsync 写入。业务输入通过 `SessionCommit` 将消息、锚点、
  队列消费和 Session 投影一起提交，不在追加成功后另行确认队列。
- `model_text_delta`、`model_reasoning_delta` 和 `model_tool_call_delta` 使用分组耐久性：每条记录在返回前必须完成
  原子、有序且对其他读取连接可见的提交，但不为每个分片单独执行存储同步。文件后端追加后由下一非 delta 边界
  flush/fsync；SQLite WAL 对 delta transaction 使用 `synchronous=NORMAL`，下一非 delta transaction 保持
  `synchronous=FULL`，其提交同步此前已提交的 WAL 前缀。不得把 NORMAL 扩展到 Run、消息、工具或其他业务边界。
- 进程异常退出后已提交的 delta 仍由 WAL/JSONL 恢复；机器掉电可能丢失最后一个尚未遇到 durable boundary 的 delta
  分组，因此恢复逻辑必须继续把未完成模型轮次视为可恢复前缀，不能伪造 `model_message_completed`。
- 临时文件和 staging 失败必须清理；无法安全回滚时错误中必须包含保留数据位置。
- record id 在参与路径拼接前限制为安全字符和长度。

## SessionStore 契约

端口覆盖：创建 snapshot、Session/Run 保存、消息/事件/压缩追加、压缩激活、Delegation repository、加载、
索引、回收站发布以及 session lock；部署初始化和 catalog 维护不属于该端口。新的后端必须保持这些行为语义，
而不只是复制方法签名。

`MessageRecord` 显式保存封闭的 `role=system|user|assistant|tool_result`、独立的 `scaffold` 布尔值和可选
`semantic_tag`。Store 逐字段原样往返，不从 role、标签、metadata 或 producer provenance 重建任何一个字段；旧字段
严格拒绝，不提供转换或兼容读取。

Harness 是 Session 内可撤销状态，统一包含原生 controls 与按 owner 绑定的 JSON 状态。`harness_version`
指向唯一当前版本；`controls` 与 extension 查询只是该版本的投影，没有独立可写副本。状态变更创建新版本，
无变化读取/回调不创建版本。每条新消息的 `before_harness_version` 表达该消息及后缀撤回时应恢复的状态。

`SessionRepository.load_session_harness(session_id)` 在一个一致性边界内返回 Session 及其引用的
`HarnessVersion`。文件后端沿用 Session mutation 锁并先完成未决提交恢复；SQLite 沿用已有读事务，
处于 mutation 中时复用该事务连接。版本身份、归属、缺失与损坏检查在读取边界保留。
Session 的可变 `controls` 投影与返回的 Harness 控制状态隔离，修改读取结果不会影响另一份视图或后续读取。
此查询不引入跨操作缓存，不加载消息或事件历史，也不创建版本或回执。

Session 单记录查询、当前 Harness 查询和应用层阶段快照复用同一读取实现；需要 Session 与 Harness 的调用者
使用成对查询，不再先取含 controls 投影的 Session 再读取同一版本。显式版本查询保持原有行为。
提交准备同样复用成对读取；文件决定完成/恢复仍独立读取并验证持久检查点，不能复用提交准备的内存值跳过核验。

`SessionCommit` 在同一 mutation 边界检查 history_epoch、版本、消息头和实际使用的队列/控制前置条件，
有序提交状态变化、消息、Run/队列效果、事件和回执。operation identity 与请求摘要确保幂等；不同请求复用
同一 identity 明确冲突。文件后端先保存版本，再持久化 `commit.json` 决定并完成效果；恢复只完成该决定，
不执行插件或外部工具。SQLite 在一个事务中提交等价效果。提交成功后才通知 Host，派生 Catalog 不进入决定。

任意 canonical 消息节点都可作为撤回起点。撤销保留此前消息、恢复目标消息前版本并递增 history_epoch，
清除 active/last Run 与失效上下文投影。Run、事件、usage、资源审计与已经接受的输入回执仍保留；原文件
副作用不回滚。文件后端按索引/尾部定位原地裁剪，不能复制保留前缀；SQLite 按索引定位并删除范围。

Delegation 以 root Session 为聚合边界，使用 `(root_session_id, idempotency_key)` 唯一约束、child Session 唯一
反向查询和 revision compare-and-swap。所有 Delegation 查询必须携带 root identity；单个 Session 的运行路径不得
扫描其他 root，历史 root 的不兼容记录只能使该 root fail-closed，不能污染新 Session。文件后端保存 root-local 原子 JSON；SQLite 使用
`delegations` 表和等价索引。CAS 冲突抛出 `DelegationRevisionConflictError`，不能与普通持久化损坏混为一类。

运行热路径使用不含 events 的 `RuntimeSessionSnapshot`；`SessionSnapshot` 是包含完整事件历史的查询与回放
视图。application 只有在检查、导出、回放或重建活动投影时才能请求完整快照，普通 Run、工具恢复、上下文
准备和队列消费不得为了 Session 状态或 event sequence 加载完整事件日志。event sequence 由 Store 通过独立
轻量端口提供。只需要 Run 元数据的检查（如插件 durable generation 引用扫描）使用独立的 Run 记录只读端口，
不为此加载消息或事件日志。

消息查询提供 `load_run_messages`、`load_message` 与 `find_queued_input_message`，分别按 Run、message id 和输入
receipt 定位。文件后端按需为目标 journal 建立可丢弃的内存字节偏移索引，最多保留八个会话索引；只在首次查询
时读取该 journal，后续仅解析追加部分并读取命中记录。canonical history_epoch、文件身份和收缩共同识别
历史替换；同长度或更长的重写也必须使旧索引失效。索引不持久化、不保存消息正文，也不扫描其他会话。SQLite 使用对应的 Run 和 queue id
表达式索引。点查、结果交付与等待回执验证不得为此加载完整 runtime session。

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
- 完整 snapshot/export 包含 canonical Session、Harness 版本和提交回执；File/SQLite 转换保留锚点、代次
  与去重事实。SQLite 完整快照采用一个只读事务。普通消息分页和 Catalog 不暴露 owner 私有状态。

## 回收站

正常生命周期删除先对整树中确有 live owner 的 Run 复用 canonical abort transition，等待运行方观察 durable abort
并释放锁；超时则 fail-closed。无法解码 canonical 的显式强制删除不猜测 Run 状态，直接按稳定顺序尝试取得范围内
全部 Session 锁，遇到 live owner 同样 fail-closed；持久化为 running/waiting 但没有 live owner 的旧 Run 直接随树封存。
Local backend 使用 tree staging 后原子发布到
`trash/sessions/<trash_id>/sessions/<session_id>`；SQLite backend 在一个事务中封存原始 rows 并删除整树。发布失败
必须回滚全部 Session，或明确报告数据保留位置。初始化负责完成可判定安全的中断 tree publish。
Harness 版本和提交回执随同一 Session tree 或外键事务封存，不依赖 Observer 做事后状态清理。

附件随所属会话回收：文件后端将资源目录包含在上述会话目录内；SQLite 将资源文件移至
`trash/sessions/<trash_id>/<session_id>/resources`，并在数据库封存资源记录。普通事务失败恢复已移动的目录；
文件与数据库不承诺掉电级联合事务。其他会话拥有的独立副本保持可用。

Host 可以继续用“已归档或存在索引/canonical 错误”限制普通删除菜单的可见范围；一旦用户明确确认删除，确认本身就是
破坏性操作授权。Application 的 Catalog-scope trash 不再读取目标 canonical observation，也不以 `catalog_error`、health
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
