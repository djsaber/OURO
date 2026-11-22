"""Host-neutral SDK wiring for the UI Workbench example."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from ouro.sdk.plugins import v6 as sdk

from .browser import CAPTURE_PARAMETERS, capture
from .comparison import build_comparison
from .files import WorkbenchError, publish_bundle
from .image import IMAGE_PARAMETERS, image
from .media import MEDIA_PARAMETERS, media
from .settings import IMAGE_CREDENTIAL, Settings

_SKILL = "ui-workbench"
_COMPARE_PARAMETERS = {
    "type": "object",
    "properties": {"manifest": {"type": "string", "minLength": 1, "maxLength": 1024}},
    "required": ["manifest"],
    "additionalProperties": False,
}
_EMPTY_PARAMETERS = {"type": "object", "additionalProperties": False}

_EXPLORE = (
    "Load the ui-workbench skill. Explore three interface directions that differ in layout, "
    "hierarchy, and interaction rather than just decoration. Create three self-contained "
    "first-screen HTML prototypes in the workspace and a comparison manifest. Build the offline "
    "board with ui_workbench__compare, describe the main trade-off of each direction, and ask the "
    "user to choose before changing the actual implementation."
)
_REVIEW = (
    "Load the ui-workbench skill. Review the requested interface in two passes: visual hierarchy "
    "and usability. When approved and configured, capture desktop and mobile evidence with "
    "ui_workbench__capture. Check one relevant keyboard or pointer flow, distinguish measured "
    "findings from manual judgments and untested cases, and report prioritized issues with "
    "evidence. Do not modify the implementation during the review. Ask for a target if unclear."
)


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _workspace(value: str | None) -> Path:
    if value is None:
        raise WorkbenchError("This operation needs a session workspace or an explicit --workspace.")
    return Path(value)


def _workflow(
    call: sdk.CommandCall, instructions: str, *, require_brief: bool
) -> sdk.CommandOutput:
    brief = call.argument.strip()
    if require_brief and not brief:
        return sdk.CommandOutput(
            output="Provide a design brief: /dev.ouro.examples.ui_workbench:explore <brief>"
        )
    if len(brief) > 6000:
        return sdk.CommandOutput(output="Keep the command argument under 6,000 characters.")
    inputs: tuple[sdk.SessionInput, ...] = (
        sdk.SessionInput(prompt=instructions, semantic_tag="ui-workbench"),
    )
    if brief:
        inputs += (sdk.SessionInput(prompt=brief, role="user"),)
    return sdk.CommandOutput(
        output="UI Workbench follow-up requested.",
        session_input_batches=(sdk.SessionInputBatch(inputs=inputs),),
    )


def _explore(call: sdk.CommandCall) -> sdk.CommandOutput:
    return _workflow(call, _EXPLORE, require_brief=True)


def _review(call: sdk.CommandCall) -> sdk.CommandOutput:
    return _workflow(call, _REVIEW, require_brief=False)


class WorkbenchPlugin:
    def __init__(self, context: sdk.PluginContext) -> None:
        self._context = context
        self._settings = Settings.from_configuration(context.configuration)
        self._root = Path(context.artifact_root) if context.artifact_root is not None else None
        self._capture_lock = asyncio.Lock()

    async def prepare(self) -> sdk.ContributionSet:
        if self._root is None:
            raise WorkbenchError("UI Workbench needs a host with a local artifact root.")
        return sdk.ContributionSet(
            commands=[
                sdk.CommandContribution(
                    "status",
                    "Show local UI Workbench capability and configuration status.",
                    self._status_command,
                ),
                sdk.CommandContribution(
                    "compare",
                    "Build an offline comparison board from a workspace manifest.",
                    self._compare_command,
                    usage="<manifest.json>",
                ),
                sdk.CommandContribution(
                    "explore",
                    "Start a three-direction interface design exploration.",
                    _explore,
                    usage="<design brief>",
                ),
                sdk.CommandContribution(
                    "review",
                    "Start a visual and usability review of an interface.",
                    _review,
                    usage="[page or review focus]",
                ),
            ],
            tools=[
                _tool(
                    "status",
                    "Read UI Workbench capability and configuration status without network access.",
                    self._status_tool,
                    _EMPTY_PARAMETERS,
                    read_only=True,
                ),
                _tool(
                    "compare",
                    "Package workspace HTML candidates into an offline comparison board.",
                    self._compare_tool,
                    _COMPARE_PARAMETERS,
                ),
                _tool(
                    "capture",
                    "Capture and inspect a trusted UI in a local browser across viewports "
                    "and interaction states.",
                    self._capture_tool,
                    CAPTURE_PARAMETERS,
                    exclusive=True,
                ),
                _tool(
                    "media",
                    "Find or download public-domain collection images with rights "
                    "and attribution metadata.",
                    self._media_tool,
                    MEDIA_PARAMETERS,
                ),
                _tool(
                    "image",
                    "Generate or edit an image using the explicitly configured image API.",
                    self._image_tool,
                    IMAGE_PARAMETERS,
                ),
            ],
        )

    async def start(self) -> None:
        # Network clients belong to individual calls rather than running in the background.
        pass

    async def stop(self, reason: sdk.StopReason, deadline: datetime) -> None:
        pass

    def _status(self) -> dict[str, object]:
        return {
            "version": self._context.identity.version,
            "bundled_skill": _SKILL,
            "comparison_available": self._root is not None,
            "capture_configured": bool(self._settings.webdriver_url),
            "media_enabled": self._settings.media_enabled,
            "image_api_configured": bool(self._settings.image_api_base),
        }

    def _status_command(self, call: sdk.CommandCall) -> sdk.CommandOutput:
        if call.argument.strip():
            return sdk.CommandOutput(output="The status command does not take arguments.")
        result = self._status()
        return sdk.CommandOutput(output=_json(result), data=result)

    def _status_tool(self, call: sdk.ToolCall) -> sdk.ToolOutput:
        result = self._status()
        return sdk.ToolOutput(content=_json(result), structured=result)

    async def _compare_command(self, call: sdk.CommandCall) -> sdk.CommandOutput:
        try:
            path = call.argument.strip()
            if not path:
                raise WorkbenchError("Provide the workspace-relative comparison manifest path.")
            data = await self._build_board(call.workspace, path)
            result = self._publish("board", {"comparison.html": data})
        except WorkbenchError as exc:
            return sdk.CommandOutput(output=str(exc), data={"error": str(exc)})
        return sdk.CommandOutput(output=_json(result), data=result)

    async def _build_board(self, workspace: str | None, manifest: str) -> bytes:
        # Bounded preparation performs only reads; a cancelled await cannot publish a board.
        return await asyncio.to_thread(build_comparison, _workspace(workspace), manifest)

    async def _compare_tool(
        self, call: sdk.ToolCall, execution: sdk.ToolExecutionContext
    ) -> sdk.ToolOutput:
        try:
            manifest = call.arguments.get("manifest")
            if not isinstance(manifest, str) or not manifest.strip():
                raise WorkbenchError("Provide the workspace-relative comparison manifest path.")
            data = await self._build_board(call.workspace, manifest)
            execution.raise_if_aborted()
            return self._publish_output("board", {"comparison.html": data}, {})
        except WorkbenchError as exc:
            return _failure(exc)

    async def _capture_tool(
        self, call: sdk.ToolCall, execution: sdk.ToolExecutionContext
    ) -> sdk.ToolOutput:
        try:
            self._require_network()
            if not self._settings.webdriver_url:
                raise WorkbenchError("Set webdriver_url to enable local browser capture.")
            async with self._capture_lock:
                execution.raise_if_aborted()
                files, metadata = await capture(
                    _workspace(call.workspace),
                    call.arguments,
                    endpoint=self._settings.webdriver_url,
                    browser=self._settings.browser,
                )
            execution.raise_if_aborted()
            return self._publish_output(
                "capture", {**files, "inspection.json": _json(metadata).encode()}, metadata
            )
        except WorkbenchError as exc:
            return _failure(exc)

    async def _media_tool(
        self, call: sdk.ToolCall, execution: sdk.ToolExecutionContext
    ) -> sdk.ToolOutput:
        try:
            self._require_network()
            if not self._settings.media_enabled:
                raise WorkbenchError("Set media_enabled to use the public-domain collection.")
            files, metadata = await media(call.arguments)
            execution.raise_if_aborted()
            if not files:
                return sdk.ToolOutput(content=_json(metadata), structured=metadata)
            return self._publish_output("media", files, metadata)
        except WorkbenchError as exc:
            return _failure(exc)

    async def _image_tool(
        self, call: sdk.ToolCall, execution: sdk.ToolExecutionContext
    ) -> sdk.ToolOutput:
        try:
            self._require_network()
            if not self._settings.image_api_base:
                raise WorkbenchError("Configure image_api_base and image_model to use image tools.")
            try:
                key = self._context.credentials.resolve(IMAGE_CREDENTIAL)
            except PermissionError, RuntimeError:
                raise WorkbenchError(
                    "Bind a usable image_api_key through OURO plugin configuration."
                ) from None
            files, metadata = await image(
                Path(call.workspace) if call.workspace is not None else None,
                call.arguments,
                endpoint=self._settings.image_api_base,
                model=self._settings.image_model,
                api_key=key,
            )
            execution.raise_if_aborted()
            return self._publish_output("image", files, metadata)
        except WorkbenchError as exc:
            return _failure(exc)

    def _require_network(self) -> None:
        if "network.outbound" not in self._context.permissions:
            raise WorkbenchError(
                "This operation requires the declared network.outbound permission."
            )

    def _publish(self, label: str, files: Mapping[str, bytes]) -> dict[str, object]:
        assert self._root is not None  # Checked before the contributions become callable.
        directory = publish_bundle(self._root, label, files)
        return {"files": [str(directory / name) for name in files]}

    def _publish_output(
        self, label: str, files: Mapping[str, bytes], metadata: Mapping[str, object]
    ) -> sdk.ToolOutput:
        result = {**metadata, **self._publish(label, files)}
        return sdk.ToolOutput(content=_json(result), structured=result)


def _tool(
    tool_id: str,
    description: str,
    handler: sdk.ToolHandler,
    parameters: Mapping[str, object],
    *,
    read_only: bool = False,
    exclusive: bool = False,
) -> sdk.ToolContribution:
    return sdk.ToolContribution(
        id=tool_id,
        exposed_name=f"ui_workbench__{tool_id}",
        description=description,
        handler=handler,
        parameters_schema=parameters,
        permission_mode="auto" if read_only else "approval",
        effect="read_only" if read_only else "state_changing",
        batch_behavior="exclusive" if exclusive else "compatible",
    )


def _failure(exc: WorkbenchError) -> sdk.ToolOutput:
    return sdk.ToolOutput(content=str(exc), structured={"error": str(exc)}, is_error=True)


def create_plugin(context: sdk.PluginContext) -> WorkbenchPlugin:
    return WorkbenchPlugin(context)
