from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ouro.billing import LocalUsageLedger
from ouro.cli.main import main
from ouro.cli.run import build_default_runtime
from ouro.core.billing import ProviderUsageEntry
from ouro.core.errors import CompactionError


@pytest.mark.asyncio
async def test_default_runtime_records_provider_usage_not_context_estimates(tmp_path) -> None:
    runtime = build_default_runtime(tmp_path)
    result = await runtime.run("A very long prompt. " * 2_000)

    ledger = LocalUsageLedger(tmp_path)
    entries = ledger.list_entries()
    assert result.run.usage["input_tokens"] == 1
    assert len(entries) == 1
    assert entries[0].input_tokens == 1
    assert entries[0].output_tokens == 4
    assert entries[0].metadata["token_source"] == "provider_response"
    assert entries[0].session_id == result.session.session_id
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_compaction_still_records_billable_provider_usage(tmp_path) -> None:
    runtime = build_default_runtime(tmp_path)
    first = await runtime.run("first turn")
    await runtime.run("second turn", session_id=first.session.session_id)

    with pytest.raises(CompactionError):
        await runtime.compact(session_id=first.session.session_id)

    entries = LocalUsageLedger(tmp_path).list_entries()
    assert [entry.purpose for entry in entries] == ["agent", "agent", "compaction"]
    assert entries[-1].input_tokens == 1
    await runtime.close()


def test_usage_cli_filters_modifies_and_resets_entries(tmp_path, capsys) -> None:
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    entry = ProviderUsageEntry(
        entry_id="use_cli",
        occurred_at=datetime(2026, 7, 15, 8, tzinfo=UTC),
        provider_id="provider-a",
        endpoint_id="endpoint-a",
        model_id="model-a",
        session_id="ses_a",
        run_id="run_a",
        run_kind="agent",
        purpose="agent",
        request_id="request-a",
        input_tokens=10,
        output_tokens=5,
        metadata={"token_source": "provider_response"},
    )
    ledger.record(entry)

    assert (
        main(
            [
                "usage",
                "summary",
                "--home",
                str(tmp_path),
                "--since",
                "2026-07-15",
                "--until",
                "2026-07-15",
                "--model",
                "model-a",
                "--group-by",
                "model",
                "--json",
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["totals"]["total_tokens"] == 15
    assert summary["groups"][0]["key"] == "model-a"

    assert (
        main(
            [
                "usage",
                "modify",
                "use_cli",
                "--home",
                str(tmp_path),
                "--input-tokens",
                "12",
                "--note",
                "invoice correction",
                "--json",
            ]
        )
        == 0
    )
    modified = json.loads(capsys.readouterr().out)
    assert modified["entry"]["revision"] == 2
    assert modified["entry"]["input_tokens"] == 12

    assert (
        main(
            [
                "usage",
                "reset",
                "--home",
                str(tmp_path),
                "--provider",
                "provider-a",
                "--note",
                "new period",
                "--force",
                "--json",
            ]
        )
        == 0
    )
    reset = json.loads(capsys.readouterr().out)
    assert reset["affected_count"] == 1
    assert LocalUsageLedger(tmp_path).list_entries() == []
