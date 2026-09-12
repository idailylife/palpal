import copy
import hashlib
import json
import threading

import pytest

from palpal.debug_server import DIR_TO_BUTTON, decode_nav_grid, nav_bit, search_hits, world_pos
from tests.support import make_debug_state, make_state, write_state


def target(x=240, y=120):
    return dict(id=810, x=x, y=y, sprite_id=150, state=2, trigger_mode=2,
                trigger_script=14191, auto_script=14202, blocking=True, interactable=True)


def test_navigation_observation_omits_presentation_but_records_route(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = make_state(width=25, height=25, start=(0, 0), objects=[target()])
    write_state(debug, state)
    result = debug.navigation_step(state, 810)
    assert result["status"] == "stepping"
    debug.action_path.unlink()
    state["party_position"] = result["expected_node"]
    write_state(debug, state)
    monkeypatch.setattr(debug, "lightweight_navigation", lambda *_: pytest.fail("presentation on movement path"))
    monkeypatch.setattr(debug, "exploration_view", lambda *_args, **_kw: pytest.fail("full exploration on movement path"))
    observed = debug.navigation_snapshot()
    assert "_navigation" not in observed
    assert "agent_session" not in observed["_debug"]
    room = debug.read_exploration()["rooms"][debug.room_identity(observed)["id"]]
    assert room["last_route"]["outcome"] == "expected_node_reached"
    assert len(room["visited"]) >= result["segment_steps"]


def test_affordance_reads_history_once_independent_of_target_count(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[{**target(), "id": n} for n in range(30)])
    debug.interaction_context_snapshot()  # Initialize the persistent context once.
    read = debug.read_exploration
    calls = []

    def counted():
        calls.append(1)
        return read()

    monkeypatch.setattr(debug, "read_exploration", counted)
    debug.lightweight_navigation(state)
    assert len(calls) == 1


def test_history_cache_is_immutable_and_tracks_file_replacement(tmp_path):
    debug = make_debug_state(tmp_path)
    data = debug.empty_exploration()
    data["tasks_revision"] = 12
    debug.write_exploration(data)
    first = debug.read_exploration()
    first["tasks_revision"] = 99
    assert debug.read_exploration()["tasks_revision"] == 12
    data["tasks_revision"] = 13
    # An external writer does not call write_exploration or invalidate its cache.
    debug.atomic_write_text(debug.exploration_path, json.dumps(data))
    assert debug.read_exploration()["tasks_revision"] == 13


def test_primed_history_cache_matches_normal_read_normalization(tmp_path):
    debug = make_debug_state(tmp_path)
    data = debug.empty_exploration()
    data["rooms"]["legacy"] = {"complete": True}
    debug.write_exploration(data)
    cached = debug.read_exploration()
    debug.exploration_read_cache = None
    assert debug.read_exploration() == cached
    assert "complete" not in cached["rooms"]["legacy"]


def test_components_and_room_hash_match_reference_and_invalidate(tmp_path):
    debug = make_debug_state(tmp_path)
    for blocked in [(), ((2, 2), (3, 3)), ((1, 1), (2, 2))]:
        state = make_state(width=12, height=14, blocked=blocked)
        grid = decode_nav_grid(state)
        passable = {(x, y) for y in range(grid["height"]) for x in range(grid["width"]) if nav_bit(grid, (x, y))}
        seed, _ = debug.logical_room_seed(grid, (1, 1), passable)
        expected = debug.logical_room_component(grid, seed, passable)
        actual, _ = debug.static_room_component_for_state(state, grid)
        assert actual == expected
        payload = [3, 10, [list(world_pos(grid, node)) for node in sorted(expected, key=lambda n: (n[1], n[0]))]]
        digest = hashlib.sha256(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        assert debug.logical_room_hash(3, 10, grid, actual) == digest
        actual.clear()
        assert debug.static_room_component_for_state(state, grid)[0] == expected


def test_touch_cut_cache_uses_exact_dynamic_boundary(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(width=12, height=12, start=(0, 0), objects=[{**target(32, 16), "trigger_mode": 4, "sprite_id": 0}])
    grid = decode_nav_grid(state)
    before = debug.logical_room_component_for_state(state, grid)
    state["objects"][0].update(x=112, y=56)
    after = debug.logical_room_component_for_state(state, grid)
    assert before[1] != after[1]
    assert after[0].isdisjoint(after[1])


def test_moving_targets_use_short_segments_and_reset_on_instance_change(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(width=25, height=25, start=(0, 0), objects=[target()])
    state["game_instance_id"] = "first"
    write_state(debug, state)
    assert debug.navigation_segment_limit(state, 810, 20) == 8
    state["objects"][0]["x"] += 16
    write_state(debug, state)
    assert debug.navigation_segment_limit(state, 810, 20) == 2
    assert debug.navigation_segment_limit(state, 810, 2) == 1
    result = debug.navigation_step(state, 810)
    assert result["segment_steps"] <= 2
    state["game_instance_id"] = "second"
    write_state(debug, state)
    assert debug.navigation_segment_limit(state, 810, 20) == 8


@pytest.mark.parametrize("always_change", [False, True])
def test_target_moving_during_planning_never_receives_old_action(tmp_path, monkeypatch, always_change):
    debug = make_debug_state(tmp_path)
    state = make_state(width=30, height=30, start=(0, 0), objects=[target()])
    write_state(debug, state)
    route = debug.route_to_object
    planned = []

    def change_after_route(current, *args, **kwargs):
        result = route(current, *args, **kwargs)
        planned.append(current["objects"][0]["x"])
        if always_change or len(planned) == 1:
            new = copy.deepcopy(current)
            new["objects"][0]["x"] += 16
            write_state(debug, new)
        return result

    monkeypatch.setattr(debug, "route_to_object", change_after_route)
    result = debug.navigation_step(state, 810)
    assert planned[:2] == [240, 256]
    if always_change:
        assert result["status"] == "replanning"
        assert len(planned) == 3
        assert not debug.action_path.exists()
    else:
        assert result["status"] == "stepping"
        assert len(debug.action_log) == 1
        assert result["segment_steps"] <= 2


def test_unrelated_moving_object_does_not_invalidate_movement(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = make_state(width=30, height=30, start=(0, 0), objects=[target(), {**target(400, 200), "id": 811}])
    write_state(debug, state)
    route = debug.route_to_object
    calls = []

    def moved(current, *args, **kwargs):
        result = route(current, *args, **kwargs)
        calls.append(1)
        new = copy.deepcopy(current)
        new["objects"][1]["x"] += 16
        write_state(debug, new)
        return result

    monkeypatch.setattr(debug, "route_to_object", moved)
    assert debug.navigation_step(state, 810)["status"] == "stepping"
    assert len(calls) == 1


def test_target_leaving_search_range_is_chased_without_search(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = make_state(width=30, height=30, start=(1, 1), objects=[target(32, 0)])
    write_state(debug, state)
    route = debug.route_to_object
    calls = []

    def escaped(current, *args, **kwargs):
        result = route(current, *args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            assert not result["route"]["moves"]
            new = copy.deepcopy(current)
            new["objects"][0].update(x=240, y=120)
            write_state(debug, new)
        return result

    monkeypatch.setattr(debug, "route_to_object", escaped)
    result = debug.navigation_step(state, 810)
    assert result["status"] == "stepping"
    assert all("search" not in action["buttons"] for action in debug.action_log)
    assert debug.pending_interaction_observation is None


def test_deterministic_turning_target_is_followed_with_fresh_short_segments(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(width=40, height=40, start=(0, 0), objects=[target()])
    state.update(game_instance_id="pursuit", agent_action={
        "generation": 0, "pending": False, "remaining_steps": 0, "waiting_for_move": False,
    })
    write_state(debug, state)
    trajectory = [(256, 128), (272, 120), (256, 112), (240, 120)]
    segments = []
    for index in range(60):
        x, y = trajectory[index % len(trajectory)]
        state["objects"][0].update(x=x, y=y)
        state["frame"] += 1
        write_state(debug, state)
        result = debug.navigation_step(debug.navigation_snapshot(), 810)
        if result["status"] == "interacting":
            position = state["party_position"]
            assert search_hits((position["x"], position["y"]), state["party_direction"], state["objects"][0])
            assert result["action"]["buttons"] == ["search"]
            break
        assert result["status"] in {"stepping", "facing"}
        if result["status"] == "stepping":
            assert result["segment_steps"] <= 2
            segments.append(result["move"])
            state["party_position"] = result["expected_node"]
        button = result.get("move") or result["required_direction"]
        state["party_direction"] = next(direction for direction, value in DIR_TO_BUTTON.items() if value == button)
        debug.action_path.unlink()
        state["agent_action"]["generation"] += 1
        write_state(debug, state)
    else:
        pytest.fail("target was never in verified search range")
    assert len(segments) > 2
    assert len(set(segments)) > 1


@pytest.mark.parametrize("interrupt", [False, True])
def test_control_change_or_cancel_during_planning_prevents_write(tmp_path, monkeypatch, interrupt):
    debug = make_debug_state(tmp_path)
    state = make_state(width=25, height=25, start=(0, 0), objects=[target()])
    write_state(debug, state)
    cancel = threading.Event()
    route = debug.route_to_object

    def changed(current, *args, **kwargs):
        result = route(current, *args, **kwargs)
        if interrupt:
            cancel.set()
        else:
            write_state(debug, {**current, "in_battle": True})
        return result

    monkeypatch.setattr(debug, "route_to_object", changed)
    result = debug.navigation_step(state, 810, cancel_event=cancel)
    assert result["status"] == ("cancelled" if interrupt else "in_battle")
    assert not debug.action_path.exists()


def test_agent_movement_uses_native_completion_without_quiet_wait(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    state = make_state()
    calls = []
    results = iter([
        dict(ok=True, status="stepping", action={"action_generation_before": 3}),
        dict(ok=True, status="arrived"),
    ])
    monkeypatch.setattr(debug, "navigation_snapshot", lambda: state)
    monkeypatch.setattr(debug, "navigation_step", lambda *_args, **_kw: next(results))
    monkeypatch.setattr(runtime, "_wait_action_completion", lambda action: calls.append(action) or state)
    monkeypatch.setattr(runtime, "_wait_action_and_stable", lambda **_kw: pytest.fail("quiet window during movement"))
    assert runtime._navigate(810)["status"] == "arrived"
    assert len(calls) == 1


def test_navigation_status_is_coherent_and_does_not_reconcile(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = {**make_state(), "game_instance_id": "test", "agent_action": {
        "generation": 7, "pending": False, "remaining_steps": 0, "waiting_for_move": False,
    }}
    write_state(debug, state)
    monkeypatch.setattr(debug, "snapshot", lambda **_kw: pytest.fail("full snapshot in status"))
    monkeypatch.setattr(debug, "read_exploration", lambda: pytest.fail("history in status"))
    status = debug.navigation_status()
    assert status["sequence"] == debug.live_state_sequence
    assert status["agent_action"]["generation"] == 7
    assert status["control"]["navigation_ready"]
    assert "nav_grid" not in status
