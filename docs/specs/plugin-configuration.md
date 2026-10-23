# 插件配置规格

> 文档类型：现行领域规格
> 范围：插件配置表达、静态发现、Host 交互、统一落实管线、凭据与运行时生效
> 实施范围与验证证据见[完成记录](../plans/archive/2026-10-14-plugin-configuration.md)。

本文定义插件配置的结构表达、交互与生效契约。插件系统的生命周期与执行边界见
[插件系统规格](plugins.md)。

## 1. 目标与职责

插件配置是插件行为参数的声明、用户取值及其被接受后的执行快照。契约必须独立于具体插件，能够组合表达
不同领域的配置结构；记忆、自动化、搜索和媒体生成仅是应用实例，不决定框架中的类型或分支。

以下四项是设计与验收的共同目标：

1. OURO 提供通用配置契约，表达结构、约束、说明、默认值与凭据需求，不内置具体插件的业务字段。
2. OURO 提供统一配置落实管线；CLI、TUI、桌面端及其他 Host 使用同一套查询、校验和应用用例。
3. 插件开发者使用契约声明自己的配置结构和字段，并在实现中消费已接受的配置。
4. Host 根据契约理解结构模型，生成正确的配置交互；新增符合契约的插件不要求 Host 增加插件专用代码。

| 参与方 | 拥有的职责 | 不拥有的职责 |
|---|---|---|
| 配置契约与框架实现 | 通用表达、解析、规范化、校验、修订及生效规则 | 插件业务字段、远程服务行为 |
| 插件开发者 | 业务结构、字段含义、约束、凭据需求、业务消费和环境检查 | Host 控件、框架状态文件、配置提交事务 |
| Application | 查询与预检、冲突检查、持久化协调、生命周期应用及结果投影 | 具体界面布局 |
| Host | 表单或文件输入、草稿、提示、错误定位及生效结果展示 | 权威校验、秘密存储、插件重启和提交规则 |

配置声明必须能在插件未启用、配置无效或缺少凭据时读取；查询、静态预检和编辑界面生成均不得执行插件代码。
框架不为发现配置导入插件的 Python 模型，也不启动 worker、MCP、网络请求或后台服务。

## 2. 数据边界与范围

| 数据 | 含义与权威位置 |
|---|---|
| 配置定义 | 不可变插件包中的 Schema 与 Manifest 元数据；定义可接受的取值 |
| 普通配置值 | 用户提交并经框架处理的 JSON object；保存在配置修订中 |
| 凭据绑定 | 插件 slot 到环境变量引用或直接密钥的映射；保存在配置修订中 |
| 秘密材料 | 环境引用由 Host 解析，直接密钥保存在受保护的本地修订；不进入普通配置值或包，公开展示脱敏 |
| 编辑草稿 | Host 或导入文件持有的候选值，不影响正在运行的插件 |
| 业务数据 | 插件私有内容、索引、游标、缓存等，由插件数据接口管理 |
| 原生业务资源 | Task、Session、Run 等，由对应原生领域管理 |
| 调用参数 | 单次 Tool、Command、Service 或 Run 的输入，不改写插件配置 |

配置作用域是当前 `OURO_HOME` 的插件记录，不引入 workspace/project 覆盖、多层优先级或每会话配置分支。
同一插件的包版本可以分别接受配置；不可变修订属于插件记录，包 digest 引用对应修订。

本次实施覆盖 user plugin。Builtin 的配置接入以具体需求为前提，不为本次工作创建另一套内置配置存储。
配置描述不包含任意可执行 UI、动态字段脚本、远程 Schema 获取、热修改实例字段或自动数据迁移。

## 3. 声明与静态发现

### 3.1 Manifest

`configuration` 为可选对象，包含以下字段：

| 字段 | 契约 |
|---|---|
| `schema` | 可选，包内 JSON Schema 文件路径，遵守插件包路径规则 |
| `version` | 正整数，默认 `1`，标识插件配置定义的业务版本，包含普通值和凭据要求 |
| `credentials` | 可选，按 slot ID 索引的描述对象；只提供声明槽位的展示与必填语义 |

每个 credential 描述包含可选 `title`、可选 `description` 和默认 `false` 的 `required`。
`credentials.use.scopes` 是允许解析的槽位集合；配置描述的 key 必须属于该集合，不能扩张权限。
没有单独描述的已声明槽位仍可配置，以 slot ID 为名称、权限 reason 为说明，且不推断为必填。
每个槽位均支持环境变量引用和直接保存密钥，由用户选择绑定方式；插件不需要声明重复槽位。

