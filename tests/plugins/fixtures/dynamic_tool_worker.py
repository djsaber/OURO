from __future__ import annotations

import asyncio
from dataclasses import replace

from dynamic_records_plugin import record_group
from worker import Worker

from ouro.sdk.plugins import v4 as sdk


class Probe(Worker):
    def __init__(self):
        super().__init__()
        self.handles = []
        self.peer.handlers.update(
            {"test.inspect": self.inspect, "test.publish": self.publish, "test.close": self.close}
        )

    async def inspect(self, params):
        return list(self.groups._handlers)

    @staticmethod
    def group(fields):
        group = record_group(fields)

        def resume(call):
            return f"{','.join(fields)}:{call.resolution.kind}"

        return replace(group, tools=(replace(group.tools[0], resume_handler=resume),))

    async def prepare(self, params):
        return {
            "commands": ["record-fields"],
            "tool_groups": self.groups.prepare([self.group(("title",))]),
        }

    async def publish(self, params):
        group = self.group(tuple(params.get("fields", ["title"])))
        if params.get("invalid"):
            group = replace(group, tools=(replace(group.tools[0], effect="read_only"),))
        if params.get("delay"):

            async def delayed(call):
                await asyncio.sleep(0.1)
                return "delayed result"

            group = replace(group, tools=(replace(group.tools[0], handler=delayed),))
        if params.get("wait"):
            group = replace(
                group,
                tools=(
                    replace(
                        group.tools[0],
                        handler=lambda _: sdk.ToolInputRequest({"prompt": "Continue?"}),
                    ),
                ),
            )
        self.handles.append(await self.groups.publish(group))
        return len(self.handles) - 1

    async def close(self, params):
        await self.handles[params["index"]].close()
        return None


asyncio.run(Probe().peer.serve())
