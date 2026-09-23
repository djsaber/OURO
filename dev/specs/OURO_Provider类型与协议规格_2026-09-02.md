# 供应商类型与协议类型拆分

> 文档类型：现行规格
> 代码基线：2026-09-02

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
Endpoint 的 reasoning capability/profile 同样不参与 adapter 分派；它只在选定协议内声明该模型的真实控制能力和
原生参数映射。

## 产品类型与协议

集成厂商类型包括 `openai`、`openai-codex`、`anthropic`、`gemini`、`deepseek`、`qwen`、`kimi`、`glm`、`grok`、
`openrouter` 和 `mistral`。它们提供已核验的模板。`ProviderTypeSpec` 保存可选默认连接组合和明确的备选组合；
每个 `ProviderConnectionSpec` 包含 protocol、auth_driver 及可选服务规则引用，managed 组合必须声明服务规则引用。
连接声明可携带只读 notice，由 Application 投影到 Provider/Template DTO 并交给 CLI/TUI 展示；不写入用户配置，也不由 Host 判断厂商。
`openai` 默认使用 Responses + static，保留 Chat Completions + static 备选组合，已有配置不自动改协议。
`openai-codex` 仅声明 Responses + 同名浏览器认证驱动及服务策略。两套模板提供 GPT-5.5、GPT-5.6 Luna/Terra/Sol
与 GPT-6 Astra/Sol/Luna，默认 GPT-6 Sol；模型等级与各自后端能力按官方文档分别声明，详见
[预制配置说明](../../docs/OURO_OpenAI预制配置说明_ZH_2026-09-19.md)。订阅上下文和 max_output_tokens 为本地预算，
服务限制通过连接 notice 和模板描述披露；图像能力仍受 Responses adapter 当前实现约束。
默认组合自然属于允许集合；组合不能重复，也不能将两组独立允许列表作笛卡尔积。有默认连接的产品在创建时，
省略的协议/认证选择取各自默认值，再验证完整组合。持久化读取中省略 auth_driver 固定表示 static，不根据产品猜测其他认证。

通用配置入口为 `custom`，没有默认协议，创建时必须显式选择 `openai-chat-completions`、
`openai-responses` 或 `anthropic-messages`。三个组合都使用 static 认证；选择 Responses 不授予订阅认证能力。
`ProviderTypeSpec.default_connection` 可为空，`connections` 提供完整允许集合；没有默认连接时，
省略协议会明确失败，省略认证仍使用 static。

`custom` 类型允许用户为自定义 Endpoint 声明思考配置。协议档案同时限制 adapter 能忠实保存的
continuation：OpenAI Chat Completions 支持 `none/text`，Responses 与 Anthropic Messages 支持 `none/opaque`。不兼容的
声明在配置或 adapter 构造时失败，不能接受后再丢弃。

`faux` 类型和协议只保留给内部安全运行时与测试，不属于用户可配置类型，也不出现在用户 Provider 目录中。当前注册协议是 `faux`、
`openai-chat-completions`、`openai-responses` 和 `anthropic-messages`。不接受任意协议字符串，也不通过 provider id 猜测厂商。Responses 注册不代表订阅产品已开放；其参数和 static 续接范围见[Responses 规格](OURO_Responses编解码规格_2026-09-19.md)。

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

配置读取不包含历史推导分支。缺少 `protocol`、未注册协议或不被产品档案允许的组合都明确返回
`ConfigError`，且读取本身不修改文件。

## 分层职责

- `ouro/config/provider_models.py` 定义类型、协议档案、组合校验和持久化领域模型；
- `ouro/config/provider_templates.py` 提供厂商产品档案的连接和 Endpoint 默认值；
- `ouro/providers/factory.py` 只按 `ProviderProtocol` 注册表选择 adapter，不读取 `provider_type`；
- `ouro/application` 暴露包含 `provider_type`、`protocol` 与 `auth_driver` 的类型化 DTO，并编排配置用例；
- CLI 只解析创建、更新或替换意图并格式化结果，冲突规则由配置/application 层执行。

