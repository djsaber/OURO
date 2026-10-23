"""TUI policies for interrupted submissions and retained welcome resources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ouro.tui.composer.drafts import TuiInputDrafts

if TYPE_CHECKING:
    from ouro.tui.controller import TuiController


async def recover_input_drafts(
    controller: TuiController, drafts: TuiInputDrafts
) -> tuple[TuiInputDrafts, str | None]:
    resume = None
    for pending in drafts.pending:
        accepted = await controller.input_accepted(pending.session_id, pending.input_id)
        drafts = drafts.finish_submission(pending.input_id)
        if accepted:
            if pending.origin_session_id is None:
                resume = pending.session_id
                drafts = drafts.model_copy(update={"welcome_resource_id": None})
        else:
            content = drafts.content_for(pending.origin_session_id).followed_by(pending)
            drafts = drafts.remember(
                pending.origin_session_id,
                content.text,
                attachments=content.attachments,
                skills=content.skills,
            )
    return drafts, resume


async def release_unused_draft_resources(
    controller: TuiController, drafts: TuiInputDrafts, *, remember: bool
) -> TuiInputDrafts:
    identity = drafts.welcome_resource_id
    if identity is None:
        return drafts
    retained: list[str] = []
    if remember:
        retained.extend(drafts.welcome.resource_refs)
        for pending in drafts.pending:
            if pending.session_id == identity:
                retained.extend(pending.resource_refs)
    retain = tuple(dict.fromkeys(retained))
    await controller.release_input_resources(identity, retain)
    return drafts if retain else drafts.model_copy(update={"welcome_resource_id": None})
