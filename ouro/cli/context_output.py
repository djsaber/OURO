from __future__ import annotations

import json

from ouro.application.session_models import SessionContextSnapshot, SessionContextStatus


def render_context_status(status: SessionContextStatus) -> str:
    metrics = status.metrics
    route = "/".join(
        value for value in (status.provider_id, status.endpoint_id, status.model_id) if value
    )
    if metrics.input_budget_tokens is None:
        size = f"{metrics.estimated_input_tokens} estimated input tokens"
    else:
        utilization = metrics.estimated_utilization_ratio or 0.0
        remaining = metrics.estimated_remaining_tokens or 0
        size = (
            f"{metrics.estimated_input_tokens}/{metrics.input_budget_tokens} estimated input "
            f"tokens ({utilization:.1%}, {remaining} remaining)"
        )
    lines = [
        f"Context: {status.context_id}",
        f"Route: {route}",
        f"Size: {size}",
        f"Prepared: {status.prepared_at.isoformat()}",
    ]
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


def render_context_snapshot(snapshot: SessionContextSnapshot) -> str:
    payload = snapshot.provider_context.model_dump(mode="json")
    return "\n".join(
        (
            render_context_status(snapshot.status),
            f"Digest: {snapshot.context_digest}",
            "Provider context:",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
    )


__all__ = [
    "render_context_snapshot",
    "render_context_status",
]
