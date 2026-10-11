# OURO TUI 规格

> 文档类型：现行规格
> 主要实现：`ouro/tui/`、`ouro/local_application.py`、`ouro/application/`

## 目标与边界

OURO 默认以 Textual 全屏 TUI 启动。TUI 是 Application 的本地 Host adapter，负责页面状态、输入映射、
渲染、导航和本地偏好；Session、Run、工具审批、AskUser、子智能体、插件、Provider、MCP、Skills 与恢复规则
仍由 Application/Core 及其端口拥有。

TUI 不得：

- 读取或改写 Session 文件、SQLite 表、插件状态文件或全局配置 JSON；
- 从原始事件 payload 推断 Application 已提供的业务状态；
- 自建 Agent loop、工具执行器、审批状态机、子智能体调度器或 MCP 生命周期；
- 因切换视图、刷新页面或关闭弹层而改变后台 Run 生命周期；
- 将 Textual widget、Message、Screen 或 driver 类型暴露给 Application。

## 组合与生命周期

`ouro` 无子命令时，CLI 解析 `--home`、`--session-backend`、`--system-prompt`、`--session`、
`--approval-mode` 与 `--afk`，再由本地 composition root 创建 `LocalApplication`、`TuiController` 和
`OuroTuiApp`。TUI 只使用 controller 暴露的 DTO、命令与借用的 Application 用例服务。

启动流程必须满足：

1. 解析 OURO home、Session 后端和初始策略；
2. 启动 `LocalApplication`，完成 Provider、插件、MCP、Skills 与 Agent catalog 装配；
3. 读取 TUI 偏好和最近工作区；
4. 加载指定 Session，或显示新 Session 欢迎页；
5. 建立受控 Session 的 Runtime 订阅；
6. 退出时依次停止观察、保存允许持久化的 UI 状态并关闭 Application 资源。

启动诊断属于系统状态，不得用假 Session 消息写入对话。单个 optional MCP 或插件贡献失败可以显示 degraded
状态；无法建立权威 Application 边界的错误必须阻止对应操作。

## 页面与状态所有权

主页面由以下稳定区域组成：

- 品牌与系统状态头部；
- 欢迎页或会话时间线；
- 运行活动、上下文和子智能体导航投影；
- composer、排队输入提示和交互面板；
- command popup、选择器、配置页和帮助页等临时 surface。

TUI 状态分为三类：

| 状态 | 所有者 | 示例 |
|---|---|---|
| 业务事实 | Application/SessionStore | Session、Run、消息、工具批次、审批、交互、Delegation |
| Host 控制状态 | TUI controller | controlled Session、当前模型选择、待提交输入、订阅游标 |
| 纯呈现状态 | Widget/App | visible Session、滚动锚点、选中项、弹层、展开状态、输入光标 |

`controlled Session` 是允许提交输入和控制 Run 的主会话；`visible Session` 是时间线当前展示的会话。
查看 child Session 时两者可以不同。只切换 visible Session 不得重启、暂停、接管或取消任何 Run。

## 欢迎页样式

欢迎页样式与界面配色为并行配置，保存于 `<OURO_HOME>/config/tui.json`：

| 字段 | 默认值 | 约束 |
|---|---|---|
| `welcome_style` | `event-horizon` | 必须存在于当前 App 的样式包目录 |
| `welcome_motion` | `true` | 控制样式包内动画播放；暂停时保留静态呈现 |

`Ctrl+P → Theme` 使用标准筛选菜单，依次展示 `Welcome style` 与 `Theme color` 两个分组。
欢迎页样式直接以单行名称展示，不显示描述；同组的 `Animation` 切换播放与暂停。
主题色保留色阶预览，`←/→` 仅在主题色行调整饱和度，`Enter` 应用当前选项。
选择必须先成功持久化再应用。存储失败时保持当前组件与偏好并显示错误。连续选择和关闭菜单不取消
App 持有的写入/挂载事务；事务与其他 TUI 偏好更新共用锁。切换包不重建 composer 或清除草稿。

