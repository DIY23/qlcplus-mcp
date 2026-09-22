"""Async websocket client for the QLC+ Agent API.

The socket carries several message families (see :mod:`qlcplus_mcp.framing`).
This client guarantees the properties the ``QLC+AGENT`` family promises:

* one request -> exactly one reply, correlated by ``reqId``;
* replies may arrive **out of order** (several requests may be in flight at
  once) and are still routed to the right caller;
* non-``QLC+AGENT`` traffic is *skipped*, never mistaken for a reply, and is
  available afterwards for inspection;
* a request that is never answered raises :class:`~qlcplus_mcp.framing.QlcTimeoutError`
  after the configured timeout instead of hanging forever;
* an ``err`` reply raises :class:`~qlcplus_mcp.framing.QlcAgentError` whose
  message contains the server's own explanation.

Configuration comes from the environment, with arguments overriding it::

    QLC_ENABLED   optional "0"/"false" to disable the whole integration
    QLC_HOST      default 127.0.0.1
    QLC_PORT      default 9999
    QLC_WS_PATH   default /qlcplusWS
    QLC_TIMEOUT   default 10 (seconds, float accepted)
    QLC_WS_URL    full override, e.g. ws://127.0.0.1:9999/qlcplusWS
"""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from collections import deque
from typing import Any, Awaitable, Callable, Iterable

import websockets

from .framing import (
    EMPTY_ARGS_B64,
    AgentEvent,
    AgentMessage,
    OtherMessage,
    QlcAgentError,
    QlcConnectionError,
    QlcProtocolError,
    QlcTimeoutError,
    build_request_frame,
    parse_agent_message,
    split_frames,
)

__all__ = ["QlcAgentClient", "client_from_env"]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9999
DEFAULT_WS_PATH = "/qlcplusWS"
DEFAULT_TIMEOUT = 10.0

EventCallback = Callable[[AgentEvent], Any]
OtherCallback = Callable[[OtherMessage], Any]


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise QlcProtocolError(
            f"environment variable {name}={raw!r} is not a number"
        ) from exc


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "disabled")


def build_uri(host: str, port: int, path: str) -> str:
    """Assemble ``ws://host:port/path`` from its parts."""
    if path and not path.startswith("/"):
        path = "/" + path
    return f"ws://{host}:{port}{path}"


