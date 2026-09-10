import copy
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from palpal.agent_runtime import BattleDelegate, BATTLE_INSTRUCTIONS
from palpal.battle_items import BattleItemDetails
from palpal.battle_observations import survival_risk
from tests.support import make_debug_state


def test_round_end_accounts_poison_and_status_without_counting_an_enemy_action(tmp_path):
    debug = make_debug_state(tmp_path)
    state = {"battle": {"players": [{"index": 1, "hp": 178}], "visible_outcomes": []}}
    _, checkpoint = survival_risk(debug.player_battle_view(state), None)
    state["battle"]["players"][0]["hp"] = 52
    state["battle"]["visible_outcomes"] = [
        {"sequence": 1, "actor": {"side": "enemy", "index": 0}, "action": {"action_type": "enemy_action"},
         "player_changes": [{"index": 1, "hp": {"before": 178, "after": 59}}]},
        {"sequence": 2, "actor": {"side": "environment"}, "action": {"action_type": "round_end"},
         "player_changes": [{"index": 1, "hp": {"before": 59, "after": 52},
                             "status_before": [{"key": "sleep", "remaining_rounds": 1}], "status_after": []}]},
    ]
    view = debug.player_battle_view(state, all_outcomes=True)
    risk, checkpoint = survival_risk(view, checkpoint)
    assert risk["last_window"]["hp_accounting"] == "matched"
    assert risk["last_window"]["enemy_action_count"] == 1
    assert risk["last_window"]["players"][0]["observed_hp_loss"] == 126
    assert risk["last_window"]["players"][0]["unexplained_hp_delta"] == 0
    view["survival_risk"] = risk
    compact = BattleDelegate._agent_battle_view(view, include_experience=False, after_outcome_sequence=0)
    event = compact["new_outcomes"][1]
    assert event["actor_side"] == "environment" and event["action_type"] == "round_end"
    assert event["player_changes"][0]["status_after"] == []
    assert survival_risk(view, checkpoint)[0] == risk


def test_unexplained_hp_is_not_guessed_to_be_poison_and_no_outcome_change_detected():
    view = {"players": [{"index": 0, "hp": 100}]}
    _, previous = survival_risk(view, None)
    view["players"][0]["hp"] = 93
    risk, checkpoint = survival_risk(view, previous)
    assert risk["last_window"]["complete"]
    assert risk["last_window"]["hp_accounting"] == "unresolved"
    assert risk["last_window"]["players"][0]["unexplained_hp_delta"] == -7
    assert "poison" not in json.dumps(risk["last_window"])
    assert survival_risk(view, checkpoint)[0] == risk


def test_team_projection_hides_old_actions_and_keeps_committed_actions():
    action = {"action_type": "magic", "object_id": 296, "target_index": 0}
    view = {"players": [{"index": i, "command_status": status, "submitted_action": action}
                        for i, status in enumerate(("committed", "selecting", "unable", "unselected", "unknown"))],
            "enemy_changes": {"appeared": [{"slot_index": 3}]}}
    original = copy.deepcopy(view)
    compact = BattleDelegate._agent_battle_view(view, include_experience=False, after_outcome_sequence=0)
    assert compact["players"][0]["committed_action"] == action
    assert all("committed_action" not in p for p in compact["players"][1:])
    assert "submitted_action" not in json.dumps(compact)
    assert compact["enemy_roster_changes"] == view["enemy_changes"]
    assert "enemy_changes" not in compact and view == original
    assert compact["decision_feedback"]["status"] == "no_new_effects"


def test_zero_healing_and_multiple_affected_targets_are_explicit():
    view = {"recent_outcomes": [
        {"sequence": 1, "actor_side": "player", "action_type": "magic", "object_id": 296,
         "player_changes": [{"index": 0, "mp": {"before": 50, "after": 44}}]},
        {"sequence": 2, "actor_side": "enemy", "player_changes": [
            {"index": 0, "hp": {"before": 200, "after": 90}},
            {"index": 1, "hp": {"before": 100, "after": 0}, "knocked_out": True}]},
    ]}
    compact = BattleDelegate._agent_battle_view(view, include_experience=False, after_outcome_sequence=0)
    assert compact["decision_feedback"]["events"][0]["effective_hp_recovery"] == 0
    assert compact["decision_feedback"]["events"][1]["affected_players"] == [0, 1]
    assert compact["new_outcomes"][1]["player_changes"][1]["knocked_out"]


def item_reader(page_chars=12000):
    state = {"inventory": [{"item_id": 255, "amount": 4, "use_effect": {"preview_complete": True,
             "effects": [{"kind": "remove_status", "status": "sleep"}]},
             "throw_effect": {"preview_complete": False, "effects": [{"kind": "enemy_damage", "amount": 120}]}}]}
    calls = []
    def snapshot():
        calls.append(True)
        return copy.deepcopy(state)
    def project(item):
        projected = copy.deepcopy(item)
        projected["available_amount"] = int(item.get("amount") or 0)
        return projected
    debug = SimpleNamespace(snapshot=snapshot, player_battle_inventory_item=project,
                            combat_experience=lambda battle: {"support_actions": []})
    return BattleItemDetails(debug, page_chars=page_chars), state, calls


