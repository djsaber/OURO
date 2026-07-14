# Provider 与配置规格

状态：当前

## 目标与范围

配置层负责读取、验证和持久化当前权威配置；Provider 层负责把已解析的运行参数适配到外部模型协议。
会话模型选择属于 application，不属于 Provider adapter。

## Provider 与 Endpoint

Provider 表示供应商级连接和默认值：

- `provider_id`；
- `provider_type`：`faux`、`openai-compatible` 或 `anthropic-compatible`；类型表示线协议，
  不表示具体厂商品牌；
- `credential_ref`；
- `base_url`、`api_path`、timeout 和 retry policy；
- enabled、default endpoint 和 endpoint registry。

Endpoint 表示一个具体可调用模型：

- `endpoint_id` 与 `model_id`；
- 可覆盖的 base URL/API path；
- temperature、context window、max output tokens；
- tools、vision、streaming、structured output、reasoning 能力；
- supported/default thinking level；
- request options 与展示 metadata。

Endpoint 的输出上限不能超过 context window。Thinking level 只能在 reasoning 开启时配置。

## 权威配置结构

```json
{
  "providers": {
    "default": "supplier",
    "items": {
      "supplier": {
        "provider_id": "supplier",
        "provider_type": "openai-compatible",
        "credential_ref": "env:API_KEY",
        "base_url": "https://example.test/v1",
        "api_path": "/chat/completions",
        "default_endpoint": "chat",
        "endpoints": {
          "chat": {
            "model_id": "model-name"
          }
        }
      }
    }
  }
}
```

`providers.items` 和 `endpoints` 必须是以 id 为 key 的对象。未知字段、列表形态、短字段别名和根级
`provider` 必须明确失败。读取器不能修复、迁移或静默忽略非权威结构。

Host 通过 application `ConfigurationFacade` 查询和修改配置。Facade 返回类型化、不可变 DTO，并只公开凭据的
configured/missing 状态；每次 mutation 返回变更项和生效范围。CLI、Desktop 等 Host 不直接依赖各 config
manager。Provider retry 的关联字段作为一个原子 mutation 提交，不能留下部分更新。

## 内置模板

`config/provider_templates.py` 保存经过核验的只读模板目录。模板是配置域数据，不是新的 adapter 类型；多个
厂商可复用同一线协议。每项包含稳定 template id、建议的 provider/endpoint id、模型 ID、连接地址、凭据
环境变量、Endpoint capability、官方文档和核验日期。

当前目录包含 OpenAI、Anthropic、Gemini、DeepSeek、Qwen、GLM、OpenRouter 和 Mistral。模板值是便捷默认值，
CLI 允许显式覆盖 provider id、endpoint id、model、base URL 和 credential。

`provider configure <template>` 是显式复合用例：配置 manager 在一次校验和一次写盘中创建/更新 Provider 与
默认 Endpoint。失败不得产生半配置。重复应用时保留同 Provider 的其他 Endpoint、凭据、timeout、retry 与
extra；目标 Endpoint 使用模板和本次覆盖刷新。已存在 Provider 的线协议不匹配时明确失败。

Anthropic 模板当前显式关闭 adaptive thinking。原因是 Anthropic 工具续接要求原样保存 opaque thinking
signature，而 canonical provider continuation 尚未提供该字段；在该底层契约完善前，不用供应商特例把签名
塞入通用 reasoning 文本。

## 模型路由

持久化和 Host 输入统一使用 `provider_id/endpoint_id`。`ConfiguredModelCatalog`：

- 只列出 enabled Provider 的 Endpoint；
- 精确解析完整 route；
- 为一次请求构建 adapter；
- 允许配置变化在下次请求生效；
- 不接受 model id 或 endpoint id 短名。

## 参数解析顺序

运行参数按以下顺序合并，后者覆盖前者：

1. Provider 类型默认值；
2. Provider 配置；
3. Endpoint 配置；
4. 本次命令覆盖。

Endpoint 显式 base URL/API path 优先于 Provider 默认值。协议默认值由 adapter spec 注册表提供：
OpenAI-compatible 使用 `/chat/completions`，Anthropic-compatible 使用 `/v1/messages`。数值 `0` 等合法
显式值不能被 truthy 合并误替换。

## 凭据

- 首选 `env:VARIABLE`；支持显式 inline 值，但 CLI 必须提示其存储风险。
- Provider factory 是凭据解析边界。
- 公开 DTO/JSON 只显示引用或脱敏值。
- `request_options` 和 metadata 中常见敏感 key 递归脱敏。
- 日志、事件和错误不得包含密钥值。

## Provider adapter

Core `Provider` 只暴露 `stream` 和 `check_connection`。Adapter factory 通过 `ProviderKind` 构造器注册表扩展，
配置默认 API path 由对应 adapter spec 提供；调用方不维护 kind 条件分支。HTTP JSON/SSE transport、transient
错误分类和“仅首个输出前重试”策略由协议实现共享，HTTP/SSE 对象和异常不得进入 core/application。

OpenAI-compatible 实现负责：

- system message、普通内容、vision、tool history 和 reasoning 字段编码；
- 普通 JSON 与 SSE transport；
- usage、tool call、reasoning 和错误响应解码；
- 外部流缺失 tool index 时按块位置处理；
- 不符合 Endpoint 能力的 image/tool response 明确报错。

Anthropic-compatible 实现负责 Messages API 的顶层 system、content blocks、vision source、tool use/result、
usage/error 与 SSE event 映射。协议差异留在各自 codec/adapter 中。

Endpoint 声明 structured output 能力后才接受结构化 `response_format`。Transport 只对明确可重试的
timeout/HTTP 状态执行有界退避；一旦已经向上游发出任何模型流事件，就不再重放请求。每次重试形成可持久化
的 `run_retry_scheduled` 事件。

## 配置分区

共享 `config/config.json` 还包含：`prompts`、`permissions`、`tool_execution`、`run_budget`、`compaction`、
`skills`。各 manager 只修改自己的分区并保留其他当前分区。

## 失败语义

- 配置结构错误统一转换为 `ConfigError`。
- 默认 Provider 必须存在、enabled 且有默认 Endpoint；否则不能被设为默认。
- 删除或禁用当前默认目标时安全回退到内置 Faux。
- Provider connection check 返回类型化结果；CLI 以退出码区分连接失败和配置错误。

## 验收证据

- `tests/config/test_credentials_config.py`
- `tests/providers/test_factory.py`
- `tests/providers/test_openai_compatible.py`
- `tests/providers/test_anthropic_compatible.py`
- `tests/application/test_models.py`
- `tests/application/test_configuration.py`
- `tests/application/test_session_models.py`
- `tests/cli/test_run_cli.py`
- `tests/cli/test_provider_template_commands.py`
