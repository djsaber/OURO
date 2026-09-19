"""The driver owns this loopback receiver until authorization cleanup finishes."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

from ouro.auth.contracts import AuthError
from ouro.auth.drivers.openai_errors import OpenAIOAuthError


class CallbackReceiver:
    """Own the loopback socket and its bounded, cancellable HTTP connections."""

    def __init__(self, state: str) -> None:
        self._state = state
        self.code: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()

    async def start(self, port: int) -> None:
        try:
            self._server = await asyncio.start_server(self._accept, "127.0.0.1", port, limit=8192)
        except OSError:
            raise OpenAIOAuthError("callback_port_unavailable") from None

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._connections.add(task)
        task.add_done_callback(self._connections.discard)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(5):
                raw = await reader.readuntil(b"\r\n\r\n")
                method, target, version = raw.split(b"\r\n", 1)[0].decode("ascii").split(" ")
                url = urlsplit(target)
                params = parse_qs(url.query, keep_blank_values=True)
                status = 400
                if (
                    method == "GET"
                    and version in {"HTTP/1.0", "HTTP/1.1"}
                    and url.path == "/auth/callback"
                    and not url.scheme
                    and not url.netloc
                    and not url.fragment
                    and params.get("state") == [self._state]
                ):
                    if self.code.done():
                        status = 409
                    elif "error" in params:
                        self.code.set_exception(AuthError("authorization_denied"))
                        status = 200
                    elif len(params.get("code", [])) == 1 and params["code"][0]:
                        self.code.set_result(params["code"][0])
                        status = 200
                body = b"Authorization callback received. Return to OURO to finish signing in."
                writer.write(
                    f"HTTP/1.1 {status} Result\r\nContent-Type: text/plain\r\n"
                    f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                    "Connection: close\r\n\r\n".encode("ascii")
                    + body
                )
                await writer.drain()
        except (
            OSError,
            ValueError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass  # Malformed/disconnected local clients cannot finish the login.
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass  # A peer reset is already closed; it cannot affect authorization.

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        tasks = tuple(self._connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not self.code.done():
            self.code.cancel()
        elif not self.code.cancelled():
            self.code.exception()
