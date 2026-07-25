import json
from unittest.mock import patch

from tests.support import fake_load_command, fake_save_command, make_debug_state, make_state, write_state


def search_object(object_id, *, state=1, blocking=False, trigger_script=8149):
    return {
        "id": object_id,
        "x": 16 if object_id == 74 else 32,
        "y": 8 if object_id == 74 else 16,
        "sprite_id": object_id,
        "state": state,
        "trigger_mode": 1,
        "trigger_script": trigger_script,
        "auto_script": 5000,
        "blocking": blocking,
        "interactable": True,
    }


def test_animation_noise_does_not_displace_persistent_cleared_history(tmp_path):
    debug = make_debug_state(tmp_path)
    left = search_object(74)
    closed_right = search_object(75, state=2, blocking=True)
    opened_right = search_object(75)
    closed = make_state(objects=[left, closed_right], frame=10)
    opened = make_state(objects=[left, opened_right], frame=11)

    debug.record_state_events(closed)
    debug.record_state_events(opened)
    assert debug.event_log[-1]["type"] == "object_changed"

    for frame in range(12, 262):
        animated = make_state(objects=[left, {**opened_right, "auto_script": 5000 + frame}], frame=frame)
        debug.record_state_events(animated)
    assert len(debug.event_log) == 1

    debug.event_log = []
    debug.write_events()
    returned = make_state(objects=[left, opened_right], frame=300)
    returned["region_key"] = "3:10:99:99"
    navigation = debug.run_agent_tool("get_navigation", {}, returned)["navigation"]
    targets = {target["id"]: target for target in navigation["targets"]}
    assert targets[74]["interaction_state"] == "previously_cleared"
    assert targets[75]["interaction_state"] == "previously_cleared"

    history = debug.run_agent_tool("inspect_target", {"id": 75}, returned)
    assert history["interaction_summary"]["cleared_blocker"] is True
    assert any(item["type"] == "object_changed" for item in history["history"])


def test_old_animation_only_events_are_removed_on_read_and_rewritten(tmp_path):
    debug = make_debug_state(tmp_path)
    animation_events = [
        {
            "type": "object_changed",
            "object_id": 63,
            "frame": frame,
            "changes": {"auto_script": {"from": 5014, "to": 5015}},
        }
        for frame in range(250)
    ]
    semantic = {
        "type": "object_changed",
        "object_id": 75,
        "frame": 300,
        "changes": {"state": {"from": 2, "to": 1}},
    }
    debug.events_path.write_text(json.dumps(animation_events + [semantic]), encoding="utf-8")

    reloaded = make_debug_state(tmp_path)
    assert reloaded.event_log == [semantic]
    assert json.loads(reloaded.events_path.read_text(encoding="utf-8")) == [semantic]


def test_persistent_attempts_are_room_scoped_and_reopen_after_material_change(tmp_path):
    debug = make_debug_state(tmp_path)
    target = search_object(75)
    state = make_state(objects=[target], frame=20)
    snapshot = debug.object_snapshot_for_interaction(state, 75)
    debug.append_event(
        state,
        "interaction_no_effect",
        {
            "object_id": 75,
            "target_id": 75,
            "started_object": snapshot,
            "finished_object": snapshot,
            "reason": "no_state_change",
        },
    )
    assert debug.target_affordance(state, 75)["interaction_state"] == "previously_tried"

    changed = make_state(objects=[search_object(75, trigger_script=9000)], frame=30)
    assert debug.target_affordance(changed, 75)["interaction_state"] == "fresh"

    other_room = make_state(objects=[target], frame=30)
    other_room["scene_id"] = 4
    assert debug.target_affordance(other_room, 75)["interaction_state"] == "fresh"


