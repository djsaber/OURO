"""Historical SQLite layout and request projections produced by the usage 1/2 -> 5 step."""

import sqlite3
from contextlib import closing

from ouro.core.billing import ProviderUsageEntry
from ouro_upgrade.json_codec import loads
from ouro_upgrade.records import validate

_ENTRY_COLUMNS = """(
        entry_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, day TEXT NOT NULL,
        provider_id TEXT NOT NULL, endpoint_id TEXT, model_id TEXT NOT NULL, workspace TEXT,
        session_id TEXT, run_id TEXT,
        purpose TEXT NOT NULL, plugin_id TEXT, billing_mode TEXT NOT NULL,
        input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
        cache_read_tokens INTEGER NOT NULL, cache_write_tokens INTEGER NOT NULL, estimated_cost REAL
    )"""

_REQUEST_TABLES_AND_INDEXES = (
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
    "CREATE INDEX entries_totals ON entries(day, billing_mode, input_tokens, output_tokens, "
    "cache_read_tokens, cache_write_tokens, estimated_cost)",
    *(
        f"CREATE INDEX entries_{column} ON entries({column}, occurred_at DESC, entry_id DESC)"
        for column in (
            "provider_id",
            "endpoint_id",
            "model_id",
            "workspace",
            "session_id",
            "run_id",
            "purpose",
            "plugin_id",
            "billing_mode",
        )
    ),
)


def rebuild_v5_requests(connection: sqlite3.Connection) -> None:
    connection.execute(f"CREATE TABLE new_entries {_ENTRY_COLUMNS}")
    columns = [row[1] for row in connection.execute("PRAGMA table_info(entries)")]
    names = ",".join(columns)
    connection.execute(f"INSERT INTO new_entries ({names}) SELECT {names} FROM entries")
    connection.execute("DROP TABLE entries")
    connection.execute("ALTER TABLE new_entries RENAME TO entries")
    connection.execute("ALTER TABLE entry_records DROP COLUMN source_event_id")
    for statement in _REQUEST_TABLES_AND_INDEXES:
        connection.execute(statement)
    with closing(
        connection.execute("SELECT event_id, payload FROM events ORDER BY sequence")
    ) as rows:
        for row in rows:
            event = loads(row["payload"])
            if event["event_type"] != "record":
                continue
            entry = validate(ProviderUsageEntry, event["entry"])
            connection.execute(
                "INSERT INTO raw_requests VALUES (?, ?)", (entry.entry_id, row["event_id"])
            )
            if entry.run_id is not None:
                _add_run_usage(connection, entry)


def _add_run_usage(connection: sqlite3.Connection, entry: ProviderUsageEntry) -> None:
    connection.execute(
        "INSERT INTO raw_run_totals VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(session_id, run_id) DO UPDATE SET "
        "request_count=request_count+1, "
        "agent_request_count=agent_request_count+excluded.agent_request_count, "
        "compaction_request_count=compaction_request_count+excluded.compaction_request_count, "
        "input_tokens=input_tokens+excluded.input_tokens, "
        "output_tokens=output_tokens+excluded.output_tokens, "
        "cache_read_tokens=cache_read_tokens+excluded.cache_read_tokens, "
        "cache_write_tokens=cache_write_tokens+excluded.cache_write_tokens, "
        "estimated_cost=estimated_cost+excluded.estimated_cost, "
        "priced_request_count=priced_request_count+excluded.priced_request_count, "
        "has_unknown=MAX(has_unknown, excluded.has_unknown), "
        "has_usage_based=MAX(has_usage_based, excluded.has_usage_based), "
        "has_subscription=MAX(has_subscription, excluded.has_subscription)",
        (
            entry.session_id,
            entry.run_id,
            int(entry.purpose == "agent"),
            int(entry.purpose == "compaction"),
            entry.input_tokens,
            entry.output_tokens,
            entry.cache_read_tokens,
            entry.cache_write_tokens,
            entry.estimated_cost or 0.0,
            int(entry.estimated_cost is not None),
            int(entry.billing_mode == "unknown"),
            int(entry.billing_mode == "usage_based"),
            int(entry.billing_mode == "subscription"),
        ),
    )
