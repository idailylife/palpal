import json
import tempfile

from conftest import make_debug_state, make_state, make_touch_object, write_state
from palpal.debug_server import decode_nav_grid, nav_bit, pal_distance, trigger_radius, world_pos



def test_navigation_step_sends_first_move_to_reachable_target():
    target = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 21, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[target])
        write_state(debug, state)
        result = debug.navigation_step(state, target_id=48)
        assert result['ok']
        assert result['status'] == 'stepping'
        assert result['action']['buttons'] == ['up']
        assert debug.action_path.exists()



def test_navigation_step_sends_straight_segment_until_turn():
    target = {'id': 48, 'x': 64, 'y': 0, 'sprite_id': 21, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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



def test_navigation_step_sends_straight_transition_segment():
    target = {'id': 52, 'x': 64, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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



def test_navigation_step_interacts_with_search_target_at_current_node():
    target = {'id': 63, 'x': 32, 'y': 0, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(start=(2, 0), objects=[target])
        write_state(debug, state)
        result = debug.navigation_step(state, target_id=63)
        assert result['ok']
        assert result['status'] == 'interacting'
        assert result['action']['buttons'] == ['search']
        assert debug.action_path.exists()



def test_navigation_step_stops_when_target_becomes_inactive():
    target = {'id': 63, 'x': 32, 'y': 0, 'state': 0, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': False}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(start=(2, 0), objects=[target])
        write_state(debug, state)
        result = debug.navigation_step(state, target_id=63)
        assert not result['ok']
        assert result['status'] == 'target_inactive'
        assert not debug.action_path.exists()



def test_navigation_step_faces_search_target_when_already_at_goal():
    target = {'id': 10, 'x': 32, 'y': 0, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(start=(1, 1), objects=[target])
        state['party_direction'] = 3
        write_state(debug, state)
        result = debug.navigation_step(state, target_id=10)
        assert result['ok']
        assert result['status'] == 'facing'
        assert result['action']['buttons'] == ['up']
        assert result['required_direction'] == 'up'
        assert debug.action_path.exists()



def test_route_reports_interactable_blocker():
    door = {'id': 74, 'x': 16, 'y': 8, 'state': 2, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': True, 'interactable': True}
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 21, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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



def test_navigation_response_identifies_environment_without_animation_frame():
    target = make_touch_object(52, x=32, y=0, sprite_id=21)
    target['current_frame'] = 1
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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


def test_navigation_attaches_logical_room_once_for_raw_state(monkeypatch):
    target = make_touch_object(52, x=32, y=0, sprite_id=0)
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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


def test_lightweight_navigation_does_not_run_full_target_decoration(monkeypatch):
    target = make_touch_object(52, x=32, y=0, sprite_id=0)
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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


def test_touch_boundary_nodes_match_bruteforce_scan():
    target = make_touch_object(52, x=77, y=41, sprite_id=0)
    target["trigger_mode"] = 5
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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



def test_navigation_step_interacts_with_blocking_door():
    door = {'id': 74, 'x': 16, 'y': 8, 'state': 2, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': True, 'interactable': True}
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 21, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(width=3, height=3, start=(0, 2), objects=[door, target])
        write_state(debug, state)
        result = debug.navigation_step(state, target_id=52)
        assert result['ok']
        assert result['status'] == 'interacting'
        assert result['blocker']['id'] == 74
        assert result['action']['buttons'] == ['up', 'search']
        assert debug.action_path.exists()



def test_invisible_touch_target_is_reported_as_transition():
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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



def test_navigation_transition_records_observed_destination():
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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
        assert record['from_subregion_id'] is None
        assert record['to_subregion_id'] is None
        assert record['from_location_id'] == from_identity['id']
        assert record['to_location_id'] == to_identity['id']


def test_navigation_step_keeps_pending_transition_until_destination_is_stable():
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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


def test_navigation_step_does_not_discard_changed_room_observation_without_matching_trigger():
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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


def test_navigation_step_waits_for_pending_transition_when_target_is_unreachable_after_landing():
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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


def test_navigation_step_replans_after_pending_transition_times_out():
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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


def test_navigation_step_confirms_delayed_intra_room_transition_after_landing():
    target = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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


def test_transition_observation_ignores_logical_room_change_without_native_trigger():
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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


def test_transition_observation_requires_matching_native_object_and_stable_destination():
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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


def test_manual_movement_records_native_transition_after_stable_destination():
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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


def test_manual_movement_ignores_native_trigger_with_mismatched_source_scene():
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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


def test_intra_room_transition_freezes_source_and_uses_nearest_portal():
    source_portal = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    target_portal = {**source_portal, 'id': 47, 'x': 48, 'y': 8}
    tied_portal = {**source_portal, 'id': 49, 'x': 48, 'y': 8}
    inactive_portal = {**source_portal, 'id': 46, 'x': 48, 'y': 8, 'state': 0}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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
        assert record['from_subregion_id'] == '.48'
        assert record['to_subregion_id'] == '.47'
        assert record['from_location_id'] == f"{source_identity['id']}.48"
        assert record['to_location_id'] == f"{source_identity['id']}.47"
        assert record['subregion_basis'] == 'native_trigger_destination_nearest_active_transition'
        assert record['subregion_match_distance'] == 0
        assert record['native_destination']['x'] == 48
        assert record['observed_to_state']['party_position'] == {'x': 80, 'y': 16}

        known = debug.known_transitions_summary(source_identity['id'], destination)['transitions'][0]
        assert known['intra_room_transition'] is True
        assert known['from_location_id'].endswith('.48')
        assert known['to_location_id'].endswith('.47')
        assert known['returns_to_recent_room'] is False


def test_same_room_trigger_without_position_jump_is_not_recorded():
    portal = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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



def test_navigation_and_route_include_known_transition_destination():
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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
        room['complete'] = False
        debug.write_exploration(data)
        navigation = debug.run_agent_tool('get_navigation', {}, state)['navigation']
        target_status = {target['id']: target for target in navigation['targets']}
        route = debug.run_agent_tool('inspect_target', {'id': 52}, state)['route']
        known = debug.run_agent_tool('get_room_memory', {}, state)['transitions']
        assert target_status[52]['known_destination']
        assert target_status[52]['known_destination_room_id'] == to_identity['id']
        assert target_status[52]['destination_priority'] == 'partial'
        assert target_status[52]['destination_exploration']['frontier'] == 1
        assert route['known_destination']
        assert route['known_destination_room_id'] == to_identity['id']
        assert route['destination_priority'] == 'partial'
        assert known['from_nav_room_id'] == from_identity['nav_room_id']
        assert known['known_transitions'][transition_key]['to_room_id'] == to_identity['id']
        assert known['known_transitions'][transition_key]['to_nav_room_id'] == to_identity['nav_room_id']
        assert known['known_transitions'][transition_key]['destination_priority'] == 'partial'
        assert known['known_transitions'][transition_key]['destination_exploration']['explored'] == 2
        assert known['known_transitions_by_object']['52'][0]['transition_key'] == transition_key



def test_transition_destination_is_known_across_regions_in_same_room():
    target = {'id': 54, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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
        assert known['from_nav_room_id'] == debug.room_identity(query_from)['nav_room_id']
        assert known['transitions'][0]['from_room_id'] == debug.room_identity(observed_from)['id']
        assert known['transitions'][0]['to_room_id'] == to_identity['id']



def test_transition_destination_crosses_raw_room_key_when_logical_room_matches():
    target = {'id': 54, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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
        assert known['known_transitions']


def test_transition_destination_requires_exact_position_and_script_key():
    observed_target = {'id': 54, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    query_target = {**observed_target, 'x': 64}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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
        assert known['known_transitions_by_object'] == {}


def test_transition_semantic_annotation_is_exposed_to_agent_navigation():
    target = {'id': 54, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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
        target_status = {target['id']: target for target in navigation['targets']}
        assert target_status[54]['to_semantic_room'] == '厨房'
        assert target_status[54]['confidence'] == 'human_confirmed'
        assert route['to_semantic_room'] == '厨房'
        assert known['known_transitions'][record['transition_key']]['from_semantic_room'] == '一楼大堂'
        assert known['known_transitions'][record['transition_key']]['to_semantic_room'] == '厨房'



def test_unknown_transition_destination_is_explicit():
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[target])
        route = debug.run_agent_tool('inspect_target', {'id': 52}, state)['route']
        assert not route['known_destination']
        assert route['known_destination_room_id'] is None
        assert route['known_destination_observed_at'] is None
        assert route['destination_priority'] == 'unknown'
        assert not route['destination_exploration']['known']


def test_legacy_transition_record_is_read_but_not_used_as_a_known_destination():
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[target])
        destination = make_state(objects=[])
        destination['scene_id'] = 4
        destination['map_id'] = 11
        record = debug.record_transition_destination(
            debug.room_identity(state), 52, debug.room_identity(destination),
            from_object=target, from_state=state, to_state=destination,
        )
        data = debug.read_exploration()
        data['transitions'][debug.room_identity(state)['id']][record['transition_key']].pop('confirmation')
        debug.write_exploration(data)
        route = debug.run_agent_tool('inspect_target', {'id': 52}, state)['route']
        known = debug.run_agent_tool('get_room_memory', {}, state)['transitions']
        assert not route['known_destination']
        assert route['destination_priority'] == 'unknown'
        assert known['known_transitions'][record['transition_key']]['unconfirmed_destination']
        assert known['known_transitions'][record['transition_key']]['destination_priority'] == 'unknown'



def test_recent_transition_destination_is_marked_as_backtrack():
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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
        assert target_status[52]['returns_to_recent_room']
        assert target_status[52]['agent_priority'] == 'avoid_backtrack'
        assert known['known_transitions'][transition_key]['returns_to_recent_room']
        assert known['known_transitions'][transition_key]['agent_priority'] == 'avoid_backtrack'



def test_rules_prefer_unknown_transition_over_recent_backtrack():
    backtrack = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    unknown = {'id': 53, 'x': 64, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4639, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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
        assert decision['evidence']['target']['destination_priority'] == 'unknown'



def test_dialog_stops_navigation_step():
    target = {'id': 48, 'x': 32, 'y': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[target])
        state['dialog'] = {'active': True}
        write_state(debug, state)
        result = debug.navigation_step(state, target_id=48)
        assert not result['ok']
        assert result['status'] == 'in_dialog'
        assert not debug.action_path.exists()



def test_agent_decide_recommends_reachable_non_transition_target():
    target = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 21, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[target])
        write_state(debug, state)
        decision = debug.rules_decide_agent(state)
        assert decision['recommended_intent'] == 'inspect_target'
        assert decision['recommended_action']['tool'] == 'navigation_step'
        assert decision['recommended_action']['args'] == {'id': 48}
        assert decision['evidence']['target']['id'] == 48
        assert not debug.action_path.exists()



def test_agent_decide_warns_on_transition_target():
    target = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[target])
        write_state(debug, state)
        decision = debug.rules_decide_agent(state)
        assert decision['recommended_intent'] == 'enter_transition'
        assert decision['recommended_action']['tool'] == 'navigation_step'
        assert decision['recommended_action']['args'] == {'id': 52}
        assert '离开当前房间' in decision['risk']
        assert decision['evidence']['target']['id'] == 52
        assert not debug.action_path.exists()



def test_agent_expands_navigation_submission_to_navigation_step():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[{'id': 10, 'trigger_mode': 1, 'state': 1}])
        decision = debug.normalize_agent_submission({'action': 'navigate', 'target_id': 10, 'reason': 'Inspect target.'}, state)
        assert decision['recommended_action']['tool'] == 'navigation_step'
        assert decision['recommended_action']['args'] == {'id': 10}
        assert decision['recommended_intent'] == 'inspect_target'
        assert not debug.action_path.exists()



def test_navigation_applies_dialog_feedback_across_regions_in_same_nav_room():
    target = {'id': 25, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[target])
        state['region_key'] = '3:10:upper-region'
        debug.dialog_history = [{'frame': 50, 'room_id': '3/10/test-room/3:10:lower-region', 'room_key': 'test-room', 'region_key': '3:10:lower-region', 'nav_room_id': '3/10/test-room', 'scene_id': 3, 'map_id': 10, 'event_object_id': 25, 'message_id': 588, 'speaker_hint': '李大娘', 'text': '呵～累了，想早點休息'}]
        navigation = debug.run_agent_tool('get_navigation', {}, state)['navigation']
        targets = {item['id']: item for item in navigation['targets']}
        assert targets[25]['interaction_state'] == 'recent_dialog'
        assert targets[25]['agent_priority'] == 'avoid_repeat'
        assert targets[25]['dialog_feedback_state'] == 'recent_dialog'
        assert targets[25]['recent_dialog_feedback'][0]['text'] == '呵～累了，想早點休息'



def test_navigation_marks_same_full_room_object_with_recent_dialog():
    target = {'id': 57, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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


def test_navigation_marks_same_object_in_different_nav_room_as_stale():
    target = {'id': 25, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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



def test_navigation_does_not_apply_dialog_feedback_from_different_room():
    target = {'id': 25, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[target])
        debug.dialog_history = [{'frame': 50, 'room_key': 'other-room', 'scene_id': 3, 'map_id': 10, 'event_object_id': 25, 'message_id': 588, 'speaker_hint': '李大娘', 'text': '呵～累了，想早點休息'}]
        navigation = debug.run_agent_tool('get_navigation', {}, state)['navigation']
        targets = {item['id']: item for item in navigation['targets']}
        assert targets[25]['interaction_state'] == 'fresh'
        assert targets[25]['agent_priority'] == 'normal'
        assert 'recent_dialog_feedback' not in targets[25]



def test_agent_context_keeps_story_dialog_and_transition_candidates():
    transition = {'id': 52, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4631, 'auto_script': 0, 'blocking': False, 'interactable': True}
    search = {'id': 10, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[transition, search])
        identity = debug.room_identity(state)
        dialogs = {'dialogs': [{'frame': 50, 'scene_id': identity['scene_id'], 'map_id': identity['map_id'], 'room_id': identity['id'], 'room_key': identity['room_key'], 'region_key': identity['region_key'], 'nav_room_id': identity['nav_room_id'], 'event_object_id': 10, 'message_id': 588, 'speaker_hint': '李大娘', 'text': '還不快出來幫忙招呼客人'}, {'frame': 51, 'scene_id': identity['scene_id'], 'map_id': identity['map_id'], 'room_id': identity['id'], 'room_key': identity['room_key'], 'region_key': identity['region_key'], 'nav_room_id': identity['nav_room_id'], 'event_object_id': 10, 'message_id': 590, 'speaker_hint': '李逍遙', 'text': '啊！\u3000．．我馬上就去'}]}
        context = debug.agent_context(state, debug.navigation(state), dialogs, events=[])
        targets = {item['id']: item for item in context['navigation']['targets']}
        assert targets[52]['transition_candidate']
        assert targets[10]['recent_dialog_feedback'][0]['text'] == '還不快出來幫忙招呼客人'
        assert context['dialogs'][-1]['text'] == '啊！\u3000．．我馬上就去'
        assert 'rules' not in context
