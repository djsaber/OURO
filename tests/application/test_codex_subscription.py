from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ouro.application.configuration import ConfigurationFacade
from ouro.automation.models import TaskConfiguration, TaskSchedule
from ouro.config import ProviderConfigManager
from ouro.core.base import utc_now
from ouro.local_application import LocalApplication, open_local_provider_auth
from ouro.tools import Tool, ToolDefinition, ToolResult
from tests.auth.openai_fixtures import tokens
from tests.providers.responses_fixtures import call, item_event, message, reasoning, terminal


@pytest.mark.parametrize("base_url", ["https://api.openai.com/v1", "https://elsewhere.invalid"])
async def test_subscription_target_override_fails_before_credentials_or_network(
    tmp_path, monkeypatch, base_url
):
    facade = ConfigurationFacade(tmp_path)
    created = facade.add_provider(
        display_name="Personal Codex",
        provider_type="openai-codex",
        make_default=True,
    )
    provider_id = created.changed_provider_id
    assert provider_id is not None
    facade.update_provider(provider_id, base_url=base_url)

    def forbidden_client(**kwargs):
        pytest.fail("rejected subscription target must not create a network client")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)
    async with LocalApplication.build(tmp_path) as app:
        result = await app.runtime.run("Do not send this request.")
        assert result.run.status == "failed"
        assert any(
            event.payload.get("error_code") == "authentication_target_rejected"
            for event in result.events
        )


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_builtin_subscription_login_persist_restart_refresh_tools_and_logout(
    tmp_path, monkeypatch, backend
):
    model_requests, token_requests, clients, executions = [], [], [], []
    replies = deque()

    def respond(request):
        assert request.url.scheme == "https"
        if request.url.host == "auth.openai.com":
            assert request.url.path == "/oauth/token"
            form = parse_qs(request.content.decode())
            token_requests.append(form)
            assert form["client_id"] == ["app_EMoamEEZ73f0CkXaXp7hrann"]
            if form["grant_type"] == ["authorization_code"]:
                return httpx.Response(200, json=tokens())
            assert form["refresh_token"] == ["refresh-secret"]
            return httpx.Response(
                200, json=tokens(access_token="rotated-secret", refresh_token="rotated-refresh")
            )
        assert str(request.url) == "https://chatgpt.com/backend-api/codex/responses"
        payload = json.loads(request.content)
        model_requests.append((payload, dict(request.headers)))
        assert "max_output_tokens" not in payload and "truncation" not in payload
        assert payload["reasoning"] == {"effort": "medium"}
        assert "temperature" not in payload
        assert payload["stream"] is True and payload["store"] is False
        reply = replies.popleft()
        if isinstance(reply, int):
            return httpx.Response(reply)
        items = reply["response"].pop("output")
        events = [item_event(index, item) for index, item in enumerate(items)] + [reply]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
        )

    original_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(respond))
        client = original_client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    facade = ConfigurationFacade(tmp_path)
    created = facade.add_provider(
        display_name="Personal Codex",
        provider_type="openai-codex",
        make_default=True,
    )
    provider_id = created.changed_provider_id
    assert provider_id is not None
    configured = facade.provider(provider_id)
    gpt_55_endpoint_id = next(
        endpoint.endpoint_id for endpoint in configured.endpoints if endpoint.model_id == "gpt-5.5"
    )
    assert configured.auth_driver == "openai-codex"
    assert "local planning budget" in configured.notice
    assert configured.endpoints[0].reasoning.capability.continuation == "opaque"
    assert not configured.endpoints[0].supports_vision
    assert not model_requests and not token_requests

    # Unattended consumers fail without opening an authorization flow or sending HTTP.
    async with LocalApplication.build(tmp_path, session_backend=backend) as app:
        unattended = await app.runtime.run("Reply OK.", interaction_mode="unattended")
        assert unattended.run.status == "failed"
        assert any(
            event.payload.get("error_code") == "authentication_required"
            for event in unattended.events
        )
        await app.task_scheduler.stop()
        due = utc_now().replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
        app.tasks.clock = lambda: due
        task = await app.tasks.create(
            TaskConfiguration(
                workspace=str(tmp_path),
                model=f"{provider_id}/{gpt_55_endpoint_id}",
                description="Reply OK.",
                schedule=TaskSchedule(frequency="daily", starts_at=due, ends_at=due),
            )
        )
        execution = app.task_scheduler.execution
        await execution.tick()
        async with asyncio.timeout(10):
            while (await app.tasks.get(task.task_id)).status != "failed":
                await execution.reconcile()
                await asyncio.sleep(0.01)
        scheduled = (await app.tasks.executions(task.task_id))[0]
        snapshot = app.runtime.store.load_session(scheduled.session_id)
        assert snapshot.session.interaction_mode == "unattended"
        assert any(
            event.payload.get("error_code") == "authentication_required"
            for event in snapshot.events
        )
        assert not model_requests and not token_requests

    # Exercise the real lightweight composition, loopback callback and file commit.
    async with open_local_provider_auth(tmp_path) as auth:
        assert (await auth.status(provider_id)).state == "missing"
        assert [method.method_id for method in await auth.methods(provider_id)] == ["browser"]
        challenge = await auth.begin(provider_id, "browser")
        query = parse_qs(urlsplit(str(challenge.challenge.authorization_url)).query)
        async with original_client(
            transport=httpx.AsyncHTTPTransport(trust_env=False), trust_env=False
        ) as local:
            response = await local.get(
                "http://127.0.0.1:1455/auth/callback",
                params={"state": query["state"][0], "code": "fixture-code"},
            )
            assert response.status_code == 200
        assert (await auth.wait(challenge.operation_id)).state == "succeeded"
        assert (await auth.status(provider_id)).credential.revision == 1

    def echo(invocation, context):
        executions.append(invocation.arguments["round"])
        return ToolResult(
            tool_call_id=invocation.tool_call_id, tool_name="echo", content="fixture result"
        )

    async with LocalApplication.build(tmp_path, session_backend=backend) as app:
        for session_id, run_id in (
            (unattended.session.session_id, unattended.run.run_id),
            (scheduled.session_id, scheduled.run_id),
        ):
            replies.append(terminal([message("recovered")]))
            recovered = await app.runtime.continue_run(session_id=session_id)
            assert recovered.run.status == "completed", recovered.run.error
            assert recovered.run.run_id == run_id
            assert recovered.final_text == "recovered"
            assert recovered.session.interaction_mode == "unattended"
        app.runtime.tools.register(
            Tool(
                definition=ToolDefinition(
                    name="echo",
                    description="Echo round",
                    effect="read_only",
                    parameters_schema={
                        "type": "object",
                        "properties": {"round": {"type": "integer"}},
                        "required": ["round"],
                    },
                ),
                handler=echo,
            )
        )
        replies.extend(
            [
                terminal([reasoning(), call(1)]),
                terminal([call(2)]),
                terminal([message("finished")]),
            ]
        )
        first = await app.runtime.run("Use echo twice.")
        assert first.run.status == "completed", first.run.error
        assert executions == [1, 2]
        session_id = first.session.session_id
        saved = app.runtime.store.load_session(session_id).messages
        assert app.runtime.adapter.model.max_output_tokens == 32_768
        context = app.runtime.store.load_latest_context(session_id)
        assert context.metrics.reserved_output_tokens == 32_768
        for key in first.run.usage_entry_ids:
            entry = app.runtime.usage.ledger.get(key)
            assert entry.billing_mode == "subscription" and entry.estimated_cost is None

    # Reconstruct with built-in registration only: persisted credentials/history are sufficient.
    async with LocalApplication.build(tmp_path, session_backend=backend) as app:
        assert app.runtime.store.load_session(session_id).messages == saved
        replies.extend([401, terminal([message("continued", item_id="next")])])
        continued = await app.runtime.run("Continue.", session_id=session_id)
        assert continued.run.status == "completed", continued.run.error
        assert continued.final_text == "continued"
        assert model_requests[-2][0] == model_requests[-1][0]
        assert model_requests[-2][1]["authorization"] == "Bearer access-secret"
        assert model_requests[-1][1]["authorization"] == "Bearer rotated-secret"
        assert reasoning() in model_requests[-1][0]["input"]
        assert (await app.provider_auth.status(provider_id)).credential.revision == 2
        assert executions == [1, 2]
        await app.provider_auth.logout(provider_id)
        count = len(model_requests)
        failed = await app.runtime.run("After logout.", session_id=session_id)
        assert failed.run.status == "failed"
        assert any(e.payload.get("error_code") == "authentication_required" for e in failed.events)
        assert len(model_requests) == count
        public = app.runtime.store.load_session(session_id).model_dump_json()
        public += facade.provider(provider_id).model_dump_json()
        assert all(
            secret not in public for secret in ("access-secret", "refresh-secret", "rotated-secret")
        )

    assert len(token_requests) == 2 and not replies
    assert all(client.is_closed for client in clients)
    async with open_local_provider_auth(tmp_path) as auth:
        assert (await auth.status(provider_id)).state == "logged_out"
    provider = ProviderConfigManager(tmp_path).get_provider(provider_id)
    assert provider is not None and provider.credential_ref.type == "managed"
