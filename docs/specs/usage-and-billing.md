# OURO 用量与计费规格

> 文档类型：现行规格
> 主要实现：`core/billing.py`、`core/usage_queries.py`、`application/usage.py`、`billing/`、CLI/TUI 用量呈现

## 事实与责任边界

`Usage` 承载供应商返回的 token 与可确认的 USD 估算金额，上下文估算不进入账本。input 包含 cached input，缓存读写只提供细分，不重复加入 total；reasoning token 不重复计入 output。普通回合与压缩请求在供应商响应完成后经同一 UsageService 记账，后续摘要校验失败不撤销已发生的用量。

每条 ProviderUsageEntry 记录请求身份、provider、endpoint、model、工作空间、Session、Run、用途及带时区的发生时间。运行时从所属会话取得工作空间并固定到账单；未指定工作空间保存 None。历史用量查询只依赖账本，不加载 Session、Run 或当前配置。Session 归档、撤销对话或删除会话不改变账单。

计费模式仅表示服务连接的声明，取值为 unknown、usage_based、subscription。组合根的 ProviderRequestPolicy 经 adapter factory 固定到当次 ModelMetadata，UsageService 将其写入 metadata.billing_mode。不得从登录方式、账户身份或模型名称推断计费方式。进行中的请求保留其 adapter 声明，下一请求使用重新解析的 adapter。

`core/billing.py` 定义事实、筛选与审计事件，`core/usage_queries.py` 定义共享查询值。UsageService 负责请求归属、身份生成及人工修订用例；UsageLedger 提供存储无关的查询和事务边界；本地实现负责数据库、索引和查询执行。CLI/TUI 不实现独立的统计算法。

## 存储与一致性

本地账本位于 `OURO_HOME/billing/usage.sqlite3`，使用 SQLite WAL，与 Session 后端独立。数据库文件不存在时自动初始化，目录中的其他文件不参与初始化判断。数据库标识和 user_version 只接受当前版本，持久化记录校验 schema_version。

首次建库通过同目录 `usage.init.lock` 协调跨实例与跨进程初始化，锁等待上限为 5 秒。获得锁后重新检查 schema，
再设置 WAL 并在事务内创建表，避免多个空库读取者同时切换日志模式。现有数据库只验证 schema，不获取初始化
文件锁；常规读写继续由 SQLite 事务协调。WAL 是持久数据库属性，见 [SQLite WAL 说明](https://www.sqlite.org/wal.html#persistence_of_wal_mode)。

- entries 保存有效记录的类型化维度及数值投影；entry_records 保存详情及首次 record 审计引用。数值扫描不读取 JSON 明细。
- events 按数据库提交序号追加 record、replace、reset 事件。原始供应商事实保存在首次 record 事件，人工修订不会覆盖它。
- scopes 保存请求与原生预算 owner/scope 的去重关联。预算读取按 Session 索引聚合有效 token，修订与 reset 即时生效。
- 每次写入在同一事务内更新有效值、关联与审计。失败全部回滚；读事务观察一致快照，列表计数与当前页来自同一快照。

请求身份由 Run、request_id、用途和模型路由确定。相同请求重复提交必须与首次事实一致，接收时钟差异不参与幂等判定；即使有效数值已人工修订，也仍校验原始数值、工作空间及计费归属。修订使用递增 revision 防止覆盖并发修改，只能修改 token 和金额，必须保留原因。reset 按有效记录执行筛选，返回同一事务内计算的受影响总量，保留完整审计。

## 查询契约

UsageFilter 支持带时区的 occurred_from（含）与 occurred_to（不含），以及 provider_ids、endpoint_ids、model_ids、workspaces、session_ids、run_ids、purposes、billing_modes。空集合表示不限；同维度多个值取并集，不同维度取交集。endpoint_ids 和 workspaces 可包含 None，选择未归属记录。字符串归属按保存值精确匹配。

summary 支持 date、provider、endpoint、model、workspace、session、run、purpose、billing_mode 的有序复合分组。date 使用 UTC 日期。group_by 是维度元组，group.key 按同一顺序提供值；未归属值保留 None，不使用可能与真实值冲突的占位字符串。分组键升序排列，None 在字符串之前；总量由同一分组结果合并，不再次扫描明细。

详情使用 `(occurred_at, entry_id)` 降序游标，审计使用单调递增提交序号的降序游标。limit 范围为 1–10000，next_cursor 为 None 表示已读完。total_matching / total_events 表示本次快照的全部匹配数量，truncated 表示该页之后还有记录。后续页是新的读取快照；游标避免新记录插入导致已有页整体位移，不承诺跨页冻结正在发生的修改或 reset。

全局和日期汇总读取覆盖数值的索引；维度、时间、Session、Run 局部查询使用相应索引。写入事务通过有界 `PRAGMA optimize` 维护索引分布统计，组合筛选由数据库选择合适索引；统计维护遵循 [SQLite 查询规划建议](https://www.sqlite.org/lang_analyze.html#periodically_run_pragma_optimize)，不进入读取路径。数据库完成筛选、聚合、排序及分页；Python 只构造分组结果或当前页。全局汇总仍是线性数值扫描，局部查询的成本取决于索引候选记录数，复合分组的成本还取决于结果基数。账本不维护第二份汇总状态，不进行日志重放或跨进程缓存失效处理。

## 金额与扩展边界

estimated_cost 是可选的 USD 估算值，已知的 0.0 属于 priced 请求，None 属于 unpriced 请求。订阅不意味着单次金额为零；不摊分订阅费用，也不根据认证方式套用 API 单价。金额必须有限且非负。

UsageTotals 的 estimated_cost 仅累计已知金额，priced_request_count / unpriced_request_count 表示覆盖范围；billing_modes 是参与统计的模式去重排序结果。总量中的 0.0 必须结合覆盖范围解释。当前金额采用浮点估算语义，不代表供应商最终结算账单。

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
- `tests/application/test_usage.py`、`test_billing_attribution.py`、`test_harness_usage.py`：记账用例、请求归属、压缩失败记账、人工修订与原生预算。
- `tests/cli/test_usage_commands.py`：CLI 筛选、分组、分页、修订、重置与金额语义。
- `tests/tui/test_usage_presentation.py`、`test_background_navigation.py`：用量展示及后台会话完成后的欢迎页刷新。
