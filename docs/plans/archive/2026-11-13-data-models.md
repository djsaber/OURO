# 数据模型与转换边界：设计与验收

> 文档类型：已完成工作记录
> 全量测试：维护者于 2026-11-13 确认已手动执行，全部通过

## 设计范围

| 边界 | 实现与约束 |
|---|---|
| 工具结果 | 执行器取得独立的类型化快照；内部观察与提案使用已有类型投影，共享执行器拥有的数据；两后端同步编码事实 |
| Provider 事件 | 原生属性验证构造核心事件；不透明 continuation 块取得独立副本 |
| Worker | 缓存 SDK 类型适配器；控制提案按 dataclass 字段构造并保留传输层 JSON payload，共享 SDK 到 core 边界负责领域校验；固定消息与提案值对象使用冻结的 slots dataclass |
| Worker RPC | Host 的 RPC 输出使用原生 JSON 值序列化；SDK 不引入 Host 模型或第三方运行时依赖 |
| Provider 配置 | 模型声明字段、默认值和归一化；codec 负责身份、端点集合及字段名称；配置文件负责 JSON 编码与原子发布 |
| 插件配置 | 原生有限 JSON 类型验证拥有数据复制；不可变修订拥有递归冻结与摘要 |

权威契约见[工具结果架构](../../architecture/tool-results.md)、[插件规格](../../specs/plugins.md)、
[Provider 配置](../../specs/provider-configuration.md)和[插件配置](../../specs/plugin-configuration.md)。
持久化版本、Call/Outcome 身份、事务、恢复、取消与更新请求的缺失/空值语义遵循各自领域契约。

## 定向回归

Windows、Python 3.14.6、Pydantic 2.13.4 下，以下选择运行结果为 **800 passed，68.65 秒**。

```powershell
$targets = @(
    'tests/architecture',
    'tests/core/test_models.py',
    'tests/core/test_tool_facts.py',
    'tests/core/test_tool_batch_transitions.py',
    'tests/core/test_tool_result_projection.py',
    'tests/sessions/test_tool_fact_storage.py',
    'tests/tools/test_executor_outcomes.py',
    'tests/tools/test_executor_permissions.py',
    'tests/application/test_late_tool_results.py',
    'tests/application/test_tool_call_batches.py',
    'tests/application/test_tool_result_content.py',
    'tests/application/test_runtime.py',
    'tests/application/test_run_drivers.py',
    'tests/application/test_session_recovery_commits.py',
    'tests/plugins/test_worker_results.py',
    'tests/plugins/test_worker_tool_sdk.py',
    'tests/plugins/test_worker_bidirectional.py',
    'tests/plugins/test_worker_home_services.py',
    'tests/plugins/test_worker_harness_calls.py',
    'tests/plugins/test_worker_harness_usage.py',
    'tests/plugins/test_worker_services.py',
    'tests/plugins/test_worker_tool_groups.py',
    'tests/plugins/test_providers.py',
    'tests/plugins/test_configuration.py',
    'tests/plugins/test_configuration_schema.py',
    'tests/plugins/test_configuration_snapshots.py',
    'tests/config/test_credentials_config.py',
    'tests/config/test_output_limits.py',
    'tests/config/test_provider_auth_config.py',
    'tests/config/test_provider_templates.py',
    'tests/config/test_provider_template_updates.py',
    'tests/config/test_reasoning_config.py',
    'tests/upgrade/test_providers.py'
)
uv run --frozen pytest -q -n 4 --dist worksteal @targets
```

`tests/application/test_runtime.py` 的类型断言整理后单独运行结果为 **76 passed，11.50 秒**。
原生编译校验器调用经 `tests/plugins/test_worker_results.py` 验证，结果为 **71 passed，1.08 秒**。
两组与上述选择重叠，不累计为独立用例数量。

回归覆盖回调返回值隔离、存储事实独立性、Provider continuation 隔离、候选配置嵌套隔离、有限 JSON 值、
嵌套 Worker 默认值、控制 payload 的值保真、无效输出结构不暂存 Session 状态、未知配置字段脱敏，
以及既有工具取消、恢复、权限和代次生命周期。文件与 SQLite 后端均验证恢复时保留观察事实、丢弃未发布控制提案。
Worker 解析错误使用统一的协议异常；配置验证错误保留字段定位，不依赖手写解析器的错误措辞。

已修改的 33 个 Python 文件通过 Ruff 检查、格式检查，以及 `mypy --follow-imports=silent --platform win32`。
此类型检查只覆盖所选文件，不等同于完整项目静态门禁。

