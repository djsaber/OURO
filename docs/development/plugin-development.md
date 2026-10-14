# OURO 插件开发指南

> 文档类型：现行开发指南

本文面向本地 user plugin 作者，描述插件开发和运行契约。规范性约束以
[插件系统规格](../specs/plugins.md)为准。

## 1. 先选择正确的扩展形态

OURO 插件是系统级扩展模块，只有两类：

- `builtin` 由 OURO composition root 提供，user package 不能安装、更新、移除或覆盖其 identity；
- `user` 安装到一个 `OURO_HOME`，不随项目或 workspace 自动激活。

Builtin 不是“启动时自动安装的 user plugin”，也不是绕过插件生命周期的 Host baseline。启用后它与 user plugin 使用
相同的 Generation、gate、Catalog lease 和 durable execution contract，只省略 source materialization 与安装。
`builtin | user` 不能由 Manifest 声明；它由 installer 或 composition root 根据真实供应方式确定。

workspace 可以作为一次 Tool/Command 调用的 request context。项目级说明应使用 workspace Skill 或提示词，而不是
创建项目插件。

按能力选择 runtime：

| Runtime | 用途 | 信任与资源边界 |
|---|---|---|
| `declarative` | Agent Skill、MCP descriptor 等声明式资源 | 不 import 插件 entrypoint；启用接受 selected digest |
| `python-inprocess` | 深度 Python 扩展、低延迟回调、Host request context | 与 OURO 同进程、full-trust；启用接受 selected digest |
| `worker` | 独立进程、多语言或进程故障隔离 | 严格 stdio JSON-RPC 协议；不是 OS sandbox |

不要为了简单 Skill 建 worker，也不要把需要独立第三方依赖环境的代码塞进 OURO 主 Python 环境。

OURO 自身开发者新增 builtin 时，应在 `ouro/builtin_plugins/` 中实现普通 Plugin SDK factory，并在本地 application
composition root 的 builtin registry 显式装配。插件机制层不得 import Plan、Goal 等产品插件；具体 builtin 也不得取得
`RuntimeService`、`SessionStore` 或可变 registry。Builtin 使用 in-process、空配置形态，并只承载产品内建能力。

用户可用普通管理入口查询、启用、关闭、重启和检查 builtin，例如
`ouro plugin enable dev.ouro.plan`。Builtin 不经过 install/configure/update/remove；这些操作会
明确返回 operation-not-allowed。关闭后可以显式清理其私有 data，但不能删除随 OURO 分发的 definition/code。

## 2. 最小本地闭环

创建一个可运行的 in-process 插件：

```bash
uv run --frozen ouro plugin scaffold ./my-plugin \
  --plugin-id dev.example.my_plugin \
  --name "My plugin" \
  --json
```

scaffold 原子创建新目录，目标已存在时拒绝覆盖，并在发布目录前运行完整 package validation。随后显式完成安装与
启用：

```bash
uv run --frozen ouro plugin validate ./my-plugin --json
uv run --frozen ouro plugin install ./my-plugin --home ./.ouro-dev --json
uv run --frozen ouro plugin enable dev.example.my_plugin --home ./.ouro-dev --json
uv run --frozen ouro plugin run-command dev.example.my_plugin:hello world --home ./.ouro-dev
```

安装只保存不可变 bytes，不 import、不执行也不启用。`enable` 显式接受并运行当前 selected digest；`update` 只并排
安装候选，`select`、`reload` 和 `rollback` 才显式切换版本。激活前 Host 重新校验包完整性、配置、依赖与 Manifest
契约。插件作者仍只声明 `runtime`、`permissions` 和 `contributes` 等可验证事实。

本地修改后可使用开发 reload：

```bash
uv run --frozen ouro plugin reload dev.example.my_plugin ./my-plugin --home ./.ouro-dev --json
```

reload 先静态校验并并排安装新 digest；只有候选配置、依赖和包完整性都有效时才原子 select，失败时旧 Catalog
继续可用。相同 digest 的 reload 执行显式 restart，不制造新 package。

## 3. Manifest 与 package

Manifest 固定在 `.ouro-plugin/plugin.json`，关键 identity 是：

- `plugin_id`：反向域名风格的稳定 identity；
- `version`：作者发布的 SemVer；
- `package_digest`：OURO 对规范化文件树计算的 SHA-256，不由作者填写；
- `namespace`：Tool 暴露名和 Python owned module 的命名边界；
- `engines.ouro` / `engines.plugin_api`：显式版本范围；
- `runtime`、`contributes`、`permissions`、`dependencies`：可执行契约。

相同 version、不同 bytes 会形成不同 digest 并并排保存，不静默覆盖。旧 state、manifest/SDK 或 worker protocol
不做兼容或迁移，schema 错误直接拒绝。

Git source 使用 shallow、blob-filtered fetch；指定 `--subdirectory` 时在 checkout 前按 Git tree 校验文件数与大小，
再做 sparse checkout。Git LFS 自动下载被禁用。archive/local/Git 最终都经过同一 package path、symlink、类型、大小、
manifest 与 digest 校验。

## 4. SDK contribution

in-process 插件只从 `ouro.sdk.plugins.v3` 使用公开类型。factory 接收 `PluginContext` 并返回实现以下生命周期的对象：

```python
class Plugin:
    async def prepare(self) -> ContributionSet: ...
    async def start(self) -> None: ...
    async def stop(self, reason, deadline) -> None: ...
```

- `prepare` 声明 contribution，不发布全局对象，不启动长期副作用；
- `start` 在候选 contribution 与 required resource 准备完成后调用；
- `stop` 必须幂等，并在 deadline 内关闭 owner 资源。

`PluginContext.data` 保存实例级插件私有数据；会话业务状态使用调用内的 `call.session_state`。若插件需要产生可由 OURO 内置文件工具读取或编辑的用户制品，使用
`PluginContext.artifact_root`。Host 为每个插件提供 `OURO_HOME/artifacts/<plugin-id>/` 隔离目录；该目录不是
configuration、插件私有 data 或 canonical Session 数据。没有本地用户数据根的 Host 可以返回 `None`，依赖文件
制品的插件应在激活时明确失败。

`PluginContext.environment.agent_instructions(workspace)` 读取 Host 配置与 workspace 对应的指令文档。
结果包含来源路径、作用域和文本，插件不接收整个 Host home。Agent Context 与用户插件使用同一公开端口。

