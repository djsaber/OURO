# Harness 状态与撤销：Windows / POSIX 性能对照

测量日期：2026-09-11；收尾复核：2026-09-12。M4 基线为 `7a241fa`，M5 业务代码为 `32b601d`，
两个 revision 使用随本报告提交的同一诊断脚本。本轮没有修改 OURO 业务代码。

## 方法与环境

- Windows 11 / NTFS；Linux 为临时 WSL2、Ubuntu 24.04.5、Linux 6.18.33.2，代码和测试数据均在 Linux ext4 内。
- 两平台均使用 CPython 3.14.6、SQLite 3.53.1 和仓库锁定依赖。每个平台内依次运行 M4、M5；不与 pytest 或 TUI 并行。
- 每个 revision 覆盖两个后端、100/10,000 条保留消息、固定两条撤回后缀、三种 owner 数据大小，各重复三次。每个平台每个 revision 36 次，合计 144 次。
- 消息后另写入不同状态，撤销时断言恢复消息前值。每阶段捕获一次完整状态，各 owner 并发执行 20 次只读或无变化回调。
- 夹具写入、首次索引建立、检查点计数在测量区间之外，数据描述热索引下的撤销成本。
- Windows 通过 [SQLite VFS 接口](https://www.sqlite.org/c3ref/vfs.html) 计数并原样转发 FlushFileBuffers；Linux 使用 strace 直接观察 fsync/fdatasync。Unix VFS 的同步调用不经过其可替换系统调用表，故不使用该表推断零同步。
- strace 附加、退出和 trace 文件读取在计时与 I/O 采样之外；SQLite 阶段耗时仍包含跟踪系统调用的开销。两平台和不同观测方式的绝对耗时不直接横比。
- 不调整 WAL/FULL、fsync、流式策略或缓存上限。操作系统进程 I/O 计数不等于物理介质读写量。

脚本与复现参数见 [benchmark README](../README.md)。原始数据：

- Windows：[M4](harness-m4-windows-2026-09-11.jsonl)、[M5](harness-m5-windows-2026-09-11.jsonl)。
- Linux：[M4](harness-m4-linux-2026-09-11.jsonl)、[M5](harness-m5-linux-2026-09-11.jsonl)。

## 撤销耗时

以下为三次重复的中位数，单位毫秒。短样本用于核对成本形状，不据此宣称稳定的百分比提速。

### Windows

| owner 数据 | 保留消息 | File M4 | File M5 | SQLite M4 | SQLite M5 |
|---|---:|---:|---:|---:|---:|
| 1 × 1 KiB | 100 | 32.496 | 39.550 | 15.079 | 13.298 |
| 1 × 1 KiB | 10,000 | 52.769 | 45.147 | 13.877 | 15.347 |
| 8 × 8 KiB | 100 | 35.916 | 50.546 | 12.992 | 13.584 |
| 8 × 8 KiB | 10,000 | 51.938 | 35.698 | 14.383 | 12.143 |
| 8 × 64 KiB | 100 | 58.763 | 51.382 | 16.010 | 15.246 |
| 8 × 64 KiB | 10,000 | 55.422 | 39.127 | 22.327 | 19.464 |

### Linux / ext4

| owner 数据 | 保留消息 | File M4 | File M5 | SQLite M4 | SQLite M5 |
|---|---:|---:|---:|---:|---:|
| 1 × 1 KiB | 100 | 27.683 | 25.721 | 52.767 | 50.453 |
| 1 × 1 KiB | 10,000 | 31.449 | 35.194 | 47.156 | 55.538 |
| 8 × 8 KiB | 100 | 35.879 | 27.315 | 39.634 | 52.251 |
| 8 × 8 KiB | 10,000 | 29.586 | 30.927 | 49.339 | 58.369 |
| 8 × 64 KiB | 100 | 39.430 | 35.417 | 75.865 | 75.389 |
| 8 × 64 KiB | 10,000 | 37.030 | 41.859 | 74.095 | 74.481 |

## 同步和版本成本

下列次数在全部状态大小、前缀长度和重复中一致，M4/M5 相同。计数覆盖完整 Store 操作及连接关闭。

| 后端 / 平台 | 状态提交同步 | 撤销同步 | 捕获 / 只读 / 无变化回调同步 |
|---|---:|---:|---:|
| File / Windows、Linux | 4 次 Python fsync | 5 次 Python fsync | 0 |
| SQLite / Windows | 4 次 FlushFileBuffers | 4 次 FlushFileBuffers | 0 |
| SQLite / Linux | 5 次 fsync | 5 次 fsync | 0 |

每次状态变更新增一个版本；撤销、只读及无变化回调均新增零个版本。所有只读及无变化回调均零写入，
没有新增 SQLite 同步或 Python fsync。状态提交写入随 H 增加，撤销复用已保存的状态版本。

## 读写量与测量边界

Windows File 的 M4/M5 读写量完全一致：三种状态大小对应的撤销读取约 77 KB、346 KB、2.18 MB。
保留前缀从 100 增至 10,000 条，每种只增加 10 字节读取；写入从 9,469 增至 9,485 字节。
Linux File 同样保持此量级，额外的少量读取差异包含 /proc/self/io 自身计数长度。

两平台 SQLite 撤销读取随状态大小增加，长前缀只增加少量数据库页访问；没有与保留消息正文成比例增长。
原始数据保留各次读写量及操作次数，不用均值掩盖页布局变化。

Linux SQLite 的状态捕获阶段有固定 8 字节进程写入，M4/M5 一致。
[单独系统调用追踪](harness-linux-read-trace-2026-09-12.txt) 证实是创建 sessions.db-shm 时的八次单字节页扩展，
没有数据库正文写入或同步调用。因此不能把“没有状态提交”写成“整个进程绝对零写入”。
状态捕获后，在已有快照上的只读和无变化 owner 回调才是零写入。

## 验收结果

144 次测量均验证准确状态回退、每次状态变更新增一个版本、撤销及回调零新版本，以及回调零写入/零同步。
原生同步次数没有因状态大小或保留前缀增加，也没有相对 M4 增加。结合索引定位和尾部裁剪实现，满足 PF-01/02 的本阶段成本要求。

POSIX 业务回归 257 项通过：存储 183、核心 Harness 调用 12、历史观察/事件跟随/撤销执行 62。
覆盖文件决定写入失败、部分提交恢复、迁移重入、历史截断、独立 reader 和跨后端生命周期。
原生同步探针在 Windows、Linux 各 2 项通过，验证实际持久提交、只读及测量异常后的清理。
在 Linux ext4 源码目录中使用锁定依赖执行：

```bash
python -m pytest tests/sessions tests/core/test_harness_calls.py \
  tests/application/test_history_observation.py \
  tests/application/test_session_event_following.py \
  tests/application/test_history_execution.py -q
python -m pytest tests/benchmarks/test_sqlite_sync.py -q
```

完整验收映射见[会话撤销与 Harness 规格](../../specs/OURO_会话撤销与Harness状态规格_2026-09-08.md#13-验收证据索引)。

这些证据完成 M5 性能与 POSIX 文件恢复验收；不代表物理掉电测试、所有 POSIX 系统或 M6 全仓库验收。
