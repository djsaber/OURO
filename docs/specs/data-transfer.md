# 配置与数据传输规格

> 文档类型：现行规格

## 配置所有权

`OURO_HOME/config/` 使用有限的领域文件，不提供任意 include、覆盖顺序或双写：

| 文件 | 权威内容 |
|---|---|
| `config.json` | 通用运行设置、权限和 Skills 发现策略 |
| `providers.json` | `providers.default/items`，包括 Endpoint 与认证引用 |
| `mcp.json` | `mcp.servers` |
| `agents.json` | `agents.revision/definitions/runtime` |
| `prompts.json` | `prompts.revision/replacements/additions` |
| `provider-templates.json` | 当前版本的模板目录及 overlay 元数据 |

领域 manager 保留类型化接口；`JsonConfigFile` 负责所属文件的跨进程 read-modify-write 锁与原子替换。
每次读取只解析所属文件。独立领域的查看和修改不读取共享文件或其他领域文件；需要组合配置的应用操作
显式读取其依赖；运行配置快照、Provider 导入的压缩模型引用检查仍须读取共享配置。领域导出只读取对应领域文件。
读取主文件时，出现已拆分分区则明确拒绝。
损坏或不支持的数据只阻止依赖它的操作，不能退回默认值继续执行。不提供历史格式兼容或自动迁移。

## 应用边界

`DataTransferService` 接受 `DataTransferRequest` 并返回 `DataTransferResult`。它编排配置、技能与会话传输，
不依赖 argparse、Textual、控制台输出或 Host 生命周期。配置验证及跨域引用检查归属 application，
归档格式和技能文件发布归属 `ouro.transfer`。会话业务编排使用 `SessionStore` 与 `ResourceStore` 端口，
文件和 SQLite adapter 各自实现发布边界。

组合根延迟装配会话服务；只传输配置或技能时不初始化、不读取 Session 数据库。
已有 LocalApplication 使用自己的 canonical SessionStore，TUI 不重新推断后端。
`ouro.data_commands` 仅包含 CLI/TUI 共享的参数适配和结果格式；CLI 和 TUI 通过同一应用入口执行。

## 命令契约

```text
ouro data <export|import> --kind <kind> --path <archive> [--id <id>] [--replace]
```

CLI 另有 `--home`、`--session-backend`、`--json`。TUI `/export` 仅导出当前会话，不接受参数，未打开会话时明确提示。
其余操作通过 Actions → Manage data 分页菜单生成同一业务请求，不提供 `/import` 命令。
导出通过菜单选择类型；导入选择归档后，应用层根据通过格式与版本校验的 manifest 自动识别类型，
TUI 显示相应范围与覆盖选项；不按文件名推断类型。正式导入重新校验归档及识别类型，避免文件更换后误导入。
无需填写 flags 或会话 ID；会话候选读取复用应用层只读查询，ID 显示为右侧标签。
菜单内动态展示后台传输与结果，不写入对话栏。菜单关闭不取消 App 持有的操作，重开继续查看；同一时间只接受一项传输。
kind 取 `providers`、`provider-templates`、`mcp`、`agents`、`prompts`、`sessions`、`skills`，
一次操作一种类型。

- 五类配置按领域完整传输。已有非空目标必须显式 `--replace`；配置不做深合并或按字段猜测覆盖。
- Agent 与个性化提示词导入在目标当前 revision 上递增，不采用源机器的 CAS revision。
- Provider 导入不能使当前压缩模型的专用 Provider/Endpoint 引用失效。
- 会话 export/import 均支持重复 `--id`。省略则处理全部源会话或全部归档会话；子会话 ID 解析为所属
  primary tree，包含其全部子会话与 Delegation。普通 fork 的来源仅作历史 provenance，不强制传输来源。
- 会话导入保留 ID。默认跳过存在任一 ID 冲突的完整 primary tree；显式 `--replace` 替换所选整树，
  包括其子会话和附件。已有会话必须空闲或失败且没有活跃 Run；同一 ID 的树归属与会话类型必须一致。
- 技能传输原生用户目录 `OURO_HOME/skills` 中的完整内容；export 可重复指定技能名称。导入同名冲突
  可显式替换，不删除未选中技能。保留脚本执行位，排除版本控制、虚拟环境及常见缓存目录。
  不传输 discovery policy；替换成功后清除目标同名技能的旧安装来源，保留启停等策略。
- 导出已存在文件需要 `--replace`；目的地不能指向 OURO 管理的配置、会话、资源、技能、凭据或锁路径。

配置及技能导入只改变持久数据，不会在导入时连接 MCP 或执行技能。个性化提示词由每次模型请求读取，
技能在目录查询、显式引用接受和模型请求准备时读取，导入后均无需重启；其他配置由运行中的 Host 重启后加载。

## 归档与验证

