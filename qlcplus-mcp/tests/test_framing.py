"""Pure framing tests: no sockets, no QLC+.

These pin the wire format itself, which is the part another engineer's C++ is
being written against right now.
"""

from __future__ import annotations

import base64
import json

import fake_wire
import pytest
from qlcplus_mcp import framing


# --------------------------------------------------------------------------- #
# requests
# --------------------------------------------------------------------------- #
def test_request_frame_matches_the_frozen_spec():
    frame = framing.build_request_frame("7", "getState", {})
    assert frame == "QLC+AGENT|7|getState|e30="
    # and the independent parser agrees
    parsed = fake_wire.parse_request(frame)
    assert parsed["req_id"] == "7"
    assert parsed["verb"] == "getState"
    assert parsed["args"] == {}


def test_int_req_id_is_rendered_as_a_string():
    assert framing.build_request_frame(12, "getState") == "QLC+AGENT|12|getState|e30="


def test_no_args_means_base64_of_empty_object():
    frame = framing.build_request_frame("1", "saveProject")
    assert frame.endswith("|" + base64.b64encode(b"{}").decode())
    assert framing.EMPTY_ARGS_B64 == "e30="


def test_payload_is_base64_of_utf8_json():
    args = {"manufacturer": "Showtec", "quantity": 4}
    frame = framing.build_request_frame("1", "addFixture", args)
    payload = frame.split("|")[3]
    assert json.loads(base64.b64decode(payload).decode("utf-8")) == args


def test_frame_has_exactly_three_delimiters_even_with_pipes_in_the_text():
    nasty = {"name": "left | right | middle", "caption": "a|b|c"}
    frame = framing.build_request_frame("3", "setFixtureName", nasty)
    assert frame.count("|") == 3
    assert frame.split("|")[3] == framing.encode_payload(nasty)
    assert fake_wire.parse_request(frame)["args"] == nasty


def test_newlines_and_unicode_do_not_split_or_mangle_the_frame():
    nasty = {
        "name": "Chase\nLine two\n\tTabbed | pipe",
        "emoji": "🎛️🕺",
        "cjk": "舞台灯一号",
        "rtl": "إضاءة",
    }
    frame = framing.build_request_frame("9", "setFixtureName", nasty)
    assert "\n" not in frame
    assert frame.count("|") == 3
    # Independent decoding of the payload field recovers exactly the object,
    # whichever encoder produced the JSON bytes.
    assert fake_wire.decode_payload(frame.split("|")[3]) == nasty
    assert framing.decode_payload(fake_wire.encode_payload(nasty)) == nasty
    assert framing.decode_payload(frame.split("|")[3]) == nasty


def test_large_payload_round_trips():
    huge = {"blob": "x" * 200_000, "marker": "end|of|it"}
    frame = framing.build_request_frame("1", "setFixtureName", huge)
    decoded = framing.decode_payload(frame.split("|")[3])
    assert decoded == huge
    assert len(decoded["blob"]) == 200_000


def test_payloads_are_interchangeable_with_an_independent_encoder():
    """Byte layout may differ (ours is compact); the JSON it carries may not."""
    objects = ({}, {"a": 1}, {"n": None, "b": True}, {"t": "héllo|there\n"}, [1, 2, 3])
    for obj in objects:
        # our encoder -> their decoder, and their encoder -> our decoder
        assert fake_wire.decode_payload(framing.encode_payload(obj)) == obj
        assert framing.decode_payload(fake_wire.encode_payload(obj)) == obj


def test_verb_may_not_contain_the_delimiter():
    with pytest.raises(framing.QlcProtocolError):
        framing.build_request_frame("1", "bad|verb", {})
    with pytest.raises(framing.QlcProtocolError):
        framing.build_request_frame("1", "", {})


def test_unserialisable_arguments_raise_a_clear_error():
    with pytest.raises(framing.QlcProtocolError) as excinfo:
        framing.build_request_frame("1", "getFunction", {"id": object()})
    assert "JSON" in str(excinfo.value)


def test_nan_is_rejected_rather_than_written_as_invalid_json():
    with pytest.raises(framing.QlcProtocolError):
        framing.encode_payload({"v": float("nan")})


# --------------------------------------------------------------------------- #
# decoding
# --------------------------------------------------------------------------- #
def test_decode_tolerates_whitespace_and_missing_padding():
    raw = fake_wire.encode_payload({"a": "ü"})
    assert framing.decode_payload(f"  {raw}\n") == {"a": "ü"}
    assert framing.decode_payload(raw.rstrip("=")) == {"a": "ü"}


def test_decode_accepts_the_url_safe_alphabet():
    raw = base64.urlsafe_b64encode(b'{"a":"b~c"}').decode()
    assert framing.decode_payload(raw) == {"a": "b~c"}


