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
