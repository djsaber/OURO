from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import SecretStr

import ouro.plugins.installation.store as store_module
import ouro.plugins.lifecycle.runtime as runtime_module
from ouro.application.contracts.plugin_configuration import PluginConfigurationCandidate
from ouro.application.plugins.configuration import (
    PluginConfigurationConflictError,
    PluginConfigurationValidationError,
)
from ouro.application.plugins.service import PluginService
from ouro.plugins.credentials import InlinePluginCredential
from ouro.plugins.installation.store import (
    PluginPackageStore,
    PluginStateConflictError,
    PluginStateFormatError,
)
from ouro.plugins.models import create_configuration_revision
from tests.plugins.test_plugin_system import _runtime, _write_native_plugin


def _package(root, *, runtime=False, fail_default=False):
    _write_native_plugin(
        root,
        runtime={"kind": "python-inprocess", "entrypoint": "echo_plugin:create_plugin"}
        if runtime
        else None,
        extra={
            "configuration": {"schema": "./config.json"},
            "permissions": [{"id": "credentials.use", "scopes": ["optional"], "reason": "Test"}],
        },
    )
    (root / "config.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "minimum": 0, "default": 2},
                    "fail": {"type": "boolean", "default": fail_default},
                    "label": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "object"}},
                },
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    if runtime:
        (root / "echo_plugin.py").write_text(
            """
from ouro.sdk.plugins.v5 import ContributionSet
class Plugin:
    def __init__(self, context): self.context = context
    async def prepare(self):
        if self.context.configuration['fail']: raise RuntimeError('candidate failed')
        return ContributionSet()
    async def start(self): pass
    async def stop(self, reason, deadline): pass
def create_plugin(context): return Plugin(context)
""",
            encoding="utf-8",
        )


def test_query_and_preflight_are_static_detached_and_materialize_defaults(tmp_path):
    source = tmp_path / "package"
    _package(source, runtime=True)
    (source / "echo_plugin.py").write_text(
        "raise RuntimeError('must not execute')", encoding="utf-8"
    )
    service = PluginService(PluginPackageStore(tmp_path / "home"))
    service.install(source)
    original = service.store.read_state()
    view = service.configuration("dev.ouro.echo")
    candidate = view.candidate(values={"items": [{"a": 1}]})
    result = service.preview_configuration(candidate)
    assert result.valid and result.changed and result.scope == "stored"
    assert result.candidate.values == {"count": 2, "fail": False, "items": [{"a": 1}]}
    assert candidate.values == {"items": [{"a": 1}]}
    assert view.schema_document["properties"]["count"]["minimum"] == 0
    assert view.credential_slots[0].availability == "unknown"
    result.candidate.values["items"][0]["a"] = 2
    assert candidate.values["items"][0]["a"] == 1
    view.schema_document.clear()
    assert service.configuration("dev.ouro.echo").schema_document
    assert service.store.read_state() == original


async def test_full_replacement_defaults_noop_and_safe_structured_errors(tmp_path):
    _package(tmp_path / "package")
    service = PluginService(PluginPackageStore(tmp_path / "home"))
    service.install(tmp_path / "package")
    view = service.configuration("dev.ouro.echo")
    unchanged = await service.configure(view.candidate(values={}))
    assert unchanged.outcome == "unchanged" and not unchanged.changed
    saved = await service.configure(
        view.candidate(values={"label": "set"}, credential_refs={"optional": "env:KEY"})
    )
    assert saved.outcome == "saved" and saved.scope == "stored"
    view = service.configuration("dev.ouro.echo")
    await service.configure(view.candidate(values={}, credential_refs={}))
    view = service.configuration("dev.ouro.echo")
    assert view.values == {"count": 2, "fail": False} and view.credential_refs == {}
    invalid = view.candidate(values={"count": "PRIVATE_PAYLOAD"})
    preview = service.preview_configuration(invalid)
    assert not preview.valid
    assert preview.issues[0].path == "/values/count"
    with pytest.raises(PluginConfigurationValidationError) as error:
        await service.configure(invalid)
    assert error.value.issues == preview.issues
    assert "PRIVATE_PAYLOAD" not in str(error.value)


