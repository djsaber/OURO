# OURO TUI 视觉样式与 Markdown 渲染设计

> 文档类型：设计提案
> 设计基线：2026-09-02
> 适用实现：`ouro/tui/`

## 设计目标

OURO TUI 支持由 Host 调用方注入自定义主题。主题以完整、不可变、类型明确的 `TuiTheme` 表达，并统一作用于
Textual CSS、Rich 文本、assistant Markdown、代码高亮、结构标识符和状态动画。

内置主题提供零配置启动行为。Host 注入主题与内置主题经过相同的投影、渲染、缓存和降级路径，不依赖模块全局变量
修改或 monkeypatch。

TuiTheme 覆盖颜色、渲染样式、单 cell 标识符和 spinner 动画，不开放布局自定义。会话宽度、Widget 层级、Padding、
Margin、Border 形态、滚动和响应式规则由 TUI 固定管理。代码块和用户消息使用相同的 `panel` 背景，以统一 surface
语义。

欢迎页整体呈现由独立的 `WelcomeStyle` / `WelcomePage` 样式包提供，与 `TuiTheme` 配色并行配置。
样式包框架管理注册、选择与页面生命周期，包控制整个欢迎页的构图、组件树、布局与渲染方式。
Host 将持久输入层叠放于页面指定的位置，组件不随样式切换重建；系统与用量快照由 Host 提供，包按需使用公共
信息组件投影。居中布局是内置包的可选组合，点阵引擎仅属于使用它的艺术包。扩展入口见[欢迎页样式包开发](../development/welcome-styles.md)。

## 设计原则

### 内容事实与视觉投影分离

Session 中的用户文本、assistant Markdown、reasoning、工具状态和 Run 状态是展示输入。颜色、字重、背景和语法高亮
属于 Host 投影。样式不进入 canonical Message、Event、Run 或 ToolCall，也不改变 Application 提供的业务状态。

复制使用 canonical 文本或 TUI 选择语义，不包含颜色、背景、spinner 或 ANSI 样式。

### 主题是受约束的数据

TuiTheme 只表达有限且稳定的视觉 Token。它不携带 Python callback、Rich renderable、TCSS、插件 Hook、动态
import 路径或文件系统位置。

完整且不可变的主题值可以安全传入后台渲染、参与缓存兼容判断，并允许多个 App 实例彼此隔离。

### 展示语义由 OURO 拥有

标题、链接、reasoning、代码块、选择项、状态和 spinner 的语义角色由 OURO 固定。TuiTheme 为这些角色提供颜色、
文本强调、标识符和动画参数，不修改角色含义、间距、边框形态、Widget display 或布局。

Rich 和 Pygments 只负责解析与终端渲染，其默认颜色不构成 OURO 的视觉契约。

### 单一主题来源

一个 `OuroTuiApp` 在任一时刻只使用一份 `TuiTheme`。Textual CSS、mounted Widget、detached history render 和
Markdown/SyntaxTheme 均从该对象取得视觉语义。

`TuiTheme` 同时包含 identity、color、glyph、motion Token 和样式投影，是公共界面的视觉语义对象。主题应用不存在
并行状态、覆盖优先级或合并链。主题选择面板的 accent 预览是面板自身的临时视觉投影，不替换活动 TuiTheme，也不进入
其他 Widget、历史缓存或持久化配置。

## 范围

### 菜单布局与导航

内联命令菜单由 CommandPopup 保存候选项和对应的查询文本，按可见行生成终端 strip。上下移动只改变焦点与滚动位置，
补全使用同一份候选快照及预先计算的空输入提示；命令或查询文本不一致时，通过当前命令目录重新计算，保证同步编辑反馈。

Actions、会话、模型、Provider、插件、技能、任务及配置页面共用 ChoiceScreen 和 OptionList。
需要异步数据的子菜单在 `prepare()` 中取得初始候选后才进入屏幕栈；等待期间保留父菜单及其筛选、焦点和滚动位置，
父页面关闭或切换后丢弃迟到的打开请求。归档路径、认证状态和凭据列表遵循这一生命周期；直接打开这些菜单时也先准备候选。

