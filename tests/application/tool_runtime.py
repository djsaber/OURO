from __future__ import annotations

import json

import pytest

from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent, ToolCallDelta
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    result = (
        LocalSessionStore(tmp_path / "files")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    result.initialize()
    return result


def runtime(store, tool, arguments):
    registry = ToolRegistry()
    registry.register(tool)
    return RuntimeService(
        store=store,
        tools=registry,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent(
                            type="tool_call_delta",
                            tool_call_delta=ToolCallDelta(
                                index=0,
                                id_delta="call_state",
                                name_delta=tool.definition.name,
                                arguments_delta=json.dumps(arguments),
                            ),
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="finished"),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
    )
