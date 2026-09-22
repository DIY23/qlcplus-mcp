"""A fake QLC+ Web Access server, for tests.

Stands in for QLC+ so the suite never needs the real application (it may not even
be installed on the machine running the tests). It:

* listens on a random free port (never collides with a real QLC+ on 9999);
* validates every request frame it receives against the frozen spec via
  :func:`tests.fake_wire.parse_request` -- so a framing bug fails a test rather
  than passing silently;
* lets each test decide how to answer, so "reply out of order", "answer with an
  error", "never answer" and "interleave other message families" are all just
  handler choices.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import websockets
from websockets.asyncio.server import Server, ServerConnection

import fake_wire
from fake_wire import BadFrame, parse_request

Handler = Callable[["FakeQlcServer", ServerConnection, "ReceivedRequest"], Awaitable[None]]


@dataclass
class ReceivedRequest:
    """One request as it arrived, already parsed and validated."""

    req_id: str
    verb: str
    args: Any
    payload_b64: str
    raw: str
    received_at: float = field(default_factory=time.time)

    @property
    def field_count(self) -> int:
        return len(self.raw.split(fake_wire.SEP))


class FakeQlcServer:
    """Minimal stand-in for ``QLC+``'s websocket, with a pluggable handler."""

    def __init__(
        self,
        handler: Handler | None = None,
        *,
        host: str = "127.0.0.1",
        path: str = "/qlcplusWS",
        greeting: list[str] | None = None,
    ) -> None:
        self.handler: Handler = handler or self.default_handler
        self.host = host
        self.path = path
        #: Frames sent to the client as soon as it connects (before any request).
        self.greeting = greeting if greeting is not None else list(fake_wire.NOISE_FRAMES)

        self.requests: list[ReceivedRequest] = []
        self.sent: list[str] = []
        self.bad_frames: list[str] = []
        self.connections = 0
        self.current: ServerConnection | None = None

        self._server: Server | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.port: int | None = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> "FakeQlcServer":
        self._server = await websockets.serve(
            self._connection, self.host, 0, max_size=None, ping_interval=None
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> "FakeQlcServer":
        return await self.start()

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.stop()

    @property
    def uri(self) -> str:
        """Full websocket URL of the fake, path included."""
        return f"ws://{self.host}:{self.port}{self.path}"

    # ------------------------------------------------------------------ #
    # sending
    # ------------------------------------------------------------------ #
    async def send(self, frame: str) -> None:
        """Send one frame to the connected client and record it."""
        if self.current is None:
            raise RuntimeError("no client connected to the fake QLC+ server")
        self.sent.append(frame)
        await self.current.send(frame)

    async def send_noise(self, frames: list[str] | None = None) -> None:
        """Send frames from the *other* message families on the same socket."""
        for frame in frames if frames is not None else fake_wire.NOISE_FRAMES:
            await self.send(frame)

    def spawn(self, coro: Awaitable[None]) -> asyncio.Task[Any]:
        """Run a reply (or several) without blocking the connection handler."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ #
    # handlers
    # ------------------------------------------------------------------ #
    @staticmethod
    async def default_handler(
        self, ws: ServerConnection, request: ReceivedRequest
    ) -> None:
        """Echo the verb and args back inside a normal ``ok`` reply."""
        await self.send(
            fake_wire.ok_frame(
                request.req_id,
                request.verb,
                {"verb": request.verb, "echo": request.args, "reqId": request.req_id},
            )
        )

    @staticmethod
    async def no_reply_handler(
        self, ws: ServerConnection, request: ReceivedRequest
    ) -> None:
        """Accept the request and stay silent -- exercises the client timeout."""

    @staticmethod
    async def error_handler(
        self, ws: ServerConnection, request: ReceivedRequest
    ) -> None:
        await self.send(
            fake_wire.err_frame(request.req_id, request.verb, "universe 4 is not patched")
        )

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    async def _connection(self, ws: ServerConnection) -> None:
        self.connections += 1
        self.current = ws
        try:
            for frame in self.greeting:
                self.sent.append(frame)
                await ws.send(frame)
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", "replace")
                try:
                    parsed = parse_request(raw)
                except BadFrame as exc:
                    # Keep the connection alive so the client sees a timeout
                    # rather than a hang; the assertion lives in bad_frames.
                    self.bad_frames.append(f"{exc}")
                    continue
                request = ReceivedRequest(**parsed)
                self.requests.append(request)
                await self.handler(self, ws, request)
        except websockets.ConnectionClosed:
            pass
        finally:
            if self.current is ws:
                self.current = None

    # ------------------------------------------------------------------ #
    # test conveniences
    # ------------------------------------------------------------------ #
    @property
    def verbs_seen(self) -> list[str]:
        return [request.verb for request in self.requests]

    @property
    def req_ids_seen(self) -> list[str]:
        return [request.req_id for request in self.requests]

    async def wait_for_requests(self, count: int, timeout: float = 5.0) -> None:
        """Block until ``count`` requests have arrived (or fail the test)."""
        deadline = time.monotonic() + timeout
        while len(self.requests) < count:
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"expected {count} requests, saw {len(self.requests)}: {self.requests}"
                )
            await asyncio.sleep(0.01)