def test_batch_items_use_one_snapshot_preserve_effects_and_partial_errors():
    reader, state, calls = item_reader()
    batch = json.loads(reader.query({"item_ids": [255, 9, 255]}))
    assert len(calls) == 1 and len(batch["results"]) == 2
    item = batch["results"][0]["item"]
    assert item["use_effect"]["effects"][0]["status"] == "sleep"
    assert not item["throw_effect"]["preview_complete"]
    assert batch["results"][1]["error"] == "item_not_in_inventory"
    state["inventory"][0]["amount"] = 2
    assert json.loads(reader.query({"item_id": 255}))["item"]["available_amount"] == 2


@pytest.mark.parametrize("args", [{}, {"item_id": 0}, {"item_id": True}, {"item_ids": []},
                                   {"item_id": 1, "item_ids": [1]}, {"item_ids": list(range(1, 34))}])
def test_batch_invalid_requests_do_not_read_state(args):
    reader, _, calls = item_reader()
    assert not json.loads(reader.query(args))["ok"]
    assert not calls


def test_item_fragments_reassemble_without_rereading_changed_inventory():
    reader, state, calls = item_reader(page_chars=100)
    page = json.loads(reader.query({"item_ids": [255]}))
    encoded = page["text"]
    state["inventory"].clear()
    while not page["complete"]:
        cursor = page["next_cursor"]
        assert reader.query({"cursor": cursor}) == reader.query({"cursor": cursor})
        page = json.loads(reader.query({"cursor": cursor}))
        encoded += page["text"]
    assert len(calls) == 1
    assert json.loads(encoded)["results"][0]["item"]["available_amount"] == 4
    assert not json.loads(reader.query({"cursor": "missing"}))["ok"]


def test_item_details_compact_fields_actions_and_recent_observed_samples():
    raw_item = {
        "slot": 2, "item_id": 99, "name": "止血草", "description": "恢復少量體力",
        "amount": 4, "amount_in_use": 1, "equipped_count": 1, "available_amount": 2,
        "usable": True, "throwable": True, "consuming": True, "apply_to_all": False,
        "use_effect": {"source": "dos_script_preview", "preview_complete": False,
                       "effects": [{"kind": "restore_hp", "amount": 50, "conditional": False}]},
        "throw_effect": {"source": "dos_script_preview", "preview_complete": True,
                         "effects": [{"kind": "enemy_damage", "amount": 90, "conditional": False}]},
    }
    actions = [{
        "action_type": "use_item", "object_id": 99, "target_side": "player",
        "target_mode": "single", "submission_target_indices": [0, 1], "valid_target_indices": [0, 1],
    }, {
        "action_type": "throw_item", "object_id": 99, "target_side": "enemy",
        "target_mode": "single", "submission_target_indices": [3], "valid_target_indices": [3],
    }]

    def sample(sequence, hp_delta):
        return {
            "game_instance_id": "old-game", "battle_generation": 7, "sequence": sequence,
            "action_type": "use_item", "object_id": 99, "name": "止血草",
            "actor": {"role_id": 0, "name": "李逍遙"},
            "player_changes": [{"index": 0, "role_id": 0,
                                "hp": {"before": 100, "after": 100 + hp_delta},
                                "mp": {"before": 20, "after": 20}}],
            "enemy_changes": [], "no_numeric_change_observed": False,
        }

    observations = [{
        "action_type": "use_item", "object_id": 99, "actor_role_id": 0,
        "actor_name": "李逍遙", "name": "止血草", "updated_at": 10,
        "samples": [sample(1, 10), sample(2, 20)],
    }, {
        "action_type": "use_item", "object_id": 99, "actor_role_id": 2,
        "actor_name": "林月如", "name": "止血草", "updated_at": 20,
        "samples": [sample(3, 30), sample(4, 40)],
    }, {
        "action_type": "throw_item", "object_id": 99, "enemy_object_id": 469,
        "actor_role_id": 2, "actor_name": "林月如", "name": "止血草", "updated_at": 30,
        "samples": [{"game_instance_id": "old-game", "battle_generation": 8, "sequence": 5,
                     "enemy_changes": [{"slot_index": 3, "object_id": 469, "amount": 90,
                                        "kind": "damage", "defeated": False}]}],
    }]
    state = {"inventory": [raw_item], "battle": {"available_actions": actions}}
    debug = SimpleNamespace(
        snapshot=lambda: copy.deepcopy(state),
        player_battle_inventory_item=copy.deepcopy,
        combat_experience=lambda _battle: {"support_actions": observations},
    )
    output = json.loads(BattleItemDetails(debug).query({"item_id": 99}))

    assert set(output["item"]) == {
        "item_id", "name", "description", "available_amount", "consuming", "use_effect", "throw_effect",
    }
    assert output["legal_battle_actions"] == [{
        "action_type": "use_item", "target_side": "player", "target_mode": "single",
        "submission_target_indices": [0, 1],
    }, {
        "action_type": "throw_item", "target_side": "enemy", "target_mode": "single",
        "submission_target_indices": [3],
    }]
    use, throw = output["observed_effects"]
    assert use["sample_count"] == 4 and len(use["recent_samples"]) == 3
    assert [s["player_changes"][0]["hp_delta"] for s in use["recent_samples"]] == [40, 30, 20]
    assert throw == {
        "action_type": "throw_item", "enemy_object_id": 469, "sample_count": 1,
        "recent_samples": [{"enemy_changes": [{"slot_index": 3, "object_id": 469,
                                                 "amount": 90, "kind": "damage", "defeated": False}]}],
    }
    encoded = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    for redundant in ("game_instance_id", "battle_generation", "sequence", "actor_name", "valid_target_indices"):
        assert redundant not in encoded
    legacy = {"ok": True, "item": raw_item, "observed_effects": observations,
              "legal_battle_actions": actions}
    legacy_encoded = json.dumps(legacy, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded) <= len(legacy_encoded) * 0.75


