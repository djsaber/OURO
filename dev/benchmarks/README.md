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
