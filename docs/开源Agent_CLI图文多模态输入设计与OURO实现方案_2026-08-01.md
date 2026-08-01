# 开源 Agent CLI 图文多模态输入设计与 OURO 实现方案

> 日期：2026-08-01
> 范围：OpenCode、Kimi Code CLI、Codex CLI 的交互输入范式；OURO Endpoint、消息、资源、
> Context 与 Provider 链路；OURO Interactive 首期实现方案。

## 一、结论先行

OURO 不缺少模型侧多模态能力，缺少的是终端输入组合层。

现有底层已经形成正确的单一链路：

```text
本地文件
  -> ResourceService 内容寻址导入
  -> SessionResourceReference 建立会话可达性
  -> MessageRecord.resource_refs 保存 canonical 引用
  -> RuntimeService 按当前 Endpoint 能力校验
  -> ContextPreparationService 请求级物化
  -> ModelMessage(TextContent + ImageContent)
  -> OpenAI / Anthropic codec 编码
  -> 模型请求
```

因此本次不新增第二套“图片消息”、不把 base64 写入会话历史，也不让 CLI 直接拼 Provider payload。
首期只增加一个 Interactive composer attachment state，将本地图片或文本资源附到“下一条用户消息”，
并把资源 ID 交给现有 `RuntimeService.run(..., resource_refs=...)`。

## 二、调研方法与版本

调研同时核对产品文档和开源仓库源码，源码快照如下：

- OpenCode：`0eac0964af82272a19120a6ba85c8f35728e1790`
- Kimi Code：`e22479a62eed9c3b78a67b313f4332c2c0ba9670`
- Codex：`ee0247f95a6fe2b094ba2253d82cae2a2b4c2dff`

本文只提取可迁移的交互和架构范式，不复制各项目与其 TUI 框架绑定的实现。

## 三、开源 CLI 的输入范式

### 3.1 OpenCode：富文本编辑器中的虚拟占位符与结构化 parts

OpenCode 文档把“拖放图片到终端”作为标准使用路径。当前 TUI 也读取剪贴板媒体；若粘贴内容是
图片则创建附件，若粘贴内容是单个本地路径则尝试读取图片或 PDF。编辑区显示 `[Image N]` 或
`[PDF N]`，实际数据保存在与文本并列的 file part 中。提交时发送一个 text part 加多个非文本
parts，而不是把占位符当成模型输入。

关键设计：

- `PromptInfo` 同时持有可编辑字符串与结构化 parts；
- extmark 将可删除的虚拟占位符映射到附件 part；
- 本地路径只在输入边界读取，提交契约已经是结构化内容；
- 新会话在提交时创建，文本和附件作为同一个用户输入原子发送；
- base64 是传输形态，不是编辑器文本或会话语义。

资料：

