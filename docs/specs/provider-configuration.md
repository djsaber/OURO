# Provider 与配置规格

> 文档类型：现行规格

## 目标与范围

配置层负责读取、验证和持久化当前权威配置；Provider 层负责把已解析的运行参数适配到外部模型协议。
会话模型选择属于 application，不属于 Provider adapter。

## Provider 与 Endpoint

Provider 表示供应商级连接和默认值：

- `provider_id`：框架生成的内部实例身份，不接受用户配置；
- `display_name`：必填的用户展示名，不承担身份语义；
- `provider_type`：集成厂商产品档案，或 `custom` 通用入口；
- `protocol`：内置协议 `faux`、`openai-chat-completions`、`openai-responses`、`anthropic-messages`，或插件贡献身份 `<plugin_id>/providers/<id>`，只负责线协议 adapter 选择；
- `auth_driver`：认证选择，省略时为 `static`；
- `credential_ref`；
- `base_url`、`api_path`、timeout 和 retry policy；
- enabled、default endpoint 和 endpoint registry。

Endpoint 表示一个具体可调用模型：

- `endpoint_id`：框架生成的内部身份，不接受用户配置；
- `display_name`：可选展示名；`model_id`：发送给供应商的模型标识；
- 可覆盖的 base URL/API path；
- temperature、context window、max output tokens；
- tools、image/audio/video 输入、streaming、structured output 能力；
- 类型化 reasoning capability 与声明式原生参数 profile；
- request options 与展示 metadata。

`max_output_tokens` 为可空正整数，默认 `None`。完整配置中省略字段与显式 `null` 同义，表示 OURO 不配置输出上限；正整数表示本次请求的有限上限，必须小于 `context_window`，为输入留下空间。它不是服务端能力声明，OURO 不发现或推算服务端输出限制。

本地 `ContextBudget` 从最终请求参数计算：空上限预留 0，有限上限等额预留，输入预算为上下文窗口减预留。Chat Completions 和 Responses 在空值时省略相应参数，不发送 JSON `null`；Messages 协议要求显式有限值，空值在编码边界报错。受信 `ProviderRequestPolicy.supports_output_limit=false` 时可省略上限的协议不发送该参数，有限配置仍用于本地预留；使用入口披露这一约束，不通过本地截流模拟请求硬上限。

配置编辑只应用提交的字段：不提交上限表示保留，提交 `null` 表示清空。CLI 用 `--max-output-tokens none` 清空，TUI 的 `Max output tokens` 是可选项，空值标为 `Optional`，不显示输入占位提示。运行时覆写在构造最终模型时按字段是否提交合并；独立模型调用与摘要调用各自的上限默认 `None`，不继承端点数值。请求执行直接使用完整模型。

思考能力、原生编码和运行时意图使用下文的统一契约；
`request_options` 不能同时拥有 reasoning profile 已管理的顶层字段。

## 插件协议

`custom` Provider 可以选择一个具名插件协议，使用静态认证。配置读取只验证协议身份与 Endpoint 的领域约束，
不加载或启用插件。模型目录继续拥有 Provider/Endpoint 路由、模型参数与缓存；协议插件不创建平行模型目录。

`ProviderContribution` 提供异步 stream 和 connection check。请求 DTO 包含解析后的连接、有限请求上下文、
本次有效模型参数、模型可见消息与工具 schema。模型消息保留多模态顺序、语义标签、Skill 激活内容、工具帧、
工具来源和 opaque continuation；框架不把这些内容压缩成纯文本输入。SDK 不暴露 Session 存储或 Runtime 对象。Provider 保留请求归属与父级约束，
不继承 Driver 的循环控制端口；协议处理不能通过独立模型生成绕过活跃 Run 的预算。

内置协议的编码规则在配置阶段校验；插件负责自己的 URL、选项、输入编码及 reasoning profile 校验。
协议能力不会自动启用 Endpoint 的输入能力。插件应明确拒绝无法编码的请求，不能静默丢弃内容。

每次 stream/check 在调用前同步插件意图并局部绑定 Provider 及其依赖代次。闲置路由不持有插件租约，
升级后的请求使用当前代次，在途请求完成后释放原代次；禁用撤销调用并关闭受管资源。
移除插件不删除模型路由，也不以其他 Provider 替代不可用协议。