async def test_inline_keys_persist_across_services_and_replace_environment_bindings(tmp_path):
    _package(tmp_path / "package")
    home = tmp_path / "home"
    service = PluginService(PluginPackageStore(home))
    service.install(tmp_path / "package")
    secret = "env:literal-persisted-key"
    binding = InlinePluginCredential(value=SecretStr(secret))
    result = await service.configure(
        service.configuration("dev.ouro.echo").candidate(credential_refs={"optional": binding})
    )
    assert result.outcome == "saved"
    reopened = PluginService(PluginPackageStore(home))
    view = reopened.configuration("dev.ouro.echo")
    assert view.credential_refs["optional"].value.get_secret_value() == secret
    assert secret not in view.model_dump_json()
    assert secret not in reopened.show("dev.ouro.echo").model_dump_json()
    assert secret not in str(reopened.store.audit_events())
    assert (await reopened.configure(view.candidate())).outcome == "unchanged"
    await reopened.configure(view.candidate(credential_refs={"optional": "env:ENV_KEY"}))
    assert reopened.configuration("dev.ouro.echo").credential_refs == {"optional": "env:ENV_KEY"}


@pytest.mark.parametrize("secret", ("", "   "))
async def test_empty_inline_keys_fail_preflight_without_changing_storage(tmp_path, secret):
    _package(tmp_path / "package")
    service = PluginService(PluginPackageStore(tmp_path / "home"))
    service.install(tmp_path / "package")
    before = service.store.read_state()
    candidate = service.configuration("dev.ouro.echo").candidate(
        credential_refs={"optional": InlinePluginCredential(value=SecretStr(secret))}
    )
    preview = service.preview_configuration(candidate)
    assert not preview.valid
    assert preview.issues[0].code == "credential_value"
    with pytest.raises(PluginConfigurationValidationError):
        await service.configure(candidate)
    assert service.store.read_state() == before


async def test_inline_key_permission_failure_leaves_saved_state_and_no_temporary_secret(
    tmp_path, monkeypatch
):
    _package(tmp_path / "package")
    service = PluginService(PluginPackageStore(tmp_path / "home"))
    service.install(tmp_path / "package")
    state_path = service.store.state_path
    before = state_path.read_bytes()
    files = set(state_path.parent.iterdir())

    def cannot_protect(_path):
        raise OSError("Cannot restrict file permissions")

    monkeypatch.setattr(store_module, "restrict_to_user", cannot_protect)
    with pytest.raises(OSError, match="permissions"):
        await service.configure(
            service.configuration("dev.ouro.echo").candidate(
                credential_refs={
                    "optional": InlinePluginCredential(value=SecretStr("new-private-key"))
                }
            )
        )
    assert state_path.read_bytes() == before
    assert set(state_path.parent.iterdir()) == files


async def test_invalid_saved_state_errors_do_not_include_inline_keys(tmp_path):
    _package(tmp_path / "package")
    service = PluginService(PluginPackageStore(tmp_path / "home"))
    service.install(tmp_path / "package")
    secret = "private-key-in-invalid-state"
    await service.configure(
        service.configuration("dev.ouro.echo").candidate(
            credential_refs={"optional": InlinePluginCredential(value=SecretStr(secret))}
        )
    )
    path = service.store.state_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 0
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PluginStateFormatError) as error:
        service.store.read_state()
    assert secret not in str(error.value)


async def test_stale_offline_draft_conflicts_even_when_its_values_match_latest(tmp_path):
    _package(tmp_path / "package")
    service = PluginService(PluginPackageStore(tmp_path / "home"))
    service.install(tmp_path / "package")
    candidate = service.configuration("dev.ouro.echo").candidate(values={"count": 3})
    exported = candidate.model_dump_json()
    await service.configure(candidate)
    reloaded = PluginConfigurationCandidate.model_validate_json(exported)
    preview = service.preview_configuration(reloaded)
    assert not preview.valid and preview.issues[-1].code == "configuration_conflict"
    with pytest.raises(PluginConfigurationConflictError):
        await service.configure(reloaded)
    assert service.configuration("dev.ouro.echo").values["count"] == 3


