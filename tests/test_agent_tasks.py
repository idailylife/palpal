from unittest import mock

import pytest

from tests.support import create_agent_task, make_checkpoint_state, make_debug_state, make_object, make_state, write_state


def review_state(*, sprite=2, cash=10, inventory=None):
    state = make_checkpoint_state()
    state.update({
        "cash": cash,
        "inventory": inventory or [],
        "party": [{"role_id": 0, "name": "李逍遙", "scene_sprite_id": sprite}],
        "objects": [],
        "dialog": {"active": False},
    })
    return state


def change_task_status(debug, task_id, status, state=None, *, reason="Test evidence confirms the lifecycle change."):
    debug.event_log.append({"time": 10_000.0 + len(debug.event_log), "type": "test_task_status_evidence"})
    return debug.run_agent_tool("upsert_task", {
        "id": task_id,
        "status": status,
        "status_reason": reason,
        "status_evidence": [f"event:{len(debug.event_log) - 1}"],
    }, state)


def reconciliation_fields(ref, *, dormant=False):
    return {
        "description": "Investigate the current lead",
        "rationale": "The latest evidence supports the current stage.",
        "reconciliation_review": {
            "stage_assessment": "The lead is unresolved; the earlier stage has ended.",
            "execution_basis": "" if dormant else "The observed doorway provides a route to investigate the lead.",
            "resume_condition": "Resume when a route to the lead is observed." if dormant else "",
            "evidence_refs": [ref],
        },
    }


