from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TypedDict, Unpack

from ouro.application.usage import UsageService
from ouro.application.usage_models import UsageGroupBy, UsageTotals
from ouro.cli.errors import error_exit_code
from ouro.core.billing import UsageFilter, UsagePurpose
from ouro.core.errors import OuroError
from ouro.local_application import build_local_usage_service


class UsageFilterArgs(TypedDict, total=False):
    since: str | None
    until: str | None
    provider_ids: list[str] | None
    endpoint_ids: list[str] | None
    model_ids: list[str] | None
    session_ids: list[str] | None
    run_ids: list[str] | None
    purposes: list[UsagePurpose] | None


def summarize_usage(
    *,
    home: str | Path | None = None,
    group_by: UsageGroupBy | None = None,
    json_mode: bool = False,
    **filters: Unpack[UsageFilterArgs],
) -> int:
    try:
        result = _usage(home).summarize(_usage_filter(**filters), group_by=group_by)
    except (OuroError, ValueError) as exc:
        return _print_error(exc, json_mode=json_mode)
    if json_mode:
        print(json.dumps({"status": "ok", **result.model_dump(mode="json")}, ensure_ascii=False))
        return 0

    print("Usage summary:")
    _print_totals(result.totals, prefix="  ")
    if result.groups:
        print(f"By {result.group_by}:")
        for group in result.groups:
            totals = group.totals
            print(
                f"  {group.key}: {totals.request_count} requests, "
                f"{totals.total_tokens} tokens "
                f"({totals.input_tokens} input, {totals.output_tokens} output)"
            )
    return 0


def list_usage(
    *,
    home: str | Path | None = None,
    limit: int = 100,
    json_mode: bool = False,
    **filters: Unpack[UsageFilterArgs],
) -> int:
    try:
        result = _usage(home).list_entries(_usage_filter(**filters), limit=limit)
    except (OuroError, ValueError) as exc:
        return _print_error(exc, json_mode=json_mode)
    if json_mode:
        print(json.dumps({"status": "ok", **result.model_dump(mode="json")}, ensure_ascii=False))
        return 0
    if not result.entries:
        print("No provider usage entries.")
        return 0
    for entry in result.entries:
        route = "/".join(
            part for part in (entry.provider_id, entry.endpoint_id, entry.model_id) if part
        )
        print(
            f"{entry.entry_id} {entry.occurred_at.isoformat()} {entry.purpose} {route} "
            f"in={entry.input_tokens} out={entry.output_tokens} "
            f"cache_read={entry.cache_read_tokens} cache_write={entry.cache_write_tokens} "
            f"billing_mode={entry.billing_mode}"
        )
    if result.truncated:
        print(f"Showing {len(result.entries)} of {result.total_matching} matching entries.")
    return 0


