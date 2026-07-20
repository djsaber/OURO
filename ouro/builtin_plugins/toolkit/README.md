以下是 **Glob**、**Grep**、**TodoList** 三个工具的原始描述文本及参数 schema：

---

## Glob

**描述：**

Find files by glob pattern, sorted by modification time (most recent first).

Powered by ripgrep. Respects `.gitignore`, `.ignore`, and `.rgignore` by default — set `include_ignored` to also match ignored files (e.g. build outputs, `node_modules`). Sensitive files (such as `.env`) are always filtered out. Matches are files only — directories themselves are never listed; to find a directory, glob for a file inside it (e.g. `**/fixtures/**`). Good patterns:

- `*.ts` — all files matching an extension, at any depth below the search root (a bare pattern without `/` matches recursively)
- `src/*.ts` — files directly inside `src/` (one level, not recursive)
- `src/**/*.ts` — recursive walk with a subdirectory anchor and extension
- `**/*.py` — recursive walk from the search root for an extension
- `*.{ts,tsx}` — brace expansion is supported
- `{src,test}/**/*.ts` — cartesian brace expansion is supported too

Results are capped at the first 100 matching paths. If a search would return more, a truncation marker is appended. Refine the pattern (extension, subdirectory) when 100 is not enough, or call again with a narrower anchor.

Large-directory caveat — avoid recursing into dependency / build output even with an anchor, especially when `include_ignored` is set: `node_modules/**/*.js`, `.venv/**/*.py`, `__pycache__/**`, `target/**` can produce thousands of results that truncate at the match cap and waste context. Prefer specific subpaths like `node_modules/react/src/**/*.js`.

Windows note: the `path` argument accepts both Windows paths (e.g. `C:\Users\foo`) and POSIX-style paths (e.g. `/c/Users/foo`). Matched paths are returned in Windows backslash form; convert them to forward slashes before using them in a Bash command.

**参数 schema：**

```json
{
  "additionalProperties": false,
  "properties": {
    "include_dirs": {
      "description": "Deprecated and ignored. Results are always files-only — directories are never listed. Accepted only so older calls that still pass this flag are not rejected by parameter validation.",
      "type": "boolean"
    },
    "include_ignored": {
      "description": "Also match files excluded by ignore files such as `.gitignore`, `.ignore`, and `.rgignore` (for example `node_modules` or build outputs). Sensitive files (such as `.env`) remain filtered out for safety. VCS metadata directories (`.git` and similar) are always skipped, even when this is true. Defaults to false.",
      "type": "boolean"
    },
    "path": {
      "description": "Directory to search. Accepts an absolute path, or a path relative to the current working directory. Defaults to the current working directory.",
      "type": "string"
    },
    "pattern": {
      "description": "Glob pattern to match files.",
      "type": "string"
    }
  },
  "required": ["pattern"],
  "type": "object"
}
```

---

## Grep

**描述：**

Search file contents using regular expressions (powered by ripgrep).

Use Grep when the task is to find unknown content or unknown file locations. Do not use shell `grep` or `rg` directly; this tool applies workspace path policy, output limits, and sensitive-file filtering. ALWAYS use Grep tool instead of running `grep` or `rg` from a shell — direct shell calls bypass workspace policy, output limits, and sensitive-file filtering.

Write patterns in ripgrep regex syntax, which differs from POSIX `grep` syntax. For example, braces are special, so escape them as `\{` to match a literal `{`.

Hidden files (dotfiles such as `.gitlab-ci.yml` or `.eslintrc.json`) are searched by default. To also search files excluded by `.gitignore` (such as `node_modules` or build outputs), set `include_ignored` to `true`. Sensitive files (such as `.env`) are always skipped for safety, even when `include_ignored` is `true`.

**参数 schema：**

