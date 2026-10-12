from __future__ import annotations

import asyncio
import webbrowser
from collections.abc import Mapping

from textual import events, on
from textual.worker import Worker

from ouro.application.provider_auth import AuthError, ProviderAuthService
from ouro.application.provider_auth_models import LoginChallenge, LoginOperation
from ouro.core.cancellation import settle_owned_tasks
from ouro.tui.screens import Choice, ChoiceScreen, ConfirmationScreen
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for


def _provider_label(provider_id: str, names: Mapping[str, str]) -> str:
    return names.get(provider_id, "Unavailable provider")


def _bound_provider_labels(provider_ids: tuple[str, ...], names: Mapping[str, str]) -> str:
    return ", ".join(_provider_label(provider_id, names) for provider_id in provider_ids)


class ProviderAuthenticationScreen(ChoiceScreen):
    """Present application-owned status and methods without inspecting credentials."""

    def __init__(
        self,
        service: ProviderAuthService,
        provider_id: str,
        *,
        provider_names: Mapping[str, str],
        theme: TuiTheme = DEFAULT_TUI_THEME,
    ) -> None:
        self._initial_tui_theme = theme
        self._service = service
        self._provider_id = provider_id
        self._provider_names = provider_names
        super().__init__(
            title=theme.inline_separator.join(
                ("AUTHENTICATION", _provider_label(provider_id, provider_names))
            ),
            placeholder="Choose an authentication action…",
            choices=(),
            on_select=self._select_authentication,
            on_refresh=self._authentication_choices,
        )

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        _worker: Worker[None] = self.run_worker(
            self._refresh_authentication, exclusive=True, group="auth-status"
        )

    async def _refresh_authentication(self) -> None:
        self.update_choices(await self._authentication_choices())

    async def _authentication_choices(self) -> tuple[Choice, ...]:
        try:
            status = await self._service.status(self._provider_id)
            methods = await self._service.methods(self._provider_id)
        except AuthError as exc:
            return (
                Choice("status", "Authentication unavailable", trailing=str(exc), disabled=True),
                Choice("refresh", "Refresh"),
                Choice("credentials", "Manage local credentials"),
            )
        return (
            Choice(
                "status",
                "Status",
                trailing=status.state,
                description=status.error_code or "",
                disabled=True,
            ),
            Choice(
                "references",
                "Shared by",
                trailing=_bound_provider_labels(status.bound_provider_ids, self._provider_names)
                or "—",
                disabled=True,
            ),
            *(
                Choice(
                    f"login:{method.method_id}",
                    method.label,
                    description=method.description or "",
                    group="SIGN IN",
                )
                for method in methods
            ),
            Choice("refresh", "Refresh status"),
            Choice("logout", "Sign out", disabled=status.state == "static", destructive=True),
            Choice("credentials", "Manage local credentials"),
        )

    async def _select_authentication(self, selected: str) -> tuple[Choice, ...] | None:
        try:
            if selected.startswith("login:"):
                self.show_child(
                    ProviderLoginScreen(
                        self._service,
                        self._provider_id,
                        selected.removeprefix("login:"),
                        provider_name=_provider_label(self._provider_id, self._provider_names),
                        theme=theme_for(self),
                    )
                )
                return None
            if selected == "credentials":
                self.show_child(
                    ProviderCredentialsScreen(self._service, provider_names=self._provider_names)
                )
                return None
            if selected == "logout":
                status = await self._service.status(self._provider_id)
                confirmed = await self.open_child(
                    ConfirmationScreen(
                        title="SIGN OUT",
                        message="Clear local authentication shared by: "
                        + (
                            _bound_provider_labels(status.bound_provider_ids, self._provider_names)
                            or _provider_label(self._provider_id, self._provider_names)
                        ),
                    )
                )
                if ConfirmationScreen.confirmed(confirmed):
                    await self._service.logout(self._provider_id)
            return await self._authentication_choices()
        except AuthError as exc:
            self.notify(str(exc), severity="error")
            return await self._authentication_choices()


