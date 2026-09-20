# 订阅接入验证

`openai_codex` 是实施计划 A 的独立、显式联网实验。它不注册生产 Provider/driver，
不启动 OURO Runtime，不读取其他客户端缓存，也不读写 OURO_HOME、配置或 Session。
Token 和完整 Responses output 只保留在本进程内存中；退出后必须重新登录。
浏览器授权和 Token 解码/交换/刷新复用 `ouro/auth/drivers` 的实现；公开上游 client id
只由本实验显式注入。设备轮询和验证编排仍属于实验层；默认模式的完整 output item 收集复用正式 `providers/openai_responses_items.py`，`--adapter` 模式直接使用正式 Responses adapter。

## 运行

在仓库根目录运行，`--model` 指定本次要核验的精确模型，不表示 OURO 已支持该模型：

```powershell
uv run --frozen python -m dev.probes.openai_codex --live --method device --model gpt-5.5
uv run --frozen python -m dev.probes.openai_codex --live --method browser --model gpt-5.5 --open-browser
```

程序将登录地址/设备码显示到 stderr。设备码需在 ChatGPT 安全设置中启用；浏览器方法监听
`127.0.0.1:1455`，注册回调为 `http://localhost:1455/auth/callback`。端口被占用时明确失败，
不会停止其他客户端；可改用 device。只有指定 `--open-browser` 才打开浏览器。
登录最多等待 15 分钟，Ctrl+C 取消并释放监听器、连接和任务。

浏览器的 `Authorization callback received. Return to OURO to finish signing in.` 仅提示返回 OURO，
不表示全部验证已完成。
默认模式回调之后仍可能正在交换 Token，随后执行刷新及四次模型请求。终端会即时在 stderr 显示
等待授权/交换、刷新、四请求验证三个阶段；最终 JSON 仍只写 stdout 一次。

默认模式成功登录后自动执行一次刷新，再执行四次小型订阅模型请求：

1. 最小 SSE 文本响应；
2. 强制调用本地 `ouro_probe_echo(round=1)`；
3. 原样重放完整 output items 与工具结果，再调用 `ouro_probe_echo(round=2)`；
4. 继续重放完整上下文，检查模型确认两个工具结果。

echo 仅返回内存中的固定 JSON，不访问文件或执行模型指定的代码。无付费 API 回退、自动重试、
远端会话或 `previous_response_id`。四次请求会使用当前账户的订阅额度。

Responses 内容由 `response.output_item.done` 的完整 item 收集，按 `output_index` 恢复原始顺序。
`response.completed` 确认成功终态和 usage；其 output 可以为空或省略。有终态 output 快照时按
相同位置核对并补充缺失字段，不重复拼接文本或工具；同位置身份/内容冲突、跨位置重复 item id、
索引缺口、已添加却未完成的 item、终态后的 item 均失败。文本 delta 不再另外拼入重放上下文，
加密 reasoning 和工具 item 保留完整结构。只有完整 item、没有成功终态仍不能通过验证。

stdout 只输出一次 JSON 摘要；退出码 0 表示本次全部通过，1 表示失败，2 表示命令参数错误，
130 表示取消。可将 stdout 单独重定向到本地文件，**不要合并 stderr**，后者含登录交互材料：

```powershell
uv run --frozen python -m dev.probes.openai_codex --live --method device --model gpt-5.5 > probe-result.json
```

