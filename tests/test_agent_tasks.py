from unittest import mock

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
    updated = debug.run_agent_tool(
        "upsert_task",
        {"id": task_id, "status": "completed"},
        completion_state,
    )
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

    debug.run_agent_tool(
        "upsert_task", {"id": older["id"], "status": "completed"},
        {"frame": 10, "logical_frame": 110, "raw_frame": 10},
    )
    debug.run_agent_tool(
        "upsert_task", {"id": newer["id"], "status": "completed"},
        {"frame": 20, "logical_frame": 120, "raw_frame": 20},
    )

    completed = debug.run_agent_tool(
        "get_tasks", {"include_closed": True, "detail": "summary", "limit": 100},
    )["tasks"]
    assert [task["id"] for task in completed] == [newer["id"], older["id"]]
    assert [task["completed_frame"] for task in completed] == [120, 110]
    assert [task["id"] for task in debug.agent_tasks_view()["tasks"]] == [newer["id"], older["id"]]

    reopened = debug.run_agent_tool("upsert_task", {"id": older["id"], "status": "active"})["task"]
    assert reopened["completed_at"] is None
    assert reopened["completed_frame"] is None
    recompleted = debug.run_agent_tool(
        "upsert_task", {"id": older["id"], "status": "completed"},
        {"frame": 30, "logical_frame": 130, "raw_frame": 30},
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

    updated = debug.run_agent_tool("upsert_task", {
        "id": "buy_shrimp",
        "priority": 8,
        "room_id": "S3/M99:000000000001",
    })
    assert updated["operation"] == "updated"
    assert updated["task"]["priority"] == 8
    assert list(debug.read_exploration()["tasks"]) == ["buy_shrimp"]


def test_unchanged_upsert_explicitly_reviews_task_and_refreshes_snapshot(tmp_path):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    created = create_agent_task(debug, {
        "id": "keep-going", "description": "Keep going", "rationale": "Current evidence.",
    })["task"]

    state["cash"] = 15
    write_state(debug, state)
    assert debug.task_review_context(state)["required"] is True
    reviewed = debug.run_agent_tool("upsert_task", {"id": "keep-going"})

    assert reviewed["operation"] == "updated"
    assert reviewed["task"]["last_reviewed_at"] >= created["last_reviewed_at"]
    assert reviewed["task"]["last_reviewed_agent_turn"] == 0
    assert reviewed["task"]["history"][-1]["changes"] == {}
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
    debug.run_agent_tool("upsert_task", {"id": medicine["id"], "status": "completed"})
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
    }, state, tool_cycle=cycle)
    assert duplicate["ok"] is True
    accepted = debug.run_agent_tool(
        "submit_decision", {"action": "advance_dialog", "reason": "Continue available dialogue."},
        state, tool_cycle=cycle,
    )
    assert accepted["ok"] is True
    assert debug.agent_runtime.task_reconciliation_pending() is False
    tasks = {task["id"]: task for task in debug.task_ledger(include_closed=True, limit=None)}
    assert tasks["seek-medicine"]["status"] == "completed"
    assert tasks["seek-medicine-duplicate"]["status"] == "abandoned"


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
        "id", "description", "priority", "object_id", "transition_key", "room_id",
    }
    assert "rationale" not in context["active_tasks"][0]
    assert "history" not in context["active_tasks"][0]
    assert "updated_at" not in context["active_tasks"][0]


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


