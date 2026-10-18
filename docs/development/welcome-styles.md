# 欢迎页样式包开发

欢迎页框架位于 `ouro/tui/welcome/`。`WelcomeStyle` 描述包身份与页面工厂，`WelcomePage`
是覆盖整个欢迎页视口的 Textual 根组件。样式包负责页面构图、背景、装饰、信息组件和响应式布局；
Host 负责业务状态、持久输入组件、菜单、配置与生命周期。框架不规定图形原语、点阵、调色板或动画算法。
呈现与配置契约见 [TUI 规格](../specs/tui.md#欢迎页样式)。

## 页面与输入的边界

`WelcomeHost` 的两个直接子节点是样式包页面和持久的 `#interaction-surface`。页面占满视口，输入层
位于其上方；选择器与帮助等 Host Screen 位于两者上方。输入层包含 composer、状态栏、排队输入和交互面板。
它在切换样式、进入会话和返回欢迎页时保持同一个实例，保留草稿、附件、历史和光标。

包通过以下接口参与布局和数据投影：

| 接口 | 契约 |
|---|---|
| `place_input(viewport, measure) -> Region` | 返回页面坐标系内的输入矩形；宽度必须为 `1..viewport.width`，矩形须非空且位于视口内 |
| `measure(width) -> int` | 使用 Textual 按指定宽度测量公共输入层，包含其 padding/border；高度限于视口 |
| `input_region` | Host 写入的实际输入矩形；包读取它安排相邻内容，输入增长也会使依赖布局失效 |
| `set_system_status(status)` | 接收不可变的 `TuiSystemStatus` 快照，供包投影工作区、用量等信息 |
| `set_playback(active=..., motion=...)` | 接收页面可见性与用户动画偏好 |
| `refresh_theme()` | 更新所消费的界面语义 token |

`place_input` 是同步、无 I/O 的布局函数，不修改组件或读写业务状态。包使用 `measure` 返回的高度安排输入，
并为短终端留出可用位置。Host 将命令弹窗紧贴输入区上方，高度受上方可用空间限制；
仅上方不足最小菜单高度且下方空间更大时向下展开。

页面可以实现 `render()`、`render_line()`，也可以通过 `compose()`、TCSS 或 Textual `Layout` 组合任意子组件。
全屏字节瀑布可以直接绘制页面每一行，背景会覆盖输入区之外的全部终端；包也可以把输入放在顶部、底部或侧栏。
页面内部的布局缓存随 `input_region` 变化失效，绘制帧不需要触发布局。

## 创建与注册

工厂每次必须返回新的、未挂载的 `WelcomePage`。以下页面将字节场铺满视口，并将公共输入区放在底部：

```python
from collections.abc import Callable

from rich.segment import Segment
from textual.geometry import Region, Size
from textual.strip import Strip

from ouro.tui.entry import run_tui
from ouro.tui.welcome import WelcomePage, WelcomeStyle


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

run_tui(welcome_styles=(byte_page,))
```

`OuroTuiApp` 接受相同的 `welcome_styles` 参数。每个 App 将这些包与内置包组成自己的只读目录；
重复 ID 在组合边界报错。可用包出现在 `Theme` 菜单的 `Welcome style` 分组中，以 `label` 单行展示；
`description` 保留为包元数据，不显示在菜单中。保存自定义包后，后续启动
需要注册同一 ID；未知 ID 明确报告配置错误。JSON 只保存身份与呈现偏好，不加载 Python 路径或执行代码。

## 可选主题依赖

`ouro-agent[themes]` 是欢迎页主题拓展的标准 extra。基础安装默认显示 `basic`，TUI 其余功能完整可用。
主题是完整页面，可以静态展示，也可以播放动画；`welcome_motion` 只控制页面内部的播放行为。

`WelcomeStyle` 的元数据和工厂必须能在缺少可选依赖时导入。需要可选依赖的包通过
`check_requirements` 提供检查函数：可用时返回 `None`，否则返回含修复说明的原因。
App 在启动时执行检查，使用相同函数的主题共用一次结果；不做运行中的环境扫描或依赖安装。
工厂在选用主题时加载渲染实现，依赖检查只处理其明确负责的依赖错误，不吞掉主题代码的编程错误。

已知目录用于验证和保存主题身份，可用性用于菜单筛选与实际显示。所选主题暂不可用时，显示基础页并
提示原因，保留用户选择；配色和播放偏好的写入也保留该身份。恢复依赖并重启后恢复原选择。
完整配置与回退契约见 [TUI 规格](../specs/tui.md#欢迎页样式)。

## 公共组件与局部样式

`welcome/components.py` 提供 `WelcomeSystem`、`WelcomeUsage`、`WelcomeTips` 和 `WelcomeVersion`。
包按需组合这些组件，或者自行投影收到的状态；使用 System/Usage 时，由页面的 `set_system_status`
转发快照。组件不访问服务或持久化存储。公共组件保留 OURO 信息排版与用量悬停交互。

`CenteredWelcomePage` 是内置包复用的可选页面组合：它安排艺术区、居中输入、用量、提示、系统信息和版本。
116 列上限、居中规则和区块间距属于这个组合，通用 Host 不限制页面宽度、艺术区域或信息区顺序。

默认 TCSS 作用于包自身的组件树。欢迎页激活时，Host 具有 `welcome-style-<style_id>` 类；离开欢迎页或切换包
会撤销该类。需要调整公共输入层的外观时，包可以设置 `SCOPED_CSS = False`，并让所有跨节点规则以该类为边界：

```css
#input-region.welcome-style-byte-page #interaction-surface {
    background: $ouro-panel;
    padding: 0 1;
}
```

公共输入层是与页面并列的持久节点，因此跨节点规则需要关闭 Textual 的组件类型自动作用域。
规则应限定当前包，不能修改其他页面、菜单或会话视图；保留输入层的可见性、键盘行为与自动高度。
界面文字可以消费 `theme_for(self)` 和 `$ouro-*` token；艺术内容的光谱由包自行定义。

## 动画与资源

Host 在离开欢迎页、打开覆盖菜单或替换页面时发送 `active=False`，返回当前欢迎页时发送 `active=True`。
动画包应在 `active and motion` 时启动计时，否则暂停动画计时和逐帧计算；`motion=False` 仍保留静态呈现。
静态帧绘制独立于动画播放：菜单打开期间切换包或调整终端尺寸，也应立即显示当前尺寸下的作品。
`on_unmount()` 释放定时器、任务、缓冲与其他自有资源。包不抢占输入焦点，不写入会话事实。

按动画需要刷新实际变化的组件，避免每帧 `refresh(layout=True)` 或重建组件树。布局使用内存快照，帧计算不做文件
或网络 I/O；异步资源的取消与回收由创建它们的包负责。布局、状态投影与动画帧是独立的更新链路。

## 点阵艺术包

`styles/__init__.py` 保存内置主题描述；六个点阵实现模块各自定义数学场、节奏和光谱，按需加载。
`basic.py` 实现静态字标。
`dot_matrix/` 是艺术包可选择复用的引擎：

- `field.py`：只读网格、按视口准备的 RGB 采样器，以及光谱插值。
- `raster.py`：固定地址上的余辉、相邻光照、曝光映射、Braille 编码与变化区域。
- `widget.py`：动画时钟、后台帧计算、局部 Strip 刷新，以及点阵页面包。

每个终端 cell 对应 2×4 个固定 Braille 点位。包只计算这些采样位置的辐射值；大小变化时重新构建网格，
动画期间只改变亮度、颜色和开关状态。一个 cell 的点共享终端前景色，点的开关保留子 cell 轮廓。
彩色模式使用相对亮度保留轮廓，并以固定空间阈值逐步熄灭暗部点位；无光 cell 输出无前景色的普通空格，
直接继承页面背景。RGB 不设固定颜色下限，曝光后的各通道按 `background + (255 - background) * light`
与当前画布合成，避免低亮点比背景更暗。淡出同时依靠颜色和点位覆盖率，不依赖终端准确显示近黑前景色。
无颜色模式使用固定空间阈值保留亮度结构。标准库 `array('d')` 保存连续双精度数据，
`arrayfunc` 在 C 中批量计算，Rich/Textual 处理终端绘制；运行和测试均不依赖 NumPy。

`DotArtwork.prepare(grid)` 为当前视口准备固定几何与工作缓冲，返回 `sample(seconds)`。
采样器返回三个按点行优先排列的 RGB 数组，每个数组的长度为 `columns * rows * 8`，
值须有限且非负。返回值借用采样器的缓冲区，到下次采样前有效，消费者不得修改；
需要保留多帧时自行复制。每次 `prepare` 都创建独立状态，尺寸改变或卸载页面时释放旧状态。
样式只负责自己的数学场，公共引擎负责余辉和终端编码；不增加通用数组兼容层。

点阵组件以最高 30 Hz 请求帧，采样和编码通过 Textual Worker 与 `asyncio.to_thread`
在主循环外完成。每个组件只保留一个计算任务；计算未结束时跳过动画 tick，尺寸变更合并到最新请求。
后台任务独占光栅缓冲，不访问挂载组件；主循环只发布当前版本的结果。暂停、缩放和卸载使过期结果失效，
卸载不等待不可中断的数值计算，也不接受其完成结果。时钟只计算播放期间的时间，不补播积压帧。
背景色随帧请求传入后台；主题变化会在当前动画时间重绘一帧，暂停状态也保持正确配色。

光栅按最终 Braille 位和 RGB 值比较终端 cell，复用未变化行的 Strip，仅刷新每行变化点之间的区域。
空白 cell 使用统一状态，不因不可见的光照变化触发刷新。
未变化帧不触发重绘；输入焦点、布局和硬件光标由 Textual 管理，不随艺术计算更新。
Windows 控制台的完整帧提交由终端适配器负责，与欢迎页包无关，见 [TUI 规范](../specs/tui.md)。

`dot_matrix` 的加载边界负责检查 `arrayfunc` 版本与实际导入能力，再由页面工厂加载渲染器和采样器。
它使用 `packaging` 解析标准版本约束；主题目录和基础页面的导入不加载 `arrayfunc` 或点阵实现。
构建工具链与平台参数见[欢迎页主题指南](../guides/running.md#欢迎页主题)。

## 验证

针对欢迎页的回归入口：

```bash
uv run --frozen --extra themes pytest tests/tui/test_optional_welcome_styles.py tests/tui/test_welcome_styles.py tests/tui/test_welcome_layout.py tests/tui/test_welcome_refresh.py tests/tui/test_preferences.py
uv run --frozen --extra themes pytest tests/tui/test_app.py -k 'welcome or theme or composer_growth or session_input_surface or startup'
uv run --frozen --extra themes pytest tests/tui/test_dot_animation.py tests/tui/test_windows_driver.py
```

自定义包需验证全视口绘制、公共组件组合、输入布局与增长、短终端、命令弹窗方向、局部样式撤销、
快照更新、动画暂停/恢复、卸载资源回收和主题刷新。切换包时应保持同一输入实例与草稿；动画不得驱动全页布局。
`test_dot_animation.py` 验证局部重绘、输入响应和暂停／卸载时不发布过期帧。
艺术包的具体图案、配色、光照、背景合成和余辉衰减由实际终端中的人工视觉检查确认。
自动回归保护包的加载与失败处理、偏好保存、输入保留、动画生命周期和资源成本；
验证边界与完整回归命令见[测试策略](testing.md)。

端到端帧计算基准包含采样、光照映射、Braille 编码和 Strip 构造，不包含终端 I/O：

```bash
uv run --frozen python dev/benchmarks/welcome.py --frames 60
```

基准报告两种视口尺寸下的首帧时间、稳态中位数与 P95。性能对比应使用同一机器、
同一 Python 版本和相同帧数；不在 CI 中断言随机器负载波动的计时阈值。

输入负载基准使用真实编辑器、Textual 合成器和 ANSI 序列化，以每秒 30 个事件交替输入和删除，
报告按键到编辑处理的延迟及终端输出量：

```bash
uv run --frozen python dev/benchmarks/welcome_input.py
```

输出由测试驱动接收，不接管终端；原生终端绘制和输入法预编辑需在实际终端验证。
Windows 驱动测试另建隐藏的隔离控制台，检查大帧的原生写入边界、Unicode 完整性和并发光标读取；
测试不接管现有终端，也不模拟输入法候选窗。
