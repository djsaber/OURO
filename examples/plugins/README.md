# OURO plugin examples

These packages are focused examples and validate against the current development schema.

- `faux-provider`: a typed model protocol contribution, configured through ordinary Provider/Endpoint routes; shared in-process and worker handlers, native usage and request-scoped generation ownership.

- `declarative-skill`: no executable plugin code; contributes one Agent Skill.
- [`configuration-playground`](configuration-playground/README.md): no business logic; exercises scalar, nested, mapping, array, union, conditional and recursive configuration, typed choices and credential references through generic Host editors.
- `inprocess-command`: trusted Python in-process command contribution.
- `inprocess-observer`: non-blocking typed observation of a completed Run.
- `inprocess-prompt-policy`: fail-closed rejection before a user prompt is persisted.
- [`deepseek-search`](deepseek-search/README.md): read-only web Search and Fetch tools backed by the DeepSeek APIs, with configurable endpoint, credentials, retries, and result limits.
- `memory-service` and `memory-client`: SQLite-backed memory sharing through declared dependencies and scoped filters across two workers. Both peers run at a root concurrency limit of one.
- `worker-exec`: permission-bound Python execution through the Host process port, with ordered output callbacks.
- `review-service`: native model review and tool execution inside the caller's Run; the in-process and worker entries share the same service implementation.
- `model-review`: independent text generation through configured model routes, with plugin-attributed native usage and no synthetic Session or Run; both runtime entries share the command implementation.
- `dynamic-records`: whole-group tool discovery with replaceable record schemas, Session state proposals, and lease-driven worker handler release; the same SDK tool implementation also runs in process.
- `scheduled-sync`: owner-scoped persistent plans and native execution receipts; shared worker/in-process synchronization, preparation-time format checks, and an explicit stopped-data backup/restore command.
- `worker-tool`: protocol 3.0 stdio worker with one approval-gated Tool; its call count and last value are proposed through the call-scoped Session state, then committed with the result.

Validate any example before installing it:

```bash
uv run ouro plugin validate examples/plugins/declarative-skill --json
```

Installation never enables a plugin. Enabling explicitly accepts and runs the selected immutable
digest. Follow the [plugin development guide](../../docs/development/plugin-development.md) for the
complete local flow.

To exercise shared memory, install both packages and enable the client. Use
`/dev.ouro.examples.memory_client:remember OURO` and
`/dev.ouro.examples.memory_client:search OU` in an interactive Host. The client reads no provider files;
its dependency binds the service generation and its filter is removed after each query.

The `worker-exec` package requests `process.spawn`. Its
`/dev.ouro.examples.worker_exec:python print(6 * 7)` command executes supplied Python with the user's
OS permissions. Cancellation and plugin shutdown are managed by the Host process boundary.

`review-driver` 提供公开 SDK 的评审循环，使用原生模型请求、模型工具批次和等待 checkpoint；
同一算法可作为 Python 进程内插件或 worker 执行。

`delegating-driver` 使用公开 Agent 端口组合两个 `reader` 子任务，随后审阅结果并按模型决定执行工具或结束。
原生子任务等待和工具审批均通过 checkpoint 恢复；进程内和 worker 入口共享算法。
