# OURO Agent Phase 2 Implementation Plan

> 本文档只定义后续执行顺序。创建本文档的变更不得包含任何第二阶段实现代码。

- Date: 2026-07-11
- Status: Planned / not started
- Goal: Complete the local personal workflow without forking the Phase 1 runtime
- Master spec: `../specs/2026-07-11-phase-2-personal-workflow.md`
- Session/resource/Desktop spec: `../specs/2026-07-11-phase-2-sessions-resources-desktop.md`
- Extension spec: `../specs/2026-07-11-phase-2-extensions-official-capabilities.md`
- Phase 1 audit: `../audits/2026-07-11-phase-1-completion-audit.md`

## 1. Execution Rules

- 每个 work package 单独提交，先测试/契约再实现；不得把多个高风险迁移塞进一个提交。
- 开始 package 前重跑 Phase 1 全量基线；结束时运行 package 测试、全量 pytest、Ruff、mypy、
  format check 和 compileall。
- canonical data 变化必须先提供只读 preflight、fixture 和失败恢复测试，再启用写迁移。
- CLI 与 Desktop 只能调用 application facade；禁止为了赶 UI 进度直接读取 storage/config internals。
- 新扩展工具必须先进入 ToolRegistry/ToolExecutor，不允许建立旁路权限或事件系统。
- 可选能力失败必须证明 core 降级路径；“正常路径通过”不足以完成 package。
- 每个 package 完成后同步规格中的 Resolved/Open Questions 和本计划 checkbox。

## 2. Dependency Graph

```text
WP0 Contract/ADR
  ├─ WP1 Schema + migration + facade DTOs
  │    ├─ WP2 RuntimeSupervisor + subscriptions
  │    ├─ WP3 Session catalog/search/branch/replay
  │    ├─ WP4 Resource service/export/maintenance
  │    └─ WP5 Workspace trust/config/model/Skills services
  ├─ WP6 Plugin lifecycle/hooks/tool batches
  │    ├─ WP7 Real MCP lifecycle
  │    └─ WP8 Official plugins
  └──────────────────────────────────────┐
WP2 + WP3 + WP4 + WP5 + WP6 ────────────┴─ WP9 Host UX (Textual + Desktop)
WP1..WP9 ────────────────────────────────── WP10 Hardening/release gate
```

WP3、WP4、WP5 在 WP1 后可并行；WP7 和 WP8 在 WP6 后可并行。WP9 可以先做只读 shell，但提交
approval/resource/settings 前必须等相应 service 完成。

## 3. WP0 — Decisions and Baseline Freeze

**Objective:** 消除会改变依赖或数据布局的未决选择，不改产品行为。

- [ ] 记录 ADR：项目概述的 PyWebview + FastAPI 基线、私有 loopback host adapter 和生命周期。
- [ ] 记录 ADR：搜索索引实现与“canonical 可删除重建”约束。
- [ ] 选择 MCP SDK/协议版本，确认 stdio、streamable HTTP、SSE compatibility 的官方接口。
- [ ] 决定最小 browser plugin 是独立 process 还是 MCP adapter。
- [ ] 保存 Phase 1 数据 fixture：空会话、正常会话、tool/HITL、recoverable、损坏 index、旧 resource_refs。
- [ ] 建立性能 fixture：5,000 sessions、100,000 messages、1,000 resources。
- [ ] 把当前 217+ tests、Ruff、mypy、compileall 记录为 Phase 2 起始 gate。

**Exit gate:** 四个 ADR 有结论；fixture 可在临时 OURO_HOME 重复生成；没有 product code change。

## 4. WP1 — Data Versioning, Migration, and Application Facade

**Objective:** 先建立可演化的数据和宿主边界。

### Contracts first

- [ ] 定义 `data_layout_version`、migration journal 和 backup manifest。
- [ ] 定义显式 Session lineage/title fields、Run origin/parent fields 和兼容默认值。
- [ ] 定义 application command/query DTO、Diagnostic DTO 和 typed errors。
- [ ] 定义 facade ports，不暴露 store、registry、Task、handler、client 或 secret。
- [ ] 为 Phase 1 fixture 写只读 compatibility tests。
- [ ] 为中断、重复执行、版本过高、备份失败和索引重建失败写 migration tests。

