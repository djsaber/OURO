from __future__ import annotations

import asyncio
import json
import threading
from functools import partial

import pytest

from ouro.auth.contracts import AuthError
from ouro.auth.driver_contracts import AuthDriverDescriptor, AuthorizationMethod
from ouro.auth.store import FileCredentialStore
from ouro.cli import provider_auth_commands
from ouro.cli.main import main
from ouro.cli.parser import build_parser
from ouro.config import provider_models
from ouro.config.provider_models import ProviderConnectionSpec, ProviderTypeSpec
from ouro.config.providers import ProviderConfigManager
from ouro.local_application import open_local_provider_auth
from tests.application.test_provider_auth import ControlledDriver
from tests.auth.test_driver_contracts import drivers


@pytest.fixture
def configured_auth(tmp_path, monkeypatch):
    spec = ProviderTypeSpec(
        "custom",
        ProviderConnectionSpec("openai-chat-completions"),
        (
            ProviderConnectionSpec("openai-chat-completions", "fixture-browser", "service-browser"),
            ProviderConnectionSpec("anthropic-messages", "fixture-device", "service-device"),
        ),
    )
    monkeypatch.setitem(provider_models._PROVIDER_TYPE_SPECS, spec.provider_type, spec)
    opened = []
    monkeypatch.setattr(
        provider_auth_commands.webbrowser, "open", lambda url, **kwargs: opened.append(url) or True
    )

    def configure(driver):
        config = ProviderConfigManager(tmp_path)
        config.create_provider(
            display_name="Account",
            provider_type="custom",
            protocol="anthropic-messages"
            if driver.descriptor.driver_id == "fixture-device"
            else "openai-chat-completions",
            auth_driver=driver.descriptor.driver_id,
            base_url="https://fixture.invalid/v1",
        )
        monkeypatch.setattr(
            provider_auth_commands,
            "open_local_provider_auth",
            partial(open_local_provider_auth, drivers=(driver,)),
        )
        return config, opened

    return configure


def command(tmp_path, operation, *args):
    return ["provider", "auth", operation, *args, "--home", str(tmp_path), "--json"]


def _provider_id(config):
    providers = config.snapshot(include_builtin=False).providers
    assert len(providers) == 1
    return providers[0].provider_id


@pytest.mark.parametrize("driver", drivers(), ids=lambda value: value.descriptor.driver_id)
def test_both_driver_kinds_work_through_real_cli_and_lightweight_composition(
    tmp_path, configured_auth, capsys, driver
):
    config, opened = configured_auth(driver)
    provider_id = _provider_id(config)
    assert main(command(tmp_path, "methods", provider_id)) == 0
    methods = json.loads(capsys.readouterr().out)["methods"]
    assert methods[0]["method_id"] == driver.descriptor.methods[0].method_id
    assert main(command(tmp_path, "login", provider_id)) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["operation"]["state"] == "succeeded"
    assert "fixture.invalid" in output.err
    if driver.descriptor.driver_id == "fixture-device":
        assert "Device code: fixture-code" in output.err
    assert opened == []
    assert main(command(tmp_path, "status", provider_id)) == 0
    status = json.loads(capsys.readouterr().out)["authentication"]
    credential_id = status["credential_id"]
    provider = config.get_provider(provider_id)
    assert provider is not None
    assert credential_id == provider.credential_ref.name
    assert status["credential"]["revision"] == 1
    assert main(command(tmp_path, "list")) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["credentials"][0]["bound_provider_ids"] == [provider_id]
    assert main(command(tmp_path, "forget", credential_id)) == 0
    forgotten = json.loads(capsys.readouterr().out)
    assert forgotten["credential"]["state"] == "logged_out"
    assert main(command(tmp_path, "logout", provider_id)) == 0
    assert json.loads(capsys.readouterr().out) == forgotten
    for payload in (result, status, listing, forgotten):
        serialized = json.dumps(payload)
        for forbidden in (
            "secret_payload",
            "fixture-access",
            "fixture-ticket",
            "authorization_url",
            "user_code",
        ):
            assert forbidden not in serialized
    assert {item.name for item in tmp_path.iterdir()} <= {
        "config",
        "locks",
        "credentials",
        ".upgrade",
    }
    assert {item.name for item in (tmp_path / ".upgrade").iterdir()} == {"lock"}


