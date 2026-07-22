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
        assert created["operation"] == "created"
        task_id = created["task"]["id"]
        assert task_id.startswith("task-")
        assert created["task"]["status"] == "active"
        updated = debug.run_agent_tool("upsert_task", {"id": task_id, "status": "completed"})
        assert updated["ok"]
        assert updated["operation"] == "updated"
        assert updated["task"]["history"][-1]["changes"]["status"]["to"] == "completed"
        assert debug.task_ledger() == []
        history = debug.task_ledger(include_closed=True)
        assert history[0]["id"] == task_id
        assert not debug.run_agent_tool("upsert_task", {"description": "missing reason"})["ok"]


def test_task_upsert_accepts_stable_custom_id_and_does_not_duplicate_on_update():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        created = debug.run_agent_tool("upsert_task", {
            "id": "buy_shrimp",
            "description": "幫李大娘買新鮮蝦：先去菜市場，買不到再找漁船家。已獲五十文。",
            "status": "active",
            "priority": 5,
            "rationale": "李大娘委託，給50文買鮮蝦。",
        })

        assert created["ok"] is True
        assert created["operation"] == "created"
        assert created["task"]["id"] == "buy_shrimp"

        updated = debug.run_agent_tool("upsert_task", {
            "id": "buy_shrimp",
            "priority": 8,
            "room_id": "S3/M99:000000000001",
        })
        assert updated["operation"] == "updated"
        assert updated["task"]["priority"] == 8
        assert list(debug.read_exploration()["tasks"]) == ["buy_shrimp"]


def test_unknown_custom_task_id_requires_complete_creation_fields_without_writing():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)

        result = debug.run_agent_tool("upsert_task", {
            "id": "buy_shrimp",
            "description": "Buy shrimp",
        })

        assert result["ok"] is False
        assert "description and rationale" in result["error"]
        assert "get_tasks" in result["error"]
        assert debug.read_exploration()["tasks"] == {}


def test_get_tasks_defaults_to_active_and_can_include_closed_limit_and_full_fields():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        active = debug.run_agent_tool("upsert_task", {
            "id": "active-task",
            "description": "Reach the courtyard",
            "rationale": "The route is now known.",
            "priority": 4,
            "object_id": 17,
            "transition_key": "S1/O17",
            "room_id": "S3/M99:000000000001",
        })["task"]
        closed = debug.run_agent_tool("upsert_task", {
            "id": "closed-task",
            "description": "Old errand",
            "rationale": "It was already handled.",
            "priority": 9,
        })["task"]
        debug.run_agent_tool("upsert_task", {"id": closed["id"], "status": "completed"})

        default = debug.run_agent_tool("get_tasks")
        assert default["include_closed"] is False
        assert default["returned"] == default["total"] == 1
        assert default["tasks"][0]["id"] == active["id"]
        assert default["tasks"][0]["rationale"] == "The route is now known."
        assert default["tasks"][0]["object_id"] == 17
        assert default["tasks"][0]["transition_key"] == "S1/O17"
        assert default["tasks"][0]["room_id"] == "S3/M99:000000000001"
        assert default["tasks"][0]["created_at"] is not None
        assert default["tasks"][0]["updated_at"] is not None
        assert default["tasks"][0]["history"]

        all_tasks = debug.run_agent_tool("get_tasks", {"include_closed": True, "limit": 1})
        assert all_tasks["include_closed"] is True
        assert all_tasks["returned"] == 1
        assert all_tasks["total"] == 2
        assert all_tasks["tasks"][0]["id"] == "closed-task"


def test_active_task_context_is_sorted_bounded_and_compact():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        for priority in range(10):
            debug.run_agent_tool("upsert_task", {
                "id": f"task-{priority}",
                "description": ("x" * 220) if priority == 9 else f"Task {priority}",
                "rationale": f"Reason {priority}",
                "priority": priority,
                "object_id": priority + 1,
            })

        context = debug.active_task_context()

        assert context["active_tasks_total"] == 10
        assert len(context["active_tasks"]) == 8
        assert [task["priority"] for task in context["active_tasks"]] == list(range(9, 1, -1))
        assert len(context["active_tasks"][0]["description"]) == 160
        assert context["active_tasks"][0]["description"].endswith("…")
        assert set(context["active_tasks"][0]) == {
            "id", "description", "priority", "object_id", "transition_key", "room_id",
        }
        assert "rationale" not in context["active_tasks"][0]
        assert "history" not in context["active_tasks"][0]
        assert "updated_at" not in context["active_tasks"][0]


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
        assert debug.run_agent_tool("get_tasks")["tasks"][0]["rationale"] == "It is the next stated destination."
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
