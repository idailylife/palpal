import json
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import pytest

from tests.support import create_agent_task, make_debug_state, make_state, write_state
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


def open_websocket(base_url, path):
    parsed = urlsplit(base_url)
    connection = socket.create_connection((parsed.hostname, parsed.port), timeout=1.0)
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {parsed.hostname}:{parsed.port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    connection.sendall(request.encode("ascii"))
    response = b""
    while b"\r\n\r\n" not in response:
        response += connection.recv(4096)
    assert response.startswith(b"HTTP/1.0 101") or response.startswith(b"HTTP/1.1 101")
    return connection


def receive_websocket_json(connection):
    header = connection.recv(2)
    if not header:
        return None
    opcode = header[0] & 0x0F
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", connection.recv(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", connection.recv(8))[0]
    payload = b""
    while len(payload) < length:
        payload += connection.recv(length - len(payload))
    if opcode == 0x8:
        return None
    assert opcode == 0x1
    return json.loads(payload)


@pytest.fixture
def http_server(tmp_path):
    debug = make_debug_state(tmp_path)
    write_state(debug, make_state(frame=7))
    debug.set_live_frame(
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

    status, _, raw = http_request(base_url, "/navigation/status")
    lightweight = json.loads(raw)
    assert status == 200
    assert lightweight["frame"] == 7
    assert lightweight["sequence"] == debug.live_state_sequence
    assert "_exploration" not in lightweight
    assert "_debug" not in lightweight

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

    state = make_state(frame=8)
    state["party"] = [{"role_id": 0}]
    state["inventory"] = [{
        "item_id": 88, "amount": 1, "amount_in_use": 0,
        "usable": True, "apply_to_all": False,
    }]
    write_state(debug, state)
    debug.action_result_path.write_text('{"ok":true,"action":"use_item"}', encoding="utf-8")
    status, _, raw = http_request(
        base_url,
        "/inventory/use",
        method="POST",
        payload={"item_id": 88, "role_id": 0},
    )
    payload = json.loads(raw)
    assert status == 200
    assert payload["ok"] is True
    assert debug.action_path.read_text(encoding="ascii") == "use_item 88 0\n"
    assert not debug.action_result_path.exists()


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
    assert payload["transitions"][0]["traversal_context"]["total_count"] == 1
    assert "destination_priority" not in payload["transitions"][0]
    assert "complete" not in payload["transitions"][0]["destination_exploration"]

    data = debug.read_exploration()
    data["transitions"][debug.room_identity(state)["nav_room_id"]][record["transition_key"]].pop("confirmation")
    debug.write_exploration(data)
    status, _, raw = http_request(base_url, "/navigation/transitions")
    payload = json.loads(raw)
    assert status == 200
    assert payload["transitions"] == []
    assert payload["known_transitions"] == {}
    assert payload["known_transitions_by_object"] == {}


def test_http_handler_exposes_confirmed_intra_room_transition_endpoints(http_server):
    base_url, debug = http_server
    portal_47 = {
        "id": 47, "x": 48, "y": 8, "sprite_id": 0, "state": 1,
        "trigger_mode": 4, "trigger_script": 4701, "auto_script": 0,
        "blocking": False, "interactable": True,
    }
    portal_48 = {**portal_47, "id": 48, "x": 32, "y": 0, "trigger_script": 4801}
    state = make_state(objects=[portal_47, portal_48], frame=8)
    identity = debug.room_identity(state)
    edge_47 = debug.record_transition_destination(
        identity, 47, identity, from_object=portal_47, from_state=state, to_state=state,
        confirmation="native_trigger", intra_room_transition=True,
        native_destination={"x": 32, "y": 0},
    )
    edge_48 = debug.record_transition_destination(
        identity, 48, identity, from_object=portal_48, from_state=state, to_state=state,
        confirmation="native_trigger", intra_room_transition=True,
        native_destination={"x": 48, "y": 8},
    )
    write_state(debug, state)

    status, _, raw = http_request(base_url, "/navigation/transitions")
    payload = json.loads(raw)
    by_object = {row["object_id"]: row for row in payload["transitions"]}

    assert status == 200
    assert by_object[47]["source_endpoint"]["transition_key"] == edge_47["transition_key"]
    assert by_object[47]["destination_endpoint"] == {
        "kind": "confirmed_transition_source",
        "transition_key": edge_48["transition_key"],
        "object_id": 48,
        "position": {"x": 32, "y": 0},
    }
    assert by_object[48]["destination_endpoint"]["object_id"] == 47
    assert b"from_location_id" not in raw
    assert b"to_subregion_id" not in raw


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


def test_http_handler_agent_session_api_and_running_action_boundary(http_server):
    base_url, debug = http_server
    debug.agent_runtime.decision_provider = lambda state, model, feedback: {
        "summary": "pause for test",
        "recommended_intent": "wait_runtime",
        "recommended_action": {"tool": "no_op", "args": {}, "dry_run": True},
        "evidence": {},
        "risk": "none",
        "alternatives": [],
    }

    status, _, raw = http_request(base_url, "/agent/session")
    payload = json.loads(raw)
    assert status == 200
    assert "goal" not in payload["session"]

    status, _, raw = http_request(
        base_url,
        "/agent/session/decide",
        method="POST",
        payload={"model": "deepseek-v4-flash"},
    )
    payload = json.loads(raw)
    assert status == 202
    deadline = time.monotonic() + 2
    while debug.agent_runtime.is_running() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert debug.agent_runtime.snapshot()["stop_reason"] == "awaiting_manual_execute"

    status, _, raw = http_request(base_url, "/agent/session/events?after=0&limit=20")
    payload = json.loads(raw)
    assert status == 200
    assert any(event["type"] == "decision" for event in payload["events"])

    status, _, raw = http_request(base_url, "/agent/session/events?turn_limit=5")
    payload = json.loads(raw)
    assert status == 200
    assert payload["session_id"] == debug.agent_runtime.snapshot()["session_id"]
    assert payload["oldest_turn"] == 1
    assert payload["has_more"] is False
    assert {event["turn"] for event in payload["events"]} == {0, 1}

    debug.agent_runtime.state.update(status="running", phase="deciding")
    status, _, raw = http_request(
        base_url,
        "/action",
        method="POST",
        payload={"buttons": ["search"], "frames": 1},
    )
    payload = json.loads(raw)
    assert status == 409
    assert payload["code"] == "agent_session_running"
    assert not debug.action_path.exists()
    debug.agent_runtime.state.update(status="paused", phase="paused")

    status, _, raw = http_request(base_url, "/checkpoints/missing", method="DELETE")
    payload = json.loads(raw)
    assert status == 404
    assert payload["ok"] is False
    assert "checkpoint not found" in payload["error"]


def test_agent_event_websocket_is_fixed_to_handshake_session(http_server):
    base_url, debug = http_server
    runtime = debug.agent_runtime
    old_session_id = runtime.snapshot()["session_id"]
    cursor = runtime.emit("session", "paused", "old session ready")["seq"]
    old_connection = open_websocket(
        base_url,
        f"/agent/session/stream?after={cursor}&session_id={old_session_id}",
    )
    try:
        runtime.reset("checkpoint_restored")
        old_connection.settimeout(1.0)
        assert receive_websocket_json(old_connection) is None
    finally:
        old_connection.close()

    new_session_id = runtime.snapshot()["session_id"]
    new_cursor = runtime.snapshot()["last_event_seq"]
    new_connection = open_websocket(
        base_url,
        f"/agent/session/stream?after={new_cursor}&session_id={new_session_id}",
    )
    try:
        emitted = runtime.emit("tool", "completed", "new session event")
        received = receive_websocket_json(new_connection)
        assert received["seq"] == emitted["seq"]
        assert received["session_id"] == new_session_id
    finally:
        new_connection.close()


def test_http_handler_exposes_agent_tasks(http_server):
    base_url, debug = http_server
    task = create_agent_task(debug, {
        "id": "inspect-courtyard",
        "description": "Inspect the courtyard",
        "rationale": "The latest dialog points there.",
        "priority": 6,
    })["task"]

    status, _, raw = http_request(base_url, "/agent/tasks")
    payload = json.loads(raw)

    assert status == 200
    assert payload["ok"] is True
    assert payload["counts"] == {
        "active": 1, "dormant": 0, "completed": 0, "abandoned": 0, "total": 1,
    }
    assert payload["tasks"] == [{
        "id": "inspect-courtyard",
        "description": "Inspect the courtyard",
        "priority": 6,
        "status": "active",
    }]


def test_http_handler_exposes_current_room_memo_read_only(http_server):
    base_url, debug = http_server
    state = debug.read_live_state()
    debug.attach_logical_room_fields(state)
    room_id = debug.room_identity(state)["id"]
    created = debug.run_agent_tool(
        "upsert_room_memo",
        {"room_id": room_id, "text": "A quiet room above the inn."},
        state,
    )
    recent_room_ids = [f"S1/M1:{index:012X}" for index in range(1, 7)]
    for index, recent_room_id in enumerate(recent_room_ids, start=1):
        debug.run_agent_tool(
            "upsert_room_memo",
            {"room_id": recent_room_id, "text": f"Recent memo {index}."},
            state,
        )
    data = debug.read_exploration()
    for index, recent_room_id in enumerate(recent_room_ids, start=1):
        data["room_memos"][recent_room_id]["created_at"] = float(index)
    debug.write_exploration(data)

    status, _, raw = http_request(base_url, "/agent/room-memo")
    payload = json.loads(raw)

    assert status == 200
    assert payload["ok"] is True
    assert payload["room_id"] == room_id
    assert payload["memo"] == created["memo"]
    assert [memo["room_id"] for memo in payload["recent_memos"]] == list(reversed(recent_room_ids[1:]))
    assert [memo["text"] for memo in payload["recent_memos"]] == [
        "Recent memo 6.", "Recent memo 5.", "Recent memo 4.", "Recent memo 3.", "Recent memo 2.",
    ]


def test_http_handler_exposes_only_current_battle_memo(http_server):
    base_url, debug = http_server

    status, _, raw = http_request(base_url, "/agent/battle-memo")
    assert status == 200
    assert json.loads(raw) == {"ok": False, "status": "not_in_battle"}

    state = make_state(frame=8)
    state.update({
        "game_instance_id": "game-a",
        "in_battle": True,
        "battle": {
            "generation": 3,
            "phase": "command",
            "players": [],
            "enemies": [{"slot_index": 0, "present": True, "object_id": 81, "name": "灯笼", "hp": 999}],
            "available_actions": [],
            "visible_outcomes": [],
        },
    })
    write_state(debug, state)
    status, _, raw = http_request(base_url, "/agent/battle-memo")
    payload = json.loads(raw)
    assert status == 200
    assert payload["ok"] is True
    assert payload["is_boss"] is False
    assert payload["current_enemies"] == [{"slot_index": 0, "object_id": 81, "name": "灯笼"}]
    assert payload["enemy_memories"][0]["hp_estimate"] == {"status": "unknown"}
    assert "signature" not in payload
    assert '"hp": 999' not in raw.decode("utf-8")
    assert "max_hp" not in raw.decode("utf-8")

    debug.companion_owner_path.write_text(json.dumps({"session_id": "different"}), encoding="utf-8")
    status, _, raw = http_request(base_url, "/agent/battle-memo")
    assert status == 200
    assert json.loads(raw)["error"] == "companion_owner_mismatch"


def test_http_handler_exposes_structured_shop_actions(http_server):
    base_url, debug = http_server
    calls = []
    debug.write_shop_action = lambda action, item_id=None: (
        calls.append((action, item_id)) or {"command": action, "item_id": item_id}
    )

    status, _, raw = http_request(
        base_url,
        "/shop/buy",
        method="POST",
        payload={"item_id": 201},
    )
    assert status == 200
    assert json.loads(raw)["action"]["command"] == "shop_buy"

    status, _, raw = http_request(
        base_url,
        "/shop/sell",
        method="POST",
        payload={"item_id": 166},
    )
    assert status == 200
    assert json.loads(raw)["action"]["command"] == "shop_sell"

    status, _, raw = http_request(
        base_url,
        "/shop/leave",
        method="POST",
        payload={},
    )
    assert status == 200
    assert json.loads(raw)["action"]["command"] == "shop_leave"
    assert calls == [("shop_buy", 201), ("shop_sell", 166), ("shop_leave", None)]


def test_http_handler_exposes_structured_battle_action(http_server):
    base_url, debug = http_server
    calls = []
    debug.write_battle_action = lambda args: (
        calls.append(args) or {"command": "battle_action", **args}
    )

    status, _, raw = http_request(
        base_url,
        "/battle/action",
        method="POST",
        payload={
            "battle_generation": 3,
            "command_generation": 6,
            "action_type": "attack",
            "object_id": 0,
            "target_index": 0,
        },
    )
    payload = json.loads(raw)

    assert status == 200
    assert payload["ok"] is True
    assert calls == [{
        "battle_generation": 3,
        "command_generation": 6,
        "action_type": "attack",
        "object_id": 0,
        "target_index": 0,
    }]


def test_action_file_writers_validate_and_replace_results(tmp_path):
    debug = make_debug_state(tmp_path)
    write_state(debug, make_state())

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


def test_native_action_handshake_closes_consumed_file_race(tmp_path):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "game_instance_id": "game-a",
        "agent_action": {
            "generation": 7,
            "pending": False,
            "remaining_steps": 0,
            "waiting_for_move": False,
        },
    }
    write_state(debug, state)

    action = debug.write_action(["up", "up"], 1)
    assert action["game_instance_id"] == "game-a"
    assert action["action_generation_before"] == 7
    assert action["action_handshake_supported"] is True

    debug.action_path.unlink()
    assert debug.action_pending() is True
    with pytest.raises(RuntimeError, match="action_pending"):
        debug.write_action(["down"], 1)

    state["agent_action"] = {
        "generation": 8,
        "pending": True,
        "remaining_steps": 2,
        "waiting_for_move": True,
    }
    write_state(debug, state)
    assert debug.pending_written_action is None
    assert debug.action_pending() is True
    assert debug.snapshot()["_debug"]["action_pending"] is True
    waiting = debug.navigation_step(state, 1)
    assert waiting["ok"] is True
    assert waiting["status"] == "waiting_for_control"
    assert waiting["control"]["phase"] == "action_pending"

    state["agent_action"] = {
        "generation": 8,
        "pending": False,
        "remaining_steps": 0,
        "waiting_for_move": False,
    }
    write_state(debug, state)
    assert debug.action_pending() is False


