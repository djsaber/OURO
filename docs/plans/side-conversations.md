# TUI 会话旁问

## 开源实现依据

- **OpenClaw** 用 `/btw` 随口问当前任务。运行中优先读取活动消息快照和在途提示，否则从持久历史取上下文。单次无工具调用把答案送到临时界面，问题和答案都不写会话。OURO 借鉴直接发问和让主任务继续执行的交互。[官方说明](https://github.com/openclaw/openclaw/blob/8b2a6e57fef6c582ec6d27b85150616f9e3a7ba4/docs/tools/btw.md) · [上下文选择](https://github.com/openclaw/openclaw/blob/8b2a6e57fef6c582ec6d27b85150616f9e3a7ba4/src/agents/btw.ts#L295-L321)
- **OpenCode** 用 `POST /session/:id/fork` 从既有对话探索另一条路径。`Session.fork` 创建会话，按序复制指定消息之前的消息及各个 part，重映射 ID。省略消息 ID 时复制到当前末尾。客户端随后向新 session ID 提交输入，后续消息归该分支所有。OURO 可复用这种一次复制、各自续写的方式。[分支 API](https://github.com/anomalyco/opencode/blob/4ee426ba549131c4903a71dfb6259200467aca81/packages/web/src/content/docs/server.mdx#L159) · [发言 API](https://github.com/anomalyco/opencode/blob/4ee426ba549131c4903a71dfb6259200467aca81/packages/web/src/content/docs/server.mdx#L175-L178) · [分支实现](https://github.com/anomalyco/opencode/blob/4ee426ba549131c4903a71dfb6259200467aca81/packages/opencode/src/session/index.ts#L239-L280)
- **Pi** 的 `/tree` 在同一会话树中切换路径。用户可选择总结离开路径上从共同祖先之后的条目，把摘要追加到目标位置，后续消息接在新叶节点。OURO 借鉴追加式交付，来源范围由两个独立会话之间的持久位置确定。[官方说明](https://docs.pi.dev/latest/compaction#branch-summarization) · [选取范围](https://github.com/earendil-works/pi/blob/9a53d950c4fee96b16444eec56020884ac3dffb6/packages/coding-agent/src/core/agent-session.ts#L2625-L2632) · [追加摘要](https://github.com/earendil-works/pi/blob/9a53d950c4fee96b16444eec56020884ac3dffb6/packages/coding-agent/src/core/session-manager.ts#L1127-L1150)

## OURO 调用链

下文用 A 表示用户发起命令时的会话，用 B 表示新建的旁问。

1. `Composer.CommandSubmitted` 交给 `_execute_composer_command` 排队，再经 `resolve_command` 和 `OuroTuiApp._dispatch_command` 分派。普通输入从 `_submit_run`、`TuiController.submit` 进入 `RuntimeSupervisor.submit_user_input`，由输入接收服务以稳定 ID 持久受理。[Slash 注册](../../ouro/tui/commands.py) · [TUI 入口](../../ouro/tui/app.py) · [控制器](../../ouro/tui/controller.py) · [Supervisor](../../ouro/application/supervisor.py)
2. `SessionService.branch` 和 `create_side_conversation` 各自选择可继承的范围，共用 `_copy_branch` 复制消息、工具事实、资源、扩展状态及有效压缩链。普通 `/fork` 要求源会话空闲。旁问创建持有存储事务，运行锁仍归 A。`RunLifecycle.publish_tool_batch` 一次发布整批结果，允许区分已保存的单个工具结果与模型可见的工具交互。[会话服务](../../ouro/application/sessions/service.py) · [历史复制](../../ouro/application/sessions/history.py) · [运行生命周期](../../ouro/application/runs/lifecycle.py)
3. `RuntimeService._prepare_input_messages` 构建输入单元。`_start_queued_run` 和 `_consume_queued_turn_inputs` 在目标会话事务中经 `RunLifecycle.start` 或 `append_messages` 写入 `SessionCommit`。`ContextPreparationService.load_history/build` 只加载当前会话及其活动压缩选择，再投影工具和媒体、交给 Provider 编码。[输入接收](../../ouro/application/inputs/admission.py) · [运行编排](../../ouro/application/runtime/service.py) · [上下文准备](../../ouro/application/context/preparation.py)
4. 节点点击和快捷键都进入 `_request_session_switch`。目标稳定后，`_show_observed_session` 通过 `observe_session` 准备订阅，`_install_session_surface` 核对视图版本并安装对应的控制目标、草稿和会话投影。执行恢复由显式打开或恢复会话的入口负责。[单行节点](../../ouro/tui/session_navigation.py) · [视图切换](../../ouro/tui/app.py)

## 推荐方案

### 创建与往返

用户在 A 输入 `/btw 刚才日志里的超时是什么意思？`。TUI 在按下 Enter 时绑定 A 的 ID，命令等待期间 A 可以继续追加消息。开始创建前若用户已经切到其他会话，取消本次创建，A 的草稿保留完整的 `/btw` 命令。应用在 A 的一次一致性快照中决定复制范围，再发布普通可写会话 B。`context_source_session_id` 指向 A，B 自己是运行树的根。首问先归入 B 的草稿，视图和输入目标都切到 B 后，才经普通输入入口提交。若用户在创建过程中切走，已创建的 B 保有首问草稿。若输入受理失败，用户在同一个 B 中修改或重发。归属和输入恢复同样适用于从旁问再发起旁问。

快照可以包含 A 刚提交的用户提问。正在流式生成的答复要等正式消息提交后才能继承。若 A 正执行或等待恢复一批工具，边界停在该批调用之前。最后一个工具完成并且整批结果发布后，调用和结果才一起可见。A 保有原 Run 和审批，B 按普通 Supervisor 容量调度，用自己的 Run、工具和审批作用于同一工作区。当审批或问答面板占用 A 的输入框时，节点仍可用于访问已建旁问，新命令在输入框恢复可编辑后提交。

初始模型上下文遵守 A 的有效压缩选择、媒体边界和扩展的 `copy/reset/reject` 策略。分支点记录实际保留的最后一条源消息，继承游标记录已处理到的源位置。两者可因重置扩展投影而不同。摘要包含对所覆盖内容和前驱摘要的叙述。若覆盖范围内有必须剔除的投影或废弃输出，应退回最近一份覆盖内容完整保留的摘要，由 B 用剩余规范消息构建上下文。[会话分支与压缩选择](../../ouro/application/sessions/history.py) · [摘要输入](../../ouro/application/context/summary.py)

单行 `■■ | ▣ | ■■■` 左侧按创建顺序排列旁问，中间提供返回节点，右侧展示当前可写会话拥有的子智能体。在 B 中，左侧可访问 A 的直接旁问和 B 自己发起的旁问，中心返回 A，右侧可打开 B 派发的子智能体。进入 B 发起的 C 后，中心返回 B。观察只读子智能体时，返回其所属的可写会话。点击与 `Alt+←/→` 共用切换入口，`Alt+↑` 返回。草稿的文本、附件和技能按会话恢复，审批跟随所属会话。旁问标题和子智能体详情沿用单行节点的悬停、选中和主题样式。

B 的消息区只显示 B 自己的提问和助手的公开答复。实时输出、重开和翻页使用相同规则。初始材料、后续来源资料及 B 的工具记录仍在 B 的历史中。普通打开列表省略能从来源节点访问的 B。单独导入或来源不可打开的 B 可从普通列表打开，已归档的 B 可从归档域打开。重命名和归档菜单按所属域列出 B。重命名后节点显示新标题，归档后移除 B 的节点。仍在查看 B 时，返回节点可带用户回到 A。[转录投影](../../ouro/tui/conversation/transcript.py) · [会话列表](../../ouro/tui/menus/sessions.py) · [会话导入导出](../../ouro/application/transfer/sessions.py)

### 同步与消费

创建时的快照已足以完成一次旁问和多轮追问。为支持用户回到 A 复测后再来问 B，本方案在**成功重入 B 时冻结来源进展，在下一次用户发言时交付**。浏览产生零次模型调用。在 B 停留期间，A 后续产生的进展留到下次重入。A 构建请求始终使用自己的历史。

位置由源 session ID、history epoch、消息 ID 和序号共同标识。读取 B 最后保留的交付记录，首次从创建边界开始，再在 A 的一致性读事务中核验锚点。取锚点之后的有序可见消息，保留工具名称、参数和已发布结果的文本，媒体以类型说明表示。在途草稿、私有推理及未整批发布的工具交互均不交付。A 压缩时规范消息仍然存在，可以直接提取未交付区间，跨越游标的整份摘要不会重复进入 B。A 撤回后，只要原锚点仍匹配即可继续。若锚点丢失，打开 B 时提示原因，用户仍可用已有历史提问。[范围选择与交付](../../ouro/application/inputs/source_history.py)

浏览时的冻结范围属于当前视图。发言前退出 TUI，重新打开 B 算作一次重入。Host 调用 `RuntimeService.source_progress`，应用按 B 当前模型的输入上限，从 A 分页读取连续的完整消息或工具批次。输入受理时一起保存冻结的资料和真实追问。消费时按 B 的最新游标裁掉重叠部分，通过 `ContextPreparationService` 估算当前历史、指令、工具和本批用户输入的占用。动态系统指令的额外开销参考最近一次上下文测量。用剩余预算附上资料，单条消息或工具批次过大时，以带源位置的省略说明代表整个单元。游标只随同次提交实际附上的资料或说明推进。连说明也放不下或两个窗口的来源位置冲突时，提示原因，用 B 已有上下文处理追问。[资料交付](../../ouro/application/inputs/source_history.py) · [模型与输入预算](../../ouro/application/runtime/service.py)

取消待处理输入时保持游标。重试沿用输入 ID，重启后显式继续或再次提交时从持久队列恢复。撤回已消费的追问时一起裁掉该次资料，前一条交付记录重新成为游标。编辑或撤回 B 的其他消息时，已受理的直接用户输入保留 ID、内容和顺序。应用在消费时核验随附资料是否仍可使用。[撤回边界](../../ouro/application/sessions/editing.py) · [队列的既有约定 QU-02](../specs/session-undo.md)

### 回收与模块边界

整组回收是会话管理策略。删除 A 时，应用递归纳入其旁问及所属子智能体。直接删除 B 的范围止于 B 及其后代。CLI 列出整组成员，把用户确认的集合传给执行端。应用在请求停止活动 Run 前和最终移入回收站时都核对这份集合。期间新增关联会话则保留整组，由用户重新确认。若变化发生在停止请求发出之后，已收到请求的 Run 仍会停止。[会话管理](../../ouro/application/sessions/service.py) · [CLI 删除入口](../../ouro/cli/session_commands.py)

文件后端用来源会话的变更锁保护快照。SQLite 回收先锁定资源再取得写事务，持有写事务才能移除已发布的资源文件。创建快照时，同一写事务也保护资源读取。[持久化规格](../specs/session-persistence.md)

| 模块 | 旁问需要的最小职责 |
| --- | --- |
| TUI 命令、控制器、节点和转录 | 绑定来源 ID，提交首问，安装已观察的会话，保留草稿，筛选可见内容。导航与显示规则由 Host 持有。 |
| 应用层会话服务与输入处理 | 创建独立快照，决定可继承的范围和有效摘要，冻结与去重来源资料，协调删除范围。首问及追问沿用直接用户输入入口。 |
| 通用记录、队列与存储 | 持久化来源标识和已受理输入的附带资料。附带资料使重启后的输入可复现。沿用既有消息提交、Run 所有权、工具审批和按会话读取历史的接口。 |

### Provider 请求与验收

令 `H` 为初始日志及其所在的已提交对话。B 首轮执行工具后，取得最终答复的 Responses 请求与下一次追问的请求如下。每个方括号代表一个有序输入项，来源增量在一个带语义标签的资料项内。

```text
B 首轮最后一次请求   H [B 首问] [B 工具调用] [B 工具结果]
B 重入后首次追问     H [B 首问] [B 工具调用] [B 工具结果] [B 首轮答复] [A 新增两轮] [B 追问]
A 继续排查          H [A 新增两轮] [A 的下一条输入]
```

配置和 B 的上下文选择固定时，逐项比较前一请求的整个 `input` 与后一请求等长的前缀，并比较 `instructions` 和工具定义。B 自身压缩、历史编辑、媒体裁剪、模型路由、系统指令或工具配置变化都可能改变前缀。A 的压缩和节点浏览不会重写 B 已有消息。远端缓存另行核验，Responses 读取 `usage.input_tokens_details.cached_tokens`，Chat Completions 读取 `usage.prompt_tokens_details.cached_tokens`。OURO 归集为 `cache_read_tokens`。端点省略缓存用量时，实测结果记为未报告。[Responses 解码](../../ouro/providers/openai_responses/codec.py) · [Chat Completions 解码](../../ouro/providers/openai_chat_completions/codec.py) · [用量归集](../../ouro/application/usage.py)

验收使用 files、SQLite、真实 Responses 适配器和本地 HTTP 测试传输。下列结果可独立观察。

- A 的 `Read` 读到只存在于日志中的错误码。B 的首个请求含该码，测试服务从收到的请求提取它作为答复。B 再执行带独特标记的工具并追问。返回 A 后检查其下一请求，保留原日志，B 的问题、答复、工具标记及同步资料项计数均为零。[端到端请求回归](../../tests/tui/app/test_side_conversations.py)
- 在流式答复中和工具整批发布前后创建 B，核对 A 的 Run ID、审批和最终结果。让命令排队后切去另一工作区，核对实际创建来源、输入去向及草稿。B 派发的子智能体可从 B 打开并返回。单独导入或归档的 B 可重新找到。[会话导航回归](../../tests/tui/app/test_side_conversations.py)
- 两个窗口冻结重叠或冲突的增量，覆盖输入重试、重启、取消、撤回和编辑。在超大工具结果之后追加一条短日志，核对 B 的追问能得到答复、短日志可交付、游标与实际资料或省略说明一致。让来源摘要包含被重置投影的唯一标记，核对 B 的实际请求中该标记计数为零。[应用层回归](../../tests/application/test_side_conversations.py) · [分支与摘要回归](../../tests/application/test_sessions.py)
- 删除预览后新建 B，用户可在更新后的范围上重新决定。让创建和回收分别到达各自的锁边界后并发继续，结果应是完整回收或可重试的范围变化。成功回收后清除所属草稿和打开的视图，保留无关会话。[生命周期回归](../../tests/application/test_sessions.py)
