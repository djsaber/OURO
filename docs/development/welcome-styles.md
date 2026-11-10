# 欢迎页主题开发

欢迎页框架位于 `ouro/tui/welcome/`。`WelcomeStyle` 描述单个主题的身份与页面工厂，`WelcomePage`
是覆盖整个欢迎页视口的 Textual 根组件。主题负责页面构图、背景、装饰、信息组件和响应式布局；
Host 负责业务状态、持久输入组件、菜单、主题选择与生命周期。页面内部设置、动画和资源策略由主题自行实现。
呈现与配置契约见 [TUI 规格](../specs/tui.md#欢迎页样式)。

## 模块与发行边界

基础发行包 `ouro-agent` 包含欢迎页框架、公共组件和静态 `basic` 页，不包含可选引擎或风格实现。
可选主题扩展发行包同时提供计算引擎、官方风格和它们的依赖；用户通过
`ouro-agent[themes]` 整体安装，没有独立的引擎安装选项。

| 位置 | 职责 |
|---|---|
| `ouro/tui/welcome/` | 主题与主题包契约、宿主、只读目录、公共组件与 basic 回退页 |
| `ouro/tui/welcome_packages.py` | 检查官方扩展发行版本并加载轻量注册信息，由 App 与显式注入包组合 |
| `packages/tui-themes/src/ouro_tui_themes/tools/` | 主题无关的数值计算、终端编码和 Textual 调度组件 |
| `packages/tui-themes/src/ouro_tui_themes/packs/` | 主题包元信息，以及包内各主题的页面、算法和交互 |

框架不导入扩展实现。主题消费欢迎页公开接口，并按需复用扩展自带的计算、终端编码与调度组件；
这些组件不依赖欢迎页契约或具体主题。宿主的 `ThemePackage` 将多个 `WelcomeStyle` 组织为
带名称、简介和作者的集合，加载器直接读取扩展的 `packs.PACKAGES`。包内组织和新增入口见
[扩展包说明](../../packages/tui-themes/README.md#organization)。

## 页面与输入的边界

`WelcomeHost` 的两个直接子节点是主题页面和持久的 `#interaction-surface`。页面占满视口，输入层
位于其上方；选择器与帮助等 Host Screen 位于两者上方。输入层包含 composer、状态栏、排队输入和交互面板。
它在切换样式、进入会话和返回欢迎页时保持同一个实例，保留草稿、附件、历史和光标。

主题通过以下接口参与布局和数据投影：

| 接口 | 契约 |
|---|---|
| `place_input(viewport, measure) -> Region` | 返回页面坐标系内的输入矩形；宽度必须为 `1..viewport.width`，矩形须非空且位于视口内 |
| `measure(width) -> int` | 使用 Textual 按指定宽度测量公共输入层，包含其 padding/border；高度限于视口 |
| `input_region` | Host 写入的实际输入矩形；主题读取它安排相邻内容，输入增长也会使依赖布局失效 |
| `set_system_status(status)` | 接收不可变的 `TuiSystemStatus` 快照，供主题投影工作区、用量等信息 |
| `set_active(active)` | 接收页面是否处于前台的通知，主题自行管理资源 |
| `tui_theme` | 只读的当前 `TuiTheme`，包含已应用的主题色和饱和度 |
| `refresh_theme()` | 挂载后及配色变化时更新主题自行绘制的内容，隐藏的已挂载页面也接收通知 |

`place_input` 是同步、无 I/O 的布局函数，不修改组件或读写业务状态。主题使用 `measure` 返回的高度安排输入，
并为短终端留出可用位置。Host 将命令弹窗紧贴输入区上方，高度受上方可用空间限制；
仅上方不足最小菜单高度且下方空间更大时向下展开。

页面可以实现 `render()`、`render_line()`，也可以通过 `compose()`、TCSS 或 Textual `Layout` 组合任意子组件。
全屏字节瀑布可以直接绘制页面每一行，背景会覆盖输入区之外的全部终端；主题也可以把输入放在顶部、底部或侧栏。
页面内部的布局缓存随 `input_region` 变化失效，绘制帧不需要触发布局。

## 创建与注册

工厂每次必须返回新的、未挂载的 `WelcomePage`。以下页面将字节场铺满视口，并将公共输入区放在底部：

```python
from collections.abc import Callable

from rich.segment import Segment
from textual.geometry import Region, Size
from textual.strip import Strip

from ouro.tui.entry import run_tui
from ouro.tui.welcome import ThemePackage, WelcomePage, WelcomeStyle


class BytePage(WelcomePage):
    def place_input(self, viewport: Size, measure: Callable[[int], int]) -> Region:
        width = viewport.width
        height = measure(width)
        return Region(0, viewport.height - height, width, height)

    def render_line(self, y: int) -> Strip:
        line = "".join(f"{(x * 7 + y) % 16:X}" for x in range(self.size.width))
        return Strip([Segment(line, self.rich_style)])


byte_page = WelcomeStyle(
    style_id="byte-page",
    label="Byte page",
    description="A full-page byte field with a bottom input surface.",
    create=BytePage,
)

byte_package = ThemePackage(
    package_id="byte-art",
    name="Byte Art",
    description="Procedural character fields.",
    author="Example author",
    themes=(byte_page,),
)

run_tui(welcome_packages=(byte_package,))
```

`OuroTuiApp` 接受相同的 `welcome_packages` 参数。每个 App 将这些包与已安装的扩展组合，并索引包内主题和 basic；
重复包 ID 或主题 ID 在组合边界报错。`Theme` 菜单的 `Welcome style` 分组展示 basic 和有可用主题的包。
进入包后显示简介、作者及可用主题；选择主题后保留当前菜单，`Esc` 返回并恢复父页筛选和选中位置。
保存自定义主题后，后续启动需要注册同一 ID 才能显示原页面。配置只校验 ID 格式；未找到的已保存 ID 保留并提示，实际显示 basic。
新的主题选择必须存在于当前可用目录。JSON 只保存身份与呈现偏好，不加载 Python 路径或执行代码。

## 可选主题依赖

`ouro-agent[themes]` 是欢迎页主题拓展的统一安装入口，安装兼容版本的可选主题扩展发行包及其声明依赖。
基础安装默认显示 `basic`，TUI 其余功能完整可用。App 先检查扩展发行版本，再加载注册信息；
扩展未安装或版本不匹配时不导入实现。核心与官方扩展独立版本演进，扩展声明支持的核心版本范围。
主题是完整页面，可以静态展示，也可以自行实现动画；TUI 不提供主题内部设置入口或设置传递管线。

`WelcomeStyle` 的元数据和工厂必须能在缺少可选依赖时导入。需要可选依赖的主题通过
`check_requirements` 提供检查函数：可用时返回 `None`，否则返回含修复说明的原因。
App 在启动时执行检查，使用相同函数的主题共用一次结果；不做运行中的环境扫描或依赖安装。
工厂在选用主题时加载渲染实现，依赖检查只处理其明确负责的依赖错误，不吞掉主题代码的编程错误。

偏好保存的选择与本次实际显示分开解析。所选主题未注册、扩展被卸载或依赖暂不可用时，显示基础页并
提示原因，保留用户选择和其他偏好；配色偏好的写入也保留该身份。恢复扩展与依赖并重启后恢复原选择。
完整配置与回退契约见 [TUI 规格](../specs/tui.md#欢迎页样式)。

开发时使用 `uv sync --frozen --all-groups --extra themes` 安装 workspace 中的核心与扩展。
发布时维护变更发行包的版本，并核对两个 `pyproject.toml` 的兼容范围，运行 `uv build --all-packages` 构建发行包。
`themes` extra 所要求的扩展版本须已发布；未变更的发行包无需重复发布，基础 wheel 不携带扩展源码。

## 公共组件与局部样式

`welcome/components.py` 提供 `WelcomeSystem`、`WelcomeUsage`、`WelcomeTips` 和 `WelcomeVersion`。
主题按需组合这些组件，或者自行投影收到的状态；使用 System/Usage 时，由页面的 `set_system_status`
转发快照。组件不访问服务或持久化存储。公共组件保留 OURO 信息排版与用量悬停交互。

`CenteredWelcomePage` 是主题复用的可选页面组合：它安排艺术区、居中输入、用量、提示、系统信息和版本。
116 列上限、居中规则和区块间距属于这个组合，通用 Host 不限制页面宽度、艺术区域或信息区顺序。

字符背景与前景组件遵循 Textual 的矩形合成规则：`background: transparent` 只混合背景颜色，
不会让组件输出的空格穿透到父页面的字符画。全屏字符背景主题应直接使用 `WelcomePage`，
按内容布置前景，以布局坐标留出间距，避免用大块空白组件占位。需要复用嵌套信息组件时，
可在主题局部 CSS 中将纯布局容器设为 `visibility: hidden`，将文字子节点显式设为
`visibility: visible`，并约束文字节点的宽度；容器继续参与布局，文字保留正常绘制和鼠标交互。
Matrix 使用这一方式。Host 负责全屏范围、输入层和生命周期，主题负责内容布局与遮挡范围，
无需替换全局合成器或每帧重新布局。

默认 TCSS 作用于主题自身的组件树。欢迎页激活时，Host 具有 `welcome-style-<style_id>` 类；离开欢迎页或切换主题
会撤销该类。需要调整公共输入层的外观时，主题可以设置 `SCOPED_CSS = False`，并让所有跨节点规则以该类为边界：

```css
#input-region.welcome-style-byte-page #interaction-surface {
    background: $ouro-panel;
    padding: 0 1;
}
```

公共输入层是与页面并列的持久节点，因此跨节点规则需要关闭 Textual 的组件类型自动作用域。
规则应限定当前主题，不能修改其他页面、菜单或会话视图；保留输入层的可见性、键盘行为与自动高度。

## 联动 TUI 配色

页面通过 `self.tui_theme` 读取当前不可变的 `TuiTheme`。其中 `theme_id` 标识配色，`saturation` 是当前
饱和度，`accent`、`background`、`panel`、`text_primary` 等已经是应用饱和度后的颜色；不要重复变换颜色。
在挂载后或 `refresh_theme()` 内读取；无法取得 App 配色时返回默认配色，不应在构造阶段缓存它。

TCSS 可直接引用 `$ouro-theme`、`$ouro-background`、`$ouro-panel`、`$ouro-text-primary` 等变量，
配色切换时自动刷新。自行生成的 Rich 文本、终端行或绘制缓存通过 `refresh_theme()` 更新：

```python
from rich.text import Text
from textual.widgets import Static

# 以下方法位于已组合 Static(classes="caption") 的 WelcomePage 子类中。
def refresh_theme(self) -> None:
    palette = self.tui_theme
    self.query_one(".caption", Static).update(Text("My scene", style=palette.accent))
```

Host 在首次挂载和替换页面后调用此钩子；App 在配色或饱和度实际变化时通知所有已挂载页面，包括被菜单
遮盖和进入会话后隐藏的页面。刷新不重新创建页面，不改变其激活状态、输入草稿或焦点。主题只更新自己消费的
颜色及缓存，不能借配色通知启动动画。子组件也可使用 `ouro.tui.theme.theme_for(self)` 和同名刷新钩子。
主题自行决定哪些视觉元素联动系统配色，也可以保留独立艺术光谱。

## 动画与资源

Host 在离开欢迎页、打开覆盖菜单或替换页面时调用 `set_active(False)`，返回当前欢迎页时调用
`set_active(True)`。主题自行决定播放策略；动画主题应在隐藏时暂停逐帧计算，避免占用交互资源。
静态帧绘制独立于动画播放：菜单打开期间切换主题或调整终端尺寸，也应立即显示当前尺寸下的作品。
`on_unmount()` 释放定时器、任务、缓冲与其他自有资源。主题不抢占输入焦点，不写入会话事实。

按动画需要刷新实际变化的组件，避免每帧 `refresh(layout=True)` 或重建组件树。布局使用内存快照，帧计算不做文件
或网络 I/O；异步资源的取消与回收由创建它们的主题负责。布局、状态投影与动画帧是独立的更新链路。

内置动画在扩展包的 `tools/rendering.py` 共用帧表示与终端适配。生产者发布单 cell 字符段列表，以及
RGB 整数或共享色板 Style 列表；每行拥有独立快照，发布后不再修改，不借用数值 worker 的可变数组。
`FrameStrip.from_runs` 接管两份列表的所有权。样式继承、裁剪、合成切分与真彩色 ANSI 编码沿用这份
紧凑表示，不逐段构造 Segment、Color 或临时 Style，也不把逐帧混色保存在全局 ANSI 缓存中。
稳定的画布属性和背景最多缓存 32 组。普通 Rich 消费者通过 `_segments` 按需取得等价对象；
宽字符行和控制段使用原生 Rich 切分，装饰、复杂滤镜和降色保持兼容路径。
短寿命行仅按需创建 Textual 变换缓存；已是 RGB 且无 dim 的行跳过默认滤镜的重复拷贝。
无装饰画布直接应用继承样式和当前 Textual 滤镜；边框、padding、outline、透明度、tint、
hatch 和链接悬停仍使用 Textual 原生 CSS 路径。适配只作用于主题自己的动画行，不修改全局 Strip、
合成器或 GC，也不进入 Host 扩展协议。依赖 Textual 8 的行存储字段集中在该文件；升级依赖时运行
`test_animation_rendering.py` 的原生合成对照，包括所有主题、颜色模式、窗口尺寸和宽字符。

## 验证

针对欢迎页的回归入口：

```bash
uv run --frozen pytest tests/architecture/test_welcome_boundaries.py tests/tui/test_optional_welcome_styles.py tests/tui/test_welcome_package_recovery.py
uv run --frozen --extra themes pytest tests/tui/test_theme_package.py tests/tui/test_welcome_styles.py tests/tui/test_welcome_layout.py tests/tui/test_welcome_refresh.py tests/tui/test_preferences.py
uv run --frozen --extra themes pytest tests/tui/app/ -k 'welcome or theme or composer_growth or startup'
uv run --frozen --extra themes pytest tests/tui/test_dot_animation.py tests/tui/test_particle_fields.py tests/tui/test_particle_samplers.py
uv run --frozen --extra themes pytest tests/tui/test_animation_rendering.py
```

自定义主题需验证全视口绘制、公共组件组合、输入布局与增长、短终端、命令弹窗方向、局部样式撤销、
快照更新、动画暂停/恢复、卸载资源回收和主题刷新。切换主题时应保持同一输入实例与草稿；动画不得驱动全页布局。
自动回归保护主题的加载与失败处理、主题选择保存、输入保留、配色与饱和度联动、可见性通知和资源回收；
验证边界见[测试策略](testing.md)。主题内部计算与输入负载基准见
[本地性能测量](../../dev/benchmarks/README.md#会话管线与-tui)，基准不能替代实际终端与输入法验收。

CI 分别在隔离环境中安装基础 wheel 与带 `themes` 的 wheel，通过 `dev/check_distribution.py` 验证
实际包内容、依赖、CLI 启动、主题目录、缺失扩展时的回退，以及修改配色后原主题选择仍被保留。
