# Provider 类型与协议规格

> 文档类型：现行规格

## 目标

Provider 配置分别表达用户实例、产品配置、线协议和认证选择，避免厂商品牌、配置入口和 adapter dispatch
共用同一个枚举：

| 字段 | 语义 | 示例 |
|---|---|---|
| `provider_id` | 框架生成的内部实例身份，也是模型路由第一段 | `provider_<generated>` |
| `display_name` | 用户配置的必填展示名 | `Work Kimi` |
| `provider_type` | 产品层供应商档案或通用配置入口 | `kimi`、`custom` |
| `protocol` | adapter 使用的已注册线协议 | `openai-chat-completions`、`openai-responses`、`anthropic-messages` |
| `auth_driver` | 认证方式的稳定标识；省略为 static | `static` |

`provider_id` 不参与协议推断且不接受用户配置；相同厂商或相同展示名可以有多个实例，多个厂商也可以复用同一个协议 adapter。
Endpoint 的 reasoning capability/profile 在选定协议内声明生成控制项及其原生参数映射，不参与 adapter 分派。

## 产品类型与协议

集成厂商类型包括 `openai`、`openai-codex`、`anthropic`、`gemini`、`deepseek`、`qwen`、`kimi`、`glm`、`grok`、
`mistral`、`minimax`、`doubao`、`cohere`、`opencode-go`、`opencode-go-responses` 和 `opencode-go-messages`。
它们提供带来源与核对日期的本地模板；模板声明不代表逐个账户或模型均已通过真实验收。`ProviderTypeSpec` 保存可选默认连接组合和明确的备选组合；
每个 `ProviderConnectionSpec` 包含 protocol、auth_driver 及可选服务规则引用，managed 组合必须声明服务规则引用。
连接声明可携带只读 notice，由 Application 投影到 Provider/Template DTO 并交给 CLI/TUI 展示；不写入用户配置，也不由 Host 判断厂商。
`openai` 默认使用 Responses + static，支持 Chat Completions + static 备选组合。
三个 OpenCode Go 类型分别使用 Chat Completions、Responses 和 Anthropic Messages，均通过静态 API Key 认证。
`openai-codex` 仅声明 Responses + 同名浏览器认证驱动及服务策略。模型目录与后端能力见
[预置供应商配置](provider-presets.md)。订阅上下文和有限的 max_output_tokens 用于本地预算；空上限不预留输出空间，
服务限制通过连接 notice 和模板描述披露；两套模板均通过 Responses adapter 支持图像输入。
默认组合自然属于允许集合；组合不能重复，也不能将两组独立允许列表作笛卡尔积。有默认连接的产品在创建时，
省略的协议/认证选择取各自默认值，再验证完整组合。持久化读取中省略 auth_driver 固定表示 static，不根据产品猜测其他认证。

