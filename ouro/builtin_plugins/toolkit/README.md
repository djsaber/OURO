# Toolkit 内置插件

`dev.ouro.toolkit` 提供常用只读检索和 Session Todo 工具。它通过普通 builtin Plugin SDK factory 进入
Generation 与 Catalog，不在 Runtime 或 CLI 中注册旁路工具。插件默认启用，仍可通过标准 builtin 管理入口显式禁用。

## 工具

- `Glob`：按 glob pattern 枚举 workspace 内文件，应用忽略规则并返回稳定、有界的相对路径列表；
- `Grep`：在 workspace 内按文本或正则搜索，支持 glob、大小写和结果上限；
- `TodoList`：读取或替换当前 Session 的结构化 Todo 列表。

`Glob` 和 `Grep` 仅访问 `ToolExecutionContext` 授权的 workspace，拒绝路径逃逸；搜索结果按确定顺序返回并受
数量与输出预算约束。忽略规则由 `ignore.py` 统一解析，搜索实现位于 `search.py`。

Todo 是 canonical Session extension state，由插件的 Session state store 按 Session 隔离。列表使用版本化 envelope
和原子更新；每个条目包含标题和 `pending | in_progress | done` 状态。插件私有 data 目录不保存 Todo 状态。

## 源码入口

- `definition.py`：builtin definition 与 manifest；
- `toolkit_plugin.py`：工具贡献、schema 与 handler；
- `search.py`：有界文件枚举和文本搜索；
- `ignore.py`：忽略规则；
- `todos.py`：Todo 数据模型和状态转换。

精确工具描述、参数 Schema、默认值和错误文本以 `toolkit_plugin.py` 为单一事实源，README 不复制这些实现常量。

## 验证

```bash
uv run pytest tests/plugins/test_builtin_toolkit.py -q
```

路径限制、Session extension state 和输出上限由对应 core/application 契约测试共同约束。