### Implementation

- [ ] 实现 migration preflight，只报告计划不写文件。
- [ ] 实现 staging/backup/journal 和幂等逐 session migration。
- [ ] 增加 layout version guard；旧进程打开新 layout 时失败可读。
- [ ] 实现 facade 最小 vertical slice：list/open/create session，start/abort run，pending interaction query。
- [ ] 把现有 session CLI 的只读路径迁到 facade；输出保持兼容。

**Exit gate:** 所有 Phase 1 fixture 可打开；迁移任意步骤注入失败后原数据可恢复；CLI smoke tests 通过。

## 5. WP2 — RuntimeSupervisor and Event Subscriptions

**Objective:** 支持不同 session 的后台并发和可靠 host observation。

### Tests first

- [ ] 两 session 并发、同 session 竞争、follow-up、approval/AskUser 和 abort matrix。
- [ ] snapshot-then-stream 无缝 cursor 测试，覆盖订阅窗口内新事件。
- [ ] 慢 subscriber、buffer overflow、subscriber exception、unsubscribe race 测试。
- [ ] shutdown 测试：idle、provider stream、tool process、waiting interaction、多 task 混合。
- [ ] 未观察 task exception 检测测试。

### Implementation

- [ ] 实现 `RuntimeSupervisor` task registry 和 per-session command serialization。
- [ ] 复用 SessionStore/run lock，不创建第二份 active-run truth。
- [ ] 实现 bounded `EventSubscription`、filter、cursor、overflow diagnostic 和 refresh signal。
- [ ] 实现 supervisor status DTO 和 Host notification stream。
- [ ] 实现 graceful shutdown；超时后走 Phase 1 checkpoint/recovery。
- [ ] 把 Interactive event sink 接到 subscription adapter，证明旧 host 行为不回归。

**Exit gate:** 两 session 可真实并发；慢/失败 host 不阻塞 run；关闭后无残留 Task/进程警告。

## 6. WP3 — Session Catalog, Title, Search, Branch, and Replay

**Objective:** 提供完整历史导航，所有派生能力可重建。

### Catalog/title/search

- [ ] 定义 SessionIndexEntry schema、health 和 index generation。
- [ ] 写 5,000-session catalog、损坏/缺失 index 和并发更新测试。
- [ ] 实现 index transaction/rebuild 和 scan fallback。
- [ ] 实现 title fallback/generated/user/locked 状态机。
- [ ] 标题生成器做可替换 application port；失败/超时不影响 run。
- [ ] 定义并实现全文派生索引、filter/sort 和 rebuild command。
- [ ] 明确 reasoning/event 默认不索引，并写隐私范围测试。

### Branch/replay

- [ ] 定义合法 branch point 和 tool exchange 原子组 fixture。
- [ ] 测试 active source、waiting run、损坏 resource、staging failure 和重复请求。
- [ ] 实现 snapshot branch、message id remap、lineage 和 staging publish。
- [ ] 实现 replay projector、unknown event renderer DTO、speed/cursor。
- [ ] 实现 replay-to-branch；验证不调用 provider/tool/hook。
- [ ] CLI 增加 search/filter/rename/archive/branch/replay，并通过 facade。

**Exit gate:** 原 session 在 branch/replay 后不变；索引全删后可重建；title/search failure 不破坏 session。

## 7. WP4 — Resource Service, Export, and Maintenance

**Objective:** 让附件和生成文件具有稳定身份、归属和安全生命周期。

### Resource contracts

- [ ] 定义 ResourceRecord、ownership/state enum 和 index schema。
- [ ] 定义 content-addressed blob/staging layout 和 reference graph。
- [ ] 为路径穿越、符号链接、同名文件、大文件流式 copy、hash collision 注入写测试。
- [ ] 为旧裸路径 resource_refs 设计迁移和 missing diagnostic fixture。

### Resource implementation