菜单选择回调在 worker 中执行。`ChoiceScreen.loading_choice()` 按页面 revision 跟踪等待中的操作，使用现有
`Choice.loading` 行投影和主题 spinner Timer；临时状态不写入候选数据或页面历史。只有操作让出执行权后才发布动画，
即时选择和本地编辑不产生加载帧。同一页面等待中的选项不会重复提交，筛选、移动和关闭仍可用；选项被过滤或正在行内编辑时，
动画显示在固定高度的提示栏。已有主题切换、MCP 和数据传输操作继续拥有自身的加载状态。
配置操作由导航 worker 完整等待，提示覆盖实际读取、执行和保存过程。返回上级先恢复完整的缓存页面，再在后台刷新；
Tab 切换和全局菜单导航也不占用键盘事件处理。页面关闭或 revision 改变后，旧刷新结果不能替换当前页面。
行内保存只在编辑内容和凭据来源仍与提交时一致时结束编辑；等待期间的新输入保留，已保存的值更新为取消编辑时的基线。
批量确认完成后按当前页面更新候选，保留返回后的筛选和焦点，不重复执行返回动作或访问已卸载组件。

CommandPopup 在文件目录和模型目录读取期间保留候选快照，仅重绘选中行原有的单 cell 标记；查询输入保持可编辑，
完成后按当前查询发布候选，关闭菜单后不会被迟到结果重新打开。命令执行与普通消息的提交准备共用后台串行入口，
保证模型等配置命令完成后才接纳后续消息；等待期间切换会话的消息恢复到原草稿。没有可见菜单承载提示时，
复用输入栏旁的 ActivityIndicator 显示等待动画，并保留会话运行状态。加载结束、失败或取消后由组件清理临时状态和 Timer。

普通文本沿用 Textual 的文本布局；右侧状态列、主题色板及带状态的组标题使用禁止水平换行的单行表格，
高度由单元格中的显式换行决定。布局直接读取该高度，Rich 只渲染可见表格；宽度变化仍按实际列宽截断内容。
选项名称和右侧状态按内容分配宽度，空余空间用于两列之间的间距；空间不足时优先缩短较宽的一列，
避免长预览挤掉名称。单行编辑器的空输入提示按可用终端列数截断，输入内容通过跟随光标的水平窗口显示。
候选项替换、筛选、加载动画及行内编辑共用此布局边界，保留 OptionList 的焦点、分组滚动和主题投影。
菜单可在标题下显示次要文本说明，与筛选和可选操作分离；插件管理页用它展示插件简介。说明使用纯文本，
最多占三行；窄屏高度不足时优先保留操作区，无说明时不占空间。说明随菜单页面的进入和返回同步。
插件和静态 Provider 共用凭据行内编辑器；Enter 直接编辑，Tab 在编辑期间切换 KEY / ENV，右侧固定显示类型。
两种类型分别保留本次编辑的输入，Enter 保存当前类型，Esc 撤销本次编辑。已有凭据显示固定长度的遮蔽符
和 `[KEY]` / `[ENV]`，未配置项保持空白；编辑输入也遮蔽，类型标签不占用光标滚动区域。
各自配置用例解释绑定、保留和持久化语义；非凭据编辑及菜单导航沿用原有按键。必填标签仅提示缺失项。

### 支持

- Host 在创建 TUI 时注入一份完整 TuiTheme；
- 内置主题与 Host 注入主题共用同一类型；
- Textual CSS variables 使用活动主题；
- Rich Text 使用活动主题；
- Markdown 命名样式使用活动主题；
- SyntaxTheme 由活动主题派生；
- brand、separator、choice、status 和 disclosure 标识符使用活动主题；
- spinner 帧序列和刷新间隔使用活动主题；
- history worker 捕获不可变主题；
- history Strip 缓存区分不同主题值；
- NO_COLOR 保留非颜色结构语义。

### TuiTheme 不支持

- width、height、Padding、Margin、Border 形态、对齐、滚动和响应式断点自定义；
- Widget 树或会话正文宽度自定义；
- 插件贡献 Theme、Widget、Markdown element 或 TCSS；
- Python renderer、第三方 SyntaxTheme 或可执行主题注入；
- renderer registry、UI DSL 或 `entry_point`；
- 运行中热加载、文件监听和按 Session 使用不同主题；
- 多级继承、样式合并链、命名 profile 目录或远程主题；
- 多 cell、换行、ANSI 或控制字符标识符；
- 通用关键帧、缓动、转场、动画脚本或 Widget 专用动画回调；
- TUI 主题写入 Session、Application DTO 或 Runtime Event；
- 独立的 Markdown stream、重复的历史缓存或主题后台服务。

