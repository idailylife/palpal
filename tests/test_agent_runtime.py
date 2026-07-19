import asyncio
import json
import sys
import threading
import time
import types

import pytest

from conftest import make_debug_state, make_state, write_state
from palpal.agent_runtime import (
    DEFAULT_COMPACT_AT_TOKENS,
    TOOL_OUTPUT_PRESSURE_TARGET_TOKENS,
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
        "recommended_action": {"tool": tool, "args": args or {}, "dry_run": True},
        "evidence": {},
        "risk": "test",
        "alternatives": [],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def submission(action="wait", reason="test decision", **kwargs):
    return {"action": action, "reason": reason, **kwargs}


def wait_for_pause(runtime, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime.snapshot()["status"] == "paused":
            return runtime.snapshot()
        time.sleep(0.02)
    raise AssertionError("agent runtime did not pause")


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
    assert paused["stop_reason"] == "no_op"
    assert paused["usage"]["total_tokens"] == 15

    runtime.start(None, 1)
    resumed = wait_for_pause(runtime)
    assert resumed["turn"] == 2
    assert resumed["usage"]["total_tokens"] == 30
    assert any(event["type"] == "decision" for event in runtime.events())


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
    debug.frame_path.write_bytes(b"frame")
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

    class Completions:
        async def create(self, **kwargs):
            message = type("Message", (), {"content": "compact summary"})()
            choice = type("Choice", (), {"message": message})()
            return type("Response", (), {"choices": [choice]})()

    session = Session()
    client = type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
    asyncio.run(runtime._maybe_compact_sdk(session, client, "next"))
    assert session.items[0]["content"].startswith("Persistent context summary:")
    assert session.items[-2:] == original[-2:]
    assert runtime.snapshot()["compactions"] == 1
    assert any(event["type"] == "compression" and event["phase"] == "completed" for event in runtime.events())


def test_context_compaction_threshold_defaults_and_invalid_values(tmp_path, monkeypatch):
    runtime = make_debug_state(tmp_path).agent_runtime
    monkeypatch.delenv("PALPAL_AGENT_COMPACT_AT_TOKENS", raising=False)
    assert runtime._compact_at_tokens() == DEFAULT_COMPACT_AT_TOKENS == 128_000
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "invalid")
    assert runtime._compact_at_tokens() == DEFAULT_COMPACT_AT_TOKENS
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "0")
    assert runtime._compact_at_tokens() == DEFAULT_COMPACT_AT_TOKENS
    monkeypatch.setenv("PALPAL_AGENT_COMPACT_AT_TOKENS", "64000")
    assert runtime._compact_at_tokens() == 64_000


def test_context_estimate_counts_utf8_bytes_for_chinese():
    assert AgentRuntime._estimate_text_tokens("中" * 100) == 100
    assert AgentRuntime._estimate_text_tokens("x" * 300) == 100


def test_sdk_adapter_uses_streaming_tools_sqlite_and_buffering(tmp_path, monkeypatch):
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
            "usage": type("Usage", (), {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15})()
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
    fake_agents.OpenAIChatCompletionsModel = Model
    fake_agents.Runner = Runner
    fake_agents.SQLiteSession = SQLiteSession
    fake_agents.set_tracing_disabled = lambda **kwargs: captured.setdefault("tracing", kwargs)
    fake_agent_module = types.ModuleType("agents.agent")
    fake_agent_module.StopAtTools = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    monkeypatch.setitem(sys.modules, "agents.agent", fake_agent_module)
    monkeypatch.setattr(runtime, "_maybe_compact_sdk", lambda *args: asyncio.sleep(0))

    result = asyncio.run(runtime._sdk_decide(make_state(), "feedback"))
    expected_tools = [spec["function"]["name"] for spec in debug.agent_tool_specs()]
    assert captured["model"]["buffer_streamed_tool_calls"] is True
    assert [tool.name for tool in captured["agent"]["tools"]] == expected_tools
    assert captured["agent"]["tool_use_behavior"] == {"stop_at_tool_names": ["submit_decision"]}
    assert captured["sqlite"] == (runtime.snapshot()["session_id"], runtime.sqlite_path)
    assert captured["run"]["session"].__class__ is SQLiteSession
    assert captured["run"]["max_turns"] == 8
    assert captured["tracing"] == {"disabled": True}
    assert result["usage"]["total_tokens"] == 15


