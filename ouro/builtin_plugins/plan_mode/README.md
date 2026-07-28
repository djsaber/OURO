## 提示词

计划模式下，在每一轮用户消息前插入脚手架消息，提示词如下：

```md
Plan mode is active. Read-only tools are available without additional plan
mode restrictions. To create or change the plan, you MUST use OURO's builtin
Write or Edit tool and target the exact plan file shown below. Those Write/Edit
calls are denied for every other path. Every other state-changing or
unknown-effect tool is denied regardless of target. Do not use shell, MCP,
plugin, or other tools to make changes, even if they could write the plan file.
This supersedes any other instructions you have received. Call ExitPlanMode
before performing implementation work or making any other change.

Workflow:

1. Understand — explore the relevant context using read-only tools
   (searching, reading, querying, inspecting — anything without side effects).
2. Design — converge on the best approach; consider trade-offs but aim for a
   single recommendation.
3. Verify — re-check key facts, files, or sources to confirm your understanding.
4. Write Plan — use only OURO's builtin Write or Edit tool to record the plan in
   the designated plan file; no other file may be modified.
5. Exit — call ExitPlanMode for user approval.

Handling multiple approaches: at most 2–3 meaningfully different implementation
approaches; if one is clearly superior, just propose that one. When multiple
approaches are included, pass them as `options` to ExitPlanMode. Options are
implementation approaches, not approval actions. Never create options for
approving, revising, or rejecting the plan; the Host provides those actions
separately.

AskUser is for clarifying missing requirements or preferences. Never
ask about plan approval via text or AskUser. Do NOT reference
"the plan" in AskUser.

Plan file: {PLAN_FILE_PATH}
```

## 工具

通过两个工具表达阶段转化语义，EnterPlanMode 进入计划模式，ExitPlanMode 退出计划模式。

### EnterPlanMode

- 工具定义

```json
{
  "description": "Use this tool proactively when you're about to start a non-trivial implementation task.",
  "parameters": {
    "type": "object",
    "properties": {},
    "additionalProperties": false
  }
}
```

- 工具调用成功

```md
Plan mode is now active. Your workflow:

Plan file: {PLAN_FILE_PATH}

1. Use any read-only tools to investigate the codebase.
2. Design a concrete, step-by-step plan.
3. Write the plan with OURO's builtin Write or Edit tool, targeting exactly the
   plan file above.
4. When the plan is ready, call ExitPlanMode for user approval.

All other state-changing or unknown-effect tool calls are denied while plan
mode is active. Approval cannot override this restriction.
```

- 工具调用失败

```md
Plan mode is already active. Use ExitPlanMode when the plan is ready.
```

### ExitPlanMode

- 工具定义

```json
{
  "description": "Use this tool when you are in plan mode and have finished writing your plan to the plan file and are ready for user approval.",
  "parameters": {
    "type": "object",
    "properties": {
      "options": {
        "type": "array",
        "maxItems": 3,
        "minItems": 1,
        "description": "Optional mutually exclusive implementation approaches from the plan. Never create options for approving, revising, or rejecting the plan; the Host provides those actions separately.",
        "items": {
          "type": "object",
          "properties": {
            "label": {
              "type": "string",
              "minLength": 1,
              "maxLength": 80
            },
            "description": {
              "type": "string",
              "default": ""
            }
          },
          "required": ["label"]
        }
      }
    },
    "additionalProperties": false
  }
}
```

- 工具调用成功

读取计划文件内容，组合输出<退出计划提示>+<计划文件位置>+<计划内容>，正常退出计划模式，工具调用成功

```md
Exited plan mode. Plan mode deactivated. All tools are now available.
Plan saved to: {PLAN_FILE_PATH}

{APPROVED_PLAN}
```

- 工具调用失败

（1）计划文件不存在或读取失败：
```md
No plan file found. Write your plan to {PLAN_FILE_PATH} first, then call ExitPlanMode.
```
（2）计划空内容：
```md
Plan file exists but is empty. Write a non-empty plan to {PLAN_FILE_PATH}, then call ExitPlanMode.
```
（3）用户拒绝计划
```
Plan rejected by user. Plan mode remains active.
```

