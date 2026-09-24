from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.cli.main import main
from ouro.cli.run import build_default_runtime
from ouro.core.billing import ProviderUsageEntry, UsageFilter
from ouro.core.errors import CompactionError
from tests.application.test_billing_attribution import record


@pytest.mark.parametrize(
    "costs,expected",
    [
        ([None], "Estimated cost (USD): -"),
        ([None, 0.2], "Estimated cost (USD): 0.20000000 (partial)"),
        ([0.0], "Estimated cost (USD): 0.00000000"),
    ],
)
def test_cli_unknown_cost_count_and_recorded_billing_modes(tmp_path, capsys, costs, expected):
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    service = UsageService(ledger)
    for index, cost in enumerate(costs):
        record(service, str(index), "subscription" if cost is None else "usage_based", cost)
    args = ["--home", str(tmp_path)]
    assert main(["usage", "summary", *args]) == 0
    text = capsys.readouterr().out
    assert expected in text
    assert f"Unpriced requests: {costs.count(None)}" in text
    assert "Billing modes:" in text
    assert main(["usage", "summary", "--group-by", "billing_mode", "--json", *args]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["totals"]["unpriced_request_count"] == costs.count(None)
    assert {group["key"][0] for group in result["groups"]} == set(result["totals"]["billing_modes"])
    assert main(["usage", "list", *args]) == 0
    assert "billing_mode=" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_default_runtime_records_provider_usage_not_context_estimates(tmp_path) -> None:
    runtime = build_default_runtime(tmp_path)
    result = await runtime.run("A very long prompt. " * 2_000)

    ledger = LocalUsageLedger(tmp_path)
    entries = ledger.list_entries(UsageFilter()).entries
    assert result.run.usage["input_tokens"] == 1
    assert len(entries) == 1
    assert entries[0].input_tokens == 1
    assert entries[0].output_tokens == 4
    assert entries[0].metadata["token_source"] == "provider_response"
    assert entries[0].session_id == result.session.session_id
    assert entries[0].workspace == result.session.workspace == runtime.default_workspace
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_compaction_still_records_billable_provider_usage(tmp_path) -> None:
    runtime = build_default_runtime(tmp_path)
    first = await runtime.run("first turn")
    await runtime.run("second turn", session_id=first.session.session_id)

    with pytest.raises(CompactionError):
        await runtime.compact(session_id=first.session.session_id)

    entries = LocalUsageLedger(tmp_path).list_entries(UsageFilter()).entries
    assert [entry.purpose for entry in entries] == ["compaction", "agent", "agent"]
    assert entries[0].input_tokens == 1
    await runtime.close()


async def test_runtime_workspace_attribution_is_independent_of_session_state(tmp_path):
    first_workspace, second_workspace = tmp_path / "first", tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    runtime = build_default_runtime(tmp_path / "home")
    try:
        first = await runtime.run("first workspace", workspace=first_workspace)
        await runtime.run("second workspace", workspace=second_workspace)
        with runtime.store.session_mutation(first.session.session_id):
            session = runtime.store.load_session_record(first.session.session_id)
            runtime.store.save_session(
                session.model_copy(update={"workspace": str(second_workspace.resolve())})
            )
        service = UsageService(LocalUsageLedger(tmp_path / "home"))
        summary = service.summarize(group_by=("workspace",))
        assert {group.key[0]: group.totals.request_count for group in summary.groups} == {
            str(first_workspace.resolve()): 1,
            str(second_workspace.resolve()): 1,
        }
        assert (
            service.summarize(
                UsageFilter(workspaces={str(first_workspace.resolve())})
            ).totals.request_count
            == 1
        )
    finally:
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
    assert summary["groups"][0]["key"] == ["model-a"]

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
    assert LocalUsageLedger(tmp_path).list_entries(UsageFilter()).entries == ()


def test_cli_combines_workspace_filters_groups_and_page_cursors(tmp_path, capsys):
    workspace = str((tmp_path / "project").resolve())
    service = UsageService(LocalUsageLedger(tmp_path))
    from ouro.core.provider import ModelMetadata, Usage
    from ouro.core.state import RunRecord

    for index in range(3):
        service.record_provider_usage(
            run=RunRecord(session_id="session", run_id="run"),
            model=ModelMetadata(
                provider_id="provider", model_id="model", billing_mode="usage_based"
            ),
            usage=Usage(input_tokens=5, output_tokens=2),
            request_id=str(index),
            purpose="agent",
            workspace=workspace,
        )
    args = ["--home", str(tmp_path), "--json"]
    filters = ["--workspace", workspace, "--provider", "provider", "--billing-mode", "usage_based"]
    assert (
        main(
            [
                "usage",
                "summary",
                *args,
                *filters,
                "--group-by",
                "workspace",
                "--group-by",
                "provider",
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["group_by"] == ["workspace", "provider"]
    assert summary["groups"][0]["key"] == [workspace, "provider"]
    assert summary["totals"]["total_tokens"] == 21
    assert main(["usage", "list", *args, *filters, "--limit", "2"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                "usage",
                "list",
                *args,
                *filters,
                "--limit",
                "2",
                "--before",
                json.dumps(first["next_cursor"]),
            ]
        )
        == 0
    )
    last = json.loads(capsys.readouterr().out)
    assert len(first["entries"]) == 2 and len(last["entries"]) == 1
    assert not last["truncated"] and last["next_cursor"] is None
    assert main(["usage", "audit", *args, "--limit", "2"]) == 0
    audit = json.loads(capsys.readouterr().out)
    assert main(["usage", "audit", *args, "--before", str(audit["next_cursor"])]) == 0
    assert len(json.loads(capsys.readouterr().out)["events"]) == 1
