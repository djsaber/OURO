# OURO Agent 模型、凭据与配置规格

- Date: 2026-07-07
- Status: Accepted / Phase 1 implemented
- Related: ../../docs/OURO_AGENT_项目概述_V0707.md

## 1. Motivation

OURO 需要支持多个模型供应商、自定义模型端点和本地配置。模型配置和凭据如果没有清晰边界，会很快污染会话、工作区和插件系统。

第一阶段需要定义模型供应商抽象、凭据引用方式、配置优先级和连通性检查。

## 2. Goals

- 定义模型供应商和模型能力描述。
- 支持 OpenAI 兼容接口作为第一阶段最低可运行路径。
- 为 Anthropic、Google 等供应商预留同一抽象。
- 支持测试用模型供应商。
- 支持凭据引用、环境变量名称或本地密钥条目名称。
- 支持模型连通性检查和清晰错误提示。
- 支持配置优先级：运行时、会话、工作区、环境变量、全局、默认。
- 支持 token 用量和费用估算记录。

## 3. Non-goals

- 不实现企业级密钥管理。
- 不要求第一阶段支持所有模型供应商。
- 不要求第一阶段实现复杂模型路由或自动 fallback。
- 不把明文 API Key 写入项目配置。
- 不把凭据写入会话消息或事件 payload。

## 4. Model Provider Interface

模型供应商应提供统一能力：

- list_models。
- validate_config。
- check_connection。
- stream_response。
- estimate_cost，可选。

流式输出必须转换为 OURO 事件，不直接暴露供应商原始事件给宿主。

## 5. Provider and Endpoint Boundary

Provider 与 Endpoint 是两个不同的配置概念：

- Provider 表示供应商连接和认证边界，负责 provider 类型、凭据、启用状态、超时和重试策略。
- Endpoint 表示一个可调用的模型端点，负责模型 id、请求地址、生成参数和能力声明。

一个 Provider 可以包含多个 Endpoint，并指定其中一个为默认 Endpoint。Endpoint 必须有稳定的
`endpoint_id`；`model_id` 是发送给供应商 API 的模型标识，二者不能混为同一个配置主键。

Endpoint 配置字段：

- endpoint_id。
- model_id。
- base_url，可选；未配置时继承所属 Provider 的 `base_url`。
- api_path，可选；未配置时继承所属 Provider 的 `api_path`，Provider 也未配置时按 `provider_type` 推断默认值（当前 `openai-compatible` 为 `/chat/completions`）。
- temperature，可选；未配置时不向供应商发送该参数。
- context_window。
- max_output_tokens。
- supports_tools。
- supports_vision，用于图像等多模态输入能力声明。
- supports_streaming。
- supports_structured_output。
- supports_reasoning。
- supported_thinking_levels。
- default_thinking_level。
- request_options，用于供应商特有但尚未进入稳定契约的请求参数。
- metadata。

Endpoint 能力字段必须进入 core `ModelMetadata`，供上下文、工具、UI 和 provider 适配层共同使用；
CLI 和具体 provider 不得各自维护一套平行的能力判断。

`request_options` 只能补充供应商特有字段，不能覆盖 `model`、messages、stream、temperature、
max output、tools 或由 thinking level 映射出的稳定字段。稳定 Endpoint 契约和运行时内部控制始终优先，
避免逃生口绕过能力声明。

## 6. Model Metadata

运行时模型记录字段：

- model_id。
- provider_id。
- endpoint_id。
- display_name。
- context_window。
- max_output_tokens。
- supports_tools。
- supports_vision。
- supports_streaming。
- supports_structured_output。
- supports_reasoning。
- supported_thinking_levels。
- default_thinking_level。
- temperature。
- cost。
- metadata。

这些字段用于：

- 上下文截断。
- 工具是否可用。
- UI 展示。
- 用量统计。
- 未来模型管理界面。

## 7. Provider Config

供应商配置字段：

- provider_id。
- provider_type。
- credential_ref。
- default_endpoint。
- endpoints。
- base_url，可选；作为该 Provider 下所有 Endpoint 的默认 `base_url`。
- api_path，可选；作为该 Provider 下所有 Endpoint 的默认 `api_path`，未配置时按 `provider_type` 推断默认值。
- enabled。
- timeout_seconds。
- retry_policy。
- extra。

`provider_id` 即供应商 id；第一阶段不引入第二套供应商字段或本地别名 id。模型地址、模型 id、
温度、上下文长度和能力声明属于 Endpoint，不属于 Provider。

新配置使用以下形状：

```yaml
providers:
  default: glm
  items:
    glm:
      provider_type: openai-compatible
      credential_ref: env:OPENAI_API_KEY
      timeout_seconds: 60
      default_endpoint: glm-4.5
      endpoints:
        glm-4.5:
          model_id: glm-4.5
          # base_url 与 api_path 可省略，继承 Provider 级默认值；
          # 显式声明则覆盖 Provider 默认值。
          temperature: 0.2
          context_window: 131072
          max_output_tokens: 8192
          supports_tools: true
          supports_vision: false
          supports_streaming: true
          supports_reasoning: true
          supported_thinking_levels: [low, medium, high]
          default_thinking_level: medium
```

Provider 配置统一使用嵌套 Endpoint 结构；Provider 级 `base_url` 和 `api_path` 仅作为 Endpoint
默认值，不会再自动合成默认 Endpoint。旧版单 `provider` 对象、Provider 级 `default_model`、
`base_url`、`extra` 以及 `api_key_env`/`api_key` 等扁平字段不再兼容读取。