内置包包括 `basic`、`event-horizon`、`electron-bloom`、`julia-reliquary`、`interference`、
`clifford-torus` 和 `quasicrystal`。`basic` 是静态 OURO 字标；其余包各自拥有固定点阵上的独立场与光谱。
点阵包目标刷新率为 30 FPS，实际帧率取决于终端尺寸、字体和输出速度。它们在不可见时停止动画计算，
卸载时停止定时器；暂停和恢复不会将隐藏时长计入动画。菜单打开期间切换样式须立即显示静态作品；
终端缩放只重建当前尺寸的网格与帧缓冲，并绘制当前帧，不依赖动画播放状态。

`WelcomeStyle` 以身份、名称、说明和工厂组成通用包契约；工厂提供新的 `WelcomePage` 全屏根组件。
`run_tui(welcome_styles=...)` 与 `OuroTuiApp(welcome_styles=...)` 支持显式注入自定义包，目录按 App 隔离，
重复 ID 被拒绝。框架只管理注册、选择、配置和组件生命周期，支持任意 Textual 渲染或组件组合，
不要求点阵、RGB 采样或特定动画算法。点阵要求只约束上述六个内置艺术包。

样式包控制整个欢迎页的背景、装饰、组件组合、信息布局与响应式规则。包通过 `place_input(viewport, measure)`
返回公共输入层的位置与尺寸；Host 在其上层呈现持久输入组件，按剩余空间安排命令弹窗，菜单覆盖整页。
System、Usage、Tips、Version 是可选的复用组件，包通过 `set_system_status` 接收 Host 的不可变信息快照。
通用 Host 不限定居中布局、最大页宽、艺术区域或点阵形式。局部输入样式由当前包的 Host 类限定，离开欢迎页即撤销。

扩展代码及生命周期说明见
[欢迎页样式包开发](../development/welcome-styles.md)。行为由 `tests/tui/test_welcome_styles.py`、
`test_welcome_layout.py`、`test_welcome_refresh.py` 及主题菜单回归验证。

## 会话与工作区

新会话通常在第一条普通输入提交时创建；附件导入可提前准备空闲 Session 以保存资源归属，页面仍停留在欢迎页，
发送后才展示会话时间线。欢迎页可通过 `/workspace [path]` 选择工作区；Session 创建后工作区固定，
变更工作区必须先进入新会话。`/workspace clear` 只清理最近工作区记录。

会话入口支持：

- `/new` 或 `Ctrl+N`：回到新会话欢迎页；
- `/sessions`、`/resume` 或 `Ctrl+S`：浏览并打开 durable Session；
- `/fork [full|#N]` 或 `/branch`：在合法消息边界创建独立分支；
- `/clear`：只清空当前可见时间线，不删除 canonical 数据；
- `/navigate [target]`：在顶部、底部和用户消息之间导航。

打开、重命名和归档会话菜单通过 `Tab` 循环切换 `USER`、`TASK`、`SUBAGENT`、`ARCHIVED` 会话域，允许域为空。
前三个域分别展示未归档的用户主会话、任务主会话和所有子智能体会话；子智能体按 `session_kind` 汇总，
不受主会话、命名空间或派遣层级限制。`ARCHIVED` 汇总所有已归档会话，归档状态不再作为列表内分组。
归档菜单在前三个域执行归档，在 `ARCHIVED` 域执行取消归档；重命名适用于所有域。
删除菜单固定展示 `ARCHIVED` 域，沿用多选与永久删除确认，并在确认时重新检查归档状态。
未归档的会话即使存在目录或内容错误，也不进入删除列表。