```json
{
  "additionalProperties": false,
  "properties": {
    "-A": {
      "description": "Number of lines to show after each match. Applies only when `output_mode` is `content`.",
      "maximum": 9007199254740991,
      "minimum": 0,
      "type": "integer"
    },
    "-B": {
      "description": "Number of lines to show before each match. Applies only when `output_mode` is `content`.",
      "maximum": 9007199254740991,
      "minimum": 0,
      "type": "integer"
    },
    "-C": {
      "description": "Number of lines to show before and after each match. Applies only when `output_mode` is `content`; takes precedence over `-A` and `-B`.",
      "maximum": 9007199254740991,
      "minimum": 0,
      "type": "integer"
    },
    "-i": {
      "description": "Perform a case-insensitive search. Defaults to false.",
      "type": "boolean"
    },
    "-n": {
      "description": "Prefix each matching line with its line number. Applies only when `output_mode` is `content`. Defaults to true.",
      "type": "boolean"
    },
    "glob": {
      "description": "Optional glob filter for which files to search, e.g. `*.ts`. Matched against each file's full absolute path, so a path-anchored pattern like `src/**/*.ts` silently matches nothing — use a basename pattern (`*.ts`), or anchor with `**/` (`**/src/**/*.ts`). To scope the search to a directory, use `path` instead.",
      "type": "string"
    },
    "head_limit": {
      "description": "Limit output to the first N lines/entries after offset. Defaults to 250. Pass 0 for unlimited.",
      "maximum": 9007199254740991,
      "minimum": 0,
      "type": "integer"
    },
    "include_ignored": {
      "description": "Also search files excluded by ignore files such as `.gitignore`, `.ignore`, and `.rgignore` (for example `node_modules` or build outputs). Sensitive files (such as `.env`) remain filtered out for safety. VCS metadata directories (`.git` and similar) are always skipped, even when this is true. Defaults to false.",
      "type": "boolean"
    },
    "multiline": {
      "description": "Enable multiline matching, where the pattern can span line boundaries and `.` also matches newlines. Defaults to false.",
      "type": "boolean"
    },
    "offset": {
      "description": "Number of leading lines/entries to skip before applying `head_limit`. Use it together with `head_limit` to page through large result sets. Defaults to 0.",
      "maximum": 9007199254740991,
      "minimum": 0,
      "type": "integer"
    },
    "output_mode": {
      "description": "Shape of the result. `content` shows matching lines (honors `-A`, `-B`, `-C`, `-n`, and `head_limit`); `files_with_matches` shows only the paths of files that contain a match, most-recently-modified first (honors `head_limit`); `count_matches` shows per-file match counts as `path:count` lines, preceded by an aggregate total line. Defaults to `files_with_matches`.",
      "enum": ["content", "files_with_matches", "count_matches"],
      "type": "string"
    },
    "path": {
      "description": "File or directory to search. Accepts an absolute path, or a path relative to the current working directory. Omit to search the current working directory. Use Read instead when you already know a concrete file path and need its contents.",
      "type": "string"
    },
    "pattern": {
      "description": "Regular expression to search for.",
      "type": "string"
    },
    "type": {
      "description": "Optional ripgrep file type filter, such as ts or py. Prefer this over `glob` when filtering by language or file kind: it is more efficient and less error-prone than an equivalent glob pattern.",
      "type": "string"
    }
  },
  "required": ["pattern"],
  "type": "object"
}
```

---

## TodoList

**描述：**

```text
Use this tool to maintain a structured TODO list as you work through a multi-step task. Use it proactively and often when progress tracking helps the current work. This is especially useful in long-running investigations and implementation tasks with several tool calls; in plan mode, write the plan to the plan file rather than tracking it here.

When to use:

- Multi-step tasks that span several tool calls
- Tracking investigation progress across a large codebase search
- Planning a sequence of edits before making them
- After receiving new multi-step instructions, capture the requirements as todos
- Before starting a tracked task, mark exactly one item as `in_progress`
- Immediately after finishing a tracked task, mark it `done`; do not batch completions at the end

When NOT to use:

- Single-shot answers that complete in one or two tool calls
- Trivial requests where tracking adds no clarity
- Purely conversational or informational replies

Avoid churn:

- Do not re-call this tool when nothing meaningful has changed since the last call — update the list only after real progress.
- When unsure of the current state, call query mode first (omit `todos`) to check the list before deciding what to update.
- If no available tool can move any task forward, tell the user where you are stuck instead of repeatedly re-ordering the same todos.

How to use:

- Call with `todos: [...]` to replace the full list. Statuses: pending / in_progress / done.
- Call with no `todos` argument to retrieve the current list without changing it.
- Call with `todos: []` to clear the list.
- Keep titles short and actionable (e.g. "Read session-control.ts", "Add planMode flag to TurnManager").
- Update statuses as you make progress.
- When work is underway, keep exactly one task `in_progress`.
- Only mark a task `done` when it is fully accomplished.
- Never mark a task `done` if tests are failing, implementation is partial, unresolved errors remain, or required files/dependencies could not be found.
- If you encounter a blocker, keep the blocked task `in_progress` or add a new pending task describing what must be resolved.
```

