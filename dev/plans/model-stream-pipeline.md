# 模型流生产到消费改进计划

状态：已实施  
范围：Provider 传输、RuntimeAdapter、InnerAgentLoop 持久化与分发、TUI 实时消费  
约束：遵循 `docs/OURO_AGENT_编码准则_ZH.md`，优先修正责任层与不变量，不以超时、重试或扩大缓存掩盖问题。

## 1. 问题与根因

当前完整链路是：

```text
HTTP/SSE socket
  -> provider 解码
  -> RuntimeAdapter 按事件拉取
  -> InnerAgentLoop 按 delta 持久化
  -> RuntimeEventRecorder 非阻塞分发
  -> TUI 50 ms 合帧
  -> AssistantMarkdownStream / Rich / Textual
```

主线存在两个相互放大的设计缺陷：

1. `RuntimeAdapter` 每次只从 provider 拉取一个事件，并在下游处理完该事件后才继续读 socket。持久化和其他消费者的延迟因此会直接暂停网络读取。
2. TUI 的活动 Markdown 尾块在每一帧都重建完整源码、重新解析完整尾块并同步构造 Rich Markdown。长时间未闭合的块或长单行内容会产生累计 O(n²) 工作，而且这些 CPU 工作和 HTTP 客户端运行在同一个 asyncio 事件循环上。

`peer closed connection without sending complete message body (incomplete chunked read)` 是 HTTP 分块响应在终止块之前收到 EOF 的表象。OURO 无法从该异常本身证明远端为何断开，但当前链路确实破坏了流式客户端应持续读取网络的基本不变量，并会把 UI/持久化抖动放大为 socket 长时间不读。

此外，provider 的传输分片直接成为持久化、内存结果和 Host 分发的事件粒度，在积压时会造成不必要的 I/O、对象和调度放大。

## 2. 目标架构与不变量

```text
socket reader/pump
  -> 单一有界、无损、保序入口
  -> 积压时合并相邻同类 delta
  -> 语义事件消费与有序持久化
  -> 非阻塞 Host fan-out
  -> TUI 类型化合帧
  -> 完成块一次渲染 + 活动尾部增量纯文本渲染
```

必须保持以下不变量：

- 网络读取器不等待持久化或 UI；仅在唯一入口队列满时施加显式背压。
- 入口队列无损、FIFO；不得跨 `text/reasoning` 类型或 continuation、tool、retry、done、error 等结构边界合并。
- 没有积压时不等待额外定时器，因此不增加首 token 延迟。
- 持久化先于 Host 分发，运行结果和持久化日志保持同序。
- UI 永远不能反压模型；活动帧的常规工作量只与新增文本和至多一个未完成视觉行有关，完成 Markdown 块只渲染一次。
- 取消必须关闭 reader 和 provider 迭代器；异常必须原样越过队列边界，不能伪装为正常完成。
- 已输出内容后不做透明重试，避免重复文本和重复工具调用。

## 3. 最小实施范围

### 阶段 A：连续读取与自适应合并

- 将 `RuntimeAdapter` 的逐事件 `anext()` 改为常驻 reader task 和固定容量 FIFO。
- reader 独占 provider 迭代器，消费者独占队列；取消消费者时取消 reader，并在 `finally` 中关闭 provider 流。
- 消费者取到 delta 后，只检查队列中已经存在的后续事件；相邻同类文本/推理 delta 在字符上限内合并。遇到任一结构边界立即停止。
- 不增加定时器、后台线程、可配置队列体系或第二级缓冲。

### 阶段 B：有界 TUI 活动尾部

- `AssistantMarkdownStream` 以片段和完成行增量累积，不再执行 `source + text` 后全量重切行。
- 只有新增换行可能产生新顶层块且活动尾部仍在固定字符上限内时才重新检查 Markdown 块边界；超过上限的长未闭合块
  保持纯文本追加，结束时做一次最终检查。
