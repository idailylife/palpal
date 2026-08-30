import asyncio
import json
from unittest.mock import patch

import pytest

from palpal.agent_runtime import BattleStarted
from palpal.debug_server import COMBAT_SAMPLE_CAPACITY
from tests.support import fake_load_command, fake_save_command, make_debug_state, make_state, write_state


def battle_state(*, generation=4, sequence=1, result="ongoing"):
    outcome = {
        "sequence": sequence,
        "actor": {"side": "player", "index": 0, "role_id": 0, "name": "李逍遥"},
        "action": {"action_type": "magic", "object_id": 327, "name": "炎咒", "mp_cost": 8},
        "target": {"index": 0, "side": "enemy", "object_id": 81, "name": "灯笼"},
        "player_changes": [{
            "index": 0,
            "hp": {"before": 90, "after": 100, "max": 100},
            "mp": {"before": 20, "after": 12, "max": 30},
            "status_before": [],
            "status_after": [],
            "poisons_before": [],
            "poisons_after": [],
        }],
        "enemy_changes": [{
            "slot_index": 0, "object_id": 81, "name": "灯笼", "amount": 23, "kind": "damage",
        }],
        "numbers": [
            {"side": "enemy", "target_index": 0, "stat": "hp", "amount": 10, "kind": "damage", "object_id": 81, "name": "灯笼"},
            {"side": "enemy", "target_index": 0, "stat": "hp", "amount": 13, "kind": "damage", "object_id": 81, "name": "灯笼"},
        ],
        "no_numeric_change_observed": False,
    }
    return {
        "game_instance_id": "game-a",
        "in_battle": True,
        "battle": {
            "generation": generation,
            "command_generation": 2,
            "phase": "command",
            "players": [{"index": 0, "role_id": 0, "name": "李逍遥", "hp": 100, "max_hp": 100, "mp": 12, "max_mp": 30}],
            "enemies": [{"slot_index": 0, "present": True, "object_id": 81, "name": "灯笼", "hp": 999, "max_hp": 9999, "magic_id": 500}],
            "available_actions": [],
            "visible_outcomes": [outcome],
        },
        "last_battle": None,
    }


def test_visible_outcomes_are_projected_without_enemy_internal_state(tmp_path):
    debug = make_debug_state(tmp_path)
    state = battle_state()

    view = debug.player_battle_view(state)

    assert view["enemies"] == [{"slot_index": 0, "present": True, "object_id": 81, "name": "灯笼"}]
    assert view["recent_outcomes"][0]["enemy_changes"][0]["amount"] == 23
    assert [number["amount"] for number in view["recent_outcomes"][0]["numbers"]] == [10, 13]
    assert view["recent_outcomes"][0]["player_changes"][0]["mp"] == {"before": 20, "after": 12, "max": 30}
    encoded = json.dumps(view, ensure_ascii=False)
    assert '"hp": 999' not in encoded
    assert "magic_id" not in encoded


def test_combat_knowledge_deduplicates_and_retains_recent_samples(tmp_path):
    debug = make_debug_state(tmp_path)
    for sequence in range(1, COMBAT_SAMPLE_CAPACITY + 4):
        debug.observe_combat_knowledge(battle_state(sequence=sequence))
    debug.observe_combat_knowledge(battle_state(sequence=COMBAT_SAMPLE_CAPACITY + 3))

    knowledge = debug.read_exploration()["combat_knowledge"]
    entry = next(iter(knowledge["actions"].values()))
    assert len(entry["samples"]) == COMBAT_SAMPLE_CAPACITY
    assert [sample["sequence"] for sample in entry["samples"]] == list(range(4, 12))
    assert entry["samples"][-1]["player_changes"][0]["hp"]["max"] == 100
    assert "hp_after" not in entry["samples"][-1]["enemy_changes"][0]


def test_combat_knowledge_caps_action_and_encounter_keys(tmp_path):
    debug = make_debug_state(tmp_path)
    data = debug.empty_exploration()
    knowledge = data["combat_knowledge"]
    knowledge["actions"] = {
        str(index): {"updated_at": index, "samples": []} for index in range(260)
    }
    knowledge["encounters"] = {
        str(index): {"updated_at": index, "samples": []} for index in range(132)
    }
    debug._combat_trim_keys(knowledge["actions"], 256)
    debug._combat_trim_keys(knowledge["encounters"], 128)

    assert len(knowledge["actions"]) == 256
    assert len(knowledge["encounters"]) == 128
    assert set(map(str, range(4))).isdisjoint(knowledge["actions"])
    assert set(map(str, range(4))).isdisjoint(knowledge["encounters"])