厂商模板不会产生一厂商一个 adapter。Gemini、DeepSeek、Qwen、KIMI、GLM、Grok、OpenRouter 和 Mistral 当前都
复用 OpenAI Chat Completions adapter；Anthropic 使用 Messages adapter。

## 命名约定

协议标识使用 API 的完整名称，Python 模块使用对应的 snake_case，公开实现类保留 Provider 后缀：

| 协议标识 | 实现模块 | 实现类 |
|---|---|---|
| `openai-chat-completions` | `openai_chat_completions.py` | `OpenAIChatCompletionsProvider` |
| `openai-responses` | `openai_responses.py` | `OpenAIResponsesProvider` |
| `anthropic-messages` | `anthropic_messages.py` | `AnthropicMessagesProvider` |

编解码器及流式处理模块沿用协议前缀，例如 `openai_chat_completions_codec.py`、`openai_responses_stream.py`。
跨模块的协议专用 reasoning 构造函数和预设同样明确 API 名称；模块内的局部变量可使用 message、usage 等简洁名称。
HTTP 路径统一称为 `api_path`，Endpoint 表示模型路由。`provider_id` 始终表示框架生成的实例身份，不随类型或协议名称推断、重写。
协议无关的 `ProviderCompletion` 表示调用结束原因，与 Chat Completions API 无关。

## 创建、更新与替换

CLI 的 `provider add/replace --protocol` 将显式协议传递到共享 Application 配置用例；custom 类型必须显式指定协议，
集成类型省略时使用模板协议。显式协议与模板不一致时拒绝写盘，用户应使用 `custom` 类型配置自定义连接。
TUI 的 Add provider 菜单在 Custom connection 分组直接提供三个协议入口，各自预选协议与默认 API 路径。
Provider 设置复用 `EditableChoiceScreen` 和 `ChoiceScreen`，从类型连接档案中筛选当前认证驱动支持的协议。
新建自定义供应商可选择协议；仅有一种协议、模板供应商及已有供应商显示只读协议。切换协议时，仍为旧协议默认值的
API 路径跟随新协议默认值，用户自定义路径保持不变。协议选择经 `TuiProviderConfiguration` 传入 Application，
不增加 Responses 专属供应商类型，也不在界面层复制连接组合校验。

Provider mutation 使用三个明确操作：

- `provider add`：唯一创建入口；根据 type 为集成厂商原子装载模板，或为 `custom` 类型创建空 Provider；
  接收必填展示名并返回框架生成的 `provider_id`；
- `provider update`：只更新现有 Provider；模板来源类型使用持久化旧模板基线做三方合并，只更新未被用户
  修改的模板值，保留未知、修改或主动删除的 Endpoint，并报告同 id 冲突；`custom` 类型只修改连接、凭据、
  timeout、retry 和 enabled；
- `provider replace`：只替换现有 Provider；内置类型装载新类型模板，`custom` 类型清空原 Endpoint、凭据和
  扩展设置。

模板更新优先根据持久化模板绑定定位同来源模板；旧配置首次更新才根据现有 `provider_type` 安全接管。持久化的
type/protocol 组合仍必须合法。所有冲突与校验在一次写盘前完成；失败时配置保持不变。交互 Host 遇到 Provider
更新与替换以已生成 id 选择目标；Host 不提供编辑内部身份的入口。

## 非目标

当前不引入动态协议插件、任意请求 schema、供应商专属 adapter 基类或历史配置自动迁移。新增原生协议时，
只需增加协议类型、协议档案、adapter 构造器和允许该协议的产品档案，并用边界测试证明 factory 仍不依赖厂商
名称。Reasoning profile 仅是经过校验的静态请求片段，不是动态 schema 或可执行插件。

## 验收

- 公开 Provider/Template DTO 展示产品类型、协议和认证选择；
- 持久化配置缺失协议会失败，不从旧类型或 id 推导；
- 产品类型与协议的不合法组合会失败；
- runtime options 携带认证引用和产品服务规则标识，adapter registry 仍只按协议分派；
- 相同展示名的多次创建会生成不同内部身份且不会静默修改既有配置；
- 更新与替换具有不同、原子且经过 CLI 集成测试保护的状态变化。
