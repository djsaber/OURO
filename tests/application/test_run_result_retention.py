from __future__ import annotations

import gc
import weakref

import pytest

from ouro.application.runs.drivers import NativeRunDriverContext
from ouro.core.results import RunResult
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from tests.application.test_run_drivers import DRIVER, PLUGIN, host, plugin_source, set_driver_body


@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
@pytest.mark.parametrize("rounds", [3, 30])
async def test_driver_releases_previous_batch_results_and_returns_only_a_summary(
    tmp_path, monkeypatch, store_type, rounds
):
    store = store_type(tmp_path / "sessions.db" if store_type is SQLiteSessionStore else tmp_path)
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls, mode="auto", content="x" * 65536)
    package = plugin_source(tmp_path, "python-inprocess")
    set_driver_body(
        package,
        f"""
async def review(context, resume):
    for index in range({rounds}):
        output = await context.tools.execute(
            (sdk.RunToolCall("Deploy"),), checkpoint=index
        )
        assert output.results[0].content == "x" * 65536
    return sdk.DriverFinished("done")
""",
    )
    service.install(package)
    await service.enable(PLUGIN)
    previous = []
    accepted = 0
    accept = NativeRunDriverContext._accept_tool_result

    async def capture(context, result):
        nonlocal accepted
        gc.collect()
        assert not any(reference() is not None for reference in previous)
        previous[:] = [weakref.ref(item) for item in result.results]
        assert previous
        accepted += 1
        return await accept(context, result)

    monkeypatch.setattr(NativeRunDriverContext, "_accept_tool_result", capture)
    try:
        result = await runtime.run("repeat independent operations", driver_id=DRIVER)
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "done"
        assert accepted == rounds
        assert len(calls) == rounds
        assert not result.had_tool_error
        assert not {"events", "tool_results", "usage_entry_ids"} & RunResult.model_fields.keys()
        gc.collect()
        assert not any(reference() is not None for reference in previous)
    finally:
        await runtime.close()
        await plugins.close()
