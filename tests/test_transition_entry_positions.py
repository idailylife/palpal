import json

import pytest

from tests.support import make_debug_state, make_state, write_state
from tests.test_agent_rules import entry_room_state, install_entry_context


@pytest.mark.parametrize('direction', ['incoming', 'outgoing'])
def test_room_memory_keeps_small_historical_positions(tmp_path, direction):
    debug = make_debug_state(tmp_path)
    source = make_state(objects=[{
        'id': 1865, 'x': 1456, 'y': 456, 'trigger_mode': 5,
        'trigger_script': 17543, 'state': 1, 'interactable': True,
    }])
    destination = make_state()
    destination['map_id'] = 81
    record = debug.record_transition_destination(
        debug.room_identity(source), 1865, debug.room_identity(destination),
        from_object=source['objects'][0], from_state=source, to_state=destination,
        confirmation='native_trigger',
        native_destination={'x': 1392, 'y': 1768, 'frame': 8541, 'map_id': 81},
    )
    room = source if direction == 'outgoing' else destination
    result = debug.run_agent_tool('get_room_memory', {'room_id': debug.room_identity(room)['id']}, room)
    edge = result['transitions'][direction][0]
    assert json.loads(edge['transition_key'])[2:4] == [1456, 456]
    assert 'source_position' not in edge
    assert edge['landing_position'] == {'x': 1392, 'y': 1768}
    assert edge['confirmation'] == 'native_trigger'
    assert 'native_destination' not in edge
    positions = {'landing_position': edge['landing_position']}
    assert len(json.dumps(positions, separators=(',', ':'))) < 50

    # Volatile fields must not inflate or churn the model-visible history.
    before = debug.compact_agent_room_transition(record, direction=direction, data=debug.read_exploration())
    record['native_destination']['frame'] += 1000
    after = debug.compact_agent_room_transition(record, direction=direction, data=debug.read_exploration())
    assert before == after


@pytest.mark.parametrize('landing', [None, {}, {'x': True, 'y': 1}])
def test_missing_or_invalid_historical_landing_is_not_invented(tmp_path, landing):
    debug = make_debug_state(tmp_path)
    edge = debug.compact_agent_room_transition(
        {'native_destination': landing}, direction='incoming', data=debug.read_exploration(),
    )
    assert 'landing_position' not in edge


@pytest.mark.parametrize('near_entry', [True, False])
def test_same_room_destination_requires_original_entry_proximity(tmp_path, near_entry):
    debug = make_debug_state(tmp_path)
    state = entry_room_state()
    state['nav_grid'] = make_state(width=20, height=5)['nav_grid']
    context = install_entry_context(debug, state)
    if not near_entry:
        state['objects'][0]['x'] = 160
    # Moving the party must not redefine the original entrance.
    state['party_position'] = {'x': 160, 'y': 16}
    targets = [{
        'id': 758, 'transition_candidate': True, 'known_destination': True,
        'known_destination_room_id': context['source_room_id'],
        'agent_priority': 'avoid_backtrack', 'deprioritize_reason': 'old room',
    }]
    decorated, _ = debug.decorate_room_entry_navigation(state, targets)
    target = decorated[0]
    if near_entry:
        assert target['entry_relation'] == 'confirmed_entry_return'
        assert target['possible_entry_return'] is True
        assert target['agent_priority'] == 'avoid_immediate_backtrack'
    else:
        assert target['entry_relation'] == 'same_room_return'
        assert 'possible_entry_return' not in target
        assert target['agent_priority'] == 'normal'
        assert 'deprioritize_reason' not in target


def test_other_exit_to_same_room_remains_executable(tmp_path):
    debug = make_debug_state(tmp_path)
    state = entry_room_state()
    state['nav_grid'] = make_state(width=20, height=5)['nav_grid']
    context = install_entry_context(debug, state)
    state['objects'][0].update(x=160, y=16)
    source = make_state()
    source['map_id'] = 40
    debug.record_transition_destination(
        debug.room_identity(state), 758, debug.room_identity(source),
        from_object=state['objects'][0], from_state=state, to_state=source,
        native_destination={'x': 1392, 'y': 1768},
    )
    write_state(debug, state)
    navigation = debug.run_agent_tool('get_navigation', {}, state)['navigation']
    target = next(item for item in navigation['targets'] if item['id'] == 758)
    assert target['known_destination_room_id'] == context['source_room_id']
    assert target['entry_relation'] == 'same_room_return'
    assert 'possible_entry_return' not in target
    decision = debug.run_agent_tool('submit_decision', {
        'action': 'navigate', 'target_id': 758, 'reason': 'Enter the other side of the known room.',
    }, state)['decision']
    assert decision['recommended_action']['tool'] == 'navigation_step'
    assert decision.get('validation', {}).get('status') != 'rejected'
    assert not debug.action_path.exists()


def test_unknown_landing_does_not_confirm_original_entrance(tmp_path):
    debug = make_debug_state(tmp_path)
    state = entry_room_state()
    context = install_entry_context(debug, state)
    context.pop('landing_position')
    decorated, _ = debug.decorate_room_entry_navigation(state, [{
        'id': 758, 'transition_candidate': True, 'known_destination': True,
        'known_destination_room_id': context['source_room_id'],
    }])
    assert decorated[0]['entry_relation'] == 'same_room_return'
    assert 'possible_entry_return' not in decorated[0]
