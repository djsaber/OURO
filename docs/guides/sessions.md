# 会话与运行控制

> 文档类型：使用指南

TUI 的 `/undo` 撤回最近一条直接用户输入；有待消费的排队输入时，优先取消最新一条。
已进入历史的输入连同其后的消息被撤销，会话 Harness 状态恢复到该消息之前。正文、附件引用和技能调用
返回编辑草稿，编辑后可重新提交；连续撤销可逐次回溯。原生状态和插件状态遵守同一规则。

撤销 canonical 消息前需先中断活跃运行并完成取消结算。撤销取得执行所有权后，原子发布消息切口、状态指针
和新的历史代次，再结清受影响的输入租约与子会话关系。工具已经造成的文件与
外部系统变化、真实用量账本不回滚。旧运行及事件保留用于审计，不能重新写入撤销后的当前历史。

`/continue` 在当前历史代次恢复合法的中断、失败或暂停检查点，保留原 Run 身份、已提交工具结果与累计用量。
撤销后则基于保留消息和恢复后的 Harness 创建新 Run，不重复追加保留输入，也不复用旧代次的审批或工具执行。
不完整工具交换由 Provider 请求投影处理，不再隐式裁剪 canonical 消息。约束仍未满足时显示具体原因。

使用方式与边界说明见 [消息撤销与会话继续](session-history.md)。

```bash
uv run --frozen ouro session list --home ./.ouro-dev --search auth --no-archived
uv run --frozen ouro session show <session_id> --home ./.ouro-dev
uv run --frozen ouro session summary <session_id> --home ./.ouro-dev
uv run --frozen ouro session summary <session_id> --home ./.ouro-dev --json
uv run --frozen ouro session context <session_id> --home ./.ouro-dev
uv run --frozen ouro session context <session_id> --home ./.ouro-dev --content --json
uv run --frozen ouro session rename <session_id> "Auth refactor" --home ./.ouro-dev
uv run --frozen ouro session tags <session_id> auth review --home ./.ouro-dev
uv run --frozen ouro session approval-mode <session_id> auto --home ./.ouro-dev
uv run --frozen ouro session afk <session_id> on --home ./.ouro-dev
uv run --frozen ouro session branch <session_id> <message_id> --home ./.ouro-dev
uv run --frozen ouro session replay <session_id> --home ./.ouro-dev --json
uv run --frozen ouro session messages <session_id> --home ./.ouro-dev --cursor 0 --limit 100 --json
uv run --frozen ouro session events <session_id> --home ./.ouro-dev --run <run_id> --cursor 0 --limit 100 --json
uv run --frozen ouro session compact <session_id> --home ./.ouro-dev
uv run --frozen ouro session archive <session_id> --home ./.ouro-dev
uv run --frozen ouro session remove <session_id> --home ./.ouro-dev --force

uv run --frozen ouro approve <session_id> --home ./.ouro-dev
uv run --frozen ouro reject <session_id> --home ./.ouro-dev --reason "unsafe"
uv run --frozen ouro abort <session_id> --home ./.ouro-dev
uv run --frozen ouro run --session <session_id> --queue-mode steer "Change direction"

uv run --frozen ouro resource import <session_id> ./attachment.pdf --home ./.ouro-dev
uv run --frozen ouro resource import <session_id> ./context.txt --home ./.ouro-dev
uv run --frozen ouro resource list <session_id> --home ./.ouro-dev
uv run --frozen ouro resource show <session_id> <resource_id> --home ./.ouro-dev
uv run --frozen ouro resource export <session_id> <resource_id> ./copy.pdf --home ./.ouro-dev
uv run --frozen ouro resource remove <session_id> <resource_id> --home ./.ouro-dev --force
uv run --frozen ouro run --session <session_id> --resource <text_resource_id> "Use this context"
```

`session remove` resolves the owning primary Session and moves its complete subagent tree to one
trash entry. It first requests canonical abort for genuinely live Runs and waits for their locks,
then acquires the complete tree lock set and discards stale unfinished records. Tree scope never
depends on deserializing historical Run or Delegation records.

模型附件当前支持有大小上限的 UTF-8 文本/JSON/XML，以及 vision Endpoint 上的 PNG、JPEG、GIF、WebP。
PDF 可存储和导出，但在接入专用解析 adapter 前不能直接投影给模型。

资源通过 `resource import` 导入会话，再由 `run --resource <resource_id>` 附加到输入。TUI 可以恢复撤销消息的
资源引用；手动选择新附件的 Composer 界面尚未提供。

`run --queue-mode follow-up|steer` 选择排队交付边界。follow-up 在当前 Run 结束后进入后续 Run，steer 在当前
完整 model turn 结束后进入同一 Run。TUI 在 Composer 中提供同一输入交付模式。
`session summary <session_id>` 查询会话历次保留的压缩摘要，显示完整正文、生成时间和生效状态，支持 `--json`。
TUI 使用无参数命令 `/summary` 仅显示当前可见会话最新一次摘要，命令输出和提示不展示会话 ID。查询不会调用模型或改变会话状态。

`session context [--content]` 读取最近一次真实 Provider 请求的指标或完整 system prompt、messages 和 tools；
TUI 可从 Actions 查看相同的上下文快照。

批准模式是会话状态，不是工具配置：对于默认 `permission_control="host"` 的工具，`inherit` 沿用工具定义和
`config permissions` 的结果，`auto` 自动执行，`approval` 要求审批，`deny` 拒绝调用（`AskUser` 在可执行时
仍按用户问答流程处理，而不是转成普通工具审批）。单次运行可使用
`--approval-mode` 创建或更新空闲会话；TUI 可通过 `/approval` 或审批选择器切换，活动 Run 也可使用。已经产生的
审批保持原状态，下一次工具审批边界读取新模式。会话分支继承源会话的批准模式。

AFK 是应用层预设，不是新的权限枚举：`AFK = approval_mode:auto + interaction_mode:unattended`。可用
`--afk`、TUI `/approval afk` 或 `session afk <session_id> on|off` 原子切换。无人值守模式不会让 Run
停在 `waiting_user`：支持该能力的 resumable tool 收到显式 `UnattendedResolution`；其他工具得到可反馈给模型的
`UserInputUnavailable` 结果。AFK 只自动批准可批准的调用，不覆盖 Tool Policy `deny`、工具硬拒绝、schema、路径、
执行契约或取消边界。会话分支同时继承 interaction mode。

Harness 编排工具可声明 `permission_control="tool"`，此时工具定义的权限是唯一执行依据，只允许 `auto` 或
`deny`；用户同名权限覆盖、会话批准模式和限制性 `tool_policies` 均不参与决策。该控制权适用于内置和受信
插件工具，适合进入/退出计划模式、目标模式等必须保持可调用的控制面操作。
