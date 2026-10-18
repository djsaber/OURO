from __future__ import annotations

import re

from ouro.sdk.plugins import v5 as sdk


def record_group(fields: tuple[str, ...] = ("title",)) -> sdk.ToolGroupContribution:
    def record(call: sdk.ToolCall) -> sdk.ToolOutput:
        if call.session_state is None:
            raise ValueError("recording requires a Session")
        previous = call.session_state.read()
        records = previous.payload["records"] if previous is not None else []
        value = {name: call.arguments[name] for name in fields}
        call.session_state.replace(
            state_schema_version=1, payload={"records": [*records, value]}, branch_policy="copy"
        )
        return sdk.ToolOutput(content="Record saved.", structured={"record": value})

    tools = (
        (
            sdk.ToolContribution(
                "record",
                "RecordData",
                "Save a structured record in the current Session.",
                record,
                parameters_schema={
                    "type": "object",
                    "properties": {name: {"type": "string"} for name in fields},
                    "required": list(fields),
                    "additionalProperties": False,
                },
                permission_mode="auto",
                effect="state_changing",
            ),
        )
        if fields
        else ()
    )
    return sdk.ToolGroupContribution("records", "record-v1:" + ",".join(fields), tools)


async def set_fields(groups: sdk.PluginToolGroups, argument: str) -> sdk.CommandOutput:
    fields = (
        () if argument.strip() == "off" else tuple(item.strip() for item in argument.split(","))
    )
    if len(set(fields)) != len(fields) or any(
        re.fullmatch(r"[a-z][a-z0-9_]*", name) is None for name in fields
    ):
        raise ValueError("fields must be unique lowercase identifiers separated by commas")
    await groups.publish(record_group(fields))
    return sdk.CommandOutput(
        output="Record fields: " + ", ".join(fields) if fields else "Record tool withdrawn."
    )


class RecordPlugin:
    def __init__(self, context: sdk.PluginContext) -> None:
        assert context.tool_groups is not None
        self.groups = context.tool_groups

    async def prepare(self) -> sdk.ContributionSet:
        async def command(call: sdk.CommandCall) -> sdk.CommandOutput:
            return await set_fields(self.groups, call.argument)

        return sdk.ContributionSet(
            tool_groups=[record_group()],
            commands=[
                sdk.CommandContribution(
                    "record-fields",
                    "Set record fields or withdraw the tool.",
                    command,
                    usage="<field,...|off>",
                )
            ],
        )

    async def start(self) -> None:
        pass

    async def stop(self, reason: sdk.StopReason, deadline: float) -> None:
        pass


def create_plugin(context: sdk.PluginContext) -> RecordPlugin:
    return RecordPlugin(context)
