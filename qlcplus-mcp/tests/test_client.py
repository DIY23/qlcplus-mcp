"""Client tests against the fake QLC+ websocket.

This is where the behavioural promises get proved: exact framing on the wire,
reply correlation by ``reqId`` including out-of-order replies, skipping of the
other message families that share the socket, clear exceptions for ``err``
replies, timeouts that raise instead of hanging, and unicode/pipes/newlines
surviving a round trip.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket

import fake_wire
import pytest
from fake_server import FakeQlcServer, ReceivedRequest
from qlcplus_mcp.client import QlcAgentClient, build_uri
from qlcplus_mcp.framing import (
    QlcAgentError,
    QlcConnectionError,
    QlcProtocolError,
    QlcTimeoutError,
)

# --------------------------------------------------------------------------- #
# framing on the wire
# --------------------------------------------------------------------------- #
async def test_request_arrives_exactly_as_the_spec_says(client, server):
    result = await client.request("getState", {})

    assert server.requests, "the fake QLC+ never received anything"
    request = server.requests[0]
    assert request.field_count == 4, f"wrong field count in {request.raw!r}"
    assert request.raw.startswith("QLC+AGENT|")
    assert request.verb == "getState"
    assert request.req_id == "1"
    assert request.payload_b64 == "e30=", "empty args must be base64('{}')"
    assert request.args == {}
    # and the answer came back through the same channel
    assert result["verb"] == "getState"


async def test_arguments_decode_to_exactly_what_was_sent(client, server):
    args = {
        "manufacturer": "Showtec",
        "model": "Par 64",
        "mode": "6 Channel",
        "name": 'Front | Wash "A"\nsecond line',
        "universe": 0,
        "address": 12,
        "quantity": 4,
        "gap": 1,
    }
    await client.request("addFixture", args)

    request = server.requests[0]
    assert request.args == args
    # the payload field is a single field: no '|' leaked out of the text
    assert request.field_count == 4
    assert request.raw.count("|") == 3
    assert "\n" not in request.raw
    # independently decodable straight from the raw bytes
    assert fake_wire.decode_payload(request.raw.split("|")[3]) == args


async def test_none_arguments_are_sent_as_an_empty_object_not_null(client, server):
    await client.request("getState", None)
    assert server.requests[0].payload_b64 == "e30="
    assert server.requests[0].args == {}


async def test_req_ids_are_unique_and_increase():
    server = FakeQlcServer()
    await server.start()
    try:
        async with QlcAgentClient(uri=server.uri, timeout=2.0) as client:
            await client.request("getState")
            await client.request("getState")
            await client.request("getState")
    finally:
        await server.stop()

    ids = server.req_ids_seen
    assert len(set(ids)) == 3, f"reqIds must be unique, got {ids}"
    assert [int(value) for value in ids] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# correlation
# --------------------------------------------------------------------------- #
async def test_out_of_order_replies_go_to_the_right_caller():
    async def reversed_replies(server: FakeQlcServer, ws, request: ReceivedRequest):
        # The 5th request answers first, the 1st answers last.
        delay = 0.02 * (6 - int(request.req_id))
        server.spawn(_delayed_reply(server, request, delay))

    async def _delayed_reply(server, request, delay):
        await asyncio.sleep(delay)
        await server.send(
            fake_wire.ok_frame(request.req_id, request.verb, {"who": request.req_id})
        )

    server = FakeQlcServer(handler=reversed_replies, greeting=[])
    await server.start()
    try:
        async with QlcAgentClient(uri=server.uri, timeout=3.0) as client:
            results = await asyncio.gather(
                *(client.request("getFunction", {"id": n}) for n in range(5))
            )
    finally:
        await server.stop()

    # Each caller got *its own* answer even though the server answered backwards.
    assert [r["who"] for r in results] == ["1", "2", "3", "4", "5"]
    assert [int(i) for i in server.req_ids_seen] == [1, 2, 3, 4, 5]


async def test_interleaved_replies_are_sorted_by_req_id():
    """Two requests in flight, answers swapped: each still lands correctly."""

    async def swapped(server: FakeQlcServer, ws, request: ReceivedRequest):
        other = "2" if request.req_id == "1" else "1"
        await server.send(fake_wire.ok_frame(other, request.verb, {"who": other}))
        await server.send(fake_wire.ok_frame(request.req_id, request.verb, {"who": request.req_id}))

    server = FakeQlcServer(handler=swapped, greeting=[])
    await server.start()
    try:
        async with QlcAgentClient(uri=server.uri, timeout=3.0) as client:
            first, second = await asyncio.gather(
                client.request("getWidget", {"id": 1}),
                client.request("getWidget", {"id": 2}),
            )
    finally:
        await server.stop()

    assert first == {"who": "1"}
    assert second == {"who": "2"}


async def test_a_reply_naming_an_unknown_req_id_does_not_resolve_anything():
    """A stray reply for a request we never made is recorded, not mis-delivered."""

    async def stray_then_real(server: FakeQlcServer, ws, request: ReceivedRequest):
        await server.send(fake_wire.ok_frame("9999", "getState", {"who": "ghost"}))
        await server.send(fake_wire.ok_frame(request.req_id, request.verb, {"who": "real"}))

    server = FakeQlcServer(handler=stray_then_real, greeting=[])
    await server.start()
    try:
        async with QlcAgentClient(uri=server.uri, timeout=2.0) as client:
            result = await client.request("getState")
            assert client.stray_replies, "the ghost reply should have been recorded"
    finally:
        await server.stop()

    assert result == {"who": "real"}


# --------------------------------------------------------------------------- #
# other message families on the same socket
# --------------------------------------------------------------------------- #
async def test_other_families_are_skipped_without_desync(client, server):
    """Noise before, between and after the reply must not disturb the answer."""

    async def noisy(server: FakeQlcServer, ws, request: ReceivedRequest):
        await server.send_noise(
            [
                fake_wire.api_frame("getFunctionsList"),
                fake_wire.gm_value_frame(127),
                fake_wire.vc_page_frame(0),
                fake_wire.cmd_frame("BLACKOUT"),
                "QLC+IO|1|2|3",
                "",
                "QLC+API|getChannelsValues|1|0|10",
            ]
        )
        await server.send(
            fake_wire.ok_frame(request.req_id, request.verb, {"survived": True})
        )
        await server.send_noise([fake_wire.gm_value_frame(0), fake_wire.api_frame("whatever")])

    server = FakeQlcServer(handler=noisy, greeting=[])
    await server.start()
    try:
        async with QlcAgentClient(uri=server.uri, timeout=2.0) as client:
            assert await client.request("getState") == {"survived": True}
            # a second request still works: the reader never lost the stream
            assert await client.request("getState") == {"survived": True}
            families = {message.family for message in client.other_messages}
    finally:
        await server.stop()

    assert "QLC+API" in families
    assert "GM_VALUE" in families
    assert "VC_PAGE" in families
    assert "QLC+CMD" in families
    assert "QLC+IO" in families
    assert client.pending_count == 0


async def test_greeting_noise_sent_at_connection_time_is_skipped(client, server):
    """The fixture server floods the socket the moment we connect."""
    assert await client.request("getState") is not None
    assert len(client.other_messages) >= 5
    assert client.protocol_errors == client.protocol_errors  # touch, no exception


async def test_an_api_frame_is_never_mistaken_for_a_reply():
    """A QLC+API frame that even quotes the verb must not resolve the request."""

    async def api_only(server: FakeQlcServer, ws, request: ReceivedRequest):
        await server.send(f"QLC+API|{request.verb}|{request.req_id}|0|0|255")

    server = FakeQlcServer(handler=api_only, greeting=[])
    await server.start()
    try:
        async with QlcAgentClient(uri=server.uri, timeout=0.4) as client:
            with pytest.raises(QlcTimeoutError):
                await client.request("getState")
    finally:
        await server.stop()


async def test_batched_frames_in_one_message_are_all_processed():
    """A sender that packs three frames into one message must still work."""

    async def batched(server: FakeQlcServer, ws, request: ReceivedRequest):
        packed = "\n".join(
            [
                fake_wire.api_frame("getFunctionsList"),
                fake_wire.ok_frame(request.req_id, request.verb, {"packed": 3}),
                fake_wire.gm_value_frame(9),
            ]
        )
        await server.send(packed)

    server = FakeQlcServer(handler=batched, greeting=[])
    await server.start()
    try:
        async with QlcAgentClient(uri=server.uri, timeout=2.0) as client:
            assert await client.request("getState") == {"packed": 3}
    finally:
        await server.stop()


async def test_a_malformed_agent_frame_is_recorded_and_the_stream_continues():
    async def garbage_then_reply(server: FakeQlcServer, ws, request: ReceivedRequest):
        await server.send("QLC+AGENT|broken")
        await server.send("QLC+AGENT|getState|1|maybe|e30=")
        await server.send(fake_wire.ok_frame(request.req_id, request.verb, {"fine": True}))

    server = FakeQlcServer(handler=garbage_then_reply, greeting=[])
    await server.start()
    try:
        async with QlcAgentClient(uri=server.uri, timeout=2.0) as client:
            assert await client.request("getState") == {"fine": True}
            assert len(client.protocol_errors) == 2
    finally:
        await server.stop()


async def test_other_message_callback_is_fed_and_a_bad_callback_is_survivable():
    seen = []

    def explode(message):
        seen.append(message.family)
        raise RuntimeError("callback is broken on purpose")

    server = FakeQlcServer(greeting=[fake_wire.api_frame("getFunctionsList")])
    await server.start()
    try:
        async with QlcAgentClient(uri=server.uri, timeout=2.0, on_other=explode) as client:
            assert await client.request("getState") is not None
    finally:
        await server.stop()

    assert "QLC+API" in seen
    assert "callback" in (client.last_error or "")


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
async def test_events_do_not_resolve_requests_and_are_buffered(client, server):
    async def event_first(server: FakeQlcServer, ws, request: ReceivedRequest):
        await server.send(fake_wire.event_frame("functionStarted", {"id": 3}))
        await server.send(fake_wire.event_frame("widgetChanged", {"id": 7, "caption": "Go | now"}))
        await server.send(fake_wire.ok_frame(request.req_id, request.verb, {"ok": True}))

    server.handler = event_first
    assert await client.request("getState") == {"ok": True}
    assert client.pending_count == 0

    event = await client.next_event(timeout=1.0)
    assert event.name == "functionStarted"
    assert event.payload() == {"id": 3}
    assert event.received_at is not None

    second = await client.next_event(timeout=1.0)
    assert second.payload()["caption"] == "Go | now"


async def test_next_event_waits_for_an_event_that_has_not_arrived_yet(client, server):
    async def later(server: FakeQlcServer, ws, request: ReceivedRequest):
        async def emit():
            await asyncio.sleep(0.05)
            await server.send(fake_wire.event_frame("fixtureAdded", {"id": 4}))

        server.spawn(emit())
        await server.send(fake_wire.ok_frame(request.req_id, request.verb, {}))

    server.handler = later
    await client.request("getState")
    event = await client.next_event(timeout=2.0)
    assert event.name == "fixtureAdded"
    assert event.payload() == {"id": 4}


async def test_next_event_times_out_clearly():
    server = FakeQlcServer(greeting=[])
    await server.start()
    try:
        async with QlcAgentClient(uri=server.uri, timeout=0.3) as client:
            await client.connect()
            with pytest.raises(QlcTimeoutError):
                await client.next_event(timeout=0.2)
    finally:
        await server.stop()


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
async def test_err_reply_raises_a_clear_exception(client, server):
    server.handler = FakeQlcServer.error_handler

    with pytest.raises(QlcAgentError) as excinfo:
        await client.request("patchUniverse", {"universe": 4, "plugin": "ArtNet", "line": 0})

    error = excinfo.value
    assert error.error == "universe 4 is not patched"
    assert error.verb == "patchUniverse"
    assert error.req_id == "1"
    assert "universe 4 is not patched" in str(error)
    assert "patchUniverse" in str(error)
    assert client.pending_count == 0, "a failed request must not stay pending"


async def test_err_reply_message_with_pipes_and_unicode_survives(client, server):
    message = "fixture 'Par | 64\n第二行' not found 🎛️"

    async def failing(server: FakeQlcServer, ws, request: ReceivedRequest):
        await server.send(fake_wire.err_frame(request.req_id, request.verb, message))

    server.handler = failing
    with pytest.raises(QlcAgentError) as excinfo:
        await client.request("removeFixture", {"id": 9})
    assert excinfo.value.error == message


async def test_an_error_for_one_request_does_not_break_the_next_one():
    async def sometimes_fails(server: FakeQlcServer, ws, request: ReceivedRequest):
        if request.args.get("id") == 1:
            await server.send(fake_wire.err_frame(request.req_id, request.verb, "no such fixture"))
        else:
            await server.send(fake_wire.ok_frame(request.req_id, request.verb, {"id": 2}))

    server = FakeQlcServer(handler=sometimes_fails, greeting=[])
    await server.start()
    try:
        async with QlcAgentClient(uri=server.uri, timeout=2.0) as client:
            with pytest.raises(QlcAgentError):
                await client.request("getFunction", {"id": 1})
            assert await client.request("getFunction", {"id": 2}) == {"id": 2}
            assert client.pending_count == 0
    finally:
        await server.stop()


# --------------------------------------------------------------------------- #
# timeouts
# --------------------------------------------------------------------------- #
async def test_a_silent_server_raises_instead_of_hanging():
    server = FakeQlcServer(handler=FakeQlcServer.no_reply_handler, greeting=[])
    await server.start()
    try:
        async with QlcAgentClient(uri=server.uri, timeout=0.25) as client:
            started = asyncio.get_running_loop().time()
            with pytest.raises(QlcTimeoutError) as excinfo:
                await client.request("saveProject")
            elapsed = asyncio.get_running_loop().time() - started
            assert elapsed < 2.0, f"timeout took {elapsed:.2f}s, far beyond the 0.25s asked"
            assert elapsed >= 0.2, "the timeout fired suspiciously early"
            assert "saveProject" in str(excinfo.value)
            assert "0.25s" in str(excinfo.value)
            assert client.pending_count == 0, "the abandoned request must be cleaned up"
    finally:
        await server.stop()


async def test_per_call_timeout_overrides_the_default():
    server = FakeQlcServer(handler=FakeQlcServer.no_reply_handler, greeting=[])
    await server.start()
    try:
        async with QlcAgentClient(uri=server.uri, timeout=30.0) as client:
            with pytest.raises(QlcTimeoutError) as excinfo:
                await client.request("getState", timeout=0.2)
            assert "0.2s" in str(excinfo.value)
    finally:
        await server.stop()


async def test_the_client_is_still_usable_after_a_timeout():
    calls = {"n": 0}

    async def slow_once(server: FakeQlcServer, ws, request: ReceivedRequest):
        calls["n"] += 1
        if calls["n"] == 1:
            return  # deliberately silent
        await server.send(fake_wire.ok_frame(request.req_id, request.verb, {"second": True}))

    server = FakeQlcServer(handler=slow_once, greeting=[])
    await server.start()
    try:
        async with QlcAgentClient(uri=server.uri, timeout=0.25) as client:
            with pytest.raises(QlcTimeoutError):
                await client.request("getState")
            assert await client.request("getState") == {"second": True}
            assert client.pending_count == 0
    finally:
        await server.stop()


async def test_a_reply_that_arrives_after_the_timeout_is_recorded_not_delivered():
    async def too_late(server: FakeQlcServer, ws, request: ReceivedRequest):
        async def emit():
            await asyncio.sleep(0.4)
            await server.send(
                fake_wire.ok_frame(request.req_id, request.verb, {"late": True})
            )

        server.spawn(emit())

    server = FakeQlcServer(handler=too_late, greeting=[])
    await server.start()
    try:
        async with QlcAgentClient(uri=server.uri, timeout=0.15) as client:
            with pytest.raises(QlcTimeoutError):
                await client.request("getState")
            await asyncio.sleep(0.6)
            assert client.pending_count == 0
            assert len(client.stray_replies) == 1
            assert client.stray_replies[0].payload() == {"late": True}
            # ...and the client still works afterwards
            assert client.connected
    finally:
        await server.stop()


# --------------------------------------------------------------------------- #
# bidirectional unicode / large payloads
# --------------------------------------------------------------------------- #
async def test_large_unicode_payloads_with_pipes_and_newlines_round_trip(client, server):
    name = (
        "Zeus | Front\n第三行\t🎛️ "
        + "x" * 150_000
        + " | \n end"
    )
    result_blob = "y" * 150_000 + " | reply\nline2 🕺"

    async def echo_big(server: FakeQlcServer, ws, request: ReceivedRequest):
        await server.send(
            fake_wire.ok_frame(
                request.req_id,
                request.verb,
                {"blob": result_blob, "len": len(result_blob), "echo": request.args},
            )
        )

    server.handler = echo_big
    out = await client.request("setFixtureName", {"id": 1, "name": name})

    # outbound survived
    assert server.requests[0].args == {"id": 1, "name": name}
    assert server.requests[0].field_count == 4
    # inbound survived
    assert out["blob"] == result_blob
    assert out["len"] == len(result_blob)
    assert out["echo"]["name"] == name


async def test_emoji_and_rtl_text_survive_both_ways(client, server):
    args = {"name": "אור | 🕺 | 舞台 | \\n literal backslash-n"}
    async def echo(server: FakeQlcServer, ws, request: ReceivedRequest):
        await server.send(
            fake_wire.ok_frame(request.req_id, request.verb, {"args": request.args})
        )

    server.handler = echo
    out = await client.request("setFixtureName", args)
    assert out["args"] == args
    # and the bytes on the wire really were UTF-8 inside base64
    payload = server.requests[0].payload_b64
    assert json.loads(base64.b64decode(payload).decode("utf-8")) == args


# --------------------------------------------------------------------------- #
# connection handling
# --------------------------------------------------------------------------- #
async def test_connecting_to_nothing_raises_connection_error_quickly():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]

    client = QlcAgentClient(uri=build_uri("127.0.0.1", free_port, "/qlcplusWS"), timeout=0.5)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(QlcConnectionError) as excinfo:
        await client.request("getState")
    assert loop.time() - started < 5.0
    assert "could not connect" in str(excinfo.value)
    assert client.last_error is not None


async def test_pending_requests_fail_when_the_server_disappears():
    server = FakeQlcServer(handler=FakeQlcServer.no_reply_handler, greeting=[])
    await server.start()
    try:
        client = QlcAgentClient(uri=server.uri, timeout=5.0)
        await client.connect()
        task = asyncio.create_task(client.request("getState"))
        await server.wait_for_requests(1)
        await server.stop()  # rip the socket out from under the request
        with pytest.raises(QlcConnectionError):
            await task
        assert client.pending_count == 0
        await client.close()
    finally:
        await server.stop()


async def test_close_is_idempotent_and_a_later_request_reconnects():
    server = FakeQlcServer(greeting=[])
    await server.start()
    try:
        client = QlcAgentClient(uri=server.uri, timeout=2.0)
        await client.connect()
        await client.close()
        await client.close()  # must not raise
        assert not client.connected
        # A later request re-opens the socket on demand rather than refusing:
        # QLC+ is often restarted while the MCP server keeps running.
        assert await client.request("getState") is not None
        assert server.connections == 2
        await client.close()
    finally:
        await server.stop()


async def test_the_client_reconnects_after_the_server_drops_the_socket():
    server = FakeQlcServer(greeting=[])
    await server.start()
    try:
        client = QlcAgentClient(uri=server.uri, timeout=2.0)
        assert await client.request("getState") is not None
        assert server.connections == 1

        # QLC+ goes away mid-session...
        await server.current.close()
        await asyncio.sleep(0.1)

        # ...and the next tool call brings the socket back.
        assert await client.request("getState") is not None
        assert server.connections == 2
        await client.close()
    finally:
        await server.stop()


async def test_lazy_connect_and_reuse_keeps_one_connection():
    server = FakeQlcServer(greeting=[])
    await server.start()
    try:
        client = QlcAgentClient(uri=server.uri, timeout=2.0)  # never connect()ed explicitly
        await client.request("getState")
        await client.request("getState")
        await client.request("getState")
        assert server.connections == 1, "the client should hold one socket open"
        await client.close()
    finally:
        await server.stop()


async def test_uri_comes_from_environment(monkeypatch):
    monkeypatch.setenv("QLC_HOST", "10.0.0.5")
    monkeypatch.setenv("QLC_PORT", "8123")
    monkeypatch.setenv("QLC_WS_PATH", "custom")
    monkeypatch.setenv("QLC_TIMEOUT", "3.5")
    monkeypatch.delenv("QLC_WS_URL", raising=False)

    client = QlcAgentClient()
    assert client.uri == "ws://10.0.0.5:8123/custom"
    assert client.timeout == 3.5


async def test_explicit_uri_beats_the_environment(monkeypatch):
    monkeypatch.setenv("QLC_HOST", "10.0.0.5")
    client = QlcAgentClient(uri="ws://127.0.0.1:1/qlcplusWS")
    assert client.uri == "ws://127.0.0.1:1/qlcplusWS"


async def test_qlc_enabled_zero_refuses_to_connect(monkeypatch):
    monkeypatch.setenv("QLC_ENABLED", "0")
    client = QlcAgentClient(uri="ws://127.0.0.1:1/qlcplusWS")
    with pytest.raises(QlcConnectionError) as excinfo:
        await client.request("getState")
    assert "disabled" in str(excinfo.value)


async def test_a_bad_timeout_environment_variable_is_reported(monkeypatch):
    monkeypatch.setenv("QLC_TIMEOUT", "soon")
    with pytest.raises(QlcProtocolError):
        QlcAgentClient()


# --------------------------------------------------------------------------- #
# convenience API used by the CLI and by tools
# --------------------------------------------------------------------------- #
async def test_call_verb_drops_nothing_supplied(client, server):
    await client.call_verb("addFixture", manufacturer="Robe", quantity=0, gap=None)
    assert server.requests[0].args == {"manufacturer": "Robe", "quantity": 0}
    assert server.requests[0].verb == "addFixture"


async def test_request_many_keeps_input_order(client, server):
    results = await client.request_many(
        [("getState", {}), ("getFunction", {"id": 2}), ("getWidget", {"id": 3})]
    )
    assert [r["verb"] for r in results] == ["getState", "getFunction", "getWidget"]
    assert server.verbs_seen == ["getState", "getFunction", "getWidget"]


async def test_request_sync_works_from_a_plain_script():
    """request_sync drives its own event loop, which is what cli.py relies on."""
    server = FakeQlcServer(greeting=[])
    await server.start()
    try:
        # Called from inside a running loop it would fail, so run it in a thread.
        result = await asyncio.to_thread(
            QlcAgentClient(uri=server.uri, timeout=2.0).request_sync, "getState", {}
        )
    finally:
        await server.stop()
    assert result["verb"] == "getState"
