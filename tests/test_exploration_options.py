import copy
import json
from pathlib import Path

from palpal.agent_runtime import AgentRuntime
from palpal.debug_server import decode_nav_grid, node_key, world_pos
from tests.support import create_agent_task, make_debug_state, make_object, make_state, make_touch_object, write_state


def corridor():
    # Synthetic geometry: first encounter separates the player from an exit.
    return make_state(width=3, height=3, start=(0, 2), objects=[
        make_touch_object(1713, x=16, y=8, sprite_id=21),
        make_touch_object(1712, x=32, y=0, sprite_id=0),
    ])


def inspect(debug, state, ident=1712):
    write_state(debug, state)
    return debug.run_agent_tool('inspect_target', {'id': ident}, state)['route']


def test_blocked_exit_evidence_survives_model_projection_and_refresh(tmp_path):
    fixture = json.loads((Path(__file__).parent / 'fixtures/blocked_exit_decisions.json').read_text())
    debug = make_debug_state(tmp_path)
    state = corridor()
    data = debug.read_exploration()
    room_id = debug.room_identity(state)['id']
    data['battle_reappearances'] = {room_id: {'1713': {'60': {
        'status': 'confirmed', 'battle_count': 1, 'reappearance_count': 1,
    }}}}
    debug.write_exploration(data)
    route = inspect(debug, state, fixture['exit_id'])
    assert route['status'] == 'blocked_by_transition'
    evidence = route['diagnostic']
    first = evidence['first_triggers'][0]
    assert first['id'] == fixture['encounter_id']
    assert first['executable']
    assert first['interaction_state'] == 'reappeared_battle_trigger'
    assert first['agent_priority'] == 'avoid_repeat'
    assert evidence['guarantees_passage'] is False
    runtime = object.__new__(AgentRuntime)
    payload = {'ok': True, 'route': route}
    projected = runtime._model_tool_result('inspect_target', payload)
    assert projected['route']['diagnostic'] == evidence
    task = create_agent_task(debug, {
        'description': 'Investigate the untraversed exit',
        'rationale': fixture['objective'], 'object_id': fixture['exit_id'],
    }, state)['task']
    after = copy.deepcopy(state)
    after['frame'] += 1
    after['objects'][0].update(state=0, interactable=False)
    refreshed = inspect(debug, after)
    assert refreshed['status'] == 'transition'
    assert 'first_triggers' not in refreshed['diagnostic']
    assert refreshed['diagnostic']['observation']['frame'] != evidence['observation']['frame']
    tasks = debug.run_agent_tool('get_tasks', {'detail': 'full'}, after)
    projected_tasks = runtime._model_tool_result('get_tasks', tasks)
    retained = next(t for t in projected_tasks['tasks'] if t['id'] == task['id'])
    assert retained['rationale'] == fixture['objective']
    assert retained['object_id'] == fixture['exit_id']
    # Preserve actual turn evidence through the model's dialog projection;
    # the old pursuit instruction must not appear newer than the NPC retry.
    debug.dialog_history = [
        {'agent_turn': entry['turn'], 'time': float(entry['turn']),
         'event_object_id': entry.get('object_id'), 'text': entry['summary'],
         'scene_id': state['scene_id'], 'map_id': state['map_id']}
        for entry in fixture['chronology'] if entry['kind'] == 'dialog'
    ]
    observations = debug.run_agent_tool('get_observations', {'kind': 'dialogs'}, after)
    projected_dialogs = debug.project_agent_observations(observations)
    field = projected_dialogs['dialog_fields'].index('agent_turn')
    assert [row[field] for row in projected_dialogs['dialogs']] == [1678, 1693, 1749]


def test_diagnostic_stops_at_transition_and_reports_overlapping_triggers(tmp_path):
    debug = make_debug_state(tmp_path)
    state = corridor()
    state['objects'][0]['sprite_id'] = 0
    state['objects'].append(make_touch_object(1714, x=16, y=8, sprite_id=21))
    destination = make_state()
    destination['scene_id'] = 4
    destination['room']['key'] = 'other-room'
    debug.record_transition_destination(
        debug.room_identity(state), 1713, debug.room_identity(destination),
        from_object=state['objects'][0], from_state=state, to_state=destination)
    route = inspect(debug, state)
    evidence = route['diagnostic']
    assert [t['id'] for t in evidence['first_triggers']] == [1713, 1714]
    assert evidence['first_triggers'][0]['transition_candidate']
    assert evidence['first_triggers'][0]['known_destination']
    assert evidence['stops_at_first_trigger']
    assert evidence['path_to_first_trigger'][-1] == {'x': 16, 'y': 8}
    assert {'x': 32, 'y': 0} not in evidence['path_to_first_trigger']


