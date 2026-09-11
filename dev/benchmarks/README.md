# 会话与 TUI 响应性基准

`session_pipeline.py` 使用临时目录和合成会话，不访问网络模型、个人 OURO home 或真实会话内容。

```powershell
uv run --frozen python -m dev.benchmarks.session_pipeline --repeat 3
uv run --frozen python -m dev.benchmarks.session_pipeline --mode tui --store-delay-ms 5 --repeat 3
```

支持 `--mode runtime|tui|activity|all`、`--backend files|sqlite|all`。输出为 JSON Lines。
比较不同版本时，把相同脚本复制到各版本 checkout，使用同一 Python 环境，顺序运行，避免同时运行测试。

- 每个 Session 预置 120 条历史消息，每条约 840 字符。
- 两轮模型响应，每轮 40 个间隔 5 ms 的文本 delta，中间执行一次约 32 KiB 返回内容的只读工具。
- TUI 使用 Textual `run_test(size=(120, 40))`，打开已有 Session，再提交一次请求；测量覆盖打开、运行与末尾刷新。
- 1 ms ticker 测量事件循环调度延迟；运行期间每隔至少 30 ms 注入一个 Key，直到 Composer 文本改变。
- 记录 ActivityIndicator 实际 `_tick` 调用间隔，空闲状态清除间隔起点；这不是终端像素呈现时间。
- Store 调用计数包括公共方法的嵌套调用，表示主循环上命中的同步边界次数，不是物理磁盘操作数或事务数。
- 可选延迟在读写类 Store 方法上注入，嵌套方法也计入；用于暴露阻塞，不能等同于特定硬件的磁盘性能。
- `activity` 分项比较四个工具各 5,000 条合成结果的快照，其 sequence 改变但数据没有改变。
- 脚本临时包装 Store、history renderer、spinner、asyncio callback，并记录 GC 时长，退出时恢复。
  仅在 callback 超过 16 ms 时提取其名称，不在每个 tick 检查 frame locals，以免诊断改变对象分配。
  包装仅存在于独立诊断脚本中；正常 OURO 运行没有新增遥测开销。
- 不禁用 GC、不调整计时精度、不修改流式窗口或动画频率。分位数是每次运行的样本分布，短样本优先看最大值。

此基准适合发现同步阻塞与验证边界，不代表 Windows/POSIX 真实终端验收，也不覆盖真实 Provider 网络延迟、
大规模子树或万条消息的首屏成本。性能数值不作为 CI 的固定时间断言；CI 使用线程归属、顺序、取消及过时结果测试。

2026-09-05 的原始对照数据见 `results/2026-09-05/`，解释见
[本次审计报告](../../docs/OURO_会话与TUI性能审计_2026-09-05.md)。

## Harness 状态与撤销成本

```powershell
uv run --frozen python -m dev.benchmarks.harness_lifecycle --label m5 --repeat 3
uv run --frozen python -m dev.benchmarks.harness_lifecycle --label m4 --source C:/path/to/m4 --repeat 3
```

同一脚本、Python 环境依次运行两个 revision，不与 pytest 并行。每个后端覆盖 100/10,000 条保留消息、
固定 2 条撤回后缀、1 × 1 KiB / 8 × 8 KiB / 8 × 64 KiB owner 数据，各 3 次。先写入另一份状态，
再撤销并验证恢复原值；单独测量首次状态提交、整阶段状态读取、并发只读与无变化回调。
夹具创建、文件索引首次建立和检查点计数不计入耗时，因此不代表冷启动万条消息的首屏成本。

记录进程 I/O 调用与字节、Python fsync 调用和新增版本数。Windows 使用 GetProcessIoCounters，
Linux 使用 `/proc/self/io`；这些是操作系统进程计数，不能等同于绕过文件缓存的物理介质读写量。
SQLite VFS 在 C 层调用同步，不会经过 Python 包装器，原生同步次数标为 `null`；保持 WAL/FULL 原有策略。
测试脚本不改变业务代码、持久化强度、同步配置或缓存上限。

2026-09-11 的原始数据与解释见 [Windows 成本对照](results/harness-windows-2026-09-11.md)。
