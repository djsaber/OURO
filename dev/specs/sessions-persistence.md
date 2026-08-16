# 会话与持久化规格

状态：当前

## 目标与范围

持久化层保存可恢复的 canonical Session 数据，并提供可替换的 `SessionStore` 端口。默认实现是
`LocalSessionStore`；application 不依赖本地文件路径和原子替换细节。

## 数据布局

```text
OURO_HOME/
  layout.json
  config/config.json
  skills/
  locks/sessions/
  sessions/
    index.json
    <session_id>/
      session.json
      messages.jsonl
      events.jsonl
      compactions.jsonl
      runs/<run_id>.json
      delegations/<delegation_id>.json
      resources/index.json
      extensions/index.json
  resources/
    records/<resource_id>.json
    blobs/sha256/<digest>
    index.json
    staging/
  trash/sessions/<trash_id>/
```

当前 `data_layout_version` 为 6；版本 3/4 会确定性升级 delegation 目录语义并为每个现有 Session 创建版本化
extension state collection；版本 5 的 transcript-bearing disposable Catalog 直接丢弃并按有界投影重建。只有全部
canonical 迁移成功后才推进 layout manifest；迁移可重入。其他不兼容布局必须在
初始化时抛出 `MigrationRequiredError`，不能猜测迁移。

版本 6 启动期仍执行一次性 builtin Goal/Plan/Todo 私有文件迁移，成功后删除源文件，孤儿移入 trash，运行时没有
读回旧路径的 fallback。该启动扫描的删除窗口是 `data_layout_version=7`：v7 migrator 必须先转换或明确拒绝全部旧
路径，再从 composition root 删除 `migrate_builtin_session_state`。同理，无 `turn_id` 的历史未完成模型轮次只在 v6
冷恢复中使用保守去重；v7 迁移必须终结该历史形态，不能让时间比较继续进入当前 Run 热路径。

## Canonical 数据与投影

Canonical 数据包括：

- `session.json`；
- `messages.jsonl`；
- `events.jsonl`；
- `runs/*.json`；
- root Session 下的 `delegations/*.json`；
- `compactions.jsonl`。
- `extensions/index.json` 中按稳定 owner identity 唯一的 Session extension state。

Session、Run、Message、Event、Compaction、ContextSnapshot 和 Delegation 是独立持久化根记录。每个 payload
必须显式携带当前 `schema_version=1`；缺失、非整数、旧版本或未来版本均由共享 versioned decoder 在 adapter
读取边界抛出 `MigrationRequiredError`。领域模型的版本字段使用封闭字面量类型，未知版本不能被验证为当前记录。
Store 不通过字段默认值猜测旧格式；需要兼容的历史版本必须先通过独立、可测试的 migrator 转换。

`data_layout_version`、SQLite schema version 和插件 manifest/state schema 是独立版本域，不能因为当前数字相同
而共用迁移规则。Session resource reference index 与 Session catalog 是 layout-bound 文档，不冒充核心 record 版本。

`sessions/index.json` 是可重建查询投影，不属于 canonical transaction。索引更新失败时：

1. canonical 写入保持成功；
2. Session 写路径只在 catalog lock 下写入 dirty marker，不同步读取或重建全量索引；
3. 进程初始化只把既有索引标记为待刷新，不解析索引或扫描 Session 目录；
4. 只有显式 catalog 查询才扫描 canonical Session 并重建索引；
5. dirty marker 写入或锁失败时 application 返回 diagnostic。

## 写入规则

- JSON 快照使用同目录临时文件、flush/fsync 和原子 replace。
- Windows sharing conflict 仅在本地 I/O 原语中有限重试。
- JSONL 追加后执行 flush/fsync。
- `append_messages` 是有序消息批次的持久化边界：SQLite 在当前 mutation transaction 中插入整批；文件后端把
  完整批次编码后以一次 JSONL append/flush/fsync 写入。调用方只在批次成功后推进 Session 投影和确认队列。
- 临时文件和 staging 失败必须清理；无法安全回滚时错误中必须包含保留数据位置。
- record id 在参与路径拼接前限制为安全字符和长度。

## SessionStore 契约

端口覆盖：创建 snapshot、Session/Run 保存、消息/事件/压缩追加、压缩激活、Delegation repository、加载、
索引、回收站发布以及 session lock；部署初始化和 catalog 维护不属于该端口。新的后端必须保持这些行为语义，
而不只是复制方法签名。

`MessageRecord` 显式保存封闭的 `role=system|user|assistant|tool_result`、独立的 `scaffold` 布尔值和可选
`semantic_tag`。Store 逐字段原样往返，不从 role、标签、metadata 或 producer provenance 重建任何一个字段；旧字段
严格拒绝。若未来明确决定保留旧数据，只能通过独立 migrator 转换，不能在读取热路径兼容。