Actions 的 Settings 分组提供 `Session grouping`，可选择 `By date`（默认）或 `By workspace`，持久化到
TUI 偏好 `session_grouping`。所有会话列表共用同一分组投影：日期按本地最近活动时间分组，工作空间按完整路径
分组，未指定工作空间使用 `No workspace`。分组按目录中首次出现的顺序排列，组内保留目录顺序。
各页复用筛选、左右键分组导航、当前会话标记及健康状态样式，不扫描 canonical Session。
页标题显示当前会话域，Tab 提示统一为 `Tab switch sessions`；行内重命名期间保留编辑上下文，不切换域。
打开子智能体会话复用现有只读视图及主会话控制边界。

会话运行或等待审批、输入时仍可返回欢迎页、浏览和切换会话，包括从只读 child 视图离开。
导航只切换页面及其订阅，不取消后台 Run。后台事件、完成结果和排队输入不得覆盖当前页面、草稿或交互面板；
重新打开会话时复用标准 snapshot/live 衔接，恢复流式内容及待处理交互。

打开 Session 时，历史 snapshot 与 live 事件必须通过 sequence/high-water 契约衔接：snapshot 前缀只呈现一次，
其后的事件按序归约；订阅落后时从持久化 cursor 追赶。snapshot 与点查携带 history_epoch，先过滤旧代次
再读取消息；历史变化时重载，当前历史中缺失消息仍报告错误。恢复不得依赖 widget 树中残留的旧状态。

Session 历史读取与模型执行配置解耦。模型路由失效、Provider 不可用或 reasoning 无法解析，不得阻止打开和
观察有效会话。上下文用量优先保留持久化事实，当前模型可解析时投影其额度，否则保留记录中的额度；无法解析
的 reasoning 不展示状态指示。加载不自动替换持久化模型，用户可重新选择有效路由；模型切换只验证目标路由，
实际请求仍执行模型、认证与能力校验。会话数据缺失、损坏或版本不受支持仍按持久化契约报错。

## 输入与运行控制

会话正文、思考正文、流式文本与多行输入按终端 cell 宽度软换行。可用宽度由组件扣除边框、内边距、
滚动条及光标预留后提供。中文等非空白分词文本使用 Unicode 断行边界，英文单词优先保持完整，
超长单位按完整字形折行。纯文本中的显式换行保留；软换行不向输入或 canonical 消息插入字符。
Markdown 遵循自身的段落与硬换行语义，保留列表缩进、行内样式及链接，代码块沿用 Rich Syntax 的代码折行策略。

编辑器的显示行、光标、选区和鼠标定位共用文档断行偏移。窗口尺寸变化重建显示几何；文本编辑只重排
受影响的源行并更新后续行的位置索引。附件身份、撤销重做及提交文本继续由原有文档和编辑流程拥有。

composer 支持多行编辑、历史、命令补全和运行中排队：

- `Enter` 提交通常输入；
- `Shift+Enter` 插入换行；
- `Ctrl+Enter` 或 `Ctrl+J` 将运行中输入作为 steer 提交；
- 普通运行中提交按配置形成 follow-up；
- `/unqueue` 将选中的未消费输入取回 composer；
- `/undo` 撤销最近直接用户输入，优先选择待消费输入，并恢复完整编辑草稿；
- `/continue` 继续合法检查点，撤销后从当前历史创建新 Run；
- `Ctrl+C` 或 `/abort` 请求中止 active Run；
- `Ctrl+Q` 或 `/quit` 关闭 TUI。

直接用户输入必须保持 `role=user`、`scaffold=false` 和用户 provenance。命令只负责选择 Application 动作；
有效命令不得作为普通用户消息误提交；无法匹配的命令或固定参数作为原始正文发送。
排队输入使用 Application 的 durable queue、消费模式和去重语义。

## Slash 命令

参数菜单区分固定选项和自由输入的建议项。输入非空且匹配结果包含建议项时，不自动选中候选；
`Enter` 提交输入原文，`↑/↓` 显式选择后可提交候选，`→` 接受行内补全。继续编辑会清除建议项的选择。
空输入和仅含固定选项的菜单沿用默认选择行为。工作区历史路径属于建议项，`clear` 属于固定选项。

