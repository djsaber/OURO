# OURO 插件开发指南

本文面向本地 user plugin 作者，描述当前代码实际支持的契约。规范性约束以
[插件系统目标规格](../dev/specs/plugin-system.md)为准。

## 1. 先选择正确的扩展形态

OURO 插件是系统级扩展模块，只有两类：

- `builtin` 由 OURO composition root 提供，user package 不能安装、更新、移除或覆盖其 identity；
- `user` 安装到一个 `OURO_HOME`，不随项目或 workspace 自动激活。

Builtin 不是“启动时自动安装的 user plugin”，也不是绕过插件生命周期的 Host baseline。启用后它与 user plugin 使用
相同的 Generation、gate、Catalog lease 和 durable execution contract，只省略 source materialization、安装、能力授权和
代码信任。`builtin | user` 不能由 Manifest 声明；它由 installer 或 composition root 根据真实供应方式确定。

workspace 可以作为一次 Tool/Command 调用的 request context。项目级说明应使用 workspace Skill 或提示词，而不是
创建项目插件。

按能力选择 runtime：

| Runtime | 用途 | 信任与资源边界 |
|---|---|---|
| `declarative` | Agent Skill、MCP descriptor 等静态资源 | 不 import 插件代码 |
| `python-inprocess` | 深度 Python 扩展、低延迟回调、Host request context | 与 OURO 同进程、full-trust、exact-digest code trust |
| `worker` | 独立进程、多语言或进程故障隔离 | 严格 stdio JSON-RPC 协议；不是 OS sandbox |

不要为了简单 Skill 建 worker，也不要把需要独立第三方依赖环境的代码塞进 OURO 主 Python 环境。

OURO 自身开发者新增 builtin 时，应在 `ouro/builtin_plugins/` 中实现普通 Plugin SDK factory，并在本地 application
composition root 的 builtin registry 显式装配。插件机制层不得 import Plan、Goal 等产品插件；具体 builtin 也不得取得
`RuntimeService`、`SessionStore` 或可变 registry。首版 builtin 只支持已经有真实需求的 in-process、空配置形态。

用户可用普通管理入口查询、启用、关闭、重启和检查 builtin，例如
`ouro plugin enable dev.ouro.plan_mode`。Builtin 不经过 install/authorize/trust-code/configure/update/remove；这些操作会
明确返回 operation-not-allowed。关闭后可以显式清理其私有 data，但不能删除随 OURO 分发的 definition/code。

## 2. 最小本地闭环

创建一个可运行的 in-process 插件：

```bash
uv run ouro plugin scaffold ./my-plugin \
  --plugin-id dev.example.my_plugin \
  --name "My plugin" \
  --json
```

scaffold 原子创建新目录，目标已存在时拒绝覆盖，并在发布目录前运行完整 package validation。随后显式完成安装与
授权：

```bash
uv run ouro plugin validate ./my-plugin --json
uv run ouro plugin install ./my-plugin --home ./.ouro-dev --json
uv run ouro plugin authorize dev.example.my_plugin --home ./.ouro-dev --json
uv run ouro plugin trust-code dev.example.my_plugin --home ./.ouro-dev --json
uv run ouro plugin enable dev.example.my_plugin --home ./.ouro-dev --json
uv run ouro plugin run-command dev.example.my_plugin:hello world --home ./.ouro-dev
```

安装只保存不可变 bytes，不 import、不执行、不给权限也不启用。`authorize` 接受 manifest 声明的 Host capability；
`trust-code` 接受 executable package 的 exact digest。两者不是同一个授权。

本地修改后可使用开发 reload：

```bash
uv run ouro plugin reload dev.example.my_plugin ./my-plugin \
  --authorize \
  --trust-code \
  --home ./.ouro-dev \
  --json
```

reload 先静态校验并并排安装新 digest；只有候选配置、能力授权和代码信任完整时才原子 select。省略显式授权选项时，
active 插件的候选保持并排安装并返回 `needs_authorization` 或 `needs_code_trust`，旧 Catalog 继续可用。相同 digest 的
reload 执行显式 restart，不制造新 package。

## 3. Manifest 与 package

