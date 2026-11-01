import copy

import pytest

from palpal.spatial import recovery_options
from tests.support import make_debug_state, make_state, make_object, make_touch_object, write_state


def blocked_start():
    # Same local geometry as the recorded (1632,704) landing; down escapes.
    return make_state(width=7, height=7, start=(2, 2),
                      blocked=[(2, 2), (1, 1), (3, 3)] +
                      [(x, y) for x in range(7) for y in range(7) if (x + y) % 2])


def test_queries_and_move_share_safe_native_steps(tmp_path):
    debug = make_debug_state(tmp_path)
    state = blocked_start()
    before = copy.deepcopy(state)
    options = recovery_options(debug, state)
    assert [(o['move'], o['x'], o['y']) for o in options] == [('up', 48, 8), ('down', 16, 24)]
    nav = debug.run_agent_tool('get_navigation', {}, state)['navigation']
    assert nav['status'] == 'bad_start'
    assert nav['recovery_options'] == options
    exploration = debug.run_agent_tool('get_exploration_options', {'limit': 1}, state)
    assert exploration['options'] == options[:1]
    for o in options:
        route = debug.route_to_position(state, o['x'], o['y'])
        assert route['recovery'] and route['steps'] == 1
        assert route['path'] == [{'move': o['move'], 'x': o['x'], 'y': o['y'], 'trigger_ids': []}]
    assert debug.route_to_position(state, 32, 32)['status'] == 'bad_start'
    assert debug.route_to_position(state, 16, 24, 'right')['status'] == 'bad_start'
    assert state['party_position'] == before['party_position']
    assert not debug.action_path.exists()


@pytest.mark.parametrize('barrier', ['wall', 'touch', 'blocker'])
def test_recovery_prefers_open_area_over_pocket_and_respects_boundaries(tmp_path, barrier):
    debug = make_debug_state(tmp_path)
    pocket = {(5, 3), (6, 2), (7, 1), (8, 2), (7, 3)}
    open_area = {(3, 5), (2, 6), (1, 7), (2, 8), (3, 7),
                 (4, 6), (5, 7), (4, 8), (6, 6), (7, 5)}
    walkable = pocket | open_area
    objects = [make_object(10, x=112, y=8), make_object(20, x=32, y=64)]
    if barrier != 'wall':
        walkable.add((6, 4))
        objects.append(make_touch_object(30, x=96, y=32) if barrier == 'touch' else
                       make_object(30, x=96, y=32, blocking=True, interactable=False))
    state = make_state(width=9, height=9, start=(4, 4), objects=objects,
                       blocked=[(x, y) for x in range(9) for y in range(9)
                                if (x, y) not in walkable])
    before = copy.deepcopy(state)
    options = recovery_options(debug, state)
    assert state == before
    assert [option['move'] for option in options] == ['down', 'up']
    assert [option['safe_reachable_cells'] for option in options] == [len(open_area), len(pocket)]
    assert [option['recommended'] for option in options] == [True, False]
    assert 20 in options[0]['reachable_target_ids']
    assert 10 not in options[0]['reachable_target_ids']
    assert 10 in options[1]['reachable_target_ids']
    assert 20 not in options[1]['reachable_target_ids']
    # A limited exploration query must retain the ranking, not native key order.
    assert debug.run_agent_tool('get_exploration_options', {'limit': 1}, state)['options'] == options[:1]
    assert debug.navigation(state)['recovery_options'] == options
    # Ranking does not invalidate an explicitly chosen safe alternative.
    for option in options:
        assert debug.route_to_position(state, option['x'], option['y'])['recovery']
    assert state['party_position'] == before['party_position']
    assert not debug.action_path.exists()


def test_recovery_counts_shared_component_once_and_keeps_ties_stable(tmp_path):
    debug = make_debug_state(tmp_path)
    options = recovery_options(debug, blocked_start())
    assert len(options) == 2
    assert options[0]['safe_reachable_cells'] == options[1]['safe_reachable_cells']
    assert [option['move'] for option in options] == ['up', 'down']
    assert [option['recommended'] for option in options] == [True, False]


@pytest.mark.parametrize('change', ['blocker', 'touch', 'unaligned', 'outside', 'no_grid', 'battle', 'entering'])
def test_unsafe_or_unavailable_recovery_is_not_offered(tmp_path, change):
    debug = make_debug_state(tmp_path)
    state = blocked_start()
    if change == 'blocker':
        state['objects'] = [make_object(i, x=x, y=y, blocking=True) for i, x, y in [(1,48,8),(2,16,24)]]
    elif change == 'touch':
        state['objects'] = [make_touch_object(i, x=x, y=y) for i, x, y in [(1,48,8),(2,16,24)]]
    elif change == 'unaligned':
        state['party_position']['x'] += 1
    elif change == 'outside':
        state['party_position']['x'] = -16
    elif change == 'no_grid':
        state.pop('nav_grid')
    elif change == 'battle':
        state['in_battle'] = True
    else:
        state['entering_scene'] = True
    assert recovery_options(debug, state) == []


def test_execution_revalidates_candidate_and_writes_only_one_step(tmp_path):
    debug = make_debug_state(tmp_path)
    state = blocked_start()
    write_state(debug, state)
    writes = []
    debug.write_action = lambda buttons, frames: writes.append((buttons, frames)) or {'generation': 1}
    result = debug.move_to_step(state, {'x':16, 'y':24})
    assert result['recovery']
    assert writes == [(['down'], 1)]
    changed = copy.deepcopy(state)
    changed['objects'] = [make_object(1, x=16, y=24, blocking=True)]
    write_state(debug, changed)
    assert debug.move_to_step(state, {'x':16, 'y':24})['status'] == 'state_changed'
    assert not debug.move_to_step(changed, {'x':16, 'y':24})['ok']
    assert len(writes) == 1


@pytest.mark.parametrize('outcome', ['recovered', 'no_progress', 'dialog', 'room', 'battle', 'entering'])
def test_recovery_runtime_always_stops_after_one_receipt(tmp_path, outcome):
    debug = make_debug_state(tmp_path)
    state = blocked_start()
    after = copy.deepcopy(state)
    if outcome != 'no_progress':
        after['party_position'] = {'x':16, 'y':24}
    if outcome == 'dialog':
        after['dialog'] = {'active': True}
    if outcome == 'battle':
        after['in_battle'] = True
    if outcome == 'entering':
        after['entering_scene'] = True
    if outcome == 'room':
        after['scene_id'] = 4
        after['room']['key'] = 'different'
    debug.navigation_snapshot = lambda: state
    write_state(debug, state)
    writes = []
    debug.write_action = lambda buttons, frames: writes.append((buttons, frames)) or {'generation':1}
    debug.agent_runtime._wait_action_completion = lambda action: after
    result = debug.agent_runtime._move_to({'x':16, 'y':24})
    assert result['status'] == (outcome if outcome in ('recovered', 'no_progress') else 'recovery_interrupted')
    assert result['refresh_navigation']
    assert writes == [(['down'], 1)]