**参数 schema：**

```json
{
  "additionalProperties": false,
  "properties": {
    "todos": {
      "description": "The updated todo list. Omit to read the current todo list without making changes. Pass an empty array to clear the list.",
      "items": {
        "additionalProperties": false,
        "properties": {
          "status": {
            "description": "Current status of the todo.",
            "enum": ["pending", "in_progress", "done"],
            "type": "string"
          },
          "title": {
            "description": "Short, actionable title for the todo.",
            "minLength": 1,
            "type": "string"
          }
        },
        "required": ["title", "status"],
        "type": "object"
      },
      "type": "array"
    }
  },
  "type": "object"
}
```

---

## 输出样例

### Glob 输出样例

输入参数：

```json
{"pattern": "**/*.py"}
```

原始输出：

```text
[Truncated at 100 matches — use a more specific pattern]
Only the first 100 matches are returned.
tests/providers/test_factory.py
tests/sessions/test_store_contract.py
tests/tools/test_builtin_file_tools.py
tests/tools/test_builtin_shell.py
tests/tools/test_executor_permissions.py
tests/tools/test_tools.py
tests/providers/test_anthropic_compatible.py
tests/plugins/test_plugin_system.py
tests/plugins/test_execution_contracts.py
tests/core/test_turn_policies.py
tests/mcp/test_manager.py
tests/plugins/test_builtin_plan_mode.py
tests/core/test_tool_policies.py
tests/cli/test_session_commands.py
tests/core/test_approval_modes.py
tests/core/test_context.py
tests/core/test_context_providers.py
tests/core/test_hooks.py
tests/core/test_inner_loop.py
tests/cli/test_mcp_commands.py
tests/cli/test_plan_confirmation.py
tests/cli/test_run_cli.py
tests/cli/test_session_approval_modes.py
tests/application/test_tool_call_batches.py
tests/cli/test_approval_commands.py
tests/cli/test_interactive_io.py
tests/application/test_runtime.py
tests/application/test_runtime_activity.py
tests/application/test_sessions.py
tests/application/test_session_approval_modes.py
tests/application/test_supervisor.py
tests/application/test_queued_inputs.py
tests/application/test_plugin_extensibility.py
tests/application/test_queued_input_modes.py
tests/application/test_local_application.py
tests/application/test_compaction.py
ouro/tools/registry.py
ouro/tools/executor.py
ouro/tools/builtin_files.py
ouro/tools/builtin_shell.py
ouro/tools/batch_executor.py
ouro/tools/builtin.py
ouro/tools/base.py
ouro/tools/__init__.py
ouro/sessions/local_files.py
ouro/sessions/sqlite.py
ouro/sdk/plugins/v1.py
ouro/sessions/base.py
ouro/providers/openai_compatible.py
ouro/providers/faux.py
ouro/providers/openai_codec.py
ouro/providers/anthropic_compatible.py
ouro/providers/anthropic_codec.py
ouro/plugins/worker.py
ouro/plugins/store.py
ouro/plugins/runtime.py
ouro/plugins/models.py
ouro/plugins/registry.py
ouro/plugins/manifest.py
ouro/plugins/generation_resources.py
ouro/plugins/installer.py
ouro/plugins/execution_contracts.py
ouro/plugins/contributions.py
ouro/plugins/dependencies.py
ouro/plugins/contribution_types.py
ouro/plugins/contracts.py
ouro/plugins/catalog.py
ouro/plugins/candidates.py
ouro/plugins/api.py
ouro/plugins/builtin.py
ouro/plugins/adapters.py
ouro/plugins/__init__.py
ouro/mcp/tool_adapter.py
ouro/mcp/manager.py
ouro/mcp/models.py
ouro/core/tools.py
ouro/core/tool_batches.py
ouro/core/tool_policies.py
ouro/core/turn_policies.py
ouro/local_application.py
ouro/mcp/configuration.py
ouro/core/state.py
ouro/core/session_controls.py
ouro/core/run_budget.py
ouro/core/messages.py
ouro/core/provider.py
ouro/core/inner_loop.py
ouro/core/hooks.py
ouro/core/context_snapshots.py
ouro/core/events.py
ouro/core/context.py
ouro/core/context_providers.py
ouro/core/approvals.py
ouro/core/adapter.py
ouro/core/__init__.py
ouro/cli/session_commands.py
ouro/config/queued_inputs.py
ouro/cli/plan_confirmation.py
ouro/cli/parser.py
ouro/cli/mcp_commands.py
```