Manifest 固定在 `.ouro-plugin/plugin.json`，关键 identity 是：

- `plugin_id`：反向域名风格的稳定 identity；
- `version`：作者发布的 SemVer；
- `package_digest`：OURO 对规范化文件树计算的 SHA-256，不由作者填写；
- `namespace`：Tool 暴露名和 Python owned module 的命名边界；
- `engines.ouro` / `engines.plugin_api`：显式版本范围；
- `runtime`、`contributes`、`permissions`、`dependencies`：可执行契约。

相同 version、不同 bytes 会形成不同 digest 并并排保存，不静默覆盖。除显式提供的单向 state schema migration 外，
旧 manifest/SDK 或 worker protocol 不做隐式兼容，schema 错误会直接拒绝。

Git source 使用 shallow、blob-filtered fetch；指定 `--subdirectory` 时在 checkout 前按 Git tree 校验文件数与大小，
再做 sparse checkout。Git LFS 自动下载被禁用。archive/local/Git 最终都经过同一 package path、symlink、类型、大小、
manifest 与 digest 校验。

## 4. SDK contribution

in-process 插件只从 `ouro.sdk.plugins.v1` 使用公开类型。factory 接收 `PluginContext` 并返回实现以下生命周期的对象：

```python
class Plugin:
    async def prepare(self) -> ContributionSet: ...
    async def start(self) -> None: ...
    async def stop(self, reason, deadline) -> None: ...
```

- `prepare` 声明 contribution，不发布全局对象，不启动长期副作用；
- `start` 在候选 contribution 与 required resource 准备完成后调用；
- `stop` 必须幂等，并在 deadline 内关闭 owner 资源。

`PluginContext.data` 保存插件私有状态；若插件需要产生可由 OURO 内置文件工具读取或编辑的用户制品，使用
`PluginContext.artifact_root`。Host 为每个插件提供 `OURO_HOME/artifacts/<plugin-id>/` 隔离目录；该目录不是
configuration、插件私有 data 或 canonical Session 数据。没有本地用户数据根的 Host 可以返回 `None`，依赖文件
制品的插件应在激活时明确失败。

当前稳定 contribution 包括 Tool、Command、Observer、Context Provider、Prompt Policy、Turn Policy、Compaction
Strategy、Skill、MCP Server、Background Service、Continuation Policy、Tool Policy 和 Session Controller。插件不能
动态注册新的 extension point。

Tool 可选择接收 Host-owned `ToolExecutionContext`，使用其中的 workspace、Host artifact 受约束路径解析、取消与
输出限制。Command、
Continuation 不直接写 Session/Run，而是返回类型化意图；application 层负责 synthetic input、队列和持久化。Tool
Policy 只能把执行提升为审批或拒绝，不能降低 Host policy、批准调用或改写参数。

Tool handler 除 `ToolOutput` 或字符串外，还可返回 `ToolInputRequest(data=...)`。`data` 是 Host 透明转发和持久化的
任意 JSON 值；Host 可据此构建单选、多选、表单或卡片界面。Run 会进入 `waiting_user`，Host 调用
`answer_user` 后，非 resumable 工具把回答作为该次 tool call 的结果；resumable 工具则由原 contribution 的
`resume_handler` 消费 `ToolResumeCall`，可以返回最终输出或下一次 `ToolInputRequest`。恢复始终绑定原 Catalog 和
execution contract。此协议收集数据，不表示批准或拒绝工具执行。

`ToolResumeCall.resolution` 是判别联合，而不是把控制信息混入任意回答 JSON：正常交互收到
`AnswerResolution(kind="answer", value=...)`，`call.answer` 仍可作为该分支的便捷投影；AFK 等 Host policy
会发送 `UnattendedResolution(kind="unattended", resolved_by="host_policy", resolver_id=...)`。只有同时声明
`resume_handler` 和 `supports_unattended=True` 的工具会收到无人值守 resolution。该 handler 必须返回最终输出；
再次返回 `ToolInputRequest` 会被 Host 转换为 `UnattendedInteractionLoop`。未声明能力的工具不会收到伪造答案，而会
得到标准 `UserInputUnavailable` 工具结果。