## 核心流程

1. 进入计划模式：
  - LLM 自主决策，调用 EnterPlanMode 工具进入；
  - 用户指定开启时，`/plan` 只返回 Session Controller acquire transition，不创建 Run、不提交模型输入；Host 仲裁成功
    后调用插件 activate lifecycle，用户随后单独发送规划请求，Context Provider 在该模型轮注入脚手架消息；
  - `/plan off` 返回 release transition；在等待计划确认时关闭会取消绑定该 controller 的 pending interaction，且不
    删除计划 artifact、不批准计划、不创建实施 Run。
2. 创建计划并保存：需要上下文时，LLM 可调用任意声明为 `read_only` 的工具查询或搜索；保存计划时只能调用 OURO
   内置 Write/Edit 且路径必须精确等于当前计划文件；
3. 退出计划模式：LLM 自主决策，调用 ExitPlanMode 退出计划：
  1. 未创建 / 空计划时，ExitPlanMode 调用失败，返回错误结果，LLM 重新决策；
  2. 已创建，触发用户批准反馈，LLM 基于反馈自主决策后续动作（执行任务/chong）

## 核心设计

1. 正常对话时，EnterPlanMode 调用决策交给 LLM；
2. 插件启用期间，`EnterPlanMode` 和 `ExitPlanMode` 始终出现在模型工具列表中，工具集合不随模式切换而变化；
   `ExitPlanMode` 在非计划模式下由自身状态契约返回明确错误，并用独立的 `interaction_controller=mode` 声明等待审批
   归属。指定计划模式时（`/plan`），命令只请求 Host 激活 controller；下一条用户消息触发模型 Run 时，绑定
   `active_when=mode` 的 Context Provider 才注入脚手架消息；
3. 处于计划模式时，每轮用户消息前面都要注入脚手架消息，加强对 LLM 引导；
4. 同一会话内多次开启计划模式，每次计划模式的 PLAN_FILE_PATH 指向不同文件，可用时间后缀方式；
5. ExitPlanMode 调用决策交给 LLM；
6. ExitPlanMode 工具只有一个可选参数 options，用于在计划包含多个可选实施方案时，让用户在批准时选择执行哪一个；
   options 不能承载 accept/revise/reject，这三个审批动作由交互请求中的稳定 action code 独立表达；
7. EnterPlanMode 和 ExitPlanMode 工具自身的调用不走批准策略；
8. 计划模式不控制 Run 的继续或完成，不注册 Turn Policy；EnterPlanMode 和 ExitPlanMode 通过通用 transition 向
   Host 请求状态迁移，由 LLM 自主决定下一步；
9. active/awaiting_approval 阶段的 Tool Policy 使用硬限制：插件自身 Enter/Exit 与任意 `read_only` 工具放行；内置
   Write/Edit 仅在目标为当前计划文件时放行；其他 `state_changing` 或 `unknown` 工具直接 `deny`。Session
   approval mode 和用户批准不能覆盖 deny；拒绝以 `ToolPermissionDenied` 工具结果反馈，模型循环照常继续。


PLAN_FILE_PATH: {OURO_HOME}/artifacts/dev.ouro.plan_mode/sessions/{session_id}/plans/plan_xxx.md

> Plan 不改变也不控制智能体主循环。Host 只认识通用 Session Controller、claim、transition 和模型轮次 activation
> snapshot；Plan 私有状态负责计划文件与审批业务。只有脚手架和 Tool Policy 通过 `active_when=mode` 启停；
> EnterPlanMode 和 ExitPlanMode 在插件启用期间始终可见，由工具自身校验状态。Exit 的 pending interaction 通过独立的
> `interaction_controller=mode` 绑定 controller 生命周期。Host 不包含 Plan/Goal 冲突特例。

## 计划确认交互契约