@pytest.mark.parametrize("result", ["won", "lost", "fled"])
def test_completed_encounter_records_result_for_exact_multiset(tmp_path, result):
    debug = make_debug_state(tmp_path)
    state = battle_state(result=result)
    state["in_battle"] = False
    state["battle"] = None
    state["last_battle"] = {
        **battle_state()["battle"],
        "result": result,
        "enemies": [
            {"object_id": 81, "name": "灯笼"},
            {"object_id": 81, "name": "灯笼"},
        ],
    }

    debug.observe_combat_knowledge(state)

    encounter = debug.read_exploration()["combat_knowledge"]["encounters"]["81,81"]
    assert encounter["samples"][0]["result"] == result
    assert encounter["enemy_object_ids"] == [81, 81]


def test_battle_memo_filters_current_lineup_and_honors_owner(tmp_path):
    debug = make_debug_state(tmp_path)
    state = battle_state()
    debug.observe_combat_knowledge(state)
    write_state(debug, state)

    memo = debug.current_battle_memo_view()
    assert memo["ok"] is True
    assert memo["signature"] == "81"
    assert {entry["enemy_object_id"] for entry in memo["action_experience"]} == {81}

    debug.companion_owner_path.write_text(json.dumps({"session_id": "someone-else"}), encoding="utf-8")
    rejected = debug.current_battle_memo_view()
    assert rejected == {"ok": False, "status": "memory_unavailable", "error": "companion_owner_mismatch"}


def test_nonbattle_item_observation_preserves_capped_recovery(tmp_path):
    debug = make_debug_state(tmp_path)
    before = make_state(frame=10)
    before.update(game_instance_id="game-a", party=[{
        "role_id": 0, "name": "李逍遥", "hp": 95, "max_hp": 100, "mp": 29, "max_mp": 30,
        "status_effects": [], "poisons": [],
    }], inventory=[{"item_id": 9, "name": "药", "amount": 1}])
    after = json.loads(json.dumps(before))
    after["frame"] = 11
    after["party"][0].update(hp=100, mp=30)

    debug.observe_visible_item_use(before, after, 9, 0)

    sample = debug.combat_experience(None, item_id=9)["actions"][0]["samples"][0]
    assert sample["player_changes"][0]["hp"] == {"before": 95, "after": 100, "max": 100}
    assert sample["player_changes"][0]["mp"] == {"before": 29, "after": 30, "max": 30}


def test_checkpoint_rollback_restores_branch_local_combat_knowledge(tmp_path):
    debug = make_debug_state(tmp_path)
    live = make_state(frame=20)
    live["game_instance_id"] = "game-a"
    write_state(debug, live)
    debug.observe_combat_knowledge(battle_state(generation=1, sequence=1))

    with patch.object(debug, "run_slot_command", side_effect=fake_save_command(debug)):
        debug.save_checkpoint("combat-a", "Combat A")

    debug.observe_combat_knowledge(battle_state(generation=2, sequence=1))
    assert len(debug.read_exploration()["combat_knowledge"]["actions"].values()) == 1
    samples = next(iter(debug.read_exploration()["combat_knowledge"]["actions"].values()))["samples"]
    assert [sample["battle_generation"] for sample in samples] == [1, 2]

    with patch.object(debug, "run_slot_command", side_effect=fake_load_command):
        debug.load_checkpoint("combat-a")

    restored = next(iter(debug.read_exploration()["combat_knowledge"]["actions"].values()))["samples"]
    assert [sample["battle_generation"] for sample in restored] == [1]


def test_submit_decision_reports_battle_before_task_inbox_gate(tmp_path):
    debug = make_debug_state(tmp_path)
    state = battle_state()
    write_state(debug, state)

    result = debug.run_agent_tool(
        "submit_decision",
        {"action": "navigate", "target_id": 3, "reason": "Move."},
        make_state(),
        tool_cycle={"task_inbox_pending": True},
    )

    assert result["error"]["code"] == "battle_started"


def test_model_battle_watch_cancels_ordinary_decision(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    calls = 0

    async def slow_decision(_state, _model, _feedback):
        await asyncio.sleep(5)
        return {"recommended_action": {"tool": "no_op", "args": {}}}

    ordinary = make_state()
    ordinary.update(game_instance_id="game-a", in_battle=False, battle=None)
    started = battle_state()

    def snapshot():
        nonlocal calls
        calls += 1
        return ordinary if calls == 1 else started

    runtime.decision_provider = slow_decision
    debug.snapshot = snapshot

    with pytest.raises(BattleStarted) as exc:
        asyncio.run(runtime._decide_with_battle_watch(ordinary, "choose"))
    assert exc.value.state["battle"]["generation"] == 4


def test_legacy_raw_battle_audit_is_projected_only_for_model_input(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    raw = battle_state()["battle"]
    item = {"role": "user", "content": json.dumps({"battle": raw}, ensure_ascii=False)}

    projected = runtime._project_model_transcript_item(item)

    assert json.loads(item["content"])["battle"]["enemies"][0]["hp"] == 999
    visible = json.loads(projected["content"])["battle"]
    assert "hp" not in visible["enemies"][0]
    assert "magic_id" not in json.dumps(visible)
