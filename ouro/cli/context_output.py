from __future__ import annotations

import json

from ouro.application.contracts.sessions import SessionContextSnapshot, SessionContextStatus
from ouro.core.context import ContextMetrics

NO_PREPARED_CONTEXT = "No Provider context has been prepared for this Session."


def render_context_status(status: SessionContextStatus) -> str:
    route = "/".join(
        value for value in (status.provider_id, status.endpoint_id, status.model_id) if value
    )
    size = _context_size(status.metrics)
    lines = [
        f"Context: {status.context_id}",
        f"Route: {route}",
        f"Size: {size}",
        f"Prepared: {status.prepared_at.isoformat()}",
    ]
    metrics = status.metrics
    if metrics.context_window_tokens is not None:
        lines.append(
            f"Window: {metrics.context_window_tokens} tokens "
            f"({metrics.reserved_output_tokens} reserved for output)"
        )
    if status.provider_reported_input_tokens is not None:
        lines.append(f"Provider reported input: {status.provider_reported_input_tokens} tokens")
    if status.active_compaction_id is not None:
        lines.append(f"Compaction: {status.active_compaction_id}")
    return "\n".join(lines)


def _context_size(metrics: ContextMetrics) -> str:
    if metrics.input_budget_tokens is None:
        return f"{metrics.estimated_input_tokens} estimated input tokens"
    utilization = metrics.estimated_utilization_ratio or 0.0
    remaining = metrics.estimated_remaining_tokens or 0
    return (
        f"{metrics.estimated_input_tokens}/{metrics.input_budget_tokens} estimated input "
        f"tokens ({utilization:.1%}, {remaining} remaining)"
    )


def render_context_snapshot(snapshot: SessionContextSnapshot) -> str:
    payload = snapshot.provider_context.model_dump(mode="json")
    return "\n".join(
        (
            render_context_status(snapshot.status),
            "Provider context:",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
    )


__all__ = [
    "NO_PREPARED_CONTEXT",
    "render_context_snapshot",
    "render_context_status",
]
