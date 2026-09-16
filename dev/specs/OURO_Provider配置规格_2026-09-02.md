# Provider 与配置规格

> 文档类型：现行规格
> 代码基线：2026-09-02

## 目标与范围

配置层负责读取、验证和持久化当前权威配置；Provider 层负责把已解析的运行参数适配到外部模型协议。
会话模型选择属于 application，不属于 Provider adapter。

## Provider 与 Endpoint

Provider 表示供应商级连接和默认值：

- `provider_id`：用户拥有的配置实例 id；
- `provider_type`：集成厂商产品档案，或 `openai-compatible`、`anthropic-compatible` 通用入口；
- `protocol`：`faux`、`openai-chat-completions` 或 `anthropic-messages`，只负责线协议 adapter 选择；
- `auth_driver`：认证选择，省略时为 `static`；
- `credential_ref`；
- `base_url`、`api_path`、timeout 和 retry policy；
- enabled、default endpoint 和 endpoint registry。

Endpoint 表示一个具体可调用模型：

- `endpoint_id` 与 `model_id`；
- 可覆盖的 base URL/API path；
- temperature、context window、max output tokens；
- tools、vision、streaming、structured output 能力；
- 类型化 reasoning capability 与声明式原生参数 profile；
- request options 与展示 metadata。

Endpoint 的输出上限不能超过 context window。思考能力、原生编码和运行时意图使用下文的统一契约；
`request_options` 不能同时拥有 reasoning profile 已管理的顶层字段。

## 权威配置结构

```json
{
  "providers": {
    "default": "supplier",
    "items": {
      "supplier": {
        "provider_id": "supplier",
        "provider_type": "openai-compatible",
        "protocol": "openai-chat-completions",
        "credential_ref": "env:API_KEY",
        "base_url": "https://example.test/v1",
        "api_path": "/chat/completions",
        "default_endpoint": "chat",
        "endpoints": {
          "chat": {
            "model_id": "model-name",
            "reasoning": {
              "capability": {
                "activation": "toggle",
                "supported_efforts": [],
                "default": {"mode": "enabled", "effort": null},
                "output": "text",
                "continuation": "text"
              },
              "profile": {
                "name": "thinking-object-toggle",
                "variants": {
                  "enabled": {"thinking": {"type": "enabled"}},
                  "disabled": {"thinking": {"type": "disabled"}}
                }
              }
            }
          }
        }
      }
    }
  }
}
```

`providers.items` 和 `endpoints` 必须是以 id 为 key 的对象。未知字段、列表形态、短字段别名和根级
`provider` 必须明确失败。读取器不能修复、迁移或静默忽略非权威结构。

## 思考契约

思考能力分为四个相互独立的概念，避免把厂商差异泄漏到 core 或 CLI：

1. `ReasoningRequest` 是本次调用的供应商无关意图：`mode` 为 `default`、`enabled` 或 `disabled`，可选
   `effort` 为 `minimal`、`low`、`medium`、`high`、`xhigh` 或 `max`；
2. `ReasoningCapability` 是 Endpoint 的真实能力：是否不支持、始终开启或可切换，精确支持哪些 effort，
   `enabled` 未指定等级时使用的 `default_effort`、Endpoint 默认意图，以及是否有可展示输出和需要续接的
   私有状态；
3. `ReasoningProfileConfig` 按解析后的 variant 保存静态、经过校验的厂商原生请求片段；
4. `ReasoningResolution` 严格把一次意图解析为一个有效 variant，再由协议 adapter 编码。

解析不近似等级、不猜测厂商参数，也不静默降级：不支持的开启/关闭/effort 在 adapter 构造、HTTP 请求之前
转为 `ConfigError`。声明一个 effort 时，该等级自然成为 `default_effort`；声明多个 effort 时必须配置
`default_effort`，也可从带 effort 的 Endpoint 默认意图中无歧义推导。`mode=enabled` 未指定 effort 时解析为这个
确定等级，不把强度委托给厂商默认。没有 effort 控制的 Endpoint 仍解析为独立的 `enabled` variant；对于可关闭或
原生默认值可能关闭思考的模型，该 variant 必须包含能真实开启思考的原生参数，已声明为 `always_on` 的模型可使用
空片段。`mode=default` 使用 Endpoint 的类型化默认意图。

