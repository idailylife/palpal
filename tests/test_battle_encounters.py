import json
from unittest.mock import patch

import pytest

from tests.support import (
    fake_load_command,
    fake_save_command,
    make_debug_state,
    make_state,
    make_touch_object,
    write_state,
)


def encounter_summary(debug, state, *, object_id=57, trigger_script=60,
                      result="won", is_boss=False, enemies=None):
    return {
        "result": result,
        "is_boss": is_boss,
        "enemies": enemies or ["绿叶妖精"],
        "trigger_context": {
            "logical_room_id": debug.room_identity(state)["id"],
            "event_object_id": object_id,
            "trigger_script": trigger_script,
        },
    }


def only_encounter(debug):
    rooms = debug.read_exploration()["battle_reappearances"]
    objects = next(iter(rooms.values()))
    scripts = next(iter(objects.values()))
    return next(iter(scripts.values()))


def hidden_object(object_id=57, trigger_script=60):
    return {**make_touch_object(object_id), "trigger_script": trigger_script,
            "state": 0, "interactable": False, "vanish_time": 100}


def test_win_creates_pending_then_matching_reappearance_confirms(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[hidden_object()])
    summary = encounter_summary(debug, state)

    pending = debug.record_battle_reappearance_candidate(summary, state)
    assert pending["status"] == "awaiting_reappearance"
    assert debug.target_affordance(state, 57)["interaction_state"] == "fresh"

    visible = make_state(objects=[make_touch_object(57)])
    confirmed = debug.observe_battle_reappearances(visible)
    assert confirmed[0]["status"] == "confirmed"
    projected = debug.target_affordance(visible, 57)
    assert projected["interaction_state"] == "reappeared_battle_trigger"
    assert projected["agent_priority"] == "avoid_repeat"
    assert projected["battle_reappearance_history"]["battle_count"] == 1
    assert projected["battle_reappearance_history"]["reappearance_count"] == 1


@pytest.mark.parametrize("change", [
    {"result": "fled"},
    {"result": "lost"},
    {"result": "terminated"},
    {"trigger_context": None},
    {"trigger_context": {"logical_room_id": "bad", "event_object_id": 57, "trigger_script": 60}},
    {"trigger_context": {"logical_room_id": "S3/M10:2F2C967F8E00", "event_object_id": 0, "trigger_script": 60}},
    {"trigger_context": {"logical_room_id": "S3/M10:2F2C967F8E00", "event_object_id": 57, "trigger_script": 0}},
])
def test_rejects_non_wins_or_unreliable_trigger_context(tmp_path, change):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[hidden_object()])
    summary = encounter_summary(debug, state)
    summary.update(change)

    assert debug.record_battle_reappearance_candidate(summary, state) is None
    assert debug.read_exploration()["battle_reappearances"] == {}


@pytest.mark.parametrize("is_boss", [True, False, None])
def test_reappearance_memory_does_not_classify_bosses_or_save_enemy_names(tmp_path, is_boss):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[hidden_object()])
    entry = debug.record_battle_reappearance_candidate(
        encounter_summary(debug, state, is_boss=is_boss, enemies=["任意敌人"]), state
    )

    assert entry["status"] == "awaiting_reappearance"
    assert "is_boss" not in entry
    assert "enemies" not in entry


def test_win_without_observed_absence_does_not_create_pending(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[make_touch_object(57)])

    assert debug.record_battle_reappearance_candidate(encounter_summary(debug, state), state) is None
    assert debug.read_exploration()["battle_reappearances"] == {}


@pytest.mark.parametrize("visible", [
    make_state(objects=[make_touch_object(58)]),
    make_state(objects=[{**make_touch_object(57), "trigger_script": 61}]),
    make_state(objects=[make_touch_object(57)], blocked={(0, 0)}),
])
def test_pending_requires_same_room_object_and_script(tmp_path, visible):
    debug = make_debug_state(tmp_path)
    hidden = make_state(objects=[hidden_object()])
    debug.record_battle_reappearance_candidate(encounter_summary(debug, hidden), hidden)

    assert debug.observe_battle_reappearances(visible) == []
    assert only_encounter(debug)["status"] == "awaiting_reappearance"


def test_multiple_wins_and_reappearances_accumulate_without_countdown_observations(tmp_path):
    debug = make_debug_state(tmp_path)
    hidden = make_state(objects=[hidden_object()])
    visible = make_state(objects=[make_touch_object(57)])

    debug.record_battle_reappearance_candidate(encounter_summary(debug, hidden), hidden)
    debug.observe_battle_reappearances(visible)
    debug.record_battle_reappearance_candidate(encounter_summary(debug, hidden), hidden)
    assert debug.observe_battle_reappearances(hidden) == []
    debug.observe_battle_reappearances(visible)

    entry = only_encounter(debug)
    assert entry["battle_count"] == 2
    assert entry["reappearance_count"] == 2


