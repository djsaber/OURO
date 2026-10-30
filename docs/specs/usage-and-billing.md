# OURO 用量与计费规格

> 文档类型：现行规格
> 主要实现：`core/billing.py`、`core/usage_queries.py`、`application/usage.py`、`billing/`、CLI/TUI 用量呈现

## 事实与责任边界

`Usage` 承载供应商返回的 token 与可确认的 USD 估算金额，上下文估算不进入账本。input 包含 cached input，缓存读写只提供细分，不重复加入 total；reasoning token 不重复计入 output。普通回合与压缩请求在供应商响应完成后经同一 UsageService 记账，后续摘要校验失败不撤销已发生的用量。

每条 ProviderUsageEntry 记录请求身份、provider、endpoint、model、工作空间、用途及带时区的发生时间。`agent` 和 `compaction` 用量必须关联真实 Session 与 Run；`plugin` 用量必须关联插件 ID，Run 身份为空，Session 与工作空间仅在调用来源存在时保存。独立请求不创建会话或 Run。历史用量查询只依赖账本，不加载 Session、Run 或当前配置。Session 归档、撤销对话或删除会话不改变账单。

计费模式仅表示服务连接的声明，取值为 unknown、usage_based、subscription。组合根的 ProviderRequestPolicy 经 adapter factory 固定到当次 ModelMetadata，UsageService 将其写入 metadata.billing_mode。不得从登录方式、账户身份或模型名称推断计费方式。进行中的请求保留其 adapter 声明，下一请求使用重新解析的 adapter。

`core/billing.py` 定义事实、筛选与审计事件，`core/usage_queries.py` 定义共享查询值。UsageService 负责请求归属、身份生成及人工修订用例；UsageLedger 提供存储无关的查询和事务边界；本地实现负责数据库、索引和查询执行。CLI/TUI 不实现独立的统计算法。

## 存储与一致性

本地账本位于 `OURO_HOME/billing/usage.sqlite3`，使用 SQLite WAL，与 Session 后端独立。数据库文件不存在时自动初始化，目录中的其他文件不参与初始化判断。数据库 user_version 为 5，请求记录和审计事件的 schema_version 为 2。只接受当前格式，不自动迁移或删除不支持的数据。

