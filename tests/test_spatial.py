import copy
import threading

from palpal.agent_runtime import AgentRuntime
from palpal.debug_server import BUTTON_TO_DIR, MOVE_DELTAS
from palpal.spatial import MotionHistory, preview_placement
from tests.support import make_debug_state, make_state, make_object, write_state


def walk(state, path):
    state = copy.deepcopy(state)
    for step in path:
        state['party_position'] = {'x': step['x'], 'y': step['y']}
        state['party_direction'] = BUTTON_TO_DIR[step['move']]
    return state


def test_exact_position_and_face_uses_last_step(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(width=9, height=9, start=(4, 4))
    for face in MOVE_DELTAS:
        route = debug.route_to_position(state, 64, 32, face)
        assert route['ok']
        end = walk(state, route['path'])
        assert end['party_position'] == {'x': 64, 'y': 32}
        assert end['party_direction'] == BUTTON_TO_DIR[face]
    assert not debug.route_to_position(state, 65, 32)['ok']
    assert not debug.route_to_position(state, True, 32)['ok']


def test_obstacle_and_touch_boundary_are_not_corridors(tmp_path):
    debug = make_debug_state(tmp_path)
    obstacle = make_object(1, x=64, y=32, blocking=True, trigger_mode=0)
    state = make_state(width=9, height=9, start=(2, 2), objects=[obstacle])
    assert debug.route_to_position(state, 64, 32)['status'] == 'position_blocked'
    trigger = make_object(2, x=64, y=32, blocking=False, trigger_mode=5)
    state['objects'] = [trigger]
    route = debug.route_to_position(state, 96, 48)
    if route['ok']:
        assert not any(p['trigger_ids'] for p in route['path'][:-1])


def test_submission_and_cancel_before_write(tmp_path):
    debug = make_debug_state(tmp_path)
    raw = {'action': 'move_to', 'x': 0, 'y': 0, 'face': 'down', 'reason': 'position'}
    specs = {s['function']['name']: s['function'] for s in debug.agent_tool_specs()}
    assert set(raw) <= specs['submit_decision']['parameters']['properties'].keys()
    assert 'face' not in specs['inspect_screen']['parameters']['properties']
    assert debug.agent_decision_submission_error(raw) is None
    assert debug.agent_decision_submission_error({**raw, 'x': True})
    assert debug.agent_decision_submission_error({**raw, 'item_id': 3})
    cancelled = threading.Event(); cancelled.set()
    debug.write_action = lambda *a: (_ for _ in ()).throw(AssertionError('wrote after cancellation'))
    assert debug.move_to_step(make_state(), {'x': 0, 'y': 0}, cancelled)['status'] == 'cancelled'


def test_step_revalidates_dynamic_blocker(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(width=9, height=9, start=(2, 2))
    changed = copy.deepcopy(state)
    changed['objects'] = [make_object(1, x=48, y=24, blocking=True)]
    write_state(debug, changed)
    debug.write_action = lambda *a: (_ for _ in ()).throw(AssertionError('stale write'))
    assert debug.move_to_step(state, {'x': 64, 'y': 32})['status'] == 'state_changed'


def placement_state():
    state = make_state(width=9, height=9, start=(4, 4), objects=[make_object(7, state=0)])
    state['inventory'] = [{'item_id': 5, 'amount': 1, 'usable': True,
        'use_effect': {'requirements': [{'kind': 'place_event_object', 'event_object_id': 7}]}}]
    return state


def test_placement_offsets_and_hidden_object_result(tmp_path):
    debug = make_debug_state(tmp_path)
    state = placement_state()
    result = preview_placement(debug, state, 5)
    assert result['task_effectiveness'] == 'unknown'
    # Directions follow SDLPAL's isometric button mapping, checked explicitly.
    expected = {'up': (80, 24), 'right': (80, 40), 'down': (48, 40), 'left': (48, 24)}
    for option in result['options']:
        assert tuple(option['landing_position'].values()) == expected[option['face']]
        assert option['placement_valid'] and option['standing_reachable']
    match = debug.matched_item_event_object(state, 5)
    after = copy.deepcopy(state)
    after['objects'][0].update(state=2, x=48, y=40)
    effect = debug.item_object_effect_observation(state, after, match)
    assert effect['effect_observed']
    assert effect['object_delta']['x']['after'] == 48
    back = debug.item_object_effect_observation(after, state, match)
    assert back['object_delta']['state']['after'] == 0
    assert debug.item_object_effect_observation(state, state, match)['effect_observed'] is False


def test_motion_retains_turns_and_script_changes_without_future_path():
    history = MotionHistory()
    state = make_state(objects=[make_object(7)])
    for frame, (x, y) in enumerate([(0, 0), (8, 4), (16, 8), (8, 12)], 1):
        state['frame'] = frame
        state['objects'][0].update(x=x, y=y)
        history.observe(state)
    result = history.view(state, 7, offset=0, limit=2)
    assert result['moved'] and result['total'] == 3
    assert not result['complete']
    assert result['points'][1]['x'] == 16
    page = history.view(state, 7, offset=result['next_offset'])
    assert page['complete'] and page['points'][0]['y'] == 12
    cursor = history.serial
    state['frame'] += 1
    state['objects'][0]['auto_script'] = 123
    history.observe(state)
    assert history.serial == cursor
    state['objects'][0]['trigger_script'] = 456
    history.observe(state)
    assert history.view(state, 7)['start']['x'] == 0
    assert not history.changes(state, cursor)


def test_motion_bounds_room_reentry_and_timeline_reset():
    history = MotionHistory()
    state = make_state(objects=[make_object(7)])
    for frame in range(520):
        state['frame'] = frame
        state['objects'][0].update(x=frame, y=frame % 2)
        history.observe(state)
    assert history.view(state, 7)['truncated']
    original = copy.deepcopy(state)
    state['room']['key'] = 'another-room'
    history.observe(state)
    original['frame'] += 1
    original['objects'][0]['x'] += 20
    history.observe(original)
    assert history.view(original, 7)['end']['gap']
    original['frame'] = 0
    history.observe(original)
    assert history.view(original, 7)['total'] == 1
    assert not history.view(original, 7)['moved']
    for i in range(10):
        original['room']['key'] = str(i)
        history.observe(original)
    assert len(history.rooms) == 8


def test_movement_survives_action_compaction():
    result = AgentRuntime._compact_action_result({'object_movement': [{'object_id': 7, 'moved': True}],
                                                'cell': {'x': 16, 'y': 8}, 'face': 'right'})
    assert result['object_movement'][0]['object_id'] == 7
    assert result['face'] == 'right'


def test_move_runtime_stops_on_transition_without_second_action(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    start = make_state(width=9, height=9)
    changed = copy.deepcopy(start)
    changed['room']['key'] = 'other-room'
    changed['scene_id'] = 4
    states = iter([start, start, changed])
    debug.navigation_snapshot = lambda: next(states)
    calls = []
    debug.move_to_step = lambda *a: calls.append(a) or {'ok': True, 'status': 'stepping', 'action': {'generation': 1}}
    runtime._wait_action_completion = lambda *a: changed
    result = runtime._move_to({'x': 32, 'y': 16})
    assert result['reason'] == 'room_changed'
    assert len(calls) == 1


def test_move_runtime_control_and_no_progress_are_bounded(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    state = make_state()
    debug.navigation_snapshot = lambda: state
    debug.move_to_step = lambda *a: {'ok': False, 'status': 'dialog'}
    assert runtime._move_to({'x': 32, 'y': 16})['status'] == 'dialog'
    calls = []
    debug.move_to_step = lambda *a: calls.append(a) or {'ok': True, 'status': 'stepping', 'action': {'generation': 1}}
    runtime._wait_action_completion = lambda *a: state
    assert runtime._move_to({'x': 32, 'y': 16})['status'] == 'no_progress'
    assert len(calls) == 2


def test_post_trigger_observation_keeps_moving_samples_then_stops(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[make_object(7, x=0, y=0)])
    clock = [0.0]
    monkeypatch.setattr('palpal.agent_runtime.time.monotonic', lambda: clock[0])
    def update(timeout):
        clock[0] += .1
        if clock[0] < .6:
            state['objects'][0]['x'] += 8
        state['frame'] += 1
        debug.motion_history.observe(state)
    debug.motion_history.observe(state)
    debug.wait_navigation_update = update
    debug.navigation_snapshot = lambda: state
    after = debug.agent_runtime._observe_target_motion(copy.deepcopy(state), 7)
    assert after['objects'][0]['x'] == 40
    assert .9 <= clock[0] < 1.5
    debug.agent_runtime.cancel_event.set()
    import pytest
    with pytest.raises(RuntimeError, match='interrupted'):
        debug.agent_runtime._observe_target_motion(state, 7)


def test_move_submission_keeps_task_review_gate(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(width=9, height=9)
    write_state(debug, state)
    cycle = debug.new_agent_tool_cycle(state)
    cycle.update(required_task_ids=['spatial-task'], tasks_queried=False)
    result = debug.run_agent_tool('submit_decision', {
        'action': 'move_to', 'x': 32, 'y': 16, 'reason': 'position'}, state, tool_cycle=cycle)
    assert result['error']['code'] == 'task_review_required'
    assert not debug.action_path.exists()


def test_motion_current_status_uses_stream_freshness_and_resets(tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr('palpal.spatial.time.monotonic', lambda: clock[0])
    history = MotionHistory()
    state = make_state(objects=[make_object(7, x=0, y=0)], frame=10)
    history.observe(state)
    assert history.view(state, 7)['current_status'] == 'unknown'
    clock[0] += .2
    state['frame'] += 1
    state['objects'][0]['x'] = 16
    history.observe(state)
    assert history.view(state, 7)['current_status'] == 'moving'
    clock[0] += .5
    state['frame'] += 1
    history.observe(state)
    view = history.view(state, 7)
    assert view['moved'] and view['moved_scope'] == 'historical'
    assert view['current_status'] == 'stationary'
    assert view['sample_frame'] == 12 and view['last_moved_frame'] == 11
    assert view['observed_stationary_seconds'] == .5
    clock[0] += 1.1
    assert history.view(state, 7)['current_status'] == 'unknown'
    history.observe(state)
    assert history.view(state, 7)['current_status'] == 'unknown'
    state['objects'] = []
    history.observe(state)
    assert history.view(state, 7)['current_status'] == 'unknown'
    state['objects'] = [make_object(7, x=16, y=0)]
    history.observe(state)
    assert history.view(state, 7)['current_status'] == 'unknown'
    state['frame'] = 1
    history.observe(state)
    assert not history.view(state, 7)['moved']
    state['objects'][0]['state'] = 0
    clock[0] += .5
    history.observe(state)
    state['objects'][0]['state'] = 2
    clock[0] += .5
    history.observe(state)
    assert history.view(state, 7)['current_status'] == 'unknown'
    history.clear()
    assert history.view(state, 7)['current_status'] == 'unknown'


def test_completed_trigger_survives_route_replacement_and_target_movement(tmp_path):
    # Rounds 1202/1203: confirmed navigation-owned trigger, then moving target
    # changes the route endpoint/reachability while completion is being delivered.
    for generation, endpoint in ((31, (1088, 400)), (32, (1264, 344))):
        (tmp_path / str(generation)).mkdir()
        debug = make_debug_state(tmp_path / str(generation))
        target = make_object(797, x=96, y=0, trigger_mode=7, trigger_script=14777, blocking=True)
        state = make_state(width=9, height=9, start=(0, 8), objects=[target], frame=10)
        write_state(debug, state)
        debug.snapshot()
        with debug.spatial_execution_scope('navigate', 797) as execution:
            route = debug.route_to_object(state, 797)['route']
            debug.remember_navigation_route(state, 797, route, route['steps'], 0, 'reachable')
            expected = route['nodes'][-1]
            reached = copy.deepcopy(state)
            reached['party_position'] = {k: expected[k] for k in ('x', 'y')}
            reached['frame'] = 11
            write_state(debug, reached)
            debug.snapshot()
            completed = copy.deepcopy(reached)
            completed['frame'] = 12
            completed['objects'][0].update(x=endpoint[0], y=endpoint[1], trigger_mode=0)
            completed['trigger_observation'] = {
                'generation': generation, 'event_object_id': 797, 'trigger_script': 14777,
                'completed': True, 'source': {'scene_id': state['scene_id'], 'map_id': state['map_id'],
                                             'x': expected['x'], 'y': expected['y']},
            }
            write_state(debug, completed)
            debug.snapshot()
            assert execution['receipt'] is not None
            data = debug.read_exploration()
            data['rooms'][execution['room_id']]['last_route'] = {'outcome': 'unreachable_static'}
            debug.write_exploration(data)
            assert debug.consume_spatial_completion(completed, 796) is None
            result = debug.navigation_step(completed, 797)
            assert result['completion'] == 'trigger_completed'
            assert result['trigger_evidence']['trigger_generation'] == generation
            assert result['trigger_evidence']['object_changes']['trigger_mode'] == {'from': 7, 'to': 0}
            assert 'x' not in result['trigger_evidence']['object_changes']
            assert debug.consume_spatial_completion(completed, 797) is None
        assert debug.spatial_execution is None


def test_completion_rejects_stale_foreign_background_cancelled_and_loaded(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[make_object(797, x=32, y=16)], frame=10)
    state['trigger_observation'] = {'generation': 30, 'event_object_id': 797}
    write_state(debug, state)
    with debug.spatial_execution_scope('navigate', 797) as execution:
        pending = {'execution_id': execution['id'], 'owner': 'navigation',
                   'from_room': {'id': execution['room_id']}, 'object_id': 797, 'trigger_generation': 31}
        completed = copy.deepcopy(state)
        completed['trigger_observation'] = {'generation': 31, 'event_object_id': 797, 'completed': True}
        for change in ({'execution_id': -1}, {'owner': 'background'}, {'object_id': 796},
                       {'trigger_generation': 30}, {'from_room': {'id': 'other'}}):
            debug.capture_spatial_completion({**pending, **change}, completed)
            assert execution['receipt'] is None
        wrong_instance = {**completed, 'game_instance_id': 'new'}
        debug.capture_spatial_completion(pending, wrong_instance)
        assert execution['receipt'] is None
        execution['cancel_event'] = threading.Event()
        execution['cancel_event'].set()
        debug.capture_spatial_completion(pending, completed)
        assert execution['receipt'] is None
        execution['cancel_event'].clear()
        debug.capture_spatial_completion(pending, completed)
        assert execution['receipt']
        debug.cancel_runtime_observations('checkpoint_restored')
        assert debug.consume_spatial_completion(completed, 797) is None


def test_motion_association_keeps_confirmed_evidence_without_future_claim(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('palpal.spatial.time.monotonic', lambda: clock[0])
    history = MotionHistory()
    state = make_state(objects=[make_object(7, x=0, y=0)])
    history.observe(state)
    cursor = history.serial
    state['objects'][0]['x'] = 16
    clock[0] = .1
    history.observe(state)
    clock[0] = .6
    history.observe(state)
    evidence = {'object_id': 7, 'trigger_generation': 31, 'source': 'native_trigger_completion'}
    summary = history.associate(state, 7, 'navigate', evidence, cursor)
    assert summary['association'] == 'native_trigger_confirmed'
    assert summary['moved'] and summary['current_status'] == 'stationary'
    assert history.view(state, 7)['latest_action_association'] == summary
    assert AgentRuntime._compact_action_result({'motion_association': summary})['motion_association'] == summary
    background = history.associate(state, 7, 'press_buttons', None, cursor)
    assert background['association'] == 'observed_during_action'


def test_coordinate_boundary_requires_native_source_and_completion(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[make_object(7, x=96, y=0, trigger_mode=7)], frame=10)
    write_state(debug, state)
    with debug.spatial_execution_scope('move_to') as execution:
        source = {'x': 64, 'y': 16, 'scene_id': state['scene_id'], 'map_id': state['map_id']}
        import time
        execution['armed_step'] = {'ids': [7], **source, 'sent_at': time.time()}
        assert debug.native_transition_observation_owner(state, 7, source)[0] == 'navigation'
        assert debug.native_transition_observation_owner(state, 8, source)[0] == 'background'
        assert debug.native_transition_observation_owner(state, 7, {**source, 'x': 80})[0] == 'background'
        assert debug.consume_spatial_completion(state) is None
        pending = {'execution_id': execution['id'], 'owner': 'navigation', 'object_id': 7,
                   'from_room': {'id': execution['room_id']}, 'trigger_generation': 1}
        state['trigger_observation'] = {'generation': 1, 'event_object_id': 7, 'completed': False}
        debug.capture_spatial_completion(pending, state)
        assert debug.consume_spatial_completion(state) is None
        state['trigger_observation']['completed'] = True
        debug.capture_spatial_completion(pending, state)
        assert debug.consume_spatial_completion(state)['trigger_evidence']['action'] == 'move_to'


def test_motion_wait_does_not_infer_stationary_from_stale_snapshot(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('palpal.spatial.time.monotonic', lambda: clock[0])
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[make_object(7, x=0, y=0)])
    debug.motion_history.observe(state)
    debug.navigation_snapshot = lambda: state
    def update(timeout):
        clock[0] += .1
    debug.wait_navigation_update = update
    debug.agent_runtime._observe_target_motion(state, 7, timeout=1.5)
    assert clock[0] >= 1.5
    assert debug.motion_history.view(state, 7)['current_status'] == 'unknown'


def test_passive_observation_tracks_intermediate_motion_and_triggers(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    clock = [0.0]
    monkeypatch.setattr('palpal.spatial.time.monotonic', lambda: clock[0])
    state = make_state(objects=[make_object(7, x=32, y=16)])
    debug.motion_history.observe(state)
    debug.navigation_snapshot = lambda: copy.deepcopy(state)
    def update(timeout):
        clock[0] += .1
        state['frame'] += 1
        state['objects'][0]['x'] = 48 if clock[0] < .3 else 32
        state['trigger_observation'] = {'generation': 1, 'event_object_id': 8, 'completed': True}
        debug.motion_history.observe(state)
    debug.wait_navigation_update = update
    result = debug.run_agent_tool('observe_target', {'id': 7, 'seconds': .5}, state)
    assert result['ok']
    observation = result['observation']
    assert observation['moved'] and observation['target_start'] == observation['target_end']
    assert observation['buttons_sent'] == 0 and not observation['player_moved']
    assert observation['trigger_events'][0]['event_object_id'] == 8
    assert observation['movement_rule'] == 'not_inferred'
    assert not debug.action_path.exists()
    view = debug.motion_history.view(state, 7)
    assert view['observation_windows'][-1] == observation
    assert view['movement_rule'] == 'not_inferred'


def test_passive_observation_interruptions_and_invalid_input(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    clock = [0.0]
    monkeypatch.setattr('palpal.spatial.time.monotonic', lambda: clock[0])
    state = make_state(objects=[make_object(7, x=32, y=16)])
    debug.navigation_snapshot = lambda: copy.deepcopy(state)
    for seconds in (0, 6, True, 'bad', float('nan')):
        assert not debug.observe_target(7, seconds)['ok']
    assert not debug.observe_target(0, 1)['ok']
    debug.motion_history.observe(state)
    def update(timeout):
        clock[0] += .1
        state['party_position']['x'] += 16
        debug.motion_history.observe(state)
    debug.wait_navigation_update = update
    result = debug.observe_target(7, .5)
    assert result['observation']['status'] == 'player_moved'
    debug.agent_runtime.cancel_event.set()
    assert debug.observe_target(7, .5)['observation']['status'] == 'cancelled'
    debug.agent_runtime.cancel_event.clear()
    clock[0] += 2
    assert debug.observe_target(7, .5)['observation']['status'] == 'observation_unavailable'
    assert not debug.action_path.exists()


def test_inspection_uses_latest_position_and_bounds_evidence_history(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[make_object(7, x=32, y=16)], frame=10)
    fresh = copy.deepcopy(state)
    fresh['objects'][0]['x'] = 64
    fresh['frame'] = 11
    write_state(debug, fresh)
    result = debug.run_agent_tool('inspect_target', {'id': 7}, state)
    assert result['current']['x'] == 64
    for n in range(25):
        debug.motion_history.record_window(fresh, 7, {'sample': n})
    windows = debug.motion_history.view(fresh, 7)['observation_windows']
    assert len(windows) == 16 and windows[-1]['sample'] == 24
    debug.motion_history.clear()
    assert debug.motion_history.view(fresh, 7)['current_status'] == 'unknown'



def test_native_completion_is_not_discarded_by_outer_geometry_validation(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[make_object(7, x=32, y=16)], frame=10)
    moved = copy.deepcopy(state)
    moved['objects'][0]['x'] = 64
    write_state(debug, moved)
    calls = []
    receipt = {'ok': True, 'status': 'arrived', 'completion': 'trigger_completed',
               'trigger_evidence': {'object_id': 7, 'trigger_generation': 2}}
    def step(*args):
        calls.append(args)
        return receipt if len(calls) == 1 else {'ok': False, 'status': 'unreachable_static'}
    debug._navigation_step = step
    assert debug.navigation_step(state, 7) == receipt
    assert len(calls) == 1
    assert not debug.action_path.exists()


def test_passive_window_cannot_bridge_stream_gap(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    clock = [0.0]
    monkeypatch.setattr('palpal.spatial.time.monotonic', lambda: clock[0])
    state = make_state(objects=[make_object(7, x=32, y=16)])
    debug.motion_history.observe(state)
    debug.navigation_snapshot = lambda: copy.deepcopy(state)
    def update(timeout):
        clock[0] += 1.1
        debug.motion_history.observe(state)
    debug.wait_navigation_update = update
    result = debug.observe_target(7, 2)
    assert not result['ok']
    assert result['observation']['status'] == 'observation_interrupted'
