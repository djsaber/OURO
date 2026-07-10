import pytest

from ouro.core.hooks import HookContext, HookRegistration, HookRegistry, HookResult


@pytest.mark.asyncio
async def test_hook_ordering_and_context_merge() -> None:
    registry = HookRegistry()

    async def official_handler(context: HookContext) -> HookResult:
        assert context.data["value"] == 1
        return HookResult(added_context=["official"])

    def third_party_handler(context: HookContext) -> HookResult:
        assert context.run_id == "run_1"
        return HookResult(added_context=["third"])

    registry.register(
        HookRegistration(
            plugin_id="third",
            hook_name="after_context_build",
            source="third_party",
            priority=0,
            handler=third_party_handler,
        )
    )
    registry.register(
        HookRegistration(
            plugin_id="official",
            hook_name="after_context_build",
            source="official",
            priority=100,
            handler=official_handler,
        )
    )

    result = await registry.dispatch(
        HookContext(
            hook_name="after_context_build",
            session_id="ses_1",
            run_id="run_1",
            data={"value": 1},
        )
    )

    assert [item.added_context[0] for item in result.results] == ["official", "third"]
    assert result.failures == []


@pytest.mark.asyncio
async def test_third_party_hook_failure_is_isolated_for_run() -> None:
    registry = HookRegistry()
    calls = 0

    def failing_handler(context: HookContext) -> HookResult:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    registry.register(
        HookRegistration(
            plugin_id="bad",
            hook_name="on_event",
            source="third_party",
            handler=failing_handler,
        )
    )

    context = HookContext(hook_name="on_event", session_id="ses_1", run_id="run_1")
    first = await registry.dispatch(context)
    second = await registry.dispatch(context)

    registry.finish_run("run_1")
    after_finish = await registry.dispatch(context)

    assert len(first.failures) == 1
    assert second.failures == []
    assert len(after_finish.failures) == 1
    assert calls == 2