```python
from ouro.sdk.plugins.v1 import ToolInputRequest

async def confirm_plan(call):
    return ToolInputRequest(
        data={
            "type": "plan_confirmation",
            "plan_path": call.arguments["plan_path"],
            "plan": call.arguments["plan"],
            "plan_digest": call.arguments["plan_digest"],
            "options": call.arguments.get("options", []),
            "actions": ["accept", "revise", "reject"],
            "error": None,
        }
    )
```

前端适配只负责把用户选择编码为请求生产者定义的 JSON，领域校验和状态迁移仍由 resumable tool 完成。OURO CLI
只为带官方内置 Plan 插件 provenance 的 `plan_confirmation` 提供专用展示；其他插件返回相同 `type` 不会获得官方
界面，而是继续使用通用 JSON/文本输入。这种展示映射不改变 `ToolInputRequest` 的 Host-neutral Core 契约。

Context Provider 返回的字符串或普通 `ContextFragment` 默认只影响本次模型请求。如果 Harness 指令必须进入可审计、
可恢复的 canonical history，显式返回持久 fragment：

```python
from ouro.sdk.plugins.v1 import ContextFragment, ContextOutput

def plan_scaffold(call):
    return ContextOutput(
        fragments=(
            ContextFragment(
                text="Plan mode is active for this Session.",
                persistence="message",
                semantic_tag="plan-context",
            ),
        )
    )
```

Host 在调用模型前把它写成带 plugin/provider/generation provenance 的 `system_synthetic` 消息；同一
Run/model round 的恢复不会重复写入。`semantic_tag` 默认是 `system-reminder`，可设置为小写字母、数字和连字符
组成的具体语义名。构建 ProviderContext 时正文会被包装为 `<plan-context>...</plan-context>`；标签只描述来源语义，
不会改变消息的协议权限。不要把瞬时状态快照无差别持久化。

如果片段属于完整 system prompt 而不是 synthetic message，显式使用
`ContextFragment(target="system_prompt")`。Application 会在基础 system prompt 与 Skills 指令之后按 Context
Provider 顺序追加它；该目标只允许 `persistence="ephemeral"`，不会构造或持久化消息。
`ContextCall.workspace` 提供本次 Session 中 Agent 的工作目录。

### Observer 与 Prompt Policy

Observer 只接收已经发生的类型化事实，handler 必须返回 `None`。Application 为每个 registration 使用独立有界
mailbox；回调不在模型、工具或 Run 的语义关键路径中被等待。需要改变行为时应使用对应的 Context Provider、Policy、
Strategy 或 Session Controller，不能依赖 Observer 的执行顺序。

Prompt Policy 在 Host 路由后的直接用户提示进入 canonical message、durable queue 或 Run 前返回 `allow | deny`。它不能改写提示；
timeout、异常和非法返回都会 fail-closed。User plugin 必须声明 `sessions.prompt.inspect`，因为回调会收到原始提示以及
附件的 resource id、media type、size，不会收到附件正文或来源路径。完整示例见
[`inprocess-observer`](../examples/plugins/inprocess-observer)和
[`inprocess-prompt-policy`](../examples/plugins/inprocess-prompt-policy)。

### Session 级控制模式

如果插件需要让 Agent 在多个模型轮次或多个 Run 中保持一种行为，声明 `SessionControllerContribution`，不要让
Command 直接把插件私有 state 改成 active。Command/Tool 返回 `SessionControlTransition(action="acquire"|"release")`，
Host 完成 claim 仲裁和 durable activation 后才调用 lifecycle handler。

Manifest 中可把需要随模式启停的贡献绑定到 controller：

```json
{
  "contributes": {
    "commands": [{
      "id": "mode",
      "description": "Control this Session mode.",
      "requires_session": true
    }],
    "session_controllers": [{
      "id": "mode",
      "description": "Own this Session mode.",
      "claims": [{"domain": "dev.example.primary_behavior", "access": "exclusive"}]
    }],
    "context_providers": [{"id": "scaffold", "active_when": "mode"}],
    "tool_policies": [{"id": "gate", "active_when": "mode"}],
    "tools": [{
      "id": "finish",
      "exposed_name": "FinishMode",
      "description": "Finish the active mode.",
      "resumable": true,
      "interaction_controller": "mode"
    }]
  }
}
```

