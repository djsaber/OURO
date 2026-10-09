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
并为短终端留出可用位置。Host 按输入区上下剩余空间选择命令弹窗方向并限制高度。

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
重复 ID 在组合边界报错。包自动出现在 `Theme → Welcome style` 菜单中。保存自定义包后，后续启动
需要注册同一 ID；未知 ID 明确报告配置错误。JSON 只保存身份与呈现偏好，不加载 Python 路径或执行代码。

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
动画包应在 `active and motion` 时启动计时，否则暂停计时和计算；`motion=False` 仍保留静态呈现。
`on_unmount()` 释放定时器、任务、缓冲与其他自有资源。包不抢占输入焦点，不写入会话事实。

按动画需要刷新实际变化的组件，避免每帧 `refresh(layout=True)` 或重建组件树。布局使用内存快照，帧计算不做文件
或网络 I/O；异步资源的取消与回收由创建它们的包负责。布局、状态投影与动画帧是独立的更新链路。

## 点阵艺术包

`styles/` 中的六个艺术包各自定义数学场、节奏和光谱；`basic.py` 实现静态字标。
`dot_matrix/` 是艺术包可选择复用的引擎：

- `field.py`：只读网格、带显式时间参数的 RGB 辐射场，以及光谱插值。
- `raster.py`：固定地址上的余辉、相邻光照、曝光映射与 Braille 编码。
- `widget.py`：动画时钟、局部 Strip 刷新，以及点阵页面包。

每个终端 cell 对应 2×4 个固定 Braille 点位。包只计算这些采样位置的辐射值；大小变化时重新构建网格，
动画期间只改变亮度、颜色和开关状态。一个 cell 的点共享终端前景色，点的开关保留子 cell 轮廓。
无颜色模式使用固定空间阈值保留亮度结构。NumPy 批量计算场，Rich/Textual 处理终端绘制。

## 验证

针对欢迎页的回归入口：

```bash
uv run --frozen pytest tests/tui/test_welcome_styles.py tests/tui/test_welcome_layout.py tests/tui/test_welcome_refresh.py tests/tui/test_preferences.py
uv run --frozen pytest tests/tui/test_app.py -k 'welcome or theme or composer_growth or session_input_surface or startup'
```

自定义包需验证全视口绘制、公共组件组合、输入布局与增长、短终端、命令弹窗方向、局部样式撤销、
快照更新、动画暂停/恢复、卸载资源回收和主题刷新。切换包时应保持同一输入实例与草稿；动画不得驱动全页布局。
点阵包还应验证不同时间下的有限非负输出、固定坐标不变、尺寸边界、确定性和终端行宽。
