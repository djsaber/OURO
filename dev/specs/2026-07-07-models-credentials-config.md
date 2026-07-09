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

## 5. Model Metadata

模型记录字段：

- model_id。
- provider_id。
- display_name。
- context_window。
- max_output_tokens。
- supports_tools。
- supports_vision。
- supports_streaming。
- supports_structured_output。
- supports_reasoning。
- default_reasoning_level。
- cost。
- metadata。

这些字段用于：

- 上下文截断。
- 工具是否可用。
- UI 展示。
- 用量统计。
- 未来模型管理界面。

## 6. Provider Config

供应商配置字段：

- provider_id。
- provider_type。
- base_url。
- credential_ref。
- default_model。
- enabled。
- timeout_seconds。
- retry_policy。
- extra。

`provider_id` 即供应商 id；第一阶段不引入第二套供应商字段或本地别名 id。

credential_ref 可以是：

- env:OPENAI_API_KEY。
- keyring:openai/default。
- file_ref:local-only-secret-name。
- none。

第一阶段可以只实现 env 引用和本地配置引用，但配置格式必须允许未来接入系统 keyring。

## 7. Credential Rules

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

## 8. Configuration Layers

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
- 当前实现支持 `providers.default` + `providers.items` 多 provider 配置形状，并保留旧版单 `provider` 对象读取兼容。
- 当前实现提供 `ouro provider list/show/set/use/remove/check` 管理命令；`ouro config check` 保留为默认 provider 连通性检查兼容入口。
- Provider 适配层必须把 HTTP 200 但包含 error body、无 choices、空 assistant message、或未完成的 stream 视为失败，不允许静默完成为空输出。

## 9. Connection Check

模型连通性检查应覆盖：

- base_url 是否可访问。
- 凭据是否存在。
- 凭据是否可用。
- 默认模型是否存在或可调用。
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

## 10. Usage and Cost

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

## 11. Acceptance

完成本规格后，应满足：

- 用户可以配置 OpenAI 兼容模型端点。
- 用户可以通过 `OURO_HOME/config/config.json` 配置默认 provider，并通过 CLI 参数覆盖。
- 用户可以通过 CLI 创建、查看、切换、删除 provider 配置，且输出不会展示 inline credential 明文。
- 用户可以通过环境变量引用 API Key。
- 缺少凭据时给出清晰错误。
- 可以执行模型连通性检查。
- 模型流式输出被转换为 OURO 事件。
- 用量统计进入事件和运行记录。
- 会话可以覆盖当前模型。
- 测试供应商可以在无网络环境下跑通核心运行测试。

## 12. Resolved Decisions

- 第一阶段以内置 FauxProvider 和 OpenAI-compatible Provider 启动；Anthropic 原生 provider 后置。
- 第一阶段允许环境变量引用和 inline 本地凭据占位；显示时必须脱敏。
- 模型能力元数据第一阶段由本地配置和代码默认值提供；供应商 API 发现与缓存后置。
