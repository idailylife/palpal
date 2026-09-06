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


def test_player_battle_projection_is_pure_idempotent_and_knowledge_free(tmp_path):
    debug = make_debug_state(tmp_path)
    state = battle_state()
    debug.observe_combat_knowledge(state)

    first = debug.player_battle_view(state)
    second = debug.player_battle_view({"battle": state["battle"]})

    assert first == second
    assert "experience" not in first
    assert debug.player_battle_view({"battle": first}) == first
    assert "hp" not in first["enemies"][0]
    assert "magic_id" not in json.dumps(first)
    assert "experience" in debug.current_player_battle_view(state)


def test_new_combat_knowledge_is_only_added_in_the_new_sqlite_turn(tmp_path):
    from agents import SQLiteSession

    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    session = SQLiteSession("battle-history", runtime.sqlite_path)

    async def append_prompt(state):
        prompt = {
            "feedback": "next command",
            "battle": debug.current_player_battle_view(state),
        }
        await session.add_items([{
            "role": "user",
            "content": json.dumps(prompt, ensure_ascii=False, separators=(",", ":")),
        }])
        return prompt

    first_state = battle_state(sequence=1)
    debug.observe_combat_knowledge(first_state)
    first_prompt = asyncio.run(append_prompt(first_state))
    prefix_before = asyncio.run(session.get_items())
    prefix_bytes = json.dumps(prefix_before, ensure_ascii=False, separators=(",", ":")).encode()

    second_state = battle_state(sequence=2)
    debug.observe_combat_knowledge(second_state)
    second_prompt = asyncio.run(append_prompt(second_state))
    history = asyncio.run(session.get_items())

    assert json.dumps(history[:1], ensure_ascii=False, separators=(",", ":")).encode() == prefix_bytes
    assert first_prompt["battle"]["experience"] != second_prompt["battle"]["experience"]
    assert len(history) == 2


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


def test_v1_combat_knowledge_migrates_without_using_or_rewriting_encounters(tmp_path):
    debug = make_debug_state(tmp_path)
    data = debug.empty_exploration()
    data["combat_knowledge"] = {
        "version": 1,
        "actions": {"kept": {"samples": []}},
        "encounters": {"81,81": {"samples": [{"result": "lost"}]}},
        "seen": ["old"],
    }
    debug.write_exploration(data)

    migrated = debug.read_exploration()["combat_knowledge"]

    assert migrated["version"] == 2
    assert migrated["actions"] == {"kept": {"samples": []}}
    assert migrated["encounters"] == {"81,81": {"samples": [{"result": "lost"}]}}
    assert migrated["seen"] == ["old"]
    assert migrated["enemies"] == {}
    assert migrated["active_battles"] == {}
    assert migrated["hp_tracking_version"] == 2


def test_defeated_enemy_hp_interval_is_published_only_after_battle_completion(tmp_path):
    debug = make_debug_state(tmp_path)
    state = battle_state()
    first = state["battle"]["visible_outcomes"][0]
    first["enemy_changes"][0]["amount"] = 100
    final = json.loads(json.dumps(first))
    final["sequence"] = 2
    final["enemy_changes"][0].update(amount=29, defeated=True)
    state["battle"]["visible_outcomes"] = [first, final]

    debug.observe_combat_knowledge(state)
    assert debug.combat_experience(state["battle"])["enemy_memories"][0]["hp_estimate"] == {
        "status": "unknown",
    }

    completed = json.loads(json.dumps(state))
    completed.update(in_battle=False, battle=None)
    completed["last_battle"] = state["battle"] | {"result": "lost"}
    debug.observe_combat_knowledge(completed)

    estimate = debug.combat_experience(state["battle"])["enemy_memories"][0]["hp_estimate"]
    assert estimate == {
        "status": "estimated", "lower_exclusive": 100,
        "upper_inclusive": 129, "sample_count": 1,
    }
    assert debug.read_exploration()["combat_knowledge"]["encounters"] == {}


