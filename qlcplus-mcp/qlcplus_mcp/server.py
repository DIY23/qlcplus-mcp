"""MCP server exposing the QLC+ Agent API as tools.

Design choice (stated because the task asked): this uses the **official ``mcp``
Python SDK**, specifically ``mcp.server.mcpserver.MCPServer`` — the class that
the SDK v1 shipped as ``FastMCP`` and renamed in v2.0 (``mcp.server.fastmcp``
now raises a migration error pointing at it). So we get the official SDK, its
schema generation and its stdio transport, on whatever ``mcp`` version is
installed, instead of hand-rolling JSON-RPC. The venv here has ``mcp==2.2.0``.

Every tool maps 1:1 to one ``QLC+AGENT`` verb and does nothing but forward its
arguments. Each description states, in plain language, that agent edits apply
live to the running project and that undo (Cmd+Z) does not cover them.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator

try:
    # MCP SDK v2. In v1 this class was called FastMCP and lived at
    # mcp.server.fastmcp; v2 raises a migration error from that path.
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError as exc:  # pragma: no cover - depends on the installed SDK
    raise ImportError(
        "qlcplus-mcp needs the MCP Python SDK v2 or newer (its high level server "
        "was renamed from FastMCP to MCPServer). Install it with: "
        "uv pip install 'mcp>=2,<3'"
    ) from exc

from pydantic import Field

from .client import QlcAgentClient, client_from_env
from .framing import (
    QlcAgentError,
    QlcConnectionError,
    QlcError,
    QlcProtocolError,
    QlcTimeoutError,
)

__all__ = ["mcp", "main", "TOOLS", "WRITE_VERBS", "READ_VERBS"]

SERVER_NAME = "qlcplus-agent"
SERVER_VERSION = "1.0.0"

# --------------------------------------------------------------------------- #
# Shared wording for the tool descriptions.
#
# Two hard requirements from the brief: plain language (no protocol jargon) and
# an explicit warning that changes land live and that Cmd+Z will not take them
# back. Keeping the wording in one place means no tool can quietly omit it.
# --------------------------------------------------------------------------- #
LIVE_WARNING = (
    "This changes the project that is running right now - it takes effect "
    "immediately, with no staging step and no confirmation prompt. Undo "
    "(Cmd+Z) does NOT cover edits made by the agent, so a change you regret "
    "has to be reversed by another tool call, or by re-opening the project."
)

READ_ONLY_NOTE = (
    "This only looks at the project that is running and changes nothing. "
    "Undo (Cmd+Z) does NOT cover edits made by the agent, so this cannot roll "
    "one back either."
)

# Parameter descriptions reused across tools.
PAGE_INDEX = "Which page to act on. Pages are numbered from 0. -1 means the end of the list."
FIXTURE_ID = "The number that identifies this fixture in the project (from getState)."


def _live(text: str) -> str:
    return f"{text} {LIVE_WARNING}"


def _read(text: str) -> str:
    return f"{text} {READ_ONLY_NOTE}"


# --------------------------------------------------------------------------- #
# Client plumbing
# --------------------------------------------------------------------------- #
_client: QlcAgentClient | None = None
_client_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock


async def get_client() -> QlcAgentClient:
    """Return the one long-lived client for this server process."""
    global _client
    async with _lock():
        if _client is None:
            _client = client_from_env()
        return _client


async def close_client() -> None:
    """Close the socket, if one was ever opened."""
    global _client
    client, _client = _client, None
    if client is not None:
        await client.close()


def explain_failure(verb: str, exc: QlcError) -> str:
    """Turn a client-level failure into something an operator can act on.

    The MCP SDK replaces an unexpected tool exception with a generic "Error
    executing tool X", which would hide the reason from the model. Raising
    ``ToolError`` instead keeps the whole message, so this text is what the
    agent (and the human reading the transcript) actually sees.
    """
    if isinstance(exc, QlcAgentError):
        return f"QLC+ refused {verb}: {exc.error}"
    if isinstance(exc, QlcTimeoutError):
        return (
            f"QLC+ did not answer {verb} in time, so the request was abandoned. "
            "Check QLC+ is still running and responsive; if the operation is "
            "genuinely slow (saving a large project, for example) raise "
            "QLC_TIMEOUT and try again."
        )
    if isinstance(exc, QlcConnectionError):
        return (
            f"Could not reach QLC+: {exc} "
            "Start QLC+ with web access enabled (qlcplus-qml -w) and check "
            "QLC_HOST, QLC_PORT and QLC_WS_PATH."
        )
    if isinstance(exc, QlcProtocolError):
        return f"QLC+ sent something this client could not understand: {exc}"
    return str(exc)


async def _call(verb: str, args: dict[str, Any]) -> str:
    """Forward one verb and hand back its answer as JSON text.

    The answer is returned as a JSON *string* rather than as the raw Python
    object on purpose: QLC+'s replies are arbitrary nested data, and letting the
    SDK convert them changes the shape (a returned list becomes one content
    block per item, which is not what QLC+ sent). One text block carrying the
    exact JSON is faithful for objects, lists and scalars alike.
    """
    client = await get_client()
    payload = {key: value for key, value in args.items() if value is not None}
    try:
        result = await client.request(verb, payload)
    except QlcError as exc:
        raise ToolError(explain_failure(verb, exc)) from exc
    return json.dumps(result, ensure_ascii=False)


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[dict[str, Any]]:
    """Hold the QLC+ socket open for the life of the MCP session."""
    try:
        yield {}
    finally:
        await close_client()


mcp: MCPServer = MCPServer(
    name=SERVER_NAME,
    title="QLC+ lighting control",
    version=SERVER_VERSION,
    instructions=(
        "Controls a running QLC+ lighting project: read what is patched and "
        "programmed, patch and remove fixtures, build scenes, chasers, "
        "sequences and collections, lay out the virtual console, and save the "
        "project. Every change applies live to the running project and undo "
        "(Cmd+Z) does NOT cover edits made through these tools, so prefer "
        "reading with getState (and listFixtureDefs to resolve fixture names) "
        "before you change anything."
    ),
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
@mcp.tool(
    structured_output=False,
    name="getState",
    title="Read the whole project",
    description=_read(
        "Report everything in the project as it stands: the channels (universes) "
        "and what drives each of them, every fixture with its name, make, model, "
        "mode, universe and starting address, every function (scene, chaser, "
        "sequence, collection) with whether it is playing and how long it lasts, "
        "and the virtual console pages with the controls on them. Read this "
        "first when you need to know what you are working with, and to get the "
        "numbers that the other tools ask for."
    ),
)
async def get_state() -> str:
    """Return the full project snapshot."""
    return await _call("getState", {})


@mcp.tool(
    structured_output=False,
    name="listFixtureDefs",
    title="List known fixture types",
    description=_read(
        "List the fixture types QLC+ can patch, grouped by maker, then by model, "
        "then by mode, with how many channels each mode uses. Use this to turn "
        "the name a person says out loud ('a Showtec Par 64', 'the Robe spots') "
        "into the exact maker, model and mode that adding a fixture needs. Leave "
        "both arguments out to see everything, or give a maker and/or a model to "
        "narrow it down. Only the definitions already downloaded into QLC+ on "
        "this machine can be listed."
    ),
)
async def list_fixture_defs(
    manufacturer: Annotated[
        str | None,
        Field(description="Only show this maker, e.g. 'Showtec'. Optional."),
    ] = None,
    model: Annotated[
        str | None,
        Field(description="Only show models whose name contains this text. Optional."),
    ] = None,
) -> str:
    """List fixture definitions from QLC+'s cache."""
    return await _call("listFixtureDefs", {"manufacturer": manufacturer, "model": model})


