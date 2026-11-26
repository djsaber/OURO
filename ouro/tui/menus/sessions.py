from __future__ import annotations

from calendar import month_name
from collections.abc import Iterable
from datetime import date, datetime

from rich.text import Text

from ouro.application.contracts.sessions import SessionCatalogEntry
from ouro.tui.menus.models import Choice
from ouro.tui.preferences import SessionGrouping
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme

_SESSION_TITLE_MAX_CELLS = 56


_SESSION_DOMAIN_LABELS = {
    "default": "USER",
    "task": "TASK",
    "subagent": "SUBAGENT",
    "archived": "ARCHIVED",
}


_SESSION_GROUPING_LABELS: dict[SessionGrouping, str] = {
    "date": "By date",
    "workspace": "By workspace",
}


def _truncate_session_title(title: str) -> str:
    rendered = Text(title)
    rendered.truncate(_SESSION_TITLE_MAX_CELLS, overflow="ellipsis")
    return rendered.plain


def _session_choice_label(item: SessionCatalogEntry, *, theme: TuiTheme) -> str:
    title = item.title
    if item.session_kind == "primary" and item.parent_session_id is not None:
        title = f"forked{theme.inline_separator}{title}"
    if item.pinned:
        title = f"★ {title}"
    return _truncate_session_title(title)


def _has_navigation_node(item: SessionCatalogEntry) -> bool:
    return (
        item.session_kind == "primary"
        and not item.archived
        and item.health in {"ok", "recoverable"}
        and item.catalog_error is None
    )


def _side_session_nodes(related: Iterable[SessionCatalogEntry]) -> tuple[SessionCatalogEntry, ...]:
    visible = {item.session_id: item for item in related if _has_navigation_node(item)}

    def node_order(item: SessionCatalogEntry) -> tuple[datetime, str, bool, datetime, str]:
        owner = visible.get(item.context_source_session_id or "", item)
        return (
            owner.created_at,
            owner.session_id,
            owner is not item,
            item.created_at,
            item.session_id,
        )

    return tuple(sorted(visible.values(), key=node_order, reverse=True))


def _session_open_targets(
    sessions: tuple[SessionCatalogEntry, ...],
) -> tuple[SessionCatalogEntry, ...]:
    navigable = {item.session_id for item in sessions if _has_navigation_node(item)}
    return tuple(
        item
        for item in sessions
        if item.context_source_session_id not in navigable or item.session_id not in navigable
    )


def _sessions_in_domain(
    sessions: tuple[SessionCatalogEntry, ...], domain: str
) -> tuple[SessionCatalogEntry, ...]:
    if domain == "archived":
        return tuple(item for item in sessions if item.archived)
    return tuple(
        item
        for item in sessions
        if not item.archived
        and (
            item.session_kind == "subagent"
            if domain == "subagent"
            else item.session_kind != "subagent" and item.namespace == domain
        )
    )


def _session_choices(
    sessions: tuple[SessionCatalogEntry, ...],
    *,
    selected_session_id: str | None = None,
    today: date | None = None,
    grouping: SessionGrouping = "date",
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    local_today = today or datetime.now().astimezone().date()
    groups: dict[str, list[Choice]] = {}
    for item in sessions:
        group = (
            item.workspace or "No workspace"
            if grouping == "workspace"
            else _session_date_group(item.last_activity_at, today=local_today)
        )
        groups.setdefault(group, []).append(
            Choice(
                value=item.session_id,
                label=_session_choice_label(item, theme=theme),
                edit_value=item.title,
                trailing="index error" if item.catalog_error is not None else item.status,
                accent=(
                    theme.failure
                    if (item.catalog_error is not None or item.catalog_marks_unreadable)
                    else theme.warning
                    if item.health != "ok"
                    else None
                ),
                marker=(
                    theme.selected_choice_marker if item.session_id == selected_session_id else ""
                ),
                group=group,
            )
        )
    return tuple(choice for group_choices in groups.values() for choice in group_choices)


def _session_date_group(activity_at: datetime, *, today: date) -> str:
    activity_date = activity_at.astimezone().date()
    age = (today - activity_date).days
    if age <= 0:
        return "Today"
    if age == 1:
        return "Yesterday"
    if age < 7:
        return "Previous 7 days"
    if age < 30:
        return "Previous 30 days"
    return f"{month_name[activity_date.month]} {activity_date.year}"
