import tempfile
from unittest import mock

from conftest import make_checkpoint_state, make_debug_state, write_state


def test_task_ledger_create_update_close_and_filtering():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        created = debug.run_agent_tool("upsert_task", {
            "description": "Find the locked door",
            "rationale": "A dialog mentioned it.",
            "priority": 7,
            "object_id": 75,
        })
        assert created["ok"]
        task_id = created["task"]["id"]
        assert created["task"]["status"] == "active"
        updated = debug.run_agent_tool("upsert_task", {"id": task_id, "status": "completed"})
        assert updated["ok"]
        assert updated["task"]["history"][-1]["changes"]["status"]["to"] == "completed"
        assert debug.task_ledger() == []
        history = debug.task_ledger(include_closed=True)
        assert history[0]["id"] == task_id
        assert not debug.run_agent_tool("upsert_task", {"description": "missing reason"})["ok"]


def test_agent_tasks_view_groups_all_statuses_and_returns_compact_records():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        active = debug.run_agent_tool("upsert_task", {
            "description": "Active high priority", "rationale": "Current evidence.", "priority": 8,
        })["task"]
        debug.run_agent_tool("upsert_task", {
            "description": "Active low priority", "rationale": "Current evidence.", "priority": 2,
        })
        completed = debug.run_agent_tool("upsert_task", {
            "description": "Completed task", "rationale": "Already handled.", "priority": 9,
        })["task"]
        debug.run_agent_tool("upsert_task", {"id": completed["id"], "status": "completed"})
        abandoned = debug.run_agent_tool("upsert_task", {
            "description": "Abandoned task", "rationale": "No longer useful.", "priority": 7,
        })["task"]
        debug.run_agent_tool("upsert_task", {"id": abandoned["id"], "status": "abandoned"})

        view = debug.agent_tasks_view()

        assert view["counts"] == {"active": 2, "completed": 1, "abandoned": 1, "total": 4}
        assert [task["status"] for task in view["tasks"]] == ["active", "active", "completed", "abandoned"]
        assert [task["description"] for task in view["tasks"][:2]] == ["Active high priority", "Active low priority"]
        assert view["tasks"][0] == {
            "id": active["id"],
            "description": "Active high priority",
            "priority": 8,
            "status": "active",
        }


def test_task_ledger_is_additive_and_survives_checkpoint_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_checkpoint_state()
        write_state(debug, state)
        debug.save_slot_path().write_bytes(b"task-checkpoint")
        debug.exploration_path.write_text('{"version":9,"rooms":{},"transitions":{}}', encoding="utf-8")
        assert debug.read_exploration()["tasks"] == {}
        created = debug.run_agent_tool("upsert_task", {
            "description": "Reach the courtyard",
            "rationale": "It is the next stated destination.",
            "room_id": "S3/M99:000000000001",
        })
        with mock.patch.object(debug, "run_slot_command", return_value={"result": {}}):
            debug.save_checkpoint("tasks", "Tasks")
            debug.write_exploration(debug.empty_exploration())
            debug.load_checkpoint("tasks")
        restored = debug.task_ledger()
        assert restored[0]["id"] == created["task"]["id"]
        assert debug.agent_tasks_view()["tasks"][0]["description"] == "Reach the courtyard"


def test_agent_session_reset_keeps_tool_tasks():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        created = debug.run_agent_tool("upsert_task", {
            "description": "Keep this across session reset",
            "rationale": "It belongs to companion memory.",
        })["task"]

        debug.agent_runtime.reset("manual_reset")

        assert debug.agent_tasks_view()["tasks"][0]["id"] == created["id"]


def test_executable_task_overrides_unknown_transition_but_unmatched_task_does_not():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_checkpoint_state()
        write_state(debug, state)
        unknown = {"id": 10, "status": "reachable", "transition_candidate": True,
                   "destination_priority": "unknown", "steps": 1, "kind": "touch"}
        task_object = {"id": 20, "status": "reachable", "transition_candidate": False,
                       "steps": 4, "kind": "search", "agent_priority": "normal"}
        debug.navigation = mock.Mock(return_value={"status": "ok", "targets": [unknown, task_object]})
        debug.route_to_object = mock.Mock(return_value={"route": {"moves": ["right"], "steps": 1}})
        debug.run_agent_tool("upsert_task", {
            "description": "Inspect the marked object", "rationale": "Dialog referred to it.", "object_id": 20,
        })
        assert debug.rules_decide_agent(state)["recommended_action"]["args"] == {"id": 20}

        debug.write_exploration(debug.empty_exploration())
        debug.run_agent_tool("upsert_task", {
            "description": "Later room", "rationale": "Not reachable from this room.", "room_id": "S9/M9:000000000002",
        })
        assert debug.rules_decide_agent(state)["recommended_action"]["args"] == {"id": 10}
