# 供应商类型与协议类型拆分

> 文档类型：现行规格
> 代码基线：2026-09-02

## 目标

Provider 配置把用户实例、产品配置和线协议表达为三个独立概念，避免厂商品牌、配置入口和 adapter dispatch
共用同一个枚举：

| 字段 | 语义 | 示例 |
|---|---|---|
| `provider_id` | 用户拥有的配置实例 id，也是模型路由第一段 | `work-kimi` |
| `provider_type` | 产品层供应商档案或通用配置入口 | `kimi`、`openai-compatible` |
| `protocol` | adapter 使用的已注册线协议 | `openai-chat-completions`、`anthropic-messages` |

`provider_id` 不参与协议推断；相同厂商可以有多个用户实例，多个厂商也可以复用同一个协议 adapter。
Endpoint 的 reasoning capability/profile 同样不参与 adapter 分派；它只在选定协议内声明该模型的真实控制能力和
原生参数映射。

## 产品类型与协议

集成厂商类型包括 `openai`、`anthropic`、`gemini`、`deepseek`、`qwen`、`kimi`、`glm`、`grok`、
`openrouter` 和 `mistral`。它们提供已核验的模板，并由 `ProviderTypeSpec` 定义默认和允许的协议。

通用配置入口包括：

- `openai-compatible`：固定使用 `openai-chat-completions`；
- `anthropic-compatible`：固定使用 `anthropic-messages`。

两种 compatible 类型都允许用户为自定义 Endpoint 声明思考配置。协议档案同时限制 adapter 能忠实保存的
continuation：OpenAI Chat Completions 支持 `none/text`，Anthropic Messages 支持 `none/opaque`。不兼容的
声明在配置或 adapter 构造时失败，不能接受后再丢弃。

`faux` 类型和协议只保留给内部安全运行时与测试，不属于用户可配置类型，也不出现在用户 Provider 目录中。当前注册协议是 `faux`、
`openai-chat-completions` 和 `anthropic-messages`。不接受任意协议字符串，也不通过 provider id 猜测厂商。

持久化配置必须同时写入类型和协议：

```json
{
  "provider_id": "work-kimi",
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
- `ouro/application` 暴露同时包含 `provider_type` 与 `protocol` 的类型化 DTO，并编排配置用例；
- CLI 只解析创建、更新或替换意图并格式化结果，冲突规则由配置/application 层执行。

厂商模板不会产生一厂商一个 adapter。Gemini、DeepSeek、Qwen、KIMI、GLM、Grok、OpenRouter 和 Mistral 当前都
复用 OpenAI Chat Completions adapter；Anthropic 使用 Messages adapter。

## 创建、更新与替换

Provider mutation 使用三个明确操作：

- `provider add`：唯一创建入口；根据 type 为集成厂商原子装载模板，或为 compatible 类型创建空 Provider；
  `provider_id` 已存在时失败并提示 update/replace，不写入任何字段；
- `provider update`：只更新现有 Provider；模板来源类型使用持久化旧模板基线做三方合并，只更新未被用户
  修改的模板值，保留未知、修改或主动删除的 Endpoint，并报告同 id 冲突；compatible 类型只修改连接、凭据、
  timeout、retry 和 enabled；
- `provider replace`：只替换现有 Provider；内置类型装载新类型模板，compatible 类型清空原 Endpoint、凭据和
  扩展设置。

模板更新优先根据持久化模板绑定定位同来源模板；旧配置首次更新才根据现有 `provider_type` 安全接管。持久化的
type/protocol 组合仍必须合法。所有冲突与校验在一次写盘前完成；失败时配置保持不变。交互 Host 遇到 Provider
同 id 创建冲突时应让用户选择新 id、更新或替换，不能自行改变操作语义。

## 非目标

当前不引入动态协议插件、任意请求 schema、供应商专属 adapter 基类或历史配置自动迁移。新增原生协议时，
只需增加协议类型、协议档案、adapter 构造器和允许该协议的产品档案，并用边界测试证明 factory 仍不依赖厂商
名称。Reasoning profile 仅是经过校验的静态请求片段，不是动态 schema 或可执行插件。

## 验收

- 公开 Provider/Template DTO 同时展示产品类型和协议；
- 持久化配置缺失协议会失败，不从旧类型或 id 推导；
- 产品类型与协议的不合法组合会失败；
- runtime options 和 adapter registry 只携带/读取协议；
- 相同 `provider_id` 的创建不会静默修改既有配置；
- 更新与替换具有不同、原子且经过 CLI 集成测试保护的状态变化。
