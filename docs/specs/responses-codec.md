# Responses 编解码

> 文档类型：现行规格
> 范围：Responses adapter、配置/工厂注册、请求认证、内容编码、SSE 事件投影与完整续接封装。浏览器订阅产品已内建注册；Windows/browser/gpt-5.5 真实持久凭据 CLI 闭环已验收。

产品接入契约与待验证能力见[订阅认证规格](subscription-authentication.md#s7-responses-与-continuation)和[验证计划](../plans/subscription-validation.md)。模块位于 `ouro/providers/openai_responses/` 下的 `codec.py`、`items.py`、`stream.py` 与 `errors.py`。它们只依赖 canonical 消息/事件和纯协议数据，不依赖 OAuth、配置、HTTP 客户端或实验目录。

## 请求内容

`responses_input` 返回 instructions 和 input：system_prompt 与 system 消息依次合并为 instructions；普通 user/assistant 文本转为消息输入。先复用 model_visible_message，保留 OURO 的语义标签与技能内容投影。

canonical tool calls 转成 function_call，其 call_id 来自 ToolCallRequest.id；工具结果转成 function_call_output，其 call_id 来自 ToolResultReference。供应商 item id 不用作工具结果关联。工具 JSON 不允许非有限数字；缺少引用的工具结果明确失败。

Endpoint 声明 `supports_image_input` 时，用户消息和工具结果中的图片按顺序编码为 `input_image`，data URI 使用 `image_url`，
图片前后文字为 `input_text`。工具结果直接使用 `function_call_output.output` 内容数组；纯文本结果使用字符串。
不支持图片输入时，工具图片经通用结果投影转换为明确说明，原始结果不变；直接传入的用户图片及其他角色的图片明确失败。
Application 按[附件规格](attachments.md)先准备合法输入。协议依据
[OpenAI Images and vision](https://developers.openai.com/api/docs/guides/images-vision) 和
[Function calling](https://developers.openai.com/api/docs/guides/function-calling#formatting-results)。

`responses_tools` 将现有 parameters_schema 原样放入平铺 function 工具声明，显式 strict=false；不为了满足 strict schema 改写可选参数、required 或 additionalProperties。内置工具、结构化输出、图像和模型参数能力不由这一内容 codec 承诺。

模型名、reasoning 参数、温度、输出硬上限、stream/store 策略及认证由 adapter/服务规则负责。本 codec 不接收任意 request_options，也不将预算预留解释为后端输出上限。

## Adapter 与配置边界

`OpenAIResponsesProvider` 只实现线协议，factory 仅按 `openai-responses` 选择构造器。`custom` 类型必须显式选择协议，支持 Responses + static 组合；不据此注册 OpenAI 订阅产品或推断任何厂商能力。默认 API path 为 `/responses`。

adapter 借用 `RequestAuthenticator`，每次尝试重新 acquire；用同次快照的 headers 发送请求、scope 校验输入并封装输出。最终目标、刷新和账户身份仍由认证端口负责；额度分类与计费归属仍由注入的服务规则负责。adapter 不导入具体 OAuth driver，也不识别品牌、客户端 ID 或账户套餐。

请求固定携带完整本地 input、store=false、include=[reasoning.encrypted_content]，不使用远程 conversation/previous_response_id 或 background。request_options 不能覆盖上述字段、model、tools、stream、instructions、temperature 或输出上限字段。truncation 不发送，沿用标准协议的 disabled 默认行为；该字段仍由协议拥有，禁止 request_options 启用自动截断。用户实测订阅后端拒绝显式 truncation，省略该冗余字段不需要厂商分支；后端实际超限行为仍须验收。

max_output_tokens 是可空正整数，完整配置缺失和 null 均表示 OURO 不配置输出上限，本地输出预留为 0。有限值等额预留。adapter 仅在最终请求值非空且 ProviderRequestPolicy.supports_output_limit 为 true（默认值）时发送该字段。明确声明 false 的服务不发送该字段，实际输出可能超过本地预算，使用入口必须披露限制。该能力不由认证方式、计费类型、URL 或模型名推断；request_options 不能覆盖，HTTP 失败后不自动调整。Codex 探针基于实测参数拒绝声明 false，省略后的正式路径已在 Windows/browser/gpt-5.5 真实实验中通过文本、刷新及工具续接；标准 Responses 继续支持发送上限。

reasoning profile 只能声明原生 reasoning 对象，不能复用 Chat Completions 的 reasoning_effort。text.format 需要结构化输出能力；原生图片需 Endpoint 声明 `supports_image_input`。未声明工具能力时拒绝工具输入及工具输出。展示层可隐藏 reasoning_delta，但隐藏发生在共享重试边界之后，已收到思考输出仍禁止重放。完整 reasoning item 的保存不受展示开关影响。

JSON 与 SSE 使用同一 decoder/完成校验。等待预算、错误分类、重试与关闭遵循
[跨协议公共契约](provider-configuration.md#请求等待错误与资源生命周期)，不设置响应总生成时限。
SSE 在等待下一条完整事件时启用 Provider timeout，持续到达的工具参数、思考及生命周期事件均刷新等待预算。
显式 check 保留端点的流模式和输出上限，避免用 1 token 限制误判需要思考预算的模型。
外层关闭逐层关闭当前尝试、decoder 与 HTTP 流；借用的认证服务/HTTP client 不被关闭。

managed scope 使用稳定服务/账户身份，刷新 revision 不改变 scope。static 无法证明跨重建的账户身份，因此使用认证实例生命周期内的随机绑定标识，不持久化或散列 API Key。同实例可原生重放；重建 static 认证后从 canonical 消息帧继续，不发送旧 scope 的 opaque items。

## 完整续接

可见思考支持 `response.reasoning_summary_text.delta/done` 与 `response.reasoning_text.delta/done`，
分别按 summary_index 和 content_index 跟踪，并从选定的完整 reasoning item 的 summary/content 补齐未展示的文本。
两条流均投影为 canonical reasoning，不混入回答正文；首次定稿文本须与已输出增量一致，重复完整项不重复展示。
reasoning 的 summary/content 可缺省、为 null 或为空，终态无需重复流式思考文本；完整原生 item 仍原样续接，
不将仅用于展示的 delta 拼造为续接数据。消息正文和工具参数须与流式输出一致，关联身份与成功终态须有效。
事件与字段定义见 [OpenAI 流式事件规范](https://developers.openai.com/api/reference/resources/responses/streaming-events)。

完整输出由 `OpenAIResponsesItemAccumulator` 按 output_index 收集。每个位置的首次 output_item.done 定稿一个完整原生对象；终态 output 快照只补充尚未定稿的位置。只有终态快照的响应使用其中的完整对象。delta 仅用于展示，不构造续接材料。

每个完整项只投影一次，文本、工具调用与续接均使用同一个选定对象。已定稿位置的重复完整副本直接跳过，不合并或比较其正文、可选字段、附加元数据和密文。终态副本独有的字段不拼入已定稿对象。已定稿位置的 added 和部分 done 通知不改变输出；该位置的新 delta 明确失败，保证已展示内容与续接对象一致。收集器保存和返回独立副本，按原始位置排序；参与收集的观察须保持 type/id 一致、item id 不跨位置重复、索引连续且已观察项均完成。隔离探针复用此收集器。取值规则仅依赖协议事件与位置，不依赖厂商、模型或认证方式。

接收字段以官方 schema 的含义为准，校验限于 OURO 消费及重放所需的语义，不要求服务复刻全部响应元数据。function_call.id/status 可省略，id 可为 null；call_id、name 和合法 arguments 仍必需，不以 item id 替代 call_id，也不伪造 id。message/reasoning 的 id、消息 role/type 与内容类型维持明确校验。省略 annotations/usage 或提供未知附加字段不会单独导致失败；提供的实际 usage 则须合法。未知元数据事件可忽略，未知内容/可执行类型明确拒绝，避免把未支持能力误报为成功。

ProviderContinuationArtifact 外层保留 provider_id、protocol=`openai-responses`、model_id；blocks 只含一个严格版本 1 envelope：

```json
{
  "format_version": 1,
  "scope": ["service-identity", "account-identity"],
  "items": ["完整供应商 output items；此处仅为结构示意"]
}
```

scope 由调用者从同次认证快照传入，不含 Token、有效期或 refresh revision。codec 不重新读凭据。没有 continuation 的普通文本不要求账户 scope；生成或重放 opaque 状态必须有非空稳定 scope。

原生重放是请求编码的可选路径。provider/protocol/model 不匹配时，不解释来源 codec 的 blocks，直接编码该消息的 canonical 正文和工具调用。同一路由先校验 envelope 的版本与字段，再比较本次认证 scope；scope 不同同样使用 canonical 消息帧。选择只影响请求投影，不修改持久化附件，也不发送其他路由或账户的 opaque items。

路由与 scope 均兼容时，重新验证完整 item 结构。function_call 的 call_id、name、解析后的 arguments 及次序必须与 canonical tool_calls 一致。损坏的 envelope、未知可执行 item、工具帧矛盾明确失败，不通过退回 canonical 编码掩盖数据错误。

有效 continuation 的 items 原样替换对应 assistant 消息的 canonical 文本和工具调用编码，不重复构造一份；tool_result 仍独立编码。reasoning.encrypted_content、消息 phase、annotations 及其他附加字段保留。OURO envelope 不发送给供应商。未知可执行 item 类型明确拒绝，不能当作普通文本忽略。

## Runtime 与会话边界

Responses 复用既有工具循环、消息存储、恢复入口、分支和压缩管线。成功的模型轮次保存完整 continuation 与 canonical 工具帧；同一历史执行范围内重建 Runtime 后，managed 认证使用稳定 scope 续接，401 刷新不会改变重放内容或重新执行已完成工具。

协议以 incomplete、failed 或 error 结束的响应不提交成功 continuation 或用量，也不能执行已观察到的工具调用。
重启后使用 continue_run 恢复已有任务，不追加提示词，不重复执行已完成工具。当前 Session 模型在通用请求边界绑定；跨模型、协议或账户继续使用 canonical 完整帧，兼容原生附件可随对应消息重放。后续成功响应才能触发新的工具执行和用量记录。

分支保留完整 canonical 工具调用/结果帧，沿用现有规则隔离源执行范围的 opaque 状态；不会把源 Run 的执行许可带入新分支。源消息保持不变。压缩以 canonical 历史生成摘要，不把密文或续接 envelope 发送给摘要模型；主模型请求仅保留未被摘要覆盖的完整帧及其有效 continuation。

## 流与完成边界

ResponsesStreamDecoder 处理一个响应，responses_events 将已有 HTTP/SSE transport 的 JSON 迭代器接入该解码器。后者在完成、失败、取消和显式关闭时关闭拥有的流迭代器，不拥有认证或客户端生命周期。

| 输入 | canonical 行为 |
|---|---|
| output_text/refusal delta | text_delta |
| reasoning_summary_text delta | reasoning_delta；不展示加密 reasoning |
| function_call item 与 arguments delta | tool_call_delta；工具序号从零连续，不泄漏 output_index |
| text/arguments done、首次选定的完整 item | 校验已输出前缀，仅补尚未输出的后缀 |
| 重复完整 item、终态 output 快照 | 已定稿位置直接跳过；缺失位置定稿并投影，不重复文本、名称、ID 或参数 |
| completed 且流正常结束 | 完整验证后发一个 continuation（有 output 时），再发一次 done/usage；Core 独立判断 Agent 是否可继续 |
| incomplete、failed、error、提前 EOF | 固定协议错误，不发成功 done 或 continuation |

`done` 表示已验证的协议结束边界，不代表 Agent 已完成任务。完整响应中的 refusal 投影为可见文本，完成原因
规范化为 `content_filter`；reasoning item 的存在通过 `ProviderCompletion.has_reasoning` 表达，即使没有可见
摘要也不归为完全空响应。Core 对只有思考、空输出和拒绝响应分别报告失败，保留可见内容和此 `done` 携带的
usage，不将失败响应的 continuation 提交为可重放状态。

message、reasoning 和 function_call 是当前支持的完整输出类型。未知元数据事件可忽略，选定的完整项包含未知输出类型时明确失败。完整工具 arguments 必须为 JSON 对象，拒绝重复键、非有限数字、数组和残缺 JSON；同响应 call_id 不得重复。交错工具通过各自 item identity 聚合。

按事件语义应用终态约束：终态后再次收到已识别的输出、生命周期事件或重复终态明确失败；未知事件类型在终态前后均忽略，不按供应商或具体扩展名称设置特例，不从扩展事件推断完成或用量。事件仍须带字符串 type，选定的完整 output item 内的未知类型仍失败。完成事实延迟到输入流正常结束后提交，避免已经发出成功/usage 后又发现矛盾。解码器 finish 后不再接受事件。网络等待由公共 transport 计时，重试预算由共享请求策略拥有。消费者提前退出时必须显式关闭外层异步生成器。

缺少有效 completed 终态的响应按协议失败结束，即使此前已展示了工具参数，也不能触发 Core 的 done 边界执行。
这类协议失败不发 usage；只有经过验证的完成事件才能进入当前用量账本。

usage 只采用供应商实际值，缺失时为 None；input_tokens 包含 cached tokens，reasoning tokens 已包含于 output_tokens，不重复累加。金额保持未知。错误携带固定本地错误码；输出项冲突说明包含本地校验名称和适用的 output_index，不附原始 SSE、item id、工具参数或密文。

## 验收与证据边界

- `tests/providers/test_openai_responses_codec.py`：请求内容、工具 schema、两轮完整工具/reasoning 续接、MessageRecord JSON 保存/恢复、跨路由/scope 的 canonical 编码、工具帧一致性及 usage。
- `tests/providers/test_openai_responses_stream.py`：item/最终快照的三种组合、交错工具、增量去重、拒绝/仅 reasoning/空输出、错误/截断/矛盾终态、共享 SSE transport 及关闭/取消。
- `tests/probes/test_codex_responses.py`：实验与正式收集器共用后的回归；架构测试保护纯协议边界。
- `tests/probes/test_codex_adapter.py`：正式 adapter 的内存探针，合成 HTTP 验证登录材料首轮请求、跨刷新加密 reasoning 重放、两轮工具、服务声明省略输出上限及预算限制披露、HTTP 失败/取消清理及安全摘要；真实操作见[探针说明](../../dev/probes/README.md#正式-adapter-验证)。
- `tests/providers/test_openai_responses.py`：配置/工厂到请求、原生 reasoning、静态绑定、managed 刷新续接/换账户、显式关闭、隐藏思考后的禁止重试。
- `tests/providers/test_model_request_timeout.py`：三种协议的持续输出、无输出进度事件、消费者背压、停滞、超时重试、隐藏思考后的禁止重放、取消及显式关闭。
- `tests/providers/test_request_authentication.py`：三种协议共用两种不同头编码的 driver，覆盖 JSON/SSE、注销、共享重试预算及安全错误。
- `tests/application/test_responses_runtime.py`：完整 LocalApplication + 正式 HTTP adapter，覆盖文件/SQLite 和 JSON/SSE 四种组合下的两轮工具、reasoning、重启后 401 刷新续接、注销、不完整响应恢复、跨协议/模型切换后的无输入续跑及普通提交、分支与压缩；验证工具只执行一次、原历史保留、实际 token 入账、订阅计费归属及未知金额。

自动测试使用合成认证驱动与服务响应。用户提供的 Windows/browser/gpt-5.5 正式 adapter 真实摘要已验证登录、刷新、两轮工具/结果续接及跨刷新加密 reasoning 重放，scope 保持稳定，见[实测证据](../../dev/probes/README.md#正式-adapter-验证)。此结果不包含原始 SSE fixture，也不替代持久凭据下的真实 Host 闭环、其他模型能力或设备码验收。生产组合已注册；持久凭据的真实 CLI 登录、check/run、跨进程续接及注销已单独通过，见[使用指南](../guides/subscriptions.md#证据范围)。tests/application/test_codex_subscription.py 使用正式驱动、回调、文件凭据与合成 HTTP 验证登录保存、重建、刷新、工具、续接和注销；CLI/TUI 验证正式产品入口与服务限制披露。

协议依据：[官方 Responses 流事件](https://developers.openai.com/api/reference/resources/responses/streaming-events)、[请求格式](https://developers.openai.com/api/reference/typescript/resources/responses/methods/create)、[reasoning 续接](https://developers.openai.com/api/docs/guides/reasoning?api-mode=responses)。官方要求以完成 item 作为续接材料；OURO 的状态收敛与 scope 校验是本地契约。Codex 终态可省略完整 output 的实测边界见[探针说明](../../dev/probes/README.md#验证边界)。

字段核对：[官方 FunctionToolCall schema](https://developers.openai.com/api/reference/python/__sdk_schema?declaration=%28resource%29+responses+%3E+%28model%29+response_function_tool_call+%3E+%28schema%29&selected=%28resource%29+responses) 区分可选 id/status 与必需 call_id/name/arguments；[OutputMessage schema](https://developers.openai.com/api/reference/python/__sdk_schema?declaration=%28resource%29+responses+%3E+%28model%29+response_output_message+%3E+%28schema%29&selected=%28resource%29+responses) 定义 message/content/output_text 及 phase。完整项按协议事件定稿并原样续接。
