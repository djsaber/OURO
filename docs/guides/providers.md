# 配置 Provider 与 Endpoint

> 文档类型：使用指南

Provider 用展示名、`provider_type` 和 `protocol` 表达用户配置；`provider_id` 与 `endpoint_id` 由 OURO
生成，仅作为内部身份与路由键。Provider 展示名必填，Endpoint 展示名选填。Endpoint 保存具体模型及其能力。
可先查看内置模板，再用一条命令
原子创建 Provider 与模板预置的整组 Endpoint：

```bash
uv run --frozen ouro provider templates
uv run --frozen ouro provider templates anthropic --json

# 显式绑定环境变量；模板中的名称只是建议
uv run --frozen ouro provider add "Anthropic" --type anthropic --api-key-env ANTHROPIC_API_KEY --home ./.ouro-dev --default

# 可指定默认 Endpoint 的模型 ID、Base URL 和凭据环境变量
uv run --frozen ouro provider add "Google Gemini" \
  --home ./.ouro-dev \
  --type gemini \
  --model gemini-3.8-flash \
  --api-key-env GEMINI_API_KEY \
  --default
```

TUI 的供应商配置页选中 **API key** 后按 Enter 直接编辑，Tab 切换 **KEY / ENV**。KEY 输入直接密钥，ENV
输入 `env:VARIABLE` 环境变量引用；右侧类型标签随切换更新，两种类型分别保留本次编辑的输入。
未配置的凭据项保持空白，预置供应商不会自动绑定模板建议的变量。已有绑定显示遮蔽符及 `[KEY]` / `[ENV]`，
进入编辑不会读取已保存密钥的明文；Esc 取消本次修改，留空保留当前凭据，Enter 保存当前输入与类型，
最后通过供应商页面的 Save 提交。KEY 保留密钥原文，即使以 `env:` 开头也不会被解释为环境引用；
CLI 的 `--api-key` 与 `--api-key-env` 分别对应这两种来源。

当前模板包括 OpenAI（API 与 Codex 订阅）、Anthropic、Gemini、DeepSeek、Qwen、KIMI、GLM、Grok、Mistral、
MiniMax、豆包和 Cohere；每个模板携带
一个明确的默认 Endpoint、仓库预制的模型 Endpoint、预定义展示名、官方文档链接与核验日期。列表不是服务端实时目录。`--model`
只覆盖默认 Endpoint 的模型标识。模型与视觉能力的核对依据见[预置供应商配置](../specs/provider-presets.md)。
OpenRouter 等聚合服务通过 `custom` 与兼容协议配置。`add` 根据 `--type` 自动判断是否装载内置模板并返回生成的 Provider ID。
内置类型的 `update` 会基于创建时记录的模板基线做安全三方
合并：未修改的预置值跟随新模板，用户修改或删除的预置 Endpoint、未知 Endpoint 和自定义连接信息保留；新增
模板 Endpoint 自动加入，同名占用会报告而不覆盖。没有模板基线的 Provider 不执行推断式更新，必须显式
`replace` 后才能跟踪模板版本。`replace` 会用所选类型的模板完整替换现有 Provider。

CLI 更新会在文本或 `--json` 输出中给出 added/updated/removed/preserved/conflicts 报告；TUI 可在供应商列表选中
Provider 回车后，通过 `Update from preset` 执行同一更新逻辑。模板已删除、禁用或来源发生替换时，更新失败且
Provider 保持不变。

受管理 Provider 可在 TUI 的 `Manage providers → Provider → Authentication` 中登录、查看状态和注销，
或通过 `Manage local credentials` 清理共享及未绑定的本地凭据。登录方法由已注册驱动提供；浏览器只在选择
`Open browser` 时打开，也可复制授权链接手动操作。内建 OpenAI 浏览器订阅使用同一认证管理入口，
支持与验证范围见[订阅支持指南](subscriptions.md)。

需要完全自定义时，Provider 的创建、更新、替换分别使用 `add`、`update`、`replace`，Endpoint 仍由独立命令
管理，不会隐式创建另一层对象：

