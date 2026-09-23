# OpenAI 预制配置

核对日期：2026-09-23。`openai`（API Key，模板修订 5）和 `openai-codex`
（浏览器订阅，模板修订 6）均提供以下七个精确模型 ID，默认端点为 `gpt-6-sol`。
模板核对表示官方目录与参数已核对，不等同于 OURO 对每个账户、模型和参数完成真实调用验收。
GPT-6 Sol/Luna 于 2026-09-22 发布，官方公告其进入 ChatGPT Work 与 Codex
（Plus、Pro、Business、Enterprise、Edu）并逐步放量。

| 模型 ID / 官方资料 | API reasoning effort | 订阅模板开放的 effort |
|---|---|---|
| [gpt-5.5](https://developers.openai.com/api/docs/models/gpt-5.5) | none、low、medium、high、xhigh | low、medium、high、xhigh |
| [gpt-5.6-luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) | none、low、medium、high、xhigh、max | low、medium、high、xhigh、max |
| [gpt-5.6-terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra) | none、low、medium、high、xhigh、max | low、medium、high、xhigh、max |
| [gpt-5.6-sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol) | none、low、medium、high、xhigh、max | low、medium、high、xhigh、max |
| [gpt-6-astra](https://developers.openai.com/api/docs/models/gpt-6-astra) | low、medium、high、xhigh、max | low、medium、high、xhigh、max |
| [gpt-6-sol](https://developers.openai.com/api/docs/models/gpt-6-sol) | none、low、medium、high、xhigh、max | low、medium、high、xhigh、max |
| [gpt-6-luna](https://developers.openai.com/api/docs/models/gpt-6-luna) | none、low、medium、high、xhigh、max | low、medium、high、xhigh、max |

API 的 none 映射到 OURO 的 reasoning disabled；Astra 不提供关闭思考。
订阅模板按 [Codex 模型与等级说明](https://learn.chatgpt.com/docs/models)声明显式等级，默认使用 medium，
不把 API 的 none 推定为订阅后端能力，也不把 Codex 的 Ultra 多代理模式编码成 reasoning effort。
请求使用原生 `reasoning: {effort: ...}`，完整加密 reasoning 仍由 opaque 续接保存。

## 协议、预算与能力

两个模板均使用 Responses。API 地址为 `https://api.openai.com/v1/responses`；订阅使用既有
受限服务目标与浏览器认证。[Astra 官方指南](https://developers.openai.com/api/docs/guides/latest-model)
明确其工具调用要求 Responses，且不接受 temperature、top_p、top_logprobs；预制模板不发送这些参数。

| 配置 | API Key 模板 | 订阅模板 |
|---|---|---|
| context_window | 1,050,000，按上述模型文档 | 1,050,000，按相同模型的 API 文档设置本地规划值；未验证订阅后端极限 |
| max_output_tokens | 32,768，实际发送的默认请求预算；文档上限为 128,000 | 32,768，仅本地预算，不发送；未核实订阅后端输出上限 |
| 文本 / 流式 / 函数工具 | 开启 | 开启 |
| 结构化输出 | 开启，Responses text.format | 未声明支持 |
| 图像输入 | 未开启，OURO Responses adapter 尚未实现 | 未开启 |
| 计费 | API 用量计费 | 订阅归属，未取得价格时金额未知 |

各模型的 API 文档均列出图像输入能力，但模板能力必须同时满足当前 adapter 能力，不能仅根据模型目录开启。
服务预算和能力分别声明，不从 API 模板复制订阅限制。API 价格有长上下文等计费条件，本次不写入无法完整表达的固定价格。

当前 static 认证的 opaque 续接绑定认证实例生命周期；重建 API Key adapter 后不能重放旧 opaque 状态，
会明确拒绝。浏览器订阅使用稳定账户 scope，已支持跨进程续接。详细边界见
[Responses 规格](../dev/specs/OURO_Responses编解码规格_2026-09-19.md)。

## 创建与更新

```powershell
uv run --frozen ouro provider add "OpenAI API" --type openai
uv run --frozen ouro provider add "Personal Codex" --type openai-codex
uv run --frozen ouro provider auth login personal --method browser --open-browser
```

API 实例通过 `OPENAI_API_KEY` 环境变量取凭据；订阅实例使用独立持久凭据，流程见
[订阅使用指南](OURO_订阅支持使用指南_ZH_2026-09-19.md)。指定模型时使用实际实例 ID，例如 `api/gpt-6-astra`。

内联模板按修订号更新未被用户修改的本地模板；用户修改过的模板按既有规则保留并提示。
已有订阅实例可显式执行 `ouro provider update personal`，复用三方合并并保留原认证引用及用户修改。
先前 `openai` 实例的 Chat Completions 协议仍合法，不会被静默转换；更新时若协议不符会明确报错并保持文件不变。
需要新 Responses 配置时创建另一实例，或审查既有自定义设置后显式 `provider replace <id> --type openai`。
replace 会重建该实例的端点和设置，不用于仅刷新模型清单。

官方已公告 GPT-5.5 将于 2026-10-14 从 ChatGPT 登录的 Codex 退役，API 不受此公告影响。
因此模板保留用户要求的 GPT-5.5，但默认使用 GPT-6 Sol。订阅模型实际可用性受账户、客户端和服务发布进度影响，
依据见上述 Codex 模型说明；失败不会回退到付费 API 或其他模型。

现有真实验收仍只覆盖 Windows/browser/gpt-5.5 的默认参数、刷新、工具与续接；GPT-6 Sol/Luna 及其余模型和
显式等级已通过配置和合成 HTTP 定向测试，尚未在 OURO 中逐项真实调用。