Capability 的输出和续接是两条独立轴：

- `output` 为 `none`、`summary` 或 `text`，只决定是否把可展示思考投影为 canonical reasoning；
- `continuation` 为 `none`、`text` 或 `opaque`，只决定工具回合中是否保存供应商要求回传的状态；
- `opaque` continuation 使用带 `provider_id`、`protocol`、`model_id` 的
  `ProviderContinuationArtifact`，只允许同一路由重放；Anthropic 签名 thinking block 仅在 assistant 工具调用
  与其 tool result 的续接请求中原样回放，不塞入通用文本字段。

Provider profile 只拥有 reasoning 相关顶层字段，禁止覆盖 `model`、`messages`、`tools`、token limit、
`stream` 和 `system` 等 core 请求字段。若 `request_options` 与 profile 字段冲突，配置明确失败，避免由合并顺序
暗中改变行为。

### 自定义模型

`openai-compatible` 与 `anthropic-compatible` 创建空 Provider 后，用户可在任意 Endpoint 上使用
`--reasoning-preset` 声明常见契约，或使用 `--reasoning-config '<JSON>'` 提交完整的 capability/profile。
Preset 包括：

- `none`；
- `openai-effort`、`reasoning-content-effort`；
- `thinking-toggle`、`enable-thinking-toggle`；
- `anthropic-adaptive-effort`。

Preset 是显式配置，不代表 OURO 自动探测了自定义模型。模型仅支持部分 effort 或使用不同参数时，完整 JSON
必须只列出实际支持的 effort，声明明确的 `default_effort`，并为 `enabled`、可用 effort 以及可切换模型的
`disabled` 提供精确原生片段。
运行和压缩模型统一接受 `--thinking-mode` 与 `--thinking-effort`；Endpoint 不支持请求意图时立即失败。

Host 通过 application `ConfigurationFacade` 查询和修改配置。Facade 返回类型化、不可变 DTO，并只公开凭据引用和
可安全查询的状态；每次 mutation 返回变更项和生效范围。CLI、Desktop 等 Host 不直接依赖各 config
manager。Provider retry 的关联字段作为一个原子 mutation 提交，不能留下部分更新。

## 配置事务与锁等待

`JsonConfigFile.mutation` 覆盖完整的同步读、校验和原子替换；同线程嵌套配置命令复用同一事务。
获取锁默认最多等待 10 秒，可显式指定非负、有限的 `timeout`；0 表示只尝试立即获取。
线程内互斥和跨进程文件锁共用一次等待预算，不为第二层锁重新计时。竞争超时返回 `ConfigMutationTimeout`（`ConfigError` 子类），
不进入事务、不修改配置。等待预算约束锁竞争，不承诺阻塞文件系统调用或已开始事务的硬截止时间。

调用方可传入 `threading.Event` 作为 `cancel_event`。获取期间观察到取消时抛出 `InterruptedError`，
释放已获取的资源；没有脱离调用方生命周期、以后还能进入事务的后台锁等待者。
事务已经开始后，取消获取的信号不回滚写入，调用方必须完成事务并释放锁。

这是线程可重入的同步边界，不能跨 `await` 持有，也不能在一个线程获取后由另一个线程释放。
异步用例接入时应让完整同步事务在同一 worker 内收尾，显式协调取消与开始提交的先后，不能仅取消外层等待。
`ProviderConfigManager.mutation` 提供同一事务边界；通用登录的绑定检查和条件提交已按[Provider 认证用例规格](OURO_Provider认证用例规格_2026-09-16.md)接线，不在 config 引入认证服务依赖。
线程/进程竞争、取消、嵌套、异常释放和配置保留的证据见 `tests/test_local_locks.py` 与
`tests/config/test_atomic_config.py`。

## 内置模板

`config/provider_templates.py` 保存经过核验的只读模板目录。模板是配置域数据，不是新的 adapter 类型；多个
厂商可复用同一线协议。每项包含稳定 template id、建议的 provider id、默认 endpoint id、非空且 id 唯一的
Endpoint 集合、连接地址、凭据环境变量、Endpoint capability、官方文档和核验日期。默认 Endpoint 必须存在于
模板 Endpoint 集合中。

