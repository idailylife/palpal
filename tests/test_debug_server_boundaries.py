import json
import os
import struct
import threading
import time
import urllib.error
import urllib.request

import pytest

from conftest import make_debug_state, make_state, write_state
from palpal.debug_server import Handler, Server


def http_request(base_url, path, method="GET", payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


@pytest.fixture
def http_server(tmp_path):
    debug = make_debug_state(tmp_path)
    write_state(debug, make_state(frame=7))
    debug.frame_path.write_bytes(
        b"PFRM"
        + struct.pack("<HHHH", 1, 1, 1, 1)
        + b"\x00" * 8
        + struct.pack("<I", 4)
        + b"\x00\x00\x00\xff"
    )
    server = Server(("127.0.0.1", 0), Handler)
    server.state = debug
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", debug
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_http_handler_serves_state_frame_action_and_404(http_server):
    base_url, debug = http_server

    status, headers, raw = http_request(base_url, "/state")
    payload = json.loads(raw)
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert payload["frame"] == 7
    assert payload["_debug"]["runtime_dir"] == str(debug.runtime_dir)

    status, headers, raw = http_request(base_url, "/frame.png")
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")

    status, _, _ = http_request(base_url, "/not-found")
    assert status == 404

    status, _, raw = http_request(base_url, "/action", method="POST", payload={"buttons": "search", "frames": 999})
    payload = json.loads(raw)
    assert status == 200
    assert payload["ok"] is True
    assert payload["action"]["frames"] == 300
    assert debug.action_path.read_text(encoding="ascii") == "frames 300\nbutton search\n"

    status, _, raw = http_request(base_url, "/action", method="POST", payload={"buttons": ["up"], "frames": 1})
    payload = json.loads(raw)
    assert status == 400
    assert payload["ok"] is False
    assert payload["error"] == "action_pending"

    debug.action_path.unlink()
    status, _, raw = http_request(base_url, "/action", method="POST", payload={"buttons": ["bad"], "frames": 1})
    payload = json.loads(raw)
    assert status == 400
    assert payload["ok"] is False
    assert "invalid buttons" in payload["error"]


def test_http_handler_serves_only_confirmed_current_room_transitions(http_server):
    base_url, debug = http_server
    target = {
        "id": 52, "x": 32, "y": 0, "sprite_id": 0, "state": 1,
        "trigger_mode": 4, "trigger_script": 4631, "auto_script": 0,
        "blocking": False, "interactable": True,
    }
    state = make_state(objects=[target], frame=8)
    destination = make_state(objects=[], frame=9)
    destination.update({"scene_id": 4, "map_id": 11, "room": {"key": "next-room"}})
    record = debug.record_transition_destination(
        debug.room_identity(state),
        52,
        debug.room_identity(destination),
        from_object=target,
        from_state=state,
        to_state=destination,
    )
    write_state(debug, state)

    status, _, raw = http_request(base_url, "/navigation/transitions")
    payload = json.loads(raw)
    assert status == 200
    assert payload["from_logical_room_id"] == debug.room_identity(state)["id"]
    assert [row["object_id"] for row in payload["transitions"]] == [52]
    assert payload["transitions"][0]["to_logical_room_id"] == debug.room_identity(destination)["id"]
    assert payload["transitions"][0]["to_logical_room_label"] == debug.room_identity(destination)["logical_room_label"]

    data = debug.read_exploration()
    data["transitions"][debug.room_identity(state)["nav_room_id"]][record["transition_key"]].pop("confirmation")
    debug.write_exploration(data)
    status, _, raw = http_request(base_url, "/navigation/transitions")
    payload = json.loads(raw)
    assert status == 200
    assert payload["transitions"] == []
    assert payload["known_transitions"] == {}
    assert payload["known_transitions_by_object"] == {}


def test_http_handler_deletes_checkpoints_and_reports_missing(http_server):
    base_url, debug = http_server
    checkpoint_dir = debug.checkpoint_dir("cp1")
    checkpoint_dir.mkdir(parents=True)
    (checkpoint_dir / "manifest.json").write_text('{"version":1}', encoding="utf-8")

    status, _, raw = http_request(base_url, "/checkpoints/cp1", method="DELETE")
    payload = json.loads(raw)
    assert status == 200
    assert payload == {"ok": True, "id": "cp1"}
    assert not checkpoint_dir.exists()

    status, _, raw = http_request(base_url, "/checkpoints/missing", method="DELETE")
    payload = json.loads(raw)
    assert status == 404
    assert payload["ok"] is False
    assert "checkpoint not found" in payload["error"]


def test_action_file_writers_validate_and_replace_results(tmp_path):
    debug = make_debug_state(tmp_path)

    with pytest.raises(ValueError, match="invalid buttons"):
        debug.write_action(["search", "bad"], 1)

    action = debug.write_action(["search", "up"], 999)
    assert action["frames"] == 300
    assert debug.action_path.read_text(encoding="ascii") == "frames 300\nbutton search\nbutton up\n"
    with pytest.raises(RuntimeError, match="action_pending"):
        debug.write_action(["down"], 1)

    debug.action_path.unlink()
    debug.action_result_path.write_text('{"ok": true}', encoding="utf-8")
    slot_action = debug.write_slot_action("save_slot", "12")
    assert slot_action["command"] == "save_slot"
    assert slot_action["slot"] == 12
    assert debug.action_path.read_text(encoding="ascii") == "save_slot 12\n"
    assert not debug.action_result_path.exists()

    with pytest.raises(ValueError, match="unsupported slot command"):
        debug.write_slot_action("delete_slot", 1)
    with pytest.raises(RuntimeError, match="action_pending"):
        debug.write_slot_action("load_slot", 12)


@pytest.mark.parametrize(
    ("state_update", "expected"),
    [
        ({"status": "waiting_for_game"}, "waiting_for_game"),
        ({"status": "startup_waiting"}, "startup_waiting"),
        ({"in_battle": True}, "in_battle"),
        ({"dialog": {"active": True}}, "in_dialog"),
        ({"entering_scene": True}, "entering_scene"),
        ({"in_main_game": False}, "not_in_main_game"),
    ],
)
def test_preflight_checkpoint_action_rejects_unsafe_states(tmp_path, state_update, expected):
    debug = make_debug_state(tmp_path)
    state = make_state()
    state.update(state_update)
    write_state(debug, state)

    with pytest.raises(RuntimeError, match=expected):
        debug.preflight_checkpoint_action(state)


def test_preflight_checkpoint_action_rejects_stale_and_pending_action(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()

    with pytest.raises(RuntimeError, match="state_stale"):
        debug.preflight_checkpoint_action(state)

    write_state(debug, state)
    old = time.time() - 10
    os.utime(debug.state_path, (old, old))
    with pytest.raises(RuntimeError, match="state_stale"):
        debug.preflight_checkpoint_action(state)

    write_state(debug, state)
    debug.action_path.write_text("frames 1\n", encoding="ascii")
    with pytest.raises(RuntimeError, match="action_pending"):
        debug.preflight_checkpoint_action(state)

    startup = {"status": "startup_waiting"}
    debug.action_path.unlink()
    write_state(debug, startup)
    debug.preflight_checkpoint_action(startup, allow_startup_waiting=True)


def active_dialog_state(text="hello"):
    state = make_state()
    state["dialog"] = {"active": True, "latest_event": {"text": text}}
    return state


def active_dialog_event_state(events, party=None):
    text = events[-1].get("text") if events else "hello"
    state = active_dialog_state(text)
    state["dialog"]["events"] = events
    if party is not None:
        state["party"] = party
    return state


def test_advance_dialog_writes_search_and_stops_when_dialog_ends(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    snapshots = [active_dialog_state(), make_state(), make_state(), make_state(), make_state()]
    written = []

    monkeypatch.setattr(debug, "snapshot", lambda: snapshots.pop(0))
    monkeypatch.setattr(debug, "write_action", lambda buttons, frames: written.append((buttons, frames)) or {})
    monkeypatch.setattr(debug, "wait_for_action_clear", lambda: True)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    result = debug.advance_dialog({"max_presses": 3, "delay_ms": 100})
    assert result["ok"] is True
    assert result["status"] == "dialog_inactive"
    assert result["presses"] == 1
    assert written == [(["search"], 1)]


def test_advance_dialog_continues_through_transient_inactive_gap(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    snapshots = [
        active_dialog_state("first"),
        make_state(),
        active_dialog_state("second"),
        make_state(),
        make_state(),
        make_state(),
        make_state(),
    ]
    written = []

    monkeypatch.setattr(debug, "snapshot", lambda: snapshots.pop(0))
    monkeypatch.setattr(debug, "write_action", lambda buttons, frames: written.append((buttons, frames)) or {})
    monkeypatch.setattr(debug, "wait_for_action_clear", lambda: True)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    result = debug.advance_dialog({"max_presses": 3, "delay_ms": 100})
    assert result["ok"] is True
    assert result["status"] == "dialog_inactive"
    assert result["presses"] == 2
    assert written == [(["search"], 1), (["search"], 1)]


def test_advance_dialog_reports_pending_timeout_and_cancel(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    monkeypatch.setattr(debug, "snapshot", lambda: active_dialog_state())
    monkeypatch.setattr(debug, "write_action", lambda buttons, frames: {})
    monkeypatch.setattr(debug, "wait_for_action_clear", lambda: False)

    result = debug.advance_dialog({"max_presses": 2})
    assert result["ok"] is False
    assert result["status"] == "action_pending_timeout"
    assert result["presses"] == 1

    cancel_root = tmp_path / "cancel"
    cancel_root.mkdir()
    cancelled = make_debug_state(cancel_root)
    monkeypatch.setattr(cancelled, "snapshot", lambda: active_dialog_state())
    monkeypatch.setattr(cancelled, "write_action", lambda buttons, frames: cancelled.dialog_advance_cancel.set() or {})
    monkeypatch.setattr(cancelled, "wait_for_action_clear", lambda: True)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    result = cancelled.advance_dialog({"max_presses": 2, "delay_ms": 100})
    assert result["ok"] is True
    assert result["status"] == "cancelled"
    assert result["presses"] == 1


def test_advance_dialog_stops_on_repeated_opponent_line(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    monk_a = {"sequence": 1, "frame": 10, "event_object_id": 57, "message_id": 588, "speaker_hint": "醉道士", "text": "好酒！"}
    li_b = {"sequence": 2, "frame": 11, "event_object_id": 57, "message_id": 589, "speaker_hint": "李逍遥", "text": "前辈，你醒醒。"}
    monk_a_again = {**monk_a, "sequence": 3, "frame": 12}
    snapshots = [
        active_dialog_event_state([monk_a]),
        active_dialog_event_state([monk_a, li_b]),
        active_dialog_event_state([monk_a, li_b, monk_a_again]),
    ]
    written = []

    monkeypatch.setattr(debug, "snapshot", lambda: snapshots.pop(0))
    monkeypatch.setattr(debug, "write_action", lambda buttons, frames: written.append((buttons, frames)) or {})
    monkeypatch.setattr(debug, "wait_for_action_clear", lambda: True)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    result = debug.advance_dialog({"max_presses": 2, "stop_on_repeat": True})
    assert result["ok"] is True
    assert result["status"] == "repeated_dialog_opponent_line"
    assert result["presses"] == 2
    assert written == [(["search"], 1), (["search"], 1)]


def test_advance_dialog_does_not_stop_on_repeated_party_line(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    first = {"sequence": 1, "frame": 10, "event_object_id": 57, "message_id": 589, "speaker_hint": "李逍遥", "text": "我知道了。"}
    second = {**first, "sequence": 2, "frame": 11}
    third = {**first, "sequence": 3, "frame": 12}
    snapshots = [
        active_dialog_event_state([first]),
        active_dialog_event_state([first, second]),
        active_dialog_event_state([first, second, third]),
    ]
    written = []

    monkeypatch.setattr(debug, "snapshot", lambda: snapshots.pop(0))
    monkeypatch.setattr(debug, "write_action", lambda buttons, frames: written.append((buttons, frames)) or {})
    monkeypatch.setattr(debug, "wait_for_action_clear", lambda: True)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    result = debug.advance_dialog({"max_presses": 2, "delay_ms": 100, "stop_on_repeat": True})
    assert result["ok"] is True
    assert result["status"] == "max_presses"
    assert result["presses"] == 2
    assert written == [(["search"], 1), (["search"], 1)]


def test_advance_dialog_does_not_stop_on_repeated_system_line(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    first = {"sequence": 1, "frame": 10, "event_object_id": 57, "message_id": 631, "speaker_hint": "系统", "text": "得到５００文钱"}
    second = {**first, "sequence": 2, "frame": 11}
    third = {**first, "sequence": 3, "frame": 12}
    snapshots = [
        active_dialog_event_state([first]),
        active_dialog_event_state([first, second]),
        active_dialog_event_state([first, second, third]),
    ]
    written = []

    monkeypatch.setattr(debug, "snapshot", lambda: snapshots.pop(0))
    monkeypatch.setattr(debug, "write_action", lambda buttons, frames: written.append((buttons, frames)) or {})
    monkeypatch.setattr(debug, "wait_for_action_clear", lambda: True)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    result = debug.advance_dialog({"max_presses": 2, "delay_ms": 100, "stop_on_repeat": True})
    assert result["ok"] is True
    assert result["status"] == "max_presses"
    assert result["presses"] == 2
    assert written == [(["search"], 1), (["search"], 1)]


def test_advance_dialog_stops_when_active_line_does_not_change(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    snapshots = [active_dialog_state("same line") for _ in range(3)]
    written = []

    monkeypatch.setattr(debug, "snapshot", lambda: snapshots.pop(0))
    monkeypatch.setattr(debug, "write_action", lambda buttons, frames: written.append((buttons, frames)) or {})
    monkeypatch.setattr(debug, "wait_for_action_clear", lambda: True)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    result = debug.advance_dialog({"max_presses": 100, "delay_ms": 100, "stop_on_repeat": True})
    assert result["ok"] is True
    assert result["status"] == "repeated_dialog"
    assert result["presses"] == 2
    assert written == [(["search"], 1), (["search"], 1)]


def test_dialog_advance_marker_identity_and_repeat_helpers(tmp_path):
    debug = make_debug_state(tmp_path)
    marker_entry = {"time": "1.5", "source_key": "a", "source_keys": ["b", "a"]}
    marker = debug.dialog_advance_marker_from_entry(marker_entry)
    assert marker == {"time": 1.5, "source_keys": ["a", "b"]}
    assert debug.dialog_advance_marker_from_entry(None) is None
    assert debug.dialog_advance_entry_matches_marker({"source_key": "b"}, marker)
    assert not debug.dialog_advance_entry_matches_marker({"source_key": "c"}, marker)

    debug.dialog_history = [
        {"time": 1.0, "source_key": "a", "scene_id": 3, "map_id": 10, "speaker_hint": "李大娘", "event_object_id": 7, "text": "old"},
        {"time": 2.0, "source_key": "b", "scene_id": 3, "map_id": 10, "speaker_hint": "李大娘", "event_object_id": 7, "text": "hello"},
        {"time": 3.0, "source_key": "c", "scene_id": 3, "map_id": 10, "speaker_hint": "李大娘", "event_object_id": 7, "text": "hello"},
    ]
    state = active_dialog_state("hello")
    initial = debug.dialog_advance_initial_marker(state)
    assert initial["source_keys"] == ["b"]
    assert [entry["source_key"] for entry in debug.dialog_advance_recent_dialogs(initial)] == ["c"]
    assert debug.dialog_advance_identity(debug.dialog_history[-1]) == "object:3:10::7"
    assert debug.dialog_advance_identity({"speaker_hint": "系统"}) is None
    assert debug.dialog_advance_identity({"scene_id": 3, "map_id": 10, "room_key": "inn", "speaker_hint": "李大娘"}) == "speaker:3:10:inn:李大娘"
    assert not debug.dialog_advance_reached_repeated_npc_line(initial)
    assert debug.dialog_advance_reached_repeated_npc_line({"source_keys": ["a"]})


def test_agent_argument_json_and_action_normalization_boundaries(tmp_path):
    debug = make_debug_state(tmp_path)

    assert debug.parse_agent_tool_arguments({"function": {"name": "tool"}}) == {}
    assert debug.parse_agent_tool_arguments({"function": {"name": "tool", "arguments": ""}}) == {}
    assert debug.parse_agent_tool_arguments({"function": {"name": "tool", "arguments": {"limit": 2}}}) == {"limit": 2}
    with pytest.raises(RuntimeError, match="invalid JSON"):
        debug.parse_agent_tool_arguments({"function": {"name": "tool", "arguments": "{"}})
    with pytest.raises(RuntimeError, match="JSON object"):
        debug.parse_agent_tool_arguments({"function": {"name": "tool", "arguments": "[]"}})
    with pytest.raises(RuntimeError, match="were not JSON"):
        debug.parse_agent_tool_arguments({"function": {"name": "tool", "arguments": 3}})

    assert debug.parse_model_json('```json\n{"ok": true}\n```') == {"ok": True}
    assert debug.parse_model_json('{"ok": false}') == {"ok": False}
    with pytest.raises(json.JSONDecodeError):
        debug.parse_model_json("{bad")
    with pytest.raises(ValueError, match="JSON object"):
        debug.parse_model_json("[]")

    assert debug.normalize_action_args("press_buttons", {"buttons": ["A", "escape", "bad"], "frames": 999}) == {
        "buttons": ["search", "menu"],
        "frames": 300,
    }
    assert debug.normalize_action_args("press_buttons", {"buttons": [], "frames": "bad"}) == {
        "buttons": ["search"],
        "frames": 1,
    }
    assert debug.normalize_action_args("navigation_step", {"target_id": "42"}) == {"id": 42}
    assert debug.normalize_action_args("navigation_step", {"id": "bad"}) == {"id": 0}
    assert debug.normalize_action_args("unknown", {"x": 1}) == {"x": 1}


def test_validate_agent_decision_ignores_non_navigation_and_rejects_stale_target(tmp_path):
    debug = make_debug_state(tmp_path)
    decision = {"recommended_action": {"tool": "no_op", "args": {}}, "recommended_intent": "wait_runtime"}
    assert debug.validate_agent_decision(decision) is decision

    write_state(debug, make_state(objects=[]))
    stale = {
        "recommended_intent": "inspect_target",
        "recommended_action": {"tool": "navigation_step", "args": {"id": 99}, "dry_run": True},
        "evidence": {},
        "risk": "Low.",
    }
    result = debug.validate_agent_decision(stale)
    assert result["recommended_intent"] == "wait_runtime"
    assert result["recommended_action"]["tool"] == "no_op"
    assert result["recommended_action"]["args"]["reason"] == "navigation_target_invalid"
    assert result["evidence"]["validation_target_id"] == 99
    assert result["evidence"]["validation_route_status"] == "unknown_target"
