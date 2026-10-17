from __future__ import annotations

from pathlib import Path

import pytest

from ouro.local_application import open_local_plugins

EXAMPLES = Path(__file__).parents[2] / "examples" / "plugins"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("directory", "plugin_id"),
    [
        ("declarative-skill", "dev.ouro.examples.declarative_skill"),
        ("inprocess-command", "dev.ouro.examples.inprocess_command"),
        ("inprocess-observer", "dev.ouro.examples.observer"),
        ("inprocess-prompt-policy", "dev.ouro.examples.prompt_policy"),
        ("worker-tool", "dev.ouro.examples.worker_tool"),
    ],
)
async def test_documented_plugin_examples_activate(
    tmp_path,
    directory: str,
    plugin_id: str,
) -> None:
    async with open_local_plugins(tmp_path / "home") as service:
        validation = service.validate(EXAMPLES / directory)
        assert validation.valid is True
        service.install(EXAMPLES / directory)

        result = await service.enable(plugin_id)

        assert result.status == "active"
        assert service.show(plugin_id).active_generations[0]["execution_contract_hash"]


async def test_configuration_playground_exercises_contract_without_business_logic(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OURO_CONFIGURATION_EXAMPLE_KEY", "synthetic-example-value")
    plugin_id = "dev.ouro.configuration_playground"
    async with open_local_plugins(tmp_path / "home") as service:
        validation = service.validate(EXAMPLES / "configuration-playground")
        assert validation.valid, validation.diagnostics
        assert validation.runtime_kind == "declarative"
        service.install(EXAMPLES / "configuration-playground")
        assert service.show(plugin_id).contributions == {}
        view = service.configuration(plugin_id)
        assert view.issues[0].code == "required_credential"
        values = dict(view.values)
        assert values["retry"] == {"count": 2, "delay": 0.5}
        for key, schema in view.schema_document["properties"].items():
            if "examples" in schema:
                values[key] = schema["examples"][0]
        candidate = view.candidate(
            values=values, credential_refs={"example_key": "env:OURO_CONFIGURATION_EXAMPLE_KEY"}
        )
        assert service.preview_configuration(candidate).valid
        assert (await service.configure(candidate)).outcome == "saved"
        assert (await service.enable(plugin_id)).status == "active"
        view = service.configuration(plugin_id)
        for update, code in (
            ({"attempts": -1}, "minimum"),
            ({"labels": ["same", "same"]}, "uniqueItems"),
            ({"pair": ["x", 1, 2]}, "items"),
        ):
            invalid = service.preview_configuration(
                view.candidate(values={**view.values, **update})
            )
            assert not invalid.valid and any(issue.code == code for issue in invalid.issues)
        values = {**view.values, "mode": "advanced"}
        values.pop("advanced_options")
        invalid = service.preview_configuration(view.candidate(values=values))
        assert any(issue.path == "/values/advanced_options" for issue in invalid.issues)
