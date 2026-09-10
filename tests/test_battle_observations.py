import asyncio
import copy
import json
from pathlib import Path

import pytest

from palpal.battle_observations import outcome_delta, survival_risk
from tests.support import make_debug_state
from tests.test_combat_knowledge import battle_state


@pytest.mark.parametrize("count", [9, 20, 32])
def test_agent_receives_all_retained_outcomes_but_browser_keeps_short_view(tmp_path, count):
    debug = make_debug_state(tmp_path)
    state = battle_state()
    template = state["battle"]["visible_outcomes"][0]
    state["battle"]["visible_outcomes"] = [{**template, "sequence": n} for n in range(1, count + 1)]
    assert len(debug.player_battle_view(state)["recent_outcomes"]) == 8
    full = debug.current_player_battle_view(state)
    compact = debug.agent_runtime.battle_delegate._agent_battle_view(
        full, include_experience=False, after_outcome_sequence=0,
    )
    assert [o["sequence"] for o in compact["new_outcomes"]] == list(range(1, count + 1))
    assert compact["outcome_delivery"]["complete"]
    repeated = debug.agent_runtime.battle_delegate._agent_battle_view(
        full, include_experience=False, after_outcome_sequence=count,
    )
    assert "new_outcomes" not in repeated
    assert repeated["outcome_delivery"]["through_sequence"] == count
    assert '"hp": 999' not in json.dumps(compact)


def test_overflow_internal_gap_and_duplicate_sequences():
    retained = [{"sequence": n} for n in range(9, 41)]
    _, delivery = outcome_delta(retained, 0)
    assert delivery["missing_ranges"] == [{"first": 1, "last": 8}]
    ordered, delivery = outcome_delta([{"sequence": n} for n in [14, 11, 11, 9]], 8)
    assert [o["sequence"] for o in ordered] == [9, 11, 14]
    assert delivery["missing_ranges"] == [{"first": 10, "last": 10}, {"first": 12, "last": 13}]
    assert not delivery["complete"]


def test_window_counts_damage_separately_from_recovery_and_survives_repeated_commands():
    view = {"players": [{"index": 0, "hp": 200, "status_effects": []}]}
    _, baseline = survival_risk(view, None)
    view["players"][0]["hp"] = 180
    view["recent_outcomes"] = [
        {"sequence": 1, "actor_side": "enemy", "player_changes": [{"index": 0, "hp_delta": -100}]},
        {"sequence": 2, "actor_side": "player", "player_changes": [{"index": 0, "hp_delta": 150}]},
        {"sequence": 3, "actor_side": "enemy", "player_changes": [{"index": 0, "hp_delta": -70}]},
    ]
    original = copy.deepcopy(baseline)
    summary, checkpoint = survival_risk(view, baseline)
    assert baseline == original  # Preparing a prompt does not commit its cursor.
    window = summary["last_window"]
    assert window["enemy_action_count"] == 2
    assert window["players"] == [{
        "index": 0, "hp_before": 200, "hp_after": 180,
        "observed_hp_loss": 170, "observed_hp_recovery": 150, "net_hp_loss": 20,
        "unexplained_hp_delta": 0,
    }]
    assert summary["players"][0]["observed_maxima"] == {
        "single_outcome_hp_loss": 100, "window_hp_loss": 170,
    }
    repeated, _ = survival_risk(view, checkpoint)
    assert repeated == summary
    assert summary["observed_maxima_are_not_damage_limits"]


def test_controls_and_missing_history_are_explicit():
    view = {"players": [{"index": 0, "hp": 7, "status_effects": [{"key": "sleep"}]}],
            "recent_outcomes": [{"sequence": 77, "actor_side": "enemy"}]}
    summary, checkpoint = survival_risk(view, {"last_sequence": 64})
    assert summary["history_incomplete"]
    assert not summary["last_window"]["complete"]
    assert not summary["last_window"]["baseline_available"]
    assert summary["last_window"]["missing_ranges"] == [{"first": 65, "last": 76}]
    assert summary["players"][0]["status_effects"] == [{"key": "sleep"}]
    view["recent_outcomes"] = [{"sequence": 78, "actor_side": "enemy"}]
    summary, _ = survival_risk(view, checkpoint)
    assert summary["last_window"]["complete"]
    assert summary["history_incomplete"]


def test_fox_decision_fixtures_preserve_real_gap_and_danger():
    fixture = json.loads((Path(__file__).parent / "fixtures/fox_battle_decisions.json").read_text())
    checkpoint = None
    summaries = {}
    for view in fixture["views"]:
        summary, checkpoint = survival_risk(view, checkpoint)
        summaries[view["command_generation"]] = summary
    assert summaries[25]["players"][0]["hp"] == 65
    window = summaries[29]["last_window"]
    assert window["missing_ranges"] == [{"first": 65, "last": 76}]
    assert window["players"][1]["hp_before"] == 294
    assert window["players"][1]["hp_after"] == 7
    assert window["players"][1]["net_hp_loss"] == 287
    # The delivered outcomes cannot account for all 287 lost HP.
    assert window["players"][1]["observed_hp_loss"] < 287
    assert summaries[36]["players"][0]["hp"] == 0
    assert summaries[36]["players"][1]["hp"] == 184
    assert summaries[36]["history_incomplete"]