def test_empty_payload_is_an_empty_object():
    assert framing.decode_payload("") == {}
    assert framing.decode_payload("   ") == {}


def test_bad_base64_and_bad_json_raise_protocol_errors():
    with pytest.raises(framing.QlcProtocolError):
        framing.decode_payload("!!!! not base64 !!!!")
    with pytest.raises(framing.QlcProtocolError):
        framing.decode_payload(base64.b64encode(b"not json at all").decode())


# --------------------------------------------------------------------------- #
# replies, errors, events
# --------------------------------------------------------------------------- #
def test_parses_an_ok_reply():
    message = framing.parse_agent_message(fake_wire.ok_frame(5, "getState", {"fixtures": []}))
    assert isinstance(message, framing.AgentMessage)
    assert message.kind == "reply"
    assert message.verb == "getState"
    assert message.req_id == "5"
    assert message.payload() == {"fixtures": []}


def test_parses_an_error_reply_into_an_exception():
    message = framing.parse_agent_message(fake_wire.err_frame(5, "patchUniverse", "no such plugin"))
    assert message is not None and message.kind == "error"
    error = message.to_error()
    assert isinstance(error, framing.QlcAgentError)
    assert error.verb == "patchUniverse"
    assert error.req_id == "5"
    assert error.error == "no such plugin"
    assert "no such plugin" in str(error)


def test_parses_an_event():
    message = framing.parse_agent_message(fake_wire.event_frame("fixtureChanged", {"id": 3}))
    assert isinstance(message, framing.AgentEvent)
    assert message.name == "fixtureChanged"
    assert message.payload() == {"id": 3}


def test_error_reply_payload_may_carry_extra_detail():
    frame = framing.build_error_frame("2", "addFixture", "address 20 is already used")
    message = framing.parse_agent_message(frame)
    assert message.to_error().error == "address 20 is already used"


def test_error_reply_with_missing_error_key_still_produces_a_message():
    frame = "QLC+AGENT|getState|4|err|" + fake_wire.encode_payload({"detail": "boom"})
    message = framing.parse_agent_message(frame)
    assert message.kind == "error"
    assert message.to_error().error == "unspecified error"


# --------------------------------------------------------------------------- #
# other families and malformed frames
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("frame", fake_wire.NOISE_FRAMES)
def test_non_agent_frames_are_reported_as_not_ours(frame):
    assert framing.parse_agent_message(frame) is None


@pytest.mark.parametrize(
    "frame",
    [
        "QLC+API|getFunctionsList|1|Scene One|2|Scene Two",
        "QLC+CMD|BLACKOUT|",
        "QLC+IO|1|2|3",
        "VC_PAGE|2",
        "GM_VALUE|255",
        "QLC+WV|hello",
        "hello world",
        "QLC+AGENTISH|nope|1|ok|e30=",
    ],
)
def test_foreign_frames_never_look_like_agent_messages(frame):
    assert framing.parse_agent_message(frame) is None


@pytest.mark.parametrize(
    "frame",
    [
        "QLC+AGENT|getState",
        "QLC+AGENT|getState|1|ok",
        "QLC+AGENT|getState|1|maybe|e30=",
        "QLC+AGENT|too|short",
    ],
)
def test_malformed_agent_frames_raise_a_protocol_error(frame):
    with pytest.raises(framing.QlcProtocolError):
        framing.parse_agent_message(frame)


def test_message_family_names_known_and_unknown_frames():
    assert framing.message_family("QLC+API|getFunctionsList|") == "QLC+API"
    assert framing.message_family("GM_VALUE|1") == "GM_VALUE"
    assert framing.message_family("VC_PAGE|0") == "VC_PAGE"
    assert framing.message_family("QLC+AGENT|getState|1|ok|e30=") == "QLC+AGENT"
    assert framing.message_family("WAT|1") == "unknown"
    assert framing.message_family("") == "unknown"


def test_is_agent_frame():
    assert framing.is_agent_frame("QLC+AGENT|1|getState|e30=")
    assert framing.is_agent_frame("QLC+AGENT")
    assert not framing.is_agent_frame("QLC+API|getFunctionsList|")
    assert not framing.is_agent_frame("QLC+AGENTISH|1|2|3|4")


def test_split_frames_handles_batching_and_blank_lines():
    assert framing.split_frames("QLC+AGENT|1|getState|e30=") == ["QLC+AGENT|1|getState|e30="]
    batched = "QLC+API|a|\n\nQLC+AGENT|1|getState|e30=\nGM_VALUE|3"
    assert framing.split_frames(batched) == [
        "QLC+API|a|",
        "QLC+AGENT|1|getState|e30=",
        "GM_VALUE|3",
    ]
    assert framing.split_frames("") == []
    assert framing.split_frames("\n\n") == []
