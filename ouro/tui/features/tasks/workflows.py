from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import cast
from zoneinfo import ZoneInfo

from ouro.core.model_selection import DEFAULT_MODEL
from ouro.core.tasks import AgentTaskRun, PluginTaskTarget, TaskConfiguration, TaskRecord
from ouro.tui.features.plugins.workflows import PluginManagementWorkflows
from ouro.tui.features.tasks.screens import (
    FREQUENCY_LABELS,
    TASK_STATUS_LABELS,
    TaskConfigurationScreen,
)
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.confirmation import ConfirmationScreen
from ouro.tui.menus.models import Choice
from ouro.tui.theme import TuiTheme, theme_for


class TaskManagementWorkflows(PluginManagementWorkflows):
    """Expose task configuration and background sessions through the shared menu."""

    _open_session: Callable[[str], Awaitable[None]]
    _sync_task_status: Callable[[], Awaitable[None]]

    async def _choose_tasks(
        self, *, menu: ChoiceScreen | None = None, switch: bool = False
    ) -> None:
        revision = menu.page_revision if menu is not None else None
        task_menu: ChoiceScreen
        offset = 0

        async def choices() -> tuple[Choice, ...]:
            records = await self._controller.tasks(offset=offset)
            rows = [
                Choice(
                    task.task_id,
                    task.configuration.display_title,
                    description=_task_schedule_label(task, theme_for(self)),
                    trailing=TASK_STATUS_LABELS[task.status],
                    group="TASKS",
                    accent=None
                    if task.status in {"waiting", "running"}
                    else theme_for(self).text_lowlight,
                )
                for task in records
            ]
            if not rows:
                rows.append(Choice("empty", "No tasks yet", disabled=True))
            if offset:
                rows.append(Choice("previous", "Previous page", group="PAGES"))
            if len(records) == 100:
                rows.append(Choice("next", "Next page", group="PAGES"))
            return tuple(rows)

        async def select(value: str) -> tuple[Choice, ...] | None:
            nonlocal offset
            try:
                if value in {"next", "previous"}:
                    offset += 100 if value == "next" else -100
                else:
                    await self._manage_task(value, menu=task_menu)
                    return None
                return await choices()
            except Exception as exc:
                self.notify(str(exc), severity="error", markup=False)
                return None

        try:
            rows = await choices()
        except Exception as exc:
            self.notify(str(exc), severity="error", markup=False)
            return
        if menu is not None:
            if not menu.is_attached or menu.page_revision != revision:
                return
            task_menu = menu
            navigate = menu.switch_page if switch else menu.push_page
            navigate(
                navigation_id="tasks",
                retain_for_switching=True,
                title="Tasks",
                placeholder="Filter tasks…",
                choices=rows,
                on_select=select,
                on_refresh=choices,
            )
        else:
            task_menu = ChoiceScreen(
                navigation_id="tasks",
                retain_for_switching=True,
                title="Tasks",
                placeholder="Filter tasks…",
                choices=rows,
                on_select=select,
                on_refresh=choices,
            )
            self.push_screen(task_menu)

    async def _edit_task(
        self, task: TaskRecord | None, *, menu: ChoiceScreen | None = None
    ) -> None:
        try:
            plugin_task = task is not None and isinstance(
                task.configuration.target, PluginTaskTarget
            )
            models = () if plugin_task else await self._controller.models()
            system_default_model = (
                None if plugin_task else await self._controller.configured_default_model()
            )
            screen = TaskConfigurationScreen(
                models=models,
                workspace=self._controller.draft_workspace,
                default_model=(
                    DEFAULT_MODEL
                    if system_default_model is not None
                    else (await self._controller.default_model()).key
                )
                if task is None
                else None,
                system_default_model=system_default_model,
                configuration=task.configuration if task else None,
            )
            draft = await (
                menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
            )
            if draft is None:
                return
            await self._controller.save_task(cast(TaskConfiguration, draft), existing=task)
            self.notify("Task saved")
        except Exception as exc:
            self.notify(str(exc), severity="error", markup=False)

    async def _delete_task(self, task: TaskRecord, *, menu: ChoiceScreen) -> bool:
        result = await menu.open_child(
            ConfirmationScreen(
                title="DELETE TASK",
                message=f"Delete {task.configuration.display_title}? Sessions remain available.",
                confirm_label="Delete",
            )
        )
        if not ConfirmationScreen.confirmed(result):
            return False
        await self._controller.delete_task(task)
        await self._sync_task_status()
        return True

    async def _manage_task(self, task_id: str, *, menu: ChoiceScreen) -> None:
        task = await self._controller.task(task_id)

        async def choices() -> tuple[Choice, ...]:
            nonlocal task
            task = await self._controller.task(task_id)
            return (
                Choice(
                    "status",
                    TASK_STATUS_LABELS[task.status],
                    description=task.error or _task_schedule_label(task, theme_for(self)),
                    group="STATUS",
                    disabled=True,
                ),
                Choice("history", "History", group="STATUS"),
                Choice("edit", "Edit", group="CONFIGURATION"),
                Choice(
                    "toggle",
                    "Pause" if task.enabled else "Enable",
                    group="CONFIGURATION",
                ),
                Choice(
                    "delete",
                    "Delete",
                    accent=theme_for(self).danger,
                    disabled=task.active_execution_id is not None,
                    destructive=True,
                    group="CONFIGURATION",
                ),
                Choice(
                    "interrupt",
                    "Interrupt",
                    disabled=task.active_execution_id is None,
                    group="ACTIONS",
                ),
                Choice("refresh", "Refresh", group="ACTIONS"),
            )

        async def select(value: str) -> tuple[Choice, ...] | None:
            try:
                if value == "edit":
                    await self._edit_task(task, menu=menu)
                elif value == "toggle":
                    await self._controller.set_task_enabled(task, not task.enabled)
                elif value == "interrupt":
                    await self._controller.interrupt_task(task)
                elif value == "history":
                    await self._task_history(task_id, menu=menu)
                    return None
                elif value == "delete":
                    if await self._delete_task(task, menu=menu):
                        await menu.pop_page_refreshed()
                        return None
                return await choices()
            except Exception as exc:
                self.notify(str(exc), severity="error", markup=False)
                return None

        menu.push_page(
            title=theme_for(self).section_separator.join(
                ("TASK", task.configuration.display_title)
            ),
            placeholder="Choose an action…",
            choices=await choices(),
            on_select=select,
            on_refresh=choices,
        )

    async def _task_history(self, task_id: str, *, menu: ChoiceScreen) -> None:
        async def choices() -> tuple[Choice, ...]:
            executions = await self._controller.task_executions(task_id)
            return tuple(
                Choice(
                    item.execution_id,
                    item.scheduled_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                    trailing=TASK_STATUS_LABELS[item.status],
                    description=item.error
                    or ("Open session" if isinstance(item.target, AgentTaskRun) else "View result"),
                )
                for item in executions
            ) or (Choice("empty", "No executions yet", disabled=True),)

        async def select(execution_id: str) -> tuple[Choice, ...] | None:
            executions = await self._controller.task_executions(task_id)
            execution = next(
                (item for item in executions if item.execution_id == execution_id), None
            )
            if execution is None:
                return await choices()
            if not isinstance(execution.target, AgentTaskRun):
                request = execution.target.request
                menu.push_page(
                    title="TASK EXECUTION",
                    placeholder="Execution details",
                    choices=(
                        Choice(
                            "service",
                            "Service",
                            description=f"{request.plugin_id}/{request.service}/{request.method}",
                            disabled=True,
                        ),
                        Choice(
                            "status",
                            "Status",
                            description=TASK_STATUS_LABELS[execution.status],
                            disabled=True,
                        ),
                        Choice(
                            "result",
                            "Result",
                            description=execution.error
                            or json.dumps(execution.result, ensure_ascii=False),
                            disabled=True,
                        ),
                    ),
                )
                return None
            menu.dismiss(None)
            try:
                await self._open_session(execution.target.session_id)
            except Exception as exc:
                self.notify(str(exc), severity="error", markup=False)
            return None

        menu.push_page(
            title=theme_for(self).section_separator.join(("TASK EXECUTIONS", "LATEST 50")),
            placeholder="Filter executions…",
            choices=await choices(),
            on_select=select,
            on_refresh=choices,
        )


def _task_schedule_label(task: TaskRecord, theme: TuiTheme) -> str:
    schedule = task.configuration.schedule
    label = FREQUENCY_LABELS[schedule.frequency]
    if schedule.frequency == "custom":
        label = f"Every {schedule.interval} {schedule.unit}"
    if not task.enabled:
        return theme.inline_separator.join((label, "Schedule paused"))
    if task.next_run_at is None:
        return theme.inline_separator.join((label, "Schedule ended"))
    when = task.next_run_at.astimezone(ZoneInfo(schedule.timezone)).strftime("%Y-%m-%d %H:%M")
    return theme.inline_separator.join((label, f"Next {when} ({schedule.timezone})"))
