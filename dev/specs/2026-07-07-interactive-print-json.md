# OURO Agent Interactive 与 Print / JSON 运行模式规格

- Date: 2026-07-07
- Status: Accepted / Phase 1 implemented
- Related: ../../docs/OURO_AGENT_项目概述_V0707.md

## 1. Motivation

OURO 第一阶段不做 Desktop、SDK、RPC，但必须让系统真实可用。

Interactive 提供人机协同体验，Print / JSON 提供脚本调用和自动化入口。两者必须共享核心运行层，不能各自实现一套 Agent 逻辑。

## 2. Goals

- 实现 Interactive 终端交互模式。
- 实现 Print 一次性最终文本输出。
- 实现 JSON 一次性事件或结构化结果输出。
- 支持会话创建、打开、继续。
- 支持流式输出、工具调用展示、审批、AskUser、中止。
- 支持基础 Slash 命令。
- 支持清晰退出码。
- 保证宿主只负责输入、渲染、命令和用户交互，不复制核心运行逻辑。

## 3. Non-goals

- 不实现 Desktop。
- 不冻结 SDK/RPC 协议。
- 不实现复杂 TUI 多面板体验。
- 不实现多窗口、多标签或桌面后台通知。
- 不要求第一阶段提供完整用户设置界面。

## 4. Command Shape

建议命令形态：

```
ouro
ouro --session <session_id>
ouro run "prompt"
ouro run --json "prompt"
ouro run --print "prompt"
ouro sessions
ouro config check
```

具体 CLI 可以调整，但必须覆盖：

- 进入交互。
- 一次性运行并输出最终文本。
- 一次性运行并输出 JSON。
- 指定或创建会话。
- 检查配置。

## 5. Interactive Mode

Interactive 最低能力：

- 显示当前会话标题和状态。
- 接收用户输入。
- 流式显示 assistant 文本。
- 显示工具调用请求。
- 显示审批提示。
- 允许批准、拒绝、修改参数。
- 显示 AskUser 问题并接收回答。
- 支持中止当前运行。
- 支持基础命令。

第一阶段基础命令：

- /help。
- /new。
- /sessions。
- /open。
- /model。
- /config。
- /export。
- /abort。
- /exit。

命令输出进入事件流或会话状态，便于调试和未来回放。

## 6. Print Mode

Print 模式用于脚本中获取最终文本。

行为：

- stdout 只输出最终 assistant 文本。
- 进度、工具调用、审批提示输出到 stderr。
- 如果需要用户审批但当前环境不可交互，运行失败并返回明确退出码，除非用户显式允许非交互失败。
- 如果运行失败，stderr 输出错误摘要。

Print 模式仍然写入会话、消息和事件，除非用户显式要求临时运行。

## 7. JSON Mode

JSON 模式用于脚本消费结构化信息。

建议支持两种输出：

1. JSONL event stream

   每行一个事件，适合实时消费。

2. Final JSON

   运行结束后输出一个对象，包含 final_text、session_id、run_id、status、usage、error。

第一阶段已优先实现 Final JSON，包含事件数组和 usage。JSONL event stream 可以在需要实时脚本消费时后续增加。

JSON 输出不应混入人类可读日志。日志和进度输出到 stderr。

## 8. Exit Codes

建议退出码：

- 0：成功。
- 1：一般错误。
- 2：配置错误。
- 3：凭据或模型连接错误。
- 4：用户拒绝或审批不可用。
- 5：运行被中止。
- 6：工具执行失败。
- 7：持久化错误。

退出码不需要覆盖所有细节，但必须让脚本能区分成功、配置问题、用户中止和运行失败。

## 9. Session Handling

Interactive：

- 默认打开最近会话或创建新会话。
- 用户可以通过 /sessions 和 /open 切换。
- 第一阶段可以串行处理当前会话运行。

Print / JSON：

- 若传入 session_id，则在该会话中追加运行。
- 若未传入 session_id，则创建临时或新会话，具体由参数决定。
- 输出必须包含 session_id 和 run_id，便于后续追踪。

## 10. Approval in Non-interactive Mode

非交互模式默认不能等待审批。

策略：

- 遇到需要审批的工具时失败。
- 输出 approval_required 错误。
- 允许用户通过显式参数启用自动批准策略，但必须清楚标识风险。

第一阶段不建议默认自动批准写入、编辑、命令执行。

## 11. Rendering Contract

宿主接收核心事件并渲染。

Interactive 渲染：

- model_text_delta 显示为流式文本。
- tool_call_requested 显示工具名和参数摘要。
- approval_requested 显示批准界面。
- run_status_changed 显示状态。
- run_failed 显示错误。

Print 渲染：

- 只关心最终 assistant 文本。

JSON 渲染：

- 原样输出事件或最终 JSON 对象。

## 12. Acceptance

完成本规格后，应满足：

- `ouro` 可以进入交互模式。
- 用户可以发送消息并看到流式响应。
- 工具审批能在交互模式完成。
- 用户可以中止运行。
- `ouro run --print "..."` 输出最终文本。
- `ouro run --json "..."` 输出机器可解析事件或结果。
- 非交互模式遇到审批需求时给出明确错误。
- Interactive 和 Print / JSON 使用同一核心运行接口。

## 13. Resolved Decisions

- 第一阶段使用普通 stdin/stdout Interactive，Textual/Rich 体验后置。
- Print 模式默认创建或延续本地持久会话。
- JSON 模式默认输出 final JSON，包含事件数组和 usage；JSONL event stream 后置。