credential_ref 可以是：

- env:OPENAI_API_KEY。
- keyring:openai/default。
- file_ref:local-only-secret-name。
- none。

第一阶段可以只实现 env 引用和本地配置引用，但配置格式必须允许未来接入系统 keyring。

## 8. Credential Rules

凭据规则：

- 凭据属于用户本地配置。
- 工作区配置可以引用凭据名称，但不保存明文凭据。
- 会话消息、事件 payload、导出内容默认不包含明文凭据。
- 连接失败时可以展示凭据来源，但不能打印完整密钥。
- 插件和 MCP 使用凭据时必须带来源标识。

建议单独文件：

```
config/
  credentials.yaml
```

示例形状：

```yaml
credentials:
  openai_default:
    type: env
    name: OPENAI_API_KEY
  local_test:
    type: inline
    value: ""
```

inline 可以用于本地个人场景，但 UI 必须明确它会保存在本地配置文件中。

## 9. Configuration Layers

配置层：

1. 运行时设置。
2. 本地配置文件：`OURO_HOME/config/config.json`。
3. 会话配置。
4. 工作区配置。
5. 环境变量。
6. 全局配置。
7. 默认值。

合并原则：

- 越靠前优先级越高。
- 明确设置的 null 可以表示清除上层值，但必须谨慎记录。
- 凭据明文不能由工作区配置覆盖。
- 插件和 MCP 可以声明配置项，但最终合并由配置服务完成。
- 第一阶段已接入 provider 配置文件，并允许 CLI 参数覆盖本地配置。
- 当前实现仅支持 `providers.default` + `providers.items` 多 provider 配置形状；旧版单 `provider` 对象、Provider 级扁平字段等不再读取兼容。
- Provider 级 CLI 提供 `ouro provider list/show/set/use/remove/check`。
- Endpoint 级 CLI 提供 `ouro provider endpoint list/show/set/use/remove`；运行和连通性检查可用 `--provider <provider_id>` 选择已配置 Provider，并用 `--endpoint <endpoint_id>` 选择端点。
- 图像输入能力使用 `--vision/--no-vision`，并提供语义等价的 `--multimodal/--no-multimodal` 别名。
- `ouro provider set --endpoint <id> --model/--base-url` 在创建 Provider 的同时创建/更新默认 Endpoint，并将 Provider 的 `default_endpoint` 指向该 Endpoint；省略 `--endpoint` 可创建无 Endpoint 的 Provider。
- `ouro config check` 保留为默认 Provider/Endpoint 连通性检查兼容入口。
- Provider 适配层必须把 HTTP 200 但包含 error body、无 choices、空 assistant message、或未完成的 stream 视为失败，不允许静默完成为空输出。

## 10. Connection Check

模型连通性检查应覆盖：

- base_url 是否可访问。
- 凭据是否存在。
- 凭据是否可用。
- 默认或显式选择的 Endpoint 是否存在、启用且配置完整。
- Endpoint 对应模型是否存在或可调用。
- 工具调用能力是否与模型元数据一致。

检查结果：

- ok。
- missing_credential。
- auth_failed。
- model_not_found。
- network_error。
- provider_error。
- unknown_error。

检查失败应给出用户可理解的修复提示。

## 11. Usage and Cost

每次模型调用记录：

- provider_id。
- model_id。
- input_tokens。
- output_tokens。
- cache_read_tokens。
- cache_write_tokens。
- estimated_cost。
- latency_ms。
- first_token_ms。

这些数据进入事件流和运行记录。费用只是估算，不作为账单依据。

## 12. Acceptance

完成本规格后，应满足：

- 用户可以在一个 Provider 下配置多个 OpenAI 兼容模型 Endpoint，并选择默认 Endpoint。
- Endpoint 可以独立配置 temperature、context window、output limit、多模态、工具、流式、推理与思考等级能力。
- 运行时构建的 `ModelMetadata` 与 provider 请求行为使用 Endpoint 配置，而不是硬编码默认值。
- 用户可以通过 `OURO_HOME/config/config.json` 配置默认 provider，并通过 CLI 参数覆盖。
- 用户可以通过 CLI 创建、查看、切换、删除 provider 配置，且输出不会展示 inline credential 明文。
- 用户可以通过环境变量引用 API Key。
- 缺少凭据时给出清晰错误。
- 可以针对指定 Provider/Endpoint 执行模型连通性检查。
- 模型流式输出被转换为 OURO 事件。
- 用量统计进入事件和运行记录。
- 会话可以覆盖当前模型。
- 测试供应商可以在无网络环境下跑通核心运行测试。

## 13. Resolved Decisions

- 第一阶段以内置 FauxProvider 和 OpenAI-compatible Provider 启动；Anthropic 原生 provider 后置。
- 第一阶段允许环境变量引用和 inline 本地凭据占位；显示时必须脱敏。
- 模型能力元数据第一阶段由 Endpoint 本地配置和代码默认值提供；供应商 API 发现与缓存后置。
- Provider 只承担连接、认证和生命周期配置；模型能力和请求生成参数统一归 Endpoint。
- Endpoint 嵌套配置是唯一权威读写格式；旧版扁平 provider/单模型字段不兼容读取，也不作为 CLI
  快捷方式重新引入。
- 默认 Provider 必须处于 enabled 状态且拥有有效默认 Endpoint。创建尚未配置 Endpoint 的 Provider、
  禁用默认 Provider 或删除其最后一个 Endpoint 时，配置管理器必须保留或回退到可运行的 Faux 默认值。
