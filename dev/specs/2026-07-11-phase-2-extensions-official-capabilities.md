# OURO Agent 第二阶段：扩展与官方能力规格

- Date: 2026-07-11
- Status: Proposed / implementation not started
- Parent: `2026-07-11-phase-2-personal-workflow.md`

## 1. Scope

本规格把第一阶段的插件、MCP、Skills 和 model framework 变成可管理能力，并定义四个可独立禁用的
官方插件。所有可执行能力仍通过 OURO ToolExecutor、permission 和 event 边界。

## 2. ExtensionService

ExtensionService 汇总 plugin、MCP server、Skill 和 official capability 的 catalog/health，但不把四类
概念合并成一个模型。公共状态至少包含：

- id、kind、source、display name、version；
- discovered/enabled/loaded/connected/degraded/failed；
- requested permissions 和 effective permissions；
- config validity、credential readiness、last check、diagnostics；
- contributions：tools/hooks/commands/context/MCP/Skills/UI panels。

enable/disable/reload 必须幂等。禁用贡献后，新 run 不再看到对应 tools/context；已在执行的工具收到取消
信号或按声明完成，不能在 registry 中留下半注册状态。

## 3. Plugin Loading and Authorization

### 3.1 Lifecycle

第二阶段 lifecycle：discover → validate → authorize → load entrypoint → register transaction → enabled。

- manifest 仍是静态、无副作用的发现入口。
- entrypoint 只在 manifest 有效、用户授权且 workspace policy 允许后加载。
- contributions 先进入 staging registry；全部验证成功后原子发布。
- duplicate tool/command/provider id、非法 JSON Schema、未知 hook 或权限超声明会使该 plugin 注册失败，
  不污染现有 registries。
- reload 先构造新 generation，再切换；失败保留旧 generation 并记录 diagnostic。

### 3.2 Source and Authorization

- official、third_party、ai_generated 使用相同执行权限模型，但 UI 风险提示不同。
- AI generated plugin 首次启用总要显式确认，并显示代码路径、manifest 和权限。
- `read_write`、`network` 是 plugin capability 声明，不替代每个工具的 auto/approval。
- plugin 无权读取 credential value；只能声明 credential ref，由 host 在受控 client/process 边界解析。
- 第二阶段不提供沙箱；禁用、失败隔离和用户确认不能描述成安全隔离。

### 3.3 Hook Completion

第一阶段已执行的 hooks 保持兼容。第二阶段按真实 consumer 激活：

- before_run / after_run；
- before_context_build / after_context_build；
- before_model_call / after_model_response；
- before_tool_call / after_tool_call；
- on_approval_requested / on_event / on_error。

Hook 返回值使用按 hook 区分的结构化类型，不继续扩张一个通用 dict。任何修改请求由 host 校验；hook
不能直接改 mutable session/run，也不能把 approval 工具改为 auto。

> Superseded by `2026-07-11-built-in-summary-compaction.md`: summary compaction is now a built-in
> runtime capability. Plugins contribute typed `CompactionStrategy` implementations instead of a
> generic `ContextTransform`; failures never fall back to history truncation.

## 4. MCP Lifecycle

### 4.1 Configuration

`MCPServerConfig` 第二阶段权威字段：

- server_id、name、enabled、auto_start；
- transport: stdio | streamable_http | sse_compat；
- stdio command/args/cwd；HTTP url/headers refs；
- env literal allowlist 和 credential_refs；
- connect/request/shutdown timeout；
- restart policy、max retries、backoff；
- tool include/exclude/rename policy；
- workspace binding 和 trust requirement；
- metadata。

旧 `http` transport 在迁移时映射为 `streamable_http`，旧 `sse` 映射为 `sse_compat` 并显示兼容提示。

### 4.2 Runtime State

状态机：

```text
disabled
configured → starting → connecting → connected
                          ├→ auth_required
                          ├→ degraded → reconnecting ─┐
                          └→ failed                  │
connected → stopping → stopped ←────────────────────┘
```

- config 持久化，client/process/state 不持久化。
- start/stop/restart 同一 server 串行化且幂等。
- stdio 使用 argument vector，不经 shell；cwd 和 env 在 trust/credential 边界构造。
- stop 先协议关闭，再有限等待，最后终止整个子进程组；不能遗留 child process。
- auto restart 使用有界指数 backoff；用户 stop 会取消 retry。
- Host shutdown 停止所有 owned server，外部 HTTP server 只断开 client。

### 4.3 Handshake and Tool Discovery

连接成功条件是协议 initialize/handshake、capability negotiation 和首轮 tool discovery 全部完成；不得像
第一阶段 framework 一样仅把状态改为 connected。

MCP tool 注册：