@mcp.tool(
    structured_output=False,
    name="getFunction",
    title="Read one function",
    description=_read(
        "Show the inside of one function, given its number: a scene shows the "
        "fixture and channel values it stores, a chaser shows its steps with "
        "their fade and hold times, and a sequence shows the scene it plays "
        "back."
    ),
)
async def get_function(
    id: Annotated[int, Field(description="Number of the function (from getState).")],
) -> str:
    """Return detail for a single function."""
    return await _call("getFunction", {"id": id})


@mcp.tool(
    structured_output=False,
    name="getWidget",
    title="Read one control on a page",
    description=_read(
        "Show one control on a virtual console page, given its number: the "
        "function it is wired to, where it sits and how big it is, its caption "
        "and how it looks."
    ),
)
async def get_widget(
    id: Annotated[int, Field(description="Number of the control (from getState).")],
) -> str:
    """Return detail for a single virtual console widget."""
    return await _call("getWidget", {"id": id})


# --------------------------------------------------------------------------- #
# Fixtures / patch
# --------------------------------------------------------------------------- #
@mcp.tool(
    structured_output=False,
    name="addFixture",
    title="Patch fixtures",
    description=_live(
        "Add one or more fixtures of the same type to a universe, starting at a "
        "given channel. Give the maker, model and mode exactly as "
        "listFixtureDefs reports them. 'quantity' adds that many side by side "
        "and 'gap' leaves blank channels between them, so quantity 4 with gap 1 "
        "spreads them out. The answer lists what was added, including the new "
        "numbers that the other tools then use. Check the channels you are about "
        "to occupy are free (getState shows what is patched) before adding."
    ),
)
async def add_fixture(
    manufacturer: Annotated[str, Field(description="Maker of the fixture, e.g. 'Showtec'.")],
    model: Annotated[str, Field(description="Model name, e.g. 'Par 64'.")],
    mode: Annotated[str, Field(description="Mode name, e.g. '1 Channel' or '6 Channel'.")],
    name: Annotated[str, Field(description="What to label this fixture in the project.")],
    universe: Annotated[int, Field(description="Which universe (line of channels) to patch into.")],
    address: Annotated[
        int,
        Field(
            description=(
                "Starting channel number for the fixture. Passed to QLC+ "
                "unchanged; QLC+ counts channels from 0."
            )
        ),
    ],
    quantity: Annotated[int, Field(description="How many to add. Defaults to 1.")] = 1,
    gap: Annotated[
        int, Field(description="Blank channels to leave between the copies. Defaults to 0.")
    ] = 0,
) -> str:
    """Patch fixtures into a universe."""
    return await _call(
        "addFixture",
        {
            "manufacturer": manufacturer,
            "model": model,
            "mode": mode,
            "name": name,
            "universe": universe,
            "address": address,
            "quantity": quantity,
            "gap": gap,
        },
    )


