# R1：Session / Harness 读取减负验收

> 日期：2026-09-13\
> 改前：`a57b14a`；改后：该提交上的本次工作区实现。\
> 状态：R1 已完成；验证包含全量运行及已修正测试探针的失败项复测。

## 范围与实现

本轮仅实施设计减负计划中的 R1。Session、当前 Harness、提交准备及 Application 阶段捕获共用成对读取，
一次取得匹配的 Session 和检查点。两个后端继续使用各自现有锁/事务；文件恢复独立核验持久检查点。
未修改数据格式、插件协议、权限、调度、并发、流式窗口、TUI 渲染或持久化同步策略。

私有存储读取返回供内部读取使用的匹配记录；单视图查询无需复制不可见的另一份 controls。
只有公开返回两个可变视图时才重建独立的 controls 投影。该投影按现有 schema 的 dump/validate 构造，
避免递归深拷贝每个 Pydantic 模型内部结构，同时自动覆盖嵌套字段。
不创建新数据模型、跨操作缓存或兼容分支。现行契约见
[会话持久化规格](../../../specs/OURO_会话持久化规格_2026-09-02.md)。

首个候选实现把深拷贝放在了所有 SQLite Session 读取上；多控制关系探针发现单记录读取退化，因而未采用。
该候选的结果以 `candidate1-` 前缀保留，仅用于说明被排除的实现，不混入最终 before/after 统计。

## 环境与方法

- Windows 11 build 26200；Python 3.14.6、SQLite 3.53.1、Pydantic 2.13.4、Textual 8.2.8、pytest 9.1.1。
- 使用仓库锁定依赖，`uv sync --frozen --all-groups` 确认环境；未升级依赖。
- 全部数据在临时目录合成，不调用网络 Provider，不操作个人会话。
- 基准顺序执行，不与 pytest 并发。改前复测使用 `git archive a57b14a` 的隔离源码；测量脚本取当前工作区同一份实现。
- Harness 生命周期沿用两种后端、100/10,000 条保留消息、固定 2 条撤回后缀、3 档 owner 数据，每组合 3 次。
  阶段捕获改为调用真实 `capture_harness`，改前和改后均使用该版本脚本。
- 控制投影读取使用 0/8/64 个控制关系，交替调用三种读取，每项预热 5 次、记录 200 次；原始样本保存在 JSONL。
- TUI 普通负载每后端各 6 次（初测与复测各 3 次），5 ms 合成存储延迟各 3 次；GC、动画和事件缓冲配置均保持原样。

主要复现入口：

```powershell
uv run --frozen python -m dev.benchmarks.harness_lifecycle --source C:/path/to/baseline --label before --repeat 3
uv run --frozen python -m dev.benchmarks.harness_lifecycle --label after --repeat 3
uv run --frozen python -m dev.benchmarks.session_harness_reads --source C:/path/to/baseline --label before
uv run --frozen python -m dev.benchmarks.session_harness_reads --label after
uv run --frozen python -m dev.benchmarks.session_pipeline --repeat 3
uv run --frozen python -m dev.benchmarks.session_pipeline --mode tui --store-delay-ms 5 --repeat 3
```

TUI 复测在同一脚本入口运行冻结的改前源码及当前源码。脚本细节与适用边界见
[基准说明](../../README.md)。

## 重复工作与持久化保障

下表是 Harness 检查点解码次数，不是物理磁盘操作数。原始计数见
[改前](reads-before.jsonl)与[改后](reads-after.jsonl)，最终实现另由回归用例直接约束。

| 操作 | files 改前 → 改后 | SQLite 改前 → 改后 |
|---|---|---|
| Session 单记录读取 | 1 → 1 | 1 → 1 |
| Application 状态捕获 | 2 → 1 | 2 → 1 |
| 当前 Harness 查询 | 2 → 1 | 2 → 1 |
| 无变化提交 | 3 → 2 | 2 → 1 |

文件提交保留的第二次读取用于完成/恢复阶段的持久结果验证。缺失检查点时恢复仍失败，未决决定仍保留，
修复检查点后可完成原决定并按原操作身份重试，不重新执行业务。

生命周期的每个对应场景均核对新版本数、Python fsync 和 SQLite 原生同步次数：状态变化新增一个版本，
撤销/读取/无变化回调不新增版本；同步次数保持一致。没有以减少落盘或检查换取读性能。

## 读取成本

控制投影基准每行、每种查询均为 200 次的中位数，单位 ms；这不是单次冷启动耗时或固定 CI 门槛。
原始样本见 [read-latency-before.jsonl](read-latency-before.jsonl) 和
[read-latency-after.jsonl](read-latency-after.jsonl)。

