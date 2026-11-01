"""Offline repair preserves source saves and never uses future/live evidence."""
import json
from unittest.mock import patch

import pytest

from scripts.repair_checkpoint_memory import inventory, merge_memory, read_json, repair, validator
from tests.support import create_agent_task, fake_save_command, make_debug_state, make_state, write_state


def fixture_pair(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    write_state(debug, state)
    debug.run_agent_tool("upsert_room_memo", {"text": "known historical room"}, state)
    create_agent_task(debug, {"id": "old-task", "description": "Historical task", "rationale": "Recorded dialogue"}, state)
    create_agent_task(debug, {"id": "main-task", "description": "Old main goal", "rationale": "Recorded dialogue"}, state)
    debug.agent_runtime.emit("state", "observed", "baseline evidence")
    with patch.object(debug, "run_slot_command", side_effect=fake_save_command(debug)):
        debug.save_checkpoint("baseline")
    debug.write_exploration(debug.empty_exploration())
    create_agent_task(debug, {"id": "main-task", "description": "New main goal", "rationale": "New recorded evidence"}, state)
    debug.agent_runtime.emit("state", "observed", "later evidence")
    def save_later(command, slot):
        debug.save_slot_path(slot).write_bytes(b"later game progress")
        return {"result": {"ok": True, "status": "saved"}}
    with patch.object(debug, "run_slot_command", side_effect=save_later):
        debug.save_checkpoint("target")
    return debug, debug.checkpoint_dir("baseline"), debug.checkpoint_dir("target")


def test_repair_publishes_new_bundle_preserving_game_current_goals_and_history(tmp_path):
    debug, base, target = fixture_pair(tmp_path)
    before = inventory(target)
    original_bundle = read_json(target / "agent/session.json")
    result = repair(base, target)
    fixed = target.parent / result["repaired_checkpoint"]
    validator().checkpoint_validate(fixed)
    data = read_json(fixed / "companion/exploration.json")
    bundle = read_json(fixed / "agent/session.json")
    assert len(data["room_memos"]) == 1
    assert len(data["tasks"]) == 2
    assert data["tasks"]["main-task"]["description"] == "New main goal"
    assert data["tasks"]["old-task"]["description"] == "Historical task"
    assert (fixed / "game.rpg").read_bytes() == b"later game progress"
    assert inventory(target) == before
    assert bundle["transcript"][:-1] == original_bundle["transcript"]
    assert bundle["events"][:-1] == original_bundle["events"]
    assert bundle["control"]["task_reconciliation_pending"]
    assert read_json(fixed / "recovery/report.json")["restored_task_ids"] == ["old-task"]
    with pytest.raises(FileExistsError):
        repair(base, target)


def test_repair_rejects_future_or_different_branch(tmp_path):
    _, base, target = fixture_pair(tmp_path)
    with pytest.raises(ValueError, match="ancestor"):
        repair(target, base)
    # Break the prefix while retaining a structurally valid complete checkpoint.
    bundle_path = target / "agent/session.json"
    bundle = read_json(bundle_path)
    bundle["events"][0]["summary"] = "different branch"
    bundle_path.write_text(json.dumps(bundle))
    manifest_path = target / "manifest.json"
    manifest = read_json(manifest_path)
    manifest["integrity"] = {k: v for k, v in inventory(target).items() if k != "manifest.json"}
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="ancestor"):
        repair(base, target)
    assert not (target.parent / "target-memory-repaired").exists()


def test_memory_merge_preserves_target_context_and_clears_execution_caches(tmp_path):
    debug, base, target = fixture_pair(tmp_path)
    old = read_json(base / "companion/exploration.json")
    new = read_json(target / "companion/exploration.json")
    old["rooms"] = {"room": {"visited": ["1,1"], "frontier": ["2,2"], "last_route": {"pending": True}}}
    new["rooms"] = {"room": {"visited": ["2,2"], "frontier": ["1,1", "3,3"], "blocked_edges": ["bad edge"]}}
    old["object_interactions"] = {"room": {"1": {"attempt_count": 10, "effect_count": 9,
        "history": [{"type": "interaction_effect", "time": 1}]}}}
    new["object_interactions"] = {"room": {"1": {"attempt_count": 1, "effect_count": 1,
        "history": [{"type": "interaction_effect", "time": 2}]}}}
    result = merge_memory(old, new)
    assert result["rooms"]["room"]["visited"] == ["1,1", "2,2"]
    assert result["rooms"]["room"]["frontier"] == ["3,3"]
    assert "last_route" not in result["rooms"]["room"]
    assert result["rooms"]["room"]["blocked_edges"] == []
    assert result["interaction_observable_context"] == new["interaction_observable_context"]
    assert result["tasks_revision"] > max(old["tasks_revision"], new["tasks_revision"])
    assert result["object_interactions"]["room"]["1"]["attempt_count"] == 10
    assert result["object_interactions"]["room"]["1"]["effect_count"] == 9