## 局部计算成本

使用同一份 [Worker 基准](../../../dev/benchmarks/worker_results.py) 分别加载提交 `4ab5b849` 的独立源码目录
和当前工作区，按基线、工作区、工作区、基线顺序启动四个进程。每个进程、每个组合采样 100 次，
每次调用真实 Host handler 五次；表中为每调用耗时的中位数。分配峰值通过独立的 `tracemalloc` 调用测量，不混入计时。
7 类负载、3 种规模、2 种传输模式共 **42 个组合的输出摘要全部一致**。

PowerShell 当前实现复测命令：

```powershell
uv run --frozen python -m dev.benchmarks.worker_results --label current --samples 100
```

`decoded` 模式从已解析的 JSON 对象开始。复合命令含同规模的直接输入、批次输入和控制提案；
控制 payload 仅含一项提案，其 `items` 数组中的每项含整数与 64 字符文本。

| 负载 | 规模 | 基线 μs | 当前 μs | 耗时变化 | 基线峰值 B | 当前峰值 B |
|---|---:|---:|---:|---:|---:|---:|
| Context 片段 | 1 | 3.02 | 2.60 | -13.9% | 2,478 | 2,246 |
| 复合命令 | 1 | 7.86 | 7.88 | +0.3% | 2,222 | 2,990 |
| Session 输入 | 1 | 4.06 | 3.26 | -19.7% | 2,094 | 2,526 |
| 控制 payload | 1 | 3.14 | 3.10 | -1.3% | 1,400 | 1,552 |
| 工具与控制提案 | 1 | 3.94 | 3.62 | -8.1% | 1,592 | 1,768 |
| 续接与控制提案 | 1 | 3.82 | 3.32 | -13.1% | 1,912 | 1,840 |
| 压缩 usage 项 | 1 | 1.88 | 1.74 | -7.4% | 1,416 | 1,416 |
| Context 片段 | 512 | 763.49 | 579.64 | -24.1% | 70,624 | 47,270 |
| 复合命令 | 512 | 2,158.77 | 1,717.76 | -20.4% | 214,168 | 154,590 |
| Session 输入 | 512 | 853.14 | 623.83 | -26.9% | 74,720 | 51,638 |
| 控制 payload | 512 | 3.26 | 3.02 | -7.4% | 1,400 | 1,552 |
| 工具与控制提案 | 512 | 470.11 | 458.17 | -2.5% | 75,024 | 55,024 |
| 续接与控制提案 | 512 | 455.01 | 454.45 | -0.1% | 75,072 | 55,096 |
| 压缩 usage 项 | 512 | 58.80 | 61.24 | +4.1% | 20,520 | 20,448 |

`json` 模式包含 `json.loads`；512 规模结果如下。

| 负载 | 基线 μs | 当前 μs | 耗时变化 |
|---|---:|---:|---:|
| Context 片段 | 898.32 | 711.54 | -20.8% |
| 复合命令 | 2,622.20 | 2,200.80 | -16.1% |
| Session 输入 | 989.35 | 754.86 | -23.7% |
| 控制 payload | 121.63 | 121.41 | -0.2% |
| 工具与控制提案 | 587.36 | 597.80 | +1.8% |
| 续接与控制提案 | 576.51 | 597.18 | +3.6% |
| 压缩 usage 项 | 120.94 | 122.58 | +1.4% |

控制 payload 的解码耗时与分配峰值不随其正文规模增长；JSON 解析及共享领域边界仍承担实际数据大小的成本。
512 项复合命令的解码分配峰值下降 **27.8%**，但单项命令存在数百字节的固定分配增量。
部分组合存在 **1.4%–4.1%** 的耗时上浮，不能将结果表述为所有路径均更快。

独立进程首次导入 `ouro.plugins.worker.instance` 各采样五次，基线与工作区中位数分别为 **998.03 ms / 994.27 ms**。
该数字包含模块依赖导入，不代表完整应用冷启动。热路径微测不包含 IPC、文件 I/O、插件执行或终端呈现；
分配峰值不等于进程 RSS，也不替代端到端吞吐与 p95/p99 指标。

框架源码范围为 23 个文件，增加 206 行、删除 471 行，净减少 **265 行**；不新增依赖、配置格式或兼容分支。

## 验证边界

全量测试由维护者手动执行并确认全部通过。文中的用例数量、耗时、局部静态检查和性能数据来自所列定向验证。
本记录不包含完整覆盖率、发行构建、安装验证及跨平台验收的执行证据；全量功能测试通过不等同于全场景性能零回退。