- stable local id 为 `mcp.<server_id>.<remote_name>`；显示名可保留 remote name；
- definition 标记 source=mcp、server_id、remote schema/version；
- input schema 在 ToolRegistry 注册时校验，调用前再次校验 arguments；
- result 映射 text、structured content 和 resource refs；未知 content type 保留 diagnostic；
- server disconnect 后 tool 变为 unavailable，新调用返回可回灌错误，不从进行中的 context 突然消失；
- reconnect 后以 generation 原子替换 definitions，名称冲突不覆盖非 MCP 工具。

### 4.4 Credentials, Logs, and Errors

- credential ref 只在 start/connect 边界解析；config/status/event 不保存 resolved value。
- server stdout 若承载协议不得同时作为日志解析；stderr 进入有界诊断 ring buffer，并做已知 secret 替换。
- protocol error、timeout、process exit、auth failure 和 invalid tool schema 使用不同 diagnostic code。
- 连接失败不阻止 OURO 启动；依赖该 server 的工具调用返回 model-readable unavailable result。

### 4.5 MCP Acceptance

- stdio fake server 和 HTTP test server 能完成 handshake、发现工具、调用、取消、断开和重连。
- command 不经 shell，恶意 args/env/cwd 不发生注入或越过未信任 workspace policy。
- start/stop/restart/auto-start 幂等，shutdown 不遗留进程。
- invalid schema、credential missing、timeout、协议错误和崩溃均可诊断并隔离。
- MCP tool 经过与 builtin/plugin 相同的 schema、permission、event 和 abort 流程。

## 5. Skills UX and Enforcement

- catalog 显示 root/source、metadata、enabled、conflicts、validation diagnostics 和 trust state。
- user 可 preview 完整 `SKILL.md`、enable/disable、安装、更新、删除和查看来源。
- workspace Skills 仅在 workspace trusted 后注入 system prompt；未信任时可预览但不生效。
- 同名 Skill 按显式 precedence 解析，并显示 shadowed roots，不静默覆盖。
- `allowed_tools` 在第二阶段成为约束：Skill 激活不能让未允许工具可见，也不能放宽 permission；它不是
  自动授权列表。
- 完整 Skill 内容仍按需读取，catalog 只注入 metadata；大型资源不自动进入上下文。
- Skill 文件解析、目录预算、符号链接和安装目标继续使用第一阶段安全路径规则。

## 6. Model Management UX

ModelConfigService 向 CLI/Desktop 提供：

- provider/endpoint catalog 和 effective source；
- CRUD、default 切换、enable/disable 和 connection check；
- context/output limit、tools、vision、streaming、structured output、reasoning/thinking capability；
- credential ref 状态（configured/missing/error），永不返回 credential value；
- endpoint check 的 latency、model id、capability mismatch 和可操作错误。

规则：

- 默认 provider 必须 enabled 且有有效 default endpoint，沿用第一阶段 Faux fallback。
- 删除/禁用当前 session endpoint 前先要求选择 fallback 或明确让 session degraded。
- 改 global default 不改变已有 session 的显式 endpoint。
- connection check 是只读诊断，不创建 session/run，不写用户消息。
- provider-specific `request_options` 不能覆盖稳定字段。

## 7. Workspace Configuration and Trust

### 7.1 Project Config

支持 `.ouro/config.yaml` 和 `.ouro/config.json`，同一 workspace 同时存在两者时拒绝并提示选择。允许
配置：

- session defaults、provider/endpoint reference、thinking level；
- permission overrides（只能更严格，放宽必须用户确认）；
- Skills roots/enable state；
- MCP server references；
- official plugin settings；
- include/exclude 文件提示和 workspace metadata。

禁止 inline credential、自动安装 plugin、任意 Python import path 和未授权启动命令。

### 7.2 Trust Policy

- 首次打开 workspace 默认 untrusted。
- untrusted 下只允许普通文件浏览和配置 preview；workspace Skills/MCP/脚本/可执行 contribution 不生效。
- trust/revoke 是全局用户配置，产生 audit event；path identity 变化时重新确认。
- 从不信任到信任后重新计算 effective config；从信任到撤销后停止 workspace-owned MCP，移除
  contributions，并不取消无关 session 数据。

## 8. Tool Batches and Parallelism

第二阶段把一次 provider assistant response 中的所有 tool calls 作为 `ToolCallBatch` 持久化在同一条
assistant message 中，并为每个 call 保存对应 tool_result。

调度规则：

- ToolDefinition 增加 `execution_mode: serial | parallel_safe`，默认 serial。
- 任一调用需要 approval、AskUser、共享 workspace mutation 或声明 serial，则整个 batch 按原顺序串行。
- 仅当所有调用均 auto、parallel_safe 且参数验证通过时并行执行。
- 并行结果按原 call 顺序写入 messages/events，即使完成顺序不同；事件另记录实际 started/completed time。
- 一个调用失败不自动取消其他独立调用；run abort 会取消全部可取消调用。
- before/after tool hooks 仍逐调用执行；batch hook 只提供观察，不可绕过单调用 permission。