- [ ] 实现 attach copy/reference、hash、dedup、atomic publish。
- [ ] 实现 ToolResult 路径到 ResourceRecord 的 application boundary；handler 不写 index。
- [ ] MessageRecord 新写入只保存 resource_id；读取旧 path 保持兼容。
- [ ] 实现 missing/changed/access-error 检测和 locate/remove。
- [ ] branch 复制 records 并复用 owned blobs。

### Export/maintenance

- [ ] 写 deterministic bundle manifest、secret exclusion、overwrite/symlink tests。
- [ ] 实现 directory/zip staging export，events/files 可选择。
- [ ] 实现 archive、trash/restore、purge、event cleanup preview 和 orphan dry-run。
- [ ] 所有 maintenance 写 journal，并验证中断后幂等恢复。
- [ ] CLI 增加 resources/export/archive/trash/cleanup commands，全部走 facade。

**Exit gate:** resource reload/branch/export 后 identity 稳定；大文件不整体入内存；维护命令默认不永久删除。

## 8. WP5 — Workspace Trust, Project Config, Model, and Skills Services

**Objective:** 在 Desktop 前完成可解释、可审计的设置后端。

### Trust/config

- [ ] 定义 workspace canonical identity、trust record、revoke 和 path-change policy。
- [ ] 定义 `.ouro/config.yaml|json` schema 和配置来源 DTO。
- [ ] 写 untrusted preview、双配置冲突、inline secret、symlink identity 和 precedence tests。
- [ ] 实现 `WorkspaceTrustService` 和 global trust store。
- [ ] 实现 project config read/validate/merge；untrusted layer 不生效。
- [ ] permission override 放宽时要求明确确认；收紧可直接生效。

### Model/Skills

- [ ] 把 provider/endpoint CRUD/check 包装为 ModelConfigService DTO。
- [ ] 写 secret redaction、default fallback、session pinning 和 check-no-session tests。
- [ ] 实现 endpoint health 和 effective source 查询。
- [ ] 增加 Skill conflict/shadow/trust/allowed_tools tests。
- [ ] 实现 Skill catalog UX DTO、workspace trust gating 和 allowed_tools enforcement。
- [ ] CLI 迁移到 services，保持已有命令兼容。

**Exit gate:** untrusted workspace 无可执行贡献；配置每个值可解释来源；任何输出不含 resolved secret。

## 9. WP6 — Plugin Lifecycle, Typed Hooks, and Tool Batches

**Objective:** 完成 extension transaction，并补齐 provider tool-call batch 保真。

### Plugin lifecycle

- [ ] 定义 entrypoint contract、generation、staging registry 和 authorization record。
- [ ] 写 partial registration、duplicate id、invalid schema、reload failure 和 disable-during-run tests。
- [ ] 实现 discover/validate/authorize/load/register transaction。
- [ ] 实现 generation swap、disable/unregister 和 active call cancellation policy。
- [ ] AI-generated plugin 强制确认和 source/permission UI DTO。

### Typed hooks

- [ ] 为每个 HookName 定义 typed input/result；没有 consumer 的名称不得暴露为 active。
- [ ] 为 before/after run/model/context/tool、approval、event、error 写 ordering/isolation tests。
- [ ] 实现受限 ContextTransform validator；上下文压缩由独立 typed strategy contract 负责。
- [ ] 验证 hook 永远不能放宽 tool permission 或修改 mutable state。

### Tool batches

- [ ] 定义 ToolCallBatch 和 `execution_mode`，默认 serial。
- [ ] 写多 tool-call provider fixtures：串行、全 parallel_safe、审批混合、失败、abort、完成乱序。
- [ ] 同一 assistant message 持久化完整 calls；结果按 call 原顺序落消息。
- [ ] 并行执行使用结构化 task group，取消和异常全部收集。
- [ ] 保持 Phase 1 单调用历史兼容，并让 ContextBuilder 使用持久化摘要投影视图。

**Exit gate:** plugin registry 无半状态；所有 active hooks 有 consumer；批量工具历史兼容严格 provider。

## 10. WP7 — Real MCP Lifecycle

**Objective:** 把第一阶段内存状态框架替换为真实、可诊断 MCP client manager。