@mcp.tool(
    structured_output=False,
    name="removeFixture",
    title="Unpatch a fixture",
    description=_live(
        "Take one fixture out of the patch, by its number. Values that fixture "
        "had stored inside scenes are left behind rather than rewritten, so "
        "expect scenes to keep the slot even though the fixture is gone. Any "
        "control on a page that was wired to it becomes unbound."
    ),
)
async def remove_fixture(
    id: Annotated[int, Field(description=FIXTURE_ID)],
) -> str:
    """Remove a fixture from the patch."""
    return await _call("removeFixture", {"id": id})


@mcp.tool(
    structured_output=False,
    name="setFixtureAddress",
    title="Move a fixture to another channel",
    description=_live(
        "Point an existing fixture at a different universe and/or starting "
        "channel, leaving everything else about it alone. Nothing in the scenes "
        "or pages moves with it - only where the fixture listens."
    ),
)
async def set_fixture_address(
    id: Annotated[int, Field(description=FIXTURE_ID)],
    universe: Annotated[int, Field(description="The universe to move it into.")],
    address: Annotated[
        int,
        Field(
            description=(
                "The channel it should start at. Passed to QLC+ unchanged; QLC+ "
                "counts channels from 0."
            )
        ),
    ],
) -> str:
    """Change a fixture's universe and starting channel."""
    return await _call(
        "setFixtureAddress", {"id": id, "universe": universe, "address": address}
    )


@mcp.tool(
    structured_output=False,
    name="setFixtureName",
    title="Rename a fixture",
    description=_live(
        "Change the label a fixture carries through QLC+ (the same label you see "
        "in getState and in the patch view). It does not change the make or "
        "model, only what the fixture is called."
    ),
)
async def set_fixture_name(
    id: Annotated[int, Field(description=FIXTURE_ID)],
    name: Annotated[str, Field(description="The new label for the fixture.")],
) -> str:
    """Rename a single fixture."""
    return await _call("setFixtureName", {"id": id, "name": name})


