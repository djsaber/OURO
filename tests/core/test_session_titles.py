import pytest

from ouro.core.messages import MessageRecord
from ouro.core.session_titles import fill_session_title, normalize_session_title, with_session_title
from ouro.core.state import SessionRecord


def test_title_uses_first_non_scaffold_user_text_and_limits_unicode_characters():
    session = with_session_title(SessionRecord(), None)
    messages = [
        MessageRecord.text(session_id=session.session_id, role="system", text="Instructions"),
        MessageRecord.text(
            session_id=session.session_id, role="user", text="Attachment data", scaffold=True
        ),
        MessageRecord.text(session_id=session.session_id, role="assistant", text="An answer"),
        MessageRecord.text(session_id=session.session_id, role="user", text=" \n "),
        MessageRecord.text(session_id=session.session_id, role="user", text="你好\n" * 30),
        MessageRecord.text(session_id=session.session_id, role="user", text="Another topic"),
    ]
    titled = fill_session_title(session, messages)
    assert titled.title == ("你好 " * 20).rstrip()
    assert titled.title_source == "fallback"
    assert not titled.title_locked
    assert session.title_source == "placeholder"


@pytest.mark.parametrize("source", ["fallback", "generated", "user"])
def test_input_does_not_overwrite_an_assigned_title_even_if_it_matches_placeholder(source):
    session = with_session_title(SessionRecord(), "New conversation", source=source)
    message = MessageRecord.text(session_id=session.session_id, role="user", text="New text")
    assert fill_session_title(session, [message]) is session


@pytest.mark.parametrize("value", ["", " \n ", "a" * 161])
def test_invalid_explicit_titles_are_rejected(value):
    with pytest.raises(ValueError, match="session title"):
        normalize_session_title(value)


def test_title_limit_is_applied_to_normalized_text():
    assert normalize_session_title("  " + "a" * 160 + "\n") == "a" * 160