需要读取 Session 状态、返回 Session control transition 或以其他方式依赖目标 Session 的 Command，应在 Manifest
声明 `requires_session: true`。Host 会在 handler 执行前保证 `call.session_id` 非空；Interactive CLI 在第一条输入就是
这类命令时，会用当前模型、审批与交互策略懒创建 Session 后再执行。默认值为 `false`，查询全局状态等无 Session
命令仍可在 `call.session_id=None` 下运行，也不会仅因执行命令而产生空 Session。SDK handler 不重复声明该字段，
Manifest 是 Host 路由前置条件的唯一来源。

同一 claim domain 的排他 controller 会在 activate 前得到确定冲突；插件不能用 `replace` 抢占其他 owner。为用户提供
友好的 `off` 命令，但它仍应返回 release transition。Host 的 `/control status` 与 `/control release ... [--force]`
不依赖具体插件命令，可在 owner 故障时撤销 gate。`active_when` 由 Host 按模型轮次快照执行，回调不应再次用私有
`phase` 决定是否应用权限策略。需要常驻模型工具列表的进入、查询和退出工具不要声明 `active_when`；resumable 工具若
产生应随 controller 释放而取消的等待交互，应改用独立的 `interaction_controller` 声明其生命周期归属。工具可见性与
pending interaction 归属是两个独立契约。

`SessionControllerCall` 在 activate/deactivate 两条路径都提供 `session_kind`（`primary | subagent`）；owner 应依据该
Host 事实决定自己是否支持该类 Session，不能从 session id 或目录推断。release transition 的 `payload` 会原样传给
deactivate，与 acquire/activate 对称；需要提交 owner 私有终态时，应把终态数据放在 release payload 中，由
deactivate 幂等落库，不能先改私有状态再尝试释放 Host activation。

绑定 controller 的 Continuation Policy 会收到所有 terminal `RunOutcome`。非 `completed` Run 只能返回
`pause | fail`，不能自动 `continue`；若需结束自身控制权，可在 `ContinuationDecision.session_control_transitions`
返回一个 release local intent。Host 只允许它释放同 plugin、且被该 Run checkpoint 捕获的精确 activation，并先持久化
settlement plan，再幂等执行 release/queue/checkpoint resolve。owner policy 不可用、超时或异常时，Host 仅 force-release
该 policy 绑定的精确 activation，并明确记录 owner 私有清理未确认；多个非 ignore 决策仍视为冲突并暂停。

## 5. Catalog、owner 与 durable contract

一个 Run 只读取一个不可变 `ExtensionCatalogLease`。插件更新先完整构建候选 generation 与 Catalog，再切换 current
pointer；旧 Run 继续持有旧 Catalog。Background Service 是每 plugin 的 activation-singleton，不属于 Run Catalog。

每个 generation 拥有 instance、worker、MCP、task、module、gate 和 contribution。disable/revoke 关闭 gate 后不能
开始新的插件回调；lease 释放后 owner 才回收。

Run、Tool/Approval 和 Continuation 持久化稳定 `execution_contract_hash`，覆盖 package、immutable configuration
revision、传递 dependency lock、Plugin API/worker protocol 和 contribution contract。随机 `generation_id` 只用于
当前进程 owner 与诊断。重启后可以从保留的等价内容构建新的 process-local generation；同名但不同 contract 的 Tool
不能接管 pending invocation。

对 builtin，用户包摘要由 definition 在装配时冻结的 builtin contract hash 取代；该摘要同时覆盖规范化 Manifest 与实现
目录内容，因此代码变化不会沿用旧 release identity。

## 6. 权限、配置与数据

OURO 保留个人智能体所需的基础边界：

- executable user plugin 的 exact-digest code trust；
- 封闭 permission id 与 capability authorization；
- credential 只保存 `env:VARIABLE` binding reference，秘密不进入 manifest/state/worker initialize JSON；
- user plugin Tool 始终经过 Host schema、审批、Tool Policy 和 `ToolExecutor`；
- package、workspace path 与 owner 生命周期校验。