当前目录包含以下经官方文档核验的配置：

| 模板 | 默认 Endpoint | 其他预置 Endpoint |
| --- | --- | --- |
| OpenAI | `gpt-5.6` | `gpt-5.6-terra`、`gpt-5.6-luna` |
| Anthropic | `claude-sonnet-5` | `claude-opus-4-8`、`claude-haiku-4-5` |
| Gemini | `gemini-3.5-flash` | `gemini-3.1-pro-preview`、`gemini-3.1-flash-lite` |
| DeepSeek | `deepseek-v4-pro` | `deepseek-v4-flash` |
| Qwen | `qwen3.8-max` | `qwen3.8-flash`、`qwen3.7-plus` |
| KIMI | `kimi-k3` | `kimi-k2.7-code`、`kimi-k2.7-code-highspeed`、`kimi-k2.6` |
| GLM | `glm-5.3` | `glm-5.3-flash`、`glm-5.2`、`glm-5-turbo`、`glm-5v-turbo` |
| Grok | `grok-4.6` | — |
| OpenRouter | `auto` | `free` |
| Mistral | `mistral-medium-3-5` | `mistral-small-2603`、`mistral-large-2512` |

模板值是便捷默认值。CLI 允许显式覆盖 provider id、默认 endpoint id、默认 endpoint 的 model、base URL 和
credential；覆盖不会丢弃同一模板中的其他 Endpoint。

`provider add <id> --type <type>` 是唯一创建用例。application 根据 type 判断：集成厂商类型由配置 manager
在一次校验和一次写盘中创建 Provider 与全部模板 Endpoint；compatible 类型只创建 Provider，Endpoint 由独立
命令管理。失败不得产生半配置。若 id 已存在，明确提示使用 `provider update` 或 `provider replace`。内置类型的
update 按下述模板基线规则合并，replace 按所选类型完整替换。默认 Endpoint 的 id 或 model 覆盖与其他模板
Endpoint 冲突时，在写盘前明确失败。

### 模板来源与显式更新

从模板创建或替换 Provider 时，配置随实例持久化紧凑的 `template` 绑定。绑定只记录模板 id、来源、修订号、
Provider 级模板值，以及每个模板 Endpoint 到配置 Endpoint 的 id 映射和旧内容指纹；不复制完整旧模板，也不保存
凭据值。该基线是区分“模板旧值”和“用户当前值”的必要事实，不能仅凭 `provider_type` 或 Endpoint 名称猜测。

`provider update <id>` 对模板来源 Provider 执行一次显式、原子的三方合并：旧模板基线、当前 Provider 配置和
当前本地模板目录中的最新模板。TUI 在 Provider 子菜单提供 `Update from preset`，调用同一 application 用例。
合并以保留用户意图为优先，规则如下：

| 情况 | 更新结果 |
| --- | --- |
| 模板 Endpoint 相对旧基线有变化，当前 Endpoint 与旧指纹一致 | 用新模板完整 Endpoint 更新 |
| 当前模板 Endpoint 内容被用户修改 | 完整保留当前 Endpoint；不做易产生混合语义的字段级拼接 |
| 最新模板新增 Endpoint，配置中无同 id Endpoint | 新增并纳入模板管理 |
| 最新模板新增 Endpoint，但同 id 已被未知/自定义 Endpoint 占用 | 保留用户 Endpoint、报告冲突，不接管 |
| 用户删除了仍在模板中的 Endpoint | 保持删除状态，后续更新不反复补回 |
| 最新模板移除未修改的 Endpoint | 删除该 Endpoint |
| 最新模板移除已修改或被用户选为默认的 Endpoint | 保留并解除模板管理 |
| 配置中的未知 Endpoint | 始终保留 |

Provider 级 `base_url`、`api_path`、静态模板凭据环境变量和默认 Endpoint 采用同样的旧值比较：当前值仍等于旧模板值
才跟随新模板，否则保留用户值。`enabled`、timeout、retry、provider options 和 Provider id 始终归用户管理。
实例 `auth_driver` 和 managed 引用始终保留；模板认证默认值改变时不自动切换实例认证。只有实例、旧基线和新模板
均使用 static 时，才合并环境变量变化。创建或显式替换可选择允许的认证组合；managed 每次生成新的 credential id。
Endpoint id/model、连接和凭据等本次 CLI 显式参数在合并后应用，并作为用户覆盖留待后续合并识别。