def test_single_enemy_win_log_fills_missing_defeated_marker_and_is_idempotent(tmp_path):
    debug = make_debug_state(tmp_path)
    completed = battle_state()
    outcomes = []
    for sequence, amount in enumerate((60, 97, 60, 96, 59, 97, 60, 94, 60), start=1):
        outcome = json.loads(json.dumps(completed["battle"]["visible_outcomes"][0]))
        outcome["sequence"] = sequence
        outcome["enemy_changes"][0].update(
            object_id=491, name="五毒巨蛇", amount=amount,
        )
        outcomes.append(outcome)
    completed["last_battle"] = {
        **completed.pop("battle"),
        "result": "won",
        "enemies": [{"object_id": 491, "name": "五毒巨蛇"}],
        "visible_outcomes": outcomes,
    }
    completed["in_battle"] = False
    # Simulate a result already observed by the previous implementation.
    data = debug.empty_exploration()
    data["combat_knowledge"]["seen"] = ["game-a:4:result"]
    debug.write_exploration(data)

    debug.observe_combat_knowledge(completed)
    debug.observe_combat_knowledge(completed)

    memory = debug.read_exploration()["combat_knowledge"]["enemies"]["491"]
    assert memory["hp_samples"] == [{
        "object_id": 491,
        "name": "五毒巨蛇",
        "lower_exclusive": 623,
        "upper_inclusive": 683,
        "defeat_confirmation": "completed_single_enemy_win",
        "tracking_version": 2,
        "game_instance_id": "game-a",
        "battle_generation": 4,
    }]


def test_transient_empty_enemy_slot_before_final_outcome_preserves_accumulated_damage(tmp_path):
    debug = make_debug_state(tmp_path)
    first = battle_state(sequence=1)
    first["battle"]["visible_outcomes"][0]["enemy_changes"][0]["amount"] = 100
    debug.observe_combat_knowledge(first)

    transient = json.loads(json.dumps(first))
    transient["battle"]["enemies"][0].update(present=False, object_id=0, name="")
    debug.observe_combat_knowledge(transient)

    final = json.loads(json.dumps(transient))
    final_outcome = json.loads(json.dumps(first["battle"]["visible_outcomes"][0]))
    final_outcome["sequence"] = 2
    final_outcome["enemy_changes"][0].update(amount=29, defeated=True)
    final["battle"]["visible_outcomes"].append(final_outcome)
    debug.observe_combat_knowledge(final)

    completed = json.loads(json.dumps(final))
    completed["last_battle"] = completed.pop("battle") | {
        "result": "won", "enemies": [{"object_id": 81, "name": "灯笼"}],
    }
    completed["in_battle"] = False
    debug.observe_combat_knowledge(completed)

    estimate = debug.combat_experience(first["battle"])["enemy_memories"][0]["hp_estimate"]
    assert estimate == {
        "status": "estimated", "lower_exclusive": 100,
        "upper_inclusive": 129, "sample_count": 1,
    }


@pytest.mark.parametrize("mutation", ["multiple_enemies", "sequence_gap", "identity_change"])
def test_win_log_fallback_rejects_ambiguous_enemy_lifecycles(tmp_path, mutation):
    debug = make_debug_state(tmp_path)
    source = battle_state()["battle"] | {"result": "won"}
    source["visible_outcomes"][0]["enemy_changes"][0]["defeated"] = False
    if mutation == "multiple_enemies":
        source["enemies"].append({"object_id": 82, "name": "other"})
    elif mutation == "sequence_gap":
        source["visible_outcomes"][0]["sequence"] = 2
    else:
        source["visible_outcomes"].append(json.loads(json.dumps(source["visible_outcomes"][0])))
        source["visible_outcomes"][1]["sequence"] = 2
        source["visible_outcomes"][1]["enemy_changes"][0]["object_id"] = 82

    assert debug._single_enemy_win_hp_fallback(source) is None