| 后端 | 控制关系数 | Session 单记录改前 → 改后 | 阶段捕获改前 → 改后 | 当前 Harness 改前 → 改后 |
|---|---:|---:|---:|---:|
| files | 0 | 0.661 → 0.633 | 1.117 → 0.818 | 0.924 → 0.645 |
| files | 8 | 0.919 → 0.735 | 1.497 → 0.961 | 1.273 → 0.747 |
| files | 64 | 2.338 → 1.204 | 3.449 → 1.710 | 3.175 → 1.230 |
| SQLite | 0 | 1.500 → 1.476 | 1.543 → 1.527 | 1.530 → 1.480 |
| SQLite | 8 | 1.586 → 1.586 | 1.689 → 1.679 | 1.658 → 1.587 |
| SQLite | 64 | 2.022 → 2.025 | 2.553 → 2.401 | 2.550 → 2.030 |

SQLite 单记录读取保持基线水平，公开双视图隔离没有再给单记录查询增加深拷贝负担。

生命周期基准中，保留 10,000 条消息、8 × 64 KiB owner 状态的阶段捕获，3 次中位数如下：

| 后端 | 耗时 ms 改前 → 改后 | 进程读取字节改前 → 改后 | 进程读取调用改前 → 改后 |
|---|---:|---:|---:|
| files | 3.747 → 1.822 | 1,055,811 → 528,384 | 7 → 5 |
| SQLite | 3.955 → 2.611 | 1,093,732 → 569,444 | 268 → 140 |

原始结果见 [harness-before.jsonl](harness-before.jsonl) 与 [harness-after.jsonl](harness-after.jsonl)。
进程 IO 计数包含文件缓存和数据库行为，不等同于介质 IO；SQLite 较小状态可能完全命中页缓存，
不能把解码减少等同于物理读取必然减少。含同步写入的提交/撤销总耗时仍受系统 IO 波动影响，不据此承诺全面加速。

## TUI 与测量边界

下表为各次运行 p95 的中位数，单位 ms，不是合并全部原始样本计算的总体 p95。

| 场景 | 后端 | loop lag 改前 → 改后 | Key 到 Composer 改前 → 改后 |
|---|---|---:|---:|
| 普通负载，6 次 | files | 12.617 → 11.311 | 11.090 → 11.634 |
| 普通负载，6 次 | SQLite | 10.096 → 11.304 | 8.044 → 9.721 |
| 注入 5 ms 存储延迟，3 次 | files | 11.253 → 12.668 | 13.320 → 12.057 |
| 注入 5 ms 存储延迟，3 次 | SQLite | 10.621 → 12.747 | 10.874 → 11.451 |

所有对应 Runtime/TUI 测量的 `store_on_loop` 均为空，业务运行正常完成。改前、改后的分布均有明显慢帧波动，
部分分位数变大，不能把这批小样本解释为 TUI 全面加速或所有终端条件下不退化的证明。
复测的分布存在重叠；本轮未改变交互调度或渲染，也未通过放宽窗口、限流或降频取得存储收益。
此前已知的 GC/慢帧和真实终端预算问题继续归原性能计划，不在本轮掩盖或宣称解决。

普通负载结果为 `pipeline-before/after.jsonl` 与 `pipeline-before/after-check.jsonl`；慢存储结果为
`pipeline-slow-before/after.jsonl`。这些是 Windows headless 验证，本轮未执行 POSIX 或真实终端 paint 测量。

## 功能与质量检查

- 改前定向基线：447 项通过，64.97 s；覆盖 architecture、core、全部 sessions，以及 Harness controls、undo 和异步 Session 管线。
- 回归先验证改前失败：10 个读取/提交计数场景中 8 项失败、2 项通过；失败对应重复解码或尚不存在的成对端口。
- 最终 Harness 提交专项：49 项通过，9.23 s；含新增读取成本、controls 隔离、损坏/缺失/外来检查点与文件恢复场景。
- 全量运行发现两项线程探针仍监听旧的 `load_session_record`，因此记录为空；已改为监听实际的
  `load_session_harness`，整个会话控制测试文件 12 项复测通过（4.74 s），事件循环外执行的断言继续保留。
  此修正仅更新测试观测入口，没有修改生产实现。
- Ruff 检查与格式、mypy（293 个源码文件）、compileall、sdist/wheel 构建已通过。
- 全量运行完整执行 2,713 项：2,707 通过、4 跳过、上述 2 项旧探针失败，耗时 1,747.10 s。
  随后 `pytest --lf --cov=ouro --cov-append` 复测这 2 项，全部通过（32.85 s，含覆盖率报告）。
  因生产实现未再变化，没有重复执行其他已通过用例；当前套件累计 2,709 项通过验证、4 项条件跳过，本轮无未解决失败。
- 累积总覆盖率（包含分支）84.41%，达到仓库 80% 门槛。全量与复测均保留原有资源警告处理和失败断言。

改前另一次全量测试运行约 11 分钟后主动中止，未取得完整结果，不记为通过。改后全量质量门槛保留，
不以定向测试或上轮审查结果代替。