没有 `template` 绑定的 Provider 无法执行模板更新，不根据类型或端点名称接管已有配置。
用户可显式从模板替换该 Provider，建立当前基线后再使用 update。

模板已被删除或禁用、绑定模板来源发生 inline/custom 替换、Provider type/protocol 与模板不一致时，更新明确
失败且 Provider 配置不变。每次成功更新返回新增、更新、删除、保留、冲突 Endpoint 及 Provider 字段的类型化
报告；相同输入重复执行不得产生新的语义变化。

Faux 只作为框架内部安全运行时和测试实现存在，不进入 application/CLI 的 Provider 目录、默认展示、选择或
连接检查；没有用户配置时，公开目录的默认 Provider 为 `null`。

内置模板的思考 profile 按模型分别声明，不能从厂商品牌推导：OpenAI/Gemini 使用模型各自允许的
`reasoning_effort`；DeepSeek、Qwen、KIMI、GLM 使用其原生开关和经过核验的 effort；Anthropic adaptive 或
manual thinking 同时声明可展示摘要和 opaque continuation。始终思考的模型拒绝关闭请求，只支持开关的模型
不伪造 effort 等级。

Mistral 模板暂不声明 reasoning capability。其 reasoning 响应使用嵌套 `ThinkChunk`，并要求续接时原样回放；
当前 OpenAI-compatible continuation 只声明可验证的 `reasoning_content` 文本；Mistral 的嵌套
`ThinkChunk` 尚未进入该协议契约，因此模板不会默认开启一个只能首轮工作、后续退化的能力。

模板目录以本地文件形式提供：内联目录是唯一真源，首次使用时物化为
`config/provider-templates.json`，用户可在该文件上修改、新增模板并随内联目录自动升级。
本地文件化、版本管理、修改感知与自动升级的完整契约见
[Provider 模板](OURO_Provider模板规格_2026-09-02.md)。

类型与协议的设计约束、组合和 CLI 冲突语义见
[Provider 类型与协议规格](OURO_Provider类型与协议规格_2026-09-02.md)。

## 模型路由

持久化和 Host 输入统一使用 `provider_id/endpoint_id`。`ConfiguredModelCatalog`：

- 只列出 enabled Provider 的 Endpoint；
- 精确解析完整 route；
- 为一次请求构建 adapter；
- 允许配置变化在下次请求生效；
- 不接受 model id 或 endpoint id 短名。

## 参数解析顺序

运行参数按以下顺序合并，后者覆盖前者：

1. Provider 产品档案中的协议默认值；
2. Provider 配置；
3. Endpoint 配置；
4. 本次命令覆盖。

Endpoint 显式 base URL/API path 优先于 Provider 默认值。API path 默认值由协议档案提供：
`openai-chat-completions` 使用 `/chat/completions`，`anthropic-messages` 使用 `/v1/messages`。数值 `0`
等合法显式值不能被 truthy 合并误替换。

## 凭据

- 首选 `env:VARIABLE`；支持显式 inline 值，但 CLI 必须提示其存储风险。
- Provider factory 是静态凭据解析边界；`CredentialResolver` 明确拒绝 managed 引用。
- 公开 DTO/JSON 只显示引用或脱敏值。
- `request_options` 和 metadata 中常见敏感 key 递归脱敏。
- 日志、事件和错误不得包含密钥值。

受管理引用采用对象 `{ "type": "managed", "name": "cred_<32 位小写十六进制字符>" }`。
name 是创建用例通过 `new_id("cred")` 生成的标识，不是文件路径；managed 不接受 value，凭据引用对象拒绝未知字段。
static 只能绑定 env/inline/none，其他 driver 必须绑定 managed；协议/认证组合同时受产品档案约束。
读取配置不查找或创建秘密文件，不要求已经登录。普通 update 保留身份引用，replace 创建新引用；remove 只删配置。

