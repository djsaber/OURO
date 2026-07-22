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
  resources/
    records/<resource_id>.json
    blobs/sha256/<digest>
    index.json
    staging/
  trash/sessions/<trash_id>/
```

当前 `data_layout_version` 为 4；版本 3 只执行增加 delegation 目录语义的确定性升级。其他不兼容布局必须在
初始化时抛出 `MigrationRequiredError`，不能猜测迁移。

## Canonical 数据与投影

Canonical 数据包括：

- `session.json`；
- `messages.jsonl`；
- `events.jsonl`；
- `runs/*.json`；
- root Session 下的 `delegations/*.json`；
- `compactions.jsonl`。

`sessions/index.json` 是可重建查询投影，不属于 canonical transaction。索引更新失败时：

1. canonical 写入保持成功；
2. 写入 dirty marker；
3. application 返回 diagnostic；
4. 后续查询可扫描或重建索引。

## 写入规则

- JSON 快照使用同目录临时文件、flush/fsync 和原子 replace。
- Windows sharing conflict 仅在本地 I/O 原语中有限重试。
- JSONL 追加后执行 flush/fsync。
- 临时文件和 staging 失败必须清理；无法安全回滚时错误中必须包含保留数据位置。
- record id 在参与路径拼接前限制为安全字符和长度。

## SessionStore 契约

端口覆盖：创建 snapshot、Session/Run 保存、消息/事件/压缩追加、压缩激活、Delegation repository、加载、
索引、回收站发布以及 session lock；部署初始化和 catalog 维护不属于该端口。新的后端必须保持这些行为语义，
而不只是复制方法签名。

Delegation 以 root Session 为聚合边界，使用 `(root_session_id, idempotency_key)` 唯一约束、child Session 唯一
反向查询和 revision compare-and-swap。文件后端保存 root-local 原子 JSON；SQLite schema version 2 使用
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

Session/Run 恢复之后，Agent reconciliation 按单向提交阶段核对 Delegation、child Session backlink、child Run
provenance 和冻结 contract。只有唯一可证明归属的记录才补齐；active child 转 `recoverable`，unknown outcome
不自动重放。terminal child 可重建缺失的 DelegationResult，pending delivery 通过稳定 delivery identity 对账，
不会重复写父消息或启动父 Run。无法证明一致的半提交关系 fail-closed，并保留全部 canonical 证据。

## 会话查询与维护

application 提供：

- catalog 搜索、状态/归档/置顶/工作区/模型/标签筛选与排序；
- show、rename、archive、pin、tag；
- messages、events、export 和 replay；
- branch 与 trash。

Host DTO 不暴露 catalog 内部的 `search_text`。

root Session 有 active 或无法判定的 Agent descendant 时不能归档；任何仍有 canonical Delegation 的 root 不能直接
进入 trash。subagent Session 由 owning Delegation 管理，也不能作为普通独立会话删除，以免形成孤儿关系。

## Branch

- 分支点必须存在于 canonical messages。
- 工具交换必须完整；请求或结果中间不能作为孤立边界。
- 分支消息获得新 id、清除 run/turn id，并记录 source identity。
- 分支 lineage 写入 root/parent/fork 字段。
- 含 `resource_refs` 的分支通过 ResourceService 验证存在性，并与消息一起原子发布引用关系。

## 回收站

删除使用 staging 后原子发布到 `trash/sessions/<trash_id>`。发布失败必须回滚到原 Session，或明确报告
数据保留位置。初始化负责完成可判定安全的中断发布。

发布 trash 后，ResourceService 对该 Session 原引用执行可达性清理；共享资源仍被其他活动 Session 引用时
不得删除。清理失败只形成可重试 diagnostic，不破坏已经完成的 trash 事务。

## 验收证据

- `tests/sessions/test_local_files.py`
- `tests/sessions/test_local_io.py`
- `tests/application/test_sessions.py`
- `tests/application/test_session_models.py`
- `tests/sessions/test_delegations.py`
- `tests/application/test_agent_recovery.py`
- `tests/cli/test_session_commands.py`
