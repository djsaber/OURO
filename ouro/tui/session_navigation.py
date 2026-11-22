"""One navigation row for side conversations, the return target, and the controlled Agent tree."""

from __future__ import annotations

from collections.abc import Iterable

from rich.style import Style
from rich.text import Text
from textual import events
from textual.message import Message
from textual.widgets import Static

from ouro.application.contracts.agents import AgentTreeNode
from ouro.application.contracts.sessions import SessionCatalogEntry
from ouro.tui.theme import theme_for

_MAX_NODES_PER_GROUP = 10
_SESSION_NODE_META = "ouro.session_navigation.session_id"
_FAILED_STATUSES = frozenset({"failed", "aborted", "recoverable"})
_RUNNING_STATUSES = frozenset(
    {
        "requested",
        "child_created",
        "submitted",
        "queued",
        "running",
        "waiting_user",
        "waiting_approval",
        "waiting_child",
    }
)


class SessionNavigationPanel(Static):
    """Navigate the main session, its side conversations, and its subagents."""

    class NavigationRequested(Message):
        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    def __init__(self) -> None:
        super().__init__(id="session-navigation-panel")
        self.display = False
        self._main_session_id: str | None = None
        self._main_status = "idle"
        self._subagents: tuple[AgentTreeNode, ...] = ()
        self._side_sessions: tuple[SessionCatalogEntry, ...] = ()
        self._current_session_id: str | None = None
        self._selected_session_id: str | None = None
        self._hovered_session_id: str | None = None
        self._navigation_active = False

    @property
    def current_session_id(self) -> str | None:
        return self._current_session_id

    @property
    def selected_session_id(self) -> str | None:
        return self._selected_session_id

    @property
    def navigation_active(self) -> bool:
        return self._navigation_active

    @property
    def visible_related_session_ids(self) -> tuple[str, ...]:
        return (
            *(side.session_id for side in self._visible_nodes(self._side_sessions)),
            *(agent.session_id for agent in self._visible_nodes(self._subagents)),
        )

    @property
    def _node_ids(self) -> tuple[str, ...]:
        if self._main_session_id is None:
            return ()
        return (
            *(side.session_id for side in self._side_sessions),
            self._main_session_id,
            *(agent.session_id for agent in self._subagents),
        )

    def set_nodes(
        self,
        *,
        main_session_id: str | None,
        subagents: Iterable[AgentTreeNode],
        main_status: str,
        side_sessions: tuple[SessionCatalogEntry, ...] = (),
    ) -> None:
        groups: dict[str, list[AgentTreeNode]] = {}
        for agent in subagents:
            groups.setdefault(agent.parent_run_id, []).append(agent)
        materialized = tuple(agent for group in groups.values() for agent in group)
        if (
            main_session_id == self._main_session_id
            and main_status == self._main_status
            and materialized == self._subagents
            and side_sessions == self._side_sessions
        ):
            return
        main_changed = main_session_id != self._main_session_id
        self._main_session_id = main_session_id
        self._main_status = main_status
        self._subagents = materialized
        self._side_sessions = side_sessions
        available = self._node_ids
        if main_changed:
            self._current_session_id = main_session_id
        if self._selected_session_id not in available:
            self._selected_session_id = self._current_session_id
        if self._hovered_session_id not in available:
            self._hovered_session_id = None
        self._refresh_content()

    def set_current_session(self, session_id: str | None) -> None:
        selected_session_id = self._selected_session_id if self._navigation_active else session_id
        if (
            session_id == self._current_session_id
            and selected_session_id == self._selected_session_id
        ):
            return
        self._current_session_id = session_id
        if not self._navigation_active:
            self._selected_session_id = session_id
        self._refresh_content()

    def begin_navigation(self) -> None:
        hover_changed = self._hovered_session_id is not None
        self._hovered_session_id = None
        if not (self._subagents or self._side_sessions) or self._navigation_active:
            if hover_changed:
                self._refresh_content()
            return
        self._selected_session_id = self._current_session_id or self._main_session_id
        self._navigation_active = True
        self._refresh_content()

    def end_navigation(self) -> None:
        if not self._navigation_active and self._selected_session_id == self._current_session_id:
            return
        self._navigation_active = False
        self._selected_session_id = self._current_session_id
        self._refresh_content()

    def select_relative(self, offset: int) -> str | None:
        session_ids = self._node_ids
        if not session_ids or offset == 0:
            return self._selected_session_id
        selected = self._selected_session_id
        index = session_ids.index(selected) if selected in session_ids else 0
        index = min(max(index + offset, 0), len(session_ids) - 1)
        return self._select(session_ids[index])

    def select_main(self) -> str | None:
        return self._select(self._main_session_id)

    def select_last_node(self) -> str | None:
        nodes = self._node_ids
        return self._select(nodes[-1] if nodes else None)

    def select_session(self, session_id: str) -> str | None:
        if session_id not in self._node_ids:
            return None
        return self._select(session_id)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        session_id = None
        if event.style is not None:
            candidate = event.style.meta.get(_SESSION_NODE_META)
            if isinstance(candidate, str):
                session_id = candidate
        self._set_hovered_session(session_id)

    def on_leave(self, _event: events.Leave) -> None:
        self._set_hovered_session(None)

    def on_click(self, event: events.Click) -> None:
        if event.button != 1 or event.style is None:
            return
        session_id = event.style.meta.get(_SESSION_NODE_META)
        if isinstance(session_id, str):
            self.post_message(self.NavigationRequested(session_id))
            event.stop()

    def render(self) -> Text:
        width = self.content_region.width or self.size.width
        return self.render_content(max(1, width))

    def render_content(self, width: int) -> Text:
        theme = theme_for(self)
        suffix = self._node_text()
        detail = Text()
        selected = self._hovered_session_id or self._selected_session_id
        selected_subagent = next(
            (agent for agent in self._subagents if agent.session_id == selected),
            None,
        )
        if selected_subagent is not None:
            identity = selected_subagent.nickname or selected_subagent.agent_name
            detail.append(
                theme.inline_separator.join((selected_subagent.task_description, identity))
                + theme.inline_separator,
                style=theme.text_lowlight,
            )
        elif side := next(
            (item for item in self._side_sessions if item.session_id == selected), None
        ):
            detail.append(side.title + theme.inline_separator, style=theme.text_lowlight)
        available_detail_width = max(0, width - suffix.cell_len)
        detail.truncate(available_detail_width, overflow="ellipsis")
        content = Text(" " * max(0, width - detail.cell_len - suffix.cell_len))
        content.append_text(detail)
        content.append_text(suffix)
        content.truncate(width, overflow="ellipsis")
        return content

    def refresh_theme(self) -> None:
        self._refresh_content()

    def _select(self, session_id: str | None) -> str | None:
        self._selected_session_id = session_id
        self._refresh_content()
        return session_id

    def _set_hovered_session(self, session_id: str | None) -> None:
        if session_id == self._hovered_session_id:
            return
        self._hovered_session_id = session_id
        self._refresh_content()

    def _visible_nodes[T: (SessionCatalogEntry, AgentTreeNode)](
        self, nodes: tuple[T, ...]
    ) -> tuple[T, ...]:
        if len(nodes) <= _MAX_NODES_PER_GROUP:
            return nodes
        index = next(
            (i for i, node in enumerate(nodes) if node.session_id == self._selected_session_id),
            len(nodes) - 1,
        )
        max_start = len(nodes) - _MAX_NODES_PER_GROUP
        start = min(max(0, index - _MAX_NODES_PER_GROUP // 2), max_start)
        return nodes[start : start + _MAX_NODES_PER_GROUP]

    def _node_text(self) -> Text:
        if self._main_session_id is None or (
            self._current_session_id == self._main_session_id
            and not self._subagents
            and not self._side_sessions
        ):
            return Text()
        theme = theme_for(self)
        selected = self._hovered_session_id or self._selected_session_id
        nodes = Text()

        def append_node(session_id: str, status: str) -> None:
            mark = (
                theme.selected_session_node_mark
                if selected == session_id
                else theme.session_node_mark
            )
            nodes.append(mark, style=self._node_style(status, session_id))

        for side in self._visible_nodes(self._side_sessions):
            append_node(side.session_id, side.status)
        if self._side_sessions:
            nodes.append(" | ", style=theme.text_lowlight)
        append_node(self._main_session_id, self._main_status)
        if self._subagents:
            nodes.append(" | ", style=theme.text_lowlight)
        previous_run_id: str | None = None
        for agent in self._visible_nodes(self._subagents):
            if previous_run_id is not None and agent.parent_run_id != previous_run_id:
                nodes.append(" ")
            append_node(agent.session_id, agent.status)
            previous_run_id = agent.parent_run_id
        return nodes

    def _node_style(self, status: str, session_id: str) -> Style:
        return Style(
            color=theme_for(self).accent
            if session_id == self._current_session_id
            else self._status_color(status),
            meta={_SESSION_NODE_META: session_id},
        )

    def _status_color(self, status: str) -> str:
        theme = theme_for(self)
        if status in _FAILED_STATUSES:
            return theme.failure
        if status in _RUNNING_STATUSES:
            return theme.running
        return theme.text_lowlight

    def _refresh_content(self) -> None:
        self.refresh(layout=True)


__all__ = ["SessionNavigationPanel"]
