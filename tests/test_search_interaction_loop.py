import copy

import pytest

from palpal.debug_server import decode_nav_grid, first_search_object, search_hits, world_pos
from tests.support import make_debug_state, make_state
from tests.test_agent_rules import entry_room_state, install_entry_candidate, install_entry_context


@pytest.mark.parametrize('generation', [None, 12])
def test_repeated_search_dialog_does_not_change_narrative_context(tmp_path, generation):
    debug = make_debug_state(tmp_path)
    state = make_state()
    event = {'sequence': 35, 'frame': 10, 'message_id': 4460,
             'event_object_id': 878, 'text': '一付釣具'}
    if generation is not None:
        event['trigger_generation'] = generation
    state['dialog'] = {'events': [event]}
    debug.record_dialog_event(state)
    digest = debug.narrative_context_digest()
    for sequence in range(36, 40):
        event = {**event, 'sequence': sequence, 'frame': sequence}
        if generation is not None:
            event['trigger_generation'] += 1
        state['dialog'] = {'events': [event]}
        debug.record_dialog_event(state)
        assert debug.narrative_context_digest() == digest
    assert [entry['text'] for entry in debug.dialog_history] == ['一付釣具'] * 5
    state['dialog'] = {'events': [{**event, 'sequence': 40, 'message_id': 4461, 'text': '新线索'}]}
    debug.record_dialog_event(state)
    assert debug.narrative_context_digest() != digest


def test_distinct_trigger_generations_split_even_adjacent_message_ids(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    state['dialog'] = {'events': [
        {'sequence': 1, 'frame': 10, 'message_id': 100, 'event_object_id': 877,
         'trigger_generation': 1, 'text': '第一行'},
        {'sequence': 2, 'frame': 10, 'message_id': 101, 'event_object_id': 877,
         'trigger_generation': 1, 'text': '第二行'},
        {'sequence': 3, 'frame': 20, 'message_id': 102, 'event_object_id': 877,
         'trigger_generation': 2, 'text': '另一次交互'},
    ]}
    debug.record_dialog_event(state)
    assert [entry['text'] for entry in debug.dialog_history] == ['第一行第二行', '另一次交互']


def test_progressed_entry_allows_return_despite_fresh_alternative(tmp_path):
    debug = make_debug_state(tmp_path)
    state = entry_room_state()
    context = install_entry_context(debug, state)
    install_entry_candidate(debug, state, context)
    assert debug.room_entry_navigation_rejection(state, 758)['reason'] == 'immediate_entry_return'
    context['status'] = 'progressed'
    assert debug.room_entry_navigation_rejection(state, 758) is None


def test_soft_avoided_targets_do_not_block_entry_return(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = entry_room_state()
    context = install_entry_context(debug, state)
    install_entry_candidate(debug, state, context)
    navigation = debug.navigation(state)
    for target in navigation['targets']:
        if target['id'] != 758:
            target['agent_priority'] = 'soft_avoid'
    monkeypatch.setattr(debug, 'navigation', lambda state: navigation)
    assert debug.room_entry_navigation_rejection(state, 758) is None


def search_objects():
    return [
        {'id': 877, 'x': 32, 'y': 16, 'state': 2, 'trigger_mode': 2,
         'trigger_script': 14230, 'interactable': True, 'blocking': True},
        {'id': 878, 'x': 48, 'y': 24, 'state': 2, 'trigger_mode': 1,
         'trigger_script': 15262, 'interactable': True, 'blocking': True},
    ]


def test_search_planning_avoids_nearer_object_intercept(tmp_path):
    debug = make_debug_state(tmp_path)
    objects = search_objects()
    state = make_state(width=9, height=9, start=(3, 5), objects=objects)
    position = (48, 40)
    assert search_hits(position, 1, objects[0])
    assert first_search_object(position, 1, objects) == 878
    grid = decode_nav_grid(state)
    goals, directions = debug.goal_nodes(state, grid, objects[0])
    assert goals
    for node in goals:
        assert first_search_object(world_pos(grid, node), directions[node], objects) == 877
    route = debug.route_to_object(state, 877)
    assert route['status'] == 'reachable'
    assert route['route']['steps'] > 0


def test_search_order_uses_samples_before_ids_and_ignores_inactive_objects():
    objects = search_objects()
    assert first_search_object((48, 40), 1, list(reversed(objects))) == 878
    objects[1]['state'] = 0
    assert first_search_object((48, 40), 1, objects) == 877


def test_native_mismatch_does_not_persist_requested_target_failure(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=search_objects(), frame=10)
    state['game_instance_id'] = 'test-instance'
    observation_id = debug.mark_pending_interaction_observation(state, 877, action={'buttons': ['search']})
    after = copy.deepcopy(state)
    after['frame'] = 20
    after['trigger_observation'] = {
        'generation': 1, 'event_object_id': 878, 'trigger_script': 15262, 'completed': True,
        'source': {'scene_id': state['scene_id'], 'map_id': state['map_id'], **state['party_position']},
    }
    result = debug.observe_pending_interaction_outcome(after)
    assert result['requested_object_id'] == 877
    assert result['actual_object_id'] == 878
    assert debug.observations.get(observation_id)['result']['type'] == 'interaction_target_mismatch'
    assert debug.pending_interaction_observation is None
    assert not debug.read_exploration()['object_interactions']