class ProviderLoginScreen(ChoiceScreen):
    """Own one UI waiter; the application owns authorization and its durable outcome."""

    def __init__(
        self,
        service: ProviderAuthService,
        provider_id: str,
        method_id: str,
        *,
        provider_name: str,
        theme: TuiTheme = DEFAULT_TUI_THEME,
    ) -> None:
        self._initial_tui_theme = theme
        self._service = service
        self._provider_id = provider_id
        self._method_id = method_id
        self._login: LoginChallenge | None = None
        self._login_task: asyncio.Task[None] | None = None
        self.result: LoginOperation | None = None
        super().__init__(
            title=theme.inline_separator.join(("SIGN IN", provider_name)),
            placeholder="Authorization actions…",
            choices=(
                Choice("waiting", "Starting authorization…", loading=True, disabled=True),
                Choice("cancel", "Cancel sign-in"),
            ),
            on_select=self._select_login_action,
        )

    @on(events.Mount)
    def _start_login(self) -> None:
        self._login_task = asyncio.create_task(self._authorize())

    async def _authorize(self) -> None:
        try:
            self._login = await self._service.begin(self._provider_id, self._method_id)
            self._show_login_choices(self._challenge_choices())
            self.result = await self._service.wait(self._login.operation_id)
        except asyncio.CancelledError:
            # begin() already settles cancellation before it exposes an operation.
            # Once exposed, cancel explicitly and preserve a save that won the race.
            if self._login is not None:
                self.result = await self._service.cancel(self._login.operation_id)
        except AuthError as exc:
            self._show_login_choices(
                (
                    Choice("result", "Sign-in failed", trailing=str(exc), disabled=True),
                    Choice("close", "Close"),
                )
            )
            return
        finally:
            # URLs and device codes belong only to the live challenge surface.
            self._login = None
        result = self.result
        self._show_login_choices(
            (
                Choice(
                    "result",
                    f"Sign-in {result.state}" if result else "Sign-in cancelled",
                    description=(result.message or "") if result else "",
                    disabled=True,
                ),
                Choice("close", "Close"),
            )
        )

    def _show_login_choices(self, choices: tuple[Choice, ...]) -> None:
        if self._accepts_choice_updates():
            # A filter for an earlier action must not hide the new challenge or result.
            self.replace_page(
                title=self._title,
                placeholder="Authorization actions…",
                choices=choices,
                on_select=self._select_login_action,
            )

    def _challenge_choices(self) -> tuple[Choice, ...]:
        assert self._login is not None
        challenge = self._login.challenge
        code = (
            (Choice("code", "Device code", trailing=challenge.user_code, disabled=True),)
            if challenge.kind == "device_code"
            else ()
        )
        return (
            Choice("waiting", "Waiting for authorization…", loading=True, disabled=True),
            Choice(
                "url", "Authorization URL", description=self._authorization_url(), disabled=True
            ),
            *code,
            Choice("expires", "Expires", trailing=challenge.expires_at.isoformat(), disabled=True),
            *(
                (
                    Choice(
                        "instructions",
                        "Instructions",
                        description=challenge.instructions,
                        disabled=True,
                    ),
                )
                if challenge.instructions
                else ()
            ),
            Choice("open", "Open browser"),
            Choice("copy", "Copy authorization URL"),
            Choice("cancel", "Cancel sign-in"),
        )

    def _authorization_url(self) -> str:
        assert self._login is not None
        challenge = self._login.challenge
        return str(
            challenge.authorization_url
            if challenge.kind == "browser_callback"
            else challenge.verification_uri_complete or challenge.verification_uri
        )

    async def _select_login_action(self, selected: str) -> None:
        if selected == "close":
            self.dismiss(None)
        elif selected == "cancel":
            await self._cancel_login()
        elif self._login is not None:
            if selected == "copy":
                self.app.copy_to_clipboard(self._authorization_url())
            elif selected == "open":
                try:
                    opened = await asyncio.to_thread(
                        webbrowser.open, self._authorization_url(), new=2
                    )
                except OSError, webbrowser.Error:
                    opened = False
                if not opened:
                    self.notify(
                        "Could not open a browser. Copy the authorization URL instead.",
                        severity="warning",
                    )

    async def _cancel_login(self) -> None:
        task = self._login_task
        if task is not None:
            if not task.done() and not task.cancelling():
                task.cancel()
            await settle_owned_tasks(task)
            if not task.cancelled():
                task.result()

    async def action_cancel(self) -> None:
        if self._login_task is not None and not self._login_task.done():
            await self._cancel_login()
        else:
            self.dismiss(None)

    @on(events.Unmount)
    async def _finish_login(self) -> None:
        await self._cancel_login()


class ProviderCredentialsScreen(ChoiceScreen):
    """Manage safe credential summaries, including references orphaned by provider removal."""

    def __init__(self, service: ProviderAuthService, *, provider_names: Mapping[str, str]) -> None:
        self._service = service
        self._provider_names = provider_names
        super().__init__(
            title="LOCAL CREDENTIALS",
            placeholder="Filter credentials…",
            choices=(),
            on_select=self._forget_credential,
            on_refresh=self._credential_choices,
        )

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        _worker: Worker[None] = self.run_worker(
            self._refresh_credentials, exclusive=True, group="auth-credentials"
        )

    async def _refresh_credentials(self) -> None:
        self.update_choices(await self._credential_choices())

    async def _credential_choices(self) -> tuple[Choice, ...]:
        try:
            credentials = await self._service.credentials()
        except AuthError as exc:
            return (Choice("error", str(exc), disabled=True),)
        return tuple(
            Choice(
                view.credential.credential_id,
                view.credential.credential_id,
                trailing=view.credential.state,
                description="Shared by: "
                + (_bound_provider_labels(view.bound_provider_ids, self._provider_names) or "none"),
            )
            for view in credentials
        ) or (Choice("empty", "No local credentials", disabled=True),)

    async def _forget_credential(self, credential_id: str) -> tuple[Choice, ...]:
        confirmed = await self.open_child(
            ConfirmationScreen(
                title="CLEAR LOCAL CREDENTIAL",
                message=f"Clear {credential_id} for all bound providers?",
            )
        )
        if ConfirmationScreen.confirmed(confirmed):
            try:
                await self._service.forget(credential_id)
            except AuthError as exc:
                self.notify(str(exc), severity="error")
        return await self._credential_choices()