以下为 Manifest 的配置与权限片段，其他 Manifest 必需字段省略：

```json
{
  "configuration": {
    "version": 1,
    "schema": "./schemas/config.schema.json",
    "credentials": {
      "service_api": {
        "title": "服务凭据",
        "description": "调用所选远程服务时使用",
        "required": true
      }
    }
  },
  "permissions": [
    {
      "id": "credentials.use",
      "scopes": ["service_api"],
      "reason": "访问插件使用的远程服务"
    }
  ]
}
```

普通 Schema 与 credential slots 任一存在，Host 就必须提供配置入口。仅有凭据的插件可以省略 `schema`；
此时普通值必须是 `{}`。没有 `configuration` 时配置业务版本为空；存在该声明时，修订记录其 `version`，
包括只声明凭据的情况。配置业务版本、Manifest 版本、Schema 方言、Plugin API 和 worker protocol 是不同标识。

### 3.2 包校验

安装校验必须确认配置文件在包内、可解码、符合方言、引用可解析、凭据描述合法。
Schema 文件及 Manifest 均参与包摘要。Schema 错误属于无效包；用户必填值尚未提供属于配置未就绪，不能
因此要求安装时执行插件或填写所有用户值。

支持文件内 `$defs`、JSON Pointer、anchor 和递归引用；`$ref`、`$dynamicRef` 的目标必须能在该文档内解析。
标识 URI 不授予网络或文件访问能力。包外、其他文件及远程引用明确拒绝，不通过插件代码补充 resolver。
需要拆分源码 Schema 的插件可以在打包时生成自包含文件，Host 始终取得完整的声明。

## 4. 通用建模契约

### 4.1 数据与约束表达

