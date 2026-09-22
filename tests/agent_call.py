#!/usr/bin/env python3
"""Call one QLC+ Agent API verb from the shell, for quick manual probing.

    uv run --no-project --with websockets python tests/agent_call.py getState
    uv run --no-project --with websockets python tests/agent_call.py addWidget '{"page":0,"type":"Label","caption":"Hi"}'

Requires a running QLC+ with web access enabled (see tests/agent_smoke.py).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time

PORT = int(os.environ.get("QLC_PORT", "9999"))
URL = f"ws://127.0.0.1:{PORT}/qlcplusWS"


async def call(verb: str, args: dict, timeout: float = 20.0) -> dict:
    import websockets

    async with websockets.connect(URL, max_size=None) as ws:
        payload = base64.b64encode(json.dumps(args).encode()).decode()
        await ws.send(f"QLC+AGENT|1|{verb}|{payload}")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no reply")
            parts = str(await asyncio.wait_for(ws.recv(), timeout=remaining)).split("|")
            if len(parts) < 5 or parts[0] != "QLC+AGENT" or parts[2] != "1":
                continue
            body = json.loads(base64.b64decode(parts[4]).decode() or "{}")
            return {"status": parts[3], "body": body}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    verb = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    reply = asyncio.run(call(verb, args))
    print(json.dumps(reply, indent=2))
    return 0 if reply["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