## 9. Built-in Summary Compaction and Plugin Strategy Replacement

- 默认 `builtin.llm_summary` 由 OURO application 层提供并通过 typed strategy registry 选择。
- 插件只能贡献替代策略；coverage 规划、校验、持久化、激活、锁与事件仍由 host 负责。
- summary record 包含 coverage、lineage、生成模型、prompt version/hash、usage 和 token estimates。
- summary 持久化并增量更新；历史或 lineage 不匹配时拒绝激活。
- strategy error/timeout/invalid artifact 记录失败并中止本次上下文准备；不使用截断降级。
- summary 不写回用户可见 messages，不删除 canonical history。

## 10. Official Planning and Progress Plugin

- plugin 自有 `PlanRecord`/`PlanItem` 存储，不向 core SessionRecord 增加 todo 字段。
- 提供 create/update/complete/show plan 的工具和 Host commands；默认 auto 仅限 plugin 数据，涉及外部执行仍
  由对应工具审批。
- 状态：pending/in_progress/completed/blocked；最多一个 in_progress。
- plan update 产生 plugin event 和可选 context contribution；关闭 plugin 后 session/run 仍可使用。
- UI panel 通过受控 extension view model 提供，不直接依赖 FastAPI/PyWebview 类型。

## 11. Official Minimal Local Memory Plugin

- 默认 opt-in；支持 remember/list/search/forget/disable/export。
- MemoryRecord：memory_id、text、source session/message、tags、created/updated、pinned、metadata。
- 自动写入默认关闭；模型请求 remember 仍经过明确工具调用，敏感内容提示用户。
- 首版检索使用确定性文本索引，不引入向量数据库；返回数量和 token budget 有上限。
- 注入内容标记为 memory synthetic context，并附 source；用户可查看和删除。
- disable 后停止检索/注入但不静默删除；purge 单独确认。

## 12. Official Minimal Browser Plugin

第二阶段只提供可审计的最小网页能力：navigate、read page text、list links、screenshot 和用户触发的
search。实现可采用受控独立进程或 MCP，但对 OURO 暴露相同 Tool contracts。

- 网络访问在 plugin authorization 中可见。
- 导航/读取可按用户策略 auto；表单提交、下载、上传、认证状态变化和其他副作用默认 approval。
- 下载先成为 ResourceRecord，不直接把任意路径返回给模型。
- 页面文本、URL、截图和错误有 size/time limit；超限返回结构化截断信息。
- browser process 崩溃与 core 隔离，可重启；profile/cookie 生命周期在 UI 中明确。
- 不提供任意桌面控制、验证码绕过、隐蔽操作或无限后台爬取。

## 13. Future Subagent Boundary

第二阶段只冻结以下扩展边界，不注册 Agent 工具、不创建 child run：

- RunRecord 的 origin/parent_run_id/initiator_id；
- event 和 resource 对 parent/child run 的可选关联；
- plugin 可请求受控 `RunRequest` 的未来 port，但第二阶段 implementation 返回 unsupported；
- permission、budget、workspace trust 和 cancellation 必须能从 parent 显式派生，不能隐式继承全部能力。

实际调度、child context、并发预算、结果聚合和探索策略属于第三阶段规格。

## 14. Failure Isolation

- plugin load/register 失败：不发布 contributions，其他 plugin 正常。
- hook 失败：按 source 策略隔离并记录，run 使用原始数据继续。
- summary 失败：保留旧 active compaction，记录可见失败并中止本次请求。
- memory/planning store 失败：对应 plugin degraded，core session 不失败。
- browser/MCP process 失败：工具 unavailable，可回灌错误，Host 提供 restart。
- model config check 失败：不改变当前 default/effective config。
- workspace config 非法：整个 workspace layer 不生效，global/default 继续，错误必须显示。

## 15. Acceptance

- plugin entrypoint 在授权后事务式加载；失败、reload 和 disable 不污染 registries。
- 所有已声明 runtime hooks 有明确 typed result 和至少一个真实 consumer，或不进入 public HookName。
- MCP 完成真实协议连接、工具发现、调用、取消、重连和进程清理。
- Skills `allowed_tools`、workspace trust 和同名 conflict 行为可观察并有测试。
- model UI/CLI 能安全管理 endpoint/credential refs，连接检查不写 session。
- tool batch 保留单 assistant message，多调用能按声明安全串行/并行。
- planning、memory、browser official plugin 可独立启停；summary 是内置能力且策略可替换。
- untrusted workspace 不会自动启用 workspace executable capability。
- future subagent 字段不改变第二阶段 run 行为。

## 16. Deferred

- 插件沙箱、签名信任链和在线 marketplace。
- OAuth 浏览器交互的通用框架；第二阶段按 MCP/server adapter 个案接入。
- 向量 memory/RAG、跨项目知识库和自动事实抽取。
- 高级 browser/desktop automation。
- 子智能体实现和远程 worker。