def test_sdk_prompt_omits_redundant_goal_tools_and_constraints(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    captured = {}

    class Session:
        async def get_items(self): return []
        async def clear_session(self): pass
        async def add_items(self, items): pass

    class Stream:
        final_output = json.dumps(decision())
        context_wrapper = None
        async def stream_events(self):
            if False: yield None

    class Runner:
        @classmethod
        def run_streamed(cls, agent, prompt, **kwargs):
            captured["prompt"] = json.loads(prompt)
            return Stream()

    fake_agents = types.ModuleType("agents")
    fake_agents.Agent = lambda **kwargs: object()
    fake_agents.AsyncOpenAI = lambda **kwargs: object()
    fake_agents.FunctionTool = lambda **kwargs: object()
    fake_agents.ModelSettings = lambda **kwargs: kwargs
    fake_agents.OpenAIChatCompletionsModel = lambda **kwargs: object()
    fake_agents.Runner = Runner
    fake_agents.SQLiteSession = lambda *args: Session()
    fake_agents.set_tracing_disabled = lambda **kwargs: None
    fake_agent_module = types.ModuleType("agents.agent")
    fake_agent_module.StopAtTools = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    monkeypatch.setitem(sys.modules, "agents.agent", fake_agent_module)
    monkeypatch.setattr(runtime, "_maybe_compact_sdk", lambda *args: asyncio.sleep(0))

    asyncio.run(runtime._sdk_decide(make_state(), "compact feedback"))
    assert set(captured["prompt"]) == {"feedback", "context"}
    assert set(captured["prompt"]["context"]) == {"state_summary", "active_tasks"}


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


def test_navigation_waits_for_scene_transition_instead_of_failing(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    entering = make_state()
    entering["entering_scene"] = True
    stable = make_state()
    stable["scene_id"] = 2
    snapshots = [entering, entering, stable]

    debug.snapshot = lambda: snapshots.pop(0) if snapshots else stable
    debug.navigation_step = lambda state, target_id: {
        "ok": False,
        "status": "entering_scene",
        "target_id": target_id,
    }

    result = runtime._navigate(4)
    assert result["ok"]
    assert result["status"] == "transitioned"
    assert result["target_id"] == 4
    assert result["steps"] == 1


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
    debug.navigation_step = lambda current, target_id: results.pop(0)
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
    debug.navigation_step = lambda state, target_id: results.pop(0)
    runtime._wait_action_and_stable = lambda **kwargs: dialog

    result = runtime._navigate(57)
    assert result["ok"] is True
    assert result["status"] == "interrupted_by_dialog"
    assert result["target_id"] == 57
    assert result["steps"] == 2
    assert [entry["status"] for entry in result["progress"]] == ["stepping", "in_dialog"]


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


def test_sub_limit_query_tool_outputs_remain_verbatim(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    outputs = [
        json.dumps({"ok": True, "navigation": {"targets": [{"id": 1}] * 20}}),
        json.dumps({"ok": True, "dialogs": [{"text": "观察" * 100}]} , ensure_ascii=False),
        json.dumps({"ok": True, "inventory": [{"item_id": index} for index in range(100)]}),
    ]
    session = MemorySession([
        {"type": "function_call_output", "output": output} for output in outputs
    ])
    asyncio.run(runtime._sanitize_sdk_session(session))
    assert [item["output"] for item in session.items] == outputs


@pytest.mark.parametrize(
    ("raw", "expected_format"),
    [
        (json.dumps({"ok": True, "status": "complete", "events": [{"text": "中" * 9000}]}, ensure_ascii=False), "json"),
        ("HEAD-" + ("x" * 25000) + "-TAIL", "text"),
    ],
)
def test_large_tool_outputs_archive_preview_and_page_exact_content(tmp_path, raw, expected_format):
    runtime = make_debug_state(tmp_path).agent_runtime
    session = MemorySession([{"type": "function_call_output", "output": raw}])
    asyncio.run(runtime._sanitize_sdk_session(session))

    preview = json.loads(session.items[0]["output"])
    assert preview["truncated"] is True
    assert preview["archived"] is True
    assert preview["ok"] is True
    assert preview["format"] == expected_format
    assert preview["head"] == raw[:2000]
    assert preview["tail"] == raw[-2000:]
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
    assert "".join(parts) == raw


def test_tool_output_refs_reject_paths_missing_and_cross_session_access(tmp_path):
    runtime = make_debug_state(tmp_path).agent_runtime
    output_ref = runtime._archive_tool_output("saved output")
    assert runtime.get_tool_output(output_ref)["content"] == "saved output"
    assert runtime.get_tool_output("../" + output_ref)["error"] == "invalid_output_ref"
    assert runtime.get_tool_output("tool-" + ("0" * 32) + ".txt")["error"] == "tool_output_not_found"
    assert runtime.get_tool_output(output_ref, -1)["error"] == "invalid_offset"

    runtime.reset("new_session")
    assert runtime.get_tool_output(output_ref)["error"] == "tool_output_not_found"


def test_tool_output_pressure_cleanup_has_hysteresis_and_keeps_recent_six(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    monkeypatch.setattr(debug, "agent_system_prompt", lambda: "")
    monkeypatch.setattr(debug, "agent_policy_prompt", lambda: "")
    monkeypatch.setattr(debug, "agent_tool_specs", lambda: [])
    raw_outputs = [json.dumps({"ok": True, "index": index, "data": "x" * 19900}) for index in range(18)]
    session = MemorySession([
        {"type": "function_call_output", "output": output} for output in raw_outputs
    ])

    asyncio.run(runtime._sanitize_sdk_session(session))
    parsed = [json.loads(item["output"]) for item in session.items]
    assert any(value.get("archived") is True for value in parsed[:-6])
    assert [item["output"] for item in session.items[-6:]] == raw_outputs[-6:]
    assert runtime.snapshot()["context_estimate_tokens"] <= TOOL_OUTPUT_PRESSURE_TARGET_TOKENS
    refs = [value["output_ref"] for value in parsed[:-6] if value.get("archived") is True]
    files_before = sorted(path.name for path in runtime._session_tool_outputs_dir().iterdir())

    asyncio.run(runtime._sanitize_sdk_session(session))
    parsed_again = [json.loads(item["output"]) for item in session.items]
    assert [value["output_ref"] for value in parsed_again[:-6] if value.get("archived") is True] == refs
    assert sorted(path.name for path in runtime._session_tool_outputs_dir().iterdir()) == files_before


def test_tool_output_pressure_cleanup_waits_until_96k(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    monkeypatch.setattr(debug, "agent_system_prompt", lambda: "")
    monkeypatch.setattr(debug, "agent_policy_prompt", lambda: "")
    monkeypatch.setattr(debug, "agent_tool_specs", lambda: [])
    raw_outputs = [json.dumps({"ok": True, "data": "x" * 19900}) for _ in range(13)]
    session = MemorySession([
        {"type": "function_call_output", "output": output} for output in raw_outputs
    ])

    asyncio.run(runtime._sanitize_sdk_session(session))
    assert [item["output"] for item in session.items] == raw_outputs
    assert not runtime._session_tool_outputs_dir().exists()