def test_affordance_matches_only_room_object_and_script_and_projects_to_tools(tmp_path):
    debug = make_debug_state(tmp_path)
    obj = make_touch_object(57)
    hidden = make_state(objects=[hidden_object()])
    debug.record_battle_reappearance_candidate(encounter_summary(debug, hidden), hidden)
    state = make_state(objects=[obj])
    debug.observe_battle_reappearances(state)

    navigation = debug.run_agent_tool("get_navigation", {"limit": 20}, state)["navigation"]
    target = next(entry for entry in navigation["targets"] if entry["id"] == 57)
    inspected = debug.run_agent_tool("inspect_target", {"id": 57}, state)["route"]
    for projected in (target, inspected):
        assert projected["interaction_state"] == "reappeared_battle_trigger"
        assert projected["agent_priority"] == "avoid_repeat"
        assert projected["battle_reappearance_history"]["battle_count"] == 1

    changed_script = make_state(objects=[{**obj, "trigger_script": 61}])
    assert debug.target_affordance(changed_script, 57)["interaction_state"] == "fresh"
    other_object = make_state(objects=[{**obj, "id": 58}])
    assert debug.target_affordance(other_object, 58)["interaction_state"] == "fresh"
    other_room = make_state(objects=[obj], blocked={(0, 0)})
    assert debug.room_identity(other_room)["id"] != debug.room_identity(state)["id"]
    assert debug.target_affordance(other_room, 57)["interaction_state"] == "fresh"


def test_old_v11_defaults_empty_and_saved_data_round_trips(tmp_path):
    debug = make_debug_state(tmp_path)
    debug.exploration_path.parent.mkdir(parents=True, exist_ok=True)
    debug.exploration_path.write_text('{"version":11,"rooms":{},"transitions":{}}', encoding="utf-8")
    assert debug.read_exploration()["battle_reappearances"] == {}

    state = make_state(objects=[hidden_object()])
    debug.record_battle_reappearance_candidate(encounter_summary(debug, state), state)
    persisted = json.loads(debug.exploration_path.read_text(encoding="utf-8"))
    assert debug.read_exploration()["battle_reappearances"] == persisted["battle_reappearances"]


def test_checkpoint_restores_encounter_knowledge_without_future_history(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[hidden_object()])
    write_state(debug, state)
    debug.record_battle_reappearance_candidate(encounter_summary(debug, state), state)
    with patch.object(debug, "run_slot_command", side_effect=fake_save_command(debug)):
        debug.save_checkpoint("before-repeat", "Before Repeat")

    debug.record_battle_reappearance_candidate(encounter_summary(debug, state), state)
    assert only_encounter(debug)["battle_count"] == 2
    with patch.object(debug, "run_slot_command", side_effect=fake_load_command):
        debug.load_checkpoint("before-repeat")
    assert only_encounter(debug)["battle_count"] == 1


def test_battle_trigger_uses_actual_collision_object_not_navigation_target(tmp_path):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "in_battle": True,
        "battle": {"generation": 3, "phase": "command", "enemies": []},
        "trigger_observation": {
            "generation": 8,
            "event_object_id": 88,
            "trigger_script": 7200,
            "completed": False,
            "source": {"scene_id": 3, "map_id": 10, "x": 16, "y": 8},
        },
        "_exploration": {
            "last_route": {"target_selection": {"id": 57, "kind": "search", "status": "reachable"}},
        },
    }
    context = debug.agent_runtime.battle_delegate._trigger_context(state)
    assert context["event_object_id"] == 88
    assert "navigation_target" not in context

    post = make_state(objects=[hidden_object(88, 7200)])
    debug.record_battle_reappearance_candidate({
        "result": "won", "is_boss": False, "enemies": ["小怪"], "trigger_context": context,
    }, post)
    room = debug.read_exploration()["battle_reappearances"][context["logical_room_id"]]
    assert "88" in room
    assert "57" not in room


def test_policy_explains_when_repeating_known_encounter_is_allowed(tmp_path):
    policy = make_debug_state(tmp_path).agent_policy_prompt()
    assert "reappeared_battle_trigger" in policy
    assert "清点房间" in policy
    assert "唯一有意义的推进路径" in policy