@pytest.mark.parametrize(
    ("updates", "phase", "owner", "allowed"),
    [
        ({"in_battle": True, "entering_scene": True}, "battle", "game", []),
        ({"entering_scene": True}, "entering_scene", "game", []),
        ({"dialog": {"active": True}}, "dialog", "client", ["advance_dialog"]),
        (
            {"choice": {"active": True, "generation": 3}},
            "choice",
            "client",
            ["choice_select", "press_buttons"],
        ),
        (
            {"shop": {"active": True, "mode": "buy"}},
            "shop",
            "client",
            ["shop_buy", "shop_leave", "press_buttons"],
        ),
        (
            {"shop": {"active": True, "mode": "sell"}},
            "shop",
            "client",
            ["shop_sell", "shop_leave", "press_buttons"],
        ),
    ],
)
def test_control_state_priority_and_allowed_actions(
    tmp_path, updates, phase, owner, allowed,
):
    debug = make_debug_state(tmp_path)
    state = make_state()
    state.update(updates)
    state["trigger_observation"] = {
        "generation": 4,
        "event_object_id": 52,
        "trigger_script": 4631,
        "completed": False,
    }
    write_state(debug, state)

    control = debug.snapshot()["control"]

    assert control["phase"] == phase
    assert control["owner"] == owner
    assert control["allowed_actions"] == allowed
    assert control["navigation_ready"] is False


