"""Project application snapshots into terminal status views."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from ouro.application.contracts.agents import AgentTreeNode
from ouro.application.contracts.plugins import PluginSummary
from ouro.core.usage_queries import UsageSummary
from ouro.tui.view_models import McpServerStatus, TuiDailyUsage, TuiSystemStatus

_FAILED_SUBAGENT_STATUSES = frozenset({"failed", "aborted", "recoverable"})


def _subagent_task_status(agent: AgentTreeNode) -> str:
    if agent.status == "completed":
        return "done"
    if agent.status in _FAILED_SUBAGENT_STATUSES:
        return "failed"
    if agent.status == "paused":
        return "paused"
    return _subagent_execution_status(agent.execution_mode)


def _subagent_execution_status(execution_mode: object) -> str:
    return "background" if execution_mode == "async" else "working"


_ACTIVITY_LABELS = {
    "starting": "Starting services",
    "loading_session": "Loading session",
    "mcp_connecting": "Connecting MCP servers",
    "idle": "Ready",
    "queued": "Queued",
    "preparing_model": "Preparing context",
    "waiting_model": "Waiting for model",
    "streaming_reasoning": "Thinking",
    "streaming_text": "Responding",
    "streaming_tool_call": "Preparing tool call",
    "preparing_tools": "Checking tools",
    "waiting_approval": "Approval required",
    "waiting_user": "Input required",
    "waiting_child": "Waiting for subagent",
    "executing_tools": "Running tools",
    "committing_results": "Saving tool results",
    "finalizing_response": "Finalizing",
    "compacting": "Compacting context",
    "forking": "Forking session",
    "cancelling": "Cancelling",
    "completed": "Ready",
    "aborted": "Aborted",
    "failed": "Failed",
    "paused": "Paused",
    "recoverable": "Recovery required",
}


_PLUGIN_ISSUE_STATUSES = frozenset(
    {
        "invalid",
        "incompatible",
        "needs_configuration",
        "degraded",
        "blocked",
        "failed",
    }
)


def project_usage_system_status(
    status: TuiSystemStatus,
    usage: UsageSummary,
) -> TuiSystemStatus:
    """Project canonical ledger totals and recent daily detail into the Host view."""

    totals = usage.totals
    return replace(
        status,
        usage_request_count=totals.request_count,
        usage_input_tokens=totals.input_tokens,
        usage_output_tokens=totals.output_tokens,
        usage_cache_read_tokens=totals.cache_read_tokens,
        usage_daily=tuple(
            TuiDailyUsage(
                date=cast(str, group.key[0]),
                request_count=group.totals.request_count,
                input_tokens=group.totals.input_tokens,
                output_tokens=group.totals.output_tokens,
                cache_read_tokens=group.totals.cache_read_tokens,
            )
            for group in usage.groups[-10:]
        ),
    )


def project_mcp_system_status(
    status: TuiSystemStatus,
    servers: tuple[McpServerStatus, ...],
) -> TuiSystemStatus:
    """Project one live MCP snapshot into the shared TUI system summary."""

    connected_count = 0
    tool_count = 0
    connecting_count = 0
    failed_count = 0
    for server in servers:
        if server.status == "connected":
            connected_count += 1
            tool_count += server.tool_count
        elif server.status in {"starting", "reconnecting"}:
            connecting_count += 1
        elif server.status == "failed":
            failed_count += 1
    return replace(
        status,
        mcp_connected_count=connected_count,
        mcp_total_count=len(servers),
        mcp_tool_count=tool_count,
        mcp_connecting_count=connecting_count,
        mcp_failed_count=failed_count,
    )


def project_plugin_system_status(
    status: TuiSystemStatus,
    plugins: tuple[PluginSummary, ...],
) -> TuiSystemStatus:
    """Project the canonical plugin catalog into the shared TUI summary."""

    active_names: list[str] = []
    issue_count = 0
    for plugin in plugins:
        if plugin.status in {"active", "ready"}:
            active_names.append(plugin.name)
        if plugin.status in _PLUGIN_ISSUE_STATUSES:
            issue_count += 1
    return replace(
        status,
        plugin_active_count=len(active_names),
        plugin_total_count=len(plugins),
        plugin_issue_count=issue_count,
        plugin_active_names=tuple(sorted(active_names)),
    )