流式输出逐项等待接收，Host 缓冲有界。原生与自定义 Driver、独立生成和连接检查使用相同协议边界。
框架拥有完成事件校验、取消、用量归属及账本；插件报告实际响应事实，不能另行提交框架用量。
请求异常和插件诊断使用本代次的凭证脱敏器。只有实际请求绑定的插件会收到该连接的 API key 和已解析 header。

## 输入模态契约

Endpoint 用三个相互独立、默认为 `false` 的字段声明服务实际接受的对话输入：

| 字段 | TUI 选项 | CLI 开关 |
| --- | --- | --- |
| `supports_image_input` | Image input | `--image-input` / `--no-image-input` |
| `supports_audio_input` | Audio input | `--audio-input` / `--no-audio-input` |
| `supports_video_input` | Video input | `--video-input` / `--no-video-input` |

输入能力统一按 `supports_<modality>_input` 命名，模态使用内容块的 `image`、`audio`、`video`；
界面名称、说明、能力摘要与 CLI 开关均明确表达输入方向。

这些字段从端点配置、模板、Application DTO 和 runtime override 传递到 `ModelMetadata`；
`ModelMetadata.input_modalities` 产生类型化的只读模态集合，供输入准备、请求投影和压缩共用；文本为基本输入，不占该集合项。
省略更新字段表示保留，显式 `false` 表示关闭；模型名称、厂商、后缀或其他输入能力不能代替声明。
它们不声明输出模态，不触发语音生成、自动转写、视频抽帧或媒体转码。

可发送内容同时受端点声明、协议实现、媒体格式、消息角色与请求预算约束：

| OURO 协议实现 | 图片 | 音频 | 视频 |
| --- | --- | --- | --- |
| OpenAI Chat Completions | `image_url` | `input_audio`，WAV/MP3 Base64 | `video_url` 扩展，MP4 |
| OpenAI Responses | `input_image` | 不支持 | 不支持 |
| Anthropic Messages | `image` | 不支持 | 不支持 |

`supports_video_input=true` 在 Chat Completions 路由上明确声明目标服务接受 `video_url` 扩展；
它不是 OpenAI 标准 Chat Completions 的通用保证。音频内容使用 `audio/wav` 或 `audio/mpeg` 的
Base64 data URI，编码为 `format=wav/mp3`；视频使用 `video/mp4` 的 data URI 或 HTTP(S) URL。
本地路径由附件服务观察为媒体内容，不能直接作为模型可读取的 URL 发送。视频输入声明不保证服务处理音轨。

内置 `ProviderProtocolSpec.encodable_input_modalities` 描述 OURO adapter 能编码的媒体输入集合，
Endpoint 的 `supports_<modality>_input` 描述具体服务与模型实际接受的输入。端点声明的集合必须是协议可编码集合的子集；
协议具备编码能力不会自动启用端点能力。图片、音频和视频使用相同校验；配置、模板和直接 adapter 构造均拒绝无法编码的声明。
Adapter 只按协议分派，不按厂商或模型名称猜测格式。原始 codec 调用遇到不支持的内容、格式或角色明确失败；
Application 对历史附件按本次能力投影为原生媒体或带原因的引用，不修改保存的原始内容。