RuntimeOptions 携带 `auth_driver`、引用及由产品声明解析的 `service_policy` 标识；后者不能写入用户 Provider 配置或
由本次参数覆盖。已配置 Provider 的认证驱动不能在单次请求中替换，managed 引用不能在单次请求中换绑，也不能与
API Key/API Key 环境变量覆盖并用；无产品绑定的 ad-hoc managed 请求失败。静态 API Key 覆盖语义保持。

公开 Provider/Template DTO 显示 auth_driver。配置视图中的 managed 凭据显示 `state="managed"`、
`configured=true`、`available=null`，只说明已有引用，不能据此推断已经登录、已过期或可调用。实际认证状态由后续
ProviderAuthService 提供，配置视图不读取 store 或触发刷新。

当前内建产品仍只声明 static 组合。受管理配置、模板及运行参数边界通过测试产品声明验收；尚未注册生产驱动、服务规则
或订阅产品。factory 要求组合根注入 managed RequestAuthenticator，构造时不读取秘密；未注入时明确失败，不尝试解析为 API Key。现有协议接线见[请求认证规格](OURO_请求认证规格_2026-09-17.md)，生产装配和 Host 启动验收仍见
[订阅认证规格](OURO_模型订阅认证规格_2026-09-16.md)。配置证据见 `tests/config/test_provider_auth_config.py`。

## Provider adapter

Core `Provider` 只暴露 `stream` 和 `check_connection`。Adapter factory 通过 `ProviderProtocol` 构造器注册表
扩展，配置默认 API path 由对应协议档案提供；factory 不读取厂商类型或实例 id。HTTP JSON/SSE transport、transient
错误分类和“仅首个输出前重试”策略由协议实现共享，HTTP/SSE 对象和异常不得进入 core/application。

OpenAI-compatible 实现负责：

- system message、普通内容、vision、tool history 和声明式 reasoning profile 编码；
- 普通 JSON 与 SSE transport；
- usage、tool call、reasoning 和错误响应解码；思考文本同时接受 DeepSeek 风格 `reasoning_content` 与
  OpenRouter 风格裸 `reasoning` 两种响应字段（流式 delta 与非流式 message 一致，`reasoning_content` 优先，
  两者均支持字符串或 block 数组形态），continuation 回放统一规范化为 `reasoning_content`；
- 外部流缺失 tool index 时按块位置处理；
- 不符合 Endpoint 能力的 image/tool response 明确报错。

Anthropic-compatible 实现负责 Messages API 的顶层 system、content blocks、vision source、tool use/result、
usage/error 与 SSE event 映射，并在工具续接边界原样保存/重放同路由的签名 thinking blocks。协议差异留在
各自 codec/adapter 中。

Endpoint 声明 structured output 能力后才接受结构化 `response_format`。Transport 只对明确可重试的
timeout/HTTP 状态执行有界退避；一旦已经向上游发出任何模型流事件，就不再重放请求。每次重试形成可持久化
的 `run_retry_scheduled` 事件。

## 配置分区

共享 `config/config.json` 还包含：`prompts`、`permissions`、`tool_execution`、`run_budget`、`compaction`、
`skills`。各 manager 只修改自己的分区并保留其他当前分区。

## 失败语义

- 配置结构错误统一转换为 `ConfigError`。
- 不支持的思考模式或 effort、profile 覆盖不完整、原生字段归属冲突统一在 HTTP 前失败。
- 默认 Provider 必须存在、enabled 且有默认 Endpoint；否则不能被设为默认。
- 删除或禁用当前默认目标时安全回退到内置 Faux。
- Provider connection check 返回类型化结果；CLI 以退出码区分连接失败和配置错误。

## 验收证据

- `tests/config/test_credentials_config.py`
- `tests/config/test_provider_templates.py`
- `tests/config/test_provider_template_store.py`
- `tests/config/test_reasoning_config.py`
- `tests/core/test_reasoning.py`
- `tests/providers/test_factory.py`
- `tests/providers/test_openai_compatible.py`
- `tests/providers/test_anthropic_compatible.py`
- `tests/application/test_models.py`
- `tests/application/test_configuration.py`
- `tests/application/test_session_models.py`
- `tests/cli/test_run_cli.py`
- `tests/cli/test_provider_template_commands.py`