async def test_fixed_target_does_not_follow_new_selection(tmp_path):
    source = tmp_path / "package"
    _package(source)
    service = PluginService(PluginPackageStore(tmp_path / "home"))
    first = service.install(source)
    old_draft = service.configuration("dev.ouro.echo").candidate(values={"count": 4})
    (source / "note.txt").write_text("new package", encoding="utf-8")
    second = service.install(source)
    await service.select("dev.ouro.echo", second.package_digest)
    result = await service.configure(old_draft)
    assert result.package_digest == first.package_digest and result.outcome == "saved"
    assert service.configuration("dev.ouro.echo").values["count"] == 2
    assert (
        service.configuration("dev.ouro.echo", package_digest=first.package_digest).values["count"]
        == 4
    )


async def test_failed_enabled_instance_can_be_repaired_and_noop_does_not_rebuild(tmp_path):
    _package(tmp_path / "package", runtime=True, fail_default=True)
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(tmp_path / "package")
    try:
        assert (await service.enable("dev.ouro.echo")).status == "failed"
        view = service.configuration("dev.ouro.echo")
        result = await service.configure(view.candidate(values={"fail": False}))
        assert result.outcome == "published" and result.status == "active"
        generation = runtime.catalog().generations[0].generation_id
        view = service.configuration("dev.ouro.echo")
        assert not (await service.configure(view.candidate())).changed
        assert runtime.catalog().generations[0].generation_id == generation
        management = PluginService(runtime.store)
        with pytest.raises(RuntimeError, match="without its runtime"):
            await management.configure(view.candidate(values={"count": 8, "fail": False}))
        assert service.configuration("dev.ouro.echo").values["count"] == 2
    finally:
        await runtime.close()


async def test_build_time_store_conflict_discards_candidate_and_preserves_other_writer(
    tmp_path, monkeypatch
):
    _package(tmp_path / "package", runtime=True)
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(tmp_path / "package")
    try:
        await service.enable("dev.ouro.echo")
        view = service.configuration("dev.ouro.echo")
        generation = runtime.catalog().generations[0].generation_id
        commit = service.store.commit_candidate_state

        def concurrent_commit(expected, candidate):
            def other_writer(state):
                record = state.plugins[view.plugin_id]
                record.activation.set_configuration(
                    view.package_digest,
                    create_configuration_revision(
                        values={"count": 9, "fail": False}, schema_version=1
                    ),
                    activate=True,
                )
                return state

            service.store.mutate_state(other_writer)
            return commit(expected, candidate)

        monkeypatch.setattr(service.store, "commit_candidate_state", concurrent_commit)
        with pytest.raises(PluginStateConflictError):
            await service.configure(view.candidate(values={"count": 5}))
        assert service.configuration("dev.ouro.echo").values["count"] == 9
        assert runtime.catalog().generations[0].generation_id == generation
    finally:
        await runtime.close()


async def test_cancelled_configuration_cleans_candidate_and_keeps_published_revision(
    tmp_path, monkeypatch
):
    _package(tmp_path / "package", runtime=True)
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(tmp_path / "package")
    preparing, stopped = asyncio.Event(), asyncio.Event()
    load_factory = runtime_module.load_plugin_factory

    def load_suspended_factory(*args, **kwargs):
        factory, module = load_factory(*args, **kwargs)

        def create(context):
            instance = factory(context)
            if context.configuration["count"] == 99:

                async def prepare():
                    preparing.set()
                    await asyncio.Event().wait()

                async def stop(reason, deadline):
                    stopped.set()

                instance.prepare, instance.stop = prepare, stop
            return instance

        return create, module

    try:
        await service.enable("dev.ouro.echo")
        before = service.store.read_state()
        generation = runtime.catalog().generations[0].generation_id
        monkeypatch.setattr(runtime_module, "load_plugin_factory", load_suspended_factory)
        task = asyncio.create_task(
            service.configure(
                service.configuration("dev.ouro.echo").candidate(values={"count": 99})
            )
        )
        await asyncio.wait_for(preparing.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
        assert service.store.read_state() == before
        assert runtime.catalog().generations[0].generation_id == generation
        assert service.configuration("dev.ouro.echo").values["count"] == 2
    finally:
        await runtime.close()