---

### Grep 输出样例

输入参数：

```json
{"pattern": "^class ", "type": "py", "output_mode": "content", "head_limit": 10}
```

原始输出：

```text
ouro/builtin_plugins/plan_mode/state.py:21:class SessionPlanState:
ouro/builtin_plugins/plan_mode/state.py:80:class PlanStateRepository:
ouro/builtin_plugins/plan_mode/plan_mode_plugin.py:91:class PlanModePlugin:
ouro/builtin_plugins/plan_mode/interaction.py:22:class PlanOption:
ouro/builtin_plugins/plan_mode/interaction.py:43:class PlanConfirmationRequest:
ouro/builtin_plugins/plan_mode/interaction.py:106:class PlanConfirmationAnswer:
examples/plugins/inprocess-command/inprocess_command_plugin.py:6:class Plugin:
ouro/core/adapter.py:12:class RuntimeAdapter:
ouro/core/approvals.py:50:class ApprovalRecord(OuroModel):
ouro/core/base.py:20:class OuroModel(BaseModel):
Results truncated to 10 lines (total: 632). Use offset=10 to see more.
```

---

### TodoList 输出样例

#### 创建 TODO

输入参数：

```json
{
  "todos": [
    {"title": "任务 A - 新建待处理", "status": "pending"},
    {"title": "任务 B - 进行中", "status": "in_progress"},
    {"title": "任务 C - 已完成", "status": "done"}
  ]
}
```

原始输出：

```text
Todo list updated.
Current todo list:
  [pending] 任务 A - 新建待处理
  [in_progress] 任务 B - 进行中
  [done] 任务 C - 已完成

Ensure that you continue to use the todo list to track progress. Mark tasks done immediately after finishing them, and keep exactly one task in_progress when work is underway.
```

#### 查询当前列表

输入参数：`{}`（不传 `todos`）

原始输出：

```text
Current todo list:
  [pending] 任务 A - 新建待处理
  [in_progress] 任务 B - 进行中
  [done] 任务 C - 已完成
```

#### 更新状态

输入参数：

```json
{
  "todos": [
    {"title": "任务 A - 新建待处理", "status": "in_progress"},
    {"title": "任务 B - 进行中", "status": "done"},
    {"title": "任务 C - 已完成", "status": "pending"}
  ]
}
```

原始输出：

```text
Todo list updated.
Current todo list:
  [in_progress] 任务 A - 新建待处理
  [done] 任务 B - 进行中
  [pending] 任务 C - 已完成

Ensure that you continue to use the todo list to track progress. Mark tasks done immediately after finishing them, and keep exactly one task in_progress when work is underway.
```

#### 清空列表

输入参数：`{"todos": []}`

原始输出：

```text
Todo list cleared.
```

#### 清空后查询

输入参数：`{}`

原始输出：

```text
Todo list is empty.
```
