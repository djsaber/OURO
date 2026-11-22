# 会话与运行控制

> 文档类型：使用指南

## 浏览运行中的会话

切换会话保留各自草稿与滚动位置，已受理的运行由 Application 持有。可以在一个 TUI 中管理多个后台会话，
也可以在另一进程使用同一 home 和后端打开运行中的会话；观察端独立消费正文，重新进入时补齐正式消息与流式尾部。
关闭单个观察端不取消其他进程持有的运行；显式停止和执行者进程退出仍遵循运行关闭策略。

长历史先显示近期完整消息帧。向上滚动到顶部会自动加载更早内容，并保持当前阅读位置。
导航、分支和编辑命令按需补齐历史，再解析全局编号。已加载的范围可在缓存内复用，历史替换后重新加载。导出始终包含完整历史。

排查测试时，可输入 `/btw 刚才日志里的超时是什么意思？`。旁问能引用已有日志，消息区只显示自己的提问和答复。
点击中间的来源节点或按 `Alt+↑` 回到排查现场。再从左侧节点打开原旁问，下一次追问会在模型预算内带上主会话新发布的进展。如果工具结果过大，或撤回使上次同步的位置失效，界面会提示，仍可继续提问。

## 编辑历史消息与继续运行

在空闲的主会话中输入 `/edit`，按 Enter 后从列表中选择要修改的用户消息。也可以直接输入 `/edit #3`。
输入框会带回这条消息的正文、附件和技能引用。按 Esc 取消并回到原草稿。按 Enter 提交后，OURO 在原会话中
撤回所选输入及之后的消息，恢复输入前的 Harness 状态，从修改后的输入开始新的 Run。
尚在排队的输入通过 `/unqueue` 取回。

撤销 canonical 消息前需先中断活跃运行并完成取消结算。取得执行所有权后，应用原子提交消息切口、状态指针、队列变化
和新的历史代次，再协调绑定旧历史的子会话。工具已经造成的文件与
外部系统变化、真实用量账本不回滚。旧运行与工具事实保留用于审计，不能重新写入撤销后的当前历史。

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
uv run --frozen ouro session messages <session_id> --home ./.ouro-dev --cursor 0 --limit 100 --json
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

`session remove` 会把所选主会话、它的各级旁问和所属子智能体放入同一条回收记录。直接删除旁问时，来源会话保留。
确认预览后，命令先请求停止范围内仍在执行的 Run 并等待释放锁，再锁定整组会话并回收，包括已失去执行者的未完成记录。
如果预览之后新增了旁问，请以更新后的范围重新确认。回收范围由会话关系确定。

附件提供全文、首尾预览、原生媒体或引用。PNG/JPEG/GIF/WebP 图片、WAV/MP3 音频与 MP4 视频在端点声明对应输入能力且预算允许时提供媒体内容；
PDF、Office、压缩包等以元信息和引用提交，不自动解析。模型获得原名称、来源与真实路径，自主使用现有 Read 等工具读取或处理。引用降级会明确说明尚未读取的内容。

资源通过 `resource import` 导入会话，再由 `run --resource <resource_id>` 附加到输入。在 TUI 中，
`/edit` 会将所选消息的资源引用带回输入框。也可以将多个文件拖入终端，或在输入框首位键入 `@file ` 打开目录菜单，继续键入名称过滤，
Enter 进入目录或引用文件，Space 多选文件，`..` 返回上一级，Esc 恢复草稿和光标。名称左对齐，文件选择标记在名称左侧，目录不显示多选标记。
目录读取期间保留原列表；欢迎页上传附件后仍停留在欢迎页，发送消息后才进入会话页。
菜单顶部常驻当前目录路径，右侧以低亮度 `[x]` 显示已选数量。拖入附件显示主题色文件名和扩展名，`@file` 引用保留 `@` 前缀，不显示路径；Backspace 整体删除。上传副本保存在
`OURO_HOME/sessions/<session_id>/resources/files/`，不自动写入工作目录；`@file` 保留原文件位置，不复制为会话附件。
带附件输入可照常排队；`/unqueue` 恢复文字和附件元素，保留已有草稿。细节与限额见[附件规格](../specs/attachments.md)。

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