def test_touch_occupant_is_explained_without_making_exit_executable(tmp_path):
    debug = make_debug_state(tmp_path)
    state = corridor()
    state['objects'][0]['blocking'] = True
    before = debug.navigation(state)
    route = inspect(debug, state)
    assert route['diagnostic']['first_triggers'][0]['id'] == 1713
    assert route['status'] in {'blocked_by_transition', 'unreachable_static'}
    assert debug.navigation(state)['targets'] == before['targets']


def test_static_wall_is_not_invented_as_an_encounter(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(width=3, height=3, start=(0, 2), blocked=[(1, 1)],
                       objects=[make_touch_object(1712, x=32, y=0, sprite_id=0)])
    evidence = inspect(debug, state)['diagnostic']
    assert evidence['reason'] == 'no_candidate_with_touch_boundaries_relaxed'
    assert 'first_triggers' not in evidence


def test_safe_detour_does_not_recommend_encounter(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(width=9, height=9, start=(0, 4), objects=[
        make_touch_object(1713, x=64, y=32), make_touch_object(1712, x=128, y=32, sprite_id=0)])
    route = inspect(debug, state)
    assert route['status'] == 'transition'
    assert 'first_triggers' not in route['diagnostic']


def test_options_are_read_only_safe_unvisited_and_deterministic(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(width=9, height=9, start=(4, 4), objects=[
        make_object(1, x=48, y=24, blocking=True), make_touch_object(2, x=80, y=40)])
    before = copy.deepcopy(debug.read_exploration())
    result = debug.run_agent_tool('get_exploration_options', {'limit': 2}, state)
    assert result['status'] == 'ok'
    assert 0 < len(result['options']) <= 2
    assert result == debug.run_agent_tool('get_exploration_options', {'limit': 2}, state)
    assert debug.read_exploration() == before
    for option in result['options']:
        assert (option['x'], option['y']) != (64, 32)
        route = debug.route_to_position(state, option['x'], option['y'])
        assert route['ok'] and route['steps'] == option['steps']
        assert not any(step['trigger_ids'] for step in route['path'])
    option = result['options'][0]
    changed = copy.deepcopy(state)
    changed['objects'].append(make_object(3, x=option['x'], y=option['y'], blocking=True))
    write_state(debug, changed)
    debug.write_action = lambda *args: (_ for _ in ()).throw(AssertionError('stale action'))
    assert debug.move_to_step(state, {'x': option['x'], 'y': option['y']})['status'] == 'state_changed'
    assert debug.route_to_position(changed, option['x'], option['y'])['status'] == 'position_blocked'


def test_no_safe_frontier_does_not_assert_completion(tmp_path):
    debug = make_debug_state(tmp_path)
    state = corridor()
    result = debug.run_agent_tool('get_exploration_options', {}, state)
    assert result['options'] == []
    assert result['status'] == 'no_safe_frontier'
    assert 'does not establish room completion' in result['meaning']
    state['in_battle'] = True
    assert debug.run_agent_tool('get_exploration_options', {}, state)['status'] == 'waiting_for_control'


def test_visited_frontier_and_policy_preserve_route_investigation(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(width=7, height=7, start=(2, 2))
    grid = decode_nav_grid(state)
    data = debug.read_exploration()
    room = debug.exploration_room(data, debug.room_identity(state))
    room['visited'] = [node_key((2, 2)), node_key((3, 3))]
    debug.write_exploration(data)
    result = debug.run_agent_tool('get_exploration_options', {}, state)
    assert result['options']
    assert all((o['x'], o['y']) != world_pos(grid, (3, 3)) for o in result['options'])
    policy = debug.agent_policy_prompt()
    for rule in ('重新评估原出口', '不证明唯一通路或战后必通', 'invalid_position 只否定该坐标',
                 '晚于上次交互的相关证据', '没有 fresh 交互对象不等于区域已探索完'):
        assert rule in policy