命令前缀或非空参数没有候选时隐藏菜单，不显示无匹配占位。回车仅在输入匹配命令及其参数契约时执行命令；
否则保留完整输入、已有附件及提交模式，按普通消息发送。固定选项不接受未声明的后续文字；
工作区路径与插件声明的自由参数不要求存在菜单候选，仍由对应命令校验和执行。

内置命令目录由 `ouro/tui/commands.py` 定义。当前命令为：

| 命令 | 行为 |
|---|---|
| `/new` | 新会话 |
| `/workspace [clear\|path]` | 新会话工作区与最近记录 |
| `/sessions`、`/resume` | 会话选择 |
| `/actions`、`/menu` | Action 菜单 |
| `/fork [full\|#N]`、`/branch` | 会话分支 |
| `/model <provider/endpoint>` | 会话模型路由 |
| `/approval [inherit\|auto\|approval\|deny\|afk]` | 会话审批/无人值守预设 |
| `/thinking [...]`、`/reasoning` | 会话思考意图 |
| `/thoughts [expand\|collapse]` | reasoning 显示偏好 |
| `/compact` | 手动上下文压缩 |
| `/summary` | 显示当前可见会话最新一次压缩摘要 |
| `/skill <skill> [task]` | 显式技能激活 |
| `/unqueue` | 取回排队输入 |
| `/undo` | 撤销并恢复最近用户输入 |
| `/continue` | 继续当前会话 |
| `/clear` | 清空可见时间线 |
| `/navigate` | 对话导航 |
| `/abort` | 中止 active Run |
| `/help` | 快捷键与命令帮助 |
| `/quit`、`/exit` | 退出 |

`/summary` 通过 Application 查询持久化摘要，仅显示持久化顺序中的最后一条，保留完整正文；最新结果与当前生效状态独立。
结果沿用命令输出的字面代码块，空结果沿用单行 notice。
命令不接受参数，输出标题和提示不展示会话 ID；未打开会话时提示先打开会话。显示生成时间、触发方式和生效标记，
不重新生成摘要、不切换受控或可见会话、不写入模型对话历史。查询期间若切换了可见会话，结果不追加到新会话视图。

插件 command 与内置目录合并时使用稳定 canonical identity。内置命令优先；插件 local name 只有在当前目录唯一
时才可作为短别名。插件返回的 Session input、control transition 和输出继续通过 Plugin/Application 契约执行。

## 快捷键

全局快捷键包括：

| 快捷键 | 行为 |
|---|---|
| `Ctrl+N` | 新会话 |
| `Ctrl+S` | 会话选择 |
| `Ctrl+P` | Actions |
| `Alt+P` | 插件管理 |
| `Alt+T` | 循环思考设置 |
| `Alt+A` | 循环审批设置 |
| `Ctrl+↑/↓` | 对话顶部/底部 |
| `Ctrl+←/→` | 上一/下一条用户消息 |
| `Alt+←/→` | 上一/下一子智能体节点 |
| `Alt+↑` | 主会话 |
| `Alt+↓` | 最近子智能体 |
| `Ctrl+C` | 中止 |
| `Ctrl+H` | 帮助 |
| `Ctrl+Q` | 退出 |

Windows driver 对 `Ctrl+H`、Alt 组合键和 `Shift+Enter` 做最小规范化；其余键语义由 Textual 事件处理。
键位修正不得污染领域输入或依赖全局轮询。

Windows 输出适配器在 Textual 的 flush 边界合并画面片段，复用框架的输出线程。
控制台输出通过一次 `WriteConsoleW` 提交完整 UTF-16 帧及末尾的输入光标恢复指令，
避免分段写入之间暴露绘图光标；重定向输出沿用原始文本流。独立控制指令在下一事件循环轮次排出，关闭驱动时排空待写数据。
驱动启动时查询终端的同步输出能力（DEC 2026），由 Textual 处理响应并在支持时封装完整画面更新。
同步输出控制终端画面的呈现时机，与原生写入边界是独立的约束；不支持该能力时仍保持完整帧写入。
输入法预编辑和候选窗由终端管理，TUI 不模拟输入法组合状态，也不按语言改变动画行为。