Session extension state 以 `(session_id, owner_id)` 为 identity，并显式保存自身 schema、branch policy、派生
脚手架投影的 semantic tags 和 JSON payload。文件后端把版本化 collection 保存到 Session tree；SQLite schema v3 使用
外键级联的 `session_extension_states` 表。插件代码 Generation 只拥有代码/执行资源，不拥有这些状态的生命周期。

Delegation 以 root Session 为聚合边界，使用 `(root_session_id, idempotency_key)` 唯一约束、child Session 唯一
反向查询和 revision compare-and-swap。所有 Delegation 查询必须携带 root identity；单个 Session 的运行路径不得
扫描其他 root，历史 root 的不兼容记录只能使该 root fail-closed，不能污染新 Session。文件后端保存 root-local 原子 JSON；SQLite schema version 2 使用
`delegations` 表和等价索引。CAS 冲突抛出 `DelegationRevisionConflictError`，不能与普通持久化损坏混为一类。

运行热路径使用不含 events 的 `RuntimeSessionSnapshot`；`SessionSnapshot` 是包含完整事件历史的查询与回放
视图。application 只有在检查、导出、回放或重建活动投影时才能请求完整快照，普通 Run、工具恢复、上下文
准备和队列消费不得为了 Session 状态或 event sequence 加载完整事件日志。event sequence 由 Store 通过独立
轻量端口提供。只需要 Run 元数据的检查（如插件 durable generation 引用扫描）使用独立的 Run 记录只读端口，
不为此加载消息或事件日志。

## 锁与并发

- session run lock 由 OS descriptor 持有。
- 锁文件只保存诊断信息，不作为存活事实。
- 同一 session 的运行、模型切换、分支敏感操作必须尊重 active Run 和 lock。
- catalog 使用独立锁，不能阻塞 canonical Session 打开。
- root Session mutation lock 串行化同一 root 的 Delegation 创建、CAS 更新和查询线性化点；process-local
  Agent 生命周期锁只缩小竞态窗口，不能替代 durable revision。

## 恢复

当不存在 live lock 且发现 queued/running Run 时：

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
Delegation；历史数量和单个历史记录损坏不能改变服务启动成本或可用性。Host 明确打开/运行目标 Session 时才先恢复
该 Session；若它属于 Agent aggregate，则恢复范围最多扩展到同一 `agent_root_session_id` 下由 Delegation 明确引用的
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
model 和 tags。全文搜索是独立未来能力，不能借 disposable Catalog 复制 canonical 对话。

root Session 有 active 或无法判定的 Agent descendant 时不能归档。trash 与归档语义不同：删除任意 root 或 subagent
都会先通过 `SessionRecord.parent_session_id` 解析 owning primary 和传递 subagent scope，再删除完整根树。该路径不得
通过 Run 或 Delegation 反推删除 scope；不兼容的旧执行记录不能恢复执行，但必须仍可被生命周期管理。

## Branch

- 分支点必须存在于 canonical messages。
- 工具交换必须完整；请求或结果中间不能作为孤立边界。
- 分支消息获得新 id、清除 run/turn id，并记录 source identity。
- 分支 lineage 写入 root/parent/fork 字段。
- 含 `resource_refs` 的分支通过 ResourceService 验证存在性，并与消息一起原子发布引用关系。
- extension state 的 `copy` 会随目标 Session 原子复制；`reset` 不复制并移除 owner 声明的脚手架投影；
  `reject` 明确拒绝分支。若请求分支点自身是必须 reset 的投影，则拒绝生成自相矛盾的 lineage。
- export 包含完整、版本化 extension state section；普通消息分页和 Catalog 不暴露该私有状态。

## 回收站

删除先对整树中确有 live owner 的 Run 复用 canonical abort transition，等待运行方观察 durable abort 并释放锁；
超时则 fail-closed，不越过仍在写入的 owner。持久化为 running/waiting 但没有 live owner 的旧 Run 直接随树封存。
随后按稳定顺序取得整树全部 Run 锁。Local backend 使用 tree staging 后原子发布到
`trash/sessions/<trash_id>/sessions/<session_id>`；SQLite backend 在一个事务中封存原始 rows 并删除整树。发布失败
必须回滚全部 Session，或明确报告数据保留位置。初始化负责完成可判定安全的中断 tree publish。
extension state 位于同一 Session tree 或外键事务内，因此不依赖 fail-open Observer 做事后清理。

发布 trash 后，ResourceService 对该 Session 原引用执行可达性清理；共享资源仍被其他活动 Session 引用时
不得删除。清理失败只形成可重试 diagnostic，不破坏已经完成的 trash 事务。

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