`python-inprocess` 是 full-trust，能够直接调用 Python/OS API；OURO 不把它描述为沙箱。worker 提供进程和协议隔离，
同样不承诺系统调用级沙箱。

配置是内容寻址的 immutable revision。active 插件 configure 使用候选 generation，失败不切换 active revision。
candidate 通过 Host data API 的写入先进入暂存视图，发布成功才提交；直接绕过 Host API 的 in-process OS 写入不属于
此事务保证。

## 7. Worker 最小协议

worker 从 stdin 逐行读取 JSON-RPC 2.0 request，只向 stdout 写协议 JSON；诊断写 stderr 或发送
`diagnostic.emit` notification。核心方法是：

- `initialize`：确认 manifest 的 `protocol_version`；
- `contributions.prepare`：返回 manifest 已声明 contribution 的协议描述；
- `lifecycle.start` / `lifecycle.stop` / `shutdown`；
- contribution 调用，例如 `tool.call`；
- resumable 工具使用 `tool.resume`；协议 `2.0` 的 `call.resolution` 传输 `answer` 或 `unattended` 判别对象；
- Observer 使用 `observer.observe`，Prompt Policy 使用 `prompt_policy.call`；
- `cancel`：取消超时或被 Host 取消的 request。

stdout 污染、未知消息、过大响应、声明不一致和错误 result shape 都会使协议调用失败。完整最小实现见
[`examples/plugins/worker-tool`](../examples/plugins/worker-tool)。

worker tool 以 `{"input_request": <任意 JSON 值>}` 返回同一种挂起请求；该字段不能与 `content`、
`structured`、`resource_refs` 或 `is_error` 同时出现。
声明 `resumable=true` 的 worker tool 通过 `tool.resume` 接收原请求和 `resolution`；如需在 AFK 下自定义业务迁移，
还必须在 manifest 中声明 `supports_unattended=true`。当前 worker protocol 为 `2.0`，Host 不把旧版 `answer`
字段静默转换为新契约。
协议 `2.0` 在 `session_controller.call` 输入中提供 `session_kind`，允许 `continuation.call` 输出
`session_control_transitions`，并使用新的 Observer/Prompt Policy 契约；它与 in-process Plugin API `2.0.0` 使用相同语义。
`context.call` 的 `fragments` 既接受字符串，也接受
`{"text": "...", "persistence": "ephemeral|message", "semantic_tag": "plugin-context"}` 对象。

## 8. 社区资源导入前审查

```bash
uv run ouro plugin import-report ./community-package --json
```

报告逐项给出：

- `supported`：可直接作为 Agent Skill 等资源表达；
- `converted`：语义可表达，但必须转换为 OURO descriptor/permission；
- `ignored`：源 Host 专属且不进入 OURO；
- `incompatible`：需要显式 worker/MCP port，不能直接运行。

每个可迁移项给出 `ouro_target`，报告同时列出 outcome count、lost semantics、安全差异和所需权限。该命令只读文件，
不执行社区 Hook、JavaScript runtime、安装脚本或 MCP server。

## 9. 可运行示例

- [`declarative-skill`](../examples/plugins/declarative-skill)：静态 Agent Skill；
- [`inprocess-command`](../examples/plugins/inprocess-command)：Python Command；
- [`inprocess-observer`](../examples/plugins/inprocess-observer)：非阻塞类型化 Observer；
- [`inprocess-prompt-policy`](../examples/plugins/inprocess-prompt-policy)：持久化前拒绝提示的 Prompt Policy；
- 内置 Plan 模式位于 `ouro/builtin_plugins/plan_mode`，展示了产品内置插件如何复用与用户插件相同的 Context/Tool Policy、Generation 与可恢复工具契约；
- 内置 System Information 插件位于 `ouro/builtin_plugins/system_information`，展示了请求级 system prompt
  fragment、运行环境边界和默认启用 builtin 的组合方式；
- [`worker-tool`](../examples/plugins/worker-tool)：stdio worker Tool。

五个 `examples/plugins` package 由测试执行真实 validate/authorize/enable 生命周期；内置 Plan 则由 builtin
enable/disable、Generation、工具恢复与跨进程 Run 集成测试覆盖，避免文档示例与实现漂移。