通用配置入口为 `custom`，没有默认协议，创建时必须显式选择 `openai-chat-completions`、
`openai-responses`、`anthropic-messages` 或具名插件协议 `<plugin_id>/providers/<id>`。
这些组合使用 static 认证；选择 Responses 不授予订阅认证能力。插件协议的配置、请求租约和不可用行为见
[Provider 配置规格](provider-configuration.md#插件协议)。
`ProviderTypeSpec.default_connection` 可为空，`connections` 提供完整允许集合；没有默认连接时，
省略协议会明确失败，省略认证仍使用 static。

`custom` 类型允许用户为自定义 Endpoint 声明思考请求配置。协议适配器独立解析可读思考和原生续接状态，
不由启停、等级或摘要配置过滤返回内容。规范思考文本随会话进入模型消息；Chat Completions 通过
Endpoint 的 `reasoning_text_field` 选择 `reasoning_content` 或 `reasoning`；未声明时请求省略这两个字段。
同源 `reasoning_details` 独立回传。字段与原生续接的选择规则见[端点配置规格](provider-configuration.md)。
Responses 和 Messages 使用各自原生 reasoning item 与 thinking block，没有可用原生续接时只编码正文与工具调用。
三种协议均不将思考拼入助手正文。规范会话保存可读思考；Responses 的完整输出项、
Messages 的完整 content 序列及 Chat Completions 含非文字信息的思考数组作为协议重放附件独立保存，
回传时校验来源与协议结构。有效原生表示不重复附加可读副本。

`faux` 类型和协议只保留给内部安全运行时与测试，不属于用户可配置类型，也不出现在用户 Provider 目录中。
内置协议为 `faux`、`openai-chat-completions`、`openai-responses` 和 `anthropic-messages`；
插件协议使用独立的具名贡献身份，不等同于内部 `faux`。协议字段不接受任意字符串，也不通过 provider id 猜测厂商。
Responses 协议选择不授予订阅认证能力；订阅还需要对应产品声明和认证驱动。参数和 static 续接范围见
[Responses 规格](responses-codec.md)。

持久化配置必须同时写入类型和协议：

```json
{
  "display_name": "Work Kimi",
  "provider_type": "kimi",
  "protocol": "openai-chat-completions",
  "credential_ref": "env:MOONSHOT_API_KEY",
  "base_url": "https://api.moonshot.ai/v1"
}
```

配置读取要求显式声明合法协议。缺少 `protocol`、非法协议身份或不被产品档案允许的组合都明确返回
`ConfigError`，且读取本身不修改文件。合法插件协议的配置读取不要求插件当前已激活；执行时解析其可用贡献，
插件禁用或缺失不会自动删除配置或回退到其他协议。

## 分层职责

- `ouro/config/provider_models.py` 定义类型、协议档案、组合校验和持久化领域模型；
- `ouro/config/provider_templates/` 提供厂商产品档案的连接和 Endpoint 默认值；
- `ouro/providers/factory.py` 按内置协议注册表或组合根提供的插件构造入口选择 adapter，不读取 `provider_type`；
- `ouro/application` 暴露包含 `provider_type`、`protocol` 与 `auth_driver` 的类型化 DTO，并编排配置用例；
- CLI 只解析创建、更新或替换意图并格式化结果，冲突规则由配置/application 层执行。

厂商模板不会产生一厂商一个 adapter。Gemini、DeepSeek、Qwen、KIMI、GLM、Grok、Mistral、MiniMax、豆包和 Cohere 当前都
复用 OpenAI Chat Completions adapter；Anthropic 使用 Messages adapter。

`ProviderProtocolSpec` 是已注册 adapter 的实现档案，保存默认 API 路径、连接要求、标准摘要请求选项
和 `encodable_input_modalities`。输入集合只描述 OURO 的编码实现范围，不代表同协议下所有服务与模型都支持这些输入。

| Adapter | 可编码的媒体输入 |
| --- | --- |
| OpenAI Chat Completions | `image`、`audio`、`video`；视频使用 `video_url` 扩展 |
| OpenAI Responses | `image` |
| Anthropic Messages | `image` |
| Faux | 空集合；内部占位实现不编码媒体 |

Endpoint 通过三个 `supports_<modality>_input` 字段独立声明具体服务与模型的输入能力；对内置协议，声明集合必须是上述集合的子集。
配置、模板、TUI 草稿与 adapter 构造共享该校验；三种媒体模态遵循同一规则，不因图片能力或协议名称推导音视频能力。
插件协议的输入编码由其公开贡献契约负责，不套用内置协议的编码集合。
格式、角色、配置入口和降级规则见[输入模态契约](provider-configuration.md#输入模态契约)。

三个协议共用会话角色投影。端点声明 `supports_inline_system_messages=true` 时，框架通知在原位置呈现为 `system`，随通知交付的媒体使用相邻的 `user` 内容块。默认值 `false` 将整条通知呈现为 `user`。基础系统指令使用各协议的原生入口。Anthropic Messages 将同批原生工具结果编码在一个用户回合中。

显式原生 `system` 消息保留指令角色。仅支持前置指令的端点把开头的系统消息计入指令区，在对话开始后收到显式系统消息会报告协议不匹配。

OpenAI 公共 Responses 预设开启对话内系统消息。Claude 公共 Messages 对系统消息的前后角色有额外约束，其预设采用默认值。[Responses 输入类型](https://developers.openai.com/api/reference/resources/responses/methods/create/) · [Claude 系统消息位置](https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages)

## 提示缓存用量

适配器在协议完成边界将上游计数归一为 `Usage`。整份用量缺失时返回 `None`，仅缺少缓存计数时，
`cache_read_tokens` 为零。入账与汇总见[用量与计费规格](usage-and-billing.md)。

- **Responses（含 Codex）**从非流式响应的 `usage.input_tokens_details.cached_tokens`，或
  `response.completed` 中同一响应字段读取。输入总量直接使用 `usage.input_tokens`。
  [OpenAI SDK](https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_usage.py) ·
  [Codex SSE 解码](https://github.com/openai/codex/blob/main/codex-rs/codex-api/src/sse/responses.rs#L123-L156)
- **Chat Completions**从响应或流式分片的顶层 `usage` 读取总量和 `prompt_tokens_details.cached_tokens`。
  OpenAI 支持用端点 `request_options.stream_options.include_usage` 请求末尾的用量分片，其 `choices` 为空。
  兼容端点也可在含 `finish_reason` 的分片中报告用量。流内按字段保留最新报告值，优先采用标准计数，
  兼容值补足缺失的总量及缺失或无效的标准缓存计数。缓存计数接受非负整数（含显式零），已知输入总量时须在该总量以内。
  [OpenAI 流事件](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events/) ·
  [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/) ·
  [vLLM 流式实现](https://github.com/vllm-project/vllm/blob/main/vllm/entrypoints/openai/chat_completion/serving.py)
- **Anthropic Messages**从非流式响应的 `usage.cache_read_input_tokens` 读取。流式的 `message_start.message.usage`
  和 `message_delta.usage` 按字段合并累计值，显式零更新旧值，缺失或 `null` 保留旧值。收到 `message_stop` 后将
  `input_tokens`、`cache_read_input_tokens` 和 `cache_creation_input_tokens` 合为输入总量。
  端点可通过 `request_options.cache_control` 配置 `{"type":"ephemeral"}` 以启用服务端提示缓存。
  [Anthropic 用量类型](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/usage.py) ·
  [Anthropic 流式用量合并](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/lib/streaming/_messages.py) ·
  [Claude 提示缓存](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

**Chat Completions 兼容字段**（来源核对于 2026-11-25）

| 缓存读取字段 | 来源 |
| --- | --- |
| `usage.prompt_cache_hit_tokens` | [DeepSeek 上下文缓存](https://api-docs.deepseek.com/guides/kv_cache/) · [LangChain 解码](https://github.com/langchain-ai/langchain/blob/master/libs/partners/deepseek/langchain_deepseek/chat_models.py#L53-L91) |
| `usage.cached_tokens` | [nanobot 兼容解码](https://github.com/HKUDS/nanobot/blob/main/nanobot/providers/openai_compat_provider.py#L1295-L1334) |
| 流式 `x_groq.usage.prompt_tokens_details.cached_tokens` | [Groq SDK 分片类型](https://github.com/groq/groq-python/blob/main/src/groq/types/chat/chat_completion_chunk.py#L378-L460) · [Vercel AI SDK 解码](https://github.com/vercel/ai/blob/main/packages/groq/src/groq-chat-language-model.ts) |
| 流式 `choices[0].usage` 内的 `prompt_tokens_details.cached_tokens` 或 `cached_tokens` | [OWASP AppSec Agent 响应形态](https://github.com/OWASP/appsec-agent/blob/main/CHANGELOG.md#382---2026-08-18) |

## 命名约定

协议标识使用 API 的完整名称，Python 模块使用对应的 snake_case，公开实现类保留 Provider 后缀：

| 协议标识 | 实现模块 | 实现类 |
|---|---|---|
| `openai-chat-completions` | `ouro/providers/openai_chat_completions/adapter.py` | `OpenAIChatCompletionsProvider` |
| `openai-responses` | `ouro/providers/openai_responses/adapter.py` | `OpenAIResponsesProvider` |
| `anthropic-messages` | `ouro/providers/anthropic_messages/adapter.py` | `AnthropicMessagesProvider` |

协议子包内的 `codec.py` 负责请求和响应转换；Responses 的 `stream.py`、`items.py`、`errors.py` 分别承载流式状态、原生 item 与错误处理。
跨模块的协议专用 reasoning 构造函数和预设同样明确 API 名称；模块内的局部变量可使用 message、usage 等简洁名称。
HTTP 路径统一称为 `api_path`，Endpoint 表示模型路由。`provider_id` 始终表示框架生成的实例身份，不随类型或协议名称推断、重写。
协议无关的 `ProviderCompletion` 表示调用结束原因，与 Chat Completions API 无关。

## 创建、更新与替换

CLI 的 `provider add/replace --protocol` 将显式协议传递到共享 Application 配置用例；custom 类型必须显式指定协议，
集成类型省略时使用模板协议。显式协议与模板不一致时拒绝写盘，用户应使用 `custom` 类型配置自定义连接。
TUI 的 Add provider 菜单在 Custom connection 分组直接提供三个协议入口，各自预选协议与默认 API 路径。
Provider 设置复用 `EditableChoiceScreen` 和 `ChoiceScreen`，从类型连接档案中筛选当前认证驱动支持的协议。
自定义供应商的协议由上级连接入口确定，创建表单不提供协议切换；模板供应商及已有供应商显示只读协议。
插件协议通过 CLI 创建。协议经 `TuiProviderConfiguration` 传入 Application，
不增加 Responses 专属供应商类型，也不在界面层复制连接组合校验。

Provider mutation 使用三个明确操作：

- `provider add`：唯一创建入口；根据 type 为集成厂商原子装载模板，或为 `custom` 类型创建空 Provider；
  接收必填展示名并返回框架生成的 `provider_id`；
- `provider update`：只更新现有 Provider；模板来源类型使用持久化旧模板基线做三方合并，只更新未被用户
  修改的模板值，保留未知、修改或主动删除的 Endpoint，并报告同 id 冲突；`custom` 类型只修改连接、凭据、
  timeout、retry 和 enabled；
- `provider replace`：只替换现有 Provider；内置类型装载新类型模板，`custom` 类型清空原 Endpoint、凭据和
  扩展设置。

模板更新根据持久化模板绑定定位同来源模板；缺少绑定时拒绝更新。持久化的
type/protocol 组合必须合法。所有冲突与校验在一次写盘前完成；失败时配置保持不变。交互 Host 遇到 Provider
更新与替换以已生成 id 选择目标；Host 不提供编辑内部身份的入口。

## 扩展边界

内置协议通过协议档案、adapter 构造器和允许该协议的产品档案接入；插件协议通过公开 Provider contribution 接入，
两者均须保持 factory 与厂商品牌无关。SDK 扩展方式见[插件开发指南](../development/plugin-development.md)。
Reasoning profile 只描述生成控制项的静态请求片段。
历史数据转换由独立升级流程负责，配置读取不执行隐式迁移。

## 验收

- 公开 Provider/Template DTO 展示产品类型、协议和认证选择；
- 持久化配置缺失协议会失败，不从旧类型或 id 推导；
- 产品类型与协议的不合法组合会失败；
- runtime options 携带认证引用和产品服务规则标识，adapter registry 仍只按协议分派；
- 相同展示名的多次创建会生成不同内部身份且不会静默修改既有配置；
- 更新与替换具有不同、原子且经过 CLI 集成测试保护的状态变化。
