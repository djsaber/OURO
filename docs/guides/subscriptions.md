# 模型订阅支持

OURO 通过产品连接声明、认证驱动和协议适配器组合接入订阅服务。内建产品
`openai-codex`，使用浏览器登录、受限本地文件保存凭据和 Responses 协议；API Key 产品保持独立。
此集成采用已验证的公开 Codex OAuth 客户端兼容流程，不表示 OpenAI 官方对 OURO 的支持承诺。

模型列表、默认模型、推理等级和本地预算由[预置供应商配置](../specs/provider-presets.md)统一说明。
模板启用图像输入，OURO 使用 Responses 编码器提交图片；这不等于每个账户和模型均已完成真实验收。
设备码与结构化输出未开放。CLI/TUI 会披露订阅服务的本地预算限制。

## 创建与登录

首次配置建议使用独立 home。以下 PowerShell 命令不改变已有 OURO 配置；后续所有命令使用
同一个 home。Provider ID 由 OURO 生成，与显示名称不同；若已有实例，先用 `provider list --json` 取得其 ID，再直接登录，不要重复 add 或 replace。

```powershell
$subscriptionHome = Join-Path $env:LOCALAPPDATA 'OURO-subscription-check'
$subscriptionCreated = uv run --frozen ouro provider add "Personal Codex" --type openai-codex --default --home "$subscriptionHome" --json | ConvertFrom-Json
$subscriptionProviderId = $subscriptionCreated.provider.provider_id
uv run --frozen ouro provider auth login $subscriptionProviderId --method browser --open-browser --home "$subscriptionHome"
uv run --frozen ouro provider auth status $subscriptionProviderId --home "$subscriptionHome" --json
uv run --frozen ouro provider check $subscriptionProviderId --home "$subscriptionHome" --json
```

浏览器回调后等待终端显示 Login succeeded，才表示凭据已保存。成功的 status 应为 ready；status 仅查询
本地状态，不验证账户当前权益。check 会发出模型请求，应返回 ok=true。独立探针不保存凭据，因此其
登录成功不能代替此处正式登录。无需导入 Codex/ChatGPT 客户端缓存，也不要手工编辑凭据文件。

## 使用、重启与续接

```powershell
uv run --frozen ouro run "只回复 OURO_OK" --home "$subscriptionHome" --json
```

保存输出中的 session_id，重新启动命令，用该 ID 续接同一会话：

```powershell
uv run --frozen ouro run "请复述刚才的回复" --session <session_id> --home "$subscriptionHome" --json
```

TUI 使用 `uv run --frozen ouro --home "$subscriptionHome"`。从 Manage providers 进入显示名称为 Personal Codex 的实例，再进入
Authentication 可查看状态、登录和注销。未登录仍能启动和进入管理界面；请求时才返回认证错误。
现有工具循环、分支、恢复、压缩和子会话路由共用该认证组合；不另外建立订阅专用执行流程。

材料临近到期时自动刷新；模型请求首次 401 且尚未输出时，在既有重试预算内恢复认证。普通刷新保持
账户 scope，不使历史续接失效。失效凭据要求重新登录；认证失败不会切换到 API Key 或另一计费服务。
订阅请求仍记录实际 token，用量金额未知时不显示为零费用。

## 注销

```powershell
uv run --frozen ouro provider auth logout $subscriptionProviderId --home "$subscriptionHome" --json
uv run --frozen ouro provider check $subscriptionProviderId --home "$subscriptionHome" --json
```

注销清空本地材料；此后 check 应返回 authentication_required，不发出模型请求。重新执行登录命令
即可恢复使用。注销不会退出浏览器上的 ChatGPT，也不调用远端全账户登出。

## 证据范围

仓库中的真实验收记录使用 Windows、浏览器授权与当时的 gpt-5.5 默认参数组合，不代表当前默认模型或服务可用性。
当前模板默认值见[预置供应商配置](../specs/provider-presets.md)，待验证范围见
[订阅验证计划](../plans/subscription-validation.md)。已有证据分为三层：

| 层次 | 覆盖范围 |
|---|---|
| 正式 adapter 联网验证 | 登录、刷新、两轮工具续接、跨刷新加密 reasoning 重放；方法见[协议探针](../../dev/probes/README.md#正式-adapter-验证) |
| 正式 CLI 持久凭据验证 | 内建模板、浏览器登录保存、新进程 check/run、按 Session 续接、本地注销及注销后拒绝请求 |
| 自动集成测试 | 合成 HTTP、真实本地回调与凭据文件、文件/SQLite 会话、401 刷新续接、工具执行、计费归属、发送目标保护和未登录管理 |

真实 CLI 验证不强制凭据过期，自动 401 恢复由合成 HTTP 集成测试覆盖。非交互 Run 与自动任务在未登录时
返回结构化错误；登录后使用现有 continue_run 恢复，不为后台任务打开浏览器。

其他模型、设备码、实际上下文极限、其他账户/区域和 POSIX 文件保护需要各自的验证，不能由上述范围推断。
