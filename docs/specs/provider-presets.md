# 预置供应商配置

> 文档类型：配置参考
> 配置事实来源：[provider_templates.py](../../ouro/config/provider_templates.py)；代码记录的模板核对日期为 2026-10-10。

预置目录包含 13 个模板、47 个模型端点，覆盖通用、编程、多模态及成本敏感场景。
端点声明模型 ID、上下文预算、输出预算、工具调用、图像输入、流式输出、结构化输出与思考配置。
OpenRouter 等聚合平台通过 `custom` 与兼容协议配置。

## 模型目录与官方依据

视觉能力指图像输入。表格描述 OURO 模板声明；服务可用性取决于厂商、账户和区域。

| 模板 | 默认模型 | 官方依据 |
|---|---|---|
| OpenAI / Codex | `gpt-6.1-sol` | [API 模型](https://developers.openai.com/api/docs/models/gpt-6.1-sol)、[订阅目录](https://learn.chatgpt.com/docs/models) |
| Anthropic | `claude-sonnet-5-5` | [模型目录](https://platform.claude.com/docs/en/models/overview)、[思考模式](https://platform.claude.com/docs/en/build-with-claude/thinking) |
| Gemini | `gemini-3.8-flash` | [模型目录](https://ai.google.dev/gemini-api/docs/models)、[思考档位](https://ai.google.dev/gemini-api/docs/thinking) |
| DeepSeek | `deepseek-v4-pro` | [模型与限制](https://api-docs.deepseek.com/quick_start/pricing/)、[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/) |
| Qwen | `qwen3.8-max` | [模型选择](https://help.aliyun.com/zh/model-studio/text-generation-model)、[视觉能力](https://help.aliyun.com/zh/model-studio/vision-model/) |
| Kimi | `kimi-k3` | [模型目录](https://platform.kimi.com/docs/models)、[K3 档位](https://platform.kimi.com/docs/guide/use-reasoning-effort) |
| GLM | `glm-5.3` | [模型目录](https://docs.bigmodel.cn/cn/guide/start/model-overview) |
| Grok | `grok-4.7` | [模型说明](https://docs.x.ai/developers/grok-4-7)、[Chat Completions](https://docs.x.ai/developers/model-capabilities/legacy/chat-completions) |
| Mistral | `mistral-medium-3-5` | [模型目录](https://docs.mistral.ai/models)、[Large 4](https://docs.mistral.ai/models/mistral-large-4-0)、[推理格式](https://docs.mistral.ai/studio/conversations/reasoning) |
| MiniMax | `MiniMax-M3` | [OpenAI 接口](https://platform.minimax.cn/docs/api-reference/text-openai-api)、[参数与预算](https://platform.minimax.cn/docs/api-reference/text-chat-openai) |
| Doubao | `doubao-seed-2-1-pro-260915` | [模型目录](https://docs.volcengine.com/docs/ark/model-list?lang=zh)、[思考模式](https://docs.volcengine.com/docs/ark/deep-thinking?lang=zh) |
| Cohere | `command-a-plus-05-2026` | [模型说明](https://docs.cohere.com/docs/command-a-plus)、[OpenAI 接口](https://docs.cohere.com/docs/compatibility-api) |

## 连接配置

| 类型 | Base URL | 协议 | 凭据 |
|---|---|---|---|
| `openai` | `https://api.openai.com/v1` | Responses | `OPENAI_API_KEY` |
| `openai-codex` | `https://chatgpt.com/backend-api/codex` | Responses | 浏览器订阅认证 |
| `anthropic` | `https://api.anthropic.com` | Anthropic Messages | `ANTHROPIC_API_KEY` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` | Chat Completions | `GEMINI_API_KEY` |
| `deepseek` | `https://api.deepseek.com` | Chat Completions | `DEEPSEEK_API_KEY` |
| `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | Chat Completions | `DASHSCOPE_API_KEY` |
| `kimi` | `https://api.moonshot.ai/v1` | Chat Completions | `MOONSHOT_API_KEY` |
| `glm` | `https://open.bigmodel.cn/api/paas/v4` | Chat Completions | `ZAI_API_KEY` |
| `grok` | `https://api.x.ai/v1` | Chat Completions | `XAI_API_KEY` |
| `mistral` | `https://api.mistral.ai/v1` | Chat Completions | `MISTRAL_API_KEY` |
| `minimax` | `https://api.minimax.cn/v1` | Chat Completions | `MINIMAX_API_KEY` |
| `doubao` | `https://ark.cn-beijing.volces.com/api/v3` | Chat Completions | `ARK_API_KEY` |
| `cohere` | `https://api.cohere.ai/compatibility/v1` | Chat Completions | `COHERE_API_KEY` |

Responses、Chat Completions 和 Anthropic Messages 的 API 路径分别为 `/responses`、`/chat/completions` 和 `/v1/messages`。
API Key 必须与模板所选平台、区域匹配。MiniMax 使用中国站，豆包使用北京区域；豆包模型需先在方舟开通。

## 端点字段

以下详情表中的上下文与输出预算以 token 为单位，图像列对应 `supports_vision`，结构化输出列对应
`supports_structured_output`。所有预置端点均支持文本、工具调用和流式输出。
`context_window` 用于上下文规划；`max_output_tokens` 是默认请求预算，Codex 订阅仅将其用于本地规划。
表中的思考默认值对应 `reasoning.capability.default`；effort 为开启思考时可选的等级。

## OpenAI API 与订阅

`openai`（API Key）和 `openai-codex`（浏览器订阅）均声明以下八个模型，默认模型为 `gpt-6.1-sol`。

表中的模型 ID 与实例 Endpoint ID 不同：创建实例时 OURO 生成 `provider_...` 和 `endpoint_...`，模板模型 ID 作为 `model_id` 保存，不能直接拼成实例路由。

| 模型 ID / 官方参考资料 | API 模板声明的 effort | 订阅模板声明的 effort |
|---|---|---|
| [gpt-5.5](https://developers.openai.com/api/docs/models/gpt-5.5) | none、low、medium、high、xhigh | low、medium、high、xhigh |
| [gpt-5.6-luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) | none、low、medium、high、xhigh、max | low、medium、high、xhigh、max |
| [gpt-5.6-terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra) | none、low、medium、high、xhigh、max | low、medium、high、xhigh、max |
| [gpt-5.6-sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol) | none、low、medium、high、xhigh、max | low、medium、high、xhigh、max |
| [gpt-6-astra](https://developers.openai.com/api/docs/models/gpt-6-astra) | low、medium、high、xhigh、max | low、medium、high、xhigh、max |
| [gpt-6.1-sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol) | low、medium、high、xhigh、max | low、medium、high、xhigh、max |
| [gpt-6-sol](https://developers.openai.com/api/docs/models/gpt-6-sol) | none、low、medium、high、xhigh、max | low、medium、high、xhigh、max |
| [gpt-6-luna](https://developers.openai.com/api/docs/models/gpt-6-luna) | none、low、medium、high、xhigh、max | low、medium、high、xhigh、max |

API 模板的 none 映射到 OURO 的 reasoning disabled；Astra 和 GPT-6.1 Sol 模板不提供关闭思考。
两套模板默认使用 medium，订阅模板不提供关闭思考选项。Codex 的 Ultra 涉及多代理执行，不属于单请求 effort；见[官方模型与等级说明](https://learn.chatgpt.com/docs/models)。
请求使用原生 `reasoning: {effort: ...}`，完整加密 reasoning 由 opaque 续接保存。

### 预算与能力

两个模板均使用 Responses。API 地址为 `https://api.openai.com/v1/responses`；订阅使用
受限服务目标与浏览器认证。预制模板不设置 temperature、top_p、top_logprobs；参数映射以当前模板和 adapter 为准。

| 配置 | API Key 模板 | 订阅模板 |
|---|---|---|
| context_window | 1,050,000，本地规划值 | 1,050,000，本地规划值；未验证订阅后端极限 |
| max_output_tokens | 32,768，实际发送的默认请求预算 | 32,768，仅本地预算，不发送；未核实订阅后端输出上限 |
| 文本 / 流式 / 函数工具 | 开启 | 开启 |
| 结构化输出 | 开启，Responses text.format | 未声明支持 |
| 图像输入 | 开启，使用 Responses input_image | 开启，使用同一 Responses 图像编码 |
| 计费 | API 用量计费 | 订阅归属，未取得价格时金额未知 |

模板预算用于 OURO 的上下文规划与请求控制。厂商限制见各模型官方页面；
[GPT-6.1 Sol API](https://developers.openai.com/api/docs/models/gpt-6.1-sol)的上下文为 1,050,000、输出上限为 128,000。

static 认证的 opaque 重放绑定认证实例生命周期；重建 API Key adapter 后通过通用聊天与工具帧继续任务，
不发送旧 opaque 状态。浏览器订阅使用稳定账户 scope，跨进程可重放兼容状态。详细边界见
[Responses 规格](responses-codec.md)。

GPT-5.5 的订阅服务可用期截至 2026-10-14，OpenAI API 不受影响，见[官方退役说明](https://learn.chatgpt.com/docs/models#gpt-55-retirement)。
OURO 遇到失败不会自动回退到付费 API 或其他模型。

## Anthropic

| 模型 ID | 上下文 | 输出预算 | 图像 | 结构化输出 | 思考开关 / 默认 | effort |
|---|---:|---:|---|---|---|---|
| `claude-fable-5-1` | 1,000,000 | 32,768 | 开启 | 关闭 | 始终开启 / high | low、medium、high、xhigh、max |
| `claude-sonnet-5-5`（默认） | 1,000,000 | 32,768 | 开启 | 关闭 | 始终开启 / high | low、medium、high、xhigh、max |
| `claude-opus-5-5` | 1,000,000 | 32,768 | 开启 | 关闭 | 始终开启 / medium | low、medium、high、xhigh、max |
| `claude-haiku-5-5` | 1,000,000 | 32,768 | 开启 | 关闭 | 可切换，默认开启 / medium | low、medium、high、xhigh、max |

请求使用 `thinking.type=adaptive` 与 `output_config.effort`。Haiku 关闭思考时发送
`thinking.type=disabled` 和 `output_config.effort=medium`；其余三个端点不提供关闭选项。
思考输出为摘要，通过 opaque continuation 保存带签名的 thinking 块。

## Gemini

| 模型 ID | 上下文 | 输出预算 | 图像 | 结构化输出 | 思考开关 / 默认 | effort |
|---|---:|---:|---|---|---|---|
| `gemini-3.8-flash`（默认） | 1,048,576 | 32,768 | 开启 | 开启 | 始终开启 / medium | low、medium、high |
| `gemini-3.1-pro-preview` | 1,048,576 | 32,768 | 开启 | 开启 | 始终开启 / high | low、medium、high |
| `gemini-3.5-flash-lite` | 1,048,576 | 32,768 | 开启 | 开启 | 始终开启 / minimal | minimal、low、medium、high |

通过 Chat Completions 兼容接口发送 `reasoning_effort`。模板不声明可展示的思考输出与 continuation。

## DeepSeek

| 模型 ID | 上下文 | 输出预算 | 图像 | 结构化输出 | 思考开关 / 默认 | effort |
|---|---:|---:|---|---|---|---|
| `deepseek-v4-pro`（默认） | 1,000,000 | 32,768 | 关闭 | 开启 | 可切换，默认开启 / high | low、high、max |
| `deepseek-flash` | 1,000,000 | 32,768 | 关闭 | 开启 | 可切换，默认开启 / high | low、high、max |

使用 `thinking.type=enabled/disabled` 切换思考，开启时通过 `reasoning_effort` 选择等级。
思考输出为文本，工具轮次通过 `reasoning_content` 续接。

## Qwen

| 模型 ID | 上下文 | 输出预算 | 图像 | 结构化输出 | 思考开关 / 默认 | effort |
|---|---:|---:|---|---|---|---|
| `qwen3.8-max`（默认） | 1,000,000 | 131,072 | 开启 | 开启 | 可切换，默认开启 | — |
| `qwen3.8-flash` | 1,000,000 | 131,072 | 开启 | 开启 | 可切换，默认开启 / xhigh | low、medium、xhigh |
| `qwen3.7-plus` | 1,000,000 | 131,072 | 开启 | 开启 | 可切换，默认开启 | — |

使用 `enable_thinking` 控制开关，`preserve_thinking` 随开关设置为相同布尔值；Flash 通过
`reasoning_effort` 选择等级。思考输出为文本，工具轮次通过 `reasoning_content` 续接。

## Kimi

| 模型 ID | 上下文 | 输出预算 | 图像 | 结构化输出 | 思考开关 / 默认 | effort |
|---|---:|---:|---|---|---|---|
| `kimi-k3`（默认） | 1,048,576 | 32,768 | 开启 | 开启 | 始终开启 / max | low、high、max |
| `kimi-k2.7-code` | 262,144 | 32,768 | 开启 | 开启 | 始终开启 | — |
| `kimi-k2.7-code-highspeed` | 262,144 | 32,768 | 开启 | 开启 | 始终开启 | — |
| `kimi-k2.6` | 262,144 | 32,768 | 开启 | 开启 | 可切换，默认开启 | — |

K3 使用顶层 `reasoning_effort`；K2.7 Code 系列不发送思考开关或 effort 参数。K2.6 使用
`thinking.type=enabled/disabled`，开启时附带 `thinking.keep=all`。思考输出为文本，工具轮次通过 `reasoning_content` 续接。

## GLM

| 模型 ID | 上下文 | 输出预算 | 图像 | 结构化输出 | 思考开关 / 默认 | effort |
|---|---:|---:|---|---|---|---|
| `glm-5.3`（默认） | 1,000,000 | 128,000 | 关闭 | 开启 | 始终开启 / max | low、high、max |
| `glm-5.3-flash` | 1,000,000 | 128,000 | 开启 | 开启 | 始终开启 / max | low、high、max |
| `glm-5.2` | 1,000,000 | 32,768 | 关闭 | 开启 | 可切换，默认开启 | — |
| `glm-5-turbo` | 200,000 | 32,768 | 关闭 | 开启 | 可切换，默认开启 | — |
| `glm-5v-turbo` | 200,000 | 32,768 | 开启 | 关闭 | 可切换，默认开启 | — |

使用 `thinking.type=enabled/disabled`；5.3 与 5.3 Flash 固定 enabled，并通过 `reasoning_effort` 选择等级。
除 5.3 外，端点在开启思考时设置 `thinking.clear_thinking=false`。思考输出为文本，工具轮次通过 `reasoning_content` 续接。

## Grok

| 模型 ID | 上下文 | 输出预算 | 图像 | 结构化输出 | 思考开关 / 默认 | effort |
|---|---:|---:|---|---|---|---|
| `grok-4.7`（默认） | 500,000 | 128,000 | 开启 | 开启 | 始终开启 / high | low、medium、high、xhigh |

通过 `reasoning_effort` 选择等级，展示思考摘要，不声明 continuation。`provider_output_limit=unlimited`
记录厂商输出限制声明；表中的 128,000 是 OURO 默认请求预算。

## Mistral

| 模型 ID | 上下文 | 输出预算 | 图像 | 结构化输出 | 思考开关 / 默认 | effort |
|---|---:|---:|---|---|---|---|
| `mistral-medium-3-5`（默认） | 256,000 | 32,768 | 开启 | 开启 | 固定关闭 | — |
| `mistral-small-2603` | 256,000 | 32,768 | 开启 | 开启 | 固定关闭 | — |
| `mistral-large-4-0` | 1,000,000 | 32,768 | 开启 | 开启 | 固定关闭 | — |

三个端点均固定发送 `reasoning_effort=none`，不提供思考开关或档位。Chat Completions adapter 按文本解码，
不处理 Mistral 的 thinking chunk。`mistral-small-2603` 对应 Small 4，`mistral-large-4-0` 对应 Large 4 Public Preview。

## MiniMax

| 模型 ID | 上下文 | 输出预算 | 图像 | 结构化输出 | 思考开关 / 默认 | effort |
|---|---:|---:|---|---|---|---|
| `MiniMax-M3`（默认） | 1,000,000 | 131,072 | 开启 | 关闭 | 可切换，默认开启 | — |
| `MiniMax-M2.7` | 204,800 | 65,536 | 关闭 | 关闭 | 始终开启 | — |
| `MiniMax-M2.7-highspeed` | 204,800 | 65,536 | 关闭 | 关闭 | 始终开启 | — |

M3 使用 `thinking.type=adaptive/disabled`；M2.7 系列不发送思考控制参数。所有端点发送
`reasoning_split=true`，展示思考文本，并通过 `reasoning_content` 保存工具轮次续接。

## 豆包

| 模型 ID | 上下文 | 输出预算 | 图像 | 结构化输出 | 思考开关 / 默认 | effort |
|---|---:|---:|---|---|---|---|
| `doubao-seed-2-1-pro-260915`（默认） | 1,048,576 | 32,768 | 开启 | 开启 | 可切换，默认开启 / high | minimal、low、medium、high |
| `doubao-seed-2-1-lite-260915` | 1,048,576 | 32,768 | 开启 | 开启 | 可切换，默认开启 / high | minimal、low、medium、high |

使用 `thinking.type=enabled/disabled` 切换思考，开启时通过 `reasoning_effort` 选择等级。
展示并回传 `reasoning_content` 思考摘要；Chat Completions adapter 不保存 `encrypted_content` 加密原文，
多轮工具调用的推理效果受此限制。

## Cohere

| 模型 ID | 上下文 | 输出预算 | 图像 | 结构化输出 | 思考开关 / 默认 | effort |
|---|---:|---:|---|---|---|---|
| `command-a-plus-05-2026`（默认） | 128,000 | 32,768 | 开启 | 开启 | 可切换，默认开启 / high | high |

使用 `reasoning_effort=high/none` 映射思考开关。模板不声明可展示的思考输出与 continuation。

## 使用方式

```powershell
uv run --frozen ouro provider templates --home ./.ouro-dev
uv run --frozen ouro provider templates minimax --home ./.ouro-dev --json
uv run --frozen ouro provider add "MiniMax" --type minimax --home ./.ouro-dev --json
uv run --frozen ouro provider list --home ./.ouro-dev --json
```

创建命令装载整组预置端点，并从上表所列环境变量获取 API Key。订阅实例使用独立持久凭据，流程见
[订阅使用指南](../guides/subscriptions.md)。从命令输出取得 Provider ID 与 Endpoint ID，路由格式为 `<provider_id>/<endpoint_id>`。
创建、更新和替换操作见[配置指南](../guides/providers.md)；模板文件与版本契约见[供应商模板规格](provider-templates.md)。