## 总体结构

```text
TuiTheme ──> OuroTuiApp ──┬───> Textual CSS variables
                          ├───> mounted Widget Rich Text / glyphs
                          ├───> spinner frames / cadence
                          └───> Markdown adapter ──> Rich Markdown / SyntaxTheme

TranscriptEntry + TuiTheme + render width
  ──> Rich renderable
  ──> Textual Strip
  ──> ConversationHistory cache
```

主题系统完全位于 `ouro.tui`。Application、Core、Session、Provider、Tool 和 Plugin 不依赖 TUI 主题类型。

## 主题模型

### TuiTheme

`TuiTheme` 是 frozen、slots dataclass，保存主题身份和完整视觉 Token：

| 类别 | Token |
|---|---|
| identity | `theme_id`、`label`、`description` |
| surface | `background`、`panel`、`highlight`、`input_highlight` |
| border | `progress_track`、`border_focus`、`separator_tone`、`border`、`scrollbar_hover` |
| text | `text_disabled`、`text_faint`、`text_lowlight`、`text_metadata`、`text_detail`、`text_choice`、`text_muted`、`text_secondary`、`text_primary`、`text_highlight` |
| accent | `accent`、`accent_bright` |
| status | `running`、`success`、`warning`、`warning_bright`、`danger`、`failure`、`critical` |
| syntax | `syntax_comment`、`syntax_keyword`、`syntax_string`、`syntax_number`、`syntax_operator`、`syntax_name`、`syntax_error` |
| glyph | `section_separator_mark`、`inline_separator_mark`、`brand_mark`、`mask_character`、`selected_choice_marker`、`unselected_choice_marker`、`active_item_marker`、`completed_item_marker`、`pending_item_marker`、`session_node_mark`、`selected_session_node_mark`、`expanded_disclosure_marker`、`collapsed_disclosure_marker` |
| motion | `spinner_frames`、`spinner_interval_seconds` |

每个字段使用组件无关的语义名称。组件专用名称、特定语言 token 和渲染器实现名称不进入 TuiTheme。
`accent` 用于操作、链接、inline code 和活动状态，`accent_bright` 用于焦点和高亮操作。

会话栏中的思考标记、思考正文和普通工具名称统一使用 `text_lowlight`；思考正文保留 italic，不叠加 dim。
模型正文使用 `text_primary`，Markdown 内容保留各自的语义样式。失败状态继续使用错误色，悬浮高亮与点击语义保持一致。
压缩摘要与运行摘要使用常规字重。短命令反馈复用 `brand_mark` 加消息文本的提醒结构；多行命令内容使用纯文本代码块，
复用 Markdown 代码块的 SyntaxTheme、panel 背景、换行与内边距，内容中的 Markdown 标记按原文显示。

颜色只接受规范化 `#RRGGBB`。透明度、ANSI 名称、渐变和任意 Rich style string 不属于主题契约。bold、italic、
underline、dim 等渲染样式由 OURO 根据主题语义建立，不作为可执行字符串存入主题。

### Token 约束

- `theme_id`、label 和 description 是非空字符串；内置主题的 theme_id 唯一；
- 所有 glyph Token 是非空字符串，不包含换行、控制字符或 ANSI 序列，终端显示宽度严格为一个 cell；
- section 和 inline separator 的两侧空格由 TuiTheme 派生属性生成，不存入 marker Token；
- selected/unselected choice marker 与 expanded/collapsed disclosure marker 分别可区分；
- spinner_frames 是非空、元素唯一的 tuple，每一帧满足 glyph Token 约束；单帧 tuple 表示静态状态；
- spinner_interval_seconds 是有限浮点数，范围为 0.05 至 2.0 秒；
- TuiTheme 的相等性覆盖全部字段，颜色、glyph 或 motion Token 的差异都形成不同主题值。

