from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest
from textual.app import App
from textual.widgets import Input, OptionList

from ouro.application.configuration import ConfigurationFacade
from ouro.auth.authorization import TaskAuthorizationAttempt
from ouro.auth.contracts import AuthError
from ouro.local_application import LocalApplication
from ouro.tui.app import OuroTuiApp
from ouro.tui.provider_auth_screens import (
    ProviderAuthenticationScreen,
    ProviderCredentialsScreen,
    ProviderLoginScreen,
)
from ouro.tui.provider_screens import ProviderConfigurationScreen, profile_for_provider
from ouro.tui.screens import ConfirmationScreen
from tests.application.test_provider_auth import auth_home as auth_home
from tests.application.test_runtime_authentication import managed_runtime as managed_runtime
from tests.auth.test_driver_contracts import NOW, BrowserFixtureDriver, DeviceFixtureDriver
from tests.tui.helpers import run_ready_app


class GatedDriver:
    """Gate only the external authorization boundary; keep application and storage real."""

    def __init__(self, driver):
        self.driver = driver
        self.descriptor = driver.descriptor
        self.refresh = driver.refresh
        self.encode = driver.encode
        self.release = asyncio.Event()
        self.cleaned = 0
        self.failure = None

    async def begin_authorization(self, method):
        attempt = await self.driver.begin_authorization(method)

        async def authorize():
            await self.release.wait()
            if self.failure:
                raise self.failure
            return await attempt.wait()

        async def cleanup():
            await attempt.close()
            self.cleaned += 1

        return TaskAuthorizationAttempt(
            attempt.challenge,
            authorize=authorize,
            cleanup=cleanup,
            clock=lambda: NOW,
        )


class AuthApp(App):
    def __init__(self, screen):
        super().__init__()
        self.initial = screen

    def on_mount(self):
        self.push_screen(self.initial)


def _provider_id(owner) -> str:
    config = owner if hasattr(owner, "snapshot") else owner._config
    providers = config.snapshot(include_builtin=False).providers
    assert len(providers) == 1
    return providers[0].provider_id


async def test_builtin_subscription_starts_without_login_and_shows_service_limits(tmp_path):
    facade = ConfigurationFacade(tmp_path)
    created = facade.add_provider(
        display_name="Personal Codex",
        provider_type="openai-codex",
        make_default=True,
    )
    provider_id = created.changed_provider_id
    assert provider_id is not None
    application = LocalApplication.build(tmp_path)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app) as pilot:
        await pilot.press("ctrl+p")
        await choose(app, pilot, "Manage providers")
        await choose(app, pilot, "Personal Codex")
        assert "local planning budget" in choices(app.screen)["service-notice"].description
        await choose(app, pilot, "Authentication")
        assert isinstance(app.screen, ProviderAuthenticationScreen)
        await eventually(lambda: "status" in choices(app.screen))
        assert choices(app.screen)["status"].trailing == "missing"
        assert any(choice.label == "Browser" for choice in choices(app.screen).values())
        provider = facade.provider(provider_id)
        profile = profile_for_provider(provider, facade.provider_template_catalog().templates)
        editor = ProviderConfigurationScreen(profile, existing=provider)
        assert "local planning budget" in next(
            choice.description
            for choice in editor._provider_choices()
            if choice.value == "service-notice"
        )
        assert all(choice.value != "provider-api-key" for choice in editor._provider_choices())