- [ ] 固定 MCP SDK 版本和 transport adapter interface。
- [ ] 建立 deterministic stdio fake server、HTTP test server 和 malformed server fixtures。
- [ ] 写 handshake、capability、discovery、call、cancel、timeout、auth、disconnect、reconnect tests。
- [ ] 写 start/stop/restart/auto-start race、backoff cancel 和 host shutdown tests。
- [ ] 实现 config migration：http→streamable_http，sse→sse_compat。
- [ ] 实现 stdio process group、vector args、bounded stderr diagnostics 和 secret redaction。
- [ ] 实现 HTTP/SSE client、credential resolution 和 timeout policy。
- [ ] 实现 `mcp.<server>.<tool>` generation registry 和 unavailable behavior。
- [ ] MCP result 映射 text/structured/resource refs，未知 content type 可诊断。
- [ ] CLI/facade 增加 list/show/start/stop/restart/check/logs/tool catalog。

**Exit gate:** 真实 fake server 端到端通过；无 shell 注入、secret 泄漏或 shutdown 遗留进程。

## 11. WP8 — Official Plugins

每个插件独立 package/commit/feature flag，不允许互相成为隐式依赖。

### WP8A Built-in Summary Compaction

- [x] 定义并实现 summary record、coverage、active pointer、lineage 和 token budget。
- [ ] 测试 tool exchange、branch、timeout、invalid transform、plugin disable。
- [x] 实现内置增量 summary、持久化 context projection 和 typed strategy replacement。
- [x] 实现自动与手动 `/compact` 共用管线和前端事件。
- [x] 证明失败时不会截断 canonical history 或静默发送不完整上下文。

### WP8B Planning and Progress

- [ ] 定义 PlanRecord/PlanItem 和单 in_progress invariant。
- [ ] 实现 tools、commands、events、context contribution 和 UI view model。
- [ ] 测试 disable/re-enable、损坏 plugin store 和并发 update。

### WP8C Minimal Local Memory

- [ ] 定义 MemoryRecord、opt-in、deterministic search 和 token/result limits。
- [ ] 实现 remember/list/search/forget/disable/export。
- [ ] 测试 source visibility、sensitive confirmation、disable vs purge、store failure isolation。

### WP8D Minimal Browser

- [ ] 根据 WP0 ADR 实现 process/MCP adapter 和 browser Tool schemas。
- [ ] 实现 navigate/read/links/screenshot/search 和 ResourceService 下载接入。
- [ ] 对 submit/upload/download/auth mutation 默认 approval。
- [ ] 测试 timeout、size limit、process crash/restart、profile/cookie lifecycle 和 abort。

**Exit gate:** 四个插件可分别安装/启用/禁用；任一失败不影响 core run 或其他插件。

## 12. WP9 — Textual Interactive and PyWebview Desktop Vertical Slices

**Objective:** 以可测试 vertical slice 交付日常 Interactive 和基础 Desktop，不复制 service 逻辑。

### Textual + Rich Interactive

- [ ] 建立 Textual app/composition adapter 和 terminal capability fallback。
- [ ] 实现 session switcher、后台 badge、message/tool/reasoning timeline 和多行 composer。
- [ ] 实现 approval/AskUser、follow-up、abort、command palette 和 model/extension 状态入口。
- [ ] 保留 line host fallback；验证 Print stdout、JSON payload 和无色输出不回归。
- [ ] 测试 resize、窄屏、快速 session 切换、慢 subscriber、EOF/interrupt 和 pending interaction 去重。

### Desktop shell and bridge

- [ ] 创建 Desktop package、composition root、静态 Web UI 和无窗口 test harness。
- [ ] 实现 facade FastAPI adapter、随机 loopback port、每次启动 token、Origin/Host 校验和 shutdown
  coordinator；明确该协议不稳定且不属于 Phase 3 RPC。
- [ ] 实现 WebSocket/SSE subscription、cursor reconnect、bounded queue 和 snapshot refresh。
- [ ] 测试外部绑定被拒绝、token/Origin/Host、窗口销毁后的 event、slow rendering、overflow refresh 和
  task exception。

### Desktop vertical slices