```bash
uv run --frozen ouro provider add "GLM Gateway" \
  --home ./.ouro-dev \
  --type custom --protocol openai-chat-completions \
  --base-url https://example.test/v1 \
  --api-key-env GLM_API_KEY \
  --retry-attempts 3 \
  --retry-initial-backoff 0.5 \
  --retry-max-backoff 8

# update 不允许改变 provider type/protocol
uv run --frozen ouro provider update <provider_id> --home ./.ouro-dev --timeout 90

# 自定义供应商显式选择 Responses 协议
uv run --frozen ouro provider add "Responses Gateway" \
  --home ./.ouro-dev \
  --type custom \
  --protocol openai-responses \
  --base-url https://models.example.com/v1 \
  --api-key-env GATEWAY_API_KEY

uv run --frozen ouro provider endpoint add <provider_id> \
  --home ./.ouro-dev \
  --name "Chat" --model model-name --tools --streaming --default

# 自定义连接显式选择 Anthropic Messages 协议
uv run --frozen ouro provider add "Private Model" \
  --home ./.ouro-dev \
  --type custom --protocol anthropic-messages \
  --base-url https://models.example.com

uv run --frozen ouro provider endpoint add <provider_id> \
  --home ./.ouro-dev \
  --name "Reasoning" \
  --model model-name \
  --temperature 0.2 \
  --context-window 200000 \
  --max-output-tokens 16000 \
  --tools --streaming --structured-output \
  --reasoning-preset thinking-toggle \
  --default

# Anthropic Messages 自定义模型也可声明 adaptive thinking 与 effort
uv run --frozen ouro provider endpoint add <provider_id> \
  --home ./.ouro-dev \
  --name "Claude" \
  --model custom-claude-model \
  --reasoning-preset anthropic-messages-adaptive-effort \
  --default

# 单次运行覆盖统一的思考意图；不支持的模式或等级会在发起 HTTP 前失败
uv run --frozen ouro run --home ./.ouro-dev \
  --provider <provider_id> --endpoint <endpoint_id> \
  --thinking-mode enabled --thinking-effort high \
  "Analyze this problem"

uv run --frozen ouro provider use <provider_id> --home ./.ouro-dev
uv run --frozen ouro provider check <provider_id> --endpoint <endpoint_id> --home ./.ouro-dev
uv run --frozen ouro provider list --home ./.ouro-dev
uv run --frozen ouro provider endpoint list <provider_id> --home ./.ouro-dev
```

`provider add/replace --type custom` 必须通过 `--protocol` 显式选择线协议：
`openai-chat-completions`、`openai-responses` 或 `anthropic-messages`，默认 API 路径分别为
`/chat/completions`、`/responses` 和 `/v1/messages`。集成产品类型使用模板协议。
自定义连接也可指定 `<plugin_id>/providers/<id>`，使用已安装并启用的插件协议。
插件协议通过 CLI 创建；URL、原生选项和模型能力遵循对应插件的说明。
可运行的注册样例见 [Faux Provider](../../examples/plugins/faux-provider)。
TUI 在 `Add provider` 的 `Custom connection` 分组直接提供三个协议入口；创建时可以切换协议，
仍为默认值的 API 路径随之更新，自定义路径保持不变。已有供应商的协议只读，普通编辑保留协议和 Endpoint。

`--reasoning-preset` 提供 `none`、`openai-chat-completions-effort`、`openai-responses-effort`、`openai-responses-effort-summary`、`reasoning-content-effort`、
`thinking-toggle`、`enable-thinking-toggle` 和 `anthropic-messages-adaptive-effort`。Preset 是用户对目标模型协议能力的
显式声明，不是自动探测；若模型只支持部分等级，或参数值与 preset 不同，应使用
`--reasoning-config '<JSON>'` 完整声明 capability 与各 variant 的原生请求片段。思考配置归 Endpoint 所有，
因此 `custom` 类型的任意自定义模型都使用同一套配置与运行时 flags。

Responses 端点选择 `openai-responses-effort`（TUI 的 `Thinking preset → OpenAI Responses effort`），
将统一等级编码为 `reasoning.effort`；`openai-chat-completions-effort` 使用 Chat Completions 的 `reasoning_effort`。
例如，为已经使用 `openai-responses` 的供应商声明端点并指定单次运行等级：