@mcp.tool(
    structured_output=False,
    name="patchUniverse",
    title="Choose what drives a universe",
    description=_live(
        "Decide what feeds or listens to one universe. 'plugin' is the name of "
        "the connection QLC+ should use, for example ArtNet, E1.31, MIDI or "
        "Loopback; 'line' is which of that connection's outputs or inputs to "
        "use; and 'direction' says whether the universe is sent out (output), "
        "read in (input), or echoed back to the app that sent it (feedback). "
        "Use getState to see what each universe currently uses, so you can name "
        "the plugin spelling QLC+ already reports."
    ),
)
async def patch_universe(
    universe: Annotated[int, Field(description="Which universe to configure.")],
    plugin: Annotated[
        str, Field(description="Connection to use, e.g. 'ArtNet', 'E1.31', 'MIDI', 'Loopback'.")
    ],
    line: Annotated[int, Field(description="Which line of that connection to use, from 0.")],
    direction: Annotated[
        str,
        Field(description="One of 'output' (send out), 'input' (read in) or 'feedback'."),
    ] = "output",
) -> str:
    """Patch a universe to an input/output plugin."""
    return await _call(
        "patchUniverse",
        {"universe": universe, "plugin": plugin, "line": line, "direction": direction},
    )


# --------------------------------------------------------------------------- #
# Functions (scenes, chasers, sequences, collections)
# --------------------------------------------------------------------------- #
@mcp.tool(
    structured_output=False,
    name="createFunction",
    title="Create an empty function",
    description=_live(
        "Create a new, empty function and report the number it was given. "
        "'kind' must be one of Scene, Chaser, Sequence or Collection. A name is "
        "optional here and can be set later. For a scene you can pass the "
        "fixtures it should include, and for a collection or chaser the "
        "functions it should contain. Nothing starts playing by itself - use "
        "setFunctionStatus to start it."
    ),
)
async def create_function(
    type: Annotated[
        str, Field(description="One of 'Scene', 'Chaser', 'Sequence', 'Collection'.")
    ],
    name: Annotated[str | None, Field(description="Name for the new function. Optional.")] = None,
    fixtureIds: Annotated[
        list[int] | None,
        Field(
            description=(
                "Numbers of existing fixtures (for a scene) or functions (for a "
                "collection or chaser) to put inside it. Optional."
            )
        ),
    ] = None,
) -> str:
    """Create a new function of the requested kind."""
    return await _call(
        "createFunction", {"type": type, "name": name, "fixtureIds": fixtureIds}
    )


@mcp.tool(
    structured_output=False,
    name="renameFunction",
    title="Rename a function",
    description=_live("Give an existing function a different name."),
)
async def rename_function(
    id: Annotated[int, Field(description="Number of the function to rename.")],
    name: Annotated[str, Field(description="The new name.")],
) -> str:
    """Rename a function."""
    return await _call("renameFunction", {"id": id, "name": name})


@mcp.tool(
    structured_output=False,
    name="deleteFunction",
    title="Delete a function",
    description=_live(
        "Delete a function by its number. Anything that pointed at it - a button "
        "on a page, a step in a chaser, an entry in a collection - is left "
        "pointing at nothing, so check what uses it (getState shows the pages "
        "and their controls) if that matters."
    ),
)
async def delete_function(
    id: Annotated[int, Field(description="Number of the function to delete.")],
) -> str:
    """Delete a function."""
    return await _call("deleteFunction", {"id": id})


@mcp.tool(
    structured_output=False,
    name="setSceneValues",
    title="Set what a scene remembers",
    description=_live(
        "Store channel values inside a scene. Each entry names a fixture, a "
        "channel of that fixture, and the level to remember (0 to 255). With "
        "merge left on, anything you do not mention keeps the value it already "
        "had; with merge turned off, the scene is emptied first and ends up "
        "holding exactly the list you sent. If the scene is playing while you "
        "change it, the output follows."
    ),
)
async def set_scene_values(
    id: Annotated[int, Field(description="Number of the scene to change.")],
    values: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "One entry per channel: {'fixture': <fixture number>, "
                "'channel': <channel on that fixture>, 'value': 0-255}."
            )
        ),
    ],
    merge: Annotated[
        bool,
        Field(description="True keeps existing values, False replaces the whole scene."),
    ] = True,
) -> str:
    """Set the channel values stored in a scene."""
    return await _call("setSceneValues", {"id": id, "values": values, "merge": merge})


