# OURO Agent 开发架构

本文描述当前代码的权威模块边界。产品愿景见项目概述；编码行为遵循
`OURO_AGENT_编码准则_EN.md`。目录位置不能替代职责判断：新代码应先确定变化原因和
依赖方向，再决定放置位置。

## 依赖方向

```text
CLI / future hosts
        |
        v
application orchestration
   |        |         |
   v        v         v
 core    config    session/tool ports
   ^                    ^
   |                    |
provider adapters   local implementations
```

- `ouro.core` 是内层。它只依赖 `ouro.core`，包含稳定领域记录、Provider/Tool 契约、
  ContextBuilder、取消信号以及单个 active run 的 `InnerAgentLoop`。
- `ouro.application` 编排用例和持久化事务。它可以依赖 core、配置契约、session/tool
  边界，但不能依赖 CLI 或具体 provider 实现。
- `ouro.providers` 是具体厂商适配层。它包含 Faux/OpenAI-compatible 实现、HTTP/SSE
  transport、codec 和 adapter factory；不拥有会话模型选择、运行时状态或 core 契约的
  便利转售。
- `ouro.sessions` 定义存储 port，并提供本地文件实现。文件记录读写和 OS 锁各自封装，
  但都属于同一本地持久化后端。会话命令、查询和 Host DTO 属于 application，不放在该包。
- `ouro.cli` 是组合根和界面适配器。它解析输入、组装 application 服务并格式化输出，
  不承载运行规则。
- `ouro.config`、`ouro.tools`、`ouro.skills`、`ouro.plugins`、`ouro.mcp` 分别维护自身
  配置或扩展边界，不能反向依赖 application/CLI。

## 核心模块职责

### Core

- `core/provider.py`：Provider 协议、模型能力元数据、流事件和 usage。
- `core/adapter.py`：把 Provider 协议适配为上下文流式调用，并处理协作式取消。
- `core/inner_loop.py`：一次 active run 内的模型轮次、流消费、工具 continuation 与
  终止状态机。`InnerLoopPorts` 显式列出所有外部副作用端口。
- `core/context.py` 与 `core/compaction.py`：纯上下文预算、压缩规划和投影规则；不执行
  provider 或文件 I/O。

### Application

- `application/runtime.py`：公开运行门面和事务边界；负责新运行、恢复人工交互、手动
  压缩、abort、session lock 与 follow-up drain。
- `application/runs.py`：durable message、usage 以及 run/session 生命周期快照转换；流式 delta 不写快照。
- `application/tool_calls.py`：模型请求的工具调用在 hooks、executor、事件和消息之间的
  协调；不拥有会话锁或恢复循环。
- `application/models.py`：可选模型 DTO、session 路由 key、目录解析和 request-local
  adapter 选择。具体 adapter builder 由 CLI 组合根注入。
- `application/compaction.py`：LLM 摘要策略与上下文准备/压缩事务。两者共享压缩计划、
  fallback、记录和投影不变量，当前保留在同一模块。
- `application/events.py`、`followups.py`：事件持久化/分发和 durable follow-up queue。
- `application/sessions.py`、`session_history.py`、`session_models.py`：会话命令/查询、历史
  branch/replay 投影、标题状态机、Host DTO 和可恢复 diagnostic；不暴露具体存储对象。
- `application/facade.py`：CLI、未来 Textual/Desktop 共用的进程内命令/查询入口；没有 runtime 的
  组合可以只提供会话维护命令。

### Interface and infrastructure

- `cli/interactive.py`：只维护交互主循环和 active session id。
- `cli/interactive_commands.py`：slash command 解析与查询输出。
- `cli/interactive_io.py`：流式事件渲染以及 approval/AskUser 输入。
- `sessions/local_files.py`：canonical session 聚合文件、崩溃恢复和 trash 发布事务。
- `sessions/local_index.py`：本地可重建 catalog 的扫描、增量更新、dirty 自愈与独立锁。
- `sessions/local_io.py`：本地原子文件替换原语；不包含会话或目录业务规则。
- `sessions/locks.py`：跨进程 session run lock。
- `sessions/index.py`、`layout.py`：可重建 catalog 投影和独立数据布局版本，不承载 application 用例。
- `providers/factory.py`：把已解析配置构建为具体 `RuntimeAdapter`。

## 关键边界决定

### 模型目录属于 application

`provider/endpoint` 是持久化在 session 上的运行路由。列出、解析、切换并为一次请求选择
adapter 都是应用用例，不是厂商 API 适配。因此目录位于 `application/models.py`；
providers 只提供 adapter builder。`AvailableModel` 也不是 Provider 领域协议，它是应用层
查询 DTO。

### RuntimeService 保持门面而非继续机械拆分

`RuntimeService` 的公开方法共享 session lock、abort signal、durable run 状态和 follow-up
drain 事务。将每个方法包装成一个小 service 会引入循环回调和无意义转发。只把有独立
变化原因、能形成完整协作边界的工具调用协调提取到 `ToolCallCoordinator`。

### Catalog 不是 canonical transaction

`session.json`、JSONL、run 和 compaction 是 canonical 数据；`sessions/index.json` 只用于查询。普通
状态变化以独立 catalog lock 增量更新单个 entry，失败时留下 dirty marker 并返回 diagnostic。打开
会话不依赖 index 成功，后续查询可 scan fallback 或全量重建。Host DTO 不包含内部全文字段。

### Branch 不绕过资源身份

Branch 由 application 计算合法消息边界和 lineage，由存储后端 staging 后原子发布。当前
ResourceService 尚未完成，因此含 `resource_refs` 的 branch 明确失败；不复制裸路径或制造悬空 id。

### Event journal 是流式恢复的唯一真相

Provider text/reasoning delta 只顺序追加到 `events.jsonl`。`session.json` 与 run JSON 只在消息提交和
生命周期状态转换时更新，不为每个流片保存第二份 checkpoint。崩溃恢复从最后一个模型轮次的 durable
events 重建未完成 assistant 输出，并避开已经提交的 tool-call/tool-result 消息。Windows snapshot
replace 的瞬时 sharing conflict 由本地文件原语有限重试，不在 application 增加 timer 或后台写队列。

### 不按行数拆配置和压缩模块

`ProviderConfigManager` 虽然较长，但它的唯一变化原因仍是 provider/endpoint 配置事务；
codec、runtime option 解析和数据模型已经分离。`application/compaction.py` 的两个类共享
强不变量。若未来出现第二种上下文准备策略或独立压缩后端，再根据真实变化轴拆包。

### 显式端口不等于过度抽象

`InnerLoopPorts` 的单方法 Protocol 数量较多，但它们把 core 状态机的副作用面完整地暴露
出来，并由 mypy 检查复杂 async 签名。当前改成匿名 `Callable` 会降低可读性，改成多个
空壳 service 则会增加间接层，因此保留。

## 架构验收

`tests/architecture/test_boundaries.py` 自动验证：

- core 不导入外层；
- application 不导入 CLI 或具体 providers；
- config/provider 配置不依赖 CLI、application 或 provider 实现；
- providers、sessions、tools 等低层不依赖 application/CLI；
- 模型目录只由 application 拥有。

结构性修改至少运行：

```bash
python -m pytest
python -m ruff check ouro tests
python -m ruff format --check ouro tests
python -m mypy ouro
python -m compileall -q ouro
```
