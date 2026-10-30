from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ouro.billing.queries import FILTER_COLUMNS, summarize, timestamp, validate_limit, where
from ouro.billing.schema import USAGE_SCHEMA as _SCHEMA
from ouro.core.billing import ProviderUsageEntry, UsageFilter, UsageLedgerEvent
from ouro.core.errors import PersistenceError, UnsupportedDataVersionError
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.usage_queries import (
    RunUsageTotals,
    UsageAuditLog,
    UsageEntryCursor,
    UsageEntryList,
    UsageGroupBy,
    UsageSummary,
    UsageTotals,
)
from ouro.local_locks import FileMutationLock

_SCHEMA_VERSION = 5
_APPLICATION_ID = 0x4F555242
_MEASURES = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "estimated_cost",
)
_COLUMNS = ("entry_id", "occurred_at", "day", *FILTER_COLUMNS.values(), *_MEASURES)


class LocalUsageLedger:
    """Transactional local usage facts, query projections, and append-only audit."""

    def __init__(self, root: str | Path | None = None) -> None:
        root_value = root if root is not None else os.environ.get("OURO_HOME")
        self.root = Path(root_value) if root_value is not None else Path.home() / ".ouro"
        self.path = self.root / "billing" / "usage.sqlite3"
        self._initialized = False
        self._initialization_lock = threading.Lock()

    def initialize(self) -> None:
        with self._initialization_lock:
            if self._initialized:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self._connect() as connection:
                    ready = self._has_schema(connection)
                if not ready:
                    # WAL mode changes precede the SQL transaction and need their
                    # own cross-instance owner. Existing databases stay lock-free here.
                    lock = FileMutationLock(self.path.with_suffix(".init.lock"))
                    if not lock.acquire(timeout=5):
                        raise PersistenceError(f"usage initialization lock timed out: {self.path}")
                    try:
                        with self._connect() as connection:
                            if not self._has_schema(connection):
                                connection.execute("PRAGMA journal_mode=WAL")
                                connection.execute("BEGIN IMMEDIATE")
                                if not self._has_schema(connection):
                                    for statement in _SCHEMA:
                                        connection.execute(statement)
                                    connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                                    connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                    finally:
                        lock.release()
            except OSError as exc:
                raise PersistenceError(f"could not initialize usage ledger: {self.path}") from exc
            self._initialized = True

    @staticmethod
    def _has_schema(connection: sqlite3.Connection) -> bool:
        version, application, has_tables = connection.execute(
            "SELECT user_version, application_id, "
            "EXISTS(SELECT 1 FROM sqlite_master WHERE type='table') "
            "FROM pragma_user_version, pragma_application_id"
        ).fetchone()
        if version == _SCHEMA_VERSION and application == _APPLICATION_ID:
            return True
        if version == 0 and application == 0 and not has_tables:
            return False
        raise UnsupportedDataVersionError(
            f"unsupported usage database schema: found {version}, expected {_SCHEMA_VERSION}"
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        except sqlite3.Error as exc:
            raise PersistenceError(
                f"usage storage failed ({type(exc).__name__}): {self.path}"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            if write:
                # Maintain bounded planner statistics as data distribution changes.
                # Include all tables: each operation owns a fresh connection.
                connection.execute("PRAGMA optimize=0x10012")

    def record(self, entry: ProviderUsageEntry) -> ProviderUsageEntry:
        with self._transaction(write=True) as connection:
            source = self._get_raw(connection, entry.entry_id)
            if source is not None:
                if source.billing_mode != entry.billing_mode:
                    raise PersistenceError(f"usage billing mode conflicts: {entry.entry_id}")
                if source != entry.model_copy(update={"occurred_at": source.occurred_at}):
                    raise PersistenceError(
                        f"usage entry id conflicts with existing data: {entry.entry_id}"
                    )
                return source
            if entry.revision != 1:
                raise PersistenceError("new usage entries must have revision 1")
            self._write_entry(connection, entry)
            event = UsageLedgerEvent(event_type="record", entry=entry)
            self._append_event(connection, event)
            connection.execute(
                "INSERT INTO raw_requests VALUES (?, ?)", (entry.entry_id, event.event_id)
            )
            if entry.run_id is not None:
                self._add_run_usage(connection, entry)
            connection.execute(
                "INSERT INTO entry_records VALUES (?, ?)",
                (entry.entry_id, entry.model_dump_json()),
            )
            connection.executemany(
                "INSERT INTO scopes VALUES (?, ?, ?)",
                (
                    (entry.entry_id, owner, scope)
                    for owner, scopes in entry.harness_scopes.items()
                    for scope in set(scopes)
                ),
            )
        return entry.model_copy(deep=True)

    def get_raw(self, entry_id: str) -> ProviderUsageEntry | None:
        """Return the original report, including requests hidden by a billing reset."""
        with self._transaction() as connection:
            return self._get_raw(connection, entry_id)

    @staticmethod
    def _get_raw(connection: sqlite3.Connection, entry_id: str) -> ProviderUsageEntry | None:
        row = connection.execute(
            "SELECT events.payload FROM raw_requests "
            "JOIN events ON source_event_id=event_id WHERE raw_requests.entry_id=?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return None
        event = decode_versioned_record_json(UsageLedgerEvent, row[0], label="usage ledger event")
        if event.event_type != "record" or event.entry is None or event.entry.entry_id != entry_id:
            raise PersistenceError("usage source event does not match its original request")
        return event.entry

    @staticmethod
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

    def run_usage(self, session_id: str, run_id: str) -> RunUsageTotals | None:
        """Point-read raw consumption; no recorded request remains unknown."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM raw_run_totals WHERE session_id=? AND run_id=?",
                (session_id, run_id),
            ).fetchone()
        return self._raw_totals(row)

    def session_usage(self, session_id: str) -> RunUsageTotals | None:
        """Aggregate one Session's narrow raw Run totals without loading Run records."""
        sums = (
            "request_count",
            "agent_request_count",
            "compaction_request_count",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "estimated_cost",
            "priced_request_count",
        )
        modes = ("has_unknown", "has_usage_based", "has_subscription")
        projection = ", ".join(
            [
                *(f"SUM({column}) AS {column}" for column in sums),
                *(f"MAX({column}) AS {column}" for column in modes),
            ]
        )
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {projection} FROM raw_run_totals WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return self._raw_totals(row)

    @staticmethod
    def _raw_totals(row: sqlite3.Row | None) -> RunUsageTotals | None:
        if row is None or row["request_count"] is None:
            return None
        return RunUsageTotals(
            request_count=row["request_count"],
            agent_request_count=row["agent_request_count"],
            compaction_request_count=row["compaction_request_count"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cache_read_tokens=row["cache_read_tokens"],
            cache_write_tokens=row["cache_write_tokens"],
            estimated_cost=row["estimated_cost"],
            priced_request_count=row["priced_request_count"],
            billing_modes=tuple(
                mode for mode in ("subscription", "unknown", "usage_based") if row[f"has_{mode}"]
            ),
        )

    def get(self, entry_id: str) -> ProviderUsageEntry:
        with self._transaction() as connection:
            return self._get(connection, entry_id)

    @staticmethod
    def _get(connection: sqlite3.Connection, entry_id: str) -> ProviderUsageEntry:
        row = connection.execute(
            "SELECT payload FROM entry_records WHERE entry_id=?", (entry_id,)
        ).fetchone()
        if row is None:
            raise KeyError(entry_id)
        return _entry(row[0])

    def list_entries(
        self,
        usage_filter: UsageFilter,
        *,
        limit: int = 100,
        before: UsageEntryCursor | None = None,
    ) -> UsageEntryList:
        validate_limit(limit)
        condition, values = where(usage_filter)
        with self._transaction() as connection:
            count = connection.execute(
                f"SELECT COUNT(*) FROM entries WHERE {condition}", values
            ).fetchone()[0]
            if before is not None:
                condition += " AND (occurred_at, entry_id) < (?, ?)"
                values.extend((timestamp(before.occurred_at), before.entry_id))
            rows = connection.execute(
                "SELECT payload FROM entry_records JOIN ("
                f"SELECT entry_id, occurred_at FROM entries WHERE {condition} "
                "ORDER BY occurred_at DESC, entry_id DESC LIMIT ?) selected USING(entry_id) "
                "ORDER BY occurred_at DESC, entry_id DESC",
                [*values, limit + 1],
            ).fetchall()
        entries = tuple(_entry(row[0]) for row in rows[:limit])
        truncated = len(rows) > limit
        return UsageEntryList(
            usage_filter=usage_filter,
            entries=entries,
            total_matching=count,
            next_cursor=(
                UsageEntryCursor(occurred_at=entries[-1].occurred_at, entry_id=entries[-1].entry_id)
                if truncated
                else None
            ),
        )

    def summarize(
        self,
        usage_filter: UsageFilter,
        *,
        group_by: tuple[UsageGroupBy, ...] = (),
    ) -> UsageSummary:
        with self._transaction() as connection:
            return summarize(connection, usage_filter, group_by)

    def session_scope_tokens(self, session_id: str) -> dict[str, dict[str, int]]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT owner_id, scope, SUM(input_tokens + output_tokens) AS tokens "
                "FROM entries JOIN scopes USING(entry_id) WHERE session_id=? "
                "GROUP BY owner_id, scope HAVING tokens > 0",
                (session_id,),
            ).fetchall()
        result: dict[str, dict[str, int]] = {}
        for row in rows:
            result.setdefault(row["owner_id"], {})[row["scope"]] = row["tokens"]
        return result

    def replace(self, entry: ProviderUsageEntry, *, note: str) -> ProviderUsageEntry:
        if not note.strip():
            raise ValueError("a modification note is required")
        with self._transaction(write=True) as connection:
            current = self._get(connection, entry.entry_id)
            if entry.revision != current.revision + 1:
                raise PersistenceError(f"stale usage revision for {entry.entry_id}")
            mutable = {*_MEASURES, "revision", "modified_at", "modification_note", "metadata"}
            current_metadata = {
                key: value for key, value in current.metadata.items() if key != "manually_corrected"
            }
            entry_metadata = {
                key: value for key, value in entry.metadata.items() if key != "manually_corrected"
            }
            if (
                current.model_dump(exclude=mutable) != entry.model_dump(exclude=mutable)
                or current_metadata != entry_metadata
            ):
                raise PersistenceError("usage correction cannot change request attribution")
            self._write_entry(connection, entry)
            connection.execute(
                "UPDATE entry_records SET payload=? WHERE entry_id=?",
                (entry.model_dump_json(), entry.entry_id),
            )
            self._append_event(
                connection, UsageLedgerEvent(event_type="replace", entry=entry, note=note.strip())
            )
        return entry.model_copy(deep=True)

    def reset(self, usage_filter: UsageFilter, *, note: str) -> UsageTotals:
        if not note.strip():
            raise ValueError("a reset note is required")
        condition, values = where(usage_filter)
        with self._transaction(write=True) as connection:
            removed = summarize(connection, usage_filter, ()).totals
            connection.execute(f"DELETE FROM entries WHERE {condition}", values)
            self._append_event(
                connection,
                UsageLedgerEvent(event_type="reset", usage_filter=usage_filter, note=note.strip()),
            )
        return removed

    def audit(self, *, limit: int = 100, before: int | None = None) -> UsageAuditLog:
        validate_limit(limit)
        if before is not None and before < 1:
            raise ValueError("usage audit cursor must be positive")
        with self._transaction() as connection:
            count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            rows = connection.execute(
                "SELECT sequence, payload FROM events "
                + ("WHERE sequence < ? " if before else "")
                + "ORDER BY sequence DESC LIMIT ?",
                ([before, limit + 1] if before else [limit + 1]),
            ).fetchall()
        return UsageAuditLog(
            events=tuple(
                decode_versioned_record_json(
                    UsageLedgerEvent, row["payload"], label="usage ledger event"
                )
                for row in rows[:limit]
            ),
            total_events=count,
            next_cursor=rows[limit - 1]["sequence"] if len(rows) > limit else None,
        )

    @staticmethod
    def _write_entry(connection: sqlite3.Connection, entry: ProviderUsageEntry) -> None:
        occurred_at = timestamp(entry.occurred_at)
        values = (
            entry.entry_id,
            occurred_at,
            occurred_at[:10],
            *(getattr(entry, column) for column in FILTER_COLUMNS.values()),
            *(getattr(entry, field) for field in _MEASURES),
        )
        updates = ", ".join(f"{field}=excluded.{field}" for field in _MEASURES)
        connection.execute(
            f"INSERT INTO entries ({','.join(_COLUMNS)}) VALUES ({','.join('?' for _ in values)}) "
            f"ON CONFLICT(entry_id) DO UPDATE SET {updates}",
            values,
        )

    @staticmethod
    def _append_event(connection: sqlite3.Connection, event: UsageLedgerEvent) -> None:
        connection.execute(
            "INSERT INTO events(event_id, payload) VALUES (?, ?)",
            (event.event_id, event.model_dump_json()),
        )


def _entry(payload: str) -> ProviderUsageEntry:
    return decode_versioned_record_json(ProviderUsageEntry, payload, label="provider usage entry")