async def eventually(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


async def choose(app, pilot, text):
    if isinstance(app.screen, ConfirmationScreen):
        options = app.screen.query_one(OptionList)
        options.highlighted = 1 if text == "Confirm" else 0
        options.focus()
        await pilot.press("enter")
        await pilot.pause()
        return
    app.screen.query_one("#choice-filter", Input).value = text
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


def choices(screen):
    return {choice.value: choice for choice in screen._choices}


@pytest.mark.parametrize("driver", [BrowserFixtureDriver, DeviceFixtureDriver])
async def test_method_challenge_success_logout_and_no_challenge_in_terminal_ui(
    auth_home,
    driver,
    monkeypatch,
):
    driver = GatedDriver(driver())
    config, _, _, service, _ = auth_home(driver)
    provider_id = _provider_id(config)
    opened = []
    monkeypatch.setattr(
        "ouro.tui.provider_auth_screens.webbrowser.open",
        lambda url, **kw: opened.append(url) or True,
    )
    menu = ProviderAuthenticationScreen(service, provider_id)
    app = AuthApp(menu)
    async with app.run_test(size=(100, 40)) as pilot:
        await eventually(lambda: "status" in choices(menu))
        assert choices(menu)["status"].trailing == "missing"
        method = driver.descriptor.methods[0]
        assert choices(menu)[f"login:{method.method_id}"].label == method.label
        await choose(app, pilot, method.label)
        login = app.screen
        assert isinstance(login, ProviderLoginScreen)
        await eventually(lambda: login._login is not None)
        rows = choices(login)
        assert opened == []
        assert ("code" in rows) == (method.kind == "device_code")
        if method.kind == "device_code":
            assert rows["code"].trailing == "fixture-code"
        await choose(app, pilot, "Open browser")
        assert opened == [rows["url"].description]
        driver.release.set()
        await eventually(lambda: login.result is not None)
        assert login.result.state == "succeeded"
        assert driver.cleaned == 1
        assert set(choices(login)) == {"result", "close"}
        assert login.query_one("#choice-filter", Input).value == ""
        assert login.query_one(OptionList).get_option("result")
        assert login._login is None
        assert (await service.status(provider_id)).state == "ready"
        await pilot.press("escape")
        await eventually(lambda: choices(menu)["status"].trailing == "ready")
        await choose(app, pilot, "Sign out")
        assert isinstance(app.screen, ConfirmationScreen)
        await choose(app, pilot, "Cancel")
        assert (await service.status(provider_id)).state == "ready"
        await choose(app, pilot, "Sign out")
        await choose(app, pilot, "Confirm")
        assert (await service.status(provider_id)).state == "logged_out"
        # Authentication UI never creates conversation notices or model events.
        assert "fixture-access" not in repr(menu._choices)


@pytest.mark.parametrize("stop", ["escape", "unmount", "shutdown"])
async def test_pending_authorization_is_cancelled_and_cleaned_when_ui_leaves(auth_home, stop):
    driver = GatedDriver(BrowserFixtureDriver())
    config, _, _, service, _ = auth_home(driver)
    provider_id = _provider_id(config)
    login = ProviderLoginScreen(service, provider_id, "external-browser")
    app = AuthApp(login)
    async with app.run_test() as pilot:
        await eventually(lambda: login._login is not None)
        if stop == "escape":
            await pilot.press("escape")
            assert login.result.state == "cancelled"
            assert set(choices(login)) == {"result", "close"}
        elif stop == "unmount":
            app.pop_screen()
            await eventually(lambda: driver.cleaned == 1)
    assert driver.cleaned == 1
    assert login._login_task.done()
    assert (await service.status(provider_id)).state == "missing"


@pytest.mark.parametrize(
    "failure",
    [
        AuthError("authorization_denied"),
        AuthError("authorization_expired"),
        OSError("fixture-private-response"),
    ],
)
async def test_driver_failures_use_application_terminal_state_and_safe_message(auth_home, failure):
    driver = GatedDriver(DeviceFixtureDriver())
    driver.failure = failure
    config, _, _, service, _ = auth_home(driver)
    provider_id = _provider_id(config)
    login = ProviderLoginScreen(service, provider_id, "link-device")
    app = AuthApp(login)
    async with app.run_test() as pilot:
        await eventually(lambda: login._login is not None)
        driver.release.set()
        await eventually(lambda: login.result is not None)
        assert login.result.state in {"failed", "expired"}
        assert "fixture-private-response" not in repr(login._choices)
        assert "fixture-code" not in repr(login._choices)
        assert (await service.status(provider_id)).state == "missing"
        await pilot.press("escape")
    assert driver.cleaned == 1


async def test_storage_failure_cannot_be_presented_as_login_success(auth_home, monkeypatch):
    driver = GatedDriver(BrowserFixtureDriver())
    config, store, _, service, _ = auth_home(driver)

    def fail_save(*args, **kwargs):
        raise OSError("fixture-private-disk-path")

    monkeypatch.setattr(store, "write", fail_save)
    login = ProviderLoginScreen(service, _provider_id(config), "external-browser")
    async with AuthApp(login).run_test():
        await eventually(lambda: login._login is not None)
        driver.release.set()
        await eventually(lambda: login.result is not None)
        assert login.result.state == "failed"
        assert "fixture-private-disk-path" not in repr(login._choices)


async def test_local_credentials_list_allows_clearing_orphan_without_false_cancel(auth_home):
    config, _, _, service, _ = auth_home(BrowserFixtureDriver())
    provider_id = _provider_id(config)
    started = await service.begin(provider_id, "external-browser")
    result = await service.wait(started.operation_id)
    credential_id = result.credential.credential_id
    config.remove_provider(provider_id)
    screen = ProviderCredentialsScreen(service)
    app = AuthApp(screen)
    async with app.run_test() as pilot:
        await eventually(lambda: credential_id in choices(screen))
        assert choices(screen)[credential_id].description == "Shared by: none"
        await choose(app, pilot, credential_id)
        await choose(app, pilot, "Cancel")
        assert (await service.credentials())[0].credential.state == "active"
        await choose(app, pilot, credential_id)
        await choose(app, pilot, "Confirm")
        assert (await service.credentials())[0].credential.state == "logged_out"


async def test_browser_launch_failure_leaves_challenge_and_cancellation_available(
    auth_home, monkeypatch
):
    driver = GatedDriver(BrowserFixtureDriver())
    config, _, _, service, _ = auth_home(driver)
    monkeypatch.setattr("ouro.tui.provider_auth_screens.webbrowser.open", lambda *a, **kw: False)
    login = ProviderLoginScreen(service, _provider_id(config), "external-browser")
    app = AuthApp(login)
    async with app.run_test() as pilot:
        await eventually(lambda: login._login is not None)
        await choose(app, pilot, "Open browser")
        assert login.result is None
        assert "copy" in choices(login) and "cancel" in choices(login)
        await pilot.press("escape")
        assert login.result.state == "cancelled"


async def test_cancel_during_admitted_save_reports_the_committed_success(auth_home, monkeypatch):
    driver = GatedDriver(BrowserFixtureDriver())
    config, store, _, service, _ = auth_home(driver)
    entered, release = threading.Event(), threading.Event()
    write = store.write

    def blocked_write(record):
        entered.set()
        assert release.wait(5)
        write(record)

    monkeypatch.setattr(store, "write", blocked_write)
    provider_id = _provider_id(config)
    login = ProviderLoginScreen(service, provider_id, "external-browser")
    async with AuthApp(login).run_test():
        await eventually(lambda: login._login is not None)
        driver.release.set()
        assert await asyncio.to_thread(entered.wait, 5)
        cancelled = asyncio.create_task(login.action_cancel())
        try:
            await eventually(lambda: login._login_task.cancelling())
        finally:
            release.set()
        await cancelled
        assert login.result.state == "succeeded"
        assert choices(login)["result"].label == "Sign-in succeeded"
        assert (await service.status(provider_id)).state == "ready"


async def test_cancel_while_begin_has_not_returned_cleans_up_operation(auth_home):
    entered, cleaned = asyncio.Event(), asyncio.Event()

    class SlowBegin(BrowserFixtureDriver):
        async def begin_authorization(self, method):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    config, _, _, service, _ = auth_home(SlowBegin())
    provider_id = _provider_id(config)
    login = ProviderLoginScreen(service, provider_id, "external-browser")
    async with AuthApp(login).run_test() as pilot:
        await entered.wait()
        await pilot.press("escape")
        assert cleaned.is_set()
        assert login._login is None
        assert choices(login)["result"].label == "Sign-in cancelled"
        assert (await service.status(provider_id)).state == "missing"


async def test_configuration_change_during_login_is_shown_as_conflict(auth_home):
    driver = GatedDriver(BrowserFixtureDriver())
    config, _, _, service, _ = auth_home(driver)
    provider_id = _provider_id(config)
    login = ProviderLoginScreen(service, provider_id, "external-browser")
    async with AuthApp(login).run_test():
        await eventually(lambda: login._login is not None)
        config.remove_provider(provider_id)
        driver.release.set()
        await eventually(lambda: login.result is not None)
        assert login.result.state == "failed"
        assert login.result.error_code == "credential_conflict"


async def test_full_tui_uses_runtime_auth_and_preserves_managed_provider_edit(
    tmp_path,
    managed_runtime,
):
    configs, drivers, policies, _ = managed_runtime
    provider_id = _provider_id(configs)
    application = LocalApplication.build(tmp_path, auth_drivers=drivers, service_policies=policies)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(100, 40)) as pilot:
        controller = app._controller
        assert controller.provider_auth() is application.provider_auth
        configuration = ConfigurationFacade(tmp_path)
        before = configuration.provider(provider_id)
        profile = profile_for_provider(before, ())
        editor = ProviderConfigurationScreen(profile, before)
        assert "provider-api-key" not in choices(editor)
        assert "provider-api-key-env" not in choices(editor)
        draft = editor._draft()
        assert draft.api_key is None and draft.api_key_env is None
        saved = await controller.update_provider(provider_id, replace(draft, timeout=80))
        assert saved.credential == before.credential and saved.auth_driver == before.auth_driver
        assert saved.timeout == 80
        # A managed setup default reaches the creation use case without a key field.
        managed_profile = replace(
            profile,
            auth_driver=before.auth_driver,
            display_name="Second",
            base_url=before.base_url,
        )
        creation = ProviderConfigurationScreen(managed_profile)._draft()
        created = await controller.add_provider(creation)
        assert created.auth_driver == before.auth_driver
        assert created.credential.state == "managed"
        assert created.credential.reference != before.credential.reference
        await pilot.press("ctrl+p")
        await choose(app, pilot, "Manage providers")
        await choose(app, pilot, "Subscription")
        await choose(app, pilot, "Authentication")
        assert isinstance(app.screen, ProviderAuthenticationScreen)
        await eventually(lambda: "status" in choices(app.screen))
        assert choices(app.screen)["status"].trailing == "missing"
        await choose(app, pilot, drivers[0].descriptor.methods[0].label)
        login = app.screen
        await eventually(lambda: login.result is not None)
        assert login.result.state == "succeeded"
        assert (await application.provider_auth.status(provider_id)).state == "ready"


