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
      resources/index.json
  resources/
    records/<resource_id>.json
    blobs/sha256/<digest>
    index.json
    staging/
  trash/sessions/<trash_id>/
```

当前 `data_layout_version` 为 3。不兼容布局必须在初始化时抛出 `MigrationRequiredError`，不能猜测迁移。

## Canonical 数据与投影

Canonical 数据包括：

- `session.json`；
- `messages.jsonl`；
- `events.jsonl`；
- `runs/*.json`；
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

端口覆盖：初始化、创建 snapshot、Session/Run 保存、消息/事件/压缩追加、压缩激活、加载、索引、
回收站发布以及 session lock。新的后端必须保持这些行为语义，而不只是复制方法签名。

## 锁与并发

- session run lock 由 OS descriptor 持有。
- 锁文件只保存诊断信息，不作为存活事实。
- 同一 session 的运行、模型切换、分支敏感操作必须尊重 active Run 和 lock。
- catalog 使用独立锁，不能阻塞 canonical Session 打开。

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

## 会话查询与维护

application 提供：

- catalog 搜索、状态/归档/置顶/工作区/模型/标签筛选与排序；
- show、rename、archive、pin、tag；
- messages、events、export 和 replay；
- branch 与 trash。

Host DTO 不暴露 catalog 内部的 `search_text`。

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
- `tests/cli/test_session_commands.py`