@mcp.tool(
    structured_output=False,
    name="addChaserSteps",
    title="Add steps to a chaser",
    description=_live(
        "Append steps to a chaser. Each step names the function it should play "
        "and how it should be timed in seconds: fadeIn to bring it up, hold to "
        "keep it there, fadeOut to take it back down. The steps land after "
        "whatever the chaser already had, in the order you give them."
    ),
)
async def add_chaser_steps(
    id: Annotated[int, Field(description="Number of the chaser to add to.")],
    steps: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "One entry per step: {'functionId': <function number>, "
                "'fadeIn': seconds, 'hold': seconds, 'fadeOut': seconds}."
            )
        ),
    ],
) -> str:
    """Append steps to a chaser."""
    return await _call("addChaserSteps", {"id": id, "steps": steps})


@mcp.tool(
    structured_output=False,
    name="setCollectionFunctions",
    title="Set what a collection holds",
    description=_live(
        "Replace the contents of a collection with exactly the functions in this "
        "list. When the collection is started, everything in it plays together. "
        "Anything that was in the collection but is not in your list is removed "
        "from it - the functions themselves still exist."
    ),
)
async def set_collection_functions(
    id: Annotated[int, Field(description="Number of the collection to change.")],
    functionIds: Annotated[
        list[int], Field(description="Numbers of the functions the collection should hold.")
    ],
) -> str:
    """Replace the function list of a collection."""
    return await _call("setCollectionFunctions", {"id": id, "functionIds": functionIds})


@mcp.tool(
    structured_output=False,
    name="setFunctionStatus",
    title="Start or stop a function",
    description=_live(
        "Start or stop one function, the same as pressing its button on the page: "
        "run=true starts it, run=false stops it. This is live output, so a scene "
        "you start here is immediately visible on the lights."
    ),
)
async def set_function_status(
    id: Annotated[int, Field(description="Number of the function to start or stop.")],
    run: Annotated[bool, Field(description="True starts it, False stops it.")],
) -> str:
    """Start or stop a function."""
    return await _call("setFunctionStatus", {"id": id, "run": run})


# --------------------------------------------------------------------------- #
# Virtual console pages and widgets
# --------------------------------------------------------------------------- #
@mcp.tool(
    structured_output=False,
    name="addPage",
    title="Add a virtual console page",
    description=_live(
        "Add a new page to the virtual console - a fresh screen of controls for "
        "the operator. 'index' says where in the strip of pages it goes (-1, the "
        "default, puts it at the end) and 'name' is the title shown on it."
    ),
)
async def add_page(
    index: Annotated[int, Field(description=PAGE_INDEX)] = -1,
    name: Annotated[str | None, Field(description="Title for the new page. Optional.")] = None,
) -> str:
    """Add a virtual console page."""
    return await _call("addPage", {"index": index, "name": name})


@mcp.tool(
    structured_output=False,
    name="deletePage",
    title="Delete a virtual console page",
    description=_live(
        "Delete one page of the virtual console, along with the controls that sit "
        "on it. The functions those controls were wired to are not deleted."
    ),
)
async def delete_page(
    index: Annotated[int, Field(description="Which page to delete.")],
) -> str:
    """Delete a virtual console page."""
    return await _call("deletePage", {"index": index})