原生 contribution 包括 Tool、Command、Observer、Context Provider、Prompt Policy、Turn Policy、Compaction
Strategy、Skill、MCP Server、Background Service、Continuation Policy、Tool Policy 和 Session Controller。
这些类型对应明确的 Host 语义消费者；新业务通过独立的 `exports` 服务声明扩展，无需增加原生 contribution 类型。
完整 producer/consumer/contract 证据见[插件系统规格](../specs/plugins.md#91-目标稳定-contribution)。

Tool 可选择接收 Host-owned `ToolExecutionContext`，使用其中的 workspace、Host artifact 受约束路径解析、取消与
输出限制。Tool / resume 以及 Compaction 的可选上下文回调在 contribution 装配时绑定；签名不满足调用契约时
装配失败，执行期间不重复探测签名。Command、
Continuation 不直接写 Session/Run，而是返回类型化意图；application 层负责 attributed input、队列和持久化。
Compaction Strategy handler 同样可以把 `CancellationContext` 作为第二个参数；长时间运行的进程内同步 handler 必须
周期性调用 `raise_if_aborted`，异步 handler 和 worker 调用则由 Host task cancellation 传播。Host 不会强杀或遗弃仍在
运行的 Python 同步线程，取消后产生的迟到摘要不会被激活。
`SessionInput` 分别携带 `role`、`scaffold` 和可选 `semantic_tag`；SDK 的默认构造值为
`role="system", scaffold=True, semantic_tag=None`，插件应按消息真实语义显式选择，Host 不从其中任何一项推导
另一项。插件 provenance 始终保留，`role="user"` 不等于直接用户来源或授权。Tool
Policy 只能把执行提升为审批或拒绝，不能降低 Host policy、批准调用或改写参数。

`ToolOutput.content` 接受字符串，或由 `ToolTextContent(text=...)`、`ToolImageContent(uri=..., media_type=...)`、
`ToolAudioContent(uri=..., media_type=...)`、`ToolVideoContent(uri=..., media_type=...)` 组成的有序 tuple。
音频与视频必须声明对应 MIME 类型。图片和视频 URI 使用 HTTP(S) 地址或 Base64 data URL，
音频输入使用 Base64 data URL；不把本地路径或编码写入文本冒充媒体。
SDK 保持独立于内部领域模型；worker 使用相同的 `type: text/image/audio/video`、`text`、`uri`、`media_type` 字段返回 JSON
内容数组，非法内容类型在 worker 边界拒绝。执行与 resume 使用同一内容契约。
Host 保留执行事实，按当前端点能力和协议生成原生结果或补充上下文；插件无需判断供应商协议，也不自行追加
用户消息。`structured` 用于业务数据，`resource_refs` 用于会话资源关系，均不代替 `content`。

Tool handler 除 `ToolOutput` 或字符串外，还可返回 `ToolInputRequest(data=...)`。`data` 是 Host 透明转发和持久化的
任意 JSON 值；Host 可据此构建单选、多选、表单或卡片界面。Run 会进入 `waiting_user`，Host 调用
`answer_user` 后，非 resumable 工具把回答作为该次 tool call 的结果；resumable 工具则由原 contribution 的
`resume_handler` 消费 `ToolResumeCall`，可以返回最终输出或下一次 `ToolInputRequest`。恢复检查原调用的 execution contract 及当前权限；不匹配时保留回答并返回局部错误。此协议收集数据，不表示批准或拒绝工具执行。

`ToolResumeCall.resolution` 是判别联合，而不是把控制信息混入任意回答 JSON：正常交互收到
`AnswerResolution(kind="answer", value=...)`，`call.answer` 仍可作为该分支的便捷投影；AFK 等 Host policy
会发送 `UnattendedResolution(kind="unattended", resolved_by="host_policy", resolver_id=...)`。只有同时声明
`resume_handler` 和 `supports_unattended=True` 的工具会收到无人值守 resolution。该 handler 必须返回最终输出；
再次返回 `ToolInputRequest` 会被 Host 转换为 `UnattendedInteractionLoop`。未声明能力的工具不会收到伪造答案，而会
得到标准 `UserInputUnavailable` 工具结果。

```python
from ouro.sdk.plugins.interactions import PlanConfirmationRequest
from ouro.sdk.plugins.v3 import ToolInputRequest

async def confirm_plan(call):
    return ToolInputRequest(
        data=PlanConfirmationRequest(
            plan_path=call.arguments["plan_path"],
            plan=call.arguments["plan"],
            plan_digest=call.arguments["plan_digest"],
        ).to_json()
    )
```

`ouro.sdk.plugins.interactions` 提供 `PlanConfirmationRequest`、`PlanOption` 和 `PlanConfirmationAnswer`。
TUI 按 `plan_confirmation` 的公开结构提供计划预览与选择，不要求 builtin 身份或特定工具名。无效或未知结构使用
通用 JSON/文本输入。前端只编码真实用户的选择，校验计划内容、应用答案和迁移业务状态仍由 resumable tool 完成；
呈现类型不授予权限，不激活或释放 Session controller，也不改变无人值守处理。这种映射保留 `ToolInputRequest`
的 Host-neutral Core 契约。

Context Provider 返回的字符串或普通 `ContextFragment` 默认只影响本次模型请求。如果 Harness 指令必须进入可审计、
可恢复的 canonical history，显式返回持久 fragment：

```python
from ouro.sdk.plugins.v3 import ContextFragment, ContextOutput

def plan_scaffold(call):
    return ContextOutput(
        fragments=(
            ContextFragment(
                text="Plan mode is active for this Session.",
                persistence="message",
                role="system",
                scaffold=True,
                semantic_tag="plan-context",
            ),
        )
    )
```

Host 在调用模型前按 fragment 声明的 role/scaffold/tag 写入带 plugin/provider/generation provenance 的 canonical
消息；同一 Run/model round 的恢复不会重复写入。`semantic_tag` 默认是 `None`，可设置为小写字母、数字和连字符
组成的具体语义名。只有设置标签时，构建 ProviderContext 才把正文包装为
`<plan-context>...</plan-context>`；标签不会改变 role、scaffold、来源或协议权限。不要把瞬时状态快照无差别持久化。

如果片段属于完整 system prompt 而不是 canonical message，显式使用
`ContextFragment(target="system_prompt")`。Application 会在基础 system prompt 与 Skills 指令之后按 Context
Provider 顺序追加它；该目标只允许 `persistence="ephemeral"`，不会构造或持久化消息。
提供方可通过 `semantic_tag` 声明片段语义，正文只返回未包裹的原始文本；标签存在时 Core 与 message target
共用同一 semantic envelope 投影，验证标签、转义正文中的同名包络分隔符并构造标签；标签不存在时原样投影。
`ContextCall.workspace` 提供本次 Session 中 Agent 的工作目录。

### Observer 与 Prompt Policy

Observer 只接收已经发生的类型化事实，handler 必须返回 `None`。Application 为每个 registration 使用独立有界
mailbox；回调不在模型、工具或 Run 的语义关键路径中被等待。需要改变行为时应使用对应的 Context Provider、Policy、
Strategy 或 Session Controller，不能依赖 Observer 的执行顺序。

Prompt Policy 在 Host 路由后的直接用户提示进入 canonical message、durable queue 或 Run 前返回 `allow | deny`。它不能改写提示；
timeout、异常和非法返回都会 fail-closed。User plugin 必须声明 `sessions.prompt.inspect`，因为回调会收到原始提示以及
附件的 resource id、media type、size，不会收到附件正文或来源路径。完整示例见
[`inprocess-observer`](../../examples/plugins/inprocess-observer)和
[`inprocess-prompt-policy`](../../examples/plugins/inprocess-prompt-policy)。

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
      "usage": "[on|off|status]",
      "argument_options": [
        {"value": "on", "description": "Enter this mode."},
        {"value": "off", "description": "Leave this mode."},
        {"value": "status", "description": "Show the current mode."}
      ],
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
声明 `requires_session: true`。Host 会在 handler 执行前保证 `call.session_id` 非空；TUI 在第一条输入就是
这类命令时，会用当前模型、审批与交互策略懒创建 Session 后再执行。默认值为 `false`，查询全局状态等无 Session
命令仍可在 `call.session_id=None` 下运行，也不会仅因执行命令而产生空 Session。SDK handler 不重复声明该字段，
Manifest 是 Host 路由前置条件的唯一来源。

参数型 Command 应在 Manifest 用 `argument_options` 声明可选择的首个参数，而不是要求 Host 解析面向人的 `usage`
字符串。每个 `value` 是一个无空白 token，`description` 说明行为；若该选项后仍可跟参数，用该项的 `usage` 描述剩余
语法。CLI 可以继续只显示整体 `usage`，TUI 则会用结构化选项提供过滤、上下键选择和输入联动。自由文本参数不需要
伪装成枚举项，保留在 Command 的整体 `usage` 中即可。

同一 claim domain 的排他 controller 会在 activate 前得到确定冲突；插件不能用 `replace` 抢占其他 owner。为用户提供
友好的 `off` 命令，但它仍应返回 release transition。Host 的 `/control status` 与 `/control release ... [--force]`
不依赖具体插件命令，可在 owner 故障时撤销 gate。`active_when` 由 Host 按模型轮次快照执行，回调不应再次用私有
`phase` 决定是否应用权限策略。需要常驻模型工具列表的进入、查询和退出工具不要声明 `active_when`；resumable 工具若
产生应随 controller 释放而取消的等待交互，应改用独立的 `interaction_controller` 声明其生命周期归属。工具可见性与
pending interaction 归属是两个独立契约。

`SessionControllerCall` 在 activate/deactivate 两条路径都提供 `session_kind`（`primary | subagent`）；owner 应依据该
Host 事实决定自己是否支持该类 Session，不能从 session id 或目录推断。release transition 的 `payload` 会原样传给
deactivate，与 acquire/activate 对称；需要提交 owner 私有终态时，应把终态数据放在 release payload 中，由
deactivate 暂存变更，与控制关系一起提交。replace 在同一草稿中先 deactivate 旧关系，再 activate 新关系；
后者看到前者的暂存结果。回调期间不发布半完成模式，也不自行保存 Session。

deactivate 调用的 `replacement_controller_id` 明确给出本次 replace 的目标 canonical controller id；
普通 release 时为 None。需要区分自身切换与退出时据此决定业务草稿如何处理，不解析 reason 文本，
不保存 activation 副本推断执行顺序。替换仍限定在同一 owner 内；目标 controller 的 payload 由目标解释。
SDK 和 worker 使用同一字段。控制器和 Continuation 不再输出 owner/state revision，提交冲突由原生
Harness 版本、history_epoch 和消息头前置条件处理。

绑定 controller 的 Continuation Policy 会收到所有 terminal `RunOutcome`。非 `completed` Run 只能返回
`pause | fail`，不能自动 `continue`；若需结束自身控制权，可在 `ContinuationDecision.session_control_transitions`
返回一个 release local intent。Host 只允许它释放同 plugin、且被该 Run checkpoint 捕获的精确 activation，并先持久化
完成策略与嵌套 Controller 准备并保存 settlement plan，再一起提交状态、queue 与 checkpoint resolve；恢复不再调用
策略或 Controller。owner policy 不可用、超时或异常时，Host 仅 force-release
该 policy 绑定的精确 activation，并明确记录 owner 私有清理未确认；多个非 ignore 决策仍视为冲突并暂停。

### 调用内会话状态

Python SDK 入口为 `ouro.sdk.plugins.v3`，当前 Host 的 Plugin API 为 `3.1.0`，worker 使用 protocol `3.0`。
依赖通用配置契约的插件在 `engines.plugin_api` 声明 `>=3.1.0,<4.0.0`。
PluginContext 不提供会话状态写入器。可修改状态的回调使用本次调用的 owner 视图：

```python
def handler(call):
    state = call.session_state
    before = state.read()  # None 或 PluginSessionState；返回独立的值
    state.replace(
        state_schema_version=1,
        payload={"items": list(call.arguments["items"])},
        branch_policy="reset",
    )
    return ToolOutput(content="Updated")
```

`replace` / `delete` 只提出变化，工具声明 `effect="state_changing"`。框架在结果被接受时与工具消息共同提交。
同一 owner 的并行变化不能相互覆盖；只能单独调用的状态工具声明 `batch_behavior="exclusive"`。
只读工具、Context、Observer、Prompt/Tool/Turn Policy、Compaction 只能 read。无目标 Session 时视图为 None。
视图在回调返回、异常或取消后关闭，不保存到插件实例或后台任务；不要自己加持久化锁或写 Session 文件。

`state.controls()` 返回当前 owner 已激活的控制关系（本地 controller_id、activation_id、usage_scope 和 data），所有调用均可
只读访问，不应把这些事实再存入 owner JSON。工具请求等待时，即使没有修改 owner 状态，也检查原调用的状态基准。

控制器通过 `SessionControllerOutput.usage_scope` 声明业务用量标识。`state.run_controls()` 返回当前 Run
请求开始时的控制绑定，`state.usage_tokens()` 返回本 owner 各标识对应的原生累计消耗。不存在账本能力时返回
`None`，已知没有消耗时返回空映射。框架按 Provider 请求身份记账和去重；同一业务标识撤销再恢复后保留真实消耗，
插件不把累计 usage、运行绑定或 activation 再写入可撤销业务状态。
运行绑定来自执行调用方实际持有的请求快照。命令和事件观察没有请求绑定，不通过事件中的 run_id 反查并猜测所属请求。

worker 随调用接收 `params.session_state` 中的 `writable`、`value` 和只读 `controls`、`run_controls`、
`usage_tokens`；控制器结果使用可选 `usage_scope`，状态结果返回替换值，
省略表示不变，null 表示删除。
参见 [worker 示例](../../examples/plugins/worker-tool/worker.py)；宿主内置工具使用相同事务语义的实现可见
[TodoList](../../ouro/tools/builtin_todolist.py)。
状态与消息共同提交、失败处理及撤销语义见
[Harness 规格](../specs/session-undo.md)。撤销由框架恢复不透明状态版本，
插件不提供 undo 钩子。计划等文件写入不随会话回滚；业务 payload 不保存可复活旧执行的任务、锁或审批许可。
分支 `copy/reset/reject` 作用于保留消息引用的所有状态版本，按真实业务语义声明，不自行清理历史快照。

## 5. Catalog、owner 与 durable contract

每轮模型决策读取一个当前不可变 `ExtensionCatalogLease`。插件更新先完整构建候选 generation 与 Catalog，再切换 current
pointer；已发出的模型请求及其调用保持原绑定，等待时释放环境租约。Background Service 不属于 Run Catalog，
其实例由激活 generation 管理。

在 `contributes.background_services` 中以 `{"id": "sync", "scope": "home"}` 声明 home 单例服务；省略 scope
表示每个 Host 各自运行。home 服务跟随现有 TaskScheduler owner，只有当选 Host 调用 SDK contribution 的 start，
所有权交接前等待 stop。仅打开插件管理服务不会参与该选举。stop 应幂等，并等待服务任务及外部资源真正退出；
停止失败会保留所有权，不能让另一个 Host 同时启动替代实例。

worker 对应实现 `background.start` / `background.stop`，从 `params.contribution_id` 识别服务并返回 `{}`。
不要在 `lifecycle.start` 中启动这些后台工作；该入口处于候选阶段，也会在没有 home 所有权的 Host 中执行。

每个 generation 拥有 instance、worker、MCP、task、module、gate 和 contribution。命令和 Observer 只绑定所用
贡献及其 owner 的依赖，不持有完整 Catalog。正常升级允许已绑定的调用使用旧代，相关租约释放后回收旧 owner。

disable 关闭该插件所有代次的 gate 并取消正在执行的回调；实际调用及资源清理完成才呈现 `disabled`。
等待超过本 Host 的停止预算时呈现 `stopping`，清理失败时呈现 `failed`；再次 disable 可重试失败清理。
旧 Background Service 停止失败时，不启动新代服务。停止中的代码或私有数据不能被卸载、清除。
同步 Python 回调的线程不能强杀；取消后仍等待线程退出并丢弃迟到结果，插件应主动遵守停止约定。

Run、Tool/Approval 和 Continuation 持久化稳定 `execution_contract_hash`，覆盖 package、immutable configuration
revision、传递 dependency lock、Plugin API/worker protocol 和 contribution contract。随机 `generation_id` 只用于
当前进程 owner 与诊断。重启后使用当前已激活的 process-local generation 检查具体调用；同名但不同 contract 的 Tool
不能接管 pending invocation。

对 builtin，用户包摘要由 definition 在装配时冻结的 builtin contract hash 取代；该摘要同时覆盖规范化 Manifest 与实现
目录内容，因此代码变化不会沿用旧 release identity。

### 动态工具组

可执行插件可在 Manifest 顶层声明 `tool_groups`，每组声明固定执行策略；工具名称、说明和 JSON Schema
在发布时提供。组内工具必须符合声明的 `permission_mode`、`permission_control`、`effect`、`batch_behavior`、
`resumable`、`supports_unattended` 与 Controller 绑定。具体工具名称无需写入 Manifest。

```json
{
  "contributes": {
    "tool_groups": [
      {"id": "sources", "permission_mode": "auto", "effect": "read_only"}
    ]
  }
}
```

```python
from ouro.sdk.plugins.v3 import ToolContribution, ToolGroupContribution

group = ToolGroupContribution(
    id="sources",
    revision="source-api-2",
    tools=(ToolContribution(
        id="search",
        exposed_name="sources__search",
        description="Search the discovered source.",
        parameters_schema={"type": "object", "properties": {}},
        permission_mode="auto",
        effect="read_only",
        handler=search,
    ),),
)
registration = await context.tool_groups.publish(group)
await registration.close()
```

`prepare()` 可通过 `ContributionSet.tool_groups` 提交初始工具组；候选阶段不能调用 `publish()`。
运行中每次发布替换整组，空组移除该组全部工具。Host 校验所有工具及目录冲突后一次性发布，失败保留原组。
未声明工具组的插件没有该端口。发布只接受当前 active generation，拒绝只读或已结束的调用作用域。
生命周期变更正在进行时，发布立即返回错误；调用方可在自己的工作流程中重新发起，不应等待停止自身的变更。

`revision` 是实现语义的稳定标识；即使 schema 不变，处理行为变化也应改变 revision。工具规范身份为
`plugin_id/tools/group_id/tool_id`。工具契约包含 revision 与具体定义，generation 的执行契约不随组发布改变。
已发出的模型请求保留原组 schema 和处理器；下一轮读取当前组。进入审批或其他持久等待后释放执行租约，
重启恢复必须匹配原工具契约，不能用同名新工具替代。组发布不持久化 Python 闭包，初始发现应提供可重复的契约标识。

关闭句柄幂等，只撤销该次发布；旧句柄不能删除替代它的新组。撤销立即影响后续决策，已接受调用保留原修订至
租约释放。禁用插件则关闭所有修订的调用入口。句柄也可在 `stop()` 中关闭，Host 负责最终回收所属注册。

Python worker 使用 `ouro.sdk.plugins.worker_tool_groups.WorkerToolGroups(peer)`。该 helper 安装
`tool_groups.call`、`tool_groups.resume` 和 `tool_groups.release` 路由；在 `contributions.prepare` 结果中
返回 `{"tool_groups": groups.prepare(initial_groups)}`，运行中调用同样的 `await groups.publish(group)`。
工具使用单参数 SDK `ToolCall`／`ToolResumeCall` handler；workspace 位于 call 中，原生能力通过对应 worker
端口访问。Session 状态视图保持调用作用域，结果由 Host 校验并与消息提交，取消后的迟到状态不可写入。

Host 按发布身份调用 worker 处理器，租约排空后发送 `tool_groups.release` 通知。通知在协议读取循环中移除引用，
不占用业务并发槽。发布回执取消时，SDK 按预先建立的身份请求撤销；不能因为没有收到成功回执就删除可能已被
模型决策持有的处理器。工具及恢复 RPC 跟随 Run／generation 生命周期，不受普通传输默认超时截断。
完整示例见 [dynamic-records](../../examples/plugins/dynamic-records/README.md)。

## 6. 权限、配置与数据

OURO 保留个人智能体所需的基础边界：

- 安装默认禁用，显式 enable/select/reload/rollback 只接受目标 immutable digest；
- 封闭 permission id 与真实 Host 能力检查；
- credential 保存 `env:VARIABLE` 引用或 `{"type":"inline","value":"key"}` 直接密钥绑定；直接密钥持久化在受保护的本地 state 中，不进入 manifest、普通查询或 worker initialize JSON；
- user plugin Tool 始终经过 Host schema、审批、Tool Policy 和 `ToolExecutor`；
- package、workspace path 与 owner 生命周期校验。

`python-inprocess` 是 full-trust，能够直接调用 Python/OS API；OURO 不把它描述为沙箱。worker 提供进程和协议隔离，
同样不承诺系统调用级沙箱。

配置是内容寻址的 immutable revision。active 插件 configure 使用候选 generation，失败不切换 active revision。
`prepare/start` 期间 Host data API 只读；写入、删除会明确失败。将需要持久写入的自主工作放入 Background Service，
框架会在发布后启动它。通过 task group 登记的任务同样延迟到发布后运行。候选失败时框架清理其已获取资源；
直接绕过 Host API 的 in-process OS 写入不受该边界约束。

### 声明和消费配置

在 Manifest 的 `configuration` 中声明包内 `schema` 路径、正整数业务 `version` 和可选 credential 描述。
普通值使用自包含 JSON Schema Draft 2020-12，根类型为 object；可以自由组合嵌套、动态映射、数组、元组、
联合、条件、依赖、引用和递归结构。不要为 CLI/TUI 编写另一份字段清单或让 Host 导入插件业务模型。

`configuration.credentials` 的 key 必须已出现在 `credentials.use.scopes` 中；每个描述可以包含 `title`、
`description` 和 `required`。仅凭据插件可以省略普通 Schema，普通值保持 `{}`。凭据值通过
`context.credentials.resolve(slot)` 获取。每个槽位都支持由用户选择环境变量引用或直接保存密钥，开发者无需
为同一个凭据声明两个槽位，也不在普通 Schema 中声明 API Key 字符串字段。

例如，用户可为同一个 `service_api` 槽位提交以下任一绑定：

```json
{"credential_refs":{"service_api":"env:SERVICE_API_KEY"}}
```

```json
{"credential_refs":{"service_api":{"type":"inline","value":"actual-key"}}}
```

直接密钥必须非空白，框架保留完整值；切换来源替换槽位绑定，省略槽位解除绑定。SDK 的读取方式保持一致：

```python
api_key = context.credentials.resolve("service_api")
```

普通查询、预检 JSON 和日志对密钥脱敏；显式 `plugin configuration --export` 输出完整编辑草稿，包含保存的
直接密钥，并将文件权限限制为当前用户。环境引用在导出时不解析环境变量。

in-process 插件从 `context.configuration` 读取固定的运行实例快照，其中对象为递归只读 Mapping，数组为
tuple。业务模型可以接受这些只读集合或显式转换；不要修改它们，也不要读取框架内部 state 文件。
worker 在 initialize 中接收独立的普通 JSON 对象，秘密沿既有临时环境变量通道传递。

默认值由框架在安装和接受完整候选时确定性填充；组合或条件分支的默认值是提示，不自动选择分支。
已保存修订在重启和持久调用恢复时不重新填默认值。`format` 是注解；跨字段计算、文件可写性和远程服务
权限等业务检查属于候选生命周期或实际操作边界，静态发现与预检不执行这些逻辑。

插件字段应说明单位和路径基准，私有路径以插件 data 根为基准，不依赖 Host 工作目录。改变路径不自动迁移
业务数据。调度默认参数可以是配置；已有持久 Task 的修改仍通过原生调度接口完成，不由配置提交隐式重写。

Host 使用 `PluginService.configuration()` 获取定义与固定基线，用视图的 `candidate()` 建立完整草稿，
调用 `preview_configuration()` 做静态预检，再调用 `await configure(candidate)` 应用。草稿应在开始编辑时
创建，不在提交前重新读取基线覆盖它；凭据和普通值一起全量提交。精确字段、生效及失败规则见
[配置规格](../specs/plugin-configuration.md)，可直接验证的完整声明见
[Configuration Playground](../../examples/plugins/configuration-playground/README.md)。

### 具名服务与可撤销注册

提供者在 Manifest 的 `exports` 声明服务 ID、方法输入 JSON Schema、`effect` 和 `registration`，并在
`ContributionSet.services` 提供相同的服务与方法。消费插件在 `dependencies.plugins` 声明提供者及版本范围，
再使用 `context.services.call(plugin_id, service, method, arguments)`；只传 JSON 数据，不读取提供者私有目录。
`read_only` 方法不能通过 SDK 修改私有数据或调用有副作用的服务。

需要让消费者提供过滤器等回调时，提供者声明注册方法，并在方法中先登记清理：

```python
async def register_filter(call):
    call.on_close(lambda: filters.remove(call.handler))
    filters.append(call.handler)
```

消费者使用 `await context.services.register(provider, service, method, handler)` 得到关闭句柄，调用
`await registration.close()` 撤销。回调不是可长期自由使用的 Python 对象；任一插件停止、注册取消或显式关闭都会
撤销其执行入口。清理失败保留归属供再次关闭，不自动重试业务调用。

完整示例是 [`memory-service`](../../examples/plugins/memory-service) 与
[`memory-client`](../../examples/plugins/memory-client)：数据由提供者的 SQLite 事务管理，消费方注册结果过滤器，
两者都在独立 worker 中运行。结果最多返回 20 条，不把完整数据集复制到 Host。

### 定时服务调用

可供 Host 定时执行的方法在服务声明中设置 `schedulable=true`，并请求 `background.run` 权限。
Host 使用原生 Task 的 `plugin` 目标保存插件 ID、服务、方法和参数；注册方法不支持调度。
方法接收的 `ServiceCall.scheduled` 是 `ScheduledInvocation`，包含 `task_id`、`execution_id`、`scheduled_at`。
普通调用时该值为 `None`。返回 JSON 会保存到执行记录，适合返回同步摘要；业务自己的游标和数据仍由插件持有。
worker 的 `services.call` 参数包含同名 `scheduled` 对象，其时间为 ISO 8601；这条调用没有原生 Run 上下文。
任务领取后的实现不会因升级而切换，未知结果不会被框架自动重试。

### 持久计划端口

`context.scheduler` 提供原生计划用例；worker 使用 `ouro.sdk.plugins.worker_scheduling.WorkerScheduler(peer)`，
Host 的 initialize features 包含 `scheduler` 时可用。端口方法为 `create`、`get`、`list`、`update`、
`set_enabled`、`delete`、`executions`、`interrupt`。`context.tasks` 管理代次内协程，持久计划则由原生调度 owner
领取；两者具有不同生命周期。

```python
from datetime import UTC, datetime
from ouro.sdk.plugins.scheduling import ScheduledService, TaskSchedule, TaskSpec

plan = await context.scheduler.create(TaskSpec(
    target=ScheduledService(context.identity.plugin_id, "sync", "run", arguments=None),
    schedule=TaskSchedule("hourly", starts_at=datetime.now(UTC)),
    title="Synchronize records",
))
await context.scheduler.set_enabled(plan.task_id, False, expected_revision=plan.revision)
```

Host 固定创建者身份，端口只返回或控制本插件拥有的任务，服务目标限本插件或直接声明的依赖。写计划要求
`background.run`；使用 `ScheduledAgent(prompt, workspace, ...)` 创建 Agent 任务还需 `sessions.write`。
`ModelReasoning` 表达模型思考配置，日历和目标由原生领域校验；不必构造内部模型。

`update`、`set_enabled`、`delete` 和 `interrupt` 需要最近读取的 `expected_revision`，冲突应重新查询后决策。
只读回调可以查询计划和执行记录，不能写计划。候选尚未发布时不可调用端口；需要启动时建立计划的逻辑应放在
发布后的 Background Service，并自行明确重复启动的业务规则。每次 `create` 都创建新任务；回执丢失时先查询，
不要盲目重发。已停止回调和撤销代次不能借旧引用新开业务。

`ScheduledExecution` 对 Agent 任务返回真实 Session／Run 身份，对插件服务任务返回 JSON 结果，身份字段为
`None`。`interrupt` 持久化请求后返回，执行状态以随后查询为准。owner 停用阻止派发并中断在途工作；计划、历史
和数据保留，已暂停的计划需要显式启用。公开样例 [`scheduled-sync`](../../examples/plugins/scheduled-sync)
通过同一 SDK 管理计划和更新私有记录快照，支持进程内和 worker。

### 进程执行

声明 `process.spawn` 后，可调用 `context.processes.run(ProcessRequest(arguments=(...)), on_output=...)`。
`ProcessRequest` 位于 `ouro.sdk.plugins.processes`。入口接收 argv、cwd、超时和可选环境；不隐式插入 shell。
输出回调接收有序的 `ProcessOutput`，等待回调形成背压；完成结果有界且带截断标记。大输出可在回调中写入
plugin-scoped artifact。调用取消、插件撤销、超时及输出回调失败均触发进程树清理。进程仍拥有执行用户的 OS 权限。

Worker 对应的 `WorkerProcesses` 与 `WorkerServices` 使用同一 Host 能力及取消边界；
[`worker-exec`](../../examples/plugins/worker-exec) 展示受管理的 Python 执行与输出回调。

### 独立模型请求

无 Run 的命令和后台业务可以声明 `models.request`，使用公开模型 DTO：

```python
from ouro.sdk.plugins.models import ModelInput, ModelRequest

result = await context.models.generate(
    ModelRequest(
        messages=(ModelInput("user", "Review this proposal."),),
        model="default",
        timeout_seconds=120,
    ),
    on_output=receive_output,
)
```

`receive_output` 是可选异步回调，按序接收带 sequence、kind（text/reasoning）和 text 的 `ModelOutput`。
回调应及时处理文本；它的完成控制后续输出交付。结果含请求 ID、实际模型路由、文本、思考文本和供应商用量。
`ModelReasoning` 与调度 Agent 共用思考配置契约；输出上限不得超过所选端点配置，输入不得超出上下文预算。
`default` 要求已配置默认端点，也可传 `provider/endpoint`。命令自身的 timeout_seconds 应覆盖模型请求期限。

用量归属插件，存在调用者 Session 时保留该关联；此调用不创建 Session 或 Run。准备阶段、只读回调以及活跃
Run 都不能使用 `generate`。活跃 Run 使用下述 `request` 端口；停用插件会取消其未完成的模型请求。
worker 创建 `models = WorkerModels(peer)`，并把 `"models.output"` 路由注册到 `models.output` 方法。
完整命令与两种运行入口见 [`model-review`](../../examples/plugins/model-review)。

### Run Driver

`PluginContext.models.request`、`.tools` 和 `.agents` 也可供普通插件服务使用。它们复用调用链上已接受的 Run，服务需声明
`sessions.write`，方法 effect 为 `state_changing` 或 `unknown`。模型调用会写入 Run 历史、计数和用量，不能标为
只读方法。嵌套服务继承调用者的 Run、工作区和只读约束；generation 不缓存某次调用的 Run。

这些端口要求当前调用拥有活跃 Run，不能从无 Run 的命令、准备阶段或后台代码直接调用。命令可返回带 Driver 选择的
`SessionInput` 进入原生接受链。一个 Run 的原生操作必须顺序 await；工具或上下文回调不能递归开启同一 Run 的模型
请求。worker 使用 `WorkerRunPorts(peer)` 获取同等端口，反向请求沿当前服务调用继承 Host 作用域，不接受 Run ID。
[`review-service`](../../examples/plugins/review-service) 展示独立服务中的模型审阅、工具执行及等待 checkpoint。

`contributes.run_drivers` 声明 Driver id，并申请 `sessions.write` 权限。插件通过
`RunDriverContribution(id, handler)` 注册普通 async 函数，参数为 `RunDriverContext` 和可选 `DriverResume`。

算法可以 `await context.inputs.consume()` 在安全决策边界接收 steering。返回 `RunInput` 包含 `message_id`、
`role`、JSON 内容块 `content`、`scaffold`、`semantic_tag`、来源 `metadata` 和 `resource_refs`；`text` 提取其中的文本块。
这些是已经提交的消息副本，修改它们不会改变 Host 历史。模型请求前会自动交付待处理 steering；若算法刚显式读取过，
紧接的模型请求使用该次输入集合。纯代码／服务算法可再次显式读取以开始下一次决策，不依赖固定模型循环。
存在未处理的模型工具响应时须先执行并结算该批次；不能把输入插入工具请求与结果之间。
队列、策略和恢复语义以[运行时规格](../specs/runtime.md#run-driver-与原生端口)为准。
函数返回 `DriverFinished(text)` 或 `DriverSuspended()`，不直接修改 Run、Store 或执行所有权。
直接采用当前模型响应作为最终答复时，返回 `DriverFinished(response.text, response_id=response.response_id)`。
Host 校验真实响应句柄与文本，沿用已提交的 assistant 消息和模型元数据；省略句柄会保存 Driver 自己的最终输出。
句柄不能用于改写模型输出，不能跨调用恢复，也不能在后续模型请求或程序化工具调用后复用。

`context.models.request(prompt=..., tools=...)` 执行一次原生模型请求。prompt 是本次上下文的临时 Driver 指令，
`tools=None` 使用当前可用工具，空 tuple 禁用工具，指定名字只选取当前目录的子集。上下文保存、预算、模型路由、
计数、事件和用量仍由 Host 处理；已完成的无工具模型响应进入 canonical history。

`context.tools.execute_model_response(response.response_id, checkpoint=...)` 只能消费当前调用中尚未消费的真实模型
工具响应。公开 DTO 的修改不改变 Host 持有的调用；响应句柄不能跨 Run 或跨恢复调用复用。工具结果包含结构化输出、
媒体内容和资源引用。工具权限、审批与交互沿用原生协调器。

`context.tools.execute((RunToolCall(name, arguments), ...), checkpoint=...)` 发起程序化工具批次。
Host 分配调用 ID、按当前决策目录绑定执行契约，并记录 Driver 来源。调用不会伪造模型工具请求或增加模型计数；
结果由 Provider 按来源投影并应用媒体支持策略。存在未解决的模型工具响应时必须先消费该响应。

`context.agents.spawn((AgentTask(agent_id, task, description), ...), checkpoint=...)` 使用原生 Agent
目录、权限与共享预算。`AgentTask` 支持 `nickname`、`execution_mode`（默认 `sync`）和 `resource_refs`。
`context.agents.send(delegation_id, message, mode=..., checkpoint=...)` 与
`context.agents.interrupt(delegation_id, reason=..., checkpoint=...)` 使用同一工具执行边界。
这些端口返回 `ToolBatchResult`，子任务身份和结果沿用原生结构化输出。同步子任务等待会释放父 Run 的执行所有权。
批次并发由 Host 工具配置决定，子任务并发由原生调度器决定，插件不能越过这些限制。

调用可能返回 `suspended=True`，此时立即返回 `DriverSuspended()`。恢复参数包含保存的 JSON checkpoint 和已结算
工具结果。checkpoint 限制为 64 KiB UTF-8 JSON，存放小状态和资源引用；Host 在重新进入插件代码前消费它。
消费后的中断不能自动重放插件副作用。Run 绑定所属插件和传递依赖的发布、配置修订及执行契约。升级后恢复按需装载
原契约，不改变当前选版；该实现只在执行期间持有资源，不发布全局贡献或启动后台服务。已禁用的插件／依赖、缺失的
包或配置修订、以及无法重建的契约会阻止交付 checkpoint，保留诊断与未消费结果。尚有可恢复 Driver 引用的包及依赖包
受清理保留规则约束。恢复读取当前私有数据；插件更新必须考虑未完成调用所需的数据格式。

原生调用必须逐个 await，调用上下文在 Driver 返回、等待或撤销后失效。模型／工具调用使用所属 Run 的权限与预算，
插件服务和进程调用继承该 Run 的工作区约束。用户中断会等待原生端口结算取消结果，再结束 Driver 的剩余代码。
示例 [`review-driver`](../../examples/plugins/review-driver)
包含同一个算法的进程内入口与 worker 入口，提供 `/review <request>` 命令。命令返回
`SessionInput(prompt=call.argument, driver_id="dev.ouro.examples.review_driver/run_drivers/review")`，
申请 `sessions.write` 和 `background.run`，由 Host 接受输入后启动原生 Run。忙碌 Session 将输入排队，接受时即
绑定 Driver 的发布、配置及依赖；升级和重启不会改变该选择。单个原子 batch 的成员必须选择相同 Driver，
`steer` 不接受 Driver 选择。Host 也可直接调用 `RuntimeService.run(..., driver_id=...)`。
[`delegating-driver`](../../examples/plugins/delegating-driver) 组合两个原生子任务、模型审阅和条件工具执行，
支持子任务等待重启及后续工具审批。

## 7. Worker 最小协议

worker 从 stdin 逐行读取 JSON-RPC 2.0 request，只向 stdout 写协议 JSON；诊断写 stderr 或发送
`diagnostic.emit` notification。核心方法是：

- `initialize`：确认 manifest 的 `protocol_version`；
- `contributions.prepare`：返回 manifest 已声明 contribution 的协议描述；
- `lifecycle.start` / `lifecycle.stop` / `shutdown`；
- contribution 调用，例如 `tool.call`；
- resumable 工具使用 `tool.resume`；`call.resolution` 传输 `answer` 或 `unattended` 判别对象；
- Observer 使用 `observer.observe`，Prompt Policy 使用 `prompt_policy.call`；
- `cancel`：取消超时或被 Host 取消的 request。

Python worker 可使用 `ouro.sdk.plugins.worker.WorkerPeer`。它独立分派请求和响应，允许 worker → Host →
worker 及跨多个 worker 的嵌套调用。`WorkerServices`、`WorkerProcesses`、`WorkerEnvironment` 分别提供插件服务、
进程与环境代理；`WorkerRunDrivers` 分派 `drivers.call` 并代理调用期间的原生模型／工具端口；需要将代理的 handler 接到示例中列出的明确路由。双方都限制消息大小与并发请求数，取消通过
请求 ID 传播。`timeout_seconds=0` 表示由所属调用生命周期控制取消，不设置 RPC 时间上限；Driver 及其原生端口使用该模式，模型与工具仍执行各自的预算／超时。业务调用携带父调用 ID，注册清理可以在原调用结束后通过所属 worker 的注册 ID 执行。

stdout 污染、未知消息、过大响应、声明不一致和错误 result shape 都会使协议调用失败。完整最小实现见
[`examples/plugins/worker-tool`](../../examples/plugins/worker-tool)。

worker tool 以 `{"input_request": <任意 JSON 值>}` 返回同一种挂起请求；该字段不能与 `content`、
`structured`、`resource_refs` 或 `is_error` 同时出现。
声明 `resumable=true` 的 worker tool 通过 `tool.resume` 接收原请求和 `resolution`；如需在 AFK 下自定义业务迁移，
还必须在 manifest 中声明 `supports_unattended=true`。Worker protocol `3.0` 只接受 `resolution` 判别联合；出现
`answer` 字段时协议校验失败。
协议 `3.0` 在 `session_controller.call` 输入中提供 `session_kind`，允许 `continuation.call` 输出
`session_control_transitions`，与 in-process Plugin API `3.1.0` 使用相同状态及结算语义。
`context.call` 的 `fragments` 既接受字符串，也接受带 `text`、`persistence`、`target`、`role`、`scaffold` 和
可选 `semantic_tag` 的对象。Worker 应传递自己选择的字段；Host 不从 target、persistence 或 provenance 反推
消息属性。

## 8. 社区资源导入前审查

```bash
uv run --frozen ouro plugin import-report ./community-package --json
```

报告逐项给出：

- `supported`：可直接作为 Agent Skill 等资源表达；
- `converted`：语义可表达，但必须转换为 OURO descriptor/permission；
- `ignored`：源 Host 专属且不进入 OURO；
- `incompatible`：需要显式 worker/MCP port，不能直接运行。

每个可迁移项给出 `ouro_target`，报告同时列出 outcome count、lost semantics、安全差异和所需权限。该命令只读文件，
不执行社区 Hook、JavaScript runtime、安装脚本或 MCP server。

## 9. 模型协议贡献

Manifest 的 `contributes.providers` 声明本地 `id`，`ContributionSet.providers` 返回对应的
`ouro.sdk.plugins.providers.ProviderContribution(id, stream, check)`。模型配置选择
`<plugin_id>/providers/<id>`。供应商实例与 Endpoint 身份仍由框架管理。

`stream(request, output)` 接收 `ProviderRequest` 并逐次 `await output(ProviderEvent(...))`。
正文、推理、工具参数片段、continuation、重试通知和终态分别使用相应事件；`done` 携带规范化用量及完成原因。
handler 必须等待输出、取消后结算自己的请求资源，并在返回后停止输出。`check(request)` 返回 `ProviderCheck`。
实例级 HTTP client 等共享资源由插件 `start/stop` 管理；框架仅在请求期间持有代次租约。

Provider 可以使用插件已授权的服务与进程端口实现协议。SDK 请求只有本次模型可见事实，不含 Host 内部对象；
Provider 保留调用来源及父级约束，不继承当前 Driver 的模型、工具或 Agent 控制端口；活跃 Run 中也不能
通过独立生成绕过预算。协议实现须校验自身支持的连接选项、内容编码和 reasoning 语义。
公共类型定义见 `ouro/sdk/plugins/providers.py`，独立生成端口见 `ouro/sdk/plugins/models.py`。

worker 使用 `WorkerProviders(peer)` 注册异步处理器，并在 `contributions.prepare` 返回注册得到的 Provider ID。
与其他贡献共用一个 `WorkerPeer`；反向输出 RPC 逐项等待，根并发上限为一也能完成嵌套模型调用。

## 10. 可运行示例

- [`faux-provider`](../../examples/plugins/faux-provider)：纯 SDK Provider 与普通模型路由，含两种运行入口。

- [`declarative-skill`](../../examples/plugins/declarative-skill)：静态 Agent Skill；
- [`inprocess-command`](../../examples/plugins/inprocess-command)：Python Command；
- [`inprocess-observer`](../../examples/plugins/inprocess-observer)：非阻塞类型化 Observer；
- [`inprocess-prompt-policy`](../../examples/plugins/inprocess-prompt-policy)：持久化前拒绝提示的 Prompt Policy；
- 内置 Plan 插件位于 `ouro/builtin_plugins/plan`，展示了产品内置插件如何复用与用户插件相同的 Context/Tool Policy、Generation 与可恢复工具契约；
- 内置 Agent Context 插件位于 `ouro/builtin_plugins/agent_context`，展示了分层 `AGENTS.md`、请求级 system
  prompt fragment、运行环境边界和默认启用 builtin 的组合方式；
- [`worker-tool`](../../examples/plugins/worker-tool)：stdio worker Tool；
- [`review-driver`](../../examples/plugins/review-driver)：原生模型调用、工具批次、审批等待与 Driver checkpoint。
- [`delegating-driver`](../../examples/plugins/delegating-driver)：Agent 端口、并发子任务、等待恢复及条件工具执行。

`examples/plugins` package 由测试执行真实 validate/install/enable 生命周期；内置 Plan 则由 builtin
enable/disable、Generation、工具恢复与跨进程 Run 集成测试覆盖，避免文档示例与实现漂移。
