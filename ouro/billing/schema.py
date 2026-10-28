"""Current usage ledger DDL shared by storage and offline data upgrades."""

from ouro.billing.queries import FILTER_COLUMNS

USAGE_SCHEMA = (
    """CREATE TABLE entries (
        entry_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, day TEXT NOT NULL,
        provider_id TEXT NOT NULL, endpoint_id TEXT, model_id TEXT NOT NULL, workspace TEXT,
        session_id TEXT, run_id TEXT,
        purpose TEXT NOT NULL, plugin_id TEXT, billing_mode TEXT NOT NULL,
        input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
        cache_read_tokens INTEGER NOT NULL, cache_write_tokens INTEGER NOT NULL, estimated_cost REAL
    )""",
    """CREATE TABLE entry_records (
        entry_id TEXT PRIMARY KEY REFERENCES entries(entry_id) ON DELETE CASCADE,
        payload TEXT NOT NULL
    )""",
    """CREATE TABLE scopes (
        entry_id TEXT NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
        owner_id TEXT NOT NULL, scope TEXT NOT NULL, PRIMARY KEY(entry_id, owner_id, scope)
    ) WITHOUT ROWID""",
    """CREATE TABLE events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL
    )""",
    """CREATE TABLE raw_requests (
        entry_id TEXT PRIMARY KEY,
        source_event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id)
    ) WITHOUT ROWID""",
    """CREATE TABLE raw_run_totals (
        session_id TEXT NOT NULL, run_id TEXT NOT NULL,
        request_count INTEGER NOT NULL,
        agent_request_count INTEGER NOT NULL,
        compaction_request_count INTEGER NOT NULL,
        input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
        cache_read_tokens INTEGER NOT NULL, cache_write_tokens INTEGER NOT NULL,
        estimated_cost REAL NOT NULL, priced_request_count INTEGER NOT NULL,
        has_unknown INTEGER NOT NULL, has_usage_based INTEGER NOT NULL,
        has_subscription INTEGER NOT NULL,
        PRIMARY KEY(session_id, run_id)
    ) WITHOUT ROWID""",
    "CREATE INDEX entries_time ON entries(occurred_at DESC, entry_id DESC)",
    # Full-ledger and daily aggregates read this narrow covering index without row lookups.
    "CREATE INDEX entries_totals ON entries(day, billing_mode, input_tokens, output_tokens, "
    "cache_read_tokens, cache_write_tokens, estimated_cost)",
    *(
        f"CREATE INDEX entries_{column} ON entries({column}, occurred_at DESC, entry_id DESC)"
        for column in FILTER_COLUMNS.values()
    ),
)