ExitPlanMode 通过 resumable `ToolInputRequest` 进入 `waiting_user`，而不是进入工具权限系统的
`waiting_approval`。Core 和 Runtime 只持久化、转发请求与回答 JSON，不解释计划语义。CLI 仅在等待调用同时满足
以下 provenance 时启用官方计划确认界面：

- `plugin_class=builtin`；
- `plugin_id=dev.ouro.plan_mode`；
- canonical tool id 为 `dev.ouro.plan_mode/tools/exit`；
- `ToolInputRequest.data.type=plan_confirmation`。

CLI 展示计划和候选实施方案，通过编号选择保留请求中的原始 option label，并构造以下回答：

```json
{"action": "accept"}
{"action": "accept", "option": "所选实施方案的原始 label"}
{"action": "revise", "feedback": "需要补充回滚步骤"}
{"action": "reject"}
```

没有 options 时 accept 不携带 option；一个 option 时 CLI 展示后自动绑定；多个 options 时要求用户明确选择，不能
默认选择第一项。`/abort` 仍终止 Run，不作为插件回答。未知或非官方来源的结构化请求继续使用通用 JSON/文本输入
fallback，计划 UI 不是新的插件 contribution 或 Core 特例。

AFK 下 `ExitPlanMode` 不进入 `waiting_user`。该工具显式声明 `supports_unattended`，收到
`UnattendedResolution(resolver_id="ouro.afk")` 后把 `awaiting_approval` 原子迁移为 `inactive`，并在结果中记录
`resolved_by=host_policy`。无 options 时直接继续；只有一个 option 时记录该选择；多个 options 时不擅自选择第一项，
由 Agent 根据计划自主决定后续实现。此路径不使用 reject 作为 fallback，也不声称“用户已批准”。Plan Tool Policy 的
硬 `deny` 在 AFK 下仍然有效。

## 内置插件实现约束

内置插件 `ouro/builtin_plugins/plan_mode` 复用 Host 的 `Write`/`Edit`，不贡献重复的文件工具。由于 OURO 默认启用
`tool_execution.workspace_only=true`，Host 除 workspace 外显式提供隔离的用户制品根目录；插件的实际路径采用：

```text
{OURO_HOME}/artifacts/dev.ouro.plan_mode/sessions/{session_id}/plans/plan_{timestamp}_{nonce}.md
```

`OURO_HOME` 由统一部署解析决定（默认 `~/.ouro`，并尊重显式 home 或 `OURO_HOME` 覆盖），因此计划文件不依赖
项目目录或进程工作目录。`artifacts/<plugin-id>/` 是用户可见制品，不属于 canonical Session 数据，也不同于
`plugins/data/<plugin-id>/` 的插件私有状态。`ToolExecutionContext` 只把 workspace 与 `OURO_HOME/artifacts` 作为
受控路径根；它不会放开整个用户目录。Plan 插件再通过调用级 Tool Policy 把 Write/Edit 精确收窄到当前计划文件，
因此 Core 只认识通用制品边界，不认识计划文件特权。

Tool Policy 依据 validated invocation 的 canonical tool id、effect 和 path 判定，不按 exposed name、描述或命令文本
猜测副作用。任何来源只要明确声明 `read_only` 均可用于调查；`unknown` 因无法证明无副作用而按非只读处理。Shell、
MCP、用户插件或其他 Host 工具即使理论上能写计划文件，也不能替代 canonical `ouro.host/tools/Write` 和
`ouro.host/tools/Edit`。错误路径或不允许的工具通过 deny 生成普通错误 ToolResult，不创建审批、不改变 Plan Session
状态，也不增加 Harness 分支。

脚手架消息属于模型执行事实，必须持久化：

- `/plan` 本身不产生消息；它只提交 acquire transition，等待下一条用户消息正常创建 Run；
- Host 只在 controller activation 存在于本轮快照时分发 Context Provider；provider 返回
  `persistence="message"` 的 Context Fragment，Application 在模型请求前通过
  `RunLifecycle` 落盘，并记录 plugin/provider/generation/model-round provenance；
- 同一 Run/model round 的恢复使用 delivery key 去重，持久化失败时阻断模型请求，不能静默降级为临时提示。