## 模型、上下文与设置

`/model` 与模型选择器使用完整 `provider/endpoint` 路由。已有 Session 的切换只影响下一次模型请求；已发出的
请求继续使用原路由。`/thinking` 写入供应商中立意图，Endpoint 能力不支持时在网络调用前失败。

`/compact` 调用现有 compaction 用例；TUI 不自行选取消息或生成摘要。上下文详情读取最近一次真实 Provider
请求快照，不重新运行 Context Provider，也不从消息历史猜测请求内容。

Actions/设置界面当前覆盖主题、reasoning 展开、输入草稿、排队输入、压缩、工具执行、Agent definition 与派遣
策略、Provider/Endpoint、MCP、插件、Skills 及来源。编辑使用 Configuration/Application 命令的 revision 与
校验规则；UI 不直接合并共享配置。

端点列表在当前 Session 或待建会话选中的完整 `provider/endpoint` 路由左侧显示主题选定标记，与菜单焦点独立。
全局默认端点通过右侧 `default` 标签展示，两者可以同时出现；修改默认配置不覆盖已有会话选择。
列表刷新时同步更新标记与标签；当前选择无法解析时不显示选定标记，仍允许浏览端点菜单。
添加自定义供应商时，协议由上级自定义连接类型确定，表单不再提供协议配置项；已有供应商及预设的协议信息只读。
新增 Endpoint 表单的 context window 默认为 `1000000`，max output tokens 默认为 `32768`；编辑已有 Endpoint 时
显示并保留其已配置额度。

欢迎页 USAGE 区显示最近日期的用量图、请求数、输入/输出 token 和缓存命中率；日期悬停展示对应日数据。
进入欢迎页时先显示最近一次有效数据，首帧绘制后在后台读取最新系统状态与用量，不阻塞页面显示和输入。
运行完成只在欢迎页可见时刷新全局用量；完整状态与用量刷新共用串行读取，并发通知合并，完整状态读取优先，
读取期间的新通知触发后续读取。未变化的数据不更新控件，成功刷新不显示提示；读取失败保留有效数据并显示警告。
离开页面前发起的读取不得覆盖新页面状态；完整状态读取期间若工作区、模型或服务状态发生变化，重新读取后再更新。
主页不展示费用、计费模式或 priced/unpriced 信息，账本查询契约见[用量与计费规格](usage-and-billing.md)。

## Provider 认证管理

Provider 子菜单的 `Authentication` 入口通过 `TuiController.provider_auth()` 借用当前 LocalApplication 的同一 ProviderAuthService；TUI 不创建认证服务、不读取凭据文件，也不导入 auth 的驱动或存储实现。

- 状态和共享引用直接来自 Application DTO，手动刷新及返回子页面时重新查询。缺失或损坏凭据不阻止完整 TUI 启动；损坏状态显示安全错误码。static 不显示登录方法，注销项禁用。
- 登录列表使用稳定 method id、label 和 description；浏览器回调和设备码页面由 challenge.kind 决定，不按厂商分支。URL、设备码、有效期和说明仅在当前登录页展示，不写入会话、通知或本地偏好。
- 只有用户选择 `Open browser` 才打开浏览器；复制 URL 同样是显式动作。浏览器启动失败仍保留手动打开和取消入口。成功后不自动发送模型请求。
- 登录页只拥有一个等待任务；Application 拥有授权、条件提交与终态。Esc 或取消动作取消当前操作并等待清理，随后显示实际终态；若保存已先行，显示成功。页面卸载或 Host 退出同样收尾，不能留下迟到保存。临时筛选条件不会隐藏终态。
- 注销先展示当前共享 Provider 引用并使用现有确认页。`Manage local credentials` 展示安全摘要，允许清理 Provider 删除后留下的凭据；取消确认没有副作用。清理保留 Application 定义的 logged_out 记录。
- 受管理 Provider 的配置页显示认证驱动并隐藏 API Key/env 输入；创建时传递模板认证默认值，普通编辑保留现有认证绑定，不隐式切换账户或认证方式。
- Provider 配置页的 Request headers 接受 literal/env/context 请求头声明 JSON；空白保留已有配置，`{}` 清空，完整对象整组替换。页面显示当前条目数，编辑框不预填脱敏值；保存通过 ConfigurationFacade 原子校验并持久化，格式及字段错误阻止保存。

