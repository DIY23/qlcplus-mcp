"""Wire framing for the QLC+ Agent API (the ``QLC+AGENT`` message family).

QLC+'s Web Access server (``ws://127.0.0.1:9999/qlcplusWS``, enabled by launching
QLC+ with ``-w``) speaks a *text*, pipe-delimited protocol. Several message
families share that one socket::

    QLC+CMD|...
    QLC+API|<verb>|<args...>
    QLC+IO|...
    VC_PAGE|<page>
    GM_VALUE|...

This module implements the client side of the newer ``QLC+AGENT`` family::

    request   QLC+AGENT|<reqId>|<verb>|<base64(json-args)>
    reply     QLC+AGENT|<verb>|<reqId>|ok|<base64(json-result)>
    error     QLC+AGENT|<verb>|<reqId>|err|<base64(json {"error": "..."})>
    event     QLC+AGENT|<event>|event|<base64(json-payload)>

Design rules baked in here:

* **One request -> exactly one reply**, correlated by ``reqId`` (the server
  echoes it back). ``reqId`` is a plain incrementing integer rendered as a
  string, which is the least surprising thing to hand a C++ side that parses it.
* **Payloads are base64 of UTF-8 JSON.** Base64's alphabet contains neither
  ``|`` nor a newline, so a fixture name containing ``|`` or a multi-line
  caption can never split a frame. We deliberately do *not* rely on JSON string
  escaping to keep the framing intact.
* **Empty args are sent as ``base64("{}")`` == ``e30=``.**
* Frames from other families (``QLC+API|...``, ``GM_VALUE|...``, ...) are
  recognised and returned as ``None`` by :func:`parse_agent_message` so the
  reader can skip them without losing its place in the stream.

Everything in this module is synchronous, dependency-free and side-effect free,
which is what makes the framing itself unit-testable without a socket.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "AGENT_PREFIX",
    "FIELD_SEP",
    "EMPTY_ARGS_B64",
    "KNOWN_FAMILIES",
    "OK_FIELD",
    "ERR_FIELD",
    "EVENT_FIELD",
    "QlcError",
    "QlcConnectionError",
    "QlcTimeoutError",
    "QlcProtocolError",
    "QlcAgentError",
    "AgentMessage",
    "AgentEvent",
    "OtherMessage",
    "message_family",
    "is_agent_frame",
    "encode_payload",
    "decode_payload",
    "build_request_frame",
    "build_reply_frame",
    "build_error_frame",
    "build_event_frame",
    "parse_agent_message",
    "split_frames",
]

#: Family tag that introduces every message this module deals with.
AGENT_PREFIX = "QLC+AGENT"

#: The wire delimiter. Kept as a named constant so it is greppable.
FIELD_SEP = "|"

#: ``base64("{}")`` -- what an empty argument list looks like on the wire.
EMPTY_ARGS_B64 = "e30="

#: Status token used by a successful reply.
OK_FIELD = "ok"

#: Status token used by an error reply.
ERR_FIELD = "err"

#: Second token of an event frame (an event, not a reply to a request).
EVENT_FIELD = "event"

#: Families we know can appear on the same socket. Anything else is still
#: reported (see :func:`message_family`) and skipped -- never fatal.
KNOWN_FAMILIES = (
    "QLC+AGENT",
    "QLC+CMD",
    "QLC+API",
    "QLC+IO",
    "VC_PAGE",
    "GM_VALUE",
)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class QlcError(Exception):
    """Base class for every error raised by this package."""


class QlcConnectionError(QlcError):
    """The websocket to QLC+ is not up, or went away mid-request.

    Fix: launch QLC+ with web access enabled (``qlcplus-qml -w``) and check the
    host/port/path (``QLC_HOST``, ``QLC_PORT``, ``QLC_WS_PATH``).
    """


class QlcTimeoutError(QlcError):
    """QLC+ did not answer a request within the per-request timeout.

    This is raised instead of hanging forever. Raise ``QLC_TIMEOUT`` (seconds)
    if a verb is legitimately slow; ``saveProject`` on a large project is the
    usual candidate.
    """


class QlcProtocolError(QlcError):
    """A frame could not be understood: bad base64, or JSON that is not valid."""


class QlcAgentError(QlcError):
    """QLC+ answered a request with an explicit ``err`` reply.

    Attributes carry the structured pieces so callers can branch on them.
    """

    def __init__(
        self,
        verb: str,
        req_id: str | None,
        error: str,
        raw: str | None = None,
        detail: Any = None,
    ) -> None:
        self.verb = verb
        self.req_id = req_id
        self.error = error
        self.raw = raw
        self.detail = detail if detail is not None else {"error": error}
        super().__init__(
            f"QLC+ rejected request for {verb!r} (reqId {req_id}): {error}"
        )


# --------------------------------------------------------------------------- #
# Payload encoding
# --------------------------------------------------------------------------- #
def encode_payload(obj: Any) -> str:
    """Serialise ``obj`` to base64-of-UTF-8-JSON for the wire.

    Compact separators keep frames short; ``ensure_ascii=False`` means non-ASCII
    text really travels as UTF-8 bytes (which the base64 layer then protects
    from the delimiter).
    """
    try:
        blob = json.dumps(
            obj,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise QlcProtocolError(
            f"arguments could not be serialised to JSON: {exc}"
        ) from exc
    return base64.b64encode(blob.encode("utf-8")).decode("ascii")


def _b64_to_bytes(b64: str) -> bytes:
    """Decode base64 tolerantly (whitespace, missing padding, urlsafe alphabet)."""
    text = "".join(b64.split())
    if not text:
        return b""
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except binascii.Error:
        try:
            return base64.urlsafe_b64decode(padded)
        except (binascii.Error, ValueError) as exc:
            raise QlcProtocolError(
                f"payload is not valid base64: {b64[:80]!r}"
            ) from exc


def decode_payload(b64: str) -> Any:
    """Inverse of :func:`encode_payload`.

    An empty payload is treated as ``{}`` (a server with nothing to say) rather
    than an error, so a bare ``QLC+AGENT|<verb>|<reqId>|ok|`` still resolves the
    request.
    """
    if not b64 or not b64.strip():
        return {}
    raw = _b64_to_bytes(b64)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QlcProtocolError(
            f"payload is not valid UTF-8 (after base64 decode): {exc}"
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise QlcProtocolError(
            f"payload is base64 but not valid JSON: {text[:200]!r} ({exc})"
        ) from exc


# --------------------------------------------------------------------------- #
# Frame building
# --------------------------------------------------------------------------- #
def build_request_frame(req_id: str | int, verb: str, args: Any = None) -> str:
    """``QLC+AGENT|<reqId>|<verb>|<base64(json-args)>``."""
    if not verb or FIELD_SEP in verb:
        raise QlcProtocolError(f"invalid verb for the wire: {verb!r}")
    payload = encode_payload({} if args is None else args)
    return FIELD_SEP.join((AGENT_PREFIX, str(req_id), verb, payload))


def build_reply_frame(req_id: str | int, verb: str, result: Any = None) -> str:
    """Successful reply. Present so the test harness can speak the protocol."""
    return FIELD_SEP.join(
        (AGENT_PREFIX, verb, str(req_id), OK_FIELD, encode_payload({} if result is None else result))
    )


def build_error_frame(req_id: str | int, verb: str, message: str) -> str:
    """``err`` reply. The JSON object *must* carry an ``error`` key."""
    return FIELD_SEP.join(
        (
            AGENT_PREFIX,
            verb,
            str(req_id),
            ERR_FIELD,
            encode_payload({"error": str(message)}),
        )
    )


def build_event_frame(name: str, payload: Any = None) -> str:
    """``QLC+AGENT|<event>|event|<base64(json-payload)>``."""
    return FIELD_SEP.join(
        (
            AGENT_PREFIX,
            name,
            EVENT_FIELD,
            encode_payload({} if payload is None else payload),
        )
    )


# --------------------------------------------------------------------------- #
# Frame parsing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AgentMessage:
    """A ``QLC+AGENT`` reply or error, already split into its fields."""

    kind: Literal["reply", "error"]
    verb: str
    req_id: str
    payload_b64: str
    raw: str

    @property
    def status(self) -> str:
        return OK_FIELD if self.kind == "reply" else ERR_FIELD

    def payload(self) -> Any:
        """The decoded JSON result (or ``{"error": ...}`` for an error reply)."""
        return decode_payload(self.payload_b64)

    def to_error(self) -> QlcAgentError:
        """Turn this ``err`` reply into the exception the client raises."""
        decoded = self.payload()
        message = "unspecified error"
        if isinstance(decoded, dict):
            message = str(decoded.get("error", message))
        elif decoded:
            message = str(decoded)
        return QlcAgentError(
            verb=self.verb,
            req_id=self.req_id,
            error=message,
            raw=self.raw,
            detail=decoded,
        )


@dataclass(frozen=True)
class AgentEvent:
    """An unsolicited ``QLC+AGENT`` event (not a reply to anything)."""

    name: str
    payload_b64: str
    raw: str
    received_at: float | None = None

    def payload(self) -> Any:
        return decode_payload(self.payload_b64)


@dataclass(frozen=True)
class OtherMessage:
    """A frame from a different family that travelled over the same socket."""

    family: str
    raw: str


def message_family(frame: str) -> str:
    """Return the leading family token of ``frame`` (``unknown`` if unfamiliar)."""
    head = frame.split(FIELD_SEP, 1)[0]
    return head if head in KNOWN_FAMILIES else "unknown"


def is_agent_frame(frame: str) -> bool:
    """True when ``frame`` belongs to the ``QLC+AGENT`` family."""
    return frame == AGENT_PREFIX or frame.startswith(AGENT_PREFIX + FIELD_SEP)


def parse_agent_message(frame: str) -> AgentMessage | AgentEvent | None:
    """Parse one frame.

    Returns ``None`` for anything that is not a ``QLC+AGENT`` message (the
    caller records and skips it). Raises :class:`QlcProtocolError` only for a
    frame that *claims* to be ``QLC+AGENT`` but matches no legal shape -- the
    reader catches that too, so malformed input never desyncs the client.
    """
    if not is_agent_frame(frame):
        return None

    parts = frame.split(FIELD_SEP)
    if len(parts) < 4:
        raise QlcProtocolError(f"truncated QLC+AGENT frame: {frame!r}")

    name, second = parts[1], parts[2]

    # event: QLC+AGENT|<event>|event|<payload>
    if second == EVENT_FIELD:
        return AgentEvent(name=name, payload_b64=FIELD_SEP.join(parts[3:]), raw=frame)

    # reply/error: QLC+AGENT|<verb>|<reqId>|<status>|<payload>
    if len(parts) < 5:
        raise QlcProtocolError(f"truncated QLC+AGENT reply frame: {frame!r}")
    status = parts[3]
    if status not in (OK_FIELD, ERR_FIELD):
        raise QlcProtocolError(
            f"QLC+AGENT reply has an unknown status {status!r}: {frame!r}"
        )
    return AgentMessage(
        kind="reply" if status == OK_FIELD else "error",
        verb=name,
        req_id=second,
        payload_b64=FIELD_SEP.join(parts[4:]),
        raw=frame,
    )


def split_frames(raw: str) -> list[str]:
    """Split one incoming websocket text message into individual frames.

    Normally one websocket message is one frame, and because payloads are
    base64 a well-formed frame never contains a newline. Splitting on newlines
    therefore only ever helps: it lets us cope with a sender that batches
    several frames into one message, and drops blank keep-alive lines.
    """
    if "\n" not in raw:
        return [raw] if raw else []
    return [line for line in (ln.strip() for ln in raw.splitlines()) if line]
