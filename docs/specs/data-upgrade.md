# 数据升级

> 文档类型：现行契约

## 目标和责任边界

`ouro-upgrade` 在指定的、已经停写的 home 内，将明确支持的历史核心数据升级为本机 OURO 的当前格式。
数据保留原位置，Session 与任务后端保持原选择。工具随 `ouro-agent` 发行，可用
`python -m ouro_upgrade` 调用；操作说明见[数据升级指南](../guides/data-upgrade.md)。

支持范围由实际数据契约和固定历史夹具确定，不能仅凭应用版本判断。工具不启动 Runtime、插件、模型、
认证刷新或任务调度；不修复损坏、不推测执行结果、不保证旧 Run 能恢复执行，也不处理插件私有数据升级。
正常 OURO reader 继续只接受当前格式，历史转换集中在独立的 `ouro_upgrade` 包。

升级成功表示所选后端的受支持核心数据已完成转换和验证。回收站、维护备份、未选后端、配置、凭据、
插件包及插件私有文件保留原样，并在结果的 `preserved_outside_scope` 中列出；它们不属于升级可用性保证。
尤其不能直接将旧回收站内容恢复到在线数据中并假定它已升级。归档但仍保存在在线 Session 存储中的会话
按普通 Session 处理。

## 支持矩阵

固定历史写入基线为 `96ebb7e`，包版本 `0.1.0`；ContextSnapshot 3 夹具由 `aa94cc5` 的模型序列化。
夹具和来源说明位于
[tests/upgrade/fixtures](../../tests/upgrade/fixtures/README.md)。以下数据契约可组合升级，当前格式直接验证：

| 容器 / 记录 | 接受的源格式 | 当前格式与处理 |
|---|---|---|
| 文件 Session 容器 | layout 9 | 保持布局；必要时重建 `sessions/index.json` |
| SQLite Session 容器 | schema 7 | 更新在线表中的 JSON payload，重建受影响 catalog；保留行身份与序列 |
| Session / Event / Harness / Compaction / Delegation / Resource | 2 / 2 / 1 / 1 / 1 / 2 | 验证宿主结构并保留数据 |
| Run | 2、5 | 目标 5；源 2 绑定 `ouro.default/1`，implementation 为 null、dependencies 为空、checkpoint 为 null |
| Message | 3、4 | 目标 4；源 3 的工具结果补 model producer |
| ContextSnapshot | 3、4 | 目标由当前模型定义；源 3 的 supports_vision 转为 supports_image_input，音频、视频输入能力设为 false；转换 source_messages，为缺失来源的 injected tool result 补 model producer |
| SessionCommitResult | 1 | 转换 messages、Session、Event 及 truncation 中的 Message |
| 文件任务 / SQLite 任务 | 1、3 | 目标 3；迁移 agent target、owner、result 与 cancel 字段，保留历史链与防重标记 |
| 用量 SQLite / 用量记录 / 审计 | 1、2 | 目标 2；增加 plugin 归属，保留原 Run 归属，更新表结构及审计内嵌记录 |

Run 2 的工具批次和 invocation 同样补 model producer。任务的 description 成为 agent target 的 prompt，
workspace、model、reasoning、approval_mode、session_mode 移入该 target；execution 的 Session / Run
身份移入 agent target。源任务没有插件所有者，owner_id 明确为 null；result 为 null、cancel_requested 为 false。

用量记录增加 `plugin_id=null`。用量主表允许独立插件归属所需的空 Session / Run 列，但升级不制造插件用量。
当前有效记录与完整审计分别转换，不通过重放审计重新累计用量，因此 reset 和 replace 的历史语义保持不变。

未列出的历史布局、记录版本或中间版本明确拒绝。SQLite Session schema 1 不在支持范围。
选定后端中的未知原生字段、缺失的必要事实、重复 JSON 键、非有限数字和不可解析数据均在业务写回前拒绝。
不通过当前模型的默认值补造 ID、时间、revision 或执行状态。

ContextSnapshot 按源版本登记到下一版本的转换步骤，依次执行到当前模型的 schema_version，
再转换嵌套消息并验证目标结构。增加版本时需提供相邻版本转换和对应夹具；缺少任一步骤即拒绝升级。
步骤只修改明确列出的字段，保留快照内容、身份、时间、metrics、metadata 与原 context_digest。
文件和 SQLite 后端共用此转换，不在存储层或 TUI 中实现版本兼容。