摘要只包含固定来源 revision、模型、已完成阶段、事件类型计数、output/content 类型、文本字符数、
加密 reasoning 是否出现、实际 token 用量及固定错误码/HTTP 状态。每个已收到完成响应的场景在校验前
记录脱敏观察值，`validated` 区分已收到响应与断言通过，失败仍保留此前观察。文本确认标记允许
周围有 Markdown、标点或说明文字，但空文本、缺失标记和嵌入其他单词的标记仍失败。
未知事件/项目只记为 `unknown`，不保存
Token、账户标识、授权码、回调 URL、模型文本、工具 arguments、完整 output 或服务原始错误。
授权任务通过驱动/实验各自的 `AuthError` 子类保留已经脱敏的固定错误码和 HTTP 状态，便于区分
网络失败、服务错误与材料解码失败；未知异常仍由现有 `TaskAuthorizationAttempt` 收敛为
`authorization_failed`，不导出异常文本或服务响应。
摘要不是可重放的真实协议 fixture，也不能证明所有模型能力或生产支持。

## 正式 adapter 验证

在已有浏览器实验通过后，使用以下命令验证正式 `OpenAIResponsesProvider` 的真实请求：

```powershell
uv run --frozen python -m dev.probes.openai_codex --live --method browser --model gpt-5.5 --adapter --max-output-tokens 8192 --open-browser
```

`--adapter` 仍是内存实验，不注册产品、不读取或保存凭据文件。认证头由正式 OpenAI driver 编码；
探针通过通用 RequestAuthenticator 注入快照，最终目标固定为订阅 Responses 地址，不增加任何生产品牌分支。
此模式先用登录材料取得文本与完整 continuation，再显式刷新一次，随后通过同一个 adapter 重放首轮
完整内容，执行两轮固定 echo 工具及最终文本确认。四个请求均使用正式 codec、SSE 解码、scope 检查与关闭路径。

`--max-output-tokens` 仅用于 `--adapter`，省略时沿用 8192；正整数值按正式 ModelMetadata 校验。
此 Codex 集成接受没有请求级输出硬上限的限制：通过注入的服务策略声明 supports_output_limit=false，
adapter 不发送该字段，max_output_tokens 仅保留本地预算语义，实际输出可能超过这个值。
启动提示和 JSON 摘要明确披露此限制；探针本身只验证适配器请求，本地上下文预算由 Runtime 使用。
不引入 server-default 或独立输出预留，不根据 HTTP 错误自动删参数、重试或切换服务。
正式请求同时包含 store=false 和 encrypted reasoning include；truncation 省略，沿用协议默认 disabled，工具 schema 保持
正式 codec 的 strict=false。本地 ModelMetadata 的默认上下文窗口不是该订阅模型的已核验能力。

