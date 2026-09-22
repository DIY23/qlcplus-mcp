# qlcplus-mcp

An MCP server (plus a CLI) that lets an AI agent drive **a running QLC+ 5 lighting
project** over QLC+'s own Web Access websocket: read the patch and the
programming, patch and unpatch fixtures, build scenes / chasers / sequences /
collections, lay out the virtual console, start and stop functions, and save the
project.

It speaks the fork's new **`QLC+AGENT`** message family, side by side with the
`QLC+API` / `GM_VALUE` / `VC_PAGE` traffic QLC+ already sends on the same socket.

```
agent  ->  MCP (stdio)  ->  qlcplus_mcp  ->  ws://127.0.0.1:9999/qlcplusWS  ->  QLC+
```

---

## 1. Requirements

| | |
|---|---|
| Python | 3.11 or newer (built and tested against 3.12.13; `uv venv --python 3.12`) |
| `mcp` Python SDK | **v2 or newer** — see [Why mcp>=2](#why-mcp2) |
| `websockets` | 13 or newer (built against 17.1) |
| QLC+ | a build of the fork that implements the `QLC+AGENT` family, running with web access enabled |
| `uv` | for creating the venv and installing (plain `venv` + `pip` works too) |

Nothing here talks to the network except the local socket to QLC+.

## 2. Install

```bash
cd /Users/tom/Hermes/qlcplus/qlcplus-mcp

# 1. a virtualenv on this Intel Mac (Python 3.12)
uv venv --python 3.12

# 2. install the package and its two dependencies, importable from anywhere
uv pip install -e .

# 3. (optional) the test dependencies
uv pip install -e ".[test]"
```

`uv` and `uvx` may live outside your default `PATH` on this machine — if `uv` is
not found, prefix the commands with `export PATH="$HOME/.hermes/bin:$PATH"`.

After `uv pip install -e .` the venv has both entry points:

```bash
.venv/bin/qlcplus-mcp           # the MCP server on stdio (what MCP clients call)
.venv/bin/qlcplus-agent         # the CLI
.venv/bin/python -m qlcplus_mcp # same as qlcplus-mcp, works from any directory
```

## 3. Make QLC+ listen

QLC+ only opens the websocket if it was started with web access enabled. The
macOS binary is `qlcplus-qml` (there is no `Contents/MacOS/QLC+`):

```bash
/Applications/QLC+.app/Contents/MacOS/qlcplus-qml -w
```

Flags this build accepts (read out of the installed binary, not from memory):

| Flag | Long form | Meaning |
|---|---|---|
| `-w` | `--web` | enable remote web access — **this is the one that opens the socket** |
| `-wp` | `--web-port` | set the port used for web access (default 9999) |
| `-wa` | `--web-auth` | require users authentication for web access |
| `-a` | `--web-auth-file` | file the web-access credentials are stored in |

Then confirm the socket is there before pointing anything at it:

```bash
lsof -nP -iTCP -sTCP:LISTEN | grep 9999
# qlcplus-qml ... TCP 127.0.0.1:9999 (LISTEN)

# and prove the protocol answers (no MCP involved):
cd /Users/tom/Hermes/qlcplus/qlcplus-mcp
source .venv/bin/activate          # so ./cli.py uses this project's venv
./cli.py call getState --compact
```

If you launch QLC+ with `-wa`, web access is behind HTTP basic authentication and
this client does not implement that handshake — leave authentication off, or put
QLC+ behind a local reverse proxy.

## 4. Configuration

Everything is environment-driven, so the same code works for the CLI, for tests
and for any MCP client.

| Variable | Default | Meaning |
|---|---|---|
| `QLC_HOST` | `127.0.0.1` | where QLC+ listens |
| `QLC_PORT` | `9999` | QLC+ web access port |
| `QLC_WS_PATH` | `/qlcplusWS` | socket path on that port |
| `QLC_TIMEOUT` | `10` | seconds to wait for an answer before failing (floats allowed) |
| `QLC_WS_URL` | — | full override, e.g. `ws://127.0.0.1:9999/qlcplusWS` |
| `QLC_ENABLED` | `1` | set to `0` to make every call refuse to touch QLC+ |
| `QLC_MCP_TRANSPORT` | `stdio` | transport for `python -m qlcplus_mcp` (`stdio`, `sse`, `streamable-http`) |

Command-line flags (`--host`, `--port`, `--path`, `--uri`, `--timeout`) override
the environment; either `cli.py --timeout 2 call getState` or
`cli.py call getState --timeout 2` works.

## 5. Wire protocol (the frozen `QLC+AGENT` family)

```
request   QLC+AGENT|<reqId>|<verb>|<base64(json-args)>
reply     QLC+AGENT|<verb>|<reqId>|ok|<base64(json-result)>
error     QLC+AGENT|<verb>|<reqId>|err|<base64(json {"error": "..."})>
event     QLC+AGENT|<event>|event|<base64(json-payload)>
```

* **One request → exactly one reply**, correlated by `reqId`; the server echoes the
  id back. `reqId` is an incrementing integer rendered as a string, which is the
  least surprising thing to hand a C++ side that parses it.
* **Payloads are base64 of UTF-8 JSON.** Base64 contains neither `|` nor a
  newline, so a fixture named `Front | Wash` with a multi-line caption can never
  split a frame. JSON escaping alone is *not* relied on for framing.
* **Empty arguments are sent as `base64("{}")` = `e30=`.**
* Replies may arrive **out of order** (several requests in flight) and are routed
  to the right caller.
* A request that is never answered raises after `QLC_TIMEOUT` instead of hanging.
* Frames from other families (`QLC+API|...`, `QLC+CMD|...`, `QLC+IO|...`,
  `VC_PAGE|...`, `GM_VALUE|...`) are recognised, skipped, and kept in
  `client.other_messages` for inspection — the reader never loses its place.
* `QLC+AGENT` frames that claim the family but match no legal shape are recorded in
  `client.protocol_errors` and skipped, so malformed traffic cannot desync a call.
* Events (`QLC+AGENT|<event>|event|<base64>`) never resolve a request. They go to
  `client.events` and to `await client.next_event()`. The MCP tool list is frozen
  to the 22 verbs below, so events are **not** exposed as tools.

Concrete example — `cli.py call setFixtureName '{"id": 1, "name": "Front | Wash"}'`
puts this on the wire:

```
QLC+AGENT|1|setFixtureName|eyJpZCI6MSwibmFtZSI6IkZyb250IHwgV2FzaCJ9
```

## 6. Tools

Every tool maps 1:1 to one agent verb and forwards its arguments. All of them say
in plain language that agent edits **apply live to the running project** and that
**undo (Cmd+Z) does not cover them**. Answers come back as a single block of JSON
text, exactly as QLC+ sent it.

### Reading

| Tool | Arguments | What it does |
|---|---|---|
| `getState` | — | the whole project: universes and what drives them, every fixture (name, make, model, mode, universe, address), every function with whether it is running and how long it lasts, pages and their controls |
| `listFixtureDefs` | `manufacturer?`, `model?` | fixture definitions in QLC+'s cache, grouped maker → model → mode with channel counts; use it to resolve what a person means by a fixture name |
| `getFunction` | `id` | one function in detail (scene values, chaser steps, the scene a sequence plays) |
| `getWidget` | `id` | one virtual console control (bound function, geometry, appearance) |

### Changing the patch

| Tool | Arguments |
|---|---|
| `addFixture` | `manufacturer`, `model`, `mode`, `name`, `universe`, `address`, `quantity=1`, `gap=0` |
| `removeFixture` | `id` |
| `setFixtureAddress` | `id`, `universe`, `address` |
| `setFixtureName` | `id`, `name` |
| `patchUniverse` | `universe`, `plugin`, `line`, `direction="output"` (output \| input \| feedback) |

### Changing the programming

| Tool | Arguments |
|---|---|
| `createFunction` | `type` (Scene \| Chaser \| Sequence \| Collection), `name?`, `fixtureIds?` |
| `renameFunction` | `id`, `name` |
| `deleteFunction` | `id` |
| `setSceneValues` | `id`, `values: [{fixture, channel, value}]`, `merge=True` |
| `addChaserSteps` | `id`, `steps: [{functionId, fadeIn, hold, fadeOut}]` |
| `setCollectionFunctions` | `id`, `functionIds` |
| `setFunctionStatus` | `id`, `run` |

### Changing the virtual console

| Tool | Arguments |
|---|---|
| `addPage` | `index=-1`, `name?` |
| `deletePage` | `index` |
| `addWidget` | `page`, `type`, `x`, `y`, `w`, `h`, `caption?`, `functionId?`, `props?` |
| `setWidget` | `id`, `caption?`, `functionId?`, `x?`, `y?`, `w?`, `h?`, `props?` |
| `removeWidget` | `id` |

`type` for `addWidget` is one of `Button`, `Slider`, `CueList`, `Label`, `XYPad`,
`SpeedDial`, `Frame`, `Clock`, `AudioTriggers`, `Animation`.

### Saving

| Tool | Arguments |
|---|---|
| `saveProject` | `path?` — blank means overwrite the file the project was opened from |

## 7. Using it

### Hermes Agent

Hermes reads `mcp_servers` from `~/.hermes/config.yaml` and registers the tools as
`mcp_<server>_<tool>` (so `mcp_qlcplus_getState`). Hermes passes only a filtered
environment to stdio servers, so the `QLC_*` variables have to be listed
explicitly:

```yaml
mcp_servers:
  qlcplus:
    command: "/Users/tom/Hermes/qlcplus/qlcplus-mcp/.venv/bin/python"
    args: ["-m", "qlcplus_mcp"]
    env:
      QLC_HOST: "127.0.0.1"
      QLC_PORT: "9999"
      QLC_WS_PATH: "/qlcplusWS"
      QLC_TIMEOUT: "10"
    timeout: 60          # per tool call, seconds
    connect_timeout: 30
```

Restart Hermes after editing the config — MCP servers are connected at startup.

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "qlcplus": {
      "command": "/Users/tom/Hermes/qlcplus/qlcplus-mcp/.venv/bin/python",
      "args": ["-m", "qlcplus_mcp"],
      "env": {
        "QLC_HOST": "127.0.0.1",
        "QLC_PORT": "9999",
        "QLC_WS_PATH": "/qlcplusWS",
        "QLC_TIMEOUT": "10"
      }
    }
  }
}
```

Any MCP client that can run a stdio server works the same way: run
`/Users/tom/Hermes/qlcplus/qlcplus-mcp/.venv/bin/python -m qlcplus_mcp` (or the
`qlcplus-mcp` script) with `QLC_*` in its environment. If you would rather not
install the package, set `PYTHONPATH=/Users/tom/Hermes/qlcplus/qlcplus-mcp` instead.

## 8. CLI

The CLI has two halves: `tools` / `verbs` read the MCP server definition (no
socket, no QLC+ needed), while `call` / `check` send verbs straight down the wire
— which makes them the right way to test the socket, the framing or an argument
shape before wiring an agent to it.

Run it either as the installed script (no activation needed) or as `./cli.py`
**after activating the venv** — this machine's ambient `python3` has neither `mcp`
nor `websockets`, so `./cli.py` on its own will fail to import:

```bash
cd /Users/tom/Hermes/qlcplus/qlcplus-mcp
source .venv/bin/activate                     # then `./cli.py ...` works
# ...or skip activation and use the entry point directly:
.venv/bin/qlcplus-agent <command>