`DEFAULT_TUI_THEME` 是完整的默认实例，`TUI_THEMES` 是只读内置主题映射，其中每个项也是完整
TuiTheme。Host 可以通过 dataclass replace 构造完整的自定义主题：

```python
from dataclasses import replace

from ouro.tui import run_tui
from ouro.tui.theme import DEFAULT_TUI_THEME

custom_theme = replace(
    DEFAULT_TUI_THEME,
    theme_id="graphite",
    label="Graphite",
    description="Low-noise neutral palette",
    text_primary="#e2e8f0",
    text_muted="#94a3b8",
    accent="#67b7d1",
    syntax_keyword="#7aa2c7",
    brand_mark="◆",
    selected_choice_marker="■",
    spinner_frames=("◐", "◓", "◑", "◒"),
    spinner_interval_seconds=0.1,
)

run_tui(theme=custom_theme)
```

## 组合与注入

`run_tui()` 与 `OuroTuiApp.__init__()` 接受同一个可选 `theme: TuiTheme | None` 参数。None 根据 TUI preference 从
`TUI_THEMES` 选择主题；显式值作为 App 的启动主题。App 通过只读 `tui_theme` 属性公开活动主题。

自定义主题入口是 Python API。自定义主题不自动写入 `TUI_THEMES` 或 `tui.json`。设置项 `THEME` 列出并持久化内置
主题 ID；用户选择内置主题时替换活动主题。显式注入值由 Host 管理。

`theme_for(node)` 是唯一的 Widget 主题查询入口：

- mounted Widget 读取 owning App 的 tui_theme；
- 显式接收启动主题的 pre-mount 配置界面使用该主题，其他 pre-mount 或独立测试节点使用
  DEFAULT_TUI_THEME；
- 不读取环境变量、文件或模块可变状态；
- 不缓存 Widget 到 App 的关系。

纯投影函数显式接收 TuiTheme。后台渲染请求捕获 TuiTheme 值，不在线程中回读 App。

## CSS 与 Widget 样式

`TuiTheme.css_variables()` 将语义颜色投影到 Textual CSS variable：

- `background` 对应 canvas；
- `panel` 同时对应用户消息和代码块 surface；
- text token 对应各层级文本；
- border token 对应边框颜色；
- accent/status token 对应交互与状态颜色。

App 的 CSS variables 全部由活动 TuiTheme 提供。TCSS selector、spacing 和 geometry 是固定代码，不属于主题。

mounted Widget 使用 `theme_for(self)` 构造 Rich Text、标识符和 spinner。主题读取不依赖模块变量赋值。

## 标识符与动画

brand、separator、choice、activity、completion、session node、disclosure 和 mask 均从活动 TuiTheme 读取。语义角色由
组件确定，主题只提供角色对应的单 cell glyph。`section_separator`、`inline_separator` 和 `idle_mark` 由 marker Token
纯派生，调用方不拼接主题外的替代字符。

spinner 组件使用 `spinner_frames` 按顺序循环，并以 `spinner_interval_seconds` 作为刷新间隔。TuiTheme 只声明帧与
节奏；Timer 的创建、暂停、恢复、停止和刷新仍由 Widget 生命周期管理。单帧 spinner 不创建周期 Timer。

活动主题切换时，mounted spinner 重置到第零帧。刷新间隔发生变化时，仅为处于活动状态的 spinner 重建 Timer。
elapsed clock、Provider delta、历史渲染和其他后台任务不读取 spinner_interval_seconds。

## 文本断行边界

`text_wrapping.py` 将 Unicode 断行单位适配为 Rich 的源字符偏移。`unicode-segmentation-rs` 通过原生
Rust 扩展提供 Unicode 断行规则，Rich 提供终端 cell 测量、字形安全的超长单位折行及样式切分；
TUI 不维护 Unicode 字符分类表。
`ProseText` 通过 Rich `Text.wrap` 扩展点应用这些边界，ASCII 文本及非折行模式复用原生行为。
Markdown 通过局部 element 映射为段落、标题和表格单元格提供 `ProseText`，列表、引用和代码结构继续
由 Rich 负责。流式尾部与冻结正文使用相同的正文断行策略。