`tests/tui/test_provider_auth.py` 以两种不同方法及秘密结构的测试驱动验证登录—状态—注销/清理、授权失败、保存失败、配置冲突、取消/保存竞争、卸载和完整 Host 关闭；实际配置及凭据服务参与测试。此证据不代表生产 OAuth 或订阅后端已接入。

## 工具审批与 AskUser

审批、AskUser 和 resumable plugin tool 使用 composer 上方的专用交互 surface。面板必须显示明确的调用身份、
问题/选项或批准范围，并把结果路由到精确 Session/Run/ToolCall/request identity。

- approval 与 user input 是不同状态，不得共用含糊布尔值；
- AskUser 支持稳定问题 id、选项说明、多选和 Host 提供的自由输入；
- `/abort` 与退出在交互面板打开时仍必须可达；
- AFK resolution 使用 host-policy provenance，不渲染为用户回答；
- 交互完成前不得提前显示成功 ToolResult。

Plan 的退出确认可使用专用计划正文视图，但提交仍走通用 resumable tool/Session control 契约。

## 子智能体视图

SpawnAgent 工具记录携带类型化导航 identity。用户可从历史或 live 工具行打开 child Session，并通过键盘或
Agent tree 在主会话和多个 child 之间切换。

child surface 为只读：禁止普通输入、steer、follow-up、abort、fork、模型修改、审批策略修改和 destructive
Session 操作。root interaction inbox 中的 child 审批与 AskUser 仍由 controlled root 的交互面板处理；只读限制
不等于丢弃 HITL。不可见 child 继续运行，重新打开时使用 snapshot/high-water/cursor 衔接全部缺失事件。

## 渲染与性能

时间线分别呈现用户文本、assistant Markdown、reasoning、工具活动、压缩和 Run 状态。Provider delta 是输入事实，
不是 UI 重绘单位：TUI 按帧合并连续增量，保持到达顺序，并在完成边界冻结稳定块。

思考、工具调用（含子智能体）、压缩和 Run 的耗时统一通过 TUI 的 `format_duration` 呈现，实时与历史复用同一规则。
不足一秒显示整数毫秒；其余按天、时、分、秒进位，使用 `d/h/m/s`，秒保留一位小数。
省略前导零单位，从最高有效单位连续显示至秒，例如 `1h 0m 00.0s`、`1d 2h 3m 04.5s`；
包含分钟时秒补齐两位整数。按显示精度舍入后再进位，负耗时按零呈现，工具行继续隐藏舍入后为零的耗时。

强制约束：

- 事件 mailbox 有界；落后时回读持久化事实，不无限扩张内存队列；
- 文本分片以线性累积结构归约，避免每个 delta 复制完整前缀；
- 相同状态不重复构造 Rich 内容或触发 Textual layout；
- 用户离开底部后，流式事件不得抢回滚动位置；返回底部后恢复跟随；
- 历史只读 Markdown 不创建无期限存活的流任务；
- 文件 I/O、SQLite 查询、长历史投影和重 CPU 渲染准备不得长期占用 UI loop；
- 最近会话缓存、活动尾部、工具摘要和可见控件数量保持显式上界。

更细的流式、工具边界和响应性约束见[性能热路径规格](performance.md)。

### 事件与呈现边界