```bash
uv run --frozen ouro provider endpoint add <provider_id> --home ./.ouro-dev --name "Custom" --model custom-model --reasoning-preset openai-responses-effort
uv run --frozen ouro run --home ./.ouro-dev --provider <provider_id> --endpoint <endpoint_id> --thinking-effort high "检查实现"
```

`openai-responses-effort` 声明 minimal、low、medium、high、xhigh、max 和开关能力；默认意图保留服务端默认值，
显式 enabled 且未指定等级时使用 medium，disabled 编码为 none。支持等级取决于目标模型，
不支持关闭或仅支持部分等级的模型应使用内置模型模板，或通过 `--reasoning-config` / TUI 的
`Thinking settings (JSON)` 声明实际能力与对应 variants；Python 配置可使用 `openai_responses_effort_reasoning`
指定 `supported_efforts`、`default_effort` 和 `activation`，用 `output` 控制可见输出，用可选 `summary`
声明原生摘要请求。协议字段见
[OpenAI 推理文档](https://developers.openai.com/api/docs/guides/reasoning)。

`openai-responses-effort` 将 `capability.output` 设为 `summary`，展示服务端返回的思考摘要或文本，不额外发送
`reasoning.summary`。需要显式请求摘要且服务端支持该参数时，选择 `openai-responses-effort-summary`
（TUI 的 `OpenAI Responses effort + request summary`）；它在默认及开启思考时发送 `summary: "auto"`。
仅有加密续接数据时不生成可见思考。已有端点可在 `Thinking settings (JSON)` 中将 `capability.output`
设为 `summary`，保留自身等级与参数映射；TUI 的思考展开设置独立控制显示折叠状态。

会话中的模型路由始终使用完整的 `provider/endpoint` 形式。交互模式可通过 `/models` 查看模型，
通过 `/model provider/endpoint` 选择模型；若会话尚未创建，该选择会用于下一条普通消息创建的首个会话，
否则切换当前会话的模型。活动 Run 已发出的请求继续使用原模型，下一次模型请求重新读取会话路由。
`/thinking` 显示当前 Endpoint 的有效思考设置与可选值；
`/thinking default|enabled|disabled|minimal|low|medium|high|xhigh|max` 设置当前会话（或下一新会话）的
供应商中立思考意图。每个会话独立持久化该设置，共享同一 Endpoint 的会话不会相互影响；不受 Endpoint
支持的模式或等级会在状态变更前被拒绝。切换模型时也会重新校验当前会话设置，不会静默降级或重置。有多个
effort 的 Endpoint 使用显式 `default_effort` 解析未带等级的 `enabled`，不依赖厂商默认。TUI 菜单只显示不同的
有效配置：可切换且支持 effort 时显示 `disabled` 和各 effort，不支持 effort 时显示 `enabled`、`disabled`；
`Alt+T` 按相同集合循环，不经过重复的 `default` 状态。

## 图片、音频与视频输入

Endpoint 分别提供 `--image-input`、`--audio-input` 和 `--video-input` 开关，以及对应的 `--no-*` 关闭开关；
TUI 的 Endpoint 能力菜单提供同样的设置。按目标服务与具体模型实际接受的格式开启：

```bash
# Chat Completions 音频输入端点，接受 WAV/MP3
uv run --frozen ouro provider endpoint add <provider_id> --model <audio-model> --audio-input

# 仅对接受 video_url 扩展的服务开启 MP4 视频输入
uv run --frozen ouro provider endpoint update <provider_id> <endpoint_id> --video-input
```

音视频文件使用普通上传或 `@` 原路径引用入口。支持的内容会作为原生媒体送入对话；能力、格式或大小不符合要求时，
保留文件说明与读取路径。三个开关均只声明输入能力，不启用语音或视频输出。
Responses 与 Anthropic Messages 路由不接受音视频能力配置。Gemini 模板启用音频输入，Qwen 模板启用视频输入；其余音视频开关默认关闭。
模板按经过核验的具体模型与连接声明能力，运行时不根据厂商名称推断。模型与格式限制见[输入模态契约](../specs/provider-configuration.md#输入模态契约)。