@pytest.mark.parametrize("mode", ["buy", "sell"])
@pytest.mark.parametrize("phase", ["select", "confirm"])
@pytest.mark.parametrize("button", ["down", "search", "menu"])
def test_http_shop_accepts_manual_buttons_and_blocks_pending_actions(
    http_server, mode, phase, button,
):
    base_url, debug = http_server
    state = make_state()
    state["shop"] = {"active": True, "mode": mode, "phase": phase}
    state["trigger_observation"] = {"generation": 4, "completed": False}
    write_state(debug, state)

    status, _, raw = http_request(base_url, "/state")
    assert status == 200
    assert "press_buttons" in json.loads(raw)["control"]["allowed_actions"]

    status, _, raw = http_request(
        base_url, "/action", method="POST", payload={"buttons": [button], "frames": 1},
    )
    assert status == 200
    assert json.loads(raw)["ok"] is True
    assert debug.action_path.read_text(encoding="ascii") == f"frames 1\nbutton {button}\n"

    status, _, raw = http_request(
        base_url, "/action", method="POST", payload={"buttons": [button], "frames": 1},
    )
    assert status == 400
    assert json.loads(raw)["error"] == "action_pending"
    assert debug.action_path.read_text(encoding="ascii") == f"frames 1\nbutton {button}\n"


def test_battle_control_exposes_only_command_windows(tmp_path):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "in_battle": True,
        "battle": {
            "generation": 4,
            "command_generation": 7,
            "phase": "command",
            "current_player_index": 1,
            "available_actions": [],
        },
    }

    control = debug.control_state(state)
    assert control == {
        "owner": "client",
        "phase": "battle",
        "allowed_actions": ["battle_action", "press_buttons"],
        "navigation_ready": False,
        "battle_state_available": True,
    }

    state["battle"]["phase"] = "resolving"
    control = debug.control_state(state)
    assert control["owner"] == "game"
    assert control["allowed_actions"] == []


def test_battle_player_view_exposes_only_human_visible_facts(tmp_path):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "in_battle": True,
        "battle": {
            "generation": 4,
            "command_generation": 7,
            "phase": "command",
            "result": "ongoing",
            "is_boss": True,
            "current_player_index": 0,
            "awaiting_ack": False,
            "players": [{
                "index": 0, "role_id": 1, "name": "李逍遙",
                "hp": 80, "max_hp": 100, "mp": 22, "max_mp": 30,
                "level": 5, "attack_strength": 21, "magic_strength": 18,
                "defense": 17, "dexterity": 16, "flee_rate": 15,
                "poison_resistance": 9, "elemental_resistance": [1, 2, 3, 4, 5],
                "status": [0, 2], "time_meter": 48.0, "fighter_state": "command",
                "status_effects": [
                    {"status_id": 1, "key": "slow", "remaining_rounds": 2, "persistent": False},
                    {"status_id": 6, "key": "protect", "remaining_rounds": None, "persistent": True},
                ],
                "defending": False,
                "submitted_action": {"action_type": "pass", "object_id": 0},
                "poisons": [{"poison_id": 563, "script": 100, "name": "赤毒"}],
            }],
            "enemies": [{
                "slot_index": 0, "present": True, "object_id": 81, "enemy_id": 12,
                "name": "燈籠", "hp": 70, "max_hp": 100, "level": 8,
                "physical_resistance": 4, "elemental_resistance": [2, 3, 4, 5, 6],
                "magic_id": 321, "magic_rate": 7, "status": [0, 0],
                "poisons": [], "time_meter": 60.0, "fighter_state": "waiting",
            }],
            "available_actions": [{
                "action_type": "magic", "object_id": 327, "name": "炎咒",
                "target_side": "enemy", "target_mode": "single",
                "valid_target_indices": [0], "submission_target_indices": [0],
                "mp_cost": 8, "description": "灼熱火焰\n攻擊敵人",
                "base_damage": 70, "element": 4,
            }, {
                "action_type": "coop_magic", "object_id": 400, "name": "合擊",
                "target_side": "enemy", "target_mode": "all",
                "valid_target_indices": [], "submission_target_indices": [-1],
                "mp_cost": 99, "description": "全體合擊",
            }, {
                "action_type": "use_item", "object_id": 99, "name": "解毒草",
                "target_side": "player", "target_mode": "single",
                "valid_target_indices": [0], "submission_target_indices": [0],
                "description": "解除中毒",
            }],
        },
    }

    view = debug.player_battle_view(state)

    assert view["players"][0]["poisons"] == [{"name": "赤毒"}]
    assert view["players"][0]["status_effects"] == [
        {"status_id": 1, "key": "slow", "remaining_rounds": 2, "persistent": False},
        {"status_id": 6, "key": "protect", "remaining_rounds": None, "persistent": True},
    ]
    assert "poison_resistance" not in view["players"][0]
    assert "status" not in view["players"][0]
    assert view["enemies"] == [{
        "slot_index": 0, "present": True, "object_id": 81, "name": "燈籠",
    }]
    assert view["is_boss"] is True
    assert view["available_actions"][0]["mp_cost"] == 8
    assert view["available_actions"][0]["description"] == "灼熱火焰\n攻擊敵人"
    assert "base_damage" not in view["available_actions"][0]
    assert "element" not in view["available_actions"][0]
    assert "mp_cost" not in view["available_actions"][1]
    assert view["available_actions"][1]["description"] == "全體合擊"
    assert view["available_actions"][2]["description"] == "解除中毒"

    write_state(debug, state)
    snapshot = debug.snapshot()
    assert snapshot["battle"]["enemies"][0]["hp"] == 70
    assert snapshot["_debug"]["battle_player_view"] == view