普通配置采用 JSON Schema Draft 2020-12 的
[Core / Applicator](https://json-schema.org/draft/2020-12/json-schema-core) 与
[Validation](https://json-schema.org/draft/2020-12/json-schema-validation) 语义，根 Schema 显式声明
`type: "object"`。省略 `$schema` 时采用该方言；显式声明其他方言时包校验失败，嵌入的 Schema 资源遵守
同一方言要求。嵌套节点可以采用方言允许的其他类型或 boolean schema。
以下能力属于框架建模契约，不受某个 Host 的原生控件能力限制：

| 维度 | 表达能力与典型关键字 |
|---|---|
| 值类型 | `null`、boolean、integer、number、string、array、object；`type` 数组表达可空等类型联合 |
| 固定与有限选择 | `const`、`enum`，包括非字符串枚举值 |
| 数值 | `minimum`、`maximum`、exclusive bounds、`multipleOf` |
| 文本 | `minLength`、`maxLength`、`pattern` |
| 对象 | `properties`、`required`、嵌套对象、`minProperties`、`maxProperties` |
| 动态映射 | `additionalProperties` 的 Schema、`patternProperties`、`propertyNames` |
| 列表与元组 | `items`、`prefixItems`、条目数量、`uniqueItems`、`contains` 及其数量约束 |
| 类型组合 | `allOf`、`anyOf`、`oneOf`、`not`；`oneOf` 必须恰好满足一个分支 |
| 条件与依赖 | `if/then/else`、`dependentRequired`、`dependentSchemas` |
| 组合后的封闭性 | `unevaluatedProperties`、`unevaluatedItems` |
| 结构复用 | 自包含 `$defs`、本地引用及递归结构 |
| 说明 | `title`、`description`、`examples`、`default`、`deprecated` 和格式注解 |

JSON 值必须使用字符串对象键和有限数值；不接受 NaN、Infinity 或非 JSON 对象。不隐式执行字符串转数值、
空字符串转 `null`、时间转换、环境变量展开或路径重写。缺失字段与显式 `null` 是不同输入。
浮点表示和 JSON 序列化沿框架统一规范处理，不让各 Host 自行生成修订摘要。

`additionalProperties: false` 是插件可选择的封闭策略，不是框架对所有对象的强制注入。
动态映射与组合 Schema 必须能够正常使用；框架不能为了表单方便展开或重写 Schema 而改变其校验语义。

### 4.2 注解与格式

`title`、`description` 和 `examples` 是展示信息；`deprecated` 提示弃用，不自动删除已有值。
说明按普通内容呈现，不执行其中的 HTML、脚本、命令或网络链接。

本契约使用 Draft 2020-12 的 format annotation 语义：`format` 可以提示日期、URI 等编辑方式，但单独声明
`format` 不构成框架已执行该格式断言的保证。需要静态拒绝非法值时，使用受支持的约束组合；环境或领域语义
由对应业务边界检查。要求未支持 vocabulary 的 Schema 在包校验时失败，不得静默弱化必需约束。

`readOnly`、`writeOnly` 不扩展配置的读写权限，也不构成秘密保护。行为派生值通过独立只读视图表达；秘密
必须使用 credential slots。Host 不得因为注解而丢弃已保存字段或向普通值填充脱敏占位符。

### 4.3 建模能力与表单能力

Schema 是唯一的结构与约束来源，不并行维护一份容易分叉的控件字段清单。插件不声明 TUI、Desktop
控件类，也不按 Host 生成不同 Schema。

Host 对支持的结构提供原生控件，对其他结构提供完整的 JSON 编辑路径，并将完整候选值交给框架校验。
正确性要求是完整保留数据与约束；原生控件覆盖率影响交互体验，不改变配置能表达的范围。

需要更具体的展示提示时，只能引入有明确语义的声明式扩展。Host 忽略可选展示提示不得改变值或跳过校验；
会影响执行含义的扩展必须进入显式版本契约，不能假装成可以忽略的注解。

### 4.4 通用组合示例

下面是完整的普通配置 Schema，组合条件字段、动态映射、可空数值、数组、带类型标识的联合和元组。
这些都是协议结构，没有对应的框架业务类型；Host 不需要认识插件身份即可编辑它们。

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "mode": {"enum": ["basic", "extended"], "default": "basic"},
    "entries": {
      "type": "array",
      "items": {"$ref": "#/$defs/entry"},
      "default": []
    },
    "limits": {
      "type": "object",
      "propertyNames": {"pattern": "^[a-z][a-z0-9_]*$"},
      "additionalProperties": {"type": ["integer", "null"], "minimum": 0}
    }
  },
  "required": ["mode", "entries"],
  "additionalProperties": false,
  "if": {"properties": {"mode": {"const": "extended"}}, "required": ["mode"]},
  "then": {"required": ["limits"]},
  "$defs": {
    "entry": {
      "oneOf": [
        {
          "type": "object",
          "properties": {
            "kind": {"const": "text"},
            "value": {"type": "string", "minLength": 1}
          },
          "required": ["kind", "value"],
          "additionalProperties": false
        },
        {
          "type": "object",
          "properties": {
            "kind": {"const": "pair"},
            "value": {
              "type": "array",
              "prefixItems": [{"type": "string"}, {"type": "number"}],
              "items": false,
              "minItems": 2
            }
          },
          "required": ["kind", "value"],
          "additionalProperties": false
        }
      ]
    }
  }
}
```

以下值满足该 Schema：

```json
{
  "mode": "extended",
  "entries": [
    {"kind": "text", "value": "example"},
    {"kind": "pair", "value": ["weight", 0.5]}
  ],
  "limits": {"primary": 8, "secondary": null}
}
```

删除 `limits` 违反条件必填；把 `primary` 改为负数违反数值约束；给 pair 增加第三项违反元组约束；
把 text 的 `value` 改成数组违反联合分支约束。提交 `{}` 时，框架补齐为 basic 模式和空 entries，
不会因为另一个模式需要 limits 就自行创建它。该例用于解释组合能力，不限定插件必须采用这些字段或结构。

### 4.5 静态建模与业务语义

结构可以任意组合，但 Schema 不是业务执行语言。框架的静态接受意味着候选满足声明的数据约束，不表示
路径可写、远程账户有权限或资源之间存在有效的业务关系。不能由标准 Schema 表达的跨字段计算、外部资源
检查和业务约束，由插件在既有候选生命周期或实际操作边界检查；失败沿同一管线返回安全诊断。

Host 无需理解这些业务规则，也不加载插件校验器来生成界面。独立业务检查可以形成单独用例，但不能让
静态配置查询和预检隐式执行插件代码。框架不为补足任意业务判断而增加表达式语言或可执行 Schema 扩展。

### 4.6 契约的扩展边界

新增插件的字段、枚举、嵌套组合及业务配置版本由插件自行定义，不要求修改框架类型或发布新 Host。
新增需要框架解释的语义，例如新的 credential reference 类型或必需校验扩展，才需要框架契约与消费者
共同演进。可忽略的展示注解不能承载必需校验、安全要求或运行行为。

## 5. 默认值与规范化

Schema 校验与默认值填充是两个步骤。JSON Schema 的 `default` 是注解，以下自动填充规则属于 OURO 的
配置契约。框架在安装生成初始值、预检候选和接受用户配置时使用同一个规范化函数，Host 不承担权威填充逻辑。

### 5.1 确定性填充

1. 在独立 JSON 副本上处理候选值，不修改调用方草稿、Schema 或已有修订。
2. 沿明确的 `properties` 和文档内引用处理对象。字段缺失且该字段声明 `default` 时，复制该值后继续处理。
3. 缺失父对象只有自身声明 `default` 时才创建；不因子字段有默认值就自动创建父对象。
4. 对已经存在的数组元素，沿 `items/prefixItems` 处理其中对象的字段；不凭空创建、补齐或删除数组元素。
5. `allOf/anyOf/oneOf`、条件和依赖分支中的默认值仅作提示，不自动选择分支或参与填充。分支约束仍完整校验。
6. 显式 `false`、`0`、空字符串、空集合和 `null` 不被默认值覆盖；显式值是否合法由 Schema 判断。
7. 填充结束后按完整 Schema 校验，再生成配置修订。

该规则限制的是自动补值，不限制组合 Schema 的建模或校验。表单明确选择某个分支后可以让用户接受该分支的
建议值，提交结果仍是普通候选数据，不能让不同 Host 隐式产生不同的有效配置。

声明的默认值按相同规则完成有限展开后，必须符合所在节点的 Schema，包括仅作提示的分支默认值。
同一填充位置通过声明与引用
取得不一致的默认值时，包校验必须拒绝，不以遍历顺序决定取值。根对象由候选提供，不以根 `default`
替换整份候选。声明导致无限递归生成默认对象时，包校验必须拒绝。
支持有限输入中的递归结构，不意味着默认值可以无限实例化该结构。规范化必须幂等，相同输入和定义得到
相同结果，不读取时钟、当前工作目录、网络或环境变量。

### 5.2 完整替换与缺省

提交的 `values` 是完整对象；credential bindings 也是完整映射。字段缺失表示未设置，由上述规则决定是否
使用默认值；不解释为保留旧字段。数组作为完整值处理，不增加 JSON Merge Patch 或按下标合并协议。
Host 局部编辑必须在完整草稿上应用操作，再提交整份候选。

初始值完成默认值处理后仍缺少必填字段或凭据时，保留未就绪配置并阻止激活，安装仍保持默认禁用。
用户应用配置时必须通过完整静态校验；不把无效草稿提交成正在使用的新修订。

既有修订读取、重启和精确恢复均使用已保存值，不重复应用默认值。包升级不能修改旧修订；用户明确将值
提交给目标包后，以目标定义重新规范化和校验。配置重置也是显式提交，不在查询或升级中自动发生。

## 6. 凭据绑定与安全输出

`credential_refs` 是按槽位索引的完整绑定映射，每个绑定采用以下一种形式：

| 形式 | 示例 | 存储与解析 |
|---|---|---|
| 环境变量引用 | `"env:SERVICE_API_KEY"` | 保存引用，当前 Host 在解析时读取环境变量 |
| 直接密钥 | `{"type":"inline","value":"actual-key"}` | 将原始密钥保存到本地配置修订，解析时直接返回 |

环境变量名遵守 `[A-Za-z_][A-Za-z0-9_]*`。直接密钥必须包含非空白内容，合法值逐字符保留。
类型由绑定结构决定；直接密钥即使以 `env:` 开头也不被解释为引用。没有绑定用省略槽位表达，不使用空密钥。
普通配置 Schema 不用字符串字段或 `writeOnly` 承载秘密。

| 状态 | 含义 | 检查边界 |
|---|---|---|
| 已声明 | Manifest 允许插件使用该 slot | 包校验 |
| 已绑定 | 已有合法环境引用或直接密钥 | 静态配置预检 |
| 可解析 | 当前 Host 可以取得绑定的秘密材料 | 候选激活及运行时解析 |
| 远程可用 | 远端接受材料且满足业务权限 | 实际请求或显式连接检查 |

必填槽位未绑定时静态预检失败，配置状态为未就绪；它不等同于“引用已绑定，但当前 Host 环境缺少变量”。
后者是当前 Host 的激活条件失败，不删除已有引用或谎报所有 Host 均不可用。可选槽位未绑定必须允许；
已绑定的引用在运行时无法解析时，不静默回退到匿名身份。

配置查询不解析环境变量。编辑 DTO 的直接密钥使用 `SecretStr`，JSON 展示默认遮蔽，调试表示不包含密钥。
Host 编辑用例可访问完整草稿；可解析性未经本 Host 检查时为未知，不能根据引用推断。
候选准备在执行插件前检查必填凭据的可解析性，各 runtime 的静态必填语义一致。

in-process 通过受约束 resolver 获取本插件声明且绑定的 slot；worker 沿既有受管理环境注入方式获取秘密，
初始化 JSON 只携带槽位对应的临时环境变量名。两种绑定均通过 `context.credentials.resolve(slot)` 读取；
插件无需判断来源。解析到的秘密加入运行时脱敏边界，不进入事件、错误、日志或普通查询输出。
非沙箱的完整信任边界仍遵守[插件系统规格](plugins.md)。

配置摘要包括完整绑定：环境引用只对引用本身计算摘要，直接密钥对真实值计算摘要。改变直接密钥或切换
来源产生新修订，沿配置应用管线生效；仅轮换环境变量值不产生修订，调用者需刷新 Host 环境并重启实例。
已有修订保持不可变，供执行契约精确恢复使用。

插件 state 文件以当前用户专属权限原子写入；直接密钥在文件内是明文，不以界面遮蔽代替存储保护。
普通序列化默认脱敏，持久化和显式编辑导出通过 `include_secrets` 序列化上下文保留完整值。
该绑定不使用 Provider 的受管理认证引用，也不要求插件自行维护密钥文件。

## 7. Application 查询、预检与应用

`PluginService` 是 Host 的统一入口，配置用例实现位于 `ouro/application/plugins/configuration.py`，
公开数据模型位于 `ouro/application/contracts/plugin_configuration.py`。

| 方法 | 返回值 | 副作用 |
|---|---|---|
| `configuration(plugin_id, package_digest=None)` | `PluginConfigurationView` | 无；返回固定目标和当前基线 |
| `preview_configuration(candidate)` | `PluginConfigurationPreview` | 无；返回规范化候选及静态问题 |
| `await configure(candidate, request_id=None)` | `PluginConfigurationResult` | 按目标状态保存或通过运行时发布 |

`package_digest` 和 `request_id` 为仅关键字参数。`view.candidate()` 复制该视图的普通值和凭据绑定；传入
`values` 或 `credential_refs` 时完整替换对应部分，始终保留视图的目标与编辑基线。
配置无效抛出带 `issues` 的 `PluginConfigurationValidationError`；编辑基线过期抛出
`PluginConfigurationConflictError`，包含目标、期望与当前 revision ID。构建期竞争沿用
`PluginStateConflictError`。Host 不从错误消息推导是否提交。

### 7.1 配置视图

统一配置视图至少包含：插件身份、目标包 digest、是否选中、desired enablement、配置业务版本、当前配置
revision ID、完整 Schema、当前普通值、credential slots 描述、凭据绑定和静态配置问题。
对应字段为 `plugin_id`、`name`、`package_digest`、`selected`、`desired_enabled`、
`configuration_supported`、`configuration_version`、`configuration_revision_id`、`schema_document`、
`values`、`credential_slots`、`credential_refs` 和 `issues`。

查询允许指定 installed digest；省略时只解析一次 selected digest，并在结果中返回解析后的身份。
Host 不读取包路径或 state 文件来补全 Schema，不按列表项反复扫描状态；字段编辑只保存框架返回的身份。
没有配置项的插件得到明确的不可配置视图，而不是模糊的加载失败。

外层 DTO 使用类型化字段；配置值使用递归 JSON 类型，Schema 是协议数据。查询结果和草稿不得与框架中的
修订对象共享可变子对象。

### 7.2 候选与预检

配置候选包含 `plugin_id`、`package_digest`、`expected_configuration_revision_id`、完整 `values` 和完整
`credential_refs`。只有框架生成修订摘要，Host 不提交自算的新 revision ID。

预检返回规范化后的候选、字段级问题、是否构成内容变化及预计应用范围。结果不是提交授权或永久有效的
校验票据；应用时重新检查输入和编辑基线。预检不创建修订、不写状态、不启动代码，也不测试远程服务。
返回字段为 `candidate`、`valid`、`issues`、`changed` 和 `scope`；`scope` 的取值为 `none`、`stored` 或
`current_host`，只描述预计范围，不意味着已经应用。

字段问题使用稳定 code、JSON Pointer 路径及安全 message；普通值和凭据分别以 `/values`、
`/credential_refs` 为路径前缀。组合约束可以附带受控的子问题，以便 Host 定位分支冲突。
错误不得包含原始值、整段 payload 或未脱敏的验证器异常；Host 无需解析自然语言确定错误位置。

### 7.3 编辑冲突

应用前比较目标 digest 的当前 revision ID 与编辑基线。不一致则拒绝，返回冲突及可重新查询的目标，
不自动合并过期表单。显式编辑某个未选中版本只修改该版本，不因其他 Host 改变选版而重定向配置目标。

候选构建期间继续使用存储层的 compare-and-commit，防止基于旧共享状态发布。表单基线检查与提交检查
分别覆盖编辑期间和执行期间的竞争，不由同一时间点的检查互相替代。

### 7.4 应用结果

应用结果必须区分：没有变化、已保存但未激活、当前 Host 已发布新实例，以及发布后的降级状态。
结果包含目标 digest、接受的 revision ID、changed、生效范围及必要诊断，不以单个布尔值表示运行健康。
`outcome` 为 `unchanged`、`saved` 或 `published`，对应 `scope` 为 `none`、`stored` 或 `current_host`。
`status` 和 `diagnostics` 表示当前观察结果，不能用 `published` 代替健康判断。

规范化内容与当前目标修订相同时，返回无变化，不重建插件或依赖；故障重试使用显式 restart。
未启用或未选中的包只保存配置，不自动 enable/select。目标是期望启用的选中包时，必须经拥有 runtime 的
application 用例准备并应用候选；无 runtime 的管理入口明确拒绝这类应用，不能跳过激活事务直接提交。
相关 Host 不需要在激活前已有健康实例，修复失败配置也走同一候选流程。

## 8. 修订、运行时与恢复

### 8.1 不可变快照

配置修订以规范化普通值、完整凭据绑定和配置业务版本生成内容摘要。包身份通过既有执行契约
关联，不把 package digest、时间戳或 Host 草稿信息重复写入配置摘要。

已发布 generation 的配置保持固定，插件不具有修改自身框架配置的 SDK 写入口。in-process 获取递归只读
快照，对象与序列的只读形态由 SDK 明确；worker 获取独立序列化快照，不与 Host 共享可变对象。
SDK 对象为递归只读 `Mapping`，数组为 `tuple`；公开 DTO 和 worker JSON 使用普通对象与数组。
插件可以转换为自己的业务模型，但不能反向修改修订或让 Host 导入该模型才能配置。

### 8.2 应用管线

```text
查询固定目标与编辑基线
  -> 检查目标身份与编辑基线
  -> 规范化完整候选并执行静态校验
  -> 创建内存候选修订
  -> 仅保存，或按依赖顺序准备受影响 generation 集合
  -> compare-and-commit 配置意图并发布当前 Host 的 Catalog
  -> 交接自主后台服务，报告观察状态
  -> 按已有 lease、gate 和 owner 规则回收旧实例
