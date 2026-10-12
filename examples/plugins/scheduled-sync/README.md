# Scheduled Sync

This plugin owns hourly native Task plans and synchronizes a small local record set.
It uses only the public SDK. The worker runs with one business request slot; the
in-process entry uses the same commands and synchronization implementation.

Install and enable the package, then put this content in the plugin's private
`inbox.json` (`<home>/plugins/data/dev.ouro.examples.scheduled_sync/inbox.json`, creating
the directory if needed):

```json
{"schema_version": 1, "records": [{"id": "ouro", "text": "Local personal agent"}]}
```

Use `/dev.ouro.examples.scheduled_sync:sync-plan create` to create a plan. Each explicit
`create` creates a separate plan. Activation and restart do not create plans. Use
`list`, `pause <task_id>`, `resume <task_id>` and `delete <task_id>` to manage them;
updates carry the last observed revision and report conflicting edits.

Each execution validates the complete inbox, replaces `snapshot.json` atomically,
and returns the record count in native Task history. The snapshot records the exact
execution identity. A missing or invalid inbox fails that execution and preserves
the previous snapshot. The Host does not replay an occurrence with an unknown result;
after a command loses its reply, inspect the owned plan list before creating again.

Disabling the plugin revokes its calls and blocks future dispatch. Re-enabling a
plan paused by the scheduler is explicit. Plans, execution history and private data
remain available independently of the worker process. Compatible package updates
read the same schema; the example does not convert data formats.

Preparation validates an existing `snapshot.json` before publishing contributions.
Only schema 1 is supported; an unsupported or damaged snapshot blocks activation
without changing the file. A missing snapshot is valid for a fresh installation.

For maintenance, explicitly disable the plugin, including after a failed enable.
Wait for all Hosts to release its resources. Run `maintain.py` with an interpreter
that has OURO installed, using absolute paths for the interpreter, script and backups:

```text
ouro plugin disable dev.ouro.examples.scheduled_sync --home <home>
ouro plugin maintain dev.ouro.examples.scheduled_sync --home <home> --execute <python> <maintain.py> backup <snapshot-backup.json>
ouro plugin maintain dev.ouro.examples.scheduled_sync --home <home> --execute <python> <maintain.py> restore <snapshot-backup.json> --save-current <saved-current.json>
```

Backups live outside private plugin data, and existing backup files are never
overwritten. Backup saves raw bytes, including unsupported or damaged content.
Restore accepts only a valid current snapshot, saves the existing raw snapshot
before replacement, and atomically installs the validated bytes. Invalid input or
a failed backup leaves the current snapshot unchanged. `inbox.json`, plans and
execution history are outside this procedure's scope. Maintenance leaves the plugin
disabled; enable it explicitly after checking the result.

To run in process, set the manifest runtime to:

```json
{"kind": "python-inprocess", "entrypoint": "scheduled_sync_plugin:create_plugin"}
```
