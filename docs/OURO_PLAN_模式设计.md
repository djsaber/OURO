## 提示词

计划模式下，在每一轮用户消息前插入脚手架消息，提示词如下：

```md
Plan mode is active. You MUST NOT make any edits (with the exception of the
current plan file) or otherwise make changes to the environment, external
systems, or any state, unless a tool request is explicitly approved. Prefer
read-only tools. Tools that could modify state or produce side effects follow
the normal permission mode and rules. This supersedes any other instructions
you have received. Any other tools that would break plan mode — state-mutating,
side-effecting, or irreversible actions — are also blocked; call ExitPlanMode
first if you need them.

Workflow:

1. Understand — explore the relevant context using read-only tools
   (searching, reading, querying, inspecting — anything without side effects).
2. Design — converge on the best approach; consider trade-offs but aim for a
   single recommendation.
3. Verify — re-check key facts, files, or sources to confirm your understanding.
4. Write Plan — record the plan in the designated plan file; this is the only
   file you are allowed to modify.
5. Exit — call ExitPlanMode for user approval.

Handling multiple approaches: at most 2–3 meaningfully different approaches;
if one is clearly superior, just propose that one. When multiple approaches
are included, pass them as `options` to ExitPlanMode.

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

1. Use read-only tools to investigate the codebase. Use Bash only when needed.
2. Design a concrete, step-by-step plan.
3. Write the plan to the plan file with Write or Edit.
4. When the plan is ready, call ExitPlanMode for user approval.

Do NOT edit files other than the plan file while plan mode is active.
Use Bash only when needed; Bash follows the normal permission mode and rules.
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
  - 用户指定开启时，`/plan` 只迁移插件的 Session 状态，不创建 Run、不提交模型输入；用户随后单独发送规划请求，Context Provider 在该模型轮注入脚手架消息。
2. 创建计划并保存：需要上下时，LLM 调用相关只读工具查询或搜索获取信息，理解分析充分后，LLM 调用写入工具保存计划文件；
3. 退出计划模式：LLM 自主决策，调用 ExitPlanMode 退出计划：
  1. 未创建 / 空计划时，ExitPlanMode 调用失败，返回错误结果，LLM 重新决策；
  2. 已创建，触发用户批准反馈，LLM 基于反馈自主决策后续动作（执行任务/chong）

## 核心设计

1. 正常对话时，EnterPlanMode 调用决策交给 LLM；
2. 指定计划模式时（`/plan`），命令只切换状态；下一条用户消息触发模型 Run 时，Context Provider 才注入脚手架消息；
3. 处于计划模式时，每轮用户消息前面都要注入脚手架消息，加强对 LLM 引导；
4. 同一会话内多次开启计划模式，每次计划模式的 PLAN_FILE_PATH 指向不同文件，可用时间后缀方式；
5. ExitPlanMode 调用决策交给 LLM；
6. ExitPlanMode 工具只有一个可选参数 options，用于在计划包含多个可选方案时，让用户在批准时选择执行哪一个；
7. EnterPlanMode 和 ExitPlanMode 工具自身的调用不走批准策略；
8. 计划模式不控制 Run 的继续或完成，不注册 Turn Policy；EnterPlanMode 和 ExitPlanMode 只通过工具结果向 LLM 表达状态迁移，由 LLM 自主决定下一步。


PLAN_FILE_PATH: {WORKSPACE}/.ouro/sessions/{session_id}/plans/plan_xxx.md

> plan 模式不改变也不控制智能体主循环。在 ReAct 工具循环上，通过「会话内状态机 + 每轮提醒注入 + 工具调用拦截/收窄 + 一个显式的退出审批门」这四件套实现。系统提示词和工具列表全程不变，模式切换只发生在对话流里，这样 prompt cache 的前缀不会因为模式切换而整体失效。

## 内置插件实现约束

内置插件 `ouro/builtin_plugins/plan_mode` 复用 Host 的 `Write`/`Edit`，不贡献重复的文件工具。由于 OURO 默认启用
`tool_execution.workspace_only=true`，插件的实际路径采用：

```text
{WORKSPACE}/.ouro/sessions/{session_id}/plans/plan_{timestamp}_{nonce}.md
```

这保留了“每次激活生成唯一 Session 计划文件”的语义，同时不扩大内置写工具的允许根目录，也不让 Core 识别
计划文件特权。若未来产品需要把 artifact 放到 OURO_HOME，应先设计独立于 canonical Session 数据的通用 artifact
根目录，不能直接放宽 Write 对整个 `~/.ouro/sessions` 的写权限。

脚手架消息属于模型执行事实，必须持久化：

- `/plan` 本身不产生消息；它只迁移插件状态，等待下一条用户消息正常创建 Run；
- active Context Provider 返回 `persistence="message"` 的 Context Fragment，Application 在模型请求前通过
  `RunLifecycle` 落盘，并记录 plugin/provider/generation/model-round provenance；
- 同一 Run/model round 的恢复使用 delivery key 去重，持久化失败时阻断模型请求，不能静默降级为临时提示。