def test_player_battle_item_effects_are_sanitized_for_every_supported_kind(tmp_path):
    debug = make_debug_state(tmp_path)
    item = {
        "slot": 3,
        "item_id": 127,
        "name": "忘魂花",
        "amount": 2,
        "amount_in_use": 0,
        "usable": True,
        "throwable": True,
        "use_effect": {
            "script_entry": 39404,
            "preview_complete": False,
            "requirements_complete": False,
            "effects": [
                {"kind": "restore_hp", "amount": 50, "apply_to_all": False, "script_entry": 1},
                {"kind": "restore_mp", "amount": 30, "apply_to_all": True, "script_entry": 2},
                {"kind": "restore_hp_mp", "amount": 20, "apply_to_all": False, "script_entry": 3},
                {"kind": "revive", "max_hp_percent": 10, "apply_to_all": False, "script_entry": 4},
                {"kind": "apply_poison", "poison_id": 551, "poison_name": "赤毒", "script_entry": 5},
                {"kind": "cure_poison", "poison_id": 551, "poison_name": "赤毒", "script_entry": 6},
                {"kind": "cure_poison_level", "max_poison_level": 3, "apply_to_all": True, "script_entry": 7},
                {"kind": "apply_status", "status_id": 2, "status": "sleep", "duration": 3, "script_entry": 8},
                {"kind": "remove_status", "status_id": 3, "status": "silence", "script_entry": 9},
                {"kind": "drain_hp", "amount": 80, "conditional": True, "script_entry": 10},
            ],
            "requirements": [{"kind": "face_event_object", "event_object_id": 42}],
            "unparsed_effects": [{"script_entry": 11, "operation": 66, "operands": [1, 2, 3]}],
        },
        "throw_effect": {
            "script_entry": 39407,
            "preview_complete": True,
            "effects": [{
                "kind": "enemy_damage", "amount": 90, "apply_to_all": False,
                "conditional": False, "script_entry": 12, "enemy_hp": 999,
            }],
        },
    }

    visible = debug.player_battle_inventory_item(item)

    assert visible["use_effect"]["source"] == "dos_script_preview"
    assert visible["use_effect"]["preview_complete"] is False
    assert [effect["kind"] for effect in visible["use_effect"]["effects"]] == [
        "restore_hp", "restore_mp", "restore_hp_mp", "revive", "apply_poison",
        "cure_poison", "cure_poison_level", "apply_status", "remove_status", "drain_hp",
    ]
    assert visible["use_effect"]["effects"][3]["max_hp_percent"] == 10
    assert visible["use_effect"]["effects"][4]["poison_name"] == "赤毒"
    assert visible["use_effect"]["effects"][7] == {
        "kind": "apply_status", "status": "sleep", "duration": 3,
    }
    assert visible["throw_effect"] == {
        "source": "dos_script_preview",
        "preview_complete": True,
        "effects": [{
            "kind": "enemy_damage", "conditional": False,
            "amount": 90, "apply_to_all": False,
        }],
    }
    serialized = json.dumps(visible, ensure_ascii=False)
    for hidden in (
        "script_entry", "requirements", "requirements_complete", "unparsed_effects",
        "operation", "operands", "event_object_id", "poison_id", "status_id", "enemy_hp",
    ):
        assert hidden not in serialized


def test_agent_state_views_expose_nonbattle_party_effects(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    state["party"] = [{
        "role_id": 1,
        "scene_sprite_id": 7,
        "level": 5,
        "hp": 80,
        "max_hp": 100,
        "mp": 22,
        "max_mp": 30,
        "status_effects": [
            {"status_id": 7, "key": "haste", "remaining_rounds": None, "persistent": True},
        ],
        "poisons": [{"poison_id": 563, "script": 100, "name": "赤毒"}],
    }]

    summary = debug.agent_state_summary(state)
    assert summary["party"][0]["status_effects"] == [
        {"status_id": 7, "key": "haste", "remaining_rounds": None, "persistent": True},
    ]
    assert summary["party"][0]["poisons"] == [{"name": "赤毒"}]

    context = debug.agent_context(state, {"targets": []}, {"dialogs": []}, [])
    assert context["state"]["party"][0]["status_effects"][0]["key"] == "haste"
    assert context["state"]["party"][0]["poisons"] == [{"name": "赤毒"}]


@pytest.mark.parametrize(
    ("interaction", "phase", "allowed"),
    [
        ({"dialog": {"active": True}}, "dialog", ["advance_dialog"]),
        (
            {"choice": {"active": True, "generation": 3}},
            "choice",
            ["choice_select", "press_buttons"],
        ),
    ],
)
def test_prebattle_interactions_are_client_controlled(
    tmp_path, interaction, phase, allowed,
):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "in_battle": True,
        "battle": {"generation": 4, "phase": "pre_battle"},
        **interaction,
    }

    control = debug.control_state(state)

    assert control["owner"] == "client"
    assert control["phase"] == phase
    assert control["allowed_actions"] == allowed
    assert control["battle_state_available"] is True


@pytest.mark.parametrize("phase", ["resolving", "reward"])
@pytest.mark.parametrize("interaction", ["dialog", "choice"])
def test_battle_story_interactions_are_client_controlled(tmp_path, phase, interaction):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(), "in_battle": True,
        "battle": {"generation": 9, "phase": phase},
        interaction: {"active": True, "visible": True, "held": False},
    }
    control = debug.control_state(state)
    assert control["owner"] == "client"
    assert control["phase"] == interaction
    assert "battle_action" not in control["allowed_actions"]


def test_terminated_prebattle_dialog_is_client_controlled(tmp_path):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "in_battle": True,
        "battle": {"generation": 4, "phase": "pre_battle", "result": "terminated"},
        "dialog": {"active": True},
    }

    control = debug.control_state(state)

    assert control["owner"] == "client"
    assert control["phase"] == "dialog"
    assert control["allowed_actions"] == ["advance_dialog"]


@pytest.mark.parametrize("phase", ["command", "resolving", "reward"])
def test_dialog_does_not_take_control_in_normal_battle_phases(tmp_path, phase):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "in_battle": True,
        "battle": {"generation": 4, "phase": phase},
        "dialog": {"active": True},
    }

    control = debug.control_state(state)

    assert control["phase"] == "battle"
    assert "advance_dialog" not in control["allowed_actions"]


def test_old_battle_state_rejects_controls_as_unavailable(tmp_path):
    debug = make_debug_state(tmp_path)
    state = {**make_state(), "in_battle": True}

    assert debug.action_control_error(state, "press_buttons") == "battle_state_unavailable"


