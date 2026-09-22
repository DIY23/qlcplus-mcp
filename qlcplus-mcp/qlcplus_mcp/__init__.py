"""qlcplus-mcp: an MCP server and CLI in front of the QLC+ Agent API.

Layers, from the wire upwards:

* :mod:`qlcplus_mcp.framing` - the ``QLC+AGENT`` pipe-delimited framing and its
  error types. Pure functions, no sockets.
* :mod:`qlcplus_mcp.client` - the async websocket client: request/reply
  correlation by ``reqId``, per-request timeouts, skipping of other message
  families, event routing.
* :mod:`qlcplus_mcp.server` - the MCP server. One tool per agent verb.
* :mod:`qlcplus_mcp.cli` - command line access to both.
"""

from __future__ import annotations

__version__ = "1.0.0"

from .framing import (  # noqa: E402  (re-exported for convenience)
    QlcAgentError,
    QlcConnectionError,
    QlcError,
    QlcProtocolError,
    QlcTimeoutError,
    build_request_frame,
    decode_payload,
    encode_payload,
)

__all__ = [
    "__version__",
    "QlcError",
    "QlcConnectionError",
    "QlcTimeoutError",
    "QlcProtocolError",
    "QlcAgentError",
    "build_request_frame",
    "encode_payload",
    "decode_payload",
]