## 转换不变量

- 转换基于原始 JSON，只修改显式规则指定的字段。当前模型用于严格校验，不能通过模型重新序列化丢掉源字段。
- metadata、工具参数、提供方 opaque blocks、提交 output、插件 payload 等开放内容按 JSON 值保留；
  不递归搜索其中的 `schema_version`，不加载插件解释其内容。
- 保留 Session / Run / Message / Task / Execution 身份、历史时间、revision、顺序、状态、金额、token 和日程。
  `request_digest`、`context_digest` 等历史摘要保持原值，不重算旧请求。
- 文件 Session 检查消息顺序和头、消息数量、Run 引用、Harness 锚点及资源引用；附件留在原位并校验大小与摘要。
  文件任务检查执行历史链、活动执行引用和按 scheduled_at 标识的防重文件。SQLite 检查完整性、外键及转换涉及的
  记录身份、查询投影；不提供额外的数据修复服务。
- 未变化的文件不改写。Event 字节未变，因此原事件偏移索引仍有效；变更涉及的 Session / Task catalog 从当前事实重建。
- 已是当前格式时不建立备份或报告，不再次改写业务文件。检查工作区和工具锁不属于业务数据。

## 原地升级与故障恢复

```mermaid
flowchart LR
    A[识别并校验] --> B[暂存转换产物]
    B --> C[备份修改集合]
    C --> D[持久化升级中标记]
    D --> E[写回原路径并复核]
    E --> F[保存报告并解除标记]
```

1. `--home` 必须是已有的普通目录。用户负责停止所有写入者，包括旧 OURO 和外部脚本。
   当前标准 CLI/TUI 在访问 home 的完整生命周期内持有 `.upgrade/lock` 的 OS 共享锁，维护操作使用同文件的
   独占锁，禁止同时访问。嵌入式应用、直接调用 Store、旧版程序和外部脚本仍受停写前提约束。
2. 检查所选数据域。遇到 Session `commit.json`、任务 `transaction.json`、Session 创建 / 导入 / 回收发布
   中间态时停止，要求使用原版本完成正常恢复后再升级。
3. 在受限 `.upgrade/<id>/scratch` 中生成并验证产物，修改集合的产物保存在 `new/`。
   JSONL 逐行处理，数据库按游标批次转换；不复制整个 home 或附件，不构建版本路径搜索或任务工作流引擎。
4. 为修改集合生成 `before/` 与 `manifest.json`。清单记录相对路径、原来是否存在、前后 SHA-256 和文件模式。
   清单以序号保存材料，避免备份目录重复嵌套长路径。写回前再次核对所有已读取文件，发生源数据变化则停止。
5. 备份与产物持久化后写入 `.upgrade/in-progress.json`，此后才可修改业务文件。
   标准 CLI、TUI 入口和本地应用装配在初始化前检查该标记，存在时禁止 Runtime 使用此 home；
   TUI 进入独立恢复界面。入口保护不依赖升级包。
6. 逐文件原子替换并复核摘要，保存 `.upgrade/receipts/<id>.json`，最后解除标记。
   成功保留备份和清单，删除本次暂存产物；是否清理备份由用户决定。

多个文件的替换不构成 home 级原子事务。标记前失败不改变业务数据；标记后失败保留标记和恢复材料，
该 home 必须先执行 `--restore`。恢复在任何业务写入前验证全部备份，然后恢复整个修改集合，删除原来不存在的
新增文件，复核后解除标记。恢复可以在中断后重复执行，不消耗原备份；不逐步骤续跑，也不从半升级数据重新规划。
恢复仅适用于尚有标记的未完成升级，不提供成功后又产生新写入的自动降级。

SQLite 原数据库从不由升级器打开。主库及存在的 WAL / journal 先复制到工作区，由 SQLite 在副本上恢复；
SHM 在副本上重建。目标主库经过 integrity / foreign-key 检查、checkpoint、关闭连接后才写回，原 WAL、SHM、
journal 与主库作为同一个修改集合备份和恢复。自定义 trigger / view 不在支持范围，不执行其副作用。

