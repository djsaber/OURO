from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo, available_timezones

from pydantic import ValidationError
from tzlocal import get_localzone_name

from ouro.application.models import AvailableModel
from ouro.core.model_selection import DEFAULT_MODEL
from ouro.core.reasoning import ReasoningRequest, resolve_reasoning
from ouro.core.tasks import AgentTaskTarget, PluginTaskTarget, TaskConfiguration
from ouro.tui.controller import TUI_APPROVAL_PRESETS
from ouro.tui.inline_editor import MultilineEditableChoiceScreen
from ouro.tui.menu_labels import (
    MODEL_THINKING_LABEL,
    THINKING_LABELS,
    TOOL_APPROVAL_LABEL,
)
from ouro.tui.model_menus import (
    approval_choices,
    model_choices,
    model_selection_label,
    reasoning_choices,
    reasoning_request,
)
from ouro.tui.screens import Choice, ChoiceScreen
from ouro.tui.theme import theme_for

FREQUENCY_LABELS = {
    "hourly": "Every hour",
    "daily": "Every day",
    "weekdays": "Every weekday",
    "weekly": "Every week",
    "monthly": "Every month",
    "custom": "Custom",
}
TASK_STATUS_LABELS = {
    "waiting": "Waiting",
    "running": "Running",
    "paused": "Paused",
    "completed": "Completed",
    "failed": "Failed",
    "interrupted": "Interrupted",
}
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class TaskConfigurationScreen(MultilineEditableChoiceScreen):
    """Edit a draft; application validation and persistence occur only on Save."""

    def __init__(
        self,
        *,
        models: tuple[AvailableModel, ...],
        workspace: str | None,
        default_model: str | None = None,
        system_default_model: AvailableModel | None = None,
        configuration: TaskConfiguration | None = None,
    ) -> None:
        self._models = models
        self._system_default_model = system_default_model
        self._local_timezone = get_localzone_name()
        config = configuration
        target = config.target if config and isinstance(config.target, AgentTaskTarget) else None
        self._plugin_target = (
            config.target if config and isinstance(config.target, PluginTaskTarget) else None
        )
        schedule = config.schedule if config is not None else None
        timezone = schedule.timezone if schedule else self._local_timezone
        zone = ZoneInfo(timezone)
        self._values: dict[str, str] = {
            "title": config.title or "" if config else "",
            "description": target.prompt if target else "",
            "workspace": target.workspace if target else workspace or "",
            "model": (
                target.model
                if target
                else default_model or ""
                if default_model == DEFAULT_MODEL
                or any(model.key == default_model for model in models)
                else ""
            ),
            "approval": target.approval_mode if target else "afk",
            "session": target.session_mode if target else "new",
            "frequency": schedule.frequency if schedule else "daily",
            "timezone": timezone,
            "at": schedule.at.isoformat(timespec="minutes") if schedule else "09:00",
            "starts": (schedule.starts_at if schedule else datetime.now(UTC))
            .astimezone(zone)
            .replace(tzinfo=None)
            .isoformat(sep=" "),
            "ends": schedule.ends_at.astimezone(zone).isoformat()
            if schedule and schedule.ends_at
            else "",
            "interval": str(schedule.interval) if schedule else "1",
            "unit": schedule.unit if schedule else "days",
            "weekdays": ",".join(_DAYS[day] for day in schedule.weekdays) if schedule else "",
            "month_day": str(schedule.month_day) if schedule and schedule.month_day else "",
        }
        self._reasoning = target.reasoning if target else ReasoningRequest()
        if self._plugin_target is not None:
            self._values["arguments"] = json.dumps(
                self._plugin_target.arguments, ensure_ascii=False, indent=2
            )
        super().__init__(
            title="EDIT TASK" if config else "CREATE TASK",
            placeholder="Search task settings…",
            choices=self._task_choices(),
            on_save=self._save_field,
        )

    async def _select_choice(self, selected: str) -> None:
        if self._editing_value is not None:
            await super()._select_choice(selected)
            return
        if selected == "timezone":
            timezones = await asyncio.to_thread(available_timezones)
            timezones.update((self._local_timezone, self._values[selected]))
            value = await self.open_child(
                ChoiceScreen(
                    title="TASK TIME ZONE",
                    placeholder="Search by region or city…",
                    choices=tuple(
                        Choice(
                            name,
                            name,
                            description="System local time" if name == self._local_timezone else "",
                            marker=theme_for(self).selected_choice_marker
                            if name == self._values[selected]
                            else "",
                        )
                        for name in sorted(
                            timezones, key=lambda name: (name != self._local_timezone, name)
                        )
                    ),
                    initial_value=self._values[selected],
                )
            )
            if value is not None:
                self._values[selected] = value
        elif selected == "model":
            value = await self.open_child(
                ChoiceScreen(
                    title="TASK MODEL",
                    placeholder="Choose a model…",
                    choices=(
                        Choice(
                            DEFAULT_MODEL,
                            "Default model",
                            marker=theme_for(self).selected_choice_marker
                            if self._values[selected] == DEFAULT_MODEL
                            else "",
                            group="MODEL SOURCE",
                            disabled=self._system_default_model is None,
                        ),
                        *model_choices(
                            self._models, selected=self._values[selected], theme=theme_for(self)
                        ),
                    ),
                    initial_value=self._values[selected],
                )
            )
            if value is not None and value != self._values[selected]:
                self._values[selected] = value
                self._reasoning = ReasoningRequest()
        elif selected == "approval":
            value = await self.open_child(
                ChoiceScreen(
                    title=TOOL_APPROVAL_LABEL.upper(),
                    placeholder="Choose when to ask for approval…",
                    choices=approval_choices(
                        TUI_APPROVAL_PRESETS, selected=self._values[selected], theme=theme_for(self)
                    ),
                    initial_value=self._values[selected],
                )
            )
            if value is not None:
                self._values[selected] = value
        elif selected == "reasoning":
            model = self._selected_model()
            if model is None:
                return
            current = self._reasoning_value()
            choices = reasoning_choices(
                model.reasoning,
                selected=current,
                requested=self._reasoning,
                theme=theme_for(self),
            )
            if not choices:
                return
            value = await self.open_child(
                ChoiceScreen(
                    title=MODEL_THINKING_LABEL.upper(),
                    placeholder="Choose thinking settings…",
                    choices=choices,
                    initial_value=current,
                )
            )
            if value is not None:
                self._reasoning = reasoning_request(value, self._reasoning)
        elif selected in {"frequency", "session", "unit"}:
            labels = {
                "frequency": FREQUENCY_LABELS,
                "session": {"new": "Create a session each time", "reuse": "Reuse one session"},
                "unit": {
                    key: key.title() for key in ("minutes", "hours", "days", "weeks", "months")
                },
            }[selected]
            value = await self.open_child(
                ChoiceScreen(
                    title=f"TASK {selected.upper()}",
                    placeholder="Choose…",
                    choices=tuple(
                        Choice(
                            key,
                            label,
                            marker=theme_for(self).selected_choice_marker
                            if key == self._values[selected]
                            else "",
                        )
                        for key, label in labels.items()
                    ),
                    initial_value=self._values[selected],
                )
            )
            if value is not None:
                self._values[selected] = value
        elif selected == "save":
            try:
                self.dismiss(self.draft())  # type: ignore[arg-type]
            except ValueError as exc:
                self.notify(_error_message(exc), severity="error", markup=False)
            return
        else:
            await super()._select_choice(selected)
            return
        self._replace_choices(self._task_choices(), selected=selected)

    async def _save_field(self, field_id: str, value: str) -> tuple[Choice, ...]:
        self._values[field_id] = value.strip()
        return self._task_choices()

    def reasoning_options(self) -> dict[str, ReasoningRequest]:
        model = self._selected_model()
        if model is None:
            return {}
        return {
            choice.value: reasoning_request(choice.value, self._reasoning)
            for choice in reasoning_choices(
                model.reasoning,
                selected=self._reasoning_value(),
                requested=self._reasoning,
                theme=theme_for(self),
            )
        }

    def _reasoning_value(self) -> str:
        model = self._selected_model()
        if model is not None:
            try:
                return resolve_reasoning(model.reasoning, self._reasoning).effort_variant
            except ValueError:
                # Keep invalid saved settings editable after model capabilities
                # change. Draft validation still prevents saving or execution.
                pass
        return self._reasoning.effort or self._reasoning.mode

    def draft(self) -> TaskConfiguration:
        values = self._values
        zone = ZoneInfo(values["timezone"])
        frequency = values["frequency"]
        weekdays: tuple[int, ...] = ()
        calendar_weekdays = frequency == "weekly" or (
            frequency == "custom" and values["unit"] in {"days", "weeks", "months"}
        )
        if calendar_weekdays and values["weekdays"].strip():
            try:
                weekdays = tuple(
                    _DAYS.index(day.strip().title()) for day in values["weekdays"].split(",")
                )
            except ValueError as exc:
                raise ValueError("Weekdays: use Mon,Tue,Wed,Thu,Fri,Sat,Sun") from exc
        monthly = frequency == "monthly" or (frequency == "custom" and values["unit"] == "months")
        config = TaskConfiguration.model_validate(
            {
                "title": values["title"] or None,
                "target": self._plugin_target.model_copy(
                    update={"arguments": json.loads(values["arguments"])}
                )
                if self._plugin_target
                else {
                    "kind": "agent",
                    "prompt": values["description"],
                    "workspace": values["workspace"],
                    "model": values["model"],
                    "approval_mode": values["approval"],
                    "session_mode": values["session"],
                    "reasoning": self._reasoning,
                },
                "schedule": {
                    "frequency": frequency,
                    "timezone": values["timezone"],
                    "starts_at": _local_datetime(values["starts"], zone),
                    "ends_at": _local_datetime(values["ends"], zone, end=True)
                    if values["ends"]
                    else None,
                    "at": time.fromisoformat(values["at"]),
                    "interval": int(values["interval"]) if frequency == "custom" else 1,
                    "unit": values["unit"] if frequency == "custom" else "days",
                    "weekdays": weekdays,
                    "month_day": int(values["month_day"])
                    if monthly and values["month_day"]
                    else None,
                },
            }
        )
        if isinstance(config.target, PluginTaskTarget):
            return config
        model = self._selected_model()
        if model is None:
            raise ValueError(
                "System default endpoint is not configured"
                if config.target.model == DEFAULT_MODEL
                else "Select an available model"
            )
        resolve_reasoning(model.reasoning, config.target.reasoning)
        return config

    def _task_choices(self) -> tuple[Choice, ...]:
        values = self._values
        rows: list[Choice] = []
        fields: tuple[tuple[str, str, str], ...] = (
            ("title", "Title", "TASK"),
            ("description", "Instruction", "TASK"),
            ("workspace", "Workspace", "EXECUTION"),
            ("approval", "Approval", "EXECUTION"),
            ("model", "Model", "EXECUTION"),
            ("reasoning", MODEL_THINKING_LABEL, "EXECUTION"),
            ("session", "Session creation", "EXECUTION"),
            ("frequency", "Schedule", "SCHEDULE"),
            ("timezone", "Time zone", "SCHEDULE"),
            ("at", "Time (HH:MM; hourly uses minute)", "SCHEDULE"),
            ("starts", "Start (local date and time)", "SCHEDULE"),
            ("ends", "End (optional date or time)", "SCHEDULE"),
        )
        if self._plugin_target is not None:
            fields = (
                ("title", "Title", "TASK"),
                ("arguments", "Arguments (JSON)", "TASK"),
                *(field for field in fields if field[2] == "SCHEDULE"),
            )
            rows.append(
                Choice(
                    "target",
                    "Service",
                    description=f"{self._plugin_target.plugin_id}/{self._plugin_target.service}/{self._plugin_target.method}",
                    group="TASK",
                    disabled=True,
                )
            )
        if values["frequency"] == "custom" and values["unit"] in {"minutes", "hours"}:
            fields = tuple(field for field in fields if field[0] != "at")
        if values["frequency"] == "custom":
            fields += (
                ("interval", "Repeat every", "SCHEDULE"),
                ("unit", "Repeat unit", "SCHEDULE"),
            )
        if values["frequency"] == "weekly" or (
            values["frequency"] == "custom" and values["unit"] in {"days", "weeks", "months"}
        ):
            fields += (("weekdays", "Weekdays (Mon,Tue,…)", "SCHEDULE"),)
        if values["frequency"] == "monthly" or (
            values["frequency"] == "custom" and values["unit"] == "months"
        ):
            fields += (("month_day", "Day of month (1–31)", "SCHEDULE"),)
        pickers = {
            "approval",
            "model",
            "reasoning",
            "session",
            "frequency",
            "timezone",
            "unit",
        }
        for key, label, group in fields:
            value = values.get(key, "")
            display = (
                value.split("\n", 1)[0] if key == "description" else " ".join(value.split())
            )[:80] or ("Required" if key in {"description", "workspace", "model"} else "Default")
            if key == "approval":
                display = value
            elif key == "model" and value:
                display = (
                    "Default model"
                    if value == DEFAULT_MODEL
                    else model_selection_label(value, self._models, theme=theme_for(self))
                )
            elif key == "frequency":
                display = FREQUENCY_LABELS[value]
            elif key == "reasoning":
                display = THINKING_LABELS[self._reasoning_value()]
            elif key == "session":
                display = "New each time" if value == "new" else "Reuse one session"
            rows.append(
                Choice(
                    key,
                    label,
                    trailing=display,
                    group=group,
                    edit_value=value if key not in pickers else None,
                    edit_placeholder=f"Enter {label.lower()}",
                    edit_lines=3 if key in {"description", "arguments"} else 1,
                    disabled=key == "reasoning" and not self.reasoning_options(),
                )
            )
        try:
            self.draft()
            error = ""
        except (ValueError, KeyError) as exc:
            error = _error_message(exc)
        rows.append(
            Choice("save", "Save", description=error, group="ACTIONS", disabled=bool(error))
        )
        return tuple(rows)

    def _selected_model(self) -> AvailableModel | None:
        key = self._values["model"]
        if key == DEFAULT_MODEL:
            return self._system_default_model
        return next((model for model in self._models if model.key == key), None)


def _local_datetime(value: str, zone: ZoneInfo, *, end: bool = False) -> datetime:
    parsed = datetime.fromisoformat(value)
    if end and len(value) == 10:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    if parsed.tzinfo is not None:
        return parsed
    candidate = parsed.replace(tzinfo=zone, fold=0)
    if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != parsed:
        raise ValueError("This local time does not exist in the selected time zone")
    return candidate


def _error_message(error: Exception) -> str:
    if isinstance(error, ValidationError):
        first = error.errors()[0]
        return f"{'.'.join(str(part) for part in first['loc'])}: {first['msg']}"
    return str(error)