- [OpenCode 使用文档：拖放图片进入 prompt](https://opencode.ai/docs)
- [OpenCode TUI prompt：剪贴板、路径识别、占位符和 parts 提交](https://github.com/anomalyco/opencode/blob/0eac0964af82272a19120a6ba85c8f35728e1790/packages/tui/src/component/prompt/index.tsx)
- [OpenCode 本地附件媒体类型边界](https://github.com/anomalyco/opencode/blob/0eac0964af82272a19120a6ba85c8f35728e1790/packages/tui/src/component/prompt/local-attachment.ts)

### 3.2 Kimi Code：剪贴板媒体、可解析占位符、能力门控与二级读取工具

Kimi Code CLI 在 macOS/Linux 使用 `Ctrl-V`，在 Windows 使用 `Alt-V` 粘贴图片或视频。
编辑器插入带尺寸的 `[image #N (W×H)]` 占位符，附件 store 保存真实字节。提交时扫描占位符，
按原始顺序展开为 text/image/video parts；用户手写但无法在 store 中解析的占位符保持普通文本。

Kimi 还体现了两个重要边界：

- 在提交前按模型 `image_in` / `video_in` 能力显式拒绝；
- 对图片进行统一压缩和字节预算控制，并保留 `ReadMediaFile` 作为模型主动读取、裁剪和读取原图细节
  的二级链路。

这说明“用户在 composer 直接附图”和“Agent 运行中主动读图”是两个入口，但应汇入同一媒体能力与
请求编码体系。

资料：

- [Kimi Code CLI：Interaction and input](https://www.kimi.com/code/docs/en/kimi-code-cli/guides/interaction)
- [Kimi Code：图片压缩和读取预算配置](https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/config-files)
- [Kimi Code：ReadMediaFile](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/tools.html)
- [Kimi Code 图片附件 store](https://github.com/MoonshotAI/kimi-code/blob/e22479a62eed9c3b78a67b313f4332c2c0ba9670/apps/kimi-code/src/tui/utils/image-attachment-store.ts)
- [Kimi Code 占位符到 prompt parts 的唯一展开路径](https://github.com/MoonshotAI/kimi-code/blob/e22479a62eed9c3b78a67b313f4332c2c0ba9670/apps/kimi-code/src/tui/utils/image-placeholder.ts)

### 3.3 Codex：附件状态独立于文本，失败时恢复 composer

Codex 的 TUI attachment state 保存本地图片 path 与其占位符。删除占位符会同步删除附件；提交时
生成 `UserInput::LocalImage` 与 `UserInput::Text` 列表。Core 在请求序列化阶段才把本地图片转换为
模型可接收的数据。非交互 `exec`/`resume` 同时提供 `--image/-i FILE`。

Codex 有一项值得直接吸收的错误语义：如果当前模型不支持图片，提交被阻止，文本、图片及富文本
元素重新放回 composer，用户可切换模型或删除附件后重试。能力失败不能导致用户输入静默丢失。

资料：

- [Codex attachment state：占位符、path 与删除同步](https://github.com/openai/codex/blob/ee0247f95a6fe2b094ba2253d82cae2a2b4c2dff/codex-rs/tui/src/bottom_pane/chat_composer/attachment_state.rs)
- [Codex 输入提交：能力检查、LocalImage/Text 与失败恢复](https://github.com/openai/codex/blob/ee0247f95a6fe2b094ba2253d82cae2a2b4c2dff/codex-rs/tui/src/chatwidget/input_submission.rs)
- [Codex UserInput 结构化输入契约](https://github.com/openai/codex/blob/ee0247f95a6fe2b094ba2253d82cae2a2b4c2dff/codex-rs/protocol/src/user_input.rs)
- [Codex exec 的 `--image/-i` 参数](https://github.com/openai/codex/blob/ee0247f95a6fe2b094ba2253d82cae2a2b4c2dff/codex-rs/exec/src/cli.rs)

### 3.4 共同范式

| 关注点 | 共同做法 | 对 OURO 的含义 |
|---|---|---|
| 编辑体验 | 文本中显示可辨识占位符或附件计数 | Interactive 需要显式的下一消息附件状态 |
| 输入契约 | 文本与媒体是同一个用户输入的不同 part | 使用同一条 `MessageRecord.resource_refs`，不创建旁路消息 |
| 生命周期 | composer state 是临时态，会话历史是 canonical state | 待发送附件不能写进 canonical message，提交后才关联 |
| 能力 | 提交前按当前模型能力拒绝 | 继续以 Endpoint `supports_vision` 为唯一事实源 |
| 传输 | path/bytes 在接近请求处转换为 data URI 或厂商 block | 继续由 ResourceService 和 Provider codec 物化 |
| 错误 | 不支持或准备失败时保留 draft | OURO 只在 `runtime.run` 返回后清空待发送附件 |
| 资源 | 有数量、大小、类型边界 | 复用 OURO 已有 16 个附件、文本/图片字节上限 |

## 四、OURO 现状检查

### 4.1 模型 Endpoint 配置

`ProviderEndpointConfig` 已将能力作为类型化 Endpoint 配置：

- `supports_tools`
- `supports_vision`
- `supports_streaming`
- `supports_structured_output`

`supports_vision` 默认 `False`，自定义兼容 Endpoint 必须显式声明 `--vision`。这是一种正确的
fail-closed 设计，不能根据模型名称猜测。

配置经 `provider_runtime` 合并模板、持久化配置和命令行 override，再由 provider factory 投影为
`ModelMetadata.supports_vision`。Run 始终按 Session 当前选择的完整 `provider/endpoint` 路由取得
adapter，因此多模态能力属于真实调用 Endpoint，而不是 Provider 或 UI 的全局开关。

当前内置模板中：

- vision：OpenAI、Anthropic、Gemini、Kimi、OpenRouter、Mistral 的现有模板 Endpoint，以及
  GLM 的 `glm-5v-turbo`；
- non-vision：DeepSeek、Qwen、GLM 的非 V Endpoint；
- 用户自定义 compatible Endpoint：默认 non-vision，需显式开启。

### 4.2 模型侧多模态链路

Core 已定义 provider-neutral `TextContent` 与 `ImageContent`。`ModelMessage` 是硬边界，不携带 Session
identity、resource ref 或 Host metadata。

`ResourceService.project_model_messages()` 在请求准备期执行：

1. 校验资源属于该 Session，且不可变 blob 的 digest 正确；
2. 限制单条模型输入最多 16 个附件；
3. UTF-8 文本、JSON、XML 按文本内容投影；
4. PNG/JPEG/GIF/WebP 只允许出现在 user message；
5. 当前 Endpoint 必须 `supports_vision=True`；
6. 图片读取上限 20 MiB，转为请求级 data URI `ImageContent`；
7. canonical `MessageRecord` 保持 resource ID，不写回 base64。

Provider 层已完成两套编码：

- OpenAI Chat Completions：`{"type":"image_url","image_url":{"url":"data:..."}}`；
- Anthropic Messages：`{"type":"image","source":{"type":"base64",...}}`。

所以“模型基于图片反推提示词”不需要新的模型 API 或 prompt hack，只需要让同一条 user message
带上图片 resource ref。

### 4.3 会话与资源链路

资源采用内容寻址的全局不可变 blob，Session 只保存可达引用；相同内容自动去重。消息再通过
`resource_refs` 关联本轮真正使用的资源。分支会复制可验证的引用关系，删除资源会拒绝仍被 canonical
message 使用的资源，Context snapshot 只保留可还原引用与 digest，不复制 blob。

这套设计同时满足：

- 会话历史体积不会被 base64 放大；
- 重放与审计能验证资源内容；
- 分支、压缩、上下文预算都沿用统一消息语义；
- Provider SDK/协议不会泄漏进 Session 存储。

### 4.4 当前缺口

现有非交互模式要求先执行 `resource import <session>`，再执行
`run --session <session> --resource <id> <prompt>`。Interactive 的普通输入则只调用
`runtime.run(line, ...)`，没有 `resource_refs`，也没有 composer attachment state。

具体缺口是：

1. Interactive 无法从本地路径导入并选择下一条消息的资源；
2. 无法查看或取消待发送附件；
3. 提示符不展示附件状态；
4. `/new`、`/open` 等会话切换没有需要清理的 composer state；
5. Endpoint 能力校验虽然存在，但 Interactive 没有输入可触发它。

## 五、目标设计

### 5.1 首期交互契约

```text
/attach <path>       导入一个本地资源并附到下一条普通用户消息
/attachments         查看下一条消息的待发送附件
/detach <number>     从下一条消息取消一个附件
/detach all          清空下一条消息的待发送附件
```

示例：

```text
ouro> /model openai/gpt-5.6
ouro> /attach "C:\design\reference.png"
Attached for next message: [1] reference.png (image/png, 248 KiB)
ouro [1]> 基于这张图反推一份可复现构图、光线、材质和镜头语言的生成提示词。
```

行为约束：

- 若尚无 Session，`/attach` 使用当前选定模型和会话策略创建 Session，再导入资源；
- 待发送列表严格绑定 Session；`/new`、`/open` 或其他 Session 切换会清空旧 draft；
- 普通输入、`/follow`、`/steer` 都携带同一组 `resource_refs`；
- `RuntimeService.run` 正常返回后清空 draft，包括 Run 返回 failed/paused 等已接受输入的状态；
- 抛出错误时保留 draft，用户可切换到 vision Endpoint 后重试；
- `/detach` 只修改 composer state，不删除 Session 资源。资源可能已被历史消息使用，不能把 UI 取消
  误解为 canonical 删除；
- 路径只在 `/attach` 后解释，普通用户文本不做“看起来像路径”的启发式改写。

### 5.2 分层

新增 `ouro/cli/interactive_attachments.py`：

- 只负责 Session-scoped composer attachment state；
- 保存展示所需的 `ResourceView`，输出稳定的 resource ref 顺序；
- 不读取文件、不编码图片、不调用 Provider、不修改 Session store。

`interactive_commands.py`：

- 解析 `/attach`、`/attachments`、`/detach`；
- 调用 `RuntimeService.create_session` 与现有 `ResourceService.import_resource`；
- 把返回的 `ResourceView` 放入 composer state；
- 只做接口适配和输出格式化。

`interactive.py`：

- 持有 composer state 的生命周期；
- 在 prompt 中展示附件数；
- 提交时把 `resource_refs` 传给 `RuntimeService.run`；
- 根据提交成功或 Session 切换清理状态。

应用层、Core、Resource store、Context 和 Provider codec 无需新增多模态分支。

### 5.3 为什么首期不直接实现 Ctrl/Alt-V 图片剪贴板

当前 `InteractiveInput` 的契约是 `Callable[[str], str]`，它在后台线程中调用标准 `input()`，只得到
提交后的纯文本行。它看不到 key event、bracketed paste、剪贴板 MIME 或可编辑 extmark。

在这个边界上硬接平台命令读取剪贴板会造成：

- Windows/macOS/Linux 三套隐式依赖；
- 文本粘贴与图片粘贴的竞态；
- 无法在行编辑器中可靠删除或移动附件占位符；
- 测试只能 mock 平台命令，不能保护真实输入语义。

所以首期用显式 `/attach` 完成完整的图文消息能力。未来若将 `InteractiveInput` 升级为事件化 TUI
composer，只需让剪贴板/拖放事件调用同一个资源导入和 attachment state，不改 Runtime、Session 或
Provider 契约。

### 5.4 非目标

- 不增加图片 OCR、自动 prompt 反推服务；这是模型在收到图片后的任务语义；
- 不支持视频、音频或 PDF 直接模型投影；现有 ResourceService 尚无这些 adapter；
- 不在 CLI 压缩或转换图片；当前先严格遵循 ResourceService 的媒体与大小契约；
- 不自动探测模型能力，不按模型名猜测 vision；
- 不引入新的第三方依赖或 TUI 框架。

## 六、验证计划

至少覆盖以下行为：

1. composer attachment state 的 Session 隔离、去重、顺序和 detach；
2. `/attach` 在无 Session 时创建 Session 并导入资源；
3. Interactive 把图片 resource ref 写入 canonical user message；
4. 请求级上下文包含 `ImageContent`，canonical message 不包含 base64；
5. non-vision Endpoint 拒绝提交且 draft 保留；
6. `/new`、`/open` 清空不属于目标 Session 的 draft；
7. `/follow`、`/steer` 仍沿用既有队列语义并携带 refs；
8. lint、类型检查、相关测试和全量测试。

## 七、后续演进触发条件

只有当 OURO 决定引入真正的多行事件化 TUI 时，再实现下列能力：

- Ctrl/Alt-V 读取图片 MIME；
- 拖放/路径粘贴事件识别；
- 文本内可删除、可移动的附件占位符；
- 图片尺寸/缩略图预览；
- 统一图片压缩与 detail/region 策略；
- Agent 主动读取本地图片的 `ReadMediaFile`/`view_image` 工具入口。

这些都是相邻能力，但不是让 Interactive 首次具备图文输入的前置条件。