def test_surviving_enemy_and_sequence_gap_do_not_create_hp_hints(tmp_path):
    debug = make_debug_state(tmp_path)
    survivor = battle_state()
    survivor.update(in_battle=False, battle=None)
    survivor["last_battle"] = battle_state()["battle"] | {"result": "lost"}
    debug.observe_combat_knowledge(survivor)

    gap = battle_state(generation=5, sequence=2)
    gap["last_battle"] = gap.pop("battle") | {"result": "won"}
    gap["in_battle"] = False
    gap["last_battle"]["visible_outcomes"][0]["enemy_changes"][0]["defeated"] = True
    debug.observe_combat_knowledge(gap)

    estimate = debug.combat_experience(battle_state()["battle"])["enemy_memories"][0]["hp_estimate"]
    assert estimate == {"status": "unknown"}


def test_hp_estimates_intersect_and_report_inconsistent_samples_without_internal_ids(tmp_path):
    debug = make_debug_state(tmp_path)
    data = debug.empty_exploration()
    data["combat_knowledge"]["enemies"] = {
        "81": {
            "object_id": 81, "name": "灯笼", "enemy_effect_samples": [],
            "hp_samples": [
                {"lower_exclusive": 100, "upper_inclusive": 130, "game_instance_id": "hidden"},
                {"lower_exclusive": 110, "upper_inclusive": 125, "battle_generation": 2},
            ],
        },
    }
    debug.write_exploration(data)
    estimate = debug.combat_experience(battle_state()["battle"])["enemy_memories"][0]["hp_estimate"]
    assert estimate == {
        "status": "estimated", "lower_exclusive": 110,
        "upper_inclusive": 125, "sample_count": 2,
    }

    data = debug.read_exploration()
    data["combat_knowledge"]["enemies"]["81"]["hp_samples"].append({
        "lower_exclusive": 200, "upper_inclusive": 220, "game_instance_id": "also-hidden",
    })
    debug.write_exploration(data)
    inconsistent = debug.combat_experience(battle_state()["battle"])["enemy_memories"][0]["hp_estimate"]
    assert inconsistent["status"] == "inconsistent"
    assert inconsistent["samples"][-1] == {"lower_exclusive": 200, "upper_inclusive": 220}
    assert "game_instance_id" not in json.dumps(inconsistent)


def test_enemy_effect_memory_keeps_identity_and_only_friendly_visible_consequences(tmp_path):
    debug = make_debug_state(tmp_path)
    state = battle_state()
    outcome = state["battle"]["visible_outcomes"][0]
    outcome["actor"] = {"side": "enemy", "index": 0, "object_id": 81, "name": "灯笼"}
    outcome["action"] = {"action_type": "enemy_action", "object_id": 999, "name": "hidden"}

    debug.observe_combat_knowledge(state)

    sample = debug.combat_experience(state["battle"])["enemy_memories"][0]["enemy_effects"][0]
    assert sample["actor"]["object_id"] == 81
    assert sample["player_changes"]
    assert "enemy_changes" not in sample
    assert "numbers" not in sample
    assert "object_id" not in sample
    assert "name" not in sample
    assert "game_instance_id" not in sample


def test_battle_memo_filters_current_lineup_and_honors_owner(tmp_path):
    debug = make_debug_state(tmp_path)
    state = battle_state()
    debug.observe_combat_knowledge(state)
    write_state(debug, state)

    memo = debug.current_battle_memo_view()
    assert memo["ok"] is True
    assert memo["is_boss"] is False
    assert memo["current_enemies"] == [{"slot_index": 0, "object_id": 81, "name": "灯笼"}]
    assert memo["enemy_memories"][0]["object_id"] == 81
    assert memo["enemy_memories"][0]["hp_estimate"] == {"status": "unknown"}
    assert {entry["enemy_object_id"] for entry in memo["enemy_memories"][0]["player_actions"]} == {81}
    assert "signature" not in memo

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

    sample = debug.combat_experience(None, item_id=9)["matching_actions"][0]["samples"][0]
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

    projected = runtime._migrate_model_transcript_item(item)

    assert json.loads(item["content"])["battle"]["enemies"][0]["hp"] == 999
    visible = json.loads(projected["content"])["battle"]
    assert "hp" not in visible["enemies"][0]
    assert "magic_id" not in json.dumps(visible)