@mcp.tool(
    structured_output=False,
    name="addWidget",
    title="Add a control to a page",
    description=_live(
        "Put a new control on a virtual console page and report the number it "
        "was given. 'kind' is one of Button, Slider, CueList, Label, XYPad, "
        "SpeedDial, Frame, Clock, AudioTriggers or Animation. x, y, w and h are "
        "where it sits and how big it is on the page (page pixels, top-left "
        "corner at 0,0). 'caption' is the text on it, and 'functionId' wires it "
        "to the function it should control. 'props' carries the extra settings "
        "that particular kind of control takes, such as colours or a slider "
        "range - check an existing control of the same kind with getWidget to "
        "see which settings it uses."
    ),
)
async def add_widget(
    page: Annotated[int, Field(description="Which page to put it on, from 0.")],
    type: Annotated[
        str,
        Field(
            description=(
                "One of 'Button', 'Slider', 'CueList', 'Label', 'XYPad', "
                "'SpeedDial', 'Frame', 'Clock', 'AudioTriggers', 'Animation'."
            )
        ),
    ],
    x: Annotated[int, Field(description="Distance from the left edge of the page.")],
    y: Annotated[int, Field(description="Distance from the top edge of the page.")],
    w: Annotated[int, Field(description="Width of the control.")],
    h: Annotated[int, Field(description="Height of the control.")],
    caption: Annotated[str | None, Field(description="Text shown on the control. Optional.")] = None,
    functionId: Annotated[
        int | None, Field(description="Function the control should drive. Optional.")
    ] = None,
    props: Annotated[
        dict[str, Any] | None,
        Field(description="Extra settings for this kind of control. Optional."),
    ] = None,
) -> str:
    """Add a virtual console widget to a page."""
    return await _call(
        "addWidget",
        {
            "page": page,
            "type": type,
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "caption": caption,
            "functionId": functionId,
            "props": props,
        },
    )


@mcp.tool(
    structured_output=False,
    name="setWidget",
    title="Change a control on a page",
    description=_live(
        "Change an existing control on a page. Pass only what you want different "
        "- its caption, the function it is wired to, where it sits (x, y), how "
        "big it is (w, h), or its extra settings (props). Everything you leave "
        "out stays as it was."
    ),
)
async def set_widget(
    id: Annotated[int, Field(description="Number of the control to change (from getState).")],
    caption: Annotated[str | None, Field(description="New text on the control. Optional.")] = None,
    functionId: Annotated[
        int | None, Field(description="Function to wire it to. Optional.")
    ] = None,
    x: Annotated[int | None, Field(description="New distance from the left edge. Optional.")] = None,
    y: Annotated[int | None, Field(description="New distance from the top edge. Optional.")] = None,
    w: Annotated[int | None, Field(description="New width. Optional.")] = None,
    h: Annotated[int | None, Field(description="New height. Optional.")] = None,
    props: Annotated[
        dict[str, Any] | None, Field(description="Extra settings for this control. Optional.")
    ] = None,
) -> str:
    """Change fields of an existing virtual console widget."""
    return await _call(
        "setWidget",
        {
            "id": id,
            "caption": caption,
            "functionId": functionId,
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "props": props,
        },
    )


@mcp.tool(
    structured_output=False,
    name="removeWidget",
    title="Delete a control from a page",
    description=_live(
        "Delete one control from a virtual console page by its number. The "
        "function it was wired to is left alone - only the control goes."
    ),
)
async def remove_widget(
    id: Annotated[int, Field(description="Number of the control to delete.")],
) -> str:
    """Delete a virtual console widget."""
    return await _call("removeWidget", {"id": id})


# --------------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------------- #
@mcp.tool(
    structured_output=False,
    name="saveProject",
    title="Save the project to disk",
    description=_live(
        "Write the project out to a file. With no path given it saves over the "
        "file the project was opened from, which is what makes everything done "
        "so far survive a restart or a power cut. Give a path to save a copy "
        "there instead. Saving is separate from applying: changes already take "
        "effect on the lights whether or not you save."
    ),
)
async def save_project(
    path: Annotated[
        str | None,
        Field(description="File to save to. Leave blank to save over the current project file."),
    ] = None,
) -> str:
    """Save the project, defaulting to the current project file."""
    return await _call("saveProject", {"path": path})


# --------------------------------------------------------------------------- #
# Verb inventory (used by the CLI and the tests)
# --------------------------------------------------------------------------- #
READ_VERBS: tuple[str, ...] = ("getState", "listFixtureDefs", "getFunction", "getWidget")

WRITE_VERBS: tuple[str, ...] = (
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

#: Every verb this server exposes, reads first.
TOOLS: tuple[str, ...] = READ_VERBS + WRITE_VERBS


def main() -> None:
    """Run the server on stdio (what MCP clients expect)."""
    transport = os.environ.get("QLC_MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run(transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