- 完成块继续经过唯一 `assistant_markdown()` 定义渲染并冻结。
- 活动尾块改为逐行纯文本 strip 缓冲：完成视觉行只渲染一次，只重绘仍在增长的末行。块冻结时以剩余尾部重置活动缓冲。
- 不在线程中变更 Textual widget，也不实现自定义 CommonMark 增量解析器。

### 阶段 C：契约与验证

- 在核心运行时规范中记录队列、合并、取消和边界契约。
- 增加 reader 顺序、异常、队列满、消费者取消、结构边界和合并测试。
- 增加长单行、长未闭合块、完成块冻结、源码一致性和实时响应测试。
- 运行相关 core/provider/application/TUI 测试、静态检查和可重复微基准。

### 阶段 D：SQLite 流式落盘

- 将三类模型 delta 的分组耐久语义提升为文件与 SQLite 后端共享的事件分类，不在两个 adapter 中复制名单。
- SQLite 仍为每条 delta 建立独立、原子、可见的 WAL transaction，但使用 `synchronous=NORMAL` 消除逐分片 fsync。
- 下一非 delta 事件保持 `synchronous=FULL`，同步此前 WAL 前缀；外层 FULL mutation 中的嵌套 delta 不得降低事务等级。
- 不引入后台 write-behind、提交定时器、长期事务或进程内无限缓冲。

## 4. 明确不做

- 不把 `httpx` 的无限流式读取改成普通 read timeout。
- 不对已经产生输出的响应做中途续传或重放。
- 不仅扩大 TUI/网络队列，也不引入无限队列。
- 不把整个 Runtime 或 TUI 搬到独立线程。
- 不在本次改动中重写 Markdown 解析器、持久化后端或 Host 协议。
- 不把 OpenAI 兼容协议的显式终止校验与本次并发/渲染修复混在同一个变更中；该项单独跟进。

## 5. 验收标准

- 慢消费者期间，reader 能持续读至入口容量；队列满时有界等待，恢复后事件无丢失且严格保序。
- 相邻同类 delta 只在已有积压时合并；工具、continuation、retry、done 和 error 顺序与边界不变。
- 消费者提前退出不会遗留 reader task，provider 流被关闭。
- 32 KiB 长单行和长未闭合 Markdown 块的逐帧处理不再反复构造 Rich Markdown；活动渲染不随完整尾块重复解析和渲染。
- 最终冻结文本与 provider 原始文本逐字一致；最终 Markdown 外观继续使用共享渲染定义。
- 相关测试与 lint 全部通过，且没有新增中途重试、读超时或无界缓冲。

## 6. 实施记录

- [x] 完成三条候选分支与主线的流式链路审查
- [x] 定位拉取耦合、事件放大和 TUI O(n²) 主因
- [x] 实施 RuntimeAdapter reader/FIFO
- [x] 实施积压感知的相邻 delta 合并
- [x] 实施增量 Markdown 分块与活动纯文本尾部
- [x] 对齐 SQLite 与文件后端的模型 delta 分组耐久语义
- [x] 完成测试、基准、规范更新与最终审计

验证记录：核心、provider、application 与 Markdown 相关测试 126 项通过；新增 reader/背压/取消/合并及长流测试
通过；完整 TUI 文件 191 项中 190 项通过。唯一失败为既有 fork-compaction 测试对 `ContextMetrics` 的原始值做全量
相等断言，而当前 TUI 会按所选模型补齐 context window 与 output reserve；该路径与本计划修改的文件和流式链路无关。
SQLite 最终实现的同步等级、即时可见性、嵌套事务与 store contract 定向测试 34 项通过；sessions、迁移、恢复、运行时、
续接与后端选择广域回归 305 项通过。600 条 delta 加完成边界的旧/新路径对照将每轮 601 个 FULL transaction boundary
降为 1 个 FULL boundary 加 600 个 NORMAL atomic commit；本机临时 SSD 的连接开销占主导，因此不据此引入长期 writer
连接或新的 store 生命周期契约。
