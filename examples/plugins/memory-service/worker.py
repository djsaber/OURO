from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from ouro.sdk.plugins.worker import WorkerPeer


class Memory:
    def __init__(self):
        self.database = None
        self.filters = {}
        self.peer = WorkerPeer(
            {
                "initialize": self.initialize,
                "contributions.prepare": self.prepare,
                "lifecycle.start": self.lifecycle,
                "lifecycle.stop": self.lifecycle,
                "services.call": self.call,
                "services.register": self.register,
                "registrations.close": self.unregister,
            },
            max_requests=1,
        )

    async def initialize(self, params):
        self.database = Path(params["data_directory"]) / "memory.sqlite3"
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        return {"services": ["memory"]}

    async def lifecycle(self, params):
        return {}

    def append(self, text):
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS entries (text TEXT NOT NULL)")
            connection.execute("INSERT INTO entries (text) VALUES (?)", (text,))

    def search(self, query):
        if not self.database.exists():
            return []
        with sqlite3.connect(f"{self.database.as_uri()}?mode=ro", uri=True) as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT text FROM entries WHERE instr(text, ?) > 0 "
                    "ORDER BY rowid DESC LIMIT 20",
                    (query,),
                )
            ]

    async def call(self, params):
        if params["method"] == "append":
            await self.access_data(self.append, params["arguments"])
            return None
        result = await self.access_data(self.search, params["arguments"])
        for handler_id in tuple(self.filters.values()):
            result = await self.peer.request(
                "handlers.invoke", {"handler_id": handler_id, "arguments": result}
            )
        return result

    async def access_data(self, operation, argument):
        task = asyncio.create_task(asyncio.to_thread(operation, argument))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def register(self, params):
        self.filters[params["registration_id"]] = params["handler_id"]
        return None

    async def unregister(self, params):
        self.filters.pop(params["registration_id"], None)
        return None


asyncio.run(Memory().peer.serve())
