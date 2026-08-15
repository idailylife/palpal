import json
import threading
import time

from tests.support import create_agent_task, make_debug_state, make_state, make_touch_object, write_state
from palpal.debug_server import decode_nav_grid, nav_bit, pal_distance, trigger_radius, world_pos


def approach_item(item_id=279, object_id=238, distance_level=1):
    return {
        'item_id': item_id,
        'name': '破天鎚',
        'amount': 1,
        'amount_in_use': 0,
        'usable': True,
        'use_effect': {
            'script_entry': 39645,
            'preview_complete': True,
            'requirements_complete': True,
            'effects': [],
            'requirements': [{
                'kind': 'face_event_object',
                'script_entry': 39645,
                'conditional': False,
                'event_object_id': object_id,
                'distance_level': distance_level,
                'distance_limit': distance_level * 32 + 16,
                'failure_script_entry': 39647,
            }],
            'unparsed_effects': [],
        },
    }


def approach_object(object_id=238, x=32, y=16):
    return {
        'id': object_id,
        'x': x,
        'y': y,
        'sprite_id': 66,
        'state': 2,
        'trigger_mode': 0,
        'trigger_script': 9433,
        'auto_script': 0,
        'blocking': True,
        'interactable': True,
    }


def test_compact_navigation_projects_interaction_outcomes_without_duplicate_evidence(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    outcome = {
        'type': 'interaction_effect',
        'time': 0,
        'reason': 'native completion',
        'action': {'buttons': ['search'], 'frames': 1},
        'elapsed': 0.7,
        'frame': 100,
        'logical_room_id': 'S1/M2:0123456789AB',
        'event_key': 'digest',
        'started_object': {
            'state': 1, 'trigger_mode': 1, 'trigger_script': 100,
            'auto_script': 200, 'vanish_time': 10, 'interactable': True,
        },
        'finished_object': {
            'state': 1, 'trigger_mode': 4, 'trigger_script': 101,
            'auto_script': 201, 'vanish_time': 9, 'interactable': True,
        },
        'inventory_changes': [{'item_id': 7, 'delta': 1, 'amount': 2, 'name': 'item'}],
        'room_changed': False,
        'interaction_evidence': {'same_context_no_effect_count': 9},
    }
    top_evidence = {'same_context_no_effect_count': 1, 'context_changes_since_latest': []}
    debug.navigation = lambda _state: {
        'status': 'ready',
        'targets': [{
            'id': 75,
            'kind': 'search',
            'status': 'transition',
            'steps': 1,
            'transition_candidate': True,
            'previous_interaction_outcome': outcome,
            'interaction_evidence': top_evidence,
        }],
    }

    target = debug.compact_agent_navigation(state)['targets'][0]

    assert target['transition_candidate'] is True
    assert target['interaction_evidence'] == top_evidence
    assert target['previous_interaction_outcome'] == {
        'type': 'interaction_effect',
        'time': 0,
        'reason': 'native completion',
        'object_changes': {
            'trigger_mode': {'from': 1, 'to': 4},
            'trigger_script': {'from': 100, 'to': 101},
        },
        'inventory_changes': [{'item_id': 7, 'delta': 1, 'amount': 2}],
    }
    projected = target['previous_interaction_outcome']
    for forbidden in ('action', 'frame', 'frames', 'logical_room_id', 'event_key', 'started_object', 'finished_object'):
        assert forbidden not in projected
    assert 'interaction_evidence' not in projected


def test_compact_navigation_preserves_no_effect_and_true_room_change_only(tmp_path):
    debug = make_debug_state(tmp_path)
    no_effect = debug.compact_interaction_outcome({
        'type': 'interaction_no_effect',
        'time': 5,
        'reason': 'quiet state',
        'object_changes': {},
        'inventory_changes': [],
        'room_changed': False,
    })
    changed_room = debug.compact_interaction_outcome({
        'type': 'interaction_effect',
        'room_changed': True,
    })
    assert no_effect == {
        'type': 'interaction_no_effect',
        'time': 5,
        'reason': 'quiet state',
    }
    assert changed_room == {'type': 'interaction_effect', 'room_changed': True}


def test_compact_navigation_representative_history_stays_under_ten_kb_and_halves_payload(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    targets = []
    for target_id in range(1, 21):
        history = [
            {
                'type': 'interaction_effect',
                'time': event_id,
                'frame': event_id * 10,
                'action': {'buttons': ['search'], 'frames': 1},
                'logical_room_id': 'S1/M2:0123456789AB',
                'event_key': f'digest-{target_id}-{event_id}',
                'started_object': {'trigger_mode': 1, 'auto_script': event_id, 'vanish_time': 10},
                'finished_object': {'trigger_mode': 4, 'auto_script': event_id + 1, 'vanish_time': 9},
                'metadata': 'x' * 120,
            }
            for event_id in range(7)
        ]
        targets.append({
            'id': target_id,
            'kind': 'search',
            'status': 'reachable',
            'steps': target_id,
            'previous_interaction_outcome': {**history[-1], 'history': history},
            'interaction_evidence': {'same_context_no_effect_count': target_id % 2},
        })
    unprojected = {'status': 'ready', 'targets': targets}
    debug.navigation = lambda _state: unprojected

    compact = debug.compact_agent_navigation(state, limit=20)
    raw_size = len(json.dumps(unprojected, ensure_ascii=False).encode('utf-8'))
    compact_size = len(json.dumps(compact, ensure_ascii=False).encode('utf-8'))

    assert compact_size <= 10_000
    assert compact_size <= raw_size * 0.5
    assert all('history' not in target['previous_interaction_outcome'] for target in compact['targets'])


def test_item_requirement_exposes_reachable_approach_without_interacting(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(width=8, height=8, start=(7, 7), objects=[approach_object()])
    state['inventory'] = [approach_item()]
    write_state(debug, state)

    target = next(item for item in debug.navigation(state)['targets'] if item['id'] == 238)
    assert target['kind'] == 'approach'
    assert target['status'] == 'reachable'
    assert target['steps'] > 0
    assert target['approach']['item_id'] == 279
    assert target['approach']['distance_limit'] == 48
    assert target['affordances'][0]['kind'] == 'item_approach'
    assert target['affordances'][0]['requirements_complete'] is True

    route = debug.route_to_object(state, 238, approach_item_id=279)
    assert route['status'] == 'reachable'
    assert route['action'] == 'approach'
    assert route['required_direction'] in {'up', 'right', 'down', 'left'}
    step = debug.navigation_step(state, 238, approach_item_id=279)
    assert step['status'] == 'stepping'
    assert 'search' not in step['action']['buttons']


def test_approach_faces_then_stops_without_using_item(tmp_path):
    target = approach_object()
    state = make_state(width=6, height=6, start=(4, 4), objects=[target])
    state['inventory'] = [approach_item()]
    state['party_direction'] = 3

    facing_root = tmp_path / 'facing'
    facing_root.mkdir()
    facing_debug = make_debug_state(facing_root)
    write_state(facing_debug, state)
    facing = facing_debug.navigation_step(state, 238, approach_item_id=279)
    assert facing['status'] == 'facing'
    assert facing['action']['buttons'] == ['left']
    assert facing['required_direction'] == 'left'

    approached_root = tmp_path / 'approached'
    approached_root.mkdir()
    approached_debug = make_debug_state(approached_root)
    approached_state = json.loads(json.dumps(state))
    approached_state['party_direction'] = 1
    write_state(approached_debug, approached_state)
    approached = approached_debug.navigation_step(approached_state, 238, approach_item_id=279)
    assert approached['status'] == 'approached'
    assert approached['approach']['item_id'] == 279
    assert not approached_debug.action_path.exists()


def test_search_and_item_approach_affordances_coexist(tmp_path):
    target = approach_object()
    target['trigger_mode'] = 1
    debug = make_debug_state(tmp_path)
    state = make_state(width=6, height=6, start=(4, 4), objects=[target])
    state['party_direction'] = 1
    state['inventory'] = [approach_item()]
    write_state(debug, state)

    summary = next(item for item in debug.navigation(state)['targets'] if item['id'] == 238)
    assert summary['kind'] == 'search'
    assert summary['affordances'][0]['kind'] == 'item_approach'
    assert summary['affordances'][0]['item_id'] == 279

    native = debug.route_to_object(state, 238)
    item_route = debug.route_to_object(state, 238, approach_item_id=279)
    assert native['kind'] == 'search'
    assert item_route['kind'] == 'approach'
    approached = debug.navigation_step(state, 238, approach_item_id=279)
    assert approached['status'] == 'approached'
    assert approached['affordance']['kind'] == 'item_approach'
    assert not debug.action_path.exists()


def test_multiple_item_requirements_for_one_object_are_preserved_and_sorted(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(width=6, height=6, start=(4, 4), objects=[approach_object()])
    state['party_direction'] = 1
    item = approach_item()
    second = json.loads(json.dumps(item['use_effect']['requirements'][0]))
    second['script_entry'] = 39655
    item['use_effect']['requirements'].append(second)
    state['inventory'] = [item, approach_item(item_id=300)]

    target = next(entry for entry in debug.navigation(state)['targets'] if entry['id'] == 238)
    assert [(entry['item_id'], entry['script_entry']) for entry in target['affordances']] == [
        (279, 39645),
        (279, 39655),
        (300, 39645),
    ]


def test_item_effect_observation_uses_object_delta_not_script_success(tmp_path):
    debug = make_debug_state(tmp_path)
    before = make_state(objects=[approach_object()])
    after = json.loads(json.dumps(before))
    after['objects'][0]['state'] = 0
    evidence = debug.item_object_effect_observation(
        before,
        after,
        {'event_object_id': 238},
    )

    assert evidence['matched_event_object_id'] == 238
    assert evidence['effect_observed'] is True
    assert evidence['object_delta']['state'] == {'before': 2, 'after': 0}
    assert debug.item_object_effect_observation(before, after, None)['effect_observed'] is None


def test_approach_requires_matching_available_item_and_safe_route(tmp_path):
    target = approach_object(x=96, y=0)
    debug = make_debug_state(tmp_path)
    state = make_state(width=7, height=5, start=(0, 4), objects=[target], blocked={(3, y) for y in range(5)})
    state['inventory'] = [approach_item()]

    missing = debug.route_to_object(state, 238, approach_item_id=999)
    assert missing['status'] == 'approach_requirement_missing'

    target_summary = next(item for item in debug.navigation(state)['targets'] if item['id'] == 238)
    assert target_summary['kind'] == 'approach'
    assert target_summary['status'] == 'unreachable_static'
    assert target_summary['affordances'][0]['status'] == 'unreachable_static'

    unavailable_state = json.loads(json.dumps(state))
    unavailable_state['inventory'][0]['amount'] = 0
    unavailable = next(item for item in debug.navigation(unavailable_state)['targets'] if item['id'] == 238)
    assert unavailable['kind'] == 'none'
    assert unavailable['status'] == 'unreachable_static'


def test_navigation_environment_key_tracks_approach_requirements(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[approach_object()])
    state['inventory'] = [approach_item()]
    before = debug.navigation(state)
    changed = json.loads(json.dumps(state))
    changed['inventory'][0]['amount'] = 0
    after = debug.navigation(changed)
    assert before['environment_key'] != after['environment_key']



def test_navigation_step_sends_first_move_to_reachable_target(tmp_path):
    target = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 21, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    write_state(debug, state)
    result = debug.navigation_step(state, target_id=48)
    assert result['ok']
    assert result['status'] == 'stepping'
    assert result['action']['buttons'] == ['up']
    assert debug.action_path.exists()



def test_navigation_step_sends_straight_segment_until_turn(tmp_path):
    target = {'id': 48, 'x': 64, 'y': 0, 'sprite_id': 21, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(width=5, height=5, start=(0, 4), objects=[target])
    write_state(debug, state)
    result = debug.navigation_step(state, target_id=48)
    assert result['ok']
    assert result['status'] == 'stepping'
    assert result['action']['buttons'] == ['up', 'up', 'up', 'up']
    assert result['action']['frames'] == 1
    assert result['segment_steps'] == 4
    assert result['segment_moves'] == ['up', 'up', 'up', 'up']
    assert result['expected_node'] == {'x': 64, 'y': 0}
    assert result['remaining_steps'] == 0



def test_navigation_step_sends_straight_transition_segment(tmp_path):
    target = {'id': 52, 'x': 64, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(width=5, height=5, start=(0, 4), objects=[target])
    write_state(debug, state)
    result = debug.navigation_step(state, target_id=52)
    assert result['ok']
    assert result['status'] == 'stepping'
    assert result['action']['buttons'] == ['up', 'up', 'up', 'up']
    assert result['segment_steps'] == 4
    assert result['segment_moves'] == ['up', 'up', 'up', 'up']
    assert result['expected_node'] == {'x': 64, 'y': 0}
    assert result['remaining_steps'] == 0


def test_transition_completion_finalizes_remembered_navigation_route(tmp_path):
    target = {'id': 52, 'x': 64, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(width=5, height=5, start=(0, 4), objects=[target])
    route = debug.route_to_object(state, target_id=52)
    identity = debug.room_identity(state)
    debug.remember_navigation_route(state, 52, route['route'], 1, route['route']['steps'] - 1, 'transition')

    before = debug.read_exploration()['rooms'][identity['id']]['last_route']
    assert before['pending'] is True
    debug.finish_navigation_route(identity, 52, 'transitioned')
    after = debug.read_exploration()['rooms'][identity['id']]['last_route']
    assert after['pending'] is False
    assert after['outcome'] == 'transitioned'
    assert after['completed_at'] >= after['updated_at']


def test_expired_navigation_confirmation_is_not_left_pending(tmp_path):
    target = {'id': 52, 'x': 64, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(width=5, height=5, start=(0, 4), objects=[target])
    route = debug.route_to_object(state, target_id=52)
    identity = debug.room_identity(state)
    debug.remember_navigation_route(state, 52, route['route'], 1, route['route']['steps'] - 1, 'transition')
    data = debug.read_exploration()
    data['rooms'][identity['id']]['last_route']['updated_at'] = time.time() - 10
    debug.write_exploration(data)

    debug.update_exploration(state)
    last_route = debug.read_exploration()['rooms'][identity['id']]['last_route']
    assert last_route['pending'] is False
    assert last_route['outcome'] == 'confirmation_expired'



def test_navigation_step_interacts_with_search_target_at_current_node(tmp_path):
    target = {'id': 63, 'x': 32, 'y': 0, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(start=(2, 0), objects=[target])
    write_state(debug, state)
    result = debug.navigation_step(state, target_id=63)
    assert result['ok']
    assert result['status'] == 'interacting'
    assert result['action']['buttons'] == ['search']
    assert debug.action_path.exists()



def test_navigation_step_stops_when_target_becomes_inactive(tmp_path):
    target = {'id': 63, 'x': 32, 'y': 0, 'state': 0, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': False}
    debug = make_debug_state(tmp_path)
    state = make_state(start=(2, 0), objects=[target])
    write_state(debug, state)
    result = debug.navigation_step(state, target_id=63)
    assert not result['ok']
    assert result['status'] == 'target_inactive'
    assert not debug.action_path.exists()



def test_navigation_step_faces_search_target_when_already_at_goal(tmp_path):
    target = {'id': 10, 'x': 32, 'y': 0, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(start=(1, 1), objects=[target])
    state['party_direction'] = 3
    write_state(debug, state)
    result = debug.navigation_step(state, target_id=10)
    assert result['ok']
    assert result['status'] == 'facing'
    assert result['action']['buttons'] == ['up']
    assert result['required_direction'] == 'up'
    assert debug.action_path.exists()



def test_route_reports_interactable_blocker(tmp_path):
    door = {'id': 74, 'x': 16, 'y': 8, 'state': 2, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': True, 'interactable': True}
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 21, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(width=3, height=3, start=(0, 2), objects=[door, target])
    write_state(debug, state)
    result = debug.route_to_object(state, target_id=52)
    assert result['ok']
    assert result['status'] == 'blocked_by_interactable'
    assert result['blocker']['id'] == 74
    assert result['interaction']['required_direction'] == 'up'
    assert result['interaction']['route']['steps'] == 0
    navigation = debug.navigation(state)
    target_status = {target['id']: target for target in navigation['targets']}
    assert target_status[52]['status'] == 'blocked_by_interactable'
    assert target_status[52]['blocker']['id'] == 74



def test_navigation_response_identifies_environment_without_animation_frame(tmp_path):
    target = make_touch_object(52, x=32, y=0, sprite_id=21)
    target['current_frame'] = 1
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    navigation = debug.navigation(state)
    assert 'environment' in navigation
    assert 'environment_key' in navigation
    animation_state = json.loads(json.dumps(state))
    animation_state['objects'][0]['current_frame'] = 2
    animation_state['objects'][0]['auto_script'] = 9999
    animation_navigation = debug.navigation(animation_state)
    assert navigation['environment_key'] == animation_navigation['environment_key']
    changed_state = json.loads(json.dumps(state))
    changed_state['objects'][0]['state'] = 2
    changed_navigation = debug.navigation(changed_state)
    assert navigation['environment_key'] != changed_navigation['environment_key']


def test_navigation_overview_uses_exactly_three_shared_distance_fields(monkeypatch, tmp_path):
    objects = [
        make_touch_object(40 + index, x=(index % 3) * 32, y=(index // 3) * 16)
        for index in range(6)
    ]
    objects.extend(
        {
            'id': 60 + index,
            'x': (index % 3) * 32,
            'y': (index // 3) * 16,
            'state': 1,
            'trigger_mode': 1 + (index % 3),
            'trigger_script': 100,
            'blocking': False,
            'interactable': True,
        }
        for index in range(6)
    )
    debug = make_debug_state(tmp_path)
    state = make_state(width=7, height=7, start=(3, 3), objects=objects)
    calls = []
    original = debug.navigation_distance_field

    def counted(*args, **kwargs):
        calls.append(set(args[2]))
        return original(*args, **kwargs)

    monkeypatch.setattr(debug, 'navigation_distance_field', counted)
    monkeypatch.setattr(
        debug,
        'route_between_nodes',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('per-target BFS')),
    )

    navigation = debug.navigation(state)

    assert navigation['status'] == 'ok'
    assert len(navigation['targets']) == 12
    assert len(calls) == 3


def test_navigation_reuses_blocker_interaction_plan_for_overlapping_touch_targets(monkeypatch, tmp_path):
    door = {
        'id': 74, 'x': 16, 'y': 8, 'state': 2, 'trigger_mode': 1,
        'trigger_script': 8149, 'blocking': True, 'interactable': True,
    }
    targets = [
        make_touch_object(52, x=32, y=0, sprite_id=0),
        make_touch_object(53, x=32, y=0, sprite_id=21),
    ]
    debug = make_debug_state(tmp_path)
    state = make_state(width=3, height=3, start=(0, 2), objects=[door, *targets])
    calls = 0
    original = debug.blocker_interaction_plan

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(debug, 'blocker_interaction_plan', counted)
    navigation = debug.navigation(state)
    by_id = {target['id']: target for target in navigation['targets']}

    assert calls == 1
    assert by_id[52]['status'] == 'blocked_by_interactable'
    assert by_id[53]['status'] == 'blocked_by_interactable'
    assert by_id[52]['blocker']['id'] == 74
    assert by_id[53]['blocker']['id'] == 74
    assert by_id[52]['steps'] == by_id[53]['steps'] == 0


def test_navigation_overlapping_touch_targets_share_the_same_terminal_step(tmp_path):
    targets = [
        make_touch_object(52, x=32, y=0, sprite_id=0),
        make_touch_object(53, x=32, y=0, sprite_id=21),
    ]
    debug = make_debug_state(tmp_path)
    state = make_state(width=3, height=3, start=(1, 1), objects=targets)

    navigation = debug.navigation(state)
    by_id = {target['id']: target for target in navigation['targets']}

    assert by_id[52]['status'] == 'transition'
    assert by_id[53]['status'] == 'reachable'
    assert by_id[52]['steps'] == by_id[53]['steps'] == 1


def test_navigation_touch_target_does_not_route_through_another_trigger(tmp_path):
    intervening = make_touch_object(51, x=16, y=8, sprite_id=0)
    target = make_touch_object(52, x=32, y=0, sprite_id=21)
    debug = make_debug_state(tmp_path)
    state = make_state(width=3, height=3, start=(0, 2), objects=[intervening, target])

    navigation = debug.navigation(state)
    by_id = {item['id']: item for item in navigation['targets']}

    assert by_id[51]['status'] == 'transition'
    assert by_id[51]['steps'] == 1
    assert by_id[52]['status'] == 'blocked_by_transition'
    assert by_id[52]['steps'] is None


def test_navigation_does_not_report_noninteractable_blocker_as_actionable(tmp_path):
    blocker = {
        'id': 74, 'x': 16, 'y': 8, 'state': 2, 'trigger_mode': 0,
        'trigger_script': 0, 'blocking': True, 'interactable': False,
    }
    target = make_touch_object(52, x=32, y=0)
    debug = make_debug_state(tmp_path)
    state = make_state(width=3, height=3, start=(0, 2), objects=[blocker, target])

    navigation = debug.navigation(state)
    result = {item['id']: item for item in navigation['targets']}[52]

    assert result['status'] == 'unreachable_static'
    assert result['steps'] is None
    assert 'blocker' not in result


def test_navigation_touch_target_may_occupy_its_own_blocking_node(tmp_path):
    target = make_touch_object(52, x=32, y=0)
    target['blocking'] = True
    debug = make_debug_state(tmp_path)
    state = make_state(width=3, height=3, start=(0, 2), objects=[target])

    navigation = debug.navigation(state)
    result = {item['id']: item for item in navigation['targets']}[52]

    assert result['status'] == 'reachable'
    assert result['steps'] == 2


def test_navigation_search_tie_keeps_route_direction_order(tmp_path):
    target = {
        'id': 10, 'x': 32, 'y': 0, 'state': 1, 'trigger_mode': 3,
        'trigger_script': 100, 'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    state = make_state(width=7, height=7, start=(3, 3), objects=[target])

    route = debug.route_to_object(state, target_id=10)
    navigation = debug.navigation(state)
    result = {item['id']: item for item in navigation['targets']}[10]

    assert result['status'] == route['status'] == 'reachable'
    assert result['steps'] == route['route']['steps']
    assert result['required_direction'] == route['required_direction']


def test_navigation_graph_cache_keys_all_grid_fields_and_evicts_lru(tmp_path):
    debug = make_debug_state(tmp_path)
    base_state = make_state(width=3, height=3)
    base_grid = decode_nav_grid(base_state)
    base_graph = debug.navigation_graph(base_grid)

    assert debug.navigation_graph(decode_nav_grid(json.loads(json.dumps(base_state)))) is base_graph

    variants = []
    for field in ('origin', 'step', 'width', 'height', 'bits'):
        state = json.loads(json.dumps(base_state))
        nav_grid = state['nav_grid']
        if field == 'origin':
            nav_grid['origin']['x'] += 16
        elif field == 'step':
            nav_grid['step']['x'] += 16
        elif field == 'width':
            nav_grid['width'] += 1
        elif field == 'height':
            nav_grid['height'] += 1
        else:
            nav_grid['bits'] = ('0' if nav_grid['bits'][0] != '0' else 'f') + nav_grid['bits'][1:]
        variants.append(debug.navigation_graph(decode_nav_grid(state)))
    assert all(graph is not base_graph for graph in variants)

    lru_path = tmp_path / 'lru'
    lru_path.mkdir()
    debug = make_debug_state(lru_path)
    graphs = []
    grids = []
    for offset in range(16):
        state = make_state(width=3, height=3)
        state['nav_grid']['origin']['x'] = offset * 16
        grid = decode_nav_grid(state)
        grids.append(grid)
        graphs.append(debug.navigation_graph(grid))
    assert debug.navigation_graph(grids[0]) is graphs[0]

    extra = make_state(width=3, height=3)
    extra['nav_grid']['origin']['x'] = 16 * 16
    debug.navigation_graph(decode_nav_grid(extra))

    assert len(debug.navigation_graph_cache) == 16
    assert debug.navigation_graph(grids[0]) is graphs[0]
    assert debug.navigation_graph(grids[1]) is not graphs[1]


def test_navigation_attaches_logical_room_once_for_raw_state(monkeypatch, tmp_path):
    target = make_touch_object(52, x=32, y=0, sprite_id=0)
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    calls = 0
    original = debug.static_room_component_for_state

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(debug, "static_room_component_for_state", counted)
    navigation = debug.navigation(state)
    assert navigation["status"] == "ok"
    assert state.get("_logical_room", {}).get("id")
    assert calls == 1


def test_lightweight_navigation_does_not_run_full_target_decoration(monkeypatch, tmp_path):
    target = make_touch_object(52, x=32, y=0, sprite_id=0)
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])

    monkeypatch.setattr(
        debug,
        "transition_destination_for_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected transition lookup")),
    )
    monkeypatch.setattr(
        debug,
        "decorate_target_affordance",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected affordance decoration")),
    )

    navigation = debug.lightweight_navigation(state)
    assert navigation["status"] == "pending"
    assert navigation["targets"][0]["transition_candidate"]


def test_touch_boundary_nodes_match_bruteforce_scan(tmp_path):
    target = make_touch_object(52, x=77, y=41, sprite_id=0)
    target["trigger_mode"] = 5
    debug = make_debug_state(tmp_path)
    state = make_state(width=40, height=50, start=(1, 1), objects=[target], blocked={(5, 5), (6, 6)})
    grid = decode_nav_grid(state)
    radius = trigger_radius(target["trigger_mode"])
    expected = {
        (x, y)
        for y in range(grid["height"])
        for x in range(grid["width"])
        if nav_bit(grid, (x, y)) and pal_distance(world_pos(grid, (x, y)), (target["x"], target["y"])) < radius
    }
    assert debug.touch_boundary_nodes_for_object(target, grid) == expected



def test_navigation_step_interacts_with_blocking_door(tmp_path):
    door = {'id': 74, 'x': 16, 'y': 8, 'state': 2, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': True, 'interactable': True}
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 21, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(width=3, height=3, start=(0, 2), objects=[door, target])
    write_state(debug, state)
    result = debug.navigation_step(state, target_id=52)
    assert result['ok']
    assert result['status'] == 'interacting'
    assert result['blocker']['id'] == 74
    assert result['action']['buttons'] == ['up', 'search']
    assert debug.action_path.exists()



def test_invisible_touch_target_is_reported_as_transition(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    write_state(debug, state)
    route = debug.route_to_object(state, target_id=52)
    assert route['status'] == 'transition'
    assert route['transition_candidate']
    assert route['route']['steps'] == 1
    navigation = debug.navigation(state)
    target_status = {target['id']: target for target in navigation['targets']}
    assert target_status[52]['status'] == 'transition'
    assert target_status[52]['transition_candidate']
    assert target_status[52]['steps'] == 1
    step = debug.navigation_step(state, target_id=52)
    assert step['ok']
    assert step['status'] == 'stepping'
    assert step['action']['buttons'] == ['up']
    assert debug.action_path.exists()



def test_navigation_transition_records_observed_destination(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    write_state(debug, state)
    step = debug.navigation_step(state, target_id=52)
    assert step['ok']
    next_state = make_state(objects=[])
    next_state['scene_id'] = 4
    next_state['map_id'] = 11
    next_state['room'] = {'key': 'next-room'}
    next_state['trigger_observation'] = {
        'generation': 1,
        'event_object_id': 52,
        'trigger_script': 4631,
        'completed': True,
        'source': {'scene_id': 3, 'map_id': 10, 'x': 16, 'y': 8},
    }
    write_state(debug, next_state)
    debug.snapshot()
    next_state['frame'] += 1
    write_state(debug, next_state)
    debug.snapshot()
    data = debug.read_exploration()
    from_identity = debug.room_identity(state)
    to_identity = debug.room_identity(next_state)
    transition_key = debug.transition_observation_from_state(state, 52)['transition_key']
    record = data['transitions'][from_identity['nav_room_id']][transition_key]
    assert record['observed']
    assert record['transition_key'] == transition_key
    assert record['object']['id'] == 52
    assert record['transition_key_fields']['x'] == 32
    assert record['transition_key_fields']['y'] == 0
    assert record['transition_key_fields']['trigger_mode'] == 4
    assert record['transition_key_fields']['trigger_script'] == 4631
    assert record['from_room_id'] == from_identity['id']
    assert record['from_nav_room_id'] == from_identity['nav_room_id']
    assert record['from_legacy_nav_room_id'] == '3/10/test-room'
    assert record['from_region_key'] == from_identity['region_key']
    assert record['to_room_id'] == to_identity['id']
    assert record['to_nav_room_id'] == to_identity['nav_room_id']
    assert record['observed_from_state']['party_position'] == {'x': 16, 'y': 8}
    assert record['observed_to_state']['room_key'] == 'next-room'
    assert record['confirmation'] == 'native_trigger'
    assert record['intra_room_transition'] is False
    assert 'source_endpoint' not in record
    assert 'destination_endpoint' not in record


def test_dynamic_touch_native_trigger_is_handed_to_navigation_request(tmp_path):
    target = {
        'id': 117, 'x': 96, 'y': 0, 'sprite_id': 21, 'state': 1,
        'trigger_mode': 4, 'trigger_script': 5925, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    source = make_state(width=7, height=7, start=(0, 6), objects=[target], frame=10)
    source.update({'scene_id': 5, 'map_id': 2, 'room': {'key': 'dock'}})
    write_state(debug, source)
    debug.snapshot()

    started = debug.navigation_step(source, target_id=117)
    assert started['status'] == 'stepping'
    assert started['segment_steps'] == 6
    assert started['remaining_steps'] == 0
    assert 'observation_id' not in started
    debug.action_path.unlink()
    debug.pending_written_action = None

    destination = make_state(width=4, height=4, start=(2, 2), objects=[], frame=11)
    destination.update({'scene_id': 15, 'map_id': 3, 'room': {'key': 'shore'}})
    destination['trigger_observation'] = {
        'generation': 7,
        'event_object_id': 117,
        'trigger_script': 5925,
        'completed': True,
        'source': {'scene_id': 5, 'map_id': 2, 'x': 96, 'y': 0},
        'destination': {'scene_id': 15, 'map_id': 3, 'x': 32, 'y': 16, 'frame': 11},
    }
    write_state(debug, destination)
    debug.snapshot()

    pending = debug.pending_transition_observation
    assert pending is not None
    assert pending['owner'] == 'navigation'
    observation_id = pending['observation_id']
    assert debug.navigation_transition_requests == {117: observation_id}
    observing = debug.navigation_step(destination, target_id=117)
    assert observing['ok'] is True
    assert observing['status'] == 'transition_observing'

    destination['frame'] = 12
    write_state(debug, destination)
    transitioned = debug.navigation_step(destination, target_id=117)
    assert transitioned['ok'] is True
    assert transitioned['status'] == 'transitioned'
    assert transitioned['transition']['confirmation'] == 'native_trigger'
    assert transitioned['transition']['from_scene_id'] == 5
    assert transitioned['transition']['to_scene_id'] == 15
    source_identity = debug.room_identity(source)
    last_route = debug.read_exploration()['rooms'][source_identity['id']]['last_route']
    assert last_route['pending'] is False
    assert last_route['outcome'] == 'transitioned'

    diagnostics = [
        json.loads(line)
        for line in debug.navigation_log_path.read_text(encoding='utf-8').splitlines()
    ]
    owner = next(entry for entry in diagnostics if entry['type'] == 'native_transition_owner')
    assert owner['owner'] == 'navigation'
    assert owner['expected_position'] == {'x': 96, 'y': 0}
    assert owner['source_position'] == {'x': 96, 'y': 0}
    assert owner['association_rejected_reason'] is None


def test_dynamic_touch_delayed_trigger_keeps_final_route_for_navigation(tmp_path):
    target = {
        'id': 248, 'x': 64, 'y': 0, 'sprite_id': 84, 'state': 1,
        'trigger_mode': 4, 'trigger_script': 8496, 'auto_script': 2,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    source = make_state(width=9, height=7, start=(0, 4), objects=[target], frame=10)
    source.update({'scene_id': 17, 'map_id': 6, 'room': {'key': 'lotus'}})
    write_state(debug, source)
    debug.snapshot()

    started = debug.navigation_step(source, target_id=248)
    assert started['status'] == 'stepping'
    assert started['remaining_steps'] == 0
    debug.action_path.unlink()
    debug.pending_written_action = None

    reached = make_state(width=9, height=7, start=(4, 0), objects=[target], frame=11)
    reached.update({'scene_id': 17, 'map_id': 6, 'room': {'key': 'lotus'}})
    write_state(debug, reached)
    debug.snapshot()

    identity = debug.room_identity(reached)
    last_route = debug.read_exploration()['rooms'][identity['id']]['last_route']
    assert last_route['pending'] is True
    waiting = debug.navigation_step(reached, target_id=248)
    assert waiting['status'] == 'transition_observing'
    assert not debug.action_path.exists()

    destination = make_state(width=9, height=7, start=(8, 2), objects=[target], frame=12)
    destination.update({'scene_id': 17, 'map_id': 6, 'room': {'key': 'lotus'}})
    destination['_logical_room'] = debug.logical_room_identity(reached)
    destination['trigger_observation'] = {
        'generation': 1,
        'event_object_id': 248,
        'trigger_script': 8496,
        'completed': True,
        'source': {'scene_id': 17, 'map_id': 6, 'x': 64, 'y': 0},
        'destination': {'scene_id': 17, 'map_id': 6, **destination['party_position'], 'frame': 12},
    }
    write_state(debug, destination)
    debug.snapshot()

    observation_id = debug.navigation_transition_requests[248]
    pending = debug.pending_transition_observation
    assert pending is not None
    assert pending['owner'] == 'navigation'
    assert pending['observation_id'] == observation_id

    destination['frame'] = 13
    write_state(debug, destination)
    transitioned = debug.navigation_step(destination, target_id=248)
    assert transitioned['status'] == 'transitioned'
    assert transitioned['transition']['confirmation'] == 'native_trigger'
    assert transitioned['transition']['intra_room_transition'] is True
    assert not debug.action_path.exists()

    last_route = debug.read_exploration()['rooms'][identity['id']]['last_route']
    assert last_route['pending'] is False
    assert last_route['outcome'] == 'transitioned'

    diagnostics = [
        json.loads(line)
        for line in debug.navigation_log_path.read_text(encoding='utf-8').splitlines()
    ]
    owner = next(entry for entry in diagnostics if entry['type'] == 'native_transition_owner')
    assert owner['owner'] == 'navigation'
    assert owner['association_rejected_reason'] is None


def test_final_touch_route_expires_to_arrived_without_native_trigger(tmp_path):
    target = make_touch_object(248, x=64, y=0, sprite_id=84)
    debug = make_debug_state(tmp_path)
    source = make_state(width=5, height=5, start=(0, 4), objects=[target])
    route = debug.route_to_object(source, target_id=248)
    identity = debug.room_identity(source)
    debug.remember_navigation_route(source, 248, route['route'], 4, 0, 'reachable')

    reached = make_state(width=5, height=5, start=(4, 0), objects=[target])
    data = debug.read_exploration()
    data['rooms'][identity['id']]['last_route']['updated_at'] = time.time() - 10
    debug.write_exploration(data)
    write_state(debug, reached)
    debug.update_exploration(reached)

    result = debug.navigation_step(reached, target_id=248)
    assert result['status'] == 'arrived'
    last_route = debug.read_exploration()['rooms'][identity['id']]['last_route']
    assert last_route['pending'] is False
    assert last_route['outcome'] == 'confirmation_expired'


def test_reached_non_touch_route_is_not_expired_by_touch_confirmation_window(tmp_path):
    target = {
        'id': 63, 'x': 64, 'y': 0, 'state': 1, 'trigger_mode': 1,
        'trigger_script': 100, 'auto_script': 0, 'blocking': False,
        'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    source = make_state(width=5, height=5, start=(0, 4), objects=[target])
    route = debug.route_to_object(source, target_id=63)
    identity = debug.room_identity(source)
    steps = route['route']['steps']
    debug.remember_navigation_route(source, 63, route['route'], steps, 0, 'reachable')

    expected = route['route']['nodes'][-1]
    reached = make_state(
        width=5,
        height=5,
        start=(expected['x'] // 16, expected['y'] // 8),
        objects=[target],
    )
    data = debug.read_exploration()
    data['rooms'][identity['id']]['last_route']['updated_at'] = time.time() - 10
    debug.write_exploration(data)
    debug.update_exploration(reached)

    last_route = debug.read_exploration()['rooms'][identity['id']]['last_route']
    assert last_route['pending'] is False
    assert last_route['outcome'] == 'expected_node_reached'


def test_dynamic_touch_dialog_interrupt_preserves_navigation_transition_observer(tmp_path):
    target = {
        'id': 117, 'x': 32, 'y': 0, 'sprite_id': 21, 'state': 1,
        'trigger_mode': 4, 'trigger_script': 5925, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    source = make_state(objects=[target], frame=10)
    write_state(debug, source)
    debug.snapshot()
    assert debug.navigation_step(source, target_id=117)['status'] == 'stepping'
    debug.action_path.unlink()
    debug.pending_written_action = None

    destination = make_state(objects=[], frame=11)
    destination.update({'scene_id': 15, 'map_id': 3, 'room': {'key': 'shore'}})
    destination['dialog'] = {'active': True, 'visible': True, 'latest_event': {'text': '靠岸'}}
    destination['trigger_observation'] = {
        'generation': 7, 'event_object_id': 117, 'trigger_script': 5925, 'completed': True,
        'source': {'scene_id': 3, 'map_id': 10, 'x': 32, 'y': 0},
        'destination': {'scene_id': 15, 'map_id': 3, **destination['party_position']},
    }
    write_state(debug, destination)
    debug.snapshot()
    observation_id = debug.navigation_transition_requests[117]

    interrupted = debug.navigation_step(destination, target_id=117)
    assert interrupted == {'ok': False, 'status': 'in_dialog', 'target_id': 117}
    assert debug.navigation_transition_requests[117] == observation_id
    assert debug.pending_transition_observation['observation_id'] == observation_id
    assert debug.observations.get(observation_id)['status'] == 'pending'

    replacement = dict(destination)
    replacement['frame'] = 12
    replacement['dialog'] = {'active': False, 'visible': False}
    replacement['trigger_observation'] = {
        'generation': 8,
        'event_object_id': 49,
        'trigger_script': 9001,
        'completed': True,
    }
    write_state(debug, replacement)
    debug.snapshot()

    assert debug.pending_transition_observation is None
    assert debug.navigation_transition_requests == {}
    assert debug.observations.get(observation_id) is None
    resumed = debug.navigation_step(replacement, target_id=117)
    assert resumed['status'] != 'transition_observing'


def test_dynamic_touch_without_position_change_is_not_recorded_as_transition(tmp_path):
    target = {
        'id': 117, 'x': 32, 'y': 0, 'sprite_id': 21, 'state': 1,
        'trigger_mode': 4, 'trigger_script': 5925, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    source = make_state(objects=[target], frame=10)
    write_state(debug, source)
    debug.snapshot()
    assert debug.navigation_step(source, target_id=117)['status'] == 'stepping'
    debug.action_path.unlink()
    debug.pending_written_action = None

    triggered = make_state(start=(2, 0), objects=[target], frame=11)
    triggered['trigger_observation'] = {
        'generation': 1, 'event_object_id': 117, 'trigger_script': 5925, 'completed': True,
        'source': {'scene_id': 3, 'map_id': 10, 'x': 32, 'y': 0},
        'destination': {'scene_id': 3, 'map_id': 10, 'x': 32, 'y': 0, 'frame': 11},
    }
    write_state(debug, triggered)
    debug.snapshot()

    assert debug.pending_transition_observation is None
    assert debug.read_exploration()['transitions'] == {}


def test_dynamic_touch_owner_rejects_mismatched_navigation_route(tmp_path):
    target = {
        'id': 117, 'x': 32, 'y': 0, 'sprite_id': 21, 'state': 1,
        'trigger_mode': 4, 'trigger_script': 5925, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    source = make_state(objects=[target])
    route = debug.route_to_object(source, target_id=117)
    identity = debug.room_identity(source)
    cases = [
        (
            'navigation_target_mismatch',
            lambda last_route, _room: last_route.__setitem__('target_id', 999),
        ),
        (
            'navigation_target_not_touch',
            lambda last_route, _room: last_route['target_selection'].__setitem__('kind', 'search'),
        ),
        (
            'navigation_route_outside_observation_window',
            lambda last_route, _room: last_route.__setitem__('updated_at', time.time() - 10),
        ),
        (
            'navigation_expected_source_mismatch',
            lambda last_route, _room: last_route['expected_node'].__setitem__('x', 999),
        ),
        (
            'navigation_source_room_mismatch',
            lambda _last_route, room: room.__setitem__('scene_id', 99),
        ),
    ]

    for expected_reason, mutate in cases:
        debug.remember_navigation_route(source, 117, route['route'], 1, 0, 'reachable')
        data = debug.read_exploration()
        room = data['rooms'][identity['id']]
        mutate(room['last_route'], room)
        debug.write_exploration(data)

        owner, details = debug.native_transition_observation_owner(
            source,
            117,
            {'scene_id': 3, 'map_id': 10, 'x': 32, 'y': 0},
        )

        assert owner == 'background'
        assert details['association_rejected_reason'] == expected_reason


def test_navigation_step_drops_request_mapping_after_observation_retention_cleanup(tmp_path):
    debug = make_debug_state(tmp_path)
    missing = make_state(objects=[])
    observation_id = debug.observations.register(
        'transition', owner='navigation', timeout=4, payload={}
    )
    debug.navigation_transition_requests[117] = observation_id
    debug.pending_transition_observation = {
        'object_id': 117,
        'observation_id': observation_id,
        'owner': 'navigation',
    }
    debug.observations.cancel(observation_id, 'test_cleanup')
    debug.observations.consume(observation_id, owner='navigation')
    write_state(debug, missing)

    result = debug.navigation_step(missing, target_id=117)

    assert result['status'] == 'unknown_target'
    assert debug.navigation_transition_requests == {}
    assert debug.pending_transition_observation is None


def test_navigation_step_keeps_pending_transition_until_destination_is_stable(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    write_state(debug, state)
    assert debug.navigation_step(state, target_id=52)['ok']
    debug.action_path.unlink()

    destination = make_state(objects=[])
    destination['scene_id'] = 4
    destination['map_id'] = 11
    destination['room'] = {'key': 'next-room'}
    destination['trigger_observation'] = {
        'generation': 1,
        'event_object_id': 52,
        'trigger_script': 4631,
        'completed': True,
    }
    write_state(debug, destination)
    observing = debug.navigation_step(destination, target_id=52)
    assert observing['ok']
    assert observing['status'] == 'transition_observing'
    assert observing['stable_snapshots'] == 1
    assert debug.pending_transition_observation is not None
    assert debug.read_exploration()['transitions'] == {}

    destination['frame'] += 1
    write_state(debug, destination)
    transitioned = debug.navigation_step(destination, target_id=52)
    assert transitioned['ok']
    assert transitioned['status'] == 'transitioned'
    assert debug.pending_transition_observation is None

    from_identity = debug.room_identity(state)
    to_identity = debug.room_identity(destination)
    transition_key = debug.transition_observation_from_state(state, 52)['transition_key']
    record = debug.read_exploration()['transitions'][from_identity['nav_room_id']][transition_key]
    assert record['transition_key'] == transition_key
    assert record['from_room_id'] == from_identity['id']
    assert record['object']['id'] == 52
    assert record['to_room_id'] == to_identity['id']


def test_navigation_step_does_not_discard_changed_room_observation_without_matching_trigger(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    write_state(debug, state)
    assert debug.navigation_step(state, target_id=52)['ok']
    debug.action_path.unlink()

    destination = make_state(objects=[])
    destination['scene_id'] = 4
    destination['map_id'] = 11
    destination['trigger_observation'] = {
        'generation': 1,
        'event_object_id': 99,
        'trigger_script': 1,
        'completed': True,
    }
    write_state(debug, destination)
    result = debug.navigation_step(destination, target_id=52)
    assert result['ok']
    assert result['status'] == 'transition_observing'
    assert debug.pending_transition_observation is not None
    assert debug.read_exploration()['transitions'] == {}


def test_navigation_step_waits_for_pending_transition_when_target_is_unreachable_after_landing(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    write_state(debug, state)
    assert debug.navigation_step(state, target_id=52)['ok']
    debug.action_path.unlink()

    missing = make_state(objects=[])
    write_state(debug, missing)
    result = debug.navigation_step(missing, target_id=52)
    assert result['ok']
    assert result['status'] == 'transition_observing'
    assert result['stable_snapshots'] == 0
    assert debug.pending_transition_observation is not None


def test_navigation_step_replans_after_pending_transition_times_out(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    write_state(debug, state)
    assert debug.navigation_step(state, target_id=52)['ok']
    debug.action_path.unlink()
    debug.pending_transition_observation['sent_at'] -= 5

    missing = make_state(objects=[])
    write_state(debug, missing)
    result = debug.navigation_step(missing, target_id=52)
    assert not result['ok']
    assert result['status'] == 'unknown_target'
    assert debug.pending_transition_observation is None


def test_navigation_step_confirms_delayed_intra_room_transition_after_landing(tmp_path):
    target = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    source = make_state(width=6, height=4, start=(1, 1), objects=[target], frame=10)
    write_state(debug, source)
    started = debug.navigation_step(source, target_id=48)
    assert started['status'] == 'stepping'
    assert started['remaining_steps'] == 0
    debug.action_path.unlink()

    landed = make_state(width=6, height=4, start=(4, 2), objects=[], frame=11)
    landed['_logical_room'] = debug.logical_room_identity(source)
    write_state(debug, landed)
    observing = debug.navigation_step(landed, target_id=48)
    assert observing['ok']
    assert observing['status'] == 'transition_observing'
    assert observing['stable_snapshots'] == 0
    assert debug.pending_transition_observation is not None

    destination = dict(landed)
    destination['frame'] = 12
    destination['trigger_observation'] = {
        'generation': 1,
        'event_object_id': 48,
        'trigger_script': 4631,
        'completed': True,
        'source': {'scene_id': 3, 'map_id': 10, **source['party_position']},
        'destination': {'scene_id': 3, 'map_id': 10, **landed['party_position'], 'frame': 11},
    }
    write_state(debug, destination)
    stabilizing = debug.navigation_step(destination, target_id=48)
    assert stabilizing['ok']
    assert stabilizing['status'] == 'transition_observing'
    assert stabilizing['stable_snapshots'] == 1

    destination['frame'] = 13
    write_state(debug, destination)
    transitioned = debug.navigation_step(destination, target_id=48)
    assert transitioned['ok']
    assert transitioned['status'] == 'transitioned'
    assert transitioned['transition']['intra_room_transition'] is True
    assert debug.pending_transition_observation is None

    source_identity = debug.room_identity(source)
    transition_key = debug.transition_observation_from_state(source, 48)['transition_key']
    record = debug.read_exploration()['transitions'][source_identity['nav_room_id']][transition_key]
    assert record['intra_room_transition'] is True
    assert record['confirmation'] == 'native_trigger'


def test_navigation_step_receives_transition_already_confirmed_by_background_snapshot(tmp_path):
    target = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    source = make_state(width=6, height=4, start=(1, 1), objects=[target], frame=10)
    write_state(debug, source)
    started = debug.navigation_step(source, target_id=48)
    observation_id = started['observation_id']
    debug.action_path.unlink()

    destination = make_state(width=6, height=4, start=(4, 2), objects=[], frame=11)
    destination['_logical_room'] = debug.logical_room_identity(source)
    destination['trigger_observation'] = {
        'generation': 1,
        'event_object_id': 48,
        'trigger_script': 60,
        'completed': True,
        'source': {'scene_id': 3, 'map_id': 10, **source['party_position']},
        'destination': {'scene_id': 3, 'map_id': 10, **destination['party_position'], 'frame': 10},
    }
    write_state(debug, destination)
    debug.snapshot()
    destination['frame'] = 12
    write_state(debug, destination)
    debug.snapshot()

    assert debug.pending_transition_observation is None
    assert debug.observations.get(observation_id)['status'] == 'completed'
    transitioned = debug.navigation_step(destination, target_id=48)
    assert transitioned['ok']
    assert transitioned['status'] == 'transitioned'
    assert transitioned['observation_id'] == observation_id
    assert transitioned['transition']['intra_room_transition'] is True
    assert debug.observations.get(observation_id) is None


def test_runtime_history_reset_clears_transition_handoffs(tmp_path):
    target = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    write_state(debug, state)
    started = debug.navigation_step(state, target_id=48)
    observation_id = started['observation_id']

    debug.clear_runtime_history('test_reset', state)

    assert debug.pending_transition_observation is None
    assert debug.navigation_transition_requests == {}
    assert debug.observations.get(observation_id) is None


def test_transition_observation_ignores_logical_room_change_without_native_trigger(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    write_state(debug, state)
    assert debug.navigation_step(state, target_id=52)['ok']
    changed = make_state(objects=[])
    changed['room'] = {'key': 'different-logical-component'}
    changed['frame'] += 1
    write_state(debug, changed)
    debug.snapshot()
    changed['frame'] += 1
    write_state(debug, changed)
    debug.snapshot()
    assert debug.read_exploration()['transitions'] == {}


def test_transition_observation_requires_matching_native_object_and_stable_destination(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    write_state(debug, state)
    assert debug.navigation_step(state, target_id=52)['ok']
    changed = make_state(objects=[])
    changed['scene_id'] = 4
    changed['map_id'] = 11
    changed['room'] = {'key': 'stairs-destination'}
    changed['trigger_observation'] = {'generation': 1, 'event_object_id': 99, 'trigger_script': 1, 'completed': True}
    changed['frame'] += 1
    write_state(debug, changed)
    debug.snapshot()
    assert debug.read_exploration()['transitions'] == {}
    changed['trigger_observation'] = {'generation': 2, 'event_object_id': 52, 'trigger_script': 4631, 'completed': True}
    changed['frame'] += 1
    write_state(debug, changed)
    debug.snapshot()
    assert debug.read_exploration()['transitions'] == {}
    changed['frame'] += 1
    write_state(debug, changed)
    debug.snapshot()
    assert debug.read_exploration()['transitions']


def test_delayed_search_trigger_hands_interaction_to_transition_observer(tmp_path):
    target = {
        'id': 75, 'x': 16, 'y': 8, 'sprite_id': 1, 'state': 1,
        'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    source = make_state(objects=[target], frame=20)
    debug.last_stable_room_state = source
    interaction_id = debug.mark_pending_interaction_observation(
        source, 75, action={'buttons': ['search'], 'frames': 1},
    )
    debug.pending_interaction_observation['sent_at'] -= 1.0

    running = dict(source)
    running['frame'] = 30
    running['trigger_observation'] = {
        'generation': 1, 'event_object_id': 75, 'trigger_script': 8149,
        'completed': False,
        'source': {'scene_id': 3, 'map_id': 10, **source['party_position']},
    }
    debug.discover_native_transition_observation(running)
    transition_id = debug.pending_transition_observation['observation_id']

    assert debug.pending_transition_observation['linked_interaction_observation_id'] == interaction_id
    assert debug.navigation_transition_requests[75] == transition_id
    assert debug.observe_pending_interaction_outcome(running) is None
    assert not [event for event in debug.event_log if event['type'] == 'interaction_no_effect']

    destination = make_state(objects=[], frame=31)
    destination.update({'scene_id': 4, 'map_id': 11, 'room': {'key': 'destination'}})
    destination['trigger_observation'] = {
        **running['trigger_observation'],
        'completed': True,
        'destination': {'scene_id': 4, 'map_id': 11, **destination['party_position'], 'frame': 31},
    }
    debug.observe_pending_transition_destination(destination)
    destination['frame'] = 32
    record = debug.observe_pending_transition_destination(destination)

    assert record['from_room_id'] == debug.room_identity(source)['id']
    assert record['to_room_id'] == debug.room_identity(destination)['id']
    assert debug.pending_interaction_observation is None
    assert [event['type'] for event in debug.event_log] == ['interaction_effect']
    assert debug.event_log[-1]['room_changed'] is True
    assert debug.observations.get(interaction_id)['result']['transition']['transition_key'] == record['transition_key']
    assert debug.observations.get(transition_id)['status'] == 'completed'


def test_completed_search_trigger_without_change_resumes_no_effect(tmp_path):
    target = {
        'id': 75, 'x': 16, 'y': 8, 'sprite_id': 1, 'state': 1,
        'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    source = make_state(objects=[target], frame=20)
    debug.last_stable_room_state = source
    interaction_id = debug.mark_pending_interaction_observation(
        source, 75, action={'buttons': ['search'], 'frames': 1},
    )
    debug.pending_interaction_observation['sent_at'] -= 1.0
    completed = make_state(objects=[target], frame=30)
    completed['trigger_observation'] = {
        'generation': 1, 'event_object_id': 75, 'trigger_script': 8149,
        'completed': True,
        'source': {'scene_id': 3, 'map_id': 10, **source['party_position']},
        'destination': {'scene_id': 3, 'map_id': 10, **source['party_position'], 'frame': 30},
    }

    debug.discover_native_transition_observation(completed)
    debug.observe_pending_transition_destination(completed)
    outcome = debug.observe_pending_interaction_outcome(completed)

    assert debug.pending_transition_observation is None
    assert debug.navigation_transition_requests == {}
    assert outcome['reason'] == 'no_state_change'
    assert debug.event_log[-1]['type'] == 'interaction_no_effect'
    assert debug.observations.get(interaction_id)['status'] == 'completed'


def test_linked_search_transition_timeout_clears_link_and_resumes_no_effect(tmp_path):
    target = {
        'id': 75, 'x': 16, 'y': 8, 'sprite_id': 1, 'state': 1,
        'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    source = make_state(objects=[target], frame=20)
    interaction_id = debug.mark_pending_interaction_observation(
        source, 75, action={'buttons': ['search'], 'frames': 1},
    )
    debug.mark_pending_transition_observation(
        source, 75, owner='navigation',
        linked_interaction=debug.pending_interaction_observation,
    )
    debug.pending_transition_observation['sent_at'] -= 100.0
    debug.pending_interaction_observation['sent_at'] -= 100.0
    later = make_state(objects=[target], frame=30)

    debug.observe_pending_transition_destination(later)
    outcome = debug.observe_pending_interaction_outcome(later)

    assert debug.pending_transition_observation is None
    assert debug.navigation_transition_requests == {}
    assert outcome['reason'] == 'no_state_change'
    assert debug.observations.get(interaction_id)['status'] == 'completed'


def test_linked_search_transition_game_instance_change_cancels_both_observations(tmp_path):
    target = {
        'id': 75, 'x': 16, 'y': 8, 'sprite_id': 1, 'state': 1,
        'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    source = make_state(objects=[target], frame=20)
    source['game_instance_id'] = 'game-a'
    interaction_id = debug.mark_pending_interaction_observation(
        source, 75, action={'buttons': ['search'], 'frames': 1},
    )
    transition_id = debug.mark_pending_transition_observation(
        source, 75, owner='navigation',
        linked_interaction=debug.pending_interaction_observation,
    )
    replacement = make_state(objects=[target], frame=21)
    replacement['game_instance_id'] = 'game-b'

    debug.observe_pending_transition_destination(replacement)

    assert debug.pending_transition_observation is None
    assert debug.pending_interaction_observation is None
    assert debug.navigation_transition_requests == {}
    assert debug.observations.get(transition_id) is None
    assert debug.observations.get(interaction_id)['status'] == 'cancelled'


def test_bound_transition_observation_is_retired_when_native_trigger_is_superseded(tmp_path):
    portal = {
        'id': 48, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1,
        'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    source = make_state(objects=[portal], frame=10)
    write_state(debug, source)
    debug.snapshot()
    assert debug.navigation_step(source, target_id=48)['status'] == 'stepping'
    debug.action_path.unlink()
    debug.pending_written_action = None

    bound = dict(source)
    bound['frame'] = 11
    bound['trigger_observation'] = {
        'generation': 1,
        'event_object_id': 48,
        'trigger_script': 4631,
        'completed': False,
        'source': {'scene_id': 3, 'map_id': 10, 'x': 32, 'y': 0},
    }
    debug.observe_pending_transition_destination(bound)
    pending = debug.pending_transition_observation
    observation_id = pending['observation_id']
    assert pending['trigger_generation'] == 1
    assert debug.observations.get(observation_id)['deadline'] is None

    replacement = dict(bound)
    replacement['frame'] = 12
    replacement['trigger_observation'] = {
        'generation': 2,
        'event_object_id': 49,
        'trigger_script': 9001,
        'completed': True,
    }
    debug.observe_pending_transition_destination(replacement)

    assert debug.pending_transition_observation is None
    assert debug.navigation_transition_requests == {}
    assert debug.observations.get(observation_id) is None
    source_identity = debug.room_identity(source)
    last_route = debug.read_exploration()['rooms'][source_identity['id']]['last_route']
    assert last_route['pending'] is False
    assert last_route['outcome'] == 'trigger_superseded'
    diagnostics = [
        json.loads(line)
        for line in debug.navigation_log_path.read_text(encoding='utf-8').splitlines()
    ]
    event = next(item for item in diagnostics if item['type'] == 'native_transition_superseded')
    assert event['observation_id'] == observation_id
    assert event['tracked_generation'] == 1
    assert event['replacement_generation'] == 2
    assert event['replacement_object_id'] == 49

    write_state(debug, source)
    replanned = debug.navigation_step(source, target_id=48)
    assert replanned['status'] != 'transition_observing'


def test_navigation_cancel_token_prevents_action_write(tmp_path):
    debug = make_debug_state(tmp_path)
    target = {
        'id': 48, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1,
        'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    state = make_state(objects=[target])
    write_state(debug, state)
    cancelled = threading.Event()
    cancelled.set()

    result = debug.navigation_step(state, target_id=48, cancel_event=cancelled)

    assert result == {'ok': False, 'status': 'cancelled', 'target_id': 48}
    assert not debug.action_path.exists()


def test_manual_movement_records_native_transition_after_stable_destination(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    source = make_state(objects=[target], frame=10)
    write_state(debug, source)
    debug.snapshot()

    transient = make_state(objects=[], frame=11)
    transient.update({'scene_id': 4, 'map_id': 11, 'room': {'key': 'mixed-old-resources'}, 'entering_scene': True})
    transient['trigger_observation'] = {
        'generation': 1,
        'event_object_id': 52,
        'trigger_script': 4631,
        'completed': True,
        'source': {'scene_id': 3, 'map_id': 10, 'x': 48, 'y': -8},
    }
    write_state(debug, transient)
    transient_result = debug.snapshot()
    assert transient_result.get('logical_room_id') is None
    assert debug.last_stable_room_state['scene_id'] == 3
    assert list(debug.read_exploration()['rooms']) == [debug.room_identity(source)['id']]

    destination = make_state(objects=[], frame=12)
    destination.update({'scene_id': 4, 'map_id': 11, 'room': {'key': 'stairs-destination'}})
    destination['trigger_observation'] = dict(transient['trigger_observation'])
    write_state(debug, destination)
    debug.snapshot()
    assert debug.pending_transition_observation is not None
    assert debug.read_exploration()['transitions'] == {}

    destination['frame'] += 1
    write_state(debug, destination)
    debug.snapshot()
    source_identity = debug.room_identity(source)
    records = debug.read_exploration()['transitions'][source_identity['id']]
    assert len(records) == 1
    record = next(iter(records.values()))
    assert record['object_id'] == 52
    assert record['to_room_id'] == debug.room_identity(destination)['id']
    assert record['confirmation'] == 'native_trigger'
    assert record['observed_from_state']['party_position'] == {'x': 48, 'y': -8}


def test_background_search_trigger_records_objective_transition(tmp_path):
    target = {
        'id': 75, 'x': 16, 'y': 8, 'sprite_id': 1, 'state': 1,
        'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    source = make_state(objects=[target], frame=10)
    debug.last_stable_room_state = source
    destination = make_state(objects=[], frame=11)
    destination.update({'scene_id': 4, 'map_id': 11, 'room': {'key': 'destination'}})
    destination['trigger_observation'] = {
        'generation': 1, 'event_object_id': 75, 'trigger_script': 8149,
        'completed': True,
        'source': {'scene_id': 3, 'map_id': 10, **source['party_position']},
        'destination': {'scene_id': 4, 'map_id': 11, **destination['party_position'], 'frame': 11},
    }

    debug.discover_native_transition_observation(destination)
    assert debug.pending_transition_observation['owner'] == 'background'
    debug.observe_pending_transition_destination(destination)
    destination['frame'] = 12
    record = debug.observe_pending_transition_destination(destination)

    assert record['object_id'] == 75
    assert record['from_room_id'] == debug.room_identity(source)['id']
    assert record['to_room_id'] == debug.room_identity(destination)['id']
    assert debug.pending_transition_observation is None
    assert debug.navigation_transition_requests == {}


def test_manual_movement_ignores_native_trigger_with_mismatched_source_scene(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    source = make_state(objects=[target], frame=10)
    write_state(debug, source)
    debug.snapshot()
    destination = make_state(objects=[], frame=11)
    destination.update({'scene_id': 4, 'map_id': 11, 'room': {'key': 'stairs-destination'}})
    destination['trigger_observation'] = {
        'generation': 1,
        'event_object_id': 52,
        'trigger_script': 4631,
        'completed': True,
        'source': {'scene_id': 99, 'map_id': 10, 'x': 999, 'y': 999},
    }
    write_state(debug, destination)
    debug.snapshot()
    destination['frame'] += 1
    write_state(debug, destination)
    debug.snapshot()
    assert debug.pending_transition_observation is None
    assert debug.read_exploration()['transitions'] == {}
    diagnostic = json.loads(debug.navigation_log_path.read_text(encoding='utf-8').splitlines()[-1])
    assert diagnostic['type'] == 'native_transition_rejected'
    assert diagnostic['reason'] == 'source_scene_mismatch'


def test_intra_room_transition_freezes_source_and_keeps_unmatched_native_landing(tmp_path):
    source_portal = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    target_portal = {**source_portal, 'id': 47, 'x': 48, 'y': 8}
    tied_portal = {**source_portal, 'id': 49, 'x': 48, 'y': 8}
    inactive_portal = {**source_portal, 'id': 46, 'x': 48, 'y': 8, 'state': 0}
    debug = make_debug_state(tmp_path)
    source = make_state(width=6, height=4, start=(1, 1), objects=[source_portal, target_portal, tied_portal, inactive_portal], frame=10)
    source_identity = debug.room_identity(source)
    write_state(debug, source)
    debug.snapshot()

    started = dict(source)
    started['frame'] = 11
    started['trigger_observation'] = {
        'generation': 1, 'event_object_id': 48, 'trigger_script': 4631, 'completed': False,
        'source': {'scene_id': 3, 'map_id': 10, 'x': 16, 'y': 8},
    }
    write_state(debug, started)
    debug.snapshot()
    assert debug.pending_transition_observation is not None, debug.navigation_log_path.read_text(encoding='utf-8')
    assert debug.pending_transition_observation['from_room']['id'] == source_identity['id']

    middle = make_state(width=2, height=2, start=(0, 0), objects=[], frame=12)
    middle['trigger_observation'] = {
        **started['trigger_observation'],
        'completed': True,
        'destination': {'scene_id': 3, 'map_id': 10, 'x': 48, 'y': 8, 'frame': 11},
    }
    write_state(debug, middle)
    debug.snapshot()
    assert debug.read_exploration()['transitions'] == {}
    assert debug.room_identity(middle)['id'] not in debug.read_exploration()['rooms']

    destination = make_state(width=6, height=4, start=(5, 2), objects=[inactive_portal, tied_portal, target_portal, source_portal], frame=13)
    destination['_logical_room'] = debug.logical_room_identity(source)
    destination['trigger_observation'] = dict(middle['trigger_observation'])
    assert debug.room_identity(destination)['id'] == source_identity['id']
    write_state(debug, destination)
    debug.snapshot()
    destination['frame'] = 14
    write_state(debug, destination)
    debug.snapshot()

    records = debug.read_exploration()['transitions'][source_identity['id']]
    record = next(iter(records.values()))
    assert record['intra_room_transition'] is True
    assert record['source_endpoint'] == {
        'transition_key': record['transition_key'],
        'object_id': 48,
        'position': {'x': 32, 'y': 0},
        'trigger_script': 4631,
    }
    assert record['destination_endpoint'] == {
        'kind': 'native_landing',
        'position': {'x': 48, 'y': 8},
    }
    assert record['native_destination']['x'] == 48
    assert record['observed_to_state']['party_position'] == {'x': 80, 'y': 16}

    known = debug.known_transitions_summary(source_identity['id'], destination)['transitions'][0]
    assert known['intra_room_transition'] is True
    assert known['source_endpoint'] == record['source_endpoint']
    assert known['destination_endpoint'] == record['destination_endpoint']
    assert known['returns_to_recent_room'] is False


def test_intra_room_confirmed_endpoints_pair_only_from_bidirectional_records(tmp_path):
    portal_47 = {
        'id': 47, 'x': 48, 'y': 8, 'sprite_id': 0, 'state': 1,
        'trigger_mode': 4, 'trigger_script': 4701, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    portal_48 = {**portal_47, 'id': 48, 'x': 32, 'y': 0, 'trigger_script': 4801}
    debug = make_debug_state(tmp_path)
    room = make_state(objects=[portal_47, portal_48])
    identity = debug.room_identity(room)

    first = debug.record_transition_destination(
        identity, 47, identity,
        from_object=portal_47, from_state=room, to_state=room,
        confirmation='native_trigger', intra_room_transition=True,
        native_destination={'scene_id': 3, 'map_id': 10, 'x': 32, 'y': 0},
    )
    assert first['destination_endpoint']['kind'] == 'native_landing'

    second = debug.record_transition_destination(
        identity, 48, identity,
        from_object=portal_48, from_state=room, to_state=room,
        confirmation='native_trigger', intra_room_transition=True,
        native_destination={'scene_id': 3, 'map_id': 10, 'x': 48, 'y': 8},
    )
    records = debug.read_exploration()['transitions'][identity['id']]
    edge_47 = records[first['transition_key']]
    edge_48 = records[second['transition_key']]

    assert edge_47['destination_endpoint'] == {
        'kind': 'confirmed_transition_source',
        'transition_key': second['transition_key'],
        'object_id': 48,
        'position': {'x': 32, 'y': 0},
    }
    assert edge_48['destination_endpoint'] == {
        'kind': 'confirmed_transition_source',
        'transition_key': first['transition_key'],
        'object_id': 47,
        'position': {'x': 48, 'y': 8},
    }
    assert edge_47['native_destination']['x'] == 32
    assert edge_48['native_destination']['x'] == 48
    assert edge_47['traversal_count'] == edge_48['traversal_count'] == 1

    memory = debug.run_agent_tool('get_room_memory', {}, room)['transitions']['outgoing']
    by_object = {item['object_id']: item for item in memory}
    assert by_object[47]['destination_endpoint']['object_id'] == 48
    assert by_object[48]['destination_endpoint']['object_id'] == 47


def test_dynamic_same_object_transition_states_remain_distinct_endpoints(tmp_path):
    pad_a = {
        'id': 258, 'x': 16, 'y': 8, 'sprite_id': 84, 'state': 1,
        'trigger_mode': 4, 'trigger_script': 8101, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    pad_b = {**pad_a, 'x': 80, 'y': 24, 'trigger_script': 8102}
    debug = make_debug_state(tmp_path)
    room = make_state(objects=[pad_a])
    identity = debug.room_identity(room)

    edge_a = debug.record_transition_destination(
        identity, 258, identity, from_object=pad_a, from_state=room, to_state=room,
        confirmation='native_trigger', intra_room_transition=True,
        native_destination={'x': 80, 'y': 24},
    )
    edge_b = debug.record_transition_destination(
        identity, 258, identity, from_object=pad_b, from_state=room, to_state=room,
        confirmation='native_trigger', intra_room_transition=True,
        native_destination={'x': 16, 'y': 8},
    )
    records = debug.read_exploration()['transitions'][identity['id']]

    assert edge_a['transition_key'] != edge_b['transition_key']
    assert len(records) == 2
    assert records[edge_a['transition_key']]['source_endpoint']['trigger_script'] == 8101
    assert records[edge_b['transition_key']]['source_endpoint']['trigger_script'] == 8102
    assert records[edge_a['transition_key']]['destination_endpoint']['transition_key'] == edge_b['transition_key']
    assert records[edge_b['transition_key']]['destination_endpoint']['transition_key'] == edge_a['transition_key']


def test_intra_room_endpoint_ties_require_unique_same_object_state(tmp_path):
    debug = make_debug_state(tmp_path)
    room = make_state()
    identity = debug.room_identity(room)

    def record(obj, landing):
        return debug.record_transition_destination(
            identity, obj['id'], identity,
            from_object=obj, from_state=room, to_state=room,
            confirmation='native_trigger', intra_room_transition=True,
            native_destination={'x': landing[0], 'y': landing[1]},
        )

    same_object_state = {
        'id': 84, 'x': 16, 'y': 8, 'trigger_mode': 4, 'trigger_script': 8402,
    }
    other_object = {
        'id': 90, 'x': 48, 'y': 8, 'trigger_mode': 4, 'trigger_script': 9001,
    }
    record(same_object_state, (200, 200))
    record(other_object, (200, 200))
    unique = record(
        {'id': 84, 'x': 0, 'y': 0, 'trigger_mode': 4, 'trigger_script': 8401},
        (32, 0),
    )
    ambiguous = record(
        {'id': 99, 'x': 0, 'y': 16, 'trigger_mode': 4, 'trigger_script': 9901},
        (32, 0),
    )
    records = debug.read_exploration()['transitions'][identity['id']]

    assert records[unique['transition_key']]['destination_endpoint']['kind'] == 'confirmed_transition_source'
    assert records[unique['transition_key']]['destination_endpoint']['object_id'] == 84
    assert records[ambiguous['transition_key']]['destination_endpoint'] == {
        'kind': 'native_landing',
        'position': {'x': 32, 'y': 0},
    }


def test_intra_room_endpoint_does_not_match_beyond_one_pal_step(tmp_path):
    debug = make_debug_state(tmp_path)
    room = make_state()
    identity = debug.room_identity(room)
    target = {'id': 47, 'x': 80, 'y': 0, 'trigger_mode': 4, 'trigger_script': 4701}
    source = {'id': 48, 'x': 0, 'y': 0, 'trigger_mode': 4, 'trigger_script': 4801}
    debug.record_transition_destination(
        identity, 47, identity, from_object=target, from_state=room, to_state=room,
        confirmation='native_trigger', intra_room_transition=True,
        native_destination={'x': 0, 'y': 0},
    )
    edge = debug.record_transition_destination(
        identity, 48, identity, from_object=source, from_state=room, to_state=room,
        confirmation='native_trigger', intra_room_transition=True,
        native_destination={'x': 40, 'y': 0},
    )

    records = debug.read_exploration()['transitions'][identity['id']]
    assert records[edge['transition_key']]['destination_endpoint']['kind'] == 'native_landing'
    assert len(records) == 2


def test_script_control_on_non_grid_position_waits_without_bad_start_and_records_boat_transition(
    tmp_path, monkeypatch,
):
    boat = {
        'id': 52, 'x': 32, 'y': 0, 'sprite_id': 1, 'state': 1,
        'trigger_mode': 1, 'trigger_script': 4631, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    source = make_state(width=4, height=4, start=(1, 1), objects=[boat], frame=10)
    source.update({'scene_id': 5, 'map_id': 2, 'room': {'key': 'dock'}})
    write_state(debug, source)
    debug.snapshot()

    scripted = dict(source)
    scripted['frame'] = 11
    scripted['party_position'] = {'x': 17, 'y': 9}
    scripted['trigger_observation'] = {
        'generation': 1,
        'event_object_id': 52,
        'trigger_script': 4631,
        'completed': False,
        'source': {'scene_id': 5, 'map_id': 2, 'x': 16, 'y': 8},
    }
    write_state(debug, scripted)
    debug.snapshot()
    monkeypatch.setattr(
        debug,
        'route_to_object',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('route_to_object must not run while the game owns control')
        ),
    )

    waiting = debug.navigation_step(scripted, target_id=52)

    assert waiting['ok'] is True
    assert waiting['status'] == 'waiting_for_control'
    assert debug.pending_transition_observation is not None

    destination = make_state(width=4, height=4, start=(2, 2), objects=[], frame=12)
    destination.update({'scene_id': 15, 'map_id': 3, 'room': {'key': 'boat'}})
    destination['trigger_observation'] = {
        **scripted['trigger_observation'],
        'completed': True,
        'destination': {'scene_id': 15, 'map_id': 3, 'x': 32, 'y': 16, 'frame': 12},
    }
    write_state(debug, destination)
    debug.snapshot()
    destination['frame'] = 13
    write_state(debug, destination)
    debug.snapshot()

    transitions = debug.read_exploration()['transitions']
    record = next(iter(next(iter(transitions.values())).values()))
    assert record['from_scene_id'] == 5
    assert record['from_map_id'] == 2
    assert record['to_scene_id'] == 15
    assert record['to_map_id'] == 3


def test_confirmed_trigger_keeps_observation_beyond_confirmation_timeout(tmp_path):
    portal = {
        'id': 48, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1,
        'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    source = make_state(objects=[portal], frame=10)
    write_state(debug, source)
    started = debug.navigation_step(source, target_id=48)
    assert started['ok'] is True
    pending = debug.pending_transition_observation
    debug.action_path.unlink()
    debug.pending_written_action = None

    scripted = dict(source)
    scripted['frame'] = 11
    scripted['trigger_observation'] = {
        'generation': 1,
        'event_object_id': 48,
        'trigger_script': 4631,
        'completed': False,
        'source': {'scene_id': 3, 'map_id': 10, 'x': 16, 'y': 8},
    }
    pending['sent_at'] -= 5
    write_state(debug, scripted)
    debug.snapshot()

    assert debug.pending_transition_observation is pending
    assert pending['trigger_generation'] == 1
    assert pending['control_started_at'] is not None
    assert pending['landing_started_at'] is None


def test_trigger_control_timeout_is_not_deferred_to_a_later_navigation_request(tmp_path):
    portal = {
        'id': 48, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1,
        'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    source = make_state(objects=[portal], frame=10)
    write_state(debug, source)
    assert debug.navigation_step(source, target_id=48)['ok'] is True
    debug.action_path.unlink()
    debug.pending_written_action = None
    scripted = dict(source)
    scripted['frame'] = 11
    scripted['trigger_observation'] = {
        'generation': 1,
        'event_object_id': 48,
        'trigger_script': 4631,
        'completed': False,
        'source': {'scene_id': 3, 'map_id': 10, 'x': 16, 'y': 8},
    }
    write_state(debug, scripted)
    debug.snapshot()
    debug.pending_transition_observation['control_last_progress_at'] -= 31
    debug.snapshot()

    result = debug.navigation_step(scripted, target_id=48)

    assert result['ok'] is True
    assert result['status'] == 'waiting_for_control'
    assert debug.pending_transition_observation is None


def test_long_dialog_suspends_native_trigger_control_timeout(tmp_path):
    target = {
        'id': 60, 'x': 96, 'y': 0, 'sprite_id': 207, 'state': 1,
        'trigger_mode': 6, 'trigger_script': 5486, 'auto_script': 5583,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    source = make_state(width=7, height=7, start=(0, 6), objects=[target], frame=10)
    write_state(debug, source)
    debug.snapshot()
    assert debug.navigation_step(source, target_id=60)['ok'] is True
    debug.action_path.unlink()
    debug.pending_written_action = None

    dialog = dict(source)
    dialog['frame'] = 11
    dialog['dialog'] = {
        'active': True,
        'visible': True,
        'latest_event': {'sequence': 9, 'message_id': 956, 'text': '被你感動的．．'},
    }
    dialog['trigger_observation'] = {
        'generation': 1,
        'event_object_id': 60,
        'trigger_script': 5486,
        'completed': False,
        'source': {'scene_id': 3, 'map_id': 10, 'x': 96, 'y': 0},
    }
    write_state(debug, dialog)
    debug.snapshot()
    pending = debug.pending_transition_observation
    pending['control_last_progress_at'] -= 31

    debug.snapshot()

    assert debug.pending_transition_observation is pending
    assert debug.observations.get(pending['observation_id'])['status'] == 'pending'
    assert debug.navigation_step(dialog, target_id=60)['status'] == 'in_dialog'


def test_same_room_trigger_without_position_jump_is_not_recorded(tmp_path):
    portal = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    source = make_state(objects=[portal], frame=10)
    write_state(debug, source)
    debug.snapshot()
    triggered = make_state(start=(2, 2), objects=[portal], frame=11)
    triggered['trigger_observation'] = {
        'generation': 1, 'event_object_id': 48, 'trigger_script': 4631, 'completed': True,
        'source': {'scene_id': 3, 'map_id': 10, **source['party_position']},
        'destination': {'scene_id': 3, 'map_id': 10, **source['party_position'], 'frame': 10},
    }
    write_state(debug, triggered)
    debug.snapshot()
    assert debug.pending_transition_observation is None
    assert debug.read_exploration()['transitions'] == {}



def test_navigation_and_route_include_known_transition_destination(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    from_identity = debug.room_identity(state)
    to_state = make_state(objects=[])
    to_state['scene_id'] = 4
    to_state['map_id'] = 11
    to_state['room'] = {'key': 'next-room'}
    to_identity = debug.room_identity(to_state)
    transition_key = debug.transition_observation_from_state(state, 52)['transition_key']
    debug.record_transition_destination(from_identity, 52, to_identity, from_object=target, from_state=state, to_state=to_state)
    data = debug.read_exploration()
    room = debug.exploration_room(data, to_identity)
    room['visited'] = ['1,1', '2,2']
    room['frontier'] = ['3,3']
    room['explorable'] = 10
    debug.write_exploration(data)
    navigation = debug.run_agent_tool('get_navigation', {}, state)['navigation']
    target_status = {target['id']: target for target in navigation['targets']}
    route = debug.run_agent_tool('inspect_target', {'id': 52}, state)['route']
    known = debug.run_agent_tool('get_room_memory', {}, state)['transitions']
    known_by_key = {item['transition_key']: item for item in known['outgoing']}
    assert target_status[52]['known_destination']
    assert target_status[52]['known_destination_room_id'] == to_identity['id']
    assert target_status[52]['destination_exploration']['frontier'] == 1
    assert set(target_status[52]['destination_exploration']) == {
        'known', 'room_id', 'scene_id', 'map_id',
        'explored', 'explorable', 'frontier', 'updated_at',
    }
    assert target_status[52]['traversal_context']['status'] == 'no_active_tasks'
    assert target_status[52]['traversal_context']['total_count'] == 1
    assert route['known_destination']
    assert route['known_destination_room_id'] == to_identity['id']
    assert route['traversal_context']['status'] == 'no_active_tasks'
    assert set(route['destination_exploration']) == set(target_status[52]['destination_exploration'])
    assert len(known['outgoing']) == 1
    assert known_by_key[transition_key]['to_room_id'] == to_identity['id']
    assert known_by_key[transition_key]['exploration']['explored'] == 2
    assert known_by_key[transition_key]['traversal']['total_count'] == 1


def test_confirmed_search_transition_stays_search_across_agent_tools(tmp_path):
    target = {
        'id': 75, 'x': 16, 'y': 8, 'sprite_id': 1, 'state': 1,
        'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    destination = make_state(objects=[])
    destination.update({'scene_id': 4, 'map_id': 11, 'room': {'key': 'goal-room'}})
    record = debug.record_transition_destination(
        debug.room_identity(state), 75, debug.room_identity(destination),
        from_object=target, from_state=state, to_state=destination,
        confirmation='native_trigger',
    )
    advanced = dict(state)
    advanced['objects'] = [{**target, 'trigger_script': 8150}]

    navigation = debug.run_agent_tool('get_navigation', {}, advanced)['navigation']
    target_view = next(item for item in navigation['targets'] if item['id'] == 75)
    route = debug.run_agent_tool('inspect_target', {'id': 75}, advanced)['route']
    executable_route = debug.route_to_object(advanced, 75)
    memory = debug.run_agent_tool(
        'get_room_memory', {'room_id': debug.room_identity(state)['id']}, advanced,
    )['transitions']

    assert target_view['kind'] == 'search'
    assert target_view['status'] == 'transition'
    assert target_view['transition_candidate'] is True
    assert target_view['known_destination_room_id'] == debug.room_identity(destination)['id']
    assert route['kind'] == 'search'
    assert route['status'] == 'transition'
    assert route['transition_key'] == record['transition_key']
    assert executable_route['action'] == 'search'
    assert executable_route['status'] == 'transition'
    assert memory['outgoing'][0]['transition_key'] == record['transition_key']


def test_confirmed_search_transition_moves_before_starting_native_observer(tmp_path):
    target = {
        'id': 75, 'x': 48, 'y': 8, 'sprite_id': 1, 'state': 1,
        'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    debug = make_debug_state(tmp_path)
    state = make_state(width=6, height=4, start=(0, 3), objects=[target])
    destination = make_state(objects=[])
    destination.update({'scene_id': 4, 'map_id': 11, 'room': {'key': 'goal-room'}})
    debug.record_transition_destination(
        debug.room_identity(state), 75, debug.room_identity(destination),
        from_object=target, from_state=state, to_state=destination,
        confirmation='native_trigger',
    )
    write_state(debug, state)

    result = debug.navigation_step(state, target_id=75)

    assert result['status'] == 'stepping'
    assert result['action']['buttons'] == ['up', 'up']
    assert result.get('observation_id') is None
    assert debug.pending_transition_observation is None
    assert debug.navigation_transition_requests == {}


def test_generic_alternative_entrance_graph_learns_only_observed_directions(tmp_path):
    forward = make_touch_object(10, x=16, y=8, sprite_id=1)
    search = {
        'id': 75, 'x': 16, 'y': 8, 'sprite_id': 1, 'state': 1,
        'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0,
        'blocking': False, 'interactable': True,
    }
    outward = make_touch_object(20, x=16, y=8, sprite_id=1)
    debug = make_debug_state(tmp_path)
    room_a = make_state(objects=[forward])
    room_b = make_state(objects=[search])
    room_b.update({'scene_id': 4, 'map_id': 11, 'room': {'key': 'room-b'}})
    goal = make_state(objects=[outward])
    goal.update({'scene_id': 5, 'map_id': 12, 'room': {'key': 'goal'}})
    a_id = debug.room_identity(room_a)['id']
    b_id = debug.room_identity(room_b)['id']
    goal_id = debug.room_identity(goal)['id']
    debug.record_transition_destination(
        debug.room_identity(room_a), 10, debug.room_identity(room_b),
        from_object=forward, from_state=room_a, to_state=room_b,
        confirmation='native_trigger',
    )
    debug.record_transition_destination(
        debug.room_identity(goal), 20, debug.room_identity(room_b),
        from_object=outward, from_state=goal, to_state=room_b,
        confirmation='native_trigger',
    )

    goal_memory = debug.run_agent_tool('get_room_memory', {'room_id': goal_id}, room_a)
    b_memory_before = debug.run_agent_tool('get_room_memory', {'room_id': b_id}, room_b)
    search_before = next(
        item for item in debug.run_agent_tool('get_navigation', {}, room_b)['navigation']['targets']
        if item['id'] == 75
    )

    assert goal_memory['transitions']['outgoing'][0]['to_room_id'] == b_id
    assert b_memory_before['transitions']['outgoing'] == []
    assert {item['from_room_id'] for item in b_memory_before['transitions']['incoming']} == {a_id, goal_id}
    assert search_before['status'] == 'reachable'
    assert not search_before.get('transition_candidate', False)

    debug.record_transition_destination(
        debug.room_identity(room_b), 75, debug.room_identity(goal),
        from_object=search, from_state=room_b, to_state=goal,
        confirmation='native_trigger',
    )
    search_after = next(
        item for item in debug.run_agent_tool('get_navigation', {}, room_b)['navigation']['targets']
        if item['id'] == 75
    )
    b_memory_after = debug.run_agent_tool('get_room_memory', {'room_id': b_id}, room_b)

    assert a_id != b_id != goal_id
    assert search_after['kind'] == 'search'
    assert search_after['status'] == 'transition'
    assert search_after['known_destination_room_id'] == goal_id
    assert b_memory_after['transitions']['outgoing'][0]['to_room_id'] == goal_id


def test_transition_task_context_reopens_only_for_structural_task_changes(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    destination = make_state(objects=[])
    destination['scene_id'] = 4
    destination['map_id'] = 11
    from_identity = debug.room_identity(state)
    to_identity = debug.room_identity(destination)
    created = create_agent_task(debug, {
        'id': 'follow-clue', 'description': 'Follow the clue', 'rationale': 'Initial evidence.',
        'priority': 4,
    })['task']

    initial = debug.run_agent_tool('inspect_target', {'id': 52}, state)['route']['traversal_context']
    assert initial['status'] == 'untraversed_current_context'
    frozen = debug.transition_task_context_snapshot()
    debug.record_transition_destination(
        from_identity, 52, to_identity, from_object=target, from_state=state,
        to_state=destination, traversal_context_snapshot=frozen,
    )
    traversed = debug.run_agent_tool('inspect_target', {'id': 52}, state)['route']['traversal_context']
    assert traversed['status'] == 'traversed_current_context'
    assert traversed['current_context_count'] == 1

    debug.run_agent_tool('upsert_task', {'id': created['id'], 'rationale': 'Same task, clearer rationale.'})
    rationale_only = debug.run_agent_tool('inspect_target', {'id': 52}, state)['route']['traversal_context']
    assert rationale_only['context_digest'] == traversed['context_digest']
    assert rationale_only['status'] == 'traversed_current_context'

    debug.run_agent_tool('upsert_task', {'id': created['id'], 'description': 'Follow the rewritten clue'})
    rewritten = debug.run_agent_tool('inspect_target', {'id': 52}, state)['route']['traversal_context']
    assert rewritten['status'] == 'untraversed_current_context'

    debug.run_agent_tool('upsert_task', {'id': created['id'], 'status': 'completed'})
    assert debug.run_agent_tool('inspect_target', {'id': 52}, state)['route']['traversal_context']['status'] == 'no_active_tasks'
    debug.run_agent_tool('upsert_task', {'id': created['id'], 'status': 'active'})
    reopened = debug.run_agent_tool('inspect_target', {'id': 52}, state)['route']['traversal_context']
    assert reopened['status'] == 'untraversed_current_context'


def test_single_node_destination_with_zero_frontier_remains_normal_transition(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    destination = make_state(width=1, height=1, objects=[])
    destination['scene_id'] = 4
    destination['map_id'] = 11
    destination_identity = debug.room_identity(destination)
    debug.record_transition_destination(
        debug.room_identity(state), 52, destination_identity,
        from_object=target, from_state=state, to_state=destination,
    )
    data = debug.read_exploration()
    room = debug.exploration_room(data, destination_identity)
    room.update({'visited': ['0,0'], 'explorable': 1, 'frontier': []})
    debug.write_exploration(data)

    route = debug.run_agent_tool('inspect_target', {'id': 52}, state)['route']

    assert route['known_destination']
    assert route['destination_exploration']['explored'] == 1
    assert route['destination_exploration']['explorable'] == 1
    assert route['destination_exploration']['frontier'] == 0
    assert 'complete' not in route['destination_exploration']
    assert 'destination_priority' not in route


def test_transition_uses_frozen_pre_trigger_task_context(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    destination = make_state(objects=[])
    destination['scene_id'] = 4
    destination['map_id'] = 11
    task = create_agent_task(debug, {
        'id': 'route-task', 'description': 'Use the old route context', 'rationale': 'Evidence.',
    })['task']
    frozen = debug.transition_task_context_snapshot()
    cycle = debug.new_agent_tool_cycle(state)
    debug.run_agent_tool(
        'get_room_memory', {'room_id': 'S4/M11:000000000001'}, state, tool_cycle=cycle,
    )
    debug.run_agent_tool(
        'upsert_task', {'id': task['id'], 'room_id': 'S4/M11:000000000001'},
        state, tool_cycle=cycle,
    )

    debug.record_transition_destination(
        debug.room_identity(state), 52, debug.room_identity(destination),
        from_object=target, from_state=state, to_state=destination,
        traversal_context_snapshot=frozen,
    )

    evidence = debug.run_agent_tool('inspect_target', {'id': 52}, state)['route']['traversal_context']
    assert evidence['status'] == 'untraversed_current_context'
    stored = next(iter(debug.read_exploration()['transitions'][debug.room_identity(state)['id']].values()))
    assert stored['traversal_count'] == 1
    assert stored['traversal_contexts'][0]['digest'] == frozen['digest']


def test_transition_keeps_only_sixteen_recent_task_contexts(tmp_path):
    debug = make_debug_state(tmp_path)
    record = {'observed': True, 'traversal_count': 0}
    for index in range(18):
        debug.record_transition_traversal(
            record,
            {'digest': f'context-{index}', 'active_task_ids': [f'task-{index}']},
            float(index + 1),
        )

    assert record['traversal_count'] == 18
    assert len(record['traversal_contexts']) == 16
    assert record['traversal_contexts'][0]['digest'] == 'context-2'
    assert record['traversal_contexts'][-1]['digest'] == 'context-17'


def test_get_room_memory_compacts_transition_evidence_for_agent(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    destination = make_state(objects=[])
    destination['scene_id'] = 4
    destination['map_id'] = 11
    record = debug.record_transition_destination(
        debug.room_identity(state),
        52,
        debug.room_identity(destination),
        from_object=target,
        from_state=state,
        to_state=destination,
    )
    data = debug.read_exploration()
    stored = data['transitions'][debug.room_identity(state)['id']][record['transition_key']]
    stored['observed_from_state'] = {'oversized_evidence': 'from-state-' * 4000}
    stored['observed_to_state'] = {'oversized_evidence': 'to-state-' * 4000}
    debug.write_exploration(data)

    full = debug.known_transitions_summary(debug.room_identity(state)['id'], state)
    memory = debug.run_agent_tool('get_room_memory', {}, state)
    encoded = json.dumps(memory, ensure_ascii=False)
    transition = memory['transitions']['outgoing'][0]

    assert 'oversized_evidence' in full['transitions'][0]['observed_from_state']
    assert set(memory['transitions']) == {'outgoing', 'incoming'}
    assert transition['transition_key'] == record['transition_key']
    assert 'object' not in transition
    assert 'known_transitions' not in encoded
    assert 'known_transitions_by_object' not in encoded
    assert 'observed_from_state' not in encoded
    assert 'observed_to_state' not in encoded
    assert 'legacy_room_id' not in encoded
    assert 'legacy_nav_room_id' not in encoded
    assert 'oversized_evidence' not in encoded
    assert 'context_digest' not in encoded
    assert 'active_task_ids' not in encoded
    assert len(encoded) < 5000


def test_room_memory_exposes_scoped_dialog_evidence_and_confirmed_incoming_edges(tmp_path):
    debug = make_debug_state(tmp_path)
    village = make_state(objects=[make_touch_object(95, x=16, y=8, sprite_id=1)])
    village.update({'scene_id': 2, 'map_id': 9, 'room': {'key': 'village'}})
    inn = make_state(objects=[])
    inn.update({'scene_id': 3, 'map_id': 10, 'room': {'key': 'inn'}})
    lane = make_state(objects=[make_touch_object(99, x=32, y=8, sprite_id=1)])
    lane.update({'scene_id': 7, 'map_id': 11, 'room': {'key': 'lane'}})
    rumor = make_state(objects=[make_touch_object(77, x=48, y=8, sprite_id=1)])
    rumor.update({'scene_id': 6, 'map_id': 10, 'room': {'key': 'rumor'}})
    home = make_state(objects=[])
    home.update({'scene_id': 8, 'map_id': 11, 'room': {'key': 'home'}})
    village_id = debug.room_identity(village)['id']
    inn_id = debug.room_identity(inn)['id']
    lane_id = debug.room_identity(lane)['id']
    home_id = debug.room_identity(home)['id']
    debug.record_transition_destination(
        debug.room_identity(village), 95, debug.room_identity(inn),
        from_object=village['objects'][0], from_state=village, to_state=inn,
        confirmation='native_trigger',
    )
    debug.record_transition_destination(
        debug.room_identity(lane), 99, debug.room_identity(home),
        from_object=lane['objects'][0], from_state=lane, to_state=home,
        confirmation='native_trigger',
    )
    debug.record_transition_destination(
        debug.room_identity(rumor), 77, debug.room_identity(inn),
        from_object=rumor['objects'][0], from_state=rumor, to_state=inn,
        confirmation='inferred',
    )
    debug.dialog_history = [
        {'frame': 10, 'end_frame': 11, 'logical_room_id': inn_id, 'event_object_id': 25,
         'message_ids': [100, 101], 'speaker_hint': '李大娘', 'text': '这里是客栈。'},
        {'frame': 12, 'logical_room_id': inn_id, 'event_object_id': 25,
         'message_id': 102, 'speaker_hint': '系统', 'text': '得到５００文钱'},
        {'frame': 13, 'logical_room_id': inn_id, 'event_object_id': 26,
         'message_id': 103, 'dialog_position': 'center_window', 'text': '中心提示'},
        {'frame': 20, 'logical_room_id': home_id, 'event_object_id': 181,
         'message_id': 200, 'speaker_hint': '老王', 'text': '阿珠在这里。'},
        {'frame': 21, 'logical_room_id': home_id, 'event_object_id': 88,
         'message_id': 201, 'speaker_hint': '阿珠', 'text': '咕．．咕．．'},
        {'frame': 22, 'logical_room_id': home_id, 'event_object_id': 181,
         'message_id': 202, 'text': '这孩子又不知跑到哪里玩去了。'},
    ]

    inn_memory = debug.run_agent_tool('get_room_memory', {'room_id': inn_id}, village)
    home_memory = debug.run_agent_tool('get_room_memory', {'room_id': home_id}, village)

    assert inn_memory['transitions']['outgoing'] == []
    assert [(item['from_room_id'], item['object_id']) for item in inn_memory['transitions']['incoming']] == [
        (village_id, 95),
    ]
    assert home_memory['transitions']['outgoing'] == []
    assert [(item['from_room_id'], item['object_id']) for item in home_memory['transitions']['incoming']] == [
        (lane_id, 99),
    ]
    assert inn_memory['semantic_evidence']['dialog_turns_total'] == 1
    assert inn_memory['semantic_evidence']['participants'][0]['speaker_hint'] == '李大娘'
    assert home_memory['semantic_evidence']['dialog_turns_total'] == 3
    assert {item.get('speaker_hint') for item in home_memory['semantic_evidence']['participants']} == {
        '老王', '阿珠', None,
    }
    assert home_memory['semantic_evidence']['speaker_hint_is_metadata'] is True
    assert any(
        sample['text'].startswith('这孩子')
        for group in home_memory['semantic_evidence']['samples'].values()
        for sample in group
    )
    assert all(
        sample['text'] not in {'得到５００文钱', '中心提示'}
        for group in inn_memory['semantic_evidence']['samples'].values()
        for sample in group
    )
    assert all(item['from_room_id'] != inn_id for item in inn_memory['transitions']['incoming'])
    assert all(item['from_room_id'] != home_id for item in home_memory['transitions']['incoming'])


def test_room_memory_with_seven_edges_stays_under_five_kilobytes_without_redundant_fields(tmp_path):
    targets = [
        make_touch_object(50 + index, x=16 * index, y=8, sprite_id=1)
        for index in range(7)
    ]
    debug = make_debug_state(tmp_path)
    state = make_state(width=10, height=4, objects=targets)
    source_identity = debug.room_identity(state)
    for index, target in enumerate(targets):
        destination = make_state(objects=[])
        destination.update({
            'scene_id': 10 + index,
            'map_id': 20 + index,
            'room': {'key': f'destination-{index}'},
        })
        debug.record_transition_destination(
            source_identity, target['id'], debug.room_identity(destination),
            from_object=target, from_state=state, to_state=destination,
            confirmation='native_trigger',
        )
    debug.dialog_history = [
        {'frame': frame, 'logical_room_id': source_identity['id'], 'event_object_id': 70 + frame,
         'message_id': 300 + frame, 'speaker_hint': f'人物{frame}', 'text': '地点证据' * 12}
        for frame in range(1, 10)
    ]

    memory = debug.run_agent_tool('get_room_memory', {}, state)
    encoded = json.dumps(memory, ensure_ascii=False, separators=(',', ':'))

    assert len(memory['transitions']['outgoing']) == 7
    assert memory['semantic_evidence']['dialog_turns_total'] == 9
    assert len(memory['semantic_evidence']['participants']) == 8
    assert list(memory['semantic_evidence']['samples']) == ['latest']
    assert [item['frame'] for item in memory['semantic_evidence']['samples']['latest']] == [7, 8, 9]
    assert len(encoded.encode('utf-8')) < 5000
    for forbidden in (
        'context_digest', 'active_task_ids', 'observed_at', 'updated_at',
        'trigger_script', 'trigger_mode', '"x"', '"y"', 'destination_exploration',
    ):
        assert forbidden not in encoded
    def contains_null(value):
        if isinstance(value, dict):
            return any(contains_null(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_null(item) for item in value)
        return value is None

    assert not contains_null(memory['transitions'])



def test_transition_destination_is_known_across_regions_in_same_room(tmp_path):
    target = {'id': 54, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    observed_from = make_state(objects=[target])
    observed_from['region_key'] = '3:10:17:48'
    query_from = make_state(objects=[target])
    query_from['region_key'] = '3:10:16:52'
    to_state = make_state(objects=[])
    to_state['region_key'] = '3:10:16:47'
    to_identity = debug.room_identity(to_state)
    debug.record_transition_destination(debug.room_identity(observed_from), 54, to_identity, from_object=target, from_state=observed_from, to_state=to_state)
    route = debug.run_agent_tool('inspect_target', {'id': 54}, query_from)['route']
    known = debug.run_agent_tool('get_room_memory', {}, query_from)['transitions']
    assert route['known_destination']
    assert route['known_destination_room_id'] == to_identity['id']
    assert known['outgoing'][0]['to_room_id'] == to_identity['id']



def test_transition_destination_crosses_raw_room_key_when_logical_room_matches(tmp_path):
    target = {'id': 54, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    observed_from = make_state(objects=[target])
    query_from = make_state(objects=[target])
    query_from['room'] = {'key': 'other-room'}
    to_state = make_state(objects=[])
    to_state['scene_id'] = 4
    to_state['map_id'] = 11
    to_state['room'] = {'key': 'next-room'}
    debug.record_transition_destination(debug.room_identity(observed_from), 54, debug.room_identity(to_state), from_object=target, from_state=observed_from, to_state=to_state)
    route = debug.run_agent_tool('inspect_target', {'id': 54}, query_from)['route']
    known = debug.run_agent_tool('get_room_memory', {}, query_from)['transitions']
    assert route['known_destination']
    assert known['outgoing']


def test_historical_room_memory_does_not_validate_against_current_room_object(tmp_path):
    historical_target = {'id': 54, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    current_target = {**historical_target, 'x': 64, 'trigger_script': 9999}
    debug = make_debug_state(tmp_path)
    historical = make_state(objects=[historical_target])
    destination = make_state(objects=[])
    destination['scene_id'] = 5
    destination['map_id'] = 12
    current = make_state(objects=[current_target])
    current['scene_id'] = 4
    current['map_id'] = 11
    historical_identity = debug.room_identity(historical)
    destination_identity = debug.room_identity(destination)
    record = debug.record_transition_destination(
        historical_identity,
        54,
        destination_identity,
        from_object=historical_target,
        from_state=historical,
        to_state=destination,
    )

    memory = debug.run_agent_tool(
        'get_room_memory',
        {'room_id': historical_identity['id']},
        current,
    )

    assert memory['room_id'] == historical_identity['id']
    assert len(memory['transitions']['outgoing']) == 1
    assert memory['transitions']['outgoing'][0]['transition_key'] == record['transition_key']
    assert memory['transitions']['outgoing'][0]['to_room_id'] == destination_identity['id']


def test_transition_destination_requires_exact_position_and_script_key(tmp_path):
    observed_target = {'id': 54, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    query_target = {**observed_target, 'x': 64}
    debug = make_debug_state(tmp_path)
    observed_from = make_state(objects=[observed_target])
    query_from = make_state(objects=[query_target])
    to_state = make_state(objects=[])
    to_state['scene_id'] = 4
    to_state['map_id'] = 11
    to_state['room'] = {'key': 'next-room'}
    debug.record_transition_destination(
        debug.room_identity(observed_from),
        54,
        debug.room_identity(to_state),
        from_object=observed_target,
        from_state=observed_from,
        to_state=to_state,
    )
    route = debug.run_agent_tool('inspect_target', {'id': 54}, query_from)['route']
    known = debug.run_agent_tool('get_room_memory', {}, query_from)['transitions']
    assert not route['known_destination']
    assert known['outgoing'] == []


def test_transition_semantic_annotation_is_exposed_to_agent_navigation(tmp_path):
    target = {'id': 54, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    to_state = make_state(objects=[])
    to_state['room'] = {'key': 'kitchen'}
    to_identity = debug.room_identity(to_state)
    record = debug.record_transition_destination(
        debug.room_identity(state),
        54,
        to_identity,
        from_object=target,
        from_state=state,
        to_state=to_state,
    )
    debug.annotate_transition(record['transition_key'], from_semantic_room='一楼大堂', to_semantic_room='厨房')
    navigation = debug.run_agent_tool('get_navigation', {}, state)['navigation']
    route = debug.run_agent_tool('inspect_target', {'id': 54}, state)['route']
    known = debug.run_agent_tool('get_room_memory', {}, state)['transitions']
    known_by_key = {item['transition_key']: item for item in known['outgoing']}
    target_status = {target['id']: target for target in navigation['targets']}
    assert target_status[54]['to_semantic_room'] == '厨房'
    assert target_status[54]['confidence'] == 'human_confirmed'
    assert route['to_semantic_room'] == '厨房'
    assert known_by_key[record['transition_key']]['from_semantic_room'] == '一楼大堂'
    assert known_by_key[record['transition_key']]['to_semantic_room'] == '厨房'



def test_unknown_transition_destination_is_explicit(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    route = debug.run_agent_tool('inspect_target', {'id': 52}, state)['route']
    assert not route['known_destination']
    assert route['known_destination_room_id'] is None
    assert route['known_destination_observed_at'] is None
    assert not route['destination_exploration']['known']
    assert route['traversal_context']['status'] == 'no_active_tasks'
    assert route['traversal_context']['total_count'] == 0


def test_legacy_transition_record_is_read_but_not_used_as_a_known_destination(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    destination = make_state(objects=[])
    destination['scene_id'] = 4
    destination['map_id'] = 11
    record = debug.record_transition_destination(
        debug.room_identity(state), 52, debug.room_identity(destination),
        from_object=target, from_state=state, to_state=destination,
    )
    data = debug.read_exploration()
    legacy = data['transitions'][debug.room_identity(state)['id']][record['transition_key']]
    legacy.pop('confirmation')
    legacy.pop('traversal_count')
    legacy.pop('last_traversed_at')
    legacy.pop('traversal_context_version')
    debug.write_exploration(data)
    route = debug.run_agent_tool('inspect_target', {'id': 52}, state)['route']
    known = debug.run_agent_tool('get_room_memory', {}, state)['transitions']
    known_by_key = {item['transition_key']: item for item in known['outgoing']}
    assert not route['known_destination']
    assert known_by_key[record['transition_key']]['unconfirmed_destination']
    assert route['traversal_context']['total_count'] == 1
    assert route['traversal_context']['status'] == 'legacy_observed'



def test_recent_transition_destination_is_marked_as_backtrack(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    previous = make_state(objects=[])
    previous['scene_id'] = 1
    previous['map_id'] = 12
    previous['room'] = {'key': 'previous-room'}
    previous_identity = debug.room_identity(previous)
    transition_key = debug.transition_observation_from_state(state, 52)['transition_key']
    debug.record_transition_destination(debug.room_identity(state), 52, previous_identity, from_object=target, from_state=state, to_state=previous)
    debug.agent_log_path.write_text(json.dumps({'turn': 1, 'episode': previous_identity['id'], 'decision': {}}) + '\n', encoding='utf-8')
    navigation = debug.run_agent_tool('get_navigation', {}, state)['navigation']
    target_status = {target['id']: target for target in navigation['targets']}
    known = debug.run_agent_tool('get_room_memory', {}, state)['transitions']
    known_by_key = {item['transition_key']: item for item in known['outgoing']}
    assert target_status[52]['returns_to_recent_room']
    assert target_status[52]['agent_priority'] == 'avoid_backtrack'
    assert known_by_key[transition_key]['returns_to_recent_room']
    assert 'agent_priority' not in known_by_key[transition_key]



def test_rules_prefer_unknown_transition_over_recent_backtrack(tmp_path):
    backtrack = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    unknown = {'id': 53, 'x': 64, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4639, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(width=5, height=3, objects=[backtrack, unknown])
    write_state(debug, state)
    previous = make_state(objects=[])
    previous['scene_id'] = 1
    previous['map_id'] = 12
    previous['room'] = {'key': 'previous-room'}
    previous_identity = debug.room_identity(previous)
    debug.record_transition_destination(debug.room_identity(state), 52, previous_identity, from_object=backtrack, from_state=state, to_state=previous)
    debug.agent_log_path.write_text(json.dumps({'turn': 1, 'episode': previous_identity['id'], 'decision': {}}) + '\n', encoding='utf-8')
    decision = debug.rules_decide_agent(state)
    assert decision['recommended_intent'] == 'enter_transition'
    assert decision['recommended_action']['args']['id'] == 53
    assert not decision['evidence']['target']['destination_exploration']['known']
    assert decision['evidence']['target']['traversal_context']['total_count'] == 0



def test_dialog_stops_navigation_step(tmp_path):
    target = {'id': 48, 'x': 32, 'y': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    state['dialog'] = {'active': True}
    write_state(debug, state)
    result = debug.navigation_step(state, target_id=48)
    assert not result['ok']
    assert result['status'] == 'in_dialog'
    assert not debug.action_path.exists()



def test_agent_decide_recommends_reachable_non_transition_target(tmp_path):
    target = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 21, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    write_state(debug, state)
    decision = debug.rules_decide_agent(state)
    assert decision['recommended_intent'] == 'inspect_target'
    assert decision['recommended_action']['tool'] == 'navigation_step'
    assert decision['recommended_action']['args'] == {'id': 48}
    assert decision['evidence']['target']['id'] == 48
    assert not debug.action_path.exists()



def test_agent_decide_warns_on_transition_target(tmp_path):
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    write_state(debug, state)
    decision = debug.rules_decide_agent(state)
    assert decision['recommended_intent'] == 'enter_transition'
    assert decision['recommended_action']['tool'] == 'navigation_step'
    assert decision['recommended_action']['args'] == {'id': 52}
    assert '离开当前房间' in decision['risk']
    assert decision['evidence']['target']['id'] == 52
    assert not debug.action_path.exists()



def test_agent_expands_navigation_submission_to_navigation_step(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[{'id': 10, 'trigger_mode': 1, 'state': 1}])
    decision = debug.normalize_agent_submission({'action': 'navigate', 'target_id': 10, 'reason': 'Inspect target.'}, state)
    assert decision['recommended_action']['tool'] == 'navigation_step'
    assert decision['recommended_action']['args'] == {'id': 10}
    assert decision['recommended_intent'] == 'inspect_target'
    assert not debug.action_path.exists()


def test_agent_binds_approach_navigation_to_item_requirement(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(width=8, height=8, start=(7, 7), objects=[approach_object()])
    state['inventory'] = [approach_item()]
    write_state(debug, state)

    decision = debug.normalize_agent_submission(
        {'action': 'navigate', 'target_id': 238, 'item_id': 279, 'reason': 'Approach before using the hammer.'},
        state,
    )
    assert decision['recommended_intent'] == 'approach_target'
    assert decision['recommended_action']['args'] == {'id': 238, 'item_id': 279}
    assert decision['evidence']['approach']['event_object_id'] == 238
    assert debug.validate_agent_decision(decision)['recommended_action']['tool'] == 'navigation_step'


def test_agent_keeps_explicit_item_for_search_target_affordance(tmp_path):
    debug = make_debug_state(tmp_path)
    target = approach_object()
    target['trigger_mode'] = 1
    state = make_state(width=8, height=8, start=(7, 7), objects=[target])
    state['inventory'] = [approach_item()]

    decision = debug.normalize_agent_submission(
        {'action': 'navigate', 'target_id': 238, 'item_id': 279, 'reason': 'Use the item approach.'},
        state,
    )

    assert decision['recommended_intent'] == 'approach_target'
    assert decision['recommended_action']['args'] == {'id': 238, 'item_id': 279}
    assert decision['evidence']['target']['kind'] == 'search'
    assert decision['evidence']['affordances'][0]['item_id'] == 279



def test_navigation_applies_dialog_feedback_across_regions_in_same_nav_room(tmp_path):
    target = {'id': 25, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    state['region_key'] = '3:10:upper-region'
    debug.dialog_history = [{'frame': 50, 'room_id': '3/10/test-room/3:10:lower-region', 'room_key': 'test-room', 'region_key': '3:10:lower-region', 'nav_room_id': '3/10/test-room', 'scene_id': 3, 'map_id': 10, 'event_object_id': 25, 'message_id': 588, 'speaker_hint': '李大娘', 'text': '呵～累了，想早點休息'}]
    navigation = debug.run_agent_tool('get_navigation', {}, state)['navigation']
    targets = {item['id']: item for item in navigation['targets']}
    assert targets[25]['interaction_state'] == 'recent_dialog'
    assert targets[25]['agent_priority'] == 'avoid_repeat'
    assert targets[25]['dialog_feedback_state'] == 'recent_dialog'
    assert targets[25]['recent_dialog_feedback'][0]['text'] == '呵～累了，想早點休息'



def test_navigation_marks_same_full_room_object_with_recent_dialog(tmp_path):
    target = {'id': 57, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    identity = debug.room_identity(state)
    debug.dialog_history = [{'frame': 50, 'room_id': identity['id'], 'room_key': identity['room_key'], 'region_key': identity['region_key'], 'nav_room_id': identity['nav_room_id'], 'scene_id': identity['scene_id'], 'map_id': identity['map_id'], 'event_object_id': 57, 'message_id': 588, 'speaker_hint': '李大娘', 'text': '呵～累了，想早點休息', 'agent_turn': 1}]
    debug.agent_log_path.write_text(json.dumps({'turn': 1, 'episode': identity['id'], 'nav_room_id': identity['nav_room_id'], 'decision': {}}) + '\n', encoding='utf-8')
    navigation = debug.run_agent_tool('get_navigation', {}, state)['navigation']
    targets = {item['id']: item for item in navigation['targets']}
    assert targets[57]['interaction_state'] == 'recent_dialog'
    assert targets[57]['agent_priority'] == 'avoid_repeat'
    assert targets[57]['dialog_feedback_state'] == 'recent_dialog'
    assert targets[57]['recent_dialog_feedback'][0]['text'] == '呵～累了，想早點休息'


def test_navigation_marks_same_object_in_different_nav_room_as_stale(tmp_path):
    target = {'id': 25, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    state['room'] = {'key': 'upper-room'}
    identity = debug.room_identity(state)
    debug.dialog_history = [{'frame': 50, 'room_id': '3/10/lower-room/3:10:lower-region', 'room_key': 'lower-room', 'region_key': '3:10:lower-region', 'nav_room_id': '3/10/lower-room', 'scene_id': identity['scene_id'], 'map_id': identity['map_id'], 'event_object_id': 25, 'message_id': 588, 'speaker_hint': '李大娘', 'text': '呵～累了，想早點休息'}]
    navigation = debug.run_agent_tool('get_navigation', {}, state)['navigation']
    targets = {item['id']: item for item in navigation['targets']}
    assert targets[25]['interaction_state'] == 'fresh'
    assert targets[25]['agent_priority'] == 'normal'
    assert targets[25]['dialog_feedback_state'] == 'stale_different_room'
    assert 'recent_dialog_feedback' not in targets[25]



def test_navigation_does_not_apply_dialog_feedback_from_different_room(tmp_path):
    target = {'id': 25, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    debug.dialog_history = [{'frame': 50, 'room_key': 'other-room', 'scene_id': 3, 'map_id': 10, 'event_object_id': 25, 'message_id': 588, 'speaker_hint': '李大娘', 'text': '呵～累了，想早點休息'}]
    navigation = debug.run_agent_tool('get_navigation', {}, state)['navigation']
    targets = {item['id']: item for item in navigation['targets']}
    assert targets[25]['interaction_state'] == 'fresh'
    assert targets[25]['agent_priority'] == 'normal'
    assert 'recent_dialog_feedback' not in targets[25]



def test_agent_context_keeps_story_dialog_and_transition_candidates(tmp_path):
    transition = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    search = {'id': 10, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[transition, search])
    identity = debug.room_identity(state)
    dialogs = {'dialogs': [{'frame': 50, 'scene_id': identity['scene_id'], 'map_id': identity['map_id'], 'room_id': identity['id'], 'room_key': identity['room_key'], 'region_key': identity['region_key'], 'nav_room_id': identity['nav_room_id'], 'event_object_id': 10, 'message_id': 588, 'speaker_hint': '李大娘', 'text': '還不快出來幫忙招呼客人'}, {'frame': 51, 'scene_id': identity['scene_id'], 'map_id': identity['map_id'], 'room_id': identity['id'], 'room_key': identity['room_key'], 'region_key': identity['region_key'], 'nav_room_id': identity['nav_room_id'], 'event_object_id': 10, 'message_id': 590, 'speaker_hint': '李逍遙', 'text': '啊！\u3000．．我馬上就去'}]}
    context = debug.agent_context(state, debug.navigation(state), dialogs, events=[])
    targets = {item['id']: item for item in context['navigation']['targets']}
    assert targets[52]['transition_candidate']
    assert targets[10]['recent_dialog_feedback'][0]['text'] == '還不快出來幫忙招呼客人'
    assert context['dialogs'][-1]['text'] == '啊！\u3000．．我馬上就去'
    assert 'rules' not in context
