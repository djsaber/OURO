"""Shared TUI test fixtures built outside the single-adapter faux default."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event

from rich.text import Text
from textual.app import App
from textual.geometry import Size
from textual.widgets import Input

from ouro.application.contracts.models import AvailableModel
from ouro.application.models.catalog import close_runtime_adapters
from ouro.application.runtime.environment import RuntimeEnvironment
from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import ConfigError
from ouro.core.provider import ModelMetadata
from ouro.local_application import LocalApplication
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry
from ouro.tui.composer.popup import CommandPopup
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.menus.choice import ChoiceScreen


def surface_state(app: App) -> str:
    """Keep timeout failures useful without dumping an entire transcript."""

    workers = [(worker.name, worker.group, worker.state.name) for worker in app.workers]
    focused = app.focused
    projection = getattr(app, "_projection", None)
    activity = getattr(projection, "activity", None)
    pending = [item.tool_call_id for item in getattr(projection, "pending_interactions", ())]
    bound = getattr(focused, "_interaction", None)
    return (
        f"page={getattr(app.screen, 'page_title', type(app.screen).__name__)!r}, "
        f"focus={focused!r}, pending={pending!r}, "
        f"session={getattr(projection, 'session_id', None)!r}, "
        f"status={getattr(activity, 'status', None)!r}, "
        f"activity={getattr(activity, 'activity', None)!r}, "
        f"bound={getattr(bound, 'tool_call_id', None)!r}, "
        f"disabled={getattr(focused, 'disabled', None)!r}, workers={workers!r}"
    )


async def wait_until(
    predicate: Callable[[], bool],
    pilot,
    *,
    description: str,
    timeout: float = 15.0,
) -> None:
    """Wait for a business outcome; CPU idleness does not imply completed I/O."""

    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await pilot._wait_for_screen()
                await asyncio.sleep(0.01)
            await pilot._wait_for_screen()
    except TimeoutError:
        raise AssertionError(
            f"Timed out waiting for {description}; {surface_state(pilot.app)}"
        ) from None


async def wait_for_events(*events: asyncio.Event, pilot, description: str) -> None:
    """Await producer handshakes without coupling disk I/O to UI polling.

    These events hold a scenario at a known phase until the test releases it.
    The deadline is a deadlock watchdog, not a model/startup performance budget.
    """

    try:
        async with asyncio.timeout(60):
            await asyncio.gather(*(event.wait() for event in events))
    except TimeoutError:
        raise AssertionError(
            f"Timed out waiting for {description}; "
            f"events={[event.is_set() for event in events]}; {surface_state(pilot.app)}"
        ) from None


async def wait_for_screen(
    app: App,
    screen_type: type[ChoiceScreen],
    *,
    title: str,
    timeout: float = 15.0,
) -> None:
    """Wait for the requested page, including mount and its first refresh.

    Returning from an editor restores its parent before persistence completes.
    Use wait_for_menu_action on the parent before asserting a save's outcome.
    """

    try:
        async with asyncio.timeout(timeout):
            while True:
                screen = app.screen
                if (
                    isinstance(screen, screen_type)
                    and screen.page_title == title
                    and screen.is_mounted
                    and len(screen.query("#choice-filter")) == 1
                ):
                    await screen.wait_for_refresh()
                    if app.screen is screen and screen.page_title == title:
                        return
                await asyncio.sleep(0.01)
    except TimeoutError:
        raise AssertionError(f"Timed out waiting for {title}; {surface_state(app)}") from None


async def wait_for_menu_action(pilot, menu: ChoiceScreen, *, timeout: float = 15.0) -> None:
    """Await the parent menu's selection, including child results and persistence.

    Call after submitting or dismissing its child, not while the child still
    needs input. Wait only for this menu's action; session observers and other
    long-lived application workers deliberately remain running.
    """

    await wait_for_worker_group(
        pilot.app, pilot, group=f"choice-selection-{id(menu)}", timeout=timeout
    )


async def wait_for_worker_group(app: App, pilot, *, group: str, timeout: float = 15.0) -> None:
    """Await one finite operation after its initiating UI messages have drained."""

    try:
        async with asyncio.timeout(timeout):
            await pilot._wait_for_screen()
            workers = tuple(worker for worker in app.workers if worker.group == group)
            await asyncio.gather(*(worker.wait() for worker in workers))
            await pilot._wait_for_screen()
    except TimeoutError:
        raise AssertionError(
            f"Timed out waiting for worker group {group!r}; {surface_state(app)}"
        ) from None


@asynccontextmanager
async def release_on_exit(*events: asyncio.Event | Event):
    """Release test-owned gates before the enclosing application's shutdown."""

    try:
        yield
    finally:
        for event in events:
            event.set()