`wrapped_document.py` 封装 Textual 8 的文档显示几何适配。Textual 未提供断行策略回调，因此适配器
集中维护其断行与行索引缓存，复用原有文档、编辑历史和导航协议。断行和光标映射使用相同的制表符宽度；
框架内部字段的依赖限制在该适配器内，由编辑、缩放、选区及光标往返测试验证。适配不修改依赖全局状态，
不改变业务层文本，也不在渲染链路执行 I/O。

每次断行按当前源行长度线性扫描，适配层不缓存完整文本或分词结果。文本编辑只对受影响的源行重新分词，
其他行复用断行偏移；流式追加只处理未完成的显示行及新增片段。适配层复用 Rich 的有界字宽缓存，
不增加随会话增长的分词缓存。性能测量入口为 `dev/benchmarks/text_wrapping.py`。

## Markdown 适配

`ouro/tui/markdown.py` 负责把 canonical assistant Markdown 与 TuiTheme 转换为 Rich renderable。该模块拥有：

- assistant Markdown factory；
- Markdown 渲染失败的纯文本降级。

Rich `markdown.*` 命名样式和 SyntaxTheme 由 TuiTheme 的纯投影方法提供。markdown.py 只消费投影结果，不定义第二份
颜色或样式语义。该模块不读取 Session、Application、文件、环境变量或 Widget 状态。

`conversation_history.py` 负责 TranscriptEntry、Padding、interaction metadata、Strip 转换和缓存，不定义 Markdown
颜色或 Pygments theme。

完成块、重新加载历史和后台批量渲染调用同一个 Markdown factory。活动 Markdown tail 使用追加式纯文本，在稳定块
边界进入最终 Markdown factory。

### Markdown 语义映射

| Markdown 语义 | 样式 |
|---|---|
| paragraph | `text_primary`，无额外背景 |
| h1/h2/h3 | `text_primary` + bold |
| h4/h5/h6 | `text_secondary` + bold |
| strong | bold，继承当前前景色 |
| emphasis | italic，继承当前前景色 |
| inline code | `accent`，无独立背景 |
| block quote | `text_muted`，使用 OURO 定义的引用结构 |
| list marker | `text_muted` |
| list body | 继承 paragraph |
| link | `accent` + underline，保留 hyperlink metadata |
| horizontal rule | `separator_tone` + dim |

`TuiTheme.rich_theme()` 提供以上映射。bold、italic、underline 和 block 结构是固定投影规则，主题不接受任意 Rich style
string 或对应开关。

## 代码高亮与背景

代码块和用户消息统一使用 `TuiTheme.panel`。主题的 panel 同时作用于两者。

`TuiTheme.syntax_theme()` 将 Pygments token 收敛到稳定的主题 syntax token：

- comment -> `syntax_comment`；
- punctuation/operator -> `syntax_operator`；
- keyword/builtin -> `syntax_keyword`；
- string -> `syntax_string`；
- number/constant -> `syntax_number`；
- error -> `syntax_error`；
- name/text -> `syntax_name`；
- background -> `panel`。

Syntax token 与 accent/status token 分离。代码字符串、数字和关键字不借用 success、warning 或 running 状态色。

SyntaxTheme 不区分 Python、JSON、Shell 等语言的视觉策略。lexer 只负责 token 分类；未知 lexer 使用 plain text，并保持
panel 背景。

## 生命周期与缓存

OuroTuiApp 在任何历史渲染 worker 启动前确定活动 TuiTheme。Rich Markdown style 与 SyntaxTheme
由主题值惰性投影并有界缓存，每个等值主题只构造一份投影。单个 TuiTheme 值不可变；主题切换通过替换
活动值完成，worker 只读取请求捕获的主题。

`_EntryRenderRequest` 捕获：

- TranscriptEntry；
- previous entry kind；
- render width；
- TuiTheme；
- reasoning 展开与交互高亮状态。

`ConversationHistoryState` 保存生成 Strip 所使用的完整 TuiTheme。恢复缓存需要同时满足：

- render width 相同；
- reasoning 展开状态兼容；
- TuiTheme 值相同；
- entry boundary 相同。

不同 App 或不同 TuiTheme 值不共享 Markdown style、SyntaxTheme 或 history Strip。theme id 只用于身份和 preference，
不能替代完整主题值参与缓存判断。实现不使用全局 mutable current theme、ContextVar 或线程局部主题。

