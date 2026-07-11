# OURO Agent 第二阶段：会话、资源与 Desktop 规格

- Date: 2026-07-11
- Status: In progress / session core foundation implemented
- Parent: `2026-07-11-phase-2-personal-workflow.md`

## 1. Scope

本规格定义个人工作流的可见主体：session catalog、历史导航、branch/replay/export、资源记录、后台
运行状态和 PyWebview + FastAPI Desktop。它不改变第一阶段单个 run 的模型/工具循环。

已完成的 session core 边界见 `2026-07-11-session-core-foundation.md`。ResourceService、Supervisor、
Desktop、完整 export/maintenance 和性能 gate 仍未完成，本状态不代表本规格验收完成。

## 2. Session Catalog

### 2.1 SessionIndexEntry

派生索引条目至少包含：

- session_id、root_session_id、parent_session_id；
- title、title_source、title_locked；
- created_at、updated_at、last_activity_at；
- status、active_run_id、pending_interaction；
- current provider/endpoint/model；
- workspace、workspace_trusted；
- archived、pinned、tags；
- message_count、run_count、resource_count；
- last_message_preview；
- health：ok/recoverable/corrupt/missing_resource/migration_required。

索引是派生数据。写 session 成功但索引更新失败时，操作返回成功加 diagnostic；后台或用户命令可以
重建索引。

### 2.2 Title Lifecycle

- 创建时继续使用首条用户消息的确定性截断标题。
- 第一个 completed run 后可异步请求标题生成器；运行完成不等待标题。
- 生成结果必须是纯文本、单行、长度受限，失败则保留 fallback。
- 用户 rename 后设置 `title_locked=true`，自动生成不再覆盖。
- branch 默认使用“原标题 · 分支”，随后遵循相同生成/锁定规则。

### 2.3 Search and Filter

搜索范围：标题、用户/assistant 文本、tag、workspace basename、model id。第一阶段事件 payload 和
reasoning 默认不进入全文索引，避免扩大敏感内容和索引体积。

filter 至少支持：

- active/waiting/failed/recoverable/completed；
- archived/pinned；
- workspace、model、tag；
- created/updated 时间范围；
- has resources / missing resources。

排序至少支持 updated desc、created desc、title 和 relevance。索引损坏时退化为 catalog 扫描，
同时提示重建，不阻止 session 打开。

## 3. Branch and Replay

### 3.1 Branch

`branch_session(source_session_id, message_id)` 的原子结果是一个新 session：

1. 验证 message 属于 source session，且不在不完整 tool exchange 中间。
2. 取得 source 的只读 snapshot；不要求 source 无 active run，但 branch point 必须已持久化。
3. 复制从开头到 branch point 的 canonical messages；重新分配 message id，并保存
   `source_message_id` lineage。
4. 不复制 run 为可执行 run；创建 lineage snapshot，历史消息的 source run id 仅存 metadata。
5. 引用到 copied messages 的 resource records 复制记录，owned blob 使用内容寻址复用，不复制字节。
6. 写新 session、messages、resources 后再更新 catalog；失败时删除未发布的 staging 目录。

branch point 若位于 assistant tool-call，则必须包含该 assistant 的全部连续 tool results；若结果尚未
完成则拒绝 branch，并提供最近合法 message id。

### 3.2 Replay

Replay 是事件的只读投影：

- 以 sequence 排序，支持按 run filter；
- 默认忽略原始时间间隔，用户可选择 1x/2x/instant；
- 缺失或未知事件以 generic diagnostic card 显示，不导致 replay 中止；
- replay 不调用 provider、不执行工具、不重新触发 hooks；
- “从此继续”创建 branch，不修改原 run/status/event log。

## 4. Export and Data Maintenance

### 4.1 Export Bundle

默认导出为目录或 zip，包含：

```text
manifest.json
session.json
messages.jsonl
runs/*.json
events.jsonl              # 可由用户排除
resources/index.json
resources/files/*         # 仅 owned/copy 且用户选择包含
```

manifest 记录 export schema、OURO version、session lineage、文件列表、size 和 sha256。导出不得包含
全局配置、workspace secret、resolved credential 或 MCP 环境变量。