def test_object_ledger_keeps_summary_and_only_twenty_details(tmp_path):
    debug = make_debug_state(tmp_path)
    target = search_object(75)
    state = make_state(objects=[target], frame=1)
    snapshot = debug.object_snapshot_for_interaction(state, 75)
    for frame in range(1, 26):
        state["frame"] = frame
        debug.append_event(
            state,
            "interaction_no_effect",
            {
                "object_id": 75,
                "started_frame": frame,
                "started_object": snapshot,
                "finished_object": snapshot,
                "reason": "no_state_change",
            },
        )
    entry = debug.persistent_object_entry(state, 75)
    assert entry["attempt_count"] == 25
    assert entry["no_effect_count"] == 25
    assert len(entry["history"]) == 20


def test_structured_tool_log_recovery_is_idempotent_and_ignores_model_text(tmp_path):
    debug = make_debug_state(tmp_path)
    left = search_object(74)
    right = search_object(75)
    state = make_state(objects=[left, right], frame=30)
    identity = debug.room_identity(state)
    right_snapshot = debug.object_snapshot_for_interaction(state, 75)
    left_snapshot = debug.object_snapshot_for_interaction(state, 74)
    common = {
        "scene_id": identity["scene_id"],
        "map_id": identity["map_id"],
        "logical_room_id": identity["id"],
        "room_id": identity["id"],
        "room_key": identity["room_key"],
        "nav_room_id": identity["nav_room_id"],
    }
    events = [
        {
            **common,
            "type": "object_changed",
            "time": 1.0,
            "frame": 20,
            "object_id": 75,
            "changes": {
                "state": {"from": 2, "to": 1},
                "blocking": {"from": True, "to": False},
            },
            "finished_object": right_snapshot,
        },
        {
            **common,
            "type": "interaction_no_effect",
            "time": 2.0,
            "frame": 21,
            "object_id": 75,
            "started_object": right_snapshot,
            "finished_object": right_snapshot,
        },
        {
            **common,
            "type": "interaction_no_effect",
            "time": 3.0,
            "frame": 22,
            "object_id": 74,
            "started_object": left_snapshot,
            "finished_object": left_snapshot,
        },
    ]
    log_entry = {
        "state_context": common,
        "request": {
            "messages": [
                {"role": "assistant", "content": "#99 was definitely opened"},
                {
                    "role": "tool",
                    "name": "get_navigation",
                    "content": json.dumps({"navigation": {"targets": [{"recent_interaction_outcome": event} for event in events]}}),
                },
            ]
        },
    }
    debug.agent_llm_log_path.write_text(json.dumps(log_entry) + "\n", encoding="utf-8")

    assert debug.recover_object_interactions_from_agent_llm_log() == 3
    assert debug.recover_object_interactions_from_agent_llm_log() == 0
    assert debug.persistent_object_entry(state, 75)["attempt_count"] == 1
    assert debug.persistent_object_entry(state, 74)["attempt_count"] == 1
    assert debug.persistent_object_entry(state, 99) is None

    navigation = debug.run_agent_tool("get_navigation", {}, state)["navigation"]
    targets = {target["id"]: target for target in navigation["targets"]}
    assert targets[74]["interaction_state"] == "previously_cleared"
    assert targets[75]["interaction_state"] == "previously_cleared"


def test_object_ledger_survives_checkpoint_round_trip_and_old_files_are_compatible(tmp_path):
    debug = make_debug_state(tmp_path)
    target = search_object(75)
    state = make_state(objects=[target], frame=20)
    write_state(debug, state)
    snapshot = debug.object_snapshot_for_interaction(state, 75)
    debug.append_event(
        state,
        "interaction_no_effect",
        {
            "object_id": 75,
            "started_object": snapshot,
            "finished_object": snapshot,
        },
    )
    with patch.object(debug, "run_slot_command", side_effect=fake_save_command(debug)):
        debug.save_checkpoint("ledger", "Ledger")

    debug.write_exploration(debug.empty_exploration())
    with patch.object(debug, "run_slot_command", side_effect=fake_load_command):
        debug.load_checkpoint("ledger")
    assert debug.persistent_object_entry(state, 75)["attempt_count"] == 1

    debug.exploration_path.write_text(
        json.dumps({"version": 6, "rooms": {}, "transitions": {}}),
        encoding="utf-8",
    )
    old = debug.read_exploration()
    assert old["object_interactions"] == {}