TUI 主题选择面板将高亮项的 `accent` 直接投影到面板标题、输入焦点轨道和选项高亮前景。该投影只更新少量已 mounted
样式，不调用 App 主题切换、全局 CSS refresh、Widget 树遍历、Markdown 投影或历史重渲染。离开主题页时，面板局部投影
随页面生命周期清除。

确认选择后，选项进入 loading 状态，并复用 ChoiceScreen 已有的主题 spinner 帧与 Timer。选择回调只登记请求并立即返回，
不等待文件 I/O 或历史渲染，因此筛选、移动、再次确认和关闭面板始终可交互。

主题切换由 `OuroTuiApp` 拥有的单一合并式 worker 协调：

1. 请求使用单调 revision 标识，pending theme 始终表示最后一次有效选择；
2. TUI preference 更新共享异步串行边界，各更新在取得边界后从最新 preference 构造完整快照；实际文件写入通过
   `asyncio.to_thread` 离开主循环，避免不可取消线程 I/O 乱序落盘或覆盖同时发生的其他设置更新；
3. 写入完成时若 revision 已过期，不应用对应主题，直接处理最新 pending theme；
4. 最新请求写入成功后替换完整 TuiTheme，刷新 CSS variables 与 mounted Rich 内容；
5. conversation history renderer 在后台生成最新主题 Strip，主循环只原子替换有效结果；
6. mounted 内容与历史渲染均完成后清除 loading；若期间出现新请求，则等待被立即打断并转入最新请求；
7. 面板只观察切换状态，不拥有 worker。返回上级页面或关闭 Modal 不取消 preference 写入和主题应用。

主题切换的完成边界是活动主题已经替换且当前 ConversationHistory 的合并式主题渲染到达 idle。过期历史结果继续由
ConversationHistory revision 规则丢弃，不建立第二套渲染或取消协议。

## 失败语义

- 非法主题身份、颜色、glyph、spinner 帧或刷新间隔在 TuiTheme 构造边界失败；
- 内置主题无效属于开发错误，由测试阻止发布；
- 单个 Markdown block 失败时降级为有界纯文本；
- 未知 lexer 降级为 panel 背景上的 plain text；
- 降级不改变 Run、消息顺序或后续条目；
- 主题 preference 写入失败时保留当前活动主题、清除最新请求的 loading，并显示可诊断错误；
- 被后续选择覆盖的请求不提交活动主题，也不覆盖最新选择的交互状态；
- 诊断不包含完整 prompt、reasoning、工具参数或敏感内容；
- 主题错误不写入 canonical Session，也不触发 Runtime event 重放。

## 性能约束

- Rich Markdown style 和 SyntaxTheme 按 TuiTheme 值惰性构造并使用有界缓存；
- 渲染期间只读活动主题及其投影；
- Provider delta 处理频率和每帧 event budget 不受主题影响；
- 未完成 tail 不执行完整 Markdown render；
- 历史 Markdown block 不创建长期存活 Widget；
- 复杂 Markdown 和批量 immutable transcript 在 worker 渲染；
- 复用 conversation history renderer、revision 丢弃规则和 Strip cache，不创建主题级后台服务；
- 主题预览仅修改选择面板的局部 accent 样式，不触发 CSS 全局计算、Widget 树刷新或 history worker；
- 主题确认只保留一个 App 级切换 worker，TUI preference 文件写入共享串行边界且离开主循环，连续请求按 revision
  合并到最后一次选择；
- 等待真实主题渲染期间可被新请求唤醒，不轮询、不阻塞主循环，也不创建额外周期 Timer；
- spinner 复用各 Widget 的生命周期 Timer，不建立主题级 scheduler；
- 非活动 spinner 暂停或停止 Timer，单帧 spinner 不启动 Timer；
- CSS 只替换 variable 值，selector、布局计算和 resize 分支由固定布局负责；
- 同一宽度、entry 和 TuiTheme 值的不可变消息复用缓存的 Strip。

## 模块职责

