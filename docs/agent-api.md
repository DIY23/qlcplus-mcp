# QLC+ Agent API (patch branch `agent-api`)

Goal: let an external agent (MCP server, script, test harness) **create and manage a show
project live** in a running QLC+ 5 instance — no app restart, no project reload.

Status: design frozen for v1. Implemented in `webaccess/src/webaccessagent.{h,cpp}`,
dispatched from `WebAccessQml::slotHandleWebSocketRequest()`.

## Why a patch

QLC+ 5 already exposes a WebSocket API on the Web Access port (default 9999) at
`ws://<host>:9999/qlcplusWS`. What it can do today:

| capability | status in stock 5.2.2 |
|---|---|
| read functions / widgets / channel values | yes (`QLC+API|get*`) |
| start / stop functions | yes (`QLC+API|setFunctionStatus`) |
| patch universes live (no reload) | yes (`QLC+IO|<uni>|OUTPUT|INPUT|FB|<plugin>|<line>`) |
| create fixture / function / VC widget / page | **no** |
| save project | no |

The engine already has the primitives (`Doc::addFixture`, `Doc::addFunction`,
`VCFrame::loadXML`, `Doc::saveXML`); the agent API just exposes them over the same socket.

## Wire format

Deliberately the same shape as the existing protocol so it rides the same socket, auth and
logging. Payload is base64(JSON) so no `|` or newline in user text can break framing.

    request   QLC+AGENT|<reqId>|<verb>|<base64(json-args)>
    reply     QLC+AGENT|<verb>|<reqId>|ok|<base64(json-result)>
    error     QLC+AGENT|<verb>|<reqId>|err|<base64(json {"error": "..."})>
    event     QLC+AGENT|<event>|event|<base64(json-payload)>

One request → exactly one reply, always correlated by `reqId` (echoed). Empty args are sent
as the base64 of `{}`. Unknown verbs reply `err` with `unknown verb` (never silence, so the
client can fail fast instead of hanging).

## Verbs, v1

Reads (no side effects):

| verb | args | result |
|---|---|---|
| `getState` | `{}` | full project snapshot: universes+patches, fixtures, functions, VC pages+widgets |
| `listFixtureDefs` | `{manufacturer?, model?}` | fixture definitions from `QLCFixtureDefCache`: manufacturers → models → modes → channel count |
| `getFunction` | `{id}` | detail for one function (scene values, chaser steps, bound scene for sequences) |
| `getWidget` | `{id}` | widget detail incl. bound function id, geometry, appearance props |

Writes:

| verb | args | notes |
|---|---|---|
| `addFixture` | `{manufacturer, model, mode, name, universe, address, quantity=1, gap=0, channels=1}` | ≥1 fixture; `Generic Dimmer` is generated (needs `channels`), no `.qxf` required |
| `removeFixture` | `{id}` | |
| `setFixtureAddress` | `{id, universe, address}` | move/re-patch a fixture |
| `setFixtureName` | `{id, name}` | |
| `patchUniverse` | `{universe, plugin, line, direction="output"}` | wraps existing `QLC+IO` patch path |
| `addUniverse` | `{id?}` | creates a universe (QLC+ 5 starts with 4; a 467-fixture rig needs 7). Idempotent: replies `created=false` when it already exists |
| `createFunction` | `{type: Scene\|Chaser\|Sequence\|Collection, name?, fixtureIds?[]}` | sequences get a hidden bound scene, as upstream does |
| `renameFunction` / `deleteFunction` | `{id, name?}` | |
| `setSceneValues` | `{id, values: [{fixture, channel, value}], merge=true}` | `merge=false` clears first |
| `addChaserSteps` | `{id, steps: [{functionId, fadeIn, hold, fadeOut}]}` | |
| `setCollectionFunctions` | `{id, functionIds: []}` | |
| `setFunctionStatus` | `{id, run}` | already exists as `QLC+API|setFunctionStatus` |
| `addPage` | `{index=-1}` | new Virtual Console page (v5 pages are unnamed — index only) |
| `deletePage` | `{index}` | refuses to remove the last page |
| `addWidget` | `{page, type, x, y, w, h, caption?, functionId?, props?}` | type: Button, Slider, CueList, Label, XYPad, SpeedDial, Frame, SoloFrame, Clock, AudioTriggers, Animation |
| `setWidget` | `{id, caption?, functionId?, x?, y?, w?, h?, props?}` | |
| `removeWidget` | `{id}` | |
| `saveProject` | `{path}` | writes workspace XML exactly as `App::saveXML` does; `path` is required in v1 — the engine `Doc` does not expose the current project file name to this module |

`props` in `addWidget` / `setWidget` are applied by QML property name (`backgroundColor`,
`foregroundColor`, `font`, `bgImage`, type-specific properties such as `flashOnButton`,
`startupIntensity`). Keys ending in `Color` are converted to `QColor`; everything else is
passed through `QJsonValue::toVariant()`.

Explicitly **out of scope for v1**: undo/redo integration — agent edits bypass the Tardis
undo stack (same as a project load). Documented in the tool descriptions so the user is never
surprised by Cmd+Z doing nothing.

## Implementation notes (where the hooks live)

- `Doc::addFixture(Fixture*)`, `Doc::addFunction(Function*)`, `Doc::deleteFixture`,
  `Doc::deleteFunction` — engine/src/doc.h.
- `QLCFixtureDefCache::fixtureDef(manuf, model)`, `QLCFixtureDef::mode(name)` — fixture defs.
- Fixture creation mirrors `qmlui/fixturemanager.cpp::addFixture()` (universe auto-extend,
  address advance, `fxi->setFixtureDefinition()`).
- Function creation mirrors `qmlui/functionmanager.cpp::createFunction()`.
- VC widgets and pages are created by feeding generated child XML through
  `VCFrame::loadXML()` (public) — the same code path project load uses, so behaviour can't
  drift from the file format. Geometry/caption/function binding all come from that XML.
- Save mirrors `qmlui/app.cpp::App::saveXML()` (temp file + rename, `Doc::saveXML`,
  `VirtualConsole::saveXML`).

## Client

`qlcplus-mcp/` — Python MCP server exposing these verbs as tools. It is a thin transport:
connect to `ws://127.0.0.1:9999/qlcplusWS`, frame requests, decode replies, surface errors.