def test_task_ledger_create_update_close_and_filtering(tmp_path):
    debug = make_debug_state(tmp_path)
    created = create_agent_task(debug, {
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
    completion_state = review_state()
    completion_state.update({"logical_frame": 321, "raw_frame": 123})
    updated = change_task_status(debug, task_id, "completed", completion_state)
    assert updated["ok"]
    assert updated["operation"] == "updated"
    assert updated["task"]["history"][-1]["changes"]["status"]["to"] == "completed"
    assert updated["task"]["completed_at"] is not None
    assert updated["task"]["completed_frame"] == 321
    assert updated["task"]["completed_raw_frame"] == 123
    assert updated["task"]["completed_agent_turn"] == 0
    assert debug.task_ledger() == []
    history = debug.task_ledger(include_closed=True)
    assert history[0]["id"] == task_id
    assert not debug.run_agent_tool("upsert_task", {"description": "missing reason"})["ok"]


def test_projected_merged_dialog_reference_completes_task_without_reconstructing_frames(tmp_path):
    debug = make_debug_state(tmp_path)
    create_agent_task(debug, {"id": "rescue", "description": "Rescue", "rationale": "Missing person."})
    entry = {"source_key": "dialog:77:24:763427:5:5053:爹～您先醫治李少俠",
             "raw_frame": 763427, "logical_frame": 1661753,
             "message_ids": [5053, 5054], "text": "爹～您先醫治李少俠", "time": 100}
    debug.dialog_history.append(entry)
    projected = debug.project_agent_observations({"ok": True, "dialogs": [entry]})
    evidence = dict(zip(projected["dialog_fields"], projected["dialogs"][0]))
    args = {"id": "rescue", "status": "completed", "status_reason": "Person returned.",
            "status_evidence": [evidence["source_key"].replace("763427", "1661753")]}
    before = debug.read_exploration()
    failed = debug.run_agent_tool("upsert_task", args)
    assert failed["error"]["code"] == "invalid_status_evidence"
    assert failed["error"]["missing_references"] == args["status_evidence"]
    assert debug.read_exploration() == before
    args["status_evidence"] = [evidence["source_key"]]
    assert debug.run_agent_tool("upsert_task", args)["task"]["status"] == "completed"


def test_six_task_reconciliation_and_forty_inbox_recovery(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[make_object(51, sprite_id=0, trigger_mode=5)])
    state["dialog"] = {"active": False}
    write_state(debug, state)
    for index in range(6):
        assert create_agent_task(debug, {
            "id": f"task-{index}", "description": f"Task {index}", "rationale": "Follow up.",
        })["ok"]
    debug.record_task_inbox_entries([
        inbox_dialog(source_key=f"evidence-{index}", text=f"Progress {index}") for index in range(40)
    ])
    pending = [record["id"] for record in debug.task_inbox_pending()]
    assert len(pending) == 40
    debug.agent_runtime.mark_task_reconciliation_pending("context_compacted")
    cycle = debug.new_agent_tool_cycle(state)
    audit = debug.run_agent_tool("get_tasks", {
        "include_closed": True, "detail": "summary", "limit": 100,
    }, state, tool_cycle=cycle)
    before = debug.read_exploration()
    failed = debug.run_agent_tool("upsert_task", {
        "id": "task-0", "review_token": "wrong-token",
    }, state, tool_cycle=cycle)
    assert failed["error"]["code"] == "task_reconciliation_required"
    assert debug.read_exploration() == before
    for index in range(6):
        args = {"id": f"task-{index}", "review_token": audit["review_token"]}
        if index == 0:
            args.update(status="completed", status_reason="Rescue succeeded.", status_evidence=[pending[0]])
        else:
            args.update(reconciliation_fields(pending[index]))
        assert debug.run_agent_tool("upsert_task", args, state, tool_cycle=cycle)["ok"]
    assert not debug.agent_runtime.task_reconciliation_pending()
    blocked = debug.run_agent_tool("submit_decision", {
        "action": "navigate", "target_id": 51, "reason": "Continue.",
    }, state, tool_cycle=cycle)
    assert blocked["error"]["code"] == "task_inbox_review_required"
    for offset in range(0, 40, 3):
        batch = pending[offset:offset + 3]
        resolutions = {"resolutions": [
            {"inbox_id": inbox_id, "outcome": "task_updated", "task_ids": ["task-0", "task-1"],
             "rationale": "Rescue and next task progress."} for inbox_id in batch
        ]}
        before = debug.read_exploration()
        failed = debug.run_agent_tool("review_task_inbox", resolutions, state, tool_cycle=cycle)
        assert failed["missing_inbox_links"] == [
            {"inbox_id": inbox_id, "task_id": task_id}
            for inbox_id in batch for task_id in ("task-0", "task-1")
        ]
        assert debug.read_exploration() == before
        for task_id in ("task-0", "task-1"):
            assert debug.run_agent_tool("upsert_task", {
                "id": task_id, "source_inbox_ids": batch,
            }, state, tool_cycle=cycle)["ok"]
        assert debug.run_agent_tool("review_task_inbox", resolutions, state, tool_cycle=cycle)["ok"]
    assert debug.task_inbox_pending() == []
    tasks = debug.read_exploration()["tasks"]
    assert tasks["task-0"]["status"] == "completed"
    assert set(tasks["task-0"]["provenance"]["task_inbox_batch_ids"]) == set(pending)
    accepted = debug.run_agent_tool("submit_decision", {
        "action": "navigate", "target_id": 51, "reason": "Continue.",
    }, state, tool_cycle=cycle)
    assert accepted["ok"]
    assert accepted["decision"].get("validation", {}).get("status") != "rejected"
    assert accepted["decision"]["recommended_action"] == {
        "tool": "navigation_step", "args": {"id": 51}, "dry_run": True,
    }


def test_completed_tasks_return_most_recent_completion_first_and_recompletion_refreshes_frame(tmp_path):
    debug = make_debug_state(tmp_path)
    older = create_agent_task(debug, {
        "id": "older-completion",
        "description": "Older completion",
        "rationale": "This finishes first.",
        "priority": 100,
    })["task"]
    newer = create_agent_task(debug, {
        "id": "newer-completion",
        "description": "Newer completion",
        "rationale": "This finishes second.",
        "priority": 0,
    })["task"]

    change_task_status(debug, older["id"], "completed", {"frame": 10, "logical_frame": 110, "raw_frame": 10})
    change_task_status(debug, newer["id"], "completed", {"frame": 20, "logical_frame": 120, "raw_frame": 20})

    completed = debug.run_agent_tool(
        "get_tasks", {"include_closed": True, "detail": "summary", "limit": 100},
    )["tasks"]
    assert [task["id"] for task in completed] == [newer["id"], older["id"]]
    assert [task["completed_frame"] for task in completed] == [120, 110]
    assert [task["id"] for task in debug.agent_tasks_view()["tasks"]] == [newer["id"], older["id"]]

    reopened = change_task_status(debug, older["id"], "active")["task"]
    assert reopened["completed_at"] is None
    assert reopened["completed_frame"] is None
    recompleted = change_task_status(
        debug, older["id"], "completed", {"frame": 30, "logical_frame": 130, "raw_frame": 30},
    )["task"]
    assert recompleted["completed_frame"] == 130
    assert [
        task["id"] for task in debug.agent_tasks_view()["tasks"]
    ] == [older["id"], newer["id"]]


def test_task_upsert_accepts_stable_custom_id_and_does_not_duplicate_on_update(tmp_path):
    debug = make_debug_state(tmp_path)
    created = create_agent_task(debug, {
        "id": "buy_shrimp",
        "description": "幫李大娘買新鮮蝦：先去菜市場，買不到再找漁船家。已獲五十文。",
        "status": "active",
        "priority": 5,
        "rationale": "李大娘委託，給50文買鮮蝦。",
    })

    assert created["ok"] is True
    assert created["operation"] == "created"
    assert created["task"]["id"] == "buy_shrimp"

    cycle = debug.new_agent_tool_cycle()
    debug.run_agent_tool(
        "get_room_memory", {"room_id": "S3/M99:000000000001"}, tool_cycle=cycle,
    )
    updated = debug.run_agent_tool("upsert_task", {
        "id": "buy_shrimp",
        "priority": 8,
        "room_id": "S3/M99:000000000001",
    }, tool_cycle=cycle)
    assert updated["operation"] == "updated"
    assert updated["task"]["priority"] == 8
    assert list(debug.read_exploration()["tasks"]) == ["buy_shrimp"]


def test_task_room_binding_requires_same_cycle_room_memory_review(tmp_path):
    debug = make_debug_state(tmp_path)
    task = create_agent_task(debug, {
        "id": "find-inn", "description": "Find the inn", "rationale": "Need a room.",
    })["task"]
    room_id = "S3/M10:000000000001"
    baseline = debug.read_exploration()["tasks"][task["id"]].copy()

    cycle = debug.new_agent_tool_cycle()
    blocked = debug.run_agent_tool(
        "upsert_task", {"id": task["id"], "room_id": room_id}, tool_cycle=cycle,
    )
    assert blocked["error"]["code"] == "room_memory_review_required"
    assert blocked["error"]["room_id"] == room_id
    assert debug.read_exploration()["tasks"][task["id"]] == baseline

    debug.run_agent_tool("get_room_memory", {"room_id": room_id}, tool_cycle=cycle)
    bound = debug.run_agent_tool(
        "upsert_task", {"id": task["id"], "room_id": room_id}, tool_cycle=cycle,
    )
    assert bound["ok"] is True
    unchanged = debug.run_agent_tool(
        "upsert_task", {"id": task["id"], "room_id": room_id},
        tool_cycle=debug.new_agent_tool_cycle(),
    )
    assert unchanged["ok"] is True
    cleared = debug.run_agent_tool(
        "upsert_task", {"id": task["id"], "room_id": None},
        tool_cycle=debug.new_agent_tool_cycle(),
    )
    assert cleared["ok"] is True

    no_room = create_agent_task(debug, {
        "id": "unbound", "description": "Keep looking", "rationale": "No room evidence.",
    })
    assert no_room["ok"] is True

    creation_cycle = debug.new_agent_tool_cycle()
    audit = debug.run_agent_tool(
        "get_tasks", {"include_closed": True, "detail": "summary", "limit": 100},
        tool_cycle=creation_cycle,
    )
    blocked_creation = debug.run_agent_tool("upsert_task", {
        "id": "new-bound", "description": "Go there", "rationale": "Candidate location.",
        "room_id": room_id, "review_token": audit["review_token"],
    }, tool_cycle=creation_cycle)
    assert blocked_creation["error"]["code"] == "room_memory_review_required"
    assert "new-bound" not in debug.read_exploration()["tasks"]


def test_unchanged_upsert_explicitly_reviews_task_and_refreshes_snapshot(tmp_path):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    created = create_agent_task(debug, {
        "id": "keep-going", "description": "Keep going", "rationale": "Current evidence.",
        "review_watch": {"cash": True},
    })["task"]

    state["cash"] = 15
    write_state(debug, state)
    assert debug.task_review_context(state)["required"] is True
    reviewed = debug.run_agent_tool("upsert_task", {"id": "keep-going"})

    assert reviewed["operation"] == "updated"
    assert reviewed["task"]["last_reviewed_at"] >= created["last_reviewed_at"]
    assert reviewed["task"]["last_reviewed_agent_turn"] == 0
    assert reviewed["task"]["history"] == created["history"]
    assert reviewed["task"]["rationale"] == "Current evidence."
    assert debug.task_review_context(state) == {"required": False, "tasks": []}


def test_unknown_custom_task_id_requires_complete_creation_fields_without_writing(tmp_path):
    debug = make_debug_state(tmp_path)

    result = debug.run_agent_tool("upsert_task", {
        "id": "buy_shrimp",
        "description": "Buy shrimp",
    })

    assert result["ok"] is False
    assert result["error"]["code"] == "task_creation_review_required"
    assert debug.read_exploration()["tasks"] == {}


def test_get_tasks_defaults_to_active_and_can_include_closed_limit_and_full_fields(tmp_path):
    debug = make_debug_state(tmp_path)
    active = create_agent_task(debug, {
        "id": "active-task",
        "description": "Reach the courtyard",
        "rationale": "The route is now known.",
        "priority": 4,
        "object_id": 17,
        "transition_key": "S1/O17",
        "room_id": "S3/M99:000000000001",
    })["task"]
    closed = create_agent_task(debug, {
        "id": "closed-task",
        "description": "Old errand",
        "rationale": "It was already handled.",
        "priority": 9,
    })["task"]
    change_task_status(debug, closed["id"], "completed")

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


def test_new_task_creation_requires_complete_fresh_closed_summary_review(tmp_path):
    debug = make_debug_state(tmp_path)
    baseline = debug.read_exploration()

    missing = debug.run_agent_tool("upsert_task", {
        "id": "new-clue", "description": "Investigate clue", "rationale": "It affects later action.",
    })
    assert missing["error"]["code"] == "task_creation_review_required"
    assert debug.read_exploration()["tasks"] == baseline["tasks"]
    without_id = debug.run_agent_tool("upsert_task", {
        "description": "Investigate another clue", "rationale": "It also affects later action.",
    })
    assert without_id["error"]["code"] == "task_creation_review_required"

    active_only_cycle = debug.new_agent_tool_cycle()
    debug.run_agent_tool("get_tasks", {}, tool_cycle=active_only_cycle)
    active_only = debug.run_agent_tool("upsert_task", {
        "id": "new-clue", "description": "Investigate clue", "rationale": "It affects later action.",
    }, tool_cycle=active_only_cycle)
    assert active_only["error"]["code"] == "task_creation_review_required"

    cycle = debug.new_agent_tool_cycle()
    first_page = debug.run_agent_tool("get_tasks", {
        "include_closed": True, "detail": "summary", "limit": 1,
    }, tool_cycle=cycle)
    assert first_page["complete"] is True
    created = debug.run_agent_tool("upsert_task", {
        "id": "new-clue", "description": "Investigate clue", "rationale": "It affects later action.",
        "review_token": first_page["review_token"],
    }, tool_cycle=cycle)
    assert created["operation"] == "created"


def test_task_summary_pagination_invalidates_when_revision_changes(tmp_path):
    debug = make_debug_state(tmp_path)
    first = create_agent_task(debug, {
        "id": "first", "description": "First", "rationale": "First evidence.",
    })["task"]
    create_agent_task(debug, {
        "id": "second", "description": "Second", "rationale": "Second evidence.",
    })
    cycle = debug.new_agent_tool_cycle()
    page = debug.run_agent_tool("get_tasks", {
        "include_closed": True, "detail": "summary", "limit": 1,
    }, tool_cycle=cycle)
    assert page["complete"] is False
    assert "history" not in page["tasks"][0]
    assert "review_token" not in page
    before = debug.read_exploration()["tasks"].copy()
    incomplete = debug.run_agent_tool("upsert_task", {
        "id": "third", "description": "Third", "rationale": "Third evidence.",
    }, tool_cycle=cycle)
    assert incomplete["error"]["code"] == "task_creation_review_required"
    assert debug.read_exploration()["tasks"] == before

    debug.run_agent_tool("upsert_task", {"id": first["id"], "priority": 3})
    stale = debug.run_agent_tool("get_tasks", {
        "include_closed": True, "detail": "summary", "offset": page["next_offset"], "limit": 1,
    }, tool_cycle=cycle)
    assert stale["audit_valid"] is False

    cycle = debug.new_agent_tool_cycle()
    offset = 0
    while True:
        page = debug.run_agent_tool("get_tasks", {
            "include_closed": True, "detail": "summary", "offset": offset, "limit": 1,
        }, tool_cycle=cycle)
        if page["complete"]:
            break
        offset = page["next_offset"]
    assert page["audit_valid"] is True
    assert page["review_token"]


def test_reconciliation_preserves_completed_and_abandons_duplicate_before_movement(tmp_path):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    medicine = create_agent_task(debug, {
        "id": "seek-medicine", "description": "求药", "rationale": "已经取得药物。",
    })["task"]
    change_task_status(debug, medicine["id"], "completed")
    create_agent_task(debug, {
        "id": "seek-medicine-duplicate", "description": "再次求药", "rationale": "压缩摘要遗漏了完成状态。",
    })
    debug.agent_runtime.mark_task_reconciliation_pending("context_compacted")
    cycle = debug.new_agent_tool_cycle(state)

    blocked = debug.run_agent_tool(
        "submit_decision", {"action": "navigate", "target_id": 63, "reason": "Move."},
        state, tool_cycle=cycle,
    )
    assert blocked["error"]["code"] == "task_reconciliation_required"
    audited = debug.run_agent_tool("get_tasks", {
        "include_closed": True, "detail": "summary", "limit": 100,
    }, state, tool_cycle=cycle)
    assert {task["id"] for task in audited["tasks"]} == {"seek-medicine", "seek-medicine-duplicate"}
    assert next(task for task in audited["tasks"] if task["id"] == "seek-medicine")["status"] == "completed"
    duplicate = debug.run_agent_tool("upsert_task", {
        "id": "seek-medicine-duplicate", "status": "abandoned",
        "rationale": "Duplicates completed predecessor seek-medicine.",
        "review_token": audited["review_token"],
        "status_reason": "The completed predecessor proves this is a duplicate.",
        "status_evidence": ["event:0"],
    }, state, tool_cycle=cycle)
    assert duplicate["ok"] is True
    assert debug.agent_runtime.task_reconciliation_pending() is False
    accepted = debug.run_agent_tool(
        "submit_decision", {"action": "advance_dialog", "reason": "Continue available dialogue."},
        state, tool_cycle=cycle,
    )
    assert accepted["ok"] is True
    assert debug.agent_runtime.task_reconciliation_pending() is False
    tasks = {task["id"]: task for task in debug.task_ledger(include_closed=True, limit=None)}
    assert tasks["seek-medicine"]["status"] == "completed"
    assert tasks["seek-medicine-duplicate"]["status"] == "abandoned"


def test_reconciliation_clears_after_audit_and_allows_inbox_derived_task(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    monkeypatch.setattr(debug, "current_agent_turn", lambda: 12)
    create_agent_task(debug, {
        "id": "existing-task", "description": "Existing task", "rationale": "Restore it.",
    })
    debug.record_task_inbox_entries([inbox_dialog(turn=12, source_key="new-lead")])
    inbox_id = debug.task_inbox_pending()[0]["id"]
    debug.agent_runtime.mark_task_reconciliation_pending("checkpoint_restored")
    cycle = debug.new_agent_tool_cycle(state)

    audit = debug.run_agent_tool(
        "get_tasks", {"include_closed": True, "detail": "summary"}, state, tool_cycle=cycle,
    )
    assert debug.agent_runtime.task_reconciliation_pending() is True
    confirmed = debug.run_agent_tool("upsert_task", {
        "id": "existing-task", "review_token": audit["review_token"],
        **reconciliation_fields(inbox_id),
    }, state, tool_cycle=cycle)
    assert confirmed["ok"] is True
    assert debug.agent_runtime.task_reconciliation_pending() is False

    created = debug.run_agent_tool("upsert_task", {
        "id": "new-lead-task",
        "description": "Investigate the new lead",
        "rationale": "The pending dialog introduced a distinct follow-up.",
        "review_token": audit["review_token"],
        "source_inbox_ids": [inbox_id],
    }, state, tool_cycle=cycle)
    assert created["ok"] is True


def test_reconciliation_with_no_active_tasks_clears_after_complete_audit(tmp_path):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    debug.agent_runtime.mark_task_reconciliation_pending("checkpoint_restored")
    cycle = debug.new_agent_tool_cycle(state)

    audit = debug.run_agent_tool(
        "get_tasks", {"include_closed": True, "detail": "summary"}, state, tool_cycle=cycle,
    )

    assert audit["audit_valid"] is True
    assert debug.agent_runtime.task_reconciliation_pending() is False


def test_active_task_context_is_sorted_bounded_and_compact(tmp_path):
    debug = make_debug_state(tmp_path)
    for priority in range(10):
        create_agent_task(debug, {
            "id": f"task-{priority}",
            "description": ("x" * 220) if priority == 9 else f"Task {priority}",
            "rationale": f"Reason {priority}",
            "priority": priority,
            "object_id": priority + 1,
        })

    context = debug.active_task_context()

    assert context["active_tasks_total"] == 10
    assert context["task_review"] == {"required": False, "tasks": []}
    assert len(context["active_tasks"]) == 8
    assert [task["priority"] for task in context["active_tasks"]] == list(range(9, 1, -1))
    assert len(context["active_tasks"][0]["description"]) == 160
    assert context["active_tasks"][0]["description"].endswith("…")
    assert set(context["active_tasks"][0]) == {
        "id", "description", "rationale", "priority", "object_id", "transition_key", "room_id",
    }
    assert context["active_tasks"][0]["rationale"] == "Reason 9"
    assert "history" not in context["active_tasks"][0]
    assert "updated_at" not in context["active_tasks"][0]


def test_legacy_task_context_includes_latest_rationale_without_rewriting_ledger(tmp_path):
    debug = make_debug_state(tmp_path)
    create_agent_task(debug, {
        "id": "thief", "description": "Old phase; no executable clues.",
        "rationale": "Find the stolen goods.",
    })
    latest = "Moon pointed out suspicious activity at the well. " + "Investigate safely. " * 20
    debug.run_agent_tool("upsert_task", {"id": "thief", "rationale": latest})
    before = debug.exploration_path.read_bytes()

    context = debug.active_task_context()["active_tasks"][0]

    assert context["description"] == "Old phase; no executable clues."
    assert context["rationale"] == latest[:159] + "…"
    assert "progress" not in context  # Never promote legacy reasoning into evidence.
    assert debug.exploration_path.read_bytes() == before


def test_task_progress_keeps_observation_hypothesis_and_next_step_separate(tmp_path):
    debug = make_debug_state(tmp_path)
    debug.dialog_history.append({"source_key": "well-clue", "text": "井裡頭一定有古怪！", "time": 100})
    created = create_agent_task(debug, {
        "id": "thief", "description": "Investigate the well to find the thief.",
        "rationale": "A new clue needs verification.",
        "progress": {
            "evidence_refs": ["well-clue"],
            "hypotheses": ["The old passage might lead to the well; unverified."],
            "next_step": "Inspect the well objects before choosing an entrance.",
        },
    })
    assert created["ok"]
    progress = created["task"]["progress"]
    assert progress["evidence"] == [{
        "ref": "well-clue", "kind": "dialog", "observed_at": 100,
        "summary": "井裡頭一定有古怪！",
    }]
    assert "passage" not in str(progress["evidence"])
    assert created["task"]["origin_evidence"] == progress["evidence"]
    assert debug.active_task_context()["active_tasks"][0]["progress"] == progress
    assert "rationale" not in debug.active_task_context()["active_tasks"][0]
    assert debug.run_agent_tool("get_tasks", {"detail": "summary"})["tasks"][0]["progress"] == progress
    checkpoint = tmp_path / "progress-checkpoint"
    debug.copy_companion_to_checkpoint(checkpoint)

    # Evidence excerpts survive restart/pruning and an ordinary no-op review.
    reloaded = make_debug_state(tmp_path)
    before = reloaded.read_exploration()
    reviewed = reloaded.run_agent_tool("upsert_task", {"id": "thief"})
    assert reviewed["task"]["progress"] == progress
    assert reloaded.read_exploration()["tasks_revision"] == before["tasks_revision"]
    assert reviewed["task"]["history"] == before["tasks"]["thief"]["history"]
    updated = reloaded.run_agent_tool("upsert_task", {
        "id": "thief", "description": "Investigate the well to find the thief.",
        "progress": {
            "evidence_refs": ["well-clue"], "hypotheses": [],
            "next_step": "Verify another route; the old passage is not confirmed as the entrance.",
        },
    })
    assert updated["ok"]
    assert updated["task"]["progress"]["evidence"] == progress["evidence"]
    assert updated["task"]["progress"]["hypotheses"] == []
    assert reloaded.read_exploration()["tasks_revision"] == before["tasks_revision"] + 1
    reloaded.restore_companion_from_checkpoint(checkpoint)
    assert reloaded.active_task_context()["active_tasks"][0]["progress"] == progress
    assert reloaded.read_exploration()["tasks_revision"] == before["tasks_revision"]


@pytest.mark.parametrize("progress", [
    {"evidence_refs": ["imaginary-transition"], "hypotheses": [], "next_step": "Check."},
    {"evidence_refs": [], "hypotheses": [], "next_step": "x" * 161},
    {"evidence_refs": [], "hypotheses": ["x" * 161], "next_step": "Check."},
    {"evidence_refs": [], "hypotheses": ["A", "B", "C", "D"], "next_step": "Check."},
    {"evidence_refs": "invented", "hypotheses": [], "next_step": "Check."},
    {"evidence_refs": [], "hypotheses": [], "next_step": "Check.", "facts": ["This is the well."]},
])
def test_invalid_task_progress_is_rejected_before_task_or_revision_changes(tmp_path, progress):
    debug = make_debug_state(tmp_path)
    create_agent_task(debug, {"id": "thief", "description": "Find thief.", "rationale": "Clue."})
    before = debug.exploration_path.read_bytes()
    result = debug.run_agent_tool("upsert_task", {
        "id": "thief", "description": "Changed goal.", "progress": progress,
    })
    assert result["ok"] is False
    assert debug.exploration_path.read_bytes() == before


def test_task_progress_requires_current_goal_and_is_invalidated_by_legacy_narrative_update(tmp_path):
    debug = make_debug_state(tmp_path)
    create_agent_task(debug, {"id": "thief", "description": "Find thief.", "rationale": "Clue."})
    progress = {"evidence_refs": [], "hypotheses": ["Possible entrance."], "next_step": "Verify entrance."}
    before = debug.exploration_path.read_bytes()
    assert not debug.run_agent_tool("upsert_task", {"id": "thief", "progress": progress})["ok"]
    assert debug.exploration_path.read_bytes() == before
    assert debug.run_agent_tool("upsert_task", {
        "id": "thief", "description": "Verify suspected entrance.", "progress": progress,
    })["ok"]
    updated = debug.run_agent_tool("upsert_task", {"id": "thief", "rationale": "Entrance disproven; search elsewhere."})
    assert updated["ok"]
    assert updated["task"]["progress"] is None
    context = debug.active_task_context()["active_tasks"][0]
    assert "progress" not in context
    assert context["rationale"] == "Entrance disproven; search elsewhere."
    assert updated["task"]["history"][-1]["changes"]["progress"]["to"] is None


def test_task_review_matches_object_dialog_and_events_but_not_unrelated_objects(tmp_path):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    task = create_agent_task(debug, {
        "id": "object-task", "description": "Talk to object", "rationale": "NPC request.", "object_id": 63,
    })["task"]
    reviewed_at = task["last_reviewed_at"]
    debug.dialog_history = [{"time": reviewed_at + 1, "event_object_id": 99, "text": "unrelated"}]
    debug.event_log = [{"time": reviewed_at + 1, "type": "object_changed", "object_id": 99}]
    assert debug.task_review_context(state)["required"] is False

    debug.dialog_history.append({"time": reviewed_at + 2, "event_object_id": 63, "text": "new evidence"})
    review = debug.task_review_context(state)
    assert review["required"] is True
    assert review["tasks"][0]["id"] == "object-task"
    assert {reason["kind"] for reason in review["tasks"][0]["reasons"]} == {"dialog"}

    debug.run_agent_tool("upsert_task", {"id": "object-task"})
    new_reviewed_at = debug.task_ledger()[0]["last_reviewed_at"]
    debug.dialog_history = []
    debug.event_log.append({
        "time": new_reviewed_at + 1, "type": "interaction_no_effect", "target_id": 63,
    })
    kinds = {reason["kind"] for reason in debug.task_review_context(state)["tasks"][0]["reasons"]}
    assert kinds == {"interaction_no_effect"}


def test_task_review_detects_transition_and_stable_net_resource_changes(tmp_path):
    debug = make_debug_state(tmp_path)
    state = review_state(inventory=[{"item_id": 7, "amount": 1, "amount_in_use": 0}])
    write_state(debug, state)
    task = create_agent_task(debug, {
        "id": "travel-task", "description": "Reach upstairs", "rationale": "Known delivery.",
        "transition_key": "stairs", "room_id": "room-upstairs",
        "review_watch": {"cash": True, "inventory_item_ids": [7]},
    })["task"]
    data = debug.read_exploration()
    data["transitions"] = {"room-lobby": {"stairs": {
        "transition_key": "stairs", "from_room_id": "room-lobby", "to_room_id": "room-upstairs",
        "observed_at": task["last_reviewed_at"] + 1,
    }}}
    debug.write_exploration(data)
    state["cash"] = 15
    state["inventory"][0]["amount"] = 2
    write_state(debug, state)

    reasons = debug.task_review_context(state)["tasks"][0]["reasons"]
    assert {reason["kind"] for reason in reasons} == {
        "transition", "cash_net_changed", "inventory_net_changed",
    }


def test_resource_review_only_notifies_exact_subscriptions(tmp_path):
    debug = make_debug_state(tmp_path)
    before = review_state(inventory=[{"item_id": 7, "amount": 1}, {"item_id": 8, "amount": 1}])
    write_state(debug, before)
    create_agent_task(debug, {
        "id": "unwatched", "description": "Unrelated errand", "rationale": "Narrative evidence only.",
    })
    create_agent_task(debug, {
        "id": "cash-watch", "description": "Save money", "rationale": "Exact cash dependency.",
        "review_watch": {"cash": True},
    })
    create_agent_task(debug, {
        "id": "item-watch", "description": "Use item 7", "rationale": "Exact item dependency.",
        "review_watch": {"inventory_item_ids": [7]},
    })
    after = review_state(cash=20, inventory=[{"item_id": 7, "amount": 2}, {"item_id": 8, "amount": 9}])
    reviews = {item["id"]: item for item in debug.task_review_context(after)["tasks"]}
    assert set(reviews) == {"cash-watch", "item-watch"}
    assert {reason["kind"] for reason in reviews["cash-watch"]["reasons"]} == {"cash_net_changed"}
    assert reviews["item-watch"]["reasons"] == [{"kind": "inventory_net_changed", "item_ids": ["7"]}]


def test_task_status_changes_require_current_evidence_and_dormant_is_reversible(tmp_path):
    debug = make_debug_state(tmp_path)
    task = create_agent_task(debug, {
        "id": "buy-shrimp", "description": "Buy shrimp", "rationale": "The cook requested it.",
    })["task"]
    rejected = debug.run_agent_tool("upsert_task", {"id": task["id"], "status": "dormant"})
    assert rejected["ok"] is False
    assert "status_reason" in rejected["error"]
    debug.dialog_history = [{"time": 101.0, "source_key": "market-no-stock", "text": "今天没有鲜虾。"}]
    dormant = debug.run_agent_tool("upsert_task", {
        "id": task["id"], "status": "dormant", "status_reason": "No supplier currently has stock.",
        "status_evidence": ["market-no-stock"],
    })["task"]
    assert dormant["status"] == "dormant"
    assert dormant["dormant_at"] is not None
    assert debug.run_agent_tool("get_tasks")["tasks"] == []
    assert debug.run_agent_tool("get_tasks", {"include_closed": True})["tasks"][0]["status"] == "dormant"
    debug.dialog_history.append({"time": 102.0, "source_key": "new-stock", "text": "刚到了一批鲜虾。"})
    active = debug.run_agent_tool("upsert_task", {
        "id": task["id"], "status": "active", "status_reason": "Supply is available again.",
        "status_evidence": ["new-stock"],
    })["task"]
    assert active["status"] == "active"
    assert active["dormant_at"] is None
    assert active["origin_evidence"] == []
    assert [item["ref"] for item in active["recent_evidence"]] == ["market-no-stock", "new-stock"]


def test_audited_four_task_fixture_keeps_only_executable_story_task_active(tmp_path):
    debug = make_debug_state(tmp_path)
    tasks = {
        "task-borrow-boat-xianling": "Borrow a boat while following the Hidden Dragon Cave story",
        "buy-shrimp": "Buy shrimp",
        "visit-fair": "Visit the temple fair",
        "find-nest-tomorrow": "Find the bird nest tomorrow",
    }
    for task_id, description in tasks.items():
        create_agent_task(debug, {"id": task_id, "description": description, "rationale": "Visible dialog established it."})
    completed = create_agent_task(debug, {
        "id": "already-completed", "description": "Finished errand", "rationale": "Native result confirmed it.",
    })["task"]
    debug.dialog_history = [
        {"time": 101.0, "source_key": "no-shrimp", "text": "市场和船家都没有鲜虾。"},
        {"time": 102.0, "source_key": "fair-unclear", "text": "已经到了苏州。"},
        {"time": 103.0, "source_key": "nest-expired", "text": "已经过夜，再见王小虎时谈的是新话题。"},
        {"time": 104.0, "source_key": "completed-proof", "text": "委托已经办妥。"},
    ]
    change_task_status(debug, completed["id"], "completed")
    for task_id, status, evidence in (
        ("buy-shrimp", "dormant", "no-shrimp"),
        ("visit-fair", "dormant", "fair-unclear"),
        ("find-nest-tomorrow", "abandoned", "nest-expired"),
    ):
        result = debug.run_agent_tool("upsert_task", {
            "id": task_id, "status": status, "status_reason": "Fixture lifecycle evidence.",
            "status_evidence": [evidence],
        })
        assert result["ok"] is True
    default_ids = [task["id"] for task in debug.run_agent_tool("get_tasks")["tasks"]]
    assert default_ids == ["task-borrow-boat-xianling"]
    all_statuses = {task["id"]: task["status"] for task in debug.run_agent_tool(
        "get_tasks", {"include_closed": True, "limit": 100},
    )["tasks"]}
    assert all_statuses == {
        "task-borrow-boat-xianling": "active", "buy-shrimp": "dormant",
        "visit-fair": "dormant", "find-nest-tomorrow": "abandoned",
        "already-completed": "completed",
    }


def test_unchanged_review_preserves_rationale_and_evidence_survives_inbox_pruning(tmp_path):
    debug = make_debug_state(tmp_path)
    debug.record_task_inbox_entries([inbox_dialog(source_key="origin-dialog")])
    inbox_id = debug.task_inbox_pending()[0]["id"]
    task = create_agent_task(debug, {
        "id": "origin-task", "description": "Follow the request", "rationale": "Original rationale.",
        "source_inbox_ids": [inbox_id],
    })["task"]
    unchanged = debug.run_agent_tool("upsert_task", {"id": task["id"]})["task"]
    assert unchanged["rationale"] == "Original rationale."
    assert unchanged["origin_evidence"][0]["ref"] == inbox_id
    data = debug.read_exploration()
    data["task_inbox"] = {}
    debug.write_exploration(data)
    persisted = debug.run_agent_tool("get_tasks")["tasks"][0]
    assert persisted["origin_evidence"][0]["ref"] == inbox_id


def test_task_review_uses_net_sprite_state_and_ignores_recovered_animation(tmp_path):
    debug = make_debug_state(tmp_path)
    state = review_state(sprite=2)
    write_state(debug, state)
    create_agent_task(debug, {
        "id": "delivery", "description": "Carry the delivery", "rationale": "Dialog established it.",
        "review_watch": {"party_role_ids": [0]},
    })

    transient = review_state(sprite=193)
    debug.record_state_events(transient)
    recovered = review_state(sprite=2)
    debug.record_state_events(recovered)
    assert debug.task_review_context(recovered)["required"] is False

    persistent = review_state(sprite=208)
    write_state(debug, persistent)
    review = debug.task_review_context(persistent)
    sprite_reason = next(reason for reason in review["tasks"][0]["reasons"] if reason["kind"] == "party_sprite_net_changed")
    assert sprite_reason["roles"] == [{"role_id": "0", "from": 2, "to": 208}]


def test_pre_snapshot_task_reconstructs_stable_sprite_net_from_events(tmp_path):
    debug = make_debug_state(tmp_path)
    state = review_state(sprite=208)
    write_state(debug, state)
    reviewed_at = 100.0
    data = debug.empty_exploration()
    data["tasks"]["legacy-task"] = {
        "id": "legacy-task", "description": "Old task", "rationale": "Old evidence.",
        "priority": 1, "status": "active", "created_at": reviewed_at,
        "updated_at": reviewed_at, "updated_agent_turn": 2, "history": [],
        "review_watch": {"party_role_ids": [0]},
    }
    debug.write_exploration(data)
    debug.event_log = [{
        "time": 101.0, "type": "party_sprite_changed", "role_id": 0,
        "from_scene_sprite_id": 2, "to_scene_sprite_id": 208,
    }]

    task = debug.task_ledger()[0]
    assert task["last_reviewed_at"] == reviewed_at
    assert task["last_reviewed_agent_turn"] == 2
    reasons = debug.task_review_context(state)["tasks"][0]["reasons"]
    assert next(reason for reason in reasons if reason["kind"] == "party_sprite_net_changed")["roles"] == [
        {"role_id": "0", "from": 2, "to": 208},
    ]


def test_task_review_gate_allows_dialog_but_requires_get_and_each_upsert_before_action(tmp_path):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    task = create_agent_task(debug, {
        "id": "review-me", "description": "Old phase", "rationale": "Earlier dialog.", "object_id": 63,
    })["task"]
    debug.dialog_history = [{"time": task["last_reviewed_at"] + 1, "event_object_id": 63, "text": "new phase"}]
    cycle = debug.new_agent_tool_cycle(state)

    dialog = debug.run_agent_tool(
        "submit_decision", {"action": "advance_dialog", "reason": "Continue dialog."}, state, tool_cycle=cycle,
    )
    assert dialog["ok"] is True
    blocked = debug.run_agent_tool(
        "submit_decision", {"action": "navigate", "target_id": 63, "reason": "Move."}, state, tool_cycle=cycle,
    )
    assert blocked["error"]["code"] == "task_review_required"
    assert blocked["error"]["missing_task_ids"] == ["review-me"]

    debug.run_agent_tool("upsert_task", {"id": "review-me"}, state, tool_cycle=cycle)
    assert debug.run_agent_tool(
        "submit_decision", {"action": "navigate", "target_id": 63, "reason": "Move."}, state, tool_cycle=cycle,
    )["ok"] is False
    debug.run_agent_tool("get_tasks", {}, state, tool_cycle=cycle)
    debug.run_agent_tool("upsert_task", {"id": "review-me"}, state, tool_cycle=cycle)
    assert debug.run_agent_tool(
        "submit_decision", {"action": "navigate", "target_id": 63, "reason": "Move."}, state, tool_cycle=cycle,
    )["ok"] is True


def inbox_dialog(*, turn=5, text="请调查楼上的房间", source_key="dialog-1"):
    return {
        "time": 100.0,
        "updated_at": 100.0,
        "source_key": source_key,
        "speaker_hint": "李大娘",
        "event_object_id": 57,
        "agent_turn": turn,
        "room_id": "S3/M10:000000000001",
        "logical_room_id": "S3/M10:000000000001",
        "text": text,
    }


def test_task_inbox_only_blocks_transition_and_item_boundaries(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    transition = make_object(51, sprite_id=0, trigger_mode=5)
    same_room_target = make_object(52)
    state = make_state(objects=[transition, same_room_target])
    state["dialog"] = {"active": False}
    write_state(debug, state)
    monkeypatch.setattr(debug, "current_agent_turn", lambda: 5)
    debug.record_task_inbox_entries([inbox_dialog()])

    context = debug.task_inbox_gate_context(state)
    assert context["pending_total"] == 1
    assert context["review_required"] is False
    assert {reason["kind"] for reason in context["reasons"]} == {"pending_dialog_evidence"}

    cycle = debug.new_agent_tool_cycle(state)
    allowed = debug.run_agent_tool(
        "submit_decision", {"action": "navigate", "target_id": 52, "reason": "Search nearby."}, state,
        tool_cycle=cycle,
    )
    assert allowed["ok"] is True
    assert debug.task_inbox_gate_context(state)["pending_total"] == 1
    blocked = debug.run_agent_tool(
        "submit_decision", {"action": "navigate", "target_id": 51, "reason": "Go upstairs."}, state,
        tool_cycle=cycle,
    )
    assert blocked["error"]["code"] == "task_inbox_review_required"
    item_gate = debug.task_inbox_gate_context(state, action="use_item")
    assert item_gate["review_required"] is True
    assert any(reason["kind"] == "use_item_boundary" for reason in item_gate["reasons"])
    blocked_item = debug.run_agent_tool(
        "submit_decision", {
            "action": "use_item", "item_id": 1, "target": "all", "reason": "Use a story item.",
        }, state,
        tool_cycle=cycle,
    )
    assert blocked_item["error"]["code"] == "task_inbox_review_required"
    active_dialog_state = {**state, "dialog": {"active": True}}
    assert debug.run_agent_tool(
        "submit_decision", {"action": "advance_dialog", "reason": "Finish active dialog."},
        active_dialog_state, tool_cycle=cycle,
    )["ok"] is True
    batch_id = blocked["error"]["required_batch_ids"][0]

    resolved = debug.run_agent_tool("review_task_inbox", {"resolutions": [{
        "inbox_id": batch_id,
        "outcome": "no_follow_up",
        "reason_code": "already_completed",
        "rationale": "The script already completed this instruction.",
    }]}, state, tool_cycle=cycle)
    assert resolved["ok"] is True
    assert resolved["resolutions"][0]["outcome"] == "no_follow_up"
    assert resolved["pending_total"] == 0
    assert resolved["pending_batches"] == []
    assert debug.task_inbox_gate_context(state)["pending_total"] == 0


def test_task_inbox_tasks_updated_requires_query_and_same_cycle_upsert(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    monkeypatch.setattr(debug, "current_agent_turn", lambda: 7)
    debug.record_task_inbox_entries([inbox_dialog(turn=7)])
    batch_id = debug.task_inbox_pending()[0]["id"]
    cycle = debug.new_agent_tool_cycle(state)

    resolution = {"resolutions": [{
        "inbox_id": batch_id, "outcome": "new_task",
        "task_ids": ["investigate-room"], "rationale": "Explicit follow-up clue.",
    }]}
    missing_query = debug.run_agent_tool("review_task_inbox", resolution, state, tool_cycle=cycle)
    assert missing_query["ok"] is False
    assert "get_tasks" in missing_query["error"]

    audit_cycle = debug.new_agent_tool_cycle(state)
    audit = debug.run_agent_tool("get_tasks", {
        "include_closed": True, "detail": "summary",
    }, state, tool_cycle=audit_cycle)
    created = debug.run_agent_tool("upsert_task", {
        "id": "investigate-room",
        "description": "Investigate the occupied room",
        "rationale": "The latest dialog gave a durable story clue.",
        "review_token": audit["review_token"],
        "source_inbox_ids": [batch_id],
    }, state, tool_cycle=audit_cycle)
    assert created["ok"] is True
    debug.run_agent_tool("get_tasks", {}, state, tool_cycle=cycle)
    missing_upsert = debug.run_agent_tool("review_task_inbox", resolution, state, tool_cycle=cycle)
    assert missing_upsert["missing_task_ids"] == ["investigate-room"]

    debug.run_agent_tool("upsert_task", {
        "id": "investigate-room", "source_inbox_ids": [batch_id],
    }, state, tool_cycle=cycle)
    resolved = debug.run_agent_tool("review_task_inbox", resolution, state, tool_cycle=cycle)
    assert resolved["ok"] is True
    task = debug.task_ledger()[0]
    assert task["id"] == "investigate-room"
    assert task["provenance"]["task_inbox_batch_ids"] == [batch_id]


def test_task_inbox_separates_speaker_turns_with_same_trigger_object(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    monkeypatch.setattr(debug, "current_agent_turn", lambda: 110)
    common = {
        "time": 100.0,
        "updated_at": 100.0,
        "event_object_id": 63,
        "agent_turn": 110,
        "room_id": "S3/M10:000000000001",
        "logical_room_id": "S3/M10:000000000001",
    }
    debug.record_task_inbox_entries([
        {**common, "source_key": "dialog-drunk", "speaker_hint": "醉道士", "text": "明晚山神廟見。"},
        {**common, "source_key": "dialog-narration", "text": "醉道士離去了。"},
        {**common, "source_key": "dialog-aunt", "speaker_hint": "李大娘", "text": "逍遙～你過來一下！"},
    ])

    pending = debug.task_inbox_pending()
    assert len(pending) == 3
    assert {record["dialogs"][0]["speaker_hint"] for record in pending if record["dialogs"][0].get("speaker_hint")} == {
        "醉道士", "李大娘",
    }
    assert {record["source_event_object_id"] for record in pending} == {63}


def test_distinct_follow_ups_require_distinct_tasks_before_inbox_can_close(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    monkeypatch.setattr(debug, "current_agent_turn", lambda: 110)
    debug.record_task_inbox_entries([
        inbox_dialog(turn=110, source_key="mountain", text="明晚三更十里坡山神廟，不見不散。"),
        inbox_dialog(turn=110, source_key="summon", text="逍遙～你過來一下！"),
    ])
    by_text = {
        record["dialogs"][0]["text"]: record["id"]
        for record in debug.task_inbox_pending()
    }
    mountain_id = by_text["明晚三更十里坡山神廟，不見不散。"]
    summon_id = by_text["逍遙～你過來一下！"]
    cycle = debug.new_agent_tool_cycle(state)
    audit = debug.run_agent_tool(
        "get_tasks", {"include_closed": True, "detail": "summary"}, state, tool_cycle=cycle,
    )
    for task_id, description, inbox_id in (
        ("meet-at-temple", "明晚赴山神廟", mountain_id),
        ("answer-summons", "回應李大娘召喚", summon_id),
    ):
        result = debug.run_agent_tool("upsert_task", {
            "id": task_id,
            "description": description,
            "rationale": "The dialog requires a future action.",
            "review_token": audit["review_token"],
            "source_inbox_ids": [inbox_id],
        }, state, tool_cycle=cycle)
        assert result["ok"] is True
        audit["review_token"] = debug.run_agent_tool(
            "get_tasks", {"include_closed": True, "detail": "summary"}, state, tool_cycle=cycle,
        )["review_token"]

    incomplete = debug.run_agent_tool("review_task_inbox", {"resolutions": [{
        "inbox_id": mountain_id,
        "outcome": "new_task",
        "task_ids": ["meet-at-temple"],
        "rationale": "Track the appointment.",
    }, {
        "inbox_id": summon_id,
        "outcome": "task_updated",
        "task_ids": ["meet-at-temple"],
        "rationale": "Incorrectly reuse the appointment.",
    }]}, state, tool_cycle=cycle)
    assert incomplete["ok"] is False
    assert incomplete["missing_inbox_links"] == [{"inbox_id": summon_id, "task_id": "meet-at-temple"}]

    resolved = debug.run_agent_tool("review_task_inbox", {"resolutions": [{
        "inbox_id": mountain_id,
        "outcome": "new_task",
        "task_ids": ["meet-at-temple"],
        "rationale": "Track the appointment.",
    }, {
        "inbox_id": summon_id,
        "outcome": "new_task",
        "task_ids": ["answer-summons"],
        "rationale": "Track the summons until the follow-up dialog occurs.",
    }]}, state, tool_cycle=cycle)
    assert resolved["ok"] is True
    assert {task["id"] for task in debug.task_ledger()} == {"meet-at-temple", "answer-summons"}


def test_failed_task_creation_keeps_inbox_pending_and_blocks_navigation(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = review_state()
    transition = make_object(51, sprite_id=0, trigger_mode=5)
    state["objects"] = [transition]
    write_state(debug, state)
    monkeypatch.setattr(debug, "current_agent_turn", lambda: 12)
    debug.record_task_inbox_entries([inbox_dialog(turn=12, source_key="summon", text="Come here.")])
    inbox_id = debug.task_inbox_pending()[0]["id"]
    cycle = debug.new_agent_tool_cycle(state)

    failed = debug.run_agent_tool("upsert_task", {
        "id": "answer-summons",
        "description": "Answer the summons",
        "rationale": "The speaker requested a follow-up.",
        "source_inbox_ids": [inbox_id],
    }, state, tool_cycle=cycle)
    assert failed["error"]["code"] == "task_creation_review_required"
    assert [record["id"] for record in debug.task_inbox_pending()] == [inbox_id]
    blocked = debug.run_agent_tool(
        "submit_decision", {"action": "navigate", "target_id": 51, "reason": "Leave."},
        state, tool_cycle=cycle,
    )
    assert blocked["error"]["code"] == "task_inbox_review_required"

    audit = debug.run_agent_tool(
        "get_tasks", {"include_closed": True, "detail": "summary"}, state, tool_cycle=cycle,
    )
    created = debug.run_agent_tool("upsert_task", {
        "id": "answer-summons",
        "description": "Answer the summons",
        "rationale": "The speaker requested a follow-up.",
        "review_token": audit["review_token"],
        "source_inbox_ids": [inbox_id],
    }, state, tool_cycle=cycle)
    assert created["ok"] is True
    assert debug.run_agent_tool("review_task_inbox", {"resolutions": [{
        "inbox_id": inbox_id,
        "outcome": "new_task",
        "task_ids": ["answer-summons"],
        "rationale": "Persisted before acting.",
    }]}, state, tool_cycle=cycle)["ok"] is True
    assert debug.run_agent_tool(
        "submit_decision", {"action": "navigate", "target_id": 51, "reason": "Leave."},
        state, tool_cycle=cycle,
    )["ok"] is True


def test_real_bag_clue_creates_one_task_and_repeated_guard_line_has_no_follow_up(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    monkeypatch.setattr(debug, "current_agent_turn", lambda: 20)
    debug.record_task_inbox_entries([inbox_dialog(
        turn=20,
        source_key="dialog:3:10:2504:57:1431",
        text="也沒住人，就只見他們抬了一只鼓鼓的麻布袋進去，也不許咱們任何人進去那房間",
    )])
    first_batch = debug.task_inbox_pending()[0]["id"]
    cycle = debug.new_agent_tool_cycle(state)
    audit = debug.run_agent_tool("get_tasks", {
        "include_closed": True, "detail": "summary",
    }, state, tool_cycle=cycle)
    created = debug.run_agent_tool("upsert_task", {
        "id": "investigate-mysterious-bag",
        "description": "调查被占房间里的神秘麻布袋",
        "rationale": "麻布袋和禁止进入会影响后续行动。",
        "review_token": audit["review_token"],
        "source_inbox_ids": [first_batch],
    }, state, tool_cycle=cycle)
    assert created["ok"] is True
    promoted = debug.run_agent_tool("review_task_inbox", {"resolutions": [{
        "inbox_id": first_batch, "outcome": "new_task",
        "task_ids": ["investigate-mysterious-bag"], "rationale": "Promoted unresolved clue.",
    }]}, state, tool_cycle=cycle)
    assert promoted["ok"] is True

    monkeypatch.setattr(debug, "current_agent_turn", lambda: 21)
    debug.record_task_inbox_entries([inbox_dialog(
        turn=21, source_key="guard-repeat", text="不准靠近房間",
    )])
    repeated_batch = debug.task_inbox_pending()[0]["id"]
    dismissed = debug.run_agent_tool("review_task_inbox", {"resolutions": [{
        "inbox_id": repeated_batch, "outcome": "no_follow_up",
        "reason_code": "duplicate_evidence",
        "rationale": "Repeated guard warning; the existing bag investigation already covers it.",
    }]}, state, tool_cycle=debug.new_agent_tool_cycle(state))
    assert dismissed["ok"] is True
    assert [task["id"] for task in debug.task_ledger()] == ["investigate-mysterious-bag"]


def test_task_inbox_age_backlog_and_structural_change_are_soft_reasons(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    monkeypatch.setattr(debug, "current_agent_turn", lambda: 8)
    debug.record_task_inbox_entries([inbox_dialog(turn=5)])
    aged = debug.task_inbox_gate_context(state)
    assert aged["review_required"] is False
    assert {reason["kind"] for reason in aged["reasons"]} == {"pending_dialog_evidence", "aged"}

    batch_id = debug.task_inbox_pending()[0]["id"]
    debug.run_agent_tool("review_task_inbox", {"resolutions": [{
        "inbox_id": batch_id, "outcome": "no_follow_up", "reason_code": "chatter",
        "rationale": "No follow-up.",
    }]}, state, tool_cycle=debug.new_agent_tool_cycle(state))
    monkeypatch.setattr(debug, "current_agent_turn", lambda: 10)
    for index in range(3):
        debug.record_task_inbox_entries([
            inbox_dialog(turn=10 + index, source_key=f"dialog-{index + 2}", text=f"line {index}"),
        ])
    backlog = debug.task_inbox_gate_context(state)
    assert backlog["review_required"] is False
    assert any(reason["kind"] == "backlog" for reason in backlog["reasons"])

    for record in debug.task_inbox_pending():
        debug.run_agent_tool("review_task_inbox", {"resolutions": [{
            "inbox_id": record["id"], "outcome": "no_follow_up", "reason_code": "chatter",
            "rationale": "No follow-up.",
        }]}, state, tool_cycle=debug.new_agent_tool_cycle(state))
    debug.record_task_inbox_entries([inbox_dialog(turn=10, source_key="structural")])
    debug.event_log.append({"time": 101.0, "type": "object_changed", "object_id": 51, "changes": {"state": {"from": 0, "to": 1}}})
    structural = debug.task_inbox_gate_context(state)
    assert structural["review_required"] is False
    assert any(reason["kind"] == "structural_change" for reason in structural["reasons"])


def test_task_inbox_review_returns_next_compact_batches(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    monkeypatch.setattr(debug, "current_agent_turn", lambda: 30)
    debug.record_task_inbox_entries([
        inbox_dialog(turn=30, source_key=f"dialog-{index}", text=f"line {index}")
        for index in range(27)
    ])
    pending = debug.task_inbox_pending()
    assert len(debug.task_inbox_gate_context(state)["batches"]) == 3

    def dismiss(records):
        return debug.run_agent_tool("review_task_inbox", {"resolutions": [{
            "inbox_id": record["id"],
            "outcome": "no_follow_up",
            "reason_code": "chatter",
            "rationale": "No durable follow-up.",
        } for record in records]}, state, tool_cycle=debug.new_agent_tool_cycle(state))

    first = dismiss(pending[:3])
    assert first["pending_total"] == 24
    assert len(first["pending_batches"]) == 20
    assert [batch["id"] for batch in first["pending_batches"]] == [
        record["id"] for record in debug.task_inbox_pending()[:20]
    ]
    second = dismiss(debug.task_inbox_pending()[:20])
    assert second["pending_total"] == 4
    assert len(second["pending_batches"]) == 4
    final = dismiss(debug.task_inbox_pending())
    assert final["pending_total"] == 0
    assert final["pending_batches"] == []


def test_task_inbox_persists_and_legacy_bootstrap_only_uses_latest_exchange(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    monkeypatch.setattr(debug, "current_agent_turn", lambda: 4)
    debug.record_task_inbox_entries([inbox_dialog(turn=4)])
    reloaded = make_debug_state(tmp_path)
    assert [record["dialogs"][0]["text"] for record in reloaded.task_inbox_pending()] == ["请调查楼上的房间"]

    data = reloaded.read_exploration()
    data.pop("task_inbox", None)
    data.pop("task_inbox_initialized", None)
    reloaded.write_exploration(data)
    reloaded.dialog_history = [
        inbox_dialog(turn=1, text="old", source_key="old"),
        inbox_dialog(turn=2, text="latest", source_key="latest"),
    ]
    pending = reloaded.task_inbox_pending()
    assert len(pending) == 1
    assert pending[0]["dialogs"][0]["text"] == "latest"


def test_task_inbox_v1_migration_splits_pending_dialogs_and_preserves_resolved_history(tmp_path):
    debug = make_debug_state(tmp_path)
    data = debug.empty_exploration()
    data["task_inbox_version"] = 1
    data["task_inbox"] = {
        "legacy-pending": {
            "id": "legacy-pending",
            "status": "pending",
            "created_at": 10.0,
            "updated_at": 11.0,
            "created_agent_turn": 7,
            "room_id": "S3/M10:000000000001",
            "event_object_id": 63,
            "dialogs": [
                {"source_key": "drunk", "speaker_hint": "醉道士", "event_object_id": 63, "text": "Meet later."},
                {"source_key": "aunt", "speaker_hint": "李大娘", "event_object_id": 63, "text": "Come here."},
            ],
        },
        "legacy-resolved": {
            "id": "legacy-resolved",
            "status": "dismissed",
            "resolved_at": 9.0,
            "dialogs": [{"source_key": "old", "text": "Old chatter."}],
        },
    }
    debug.write_exploration(data)

    migrated = debug.read_exploration()
    assert migrated["task_inbox_version"] == 2
    assert migrated["task_inbox"]["legacy-resolved"]["status"] == "dismissed"
    pending = [record for record in migrated["task_inbox"].values() if record.get("status") == "pending"]
    assert len(pending) == 2
    assert {record["dialogs"][0]["speaker_hint"] for record in pending} == {"醉道士", "李大娘"}
    assert {record["migrated_from_batch_id"] for record in pending} == {"legacy-pending"}
    assert debug.read_exploration()["task_inbox"] == migrated["task_inbox"]


def test_agent_tasks_view_groups_all_statuses_and_returns_compact_records(tmp_path):
    debug = make_debug_state(tmp_path)
    active = create_agent_task(debug, {
        "description": "Active high priority", "rationale": "Current evidence.", "priority": 8,
    })["task"]
    create_agent_task(debug, {
        "description": "Active low priority", "rationale": "Current evidence.", "priority": 2,
    })
    completed = create_agent_task(debug, {
        "description": "Completed task", "rationale": "Already handled.", "priority": 9,
    })["task"]
    change_task_status(debug, completed["id"], "completed")
    abandoned = create_agent_task(debug, {
        "description": "Abandoned task", "rationale": "No longer useful.", "priority": 7,
    })["task"]
    change_task_status(debug, abandoned["id"], "abandoned")

    view = debug.agent_tasks_view()

    assert view["counts"] == {"active": 2, "dormant": 0, "completed": 1, "abandoned": 1, "total": 4}
    assert [task["status"] for task in view["tasks"]] == ["active", "active", "completed", "abandoned"]
    assert [task["description"] for task in view["tasks"][:2]] == ["Active high priority", "Active low priority"]
    assert view["tasks"][0] == {
        "id": active["id"],
        "description": "Active high priority",
        "priority": 8,
        "status": "active",
    }


def test_task_ledger_is_additive_and_survives_checkpoint_round_trip(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_checkpoint_state()
    write_state(debug, state)
    debug.save_slot_path().write_bytes(b"task-checkpoint")
    debug.exploration_path.write_text('{"version":9,"rooms":{},"transitions":{}}', encoding="utf-8")
    assert debug.read_exploration()["tasks"] == {}
    created = create_agent_task(debug, {
        "description": "Reach the courtyard",
        "rationale": "It is the next stated destination.",
        "room_id": "S3/M99:000000000001",
    })
    def slot_command(command, slot):
        if command == "save_slot":
            debug.save_slot_path(slot).write_bytes(b"new-task-checkpoint")
        return {"result": {"ok": True, "status": "saved" if command == "save_slot" else "loaded"}}
    with mock.patch.object(debug, "run_slot_command", side_effect=slot_command):
        debug.save_checkpoint("tasks", "Tasks")
        debug.write_exploration(debug.empty_exploration())
        write_state(debug, state)
        debug.load_checkpoint("tasks")
    restored = debug.task_ledger()
    assert restored[0]["id"] == created["task"]["id"]
    assert debug.run_agent_tool("get_tasks")["tasks"][0]["rationale"] == "It is the next stated destination."
    assert debug.agent_tasks_view()["tasks"][0]["description"] == "Reach the courtyard"


def test_agent_session_reset_keeps_tool_tasks(tmp_path):
    debug = make_debug_state(tmp_path)
    created = create_agent_task(debug, {
        "description": "Keep this across session reset",
        "rationale": "It belongs to companion memory.",
    })["task"]

    debug.agent_runtime.reset("manual_reset")

    assert debug.agent_tasks_view()["tasks"][0]["id"] == created["id"]


def test_executable_task_overrides_unknown_transition_but_unmatched_task_does_not(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_checkpoint_state()
    write_state(debug, state)
    unknown = {"id": 10, "status": "reachable", "transition_candidate": True,
               "known_destination": False, "steps": 1, "kind": "touch"}
    task_object = {"id": 20, "status": "reachable", "transition_candidate": False,
                   "steps": 4, "kind": "search", "agent_priority": "normal"}
    debug.navigation = mock.Mock(return_value={"status": "ok", "targets": [unknown, task_object]})
    debug.route_to_object = mock.Mock(return_value={"route": {"moves": ["right"], "steps": 1}})
    create_agent_task(debug, {
        "description": "Inspect the marked object", "rationale": "Dialog referred to it.", "object_id": 20,
    })
    assert debug.rules_decide_agent(state)["recommended_action"]["args"] == {"id": 20}

    debug.write_exploration(debug.empty_exploration())
    create_agent_task(debug, {
        "description": "Later room", "rationale": "Not reachable from this room.", "room_id": "S9/M9:000000000002",
    })
    assert debug.rules_decide_agent(state)["recommended_action"]["args"] == {"id": 10}


def test_rules_ignores_pending_review_task_and_empty_ledger_still_explores(tmp_path):
    debug = make_debug_state(tmp_path)
    state = review_state(cash=10)
    write_state(debug, state)
    unknown = {"id": 10, "status": "reachable", "transition_candidate": True,
               "known_destination": False, "steps": 1, "kind": "touch"}
    task_object = {"id": 20, "status": "reachable", "transition_candidate": False,
                   "steps": 4, "kind": "search", "agent_priority": "normal"}
    debug.navigation = mock.Mock(return_value={"status": "ok", "targets": [unknown, task_object]})
    debug.route_to_object = mock.Mock(return_value={"route": {"moves": ["right"], "steps": 1}})

    debug.write_exploration(debug.empty_exploration())
    assert debug.rules_decide_agent(state)["recommended_action"]["args"] == {"id": 10}

    create_agent_task(debug, {
        "id": "stale-task", "description": "Inspect marked object", "rationale": "Earlier evidence.",
        "priority": 9, "object_id": 20, "review_watch": {"cash": True},
    })
    state["cash"] = 11
    write_state(debug, state)
    assert debug.task_review_context(state)["required"] is True
    assert debug.rules_decide_agent(state)["recommended_action"]["args"] == {"id": 10}


def test_delivery_phase_regression_reviews_old_object_task_without_inventory_item(tmp_path):
    debug = make_debug_state(tmp_path)
    before = review_state(sprite=2, inventory=[])
    write_state(debug, before)
    old = create_agent_task(debug, {
        "id": "task-drive-beggar",
        "description": "向目标 63 取酒菜",
        "rationale": "较早对话把目标 63 作为取货对象。",
        "object_id": 63, "review_watch": {"party_role_ids": [0]},
    })["task"]
    after = review_state(sprite=208, inventory=[])
    write_state(debug, after)
    debug.dialog_history = [{
        "time": old["last_reviewed_at"] + 1,
        "event_object_id": 63,
        "text": "把酒菜送到楼上客人那里。",
    }]

    review = debug.task_review_context(after)
    assert review["required"] is True
    assert review["tasks"][0]["id"] == "task-drive-beggar"
    assert {reason["kind"] for reason in review["tasks"][0]["reasons"]} == {
        "dialog", "party_sprite_net_changed",
    }
    assert after["inventory"] == []

    change_task_status(
        debug, "task-drive-beggar", "completed", after,
        reason="目标 63 的新对话与稳定 sprite 净变化共同表明取货阶段已结束。",
    )
    successor = create_agent_task(debug, {
        "id": "deliver-upstairs",
        "description": "把酒菜送给楼上客人",
        "rationale": "相邻对话明确要求上楼交付；sprite 变化仅作为携带状态的数值证据。",
    })
    assert successor["task"]["status"] == "active"
    assert [task["id"] for task in debug.task_ledger()] == ["deliver-upstairs"]


@pytest.mark.parametrize('reason', ['checkpoint_restored', 'context_compacted'])
def test_reconciliation_rejects_empty_confirmation_without_mutation(tmp_path, reason):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    create_agent_task(debug, {'id': 'old', 'description': 'Buy shrimp',
                              'rationale': 'No known route, keep active.'})
    debug.agent_runtime.mark_task_reconciliation_pending(reason)
    cycle = debug.new_agent_tool_cycle(state)
    audit = debug.run_agent_tool('get_tasks', {'include_closed': True, 'detail': 'summary'},
                                 state, tool_cycle=cycle)
    base = {'id': 'old', 'review_token': audit['review_token']}
    debug.event_log.append({'source_key': 'route-evidence', 'type': 'transition', 'time': 100})
    before = debug.read_exploration()
    invalid = [base, {**base, **reconciliation_fields('invented-reference')}]
    for field in ['execution_basis', 'stage_assessment']:
        args = {**base, **reconciliation_fields('route-evidence')}
        args['reconciliation_review'][field] = ' '
        invalid.append(args)
    for field in ['description', 'rationale']:
        args = {**base, **reconciliation_fields('route-evidence')}
        del args[field]
        invalid.append(args)
    for args in invalid:
        assert not debug.run_agent_tool('upsert_task', args, state, tool_cycle=cycle)['ok']
        assert debug.read_exploration() == before
        assert debug.agent_runtime.task_reconciliation_pending()
        assert not cycle.get('reconciled_task_ids')
    blocked = debug.run_agent_tool('submit_decision', {
        'action': 'navigate', 'target_id': 63, 'reason': 'Continue.'}, state, tool_cycle=cycle)
    assert blocked['error']['code'] == 'task_reconciliation_required'
    good = debug.run_agent_tool('upsert_task', {
        **base, **reconciliation_fields('route-evidence')}, state, tool_cycle=cycle)
    assert good['ok']
    assert not debug.agent_runtime.task_reconciliation_pending()
    assert good['task']['description'] == 'Investigate the current lead'
    assert good['task']['reconciliation_review']['evidence'][0]['ref'] == 'route-evidence'
    # Ordinary reviews stay cheap and preserve the semantic audit without new history.
    confirmed = debug.run_agent_tool('upsert_task', {'id': 'old'}, state)
    assert confirmed['task']['history'] == good['task']['history']
    assert confirmed['task']['reconciliation_review'] == good['task']['reconciliation_review']
    changed = debug.run_agent_tool('upsert_task', {'id': 'old', 'description': 'A later stage'}, state)
    assert changed['task']['reconciliation_review'] is None


def test_reconciliation_shelves_unreachable_task_with_resume_condition(tmp_path):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    create_agent_task(debug, {'id': 'promise', 'description': 'Visit the fair',
                              'rationale': 'No verified route.'})
    debug.event_log.append({'source_key': 'departure', 'type': 'transition', 'time': 100})
    debug.agent_runtime.mark_task_reconciliation_pending('checkpoint_restored')
    cycle = debug.new_agent_tool_cycle(state)
    audit = debug.run_agent_tool('get_tasks', {'include_closed': True, 'detail': 'summary'},
                                 state, tool_cycle=cycle)
    args = {'id': 'promise', 'status': 'dormant', 'review_token': audit['review_token'],
            'status_reason': 'No verified path remains.', 'status_evidence': ['departure'],
            **reconciliation_fields('departure', dormant=True)}
    before = debug.read_exploration()
    args['reconciliation_review']['resume_condition'] = ''
    assert not debug.run_agent_tool('upsert_task', args, state, tool_cycle=cycle)['ok']
    assert debug.read_exploration() == before
    args['reconciliation_review']['resume_condition'] = 'Return with a verified route to the fair.'
    result = debug.run_agent_tool('upsert_task', args, state, tool_cycle=cycle)
    assert result['ok']
    assert result['task']['status'] == 'dormant'
    assert not debug.agent_runtime.task_reconciliation_pending()
    assert debug.task_ledger() == []
    assert debug.task_ledger(include_closed=True)[0]['history']
    summary = debug.task_summary_view(result['task'])
    assert summary['reconciliation_review']['resume_condition'] == 'Return with a verified route to the fair.'


def test_reconciliation_does_not_keep_an_old_structured_next_step(tmp_path):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    debug.event_log.append({'source_key': 'entered-city', 'type': 'transition', 'time': 100})
    fields = reconciliation_fields('entered-city')
    create_agent_task(debug, {'id': 'city', 'description': fields['description'],
                              'rationale': fields['rationale'],
                              'progress': {'evidence_refs': ['entered-city'], 'hypotheses': [],
                                           'next_step': 'Enter the city.'}})
    debug.agent_runtime.mark_task_reconciliation_pending('context_compacted')
    cycle = debug.new_agent_tool_cycle(state)
    audit = debug.run_agent_tool('get_tasks', {'include_closed': True, 'detail': 'summary'},
                                 state, tool_cycle=cycle)
    result = debug.run_agent_tool('upsert_task', {
        'id': 'city', 'review_token': audit['review_token'], **fields}, state, tool_cycle=cycle)
    assert result['ok']
    assert result['task']['progress'] is None
    assert 'progress' not in debug.task_summary_view(result['task'])
