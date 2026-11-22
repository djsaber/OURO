# OURO UI Workbench

一个原生 OURO 示例插件，用于**探索界面方向、并排比较原型、基于浏览器证据评审**。插件内置 `ui-workbench` Skill、四个命令和五个工具。对比板是离线 HTML，不依赖 Web 服务、远程字体或脚本。插件没有广告、遥测、自动更新或后台任务。

## 安装与入口

在 OURO 源码根目录执行。示例使用独立 Home，不改动日常使用的配置；改为常用 Home 时，请一致地替换所有命令的 `--home`。

```sh
uv run --frozen ouro plugin validate examples/plugins/ui-workbench --json
uv run --frozen ouro plugin install examples/plugins/ui-workbench --home ./.ouro-dev
uv run --frozen ouro plugin enable dev.ouro.examples.ui_workbench --home ./.ouro-dev
uv run --frozen ouro plugin run-command dev.ouro.examples.ui_workbench:status --home ./.ouro-dev
```

打开同一 Home 的交互界面（`uv run --frozen ouro --home ./.ouro-dev`），并在目标项目中使用命令：

| 命令 | 行为 |
| --- | --- |
| `/dev.ouro.examples.ui_workbench:explore <设计简报>` | 向当前会话提交设计探索任务：生成三种有实质差异的候选，打包对比并请求选择。 |
| `/dev.ouro.examples.ui_workbench:review [页面或重点]` | 向当前会话提交视觉层级和可用性两路评审任务；不直接修改页面。 |
| `/dev.ouro.examples.ui_workbench:compare <清单路径>` | 用现成的原型打包离线对比板，不调用模型。 |
| `/dev.ouro.examples.ui_workbench:status` | 查询本地版本、内置 Skill 和可选能力的配置状态，不探测网络，也不解析密钥。 |

`explore` 和 `review` 需要已配置可用的模型。命令贡献不接收附件；如果评审依赖参考图或文件，请将其放在工作区并在简报中引用，或在同一会话中先提交附件。所有输出制品写入 `OURO_HOME/artifacts/dev.ouro.examples.ui_workbench/`，在返回结果中给出完整路径。每次生成使用新目录，不覆盖先前结果。插件不会自动打开浏览器。

## 从自己已有的 HTML 原型开始

例如，在工作区的 `exploration/` 下放入 `journal.html`、`gallery.html` 和以下 `manifest.json`：

```json
{
  "title": "自然观察档案",
  "brief": "让第一次访问的读者快速找到与季节相关的物种。",
  "candidates": [
    {"title": "观察日志", "path": "journal.html", "summary": "保留时间顺序，突出连续观察。"},
    {"title": "主题图鉴", "path": "gallery.html", "summary": "按主题浏览，适合第一次使用。"}
  ]
}
```

```sh
uv run --frozen ouro plugin run-command dev.ouro.examples.ui_workbench:compare exploration/manifest.json --workspace . --home ./.ouro-dev --json
```

用浏览器打开返回的 `data.files[0]`。清单接受 2–6 个候选；候选路径相对清单所在目录，且必须仍在工作区内。每个候选是最多 1 MiB 的 UTF-8 `.html` 或 `.htm` 文件，可使用内联 CSS 与嵌入式栅格图。板中是只读预览：不读取外链或相对路径指向的资源，不执行脚本，也不保留页面跳转或可编辑表单。需要交互取证时，请对原页面截图，不要用离线对比板模拟原页面的行为。

## 可选能力

下列集成默认关闭。`plugin configure --values` 接收**完整的**配置对象；同时启用多项时合并相应字段，不要逐条直接覆盖。通过 OURO 给模型工具配置审批，浏览器交互、素材下载和生图默认需要审批。

### 真浏览器截图和交互取证

手动启动与本机 Chrome 或 Edge 版本匹配的 WebDriver，**仅监听回环地址**。插件不会下载安装或启动驱动。示例配置：

```sh
uv run --frozen ouro plugin configure dev.ouro.examples.ui_workbench --home ./.ouro-dev --values '{"webdriver_url":"http://127.0.0.1:9515","browser":"chrome"}'
```

`ui_workbench__capture` 接收工作区中的可信 HTML，或自己启动的回环 HTTP(S) 预览。示例参数：

```json
{
  "target": "http://127.0.0.1:3000/",
  "viewports": ["desktop", "mobile"],
  "states": [
    {"name": "initial"},
    {"name": "menu-open", "steps": [{"action": "click", "selector": "#menu"}]}
  ]
}
```

可用 `click`、`type`（替换普通文本输入值）和 `wait`（`ms`）组合步骤。桌面预设为 1440×900，移动预设为 390×844；每个视口和状态使用独立浏览器会话。结果包含 PNG 和 `inspection.json`，报告横向溢出、断图和简单背景上的低对比度候选。这些是走查线索，不等于无障碍合规结论。请只打开自己信任的页面，用测试数据执行步骤；浏览器会实际运行页面并加载其资源，这不是页面沙箱。当前不支持公共网址、录屏、像素差异对比或在真实移动设备上测试。

### 公有领域素材

设置 `media_enabled: true` 后，`ui_workbench__media` 可搜索大都会艺术博物馆开放馆藏：`{"action":"search","query":"botanical print","limit":5}`。结果为已核对公有领域状态的候选，含可用缩略图地址和许可信息。用 `{"action":"download","item_id":123}`（替换为真实条目 ID）下载所选图片并保存归属 JSON；插件不接受任意下载地址。

### 生成和编辑图片

用 `image_api_base`、`image_model` 显式选择服务和模型；将 `image_api_key` 绑定到环境变量或 OURO 保存的凭据引用。API 基址包含版本路径，例如 `https://images.example.test/v1`，不包含 `/images/generations`。调用 `ui_workbench__image`：

- `{"action":"generate","prompt":"Botanical line study on a cream background"}`
- `{"action":"edit","prompt":"Remove the paper texture","path":"assets/study.png","background":"transparent"}`

可选 `size` 为 `auto`、`1024x1024`、`1536x1024`、`1024x1536`，`background` 为 `auto`、`opaque`、`transparent`；仅在所选模型支持时传入，省略则使用服务默认值。

服务需兼容 `/images/generations`（JSON）和 `/images/edits`（单图 multipart），并在 `data[0].b64_json` 中返回一张图。插件不追踪服务返回的下载 URL，也没有默认服务或回退服务。编辑会将指定的工作区原图上传到所配置的服务；服务可能收费。

## 开发和卸载

- `plugin.py` 负责 SDK 装配、命令和审批边界之外的调用编排；`comparison.py`、`browser.py`、`media.py`、`image.py` 分别实现核心用例或集成。`files.py` 管理输入路径与目录内的原子制品发布。
- `uv run --frozen pytest examples/plugins/ui-workbench/tests` 运行确定性测试；浏览器和外部服务的单元测试用测试传输，不要求真实驱动或密钥。
- 关闭：`uv run --frozen ouro plugin disable dev.ouro.examples.ui_workbench --home ./.ouro-dev`。关闭不会清理已生成的用户制品。
