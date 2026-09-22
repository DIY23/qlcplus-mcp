#!/usr/bin/env python3
"""End-to-end smoke test for the QLC+ Agent API against a REAL running instance.

Starts the freshly built qlcplus-qml with web access enabled, speaks the
QLC+AGENT protocol over the websocket and checks that a project can be built
live (patch -> fixture -> scene -> cue list widget -> save) and re-read.

Usage:
    uv run --with websockets tests/agent_smoke.py [--keep]

Exit code 0 = all checks passed.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

BIN = Path("/Users/tom/Hermes/qlcplus/build/qmlui/qlcplus-qml")
OUT_PROJECT = Path("/Users/tom/Hermes/qlcplus/tests/out/agent-smoke.qxw")


def port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


class AgentClient:
    def __init__(self, ws):
        self.ws = ws
        self.seq = 0

    async def call(self, verb: str, args: dict | None = None, timeout: float = 15.0) -> dict:
        self.seq += 1
        req_id = str(self.seq)
        payload = base64.b64encode(json.dumps(args or {}).encode()).decode()
        await self.ws.send(f"QLC+AGENT|{req_id}|{verb}|{payload}")

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{verb}: no reply within {timeout}s")
            raw = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
            parts = str(raw).split("|")
            if len(parts) < 5 or parts[0] != "QLC+AGENT":
                print(f"      (skipping non-agent message: {str(raw)[:60]})")
                continue
            if parts[2] != req_id:
                print(f"      (skipping reply for another request: {parts[1]} {parts[2]})")
                continue
            status = parts[3]
            data = json.loads(base64.b64decode(parts[4]).decode() or "{}")
            if status == "err":
                raise RuntimeError(f"{verb} failed: {data.get('error')}")
            return data


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave QLC+ running afterwards")
    ap.add_argument("--port", type=int, default=9999)
    args = ap.parse_args()

    if not BIN.exists():
        print(f"FAIL: {BIN} not found - build first")
        return 2

    OUT_PROJECT.parent.mkdir(parents=True, exist_ok=True)
    if OUT_PROJECT.exists():
        OUT_PROJECT.unlink()

    print(f"starting {BIN} -w -p {args.port}")
    log = open("/tmp/qlcplus-agent-smoke.log", "wb")
    env = dict(os.environ)
    env["QLC_LOG"] = "1"
    proc = subprocess.Popen([str(BIN), "-w", "-p", str(args.port)], stdout=log, stderr=log,
                            env=env, cwd="/Users/tom/Hermes/qlcplus/build")
    try:
        for _ in range(90):
            if port_open("127.0.0.1", args.port):
                break
            if proc.poll() is not None:
                print("FAIL: QLC+ exited during startup, see /tmp/qlcplus-agent-smoke.log")
                return 2
            time.sleep(1)
        else:
            print("FAIL: web access port never opened, see /tmp/qlcplus-agent-smoke.log")
            return 2
        print("  web access up")

        import websockets

        url = f"ws://127.0.0.1:{args.port}/qlcplusWS"
        async with websockets.connect(url, max_size=None) as ws:
            client = AgentClient(ws)
            checks: list[tuple[str, bool, str]] = []

            def check(name: str, ok: bool, info: str = "") -> None:
                checks.append((name, ok, info))
                print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' - ' + info) if info else ''}")

            # 1. state
            state = await client.call("getState")
            check("getState returns app + universes",
                  "app" in state and "universes" in state,
                  f"{len(state.get('universes', []))} universes, "
                  f"{len(state.get('fixtures', []))} fixtures")

            # 2. fixture definitions
            defs = await client.call("listFixtureDefs", {"manufacturer": "Generic"})
            general = defs.get("manufacturers", [])
            check("listFixtureDefs finds Generic fixtures", len(general) > 0,
                  f"{len(general)} manufacturers matched")

            # 3. patch universe 0 to the Loopback plugin (always available)
            patched = await client.call("patchUniverse",
                                        {"universe": 0, "plugin": "Loopback", "line": 0,
                                         "direction": "output"})
            check("patchUniverse patches output", patched.get("plugin") == "Loopback",
                  json.dumps(patched))

            # 4. add fixtures (generic dimmer - always present in the fixture cache)
            added = await client.call("addFixture",
                                      {"manufacturer": "Generic", "model": "Generic Dimmer",
                                       "mode": "1 Channel", "name": "Smoke PAR",
                                       "universe": 0, "address": 0, "quantity": 4, "gap": 1})
            fixture_ids = added.get("ids", [])
            check("addFixture creates 4 fixtures", len(fixture_ids) == 4, str(fixture_ids))

            # 5. scene with values on those fixtures
            scene = await client.call("createFunction",
                                      {"type": "Scene", "name": "Smoke - Full Up",
                                       "fixtureIds": fixture_ids})
            scene_id = scene.get("id")
            values = [{"fixture": fid, "channel": 0, "value": 255} for fid in fixture_ids]
            applied = await client.call("setSceneValues", {"id": scene_id, "values": values})
            check("setSceneValues writes 4 values", applied.get("applied") == 4, json.dumps(applied))

            # 6. a second scene + chaser over both
            scene2 = await client.call("createFunction",
                                       {"type": "Scene", "name": "Smoke - Half",
                                        "fixtureIds": fixture_ids})
            await client.call("setSceneValues",
                              {"id": scene2["id"],
                               "values": [{"fixture": fid, "channel": 0, "value": 128}
                                          for fid in fixture_ids]})
            chaser = await client.call("createFunction", {"type": "Chaser", "name": "Smoke - Sequence"})
            chaser_id = chaser["id"]
            await client.call("addChaserSteps",
                              {"id": chaser_id,
                               "steps": [{"functionId": scene_id, "hold": 1000},
                                         {"functionId": scene2["id"], "hold": 1000}]})
            detail = await client.call("getFunction", {"id": chaser_id})
            check("chaser has 2 steps", len(detail.get("steps", [])) == 2,
                  json.dumps(detail.get("steps")))

            # 7. virtual console layout: page + cue list widget bound to the chaser
            page = await client.call("addPage", {})
            page_index = page.get("index", 0)
            widget = await client.call("addWidget",
                                       {"page": page_index, "type": "CueList", "x": 40, "y": 40,
                                        "w": 260, "h": 320, "caption": "Smoke Cues",
                                        "functionId": chaser_id})
            widget_id = widget.get("id")
            check("addWidget creates a cue list", isinstance(widget_id, int) and widget_id > 0,
                  json.dumps({k: widget.get(k) for k in ("id", "type", "caption", "page")}))

            # 8. widget readback
            widget_detail = await client.call("getWidget", {"id": widget_id})
            check("getWidget returns the widget", widget_detail.get("id") == widget_id,
                  f"type={widget_detail.get('type')} caption={widget_detail.get('caption')}")

            # 9. run / stop the chaser
            running = await client.call("setFunctionStatus", {"id": chaser_id, "run": True})
            await asyncio.sleep(0.5)
            stopped = await client.call("setFunctionStatus", {"id": chaser_id, "run": False})
            check("setFunctionStatus runs then stops", running.get("running") is True
                  and stopped.get("running") is False,
                  f"run={running.get('running')} stop={stopped.get('running')}")

            # 10. unknown verb must error, not hang
            try:
                await client.call("nonsenseVerb", {}, timeout=5)
                check("unknown verb reported as error", False, "no error raised")
            except RuntimeError as exc:
                check("unknown verb reported as error", "unknown verb" in str(exc), str(exc))

            # 11. save the project
            saved = await client.call("saveProject", {"path": str(OUT_PROJECT)})
            check("saveProject writes the file", OUT_PROJECT.exists()
                  and OUT_PROJECT.stat().st_size > 1000,
                  f"{OUT_PROJECT.stat().st_size if OUT_PROJECT.exists() else 0} bytes")

            # 12. the saved file really contains what we built
            if OUT_PROJECT.exists():
                text = OUT_PROJECT.read_text(errors="replace")
                has_scene = "Smoke - Full Up" in text
                has_widget = "Smoke Cues" in text
                has_fixture = text.count("<Fixture>") >= 4
                check("saved project contains fixtures/scenes/widgets",
                      has_scene and has_widget and has_fixture,
                      f"scene={has_scene} widget={has_widget} fixtures={text.count('<Fixture>')}")

            # 13. state reflects everything after the edits
            state2 = await client.call("getState")
            names = [f["name"] for f in state2.get("functions", [])]
            check("getState sees the new functions",
                  "Smoke - Full Up" in names and "Smoke - Sequence" in names,
                  f"{len(names)} visible functions")

            failed = [c for c in checks if not c[1]]
            print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
            return 0 if not failed else 1
    finally:
        if args.keep:
            print(f"QLC+ left running (pid {proc.pid})")
        else:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