Host event sink 先注册完整 mailbox 再创建消费任务，任务直接持有所属 mailbox；该顺序兼容 Textual 的 eager
task factory。超时、失败和有界队列溢出按 sink 独立隔离，不能反向阻塞 durable event 或其他 Session。
Host 可用无 I/O 的同步 filter 在入队前选择事件，拒绝投递不影响持久化或其他消费者；filter 失败隔离到所属 sink。
TUI 只接收当前控制会话内容及子会话控制候选事件；子会话归属在线程中确认，消费时重新校验页面选择。
子会话控制事件包括 `run_status_changed`。工具预检可能先发布审批/输入请求，再保存批次检查点；根界面必须在
后续状态事件到达时重新查询权威交互，避免首次查询为空后永久漏显审批。此处不转发子会话高频正文流，也不增加轮询。
无关后台文本、reasoning 和工具流不占用 TUI sink mailbox；欢迎页没有控制会话时不接收会话事件。
ready message 只启动一个呈现 worker，不在 App 消息泵中等待解析、排版或查询，以保持按键分发可用。
worker 按帧取走有界事件前缀，按 sequence 更新投影并合并连续文本增量；工具与交互面板按规范快照刷新。
帧消费与显式完成排空共享顺序锁，覆盖取出前缀、异步读取和投影应用，后续事件不能越过仍在呈现的前缀。
工具 delta 与后续生命周期事件位于同一前缀时，在相位边界分开呈现，下一原生刷新帧再处理后缀。

工具名称形成即可显示活动行，参数片段只更新有界尾部。Assistant 的 reasoning 使用纯文本缓冲，answer 使用
独立源流；完整顶层 Markdown 块冻结，未闭合长尾部保持有界解析工作，完成后再收束。较大解析和语法高亮在线程
中处理不可变快照，UI loop 只提交捕获宽度对应的 strips，后台线程不得修改 Widget。

实时和重载使用 canonical MessageRecord 的 `role/scaffold` 决定可见性；事件类型表达来源，不能替代这些字段。
`scaffold=true` 不进入对话面，semantic tag 只影响模型包络。流式回归须在 Provider 仍被阻塞时验证可见源流，
不能仅检查最终 RunResult。

Catalog 错误条目单独标记，但不能据此推断 canonical Session 不可读取。有稳定 Session identity 时仍由
Application 尝试 observation；只有该读取失败才显示加载错误。删除使用 Application 的 Catalog-scope trash，
Host 不重复解释归档、health 或持久化目录。

## 本地偏好与草稿

TUI preference 与 canonical Session 数据分离。主题、reasoning 展开方式、草稿记忆等纯呈现偏好可由
`TuiPreferencesStore` 保存；保存失败可诊断但不得损坏 Session。关闭草稿记忆时必须清理已保存草稿。

偏好只接受当前主题标识和配置结构；未知主题明确报错，不自动映射已移除的主题。主题选择通过单一
Textual worker 合并为最新请求，revision 保护异步保存与渲染结果，避免过时结果覆盖当前选择。

输入草稿按稳定 Host/Session 语义保存，采用尾沿防抖并离开 UI loop 执行持久化。切换 Session、提交、清空或关闭
草稿记忆时，内存与磁盘状态必须一致，不能把一个 Session 的草稿带入另一个 Session。

磁盘草稿采用 300 ms 防抖，Host 关闭等待 pending 保存完成；内存始终持有最新修订。
TUI 批量拖入保存会话副本，`@` 目录多选添加原路径引用；`@` 复用与输入框衔接的 CommandPopup，输入框负责过滤，目录不显示多选标记。
聊天页和欢迎页在终端事件入口识别拖入路径，不要求输入框预先聚焦；导入后填入附件元素并自动聚焦输入框。
以主题色文件元素承载附件，整体删除并恢复原位置；排队列表及
`/unqueue` 菜单分别显示附件和引用数量。完整交互见[附件规格](attachments.md)。MCP 启动呈现连接状态与工具数量，
独立的运行期连接面板属于未来界面能力。

## 失败与恢复

