import json
import socket
import struct
import tempfile
import time
from pathlib import Path

import palpal.debug_server as debug_server
from palpal.debug_server import (
    LIVE_HEADER,
    LIVE_MAGIC,
    LIVE_MAX_PAYLOAD,
    LIVE_MESSAGE_FRAME,
    LIVE_MESSAGE_FRAME_REPEAT,
    LIVE_MESSAGE_HELLO,
    LIVE_MESSAGE_STATE,
    LIVE_VERSION,
)
from tests.support import make_debug_state, make_state


def live_message(message_type, sequence, payload):
    return LIVE_HEADER.pack(
        LIVE_MAGIC,
        LIVE_VERSION,
        message_type,
        0,
        len(payload),
        sequence,
        sequence * 10,
    ) + payload


def indexed_frame(pixels=b"\x00\x01", frame_number=7):
    palette = b"\x00\x00\x00\xff\xff\x00\x00\xff"
    payload = palette + pixels
    return (
        b"PFRM"
        + struct.pack("<HHHH", 2, 2, 1, 2)
        + struct.pack("<I", 2)
        + struct.pack("<I", frame_number)
        + struct.pack("<I", len(payload))
        + payload
    )


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def short_socket_path(tmp_path):
    return Path(tempfile.gettempdir()) / f"palpal-{abs(hash(str(tmp_path))):x}.sock"


def test_live_bridge_receives_fragmented_state_and_indexed_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("PALPAL_AGENT_SOCKET", str(short_socket_path(tmp_path)))
    debug = make_debug_state(tmp_path)
    debug.start_live_bridge()
    assert wait_until(lambda: debug.live_transport_snapshot()["listening"])

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(debug.live_socket_path))
    hello = live_message(
        LIVE_MESSAGE_HELLO,
        1,
        json.dumps({"game_instance_id": "game-a"}).encode(),
    )
    state = live_message(
        LIVE_MESSAGE_STATE,
        2,
        json.dumps(make_state(frame=9)).encode(),
    )
    frame = live_message(LIVE_MESSAGE_FRAME, 3, indexed_frame())
    stream = hello + state + frame
    for offset in range(0, len(stream), 17):
        client.sendall(stream[offset : offset + 17])

    assert wait_until(lambda: debug.live_info("frame")["available"])
    assert debug.read_live_state()["frame"] == 9
    assert debug.live_transport_snapshot()["game_instance_id"] == "game-a"
    sequence, png = debug.latest_frame_png()
    assert sequence == 3
    assert png.startswith(b"\x89PNG\r\n\x1a\n")

    client.close()
    assert wait_until(lambda: not debug.live_transport_snapshot()["connected"])
    debug.stop_live_bridge()
    assert not debug.live_socket_path.exists()


def test_live_bridge_rejects_oversized_payload_and_new_instance_clears_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PALPAL_AGENT_SOCKET", str(short_socket_path(tmp_path)))
    debug = make_debug_state(tmp_path)
    debug.ingest_live_message(
        LIVE_MESSAGE_HELLO,
        1,
        0,
        json.dumps({"game_instance_id": "game-a"}).encode(),
    )
    debug.set_live_state(make_state())
    debug.set_live_frame(indexed_frame())
    debug.ingest_live_message(
        LIVE_MESSAGE_HELLO,
        2,
        0,
        json.dumps({"game_instance_id": "game-b"}).encode(),
    )
    assert not debug.live_info("state")["available"]
    assert not debug.live_info("frame")["available"]

    debug.start_live_bridge()
    assert wait_until(lambda: debug.live_transport_snapshot()["listening"])
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(debug.live_socket_path))
    client.sendall(
        LIVE_HEADER.pack(
            LIVE_MAGIC,
            LIVE_VERSION,
            LIVE_MESSAGE_STATE,
            0,
            LIVE_MAX_PAYLOAD + 1,
            3,
            0,
        )
    )
    assert wait_until(lambda: debug.live_transport_snapshot()["protocol_errors"] == 1)
    client.close()
    debug.stop_live_bridge()


