# R2：Worker 返回值一次解码验收

> 日期：2026-09-13
>
> 改前：`2824842`（R1 已提交，工作区干净）；改后：该提交上的 R2 实现。
>
> 状态：R2 已完成，全量功能与质量检查通过。

## 范围与设计判断

本轮只处理 worker 返回值在同一响应边界内的重复解释，不调整贡献装配、插件资源所有权或 Host 工作流。
改动集中在 `ouro/plugins/worker.py`，没有变更 SDK、core、存储格式、协议版本或依赖。

原实现先校验返回字典，再由 handler 构造输出。Context、Command 和控制转换的校验已经构造了 SDK 对象，
但丢弃后又构造一次；Continuation 与 Policy 也重复检查相同分支。
现在每种方法由明确的解码函数同时完成校验和最终 SDK 输出构造，handler 直接交付结果。
Tool call/resume 共享同一结果解码函数。现有请求入口只增加一个有类型的解码参数，未引入 Wire DTO、
方法注册表、通用 RPC 框架或缓存；SDK 到 core 的转换继续承担层间边界职责。

方法输出必须完整解码后才经过原有 `PluginStateView` 验证并暂存 `session_state`。
回归测试确认，旧 continuation 路径在发现非法 role/tag 前已经暂存了状态；本轮将这部分校验前移，
并使其进入统一的 `worker_invalid_result` 诊断边界。原 Application 回调包装会在异常时关闭视图，
因此该测试证明的是调用内部存在部分暂存，不能据此声称旧实现已把非法状态持久化。

保留外部类型、默认值、互斥结果、数值/语义约束、owner/只读隔离、消息大小、超时、取消和进程退出行为。
SDK 批次构造原有的 `ValueError` 与状态边界的错误类型也保留，不用宽泛异常捕获掩盖失败来源。
状态提案成功仍不代表已提交，最终接受和持久化属于 Application。

## 测量方法

环境：Windows 11、Python 3.14.6；使用锁定依赖，同一机器和脚本顺序运行，未与 pytest 并行。
改前源码由 `git archive 2824842` 固定在忽略目录 `.pytest_cache/r2-baseline`，不依赖已修改的工作区。
脚本：[worker_results.py](../../worker_results.py)。命令：

```powershell
uv run --frozen python -u dev/benchmarks/worker_results.py --source .pytest_cache/r2-baseline --label 2824842
uv run --frozen python -u dev/benchmarks/worker_results.py --label r2
```

原始结果：[改前](worker-before.jsonl)、[改后](worker-after.jsonl)。
5 种方法 × 3 种规模 × 2 种 transport 共 30 场景；每场景预热 10 次，再采样 60 组，
每组 5 次调用。下表使用组内每次调用平均耗时的中位数。未禁用 GC、调整线程调度或限制输出。

- `decoded`：模拟 transport 直接返回已解析字典，测量真实 handler 及其返回值处理。
- `json`：同一调用另包含 JSON 解析，不包含进程启动、IPC 或插件执行。
- 构造计数、3 次 tracemalloc 峰值采样与计时分开；输出 SDK 对象的规范化摘要用于核对语义。
- Command 的 N 项表示 N 个独立输入、一个含 N 个输入的批次及 N 个控制转换；Compaction 使用 N 个用量字段。

## 结果

全部 30 场景的最终 SDK 输出摘要一致。片段、Session 输入、输入批次及控制转换的构造次数均减半。
例如 N=512 时，ContextFragment 为 1,024 → 512；Command 的 SessionInput 为 2,048 → 1,024、
SessionInputBatch 为 2 → 1、SessionControlTransition 为 1,024 → 512。

| 场景（N=512） | decoded 耗时 ms 改前 → 改后 | 含 JSON 解析耗时 ms 改前 → 改后 |
|---|---:|---:|
| Context | 2.119 → 1.003 | 2.327 → 1.185 |
| Command | 5.576 → 2.758 | 6.241 → 3.314 |
| Tool | 1.407 → 0.689 | 1.587 → 0.844 |
| Continuation | 1.408 → 0.689 | 1.582 → 0.836 |
| Compaction | 0.136 → 0.081 | 0.221 → 0.165 |

这批样本的所有场景耗时中位数均下降，幅度约 13%–53%。N=1 的 decoded 样本约为
Context 6.39 → 4.06 µs、Command 18.45 → 9.98 µs；没有为大响应收益增加小响应耗时。
Compaction 去掉校验后的再次数值过滤和中间字典，512 字段的 decoded 峰值为 33,144 → 20,680 字节。

构造次数下降不意味着峰值内存按比例下降：原实现的首批对象多数很快释放。
Context/Command 部分场景的 tracemalloc 峰值增加约 0.1–0.4 KiB，增加量维持在固定量级；
在 1、64、512 规模下未出现新增中间集合随输入量增长的峰值负担。该数据不能当作长期 RSS/泄漏测试。

上述结论仅针对 Host 响应处理。未测量真实 worker IPC、网络 Provider 或终端 paint，
不宣称 TUI 端到端加速，也不替代现有真实终端性能计划。协议与 TUI 调度代码没有变化。

## 功能与质量检查

- 改前定向基线：335 项通过，27.47 s；覆盖 architecture、core、worker、SDK 与执行契约等相关边界。
- 新增回归先在改前运行：53 项通过、7 项按预期失败；其中 5 项复现重复解码，2 项复现 continuation 非法输出先暂存状态。
- 改后定向回归：97 项通过、2 项条件跳过，25.95 s；覆盖新增输出契约、无效响应/状态、真实 worker 取消/超时及 Application 工具状态集成。
- 全量回归单次完整运行：2,769 项通过、4 项条件跳过，1,645.55 s（27 分 25 秒），无失败；包含新增 60 项测试。
- 总覆盖率（包含分支）84.53%，达到仓库 80% 门槛。保留原有 ResourceWarning 和未处理异常失败策略。
- Ruff 检查与格式、mypy（293 个源码文件）、compileall、sdist/wheel 构建和文档链接检查通过。

全量命令：

```powershell
uv sync --frozen --all-groups
uv run --frozen python -u -m pytest --cov=ouro --cov-report=term-missing --cov-report=json:.pytest_cache/r2-coverage.json --durations=15
```

新增测试覆盖全部 11 个调用 handler 的默认值和非对象响应；同时覆盖成对交付合法输出/状态，
非法 role/tag、bool 数值、互斥工具输出、畸形批次、错误状态提案及 owner 身份注入。
进程退出、超时、取消、只读、代次撤销和诊断脱敏继续使用已有真实进程与集成回归。

最终契约见[插件系统规格](../../../specs/OURO_插件系统规格_2026-09-02.md)。
