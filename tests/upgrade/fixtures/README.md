# Historical upgrade fixtures

Synthetic data persisted by the unmodified OURO code at `96ebb7e` (package 0.1.0),
using `LocalSessionStore` / `SQLiteSessionStore`, `LocalTaskRepository` /
`SQLiteTaskRepository`, and `LocalUsageLedger`. These are frozen writer outputs,
not target models with edited version numbers. They contain no user data.

`records/context-v3.json` is serialized by the unmodified `ContextSnapshotRecord`
at `aa94cc5`. Its synthetic inputs share the Session, Run and messages of the
file fixture, with `supports_vision=true`, an inline PNG and opaque context
metadata. Tests install this record in both file and SQLite homes, exercise
both vision flag values, and verify snapshot preservation and nested message
conversion. No user data is included.

`records/providers-vision.json` is serialized by `provider_config_to_raw` at
`aa94cc5`. It contains image-enabled and text-only endpoints, a fake inline
credential, a header declaration and opaque options/metadata. Tests cover
offline conversion, preservation, backup restoration and TUI recovery without
contacting a provider or resolving credentials.

`records/providers-reasoning-v030.json` and
`records/provider-templates-reasoning-v030.json` are synthetic outputs of the
unmodified `v0.3.0` (`54757d7`) provider codec, template store and binding writer.
They contain inherited, explicit and disabled reasoning defaults, embedded native
summary options, an unsupported endpoint, and opaque metadata with legacy-looking
keys. A template and an endpoint were edited after their baselines were recorded
to represent user customizations. Tests verify provider payload semantics,
preservation of customizations, rebasing of untouched bindings, atomic recovery
and idempotence. The old template file is outside offline upgrade scope; the
template reader backs it up and materializes the current catalog.
No credentials are resolved and no user data is included.

`records/output-budget.json` is serialized by the unmodified models at `af353d1`.
It contains synthetic ContextSnapshot 4, Compaction 1, model request and compaction
completion events, and a SessionCommitResult with embedded events. The request
window and output reserve are finite; metadata and commit output retain opaque
copies of the same metrics to verify ownership boundaries. Tests exercise file
and SQLite publication, interrupted-write restoration, event replay and idempotence.

Each supported source schema needs synthetic writer output and conversion tests.
Record steps declare source and target schemas independently of package versions;
unregistered intermediate schemas are not inferred. Future-schema and structural
composition tests use synthetic models and fields confined to tests. They verify
that extending the registered steps retains existing conversion paths, rejects
gaps or invalid transitions, and preserves source inputs and opaque content.

`files/` contains layout 9, Session 2, Run 2, Message 3, ContextSnapshot 4,
SessionCommitResult 1, HarnessVersion 1, file tasks 1 and usage SQLite 1.
`sqlite/` contains session schema 7, task schema 1 and usage schema 1 as SQL dumps;
the test loader restores these dumps to temporary databases. SQLite dumps include
application/user version pragmas and preserve sequence values.

The Session has three messages and a completed Run with a model tool batch.
The context and commit receipt retain copies of the messages. The disabled task
has one completed execution and its original occurrence marker. The ledger
records 99 tokens, resets them, then records 100 input / 7 output tokens and
corrects input to 123 at revision 2. Its effective cost is 0.125 USD. All four
audit events must remain, including the reset usage; only the corrected entry
contributes to current totals. Metadata, arguments and commit output carry an
opaque `schema_version: 999` value which must remain unchanged.