async def test_unavailable_store_status_and_failed_begin_stay_in_management_ui(
    auth_home, monkeypatch
):
    config, _, credentials, service, _ = auth_home(BrowserFixtureDriver())

    async def unavailable(_credential_id):
        raise AuthError("credential_store_error")

    monkeypatch.setattr(credentials, "status", unavailable)
    screen = ProviderAuthenticationScreen(service, _provider_id(config))
    app = AuthApp(screen)
    async with app.run_test() as pilot:
        await eventually(lambda: "status" in choices(screen))
        assert choices(screen)["status"].trailing == "unavailable"
        assert choices(screen)["status"].description == "credential_store_error"
        await choose(app, pilot, "Use browser")
        login = app.screen
        await eventually(lambda: login._login_task.done())
        assert choices(login)["result"].label == "Sign-in failed"
        assert "close" in choices(login)


async def test_static_authentication_has_no_login_method_and_disables_logout(auth_home):
    _, _, _, service, _ = auth_home()
    screen = ProviderAuthenticationScreen(service, "faux")
    async with AuthApp(screen).run_test():
        await eventually(lambda: "status" in choices(screen))
        assert choices(screen)["status"].trailing == "static"
        assert choices(screen)["logout"].disabled
        assert not any(key.startswith("login:") for key in choices(screen))


