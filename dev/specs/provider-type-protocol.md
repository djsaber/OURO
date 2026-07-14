# 供应商类型与协议类型拆分

状态：目标（设计提案，尚未实现）

## 问题

当前 `provider_id` 是用户配置实例标识，而 `provider_type` 的值是 `openai-compatible`、
`anthropic-compatible` 等线协议适配器标识。内置厂商身份只存在于模板目录中，因此出现三个问题：

- 用户选择“OpenAI”“KIMI”或“自定义 OpenAI-compatible”后，持久化配置无法表达这些不同的配置类型；
- adapter dispatch、产品配置入口和厂商默认值共用 `provider_type`，后续新增原生协议时容易继续混淆；
- `provider configure` 在实例 id 相同时直接更新现有对象，创建与修改的意图不够明确。

本提案只定义目标契约，不改变当前运行时、CLI 或配置文件。

## 推荐模型

目标配置保留三个正交概念：

| 字段 | 语义 | 示例 |
|---|---|---|
| `provider_id` | 用户拥有的配置实例 id，也是模型路由第一段 | `work-kimi` |
| `provider_type` | 产品层配置类型/供应商配置档案 | `kimi`、`openai-compatible`、`custom` |
| `protocol` | adapter 使用的线协议 | `openai-chat-completions`、`anthropic-messages` |

`provider_type` 建议包含两组值：

- 集成厂商：`openai`、`anthropic`、`gemini`、`deepseek`、`qwen`、`kimi`、`glm`、
  `openrouter`、`mistral`；
- 通用配置入口：`openai-compatible`、`anthropic-compatible`、`custom`。

集成厂商类型提供已核验的协议、Base URL、凭据环境变量和 Endpoint 模板。通用 compatible 类型仍提供熟悉的
快捷入口，但 adapter dispatch 只读取独立的 `protocol`。`custom` 不代表允许任意未注册协议；它要求用户从
框架已经支持的协议中显式选择，并填写连接参数。

建议的持久化形态：

```json
{
  "provider_id": "work-kimi",
  "provider_type": "kimi",
  "protocol": "openai-chat-completions",
  "credential_ref": "env:MOONSHOT_API_KEY",
  "base_url": "https://api.moonshot.ai/v1"
}
```

## 默认值与约束

- 集成厂商的 `protocol` 由供应商配置档案确定，普通配置流程不要求用户再次选择；高级覆盖必须仍是已注册协议。
- `openai-compatible` 固定默认 `openai-chat-completions`，`anthropic-compatible` 固定默认
  `anthropic-messages`。
- `custom` 必须显式提供 `protocol`、Base URL 和凭据策略。
- runtime factory、默认 API path 和 codec 选择只依赖 `protocol`，不依赖厂商名称或 `provider_id`。
- 厂商模板只负责配置默认值和 Endpoint 目录，不产生一厂商一个 adapter 的重复实现。

这一结构允许同一厂商未来同时支持多个协议，例如 OpenAI Chat Completions 与 Responses，而不改变实例身份；
也允许多个厂商继续共享同一个 OpenAI-compatible adapter。

## 创建、更新与 id 冲突

创建和更新必须成为不同的显式意图：

- 新建 Provider 时，`provider_id` 已存在则默认失败，返回稳定的冲突错误，不合并任何字段；
- 更新现有 Provider 需要显式 `--update`（或独立 application command），且默认只允许相同
  `provider_type`/`protocol`；
- 切换供应商类型或协议属于替换操作，需要显式 `--replace`，在一次校验和一次写盘中完成；
- Desktop 等交互 Host 遇到冲突时应让用户选择新 id、更新或替换，不自行决定；
- 模板刷新仅在用户明确选择更新后发生，并继续遵循“刷新模板端点、保留用户额外端点”的当前事务语义。

这样可消除“同名即合并”的隐式行为，同时不为普通首次配置增加额外步骤。

## 兼容演进

现有值可以无损兼容，不需要根据 `provider_id` 猜测厂商：

- 旧 `provider_type=openai-compatible` 在内存中解析为同名通用配置类型，协议默认
  `openai-chat-completions`；
- 旧 `provider_type=anthropic-compatible` 默认协议 `anthropic-messages`；
- 旧配置保持通用 compatible 身份，即使 id 恰好叫 `openai` 或 `kimi`，也不自动升级为集成厂商类型；
- 新配置和用户显式更新后的配置写入 `protocol`，旧配置读取本身不产生写盘副作用。

只有在兼容读取、公开 DTO、CLI 冲突行为和运行时 dispatch 全部具备测试保护后，才移除从旧类型推导协议的
兼容分支。

## 分层落点

- `ouro/config`：定义 `ProviderType`、`ProviderProtocol`、供应商配置档案和兼容解析；
- `ouro/providers`：adapter registry 改为以 `ProviderProtocol` 为键，不读取供应商配置档案；
- `ouro/application`：分别公开实例 id、配置类型与协议，并编排 create/update/replace；
- CLI/Desktop：展示集成厂商与通用 compatible 入口，把冲突选择留给用户。

不引入动态插件协议注册、任意请求 schema 或供应商专属 adapter 基类；这些都不是完成语义拆分所必需的。

## 实施顺序与验收

1. 增加独立类型和兼容解析，保持当前配置可读，runtime 行为不变。
2. adapter registry 改按 `protocol` dispatch，并增加架构边界测试。
3. 模板写入集成 `provider_type` 与明确 `protocol`，公开 DTO 同时展示二者。
4. CLI/application 引入 create/update/replace 冲突语义，再迁移 Desktop 配置入口。
5. 完成旧配置、原子失败、同 id 冲突、多实例同厂商和一厂商多协议测试后，才考虑弃用旧推导路径。

验收标准是：任何调用方都不再通过厂商名称选择 codec，也不再通过协议名称推断用户选择的供应商配置类型；
相同 `provider_id` 的创建不会静默修改既有配置。
