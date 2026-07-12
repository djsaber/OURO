# OURO Agent 开发架构

本文是 OURO 当前代码结构的权威说明。新代码必须先确定职责、依赖方向和持久化边界，再选择目录；
目录位置本身不能替代架构判断。

各领域的详细数据契约、失败语义和验收证据见 [规格索引](../dev/specs/README.md)。

## 分层与依赖方向

```text
CLI / future hosts
        |
        v
application orchestration
   |        |          |
   v        v          v
 core    config    session/tool ports
   ^                     ^
   |                     |
provider adapters    local implementations
```

- `ouro.core` 定义稳定领域记录、Provider/Tool 契约、上下文构建、取消信号和单次 active run 的状态机。
  它只能依赖 `ouro.core`。
- `ouro.application` 编排用例、状态转换和持久化事务。它可以依赖 core、配置契约和基础设施端口，
  不能依赖 CLI 或具体 provider 实现。
- `ouro.cli` 是组合根和界面适配器，只负责解析、装配、调用 application 和格式化输出。
- `ouro.providers`、`ouro.sessions`、`ouro.tools` 是外部能力的具体适配与实现。
- `ouro.config`、`ouro.skills`、`ouro.plugins`、`ouro.mcp` 维护各自契约与策略，不反向依赖
  application 或 CLI。

架构测试位于 `tests/architecture/test_boundaries.py`，用于阻止低层反向 import。

## 包职责

| 包 | 负责 | 不负责 |
|---|---|---|
| `core` | 领域记录、协议、纯规则、inner loop | 文件、网络、CLI、具体厂商 SDK |
| `application` | 会话与运行用例、事务编排、Host DTO | 参数解析、终端渲染、HTTP 细节 |
| `config` | 集中配置模型、校验、持久化和运行参数合并 | 模型请求和 UI 查询用例 |
| `sessions` | SessionStore port、本地文件后端、索引和 OS 锁 | 会话命令与 Host DTO |
| `resources` | ResourceStore port、不可变 blob、Session 引用和资源索引 | Host DTO 与运行调度 |
| `providers` | Provider 实现、transport、codec、adapter factory | 会话模型选择和运行状态 |
| `tools` | 工具注册、schema 校验、权限执行和批调度 | 模型轮次状态机 |
| `skills` | 标准 Skills 发现、解析、内容加载和安装管理 | 提示词运行编排 |
| `plugins` / `mcp` | 扩展注册与生命周期边界 | 核心业务状态 |
| `cli` | 命令定义、组合、输入输出 | 领域规则和持久化事务 |

## 核心运行链

```text
CLI / Host
  -> RuntimeService
    -> session lock + recovery
    -> RunLifecycle.start
    -> InnerAgentLoop
       -> ContextPreparationService
       -> RuntimeAdapter -> Provider
       -> ToolCallCoordinator -> ToolBatchExecutor
       -> RunLifecycle complete / fail / pause
    -> queued input drain
```

`RuntimeService` 拥有跨协作者的运行事务：session lock、abort signal、恢复、人工交互恢复和排队输入
串行化。`InnerAgentLoop` 只拥有一个 active run 内的模型轮次状态机；它通过 `InnerLoopPorts` 显式访问
上下文、事件、工具和持久化副作用。`RunLifecycle` 集中承载模型循环中的 run/session 状态转换。

`RuntimeSupervisor` 位于 RuntimeService 之外，只拥有跨 Session 的进程内 FIFO 调度、并发上限、Host
订阅和 shutdown 生命周期。它不写 durable Run 状态机；submit/abort 最终调用 RuntimeService，重启恢复
仍从 SessionStore 读取。每个订阅使用有界队列，慢 Host 不反向阻塞 Run。

排队输入的提交时机是应用层契约：`follow_up` 在当前 Run 完整结束后进入新的 Run，`steer` 在当前
model turn 完整提交后进入同一 Run 的下一次模型请求。工具请求与结果是不可拆分的 turn 边界，因此
steer 不会插入二者之间。消费批量由 `queued_inputs.consumption_mode` 配置决定；`one_by_one` 每个边界
只取一条，`all_at_once` 在同一边界取出全部符合时机的消息，二者都保持符合条件消息的入队顺序。

工具调用以一次完整模型响应为批次边界。Provider 先完成流式响应收集，随后校验整批 tool calls，再按
配置选择顺序或并行执行。assistant tool-call 消息必须先持久化，tool result 随后按 provider 原始顺序提交。

## 模型路由与 Provider 边界

持久化模型路由只有一种格式：`provider_id/endpoint_id`。

- `application/models.py` 负责列出、解析和持久化会话模型路由。
- `config` 负责 provider/endpoint 配置模型与运行参数合并。
- `providers` 负责把已解析参数构建为具体 adapter。
- Provider 保存供应商级连接默认值；Endpoint 保存模型 id、采样参数、能力和可选连接覆盖。