```

运行中的配置变更复用现有 activation plan，包括受影响依赖方，不新增单独的热更新回调或配置监听线程。
`prepare/start` 期间私有数据只读，候选资源必须可并存且可关闭；不得提前更新原生 Task 或写入插件业务数据。

候选发布前失败，持久配置引用、selection 与当前 Catalog 不变，并关闭候选资源。发布后的服务交接或
启动失败遵循已发布状态的降级与资源归属，不承诺回滚已经发生的业务副作用。
取消发生在提交前时不得遗弃候选；提交后按现有 publication 收尾规则处理，不以取消暗示写入已撤销。
调用结果丢失时，通过重新查询修订和观察状态确定结果，不自动重放有副作用的生命周期操作。

### 8.3 多 Host 与精确恢复

持久意图由共享 state 文件传播，Catalog 和 generation 属于各 Host。其他 Host 沿现有准入边界及调度刷新
协调，不提供全员 ACK 或全 home 瞬时切换保证；一个 Host 激活成功不证明其他 Host 的环境和凭据可用。

同一模型决策及其已绑定调用继续使用原有配置快照，新决策采用新 Catalog。持久等待中的执行遵守精确
release、配置修订、依赖和贡献契约；不同调用类别按插件系统的恢复规则租用原实现或明确失败，不能
静默把旧调用转给同名新实现。

既有修订被活跃实例或持久执行契约引用时不能删除。包升级、移除、GC、配置修订保留与数据维护复用
插件系统的资源保留规则，不另建配置迁移或历史实例系统。

## 9. Host 自动交互

| Schema 信息 | 通用交互要求 |
|---|---|
| boolean、number、integer、string | 保留 JSON 类型的输入；呈现约束及说明 |
| `enum`、`const` | 精确保留值与类型；不把对象或数值枚举变成标签字符串 |
| object | 嵌套编辑、缺失/已设置状态；动态映射允许编辑键及其值 |
| array、tuple | 增删、排序、索引定位及各项约束；不丢失未原生渲染的数据 |
| nullable、optional | 区分未设置、`null`、空字符串与空集合 |
| 联合与条件结构 | 用户明确选择或修改分支；有效性由完整 Schema 校验决定 |
| credential slots | 独立绑定入口、必填提示及安全状态，不混入普通值 |
| 复杂或递归结构 | 有界展开或完整 JSON 编辑，保证能处理有限配置实例 |

Host 可以针对可识别的结构提供条件表单，但不能自行实现一套简化且不等价的 Schema 校验器。
上表定义数据操作能力，不要求每种结构都有专用控件；完整 JSON 编辑可以承载动态映射、元组、联合和递归
结构。TUI 复用现有输入、选择和分组组件处理常见类型，复杂模型使用 JSON 编辑，保持导航与样式一致。
插件管理操作统一归入 Actions，删除使用主题危险色。配置页的值预览与操作提示显示在名称右侧；
未设置的值和未绑定的凭据不显示状态占位标签；必填标记只在字段缺失或凭据未绑定时显示，已填的 false、0、
null 和空集合不视为缺失。字段说明与约束放在编辑提示中。
凭据与供应商密钥共用行内编辑器：Enter 直接编辑当前行，Tab 切换 KEY / ENV，右侧显示当前类型，输入始终
单行遮蔽。两种类型分别保留本次编辑的输入，Enter 保存当前来源，Esc 撤销本次输入与类型切换。保存一个
来源替换该槽位原有绑定；清空后保存解除绑定。已绑定的行显示固定长度的主题遮蔽符及 `[KEY]` / `[ENV]`。
JSON 预览和编辑均保持单行，预览按菜单宽度截断，编辑窗口随光标水平滚动，完整值不受显示长度影响。
JSON 输入优先展示 Schema 提供的示例；缺少示例时可基于结构生成有限的编辑骨架，明确需要补全的字段。
占位内容不自动写入草稿，也不承诺任意复杂约束均能自动生成有效实例，最终仍以框架预检为准。

切换分支不得静默删掉不在当前面板中的值；需要删改时明确显示结果，由用户提交完整草稿。
依赖与条件错误由框架预检定位。布局可以不同，提交相同候选必须得到相同规范化与静态校验结果。

只有在用户明确提交后才应用配置；控件修改、焦点离开、表单预检或取消不写入有效配置。
提交失败保留用户草稿和问题，允许继续编辑；冲突必须重新取得基线，不能用自动重试覆盖他人修改。

OAuth 登录、远程连接测试、管理具体 Task 等操作由独立 application 用例负责；配置结构不隐式生成
任意业务动作。Host 可以组合展示，但各领域仍保持自己的权威状态和提交规则。

## 10. 文件编辑与领域消费

### 10.1 文件输入

CLI 的 JSON 文件输入和 Host 表单提交同一完整候选模型。导出固定 `plugin_id`、`package_digest`、
`expected_configuration_revision_id`，并包含完整 `values` 与 `credential_refs`，使离线编辑保留原始基线。
调用方另行指定的目标与文件不一致时明确拒绝，文件不能隐式选版或启用插件。
显式 `--export` 包含直接密钥的完整值并限制导出文件为当前用户可访问；普通查询与预检输出保持脱敏。
导出不包含环境变量解析值、运行实例、包文件路径或内部 state 结构。将值用于其他版本时，用户必须明确取得目标包
的新基线并重新提交，而不能在文件导入时自动抹去过期基线。

文件只作为导入材料，不是并行的运行时真源。读取后应用框架规范化、校验、冲突检查和激活流程；不提供
每插件配置文件 watcher，不支持直接编辑 `plugins/state.json`，也不把配置写回不可变包。

插件的专有格式如果承载需要冻结的配置，应先转换为标准候选再显式提交；协议不要求静态发现时加载插件
解析器。仅保存可变外部文件的路径，不能声称文件内容也已纳入修订。动态业务输入文件由插件说明其独立语义。

### 10.2 消费边界

路径字段必须说明相对基准；默认私有文件路径使用插件 data 根，不依赖 Host 当前工作目录。框架不因字段
名称为 `path` 就自动重定位、授予文件权限或迁移数据。外部路径访问由使用该字段的业务边界负责。

调度默认值可以作为插件配置，具体持久计划使用原生 `TaskSpec/TaskSchedule`。默认值改变不自动重写已存在
任务；任务变更走 Task 用例，不在配置修订与 Task 中重复维护同一计划。

配置必须有明确消费者。in-process 和 worker 使用快照；declarative 资源只采用贡献适配器明确支持的映射。
MCP 的 credential slot 绑定沿现有通道处理，普通配置不会自动替换 URL、args、Skill 文本或 Manifest。
通用建模能力不意味着任意资源已经拥有通用插值语义。

## 11. 分层、版本与处理成本

配置定义、规范化和静态校验集中在 `ouro/plugins` 的配置模块，readiness、安装和运行时复用同一实现。
Application 持有配置用例与类型化 DTO；CLI/TUI 不直接调用存储层或解析内部包路径。
Core 不依赖 Schema、Manifest、Host 或插件安装实现。SDK 只公开运行时所需的配置和凭据读取契约。

配置查询、规范化和校验在各自用例内复用已加载的定义，预检处理候选值，不扫描全部历史修订。
安装复用包校验取得的定义；运行时的独立准入检查仍验证配置与不可变包完整性。不引入持久 Schema 缓存或
配置 watcher；后续优化必须以重复 I/O 或 validator 构建的实际测量为依据，且不能跨包摘要误用定义。

该配置契约由 Plugin API `5.0.0` 提供，依赖它的插件声明 `engines.plugin_api: ">=5.0.0,<6.0.0"`，使不支持
语义的 Host 在包兼容性校验时拒绝。Manifest 为版本 `3`，worker protocol 为 `3.0`，state 为版本 `4`；
worker 的普通配置通过独立 JSON 对象传递，凭据按槽位注入；持久修订保存完整绑定。配置业务版本变化不自动迁移数据。

## 12. 验收契约

| 编号 | 必须验证的行为 |
|---|---|
| PC-01 | 不启用、不执行插件即可查询完整定义；仅凭据插件可配置；候选包可独立编辑 |
| PC-02 | 标量、嵌套、动态映射、数组、元组、联合、条件、依赖、组合后封闭性和递归结构均有有效与无效用例 |
| PC-03 | 文件内引用和复用有效；包外/远程引用及不支持的必需 vocabulary 被静态拒绝，无网络获取 |
| PC-04 | 默认值确定、幂等；保留显式空值和零；不自动选分支；非法及无限默认展开被拒绝 |
| PC-05 | 不同 Host 对同一完整候选得到同一结果；复杂 JSON 编辑不丢字段、不改变类型 |
| PC-06 | 不可变快照无嵌套别名；插件或 DTO 调用方不能使修订内容与摘要分离 |
| PC-07 | 两种凭据绑定均可保存和解析；普通查询、日志、错误和初始化协议不暴露密钥，显式编辑导出可无损导入 |
| PC-08 | 过期表单和构建期竞争均被拒绝；目标 digest 固定；其他字段和插件状态不被覆盖 |
| PC-09 | 相同配置不重建；未选中/未启用版本只保存；期望启用的选中版本必须经 runtime 应用 |
| PC-10 | 候选失败不提交；取消与资源清理有归属；发布后失败如实呈现降级及已提交状态 |
| PC-11 | 配置变更重建受影响依赖方；旧调用保持原绑定；持久恢复不偷换执行契约 |
| PC-12 | 多 Host 沿同步边界协调，环境差异明确；不宣称跨 Host 原子激活 |
| PC-13 | 文件导入与表单等价；错误草稿可继续编辑；数据路径变化不自动搬迁，默认调度变化不重写任务 |
| PC-14 | 通用 Schema fixture 的交互无需识别插件 ID；代码中没有面向示例插件的配置分支 |

实际验证证据与未验证环境见[完成记录](../plans/archive/2026-10-14-plugin-configuration.md)。