`.upgrade` 在写入数据前收紧为当前用户权限，Windows 使用 ACL；待处理路径拒绝符号链接、reparse point
及文件硬链接。业务替换文件同样使用受限权限，POSIX 保留原模式。文件 fsync，POSIX 同步目录；
不承诺超出操作系统和文件系统能力的断电原子性。诊断只包含位置、契约和错误类别，不输出原始记录或模型验证输入。

## 模块和接口

| 模块 | 职责 |
|---|---|
| `ouro_upgrade/cli.py` | 参数与结果输出；`--check` / `--restore` 互斥 |
| `engine.py` | 编排所选后端、报告、有限支持边界 |
| `records.py` | 无 I/O 的显式历史记录转换、目标模型验证 |
| `files.py` | 文件记录、JSONL、引用检查和派生索引 |
| `databases.py` | 私有 SQLite 副本的 schema / payload 转换 |
| `workspace.py` | 工具锁、路径检查、暂存、备份、写回、未完成升级恢复 |
| `progress.py` | 与 UI 无关的阶段和计数通知 |
| `reset.py` | 显式清空 home，维护中断标记与锁 |
| `ouro/tui/recovery.py` | 离线恢复界面、进度展示和危险确认 |

`upgrade(home, check=False, restore=False, session_backend=None, task_backend=None, progress=None)`
提供同等 Python 接口。可选 `progress` 回调同步接收 `UpgradeProgress(phase, completed, total)`；
计数只代表当前阶段，不推算整体百分比。引擎不依赖 Textual、Runtime 或具体调度器。
后端选择遵循 OURO 的命令行优先、环境变量次之、默认 files 的规则。
`--check` 对业务数据只读，但仍需要工作区写权限和暂存空间。结果状态为 `upgrade_required`、`current`、
`upgraded` 或 `restored`；失败返回非零。报告列出修改文件、所选后端、备份与清单位置及明确保留在范围外的内容。

## TUI 启动恢复

正常 reader 用 `UnsupportedDataVersionError` 区分版本不符与一般持久化失败。TUI 的启动和显式恢复会话过程中，
数据版本错误会结束当前 Host，待服务关闭、共享访问锁释放后，进入不创建 Runtime 的恢复 App。
恢复 App 在后台线程调用同一升级引擎，成功后启动新的 Host 并返回主页，保留原 home 和后端选择。
一次启动最多自动尝试一次升级；再次失败只显示恢复选项。普通配置错误不触发升级，正常启动不扫描全量历史数据。

恢复界面复用 `ChoiceScreen`、`ConfirmationScreen` 和共享菜单样式，采用同主题背景上的居中无边框布局，
不叠加遮罩。工作线程仅更新最新进度，UI 每 100 ms 合并读取，避免大量文件导致消息队列积压；
现有 loading 行直接使用当前主题的 `spinner_frames` 和 `spinner_interval_seconds`，不覆盖动画定义。
处理中禁止菜单退出，外部关闭时也等待线程结束后才释放生命周期。

升级失败保留原因、退出和初始化入口；有未完成升级时提供恢复备份并重试。初始化属于独立的破坏性操作，
不作为升级算法的一步，也不会自动执行。二次确认展示完整 home、删除范围和不可撤销提示，默认选择取消。
`reset_home(home, progress=None)` 取得相同独占锁，先写入 `{"operation":"reset"}` 标记，再清空 home，
最后解除标记；只保留 `.upgrade/lock` 以维持锁身份，正常 Host 负责创建新数据。初始化失败保留标记，
再次显式初始化可继续清理；该状态不能用升级备份恢复。初始化拒绝根目录、用户主目录、当前工作目录及其祖先，
拒绝 home 路径链接，不沿嵌套目录链接清理外部数据。

针对性验收覆盖历史 writer 夹具、当前 reader、TUI 启动、嵌套内容与摘要、幂等、未知输入拒绝、WAL、
写回中断、恢复中断与备份损坏，以及自动升级、进度响应、危险确认取消、清空与清空中断、正常 Host 与维护互斥。
支持范围按该证据维护；未运行插件、外部执行环境与跨平台实机行为不在夹具保证内。