def show_usage_entry(
    entry_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        entry = _usage(home).get_entry(entry_id)
    except KeyError:
        return _print_error(ValueError(f"usage entry not found: {entry_id}"), json_mode=json_mode)
    except OuroError as exc:
        return _print_error(exc, json_mode=json_mode)
    payload = entry.model_dump(mode="json")
    if json_mode:
        print(json.dumps({"status": "ok", "entry": payload}, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def modify_usage_entry(
    entry_id: str,
    *,
    home: str | Path | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    estimated_cost: float | None = None,
    clear_estimated_cost: bool = False,
    note: str,
    json_mode: bool = False,
) -> int:
    try:
        result = _usage(home).replace_entry(
            entry_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            estimated_cost=estimated_cost,
            clear_estimated_cost=clear_estimated_cost,
            note=note,
        )
    except KeyError:
        return _print_error(ValueError(f"usage entry not found: {entry_id}"), json_mode=json_mode)
    except (OuroError, ValueError) as exc:
        return _print_error(exc, json_mode=json_mode)
    if json_mode:
        print(json.dumps({"status": "ok", **result.model_dump(mode="json")}, ensure_ascii=False))
    else:
        assert result.entry is not None
        print(f"Usage entry updated: {result.entry.entry_id} revision {result.entry.revision}")
    return 0


def reset_usage(
    *,
    home: str | Path | None = None,
    force: bool = False,
    note: str,
    json_mode: bool = False,
    **filters: Unpack[UsageFilterArgs],
) -> int:
    try:
        usage_filter = _usage_filter(**filters)
        if not force:
            if json_mode:
                raise ValueError("--force is required for a non-interactive JSON reset")
            confirm = input("Reset matching provider usage entries? [y/N] ").strip().lower()
            if confirm not in {"y", "yes"}:
                print("Cancelled.")
                return 0
        result = _usage(home).reset(usage_filter, note=note)
    except (OuroError, ValueError) as exc:
        return _print_error(exc, json_mode=json_mode)
    if json_mode:
        print(json.dumps({"status": "ok", **result.model_dump(mode="json")}, ensure_ascii=False))
    else:
        print(
            f"Usage reset: {result.affected_count} entries, "
            f"{result.affected_totals.total_tokens} tokens removed from active totals."
        )
    return 0


def audit_usage(
    *,
    home: str | Path | None = None,
    limit: int = 100,
    json_mode: bool = False,
) -> int:
    try:
        result = _usage(home).audit(limit=limit)
    except (OuroError, ValueError) as exc:
        return _print_error(exc, json_mode=json_mode)
    if json_mode:
        print(json.dumps({"status": "ok", **result.model_dump(mode="json")}, ensure_ascii=False))
        return 0
    if not result.events:
        print("No usage ledger events.")
        return 0
    for event in result.events:
        target = event.entry_id or "filtered entries"
        line = f"{event.recorded_at.isoformat()} {event.event_type} {target} {event.note or ''}"
        print(line.rstrip())
    if result.truncated:
        print(f"Showing {len(result.events)} of {result.total_events} events.")
    return 0


def _usage_filter(
    *,
    since: str | None = None,
    until: str | None = None,
    provider_ids: list[str] | None = None,
    endpoint_ids: list[str] | None = None,
    model_ids: list[str] | None = None,
    session_ids: list[str] | None = None,
    run_ids: list[str] | None = None,
    purposes: list[UsagePurpose] | None = None,
) -> UsageFilter:
    return UsageFilter(
        occurred_from=_parse_boundary(since, inclusive_date_end=False),
        occurred_to=_parse_boundary(until, inclusive_date_end=True),
        provider_ids=frozenset(provider_ids or ()),
        endpoint_ids=frozenset(endpoint_ids or ()),
        model_ids=frozenset(model_ids or ()),
        session_ids=frozenset(session_ids or ()),
        run_ids=frozenset(run_ids or ()),
        purposes=frozenset(purposes or ()),
    )


def _parse_boundary(value: str | None, *, inclusive_date_end: bool) -> datetime | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        if len(normalized) == 10:
            parsed_date = date.fromisoformat(normalized)
            if inclusive_date_end:
                parsed_date += timedelta(days=1)
            return datetime.combine(parsed_date, time.min, tzinfo=UTC)
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except ValueError as exc:
        raise ValueError(f"invalid ISO usage date/time: {value}") from exc


def _usage(home: str | Path | None) -> UsageService:
    return build_local_usage_service(home)


def _print_totals(totals: UsageTotals, *, prefix: str = "") -> None:
    print(f"{prefix}Requests: {totals.request_count}")
    print(f"{prefix}Unpriced requests: {totals.unpriced_request_count}")
    print(f"{prefix}Billing modes: {', '.join(totals.billing_modes) or '-'}")
    print(f"{prefix}Input tokens: {totals.input_tokens}")
    print(f"{prefix}Output tokens: {totals.output_tokens}")
    print(f"{prefix}Total tokens: {totals.total_tokens}")
    print(f"{prefix}Cache read tokens: {totals.cache_read_tokens}")
    print(f"{prefix}Cache write tokens: {totals.cache_write_tokens}")
    if totals.priced_request_count:
        suffix = " (partial)" if totals.unpriced_request_count else ""
        print(f"{prefix}Estimated cost (USD): {totals.estimated_cost:.8f}{suffix}")
    else:
        print(f"{prefix}Estimated cost (USD): -")


def _print_error(exc: Exception, *, json_mode: bool) -> int:
    if json_mode:
        print(
            json.dumps(
                {"status": "failed", "error": str(exc), "error_type": type(exc).__name__},
                ensure_ascii=False,
            )
        )
    else:
        print(f"Error: {exc}", file=sys.stderr)
    return error_exit_code(exc) if isinstance(exc, OuroError) else 1