- [ ] Slice 1：session catalog/search/filter/new/open，message history read-only。
- [ ] Slice 2：composer、streaming text/reasoning/tool cards、background session badge。
- [ ] Slice 3：approval approve/reject/modify、AskUser answer、abort/follow-up。
- [ ] Slice 4：resources attach/view/locate/remove、branch/replay/export/archive。
- [ ] Slice 5：model/Skills/plugin/MCP/workspace trust settings 和 diagnostics。
- [ ] Slice 6：official plugin panels、data migration/maintenance UI。

### Quality

- [ ] 所有错误可复制，pending interaction 键盘可达，焦点不会被后台 session 抢走。
- [ ] unknown event/resource/plugin panel 使用 generic fallback。
- [ ] UI snapshot tests 不依赖实时网络或本机 MCP。
- [ ] CLI 与 Desktop 对同一 facade fixture 产生等价状态转换。

**Exit gate:** Textual 与 Desktop 都能完成 prompt→tool approval→continue，并同时观察第二个 session；
Desktop 另能完成 resource/settings 工作流，Print/JSON 契约不变。

## 13. WP10 — Hardening and Phase 2 Release Gate

### Compatibility and recovery

- [ ] 从每个 Phase 1 fixture 完成 migration/open/run/export。
- [ ] 对 migration、branch、resource copy、export、plugin swap 注入每个写步骤失败。
- [ ] JSONL corruption 给出位置和维护建议；不误改原文件。
- [ ] active lock、stale lock file、多进程 start race 回归。

### Security

- [ ] secret scan 覆盖 events/logs/exports/diagnostics/Desktop models。
- [ ] path traversal、symlink/reparse point、zip slip、command args/env tests。
- [ ] untrusted workspace/plugin/MCP/browser 权限 matrix。
- [ ] 确认文档和 UI 不把 failure isolation 描述为 sandbox。

### Performance and lifecycle

- [ ] 运行 catalog/search/resource 流式预算基准并记录硬件环境。
- [ ] 24h 多 session/MCP/browser soak，无 task/file/process/subscriber 泄漏。
- [ ] Desktop repeated open/close 和 settings reload soak。
- [ ] event buffer、diagnostic ring、plugin caches 均有上限。

### Final verification

- [ ] `pytest` 全量通过。
- [ ] `ruff check .` 通过。
- [ ] `ruff format --check .` 通过。
- [ ] `mypy ouro` 通过。
- [ ] `compileall ouro tests` 通过。
- [ ] Phase 2 specs 的所有 Acceptance 有测试或明确人工 smoke 证据。
- [ ] README、CLI help、data migration guide、plugin/MCP troubleshooting 更新。
- [ ] 路线图 Phase 2 标记 complete；Phase 3 backlog 不被提前实现。

## 14. Commit and Review Boundaries

推荐最小提交边界：

1. ADR/fixtures only；
2. schemas + read compatibility；
3. migration write path；
4. facade DTO/queries；
5. supervisor；
6. subscription；
7. catalog/search；
8. branch/replay；
9. resources；
10. export/maintenance；
11. trust/config；
12. model/Skills services；
13. plugin transaction；
14. typed hooks；
15. tool batches；
16. MCP transports；
17–20. 每个 official plugin；
21–26. 每个 Desktop vertical slice；
27. hardening/docs。

Review 时不得用“后续再补”跳过当前 package 的 failure tests、兼容测试或降级路径。

## 15. Phase 2 Definition of Done

第二阶段只有在以下条件同时满足时完成：

- 日常个人工作流可以从 CLI 和 Desktop 完成，且两者共享 facade/runtime；
- 多 session 并发、订阅、HITL 和 shutdown 在失败路径下可靠；
- session/search/branch/replay/resource/export/maintenance 形成闭环；
- workspace trust、模型、Skills、plugin 和 MCP 可管理且不泄露 secret；
- built-in summary strategy 可替换且失败可见；planning/memory/browser 可独立降级；
- Phase 1 数据安全迁移，所有 canonical 数据仍本地可读；
- 自动化和人工验收证据齐全，没有把 SDK/RPC、子智能体、高级自动化或 SQLite 偷渡进范围。
