from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from ouro.config.providers import ProviderConfigManager
from ouro.local_application import LocalApplication, open_local_plugins

PLUGIN = "test.background.model"
PROTOCOL = f"{PLUGIN}/providers/probe"


def package(tmp_path, kind, scope):
    root = tmp_path / "package"
    (root / ".ouro-plugin").mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).parent / "fixtures/background_model.py", root / "model_plugin.py"
    )
    (root / "configuration.json").write_text('{"type":"object"}', encoding="utf-8")
    runtime = {"kind": kind, "entrypoint": "model_plugin:create_plugin"}
    if kind == "worker":
        runtime = {
            "kind": kind,
            "command": ["{python}", "model_plugin.py"],
            "protocol": "3.0",
        }
    (root / ".ouro-plugin/plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "plugin_id": PLUGIN,
                "namespace": "model",
                "name": PLUGIN,
                "version": "1.0.0",
                "engines": {"ouro": "^0.3.0", "plugin_api": "^4.0.0"},
                "runtime": runtime,
                "configuration": {"schema": "./configuration.json", "version": 1},
                "permissions": [{"id": "models.request", "reason": "Initialize service"}],
                "contributes": {
                    "providers": [{"id": "probe"}],
                    "background_services": [{"id": "initialize", "scope": scope}],
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def configure_model(home):
    config = ProviderConfigManager(home)
    provider = config.create_provider(
        display_name="Local", provider_type="custom", protocol=PROTOCOL
    )
    endpoint = config.create_endpoint(provider.provider_id, model_id="probe", make_default=True)
    config.set_default_route(provider.provider_id, endpoint.endpoint_id)


def events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def disable_externally(store):
    expected = store.read_state()
    candidate = expected.model_copy(deep=True)
    candidate.plugins[PLUGIN].activation.desired_enabled = False
    store.commit_candidate_state(expected, candidate)


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
@pytest.mark.parametrize("scope", ["host", "home"])
async def test_background_models_initialize_after_enable_configure_and_cold_start(
    tmp_path, kind, scope
):
    home, log = tmp_path / "home", tmp_path / "events"
    configure_model(home)
    configuration = {"events": str(log)}
    async with LocalApplication.build(home) as app:
        app.plugins.install(package(tmp_path, kind, scope))
        await app.plugins.configure(
            app.plugins.configuration(PLUGIN).candidate(values=configuration)
        )
        assert (await app.plugins.enable(PLUGIN)).status == "active"
        if scope == "home":
            await app.plugin_runtime.start_home_services()
        assert [phase for phase, _ in events(log)] == ["starting", "request", "started"]
        first = events(log)[0][1]
        await app.plugins.configure(
            app.plugins.configuration(PLUGIN).candidate(values={**configuration, "revision": 2})
        )
        assert app.plugin_runtime.status(PLUGIN) == "active", app.plugins.diagnostics(PLUGIN)
        phases = [phase for phase, _ in events(log)]
        assert phases == [
            "starting",
            "request",
            "started",
            "stopped",
            "starting",
            "request",
            "started",
        ]
        assert events(log)[-1][1] != first
    # The lightweight management Host uses the same admission rule at startup.
    async with open_local_plugins(home) as plugins:
        if scope == "home":
            await plugins.runtime.start_home_services()
        assert plugins.runtime.status(PLUGIN) == "active", plugins.diagnostics(PLUGIN)
        assert [phase for phase, _ in events(log)][-3:] == ["starting", "request", "started"]
    starts = [identity for phase, identity in events(log) if phase == "started"]
    stops = [identity for phase, identity in events(log) if phase == "stopped"]
    assert starts == stops and len(starts) == 3


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
async def test_cancelled_enable_settles_published_service_before_next_mutation(tmp_path, kind):
    home, log, release = tmp_path / "home", tmp_path / "events", tmp_path / "release"
    configure_model(home)
    async with LocalApplication.build(home) as app:
        app.plugins.install(package(tmp_path, kind, "host"))
        await app.plugins.configure(
            app.plugins.configuration(PLUGIN).candidate(
                values={"events": str(log), "release": str(release)}
            )
        )
        enabling = asyncio.create_task(app.plugins.enable(PLUGIN))
        try:
            async with asyncio.timeout(10):
                while not log.exists():
                    await asyncio.sleep(0.01)
            enabling.cancel()
            disabling = asyncio.create_task(app.plugins.disable(PLUGIN))
            release.touch()
            with pytest.raises(asyncio.CancelledError):
                await enabling
            assert (await disabling).status == "disabled"
            assert [phase for phase, _ in events(log)] == [
                "starting",
                "request",
                "started",
                "stopped",
            ]
            assert not app.plugin_runtime.has_resources(PLUGIN)
        finally:
            release.touch()
            await asyncio.gather(enabling, return_exceptions=True)


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
async def test_failed_background_stop_prevents_replacement_model_initialization(tmp_path, kind):
    home, log, marker = tmp_path / "home", tmp_path / "events", tmp_path / "fail_stop"
    configure_model(home)
    async with LocalApplication.build(home) as app:
        app.plugins.install(package(tmp_path, kind, "host"))
        await app.plugins.configure(
            app.plugins.configuration(PLUGIN).candidate(
                values={"events": str(log), "fail_stop": str(marker)}
            )
        )
        assert (await app.plugins.enable(PLUGIN)).status == "active"
        marker.touch()
        try:
            assert (await app.plugins.restart(PLUGIN)).status in {"degraded", "failed"}
            assert [phase for phase, _ in events(log)] == ["starting", "request", "started"]
        finally:
            marker.unlink()
        assert (await app.plugins.disable(PLUGIN)).status == "disabled"
        assert not app.plugin_runtime.has_resources(PLUGIN)


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
async def test_published_callback_finishes_before_observing_external_disable(tmp_path, kind):
    home, log, release = tmp_path / "home", tmp_path / "events", tmp_path / "release"
    configure_model(home)
    async with LocalApplication.build(home) as app:
        app.plugins.install(package(tmp_path, kind, "host"))
        await app.plugins.configure(
            app.plugins.configuration(PLUGIN).candidate(
                values={"events": str(log), "release": str(release)}
            )
        )
        enabling = asyncio.create_task(app.plugins.enable(PLUGIN))
        try:
            async with asyncio.timeout(10):
                while not log.exists():
                    await asyncio.sleep(0.01)
            # Another Host changes shared intent after this callback was admitted.
            disable_externally(app.plugin_runtime.store)
            release.touch()
            await enabling
            assert [phase for phase, _ in events(log)] == ["starting", "request", "started"]
            with pytest.raises(ValueError, match="Provider is unavailable"):
                await app.runtime.bind_provider(PROTOCOL)
            assert await app.plugin_runtime.wait_stopped(PLUGIN) == "disabled"
            assert not app.plugin_runtime.has_resources(PLUGIN)
        finally:
            release.touch()
            await asyncio.gather(enabling, return_exceptions=True)


async def test_inherited_lifecycle_admission_expires_before_independent_work(tmp_path):
    home, log = tmp_path / "home", tmp_path / "events"
    configure_model(home)
    async with LocalApplication.build(home) as app:
        app.plugins.install(package(tmp_path, "python-inprocess", "host"))
        await app.plugins.configure(
            app.plugins.configuration(PLUGIN).candidate(values={"events": str(log)})
        )
        assert (await app.plugins.enable(PLUGIN)).status == "active"
        release = asyncio.Event()

        async def later():
            await release.wait()
            return await app.runtime.bind_provider(PROTOCOL)

        with app.plugin_runtime._admit_lifecycle_callbacks():
            pending = asyncio.create_task(later())
        disable_externally(app.plugin_runtime.store)
        release.set()
        with pytest.raises(ValueError, match="Provider is unavailable"):
            await pending
        assert await app.plugin_runtime.wait_stopped(PLUGIN) == "disabled"
        assert not app.plugin_runtime.has_resources(PLUGIN)
