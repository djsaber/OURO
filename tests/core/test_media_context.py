import pytest

from ouro.core.base import utc_now
from ouro.core.errors import MediaCapacityError
from ouro.core.media_context import MediaElisionState, plan_media_elision
from ouro.core.messages import MessageRecord, ModelResponseRecord, ToolResultReference
from ouro.core.tools import ToolCallRequest


def frame(index, count=1):
    now = utc_now()
    calls = [ToolCallRequest(id=f"call-{index}-{j}", name="ViewImage") for j in range(count)]
    common = dict(session_id="s", run_id="r", turn_id=f"turn-{index}")
    return [
        MessageRecord(
            **common,
            role="assistant",
            tool_calls=calls,
            model_response=ModelResponseRecord(
                provider_id="p", model_id="m", started_at=now, ended_at=now
            ),
        ),
        *[
            MessageRecord(
                **common,
                role="tool_result",
                tool_result=ToolResultReference(tool_call_id=call.id, tool_name=call.name),
            )
            for call in calls
        ],
    ]


def test_overflow_elides_whole_batches_and_leaves_headroom_for_stable_appends():
    frames = [frame(i, count=2) for i in range(4)]
    history = [m for batch in frames for m in batch]
    sizes = {m.message_id: 5 for m in history if m.role == "tool_result"}
    state = MediaElisionState(history_epoch=0)
    selected = plan_media_elision(history, sizes, state, limit=30)
    assert selected.media_elided_through_message_id == frames[2][-1].message_id
    assert state.media_elided_through_message_id is None
    assert plan_media_elision(history, sizes, selected, limit=30) == selected
    extra = frame(4)
    sizes[extra[-1].message_id] = 10
    assert plan_media_elision([*history, *extra], sizes, selected, limit=30) == selected


@pytest.mark.parametrize("failure", [False, True])
def test_pending_tool_batch_and_later_user_input_cannot_be_elided(failure):
    history = frame(0, count=2)
    user = MessageRecord.text(session_id="s", role="user", text="also check this")
    history.append(user)
    sizes = {history[1].message_id: 10, history[2].message_id: 10, user.message_id: 15}
    if failure:
        failed = frame(1)[0]
        failed.tool_calls = []
        failed.metadata = {"incomplete": True}
        history.append(failed)
    with pytest.raises(MediaCapacityError, match="35 encoded characters"):
        plan_media_elision(history, sizes, MediaElisionState(history_epoch=0), limit=30)


def test_required_new_batch_can_exceed_retention_target_without_exceeding_capacity():
    old, new = frame(0), frame(1, count=2)
    sizes = {old[-1].message_id: 10, new[1].message_id: 11, new[2].message_id: 11}
    selected = plan_media_elision([*old, *new], sizes, MediaElisionState(history_epoch=0), limit=30)
    assert selected.media_elided_through_message_id == old[-1].message_id


def test_incomplete_exchange_protects_suffix_even_with_a_later_reply():
    incomplete, new = frame(0), frame(1)
    history = [incomplete[0], *new]
    sizes = {new[-1].message_id: 40}
    with pytest.raises(MediaCapacityError):
        plan_media_elision(history, sizes, MediaElisionState(history_epoch=0), limit=30)


def test_unidentified_history_is_protected_without_a_model_reply():
    history = [MessageRecord.text(session_id="s", role="user", text=str(i)) for i in range(3)]
    with pytest.raises(MediaCapacityError):
        plan_media_elision(
            history,
            {m.message_id: 12 for m in history},
            MediaElisionState(history_epoch=0),
            limit=30,
        )