ZIP 根部 `manifest.json` 包含 `format=ouro-data`、`version=1`、kind 与领域 data。
配置原样保留当前权威 JSON 结构。会话 data 使用后端无关的 `SessionBundle`，包含当前 Session data layout
version；内容文件由清单中的类型化引用关联。技能内容位于 `skills/<name>/…`，资源内容位于 `sessions/<session_id>/resources/files/<filename>`。

读取前检查数量与声明解压体积，上限 10,000 条目与 512 MiB。拒绝路径穿越、绝对路径、Windows 特殊路径、
符号链接、非普通文件、加密条目和大小写冲突路径；只向新建 staging 写入已校验的条目，不使用 `extractall`。
会话资源逐一校验 SHA-256、大小，以及消息文件项、媒体资源引用、队列和最近请求媒体/源消息的清单闭包；内部观察资源同样随归档携带。未知 kind、版本、无效结构、重复 identity、外部 Session 引用、
断裂的 parent/child 关系与缺失 Harness checkpoint 都会明确失败。

归档是私密数据载体，不是脱敏的查询输出：配置中原有字面量、请求头、环境配置与对话正文会保留。
托管凭据 store 不传输，也不读取环境变量的秘密值；managed credential 引用可能在目标不可用，需重新认证。
归档不包含插件安装包、第三方 Host 的技能目录、外部工作区、自动任务定义或独立计费账本。

## 会话容错与依赖

导出持有选中 primary 的运行所有权，并取得全部 child 的运行所有权及只读边界。
源会话必须 inactive，不在导出过程中恢复或中止源任务。全量范围由开始时的 Session 集合决定，不承诺
对整个 OURO_HOME 做跨领域、跨新建会话的全局时间点快照；归档包含 archived 会话，不包含回收站。
Files 存在未决原生决定时明确要求先恢复，不在导出内安装决定。SQLite 使用读事务，导出读取附件时
不占用数据库写事务；运行所有权继续保护所选树及资源闭包。释放读取边界前取得实际资源字节，
返回的归档不依赖随后可能删除的源路径。

完整传输包括 Session、消息事实、Input/状态、Call/started/Outcome/状态、events、runs、compactions、
extension states、Harness versions、请求小头、最近 Manifest、Delegation 及关联附件；不包含已退出的完整提交回执。
未知插件和工具名称不参与导入可行性判定：

- 插件的 owner id、私有 state schema version 与 payload 原样保存，不加载插件或解释其私有数据。
- 历史 tool call/result、模型引用和契约 identity 原样保留，不替换工具、不伪造结果、不丢弃未知状态。
- 导入结果携带外部引用诊断与 Session identity；查询历史不需要这些依赖。诊断只声明引用被保留，
  不把“已安装同名插件”误当作精确契约兼容。
- 用户显式继续会话时，由既有 Runtime 执行模型、工具和 extension/controller 契约检查；无法满足时
  明确失败。导入不会绕过审批、自动安装依赖、自动启动 Run 或强制清除控制器状态。

结构损坏和依赖缺席是不同错误。依赖缺席可以保留数据；损坏归档不能通过跳过记录伪装成成功。

## 写入与失败边界

配置在临时目录复用领域校验，成功后在目标文件锁内检查冲突并原子替换。
技能先完整展开并校验 SKILL.md，再在原生安装共用的锁内检查冲突、备份与发布；普通写入失败回滚
本批次的目录替换和安装来源更新。回滚本身遇到文件系统错误时，保留原文件备份并报告其位置。
导出也持有相同锁，避免与原生安装、删除或导入交错读取。

SessionBundle 在文件后端先写入私有 staging store，校验完整快照，再持有全部目标 mutation locks 发布。
发布期间遇到普通异常会撤回本批次已发布目录。SQLite 在一个 transaction 中写入并验证完整批次，失败回滚。
文件后端的多目录发布不承诺掉电级的整批原子性；各会话目录本身以完整 staging 单元发布，不能把它描述为
跨文件数据库事务。目录/数据库布局知识留在对应 adapter 中。

资源是会话拥有的独立文件，在元信息提交前发布并校验。失败可能留下无引用文件，但不提交缺少内容的引用。
SQLite 替换把已有资源目录移到备份，事务回滚恢复原目录；文件与数据库不具备掉电级联合事务。
原路径引用不打包外部文件，导入返回外部文件依赖说明；实际请求中的文本和图片保留，新请求解析目标会话附件地址。

## 验收证据

- `tests/application/test_data_transfer.py`：配置隔离、替换冲突、会话双后端往返、指定/全量、父子树、附件、
  无运行副作用、未知依赖、损坏检查点、路径/版本/类型错误及空数据。
- `tests/cli/test_data_commands.py`、`tests/cli/test_session_commands.py`：统一 CLI、JSON/文本输出及选择参数。
- `tests/tui/test_data_transfer.py`：Windows 路径、命令解析、标准菜单表单及应用服务接线。
- `tests/architecture/test_boundaries.py`：新增应用、基础设施与共享 Host 适配的明确依赖边。
