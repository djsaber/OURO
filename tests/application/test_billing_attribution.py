from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from pydantic import ValidationError

from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.config import provider_models
from ouro.config.provider_models import ProviderConnectionSpec, ProviderTypeSpec
from ouro.core.billing import ProviderUsageEntry, UsageFilter
from ouro.core.errors import CompactionError, PersistenceError
from ouro.core.provider import ModelMetadata, Usage
from ouro.core.state import RunRecord
from ouro.local_application import LocalApplication
from ouro.providers.authentication import ProviderRequestPolicy
from tests.application.test_runtime_authentication import login
from tests.application.test_runtime_authentication import managed_runtime as managed_runtime


def record(service, request, mode, cost=None):
    return service.record_provider_usage(
        run=RunRecord(session_id="session", run_id="run"),
        model=ModelMetadata(model_id="model", provider_id="provider", billing_mode=mode),
        usage=Usage(input_tokens=10, output_tokens=4, cache_read_tokens=2, estimated_cost=cost),
        request_id=request,
        purpose="agent",
    )


def test_attribution_survives_reopen_correction_and_grouping_without_fabricated_cost(tmp_path):
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    service = UsageService(ledger)
    paid = record(service, "paid", "usage_based", 0.2)
    subscription = record(service, "subscription", "subscription")
    record(service, "unknown", "unknown")
    zero = record(service, "free-request", "usage_based", 0)
    assert subscription.estimated_cost is None and zero.estimated_cost == 0
    summary = service.summarize(group_by="billing_mode")
    assert [group.key for group in summary.groups] == ["subscription", "unknown", "usage_based"]
    assert summary.totals.billing_modes == ("subscription", "unknown", "usage_based")
    assert summary.totals.priced_request_count == 2 and summary.totals.unpriced_request_count == 2
    assert summary.totals.estimated_cost == 0.2
    assert summary.totals.total_tokens == 56
    assert summary.totals.cache_read_tokens == 8
    service.replace_entry(paid.entry_id, estimated_cost=0.3, note="actual amount")
    service = UsageService(LocalUsageLedger(tmp_path))
    assert service.get_entry(paid.entry_id).billing_mode == "usage_based"
    assert service.get_entry(subscription.entry_id).metadata["billing_mode"] == "subscription"
    assert record(service, "paid", "usage_based", 0.2).estimated_cost == 0.3
    with pytest.raises(PersistenceError, match="billing mode conflicts"):
        record(service, "paid", "subscription", 0.2)
    with pytest.raises(PersistenceError, match="billing mode conflicts"):
        record(service, "subscription", "unknown")
    service.reset(UsageFilter(), note="clear")
    assert service.summarize().totals.billing_modes == ()


def test_billing_mode_is_typed_and_absence_is_unknown(tmp_path):
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    entry = record(UsageService(ledger), "r", "unknown")
    data = entry.model_dump()
    data["metadata"] = {}
    assert ProviderUsageEntry.model_validate(data).billing_mode == "unknown"
    data["metadata"] = {"billing_mode": "oauth"}
    with pytest.raises(ValidationError):
        ProviderUsageEntry.model_validate(data)
    with pytest.raises(ValidationError):
        ModelMetadata(model_id="m", provider_id="p", billing_mode="oauth")


async def test_inflight_request_keeps_old_service_attribution_after_rebinding(
    tmp_path,
    managed_runtime,
    monkeypatch,
):
    configs, drivers, policies, _ = managed_runtime
    # Both connections use managed auth, but their service billing declarations differ.
    monkeypatch.setitem(
        provider_models._PROVIDER_TYPE_SPECS,
        "custom",
        ProviderTypeSpec(
            "custom",
            ProviderConnectionSpec("openai-chat-completions"),
            (
                ProviderConnectionSpec(
                    "openai-chat-completions", "fixture-browser", "subscription"
                ),
                ProviderConnectionSpec("openai-chat-completions", "fixture-device", "metered"),
            ),
        ),
    )
    policies = (
        replace(
            policies[0],
            policy_id="subscription",
            requests=ProviderRequestPolicy(billing_mode="subscription"),
        ),
        replace(
            policies[0],
            policy_id="metered",
            requests=ProviderRequestPolicy(billing_mode="usage_based"),
        ),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    application = LocalApplication.build(tmp_path, auth_drivers=drivers, service_policies=policies)
    transport = application.runtime.adapter.provider.transport
    send = transport.post_json

    async def delayed_response(**request):
        entered.set()
        await release.wait()
        response = await send(**request)
        response["usage"] = {"prompt_tokens": 10, "completion_tokens": 4}
        return response

    monkeypatch.setattr(transport, "post_json", delayed_response)
    async with application:
        await login(application.provider_auth)
        run = asyncio.create_task(application.runtime.run("first"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            configs.replace_provider(
                "subscription",
                provider_type="custom",
                protocol="openai-chat-completions",
                auth_driver="fixture-device",
                base_url="https://fixture.invalid/api",
            )
            configs.upsert_endpoint(
                "subscription", "chat", model_id="chat", supports_streaming=False
            )
            configs.set_default_route("subscription", "chat")
            new_adapter = application.runtime.models.adapter_for("subscription/chat")
            assert new_adapter.model.billing_mode == "usage_based"
            release.set()
            first = await run
        finally:
            release.set()
            await run
        ledger = UsageService(LocalUsageLedger(tmp_path))
        entries = ledger.list_entries().entries
        assert first.run.status == "completed"
        assert len(entries) == 1 and entries[0].billing_mode == "subscription"
        assert entries[0].estimated_cost is None
        assert first.run.usage["input_tokens"] == 10
        assert "billing_mode" not in first.run.usage
        await login(application.provider_auth)
        # Capture the new request on its newly constructed transport too.
        monkeypatch.setattr(new_adapter.provider.transport, "post_json", delayed_response)
        second = await application.runtime.run("second")
        assert second.run.status == "completed"
        assert ledger.summarize().totals.billing_modes == ("subscription", "usage_based")
        assert ledger.summarize().totals.unpriced_request_count == 2


async def test_failed_dedicated_compaction_keeps_service_mode_and_actual_tokens(
    tmp_path,
    managed_runtime,
    monkeypatch,
):
    _, drivers, policies, _ = managed_runtime
    policies = (replace(policies[0], requests=ProviderRequestPolicy(billing_mode="subscription")),)
    application = LocalApplication.build(tmp_path, auth_drivers=drivers, service_policies=policies)
    transport_type = type(application.runtime.adapter.provider.transport)

    async def response(self, **request):
        return {
            "choices": [{"message": {"content": "invalid summary"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "prompt_tokens_details": {"cached_tokens": 2},
            },
        }

    monkeypatch.setattr(transport_type, "post_json", response)
    async with application:
        await login(application.provider_auth)
        first = await application.runtime.run("first")
        await application.runtime.run("second", session_id=first.session.session_id)
        with pytest.raises(CompactionError):
            await application.runtime.compact(session_id=first.session.session_id)
        entries = LocalUsageLedger(tmp_path).list_entries()
        compaction = [entry for entry in entries if entry.purpose == "compaction"]
        assert compaction and compaction[0].endpoint_id == "summary"
        assert all(entry.billing_mode == "subscription" for entry in entries)
        assert all(entry.estimated_cost is None for entry in entries)
        assert all(entry.total_tokens == 14 and entry.cache_read_tokens == 2 for entry in entries)
