"""Command line access to the QLC+ Agent API and to the MCP tool list.

Two jobs, deliberately independent:

* ``tools`` / ``verbs`` print what the MCP server would expose. They read the
  server definition, so they need no QLC+ and no socket.
* ``call`` sends one verb straight down the family-4 framing and prints the
  answer. It talks to QLC+ directly (not through MCP), which makes it the right
  tool for checking the socket, the framing and a verb's argument shape before
  wiring an agent to it.

Usage is documented in ``--help`` and in README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Sequence

from . import __version__
from .client import QlcAgentClient, build_uri
from .framing import (
    QlcAgentError,
    QlcConnectionError,
    QlcError,
    QlcProtocolError,
    QlcTimeoutError,
    build_request_frame,
    decode_payload,
)
from .server import TOOLS, mcp

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

__all__ = ["main", "build_parser"]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _first_sentence(text: str) -> str:
    """Short human summary of a long tool description."""
    body = " ".join(text.split())
    for stop in (". ", " - ", " ("):
        if stop in body:
            return body.split(stop, 1)[0].rstrip(".") + "."
    return body


def _read_args(raw: str | None) -> Any:
    """Parse the ``args`` positional: inline JSON, ``-`` for stdin, else ``{}``."""
    if raw is None:
        return {}
    if raw == "-":
        raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QlcProtocolError(
            f"arguments must be JSON ({exc}); got: {raw[:200]!r}. "
            "Quote the whole object, e.g. call getFunction '{\"id\": 3}'"
        ) from exc
    if not isinstance(parsed, (dict, list)):
        raise QlcProtocolError(
            "arguments must be a JSON object (or a list of them), "
            f"not {type(parsed).__name__}"
        )
    return parsed


def _print_json(value: Any) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
    sys.stdout.write("\n")


async def _tool_list() -> list[dict[str, Any]]:
    tools = await mcp.list_tools()
    out: list[dict[str, Any]] = []
    for tool in tools:
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
        out.append(
            {
                "name": tool.name,
                "title": getattr(tool, "title", None),
                "description": tool.description or "",
                "required": list(schema.get("required", []) or []),
                "properties": sorted((schema.get("properties") or {}).keys()),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_tools(args: argparse.Namespace) -> int:
    tools = asyncio.run(_tool_list())
    if args.json:
        _print_json(tools)
        return EXIT_OK
    print(f"{len(tools)} tools exposed by the QLC+ MCP server:\n")
    for tool in tools:
        required = ", ".join(tool["required"]) or "no arguments"
        print(f"  {tool['name']}")
        print(f"      {_first_sentence(tool['description'])}")
        print(f"      arguments: {required}")
    print(
        "\nEach tool maps to one QLC+ agent verb and forwards its arguments "
        "unchanged.\nUse `verbs --json` for the full descriptions."
    )
    return EXIT_OK


def cmd_verbs(args: argparse.Namespace) -> int:
    tools = asyncio.run(_tool_list())
    if args.json:
        _print_json(tools)
        return EXIT_OK
    print(" ".join(tool["name"] for tool in tools))
    return EXIT_OK


def _client(args: argparse.Namespace) -> QlcAgentClient:
    return QlcAgentClient(
        host=args.host,
        port=args.port,
        path=args.path,
        uri=args.uri,
        timeout=args.timeout,
        connect_timeout=args.connect_timeout,
    )


def cmd_call(args: argparse.Namespace) -> int:
    verb = args.verb
    if not args.allow_unknown and verb not in TOOLS:
        print(
            f"warning: {verb!r} is not one of the {len(TOOLS)} verbs this project "
            "knows about; sending it anyway",
            file=sys.stderr,
        )
    if args.args_b64 is not None:
        payload = decode_payload(args.args_b64)
    else:
        payload = _read_args(args.args)

    async def _run() -> Any:
        client = _client(args)
        frame = build_request_frame("1", verb, payload)
        if args.frame:
            print(f"-> {frame}", file=sys.stderr)
        try:
            await client.connect()
            return await client.request(verb, payload)
        finally:
            await client.close()

    result = asyncio.run(_run())
    if args.compact:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    else:
        _print_json(result)
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    async def _run() -> tuple[QlcAgentClient, Any]:
        client = _client(args)
        await client.connect()
        try:
            return client, await client.request("getState", {})
        except BaseException:
            await client.close()
            raise

    client, state = asyncio.run(_run())
    print(f"connected: {client.uri}")
    if isinstance(state, dict):
        for key in ("universes", "fixtures", "functions", "pages"):
            value = state.get(key)
            if value is None:
                continue
            count = len(value) if hasattr(value, "__len__") else "-"
            print(f"  {key}: {count}")
        for key, value in state.items():
            if key not in ("universes", "fixtures", "functions", "pages"):
                print(f"  {key}: {value!r}")
    else:
        print(f"  answer: {json.dumps(state, ensure_ascii=False)[:200]}")
    print(f"pending requests after the call: {client.pending_count}")
    return EXIT_OK


def cmd_env(args: argparse.Namespace) -> int:
    uri = args.uri or build_uri(
        args.host or os.environ.get("QLC_HOST", "127.0.0.1"),
        int(args.port if args.port is not None else os.environ.get("QLC_PORT", 9999)),
        args.path or os.environ.get("QLC_WS_PATH", "/qlcplusWS"),
    )
    print(f"QLC_WS_URL / resolved socket : {uri}")
    print(f"QLC_HOST                     : {os.environ.get('QLC_HOST', '127.0.0.1 (default)')}")
    print(f"QLC_PORT                     : {os.environ.get('QLC_PORT', '9999 (default)')}")
    print(f"QLC_WS_PATH                  : {os.environ.get('QLC_WS_PATH', '/qlcplusWS (default)')}")
    print(f"QLC_TIMEOUT                  : {os.environ.get('QLC_TIMEOUT', '10 (default)')}")
    print(f"QLC_ENABLED                  : {os.environ.get('QLC_ENABLED', '1 (default)')}")
    print(f"python                       : {sys.executable}")
    print(f"qlcplus-mcp version          : {__version__}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def _add_connection_options(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    """Where QLC+ is, and how long to wait for it.

    Declared twice on purpose: once on the top level parser and once (via
    ``parents=``) on each subcommand, so both of these work::

        cli.py --timeout 5 call getState
        cli.py call getState --timeout 5

    On the subcommand copy the defaults are suppressed, so not passing the flag
    there leaves the value given before the subcommand alone instead of
    resetting it to the default.
    """
    default: Any = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument("--host", default=default, help="QLC+ host (env QLC_HOST, default 127.0.0.1)")
    parser.add_argument(
        "--port",
        type=int,
        default=default,
        help="QLC+ web port (env QLC_PORT, default 9999)",
    )
    parser.add_argument(
        "--path", default=default, help="socket path (env QLC_WS_PATH, default /qlcplusWS)"
    )
    parser.add_argument(
        "--uri", default=default, help="full ws:// URL, overrides host/port/path"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=default,
        help="seconds to wait for a reply (env QLC_TIMEOUT, default 10)",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=argparse.SUPPRESS if suppress_defaults else 5.0,
        help="seconds to wait for the socket (default 5)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=os.path.basename(sys.argv[0]) or "cli.py",
        description=(
            "Talk to a running QLC+ (launched with -w so its web socket exists), "
            "and inspect the MCP tools built on top of it."
        ),
        epilog=(
            "examples:\n"
            "  cli.py tools\n"
            "  cli.py verbs --json\n"
            "  cli.py check\n"
            "  cli.py call getState\n"
            "  cli.py call getFunction '{\"id\": 3}'\n"
            "  cli.py call setFunctionStatus '{\"id\": 3, \"run\": true}'\n"
            "  echo '{\"page\": 0, \"type\": \"Button\", \"x\": 20, \"y\": 20, "
            '"w": 120, "h": 60, "caption": "Blackout"}\' | cli.py call addWidget -\n'
            "  cli.py call getState --frame      # show the exact bytes sent\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"qlcplus-mcp {__version__}")
    _add_connection_options(parser)

    # Connection options that may also be given after the subcommand.
    shared = argparse.ArgumentParser(add_help=False)
    _add_connection_options(shared, suppress_defaults=True)

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p_tools = sub.add_parser("tools", help="list the MCP tools (no socket needed)")
    p_tools.add_argument("--json", action="store_true", help="machine-readable output")
    p_tools.set_defaults(func=cmd_tools)

    p_verbs = sub.add_parser("verbs", help="print the tool/verb names, one line")
    p_verbs.add_argument("--json", action="store_true", help="full details as JSON")
    p_verbs.set_defaults(func=cmd_verbs)

    p_call = sub.add_parser(
        "call",
        parents=[shared],
        help="send one verb straight to QLC+ and print the answer",
        description=(
            "Send one agent verb to QLC+ over the web socket. ARGS is a JSON "
            "object; omit it for a request with no arguments, or pass '-' to read "
            "the JSON from standard input."
        ),
    )
    p_call.add_argument("verb", help="verb to send, e.g. getState, addFixture, saveProject")
    p_call.add_argument(
        "args",
        nargs="?",
        help="JSON object of arguments (default: none); '-' reads it from stdin",
    )
    p_call.add_argument(
        "--args-b64", help="arguments already encoded the way the wire carries them"
    )
    p_call.add_argument("--compact", action="store_true", help="print the answer on one line")
    p_call.add_argument(
        "--frame",
        action="store_true",
        help="echo the exact outgoing line to stderr (debugging framing)",
    )
    p_call.add_argument(
        "--allow-unknown",
        action="store_true",
        help="do not warn when the verb is not one this project knows",
    )
    p_call.set_defaults(func=cmd_call)

    p_check = sub.add_parser(
        "check",
        parents=[shared],
        help="connect to QLC+ and summarise the project (diagnostic)",
    )
    p_check.set_defaults(func=cmd_check)

    sub.add_parser("env", parents=[shared], help="show the resolved configuration").set_defaults(
        func=cmd_env
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE

    try:
        return int(args.func(args))
    except QlcTimeoutError as exc:
        print(f"timeout: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except QlcAgentError as exc:
        print(f"QLC+ refused the request: {exc.error}", file=sys.stderr)
        return EXIT_ERROR
    except QlcConnectionError as exc:
        print(f"cannot reach QLC+: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except QlcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