首次建库通过同目录 `usage.init.lock` 协调跨实例与跨进程初始化，锁等待上限为 5 秒。获得锁后重新检查 schema，
再设置 WAL 并在事务内创建表，避免多个空库读取者同时切换日志模式。现有数据库只验证 schema，不获取初始化
文件锁；常规读写继续由 SQLite 事务协调。WAL 是持久数据库属性，见 [SQLite WAL 说明](https://www.sqlite.org/wal.html#persistence_of_wal_mode)。

- entries 保存有效记录的类型化维度及数值投影；entry_records 保存当前账单详情。数值扫描不读取 JSON 明细。
- events 按数据库提交序号追加 record、replace、reset 事件。原始供应商事实保存在首次 record 事件，人工修订不会覆盖它。
- raw_requests 以 entry_id 定向引用首次 record 审计，不保存另一份原始正文，也不依附于可删除的 entries。
- raw_run_totals 按 Session/Run 保存原始请求数、已报告的 agent/compaction 请求数、token、已知金额及计价覆盖范围。首次接受原始身份时与审计、账单、scope
  在同一事务更新；原始身份重复时不累加，账单 replace/reset 不修改这一原始消耗投影。独立 plugin 请求不创建 Run 汇总。
- scopes 保存请求与原生预算 owner/scope 的去重关联。预算读取按 Session 索引聚合有效 token，修订与 reset 即时生效。
- 每次写入在同一事务内更新有效值、关联与审计。失败全部回滚；读事务观察一致快照，列表计数与当前页来自同一快照。

请求身份由所属 Run 或插件、request_id、用途和模型路由确定。相同请求重复提交必须与首次事实一致，接收时钟差异不参与幂等判定；即使有效数值已人工修订，也仍校验原始数值、工作空间及计费归属。record 的返回值始终是原始事实；当前修订账单由 get/list 查询。reset 后原始定位仍存在，重复报告不会重新显示已隐藏的账单或恢复其 scope 关联。修订使用递增 revision 防止覆盖并发修改，只能修改 token 和金额，必须保留原因。reset 按有效记录执行筛选，返回同一事务内计算的受影响总量，保留完整审计。

get_raw(entry_id) 点读原始请求；run_usage(session_id, run_id) 点读原始消耗汇总，不读取请求或审计正文、不扫描累计请求。
session_usage(session_id) 只汇总该 Session 的 raw_run_totals 数值行，不读取 Run、请求或审计正文；
用于 AgentTree 的 child 多 Run 用量，成本随该 Session 的 Run 汇总行数增长，不随每个 Run 的请求数增长。
没有接受过记录时返回 None，区别于已收到真实零用量报告。RunUsageTotals 的计价覆盖字段区分已知金额与未知金额，
agent_request_count 与 compaction_request_count 分别计普通模型和摘要报告；两类报告不能互相补齐缺失身份。

## Run 消耗与宿主边界

RunRecord 不保存用量数值或累计请求 ID。Runtime 的 RunResult、终态控制视图、暂停通知和 Agent 用量视图按需读取
账本原始汇总；RunResult.usage 为本次返回时的有限派生值，没有报告或没有计量端口时为 None。控制接口的 usage
映射为空表示不可用，不表示已报告为零。金额只投影已确认的部分，没有金额报告时不生成 estimated_cost。

模型完成回调先记录可信原始用量，再更新原请求的小头；记账不持有 Session mutation，也不写 Run。
普通请求使用其已绑定的 Run、模型和工作空间归属，不在报告到达时重读当前 Session 来决定归属。
内置摘要在策略开始前通过短期 Session 读取捕获 Run 和工作空间，分块、缩块重试及模型路由回退共用该归属；
每次模型调用前原子保存自身 request_id、实际模型路由、started_at、Run 当前请求身份和摘要派发计数。Run control usage scope 来自捕获的 Run，
报告时不重读 Session/Run 或重写 Run。归属读取或账本写入失败为 terminal，不继续切换模型或策略产生新消耗。
账本失败时不得完成请求小头；小头提交失败不撤销已持久化用量。原始请求重复报告仍只接受一次。

普通、摘要及独立插件请求在 Adapter reader 实际收到首个 Provider done 时执行计量回调，再将 done 交给输出 FIFO。
该回调不发布响应内容；内容消费取消、展示失败或摘要未采用不撤销已经收到并持久化的报告。
写入途中取消等待已开始的账本操作完成，Session 尚未收尾的情况仍走显式恢复；尚未实际收到报告时保持未知。

## 请求结算与恢复

普通和内置摘要请求在账本确认后将 usage_entry_id 随 Provider done 状态写入原请求头；没有报告或没有计量端口时
引用为空。流在 done 之前中断时记录本地中断，不伪造用量。头的状态与引用约束见
[会话持久化](session-persistence.md#canonical-数据与投影)。

显式 Session 恢复从 pending Run 与最近 Run 的 current_request_id 定向读取普通或摘要请求头及其 epoch，遇到活动执行 owner 时不结算。释放 Session mutation 后，
按该请求身份点读原始账本，并验证 Session、Run、turn、用途及模型路由；再次进入 mutation 后重新检查请求头、
epoch 和执行 owner，只有原请求仍有效才写入中断状态及已确认计量引用。全过程不读请求正文，不重放 Provider、
工具或插件，不用计量事实推断已提交响应。任何存储失败保留可再次收尾的请求，账单 correction/reset 不影响原始定位。
恢复之后才到达的报告仍归原始账本；再次显式恢复可以给原中断状态补齐引用，保留当时的中断时间和语义。

SessionService 的上下文状态、完整输入与观察查询只投影已保存的头；有计量引用时按身份读取原始数值，不执行恢复。
尚未结算但账本已有报告时，查询不会自行回填。没有计量端口或所用账本不包含该记录时，数值为未知，引用和已保存
请求输入仍可查看；仅转存 Session 不会隐式复制全局账本。返回了不属于该请求的账本事实则明确拒绝。
SessionContextStatus.completed_at 表示头中的 Provider done 时间，interrupted_at 表示本地中断；输入 token 数
是账本的返回时投影，不是请求记录中的另一份权威。

## 消耗预算

Run token 预算读取原始汇总，账单 correction/reset 不能补回消耗。model_round_count 和 summary_request_count
分别与账本 agent_request_count、compaction_request_count 比较，任一用途缺少报告时用量仍不完整；
配置 token 预算的下一请求以 usage_unavailable 暂停，即使此前已有用量或另一用途存在报告。已收到的真实零报告属于已知值。轮数和时间预算不依赖计量端口。
无计量端口的嵌入宿主可以运行，但不提供持久用量、用量汇总或 token 消耗预算保证；配置 token 预算时明确拒绝，
不在 Run 中隐式执行另一套累加。共享预算及新派遣约束见[子智能体规格](subagents.md#并发深度与预算)。

## 查询契约

UsageFilter 支持带时区的 occurred_from（含）与 occurred_to（不含），以及 provider_ids、endpoint_ids、model_ids、workspaces、session_ids、run_ids、plugin_ids、purposes、billing_modes。空集合表示不限；同维度多个值取并集，不同维度取交集。endpoint_ids、workspaces、session_ids、run_ids 和 plugin_ids 可包含 None，选择未归属记录。字符串归属按保存值精确匹配。

summary 支持 date、provider、endpoint、model、workspace、session、run、plugin、purpose、billing_mode 的有序复合分组。date 使用 UTC 日期。group_by 是维度元组，group.key 按同一顺序提供值；未归属值保留 None，不使用可能与真实值冲突的占位字符串。分组键升序排列，None 在字符串之前；总量由同一分组结果合并，不再次扫描明细。

详情使用 `(occurred_at, entry_id)` 降序游标，审计使用单调递增提交序号的降序游标。limit 范围为 1–10000，next_cursor 为 None 表示已读完。total_matching / total_events 表示本次快照的全部匹配数量，truncated 表示该页之后还有记录。后续页是新的读取快照；游标避免新记录插入导致已有页整体位移，不承诺跨页冻结正在发生的修改或 reset。

全局和日期账单汇总读取覆盖数值的索引；维度、时间、Session、Run 局部查询使用相应索引。写入事务通过有界 `PRAGMA optimize` 维护索引分布统计，组合筛选由数据库选择合适索引；统计维护遵循 [SQLite 查询规划建议](https://www.sqlite.org/lang_analyze.html#periodically_run_pragma_optimize)，不进入读取路径。数据库完成筛选、聚合、排序及分页；Python 只构造分组结果或当前页。全局账单汇总仍是线性数值扫描，局部查询的成本取决于索引候选记录数，复合分组的成本还取决于结果基数。可修订账单按需聚合；原始 Run 汇总仅是原始事实在同事务内维护的派生值，不是另一份可修订权威。不进行日志重放或跨进程缓存失效处理。

## 金额与扩展边界

estimated_cost 是可选的 USD 估算值，已知的 0.0 属于 priced 请求，None 属于 unpriced 请求。订阅不意味着单次金额为零；不摊分订阅费用，也不根据认证方式套用 API 单价。金额必须有限且非负。

UsageTotals 的 estimated_cost 仅累计已知金额，priced_request_count / unpriced_request_count 表示覆盖范围；billing_modes 是参与统计的模式去重排序结果。总量中的 0.0 必须结合覆盖范围解释。当前金额采用浮点估算语义，不代表供应商最终结算账单。

查询结果以基础计数作为唯一事实：total_tokens 由 input_tokens + output_tokens 得到，unpriced_request_count
由 request_count - priced_request_count 得到。分页的 truncated 由 next_cursor 是否存在决定；操作结果的
affected_count 读取 affected_totals.request_count。这些只读派生值参与 JSON 输出，不接受独立赋值，也不触发额外查询。

供应商归一化、用量事实、金额修订与查询执行各有独立边界。新增计价能力应消费稳定的请求事实，明确金额来源和计价依据，经审计事务发布有效结果；不得改写原始供应商响应、把网络查询放入统计读取路径，或将未知金额补成零。当前不包含价格同步、远端额度查询、多币种结算或订阅分摊服务。

## CLI 与 TUI

CLI 的 `--provider`、`--endpoint`、`--model`、`--workspace`、`--session`、`--run`、`--purpose`、`--billing-mode` 可重复使用并组合。工作空间参数解析为绝对路径；日期参数使用 UTC，裸 `--until` 日期包含当天，带时间的边界为不含。

```text
ouro usage summary --since 2026-09-01 --until 2026-09-30 --workspace . --provider <provider_id> --group-by date --group-by model
ouro usage summary --group-by billing_mode --json
ouro usage list --session SESSION_ID --limit 50 --json
ouro usage list --before '{"occurred_at":"2026-09-24T00:00:00Z","entry_id":"ENTRY_ID"}' --json
ouro usage audit --before 100 --limit 50 --json
```

list 和 audit 返回的 next_cursor 用于下一次 `--before`；保持筛选条件不变。CLI 显示未知、部分金额及计费来源。TUI 欢迎页通过同一查询契约异步读取全局总量和日期分组，只展示请求数、输入/输出 token 和缓存命中率，不显示费用。

## 验证范围

- `tests/billing/test_local.py`：组合筛选、UTC 边界、空归属、复合分组、游标、事务回滚、并发幂等、读写快照、修订与 scope 一致性、版本校验及聚合不解码明细。
- `tests/billing/test_raw_usage.py`：reset 后原始身份保留、原始 Run 汇总、原生提交故障、跨进程竞争及定向查询成本。
- `tests/application/test_usage.py`、`test_billing_attribution.py`、`test_harness_usage.py`：记账用例、请求归属、压缩失败记账、人工修订与原生预算。
- `tests/application/test_run_usage_ownership.py`：两后端请求完成顺序、原生失败窗口、无 Session 锁记账、账单修正隔离、未知/零用量与宿主端口。
- `tests/application/test_compaction_usage_binding.py`：两后端固定摘要归属、分块/回退、无 Session 锁记账、账本原生失败、未采用与取消。
- `tests/core/test_model_execution.py`：报告接收与输出消费分离、取消/展示失败及首个终止事件边界。
- `tests/application/test_request_usage_settlement.py`：账本提交后的进程终止、Runtime 显式恢复、替换/撤销竞争、无正文收尾、原生恢复写入失败与账本引用查询。
- `tests/cli/test_usage_commands.py`：CLI 筛选、分组、分页、修订、重置与金额语义。
- `tests/tui/test_usage_presentation.py`、`test_background_navigation.py`：用量展示及后台会话完成后的欢迎页刷新。
