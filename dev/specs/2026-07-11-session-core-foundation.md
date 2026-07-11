# OURO Session Core Foundation

- Date: 2026-07-11
- Status: Accepted / implemented
- Parent: `2026-07-11-phase-2-sessions-resources-desktop.md`

## 1. Decision

OURO 的会话核心采用三层边界：

1. `SessionStore` 保存 canonical session、message、event、run 和 compaction；
2. session index 是可删除重建的派生投影，不参与 canonical 写入的成功判定；
3. Host 只通过 `ApplicationFacade` 和不可变 application DTO 发出命令或查询状态。

`ouro.sessions` 不拥有业务用例。创建、查询、重命名、归档、pin、branch、replay 和 trash 由
`ouro.application.sessions.SessionService` 编排。`local_files`、`local_index`、`local_io` 分别负责
canonical 聚合与恢复、派生目录投影、原子文件原语，避免三条变化轴重新汇入单一存储类。

## 2. Canonical identity and lifecycle

`SessionRecord` 现在显式保存：

- `root_session_id`、`parent_session_id`；
- `forked_from_message_id`、`forked_from_run_id`；
- `title_source`、`title_locked`；
- `last_activity_at`、`active_run_id`；
- `pinned`、`workspace_trusted`。

`RunRecord` 显式保存 `origin`、`parent_run_id` 和 `initiator_id`。运行开始、等待、完成、失败、中止和
崩溃恢复会同步维护 `active_run_id` 与 `last_activity_at`，避免 Host 从事件邻接关系猜测当前状态。

## 3. Storage publication and versioning

新 session 和 branch 使用同一 staging 目录事务：先写完整 `session.json`、JSONL、runs/resources
目录和资源空索引，再以目录 rename 原子发布。准备失败会清理 staging，已存在 session 不会被覆盖。

Trash 也使用隐藏 staging：先准备 manifest，再把完整 session 移入 staging，最后以目录 rename
发布为扁平归档。发布前失败会回滚 canonical 目录；进程在中间退出时，下次初始化会完成已持久化
的 trash 发布，而不会把半成品暴露为正式归档。

`layout.json` 保存独立的 `data_layout_version`。缺失 manifest 代表当前一阶段布局，可初始化为当前
版本；损坏或版本不匹配会停止并返回明确错误，不进行隐式迁移或原地猜测。

Session run lock 位于 `locks/sessions/`，不再放入 session 目录。这使 session 目录可以在持锁期间安全
原子发布或移动到 trash，同时仍由 OS descriptor 判断锁是否存活。

## 4. Derived catalog

`sessions/index.json` 使用类型化 `SessionIndex` 和 `SessionIndexEntry`，覆盖 lineage、title、活动状态、
模型路由、workspace、archive/pin/tags、计数、preview 和 health。

- 普通状态变化只增量替换一个 index entry；
- index 使用独立 OS lock，不能获得锁时标记 dirty；
- dirty、损坏或版本不符时才全量扫描重建；
- canonical 写成功但 index 更新失败时，命令仍成功并返回 recoverable diagnostic；
- catalog 写失败时可以直接使用只读 scan projection，不阻止打开 canonical session；
- 全文派生字段只包含 user/assistant 文本，不包含 reasoning 或 event payload，并且不会暴露给 Host DTO。

## 5. Commands and queries

首批 facade 能力：

- create/open/list/search/filter/sort；
- rename、archive/unarchive、pin/unpin；
- branch、只读 replay；
- delete-to-trash；
- start/abort run 和 manual compaction 转发到既有 `RuntimeService`。

标题状态机支持 fallback/generated/user/locked。标题生成是可替换异步 application port；失败只返回
安全 diagnostic，不覆盖 fallback，也不影响 run。自动调度归未来 `RuntimeSupervisor`，当前 runtime
不会创建无法管理的后台 task。

## 6. Branch and replay invariants

Branch 创建新 session，重新分配 message id，并在 message metadata 保存 source message/run/turn
lineage。历史 run 不复制为可执行 run，原 session 不修改。完整 tool exchange 是原子组：选择 assistant
tool-call 会扩展到全部连续结果；不完整 exchange 被拒绝并报告最近合法边界。

在 `ResourceService` 完成前，包含 `resource_refs` 的 branch 明确拒绝发布，避免产生悬空资源身份。该
限制必须由后续资源事务替换，不能通过复制裸路径绕过。

Replay 只按 sequence 投影持久化事件，不调用 provider、tool 或 hook，也不修改原日志。

## 7. Deferred boundaries

本切片不声称完成：

- ResourceRecord copy/dedup/branch transaction；
- trash restore、purge、bundle export 和 maintenance journal；
- 5,000-session 性能 gate 与独立全文索引后端；
- RuntimeSupervisor、subscription、自动标题调度和 Desktop；
- canonical data migration preflight/backup/journal。

这些能力必须继续复用当前 facade、typed DTO、canonical store 和 diagnostic 边界，不得重新建立 Host
直读文件或第二份 active-run truth。

## 8. Verification

实现包含正常路径、索引失败/自愈、损坏 session、原子 staging、版本过高、tool exchange、resource
拒绝、replay 只读和 trash busy 状态测试。权威完成 gate 仍是全量 pytest、Ruff、mypy、format 和
compileall；本文件不把 Phase 2 标记为完成。