导出到已有路径默认拒绝；显式覆盖也只能替换目标 bundle，不能跟随符号链接越界。生成中写 staging，
完成后原子发布。

### 4.2 Archive, Delete, Cleanup

- archive 只更新 session 状态，不删除数据。
- delete 默认进入本地 trash 目录并保留可恢复期限；`purge` 才永久删除。
- active/waiting session 不能直接 delete，必须先 abort 或显式强制并记录诊断。
- event cleanup 只能由用户触发，支持按 session、run 和日期预览；删除后写 maintenance record。
- orphan resource cleanup 先 dry-run，按 reference graph 判定，不能仅凭文件名。
- 所有 maintenance 命令幂等，并在中断后能重新执行。

## 5. Resource Model

### 5.1 ResourceRecord

```text
schema_version
resource_id
session_id
run_id?
message_id?
tool_call_id?
kind: attachment | generated_file | external_reference | image | other
ownership: copied | generated | referenced
uri
relative_path?
display_name
media_type?
size_bytes?
sha256?
created_at
updated_at
state: available | missing | changed | removed
source
metadata
```

不允许把任意绝对路径当作 OURO owned file。`copied/generated` 的 `relative_path` 必须位于 session
resource store 或全局 content-addressed blob store；`referenced` 保留 canonical URI，并在访问时检查。

### 5.2 Add Resource

- attach 默认 copy 到 OURO 管理目录，保证原文件移动后仍可用；用户可显式选择 reference。
- copy 以流式读取计算 size/hash，临时文件完成后原子发布。
- 相同 sha256 的 owned file 可以内容寻址去重，但每个 session 保留独立 ResourceRecord。
- 文件名只用于显示，不用于身份或路径拼接。
- 文件类型由内容/显式 media type 和后缀共同判断，不信任单一后缀。

### 5.3 Tool and Message Integration

- `MessageRecord.resource_refs` 保存 resource_id，不再保存裸路径；迁移时把旧裸路径转换为
  `external_reference` 或保留兼容 diagnostic。
- ToolResult 返回路径时，ResourceService 在工具完成边界登记；工具 handler 不直接写 index。
- provider 不支持 vision 时，图片资源仍保存在 session，但进入上下文前给出明确能力错误或只注入文字
  引用，不静默发送非法 payload。
- 删除 resource record 前检查 message/tool references；默认标记 removed，不篡改历史消息。

### 5.4 Missing and Changed References

`referenced` 文件在访问时比较存在性、size、mtime 和可选 hash：

- missing：保留 record，UI 提供 locate/remove；
- changed：提示用户使用新内容、重新 copy 或保持旧记录；
- 无权限：状态不改为 missing，记录 access diagnostic；
- 检查失败不阻塞 catalog 首屏，按需或后台执行。

## 6. Multi-session Background UX

每个 session 在 Host 中显示以下互斥主状态：idle、running、waiting_approval、waiting_user、aborting、
failed、recoverable。另显示非互斥 badge：queued follow-up 数、missing resource、MCP degraded、migration。

行为规则：

- 切换 session 不取消后台 run。
- 当前不可见 session 需要审批/AskUser 时产生 Host notification，但不自动弹出抢焦点对话框。
- 用户打开 session 后，从持久化 snapshot 和 subscription cursor 恢复，无需依赖内存 UI state。
- 同一 pending interaction 只提交一次；重复 approve/reject/answer 返回幂等结果或明确 already resolved。
- 关闭窗口时展示 active run 数；用户可 keep running（仅窗口隐藏）或 graceful abort 后退出。

## 7. Textual + Rich Interactive

第二阶段 Interactive 由临时逐行 prompt 升级为 Textual TUI，但继续通过同一 ApplicationFacade 和
EventSubscription：

- session switcher 显示后台 running/waiting/failed badge；
- 主区域渲染 message、reasoning、tool、approval/AskUser 和 diagnostics；
- composer 支持多行输入、follow-up、history、abort 和 slash command palette；
- model/endpoint、Skill、MCP 和 workspace trust 提供基础选择/状态入口；
- terminal resize、无色模式、窄屏和不支持完整 TUI 的终端有降级路径；
- Print 模式 stdout 仍只有最终文本，JSON schema/ANSI 行为不因 Rich/Textual 改变；
- 非交互输入、CI 和 screen reader 场景可以使用现有 line/command host adapter。

