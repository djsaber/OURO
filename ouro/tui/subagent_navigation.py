"""Compact, root-scoped subagent navigation for the TUI input surface."""

from __future__ import annotations

from collections.abc import Iterable

from rich.text import Text
from textual.widgets import Static

from ouro.application.agent_models import AgentTreeNode
from ouro.tui.theme import TUI_DESIGN

_MAX_VISIBLE_CHILDREN = 10
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


class SubagentNavigationPanel(Static):
    """Render and navigate every agent in one controlled root Session tree."""

    def __init__(self) -> None:
        super().__init__(id="subagent-navigation-panel")
        self.display = False
        self._root_session_id: str | None = None
        self._root_status = "idle"
        self._agents: tuple[AgentTreeNode, ...] = ()
        self._current_session_id: str | None = None
        self._selected_session_id: str | None = None
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
    def selected_agent(self) -> AgentTreeNode | None:
        selected = self._selected_session_id
        return next((agent for agent in self._agents if agent.session_id == selected), None)

    @property
    def visible_session_ids(self) -> tuple[str, ...]:
        return tuple(agent.session_id for agent in self._visible_agents())

    def set_tree(
        self,
        *,
        root_session_id: str | None,
        agents: Iterable[AgentTreeNode],
        root_status: str,
    ) -> None:
        root_changed = root_session_id != self._root_session_id
        self._root_session_id = root_session_id
        self._root_status = root_status
        self._agents = tuple(
            agent
            for agent in agents
            if root_session_id is not None and agent.root_session_id == root_session_id
        )
        available = {agent.session_id for agent in self._agents}
        if root_changed or (
            self._current_session_id != root_session_id
            and self._current_session_id not in available
        ):
            self._current_session_id = root_session_id
        if (
            self._selected_session_id != root_session_id
            and self._selected_session_id not in available
        ):
            self._selected_session_id = self._current_session_id
        self.display = bool(self._agents)
        self._refresh_content()

    def set_current_session(self, session_id: str | None) -> None:
        available = {agent.session_id for agent in self._agents}
        if session_id != self._root_session_id and session_id not in available:
            return
        self._current_session_id = session_id
        if not self._navigation_active:
            self._selected_session_id = session_id
        self._refresh_content()

    def begin_navigation(self) -> None:
        if not self._agents:
            return
        if not self._navigation_active:
            self._selected_session_id = self._current_session_id or self._root_session_id
        self._navigation_active = True
        self._refresh_content()

    def end_navigation(self) -> None:
        self._navigation_active = False
        self._selected_session_id = self._current_session_id
        self._refresh_content()

    def select_relative(self, offset: int) -> str | None:
        if not self._agents or offset == 0:
            return self._selected_session_id
        child_ids = tuple(agent.session_id for agent in self._ordered_agents())
        selected = self._selected_session_id
        if selected == self._root_session_id or selected not in child_ids:
            index = 0 if offset > 0 else len(child_ids) - 1
        else:
            index = min(max(child_ids.index(selected) + offset, 0), len(child_ids) - 1)
        return self._select(child_ids[index])

    def select_root(self) -> str | None:
        return self._select(self._root_session_id)

    def select_latest_child(self) -> str | None:
        target = self._agents[-1].session_id if self._agents else self._root_session_id
        return self._select(target)

    def render(self) -> Text:
        width = self.content_region.width or self.size.width
        return self.render_content(max(1, width))

    def render_content(self, width: int) -> Text:
        suffix = self._node_text()
        detail = Text()
        selected_agent = self.selected_agent
        if selected_agent is not None:
            identity = selected_agent.nickname or selected_agent.agent_name
            detail.append(
                TUI_DESIGN.inline_separator.join((selected_agent.task_description, identity))
                + TUI_DESIGN.inline_separator,
                style=TUI_DESIGN.text_lowlight,
            )
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

    def _visible_agents(self) -> tuple[AgentTreeNode, ...]:
        ordered = self._ordered_agents()
        if len(ordered) <= _MAX_VISIBLE_CHILDREN:
            return ordered
        selected = self._selected_session_id
        if selected == self._root_session_id or selected is None:
            selected = self._agents[-1].session_id
        index = next(
            (index for index, agent in enumerate(ordered) if agent.session_id == selected),
            len(ordered) - 1,
        )
        max_start = len(ordered) - _MAX_VISIBLE_CHILDREN
        start = min(max(0, index - _MAX_VISIBLE_CHILDREN // 2), max_start)
        return ordered[start : start + _MAX_VISIBLE_CHILDREN]

    def _ordered_agents(self) -> tuple[AgentTreeNode, ...]:
        groups: dict[str, list[AgentTreeNode]] = {}
        for agent in self._agents:
            groups.setdefault(agent.parent_run_id, []).append(agent)
        return tuple(agent for group in groups.values() for agent in group)

    def _node_text(self) -> Text:
        selected = self._selected_session_id
        nodes = Text()
        root_mark = (
            TUI_DESIGN.selected_session_node_mark
            if selected == self._root_session_id
            else TUI_DESIGN.session_node_mark
        )
        nodes.append(root_mark, style=self._status_color(self._root_status))
        nodes.append(" | ", style=TUI_DESIGN.text_faint)
        previous_run_id: str | None = None
        for agent in self._visible_agents():
            if previous_run_id is not None and agent.parent_run_id != previous_run_id:
                nodes.append(" ")
            mark = (
                TUI_DESIGN.selected_session_node_mark
                if selected == agent.session_id
                else TUI_DESIGN.session_node_mark
            )
            nodes.append(mark, style=self._status_color(agent.status))
            previous_run_id = agent.parent_run_id
        return nodes

    def _status_color(self, status: str) -> str:
        if status in _FAILED_STATUSES:
            return TUI_DESIGN.failure
        if status in _RUNNING_STATUSES:
            return TUI_DESIGN.running
        return TUI_DESIGN.text_lowlight

    def _refresh_content(self) -> None:
        selected = self.selected_agent
        self.tooltip = (
            None
            if selected is None
            else TUI_DESIGN.inline_separator.join(
                (
                    selected.parent_run_id,
                    selected.execution_mode,
                    (
                        selected.activity.activity.replace("_", " ")
                        if selected.activity is not None
                        else selected.status.replace("_", " ")
                    ),
                )
            )
        )
        self.refresh(layout=True)


__all__ = ["SubagentNavigationPanel"]
