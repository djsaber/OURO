# 插件观察器与提示策略实施计划

状态：已完成（C1-C5 已实现并通过专项、全量与架构验证；全仓既有 formatter 基线见下）

## 目标

在 B1-B8 的不可变 Catalog、generation owner、类型化 contribution 和 durable execution contract 基线上，完成两项
底层改良：

1. 用诚实、非阻塞、无返回控制力的 `Observer` 取代当前观察型 `Hook`；
2. 新增独立、fail-closed 的 `PromptPolicy`，允许插件在用户提示持久化和 Run 创建前拒绝输入。

权威契约见[插件系统目标规格](../specs/plugin-system.md)，设计理由见
[插件观察器与语义扩展点设计](../../docs/OURO_插件观察器与语义扩展点设计_2026-07-29.md)。

## 当前事实基线

- `HookName` 只有五个事件；`HookResult` 只有 diagnostics；
- Registry 按 priority/canonical identity 串行 await callback，失败后按 Run 隔离；
- `on_event` 已经通过 `RuntimeEventRecorder` 的有界队列异步投递，其余四个 Hook 仍在语义路径同步等待；
- application 调用点只消费 Hook failures，不消费成功 `HookResult`；
- Context Provider、Tool Policy、Turn Policy、Continuation、Compaction 和 Session Controller 已拥有独立语义；
- Manifest schema 2、Plugin API 1.2 和 worker protocol 1.2 仍公开 `hooks`。

这些事实必须先由针对性测试固定，避免迁移时把“当前偶然行为”误认为目标契约。

## 固定架构决策

- `hooks` 破坏式替换为 `observers`，不保留 alias、双读或兼容 adapter；
- Observer handler 返回 `None`，diagnostics 只走已有归因/脱敏通道；
- Observer 不在 Agent 语义关键路径中被 await；
- 每个 registration 独立有界 mailbox，慢插件不与其他插件共享消费队列；
- Observer 是 at-most-once 进程内通知；可靠事实以 canonical event store 为准；
- Prompt Policy 只有 `allow | deny`，不修改提示；失败视为 deny；
- Prompt Policy 与 prospective Run 使用同一 Catalog lease；
- 任意模型/工具 middleware、durable observer broker 和动态 extension point 不在本计划内。

## 依赖与顺序

```text
C1 文档与事实基线
  -> C2 Observer 公共契约
  -> C3 非阻塞投递与生命周期
  -> C4 Prompt Policy
  -> C5 示例、文档与全量收口
```

C2 和 C3 必须先完成，避免在错误的 Hook 抽象上继续增加 Prompt 事件。C4 不依赖任意通用 middleware。

## C1：文档与事实基线

目标：在改代码前固定权威设计、迁移范围和可证伪验收条件。

- [x] 更新插件系统目标规格，区分 Observer、enrich、guard、strategy 和 lifecycle；
- [x] 新增 Observer/Prompt Policy 专项设计；
- [x] 将本计划加入执行计划索引和开发路线图；
- [x] 校正当前 Skills/Hook 规格中的实际排序、事件、返回值和同步/异步事实；
- [x] 为五个现有 Hook 生产者、Manifest/SDK/worker round-trip、失败隔离和 diagnostics 未消费行为补事实测试；
- [x] 建立旧名称全仓 inventory，作为 C2 删除门槛。

验收证据：文档链接检查、`git diff --check`，以及能在旧实现上通过的事实测试；C1 不改变运行行为。

## C2：Observer 公共契约

目标：一次性修正领域语言和公开协议，不保留双轨。

- [x] 新建 `ouro/core/observers.py`，定义 typed `Observation` 联合、registration、registry 和 failure；
- [x] 用 `observers` 替换 Catalog、prepared contribution、adapter 和 owner facet 中的 `hooks`；
- [x] SDK 引入 `ObserverContribution` 与 typed observation handler，删除 `HookContribution/HookCall/HookOutput`；
- [x] Manifest 将 `contributes.hooks` 替换为 `contributes.observers`，加入 `runtime_event.event_types` allowlist；
- [x] worker protocol 使用新的 observer call，删除 `hook.call`；
- [x] 提升 Manifest schema、Plugin API 和 worker protocol major version，并严格拒绝旧格式；
- [x] 同步 builtin、示例、scaffold、compatibility report、contract hash 和所有 fixtures；
- [x] 删除 `ouro/core/hooks.py` 及所有旧类型/事件名，不保留 re-export。

验收：

- 全仓除历史调研/迁移说明外不存在旧运行符号；
- Manifest/SDK/worker 对每种 Observation 完成正反向类型测试；
- `ObserverContribution` 的 identity、optional、active_when、gate、contract 和 Catalog lease 与其他 snapshot-bound 类型一致；
- 非法 payload、未知 event/filter、错误返回值在正确边界被拒绝。

## C3：非阻塞投递与生命周期

目标：让观察彻底退出 Agent 关键路径，并按 registration 隔离背压。

- [x] 在 application 层实现 generation-owned Observer dispatcher；Core registry 不创建 task 或持有 Host 生命周期；
- [x] 每个 registration 使用独立有界 FIFO mailbox/consumer，enqueue 不等待 handler；
- [x] observation 入队时保留精确 Catalog/generation lease，处理、丢弃和取消时恰好释放一次；
- [x] 将 context、tool、run 和 runtime event 的生产点全部迁移到 dispatcher；
- [x] queue full、timeout、exception、gate close 产生有界脱敏 diagnostic，并只隔离对应 observer/scope；
- [x] retire、disable、revoke、Run finish 和 Host shutdown 正确清理 isolation、mailbox、consumer 和 lease；
- [x] 删除旧同步 dispatch 和 `RuntimeEventRecorder` 的 Hook 专用单队列。

