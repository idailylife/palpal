import asyncio
import json
import sqlite3
import sys
import threading
import time
import types

import pytest

from tests.support import make_debug_state, make_state, write_state
from palpal.agent_runtime import (
    DEFAULT_COMPACT_AT_TOKENS,
    DEFAULT_COMPACT_RETAIN_TOKENS,
    AgentRuntime,
    ReplanRequired,
)


class MemorySession:
    def __init__(self, items):
        self.items = list(items)

    async def get_items(self):
        return list(self.items)

    async def clear_session(self):
        self.items = []

    async def add_items(self, items):
        self.items.extend(items)


def decision(tool="no_op", args=None, intent="wait_runtime"):
    return {
        "summary": "test decision",
        "recommended_intent": intent,
        "recommended_action": {
            "tool": tool,
            "args": ({"reason": "no_reachable_target"} if tool == "no_op" and args is None else args or {}),
            "dry_run": True,
        },
        "evidence": {},
        "risk": "test",
        "alternatives": [],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def submission(action="advance_dialog", reason="test decision", **kwargs):
    return {"action": action, "reason": reason, **kwargs}


def wait_for_pause(runtime, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime.snapshot()["status"] == "paused":
            return runtime.snapshot()
        time.sleep(0.02)
    raise AssertionError("agent runtime did not pause")


def provider_usage(input_tokens, cached_tokens, output_tokens, reasoning_tokens):
    details_in = types.SimpleNamespace(cached_tokens=cached_tokens)
    details_out = types.SimpleNamespace(reasoning_tokens=reasoning_tokens)
    entry = types.SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_tokens_details=details_in,
        output_tokens_details=details_out,
    )
    return types.SimpleNamespace(
        requests=1,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        request_usage_entries=[entry],
    )


def test_provider_usage_aggregates_scopes_and_keeps_main_last_prompt(tmp_path):
    runtime = make_debug_state(tmp_path).agent_runtime
    runtime._record_provider_usage(
        provider_usage(100, 40, 20, 7), "main_decision", update_last_prompt=True,
    )
    runtime._record_provider_usage(provider_usage(30, 10, 5, 2), "battle")
    runtime._record_provider_usage(provider_usage(12, 0, 3, 1), "compaction")

    state = runtime.snapshot()
    tracked = state["provider_usage"]
    assert tracked["scopes"]["main_decision"] == {
        "requests": 1, "input_tokens": 100, "cached_tokens": 40,
        "output_tokens": 20, "reasoning_tokens": 7, "total_tokens": 120,
        "details_complete": True,
    }
    assert tracked["scopes"]["battle"]["total_tokens"] == 35
    assert tracked["scopes"]["compaction"]["reasoning_tokens"] == 1
    assert tracked["last_request"]["scope"] == "compaction"
    assert state["last_prompt_tokens"] == 100
    usage_events = [event for event in runtime.events(limit=20) if event["type"] == "usage"]
    assert [event["details"]["scope"] for event in usage_events] == [
        "main_decision", "battle", "compaction",
    ]


def test_provider_usage_marks_missing_details_partial_and_legacy_history_incomplete(tmp_path):
    runtime = make_debug_state(tmp_path).agent_runtime
    bare = types.SimpleNamespace(
        requests=1, input_tokens=10, output_tokens=2, total_tokens=12,
        request_usage_entries=[types.SimpleNamespace(
            input_tokens=10, output_tokens=2, total_tokens=12,
            input_tokens_details=None, output_tokens_details=None,
        )],
    )
    runtime._record_provider_usage(bare, "main_decision")
    assert runtime.snapshot()["provider_usage"]["scopes"]["main_decision"]["details_complete"] is False

    legacy = runtime.snapshot()
    legacy.pop("provider_usage")
    legacy["turn"] = 3
    runtime._atomic_json(runtime.control_path, legacy)
    runtime.state = runtime._load_or_create()
    assert runtime.snapshot()["provider_usage"]["incomplete_before_tracking"] is True


def test_old_control_goal_is_silently_removed(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    legacy = runtime.snapshot()
    legacy["goal"] = "通关游戏"
    runtime._atomic_json(runtime.control_path, legacy)

    runtime.state = runtime._load_or_create()

    assert "goal" not in runtime.snapshot()
    assert "goal" not in json.loads(runtime.control_path.read_text(encoding="utf-8"))
    assert "goal" not in runtime.compact_snapshot()


def test_runtime_counts_recommendations_and_can_resume(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    runtime._unsafe_state_reason = lambda state: None
    runtime.decision_provider = lambda state, model, feedback: decision()

    started = runtime.start("deepseek-v4-flash", 2)
    assert started["status"] == "running"
    paused = wait_for_pause(runtime)
    assert paused["turn"] == 1
    assert paused["run_turn"] == 1
    assert paused["stop_reason"] == "no_reachable_target"
    assert paused["usage"]["total_tokens"] == 15

    runtime.start(None, 1)
    resumed = wait_for_pause(runtime)
    assert resumed["turn"] == 2
    assert resumed["usage"]["total_tokens"] == 30
    assert any(event["type"] == "decision" for event in runtime.events())


def test_sdk_request_event_identifies_responses_api_and_reasoning(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    runtime._unsafe_state_reason = lambda _state: None
    original_config = debug.config_value
    monkeypatch.setattr(
        debug,
        "config_value",
        lambda key, default=None: (
            "test-key" if key == "DEEPSEEK_API_KEY" else original_config(key, default)
        ),
    )

    async def sdk_decide(_state, _feedback):
        return decision()

    monkeypatch.setattr(runtime, "_sdk_decide", sdk_decide)
    runtime.start(None, 1)
    wait_for_pause(runtime)

    request = next(
        event for event in runtime.events()
        if event["type"] == "model" and event["phase"] == "request"
    )
    assert request["details"]["api"] == "responses"
    assert request["details"]["reasoning_effort"] == "high"


def test_runtime_start_feedback_has_no_permanent_macro_goal(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    runtime._unsafe_state_reason = lambda state: None
    feedback_seen = []
    runtime.decision_provider = lambda state, model, feedback: feedback_seen.append(feedback) or decision()

    runtime.start(None, 1)
    wait_for_pause(runtime)

    assert feedback_seen
    assert "permanent goal" not in feedback_seen[0].lower()
    assert "finish the game" not in feedback_seen[0].lower()


def test_new_logical_room_is_automatically_inspected_before_decision(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    baseline = make_state()
    runtime._observe_room_for_inspection(baseline)
    destination = {**make_state(), "map_id": 11}
    destination["_logical_room"] = debug.logical_room_identity(destination)
    calls = []

    def inspect(name, arguments, state, *, tool_cycle=None):
        calls.append((name, arguments, state["map_id"], tool_cycle))
        return {
            "ok": True,
            "authority": "visual_annotation",
            "focus": "room_layout",
            "frame_sequence": 77,
            "visual_hash": "abc123",
            "report": {"summary": "A room with a visible northern exit."},
        }

    monkeypatch.setattr(debug, "run_agent_tool", inspect)
    feedback = asyncio.run(runtime._automatic_room_inspection(destination))

    room_id = debug.room_identity(destination)["id"]
    assert calls[0][0] == "inspect_screen"
    assert calls[0][1]["focus"] == "room_layout"
    assert "object IDs, scripts, or confirmed transitions" in calls[0][1]["question"]
    assert "visual_annotation" in feedback
    assert runtime.snapshot()["room_inspection"] == {
        "baseline_required": False,
        "last_observed_room_id": room_id,
        "pending_room_id": None,
        "inspected_rooms": {
            room_id: {
                "inspected_at": runtime.snapshot()["room_inspection"]["inspected_rooms"][room_id]["inspected_at"],
                "frame_sequence": 77,
                "visual_hash": "abc123",
            },
        },
    }
    event = next(event for event in runtime.events() if event["phase"] == "automatic_visual_inspection")
    assert event["details"]["automatic"] is True
    assert event["details"]["room_id"] == room_id


def test_runtime_cannot_decide_before_pending_room_inspection(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    runtime._unsafe_state_reason = lambda _state: None
    runtime._observe_room_for_inspection(make_state())
    destination = {**make_state(), "map_id": 11}
    order = []
    monkeypatch.setattr(debug, "snapshot", lambda: destination)
    monkeypatch.setattr(
        debug,
        "run_agent_tool",
        lambda *args, **kwargs: order.append("inspect") or {
            "ok": True,
            "authority": "visual_annotation",
            "focus": "room_layout",
            "frame_sequence": 88,
            "visual_hash": "new-room",
            "report": {"summary": "New room."},
        },
    )
    runtime.decision_provider = (
        lambda _state, _model, feedback: order.append("decide")
        or ("Automatic low-authority visual inspection" in feedback and decision())
    )

    runtime.start(None, 1)
    paused = wait_for_pause(runtime)

    assert paused["stop_reason"] == "no_reachable_target"
    assert order == ["inspect", "decide"]


def test_automatic_room_inspection_defers_for_overlay_and_failure_remains_retryable(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    runtime._observe_room_for_inspection(make_state())
    destination = {**make_state(), "map_id": 11, "dialog": {"active": True}}
    calls = []
    monkeypatch.setattr(
        debug,
        "run_agent_tool",
        lambda *args, **kwargs: calls.append(args) or {
            "ok": False,
            "error": {"code": "frame_stale", "message": "stale"},
        },
    )

    assert asyncio.run(runtime._automatic_room_inspection(destination)) is None
    room_id = debug.room_identity(destination)["id"]
    assert runtime.snapshot()["room_inspection"]["pending_room_id"] == room_id
    assert calls == []

    destination["dialog"] = {"active": False}
    assert asyncio.run(runtime._automatic_room_inspection(destination)) == ""
    state = runtime.snapshot()
    assert state["stop_reason"] == "automatic_room_inspection_failed"
    assert state["room_inspection"]["pending_room_id"] == room_id
    assert room_id not in state["room_inspection"]["inspected_rooms"]


def test_inspected_room_revisit_and_same_room_region_change_do_not_repeat(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    source = make_state()
    runtime._observe_room_for_inspection(source)
    destination = {**make_state(), "map_id": 11}
    calls = []
    monkeypatch.setattr(
        debug,
        "run_agent_tool",
        lambda *args, **kwargs: calls.append(1) or {
            "ok": True,
            "frame_sequence": 90,
            "visual_hash": "seen",
            "report": {"summary": "seen"},
        },
    )
    asyncio.run(runtime._automatic_room_inspection(destination))

    same_room_other_region = {**destination, "region_id": 99}
    assert asyncio.run(runtime._automatic_room_inspection(same_room_other_region)) is None
    runtime._observe_room_for_inspection(source)
    assert asyncio.run(runtime._automatic_room_inspection(destination)) is None
    assert calls == [1]


def test_legacy_room_inspection_state_baselines_current_room(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    legacy = runtime.snapshot()
    legacy.pop("room_inspection")
    legacy.pop("room_entry_context")
    runtime._atomic_json(runtime.control_path, legacy)
    runtime.state = runtime._load_or_create()

    assert runtime._observe_room_for_inspection(make_state())
    inspection = runtime.snapshot()["room_inspection"]
    assert inspection["baseline_required"] is False
    assert inspection["pending_room_id"] is None
    assert runtime.snapshot()["room_entry_context"] is None


def transition_object(object_id, x, y, script):
    return {
        "id": object_id, "x": x, "y": y, "state": 1, "sprite_id": 0,
        "trigger_mode": 4, "trigger_script": script, "auto_script": 0,
        "blocking": False, "interactable": True,
    }


def test_cross_room_execution_records_entry_context_and_same_room_region_change_does_not(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    source = make_state(objects=[transition_object(706, 32, 0, 13729)], frame=10)
    source["map_id"] = 40
    destination = make_state(objects=[transition_object(758, 32, 0, 13773)], frame=20)
    destination["map_id"] = 70

    runtime._update_room_entry_after_execution(
        source, destination, "navigation_step", {"id": 706},
        {"status": "transitioned", "progress": [{"status": "transitioned"}]},
    )
    context = runtime.snapshot()["room_entry_context"]
    assert context["status"] == "entered"
    assert context["source_room_id"] == debug.room_identity(source)["id"]
    assert context["destination_room_id"] == debug.room_identity(destination)["id"]
    assert context["source_target_id"] == 706
    assert context["source_transition_key"]
    assert context["landing_position"] == destination["party_position"]
    assert runtime.recent_transition_chain(destination)[0]["target_id"] == 706

    runtime._set_room_entry_context(None)
    same_room_other_region = {**source, "region_id": 99, "frame": 11}
    runtime._update_room_entry_after_execution(
        source, same_room_other_region, "navigation_step", {"id": 706},
        {"status": "transitioned", "progress": [{"status": "transitioned"}]},
    )
    assert runtime.snapshot()["room_entry_context"] is None


def test_immediate_room_return_creates_recovery_route_and_blocks_unrelated_navigation(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    source = make_state(width=7, height=5, start=(1, 1), objects=[
        transition_object(706, 32, 0, 13729),
        {
            "id": 722, "x": 64, "y": 16, "state": 1, "sprite_id": 1,
            "trigger_mode": 1, "trigger_script": 13740, "auto_script": 0,
            "blocking": False, "interactable": True,
        },
    ], frame=10)
    source["map_id"] = 40
    destination = make_state(
        width=7, height=5, start=(1, 1),
        objects=[transition_object(758, 32, 0, 13773)], frame=20,
    )
    destination["map_id"] = 70
    runtime._update_room_entry_after_execution(
        source, destination, "navigation_step", {"id": 706},
        {"status": "transitioned", "progress": [{"status": "transitioned"}]},
    )
    runtime._update_room_entry_after_execution(
        destination, source, "navigation_step", {"id": 758},
        {"status": "transitioned", "progress": [{"status": "transitioned"}]},
    )

    context = runtime.snapshot()["room_entry_context"]
    assert context["status"] == "recovery_pending"
    assert context["return_target_id"] == 758
    assert runtime.recent_transition_chain(source)[-1]["classification"] == "transition_loop"
    write_state(debug, source)
    navigation = debug.run_agent_tool("get_navigation", {}, source)["navigation"]
    targets = {target["id"]: target for target in navigation["targets"]}
    assert targets[706]["recovery_route"] is True
    assert targets[706]["agent_priority"] == "resume_interrupted_entry"

    rejected = debug.run_agent_tool("submit_decision", {
        "action": "navigate", "target_id": 722, "reason": "Choose another target.",
    }, source)["decision"]
    assert rejected["validation"]["reason"] == "transition_recovery_required"
    assert rejected["validation"]["recovery_target_id"] == 706
    assert not debug.action_path.exists()

    recovered = debug.run_agent_tool("submit_decision", {
        "action": "navigate", "target_id": 706, "reason": "Recover the room just left.",
    }, source)["decision"]
    assert recovered["recommended_action"]["tool"] == "navigation_step"


def test_selected_only_entry_return_is_allowed_and_records_normal_entry(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    source = make_state(
        width=7,
        height=5,
        start=(1, 1),
        objects=[transition_object(706, 32, 0, 13729)],
        frame=10,
    )
    source["map_id"] = 40
    destination = make_state(
        width=7,
        height=5,
        start=(1, 1),
        objects=[transition_object(758, 32, 0, 13773)],
        frame=20,
    )
    destination["map_id"] = 70
    runtime._update_room_entry_after_execution(
        source,
        destination,
        "navigation_step",
        {"id": 706},
        {"status": "transitioned", "progress": [{"status": "transitioned"}]},
    )
    debug.record_transition_destination(
        debug.room_identity(destination),
        758,
        debug.room_identity(source),
        from_object=destination["objects"][0],
        from_state=destination,
        to_state=source,
    )
    write_state(debug, destination)
    navigation = debug.run_agent_tool("get_navigation", {}, destination)["navigation"]
    executable = [
        target for target in navigation["targets"]
        if target["status"] in {"reachable", "transition", "blocked_by_interactable"}
    ]
    assert [target["id"] for target in executable] == [758]
    assert executable[0]["entry_relation"] == "confirmed_entry_return"

    submitted = debug.run_agent_tool("submit_decision", {
        "action": "navigate",
        "target_id": 758,
        "reason": "Return through the only executable entrance.",
    }, destination)["decision"]

    assert submitted["recommended_action"]["tool"] == "navigation_step"
    assert submitted.get("validation", {}).get("status") != "rejected"

    entry_return_checks = []
    navigation_target_is_entry_return = debug.navigation_target_is_entry_return

    def is_entry_return(state, target_id):
        entry_return_checks.append((state["map_id"], target_id))
        return navigation_target_is_entry_return(state, target_id)

    monkeypatch.setattr(debug, "navigation_target_is_entry_return", is_entry_return)
    monkeypatch.setattr(debug, "snapshot", lambda: destination)
    monkeypatch.setattr(runtime, "_navigate", lambda *_args: {
        "ok": True,
        "status": "transitioned",
        "progress": [{"status": "transitioned"}],
    })
    monkeypatch.setattr(runtime, "_wait_state_quiet", lambda: source)

    runtime._execute_decision(submitted)

    context = runtime.snapshot()["room_entry_context"]
    assert context["status"] == "entered"
    assert context["source_room_id"] == debug.room_identity(destination)["id"]
    assert context["destination_room_id"] == debug.room_identity(source)["id"]
    assert entry_return_checks == [(70, 758)]


def test_site_history_entry_return_with_alternative_is_soft_avoid_only(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    source = make_state(objects=[transition_object(706, 32, 0, 13729)], frame=10)
    source["map_id"] = 40
    old_return = transition_object(758, 32, 0, 13773)
    current_return = {**old_return, "trigger_script": 13774}
    alternative = transition_object(767, 160, 40, 13790)
    destination = make_state(
        width=12, height=7, start=(1, 1),
        objects=[current_return, alternative], frame=20,
    )
    destination["map_id"] = 70
    runtime._update_room_entry_after_execution(
        source, destination, "navigation_step", {"id": 706},
        {"status": "transitioned", "progress": [{"status": "transitioned"}]},
    )
    historical_destination = {**destination, "objects": [old_return, alternative]}
    debug.record_transition_destination(
        debug.room_identity(destination),
        758,
        debug.room_identity(source),
        from_object=old_return,
        from_state=historical_destination,
        to_state=source,
        confirmation="native_trigger",
    )
    write_state(debug, destination)

    navigation = debug.run_agent_tool("get_navigation", {}, destination)["navigation"]
    target = next(item for item in navigation["targets"] if item["id"] == 758)
    assert target["entry_relation"] == "confirmed_entry_return"
    assert target["known_destination_basis"] == "confirmed_site_history"

    submitted = debug.run_agent_tool("submit_decision", {
        "action": "navigate",
        "target_id": 758,
        "reason": "Deliberately return through the known entrance.",
    }, destination)["decision"]
    assert submitted["recommended_action"]["tool"] == "navigation_step"
    assert submitted.get("validation", {}).get("status") != "rejected"


def test_recovery_then_selected_entry_return_does_not_rearm_recovery(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    source = make_state(objects=[transition_object(706, 32, 0, 13729)], frame=10)
    source["map_id"] = 40
    destination = make_state(objects=[transition_object(758, 32, 0, 13773)], frame=20)
    destination["map_id"] = 70
    transitioned = {"status": "transitioned", "progress": [{"status": "transitioned"}]}

    runtime._update_room_entry_after_execution(
        source, destination, "navigation_step", {"id": 706}, transitioned,
    )
    runtime._update_room_entry_after_execution(
        destination, source, "navigation_step", {"id": 758}, transitioned,
    )
    assert runtime.snapshot()["room_entry_context"]["status"] == "recovery_pending"

    runtime._update_room_entry_after_execution(
        source, destination, "navigation_step", {"id": 706}, transitioned,
    )
    recovered = runtime.snapshot()["room_entry_context"]
    assert recovered["status"] == "entered"
    assert recovered["recovered_from_loop"] is True

    runtime._update_room_entry_after_execution(
        destination,
        source,
        "navigation_step",
        {"id": 758},
        transitioned,
        entry_return_selected=True,
    )
    returned = runtime.snapshot()["room_entry_context"]
    assert returned["status"] == "entered"
    assert returned["source_room_id"] == debug.room_identity(destination)["id"]
    assert returned["destination_room_id"] == debug.room_identity(source)["id"]


def test_non_entry_navigation_progress_preserves_entry_return_guard(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    state = make_state(width=7, height=5, objects=[
        transition_object(758, 32, 0, 13773),
        {
            "id": 767, "x": 64, "y": 16, "state": 1, "sprite_id": 1,
            "trigger_mode": 1, "trigger_script": 13790, "auto_script": 0,
            "blocking": False, "interactable": True,
        },
    ])
    state["map_id"] = 70
    source = make_state()
    source["map_id"] = 40
    runtime.state["room_entry_context"] = {
        "status": "entered",
        "source_room_id": debug.room_identity(source)["id"],
        "source_target_id": 706,
        "source_transition_key": "source-key",
        "destination_room_id": debug.room_identity(state)["id"],
        "landing_position": dict(state["party_position"]),
    }
    return_target = debug.transition_observation_from_state(state, 758)
    exploration = debug.read_exploration()
    exploration.setdefault("transitions", {}).setdefault(
        runtime.state["room_entry_context"]["source_room_id"], {}
    )["source-key"] = {
        "transition_key": "source-key",
        "object_id": 706,
        "from_room_id": runtime.state["room_entry_context"]["source_room_id"],
        "to_room_id": runtime.state["room_entry_context"]["destination_room_id"],
        "confirmation": "native_trigger",
        "destination_entry_candidate": {
            "entry_relation": "inferred_entry_return",
            "object_id": 758,
            "transition_key": return_target["transition_key"],
            "transition_key_fields": return_target["transition_key_fields"],
            "candidate_return_room_id": runtime.state["room_entry_context"]["source_room_id"],
            "source_transition_key": "source-key",
            "evidence": {"kind": "native_landing_proximity", "steps": 3, "max_steps": 4},
        },
    }
    debug.write_exploration(exploration)
    runtime._update_room_entry_after_execution(
        state, state, "navigation_step", {"id": 767},
        {"status": "arrived", "progress": [{"status": "stepping"}]},
    )
    assert runtime.snapshot()["room_entry_context"]["status"] == "progressed"
    navigation = debug.run_agent_tool("get_navigation", {}, state)["navigation"]
    target = next(target for target in navigation["targets"] if target["id"] == 758)
    assert target["possible_entry_return"] is True
    assert target["entry_relation"] == "inferred_entry_return"
    assert target["agent_priority"] == "avoid_immediate_backtrack"

    write_state(debug, state)
    rejected = debug.run_agent_tool("submit_decision", {
        "action": "navigate", "target_id": 758, "reason": "Take the nearest transition.",
    }, state)["decision"]
    assert rejected["validation"]["reason"] == "immediate_entry_return"
    assert rejected["validation"]["alternative_target_ids"] == [767]
    assert rejected["recommended_action"]["tool"] == "no_op"


def test_entry_context_survives_overlay_before_any_navigation_progress(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    state = make_state(objects=[
        transition_object(758, 32, 0, 13773),
        {
            "id": 767, "x": 0, "y": 0, "state": 1, "sprite_id": 1,
            "trigger_mode": 1, "trigger_script": 13790, "auto_script": 0,
            "blocking": False, "interactable": True,
        },
    ])
    state["map_id"] = 70
    source = make_state()
    source["map_id"] = 40
    runtime.state["room_entry_context"] = {
        "status": "entered",
        "source_room_id": debug.room_identity(source)["id"],
        "source_target_id": 706,
        "source_transition_key": "source-key",
        "destination_room_id": debug.room_identity(state)["id"],
        "landing_position": dict(state["party_position"]),
    }
    runtime._update_room_entry_after_execution(
        state, state, "navigation_step", {"id": 767},
        {"status": "interrupted_by_dialog", "progress": [{"status": "in_dialog"}]},
    )
    assert runtime.snapshot()["room_entry_context"]["status"] == "entered"


def test_runtime_waits_for_game_control_without_consuming_a_turn(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    released = threading.Event()
    controlled = {
        **make_state(),
        "control": {
            "owner": "game",
            "phase": "game_script",
            "allowed_actions": [],
            "navigation_ready": False,
        },
        "_debug": {"runtime_stale": False, "action_pending": False},
    }
    ready = {
        **make_state(),
        "control": {
            "owner": "client",
            "phase": "ready",
            "allowed_actions": [
                "press_buttons", "navigation_step", "use_item", "equip_item",
            ],
            "navigation_ready": True,
        },
        "_debug": {"runtime_stale": False, "action_pending": False},
    }
    debug.snapshot = lambda: ready if released.is_set() else controlled
    runtime.decision_provider = lambda state, model, feedback: decision()

    runtime.start(None, 1)
    time.sleep(0.12)
    assert runtime.snapshot()["run_turn"] == 0
    assert runtime.snapshot()["status"] == "running"

    released.set()
    paused = wait_for_pause(runtime)
    assert paused["run_turn"] == 1
    assert paused["stop_reason"] == "no_reachable_target"
    assert not any("agent requested wait" in event.get("summary", "") for event in runtime.events())


def test_control_wait_is_interruptible_and_reports_timeout(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    controlled = {
        **make_state(),
        "control": {
            "owner": "game",
            "phase": "game_script",
            "allowed_actions": [],
            "navigation_ready": False,
        },
    }
    debug.snapshot = lambda: controlled

    with pytest.raises(RuntimeError, match="game_control_timeout"):
        runtime._wait_for_control(timeout=0.01)

    runtime.cancel_event.set()
    with pytest.raises(RuntimeError, match="interrupted"):
        runtime._wait_for_control(timeout=1.0)


def test_control_wait_timeout_resets_on_meaningful_progress(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime

    def controlled(x):
        return {
            **make_state(),
            "party_position": {"x": x, "y": 8},
            "control": {
                "owner": "game",
                "phase": "game_script",
                "allowed_actions": [],
                "navigation_ready": False,
            },
        }

    ready = {
        **make_state(),
        "control": {
            "owner": "client",
            "phase": "ready",
            "allowed_actions": ["navigation_step"],
            "navigation_ready": True,
        },
    }
    snapshots = [controlled(16), controlled(32), controlled(48), ready]
    debug.snapshot = lambda: snapshots.pop(0)

    assert runtime._wait_for_control(timeout=0.06) is ready


def test_battle_delegate_uses_child_budget_and_returns_one_summary(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    command = {
        **make_state(),
        "game_instance_id": "battle-game",
        "in_battle": True,
        "battle": {
            "generation": 5,
            "command_generation": 9,
            "phase": "command",
            "result": "ongoing",
            "is_boss": False,
            "current_player_index": 0,
            "players": [{"index": 0, "name": "李逍遥", "hp": 100, "max_hp": 100}],
            "enemies": [{
                "slot_index": 0, "present": True, "object_id": 10, "name": "苗人",
                "hp": 20, "max_hp": 20, "physical_resistance": 7,
            }],
            "available_actions": [{
                "action_type": "attack",
                "object_id": 0,
                "target_mode": "single",
                "valid_target_indices": [0],
            }],
        },
        "control": {
            "owner": "client",
            "phase": "battle",
            "allowed_actions": ["battle_action", "press_buttons"],
            "navigation_ready": False,
        },
        "_debug": {"runtime_stale": False},
    }
    ended = {
        **make_state(),
        "game_instance_id": "battle-game",
        "battle": None,
        "last_battle": {
            "generation": 5,
            "result": "won",
            "is_boss": False,
            "duration_ms": 1200,
            "exp_gained": 12,
            "cash_gained": 7,
            "enemies": [{"object_id": 10, "name": "苗人"}],
            "players": [{
                "index": 0, "role_id": 0, "name": "李逍遥",
                "hp_before": 100, "hp_after": 95,
                "mp_before": 20, "mp_after": 20,
                "level_before": 1, "level_after": 1,
            }],
        },
        "control": {
            "owner": "client",
            "phase": "ready",
            "allowed_actions": ["press_buttons", "navigation_step", "use_item", "equip_item"],
            "navigation_ready": True,
        },
        "_debug": {"runtime_stale": False},
    }
    current = {"value": command}
    debug.snapshot = lambda: current["value"]

    def execute_battle(args):
        assert args["battle_generation"] == 5
        assert args["command_generation"] == 9
        current["value"] = ended
        return {
            "battle_generation": 5,
            "command_generation": 9,
            "player_index": 0,
            "action_type": "attack",
            "object_id": 0,
            "target_index": 0,
        }

    debug.write_battle_action = execute_battle
    provider_states = []

    def choose_battle(state, _model, _feedback):
        provider_states.append(state)
        return {"action_type": "attack", "object_id": 0, "target_index": 0}

    runtime.battle_decision_provider = choose_battle
    main_feedback = []
    runtime.decision_provider = lambda state, model, feedback: main_feedback.append(feedback) or decision()

    runtime.start(None, 1)
    paused = wait_for_pause(runtime)

    assert paused["run_turn"] == 1
    assert paused["battle_delegate"]["instruction_count"] == 1
    assert paused["battle_delegate"]["status"] == "completed"
    assert paused["battle_delegate"]["child_session_id"] != paused["session_id"]
    assert provider_states[0]["battle"]["enemies"] == [{
        "slot_index": 0, "present": True, "object_id": 10, "name": "苗人",
    }]
    assert "_debug" not in provider_states[0]
    battle_events = [event for event in runtime.events() if event["type"] == "battle"]
    assert [event["phase"] for event in battle_events] == [
        "started", "request", "tool_called", "tool_output", "completed",
    ]
    assert battle_events[-1]["details"]["summary"]["result"] == "won"
    metadata = [event["details"]["battle_delegate"] for event in battle_events]
    assert len({entry["group_id"] for entry in metadata}) == 1
    assert {entry["depth"] for entry in metadata} == {1}
    assert {entry["battle_generation"] for entry in metadata} == {5}
    assert {event["turn"] for event in battle_events} == {0}
    assert battle_events[2]["details"]["tool"] == "native_battle_action"
    assert battle_events[3]["details"]["output"]["action_type"] == "attack"
    assert "feedback" not in battle_events[1]["details"]
    with sqlite3.connect(runtime.sqlite_path) as connection:
        has_messages = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agent_messages'"
        ).fetchone()
        child_items = (
            connection.execute(
                "SELECT COUNT(*) FROM agent_messages WHERE session_id = ?",
                (paused["battle_delegate"]["child_session_id"],),
            ).fetchone()[0]
            if has_messages else 0
        )
    assert child_items == 0
    summary = battle_events[-1]["details"]["summary"]
    assert summary["enemies"] == ["苗人"]
    assert summary["enemy_details"] == [{"object_id": 10, "name": "苗人"}]
    assert summary["enemy_identity_source"] == "last_battle"
    assert summary["trigger_context"] is None
    assert "enemy_details/enemies are the authoritative enemy identities" in main_feedback[-1]


def test_battle_summary_falls_back_to_start_roster_and_separates_trigger_context(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    state = {
        **make_state(),
        "game_instance_id": "battle-game",
        "in_battle": True,
        "battle": {
            "generation": 6,
            "phase": "command",
            "enemies": [{
                "slot_index": 0,
                "present": True,
                "object_id": 611,
                "name": "綠葉妖精",
            }],
        },
        "trigger_observation": {
            "generation": 18,
            "event_object_id": 233,
            "trigger_script": 7100,
            "completed": False,
            "source": {"scene_id": 3, "map_id": 10, "x": 16, "y": 8},
        },
        "_exploration": {
            "last_route": {
                "target_selection": {"id": 233, "kind": "touch", "status": "reachable"},
            },
        },
    }

    runtime.battle_delegate._begin(state)
    summary = runtime.battle_delegate._summary({
        **make_state(),
        "last_battle": {
            "generation": 6,
            "result": "won",
            "is_boss": False,
            "players": [],
            "enemies": [],
        },
    }, 2)

    assert summary["enemies"] == ["綠葉妖精"]
    assert summary["enemy_details"] == [{"object_id": 611, "name": "綠葉妖精"}]
    assert summary["enemy_identity_source"] == "battle_start_fallback"
    assert summary["trigger_context"] == {
        "relationship": "active_trigger_at_battle_start",
        "generation": 18,
        "event_object_id": 233,
        "trigger_script": 7100,
        "logical_room_id": "S3/M10:2F2C967F8E00",
        "source": {"scene_id": 3, "map_id": 10, "x": 16, "y": 8},
        "navigation_target": {"id": 233, "kind": "touch", "status": "reachable"},
    }


@pytest.mark.parametrize("completed,source_scene", [(True, 3), (False, 4)])
def test_battle_trigger_context_rejects_completed_or_wrong_room_observation(
    tmp_path, completed, source_scene,
):
    debug = make_debug_state(tmp_path)
    state = {
        **make_state(),
        "game_instance_id": "battle-game",
        "in_battle": True,
        "battle": {"generation": 7, "phase": "command", "enemies": []},
        "trigger_observation": {
            "generation": 19,
            "event_object_id": 233,
            "trigger_script": 7100,
            "completed": completed,
            "source": {"scene_id": source_scene, "map_id": 10, "x": 16, "y": 8},
        },
    }

    control = debug.agent_runtime.battle_delegate._begin(state)

    assert control["trigger_context"] is None


def test_battle_summary_rejects_stale_last_battle_identity(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    state = {
        **make_state(),
        "game_instance_id": "battle-game",
        "in_battle": True,
        "battle": {
            "generation": 8,
            "phase": "command",
            "enemies": [{
                "slot_index": 0, "present": True, "object_id": 611, "name": "綠葉妖精",
            }],
        },
    }
    runtime.battle_delegate._begin(state)

    summary = runtime.battle_delegate._summary({
        "last_battle": {
            "generation": 7,
            "result": "won",
            "enemies": [{"object_id": 468, "name": "石雕"}],
            "players": [],
        },
    }, 0)

    assert summary["result"] == "terminated"
    assert summary["enemy_details"] == [{"object_id": 611, "name": "綠葉妖精"}]
    assert summary["enemy_identity_source"] == "battle_start_fallback"


def test_battle_delegate_requests_action_immediately_in_next_command_window(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime

    def command(command_generation):
        return {
            **make_state(),
            "game_instance_id": "battle-game",
            "in_battle": True,
            "battle": {
                "generation": 5,
                "command_generation": command_generation,
                "phase": "command",
                "result": "ongoing",
                "current_player_index": 0,
                "players": [{"index": 0, "name": "李逍遥", "hp": 100, "max_hp": 100}],
                "enemies": [{"slot_index": 0, "present": True, "name": "苗人", "hp": 20}],
                "available_actions": [{
                    "action_type": "attack", "object_id": 0,
                    "target_mode": "single", "valid_target_indices": [0],
                    "submission_target_indices": [0],
                }],
            },
        }

    ended = {
        **make_state(),
        "game_instance_id": "battle-game",
        "in_battle": False,
        "battle": None,
        "last_battle": {
            "generation": 5, "result": "won", "players": [], "enemies": [],
        },
    }
    current = {"state": command(1)}
    feedback = []
    debug.snapshot = lambda: current["state"]

    def choose(_state, _model, current_feedback):
        feedback.append(current_feedback)
        return {"action_type": "attack", "object_id": 0, "target_index": 0}

    def execute(args):
        generation = int(args["command_generation"])
        current["state"] = command(2) if generation == 1 else ended
        return {
            "battle_generation": 5,
            "command_generation": generation,
            "player_index": 0,
            "action_type": "attack",
            "object_id": 0,
            "target_index": 0,
        }

    runtime.battle_decision_provider = choose
    debug.write_battle_action = execute

    summary = asyncio.run(runtime.battle_delegate.run(current["state"]))

    assert summary["result"] == "won"
    assert len(feedback) == 2
    assert feedback == ["", ""]


def test_battle_delegate_flee_reentry_completes_old_generation_and_uses_new_child(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime

    def command(generation, command_generation=1):
        return {
            **make_state(),
            "game_instance_id": "battle-game",
            "in_battle": True,
            "battle": {
                "generation": generation,
                "command_generation": command_generation,
                "phase": "command",
                "current_player_index": 0,
                "available_actions": [{
                    "action_type": "flee" if generation == 2 else "attack",
                    "object_id": 0,
                    "target_mode": "none" if generation == 2 else "single",
                    "valid_target_indices": [] if generation == 2 else [0],
                }],
                "enemies": [],
            },
        }

    current = {"state": command(2)}
    writes = []
    debug.snapshot = lambda: current["state"]
    runtime.battle_decision_provider = lambda state, _model, _feedback: {
        "action_type": "flee" if state["battle"]["generation"] == 2 else "attack",
        "object_id": 0,
        "target_index": -2 if state["battle"]["generation"] == 2 else 0,
    }

    def execute(args):
        writes.append(dict(args))
        generation = int(args["battle_generation"])
        if generation == 2:
            current["state"] = {
                **command(3),
                "last_battle": {
                    "generation": 2, "result": "fled", "players": [], "enemies": [],
                },
            }
        else:
            current["state"] = {
                **make_state(),
                "game_instance_id": "battle-game",
                "in_battle": False,
                "battle": None,
                "last_battle": {
                    "generation": 3, "result": "won", "players": [], "enemies": [],
                },
            }
        return {
            **args,
            "status": "submitted",
        }

    debug.write_battle_action = execute

    first = asyncio.run(runtime.battle_delegate.run(current["state"]))
    first_child = runtime.snapshot()["battle_delegate"]["child_session_id"]
    second = asyncio.run(runtime.battle_delegate.run(current["state"]))
    second_child = runtime.snapshot()["battle_delegate"]["child_session_id"]

    assert first["result"] == "fled"
    assert second["result"] == "won"
    assert first_child != second_child
    assert [entry["battle_generation"] for entry in writes] == [2, 3]
    assert not [event for event in runtime.events() if event["phase"] in {"failed", "retry"}]


def test_battle_delegate_waits_for_native_progress_after_submitted_receipt(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    command = {
        **make_state(),
        "game_instance_id": "battle-game",
        "in_battle": True,
        "battle": {
            "generation": 5,
            "command_generation": 9,
            "phase": "command",
            "current_player_index": 0,
            "available_actions": [{
                "action_type": "attack", "object_id": 0,
                "target_mode": "single", "valid_target_indices": [0],
            }],
            "enemies": [],
        },
    }
    ended = {
        **make_state(),
        "game_instance_id": "battle-game",
        "in_battle": False,
        "battle": None,
        "last_battle": {"generation": 5, "result": "won", "players": [], "enemies": []},
    }
    accepted = {"value": False, "polls": 0}

    def snapshot():
        if accepted["value"]:
            accepted["polls"] += 1
            if accepted["polls"] >= 3:
                return ended
        return command

    debug.snapshot = snapshot
    decisions = []
    runtime.battle_decision_provider = lambda *_args: decisions.append(1) or {
        "action_type": "attack", "object_id": 0, "target_index": 0,
    }
    debug.write_battle_action = lambda args: accepted.update(value=True) or {
        **args, "status": "submitted",
    }

    summary = asyncio.run(runtime.battle_delegate.run(command))

    assert summary["result"] == "won"
    assert decisions == [1]
    assert accepted["polls"] >= 3
    accepted_events = [
        event for event in runtime.events()
        if event["phase"] == "tool_output" and event["details"].get("tool") == "native_battle_action"
    ]
    assert accepted_events[-1]["summary"] == "Native battle action accepted."


def test_battle_delegate_completes_when_last_battle_appears_before_battle_clears(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    command = {
        **make_state(), "game_instance_id": "battle-game", "in_battle": True,
        "battle": {
            "generation": 5, "command_generation": 9, "phase": "command",
            "current_player_index": 0,
            "available_actions": [{
                "action_type": "attack", "object_id": 0,
                "target_mode": "single", "valid_target_indices": [0],
            }],
            "enemies": [],
        },
    }
    current = {"state": command}
    debug.snapshot = lambda: current["state"]
    runtime.battle_decision_provider = lambda *_args: {
        "action_type": "attack", "object_id": 0, "target_index": 0,
    }

    def execute(args):
        current["state"] = {
            **command,
            "battle": {**command["battle"], "phase": "resolving"},
            "last_battle": {
                "generation": 5, "result": "won", "players": [], "enemies": [],
            },
        }
        return {**args, "status": "submitted"}

    debug.write_battle_action = execute

    summary = asyncio.run(runtime.battle_delegate.run(command))

    assert summary["result"] == "won"
    assert runtime.snapshot()["battle_delegate"]["instruction_count"] == 1


def test_battle_delegate_discards_decision_when_command_window_advances(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime

    def command(command_generation):
        return {
            **make_state(),
            "game_instance_id": "battle-game",
            "in_battle": True,
            "battle": {
                "generation": 5, "command_generation": command_generation,
                "phase": "command", "current_player_index": 0,
                "available_actions": [{
                    "action_type": "attack", "object_id": 0,
                    "target_mode": "single", "valid_target_indices": [0],
                }],
                "enemies": [],
            },
        }

    current = {"state": command(1)}
    choices = []
    writes = []
    debug.snapshot = lambda: current["state"]

    def choose(state, _model, _feedback):
        choices.append(state["battle"]["command_generation"])
        if len(choices) == 1:
            current["state"] = command(2)
        return {"action_type": "attack", "object_id": 0, "target_index": 0}

    def execute(args):
        writes.append(dict(args))
        current["state"] = {
            **make_state(), "game_instance_id": "battle-game", "in_battle": False,
            "battle": None,
            "last_battle": {"generation": 5, "result": "won", "players": [], "enemies": []},
        }
        return dict(args)

    runtime.battle_decision_provider = choose
    debug.write_battle_action = execute

    summary = asyncio.run(runtime.battle_delegate.run(current["state"]))

    assert summary["result"] == "won"
    assert choices == [1, 2]
    assert [entry["command_generation"] for entry in writes] == [2]
    assert not [event for event in runtime.events() if event["phase"] == "retry"]


def test_battle_delegate_never_rewrites_decision_to_new_battle_generation(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    old = {
        **make_state(), "game_instance_id": "battle-game", "in_battle": True,
        "battle": {
            "generation": 2, "command_generation": 4, "phase": "command",
            "current_player_index": 0, "available_actions": [{
                "action_type": "flee", "object_id": 0,
                "target_mode": "none", "valid_target_indices": [],
            }],
            "enemies": [],
        },
    }
    new = json.loads(json.dumps(old))
    new["battle"]["generation"] = 3
    new["battle"]["command_generation"] = 1
    new["last_battle"] = {"generation": 2, "result": "fled", "players": [], "enemies": []}
    current = {"state": old}
    writes = []
    debug.snapshot = lambda: current["state"]

    def choose(*_args):
        current["state"] = new
        return {"action_type": "flee", "object_id": 0, "target_index": -2}

    runtime.battle_decision_provider = choose
    debug.write_battle_action = lambda args: writes.append(args)

    summary = asyncio.run(runtime.battle_delegate.run(old))

    assert summary["result"] == "fled"
    assert writes == []


def test_battle_delegate_treats_native_stale_command_as_resample(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime

    def command(command_generation):
        state = {
            **make_state(), "game_instance_id": "battle-game", "in_battle": True,
            "battle": {
                "generation": 5, "command_generation": command_generation,
                "phase": "command", "current_player_index": 0,
                "available_actions": [{
                    "action_type": "attack", "object_id": 0,
                    "target_mode": "single", "valid_target_indices": [0],
                }],
                "enemies": [],
            },
        }
        return state

    current = {"state": command(1)}
    attempts = []
    debug.snapshot = lambda: current["state"]
    runtime.battle_decision_provider = lambda *_args: {
        "action_type": "attack", "object_id": 0, "target_index": 0,
    }

    def execute(args):
        attempts.append(args["command_generation"])
        if len(attempts) == 1:
            current["state"] = command(2)
            raise RuntimeError("stale_battle_command")
        current["state"] = {
            **make_state(), "game_instance_id": "battle-game", "in_battle": False,
            "battle": None,
            "last_battle": {"generation": 5, "result": "won", "players": [], "enemies": []},
        }
        return dict(args)

    debug.write_battle_action = execute

    summary = asyncio.run(runtime.battle_delegate.run(current["state"]))

    assert summary["result"] == "won"
    assert attempts == [1, 2]
    assert not [event for event in runtime.events() if event["phase"] == "retry"]


def test_battle_delegate_reward_continue_completes_normally(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    reward = {
        **make_state(), "game_instance_id": "battle-game", "in_battle": True,
        "battle": {
            "generation": 5, "command_generation": 10, "phase": "reward",
            "current_player_index": 0,
            "available_actions": [{
                "action_type": "continue", "object_id": 0,
                "target_mode": "none", "valid_target_indices": [],
            }],
            "enemies": [],
        },
    }
    ended = {
        **make_state(), "game_instance_id": "battle-game", "in_battle": False,
        "battle": None,
        "last_battle": {"generation": 5, "result": "won", "players": [], "enemies": []},
    }
    current = {"state": reward}
    debug.snapshot = lambda: current["state"]
    runtime.battle_decision_provider = lambda *_args: {
        "action_type": "continue", "object_id": 0, "target_index": -2,
    }

    def execute(args):
        current["state"] = ended
        return {**args, "status": "submitted"}

    debug.write_battle_action = execute

    summary = asyncio.run(runtime.battle_delegate.run(reward))

    assert summary["result"] == "won"
    assert runtime.snapshot()["battle_delegate"]["instruction_count"] == 1


def test_battle_delegate_reports_generation_change_without_matching_last_battle(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    old = {
        **make_state(), "game_instance_id": "battle-game", "in_battle": True,
        "battle": {
            "generation": 2, "command_generation": 4, "phase": "command",
            "current_player_index": 0, "available_actions": [], "enemies": [],
        },
    }
    new = json.loads(json.dumps(old))
    new["battle"]["generation"] = 3
    new["battle"]["command_generation"] = 1
    runtime.battle_delegate._begin(old)
    debug.snapshot = lambda: new

    with pytest.raises(RuntimeError, match="battle_generation_changed"):
        runtime.battle_delegate._wait_for_control_or_end(2)


def test_battle_delegate_control_wait_timeout_is_terminal_error(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    command = {
        **make_state(), "game_instance_id": "battle-game", "in_battle": True,
        "battle": {
            "generation": 5, "command_generation": 9, "phase": "command",
            "current_player_index": 0,
            "available_actions": [{
                "action_type": "attack", "object_id": 0,
                "target_mode": "single", "valid_target_indices": [0],
            }],
            "enemies": [],
        },
    }
    debug.snapshot = lambda: command
    runtime.battle_decision_provider = lambda *_args: {
        "action_type": "attack", "object_id": 0, "target_index": 0,
    }
    debug.write_battle_action = lambda args: {**args, "status": "submitted"}
    monkeypatch.setattr("palpal.agent_runtime.BATTLE_CONTROL_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(RuntimeError, match="battle_control_timeout"):
        asyncio.run(runtime.battle_delegate.run(command))


def test_battle_sdk_emits_complete_tool_trace(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    battle = {
        "generation": 5,
        "command_generation": 9,
        "phase": "command",
        "current_player_index": 0,
        "available_actions": [{
            "action_type": "attack",
            "object_id": 0,
            "target_mode": "single",
            "valid_target_indices": [0],
        }, {
            "action_type": "magic",
            "object_id": 327,
            "name": "炎咒",
            "mp_cost": 8,
            "description": "灼熱火焰\n攻擊敵人",
            "target_mode": "single",
            "valid_target_indices": [0],
            "submission_target_indices": [0],
        }, {
            "action_type": "use_item",
            "object_id": 99,
            "target_mode": "single",
            "valid_target_indices": [0],
            "submission_target_indices": [0],
        }],
    }
    state = {
        **make_state(),
        "game_instance_id": "battle-game",
        "in_battle": True,
        "battle": battle,
        "inventory": [{
            "slot": 0, "item_id": 99, "name": "止血草", "amount": 2,
            "description": "恢復少量體力",
            "amount_in_use": 0, "usable": True, "throwable": False,
            "consuming": True, "apply_to_all": False,
            "use_effect": {
                "script_entry": 39208, "preview_complete": True,
                "effects": [{
                    "kind": "restore_hp", "amount": 50,
                    "conditional": False, "apply_to_all": False,
                }],
                "unparsed_effects": [],
            },
        }],
    }
    debug.snapshot = lambda: state
    debug.active_task_summary = lambda: []
    debug.battle_action_validation_error = lambda _state, _args: None
    debug.config_value = lambda key, default=None: "test-key" if key == "DEEPSEEK_API_KEY" else default
    runtime.battle_delegate._begin(state)
    captured = {}

    class FunctionTool:
        def __init__(self, **kwargs):
            self.name = kwargs["name"]
            self.kwargs = kwargs

    class Agent:
        def __init__(self, **kwargs):
            captured["agent_kwargs"] = kwargs
            captured["agent"] = self
            self.tools = kwargs["tools"]
            self.tool_use_behavior = kwargs["tool_use_behavior"]

    class Stream:
        def __init__(self, attempt):
            self.attempt = attempt
            self.context_wrapper = types.SimpleNamespace(
                usage=provider_usage(100 + attempt, 0, 5, 3),
            )

        async def stream_events(self):
            if self.attempt == 1:
                if False:
                    yield None
                return
            tools = {tool.name: tool for tool in captured["agent"].tools}
            get_output = await tools["get_battle"].kwargs["on_invoke_tool"](None, "{}")
            tool_battle = json.loads(get_output)["battle"]
            assert tool_battle["generation"] == 5
            assert tool_battle["available_actions"][1]["description"] == "灼熱火焰\n攻擊敵人"
            item_output = await tools["get_item_details"].kwargs["on_invoke_tool"](
                None, json.dumps({"item_id": 99}),
            )
            item = json.loads(item_output)
            assert item["item"]["available_amount"] == 2
            assert item["item"]["description"] == "恢復少量體力"
            assert "use_effect" not in item["item"]
            assert "effect_available" not in item["item"]
            submit_output = await tools["submit_battle_action"].kwargs["on_invoke_tool"](
                None,
                json.dumps({"action_type": "use_item", "object_id": 99, "target_index": 0}),
            )
            result = types.SimpleNamespace(tool=tools["submit_battle_action"], output=submit_output)
            assert captured["agent"].tool_use_behavior(None, [result]).is_final_output is True
            if False:
                yield None

        def cancel(self, _reason):
            return None

    class Runner:
        @classmethod
        def run_streamed(cls, _agent, prompt, **kwargs):
            captured.setdefault("runs", []).append({"prompt": prompt, **kwargs})
            return Stream(len(captured["runs"]))

    fake_agents = types.ModuleType("agents")
    fake_agents.Agent = Agent
    fake_agents.AsyncOpenAI = lambda **kwargs: object()
    fake_agents.FunctionTool = FunctionTool
    fake_agents.ModelSettings = lambda **kwargs: kwargs
    fake_agents.OpenAIResponsesModel = lambda **kwargs: captured.setdefault("model", kwargs) or object()
    fake_agents.Runner = Runner
    fake_agents.SQLiteSession = lambda *_args: object()
    fake_agents.set_tracing_disabled = lambda **_kwargs: None
    fake_agent_module = types.ModuleType("agents.agent")
    fake_agent_module.ToolsToFinalOutputResult = lambda **kwargs: types.SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    monkeypatch.setitem(sys.modules, "agents.agent", fake_agent_module)

    action = asyncio.run(runtime.battle_delegate._sdk_action(state, "Choose an action."))

    assert action["action_type"] == "use_item"
    assert set(captured["model"]) == {"model", "openai_client"}
    assert captured["agent_kwargs"]["model_settings"] == {
        "include_usage": True,
        "reasoning": {"effort": "high"},
        "tool_choice": "auto",
    }
    assert captured["agent_kwargs"]["reset_tool_choice"] is False
    assert [run["max_turns"] for run in captured["runs"]] == [6, 6]
    initial_prompt = json.loads(captured["runs"][0]["prompt"])
    assert initial_prompt["battle"]["generation"] == 5
    assert "recent_outcomes" not in initial_prompt["battle"]
    assert "valid_target_indices" not in initial_prompt["battle"]["available_actions"][1]
    assert initial_prompt["battle"]["available_actions"][1]["description"] == "灼熱火焰\n攻擊敵人"
    assert set(initial_prompt) == {"battle", "feedback"}
    assert "Item and magic descriptions are player-visible menu text" in captured["agent_kwargs"]["instructions"]
    assert "do not call get_battle unless" in captured["agent_kwargs"]["instructions"]
    assert "without an accepted submit_battle_action" in captured["runs"][1]["prompt"]
    control = runtime.snapshot()["battle_delegate"]
    assert control["last_prompt_tokens"] == 102
    assert control["experience_sent"] is True
    events = [event for event in runtime.events() if event["type"] == "battle"]
    assert [event["phase"] for event in events] == [
        "started", "protocol_retry", "tool_called", "tool_output", "tool_called", "tool_output",
        "tool_called", "tool_output",
    ]
    assert [event["details"].get("tool") for event in events[2:]] == [
        "get_battle", "get_battle", "get_item_details", "get_item_details",
        "submit_battle_action", "submit_battle_action",
    ]
    assert events[3]["details"]["output"]["battle"]["generation"] == 5
    assert events[5]["details"]["output"]["item"]["available_amount"] == 2
    assert events[6]["details"]["arguments"] == {
        "action_type": "use_item", "object_id": 99, "target_index": 0,
    }
    assert events[7]["details"]["output"] == {"ok": True, "accepted": True}


def test_battle_sdk_two_non_submit_ends_raise_protocol_error(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    state = {
        **make_state(),
        "game_instance_id": "battle-game",
        "in_battle": True,
        "battle": {
            "generation": 5,
            "command_generation": 9,
            "phase": "command",
            "current_player_index": 0,
            "available_actions": [{
                "action_type": "attack", "object_id": 0,
                "target_mode": "single", "valid_target_indices": [0],
            }],
        },
    }
    debug.config_value = lambda key, default=None: "test-key" if key == "DEEPSEEK_API_KEY" else default
    runtime.battle_delegate._begin(state)
    captured = {"runs": []}

    class Stream:
        async def stream_events(self):
            if False: yield None

    class Runner:
        @classmethod
        def run_streamed(cls, _agent, prompt, **kwargs):
            captured["runs"].append({"prompt": prompt, **kwargs})
            return Stream()

    fake_agents = types.ModuleType("agents")
    fake_agents.Agent = lambda **kwargs: object()
    fake_agents.AsyncOpenAI = lambda **kwargs: object()
    fake_agents.FunctionTool = lambda **kwargs: object()
    fake_agents.ModelSettings = lambda **kwargs: kwargs
    fake_agents.OpenAIResponsesModel = lambda **kwargs: object()
    fake_agents.Runner = Runner
    fake_agents.SQLiteSession = lambda *_args: object()
    fake_agents.set_tracing_disabled = lambda **kwargs: None
    fake_agent_module = types.ModuleType("agents.agent")
    fake_agent_module.ToolsToFinalOutputResult = lambda **kwargs: types.SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    monkeypatch.setitem(sys.modules, "agents.agent", fake_agent_module)

    with pytest.raises(RuntimeError, match="submit_battle_action_protocol_error"):
        asyncio.run(runtime.battle_delegate._sdk_action(state, "Choose an action."))

    assert [run["max_turns"] for run in captured["runs"]] == [6, 6]
    assert "without an accepted submit_battle_action" in captured["runs"][1]["prompt"]


def test_battle_delegate_failure_and_interrupt_emit_terminal_trace(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    command = {
        **make_state(),
        "game_instance_id": "battle-game",
        "in_battle": True,
        "battle": {
            "generation": 5,
            "command_generation": 9,
            "phase": "command",
            "current_player_index": 0,
            "available_actions": [{
                "action_type": "attack", "object_id": 0,
                "target_mode": "single", "valid_target_indices": [0],
            }],
        },
        "control": {
            "owner": "client", "phase": "battle",
            "allowed_actions": ["battle_action", "press_buttons"],
            "navigation_ready": False,
        },
        "_debug": {"runtime_stale": False},
    }
    debug.snapshot = lambda: command
    debug.write_battle_action = lambda _args: (_ for _ in ()).throw(RuntimeError("action_unavailable"))
    runtime.battle_decision_provider = lambda _state, _model, _feedback: {
        "action_type": "attack", "object_id": 0, "target_index": 0,
    }

    runtime.start(None, 1)
    paused = wait_for_pause(runtime)

    assert paused["battle_delegate"]["status"] == "failed"
    battle_events = [event for event in runtime.events() if event["type"] == "battle"]
    assert [event["phase"] for event in battle_events].count("retry") == 3
    assert battle_events[-1]["phase"] == "failed"
    assert battle_events[-1]["details"]["reason"] == "battle_replan_exhausted"

    interrupted_root = tmp_path / "interrupted"
    interrupted_root.mkdir()
    interrupted_debug = make_debug_state(interrupted_root)
    interrupted = interrupted_debug.agent_runtime
    interrupted.state.update(status="running", phase="deciding")
    interrupted.battle_delegate._begin(command)
    interrupted.interrupt()
    stopped = [event for event in interrupted.events() if event["type"] == "battle"]
    assert stopped[-1]["phase"] == "stopped"
    assert interrupted.snapshot()["battle_delegate"]["status"] == "stopped"


def test_prebattle_dialog_uses_main_agent_then_delegates_at_command(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    prebattle = {
        **make_state(),
        "game_instance_id": "battle-game",
        "in_battle": True,
        "battle": {
            "generation": 5, "phase": "pre_battle", "result": "terminated", "enemies": [],
        },
        "dialog": {"active": True, "latest_event": {"message_id": 10, "text": "来吧"}},
        "control": {
            "owner": "client", "phase": "dialog",
            "allowed_actions": ["advance_dialog"], "navigation_ready": False,
        },
        "_debug": {"runtime_stale": False},
    }
    command = {
        **prebattle,
        "dialog": {"active": False},
        "battle": {
            "generation": 5, "command_generation": 1, "phase": "command",
            "current_player_index": 0, "available_actions": [], "enemies": [],
        },
        "control": {
            "owner": "client", "phase": "battle",
            "allowed_actions": ["battle_action", "press_buttons"], "navigation_ready": False,
        },
    }
    current = {"state": prebattle}
    debug.snapshot = lambda: current["state"]
    runtime.decision_provider = lambda *_args: decision(
        "advance_dialog", {"max_presses": 1}, "advance_dialog",
    )
    monkeypatch.setattr(runtime, "_execute_decision", lambda _decision: current.update(state=command) or {
        "ok": True, "tool": "advance_dialog", "delta": {},
    })
    delegated = []

    async def delegate(state):
        delegated.append(state["battle"]["phase"])
        return {"result": "lost"}

    monkeypatch.setattr(runtime.battle_delegate, "run", delegate)

    runtime.start(None, 2)
    paused = wait_for_pause(runtime)

    assert delegated == ["command"]
    assert paused["stop_reason"] == "battle_lost"
    assert paused["run_turn"] == 1
    assert not [
        event for event in runtime.events()
        if event["type"] == "action" and event["phase"] == "failed"
    ]


def test_battle_delegate_reuses_child_session_for_same_generation(tmp_path):
    runtime = make_debug_state(tmp_path).agent_runtime
    state = {
        **make_state(),
        "game_instance_id": "battle-game",
        "in_battle": True,
        "battle": {"generation": 5, "phase": "command", "enemies": []},
    }

    first = runtime.battle_delegate._begin(state)
    second = runtime.battle_delegate._begin(state)

    assert second["child_session_id"] == first["child_session_id"]
    assert second["battle_generation"] == 5


def test_battle_agent_view_compacts_experience_and_only_sends_new_outcomes(tmp_path):
    delegate = make_debug_state(tmp_path).agent_runtime.battle_delegate
    verbose_sample = {
        "sequence": 3,
        "actor": {"index": 0, "role_id": 0, "name": "hero"},
        "action_type": "magic",
        "object_id": 345,
        "name": "spell",
        "mp_cost": 10,
        "game_instance_id": "game",
        "battle_generation": 3,
        "enemy_changes": [{
            "slot_index": 0, "object_id": 81, "name": "enemy",
            "amount": 90, "kind": "damage",
        }],
        "player_changes": [{
            "index": 0,
            "hp": {"before": 100, "after": 100, "max": 100},
            "mp": {"before": 50, "after": 40, "max": 50},
            "status_before": [], "status_after": [],
            "poisons_before": [], "poisons_after": [],
        }],
        "numbers": [{"side": "enemy", "amount": 90, "kind": "damage"}],
    }
    view = {
        "generation": 4,
        "command_generation": 2,
        "current_player_index": 0,
        "players": [{"index": 0, "role_id": 0, "name": "hero"}],
        "enemies": [{"slot_index": 0, "object_id": 81, "name": "enemy"}],
        "available_actions": [{
            "action_type": "magic", "object_id": 345, "name": "spell",
            "target_side": "enemy", "target_mode": "single",
            "valid_target_indices": [0], "submission_target_indices": [0],
            "mp_cost": 10,
        }],
        "recent_outcomes": [
            {**verbose_sample, "sequence": 1},
            {**verbose_sample, "sequence": 2},
        ],
        "experience": {
            "available": True,
            "enemy_object_ids": [81],
            "signature": "81",
            "actions": [{
                "actor_role_id": 0, "actor_name": "hero", "action_type": "magic",
                "object_id": 345, "name": "spell", "enemy_object_id": 81,
                "samples": [{**verbose_sample, "sequence": value} for value in range(1, 6)],
            }, {
                "actor_role_id": 2, "actor_name": "other", "action_type": "magic",
                "object_id": 999, "name": "other spell", "enemy_object_id": 81,
                "samples": [verbose_sample],
            }],
            "encounter": {
                "samples": [{
                    "result": "won",
                    "tactics": [verbose_sample],
                }],
            },
        },
    }

    compact = delegate._agent_battle_view(
        view, include_experience=True, after_outcome_sequence=1,
    )

    assert compact["new_outcomes"] == [delegate._compact_outcome({**verbose_sample, "sequence": 2})]
    assert "valid_target_indices" not in compact["available_actions"][0]
    assert compact["available_actions"][0]["submission_target_indices"] == [0]
    assert len(compact["experience"]["actions"]) == 1
    action = compact["experience"]["actions"][0]
    assert action["sample_count"] == 5
    assert len(action["recent_samples"]) == 3
    assert all("numbers" not in sample for sample in action["recent_samples"])
    assert all("actor" not in sample for sample in action["recent_samples"])
    assert action["recent_samples"][-1]["player_changes"] == [{"index": 0, "mp_delta": -10}]
    later = delegate._agent_battle_view(
        view, include_experience=False, after_outcome_sequence=2,
    )
    assert "experience" not in later
    assert "new_outcomes" not in later


def test_battle_context_compaction_is_battle_local_and_atomic(tmp_path, monkeypatch):
    runtime = make_debug_state(tmp_path).agent_runtime
    delegate = runtime.battle_delegate
    delegate._begin({
        **make_state(),
        "game_instance_id": "battle-game",
        "battle": {"generation": 5, "phase": "command", "enemies": []},
    })
    delegate._persist_control(
        last_prompt_tokens=1000,
        instruction_count=3,
        actions=[{"battle_generation": 5, "command_generation": 1, "action_type": "attack"}],
    )
    monkeypatch.setenv("PALPAL_BATTLE_COMPACT_AT_TOKENS", "1000")
    monkeypatch.setenv("PALPAL_BATTLE_COMPACT_RETAIN_TOKENS", "1")
    original = [
        {"role": "user", "content": "old one"},
        {"role": "assistant", "content": "done one"},
        {"role": "user", "content": "old two"},
        {"role": "assistant", "content": "done two"},
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "done latest"},
    ]
    session = MemorySession(original)

    assert asyncio.run(delegate._maybe_compact_sdk(session, '{"battle":{}}')) is True

    summary = json.loads(session.items[0]["content"])["battle_history_summary"]
    assert summary["battle_generation"] == 5
    assert summary["submitted_actions"][0]["action_type"] == "attack"
    assert session.items[-2:] == original[-2:]
    control = runtime.snapshot()["battle_delegate"]
    assert control["last_prompt_tokens"] is None
    assert control["compactions"] == 1
    assert control["last_compaction"]["retained_cycles"] == 1
    phases = [event["phase"] for event in runtime.events() if event["type"] == "battle"]
    assert phases[-2:] == ["compression_started", "compression_completed"]


def test_battle_context_compaction_restores_history_on_failure(tmp_path, monkeypatch):
    runtime = make_debug_state(tmp_path).agent_runtime
    delegate = runtime.battle_delegate
    delegate._begin({
        **make_state(),
        "game_instance_id": "battle-game",
        "battle": {"generation": 5, "phase": "command", "enemies": []},
    })
    delegate._persist_control(last_prompt_tokens=1000)
    monkeypatch.setenv("PALPAL_BATTLE_COMPACT_AT_TOKENS", "1000")
    original = [
        {"role": "user", "content": f"turn {index}"}
        if index % 2 == 0 else {"role": "assistant", "content": f"done {index}"}
        for index in range(6)
    ]

    class FailingSession(MemorySession):
        def __init__(self, items):
            super().__init__(items)
            self.fail_once = True

        async def add_items(self, items):
            if self.fail_once:
                self.fail_once = False
                raise OSError("write failed")
            await super().add_items(items)

    session = FailingSession(original)
    with pytest.raises(RuntimeError, match="battle_context_compaction_failed"):
        asyncio.run(delegate._maybe_compact_sdk(session, '{"battle":{}}'))

    assert session.items == original
    assert runtime.snapshot()["battle_delegate"]["compactions"] == 0
    assert runtime.events()[-1]["phase"] == "compression_failed"


def test_decide_once_is_disabled_in_battle(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    debug.snapshot = lambda: {**make_state(), "in_battle": True, "battle": {"generation": 1}}

    with pytest.raises(RuntimeError, match="decide_once_unavailable_in_battle"):
        runtime.start(None, 1, decide_only=True)


def test_equipment_review_tracks_total_ownership_and_ignores_consumables(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    base = make_state()
    base['party'] = [{'role_id': 0, 'equipment': []}]
    base['inventory'] = []
    runtime.observe_equipment_state(base)
    assert runtime.pending_equipment_review() is None

    acquired = json.loads(json.dumps(base))
    acquired['inventory'] = [{
        'item_id': 105, 'name': '还神丹', 'amount': 1, 'amount_in_use': 0,
        'usable': True, 'equipable': False,
    }, {
        'item_id': 260, 'name': '圣灵珠', 'amount': 1, 'amount_in_use': 0,
        'usable': True, 'equipable': True,
    }]
    runtime.observe_equipment_state(acquired)

    assert runtime.pending_equipment_review() == {
        'item_id': 260,
        'name': '圣灵珠',
        'new_count': 1,
        'first_seen_at': runtime.pending_equipment_review()['first_seen_at'],
    }


def test_equipment_review_does_not_treat_swapped_old_item_as_new(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    old = {'item_id': 196, 'name': '头巾', 'equipable': True, 'slot_id': 0}
    candidate = {
        'item_id': 201, 'name': '皮帽', 'amount': 1, 'amount_in_use': 0,
        'equipable': True,
    }
    before = make_state()
    before['party'] = [{'role_id': 0, 'equipment': [old]}]
    before['inventory'] = [candidate]
    runtime.observe_equipment_state(before)

    after = make_state()
    after['party'] = [{'role_id': 0, 'equipment': [{**candidate, 'slot_id': 0}]}]
    after['inventory'] = [{
        'item_id': 196, 'name': '头巾', 'amount': 1, 'amount_in_use': 0,
        'equipable': True,
    }]
    runtime.observe_equipment_state(after)

    assert runtime.pending_equipment_review() is None


def test_equipment_review_multiple_copies_clamps_after_one_is_equipped(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    base = make_state()
    base['party'] = [{'role_id': 0, 'equipment': []}]
    base['inventory'] = []
    runtime.observe_equipment_state(base)
    acquired = json.loads(json.dumps(base))
    acquired['inventory'] = [{
        'item_id': 201, 'name': '皮帽', 'amount': 2, 'amount_in_use': 0,
        'equipable': True,
    }]
    runtime.observe_equipment_state(acquired)
    assert runtime.pending_equipment_review()['new_count'] == 2

    equipped = json.loads(json.dumps(acquired))
    equipped['inventory'][0]['amount'] = 1
    equipped['party'][0]['equipment'] = [{
        'item_id': 201, 'name': '皮帽', 'equipable': True, 'slot_id': 0,
    }]
    runtime.observe_equipment_state(equipped)

    assert runtime.pending_equipment_review()['new_count'] == 1


def test_runtime_replans_rejected_recommendation_without_counting_turn(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    runtime._unsafe_state_reason = lambda state: None
    rejected = decision(
        "no_op",
        {"reason": "navigation_target_invalid", "target_id": 52, "route_status": "unreachable_static"},
        "wait_runtime",
    )
    rejected["validation"] = {
        "status": "rejected",
        "reason": "navigation_target_invalid",
        "target_id": 52,
        "route_status": "unreachable_static",
        "route_ok": True,
    }
    accepted = decision("press_buttons", {"buttons": ["search"]}, "inspect_target")
    recommendations = [rejected, accepted]
    feedback = []

    def provide(state, model, current_feedback):
        feedback.append(current_feedback)
        return recommendations.pop(0)

    runtime.decision_provider = provide
    runtime._execute_decision = lambda value: {"ok": True, "delta": {"stable": True}}
    runtime.start(None, 1)
    paused = wait_for_pause(runtime)

    assert paused["stop_reason"] == "turn_limit"
    assert paused["turn"] == 1
    assert paused["run_turn"] == 1
    assert paused["usage"]["total_tokens"] == 30
    assert "navigation_target_invalid" in feedback[1]
    discarded = [event for event in runtime.events() if event["phase"] == "discarded"]
    assert len(discarded) == 1
    assert discarded[0]["details"]["validation"]["target_id"] == 52


def test_runtime_pauses_with_details_after_replan_rejections_are_exhausted(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    runtime._unsafe_state_reason = lambda state: None

    def provide(state, model, feedback):
        value = decision(
            "no_op",
            {"reason": "navigation_target_invalid", "target_id": 63, "route_status": "unreachable_static"},
            "wait_runtime",
        )
        value["validation"] = {
            "status": "rejected",
            "reason": "navigation_target_invalid",
            "target_id": 63,
            "route_status": "unreachable_static",
            "route_ok": True,
        }
        return value

    runtime.decision_provider = provide
    runtime.start(None, 1)
    paused = wait_for_pause(runtime)

    assert paused["stop_reason"] == "replan_exhausted"
    assert paused["turn"] == 0
    assert paused["run_turn"] == 0
    assert paused["usage"]["total_tokens"] == 45
    pause = [event for event in runtime.events() if event["type"] == "pause"][-1]
    assert pause["summary"] == "Agent paused: no executable recommendation after 3 replans."
    assert pause["details"]["validation"]["target_id"] == 63
    assert len([event for event in runtime.events() if event["phase"] == "discarded"]) == 3


def test_successful_execution_resets_consecutive_replan_budget(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    runtime._unsafe_state_reason = lambda state: None

    def rejected(target_id):
        value = decision(
            "no_op",
            {"reason": "navigation_target_invalid", "target_id": target_id},
            "wait_runtime",
        )
        value["validation"] = {
            "status": "rejected",
            "reason": "navigation_target_invalid",
            "target_id": target_id,
            "route_status": "unreachable_static",
        }
        return value

    def accepted():
        return decision("press_buttons", {"buttons": ["search"]}, "inspect_target")

    recommendations = [rejected(48), accepted(), rejected(52), rejected(63), accepted()]
    runtime.decision_provider = lambda state, model, feedback: recommendations.pop(0)
    runtime._execute_decision = lambda value: {"ok": True, "delta": {"stable": True}}

    runtime.start(None, 2)
    paused = wait_for_pause(runtime)
    assert paused["stop_reason"] == "turn_limit"
    assert paused["turn"] == 2
    assert paused["run_turn"] == 2
    assert paused["usage"]["total_tokens"] == 75


def test_runtime_executes_exact_turn_budget_in_same_session(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    runtime._unsafe_state_reason = lambda state: None
    runtime.decision_provider = lambda state, model, feedback: decision(
        "press_buttons", {"buttons": ["search"]}, "inspect_target"
    )
    runtime._execute_decision = lambda value: {"ok": True, "delta": {"stable": True}}
    session_id = runtime.snapshot()["session_id"]

    runtime.start(None, 3)
    first = wait_for_pause(runtime)
    assert first["stop_reason"] == "turn_limit"
    assert first["turn"] == 3
    assert first["run_turn"] == 3

    runtime.start(None, 2)
    second = wait_for_pause(runtime)
    assert second["session_id"] == session_id
    assert second["turn"] == 5
    assert second["run_turn"] == 2


def test_runtime_restart_pauses_orphaned_run(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    control = runtime.snapshot()
    control.update(status="running", phase="deciding", stop_reason=None)
    runtime._atomic_json(runtime.control_path, control)

    restored = AgentRuntime(debug, debug.runtime_dir).snapshot()
    assert restored["status"] == "paused"
    assert restored["stop_reason"] == "server_restarted"


def test_game_instance_change_rotates_session_but_server_restart_does_not(tmp_path):
    debug = make_debug_state(tmp_path)
    state_a = make_state()
    state_a["game_instance_id"] = "game-a"
    write_state(debug, state_a)
    debug.snapshot()
    runtime = debug.agent_runtime
    first_session_id = runtime.snapshot()["session_id"]
    runtime.state["turn"] = 3
    runtime._persist()
    runtime.emit("decision", "ready", "Old game activity.")

    restarted_debug = make_debug_state(tmp_path)
    restarted_debug.snapshot()
    assert restarted_debug.agent_runtime.snapshot()["session_id"] == first_session_id
    assert restarted_debug.agent_runtime.snapshot()["turn"] == 3

    state_b = make_state()
    state_b["game_instance_id"] = "game-b"
    write_state(restarted_debug, state_b)
    restarted_debug.snapshot()
    current = restarted_debug.agent_runtime.snapshot()
    assert current["session_id"] != first_session_id
    assert current["game_instance_id"] == "game-b"
    assert current["turn"] == 0
    assert current["stop_reason"] == "game_instance_changed"
    assert all(event["summary"] != "Old game activity." for event in restarted_debug.agent_runtime.events(limit=500))


def test_game_instance_change_interrupts_before_rotating_running_session(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    runtime.state.update(game_instance_id="game-a", status="running", phase="deciding", turn=2)
    runtime._persist()
    old_session_id = runtime.snapshot()["session_id"]

    runtime.observe_game_instance("game-b")
    interrupting = runtime.snapshot()
    assert interrupting["status"] == "interrupting"
    assert interrupting["pending_game_instance_id"] == "game-b"
    assert runtime.cancel_event.is_set()

    runtime._pause("interrupted")
    current = runtime.snapshot()
    assert current["session_id"] != old_session_id
    assert current["game_instance_id"] == "game-b"
    assert current["status"] == "paused"
    assert current["stop_reason"] == "game_instance_changed"


def test_manual_execute_writes_action_and_records_delta(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(frame=7)
    write_state(debug, state)
    debug.set_live_frame(b"frame")
    runtime = debug.agent_runtime
    pending = decision("press_buttons", {"buttons": ["search"], "frames": 1}, "inspect_target")
    pending = debug.attach_decision_context(debug.snapshot(), pending)
    runtime.state["pending_decision"] = pending
    runtime._persist()

    def consume():
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if debug.action_path.exists():
                debug.action_path.unlink()
                return
            time.sleep(0.01)

    thread = threading.Thread(target=consume)
    thread.start()
    result = runtime.execute()
    thread.join(timeout=2)
    assert result["ok"] is True
    assert result["tool"] == "press_buttons"
    assert runtime.snapshot()["stop_reason"] == "manual_execute_complete"
    assert runtime.snapshot()["pending_decision"] is None


def test_interrupt_is_idempotent_and_reset_rejects_running(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    runtime.state.update(status="running", phase="deciding")
    with pytest.raises(RuntimeError, match="agent_session_running"):
        runtime.reset()
    interrupted = runtime.interrupt()
    assert interrupted["status"] == "interrupting"
    assert runtime.interrupt()["status"] == "interrupting"


def test_events_are_session_isolated_after_reset(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    old_id = runtime.snapshot()["session_id"]
    runtime.emit("tool", "completed", "old")
    runtime.reset("test")
    assert runtime.snapshot()["session_id"] != old_id
    assert all(event["session_id"] == runtime.snapshot()["session_id"] for event in runtime.events())
    assert not any(event["summary"] == "old" for event in runtime.events())


def test_idle_event_wait_does_not_scan_event_log(tmp_path, monkeypatch):
    runtime = make_debug_state(tmp_path).agent_runtime
    runtime.emit("session", "paused", "ready")
    after = runtime.snapshot()["last_event_seq"]

    def unexpected_scan(*args, **kwargs):
        raise AssertionError("idle live wait scanned events.jsonl")

    monkeypatch.setattr(runtime, "_scan_events", unexpected_scan)
    events, session_current = runtime.wait_for_events(
        after,
        session_id=runtime.snapshot()["session_id"],
        timeout=0.02,
    )

    assert events == []
    assert session_current is True


def test_event_wait_wakes_immediately_in_sequence_order(tmp_path):
    runtime = make_debug_state(tmp_path).agent_runtime
    session_id = runtime.snapshot()["session_id"]
    result = []

    def wait():
        result.append(runtime.wait_for_events(0, session_id=session_id, timeout=1.0))

    thread = threading.Thread(target=wait)
    thread.start()
    time.sleep(0.03)
    with runtime.lock:
        first = runtime.emit("model", "request", "first")
        second = runtime.emit("decision", "ready", "second")
    thread.join(timeout=0.5)

    assert not thread.is_alive()
    events, session_current = result[0]
    assert session_current is True
    assert [event["seq"] for event in events] == [first["seq"], second["seq"]]


def test_old_event_cursor_falls_back_to_disk_after_cache_overflow(tmp_path, monkeypatch):
    runtime = make_debug_state(tmp_path).agent_runtime
    for index in range(1_005):
        runtime.emit("tool", "completed", f"event {index}")
    scanned = 0
    original_scan = runtime._scan_events

    def recording_scan(*args, **kwargs):
        nonlocal scanned
        scanned += 1
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(runtime, "_scan_events", recording_scan)
    events = runtime.events(after=0, limit=3)

    assert scanned == 1
    assert [event["summary"] for event in events] == ["event 0", "event 1", "event 2"]


def test_restart_restores_event_tail_sequence_and_cache(tmp_path):
    agent_dir = tmp_path / "runtime" / "agent"
    agent_dir.mkdir(parents=True)
    session_id = "a" * 32
    lines = [
        json.dumps({"seq": seq, "session_id": session_id, "summary": f"event {seq}"})
        for seq in range(1, 1_101)
    ]
    (agent_dir / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    runtime = make_debug_state(tmp_path).agent_runtime

    assert runtime.snapshot()["last_event_seq"] == 1_100
    assert len(runtime.event_cache) == 1_000
    assert runtime.event_cache[0]["seq"] == 101


def test_restart_ignores_damaged_event_log_tail(tmp_path):
    agent_dir = tmp_path / "runtime" / "agent"
    agent_dir.mkdir(parents=True)
    session_id = "b" * 32
    (agent_dir / "events.jsonl").write_text(
        json.dumps({"seq": 7, "session_id": session_id, "summary": "valid"}) + "\n"
        + '{"seq":8,"session_id":',
        encoding="utf-8",
    )

    runtime = make_debug_state(tmp_path).agent_runtime

    assert runtime.snapshot()["last_event_seq"] == 7
    assert [event["seq"] for event in runtime.event_cache] == [7]


def test_event_wait_ends_when_session_rotates(tmp_path):
    runtime = make_debug_state(tmp_path).agent_runtime
    old_session_id = runtime.snapshot()["session_id"]
    after = runtime.snapshot()["last_event_seq"]
    result = []
    thread = threading.Thread(
        target=lambda: result.append(runtime.wait_for_events(after, session_id=old_session_id, timeout=1.0))
    )
    thread.start()
    time.sleep(0.03)

    runtime.reset("branch_restored")
    thread.join(timeout=0.5)

    assert not thread.is_alive()
    assert result == [([], False)]


def test_event_turn_pages_keep_complete_turns_and_include_session_at_the_start(tmp_path):
    runtime = make_debug_state(tmp_path).agent_runtime
    runtime.emit("session", "paused", "session created")
    for turn in range(1, 13):
        runtime.state["turn"] = turn
        runtime.emit("model", "request", f"turn {turn} request")
        runtime.emit("decision", "ready", f"turn {turn} ready")

    latest = runtime.event_turn_page(turn_limit=5)
    assert {event["turn"] for event in latest["events"]} == {8, 9, 10, 11, 12}
    assert len(latest["events"]) == 10
    assert latest["oldest_turn"] == 8
    assert latest["has_more"] is True

    middle = runtime.event_turn_page(before_turn=latest["oldest_turn"], turn_limit=5)
    assert {event["turn"] for event in middle["events"]} == {3, 4, 5, 6, 7}
    assert middle["oldest_turn"] == 3
    assert middle["has_more"] is True

    oldest = runtime.event_turn_page(before_turn=middle["oldest_turn"], turn_limit=5)
    assert {event["turn"] for event in oldest["events"]} == {0, 1, 2}
    assert oldest["oldest_turn"] == 1
    assert oldest["has_more"] is False


def test_interrupt_discards_late_decision(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    runtime._unsafe_state_reason = lambda state: None

    async def slow_decision(state, model, feedback):
        await asyncio.sleep(0.15)
        return decision("press_buttons", {"buttons": ["search"]}, "inspect_target")

    runtime.decision_provider = slow_decision
    runtime.start(None, 3)
    deadline = time.monotonic() + 1
    while not any(event["type"] == "model" for event in runtime.events()) and time.monotonic() < deadline:
        time.sleep(0.01)
    runtime.interrupt()
    paused = wait_for_pause(runtime)
    assert paused["stop_reason"] == "interrupted"
    assert paused["turn"] == 0
    assert not debug.action_path.exists()
    assert any("late model result" in event["summary"] for event in runtime.events())


def test_context_compression_replaces_history_and_keeps_recent_items(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "1000")
    original = [{"role": "user", "content": f"item {index} " + ("x" * 500)} for index in range(8)]

    class Session:
        def __init__(self):
            self.items = list(original)

        async def get_items(self):
            return list(self.items)

        async def clear_session(self):
            self.items = []

        async def add_items(self, items):
            self.items.extend(items)

    class Responses:
        async def create(self, **kwargs):
            self.kwargs = kwargs
            return type("Response", (), {
                "status": "completed", "output_text": "compact summary",
            })()

    session = Session()
    client = type("Client", (), {"responses": Responses()})()
    runtime.state["last_prompt_tokens"] = 1000
    asyncio.run(runtime._maybe_compact_sdk(session, client, "next"))
    assert session.items[0]["content"].startswith("Persistent context summary:")
    assert session.items[-2:] == original[-2:]
    assert runtime.snapshot()["compactions"] == 1
    assert runtime.snapshot()["last_prompt_tokens"] is None
    assert runtime.snapshot()["task_reconciliation_pending"] is True
    assert runtime.snapshot()["task_reconciliation_reason"] == "context_compacted"
    compact_text = json.dumps(client.responses.kwargs)
    assert "permanent goal" not in compact_text.lower()
    assert "finish the game" not in compact_text.lower()
    assert client.responses.kwargs["reasoning"] == {"effort": "low"}
    events = [event for event in runtime.events() if event["type"] == "compression"]
    assert events[0]["details"]["observed_prompt_tokens"] == 1000
    assert events[-1]["phase"] == "completed"


@pytest.mark.parametrize(
    ("status", "output_text", "error"),
    [
        ("incomplete", "partial", "response status was incomplete"),
        ("failed", "", "response status was failed"),
        ("completed", "", "returned no content"),
    ],
)
def test_context_compression_response_failure_keeps_original_session(
    tmp_path, monkeypatch, status, output_text, error,
):
    runtime = make_debug_state(tmp_path).agent_runtime
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "1000")
    original = [{"role": "user", "content": f"item {index}"} for index in range(8)]
    session = MemorySession(original)

    class Responses:
        async def create(self, **_kwargs):
            return type("Response", (), {"status": status, "output_text": output_text})()

    client = type("Client", (), {"responses": Responses()})()
    runtime.state["last_prompt_tokens"] = 1000

    with pytest.raises(RuntimeError, match=error):
        asyncio.run(runtime._maybe_compact_sdk(session, client, "next"))

    assert session.items == original
    assert runtime.snapshot()["compactions"] == 0
    assert runtime.snapshot()["last_compaction"] is None
    assert runtime.snapshot()["last_prompt_tokens"] == 1000


def test_interrupt_cancels_inflight_context_compression_and_pauses_run(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    runtime._unsafe_state_reason = lambda _state: None
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "1000")
    original = [{"role": "user", "content": f"item {index}"} for index in range(8)]
    session = MemorySession(original)
    request_started = threading.Event()
    request_cancelled = threading.Event()

    class Responses:
        async def create(self, **_kwargs):
            request_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                request_cancelled.set()
                raise

    client = type("Client", (), {"responses": Responses()})()
    runtime.state["last_prompt_tokens"] = 1000

    async def compact_decision(_state, _feedback):
        await runtime._maybe_compact_sdk(session, client, "next")
        raise AssertionError("compression unexpectedly completed")

    runtime._decide = compact_decision
    runtime.start(None, 1)
    assert request_started.wait(1)
    runtime.interrupt()
    paused = wait_for_pause(runtime)

    assert paused["stop_reason"] == "interrupted"
    assert request_cancelled.wait(1)
    assert runtime.active_compression_task is None
    assert session.items == original
    assert paused["last_prompt_tokens"] == 1000
    assert paused["compactions"] == 0
    assert paused["last_compaction"] is None
    compression_events = [event for event in runtime.events() if event["type"] == "compression"]
    assert [event["phase"] for event in compression_events] == ["starting", "cancelled"]


def test_context_compression_interrupt_after_clear_restores_transcript(tmp_path, monkeypatch):
    runtime = make_debug_state(tmp_path).agent_runtime
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "1000")
    original = [{"role": "user", "content": f"item {index}"} for index in range(8)]

    class InterruptingSession(MemorySession):
        async def clear_session(self):
            await super().clear_session()
            if not runtime.cancel_event.is_set():
                runtime.state.update(status="running", phase="deciding")
                runtime.interrupt()

    class Responses:
        async def create(self, **_kwargs):
            return type("Response", (), {"status": "completed", "output_text": "summary"})()

    session = InterruptingSession(original)
    client = type("Client", (), {"responses": Responses()})()
    runtime.state["last_prompt_tokens"] = 1000
    before_reconciliation = (
        runtime.state["task_reconciliation_pending"],
        runtime.state["task_reconciliation_reason"],
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime._maybe_compact_sdk(session, client, "next"))

    assert session.items == original
    assert runtime.active_compression_task is None
    assert runtime.state["last_prompt_tokens"] == 1000
    assert runtime.state["compactions"] == 0
    assert runtime.state["last_compaction"] is None
    assert (
        runtime.state["task_reconciliation_pending"],
        runtime.state["task_reconciliation_reason"],
    ) == before_reconciliation


def test_game_instance_change_cancels_inflight_context_compression(tmp_path, monkeypatch):
    runtime = make_debug_state(tmp_path).agent_runtime
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "1000")
    original = [{"role": "user", "content": f"item {index}"} for index in range(8)]
    session = MemorySession(original)
    request_started = asyncio.Event()

    class Responses:
        async def create(self, **_kwargs):
            request_started.set()
            await asyncio.Event().wait()

    client = type("Client", (), {"responses": Responses()})()
    runtime.state.update(
        game_instance_id="game-a", status="running", phase="deciding",
        turn=1, last_prompt_tokens=1000,
    )

    async def scenario():
        compacting = asyncio.create_task(runtime._maybe_compact_sdk(session, client, "next"))
        await asyncio.wait_for(request_started.wait(), 1)
        runtime.observe_game_instance("game-b")
        with pytest.raises(asyncio.CancelledError):
            await compacting

    asyncio.run(scenario())

    assert runtime.cancel_event.is_set()
    assert runtime.state["pending_game_instance_id"] == "game-b"
    assert runtime.active_compression_task is None
    assert session.items == original
    assert runtime.state["last_prompt_tokens"] == 1000
    assert runtime.state["compactions"] == 0


def test_context_compression_retains_two_complete_cycles_within_budget(tmp_path, monkeypatch):
    runtime = make_debug_state(tmp_path).agent_runtime
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "1000")
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_RETAIN_TOKENS", "24000")
    first = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "old answer"}]
    second = [
        {"role": "user", "content": "second"},
        {"type": "reasoning", "summary": [{"text": "reason"}]},
        {"type": "function_call", "name": "get_navigation", "arguments": "{}"},
        {"type": "function_call_output", "output": '{"ok":true}'},
        {"role": "assistant", "content": "second answer"},
    ]
    latest = [{"role": "user", "content": "latest"}, {"role": "assistant", "content": "latest answer"}]
    session = MemorySession(first + second + latest)

    class Responses:
        async def create(self, **_kwargs):
            return type("Response", (), {"status": "completed", "output_text": "summary"})()

    client = type("Client", (), {"responses": Responses()})()
    runtime.state["last_prompt_tokens"] = 1000
    asyncio.run(runtime._maybe_compact_sdk(session, client, "next prompt"))

    assert session.items[-len(second + latest):] == second + latest
    compact = runtime.snapshot()["last_compaction"]
    assert compact["trigger_prompt_tokens"] == 1000
    assert compact["compact_at_tokens"] == 1000
    assert compact["retain_tokens"] == 24000
    assert compact["retained_cycles"] == 2
    assert compact["retained_items"] == len(second + latest)
    assert compact["retained_budget_exceeded"] is False
    assert sum(compact["before_composition"].values()) == compact["before_estimated_tokens"]
    assert set(compact["before_composition"]) == {
        "user", "assistant", "reasoning", "function_call", "function_output",
        "fixed_prompt", "next_prompt",
    }
    completed = [
        event for event in runtime.events()
        if event["type"] == "compression" and event["phase"] == "completed"
    ][-1]
    assert completed["details"] == compact


def test_context_compression_summarizes_second_cycle_when_retention_exceeds_budget(tmp_path, monkeypatch):
    runtime = make_debug_state(tmp_path).agent_runtime
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "1000")
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_RETAIN_TOKENS", "250")
    second = [
        {"role": "user", "content": "second"},
        {"type": "function_call", "name": "get_navigation", "arguments": "{}"},
        {"type": "function_call_output", "output": "x" * 600},
        {"role": "assistant", "content": "second answer"},
    ]
    latest = [
        {"role": "user", "content": "latest"},
        {"type": "function_call", "name": "submit_decision", "arguments": "{}"},
        {"type": "function_call_output", "output": '{"ok":true}'},
        {"role": "assistant", "content": "latest answer"},
    ]
    original = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "old"}] + second + latest
    session = MemorySession(original)

    class Responses:
        async def create(self, **_kwargs):
            return type("Response", (), {"status": "completed", "output_text": "summary"})()

    client = type("Client", (), {"responses": Responses()})()
    runtime.state["last_prompt_tokens"] = 1000
    asyncio.run(runtime._maybe_compact_sdk(session, client, "next"))

    assert session.items[-len(latest):] == latest
    assert not any(item == second[1] for item in session.items)
    compact = runtime.snapshot()["last_compaction"]
    assert compact["retained_cycles"] == 1
    assert compact["retained_items"] == len(latest)
    assert compact["retained_budget_exceeded"] is False


def test_context_compression_keeps_latest_cycle_even_when_it_alone_exceeds_budget(tmp_path, monkeypatch):
    runtime = make_debug_state(tmp_path).agent_runtime
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "1000")
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_RETAIN_TOKENS", "50")
    latest = [
        {"role": "user", "content": "latest"},
        {"type": "function_call", "name": "get_navigation", "arguments": "{}"},
        {"type": "function_call_output", "output": "z" * 900},
        {"role": "assistant", "content": "latest answer"},
    ]
    session = MemorySession([
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "second"},
        *latest,
    ])

    class Responses:
        async def create(self, **_kwargs):
            return type("Response", (), {"status": "completed", "output_text": "summary"})()

    client = type("Client", (), {"responses": Responses()})()
    runtime.state["last_prompt_tokens"] = 1000
    asyncio.run(runtime._maybe_compact_sdk(session, client, "next"))

    assert session.items[-len(latest):] == latest
    compact = runtime.snapshot()["last_compaction"]
    assert compact["retained_cycles"] == 1
    assert compact["retained_budget_exceeded"] is True
    assert compact["retained_estimated_tokens"] > 50


def test_context_compression_replacement_failure_restores_transcript_and_state(tmp_path, monkeypatch):
    runtime = make_debug_state(tmp_path).agent_runtime
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "1000")
    original = [{"role": "user", "content": str(index)} for index in range(8)]

    class FailingSession(MemorySession):
        def __init__(self, items):
            super().__init__(items)
            self.add_calls = 0

        async def add_items(self, items):
            self.add_calls += 1
            if self.add_calls == 1:
                raise RuntimeError("replacement failed")
            self.items.extend(items)

    class Responses:
        async def create(self, **_kwargs):
            return type("Response", (), {"status": "completed", "output_text": "summary"})()

    session = FailingSession(original)
    client = type("Client", (), {"responses": Responses()})()
    runtime.state["last_prompt_tokens"] = 1000
    with pytest.raises(RuntimeError, match="replacement failed"):
        asyncio.run(runtime._maybe_compact_sdk(session, client, "next"))
    assert session.items == original
    assert runtime.snapshot()["last_prompt_tokens"] == 1000
    assert runtime.snapshot()["last_compaction"] is None
    assert runtime.snapshot()["task_reconciliation_pending"] is False


def test_failed_context_summary_records_usage_without_replacing_transcript(tmp_path, monkeypatch):
    runtime = make_debug_state(tmp_path).agent_runtime
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "1000")
    original = [{"role": "user", "content": str(index)} for index in range(8)]
    session = MemorySession(original)

    class Responses:
        async def create(self, **_kwargs):
            return type("Response", (), {
                "status": "failed", "output_text": None,
                "usage": provider_usage(25, 8, 4, 1),
            })()

    runtime.state["last_prompt_tokens"] = 1000
    client = type("Client", (), {"responses": Responses()})()
    with pytest.raises(RuntimeError, match="response status was failed"):
        asyncio.run(runtime._maybe_compact_sdk(session, client, "next"))
    assert session.items == original
    assert runtime.snapshot()["last_prompt_tokens"] == 1000
    compact_usage = runtime.snapshot()["provider_usage"]["scopes"]["compaction"]
    assert compact_usage["requests"] == 1
    assert compact_usage["input_tokens"] == 25


def test_context_compression_waits_for_next_turn_exact_prompt_threshold(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "1000")

    class Session:
        def __init__(self):
            self.items = [
                {"role": "user", "content": f"cycle {index}"}
                for index in range(8)
            ]

        async def get_items(self): return list(self.items)
        async def clear_session(self): self.items = []
        async def add_items(self, items): self.items.extend(items)

    class Responses:
        def __init__(self): self.calls = 0
        async def create(self, **_kwargs):
            self.calls += 1
            return type("Response", (), {
                "status": "completed", "output_text": "summary",
                "usage": provider_usage(20, 4, 5, 2),
            })()

    responses = Responses()
    client = type("Client", (), {"responses": responses})()
    session = Session()

    runtime.state["last_prompt_tokens"] = 999
    asyncio.run(runtime._maybe_compact_sdk(session, client, "next"))
    assert responses.calls == 0

    runtime.state["last_prompt_tokens"] = 1000
    asyncio.run(runtime._maybe_compact_sdk(session, client, "next"))
    assert responses.calls == 1
    assert runtime.snapshot()["last_prompt_tokens"] is None
    compact_usage = runtime.snapshot()["provider_usage"]["scopes"]["compaction"]
    assert compact_usage["requests"] == 1
    assert compact_usage["cached_tokens"] == 4


def test_context_compaction_threshold_defaults_and_invalid_values(tmp_path, monkeypatch):
    runtime = make_debug_state(tmp_path).agent_runtime
    assert runtime.snapshot()["last_compaction"] is None
    assert runtime.compact_snapshot()["last_compaction"] is None
    monkeypatch.delenv("PALPAL_AGENT_COMPACT_AT_TOKENS", raising=False)
    assert runtime._compact_at_tokens() == DEFAULT_COMPACT_AT_TOKENS == 200_000
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "invalid")
    assert runtime._compact_at_tokens() == DEFAULT_COMPACT_AT_TOKENS
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "0")
    assert runtime._compact_at_tokens() == DEFAULT_COMPACT_AT_TOKENS
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "64000")
    assert runtime._compact_at_tokens() == 64_000
    monkeypatch.delenv("PALPAL_AGENT_COMPACT_RETAIN_TOKENS", raising=False)
    assert runtime._compact_retain_tokens() == DEFAULT_COMPACT_RETAIN_TOKENS == 24_000
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_RETAIN_TOKENS", "invalid")
    assert runtime._compact_retain_tokens() == DEFAULT_COMPACT_RETAIN_TOKENS
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_RETAIN_TOKENS", "0")
    assert runtime._compact_retain_tokens() == DEFAULT_COMPACT_RETAIN_TOKENS
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_RETAIN_TOKENS", "12000")
    assert runtime._compact_retain_tokens() == 12_000


def test_legacy_control_upgrade_schedules_one_task_reconciliation(tmp_path):
    debug = make_debug_state(tmp_path)
    control = debug.agent_runtime.snapshot()
    control.pop("task_reconciliation_pending", None)
    control.pop("task_reconciliation_reason", None)
    debug.agent_runtime._atomic_json(debug.agent_runtime.control_path, control)

    reloaded = make_debug_state(tmp_path).agent_runtime
    assert reloaded.task_reconciliation_pending() is True
    assert reloaded.task_reconciliation_reason() == "legacy_control_upgrade"
    reloaded.clear_task_reconciliation()
    assert reloaded.task_reconciliation_pending() is False


def test_context_estimate_counts_utf8_bytes_for_chinese():
    assert AgentRuntime._estimate_text_tokens("中" * 100) == 100
    assert AgentRuntime._estimate_text_tokens("x" * 300) == 100


def test_sdk_responses_adapter_uses_streaming_tools_and_sqlite(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    captured = {}

    class FunctionTool:
        def __init__(self, **kwargs):
            self.name = kwargs["name"]
            self.kwargs = kwargs

    class Model:
        def __init__(self, **kwargs):
            captured["model"] = kwargs

    class Agent:
        def __init__(self, **kwargs):
            captured["agent"] = kwargs

    class SQLiteSession:
        def __init__(self, session_id, db_path):
            captured["sqlite"] = (session_id, db_path)
            self.items = []

        async def get_items(self):
            return list(self.items)

        async def clear_session(self):
            self.items = []

        async def add_items(self, items):
            self.items.extend(items)

    class Stream:
        final_output = json.dumps({"ok": True, "accepted": True})
        context_wrapper = type("Context", (), {
            "usage": type("Usage", (), {
                "requests": 2,
                "input_tokens": 18,
                "output_tokens": 4,
                "total_tokens": 22,
                "request_usage_entries": [
                    type("RequestUsage", (), {
                        "input_tokens": 7, "output_tokens": 1, "total_tokens": 8,
                        "input_tokens_details": types.SimpleNamespace(cached_tokens=2),
                        "output_tokens_details": types.SimpleNamespace(reasoning_tokens=1),
                    })(),
                    type("RequestUsage", (), {
                        "input_tokens": 11, "output_tokens": 3, "total_tokens": 14,
                        "input_tokens_details": types.SimpleNamespace(cached_tokens=5),
                        "output_tokens_details": types.SimpleNamespace(reasoning_tokens=2),
                    })(),
                ],
            })()
        })()

        async def stream_events(self):
            submit = next(tool for tool in captured["agent"]["tools"] if tool.name == "submit_decision")
            ack = json.loads(await submit.kwargs["on_invoke_tool"](None, json.dumps(submission())))
            assert ack == {"ok": True, "accepted": True}
            if False:
                yield None

    class Runner:
        @classmethod
        def run_streamed(cls, agent, prompt, **kwargs):
            captured["run"] = kwargs
            return Stream()

    fake_agents = types.ModuleType("agents")
    fake_agents.Agent = Agent
    fake_agents.AsyncOpenAI = lambda **kwargs: type("Client", (), {})()
    fake_agents.FunctionTool = FunctionTool
    fake_agents.ModelSettings = lambda **kwargs: kwargs
    fake_agents.OpenAIResponsesModel = Model
    fake_agents.Runner = Runner
    fake_agents.SQLiteSession = SQLiteSession
    fake_agents.set_tracing_disabled = lambda **kwargs: captured.setdefault("tracing", kwargs)
    fake_agent_module = types.ModuleType("agents.agent")
    fake_agent_module.ToolsToFinalOutputResult = lambda **kwargs: types.SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    monkeypatch.setitem(sys.modules, "agents.agent", fake_agent_module)
    monkeypatch.setattr(runtime, "_maybe_compact_sdk", lambda *args: asyncio.sleep(0))

    result = asyncio.run(runtime._sdk_decide(make_state(), "feedback"))
    expected_tools = [spec["function"]["name"] for spec in debug.agent_tool_specs()]
    assert set(captured["model"]) == {"model", "openai_client"}
    assert [tool.name for tool in captured["agent"]["tools"]] == expected_tools
    assert captured["agent"]["model_settings"] == {
        "include_usage": True,
        "reasoning": {"effort": "high"},
        "tool_choice": "auto",
    }
    assert captured["agent"]["reset_tool_choice"] is False
    behavior = captured["agent"]["tool_use_behavior"]
    query = types.SimpleNamespace(tool=types.SimpleNamespace(name="get_tasks"), output='{"ok":true}')
    rejected = types.SimpleNamespace(
        tool=types.SimpleNamespace(name="submit_decision"),
        output='{"ok":false,"error":{"code":"task_review_required"}}',
    )
    accepted = types.SimpleNamespace(
        tool=types.SimpleNamespace(name="submit_decision"),
        output='{"ok":true,"accepted":true}',
    )
    assert behavior(None, [query]).is_final_output is False
    assert behavior(None, [rejected]).is_final_output is False
    finished = behavior(None, [accepted])
    assert finished.is_final_output is True
    assert json.loads(finished.final_output) == {"ok": True, "accepted": True}
    assert "permanent goal" not in captured["agent"]["instructions"].lower()
    assert "finish the game" not in captured["agent"]["instructions"].lower()
    assert captured["sqlite"] == (runtime.snapshot()["session_id"], runtime.sqlite_path)
    assert captured["run"]["session"].__class__ is SQLiteSession
    assert captured["run"]["max_turns"] == 16
    assert "run_config" not in captured["run"]
    assert captured["tracing"] == {"disabled": True}
    assert result["usage"]["total_tokens"] == 22
    assert runtime.snapshot()["last_prompt_tokens"] == 11
    tracked = runtime.snapshot()["provider_usage"]["scopes"]["main_decision"]
    assert tracked["requests"] == 2
    assert tracked["cached_tokens"] == 7
    assert tracked["reasoning_tokens"] == 3


def test_sdk_plain_text_end_retries_once_in_same_session_and_requires_submit(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    captured = {"runs": []}

    class FunctionTool:
        def __init__(self, **kwargs):
            self.name = kwargs["name"]
            self.invoke = kwargs["on_invoke_tool"]

    class Agent:
        def __init__(self, **kwargs): self.tools = kwargs["tools"]

    class Session:
        async def get_items(self): return []
        async def clear_session(self): pass
        async def add_items(self, _items): pass

    class Stream:
        final_output = "I will submit later."

        def __init__(self, agent, attempt):
            self.agent = agent
            self.attempt = attempt
            self.context_wrapper = types.SimpleNamespace(
                usage=provider_usage(10 + attempt, attempt, 2, 1),
            )

        async def stream_events(self):
            if self.attempt == 2:
                submit = next(tool for tool in self.agent.tools if tool.name == "submit_decision")
                await submit.invoke(None, json.dumps(submission()))
            if False: yield None

    class Runner:
        @classmethod
        def run_streamed(cls, agent, prompt, **kwargs):
            captured["runs"].append({"prompt": prompt, **kwargs})
            return Stream(agent, len(captured["runs"]))

    fake_agents = types.ModuleType("agents")
    fake_agents.Agent = Agent
    fake_agents.AsyncOpenAI = lambda **kwargs: object()
    fake_agents.FunctionTool = FunctionTool
    fake_agents.ModelSettings = lambda **kwargs: kwargs
    fake_agents.OpenAIResponsesModel = lambda **kwargs: object()
    fake_agents.Runner = Runner
    fake_agents.SQLiteSession = lambda *_args: Session()
    fake_agents.set_tracing_disabled = lambda **kwargs: None
    fake_agent_module = types.ModuleType("agents.agent")
    fake_agent_module.ToolsToFinalOutputResult = lambda **kwargs: types.SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    monkeypatch.setitem(sys.modules, "agents.agent", fake_agent_module)
    monkeypatch.setattr(runtime, "_maybe_compact_sdk", lambda *args: asyncio.sleep(0))

    result = asyncio.run(runtime._sdk_decide(make_state(), "feedback"))

    assert result["recommended_intent"] == "advance_dialog"
    assert len(captured["runs"]) == 2
    assert captured["runs"][0]["session"] is captured["runs"][1]["session"]
    assert all("run_config" not in run for run in captured["runs"])
    assert "without an accepted submit_decision" in captured["runs"][1]["prompt"]
    tracked = runtime.snapshot()["provider_usage"]["scopes"]["main_decision"]
    assert tracked["requests"] == 2
    assert tracked["input_tokens"] == 23
    assert runtime.snapshot()["last_prompt_tokens"] == 12
    retry = next(event for event in runtime.events() if event["phase"] == "protocol_retry")
    assert retry["details"] == {"api": "responses", "reasoning_effort": "high"}


def test_sdk_two_non_submit_ends_raise_protocol_error_without_parsing_text(tmp_path, monkeypatch):
    runtime = make_debug_state(tmp_path).agent_runtime
    captured = {"prompts": []}

    class Session:
        async def get_items(self): return []
        async def clear_session(self): pass
        async def add_items(self, _items): pass

    class Stream:
        context_wrapper = None
        final_output = json.dumps(submission())
        async def stream_events(self):
            if False: yield None

    class Runner:
        @classmethod
        def run_streamed(cls, _agent, prompt, **_kwargs):
            captured["prompts"].append(prompt)
            return Stream()

    fake_agents = types.ModuleType("agents")
    fake_agents.Agent = lambda **kwargs: object()
    fake_agents.AsyncOpenAI = lambda **kwargs: object()
    fake_agents.FunctionTool = lambda **kwargs: object()
    fake_agents.ModelSettings = lambda **kwargs: kwargs
    fake_agents.OpenAIResponsesModel = lambda **kwargs: object()
    fake_agents.Runner = Runner
    fake_agents.SQLiteSession = lambda *_args: Session()
    fake_agents.set_tracing_disabled = lambda **kwargs: None
    fake_agent_module = types.ModuleType("agents.agent")
    fake_agent_module.ToolsToFinalOutputResult = lambda **kwargs: types.SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    monkeypatch.setitem(sys.modules, "agents.agent", fake_agent_module)
    monkeypatch.setattr(runtime, "_maybe_compact_sdk", lambda *args: asyncio.sleep(0))

    with pytest.raises(RuntimeError, match="submit_decision_protocol_error"):
        asyncio.run(runtime._sdk_decide(make_state(), "feedback"))

    assert len(captured["prompts"]) == 2


def test_responses_adapter_accepts_legacy_chat_items_from_sqlite_session(tmp_path):
    from agents import ModelSettings, OpenAIResponsesModel, SQLiteSession

    async def build_request():
        session = SQLiteSession("legacy-chat-session", tmp_path / "legacy.sqlite3")
        await session.add_items([
            {"role": "user", "content": "Legacy question"},
            {"role": "assistant", "content": "Legacy answer"},
        ])
        legacy_items = await session.get_items()
        model = OpenAIResponsesModel(model="deepseek-test", openai_client=object())
        return model._build_response_create_kwargs(
            system_instructions="Continue the existing session.",
            input=[*legacy_items, {"role": "user", "content": "New turn"}],
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            stream=True,
        )

    request = asyncio.run(build_request())

    assert request["input"] == [
        {"role": "user", "content": "Legacy question"},
        {"role": "assistant", "content": "Legacy answer"},
        {"role": "user", "content": "New turn"},
    ]
    assert request["previous_response_id"].__class__.__name__ == "Omit"
    assert request["conversation"].__class__.__name__ == "Omit"


def test_legacy_sqlite_history_migration_is_atomic_idempotent_and_snapshot_preserving(tmp_path):
    import sqlite3
    from agents import SQLiteSession

    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    session_id = runtime.snapshot()["session_id"]
    raw_battle = {
        "generation": 4,
        "phase": "command",
        "players": [{"index": 0, "name": "hero", "hp": 10, "max_hp": 20}],
        "enemies": [{"slot_index": 0, "object_id": 81, "name": "enemy", "hp": 999}],
        "recent_outcomes": [{"sequence": 1, "enemy_changes": [{"amount": 7}]}],
        "experience": {"signature": "81", "snapshot": "old"},
    }

    async def exercise():
        session = SQLiteSession(session_id, runtime.sqlite_path)
        await session.add_items([{
            "role": "user",
            "content": json.dumps({"battle": raw_battle}, ensure_ascii=False),
        }])
        assert await runtime._migrate_sdk_session_history(session) is True
        first = await session.get_items()
        first_bytes = json.dumps(first, ensure_ascii=False, separators=(",", ":")).encode()
        assert await runtime._migrate_sdk_session_history(session) is False
        second = await session.get_items()
        return first, first_bytes, second

    first, first_bytes, second = asyncio.run(exercise())
    migrated_battle = json.loads(first[0]["content"])["battle"]
    assert "hp" not in migrated_battle["enemies"][0]
    assert migrated_battle["recent_outcomes"] == raw_battle["recent_outcomes"]
    assert migrated_battle["experience"] == raw_battle["experience"]
    assert json.dumps(second, ensure_ascii=False, separators=(",", ":")).encode() == first_bytes
    events = [event for event in runtime.events() if event["phase"] == "history_migrated"]
    assert len(events) == 1
    assert events[0]["details"] == {}
    with sqlite3.connect(runtime.sqlite_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM palpal_session_migrations WHERE session_id = ?", (session_id,),
        ).fetchone()[0] == 1


def test_battle_child_history_migration_is_grouped_with_battle_events(tmp_path):
    from agents import SQLiteSession

    runtime = make_debug_state(tmp_path).agent_runtime
    command = {
        **make_state(),
        "game_instance_id": "battle-game",
        "in_battle": True,
        "battle": {
            "generation": 5,
            "command_generation": 9,
            "phase": "command",
            "enemies": [],
        },
    }
    control = runtime.battle_delegate._begin(command)
    child_id = control["child_session_id"]
    session = SQLiteSession(child_id, runtime.sqlite_path)

    assert asyncio.run(runtime._migrate_sdk_session_history(session)) is True

    event = next(event for event in runtime.events() if event["phase"] == "history_migrated")
    metadata = event["details"]["battle_delegate"]
    assert metadata == {
        "child_session_id": child_id,
        "battle_generation": 5,
        "group_id": runtime.battle_delegate._group_id(control),
        "depth": 1,
        "status": "running",
    }


def test_legacy_sqlite_history_migration_failure_rolls_back_everything(tmp_path, monkeypatch):
    import sqlite3
    from agents import SQLiteSession

    runtime = make_debug_state(tmp_path).agent_runtime
    session_id = runtime.snapshot()["session_id"]

    async def seed():
        session = SQLiteSession(session_id, runtime.sqlite_path)
        await session.add_items([{"role": "user", "content": '{"battle":{"secret":1}}'}])
        return session

    session = asyncio.run(seed())
    with sqlite3.connect(runtime.sqlite_path) as connection:
        before = connection.execute(
            "SELECT id, message_data FROM agent_messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    monkeypatch.setattr(runtime, "_migrate_model_transcript_item", lambda _item: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(runtime._migrate_sdk_session_history(session))

    with sqlite3.connect(runtime.sqlite_path) as connection:
        after = connection.execute(
            "SELECT id, message_data FROM agent_messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'palpal_session_migrations'"
        ).fetchone()
        marker = (
            connection.execute(
                "SELECT count(*) FROM palpal_session_migrations WHERE session_id = ?", (session_id,),
            ).fetchone()[0]
            if table_exists else 0
        )
    assert after == before
    assert marker == 0


def test_restored_transcript_invalidates_history_migration_marker(tmp_path):
    from agents import SQLiteSession

    runtime = make_debug_state(tmp_path).agent_runtime
    session_id = runtime.snapshot()["session_id"]

    async def exercise():
        session = SQLiteSession(session_id, runtime.sqlite_path)
        assert await runtime._migrate_sdk_session_history(session) is True
        await runtime._replace_sdk_session_items(session_id, [
            {"role": "user", "content": '{"battle":{"enemies":[{"hp":999}]}}'},
        ])
        assert await runtime._migrate_sdk_session_history(session) is True
        return await session.get_items()

    items = asyncio.run(exercise())
    assert "hp" not in items[0]["content"]


def test_last_prompt_tokens_uses_single_request_usage_only_as_exact_fallback():
    single = type("Usage", (), {
        "requests": 1,
        "input_tokens": 41,
        "request_usage_entries": [],
    })()
    aggregate_only = type("Usage", (), {
        "requests": 2,
        "input_tokens": 82,
        "request_usage_entries": [],
    })()

    assert AgentRuntime._last_prompt_tokens_from_usage(single) == 41
    assert AgentRuntime._last_prompt_tokens_from_usage(aggregate_only) is None


def test_sdk_rejected_submit_returns_to_model_and_recovers_in_same_tool_cycle(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    captured = {"calls": [], "cycles": []}

    class FunctionTool:
        def __init__(self, **kwargs):
            self.name = kwargs["name"]
            self.kwargs = kwargs

    class Agent:
        def __init__(self, **kwargs):
            self.tools = kwargs["tools"]
            self.tool_use_behavior = kwargs["tool_use_behavior"]

    class Session:
        def __init__(self, *_args):
            self.items = []

        async def get_items(self): return list(self.items)
        async def clear_session(self): self.items = []
        async def add_items(self, items): self.items.extend(items)

    accepted_decision = decision("navigation_step", {"id": 63}, "inspect_target")

    def run_agent_tool(name, arguments, state, *, tool_cycle=None):
        captured["calls"].append(name)
        captured["cycles"].append(tool_cycle)
        if name == "submit_decision" and captured["calls"].count("submit_decision") == 1:
            return {
                "ok": False,
                "error": {"code": "task_review_required", "message": "Review tasks first."},
            }
        if name == "submit_decision":
            return {"ok": True, "decision": accepted_decision}
        return {"ok": True}

    monkeypatch.setattr(debug, "run_agent_tool", run_agent_tool)

    class Stream:
        context_wrapper = None
        final_output = None

        def __init__(self, agent):
            self.agent = agent

        async def stream_events(self):
            by_name = {tool.name: tool for tool in self.agent.tools}
            submit = by_name["submit_decision"]
            rejected_output = await submit.kwargs["on_invoke_tool"](None, json.dumps(submission("navigate", target_id=63)))
            assert json.loads(rejected_output)["error"]["code"] == "task_review_required"
            rejected = types.SimpleNamespace(tool=submit, output=rejected_output)
            assert self.agent.tool_use_behavior(None, [rejected]).is_final_output is False

            outputs = []
            for name, arguments in (
                ("get_tasks", {}),
                ("upsert_task", {"id": "review-me"}),
                ("submit_decision", submission("navigate", target_id=63)),
            ):
                tool = by_name[name]
                output = await tool.kwargs["on_invoke_tool"](None, json.dumps(arguments))
                outputs.append(types.SimpleNamespace(tool=tool, output=output))
            finished = self.agent.tool_use_behavior(None, outputs)
            assert finished.is_final_output is True
            self.final_output = finished.final_output
            if False:
                yield None

    class Runner:
        @classmethod
        def run_streamed(cls, agent, _prompt, **_kwargs):
            return Stream(agent)

    fake_agents = types.ModuleType("agents")
    fake_agents.Agent = Agent
    fake_agents.AsyncOpenAI = lambda **kwargs: object()
    fake_agents.FunctionTool = FunctionTool
    fake_agents.ModelSettings = lambda **kwargs: kwargs
    fake_agents.OpenAIResponsesModel = lambda **kwargs: object()
    fake_agents.Runner = Runner
    fake_agents.SQLiteSession = Session
    fake_agents.set_tracing_disabled = lambda **kwargs: None
    fake_agent_module = types.ModuleType("agents.agent")
    fake_agent_module.ToolsToFinalOutputResult = lambda **kwargs: types.SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    monkeypatch.setitem(sys.modules, "agents.agent", fake_agent_module)
    monkeypatch.setattr(runtime, "_maybe_compact_sdk", lambda *args: asyncio.sleep(0))

    result = asyncio.run(runtime._sdk_decide(make_state(), "feedback"))

    assert captured["calls"] == ["submit_decision", "get_tasks", "upsert_task", "submit_decision"]
    assert len({id(cycle) for cycle in captured["cycles"]}) == 1
    assert result["recommended_action"] == accepted_decision["recommended_action"]
    decision_events = [event for event in runtime.events() if event["type"] == "decision"]
    assert [(event["phase"], event["summary"]) for event in decision_events] == [
        ("rejected", "Recommendation rejected: task_review_required."),
        ("submitted", "Recommendation submitted."),
    ]
    assert decision_events[0]["details"]["result"]["error"]["code"] == "task_review_required"


def test_sdk_parallel_task_query_and_create_cannot_bypass_review_result(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime

    class FunctionTool:
        def __init__(self, **kwargs):
            self.name = kwargs["name"]
            self.invoke = kwargs["on_invoke_tool"]

    class Agent:
        def __init__(self, **kwargs):
            self.tools = kwargs["tools"]
            self.tool_use_behavior = kwargs["tool_use_behavior"]

    class Session:
        async def get_items(self): return []
        async def clear_session(self): pass
        async def add_items(self, items): pass

    class Stream:
        context_wrapper = None
        final_output = None

        def __init__(self, agent): self.agent = agent

        async def stream_events(self):
            tools = {tool.name: tool for tool in self.agent.tools}
            audit_output = await tools["get_tasks"].invoke(None, json.dumps({
                "include_closed": True, "detail": "summary",
            }))
            premature = await tools["upsert_task"].invoke(None, json.dumps({
                "id": "new-sdk-task", "description": "Investigate clue",
                "rationale": "It affects future action.",
            }))
            assert json.loads(premature)["error"]["code"] == "task_creation_review_required"
            token = json.loads(audit_output)["review_token"]
            created = await tools["upsert_task"].invoke(None, json.dumps({
                "id": "new-sdk-task", "description": "Investigate clue",
                "rationale": "It affects future action.", "review_token": token,
            }))
            assert json.loads(created)["operation"] == "created"
            submitted = await tools["submit_decision"].invoke(None, json.dumps({
                "action": "advance_dialog", "reason": "Continue.",
            }))
            self.final_output = submitted
            if False:
                yield None

    class Runner:
        @classmethod
        def run_streamed(cls, agent, _prompt, **_kwargs): return Stream(agent)

    fake_agents = types.ModuleType("agents")
    fake_agents.Agent = Agent
    fake_agents.AsyncOpenAI = lambda **kwargs: object()
    fake_agents.FunctionTool = FunctionTool
    fake_agents.ModelSettings = lambda **kwargs: kwargs
    fake_agents.OpenAIResponsesModel = lambda **kwargs: object()
    fake_agents.Runner = Runner
    fake_agents.SQLiteSession = lambda *_args: Session()
    fake_agents.set_tracing_disabled = lambda **kwargs: None
    fake_agent_module = types.ModuleType("agents.agent")
    fake_agent_module.ToolsToFinalOutputResult = lambda **kwargs: types.SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    monkeypatch.setitem(sys.modules, "agents.agent", fake_agent_module)
    monkeypatch.setattr(runtime, "_maybe_compact_sdk", lambda *args: asyncio.sleep(0))

    result = asyncio.run(runtime._sdk_decide(make_state(), "feedback"))
    assert result["recommended_intent"] == "advance_dialog"
    assert [task["id"] for task in debug.task_ledger()] == ["new-sdk-task"]


def test_sdk_prompt_omits_redundant_goal_tools_and_constraints(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    captured = {}

    class FunctionTool:
        def __init__(self, **kwargs):
            self.name = kwargs["name"]
            self.invoke = kwargs["on_invoke_tool"]

    class Agent:
        def __init__(self, **kwargs):
            self.tools = kwargs["tools"]

    class Session:
        async def get_items(self): return []
        async def clear_session(self): pass
        async def add_items(self, items): pass

    class Stream:
        context_wrapper = None
        def __init__(self, agent): self.agent = agent
        async def stream_events(self):
            submit = next(tool for tool in self.agent.tools if tool.name == "submit_decision")
            await submit.invoke(None, json.dumps(submission()))
            if False: yield None

    class Runner:
        @classmethod
        def run_streamed(cls, agent, prompt, **kwargs):
            captured["prompt"] = json.loads(prompt)
            return Stream(agent)

    fake_agents = types.ModuleType("agents")
    fake_agents.Agent = Agent
    fake_agents.AsyncOpenAI = lambda **kwargs: object()
    fake_agents.FunctionTool = FunctionTool
    fake_agents.ModelSettings = lambda **kwargs: kwargs
    fake_agents.OpenAIResponsesModel = lambda **kwargs: object()
    fake_agents.Runner = Runner
    fake_agents.SQLiteSession = lambda *args: Session()
    fake_agents.set_tracing_disabled = lambda **kwargs: None
    fake_agent_module = types.ModuleType("agents.agent")
    fake_agent_module.ToolsToFinalOutputResult = lambda **kwargs: types.SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    monkeypatch.setitem(sys.modules, "agents.agent", fake_agent_module)
    monkeypatch.setattr(runtime, "_maybe_compact_sdk", lambda *args: asyncio.sleep(0))

    asyncio.run(runtime._sdk_decide(make_state(), "compact feedback"))
    assert set(captured["prompt"]) == {"feedback", "context"}
    assert set(captured["prompt"]["context"]) == {
        "state_summary", "room_memo", "active_tasks", "active_tasks_total", "task_review", "task_inbox",
        "task_reconciliation", "room_entry_context", "recent_transition_chain",
    }


def test_tool_argument_parser_repairs_one_truncated_json_ending():
    parsed = AgentRuntime._parse_tool_arguments('{"summary":"ok","alternatives":[]')
    assert parsed == {"summary": "ok", "alternatives": []}
    with pytest.raises((json.JSONDecodeError, ValueError)):
        AgentRuntime._parse_tool_arguments('{not json')


def test_state_delta_only_emits_changed_object_and_compact_dialog():
    before = make_state(objects=[{"id": 1, "state": 1, "interactable": True, "blocking": False}])
    after = json.loads(json.dumps(before))
    after["objects"][0]["state"] = 0
    before["dialog"] = {"active": True, "events": [{"text": "large old history"}], "latest_event": {"message_id": 1, "text": "old"}}
    after["dialog"] = {"active": False, "events": [{"text": "large old history"}], "latest_event": {"message_id": 1, "text": "old"}}
    delta = AgentRuntime.state_delta(before, after)
    assert delta["object_changes"] == [{"id": 1, "before": (1, None, True, False), "after": (0, None, True, False)}]
    assert "events" not in delta["dialog"]["before"]


def test_state_delta_reports_equipment_slot_change():
    before = make_state()
    before['party'] = [{'role_id': 0, 'equipment': [{'slot_id': 0, 'item_id': 196}]}]
    after = json.loads(json.dumps(before))
    after['party'][0]['equipment'][0]['item_id'] = 201

    delta = AgentRuntime.state_delta(before, after)

    assert delta['equipment_changes'] == [{
        'role_id': 0,
        'slot_id': 0,
        'before': 196,
        'after': 201,
    }]


def test_state_delta_reports_shop_cash_and_owned_item_change():
    before = make_state()
    before['cash'] = 600
    before['inventory'] = []
    before['shop'] = {
        'active': True,
        'mode': 'buy',
        'store_id': 7,
        'phase': 'select',
        'selected_item_id': 201,
        'items': [{'item_id': 201, 'owned_count': 0, 'affordable': True, 'selected': True}],
    }
    after = json.loads(json.dumps(before))
    after['cash'] = 200
    after['inventory'] = [{'item_id': 201, 'amount': 1, 'amount_in_use': 0}]
    after['shop']['items'][0]['owned_count'] = 1
    after['shop']['items'][0]['affordable'] = False

    delta = AgentRuntime.state_delta(before, after)

    assert delta['cash'] == {'before': 600, 'after': 200}
    assert delta['inventory_changes'] == [{'item_id': 201, 'before': 0, 'after': 1}]
    assert AgentRuntime._state_signature(before) != AgentRuntime._state_signature(after)


def test_live_dialog_invalidates_navigation_and_requests_replan(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    state = make_state()
    state["dialog"] = {"active": True, "latest_event": {"message_id": 1955, "text": "new dialog"}}
    recommendation = decision("navigation_step", {"id": 10}, "inspect_target")
    assert runtime._decision_replan_reason(recommendation, state) == "in_dialog"
    feedback = runtime._replan_feedback("in_dialog", state)
    assert "new dialog" in feedback
    assert "in_dialog" in feedback


def test_live_shop_allows_only_shop_actions_and_replans_after_exit(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    state = make_state()
    state['shop'] = {
        'active': True,
        'mode': 'buy',
        'store_id': 7,
        'phase': 'select',
        'selected_item_id': 201,
        'items': [{'item_id': 201, 'price': 400, 'affordable': True}],
    }

    assert runtime._decision_replan_reason(
        decision('navigation_step', {'id': 10}, 'inspect_target'),
        state,
    ) == 'in_shop'
    assert runtime._decision_replan_reason(
        decision('shop_buy', {'item_id': 201}, 'purchase_shop_item'),
        state,
    ) is None
    state['shop'] = None
    assert runtime._decision_replan_reason(
        decision('shop_leave', {}, 'leave_shop'),
        state,
    ) == 'shop_inactive'


def test_execute_shop_buy_uses_structured_writer(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    before = make_state()
    before['cash'] = 600
    before['shop'] = {
        'active': True,
        'mode': 'buy',
        'store_id': 7,
        'phase': 'select',
        'selected_item_id': 201,
        'items': [{'item_id': 201, 'price': 400, 'affordable': True}],
    }
    after = json.loads(json.dumps(before))
    after['cash'] = 200
    after['inventory'] = [{'item_id': 201, 'amount': 1, 'amount_in_use': 0}]
    calls = []
    debug.snapshot = lambda: before
    debug.write_shop_action = lambda action, item_id=None: calls.append((action, item_id)) or {
        'command': action,
        'item_id': item_id,
        'result': {'ok': True, 'action': action, 'item_id': item_id, 'status': 'purchased'},
    }
    monkeypatch.setattr(runtime, '_wait_action_and_stable', lambda **_kwargs: after)
    recommendation = debug.attach_decision_context(
        before,
        decision('shop_buy', {'item_id': 201}, 'purchase_shop_item'),
    )

    result = runtime._execute_decision(recommendation)

    assert calls == [('shop_buy', 201)]
    assert result['tool'] == 'shop_buy'
    assert result['delta']['cash'] == {'before': 600, 'after': 200}
    assert result['delta']['inventory_changes'] == [{'item_id': 201, 'before': 0, 'after': 1}]


def test_execute_choice_uses_structured_writer(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    before = make_state()
    before['choice'] = {
        'active': True,
        'generation': 5,
        'selected_index': 0,
        'options': [{'index': 0, 'text': '否'}, {'index': 1, 'text': '是'}],
    }
    after = json.loads(json.dumps(before))
    after['choice'] = None
    calls = []
    debug.snapshot = lambda: before
    debug.write_choice_action = lambda generation, option_index: calls.append((generation, option_index)) or {
        'command': 'choice_select',
        'generation': generation,
        'option_index': option_index,
        'result': {'ok': True, 'status': 'selected', 'selected_index': option_index},
    }
    monkeypatch.setattr(runtime, '_wait_action_and_stable', lambda **_kwargs: after)
    recommendation = debug.attach_decision_context(
        before,
        decision('choice_select', {'generation': 5, 'option_index': 1}, 'select_choice'),
    )

    result = runtime._execute_decision(recommendation)

    assert calls == [(5, 1)]
    assert result['tool'] == 'choice_select'
    assert result['delta']['choice']['before']['generation'] == 5
    assert result['delta']['choice']['after'] is None


def test_execute_use_item_reports_observed_object_effect_despite_raw_failure(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    target = {
        'id': 238, 'x': 32, 'y': 16, 'state': 2, 'trigger_mode': 0,
        'trigger_script': 9433, 'auto_script': 0, 'blocking': True,
        'interactable': True,
    }
    before = make_state(width=6, height=6, start=(4, 4), objects=[target])
    before['party_direction'] = 1
    before['inventory'] = [{
        'item_id': 279, 'name': '破天鎚', 'amount': 1, 'amount_in_use': 0,
        'usable': True, 'apply_to_all': True,
        'use_effect': {
            'requirements_complete': True,
            'requirements': [{
                'kind': 'face_event_object', 'script_entry': 39655,
                'event_object_id': 238, 'distance_level': 1,
                'distance_limit': 48, 'failure_script_entry': 38793,
            }],
        },
    }]
    after = json.loads(json.dumps(before))
    after['objects'][0]['state'] = 0
    debug.snapshot = lambda: before
    debug.write_use_item_action = lambda *_args, **_kwargs: {
        'command': 'use_item', 'item_id': 279, 'target': 'all',
    }
    debug.current_action_result = lambda: {
        'ok': True, 'action': 'use_item', 'item_id': 279,
        'role_id': 65535, 'script_success': False,
    }
    monkeypatch.setattr(runtime, '_wait_action_and_stable', lambda **_kwargs: after)
    recommendation = debug.attach_decision_context(
        before,
        decision('use_item', {'item_id': 279, 'target': 'all'}, 'use_inventory_item'),
    )

    result = runtime._execute_decision(recommendation)

    action = result['result']
    assert action['matched_event_object_id'] == 238
    assert action['effect_observed'] is True
    assert action['object_delta']['state'] == {'before': 2, 'after': 0}
    assert action['native_result']['script_success'] is False


def test_quiet_window_restarts_when_dialog_reappears(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    inactive = make_state()
    inactive["dialog"] = {"active": False, "latest_event": {"sequence": 1, "message_id": 1952}}
    active = make_state()
    active["dialog"] = {"active": True, "latest_event": {"sequence": 2, "message_id": 1955}}
    snapshots = [inactive, inactive, active]

    def snapshot():
        return snapshots.pop(0) if snapshots else active

    debug.snapshot = snapshot
    started = time.monotonic()
    observed = runtime._wait_state_quiet(timeout=1.0, quiet_window=0.2)
    assert observed["dialog"]["active"] is True
    assert time.monotonic() - started >= 0.3


def test_quiet_window_accepts_checkpoint_state_without_latest_dialog_event(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    restored = make_state()
    restored["dialog"] = {"active": False, "latest_event": None, "events": []}
    debug.snapshot = lambda: restored

    signature = runtime._state_signature(restored)
    assert signature[10] is None
    observed = runtime._wait_state_quiet(timeout=0.5, quiet_window=0.05)
    assert observed is restored


def test_state_signature_ignores_vanish_countdown_but_observes_phase_changes():
    positive = make_state(objects=[{
        "id": 233, "state": -1, "vanish_time": 752,
        "interactable": False, "blocking": False,
    }])
    positive["_debug"] = {"recent_events": [{
        "time": 1.0, "type": "object_changed", "object_id": 233,
        "changes": {"vanish_time": {"from": 753, "to": 752}},
    }]}
    positive_next = json.loads(json.dumps(positive))
    positive_next["objects"][0]["vanish_time"] = 751
    positive_next["_debug"]["recent_events"][-1] = {
        "time": 2.0, "type": "object_changed", "object_id": 233,
        "changes": {"vanish_time": {"from": 752, "to": 751}},
    }
    visible = json.loads(json.dumps(positive))
    visible["objects"][0]["vanish_time"] = 0
    visible["_debug"]["recent_events"][-1] = {
        "time": 3.0, "type": "object_changed", "object_id": 233,
        "changes": {"vanish_time": {"from": 1, "to": 0}},
    }
    negative = json.loads(json.dumps(positive))
    negative["objects"][0]["vanish_time"] = -15
    negative["_debug"]["recent_events"][-1] = {
        "time": 4.0, "type": "object_changed", "object_id": 233,
        "changes": {"vanish_time": {"from": 0, "to": -15}},
    }

    assert AgentRuntime._state_signature(positive) == AgentRuntime._state_signature(positive_next)
    assert AgentRuntime._state_signature(positive) != AgentRuntime._state_signature(visible)
    assert AgentRuntime._state_signature(positive) != AgentRuntime._state_signature(negative)


def test_state_signature_observes_nonbattle_party_effect_changes():
    before = make_state()
    before["party"] = [{"role_id": 1, "status_effects": [], "poisons": []}]
    poisoned = json.loads(json.dumps(before))
    poisoned["party"][0]["poisons"] = [{"poison_id": 563, "name": "赤毒"}]
    slowed = json.loads(json.dumps(before))
    slowed["party"][0]["status_effects"] = [{
        "status_id": 1, "key": "slow", "remaining_rounds": 2, "persistent": False,
    }]

    assert AgentRuntime._state_signature(before) != AgentRuntime._state_signature(poisoned)
    assert AgentRuntime._state_signature(before) != AgentRuntime._state_signature(slowed)


def test_state_signature_uses_latest_semantic_event_before_vanish_noise():
    state = make_state(objects=[{
        "id": 233, "state": -1, "vanish_time": 752,
        "interactable": False, "blocking": False,
    }])
    semantic_event = {
        "time": 1.0, "type": "interaction_no_effect", "object_id": 233,
    }
    state["_debug"] = {"recent_events": [semantic_event]}
    noisy = json.loads(json.dumps(state))
    noisy["objects"][0]["vanish_time"] = 751
    noisy["_debug"]["recent_events"].append({
        "time": 2.0, "type": "object_changed", "object_id": 233,
        "changes": {"vanish_time": {"from": 752, "to": 751}},
    })
    changed = json.loads(json.dumps(noisy))
    changed["_debug"]["recent_events"].append({
        "time": 3.0, "type": "object_changed", "object_id": 233,
        "changes": {"blocking": {"from": False, "to": True}},
    })

    assert AgentRuntime._state_signature(state) == AgentRuntime._state_signature(noisy)
    assert AgentRuntime._state_signature(noisy) != AgentRuntime._state_signature(changed)


def test_quiet_window_accepts_monotonic_vanish_countdown(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    countdown = {"value": 752}

    def snapshot():
        current = countdown["value"]
        state = make_state(objects=[{
            "id": 233, "state": -1, "vanish_time": current,
            "interactable": False, "blocking": False,
        }])
        state["_debug"] = {"recent_events": [{
            "time": float(753 - current), "type": "object_changed", "object_id": 233,
            "changes": {"vanish_time": {"from": current + 1, "to": current}},
        }]}
        countdown["value"] -= 1
        return state

    debug.snapshot = snapshot
    observed = runtime._wait_state_quiet(timeout=0.5, quiet_window=0.05)

    assert observed["objects"][0]["vanish_time"] < 752
    assert observed["objects"][0]["vanish_time"] > 0


def test_completed_action_wait_accepts_monotonic_vanish_countdown(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    countdown = {"value": 752}

    def snapshot():
        current = countdown["value"]
        state = make_state(objects=[{
            "id": 233, "state": -1, "vanish_time": current,
            "interactable": False, "blocking": False,
        }])
        state["_debug"] = {"recent_events": [{
            "time": float(753 - current), "type": "object_changed", "object_id": 233,
            "changes": {"vanish_time": {"from": current + 1, "to": current}},
        }]}
        countdown["value"] -= 1
        return state

    debug.snapshot = snapshot
    monkeypatch.setattr(runtime, "_wait_action_completion", lambda *_args, **_kwargs: snapshot())

    observed = runtime._wait_action_and_stable(
        timeout=0.5,
        quiet_window=0.05,
        action={"action_handshake_supported": True},
    )

    assert observed["objects"][0]["vanish_time"] > 0


def test_navigation_waits_for_scene_transition_instead_of_failing(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    entering = make_state()
    entering["entering_scene"] = True
    stable = make_state()
    stable["scene_id"] = 2
    snapshots = [entering, entering, stable]

    debug.snapshot = lambda: snapshots.pop(0) if snapshots else stable
    debug.navigation_step = lambda state, target_id, **_kwargs: {
        "ok": False,
        "status": "entering_scene",
        "target_id": target_id,
    }

    result = runtime._navigate(4)
    assert result["ok"]
    assert result["status"] == "transitioned"
    assert result["target_id"] == 4
    assert result["steps"] == 1


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("in_battle", "interrupted_by_battle"),
        ("in_shop", "interrupted_by_shop"),
        ("in_choice", "interrupted_by_choice"),
    ],
)
def test_navigation_reports_game_requested_interactions_explicitly(
    tmp_path, status, expected,
):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    state = make_state()
    debug.snapshot = lambda: state
    debug.navigation_step = lambda _state, target_id, **_kwargs: {
        "ok": False,
        "status": status,
        "target_id": target_id,
    }

    result = runtime._navigate(4)

    assert result["ok"] is True
    assert result["status"] == expected
    assert result["steps"] == 1


def test_obj189_consecutive_confirms_require_two_explicit_model_choices(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    state = make_state()
    state_holder = {"value": state}
    prompts_seen = []
    executed = []

    def choose(current, _model, _feedback):
        choice = current.get("choice") or {}
        if choice.get("active"):
            prompts_seen.append(choice["prompt"]["text"])
            option_index = 1 if choice["generation"] == 11 else 0
            return decision(
                "choice_select",
                {"generation": choice["generation"], "option_index": option_index},
                "select_choice",
            )
        return decision("navigation_step", {"id": 189}, "inspect_target")

    def execute(recommendation):
        action = recommendation["recommended_action"]
        executed.append((action["tool"], dict(action["args"])))
        if action["tool"] == "navigation_step":
            state_holder["value"] = {
                **state,
                "choice": {
                    "active": True,
                    "generation": 11,
                    "prompt": {"text": "要聽第一段故事嗎？"},
                    "options": [{"index": 0, "text": "否"}, {"index": 1, "text": "是"}],
                },
            }
            status = "interrupted_by_choice"
        elif action["args"]["generation"] == 11:
            state_holder["value"] = {
                **state,
                "choice": {
                    "active": True,
                    "generation": 12,
                    "prompt": {"text": "要聽第二段故事嗎？"},
                    "options": [{"index": 0, "text": "否"}, {"index": 1, "text": "是"}],
                },
            }
            status = "selected"
        else:
            state_holder["value"] = state
            status = "selected"
        return {"ok": True, "result": {"status": status}, "delta": {}}

    debug.snapshot = lambda: state_holder["value"]
    runtime._unsafe_state_reason = lambda _state: None
    runtime.decision_provider = choose
    runtime._execute_decision = execute

    runtime.start(None, 3)
    paused = wait_for_pause(runtime)

    assert paused["stop_reason"] == "turn_limit"
    assert prompts_seen == ["要聽第一段故事嗎？", "要聽第二段故事嗎？"]
    assert executed == [
        ("navigation_step", {"id": 189}),
        ("choice_select", {"generation": 11, "option_index": 1}),
        ("choice_select", {"generation": 12, "option_index": 0}),
    ]


def test_navigation_waits_for_pending_transition_observation(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    state = make_state()
    results = [
        {
            "ok": True,
            "status": "stepping",
            "target_id": 48,
            "move": "up",
            "segment_steps": 1,
            "remaining_steps": 0,
        },
        {
            "ok": True,
            "status": "transition_observing",
            "target_id": 48,
            "stable_snapshots": 0,
            "required_snapshots": 2,
        },
        {
            "ok": True,
            "status": "transitioned",
            "target_id": 48,
        },
    ]

    debug.snapshot = lambda: state
    debug.navigation_step = lambda current, target_id, **_kwargs: results.pop(0)
    monkeypatch.setattr("palpal.agent_runtime.time.sleep", lambda _seconds: None)

    result = runtime._navigate(48)
    assert result["ok"]
    assert result["status"] == "transitioned"
    assert [entry["status"] for entry in result["progress"]] == [
        "stepping",
        "transition_observing",
        "transitioned",
    ]
    assert runtime.snapshot()["stop_reason"] != "execution_failed"


def test_navigation_search_interaction_continues_when_native_transition_is_bound(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    state = make_state()
    observation_id = debug.observations.register(
        "transition", owner="navigation", timeout=4, payload={},
    )
    debug.navigation_transition_requests[75] = observation_id
    results = [
        {
            "ok": True,
            "status": "interacting",
            "target_id": 75,
            "observation_id": "interaction-observation",
            "action": {"buttons": ["search"], "frames": 1},
        },
        {
            "ok": True,
            "status": "transitioned",
            "target_id": 75,
            "observation_id": observation_id,
        },
    ]
    debug.snapshot = lambda: state
    debug.navigation_step = lambda _state, target_id, **_kwargs: results.pop(0)
    monkeypatch.setattr(runtime, "_wait_action_and_stable", lambda **_kwargs: state)
    monkeypatch.setattr(runtime, "_wait_navigation_observation", lambda *_args: None)

    result = runtime._navigate(75)

    assert result["status"] == "transitioned"
    assert [entry["status"] for entry in result["progress"]] == [
        "interacting", "transitioned",
    ]


def test_navigation_observation_polling_does_not_create_extra_steps(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    state = make_state()
    write_state(debug, state)
    observation_id = debug.observations.register(
        "transition",
        owner="navigation",
        timeout=4,
        payload={},
    )
    debug.navigation_transition_requests[48] = observation_id
    snapshots = {"count": 0}
    calls = {"count": 0}

    def snapshot():
        snapshots["count"] += 1
        if snapshots["count"] == 3:
            debug.observations.complete(observation_id, {"transition": {"object_id": 48}})
        return state

    def navigation_step(_state, target_id, **_kwargs):
        calls["count"] += 1
        request = debug.observations.get(observation_id)
        if request is not None and request["status"] == "pending":
            return {
                "ok": True,
                "status": "transition_observing",
                "target_id": target_id,
                "observation_id": observation_id,
            }
        return {"ok": True, "status": "transitioned", "target_id": target_id}

    debug.snapshot = snapshot
    debug.navigation_step = navigation_step

    result = runtime._navigate(48)

    assert result["status"] == "transitioned"
    assert result["steps"] == 2
    assert calls["count"] == 2
    assert [entry["status"] for entry in result["progress"]] == [
        "transition_observing",
        "transitioned",
    ]
    navigation_events = [
        event for event in runtime.events()
        if event.get("type") == "navigation"
    ]
    assert [event["details"]["result"]["status"] for event in navigation_events] == [
        "transition_observing",
        "transitioned",
    ]


def test_agent_interrupt_atomically_retires_navigation_observation(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    target = {
        "id": 48,
        "x": 32,
        "y": 0,
        "sprite_id": 0,
        "state": 1,
        "trigger_mode": 4,
        "trigger_script": 4631,
        "auto_script": 0,
        "blocking": False,
        "interactable": True,
    }
    state = make_state(objects=[target])
    write_state(debug, state)
    assert debug.navigation_step(state, target_id=48)["status"] == "stepping"
    debug.action_path.unlink()
    debug.pending_written_action = None
    pending = debug.pending_transition_observation
    observation_id = pending["observation_id"]
    runtime.state.update(status="running", phase="executing")

    result = runtime.interrupt()

    assert result["status"] == "interrupting"
    assert runtime.cancel_event.is_set()
    assert debug.pending_transition_observation is None
    assert debug.navigation_transition_requests == {}
    assert debug.observations.get(observation_id) is None
    assert not debug.action_path.exists()
    last_route = debug.read_exploration()["rooms"][debug.room_identity(state)["id"]]["last_route"]
    assert last_route["outcome"] == "agent_interrupt"


def test_agent_interrupt_clears_linked_search_interaction(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    target = {
        "id": 75, "x": 16, "y": 8, "sprite_id": 1, "state": 1,
        "trigger_mode": 1, "trigger_script": 8149, "auto_script": 0,
        "blocking": False, "interactable": True,
    }
    state = make_state(objects=[target])
    interaction_id = debug.mark_pending_interaction_observation(
        state, 75, action={"buttons": ["search"], "frames": 1},
    )
    transition_id = debug.mark_pending_transition_observation(
        state, 75, owner="navigation",
        linked_interaction=debug.pending_interaction_observation,
    )
    runtime.state.update(status="running", phase="executing")

    runtime.interrupt()

    assert debug.pending_transition_observation is None
    assert debug.pending_interaction_observation is None
    assert debug.navigation_transition_requests == {}
    assert debug.observations.get(transition_id) is None
    assert debug.observations.get(interaction_id)['status'] == 'cancelled'


def test_navigation_receives_transition_confirmed_during_action_wait(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    target = {
        "id": 48,
        "x": 32,
        "y": 0,
        "sprite_id": 0,
        "state": 1,
        "trigger_mode": 4,
        "trigger_script": 60,
        "auto_script": 0,
        "blocking": False,
        "interactable": True,
    }
    source = make_state(width=6, height=4, start=(1, 1), objects=[target], frame=10)
    write_state(debug, source)
    destination = make_state(width=6, height=4, start=(4, 2), objects=[], frame=11)
    destination["_logical_room"] = debug.logical_room_identity(source)
    destination["trigger_observation"] = {
        "generation": 1,
        "event_object_id": 48,
        "trigger_script": 60,
        "completed": True,
        "source": {"scene_id": 3, "map_id": 10, **source["party_position"]},
        "destination": {"scene_id": 3, "map_id": 10, **destination["party_position"], "frame": 10},
    }

    def complete_during_wait(**_kwargs):
        debug.action_path.unlink()
        write_state(debug, destination)
        debug.snapshot()
        destination["frame"] = 12
        write_state(debug, destination)
        return debug.snapshot()

    monkeypatch.setattr(runtime, "_wait_action_and_stable", complete_during_wait)
    monkeypatch.setattr("palpal.agent_runtime.time.sleep", lambda _seconds: None)

    result = runtime._navigate(48)
    assert result["ok"]
    assert result["status"] == "transitioned"
    assert [entry["status"] for entry in result["progress"]] == ["stepping", "transitioned"]


def test_navigation_treats_dialog_started_during_navigation_as_success(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    moving = make_state()
    dialog = make_state()
    dialog["dialog"] = {
        "active": True,
        "visible": True,
        "latest_event": {
            "sequence": 56,
            "message_id": 605,
            "event_object_id": 57,
            "text": "搞什麼～慢吞吞的！",
        },
    }
    snapshots = [moving, dialog]
    results = [
        {
            "ok": True,
            "status": "stepping",
            "target_id": 57,
            "move": "left",
            "segment_steps": 1,
            "remaining_steps": 0,
        },
        {
            "ok": False,
            "status": "in_dialog",
            "target_id": 57,
        },
    ]

    debug.snapshot = lambda: snapshots.pop(0)
    debug.navigation_step = lambda state, target_id, **_kwargs: results.pop(0)
    runtime._wait_action_and_stable = lambda **kwargs: dialog

    result = runtime._navigate(57)
    assert result["ok"] is True
    assert result["status"] == "interrupted_by_dialog"
    assert result["target_id"] == 57
    assert result["steps"] == 2
    assert [entry["status"] for entry in result["progress"]] == ["stepping", "in_dialog"]


def test_dynamic_touch_transition_dialog_interrupt_does_not_fail_navigation(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    target = {
        "id": 117,
        "x": 32,
        "y": 0,
        "sprite_id": 21,
        "state": 1,
        "trigger_mode": 4,
        "trigger_script": 5925,
        "auto_script": 0,
        "blocking": False,
        "interactable": True,
    }
    source = make_state(objects=[target], frame=10)
    write_state(debug, source)
    debug.snapshot()
    destination = make_state(objects=[], frame=11)
    destination.update({"scene_id": 15, "map_id": 3, "room": {"key": "shore"}})
    destination["dialog"] = {
        "active": True,
        "visible": True,
        "latest_event": {"sequence": 1, "message_id": 605, "text": "靠岸"},
    }
    destination["trigger_observation"] = {
        "generation": 7,
        "event_object_id": 117,
        "trigger_script": 5925,
        "completed": True,
        "source": {"scene_id": 3, "map_id": 10, "x": 32, "y": 0},
        "destination": {"scene_id": 15, "map_id": 3, **destination["party_position"]},
    }

    def land_during_action_wait(**_kwargs):
        debug.action_path.unlink()
        debug.pending_written_action = None
        write_state(debug, destination)
        return debug.snapshot()

    monkeypatch.setattr(runtime, "_wait_action_and_stable", land_during_action_wait)

    result = runtime._navigate(117)

    assert result["ok"] is True
    assert result["status"] == "interrupted_by_dialog"
    assert [entry["status"] for entry in result["progress"]] == ["stepping", "in_dialog"]
    observation_id = debug.navigation_transition_requests[117]
    assert debug.pending_transition_observation["observation_id"] == observation_id
    assert debug.observations.get(observation_id)["status"] == "pending"
    assert runtime.snapshot()["stop_reason"] != "execution_failed"


def test_dynamic_touch_delayed_trigger_stops_agent_before_reverse_step(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    target = {
        "id": 248,
        "x": 32,
        "y": 0,
        "sprite_id": 84,
        "state": 1,
        "trigger_mode": 4,
        "trigger_script": 8496,
        "auto_script": 2,
        "blocking": False,
        "interactable": True,
    }
    source = make_state(width=7, height=5, start=(1, 1), objects=[target], frame=10)
    source.update({"scene_id": 17, "map_id": 6, "room": {"key": "lotus"}})
    write_state(debug, source)
    debug.snapshot()

    reached = make_state(width=7, height=5, start=(2, 0), objects=[target], frame=11)
    reached.update({"scene_id": 17, "map_id": 6, "room": {"key": "lotus"}})
    destination = make_state(width=7, height=5, start=(4, 2), objects=[target], frame=12)
    destination.update({"scene_id": 17, "map_id": 6, "room": {"key": "lotus"}})
    destination["trigger_observation"] = {
        "generation": 7,
        "event_object_id": 248,
        "trigger_script": 8496,
        "completed": True,
        "source": {"scene_id": 17, "map_id": 6, "x": 32, "y": 0},
        "destination": {"scene_id": 17, "map_id": 6, **destination["party_position"], "frame": 12},
    }

    def transition_during_action_wait(**_kwargs):
        debug.action_path.unlink()
        debug.pending_written_action = None
        write_state(debug, reached)
        debug.snapshot()
        write_state(debug, destination)
        debug.snapshot()
        destination["frame"] = 13
        write_state(debug, destination)
        return debug.snapshot()

    monkeypatch.setattr(runtime, "_wait_action_and_stable", transition_during_action_wait)

    result = runtime._navigate(248)

    assert result["ok"] is True
    assert result["status"] == "transitioned"
    assert result["steps"] == 2
    assert [entry["status"] for entry in result["progress"]] == ["stepping", "transitioned"]
    assert not debug.action_path.exists()


def test_action_wait_accepts_direct_generation_completion_snapshot(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    completed = {
        **make_state(),
        "game_instance_id": "game-a",
        "agent_action": {
            "generation": 8,
            "pending": False,
            "remaining_steps": 0,
            "waiting_for_move": False,
        },
    }
    write_state(debug, completed)

    observed = runtime._wait_action_completion(
        {
            "time": time.time(),
            "game_instance_id": "game-a",
            "action_generation_before": 7,
            "action_handshake_supported": True,
        },
        receipt_timeout=0.1,
        completion_timeout=0.1,
    )

    assert observed["agent_action"]["generation"] == 8
    assert observed["agent_action"]["pending"] is False


def test_action_wait_has_distinct_generation_and_completion_timeouts(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    waiting_for_receipt = {
        **make_state(),
        "game_instance_id": "game-a",
        "agent_action": {
            "generation": 7,
            "pending": False,
            "remaining_steps": 0,
            "waiting_for_move": False,
        },
    }
    write_state(debug, waiting_for_receipt)
    action = {
        "time": time.time(),
        "game_instance_id": "game-a",
        "action_generation_before": 7,
        "action_handshake_supported": True,
    }

    with pytest.raises(RuntimeError, match="action_generation_timeout"):
        runtime._wait_action_completion(action, receipt_timeout=0.01, completion_timeout=0.01)

    waiting_for_completion = {
        **waiting_for_receipt,
        "agent_action": {
            "generation": 8,
            "pending": True,
            "remaining_steps": 1,
            "waiting_for_move": True,
        },
    }
    write_state(debug, waiting_for_completion)
    with pytest.raises(RuntimeError, match="action_completion_timeout"):
        runtime._wait_action_completion(action, receipt_timeout=0.01, completion_timeout=0.01)


def test_action_wait_exits_on_game_instance_change(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    write_state(
        debug,
        {
            **make_state(),
            "game_instance_id": "game-b",
            "agent_action": {
                "generation": 1,
                "pending": False,
                "remaining_steps": 0,
                "waiting_for_move": False,
            },
        },
    )

    with pytest.raises(RuntimeError, match="game_instance_changed"):
        runtime._wait_action_completion(
            {
                "time": time.time(),
                "game_instance_id": "game-a",
                "action_generation_before": 0,
                "action_handshake_supported": True,
            },
            receipt_timeout=0.1,
            completion_timeout=0.1,
        )


def test_navigation_replans_after_partial_segment_execution(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    source = make_state(start=(1, 1))
    partial = make_state(start=(1, 2))
    states = [source, partial]
    results = [
        {
            "ok": True,
            "status": "stepping",
            "target_id": 95,
            "move": "down",
            "segment_steps": 3,
            "remaining_steps": 2,
            "expected_node": {"x": 16, "y": 32},
            "action": {"action_generation_before": 10},
        },
        {"ok": True, "status": "arrived", "target_id": 95},
    ]

    debug.snapshot = lambda: states.pop(0) if states else partial
    debug.navigation_step = lambda state, target_id, **_kwargs: results.pop(0)
    monkeypatch.setattr(runtime, "_wait_action_and_stable", lambda **_kwargs: partial)

    result = runtime._navigate(95)

    assert result["status"] == "arrived"
    assert [entry["status"] for entry in result["progress"]] == ["stepping", "arrived"]


def test_navigation_approach_binds_item_and_finishes_without_item_use(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    state = make_state(start=(4, 4))
    calls = []

    debug.snapshot = lambda: state

    def navigation_step(current, target_id, approach_item_id=None, **_kwargs):
        calls.append((current, target_id, approach_item_id))
        return {
            "ok": True,
            "status": "approached",
            "target_id": target_id,
            "required_direction": "left",
            "approach": {"item_id": approach_item_id},
        }

    debug.navigation_step = navigation_step
    result = runtime._navigate(238, approach_item_id=279)

    assert result["status"] == "approached"
    assert [(target_id, item_id) for _, target_id, item_id in calls] == [(238, 279)]
    assert not debug.action_path.exists()


def test_navigation_stops_after_one_identical_no_progress_retry(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    stationary = make_state(start=(1, 1))
    action = {"action_generation_before": 20}
    result = {
        "ok": True,
        "status": "stepping",
        "target_id": 95,
        "move": "up",
        "segment_steps": 2,
        "remaining_steps": 4,
        "expected_node": {"x": 16, "y": -8},
        "action": action,
    }
    writes = []

    debug.snapshot = lambda: stationary

    def navigation_step(state, target_id, **_kwargs):
        writes.append((result["move"], result["segment_steps"], result["remaining_steps"]))
        return result

    debug.navigation_step = navigation_step
    monkeypatch.setattr(runtime, "_wait_action_and_stable", lambda **_kwargs: stationary)

    with pytest.raises(RuntimeError, match="navigation_no_progress"):
        runtime._navigate(95)

    assert writes == [("up", 2, 4), ("up", 2, 4)]


def test_navigation_in_dialog_before_execution_still_requests_replan(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    state = make_state()
    state["dialog"] = {
        "active": True,
        "latest_event": {
            "sequence": 56,
            "message_id": 605,
            "event_object_id": 57,
            "text": "搞什麼～慢吞吞的！",
        },
    }
    recommendation = debug.attach_decision_context(
        state,
        decision("navigation_step", {"id": 57}, "inspect_target"),
    )
    debug.snapshot = lambda: state

    with pytest.raises(ReplanRequired, match="in_dialog"):
        runtime._execute_decision(recommendation)


def test_sub_limit_query_tool_outputs_are_serialized_verbatim(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    outputs = [
        json.dumps({"ok": True, "navigation": {"targets": [{"id": 1}] * 20}}),
        json.dumps({"ok": True, "dialogs": [{"text": "观察" * 100}]} , ensure_ascii=False),
        json.dumps({"ok": True, "inventory": [{"item_id": index} for index in range(100)]}),
    ]
    assert [runtime._final_model_tool_output(json.loads(output)) for output in outputs] == [
        json.dumps(json.loads(output), ensure_ascii=False, separators=(",", ":"))
        for output in outputs
    ]


@pytest.mark.parametrize(
    ("raw", "expected_format"),
    [
        (json.dumps({"ok": True, "status": "complete", "events": [{"text": "中" * 9000}]}, ensure_ascii=False), "json"),
        ("HEAD-" + ("x" * 25000) + "-TAIL", "text"),
    ],
)
def test_large_tool_outputs_archive_preview_and_page_exact_content(tmp_path, raw, expected_format):
    runtime = make_debug_state(tmp_path).agent_runtime
    value = json.loads(raw) if expected_format == "json" else raw
    persisted = runtime._final_model_tool_output(value)
    preview = json.loads(persisted)
    archived_raw = (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if expected_format == "json" else value
    )
    assert preview["truncated"] is True
    assert preview["archived"] is True
    assert preview["ok"] is True
    assert preview["format"] == expected_format
    assert preview["head"] == archived_raw[:2000]
    assert preview["tail"] == archived_raw[-2000:]
    if expected_format == "json":
        assert preview["key_fields"]["status"] == "complete"
        assert preview["key_fields"]["events_count"] == 1

    parts = []
    offset = 0
    while True:
        page = runtime.get_tool_output(preview["output_ref"], offset)
        assert page["ok"]
        assert len(page["content"]) <= 6000
        parts.append(page["content"])
        if page["complete"]:
            assert page["next_offset"] is None
            break
        offset = page["next_offset"]
    assert "".join(parts) == archived_raw


def test_tool_output_refs_reject_paths_missing_and_cross_session_access(tmp_path):
    runtime = make_debug_state(tmp_path).agent_runtime
    output_ref = runtime._archive_tool_output("saved output")
    assert runtime.get_tool_output(output_ref)["content"] == "saved output"
    assert runtime.get_tool_output("../" + output_ref)["error"] == "invalid_output_ref"
    assert runtime.get_tool_output("tool-" + ("0" * 32) + ".txt")["error"] == "tool_output_not_found"
    assert runtime.get_tool_output(output_ref, -1)["error"] == "invalid_offset"

    runtime.reset("new_session")
    assert runtime.get_tool_output(output_ref)["error"] == "tool_output_not_found"


def test_large_tool_output_is_stable_from_first_model_visible_result(tmp_path):
    runtime = make_debug_state(tmp_path).agent_runtime
    result = {"ok": True, "data": "甲" * 9000}

    first = runtime._final_model_tool_output(result)
    preview = json.loads(first)
    archived = runtime.get_tool_output(preview["output_ref"])["content"]
    session = MemorySession([{"type": "function_call_output", "output": first}])
    prefix = json.dumps(session.items, ensure_ascii=False, separators=(",", ":")).encode()
    asyncio.run(session.add_items([{"role": "user", "content": "next turn"}]))

    assert preview["archived"] is True
    assert archived.startswith('{"ok":true,"data":"')
    assert json.dumps(session.items[:1], ensure_ascii=False, separators=(",", ":")).encode() == prefix


def test_prune_sessions_removes_non_live_session_data(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    live_id = runtime.snapshot()["session_id"]
    other_id = "a" * 32

    runtime.emit("decision", "ready", "Live activity.")
    other_state = dict(runtime.snapshot(), session_id=other_id)
    runtime._event_for(other_state, "decision", "ready", "Other activity.")

    other_dir = runtime._session_tool_outputs_dir(other_id)
    other_dir.mkdir(parents=True, exist_ok=True)
    ref = "tool-" + "b" * 32 + ".txt"
    (other_dir / ref).write_text("other output", encoding="utf-8")

    runtime._submit(runtime._replace_sdk_session_items(other_id, [
        {"role": "user", "content": "other transcript"},
    ])).result(timeout=5)

    result = runtime.prune_sessions({other_id})
    assert result["events_removed"] == 1
    assert result["tool_output_dirs_removed"] == 1
    assert result["sqlite_sessions_cleared"] == 1
    assert not other_dir.exists()
    assert any(event["summary"] == "Live activity." for event in runtime.events(limit=500))
    assert all(event["session_id"] != other_id for event in runtime._read_events_file())
    remaining = runtime._submit(runtime._sdk_session_items(other_id)).result(timeout=5)
    assert remaining == []

    protected = runtime.prune_sessions({live_id})
    assert protected["skipped_live"] == [live_id]
    assert protected["events_removed"] == 0


def test_prune_sessions_rejects_running_agent(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    runtime.state["status"] = "running"
    runtime._persist()
    with pytest.raises(RuntimeError, match="agent_session_running"):
        runtime.prune_sessions({"a" * 32})


def test_discover_session_ids_finds_events_tool_outputs_and_sqlite(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    live_id = runtime.snapshot()["session_id"]
    other_id = "a" * 32
    runtime.emit("decision", "ready", "Live activity.")
    other_state = dict(runtime.snapshot(), session_id=other_id)
    runtime._event_for(other_state, "decision", "ready", "Other activity.")
    runtime._session_tool_outputs_dir(other_id).mkdir(parents=True, exist_ok=True)
    runtime._submit(runtime._replace_sdk_session_items(other_id, [
        {"role": "user", "content": "other transcript"},
    ])).result(timeout=5)

    discovered = runtime.discover_session_ids()
    assert live_id in discovered
    assert other_id in discovered


def test_prune_sessions_protects_battle_delegate_child_session(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    live_id = runtime.snapshot()["session_id"]
    child_id = "c" * 32
    runtime.state["battle_delegate"] = {
        "child_session_id": child_id,
        "parent_session_id": live_id,
        "status": "running",
    }
    runtime._persist()
    runtime._submit(runtime._replace_sdk_session_items(child_id, [
        {"role": "user", "content": "battle context"},
    ])).result(timeout=5)

    result = runtime.prune_sessions({child_id})
    assert child_id in result["skipped_protected"]
    assert child_id not in result["pruned_session_ids"]
    remaining = runtime._submit(runtime._sdk_session_items(child_id)).result(timeout=5)
    assert [item["content"] for item in remaining] == ["battle context"]