TUI widget 不持有 RuntimeService/SessionStore；关闭/切换页面不取消后台 run，pending interaction 以
稳定 id 解析。

## 8. PyWebview + FastAPI Desktop

### 8.1 Navigation

基础布局：

- 左侧：search/filter、session list、new session；
- 中央：message timeline、streaming output、tool cards、composer；
- 右侧可折叠 inspector：run/event/resource/context diagnostics；
- 全局 settings：models、Skills、plugins、MCP、workspace trust、data maintenance。

### 8.2 Required Interactions

- 新建、切换、rename、archive、branch、export session；
- 发送 prompt、follow-up、abort；
- approval approve/reject/modify 和 AskUser answer；
- 添加/查看/定位/移除 resource；
- 查看后台任务和 extension health；
- 选择 provider/endpoint/thinking level；
- 键盘可达和可复制的错误/诊断文本。

### 8.3 Private Host Adapter and Async Lifecycle

- FastAPI route 只映射 ApplicationFacade command/query DTO，不承载第二套业务规则。
- server 绑定随机 loopback port；每次启动生成不可持久化的 host token，并校验 Origin/Host。不得绑定
  `0.0.0.0`，不得把该接口宣传为稳定 RPC。
- PyWebview 只加载本次启动的 loopback URL；外部导航在系统浏览器打开，不能在拥有 host token 的
  webview 中加载不受信内容。
- 事件使用 WebSocket 或 SSE 映射 bounded EventSubscription；断线后以 cursor 走
  snapshot-then-stream，不无限堆积 event。
- Desktop composition root 统一启动/停止 server、webview、RuntimeSupervisor 和 owned child
  processes；窗口销毁后不得继续向连接发送事件。
- HTTP/WebSocket DTO 不得包含 SessionStore、Task、handler、client、文件句柄或 resolved secret。

### 8.4 Rendering Rules

- 流式 delta 只更新当前临时 message；canonical message_added 后以持久化 message 替换。
- tool call、approval、result 以 tool_call_id 关联，不依赖相邻控件位置。
- reasoning 默认折叠并遵循 provider 能力；不会混入最终 assistant 文本复制结果。
- 未知 event/type 使用 generic renderer；新事件不会令旧 Desktop 崩溃。
- error card 展示 component、可恢复性和建议操作，不展示 secret 或完整环境。

## 9. Failure and Recovery

- catalog/search index 失败：打开 canonical session，提示重建。
- title provider 失败：保留 fallback title。
- resource copy 中断：清理 staging，不发布 record。
- referenced file 丢失：保留历史，标记 missing。
- Desktop renderer 失败：沿用 `event_sink_failed` 隔离，允许刷新 snapshot。
- process 崩溃：第一阶段 run recovery 生效；Supervisor 重启后不假装恢复旧 Task。
- branch/export 中断：未发布 staging 可安全清理，原 session 不变。

## 10. Acceptance

- 5,000-session catalog、search/filter 和 archive 路径可用，索引可重建。
- 用户 rename 后标题生成不会覆盖；标题生成失败不影响 run completion。
- 任意合法 message point 可 branch，原 session 字节/记录不变，tool exchange 不被拆开。
- replay 不执行 provider/tool；从 replay 继续会创建 branch。
- attachment、generated file 和 external reference 能 reload、branch、export 和检测 missing。
- export 不含 resolved secret，覆盖/路径穿越/符号链接边界有测试。
- 两个 session 可并发运行并分别审批；切换页面不丢事件。
- Textual Interactive 支持多会话、流、HITL 和后台 badge；Print/JSON 输出契约不回归。
- Desktop 只通过 facade 操作，支持历史、流、HITL、resources、settings 和 diagnostics。
- loopback host auth、WebSocket/SSE shutdown、subscriber overflow、renderer failure 和进程恢复均有
  自动化测试。

## 11. Deferred

- 跨设备同步和多人 session。
- 可编辑历史、合并 branch 或 Git 式三方 merge。
- event 视频化回放和高级 tracing 分析。
- OCR、音视频转写和资源内容理解 pipeline。
- Desktop 安装包、签名、自动更新和系统托盘常驻策略。