- Application 命令失败时保留用户可修正的输入与选择，不先乐观提交 canonical 状态；
- 订阅 lag、Host sink 断开或页面暂时不可见时，以 SessionStore 高水位和 cursor 恢复；
- Markdown 或单个呈现组件失败不得篡改 Run 结果；可降级为有界纯文本诊断；
- 选择控制会话时只对遗留 running Run 请求恢复；queued 尚未取得执行锁，不能据此判为进程中断。
  Supervisor 已接管的任务保持原状态，任务启动恢复仍由既有协调器负责；
- TUI 关闭不等于 abort；shutdown policy 由 Application 明确执行；
- 私密凭据、完整工具秘密参数和第三方异常原文不得进入公开状态条或诊断页。

## 撤销输入与草稿

恢复输入包含文本、技能调用、会话上传项和原路径引用。TUI draft schema 4 一并保存文本、文件名与附件位置；只有附件的草稿也有效。
编辑文本、切换会话和启用草稿记忆的重启保留附件归属。接受输入后清除草稿；接受前失败与已有草稿合并恢复，
接受后发生模型或显示失败不重复恢复输入。排队撤回也合并保留新草稿。
Controller 不维护第二份恢复输入缓存，界面只通过 Runtime 执行撤销并重载标准观察投影。

撤销后的新 Run 事件使用当前 epoch；旧 Run 的迟到事件与结果保留原代次，不能恢复旧“准备上下文”状态
或读取已删除消息。

## 验收证据

当前契约由以下测试面保护：

- `tests/tui/test_app.py`：启动、输入、运行、交互与页面行为；
- `tests/tui/test_controller.py`：Application 映射、Session 观察与配置；
- `tests/tui/test_conversation_history.py`：历史、Markdown、reasoning 与滚动；
- `tests/tui/test_commands.py`、`test_keyboard.py`：命令和键位；
- `tests/tui/test_subagent_navigation.py`：controlled/visible Session 与只读 child；
- `tests/tui/test_preferences.py`、`test_input_drafts.py`：偏好和草稿；
- `tests/tui/test_question_forms.py`、`test_plan_confirmation.py`：AskUser 与 Plan 交互；
- Application、Session、Plugin、Tool 和架构测试：TUI 依赖边界与 durable 行为。

## 数据传输

`/export` 不接受参数，仅导出打开命令时的当前会话及其完整子会话树与附件，设置页固定会话范围；
未打开会话时提示先打开会话，不回退到全量导出。TUI 不提供 `/import` 命令。
其他数据操作从 Actions → Manage data 进入分页菜单；导出先选择数据类型，导入直接进入归档设置页，
无需指定类型。归档识别通过应用层读取并校验 manifest，在后台完成；识别前禁用导入，失败留在菜单内。
识别后显示只读类型及适用的范围、覆盖选项，更换归档清空之前的选择和覆盖设置。
Esc 逐级返回并保留上层选择。
归档文件及导出目录通过目录浏览菜单选择，导出提供默认文件名；
会话及原生技能支持全部或 `MultiChoiceScreen` 勾选，导入会话候选来自归档。
会话标题为主标签，ID 以右侧标签显示，不占用独立描述行，支持按标题或 ID 筛选。
路径浏览菜单允许按需编辑位置和文件名，但不要求填写命令 flags；没有 `/data` 别名或 CLI 参数解析入口。
Controller 调用所属 `LocalApplication` 的数据传输服务，复用当前 Session repository，不重新猜测后端。
执行由 App worker 持有，立即返回菜单交互；沿用主题切换的 `Choice.loading` 和原位刷新范式。
执行期间禁用重复提交，其他设置、导航、筛选与关闭保持可用；每次提交固定当时的请求，不受后续表单编辑影响。
进度、成功结果和错误留在菜单内，不写对话消息；关闭菜单不取消传输，重开显示当前操作状态。
CLI 与 TUI 共用业务请求及成功结果文案，参数交互分别由各 Host 适配。
导入不会切换当前会话、启动任务或连接新 MCP server；配置/技能提示重启 Host 后生效。