| 模块 | 职责 |
|---|---|
| `ouro/tui/theme.py` | 完整 TuiTheme、Token 校验、内置主题、CSS/Rich/Syntax 投影与 theme_for |
| `ouro/tui/markdown.py` | 消费主题投影，建立 assistant Markdown renderable 并处理降级 |
| `ouro/tui/conversation_history.py` | 捕获 TuiTheme、生成 Strip、维护缓存 identity |
| `ouro/tui/app.py` | 接收和切换 TuiTheme，应用 CSS variables，协调主题刷新 |
| `ouro/tui/entry.py` | 将 Python API 的 theme 参数传给 App |
| `ouro/tui/widgets.py` 与其他 TUI 组件 | 从 owning App 取得主题并构造 Rich Text、glyph 与 spinner |
| `tests/tui/test_preferences.py` | 内置主题以及 color、glyph、motion Token 校验 |
| `tests/tui/test_conversation_history.py` | Markdown、SyntaxTheme、panel 背景、降级和缓存隔离 |
| `tests/tui/test_app.py` | CSS/Rich/glyph/motion 主题一致性、App 隔离、完整主题切换和布局隔离 |

`ouro.application`、`ouro.core` 以及 Session、Runtime、Provider、Plugin 领域模块不依赖 TuiTheme；
只有 `ouro.tui` 内的对应界面适配代码消费主题。

## 验收条件

### 自定义能力

- 调用方通过完整 TuiTheme 启动 OURO，无需修改模块全局变量；
- 内置主题与 Host 注入主题经过相同渲染路径；
- Host 注入主题同时影响 Textual surface、Rich Text、Markdown、代码高亮、标识符和 spinner；
- 主题切换一次性替换完整视觉主题；
- 不同 App 和不同主题值的历史缓存彼此隔离。

### 视觉语义

- Markdown 颜色全部来自 TuiTheme；
- H1-H6、inline code、blockquote 和 list 的颜色由 Markdown 语义映射提供；
- 代码高亮颜色来自 TuiTheme 派生的 SyntaxTheme；
- 代码块和用户消息使用相同 panel 背景；
- syntax token 不借用运行状态色；
- brand、choice、status、session 和 disclosure 标识符全部来自 TuiTheme；
- spinner 帧和刷新间隔全部来自 TuiTheme；
- NO_COLOR 通过 bold、dim、underline 和文本保留结构；
- Widget 尺寸、间距、滚动和响应式行为独立于主题。

### 正确性与性能

- canonical Markdown、复制文本、消息顺序和交互 metadata 独立于主题；
- streaming tail、冻结 block 和重新加载历史的最终文本一致；
- CJK、组合字符和全角标点的 cell 测量只依赖文本与终端宽度；
- 自定义 glyph 和 spinner frame 始终占用一个 terminal cell；
- 主题切换重置 spinner frame，并只重建活动 spinner 的 Timer；
- 主题预览只改变主题面板 accent，不改变活动主题、全局内容或 preference；
- 主题确认期间选项显示 loading，面板保持可交互，连续选择只提交最后一次请求；
- 关闭主题面板不取消正在执行的主题切换；
- loading 在完整主题应用及最新 history render 完成后移除；
- Markdown/theme 失败只降级当前 block；
- 历史复杂 Markdown 在 worker 渲染；
- Application/Core 不依赖 TUI 或 Rich；
- TUI 热路径、架构和行为测试通过。

视觉验证覆盖默认主题和一份高对比 Token 测试主题，包括标题、inline code、Python/JSON/未知语言代码块、引用、列表、
表格、reasoning、用户消息、工具状态、composer、选择器、自定义 glyph 和 spinner。视觉画面验证 Token 来源，行为测试
验证 canonical 文本、metadata、缓存、Timer 生命周期和失败语义。

## 演进边界

Host 自定义主题的文件持久化、主题包、字重与边框形态自定义、布局自定义和 Desktop 共享 schema 分别属于独立需求。
每项能力需要真实使用场景和独立契约，不预置继承链、目录发现、热加载、通用 renderer registry、关键帧或动画 DSL。

插件任意 Widget、可执行主题、原始 TCSS 和 Host UI DSL 不属于 TUI 主题能力。

## 相关文档

- [OURO TUI 规格](../specs/tui.md)
- [运行热路径性能规格](../specs/performance.md)
- [OURO 开发架构](README.md)
- [OURO 编码准则](../development/coding-guidelines.zh-CN.md)
