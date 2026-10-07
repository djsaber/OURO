# OpenAI 预制配置

> 文档类型：本地预制配置参考
> 配置事实来源：[provider_templates.py](../../ouro/config/provider_templates.py)；代码记录的模板核对日期为 2026-09-23。

`openai`（API Key，模板修订 5）和 `openai-codex`（浏览器订阅，模板修订 6）均声明以下七个模型，默认模型为 `gpt-6-sol`。表格描述仓库模板，不是厂商实时模型清单，也不表示每个账户、模型和参数都已真实验收。

表中的模型 ID 与实例 Endpoint ID 不同：创建实例时 OURO 生成 `provider_...` 和 `endpoint_...`，模板模型 ID 作为 `model_id` 保存，不能直接拼成实例路由。

| 模型 ID / 官方参考资料 | API 模板声明的 effort | 订阅模板声明的 effort |
|---|---|---|
| [gpt-5.5](https://developers.openai.com/api/docs/models/gpt-5.5) | none、low、medium、high、xhigh | low、medium、high、xhigh |
| [gpt-5.6-luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) | none、low、medium、high、xhigh、max | low、medium、high、xhigh、max |
| [gpt-5.6-terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra) | none、low、medium、high、xhigh、max | low、medium、high、xhigh、max |
| [gpt-5.6-sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol) | none、low、medium、high、xhigh、max | low、medium、high、xhigh、max |
| [gpt-6-astra](https://developers.openai.com/api/docs/models/gpt-6-astra) | low、medium、high、xhigh、max | low、medium、high、xhigh、max |
| [gpt-6-sol](https://developers.openai.com/api/docs/models/gpt-6-sol) | none、low、medium、high、xhigh、max | low、medium、high、xhigh、max |
| [gpt-6-luna](https://developers.openai.com/api/docs/models/gpt-6-luna) | none、low、medium、high、xhigh、max | low、medium、high、xhigh、max |

API 模板的 none 映射到 OURO 的 reasoning disabled；Astra 模板不提供关闭思考。
订阅模板默认使用 medium，不把 API 的 none 推定为订阅后端能力。Codex 的 Ultra 涉及多代理执行，不是这里的单请求 effort；见[官方模型与等级说明](https://learn.chatgpt.com/docs/models)。
请求使用原生 `reasoning: {effort: ...}`，完整加密 reasoning 仍由 opaque 续接保存。

## 协议、预算与能力

两个模板均使用 Responses。API 地址为 `https://api.openai.com/v1/responses`；订阅使用既有
受限服务目标与浏览器认证。预制模板不设置 temperature、top_p、top_logprobs；参数映射以当前模板和 adapter 为准。

| 配置 | API Key 模板 | 订阅模板 |
|---|---|---|
| context_window | 1,050,000，本地规划值 | 1,050,000，本地规划值；未验证订阅后端极限 |
| max_output_tokens | 32,768，实际发送的默认请求预算 | 32,768，仅本地预算，不发送；未核实订阅后端输出上限 |
| 文本 / 流式 / 函数工具 | 开启 | 开启 |
| 结构化输出 | 开启，Responses text.format | 未声明支持 |
| 图像输入 | 未开启，OURO Responses adapter 尚未实现 | 未开启 |
| 计费 | API 用量计费 | 订阅归属，未取得价格时金额未知 |

模型目录能力不能替代 OURO adapter 的实现能力。服务预算和能力分别声明，不从 API 模板推导订阅限制，也不写入无法完整表达计费条件的固定价格。

外部资料核对于 2026-10-07：[GPT-6 Sol API 页面](https://developers.openai.com/api/docs/models/gpt-6-sol)列出的上下文为 1,050,000、输出上限为 128,000；这不是 OURO 的默认输出预算，也不是订阅后端极限的验证结果。

static 认证的 opaque 重放绑定认证实例生命周期；重建 API Key adapter 后通过通用聊天与工具帧继续任务，
不发送旧 opaque 状态。浏览器订阅使用稳定账户 scope，跨进程可重放兼容状态。详细边界见
[Responses 规格](responses-codec.md)。

## 创建与更新

```powershell
uv run --frozen ouro provider add "OpenAI API" --type openai --home ./.ouro-dev --json
uv run --frozen ouro provider list --home ./.ouro-dev --json
```

API 实例通过 `OPENAI_API_KEY` 环境变量取凭据；订阅实例使用独立持久凭据，流程见
[订阅使用指南](../guides/subscriptions.md)。从创建 / 查询输出读取 Provider ID，再使用 `provider endpoint list <provider_id>` 获取 Endpoint ID；路由为 `<provider_id>/<endpoint_id>`。

内联模板按修订号更新未被用户修改的本地模板；用户修改过的模板按既有规则保留并提示。
已有实例可显式执行 `ouro provider update <provider_id>`，复用三方合并并保留原认证引用及用户修改。操作时指定与创建相同的 home。
先前 `openai` 实例的 Chat Completions 协议仍合法，不会被静默转换；更新时若协议不符会明确报错并保持文件不变。
需要新 Responses 配置时创建另一实例，或审查既有自定义设置后显式 `provider replace <id> --type openai`。
replace 会重建该实例的端点和设置，不用于仅刷新模型清单。

截至 2026-10-07，官方公告 GPT-5.5 将于 2026-10-14 从 ChatGPT、ChatGPT Work 和 Codex 退役，OpenAI API 不受该公告影响。模板包含该模型不保证其持续可用；应结合账户实际可用模型更新已有配置。详见[官方退役说明](https://learn.chatgpt.com/docs/models#gpt-55-retirement)。OURO 遇到失败不会自动回退到付费 API 或其他模型。

现有真实验收仍只覆盖 Windows/browser/gpt-5.5 的默认参数、刷新、工具与续接；GPT-6 Sol/Luna 及其余模型和
显式等级已通过配置和合成 HTTP 定向测试，尚未在 OURO 中逐项真实调用。
