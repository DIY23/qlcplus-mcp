"""Independent re-implementation of the QLC+AGENT wire format, for tests.

Deliberately *not* imported from ``qlcplus_mcp``: if the package's framing were
wrong, tests that reused it would agree with the bug. Everything here is built
from ``base64`` and ``json`` directly, the way the C++ side is expected to.

Frozen spec this file encodes:

    request   QLC+AGENT|<reqId>|<verb>|<base64(json-args)>
    reply     QLC+AGENT|<verb>|<reqId>|ok|<base64(json-result)>
    error     QLC+AGENT|<verb>|<reqId>|err|<base64(json {"error": "..."})>
    event     QLC+AGENT|<event>|event|<base64(json-payload)>
"""

from __future__ import annotations

import base64
import json
from typing import Any

PREFIX = "QLC+AGENT"
SEP = "|"


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
def encode_payload(obj: Any) -> str:
    """base64(UTF-8(JSON(obj))), exactly what the spec asks for."""
    return base64.b64encode(json.dumps(obj, ensure_ascii=False).encode("utf-8")).decode("ascii")


def decode_payload(b64: str) -> Any:
    """base64 -> UTF-8 -> JSON. Strict: any failure is a framing bug."""
    blob = base64.b64decode(b64, validate=True)
    return json.loads(blob.decode("utf-8"))


# --------------------------------------------------------------------------- #
# building frames (what a server sends us)
# --------------------------------------------------------------------------- #
def ok_frame(req_id: str | int, verb: str, result: Any = None) -> str:
    return SEP.join((PREFIX, verb, str(req_id), "ok", encode_payload({} if result is None else result)))


def err_frame(req_id: str | int, verb: str, message: str) -> str:
    return SEP.join(
        (PREFIX, verb, str(req_id), "err", encode_payload({"error": message}))
    )


def event_frame(name: str, payload: Any = None) -> str:
    return SEP.join(
        (PREFIX, name, "event", encode_payload({} if payload is None else payload))
    )


# non-QLC+AGENT families that share the same socket
def api_frame(verb: str, *args: Any) -> str:
    return SEP.join((PREFIX.replace("AGENT", "API"), verb, *(str(a) for a in args), ""))


def gm_value_frame(value: int) -> str:
    return f"GM_VALUE{SEP}{value}"


def vc_page_frame(page: int) -> str:
    return f"VC_PAGE{SEP}{page}"


def cmd_frame(command: str, *args: Any) -> str:
    return SEP.join(("QLC+CMD", command, *(str(a) for a in args), ""))


NOISE_FRAMES: tuple[str, ...] = (
    api_frame("getFunctionsList"),
    api_frame("getChannelsValues", 1, 0, 10),
    gm_value_frame(127),
    vc_page_frame(0),
    cmd_frame("BLACKOUT"),
    "QLC+IO|1|2|3",
    "",
)


# --------------------------------------------------------------------------- #
# parsing a request we received (what the client sends us)
# --------------------------------------------------------------------------- #
class BadFrame(AssertionError):
    """Raised when a frame the client sent does not match the frozen spec."""


def parse_request(frame: str) -> dict[str, Any]:
    """Strictly validate and unpack a request frame.

    Enforces the two things the spec cares about: the payload sits in exactly
    one field (so no ``|`` from user text leaked out), and it decodes to the JSON
    the client meant to send.
    """
    if "\n" in frame or "\r" in frame:
        raise BadFrame(f"frame contains a raw newline, framing would break: {frame!r}")
    parts = frame.split(SEP)
    if len(parts) != 4:
        raise BadFrame(
            f"request frame must have exactly 4 fields, got {len(parts)}: {frame!r}"
        )
    prefix, req_id, verb, payload = parts
    if prefix != PREFIX:
        raise BadFrame(f"request frame must start with {PREFIX!r}: {frame!r}")
    if not req_id:
        raise BadFrame(f"request frame has an empty reqId: {frame!r}")
    if not verb:
        raise BadFrame(f"request frame has an empty verb: {frame!r}")
    try:
        args = decode_payload(payload)
    except Exception as exc:  # noqa: BLE001 - any failure is the point
        raise BadFrame(f"payload is not base64(UTF-8(JSON)): {payload!r} ({exc})") from exc
    if not isinstance(args, (dict, list)):
        raise BadFrame(f"payload must decode to an object, got {type(args).__name__}")
    return {"req_id": req_id, "verb": verb, "payload_b64": payload, "args": args, "raw": frame}