[官方 Responses 参数说明](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
定义 max_output_tokens 为输出上限，包含可见输出和 reasoning tokens；truncation 默认 disabled，超限时失败。
这些标准协议契约不能代替 Codex 订阅后端的实际核验。
如果请求失败，摘要保留当前场景、HTTP 状态与固定错误码，不输出服务原始错误或静默调整请求。
HTTP 400/422 的场景还包含 request_error：只提取白名单内的 parameter/reason，或将完整匹配的
参数拒绝句子转换为同样的固定字段；未知格式返回空对象，不能从 HTTP 状态或字段名提及推断拒绝原因。
诊断观察器仅在本次模型请求期间安装，正常结束、失败或取消时移除，不改变正式 transport 或认证策略。

摘要中 request_path=production_adapter 标识实际路径，max_output_tokens 记录本地输出预算，
output_limit_enforced=false 表示该服务请求没有设置输出硬上限。
responses 使用 canonical_event_counts（正式 adapter 事件计数），并记录实际 canonical usage、项目类型、
文本字符数与验证结果；不会输出文本、完整 items、工具参数、账户 scope 或 Token。
全部通过后，最后场景的 scope_stable_after_refresh=true 表示前后认证 scope 一致且重放通过。
encrypted_reasoning_replayed 仅在首轮确实返回加密 reasoning 并跨刷新重放成功时为 true；false 不代表该能力已验收。

真实浏览器登录仍需用户完成；离线测试只证明程序编排和边界行为。用户提供的 Windows/browser/gpt-5.5
正式 adapter 实测摘要已通过：status=passed，completed_steps 包含 authorization、refresh、responses，
request_path=production_adapter，max_output_tokens=8192，output_limit_enforced=false，
expiry_provided=true，refresh_token_rotated=true。四个场景均 validated=true，且各有一次 continuation/done：

| 场景 | 完整 output 类型 | input / output tokens | 文本字符数 | 加密 reasoning |
|---|---|---|---|---|
| adapter_text | reasoning、message | 24 / 18 | 7 | 有 |
| adapter_tool_1 | function_call | 163 / 20 | 0 | 无 |
| adapter_tool_2 | function_call | 222 / 20 | 0 | 无 |
| adapter_continuation | message | 170 / 9 | 13 | 无 |

末轮 scope_stable_after_refresh=true、encrypted_reasoning_replayed=true，确认此账户/模型/授权方式
下正式 adapter 跨刷新重放首轮加密 reasoning，并完成两轮工具调用/结果续接。末轮本身没有新加密
reasoning 不影响前序状态已成功重放的事实。工具由探针提供固定无副作用结果，不替代 Runtime 工具执行验收。

参数限制的实测依据为 HTTP 400 的安全诊断：显式 truncation 和 max_output_tokens 分别被拒绝为
unsupported_parameter。当前正式 adapter 按协议默认行为省略 truncation，按显式服务声明省略输出上限，
该请求组合已经通过上述验证；默认服务策略仍发送上限，不能将此限制推广到标准 Responses。

这份证据是用户提供的真实运行摘要，不包含原始 SSE/items，不作为可重放协议 fixture。production_support=false
表示本探针不验收完整产品交付。浏览器产品现已内建注册并完成持久凭据下的真实 CLI 闭环；设备码、其余能力参数及
实际上下文超限行为仍需独立验收。正式入口及操作见[订阅使用指南](../../docs/OURO_订阅支持使用指南_ZH_2026-09-19.md)。

## 已核验的来源与实验参数

来源固定为目标设计中的两个提交：

- OpenAI Codex：`04581f96044a9fe6062593c4d557036477aee961`。
  [client id 与刷新](https://github.com/openai/codex/blob/04581f96044a9fe6062593c4d557036477aee961/codex-rs/login/src/auth/manager.rs)、
  [浏览器回调与交换](https://github.com/openai/codex/blob/04581f96044a9fe6062593c4d557036477aee961/codex-rs/login/src/server.rs)、
  [设备码](https://github.com/openai/codex/blob/04581f96044a9fe6062593c4d557036477aee961/codex-rs/login/src/device_code_auth.rs)。
- OpenCode：`e03db9bc6908f75c9334d8aa997deeaac81c0298`。
  [Codex 插件](https://github.com/anomalyco/opencode/blob/e03db9bc6908f75c9334d8aa997deeaac81c0298/packages/opencode/src/plugin/openai/codex.ts)。
- [官方认证说明](https://developers.openai.com/zh-Hans/docs/auth)与
  [Responses SSE 说明](https://developers.openai.com/api/docs/guides/streaming-responses)。

| 参数 | 实验使用值与依据 |
|---|---|
| client id | 两份源码均使用 `app_EMoamEEZ73f0CkXaXp7hrann`；仅作为公开客户端兼容实验，不代表 OURO 已获得注册或官方支持 |
| issuer | `https://auth.openai.com`；不允许命令行改写秘密发送目标 |
| 浏览器 | `/oauth/authorize`，随机 state、S256 PKCE，`http://localhost:1455/auth/callback` |
| scopes | `openid profile email offline_access`，与 OpenCode 一致；官方固定版本额外请求 connectors scopes，探针不请求连接器权限 |
| originator | `ouro-auth-probe`，不冒充上游客户端名称 |
| 设备码 | `/api/accounts/deviceauth/usercode` 申请，`/api/accounts/deviceauth/token` 轮询；403/404 为 pending；返回授权码与 PKCE 材料后再交换 |
| 设备交换 redirect | `https://auth.openai.com/deviceauth/callback` |
| Token | `/oauth/token`；授权码与刷新使用表单编码（OpenCode 路径）；官方刷新使用 JSON，不能把编码差异当成新授权方式 |
| 模型目标 | `https://chatgpt.com/backend-api/codex/responses`，Bearer 与 `ChatGPT-Account-Id` |
| 请求模式 | `stream=true`、`store=false`、完整本地上下文、请求加密 reasoning；没有温度或服务端输出上限参数 |

网络 Token 响应只解析所需账户路由元数据，不将 JWT 当成本地权益证明；不选第一个组织。
账户缺失/冲突、刷新改变账户、受区域约束的材料均明确失败。缺少 `expires_in` 时保留未知，
不编造一小时有效期；刷新仍由实验显式执行一次。所有秘密请求禁止跟随重定向，TLS 保持默认验证。

## 验证边界

Windows 浏览器方法已有用户手动执行的脱敏摘要：`authorization`、`refresh` 完成，
`expiry_provided=true`、`refresh_token_rotated=true`；`gpt-5.5` 四请求验证全部通过，
总状态为 `passed`：

| 场景 | 完整 output 类型 | input / output / total tokens | reasoning tokens |
|---|---|---|---|
| minimal_sse | reasoning、message | 24 / 18 / 42 | 9 |
| tool_round_1 | function_call | 143 / 20 / 163 | 0 |
| tool_round_2 | function_call | 202 / 20 / 222 | 0 |
| tool_continuation | reasoning、message | 158 / 24 / 182 | 13 |

两个文本响应均收到加密 reasoning；两轮工具只返回 function_call，因此这份结果证明完整工具
item/结果的续接，尚不单独证明跨请求重放加密 reasoning。文本和工具响应均具有成功终态与 usage。
该后端已观察到终态 output 为空、完整内容位于 item 事件中的行为，不能仅依赖 completed.output。
这些是用户提供的观察摘要，完整 items 的内容未采集；回归测试使用合成协议数据，
不将其称为真实 SSE fixture。跨刷新 reasoning 重放已由上文正式 adapter 验证覆盖；设备授权、其他模型及能力限制仍需独立验证。

固定 Codex 源码也分别处理
[`OutputItemDone` 与 `Completed`](https://github.com/openai/codex/blob/04581f96044a9fe6062593c4d557036477aee961/codex-rs/codex-api/src/sse/responses.rs)；
[官方 SSE 示例](https://developers.openai.com/api/reference/typescript/resources/beta/subresources/responses/methods/create)
展示了 item 事件的 `output_index` 与完整 item。Platform 的示例包含最终 output 快照，不能据此要求
Codex 后端必须重复携带同样的快照。

离线测试以合成 Token、HTTP mock、真实 loopback socket 验证请求契约、PKCE、错误 state、拒绝、
重复 callback、端口冲突、pending、时限/取消、轮换、重定向禁止、安全摘要、两轮工具续接、
item/终态快照的三种组合、交错顺序与去重、未完成 item、冲突及缺失/重复终态。
生产 `ouro` 不得反向 import 实验代码；测试保护此边界。

```powershell
uv run --frozen pytest -q tests/probes tests/auth/test_openai_protocol.py tests/auth/test_openai_driver.py tests/auth/test_authorization.py tests/architecture
uv run --frozen ruff check ouro/auth/drivers dev/probes tests/auth tests/probes
uv run --frozen mypy --explicit-package-bases ouro/auth dev/probes
```

其他真实账户/授权方式、首批模型的 reasoning 等级/上下文窗口/显式参数限制、区域与权益错误、图像及结构化
输出、可重放脱敏 fixture、POSIX 文件保护仍需独立证据。探针的通过只证明某次账户/模型/方法组合，
不会自动满足实施计划 A 的全部退出条件。