def test_battle_action_writer_injects_current_player_and_generations(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "game_instance_id": "battle-game",
        "in_battle": True,
        "battle": {
            "generation": 8,
            "command_generation": 13,
            "phase": "command",
            "current_player_index": 1,
            "available_actions": [{
                "action_type": "attack",
                "object_id": 0,
                "target_mode": "single",
                "valid_target_indices": [0, 2],
            }],
        },
    }
    write_state(debug, state)
    monkeypatch.setattr(debug, "wait_for_battle_action_result", lambda *args, **kwargs: {
        "ok": True,
        "action": "battle_action",
        "status": "submitted",
    })

    result = debug.write_battle_action({
        "battle_generation": 8,
        "command_generation": 13,
        "action_type": "attack",
        "object_id": 0,
        "target_index": 2,
    })

    assert result["player_index"] == 1
    assert debug.action_path.read_text(encoding="ascii") == (
        "battle_action 8 13 1 attack 0 2\n"
    )
    with pytest.raises(RuntimeError, match="action_pending"):
        debug.write_battle_action({
            "battle_generation": 8,
            "command_generation": 13,
            "action_type": "attack",
            "object_id": 0,
            "target_index": 1,
        })


def test_pure_trigger_script_blocks_server_actions_and_exposes_metadata(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    state["trigger_observation"] = {
        "generation": 9,
        "event_object_id": 57,
        "trigger_script": 100,
        "completed": False,
    }
    write_state(debug, state)

    control = debug.snapshot()["control"]

    assert control == {
        "owner": "game",
        "phase": "game_script",
        "allowed_actions": [],
        "navigation_ready": False,
        "trigger_generation": 9,
        "event_object_id": 57,
    }
    with pytest.raises(RuntimeError, match="game_controlled"):
        debug.write_action(["up"], 1)
    assert debug.use_item_validation_error(state, {"item_id": 1, "role_id": 0}) == "game_controlled"
    assert debug.equip_item_validation_error(state, {"item_id": 1, "role_id": 0}) == "game_controlled"


def test_missing_native_action_uses_legacy_file_compatibility(tmp_path):
    debug = make_debug_state(tmp_path)
    write_state(debug, {**make_state(), "game_instance_id": "old-game"})

    action = debug.write_action(["search"], 1)

    assert action["action_handshake_supported"] is False
    assert debug.pending_written_action is None
    assert debug.action_pending() is True
    debug.action_path.unlink()
    assert debug.action_pending() is False


def test_native_pending_blocks_item_and_equipment_validation_without_action_file(tmp_path):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "game_instance_id": "game-a",
        "agent_action": {
            "generation": 12,
            "pending": True,
            "remaining_steps": 1,
            "waiting_for_move": True,
        },
    }
    write_state(debug, state)

    assert debug.use_item_validation_error(state, {"item_id": 1, "role_id": 0}) == "action_pending"
    assert debug.equip_item_validation_error(state, {"item_id": 1, "role_id": 0}) == "action_pending"


def test_shop_action_writer_uses_structured_native_command(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "cash": 600,
        "game_instance_id": "game-a",
        "agent_action": {
            "generation": 12,
            "pending": False,
            "remaining_steps": 0,
            "waiting_for_move": False,
        },
        "shop": {
            "active": True,
            "mode": "buy",
            "store_id": 7,
            "phase": "select",
            "selected_index": 0,
            "selected_item_id": 201,
            "items": [{
                "index": 0,
                "item_id": 201,
                "price": 400,
                "affordable": True,
            }],
        },
    }
    write_state(debug, state)
    monkeypatch.setattr(
        debug,
        "wait_for_shop_action_result",
        lambda action, item_id, start_time: {
            "ok": True,
            "status": "purchased",
            "action": action,
            "item_id": item_id,
            "store_id": 7,
        },
    )

    result = debug.write_shop_action("shop_buy", 201)

    assert debug.action_path.read_text(encoding="ascii") == "shop_buy 201\n"
    assert result["command"] == "shop_buy"
    assert result["action_generation_before"] == 12
    assert result["result"]["status"] == "purchased"


def test_write_shop_sell_action_uses_structured_command(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "game_instance_id": "game-a",
        "agent_action": {
            "generation": 12,
            "pending": False,
            "remaining_steps": 0,
            "waiting_for_move": False,
        },
        "shop": {
            "active": True,
            "mode": "sell",
            "phase": "select",
            "items": [{
                "item_id": 166,
                "sell_price": 100,
                "available_count": 1,
                "can_sell": True,
            }],
        },
    }
    write_state(debug, state)
    monkeypatch.setattr(
        debug,
        "wait_for_shop_action_result",
        lambda action, item_id, start_time: {
            "ok": True,
            "status": "sold",
            "action": action,
            "item_id": item_id,
        },
    )

    result = debug.write_shop_action("shop_sell", 166)

    assert debug.action_path.read_text(encoding="ascii") == "shop_sell 166\n"
    assert result["command"] == "shop_sell"
    assert result["result"]["status"] == "sold"


def test_shop_validation_blocks_other_inventory_actions(tmp_path):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "cash": 600,
        "shop": {
            "active": True,
            "mode": "buy",
            "store_id": 7,
            "phase": "select",
            "items": [{"item_id": 201, "price": 400, "affordable": True}],
        },
        "party": [{"role_id": 0, "equipment": []}],
        "inventory": [{
            "item_id": 201,
            "amount": 1,
            "amount_in_use": 0,
            "usable": True,
            "apply_to_all": False,
            "equipable": True,
            "equipable_role_ids": [0],
            "equipment_effect": {"slot_id": 0},
        }],
    }
    write_state(debug, state)

    assert debug.shop_action_validation_error(state, "shop_buy", {"item_id": 201}) is None
    assert debug.shop_action_validation_error(state, "shop_leave", {}) is None
    assert debug.use_item_validation_error(state, {"item_id": 201, "role_id": 0}) == "in_shop"
    assert debug.equip_item_validation_error(state, {"item_id": 201, "role_id": 0}) == "in_shop"


def test_sell_shop_validation_requires_sellable_available_item(tmp_path):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "shop": {
            "active": True,
            "mode": "sell",
            "phase": "select",
            "items": [
                {"item_id": 166, "available_count": 2, "can_sell": True},
                {"item_id": 201, "available_count": 0, "can_sell": False},
            ],
        },
    }
    write_state(debug, state)

    assert debug.shop_action_validation_error(state, "shop_sell", {"item_id": 166}) is None
    assert debug.shop_action_validation_error(state, "shop_sell", {"item_id": 201}) == "item_not_sellable"
    assert debug.shop_action_validation_error(state, "shop_buy", {"item_id": 166}) == "shop_mode_mismatch"
    assert debug.shop_action_validation_error(state, "shop_leave", {}) is None


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
    debug.set_live_state(state, received_at=old)
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


def visible_dialog_state(text="hello"):
    state = active_dialog_state(text)
    state["dialog"].update({"visible": True, "held": False})
    return state