./cli.py --help                       # every command and flag, with examples
./cli.py tools                        # the 22 tools, one line each
./cli.py tools --json                 # full details as JSON (names, args, descriptions)
./cli.py verbs                        # just the names, on one line
./cli.py env                          # resolved socket URL, env vars, versions

# --- talk to QLC+ ---
./cli.py check                        # connect and summarise the project
./cli.py call getState                # no arguments
./cli.py call getFunction '{"id": 3}'
./cli.py call setFunctionStatus '{"id": 3, "run": true}'
./cli.py call saveProject '{"path": "/Users/tom/shows/main.qxw"}'

echo '{"page": 0, "type": "Button", "x": 20, "y": 20, "w": 120, "h": 60, "caption": "Blackout"}' \
  | ./cli.py call addWidget -          # JSON from stdin
./cli.py call getState --frame         # show the exact line sent, on stderr
./cli.py call getState --compact       # answer on one line
./cli.py call getState --uri ws://127.0.0.1:9999/qlcplusWS
```

Behaviour worth knowing:

* a verb that is not one of the 22 prints a warning to stderr but is still sent
  (useful while the fork is still growing verbs); silence it with `--allow-unknown`
* `--frame` prints the outgoing frame so you can eyeball the framing
* exit codes: `0` success, `1` any QLC+ / connection / timeout failure, `2` misuse
* failures print one readable line to stderr — no tracebacks: QLC+ not running,
  QLC+ refusing a verb, or a timeout all read as a sentence

## 9. Tests

```bash
cd /Users/tom/Hermes/qlcplus/qlcplus-mcp
.venv/bin/python -m pytest -q
```

The suite needs **no QLC+ and no network**: `tests/fake_server.py` is a stand-in
websocket server on a random port that validates every request frame against the
frozen spec and can reply out of order, refuse, stay silent, or interleave the
other message families. `tests/fake_wire.py` re-implements the framing
independently, so a bug in the package's own encoder cannot agree with itself.

What is actually proved:

| Area | Tests |
|---|---|
| framing | request shape, `e30=` for empty args, payload decodes to the exact JSON, one delimiter field only, big/unicode/pipes/newlines, base64 + JSON validation, non-`QLC+AGENT` families never parse as ours |
| correlation | out-of-order replies, swapped replies, concurrent requests, unique ids, stray reply for an unknown id |
| skipping | noise before/between/after the reply, a batched multi-frame message, `QLC+API` frames quoting our own verb, malformed `QLC+AGENT` frames |
| errors | `err` replies raise `QlcAgentError` carrying the server's own text (unicode + pipes intact), and the client stays usable |
| timeouts | silent server raises in ~the configured time, per-call override, recovery afterwards, a late reply is recorded not delivered |
| round trip | 150 kB payloads with `|` and newlines in both directions, emoji / CJK / RTL |
| MCP | tool list and argument names against the frozen spec, plain-language descriptions with the Cmd+Z warning, real stdio session over the official SDK client, unicode through the whole path, refusals and timeouts reaching the caller |
| CLI | every command against the fake server, stdin JSON, framing display, exit codes, flags before and after the subcommand |

## 10. Design decisions and deviations

* **MCP server implementation:** the official `mcp` SDK, using
  `mcp.server.mcpserver.MCPServer` — the class v1 shipped as `FastMCP` and v2
  renamed. `mcp.server.fastmcp` no longer exists in v2 (importing it raises a
  migration error), so **`mcp>=2,<3` is a real requirement**, declared in
  `pyproject.toml` and checked with a clear message at import time. Using the SDK
  rather than hand-rolled JSON-RPC buys schema generation, the stdio transport and
  compatibility with every MCP client. The v1 → v2 rename is the one thing to know
  about this dependency; scroll to the end of this section for the detail.
* **Tool results are JSON text**, one content block, with structured output turned
  off. QLC+'s answers are arbitrary nested data, and letting the SDK convert them
  changes the shape — a returned list is silently split into one content block per
  item, which is not what QLC+ sent. One text block carrying the exact JSON is
  faithful for objects, lists and scalars alike.
* **Tool failures are re-raised as `ToolError`.** The SDK replaces an unexpected
  tool exception with a generic `Error executing tool X`, which would hide the
  reason from the model; `ToolError` keeps the whole sentence ("QLC+ refused
  addFixture: address 24 is already taken").
* **Defaults are sent explicitly.** `quantity`, `gap`, `direction` and `merge` are
  always included in the request, so QLC+ cannot disagree with this client about
  what the default is.
* **0-based addressing.** `address` is passed to QLC+ unchanged, and the tool
  descriptions state that QLC+ counts channels from 0. This follows QLC+'s
  internal convention; if the fork's `addFixture` expects 1-based input, this is
  the one place to change.
* **Reconnect on demand.** If QLC+ is restarted, the next tool call re-opens the
  socket rather than failing until the MCP server is restarted.
* **Events are not tools.** The verb list is frozen at 22, so unsolicited events go
  into the client's buffer (`events`, `next_event()`) and are not surfaced through
  MCP.
* **No handshake.** The client opens the socket and sends verbs; nothing in the
  frozen spec describes a registration step. If the fork grows one, it belongs in
  `QlcAgentClient.connect()`.
* **Not touched:** nothing outside this directory — in particular `/Users/tom/Hermes/qlcplus/src`
  (the C++ fork) was only read where helpful, never modified.

<a id="why-mcp2"></a>
**Why mcp>=2:** SDK v1 exposed the high-level server as `mcp.server.fastmcp.FastMCP`.
SDK v2 renamed it to `mcp.server.mcpserver.MCPServer` and left a `ModuleNotFoundError`
in the old location that tells you to migrate or pin `mcp<2`. This project targets
v2 (2.2.0 here) and says so loudly instead of failing with a confusing import error.

## 11. Files

```
qlcplus-mcp/
├── cli.py                  # runnable CLI shim (./cli.py ...) — the brief's entry point
├── pyproject.toml          # deps (mcp>=2,<3, websockets), entry points, pytest config
├── README.md
├── qlcplus_mcp/
│   ├── framing.py          # QLC+AGENT framing + error types. Pure, no sockets
│   ├── client.py           # async websocket client: reqId correlation, timeouts,
│   │                       #   skipping other families, event routing
│   ├── server.py           # MCP server: 22 tools, one per verb
│   ├── cli.py              # the CLI implementation behind the shim
│   └── __main__.py         # python -m qlcplus_mcp  ->  MCP server on stdio
└── tests/
    ├── fake_wire.py        # independent re-implementation of the wire format
    ├── fake_server.py      # fake QLC+ websocket with pluggable handlers
    ├── conftest.py         # fake server + client fixtures
    ├── test_framing.py     # protocol, no sockets
    ├── test_client.py      # framing, correlation, skipping, errors, timeouts, unicode
    ├── test_mcp_server.py  # tool surface + end-to-end stdio session
    └── test_cli.py         # the CLI, as real subprocesses
```
