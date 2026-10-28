"""Publish literal request fixtures through the explicit manifest write port."""

from ouro.core.context_snapshots import ContextSnapshotRecord, RequestManifestRecord
from ouro.core.request_manifest import RequestManifest, RequestMessage
from ouro.core.session_commits import SessionCommit
from ouro.sessions import SessionStore


def save_context(store: SessionStore, value: ContextSnapshotRecord) -> None:
    store.commit_session(
        SessionCommit(
            before=store.load_session_record(value.session_id),
            request_header=value.header(),
            request_manifest=RequestManifestRecord(
                context_id=value.context_id,
                supports_image_input=value.supports_image_input,
                supports_audio_input=value.supports_audio_input,
                supports_video_input=value.supports_video_input,
                manifest=RequestManifest(
                    system_prompt=value.provider_context.system_prompt,
                    messages=[
                        RequestMessage.prepared(message)
                        for message in value.provider_context.messages
                    ],
                    tools=value.provider_context.tools,
                ),
            ),
        )
    )