async def test_corrupt_credentials_do_not_prevent_full_tui_management(tmp_path, managed_runtime):
    configs, drivers, policies, _ = managed_runtime
    provider_id = _provider_id(configs)
    provider = configs.get_provider(provider_id)
    assert provider is not None
    credential_id = provider.credential_ref.name
    directory = tmp_path / "credentials"
    directory.mkdir()
    (directory / f"{credential_id}.json").write_text(
        "{fixture-private-broken-record", encoding="utf-8"
    )
    application = LocalApplication.build(tmp_path, auth_drivers=drivers, service_policies=policies)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app):
        screen = ProviderAuthenticationScreen(app._controller.provider_auth(), provider_id)
        await app.push_screen(screen)
        await eventually(lambda: "status" in choices(screen))
        assert choices(screen)["status"].trailing == "unavailable"
        assert "fixture-private" not in repr(screen._choices)


async def test_full_tui_shutdown_settles_shared_pending_login(tmp_path, managed_runtime):
    configs, drivers, policies, _ = managed_runtime
    provider_id = _provider_id(configs)
    driver = GatedDriver(drivers[0])
    application = LocalApplication.build(
        tmp_path,
        auth_drivers=(driver, drivers[1]),
        service_policies=policies,
    )
    app = OuroTuiApp(application=application)
    async with run_ready_app(app):
        login = ProviderLoginScreen(
            app._controller.provider_auth(), provider_id, "external-browser"
        )
        await app.push_screen(login)
        await eventually(lambda: login._login is not None)
    assert driver.cleaned == 1
    assert login._login_task.done()
    with pytest.raises(AuthError, match="closed"):
        await application.provider_auth.begin(provider_id, "external-browser")