def test_prompt_replacement_is_shorter_and_not_case_specific():
    fixture = json.loads((Path(__file__).parent / "fixtures/fox_team_decisions.json").read_text())
    assert len(BATTLE_INSTRUCTIONS) < len(fixture["baseline_instructions"])
    for concept in ("committed_action", "area", "Waking", "unresolved", "whole party"):
        assert concept in BATTLE_INSTRUCTIONS
    assert "lowest-cost" not in BATTLE_INSTRUCTIONS
    assert "狐狸" not in BATTLE_INSTRUCTIONS and "蜜蜂" not in BATTLE_INSTRUCTIONS


def test_native_round_end_hook_brackets_settlement_and_command_status_is_exposed():
    root = Path(__file__).parents[1]
    fight = (root / "vendor/sdlpal/fight.c").read_text()
    start = fight.index("PALPAL_AgentVisibleRoundEndBegin();")
    end = fight.index("PALPAL_AgentVisibleActionEnd();", start)
    assert "PAL_RunTriggerScript" in fight[start:end]
    assert "rgPlayerStatus[wPlayerRole][j]--" in fight[start:end]
    assert "PAL_BattleDisplayStatChange()" in fight[start:end]
    native = (root / "vendor/sdlpal/agent.c").read_text()
    assert 'outcome->round_end ? "round_end"' in native
    assert 'player->state == kFighterAct ? "committed"' in native


def test_team_replay_has_no_future_item_samples_and_preserves_known_actions():
    from scripts.evaluate_battle_team import replay_inputs, views_with_pending, item_at
    fixture = json.loads((Path(__file__).parent / "fixtures/fox_team_decisions.json").read_text())
    for command in (6, 9, 10, 12, 13, 18):
        _, old = replay_inputs(fixture, command, False)
        messages, new = replay_inputs(fixture, command, True)
        assert old["available_actions"] == new["available_actions"]
        assert old.get("new_outcomes", []) == new.get("new_outcomes", [])
        assert all(m["role"] == "user" for m in messages)
        assert not any('"previous_item_query_results"' in m["content"] for m in messages) if command == 6 else True
    _, current = replay_inputs(fixture, 6, True)
    assert current["players"][0]["committed_action"]["object_id"] == 296
    assert "committed_action" not in current["players"][1]
    _, current = replay_inputs(fixture, 19, True)
    assert current["survival_risk"]["last_window"]["hp_accounting"] == "unresolved"
    current = list(views_with_pending(fixture, 12))[-1]
    assert item_at(fixture, 12, 131, current)["amount"] == 1
    assert item_at(fixture, 12, 131, current)["available_amount"] == 0
    assert item_at(fixture, 6, 255, current) is None


def test_replay_missing_details_are_unknown_not_absent_inventory(monkeypatch):
    from scripts.evaluate_battle_team import evaluate, Runner
    fixture = json.loads((Path(__file__).parent / "fixtures/fox_team_decisions.json").read_text())
    async def run(agent, messages, max_turns):
        tools = {tool.name: tool for tool in agent.tools}
        encoded = await tools["get_item_details"].on_invoke_tool(None, '{"item_id":255}')
        assert json.loads(encoded)["error"] == "details_not_preserved_in_replay"
        encoded = await tools["get_item_details"].on_invoke_tool(None, '{"item_ids":[255,131]}')
        assert all(r["error"] == "details_not_preserved_in_replay" for r in json.loads(encoded)["results"])
        await tools["submit_battle_action"].on_invoke_tool(None, '{"action_type":"magic","object_id":337,"target_index":-1}')
        return SimpleNamespace(context_wrapper=SimpleNamespace(usage=SimpleNamespace(input_tokens=0, output_tokens=0)), new_items=[])
    monkeypatch.setattr(Runner, "run", run)
    result = asyncio.run(evaluate(fixture, 6, True, 1, "unused-model"))
    assert result["action"]["object_id"] == 337