def test_task_review_uses_net_sprite_state_and_ignores_recovered_animation(tmp_path):
    debug = make_debug_state(tmp_path)
    state = review_state(sprite=2)
    write_state(debug, state)
    create_agent_task(debug, {
        "id": "delivery", "description": "Carry the delivery", "rationale": "Dialog established it.",
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


def test_task_inbox_is_soft_until_transition_then_requires_explicit_resolution(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    transition = make_object(51, sprite_id=0, trigger_mode=5)
    state = make_state(objects=[transition])
    state["dialog"] = {"active": False}
    write_state(debug, state)
    monkeypatch.setattr(debug, "current_agent_turn", lambda: 5)
    debug.record_task_inbox_entries([inbox_dialog()])

    context = debug.task_inbox_gate_context(state)
    assert context["pending_total"] == 1
    assert context["review_required"] is False

    cycle = debug.new_agent_tool_cycle(state)
    blocked = debug.run_agent_tool(
        "submit_decision", {"action": "navigate", "target_id": 51, "reason": "Go upstairs."}, state,
        tool_cycle=cycle,
    )
    assert blocked["error"]["code"] == "task_inbox_review_required"
    batch_id = blocked["error"]["required_batch_ids"][0]

    resolved = debug.run_agent_tool("review_task_inbox", {
        "batch_ids": [batch_id],
        "disposition": "no_follow_up",
        "rationale": "The script already completed this instruction.",
    }, state, tool_cycle=cycle)
    assert resolved["ok"] is True
    assert resolved["disposition"] == "no_follow_up"
    assert resolved["pending_total"] == 0
    assert debug.task_inbox_gate_context(state)["pending_total"] == 0


def test_task_inbox_tasks_updated_requires_query_and_same_cycle_upsert(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    monkeypatch.setattr(debug, "current_agent_turn", lambda: 7)
    debug.record_task_inbox_entries([inbox_dialog(turn=7)])
    batch_id = debug.task_inbox_pending()[0]["id"]
    cycle = debug.new_agent_tool_cycle(state)

    missing_query = debug.run_agent_tool("review_task_inbox", {
        "batch_ids": [batch_id], "disposition": "tasks_updated",
        "task_ids": ["investigate-room"], "rationale": "Explicit follow-up clue.",
    }, state, tool_cycle=cycle)
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
    }, state, tool_cycle=audit_cycle)
    assert created["ok"] is True
    debug.run_agent_tool("get_tasks", {}, state, tool_cycle=cycle)
    missing_upsert = debug.run_agent_tool("review_task_inbox", {
        "batch_ids": [batch_id], "disposition": "tasks_updated",
        "task_ids": ["investigate-room"], "rationale": "Explicit follow-up clue.",
    }, state, tool_cycle=cycle)
    assert missing_upsert["missing_task_ids"] == ["investigate-room"]

    debug.run_agent_tool("upsert_task", {"id": "investigate-room"}, state, tool_cycle=cycle)
    resolved = debug.run_agent_tool("review_task_inbox", {
        "batch_ids": [batch_id], "disposition": "tasks_updated",
        "task_ids": ["investigate-room"], "rationale": "Promoted the clue to a task.",
    }, state, tool_cycle=cycle)
    assert resolved["ok"] is True
    task = debug.task_ledger()[0]
    assert task["id"] == "investigate-room"
    assert task["provenance"]["task_inbox_batch_ids"] == [batch_id]


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
    }, state, tool_cycle=cycle)
    assert created["ok"] is True
    promoted = debug.run_agent_tool("review_task_inbox", {
        "batch_ids": [first_batch], "disposition": "tasks_updated",
        "task_ids": ["investigate-mysterious-bag"], "rationale": "Promoted unresolved clue.",
    }, state, tool_cycle=cycle)
    assert promoted["ok"] is True

    monkeypatch.setattr(debug, "current_agent_turn", lambda: 21)
    debug.record_task_inbox_entries([inbox_dialog(
        turn=21, source_key="guard-repeat", text="不准靠近房間",
    )])
    repeated_batch = debug.task_inbox_pending()[0]["id"]
    dismissed = debug.run_agent_tool("review_task_inbox", {
        "batch_ids": [repeated_batch], "disposition": "no_follow_up",
        "rationale": "Repeated guard warning; the existing bag investigation already covers it.",
    }, state, tool_cycle=debug.new_agent_tool_cycle(state))
    assert dismissed["ok"] is True
    assert [task["id"] for task in debug.task_ledger()] == ["investigate-mysterious-bag"]


def test_task_inbox_risk_gates_on_age_backlog_and_structural_change(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = review_state()
    write_state(debug, state)
    monkeypatch.setattr(debug, "current_agent_turn", lambda: 8)
    debug.record_task_inbox_entries([inbox_dialog(turn=5)])
    aged = debug.task_inbox_gate_context(state)
    assert aged["review_required"] is True
    assert {reason["kind"] for reason in aged["reasons"]} == {"aged"}

    batch_id = debug.task_inbox_pending()[0]["id"]
    debug.run_agent_tool("review_task_inbox", {
        "batch_ids": [batch_id], "disposition": "no_task", "rationale": "No follow-up.",
    }, state, tool_cycle=debug.new_agent_tool_cycle(state))
    monkeypatch.setattr(debug, "current_agent_turn", lambda: 10)
    for index in range(3):
        debug.record_task_inbox_entries([
            inbox_dialog(turn=10 + index, source_key=f"dialog-{index + 2}", text=f"line {index}"),
        ])
    backlog = debug.task_inbox_gate_context(state)
    assert backlog["review_required"] is True
    assert any(reason["kind"] == "backlog" for reason in backlog["reasons"])

    for record in debug.task_inbox_pending():
        debug.run_agent_tool("review_task_inbox", {
            "batch_ids": [record["id"]], "disposition": "no_task", "rationale": "No follow-up.",
        }, state, tool_cycle=debug.new_agent_tool_cycle(state))
    debug.record_task_inbox_entries([inbox_dialog(turn=10, source_key="structural")])
    debug.event_log.append({"time": 101.0, "type": "object_changed", "object_id": 51, "changes": {"state": {"from": 0, "to": 1}}})
    structural = debug.task_inbox_gate_context(state)
    assert structural["review_required"] is True
    assert any(reason["kind"] == "structural_change" for reason in structural["reasons"])


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
    debug.run_agent_tool("upsert_task", {"id": completed["id"], "status": "completed"})
    abandoned = create_agent_task(debug, {
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
    with mock.patch.object(debug, "run_slot_command", return_value={"result": {}}):
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
        "priority": 9, "object_id": 20,
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
        "object_id": 63,
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

    create_agent_task(debug, {
        "id": "task-drive-beggar",
        "status": "completed",
        "rationale": "目标 63 的新对话与稳定 sprite 净变化共同表明取货阶段已结束。",
    })
    successor = create_agent_task(debug, {
        "id": "deliver-upstairs",
        "description": "把酒菜送给楼上客人",
        "rationale": "相邻对话明确要求上楼交付；sprite 变化仅作为携带状态的数值证据。",
    })
    assert successor["task"]["status"] == "active"
    assert [task["id"] for task in debug.task_ledger()] == ["deliver-upstairs"]
