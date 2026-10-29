from __future__ import annotations

import json

from ouro.application.runtime.service import RuntimeService
from ouro.cli.run import run_json
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.providers.faux import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry
from tests.event_capture import recorded_events


async def test_provider_error_code_survives_runtime_persistence_and_cli_json(
    tmp_path, monkeypatch, capsys
):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent(
                            type="error",
                            error="Login required.",
                            error_code="authentication_required",
                        ),
                    ]
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
    )
    monkeypatch.setattr("ouro.cli.run.build_default_runtime", lambda *args, **kwargs: runtime)
    assert await run_json("hello", home=tmp_path) == 1
    result = json.loads(capsys.readouterr().out)
    assert "events" not in result
    assert result["error_code"] == "authentication_required"
    assert result["error"] == "Login required."
    persisted = store.load_session(result["session_id"])
    assert persisted.runs[-1].error_code == result["error_code"]
    assert recorded_events(persisted.session.session_id)[-1].payload == {
        "error": result["error"],
        "error_code": result["error_code"],
    }