模型路由不接受 model id 或 endpoint id 短名。这样持久化值、CLI 输入和运行时解析使用同一契约，
不会因多个 provider 出现同名模型而产生歧义。

## 消息、工具与上下文

`MessageRecord` 使用类型化字段表达工具关系：

- assistant 工具请求写入 `tool_calls`；
- tool result 关联写入 `tool_result`；
- `metadata` 只保存非结构化附加信息，不保存工具请求或关联身份。

一个 assistant 工具请求及其结果共享 `turn_id`。压缩以该模型回合为原子帧，不能把请求和结果拆开。
没有模型回合身份的普通消息按用户请求分组；不完整工具交换及其后续恢复上下文作为受保护后缀。

`ContextPreparationService` 负责把 canonical messages 投影为模型上下文，应用 Skills 指令、压缩摘要和
token 预算。canonical history 永不被压缩覆盖；`CompactionRecord` 通过 message id 前缀和 digest 验证
摘要来源。

## 本地持久化

默认 `LocalSessionStore` 在 `OURO_HOME` 下保存可读文件：

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
      runs/
      resources/index.json
  resources/
    records/<resource_id>.json
    blobs/sha256/<digest>
    index.json
    staging/
  trash/sessions/
```

`session.json`、messages、events、runs 和 compactions 是 canonical 数据；`sessions/index.json` 是可重建
查询投影。索引失败不得破坏 canonical 写入，失败通过 dirty marker 和 diagnostic 暴露。

Provider text/reasoning delta 只追加到 `events.jsonl`。快照只在消息提交和生命周期状态转换时写入。
崩溃恢复从 durable messages、run 状态和最后模型轮次事件重建未完成输出，不重放结果未知的外部副作用。

`layout.json` 的 `data_layout_version` 标识整套本地数据契约。遇到不兼容版本时初始化必须抛出
`MigrationRequiredError`，不能在加载过程中猜测字段或隐式改写数据。

每个 session 同时只允许一个 active run。进程持有的 OS lock 是存活判断依据，锁文件仅用于诊断。

全局 resource record/blob 与 Session 引用关系是 canonical 数据；`resources/index.json` 是可重建投影。
资源导入先 staging 并计算 SHA-256，再原子发布。分支的消息与资源引用在同一 Session staging 目录发布，
避免先出现带悬空 resource id 的分支。

## 配置契约

配置集中保存在 `config/config.json`，由 `ouro.config` 的类型化 manager 读写。各分区只接受当前权威字段：

- 系统提示词：`prompts.system`；
- Provider：`providers.default` 与 `providers.items`；
- Endpoint：每个 provider 的 `endpoints`；
- 工具执行：`tool_execution`；
- 运行预算：`run_budget`；
- 排队输入：`queued_inputs`；
- 压缩：`compaction`；
- Skills：`skills`。

无效结构必须抛出 `ConfigError`，不能静默降级或猜测另一种格式。凭据通过 `CredentialRef` 进入 provider
factory，业务代码不直接读取环境变量。

## CLI 契约

CLI 命令使用资源层级表达职责：

- `provider set` 只管理 provider；
- `provider endpoint set` 只管理 endpoint；
- `provider check` 负责连接检查；
- `session compact` 负责手动压缩；
- `resource import/list/show/export/remove` 管理 Session 资源；
- `skills remove` 负责移除 OURO 管理的 Skill。

命令入口不得实现运行规则。跨 Host 可复用的查询和命令进入 `ApplicationFacade` 或对应 application service。

## 扩展边界

- Hook 失败由 `RuntimeEventRecorder` 隔离并转为事件，不允许破坏 durable run。
- 插件只能通过注册表贡献工具、Hook、命令、上下文 provider 或压缩策略。
- MCP 工具最终必须适配为 OURO Tool 契约，第三方协议对象不得进入 core。
- 外部 Agent Skills frontmatter 在 skills parser 边界校验，核心运行只接收类型化 `SkillMetadata` 和指令文本。

## 未来演进触发条件

只有出现独立变化轴时才新增层级：

- 第二种 Supervisor 后端或跨进程调度出现后，再拆分当前进程内调度实现；
- Desktop 需要 provider/config 查询后，增加 application 配置门面；
- 第二种压缩后端独立演进后，拆分策略执行与上下文准备；
- 第二种资源后端出现后，再扩展 ResourceStore，当前只支持本地 immutable blob；
- 真实 MCP transport 落地后，在 `mcp` 内部拆分连接、协议和注册适配。

这些边界应由真实用例驱动，不能为了缩短文件或统一命名而增加空转 service、回调层或通用 manager。

## 验收要求

每次架构改动至少验证：

```bash
python -m pytest
python -m ruff check ouro tests
python -m ruff format --check ouro tests
python -m mypy ouro
python -m compileall -q ouro
git diff --check
```

同时检查依赖方向、持久化失败路径、并发锁、取消、公开数据结构和文档是否仍与实现一致。