@pytest.mark.parametrize("interrupted", [False, True])
def test_sdk_delivery_commits_only_after_completed_request(tmp_path, monkeypatch, interrupted):
    from agents import Runner

    debug = make_debug_state(tmp_path)
    delegate = debug.agent_runtime.battle_delegate
    state = battle_state(sequence=1)
    state["battle"].update(current_player_index=0, available_actions=[{
        "action_type": "attack", "object_id": 0, "target_mode": "single", "valid_target_indices": [0],
    }])
    delegate._begin(state)
    monkeypatch.setattr(debug, "config_value", lambda key, default=None: "test-key" if key == "DEEPSEEK_API_KEY" else default)
    monkeypatch.setattr(debug, "snapshot", lambda: copy.deepcopy(state))
    monkeypatch.setattr(debug, "battle_action_validation_error", lambda *args: None)
    monkeypatch.setattr(debug.agent_runtime, "_final_model_tool_output", lambda *args: "archived preview")

    class Stream:
        context_wrapper = None

        def __init__(self, agent):
            self.agent = agent

        async def stream_events(self):
            refresh = next(t for t in self.agent.tools if t.name == "get_battle")
            first = json.loads(await refresh.on_invoke_tool(None, "{}"))
            assert "new_outcomes" not in first["battle"]
            next_sequence = state["battle"]["visible_outcomes"][-1]["sequence"] + 1
            state["battle"]["visible_outcomes"].append({
                **state["battle"]["visible_outcomes"][0], "sequence": next_sequence,
            })
            second = json.loads(await refresh.on_invoke_tool(None, "{}"))
            assert [o["sequence"] for o in second["battle"]["new_outcomes"]] == [next_sequence]
            third = json.loads(await refresh.on_invoke_tool(None, "{}"))
            assert "new_outcomes" not in third["battle"]
            if interrupted:
                raise asyncio.CancelledError()
            tool = next(t for t in self.agent.tools if t.name == "submit_battle_action")
            await tool.on_invoke_tool(None, json.dumps({"action_type": "attack", "object_id": 0, "target_index": 0}))
            if False:
                yield

    monkeypatch.setattr(Runner, "run_streamed", lambda agent, *args, **kwargs: Stream(agent))
    if interrupted:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(delegate._sdk_action(state, ""))
        assert delegate._control()["last_sent_outcome_sequence"] == 0
        assert delegate._control()["survival_observations"] is None
        state["battle"]["generation"] += 1
        delegate._persist_control(last_sent_outcome_sequence=3, survival_observations={"last_sequence": 3})
        delegate._begin(state)
        assert delegate._control()["last_sent_outcome_sequence"] == 0
        assert delegate._control()["survival_observations"] is None
    else:
        asyncio.run(delegate._sdk_action(state, ""))
        assert delegate._control()["last_sent_outcome_sequence"] == 2
        assert delegate._control()["survival_observations"]["last_sequence"] == 2
        asyncio.run(delegate._sdk_action(state, ""))
        assert delegate._control()["last_sent_outcome_sequence"] == 3
        delegate._begin(state)
        assert delegate._control()["survival_observations"]["last_sequence"] == 3
        state["game_instance_id"] = "another-game"
        delegate._begin(state)
        assert delegate._control()["last_sent_outcome_sequence"] == 0
        assert delegate._control()["survival_observations"] is None


def test_model_replay_is_observation_only_and_variants_use_same_evidence():
    from scripts.evaluate_battle_survival import instructions, replay_inputs
    from palpal.agent_runtime import BATTLE_SURVIVAL_INSTRUCTIONS

    fixture = json.loads((Path(__file__).parent / "fixtures/fox_battle_decisions.json").read_text())
    for command in (25, 29, 36):
        old_messages, baseline = replay_inputs(fixture, command, False)
        new_messages, revised = replay_inputs(fixture, command, True)
        assert len(old_messages) == len(new_messages) == command
        assert all(m["role"] == "user" for m in new_messages)
        assert revised["players"] == baseline["players"]
        assert revised["new_outcomes"] == baseline["new_outcomes"]
        assert revised["available_actions"] == baseline["available_actions"]
        assert "survival_risk" in revised and "survival_risk" not in baseline
    assert instructions(True) == instructions(False) + BATTLE_SURVIVAL_INSTRUCTIONS