协议依据：[OpenAI Chat Completions 音频输入](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、
[Gemini OpenAI 兼容接口音频输入](https://ai.google.dev/gemini-api/docs/openai#audio_understanding)、
[Model Studio 视频输入扩展](https://www.alibabacloud.com/help/en/model-studio/vision)。
附件识别、存储、预算与快照契约见[附件规格](attachments.md)。

## 权威配置结构

```json
{
  "providers": {
    "default": "provider_<generated>",
    "items": {
      "provider_<generated>": {
        "display_name": "Supplier",
        "provider_type": "custom",
        "protocol": "openai-chat-completions",
        "credential_ref": "env:API_KEY",
        "base_url": "https://example.test/v1",
        "api_path": "/chat/completions",
        "default_endpoint": "endpoint_<generated>",
        "endpoints": {
          "endpoint_<generated>": {
            "display_name": "Chat",
            "model_id": "model-name",
            "reasoning": {
              "capability": {
                "activation": "toggle",
                "supported_efforts": [],
                "default": {"mode": "enabled", "effort": null}
              },
              "profile": {
                "name": "thinking-object-toggle",
                "effort": {
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

## 请求头配置

Provider 的 `headers` 是独立于请求体参数的 HTTP 连接配置，适用于该 Provider 的所有 Endpoint。
它是以头名为 key 的对象，每个值必须显式声明一种来源：

```json
{
  "headers": {
    "X-Client-Name": {"source": "literal", "value": "ouro"},
    "X-Tenant-ID": {"source": "env", "name": "MODEL_TENANT_ID"},
    "X-Conversation-ID": {"source": "context", "key": "session.id"}
  }
}
```

`literal` 只接受 `value`，`env` 只接受 `name`，`context` 只接受 `key`。来源是有限的判别联合，
未知字段与来源明确失败；不支持脚本、表达式、对象路径遍历、拼接或递归插值。头名按 HTTP token
语法校验并规范化为小写，大小写不敏感的重复项明确失败。头值限定为非空、无首尾空白的可打印 ASCII，
字面量在配置阶段校验，环境变量与上下文值在请求发送前校验。

上下文字段通过 `core/request_context.py` 的只读目录显式注册；配置只能引用目录中的 key。
`ProviderRequestContext` 是每次调用独立的不可变字符串映射，不携带 HTTP 头或供应商规则。
调用方负责字段的产生与生命周期，解析器只执行精确查找，不生成缺失值。扩展一个上下文字段需要注册
字段并接入对应提供方与传递点，不需要修改来源解析算法或协议适配器。

可引用的上下文字段及提供方契约：

| 字段 | 提供方与取值 | 可用范围 |
| --- | --- | --- |
| `session.id` | 所属会话的持久 ID | 对话、压缩；连接检查入口使用检查专用临时 ID |
| `run.id` | 当前 Run 的 ID | 对话与 Run 内压缩；手动压缩、连接检查可能没有 Run |
| `provider.id` | 最终所选 ModelMetadata 的供应商配置实例 ID | 所有 HTTP 模型调用 |
| `endpoint.id` | 最终所选 ModelMetadata 的 Endpoint ID | 已配置路由；临时模型没有 Endpoint 时缺失 |
| `model.id` | 最终所选 ModelMetadata 的模型 ID | 所有 HTTP 模型调用 |
| `request.purpose` | 调用用途：`chat`、`compaction`、`connection_check`、`plugin`；直接推理默认为 `inference` | 所有 HTTP 模型调用 |
| `request.id` | Host 分配的独立请求 ID | 插件独立模型请求 |
| `plugin.id` | 发起请求的插件 ID | 插件独立模型请求 |

模型路由字段由协议适配入口根据实际模型提供，覆盖调用方可能携带的旧路由信息。请求头解析器不理解
字段的业务意义。字段可用范围不一致是明确契约：例如配置必须引用 `run.id` 时，无 Run 的连接检查
会报告缺失，不伪造 Run、不省略请求头。字段不隐式包含用户身份、文件路径、Token 或消息内容。

`session.id` 是会话提供方注册的字段：多轮、工具续接、压缩、重试与恢复使用所属会话的稳定 ID，
独立子会话使用自身 ID。连接检查入口创建本次检查专用的临时 ID，在该次检查及重试内复用。
其他直接调用者须显式提供配置引用的上下文；缺失上下文或环境变量在 HTTP 前返回安全配置错误。
仅配置引用的字段被发送，运行值不写回配置、不进入共享 adapter 状态或模型消息。

配置 mutation 对 `headers` 使用整组原子替换：省略表示保留，空对象表示清空。Provider 创建和 replace
省略时采用所选模板的请求头默认值；不使用模板时为空配置。模板同步保留实例请求头，除非调用方显式替换；请求头声明参与运行参数指纹，
使后续路由选择读取有效配置。环境变量在每次模型调用开始时解析，重试复用本次解析结果；认证材料仍
按每次尝试独立获取。`faux` 不接受 HTTP 请求头。

公开查询只展示头名、来源与引用；所有 literal 值均显示为 `***`，不以头名猜测敏感性。
CLI `provider add/update/replace --headers '<JSON>'` 接受完整声明。TUI 的请求头编辑框空白表示保留，
输入 `{}` 表示清空，输入完整对象表示替换，不将脱敏查询结果作为可回写数据。
HTTP 合并、认证所有权和传输约束见[模型请求认证规格](request-authentication.md)。

## 思考契约

思考配置只声明请求控制项和原生参数映射，不保证远端接受参数或返回思考内容。响应解析、原生状态续接与客户端展示
分别由协议适配器和 Host 负责，不由请求能力配置决定。运行时不按供应商名称或模型 ID 猜测、改写或降级思考设置。

1. `ReasoningRequest` 表达本次调用的意图：`mode` 为 default、enabled 或 disabled；`effort` 为可选强度；
   `summary` 为独立的服务端摘要请求偏好。
2. `ReasoningCapability` 声明 Endpoint 的请求控制：`activation`、`supported_efforts`、`supported_summaries`，
   以及集中保存 mode、effort、summary 偏好的 `default`。unsupported 表示没有启停或等级控制，不表示远端没有思考输出。
3. `ReasoningProfileConfig` 的 `effort` 和 `summary` 两张表将选项映射到静态原生请求片段。
4. `ReasoningResolution` 解析有效意图，`ReasoningPayloadEncoder` 按 `effort_variant` 和 summary 组合请求片段。

### 请求解析与编码

`profile.effort` 覆盖 enabled、可切换时的 disabled 和所有已声明强度，可包含 default；`profile.summary` 覆盖
已声明摘要模式。配置必须完整、自洽，不能包含未声明的选项。只有摘要控制时，effort 表为空。
编码器递归合并对象；相同字段的相同值保留，不同值报错，数组和标量不拼接。例如
`{"reasoning":{"effort":"high"}}` 与 `{"reasoning":{"summary":"auto"}}` 合并为同一 reasoning 对象。

`default.mode` 决定默认启停，`default.effort` 指定开启且未选择等级时使用的强度。单个强度可自动补全默认值；
多个强度必须显式指定默认值。`default={"mode":"disabled","effort":"high"}` 表示默认关闭，显式开启时使用 high。
请求关闭时保留用户的强度偏好，有效请求不携带 effort；未指定 mode 且显式选择 effort 时开启思考。
默认偏好中的 effort 不会独立开启思考，`default.mode=default` 保留服务端默认行为。
无等级的 enabled 使用独立片段；always_on 可使用空片段。default 使用已配置的默认片段或省略控制参数，不能等同于显式开启。

`summary=null` 或未设置时继承 `default.summary`；provider_default 显式省略摘要参数。原生 auto 是明确请求值，
与省略参数不同。摘要请求不会开启已关闭的思考。默认摘要必须属于声明集合，或为 provider_default/null。
摘要集合为空只表示没有摘要请求控制，不限制响应解析。

声明范围外的意图在网络请求前失败。远端拒绝已声明参数时返回错误，不自动重映射或重试另一套参数。
远端未返回可选思考字段时正常处理；已支持的思考字段存在时接收，不以请求等级或摘要声明推测其是否应当出现。

Profile 禁止覆盖 model、messages、tools、token limit、stream、system 等 core 请求字段。`request_options`
与 profile 拥有的顶层字段冲突时配置失败。字段归属由两张映射表共同推导，不按选项名称猜测路径。

### 通用预设与端点声明

内置思考预设定义常见协议控制方式与请求编码，不嵌入具体厂商、模型的限制或响应策略。只有编码结构或控制语义不同
才增加预设，不为能力子集建立内置模板。端点可通过完整 JSON 声明子集或扩展映射；供应商产品模板可以提供端点默认配置，
这些数据不参与协议适配器分派。

| Effort 预设 | 请求控制 |
| --- | --- |
| none | 不发送启停或等级参数 |
| always-on | 仅开启，无等级，不发送启停参数 |
| openai-chat-completions-effort | 顶层 reasoning_effort；minimal、low、medium、high、xhigh、max，关闭为 none |
| openai-responses-effort | reasoning.effort；同上六级与关闭 |
| thinking-toggle | thinking.type=enabled/disabled |
| enable-thinking-toggle | enable_thinking=true/false |
| anthropic-messages-adaptive-effort | thinking.type=adaptive/disabled；output_config.effort 为 low、medium、high、xhigh、max |

两个 OpenAI 等级预设的显式 enabled 使用 medium；Chat Completions 预设默认开启，Responses 预设默认沿用服务端。
这些是预设的请求偏好，不是对远端默认行为的保证。always-on 不提供关闭选项，也不要求响应必须包含思考。

| Summary 预设 | 选项 | 请求映射 |
| --- | --- | --- |
| none | 空集合 | 不发送摘要参数 |
| openai-responses | auto、concise、detailed | reasoning.summary |
| anthropic-messages | summarized、omitted | thinking.display |

OpenAI Chat Completions 没有官方摘要预设；兼容接口的扩展可以通过显式片段声明。thinking.type、enable_thinking、
reasoning_content 等兼容生态字段不能视为 OpenAI 官方规范。Messages 的 display 请求需要有效 thinking 模式；
适配器校验原生结构，不根据厂商推断该模式。

CLI `--reasoning-preset` 和 TUI Effort 使用同一目录；Summary 使用独立摘要目录，与协议不兼容的标准预设不可选。
`--reasoning-config` / Settings (JSON) 编辑同一份 capability/profile，可指定子集、默认偏好和原生片段。
`with_effort_preset` 保留摘要控制与偏好；`with_summary_preset` 保留启停、等级与偏好。
选择摘要预设时保留仍兼容的默认摘要，否则采用预设首项；选择 none 清除摘要偏好。

会话、任务、运行和压缩使用统一意图。Host 按声明列出控制项，修改强度或摘要时保留另一项；模型切换使用新端点声明重新解析意图。
Application DTO 提供 capability、requested 和 effective，声明不代表自动探测结果。

### 响应、续接与展示

协议适配器接收实际返回的可读思考并生成 canonical reasoning，保留协议识别的原生续接状态。这两条输出独立存在：
没有可读正文时仍可保存签名或加密数据；没有续接状态时仍可展示正文。不从不透明数据生成文本，也不从展示文本重建原生状态。

Chat Completions 支持 reasoning_content 与 reasoning 扩展；Responses 和 Messages 按各自原生 item/block 处理。
`ProviderContinuationArtifact` 携带 provider_id、protocol、model_id，仅在来源匹配时回传，并遵守协议的认证绑定与结构校验。
普通回答和工具调用均保留实际返回的原生状态。更改请求等级或摘要选项不会丢弃已有状态。

TUI 只对已有正文提供思考区域与展开、折叠偏好。服务端摘要选项控制请求，客户端展示偏好控制投影；两者均不改变已保存的
原生续接数据。缺失思考正文不是协议错误，也不能作为远端未执行思考的证据。

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
`ProviderConfigManager.mutation` 提供同一事务边界；通用登录的绑定检查和条件提交已按[Provider 认证用例规格](provider-authentication.md)接线，不在 config 引入认证服务依赖。
线程/进程竞争、取消、嵌套、异常释放和配置保留的证据见 `tests/test_local_locks.py` 与
`tests/config/test_atomic_config.py`。

## 内置模板

`config/provider_templates/` 保存经过核验的只读模板目录。模板是配置域数据；多个
厂商可复用同一线协议。每项包含稳定 template id、必填展示名、默认模板 Endpoint 身份、非空且模板身份唯一的
Endpoint 集合、连接地址、凭据环境变量、Endpoint capability、官方文档和核验日期。默认 Endpoint 必须存在于
模板 Endpoint 集合中；模板 Endpoint 身份只服务模板版本合并，实例化时不会复用为运行时 Endpoint ID。

模型目录、默认端点、连接参数与能力声明见[预置供应商配置](provider-presets.md)。

模板值是便捷默认值。CLI 允许显式覆盖展示名、默认 endpoint 的 model、base URL 和 credential；内部身份
始终由框架生成，覆盖不会丢弃同一模板中的其他 Endpoint。

`provider add <display_name> --type <type>` 是唯一创建用例。application 根据 type 判断：集成厂商类型由配置 manager
在一次校验和一次写盘中创建 Provider 与全部模板 Endpoint；`custom` 类型只创建 Provider，Endpoint 由独立
命令管理。失败不得产生半配置。相同展示名允许存在，身份仍保持唯一。内置类型的 update 按下述模板基线规则
合并，replace 按所选类型完整替换；默认 Endpoint 的 model 覆盖在写盘前完成校验。

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

Provider 级 `base_url`、`api_path`、已绑定的静态模板凭据环境变量和默认 Endpoint 采用同样的旧值比较：当前值仍等于旧模板值
才跟随新模板，否则保留用户值。`enabled`、timeout、retry、provider options 和展示名归用户管理；Provider id
始终由框架管理。
未绑定凭据的实例不会因模板新增或修改环境变量建议而自动绑定。
实例 `auth_driver` 和 managed 引用始终保留；模板认证默认值改变时不自动切换实例认证。只有实例、旧基线和新模板
均使用 static 时，才合并环境变量变化。创建或显式替换可选择允许的认证组合；managed 每次生成新的 credential id。
Endpoint model、连接和凭据等本次 CLI 显式参数在合并后应用，并作为用户覆盖留待后续合并识别。

没有 `template` 绑定的 Provider 无法执行模板更新，不根据类型或端点名称接管已有配置。
用户可显式从模板替换该 Provider，建立当前基线后再使用 update。

模板已被删除或禁用、绑定模板来源发生 inline/custom 替换、Provider type/protocol 与模板不一致时，更新明确
失败且 Provider 配置不变。每次成功更新返回新增、更新、删除、保留、冲突 Endpoint 及 Provider 字段的类型化
报告；相同输入重复执行不得产生新的语义变化。

Faux 只作为框架内部安全运行时和测试实现存在，不进入 application/CLI 的 Provider 目录、默认展示、选择或
连接检查；没有用户配置时，公开目录的默认 Provider 为 `null`。

思考 profile 按端点声明原生参数、可展示输出与 continuation。始终思考的模型拒绝关闭请求，
只支持开关的模型不提供 effort 等级。

模板目录以本地文件形式提供：内联目录是唯一真源，首次使用时物化为
`config/provider-templates.json`，用户可在该文件上修改、新增模板并随内联目录自动升级。
本地文件化、版本管理、修改感知与自动升级的完整契约见
[Provider 模板](provider-templates.md)。

类型与协议的设计约束、组合和 CLI 冲突语义见
[Provider 类型与协议规格](provider-protocols.md)。

## 模型路由

持久化路由和创建后的 Host 选择统一使用框架返回的 `provider_id/endpoint_id`；创建表单和配置文件内容不接收
用户指定 ID。`ConfiguredModelCatalog`：

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
`openai-chat-completions` 使用 `/chat/completions`，`openai-responses` 使用 `/responses`，`anthropic-messages` 使用 `/v1/messages`。数值 `0`
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

CredentialStatus 的 available 由 state 只读派生：managed 为 null，not_required/configured 为 true，
missing 为 false。configured 独立表示是否配置凭据引用；missing 可以表示未配置或已配置但不可用。
读取派生值不再次解析凭据。

内建 openai-codex 产品声明 Responses + 同名浏览器驱动，服务策略由组合根解析；其他既有产品保持 static。
Provider/Template DTO 携带连接声明的只读 notice，用于 Host 披露服务限制，不写入用户配置。
factory 要求组合根注入 managed RequestAuthenticator，构造时不读取秘密；未注入时明确失败，不尝试解析为 API Key。现有协议接线见[请求认证规格](request-authentication.md)，生产装配和 Host 启动验收见
[订阅认证规格](subscription-authentication.md)。配置证据见 `tests/config/test_provider_auth_config.py`。

## Provider adapter

Core `Provider` 只暴露 `stream` 和 `check_connection`。Adapter factory 通过 `ProviderProtocol` 构造器注册表
扩展，配置默认 API path 由对应协议档案提供；factory 不读取厂商类型或实例 id。HTTP JSON/SSE transport、transient
错误分类和“仅首个输出前重试”策略由协议实现共享，HTTP/SSE 对象和异常不得进入 core/application。

OpenAI Chat Completions 实现负责：

- system message、文本与原生媒体输入、tool history 和声明式 reasoning profile 编码；
- 普通 JSON 与 SSE transport；
- usage、tool call、reasoning 和错误响应解码；`reasoning_content` 与 `reasoning` 是兼容服务的扩展，
  不是 OpenAI Chat Completions 的标准字段。展示文本优先读取 `reasoning_content`，支持字符串或 block 数组。
  原字段名与原生结构独立保存；流式字符串按顺序连接，block 数组按顺序追加，
  同一字段的形态不能在流中改变。回放不改名，也不从展示文本重建状态；
- 外部流缺失 tool index 时按块位置处理；
- 不符合 Endpoint 能力、媒体格式或消息角色的直接输入明确报错；工具结果按能力提供原生内容或明确降级说明。

Anthropic Messages 实现负责 Messages API 的顶层 system、content blocks、image source、tool use/result、
usage/error 与 SSE event 映射。适配器保存完整、有序的 assistant content 数组；流式响应
按 content block index 组装文本、thinking、signature、工具参数和 citations，在消息完成时保存。续接使用
原生数组，保留 thinking、redacted_thinking 与其他内容的相对顺序，不从 canonical 文本和工具调用重排。
工具调用身份及参数必须与 canonical 工具记录一致。协议差异留在各自 codec/adapter 中。

Endpoint 声明 structured output 能力后才接受结构化 `response_format`。共享请求重试只对明确可重试的
timeout/HTTP 状态执行有界退避；一旦已经收到模型输出，就不再重放请求。每次重试形成可持久化
的 `run_retry_scheduled` 事件。

### 请求等待、错误与资源生命周期

`Provider.timeout_seconds`（默认 60 秒）是等待响应进展的预算，必须为正有限数。
三种 HTTP 协议统一由 `HttpxProviderHTTPTransport` 执行，协议 adapter 不持有总生成时限：

- SSE 每次等待下一条完整 JSON 事件时启动新预算；首次等待包括连接、发送请求和响应头。
  文本、工具参数、思考、生命周期与扩展事件均属于响应进展，不依赖该事件是否产生可见输出。
  持续有事件到达的生成可以超过 timeout；SSE 注释心跳或未完成的数据片段不刷新预算。
- 向消费者交出事件后解除计时；解码、持久化、展示与队列背压不消耗下一次等待预算。
  协议仍需要读取结束标记或 EOF 时，该等待也有独立预算。
- 非流式 JSON 没有中间响应事件，以完整响应为一次等待。HTTP 连接、写入和连接池等待继续保留阶段超时。
- 等待预算耗尽统一抛出 `ProviderResponseTimeout`，经共享请求边界转换为
  `provider_response_timeout` 与 `Model response timed out after … seconds of inactivity.`。
  已知网络异常按实际异常类型保留连接、读取、写入、连接池等错误码；这些安全诊断与认证方式、
  协议或模型名无关。HTTP、认证和协议内容错误各自保留所属边界的分类，未知敏感响应仍须脱敏。

`stream_with_retries` 统一拥有模型尝试预算和退避，认证恢复最多一次且共享该预算。
只允许在模型输出之前重试；隐藏的思考输出也会禁止重放，展示过滤发生在该判断之后。
流式工具参数是尚未获执行许可的增量；失败不会补造成功完成事件，也不会执行部分工具调用。
超时、取消、协议提前结束和显式关闭均逐层关闭当前流，借用的 HTTP client 与认证服务保持可用。
替换 HTTP transport 的实现必须遵守同一等待和关闭契约；协议 adapter 不补设另一套计时器。

跨协议验证见 `tests/providers/test_model_request_timeout.py` 与
`tests/providers/test_transport_diagnostics.py`；完整工具执行边界见
`tests/application/test_responses_runtime.py`。

## 配置分区

Provider/Endpoint 的唯一权威位置是 `config/providers.json`，保留 `providers.default/items` 结构。
MCP、Agent 与个性化提示词分别由 `config/mcp.json`、`config/agents.json`、`config/prompts.json` 管理。
通用 `config/config.json` 包含 `permissions`、`tool_execution`、`run_budget`、`compaction`、`skills`。
各 manager 只修改自己的分区并保留其他当前分区。

## 失败语义

- 配置结构错误统一转换为 `ConfigError`。
- 不支持的思考模式或 effort、profile 覆盖不完整、原生字段归属冲突统一在 HTTP 前失败。
- 默认 Provider 必须存在、enabled 且有默认 Endpoint；否则不能被设为默认。
- 当前默认 Provider 不能删除、禁用或清空默认 Endpoint；须先显式选择另一有效默认路由。不会隐式回退到 Faux。
- Provider connection check 返回类型化结果；CLI 以退出码区分连接失败和配置错误。

## 验收证据

- `tests/config/test_credentials_config.py`
- `tests/config/test_provider_templates.py`
- `tests/config/test_provider_template_store.py`
- `tests/config/test_reasoning_config.py`
- `tests/core/test_reasoning.py`
- `tests/providers/test_factory.py`
- `tests/providers/test_openai_chat_completions.py`
- `tests/providers/test_anthropic_messages.py`
- `tests/application/test_models.py`
- `tests/application/test_configuration.py`
- `tests/application/test_session_models.py`
- `tests/cli/test_run_cli.py`
- `tests/cli/test_provider_template_commands.py`