class QlcAgentClient:
    """Talk to a running QLC+ over its Web Access websocket.

    Usage::

        async with QlcAgentClient() as qlc:
            print(await qlc.request("getState"))

    or, without the context manager, lazily::

        qlc = QlcAgentClient()
        await qlc.request("getState")   # connects on first use
        await qlc.close()
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        path: str | None = None,
        timeout: float | None = None,
        uri: str | None = None,
        connect_timeout: float = 5.0,
        *,
        on_event: EventCallback | None = None,
        on_other: OtherCallback | None = None,
        event_buffer: int = 1000,
        other_buffer: int = 2000,
        keepalive: bool = True,
    ) -> None:
        env_uri = os.environ.get("QLC_WS_URL")
        self.uri = (
            uri
            or (env_uri if not host and not port and not path else None)
            or build_uri(
                host or _env("QLC_HOST", DEFAULT_HOST),
                int(port if port is not None else _env("QLC_PORT", str(DEFAULT_PORT))),
                path or _env("QLC_WS_PATH", DEFAULT_WS_PATH),
            )
        )
        self.timeout = (
            float(timeout) if timeout is not None else _env_float("QLC_TIMEOUT", DEFAULT_TIMEOUT)
        )
        self.connect_timeout = connect_timeout
        self.enabled = _env_flag("QLC_ENABLED", True)
        self.keepalive = keepalive

        self._ws: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._next_req_id = 0
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._closed = False

        self.on_event = on_event
        self.on_other = on_other

        #: Events nobody was waiting for yet (bounded; oldest dropped first).
        self.events: deque[AgentEvent] = deque(maxlen=event_buffer)
        #: Frames from other families, newest last (bounded).
        self.other_messages: deque[OtherMessage] = deque(maxlen=other_buffer)
        #: ``QLC+AGENT`` frames that claimed the family but matched no legal shape.
        self.protocol_errors: deque[QlcProtocolError] = deque(maxlen=100)
        #: Replies that arrived after their request was already resolved/failed.
        self.stray_replies: deque[AgentMessage] = deque(maxlen=100)

        self._event_waiters: list[asyncio.Future[AgentEvent]] = []
        self._last_error: str | None = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return (
            self._ws is not None
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    @property
    def pending_count(self) -> int:
        """How many requests are in flight. Handy in tests and health checks."""
        return len(self._pending)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def __aenter__(self) -> "QlcAgentClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def connect(self) -> "QlcAgentClient":
        """Open the socket and start the reader loop (idempotent)."""
        if not self.enabled:
            raise QlcConnectionError(
                "the QLC+ integration is disabled (QLC_ENABLED=0); "
                "unset it to talk to QLC+"
            )
        if self.connected:
            return self
        async with self._connect_lock:
            if self.connected:
                return self
            try:
                self._ws = await websockets.connect(
                    self.uri,
                    open_timeout=self.connect_timeout,
                    ping_interval=20 if self.keepalive else None,
                    ping_timeout=20 if self.keepalive else None,
                    max_size=None,
                    # A local lighting app is never reached through a proxy.
                    proxy=None,
                )
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
                self._last_error = f"connect failed: {exc}"
                raise QlcConnectionError(
                    f"could not connect to QLC+ at {self.uri}: {exc}. "
                    "Launch QLC+ with web access (qlcplus-qml -w) and check "
                    "QLC_HOST/QLC_PORT/QLC_WS_PATH."
                ) from exc
            self._closed = False
            self._reader_task = asyncio.create_task(
                self._reader(), name="qlcplus-agent-reader"
            )
        return self

    async def close(self) -> None:
        """Cancel the reader and close the socket. Safe to call twice."""
        self._closed = True
        task, self._reader_task = self._reader_task, None
        ws, self._ws = self._ws, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown
                pass
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 - teardown
                pass
        self._fail_pending(
            QlcConnectionError("the connection to QLC+ was closed")
        )

    # ------------------------------------------------------------------ #
    # sending / receiving
    # ------------------------------------------------------------------ #
    def _new_req_id(self) -> str:
        self._next_req_id += 1
        return str(self._next_req_id)

    async def request(
        self,
        verb: str,
        args: Any = None,
        timeout: float | None = None,
    ) -> Any:
        """Send one request and wait for its reply.

        Raises :class:`QlcTimeoutError` if no reply arrives in time,
        :class:`QlcAgentError` if QLC+ answered ``err``, and
        :class:`QlcConnectionError` if the socket is gone.
        """
        await self.connect()
        effective = self.timeout if timeout is None else float(timeout)
        req_id = self._new_req_id()
        frame = build_request_frame(req_id, verb, args)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = future
        try:
            async with self._send_lock:
                ws = self._ws
                if ws is None:
                    raise QlcConnectionError("not connected to QLC+")
                try:
                    await ws.send(frame)
                except Exception as exc:  # noqa: BLE001 - transport failure surface
                    self._last_error = f"send failed: {exc}"
                    raise QlcConnectionError(
                        f"could not send {verb!r} to QLC+ at {self.uri}: {exc}"
                    ) from exc
            return await asyncio.wait_for(future, effective)
        except asyncio.TimeoutError:
            self._last_error = f"timeout waiting for {verb!r} after {effective:g}s"
            raise QlcTimeoutError(
                f"QLC+ did not reply to {verb!r} (reqId {req_id}) within "
                f"{effective:g}s. The request was abandoned; raise QLC_TIMEOUT "
                "if the operation is legitimately slow."
            ) from None
        finally:
            if self._pending.get(req_id) is future:
                del self._pending[req_id]
            if not future.done():
                future.cancel()

    #: alias -- some callers like saying "call"
    call = request

    async def request_many(
        self, calls: Iterable[tuple[str, Any]], timeout: float | None = None
    ) -> list[Any]:
        """Run several requests concurrently, preserving the input order."""
        return await asyncio.gather(
            *(self.request(verb, args, timeout=timeout) for verb, args in calls)
        )

    async def call_verb(self, verb: str, **kwargs: Any) -> Any:
        """Convenience wrapper: drop ``None`` values so the wire stays clean."""
        return await self.request(verb, {k: v for k, v in kwargs.items() if v is not None})

    async def next_event(self, timeout: float | None = None) -> AgentEvent:
        """Wait for the next unsolicited event (raises on timeout)."""
        if self.events:
            return self.events.popleft()
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[AgentEvent] = loop.create_future()
        self._event_waiters.append(waiter)
        try:
            return await asyncio.wait_for(waiter, timeout)
        except asyncio.TimeoutError:
            raise QlcTimeoutError(
                f"no QLC+ agent event arrived within {timeout:.3g}s"
                if timeout
                else "timed out waiting for a QLC+ agent event"
            ) from None
        finally:
            if waiter in self._event_waiters:
                self._event_waiters.remove(waiter)
            if not waiter.done():
                waiter.cancel()

    # ------------------------------------------------------------------ #
    # internal
    # ------------------------------------------------------------------ #
    async def _reader(self) -> None:
        """Pump the socket until it closes; route every frame by family."""
        ws = self._ws
        assert ws is not None
        try:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", "replace")
                for frame in split_frames(raw):
                    self._route(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - socket died under us
            self._last_error = f"reader stopped: {exc}"
            self._fail_pending(
                QlcConnectionError(
                    f"connection to QLC+ at {self.uri} was lost: {exc}"
                )
            )
            return
        if not self._closed:
            self._last_error = "socket closed by QLC+"
            self._fail_pending(
                QlcConnectionError(f"QLC+ closed the connection to {self.uri}")
            )

    def _route(self, frame: str) -> None:
        try:
            message = parse_agent_message(frame)
        except QlcProtocolError as exc:
            # A malformed AGENT frame must not kill the stream: record and skip.
            self.protocol_errors.append(exc)
            return
        if message is None:
            self._record_other(frame)
            return
        if isinstance(message, AgentEvent):
            self._dispatch_event(message)
            return
        future = self._pending.pop(message.req_id, None)
        if future is None or future.done():
            # A late reply (e.g. after our timeout) or a duplicate. Recorded,
            # never allowed to resolve somebody else's request.
            self.stray_replies.append(message)
            return
        if message.kind == "error":
            future.set_exception(message.to_error())
        else:
            try:
                future.set_result(message.payload())
            except QlcProtocolError as exc:
                future.set_exception(exc)

    def _record_other(self, frame: str) -> None:
        from .framing import message_family

        message = OtherMessage(family=message_family(frame), raw=frame)
        self.other_messages.append(message)
        if self.on_other is not None:
            try:
                self.on_other(message)
            except Exception as exc:  # noqa: BLE001 - a bad callback must not desync us
                self._last_error = f"on_other callback failed: {exc}"

    def _dispatch_event(self, message: AgentEvent) -> None:
        event = AgentEvent(
            name=message.name,
            payload_b64=message.payload_b64,
            raw=message.raw,
            received_at=time.time(),
        )
        delivered = False
        while self._event_waiters:
            waiter = self._event_waiters.pop(0)
            if not waiter.done():
                waiter.set_result(event)
                delivered = True
                break
        if not delivered:
            # Nobody was waiting; keep it for the next next_event() call.
            self.events.append(event)
        if self.on_event is not None:
            try:
                outcome = self.on_event(event)
                if inspect.isawaitable(outcome):
                    asyncio.ensure_future(outcome)
            except Exception as exc:  # noqa: BLE001 - as above
                self._last_error = f"on_event callback failed: {exc}"

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)
        for waiter in self._event_waiters:
            if not waiter.done():
                waiter.cancel()
        self._event_waiters.clear()

    # ------------------------------------------------------------------ #
    # convenience: fire-and-forget-ish sync use (CLI, scripts)
    # ------------------------------------------------------------------ #
    def request_sync(
        self, verb: str, args: Any = None, timeout: float | None = None
    ) -> Any:
        """Blocking one-shot request, for scripts that are not async."""

        async def _run() -> Any:
            await self.connect()
            try:
                return await self.request(verb, args, timeout=timeout)
            finally:
                await self.close()

        return asyncio.run(_run())


def client_from_env(**overrides: Any) -> QlcAgentClient:
    """Build a client from ``QLC_*`` environment variables."""
    return QlcAgentClient(**overrides)


def empty_args_frame(req_id: str | int, verb: str) -> str:
    """Return the exact frame used when a request carries no arguments."""
    return build_request_frame(req_id, verb, {})