验收：

- 一个永不返回的 observer 不增加模型、工具、Session 或其他 observer 的完成延迟；
- 一个 observer queue 满不会导致其他 observer 丢事件，也不会泄漏 Catalog lease；
- 同 registration FIFO；不同 registration 不承诺顺序且互不阻塞；
- Run-local isolation 在 Run 结束后释放，当前公开的 Session-scoped observation 有确定 scope；
- shutdown/drain 在 deadline 内完成，无 pending task 或 consumer warning。

## C4：Prompt Policy

目标：增加最小、类型化的用户提示预检能力，不引入提示改写 middleware。

- [x] 定义 `PromptPolicyCall/Decision/Registration/Registry/Evaluation/Failure`；
- [x] 新增 Manifest、SDK、worker、ContributionTypeAdapter、Catalog 和 contract facet；
- [x] 在封闭 Permission Registry 增加 `sessions.prompt.inspect`，授权说明明确 full-trust 边界；
- [x] application 在 Host command routing 后获取 prospective Catalog lease并评估 policy；
- [x] allow 后把同一 lease 转交 Run；deny/failure 时关闭 lease且不创建 canonical message/Run；
- [x] 多 policy 稳定排序、deny 短路、timeout/异常/非法返回 fail-closed；
- [x] reason 和 diagnostic 有界脱敏，被拒绝提示正文不进入 event、日志或 state；
- [x] disable/revoke、catalog swap、并发提交与 worker cancellation 使用既有 generation gate 和 owner lifecycle。

验收：

- allow、deny、timeout、异常、非法返回和 permission 缺失均有 application 集成测试；
- policy 检查与创建的 Run 使用相同 execution contract；
- policy 运行期间 catalog swap 不使 Run 换代；
- deny 不产生 user message、Run、continuation 或可恢复残留；
- plugin command、approval answer 和 ToolInput resolution 不误入 Prompt Policy；
- in-process 与 worker 行为、错误和取消等价。

## C5：文档、示例与发布收口

- [x] 更新插件开发指南、项目概述、开发架构、README 和 scaffold 文案；
- [x] 提供一个只记录非敏感 runtime event 的 Observer 示例；
- [x] 提供一个拒绝明显 secret 模式的 Prompt Policy 示例，测试不包含真实 secret；
- [x] compatibility report 将社区 Hook 分类为 observer、可转换 policy 或 incompatible，不透明执行；
- [x] 更新插件系统规格状态和计划证据，不复制日期化测试数字作为永久事实；
- [x] 运行专项与全量质量门槛并记录当前可重复命令。

## 测试矩阵

| 维度 | 必需场景 |
|---|---|
| Contract | Manifest/SDK/worker 正反向、contract hash、旧格式拒绝 |
| Catalog | 新旧 Run 不同 generation、active_when、gate close、lease 精确释放 |
| Observer | FIFO、跨 observer 隔离、积压、timeout、exception、cancel、shutdown |
| Prompt Policy | allow、deny、fail-closed、same-catalog、无持久化、permission |
| Security | prompt/reason/payload 脱敏、worker 输出限制、未知字段拒绝 |
| Recovery | waiting Run 旧 contract 不受新 Observer 代际影响；Prompt deny 无恢复记录 |
| Architecture | Core 不依赖 plugin runtime；Host 不直接调 registry；无双执行路径 |

## 风险与控制

- **大范围重命名遗漏**：先建 inventory 和 contract round-trip 测试，C2 结束时用全仓搜索证明旧符号已删除。
- **异步观察泄漏 lease**：queue item 显式拥有 release callback，所有 success/drop/cancel 分支做 exactly-once 测试。
- **诊断递归**：Observer failure diagnostic 不再次通知产生它的同一 observer，沿用 `notify_observers=false` 边界。
- **Prompt 泄密**：失败事件不含原始 prompt；permission、worker payload 和 logger 使用同一 redactor。
- **Catalog 竞态**：先 acquire lease、后评估、allow 后转交同一 lease；禁止 policy 后回读 current catalog。
- **能力膨胀**：C1-C5 不增加 transform；新增类型必须另有真实 use case、设计和计划。

## 全局质量门槛

```bash
python -m pytest
python -m ruff check ouro tests examples
python -m ruff format --check ouro tests examples
python -m mypy ouro
python -m compileall -q ouro
git diff --check
```

还必须通过 dependency architecture tests、文档链接检查、敏感信息审查和 task/process/lease 释放检查。

当前实现的可重复证据为上述 pytest、Ruff lint、mypy、compileall 和 `git diff --check` 命令，以及针对变更 Python
文件的 `ruff format --check`。全仓 formatter 检查仍会报告本次范围外、未改动的既有文件；本计划没有为制造无关
格式 diff 而改写这些文件。当前变更的本地 Markdown 链接、旧运行符号 inventory 和常见 secret pattern 扫描均通过。

## 退出条件

- 旧 Hook 抽象已从当前运行契约删除；
- Observer 不阻塞 Agent，失败、积压和关闭行为可诊断且无资源泄漏；
- Prompt Policy 在同一 prospective Catalog 上 fail-closed，拒绝不产生 canonical 状态；
- Manifest、SDK、worker、Catalog、permission、contract、示例和文档一致；
- 没有 generic middleware、动态 extension point 或兼容双轨；
- 所有验收项有当前代码与测试证据，专项和全量质量门槛通过。
