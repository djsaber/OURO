from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from ouro.core.billing import UsageFilter
from ouro.core.usage_queries import UsageGroupBy, UsageSummary, UsageSummaryGroup, UsageTotals

# SQL identifiers are owned here; caller-supplied values use bound parameters.
DIMENSIONS: dict[UsageGroupBy, str] = {
    "date": "day",
    "provider": "provider_id",
    "endpoint": "endpoint_id",
    "model": "model_id",
    "workspace": "workspace",
    "session": "session_id",
    "run": "run_id",
    "purpose": "purpose",
    "plugin": "plugin_id",
    "billing_mode": "billing_mode",
}
FILTER_COLUMNS = {
    "provider_ids": "provider_id",
    "endpoint_ids": "endpoint_id",
    "model_ids": "model_id",
    "workspaces": "workspace",
    "session_ids": "session_id",
    "run_ids": "run_id",
    "purposes": "purpose",
    "plugin_ids": "plugin_id",
    "billing_modes": "billing_mode",
}
AGGREGATES = """
    COUNT(*) AS request_count,
    COALESCE(SUM(input_tokens), 0) AS input_tokens,
    COALESCE(SUM(output_tokens), 0) AS output_tokens,
    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
    COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
    COALESCE(SUM(estimated_cost), 0) AS estimated_cost,
    COUNT(estimated_cost) AS priced_request_count,
    GROUP_CONCAT(DISTINCT billing_mode) AS billing_modes
"""


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def where(usage_filter: UsageFilter) -> tuple[str, list[object]]:
    clauses: list[str] = []
    values: list[object] = []
    for boundary, operator in ((usage_filter.occurred_from, ">="), (usage_filter.occurred_to, "<")):
        if boundary is not None:
            clauses.append(f"occurred_at {operator} ?")
            values.append(timestamp(boundary))
    for field, column in FILTER_COLUMNS.items():
        selected = getattr(usage_filter, field)
        if not selected:
            continue
        present = sorted(value for value in selected if value is not None)
        alternatives = []
        if present:
            alternatives.append(f"{column} IN ({','.join('?' for _ in present)})")
            values.extend(present)
        if None in selected:
            alternatives.append(f"{column} IS NULL")
        clauses.append("(" + " OR ".join(alternatives) + ")")
    return " AND ".join(clauses) or "1", values


def totals(row: sqlite3.Row) -> UsageTotals:
    values = {name: row[name] for name in UsageTotals.model_fields}
    values["billing_modes"] = tuple(
        sorted(filter(None, (values["billing_modes"] or "").split(",")))
    )
    return UsageTotals.model_validate(values)


def summarize(
    connection: sqlite3.Connection,
    usage_filter: UsageFilter,
    group_by: tuple[UsageGroupBy, ...],
) -> UsageSummary:
    if len(set(group_by)) != len(group_by) or any(item not in DIMENSIONS for item in group_by):
        raise ValueError("usage grouping requires distinct supported dimensions")
    condition, values = where(usage_filter)
    columns = ", ".join(DIMENSIONS[item] for item in group_by)
    if not columns:
        row = connection.execute(
            f"SELECT {AGGREGATES} FROM entries WHERE {condition}", values
        ).fetchone()
        return UsageSummary(usage_filter=usage_filter, totals=totals(row))
    rows = connection.execute(
        f"SELECT {columns}, {AGGREGATES} FROM entries WHERE {condition} "
        f"GROUP BY {columns} ORDER BY {columns}",
        values,
    )
    groups = tuple(
        UsageSummaryGroup(key=tuple(row[DIMENSIONS[item]] for item in group_by), totals=totals(row))
        for row in rows
    )
    # Overall totals share the grouped snapshot and need no second facts scan.
    combined = {
        name: sum(getattr(group.totals, name) for group in groups)
        for name in UsageTotals.model_fields
        if name != "billing_modes"
    }
    combined["billing_modes"] = tuple(
        sorted({mode for group in groups for mode in group.totals.billing_modes})
    )
    return UsageSummary(
        usage_filter=usage_filter,
        group_by=group_by,
        groups=groups,
        totals=UsageTotals.model_validate(combined),
    )


def validate_limit(limit: int) -> None:
    if not 1 <= limit <= 10_000:
        raise ValueError("usage query limit must be between 1 and 10000")