async def wait_for_history_render(pilot) -> None:
    """Observe the deferred layout after content has been installed.

    Session activation and load_messages commit entries before rendering them.
    Do not call a render method here: a missing scheduled render should fail.
    """
    history = pilot.app.query_one(ConversationHistory)
    await wait_until(
        lambda: history.render_complete,
        pilot,
        description="installed conversation history to finish rendering",
    )


async def resize_terminal(pilot, width: int, height: int, *, timeout: float = 15.0) -> None:
    """Await applied screen geometry before reading coordinates or clicking.

    Pilot updates the driver's size before Textual dispatches the deferred
    Resize event. Screen.size follows that driver immediately; Screen.region
    comes from the compositor and identifies the layout actually applied.
    """
    size = Size(width, height)
    try:
        async with asyncio.timeout(timeout):
            await pilot.resize_terminal(width, height)
            screen = pilot.app.screen
            while screen.region.size != size:
                await screen.wait_for_refresh()
            # Resize handlers may invalidate child layout after screen reflow.
            await screen.wait_for_refresh()
    except TimeoutError:
        raise AssertionError(
            f"Timed out applying terminal size {size!r}; "
            f"layout={pilot.app.screen.region!r}; {surface_state(pilot.app)}"
        ) from None


async def choose_menu(pilot, label: str, *, title: str | None = None) -> None:
    """Filter and accept a choice, optionally awaiting the resulting page.

    A reused ChoiceScreen can still show the old page while selection I/O runs.
    Supply its destination title, or explicitly await the business outcome at
    the call site; draining UI messages alone does not complete that operation.
    """

    pilot.app.screen.query_one("#choice-filter", Input).value = label
    await pilot.pause()
    await pilot.press("enter")
    if title is not None:
        await wait_for_screen(pilot.app, ChoiceScreen, title=title)
    else:
        await pilot._wait_for_screen()


def command_popup_text(popup: CommandPopup) -> Text:
    """Inspect logical menu rows through the same per-line renderer as the screen."""
    rows = (
        Text.assemble(*((segment.text, segment.style) for segment in popup._content.render_line(y)))
        for y in range(popup._content.row_count)
    )
    return Text("\n").join(rows)


@asynccontextmanager
async def run_ready_app(app, **options):
    """Start a test Host before interacting with its surface.

    Textual's mount/idle boundary does not await application startup I/O. Only
    wait for that worker; live session observers deliberately remain running.
    Tests of startup itself should continue to use App.run_test directly.
    """

    async with app.run_test(**options) as pilot:
        for worker in tuple(app.workers):
            if worker.name == "tui-start":
                await worker.wait()
        await pilot.pause()
        yield pilot


class StaticRouteCatalog:
    """Protocol-compliant catalog over fixed faux routes for interface tests."""

    def __init__(
        self,
        adapters: dict[str, RuntimeAdapter],
        *,
        default_key: str,
    ) -> None:
        self._adapters = adapters
        self._default_key = default_key
        self._models = {
            key: AvailableModel(
                key=key,
                provider_id=adapter.model.provider_id,
                endpoint_id=adapter.model.endpoint_id or adapter.model.model_id,
                model_id=adapter.model.model_id,
                context_window=adapter.model.context_window,
                max_output_tokens=adapter.model.max_output_tokens,
                reasoning=adapter.model.reasoning,
            )
            for key, adapter in adapters.items()
        }

    def list_models(self) -> list[AvailableModel]:
        return sorted(self._models.values(), key=lambda item: item.key)

    def default_model(self) -> AvailableModel:
        return self._models[self._default_key]

    def resolve(self, selector: str) -> AvailableModel:
        normalized = selector.strip()
        if normalized not in self._models:
            raise ConfigError(f"model not found: {selector}")
        return self._models[normalized]

    def adapter_for(self, selector: str) -> RuntimeAdapter:
        return self._adapters[self.resolve(selector).key]

    async def close(self) -> None:
        await close_runtime_adapters(tuple(self._adapters.values()))


def two_route_application(
    tmp_path: Path,
    *,
    provider_id: str = "faux",
    default_endpoint_id: str = "default",
    recent_endpoint_id: str = "recent",
) -> LocalApplication:
    """One faux runtime exposing a default and a separately selectable route."""

    default_adapter = RuntimeAdapter(
        FauxProvider(),
        ModelMetadata(
            model_id="default-model",
            provider_id=provider_id,
            endpoint_id=default_endpoint_id,
        ),
    )
    recent_adapter = RuntimeAdapter(
        FauxProvider(),
        ModelMetadata(
            model_id="recent-model",
            provider_id=provider_id,
            endpoint_id=recent_endpoint_id,
        ),
    )
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=default_adapter,
        model_catalog=StaticRouteCatalog(
            {
                f"{provider_id}/{default_endpoint_id}": default_adapter,
                f"{provider_id}/{recent_endpoint_id}": recent_adapter,
            },
            default_key=f"{provider_id}/{default_endpoint_id}",
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)
