"""CLI tests.

Every test runs the CLI as a real process, because that is how it will be used
(and how an MCP client will launch it), and it catches anything that only works
when the console happens to be a terminal.

The socket-facing tests are async and push the blocking subprocess onto a thread,
so the fake QLC+ server keeps serving in the test's own event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from fake_server import FakeQlcServer

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "cli.py"

ALL_VERBS = (
    "getState",
    "listFixtureDefs",
    "getFunction",
    "getWidget",
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


def run_cli(*args: str, port: int | None = None, stdin: str | None = None, timeout: float = 60.0):
    """Run ./cli.py in a subprocess and return the completed process."""
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("QLC_WS_URL", None)
    env["QLC_HOST"] = "127.0.0.1"
    env["QLC_WS_PATH"] = "/qlcplusWS"
    env["QLC_TIMEOUT"] = "4"
    if port is not None:
        env["QLC_PORT"] = str(port)
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        env=env,
        input=stdin,
        timeout=timeout,
        cwd=str(ROOT),
    )


async def arun_cli(*args: str, **kwargs):
    """Same, but off the event loop so the fake server can answer."""
    return await asyncio.to_thread(run_cli, *args, **kwargs)


# --------------------------------------------------------------------------- #
# offline commands
# --------------------------------------------------------------------------- #
def test_help_documents_the_commands_and_examples():
    done = run_cli("--help")
    assert done.returncode == 0
    for command in ("tools", "call", "check", "env", "verbs"):
        assert command in done.stdout
    assert "getState" in done.stdout, "examples should be in the help text"
    assert "QLC_PORT" in done.stdout


def test_tools_lists_every_verb_and_needs_no_socket():
    done = run_cli("tools")
    assert done.returncode == 0, done.stderr
    for verb in ALL_VERBS:
        assert verb in done.stdout, verb
    assert "22 tools" in done.stdout


def test_tools_json_is_machine_readable():
    done = run_cli("tools", "--json")
    assert done.returncode == 0, done.stderr
    tools = json.loads(done.stdout)
    assert len(tools) == 22
    assert tools[0]["name"] == "getState"
    widget = next(tool for tool in tools if tool["name"] == "addWidget")
    assert widget["required"] == ["page", "type", "x", "y", "w", "h"]
    assert "props" in widget["properties"]
    assert "Cmd+Z" in widget["description"]


def test_verbs_prints_one_line_of_names():
    done = run_cli("verbs")
    assert done.returncode == 0, done.stderr
    names = done.stdout.split()
    assert names[0] == "getState"
    assert "saveProject" in names
    assert len(names) == 22


def test_env_command_shows_the_resolved_configuration():
    done = run_cli("env")
    assert done.returncode == 0, done.stderr
    assert "ws://127.0.0.1:9999/qlcplusWS" in done.stdout
    assert "QLC_TIMEOUT" in done.stdout
    assert "qlcplus-mcp version" in done.stdout


def test_a_command_is_required():
    done = run_cli()
    assert done.returncode == 2
    assert "usage" in done.stdout.lower()


# --------------------------------------------------------------------------- #
# talking to QLC+ (the fake one)
# --------------------------------------------------------------------------- #
async def test_call_sends_the_verb_and_prints_the_answer(server: FakeQlcServer):
    done = await arun_cli("call", "getState", port=server.port)
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    assert payload["verb"] == "getState"
    assert server.requests[0].verb == "getState"
    assert server.requests[0].payload_b64 == "e30="


async def test_call_accepts_json_arguments(server: FakeQlcServer):
    done = await arun_cli("call", "getFunction", '{"id": 3}', port=server.port)
    assert done.returncode == 0, done.stderr
    assert server.requests[0].args == {"id": 3}
    assert json.loads(done.stdout)["echo"] == {"id": 3}


async def test_call_reads_arguments_from_stdin(server: FakeQlcServer):
    done = await arun_cli(
        "call", "addWidget", "-", port=server.port, stdin='{"page": 0, "type": "Button"}'
    )
    assert done.returncode == 0, done.stderr
    assert server.requests[0].args == {"page": 0, "type": "Button"}


async def test_call_carries_unicode_and_pipes(server: FakeQlcServer):
    name = "Café | Zeus\ntwo lines 🎛️"
    done = await arun_cli("call", "setFixtureName", json.dumps({"id": 1, "name": name}), port=server.port)
    assert done.returncode == 0, done.stderr
    assert server.requests[0].args == {"id": 1, "name": name}
    assert server.requests[0].field_count == 4


async def test_call_shows_the_frame_when_asked(server: FakeQlcServer):
    done = await arun_cli("call", "getState", "--frame", port=server.port)
    assert done.returncode == 0, done.stderr
    assert "-> QLC+AGENT|1|getState|e30=" in done.stderr


async def test_call_compact_prints_one_line(server: FakeQlcServer):
    done = await arun_cli("call", "getState", "--compact", port=server.port)
    assert done.returncode == 0, done.stderr
    assert done.stdout.count("\n") == 1


async def test_check_summarises_the_project(server: FakeQlcServer):
    async def full_state(server_, ws, request):
        from fake_wire import ok_frame

        await server_.send(
            ok_frame(
                request.req_id,
                request.verb,
                {
                    "universes": [{"id": 0}],
                    "fixtures": [{"id": 1}, {"id": 2}],
                    "functions": [{"id": 1}],
                    "pages": [],
                },
            )
        )

    server.handler = full_state
    done = await arun_cli("check", port=server.port)
    assert done.returncode == 0, done.stderr
    assert "connected: ws://127.0.0.1" in done.stdout
    assert "fixtures: 2" in done.stdout


def test_bad_json_arguments_fail_with_a_usable_message():
    done = run_cli("call", "getFunction", "{not json}")
    assert done.returncode == 1
    assert "JSON" in done.stderr
    assert "getFunction" in done.stderr


async def test_an_unknown_verb_warns_but_is_still_sent(server: FakeQlcServer):
    done = await arun_cli("call", "getSomethingNew", "{}", port=server.port)
    assert "not one of the 22 verbs" in done.stderr
    assert server.requests[0].verb == "getSomethingNew"


async def test_a_refusal_from_qlc_is_reported_on_stderr(server: FakeQlcServer):
    server.handler = FakeQlcServer.error_handler
    done = await arun_cli("call", "patchUniverse", '{"universe": 4}', port=server.port)
    assert done.returncode == 1
    assert "universe 4 is not patched" in done.stderr
    assert done.stdout.strip() == ""


def test_qlc_not_running_fails_cleanly():
    """Nothing listening on that port: a clear message, not a traceback."""
    done = run_cli("call", "getState", port=1)
    assert done.returncode == 1
    assert "cannot reach QLC+" in done.stderr
    assert "Traceback" not in done.stderr


async def test_timeout_flag_is_honoured(server: FakeQlcServer):
    server.handler = FakeQlcServer.no_reply_handler
    # after the subcommand
    done = await arun_cli("call", "getState", "--timeout", "0.3", port=server.port)
    assert done.returncode == 1
    assert "timeout" in done.stderr.lower()
    assert "getState" in done.stderr


async def test_connection_flags_work_before_the_subcommand_too(server: FakeQlcServer):
    server.handler = FakeQlcServer.no_reply_handler
    done = await arun_cli("--timeout", "0.3", "call", "getState", port=server.port)
    assert done.returncode == 1
    assert "0.3s" in done.stderr


async def test_a_flag_before_the_subcommand_is_not_reset_by_the_subcommand(server: FakeQlcServer):
    """The subcommand's copy of --host must not clobber the earlier value."""
    done = await arun_cli("--host", "127.0.0.1", "call", "getState", port=server.port)
    assert done.returncode == 0, done.stderr
    assert server.requests[0].verb == "getState"


async def test_uri_flag_reaches_the_right_socket(server: FakeQlcServer):
    done = await arun_cli("call", "getState", "--uri", server.uri)
    assert done.returncode == 0, done.stderr
    assert server.requests[0].verb == "getState"