def test_identical_frames_refresh_freshness_and_reuse_png(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    frame = indexed_frame()
    encode_calls = 0
    real_pfrm_to_png = debug_server.pfrm_to_png

    def count_encode(raw):
        nonlocal encode_calls
        encode_calls += 1
        return real_pfrm_to_png(raw)

    monkeypatch.setattr(debug_server, "pfrm_to_png", count_encode)
    debug.set_live_frame(frame, sequence=10, received_at=100.0)
    first_sequence, first_png = debug.latest_frame_png()
    debug.set_live_frame(indexed_frame(frame_number=8), sequence=11, received_at=200.0)
    second_sequence, second_png = debug.latest_frame_png()

    assert first_sequence == 10
    assert second_sequence == 11
    assert debug.live_info("frame")["received_at"] == 200.0
    assert debug.live_transport_snapshot()["frame_messages"] == 2
    assert second_png is first_png
    assert encode_calls == 1


def test_frame_repeat_refreshes_freshness_and_reuses_png(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    encode_calls = 0
    real_pfrm_to_png = debug_server.pfrm_to_png

    def count_encode(raw):
        nonlocal encode_calls
        encode_calls += 1
        return real_pfrm_to_png(raw)

    monkeypatch.setattr(debug_server, "pfrm_to_png", count_encode)
    debug.ingest_live_message(LIVE_MESSAGE_FRAME, 10, 0, indexed_frame())
    first_sequence, first_png = debug.latest_frame_png()
    debug.ingest_live_message(LIVE_MESSAGE_FRAME_REPEAT, 11, 0, b"")
    second_sequence, second_png = debug.latest_frame_png()

    assert first_sequence == 10
    assert second_sequence == 11
    assert debug.live_transport_snapshot()["frame_messages"] == 2
    assert second_png is first_png
    assert encode_calls == 1


def test_frame_repeat_requires_an_existing_full_frame(tmp_path):
    debug = make_debug_state(tmp_path)

    debug.ingest_live_message(LIVE_MESSAGE_FRAME_REPEAT, 1, 0, b"")

    assert debug.live_transport_snapshot()["protocol_errors"] == 1
    assert debug.live_transport_snapshot()["last_protocol_error"] == "frame_repeat_without_frame"


def test_changed_frames_reencode_and_slow_consumers_get_latest(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    encode_calls = 0
    real_pfrm_to_png = debug_server.pfrm_to_png

    def count_encode(raw):
        nonlocal encode_calls
        encode_calls += 1
        return real_pfrm_to_png(raw)

    monkeypatch.setattr(debug_server, "pfrm_to_png", count_encode)
    debug.set_live_frame(indexed_frame(), sequence=20)
    debug.latest_frame_png()
    debug.set_live_frame(indexed_frame(b"\x01\x00"), sequence=21)
    debug.set_live_frame(indexed_frame(b"\x01\x01"), sequence=22)

    assert debug.wait_for_frame(20, 0) == 22
    sequence, png = debug.latest_frame_png()
    assert sequence == 22
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert encode_calls == 2

    debug.set_live_frame(indexed_frame(), sequence=23)
    sequence, reused_png = debug.latest_frame_png()
    assert sequence == 23
    assert reused_png.startswith(b"\x89PNG\r\n\x1a\n")
    assert encode_calls == 2


def test_state_stream_payload_reuses_short_lived_snapshot(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    calls = 0

    def snapshot():
        nonlocal calls
        calls += 1
        return {"call": calls}

    monkeypatch.setattr(debug, "snapshot", snapshot)

    first = debug.state_stream_payload()
    second = debug.state_stream_payload()

    assert first == b'{"call": 1}'
    assert second is first
    assert calls == 1
