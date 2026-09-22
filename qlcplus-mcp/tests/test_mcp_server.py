"""MCP server tests: the tool list, the descriptions, and the real thing over stdio.

The stdio test starts the server as a subprocess the way an MCP client does,
points it at the fake QLC+ websocket, and drives it with the official client
SDK -- so it proves the whole path: MCP call -> tool -> framing -> socket.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import fake_wire
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from fake_server import FakeQlcServer

ROOT = Path(__file__).resolve().parent.parent

# The frozen verb list, written out from the brief on purpose: this test is not
# allowed to agree with server.py by construction.
READ_VERBS = ("getState", "listFixtureDefs", "getFunction", "getWidget")
WRITE_VERBS = (
    "addFixture",
    "removeFixture",
    "setFixtureAddress",
    "setFixtureName",
    "patchUniverse",
    "createFunction",
    "renameFunction",
    "deleteFunction",
    "setSceneValues",
    "addChaserSteps",
    "setCollectionFunctions",
    "setFunctionStatus",
    "addPage",
    "deletePage",
    "addWidget",
    "setWidget",
    "removeWidget",
    "saveProject",
)
ALL_VERBS = READ_VERBS + WRITE_VERBS

# Words an operator should never have to parse in a tool description.
JARGON = (
    "base64",
    "payload",
    "reqid",
    " json",
    "json ",
    "mcp",
    "endpoint",
    "websocket",
    "ws://",
    "schema",
    "verb",
    "api",
    "rpc",
    "stdio",
)


# --------------------------------------------------------------------------- #
# surface
# --------------------------------------------------------------------------- #
async def _tools():
    from qlcplus_mcp.server import mcp

    return await mcp.list_tools()


async def test_exactly_the_frozen_verbs_are_exposed():
    tools = await _tools()
    assert [tool.name for tool in tools] == list(ALL_VERBS)


async def test_read_tools_are_listed_first_and_each_verb_appears_once():
    names = [tool.name for tool in await _tools()]
    assert names[:4] == list(READ_VERBS)
    assert len(names) == len(set(names)) == 22


async def test_every_description_warns_about_undo():
    for tool in await _tools():
        text = tool.description or ""
        assert "undo" in text.lower(), f"{tool.name} does not mention undo"
        assert "Cmd+Z" in text, f"{tool.name} does not name the Cmd+Z shortcut"


async def test_write_tools_say_the_change_happens_live():
    tools = {tool.name: tool for tool in await _tools()}
    for verb in WRITE_VERBS:
        text = tools[verb].description or ""
        assert "changes the project that is running" in text, verb
        assert "immediately" in text, verb


async def test_read_tools_say_they_change_nothing():
    tools = {tool.name: tool for tool in await _tools()}
    for verb in READ_VERBS:
        text = tools[verb].description or ""
        assert "changes nothing" in text, verb


async def test_descriptions_are_plain_language():
    for tool in await _tools():
        text = (tool.description or "").lower()
        for word in JARGON:
            assert word not in text, f"{tool.name} leaks jargon {word!r}"
        assert len(text) > 80, f"{tool.name} description too thin to be useful"


async def test_every_tool_has_a_human_title():
    for tool in await _tools():
        assert getattr(tool, "title", None), f"{tool.name} has no title"


@pytest.mark.parametrize(
    ("verb", "required", "optional"),
    [
        ("getState", set(), set()),
        ("listFixtureDefs", set(), {"manufacturer", "model"}),
        ("getFunction", {"id"}, set()),
        ("getWidget", {"id"}, set()),
        ("addFixture", {"manufacturer", "model", "mode", "name", "universe", "address"},
         {"quantity", "gap"}),
        ("removeFixture", {"id"}, set()),
        ("setFixtureAddress", {"id", "universe", "address"}, set()),
        ("setFixtureName", {"id", "name"}, set()),
        ("patchUniverse", {"universe", "plugin", "line"}, {"direction"}),
        ("createFunction", {"type"}, {"name", "fixtureIds"}),
        ("renameFunction", {"id", "name"}, set()),
        ("deleteFunction", {"id"}, set()),
        ("setSceneValues", {"id", "values"}, {"merge"}),
        ("addChaserSteps", {"id", "steps"}, set()),
        ("setCollectionFunctions", {"id", "functionIds"}, set()),
        ("setFunctionStatus", {"id", "run"}, set()),
        ("addPage", set(), {"index", "name"}),
        ("deletePage", {"index"}, set()),
        ("addWidget", {"page", "type", "x", "y", "w", "h"},
         {"caption", "functionId", "props"}),
        ("setWidget", {"id"}, {"caption", "functionId", "x", "y", "w", "h", "props"}),
        ("removeWidget", {"id"}, set()),
        ("saveProject", set(), {"path"}),
    ],
)
async def test_argument_names_match_the_frozen_spec(verb, required, optional):
    tools = {tool.name: tool for tool in await _tools()}
    schema = tools[verb].input_schema
    properties = set(schema.get("properties", {}))
    assert set(schema.get("required", [])) == required, verb
    assert properties == required | optional, verb


async def test_defaults_match_the_spec():
    tools = {tool.name: tool for tool in await _tools()}

    def default(verb, param):
        return tools[verb].input_schema["properties"][param].get("default")

    assert default("addFixture", "quantity") == 1
    assert default("addFixture", "gap") == 0
    assert default("patchUniverse", "direction") == "output"
    assert default("setSceneValues", "merge") is True
    assert default("addPage", "index") == -1
    assert default("saveProject", "path") is None


async def test_parameter_descriptions_are_filled_in():
    tools = {tool.name: tool for tool in await _tools()}
    props = tools["addWidget"].input_schema["properties"]
    for name in ("page", "type", "x", "y", "w", "h", "caption", "functionId", "props"):
        assert props[name].get("description"), f"addWidget.{name} has no description"


# --------------------------------------------------------------------------- #
# end to end over stdio
# --------------------------------------------------------------------------- #
def _payload(result) -> object:
    """Whatever the tool returned, as Python data, across SDK versions.

    The tools hand back JSON text, so this may need more than one pass: an SDK
    that also fills in structured content can wrap the string one level deeper.
    """
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        value = structured
        if isinstance(value, dict) and set(value) == {"result"}:
            value = value["result"]
    else:
        value = "".join(getattr(block, "text", "") for block in result.content)
    while isinstance(value, str):
        value = json.loads(value)
    return value


def _is_error(result) -> bool:
    return bool(getattr(result, "is_error", getattr(result, "isError", False)))


def _text(result) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


async def _call_expecting_error(session, name: str, args: dict) -> str:
    """Call a tool that must fail; return the error text.

    Tolerates both ways an SDK can report a tool-level failure: an ``is_error``
    result or a raised exception.
    """
    try:
        result = await session.call_tool(name, args)
    except Exception as exc:  # noqa: BLE001 - either behaviour is acceptable
        return str(exc)
    assert _is_error(result), f"expected the call to fail, got: {_text(result)}"
    return _text(result)


@pytest.fixture
def stdio_params(server: FakeQlcServer):
    env = dict(os.environ)
    env.update(
        {
            "QLC_HOST": "127.0.0.1",
            "QLC_PORT": str(server.port),
            "QLC_WS_PATH": "/qlcplusWS",
            "QLC_TIMEOUT": "4",
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.pop("QLC_WS_URL", None)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "qlcplus_mcp"],
        env=env,
        cwd=str(ROOT),
    )


async def test_mcp_over_stdio_lists_and_calls_tools(server, stdio_params):
    async with stdio_client(stdio_params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=20.0) as session:
            await session.initialize()

            listing = await session.list_tools()
            assert [tool.name for tool in listing.tools] == list(ALL_VERBS)

            result = await session.call_tool("getState", {})
            assert not _is_error(result), _text(result)
            assert server.requests[0].verb == "getState"
            assert server.requests[0].payload_b64 == "e30="
            assert _payload(result)["verb"] == "getState"


async def test_mcp_over_stdio_forwards_arguments_and_defaults(server, stdio_params):
    async with stdio_client(stdio_params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=20.0) as session:
            await session.initialize()

            await session.call_tool(
                "addFixture",
                {
                    "manufacturer": "Showtec",
                    "model": "Par 64",
                    "mode": "6 Channel",
                    "name": "Front | Wash\nA second line",
                    "universe": 0,
                    "address": 24,
                },
            )
            sent = server.requests[0]
            assert sent.verb == "addFixture"
            assert sent.args["manufacturer"] == "Showtec"
            assert sent.args["address"] == 24
            # defaults are sent explicitly so QLC+ cannot disagree about them
            assert sent.args["quantity"] == 1
            assert sent.args["gap"] == 0
            assert sent.field_count == 4

            await session.call_tool("setFunctionStatus", {"id": 3, "run": True})
            assert server.requests[1].args == {"id": 3, "run": True}

            await session.call_tool("setSceneValues", {"id": 2, "values": [{"fixture": 1, "channel": 0, "value": 255}]})
            assert server.requests[2].args == {
                "id": 2,
                "values": [{"fixture": 1, "channel": 0, "value": 255}],
                "merge": True,
            }


async def test_mcp_over_stdio_carries_unicode_and_pipes(server, stdio_params):
    name = "Café | Zeus\ntwo lines 🎛️ 第三行"

    async def echo(server_, ws, request):
        await server_.send(
            fake_wire.ok_frame(
                request.req_id, request.verb, {"name": request.args.get("name"), "note": "应答 | ok\n"}
            )
        )

    server.handler = echo

    async with stdio_client(stdio_params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=20.0) as session:
            await session.initialize()
            result = await session.call_tool("setFixtureName", {"id": 1, "name": name})

            assert not _is_error(result), _text(result)
            assert server.requests[0].args == {"id": 1, "name": name}
            assert _payload(result)["name"] == name


async def test_mcp_over_stdio_surfaces_a_refusal_from_qlc(server, stdio_params):
    server.handler = FakeQlcServer.error_handler

    async with stdio_client(stdio_params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=20.0) as session:
            await session.initialize()
            message = await _call_expecting_error(
                session,
                "addFixture",
                {
                    "manufacturer": "Robe",
                    "model": "Spot",
                    "mode": "16 Channel",
                    "name": "S1",
                    "universe": 3,
                    "address": 0,
                },
            )

    assert "universe 4 is not patched" in message


async def test_mcp_over_stdio_times_out_instead_of_hanging(server, stdio_params):
    server.handler = FakeQlcServer.no_reply_handler
    stdio_params.env["QLC_TIMEOUT"] = "0.4"

    async with stdio_client(stdio_params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=20.0) as session:
            await session.initialize()
            loop = asyncio.get_running_loop()
            started = loop.time()
            message = await _call_expecting_error(session, "getState", {})
            elapsed = loop.time() - started
            assert elapsed < 10.0, f"took {elapsed:.1f}s, the timeout did not fire"

    assert "getState" in message


async def test_mcp_over_stdio_reports_a_dead_socket_clearly(server, stdio_params):
    """QLC+ not running at all: the tool must fail with a message that says so."""
    await server.stop()
    stdio_params.env["QLC_TIMEOUT"] = "1"

    async with stdio_client(stdio_params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=20.0) as session:
            await session.initialize()
            message = await _call_expecting_error(session, "getState", {})

    assert "QLC+" in message


async def test_mcp_over_stdio_passes_a_list_result_through(server, stdio_params):
    async def list_result(server_, ws, request):
        await server_.send(
            fake_wire.ok_frame(request.req_id, request.verb, [{"id": 1}, {"id": 2}])
        )

    server.handler = list_result

    async with stdio_client(stdio_params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=20.0) as session:
            await session.initialize()
            result = await session.call_tool("getState", {})
            assert not _is_error(result)
            assert len(result.content) == 1, "a list must not be split into several blocks"
            assert _payload(result) == [{"id": 1}, {"id": 2}]


async def test_tool_results_arrive_as_one_json_block(server, stdio_params):
    """A nested answer comes back verbatim, as a single block of JSON."""

    answer = {"universes": [{"id": 0, "plugin": "ArtNet"}], "fixtures": [], "note": "ok | fine"}

    async def full_state(server_, ws, request):
        await server_.send(fake_wire.ok_frame(request.req_id, request.verb, answer))

    server.handler = full_state

    async with stdio_client(stdio_params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=20.0) as session:
            await session.initialize()
            result = await session.call_tool("getState", {})
            assert not _is_error(result)
            assert len(result.content) == 1
            assert getattr(result, "structured_content", None) is None, (
                "answers should travel as one plain JSON block, not a wrapper"
            )
            assert _payload(result) == answer


def test_failures_are_explained_in_plain_language():
    from qlcplus_mcp.framing import (
        QlcAgentError,
        QlcConnectionError,
        QlcProtocolError,
        QlcTimeoutError,
    )
    from qlcplus_mcp.server import explain_failure

    refused = explain_failure("addFixture", QlcAgentError("addFixture", "1", "address 24 is taken"))
    assert "addFixture" in refused and "address 24 is taken" in refused

    timed_out = explain_failure("saveProject", QlcTimeoutError("gave up"))
    assert "saveProject" in timed_out and "QLC_TIMEOUT" in timed_out

    offline = explain_failure("getState", QlcConnectionError("socket refused"))
    assert "QLC+" in offline and "qlcplus-qml -w" in offline

    broken = explain_failure("getState", QlcProtocolError("bad base64"))
    assert "bad base64" in broken

    for text in (refused, timed_out, offline, broken):
        assert text.strip() and "Traceback" not in text


async def test_server_instructions_mention_the_undo_caveat():
    from qlcplus_mcp.server import mcp

    instructions = mcp.instructions or ""
    assert "Cmd+Z" in instructions
    assert "getState" in instructions