def held_dialog_state(text="hello"):
    state = active_dialog_state(text)
    state["dialog"].update({"visible": False, "held": True})
    return state


def active_dialog_event_state(events, party=None):
    text = events[-1].get("text") if events else "hello"
    state = active_dialog_state(text)
    state["dialog"]["events"] = events
    if party is not None:
        state["party"] = party
    return state


def run_dialog_scenario(
    debug,
    monkeypatch,
    snapshots,
    options,
    *,
    wait_for_action_clear=True,
):
    written = []
    if callable(snapshots):
        snapshot = snapshots
    else:
        snapshots = iter(snapshots)
        snapshot = lambda: next(snapshots)
    monkeypatch.setattr(debug, "snapshot", snapshot)
    monkeypatch.setattr(
        debug,
        "write_action",
        lambda buttons, frames, **_kwargs: written.append((buttons, frames)) or {},
    )
    monkeypatch.setattr(
        debug,
        "wait_for_action_clear",
        lambda: wait_for_action_clear,
    )
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    return debug.advance_dialog(options), written


def test_advance_dialog_writes_search_and_stops_when_dialog_ends(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    snapshots = [active_dialog_state(), make_state(), make_state(), make_state(), make_state()]
    result, written = run_dialog_scenario(
        debug, monkeypatch, snapshots, {"max_presses": 3, "delay_ms": 100},
    )
    assert result["ok"] is True
    assert result["status"] == "dialog_inactive"
    assert result["presses"] == 1
    assert written == [(["search"], 1)]


def test_advance_dialog_allows_prebattle_and_stops_at_native_termination(
    tmp_path, monkeypatch,
):
    debug = make_debug_state(tmp_path)
    active = active_dialog_state("你真的要交手嗎？")
    active.update({
        "in_battle": True,
        "battle": {"generation": 8, "phase": "pre_battle", "result": "terminated"},
    })
    ended = {
        **make_state(),
        "last_battle": {
            "generation": 8,
            "result": "terminated",
            "players": [],
            "enemies": [{"object_id": 611, "name": "苗人"}],
        },
    }

    result, written = run_dialog_scenario(
        debug,
        monkeypatch,
        [active, ended, ended, ended, ended],
        {"max_presses": 3, "delay_ms": 100},
    )

    assert result["ok"] is True
    assert result["status"] == "dialog_inactive"
    assert written == [(["search"], 1)]


@pytest.mark.parametrize("initial_phase", ["pre_battle", "resolving", "reward"])
@pytest.mark.parametrize("next_phase", ["command", "resolving", "reward"])
def test_advance_dialog_hands_off_when_battle_story_ends(tmp_path, monkeypatch, initial_phase, next_phase):
    debug = make_debug_state(tmp_path)
    active = visible_dialog_state("准备战斗")
    active.update({
        "in_battle": True,
        "battle": {"generation": 8, "phase": initial_phase},
    })
    command = {
        **make_state(),
        "in_battle": True,
        "battle": {"generation": 8, "phase": next_phase},
    }

    result, written = run_dialog_scenario(
        debug,
        monkeypatch,
        [active, command, command],
        {"max_presses": 3, "delay_ms": 100},
    )

    assert result["ok"] is True
    assert result["status"] == "battle_started"
    assert written == [(["search"], 1)]


def test_advance_dialog_rechecks_battle_state_without_repeating_the_press(
    tmp_path, monkeypatch,
):
    debug = make_debug_state(tmp_path)
    active = active_dialog_state("准备战斗")
    active.update({
        "in_battle": True,
        "battle": {"generation": 8, "phase": "pre_battle"},
    })
    command = {
        **make_state(),
        "in_battle": True,
        "battle": {"generation": 8, "phase": "command"},
    }

    result, written = run_dialog_scenario(
        debug,
        monkeypatch,
        [active, command, active, command, command],
        {"max_presses": 3, "delay_ms": 100},
    )

    assert result["ok"] is True
    assert result["status"] == "battle_started"
    assert result["presses"] == 2
    assert written == [(["search"], 1), (["search"], 1)]


def test_advance_dialog_hands_off_from_normal_dialog_through_prebattle(
    tmp_path, monkeypatch,
):
    debug = make_debug_state(tmp_path)
    ordinary = active_dialog_state("你是什么人？")
    prebattle = active_dialog_state("少废话，看招！")
    prebattle.update({
        "in_battle": True,
        "battle": {"generation": 8, "phase": "pre_battle"},
    })
    command = {
        **make_state(),
        "in_battle": True,
        "battle": {"generation": 8, "phase": "command"},
    }

    result, written = run_dialog_scenario(
        debug,
        monkeypatch,
        [ordinary, prebattle, command, command],
        {"max_presses": 3, "delay_ms": 100},
    )

    assert result["ok"] is True
    assert result["status"] == "battle_started"
    assert result["presses"] == 2
    assert written == [(["search"], 1), (["search"], 1)]


def test_advance_dialog_still_rejects_dialog_during_command_battle(
    tmp_path, monkeypatch,
):
    debug = make_debug_state(tmp_path)
    state = active_dialog_state()
    state.update({
        "in_battle": True,
        "battle": {"generation": 8, "phase": "command"},
    })

    result, written = run_dialog_scenario(
        debug,
        monkeypatch,
        [state, state],
        {"max_presses": 3, "delay_ms": 100},
    )

    assert result["ok"] is False
    assert result["status"] == "in_battle"
    assert written == []


def test_advance_dialog_does_not_handoff_to_a_different_battle_generation(
    tmp_path, monkeypatch,
):
    debug = make_debug_state(tmp_path)
    active = active_dialog_state("准备战斗")
    active.update({
        "in_battle": True,
        "battle": {"generation": 8, "phase": "pre_battle"},
    })
    changed = {
        **make_state(),
        "in_battle": True,
        "battle": {"generation": 9, "phase": "command"},
    }

    result, written = run_dialog_scenario(
        debug,
        monkeypatch,
        [active, changed, changed],
        {"max_presses": 3, "delay_ms": 100},
    )

    assert result["ok"] is False
    assert result["status"] == "in_battle"
    assert written == [(["search"], 1)]


def test_advance_dialog_stops_at_structured_choice_without_pressing(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    choice = {
        **make_state(),
        "dialog": {"active": False},
        "choice": {
            "active": True,
            "generation": 4,
            "selected_index": 0,
            "options": [{"index": 0, "text": "否"}, {"index": 1, "text": "是"}],
        },
    }
    result, written = run_dialog_scenario(
        debug, monkeypatch, [choice], {"max_presses": 3, "delay_ms": 100},
    )
    assert result["ok"] is True
    assert result["status"] == "choice_active"
    assert result["presses"] == 0
    assert written == []


def test_advance_dialog_detects_choice_that_appears_after_press(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    choice = {
        **make_state(),
        "dialog": {"active": False},
        "choice": {
            "active": True,
            "generation": 5,
            "selected_index": 0,
            "options": [{"index": 0, "text": "否"}, {"index": 1, "text": "是"}],
        },
    }
    result, written = run_dialog_scenario(
        debug,
        monkeypatch,
        [active_dialog_state("要聽故事嗎？"), choice],
        {"max_presses": 3, "delay_ms": 100},
    )
    assert result["ok"] is True
    assert result["status"] == "choice_active"
    assert result["presses"] == 1
    assert written == [(["search"], 1)]


def test_choice_writer_uses_generation_and_waits_for_native_result(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "game_instance_id": "choice-game",
        "choice": {
            "active": True,
            "generation": 7,
            "selected_index": 0,
            "command_pending": False,
            "prompt": {"text": "要聽故事嗎？"},
            "options": [
                {"index": 0, "semantic": "no", "text": "否"},
                {"index": 1, "semantic": "yes", "text": "是"},
            ],
        },
    }
    write_state(debug, state)
    monkeypatch.setattr(debug, "wait_for_choice_action_result", lambda generation, start_time: {
        "ok": True,
        "action": "choice_select",
        "generation": generation,
        "requested_index": 1,
        "selected_index": 1,
        "status": "selected",
    })

    result = debug.write_choice_action(7, 1)

    assert debug.action_path.read_text(encoding="ascii") == "choice_select 7 1\n"
    assert result["option"]["semantic"] == "yes"
    assert result["result"]["status"] == "selected"


@pytest.mark.parametrize(
    ("generation", "option_index", "pending", "expected"),
    [
        (6, 1, False, "stale_choice_command"),
        (7, 2, False, "invalid_choice_option"),
        (7, 1, True, "choice_action_pending"),
    ],
)
def test_choice_writer_rejects_invalid_or_pending_commands(
    tmp_path, generation, option_index, pending, expected,
):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "choice": {
            "active": True,
            "generation": 7,
            "selected_index": 0,
            "command_pending": pending,
            "options": [{"index": 0, "text": "否"}, {"index": 1, "text": "是"}],
        },
    }
    write_state(debug, state)

    with pytest.raises(RuntimeError, match=expected):
        debug.write_choice_action(generation, option_index)

    assert not debug.action_path.exists()


def test_choice_writer_propagates_native_selection_mismatch(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "choice": {
            "active": True,
            "generation": 7,
            "selected_index": 0,
            "command_pending": False,
            "options": [{"index": 0, "text": "否"}, {"index": 1, "text": "是"}],
        },
    }
    write_state(debug, state)
    monkeypatch.setattr(debug, "wait_for_choice_action_result", lambda *_args: {
        "ok": False,
        "action": "choice_select",
        "generation": 7,
        "requested_index": 1,
        "selected_index": 0,
        "status": "choice_selection_mismatch",
        "error": "choice_selection_mismatch",
    })

    with pytest.raises(RuntimeError, match="choice_selection_mismatch"):
        debug.write_choice_action(7, 1)


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
    result, written = run_dialog_scenario(
        debug, monkeypatch, snapshots, {"max_presses": 3, "delay_ms": 100},
    )
    assert result["ok"] is True
    assert result["status"] == "dialog_inactive"
    assert result["presses"] == 2
    assert written == [(["search"], 1), (["search"], 1)]


def test_advance_dialog_waits_through_held_state_until_inactive(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    snapshots = [
        visible_dialog_state("苗人头领"),
        held_dialog_state("苗人头领"),
        make_state(),
        make_state(),
        make_state(),
        make_state(),
    ]
    result, written = run_dialog_scenario(
        debug, monkeypatch, snapshots, {"max_presses": 3, "delay_ms": 100},
    )
    assert result["ok"] is True
    assert result["status"] == "dialog_inactive"
    assert result["presses"] == 1
    assert written == [(["search"], 1)]


def test_advance_dialog_waits_for_next_visible_page_before_pressing(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    snapshots = [
        visible_dialog_state("first"),
        held_dialog_state("first"),
        visible_dialog_state("second"),
        make_state(),
        make_state(),
        make_state(),
        make_state(),
    ]
    result, written = run_dialog_scenario(
        debug, monkeypatch, snapshots, {"max_presses": 3, "delay_ms": 100},
    )
    assert result["ok"] is True
    assert result["status"] == "dialog_inactive"
    assert result["presses"] == 2
    assert written == [(["search"], 1), (["search"], 1)]


def test_advance_dialog_starting_held_waits_without_pressing(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    snapshots = [held_dialog_state(), make_state(), make_state(), make_state(), make_state()]
    result, written = run_dialog_scenario(
        debug, monkeypatch, snapshots, {"max_presses": 3, "delay_ms": 100},
    )
    assert result["ok"] is True
    assert result["status"] == "dialog_inactive"
    assert result["presses"] == 0
    assert written == []


def test_advance_dialog_visibility_timeout_does_not_press(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    monkeypatch.setattr(debug, "read_live_state", lambda: held_dialog_state())
    monkeypatch.setattr(debug, "live_info", lambda _kind: {"stale": False})
    result, written = run_dialog_scenario(
        debug,
        monkeypatch,
        lambda: held_dialog_state(),
        {"max_presses": 3, "delay_ms": 100},
    )
    assert result["ok"] is False
    assert result["status"] == "dialog_visibility_timeout"
    assert result["presses"] == 0
    assert written == []


def test_background_snapshot_suspends_dialog_inactive_confirmation_near_deadline(
    tmp_path,
    monkeypatch,
):
    debug = make_debug_state(tmp_path)
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    observation_id = debug.observations.register(
        "dialog_gate",
        owner="dialog_advance",
        timeout=2,
        payload={"inactive_started": False, "inactive_polls": 0},
    )
    original_deadline = debug.observations.get(observation_id)["deadline"]
    inactive = make_state()

    now[0] = original_deadline - 0.01
    for _ in range(10):
        debug.observe_dialog_gates(inactive)
    request = debug.observations.get(observation_id)
    assert request["status"] == "pending"
    assert request["deadline"] is None
    assert request["payload"] == {"inactive_started": True, "inactive_polls": 0}

    # Room-entry processing may outlast the old visibility deadline.  The
    # inactive phase must remain available for its owner to confirm afterward.
    now[0] += 10
    assert debug.observations.get(observation_id)["status"] == "pending"

    for expected_polls in range(1, 3):
        debug.observe_dialog_gates(inactive, confirm_inactive=True)
        request = debug.observations.get(observation_id)
        assert request["status"] == "pending"
        assert request["payload"]["inactive_polls"] == expected_polls
    debug.observe_dialog_gates(inactive, confirm_inactive=True)
    assert debug.observations.get(observation_id)["status"] == "completed"
    completed = debug.observations.consume(observation_id, owner="dialog_advance")
    assert completed["result"]["status"] == "dialog_inactive"


def test_dialog_visibility_timeout_reconciles_completed_transition_without_pressing(
    tmp_path,
    monkeypatch,
):
    debug = make_debug_state(tmp_path)
    destination = {
        **make_state(),
        "scene_id": 44,
        "map_id": 40,
        "frame": 783257,
        "entering_scene": False,
        "dialog": {"active": False, "visible": False, "held": False},
    }
    samples = iter([destination, destination, destination])
    monkeypatch.setattr(debug, "read_live_state", lambda: next(samples))
    monkeypatch.setattr(debug, "live_info", lambda _kind: {"stale": False})
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    diagnostics = []
    monkeypatch.setattr(
        debug,
        "append_navigation_diagnostic",
        lambda event_type, payload=None, state=None: diagnostics.append(
            (event_type, payload, state)
        ),
    )

    state, stop_status = debug.dialog_advance_reconcile_visibility_timeout(
        held_dialog_state("還磨菇什麼，走嘛！")
    )

    assert stop_status == "dialog_inactive"
    assert state["scene_id"] == 44
    assert state["map_id"] == 40
    assert diagnostics[0][0] == "dialog_visibility_timeout_recovered"
    assert diagnostics[0][1]["recovered_status"] == "dialog_inactive"
    assert len(diagnostics[0][1]["samples"]) == 3


def test_dialog_visibility_timeout_reconciliation_does_not_mask_held_dialog(
    tmp_path,
    monkeypatch,
):
    debug = make_debug_state(tmp_path)
    monkeypatch.setattr(debug, "read_live_state", lambda: held_dialog_state())
    monkeypatch.setattr(debug, "live_info", lambda _kind: {"stale": False})
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    diagnostics = []
    monkeypatch.setattr(
        debug,
        "append_navigation_diagnostic",
        lambda *args, **kwargs: diagnostics.append((args, kwargs)),
    )

    _state, stop_status = debug.dialog_advance_reconcile_visibility_timeout(
        held_dialog_state()
    )

    assert stop_status == "dialog_visibility_timeout"
    assert diagnostics == []


@pytest.mark.parametrize(
    ("recovered_state", "expected_status"),
    [
        (visible_dialog_state("next"), None),
        (
            {
                **make_state(),
                "choice": {"active": True, "generation": 7, "options": []},
            },
            "choice_active",
        ),
        (
            {
                **make_state(),
                "in_battle": True,
                "battle": {"generation": 8, "phase": "command"},
            },
            "in_battle",
        ),
    ],
)
def test_dialog_visibility_timeout_reconciliation_recovers_terminal_or_visible_state(
    tmp_path,
    monkeypatch,
    recovered_state,
    expected_status,
):
    debug = make_debug_state(tmp_path)
    monkeypatch.setattr(debug, "read_live_state", lambda: recovered_state)
    monkeypatch.setattr(debug, "live_info", lambda _kind: {"stale": False})
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    diagnostics = []
    monkeypatch.setattr(
        debug,
        "append_navigation_diagnostic",
        lambda event_type, payload=None, state=None: diagnostics.append(
            (event_type, payload, state)
        ),
    )

    state, stop_status = debug.dialog_advance_reconcile_visibility_timeout(
        held_dialog_state()
    )

    assert state is recovered_state
    assert stop_status == expected_status
    assert diagnostics[0][0] == "dialog_visibility_timeout_recovered"


def test_dialog_inactive_confirmation_resets_to_visibility_phase(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    observation_id = debug.observations.register(
        "dialog_gate",
        owner="dialog_advance",
        timeout=2,
        payload={"inactive_started": False, "inactive_polls": 0},
    )

    debug.observe_dialog_gates(make_state())
    debug.observe_dialog_gates(make_state(), confirm_inactive=True)
    assert debug.observations.get(observation_id)["payload"]["inactive_polls"] == 1

    now[0] = 101.0
    debug.observe_dialog_gates(held_dialog_state())
    request = debug.observations.get(observation_id)
    assert request["status"] == "pending"
    assert request["deadline"] == pytest.approx(103.0)
    assert request["payload"] == {"inactive_started": False, "inactive_polls": 0}

    debug.observe_dialog_gates(visible_dialog_state("next"))
    completed = debug.observations.consume(observation_id, owner="dialog_advance")
    assert completed["status"] == "completed"
    assert completed["result"]["status"] == "ready"
    assert completed["result"]["state"]["dialog"]["latest_event"]["text"] == "next"


def test_advance_dialog_reports_pending_timeout_and_cancel(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    monkeypatch.setattr(debug, "snapshot", lambda: active_dialog_state())
    monkeypatch.setattr(debug, "write_action", lambda buttons, frames, **_kwargs: {})
    monkeypatch.setattr(debug, "wait_for_action_clear", lambda: False)

    result = debug.advance_dialog({"max_presses": 2})
    assert result["ok"] is False
    assert result["status"] == "action_pending_timeout"
    assert result["presses"] == 1

    cancel_root = tmp_path / "cancel"
    cancel_root.mkdir()
    cancelled = make_debug_state(cancel_root)
    monkeypatch.setattr(cancelled, "snapshot", lambda: active_dialog_state())
    monkeypatch.setattr(
        cancelled,
        "write_action",
        lambda buttons, frames, **_kwargs: cancelled.dialog_advance_cancel.set() or {},
    )
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
    result, written = run_dialog_scenario(
        debug,
        monkeypatch,
        snapshots,
        {"max_presses": 2, "stop_on_repeat": True},
    )
    assert result["ok"] is True
    assert result["status"] == "repeated_dialog_opponent_line"
    assert result["presses"] == 2
    assert written == [(["search"], 1), (["search"], 1)]


@pytest.mark.parametrize(
    ("speaker", "message_id", "text"),
    [
        ("李逍遥", 589, "我知道了。"),
        ("系统", 631, "得到５００文钱"),
    ],
    ids=["party", "system"],
)
def test_advance_dialog_does_not_stop_on_repeated_non_opponent_line(
    tmp_path, monkeypatch, speaker, message_id, text,
):
    debug = make_debug_state(tmp_path)
    first = {
        "sequence": 1,
        "frame": 10,
        "event_object_id": 57,
        "message_id": message_id,
        "speaker_hint": speaker,
        "text": text,
    }
    second = {**first, "sequence": 2, "frame": 11}
    third = {**first, "sequence": 3, "frame": 12}
    snapshots = [
        active_dialog_event_state([first]),
        active_dialog_event_state([first, second]),
        active_dialog_event_state([first, second, third]),
    ]
    result, written = run_dialog_scenario(
        debug,
        monkeypatch,
        snapshots,
        {"max_presses": 2, "delay_ms": 100, "stop_on_repeat": True},
    )
    assert result["ok"] is True
    assert result["status"] == "max_presses"
    assert result["presses"] == 2
    assert written == [(["search"], 1), (["search"], 1)]


def test_advance_dialog_stops_when_active_line_does_not_change(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    snapshots = [active_dialog_state("same line") for _ in range(3)]
    result, written = run_dialog_scenario(
        debug,
        monkeypatch,
        snapshots,
        {"max_presses": 100, "delay_ms": 100, "stop_on_repeat": True},
    )
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
    assert result["validation"] == {
        "status": "rejected",
        "reason": "navigation_target_invalid",
        "target_id": 99,
        "route_status": "unknown_target",
        "route_ok": False,
    }
