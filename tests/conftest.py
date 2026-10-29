from __future__ import annotations

import pytest

from ouro.auth.drivers.openai_callback import CallbackReceiver


@pytest.fixture
def openai_callback(monkeypatch):
    """Isolate the real listener while retaining the driver's fixed protocol URI.

    The first bind reserves an OS-assigned port. Subsequent binds in this test
    reuse it, so cleanup and deliberate port-conflict tests still exercise real
    socket ownership without racing other pytest workers on port 1455.
    """
    start = CallbackReceiver.start
    port = None

    async def isolated_start(receiver, requested_port):
        nonlocal port
        assert requested_port == 1455
        await start(receiver, 0 if port is None else port)
        port = receiver.port

    monkeypatch.setattr(CallbackReceiver, "start", isolated_start)

    def callback_url():
        assert port is not None
        return f"http://127.0.0.1:{port}/auth/callback"

    return callback_url


@pytest.fixture(autouse=True)
def capture_runtime_notifications(monkeypatch):
    """Business assertions inspect online publication explicitly, without event storage."""
    from ouro.application.events import RuntimeEventRecorder
    from tests.event_capture import captured

    captured.clear()
    publish = RuntimeEventRecorder._publish

    async def capture(recorder, event, *, notify_observers):
        delivering = not recorder._closed or not notify_observers
        await publish(recorder, event, notify_observers=notify_observers)
        if delivering and event.stream is None:
            captured[event.session_id].append(event.model_copy())

    monkeypatch.setattr(RuntimeEventRecorder, "_publish", capture)
    yield
    captured.clear()