def test_empty_home_and_static_provider_need_no_runtime_or_secret_directory(tmp_path, capsys):
    assert main(command(tmp_path, "list")) == 0
    assert json.loads(capsys.readouterr().out)["credentials"] == []
    assert main(command(tmp_path, "methods", "faux")) == 0
    assert json.loads(capsys.readouterr().out)["methods"] == []
    assert main(command(tmp_path, "status", "faux")) == 0
    assert json.loads(capsys.readouterr().out)["authentication"]["state"] == "static"
    assert main(command(tmp_path, "login", "faux")) == 3
    assert json.loads(capsys.readouterr().out)["error_code"] == "authorization_unsupported"
    assert {item.name for item in tmp_path.iterdir()} == {".upgrade"}
    assert {item.name for item in (tmp_path / ".upgrade").iterdir()} == {"lock"}


def test_home_precedence_and_auth_help(tmp_path, capsys):
    root = tmp_path / "global"
    parser = build_parser()
    args = parser.parse_args(["--home", str(root), "provider", "auth", "list"])
    assert args.home == str(root)
    args = parser.parse_args(["--home", str(root), *command(tmp_path, "list")])
    assert args.home == str(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        main(["provider", "auth", "login", "--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--method" in help_text and "--open-browser" in help_text
    assert "stderr" in help_text


@pytest.mark.parametrize("driver", drivers(), ids=lambda value: value.descriptor.driver_id)
def test_browser_open_is_explicit_and_uses_challenge_not_brand(
    tmp_path, configured_auth, capsys, driver
):
    config, opened = configured_auth(driver)
    assert main(command(tmp_path, "login", _provider_id(config), "--open-browser")) == 0
    assert len(opened) == 1
    assert opened[0].startswith("https://fixture.invalid/")
    assert json.loads(capsys.readouterr().out)["operation"]["state"] == "succeeded"


def test_browser_failure_keeps_manual_login_available_without_raw_error(
    tmp_path, configured_auth, capsys, monkeypatch
):
    config, _ = configured_auth(drivers()[0])

    def unavailable(*args, **kwargs):
        raise OSError("fixture-secret-browser-error")

    monkeypatch.setattr(provider_auth_commands.webbrowser, "open", unavailable)
    assert main(command(tmp_path, "login", _provider_id(config), "--open-browser")) == 0
    output = capsys.readouterr()
    assert "Could not open a browser" in output.err
    assert "fixture-secret" not in output.err + output.out


def test_multiple_methods_require_selection_without_starting_authorization(
    tmp_path, configured_auth, capsys
):
    driver = ControlledDriver()
    driver.descriptor = AuthDriverDescriptor(
        driver_id="fixture-browser",
        label="Fixture",
        methods=(
            AuthorizationMethod(method_id="one", kind="browser_callback", label="First"),
            AuthorizationMethod(method_id="two", kind="browser_callback", label="Second"),
        ),
    )
    config, _ = configured_auth(driver)
    provider_id = _provider_id(config)
    assert main(command(tmp_path, "login", provider_id)) == 2
    assert "choose --method" in json.loads(capsys.readouterr().out)["error"]
    assert not driver.started.is_set()
    driver.release.set()
    assert main(command(tmp_path, "login", provider_id, "--method", "two")) == 0
    assert json.loads(capsys.readouterr().out)["operation"]["state"] == "succeeded"


@pytest.mark.parametrize(
    "error",
    [
        AuthError("authorization_denied"),
        AuthError("authorization_expired"),
        OSError("fixture-secret-error"),
    ],
)
def test_failed_login_is_safe_nonzero_json(tmp_path, configured_auth, capsys, error):
    driver = ControlledDriver()
    driver.error = error
    driver.release.set()
    config, _ = configured_auth(driver)
    assert main(command(tmp_path, "login", _provider_id(config))) == 3
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["status"] == "failed"
    assert result["operation"]["error_code"]
    assert "fixture-secret-error" not in output.out + output.err
    assert driver.cleaned == 1


async def test_cancelling_command_cleans_attempt_and_returns_cancelled_result(
    tmp_path, configured_auth, capsys, monkeypatch
):
    driver = ControlledDriver()
    config, _ = configured_auth(driver)
    shown = asyncio.Event()
    show_challenge = provider_auth_commands._show_challenge

    def show(started):
        url = show_challenge(started)
        shown.set()
        return url

    monkeypatch.setattr(provider_auth_commands, "_show_challenge", show)
    args = build_parser().parse_args(command(tmp_path, "login", _provider_id(config)))
    task = asyncio.create_task(provider_auth_commands.run_provider_auth_command(args))
    await shown.wait()
    task.cancel()
    assert await task == 5
    result = json.loads(capsys.readouterr().out)
    assert result["operation"]["state"] == "cancelled"
    assert driver.cleaned == 1
    assert (
        FileCredentialStore(tmp_path).read(
            config.get_provider(_provider_id(config)).credential_ref.name
        )
        is None
    )


async def test_cancelling_command_after_save_starts_reports_saved_result(
    tmp_path, configured_auth, capsys, monkeypatch
):
    driver = ControlledDriver()
    driver.release.set()
    config, _ = configured_auth(driver)
    entered, release = threading.Event(), threading.Event()
    write = FileCredentialStore.write

    def slow_write(self, record):
        entered.set()
        assert release.wait(5)
        write(self, record)

    monkeypatch.setattr(FileCredentialStore, "write", slow_write)
    args = build_parser().parse_args(command(tmp_path, "login", _provider_id(config)))
    task = asyncio.create_task(provider_auth_commands.run_provider_auth_command(args))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    assert await task == 0
    assert json.loads(capsys.readouterr().out)["operation"]["state"] == "succeeded"


def test_unavailable_status_and_orphan_management_without_registered_driver(
    tmp_path, configured_auth, capsys, monkeypatch
):
    driver = drivers()[0]
    config, _ = configured_auth(driver)
    provider_id = _provider_id(config)
    assert main(command(tmp_path, "login", provider_id)) == 0
    capsys.readouterr()
    monkeypatch.setattr(
        provider_auth_commands, "open_local_provider_auth", open_local_provider_auth
    )
    assert main(command(tmp_path, "status", provider_id)) == 3
    assert (
        json.loads(capsys.readouterr().out)["authentication"]["error_code"]
        == "authorization_unsupported"
    )
    provider = config.get_provider(provider_id)
    assert provider is not None
    credential_id = provider.credential_ref.name
    config.remove_provider(provider_id)
    assert main(command(tmp_path, "list")) == 0
    assert json.loads(capsys.readouterr().out)["credentials"][0]["bound_provider_ids"] == []
    assert main(command(tmp_path, "forget", credential_id)) == 0
    assert json.loads(capsys.readouterr().out)["credential"]["state"] == "logged_out"


def test_text_output_and_missing_provider_errors(tmp_path, capsys):
    assert main(["provider", "auth", "list", "--home", str(tmp_path)]) == 0
    assert "No local credentials" in capsys.readouterr().out
    assert main(command(tmp_path, "status", "absent")) == 3
    assert json.loads(capsys.readouterr().out)["error_code"] == "authorization_configuration_error"


async def test_cancel_before_challenge_handoff_returns_safe_error(
    tmp_path, configured_auth, capsys
):
    class BeginningDriver(ControlledDriver):
        async def begin_authorization(self, method_id):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cleaned += 1

    driver = BeginningDriver()
    config, _ = configured_auth(driver)
    args = build_parser().parse_args(command(tmp_path, "login", _provider_id(config)))
    task = asyncio.create_task(provider_auth_commands.run_provider_auth_command(args))
    await driver.started.wait()
    task.cancel()
    assert await task == 5
    result = json.loads(capsys.readouterr().out)
    assert result["error_code"] == "authorization_cancelled"
    assert driver.cleaned == 1


def test_save_failure_never_prints_login_success(tmp_path, configured_auth, capsys, monkeypatch):
    config, _ = configured_auth(drivers()[0])

    def fail_write(self, record):
        raise AuthError("credential_store_error")

    monkeypatch.setattr(FileCredentialStore, "write", fail_write)
    assert main(command(tmp_path, "login", _provider_id(config))) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["operation"]["state"] == "failed"
    assert result["operation"]["error_code"] == "credential_store_error"
