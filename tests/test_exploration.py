import tempfile
import re

from conftest import make_debug_state, make_state, write_state



def test_room_identity_uses_stable_logical_room_and_keeps_legacy_fields():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        lower = make_state()
        lower['region_key'] = '3:10:1:1'
        upper = make_state()
        upper['region_key'] = '3:10:1:2'
        lower_identity = debug.room_identity(lower)
        upper_identity = debug.room_identity(upper)
        assert re.fullmatch(r'S3/M10:[0-9A-F]{12}', lower_identity['id'])
        assert lower_identity['logical_room_label'] == lower_identity['id']
        logical = debug.logical_room_identity(lower)
        assert logical['hash12'] == lower_identity['id'].rsplit(':', 1)[-1]
        assert 'full_hash' not in logical
        assert 'hash8' not in logical
        assert lower_identity['id'] == upper_identity['id']
        assert lower_identity['nav_room_id'] == upper_identity['nav_room_id']
        assert lower_identity['nav_room_id'] == lower_identity['id']
        assert lower_identity['legacy_room_id'] == '3/10/test-room/3:10:1:1'
        assert upper_identity['legacy_room_id'] == '3/10/test-room/3:10:1:2'
        assert lower_identity['legacy_nav_room_id'] == '3/10/test-room'
        assert lower_identity['room_key'] == 'test-room'
        assert upper_identity['region_key'] == '3:10:1:2'


def test_logical_room_hash_is_stable_within_portal_cut_component():
    portal = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        left = make_state(width=7, height=5, start=(0, 4), objects=[portal])
        lower_left = make_state(width=7, height=5, start=(1, 3), objects=[portal])
        assert debug.room_identity(left)['id'] == debug.room_identity(lower_left)['id']


def test_logical_room_hash_ignores_transition_range_changes():
    portal = {'id': 48, 'x': 32, 'y': 16, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        corridor = {(0, 4), (1, 3), (2, 2), (3, 1), (4, 0)}
        blocked = {(x, y) for y in range(5) for x in range(5) if (x, y) not in corridor}
        left = make_state(width=5, height=5, start=(0, 4), objects=[portal], blocked=blocked)
        right = make_state(width=5, height=5, start=(4, 0), objects=[portal], blocked=blocked)
        assert debug.room_identity(left)['id'] == debug.room_identity(right)['id']


def test_logical_room_ignores_full_transition_range():
    portal = {'id': 48, 'x': 32, 'y': 24, 'sprite_id': 0, 'state': 1, 'trigger_mode': 5, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        corridor = {(0, 4), (1, 3), (2, 2), (3, 1), (4, 0)}
        blocked = {(x, y) for y in range(5) for x in range(5) if (x, y) not in corridor}
        left = make_state(width=5, height=5, start=(0, 4), objects=[portal], blocked=blocked)
        right = make_state(width=5, height=5, start=(4, 0), objects=[portal], blocked=blocked)
        assert debug.room_identity(left)['id'] == debug.room_identity(right)['id']


def test_logical_room_is_stable_when_search_object_becomes_transition():
    closed = {'id': 10, 'x': 32, 'y': 16, 'sprite_id': 0, 'state': 1, 'trigger_mode': 2, 'trigger_script': 4510, 'auto_script': 0, 'blocking': False, 'interactable': True}
    opened = {**closed, 'trigger_mode': 4, 'trigger_script': 4520}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        before = make_state(width=7, height=5, start=(0, 4), objects=[closed])
        after = make_state(width=7, height=5, start=(0, 4), objects=[opened])
        before_identity = debug.logical_room_identity(before)
        after_identity = debug.logical_room_identity(after)
        assert before_identity['id'] == after_identity['id']
        assert before_identity['component_size'] == after_identity['component_size']
        assert before_identity['portal_cut_size'] == 0
        assert after_identity['portal_cut_size'] > 0
        assert after_identity['algorithm'] == 'static-component-v1'


def test_transition_activation_does_not_discard_visited_nodes_in_same_static_room():
    closed = {'id': 10, 'x': 32, 'y': 16, 'sprite_id': 0, 'state': 1, 'trigger_mode': 2, 'trigger_script': 4510, 'auto_script': 0, 'blocking': False, 'interactable': True}
    opened = {**closed, 'trigger_mode': 4, 'trigger_script': 4520}
    corridor = {(0, 4), (1, 3), (2, 2), (3, 1), (4, 0)}
    blocked = {(x, y) for y in range(5) for x in range(5) if (x, y) not in corridor}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        before = make_state(width=5, height=5, start=(0, 4), objects=[closed], blocked=blocked)
        identity = debug.room_identity(before)
        data = debug.empty_exploration()
        room = debug.exploration_room(data, identity)
        room['visited'] = ['0,4', '4,0']
        debug.write_exploration(data)
        after = make_state(width=5, height=5, start=(0, 4), objects=[opened], blocked=blocked)
        summary = debug.update_exploration(after)
        visited = {(node['grid_x'], node['grid_y']) for node in summary['nodes']['visited']}
        assert {(0, 4), (4, 0)} <= visited


def test_logical_room_ignores_npc_blocker_search_and_item_state():
    blocker = {'id': 74, 'x': 32, 'y': 0, 'state': 2, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': True, 'interactable': True}
    item = {'id': 75, 'x': 48, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 99, 'auto_script': 0, 'blocking': False, 'interactable': True}
    npc = {'id': 76, 'x': 64, 'y': 16, 'sprite_id': 123, 'state': 1, 'trigger_mode': 4, 'trigger_script': 100, 'auto_script': 0, 'blocking': True, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        base = make_state(width=6, height=6, start=(2, 2), objects=[blocker, item, npc])
        changed = make_state(width=6, height=6, start=(2, 2), objects=[
            {**blocker, 'state': 1, 'blocking': False},
            {**item, 'interactable': False},
            {**npc, 'x': 80, 'blocking': False},
        ])
        assert debug.room_identity(base)['id'] == debug.room_identity(changed)['id']


def test_logical_room_identity_on_transition_area_uses_static_component():
    portal = {'id': 48, 'x': 32, 'y': 16, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        corridor = {(0, 4), (1, 3), (2, 2), (3, 1), (4, 0)}
        blocked = {(x, y) for y in range(5) for x in range(5) if (x, y) not in corridor}
        on_portal = make_state(width=5, height=5, start=(2, 2), objects=[portal], blocked=blocked)
        near_portal = make_state(width=5, height=5, start=(3, 1), objects=[portal], blocked=blocked)
        identity = debug.logical_room_identity(on_portal)
        assert not identity['seed_adjusted']
        assert debug.room_identity(on_portal)['id'] == debug.room_identity(near_portal)['id']



def test_update_marks_current_and_frontier():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        write_state(debug, state)
        summary = debug.update_exploration(state)
        assert summary['status'] == 'ok'
        assert summary['visited'] == 1
        assert summary['frontier'] == 4
        assert not summary['complete']



def test_update_marks_visible_reachable_nodes_explorable_not_visited():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(width=6, height=6, start=(2, 2), viewport={'x': 16, 'y': 8})
        write_state(debug, state)
        summary = debug.update_exploration(state)
        visited = {(node['grid_x'], node['grid_y']) for node in summary['nodes']['visited']}
        explorable = {(node['grid_x'], node['grid_y']) for node in summary['nodes']['explorable']}
        frontier = {(node['grid_x'], node['grid_y']) for node in summary['nodes']['frontier']}
        assert visited == {(2, 2)}
        assert (1, 1) in explorable
        assert (5, 5) in explorable
        assert summary['explorable'] > summary['visited']
        assert (1, 1) in frontier
        assert (3, 1) in frontier
        assert not summary['complete']



def test_visible_unreachable_nodes_are_not_marked_visited():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        island = {(1, 1), (2, 2), (3, 3), (8, 1), (9, 2), (10, 3)}
        blocked = {(x, y) for y in range(6) for x in range(12) if (x, y) not in island}
        state = make_state(width=12, height=6, start=(1, 1), blocked=blocked, viewport={'x': 0, 'y': 0})
        write_state(debug, state)
        identity = debug.room_identity(state)
        data = debug.empty_exploration()
        room = debug.exploration_room(data, identity)
        room['visited'] = ['8,1', '9,2']
        debug.write_exploration(data)
        summary = debug.update_exploration(state)
        visited = {(node['grid_x'], node['grid_y']) for node in summary['nodes']['visited']}
        explorable = {(node['grid_x'], node['grid_y']) for node in summary['nodes']['explorable']}
        frontier = {(node['grid_x'], node['grid_y']) for node in summary['nodes']['frontier']}
        assert (1, 1) in visited
        assert (2, 2) in explorable
        assert (2, 2) not in visited
        assert (8, 1) not in visited
        assert (9, 2) not in visited
        assert (10, 3) not in explorable
        assert (10, 3) not in frontier



def test_update_marks_confirmed_pending_route_nodes_as_visited():
    target = {'id': 48, 'x': 64, 'y': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(width=5, height=5, start=(0, 4), objects=[target])
        write_state(debug, state)
        result = debug.navigation_step(state, target_id=48)
        assert result['ok']
        assert result['status'] == 'stepping'

        midway = make_state(width=5, height=5, start=(2, 2), objects=[target])
        summary = debug.update_exploration(midway)
        visited = {(node['grid_x'], node['grid_y']) for node in summary['nodes']['visited']}
        assert {(0, 4), (1, 3), (2, 2)} <= visited
        assert (3, 1) not in visited



def test_touch_trigger_nodes_are_not_frontier():
    touch_on_up_node = {'id': 48, 'x': 32, 'y': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[touch_on_up_node])
        write_state(debug, state)
        summary = debug.update_exploration(state)
        frontier = {(node['grid_x'], node['grid_y']) for node in summary['nodes']['frontier']}
        assert (2, 0) not in frontier
        assert summary['blocked_edges'] > 0



def test_blocking_nodes_do_not_change_logical_exploration_component():
    blocker = {'id': 74, 'x': 32, 'y': 0, 'state': 2, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': True, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[blocker])
        write_state(debug, state)
        summary = debug.update_exploration(state)
        frontier = {(node['grid_x'], node['grid_y']) for node in summary['nodes']['frontier']}
        assert (2, 0) in frontier
        assert summary['explorable'] == 5



def test_write_use_item_action_for_role():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        state['party'] = [{'role_id': 0}]
        state['inventory'] = [{'item_id': 88, 'amount': 2, 'amount_in_use': 0, 'usable': True, 'apply_to_all': False}]
        write_state(debug, state)
        debug.action_result_path.write_text('{"ok":true}', encoding='utf-8')
        result = debug.write_use_item_action(item_id=88, role_id=0)
        assert result['command'] == 'use_item'
        assert result['item_id'] == 88
        assert result['role_id'] == 0
        assert result['target'] == 'role'
        assert debug.action_path.read_text(encoding='ascii') == 'use_item 88 0\n'
        assert not debug.action_result_path.exists()



def test_write_use_item_action_for_all():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        state['party'] = [{'role_id': 0}]
        state['inventory'] = [{'item_id': 89, 'amount': 1, 'amount_in_use': 0, 'usable': True, 'apply_to_all': True}]
        write_state(debug, state)
        result = debug.write_use_item_action(item_id=89, target='all')
        assert result['target'] == 'all'
        assert result['role_id'] is None
        assert debug.action_path.read_text(encoding='ascii') == 'use_item 89 all\n'



def test_get_room_memory_returns_compact_known_and_unknown_status():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        write_state(debug, state)
        debug.update_exploration(state)
        identity = debug.room_identity(state)
        known = debug.run_agent_tool('get_room_memory', {'room_id': identity['id']}, state)['exploration']
        unknown = debug.run_agent_tool('get_room_memory', {'room_id': '9/9/missing'}, state)['exploration']
        assert known['known']
        assert known['room_id'] == identity['id']
        assert known['scene_id'] == 3
        assert known['map_id'] == 10
        assert known['room_key'] == 'test-room'
        assert known['region_key'] == 'test-room'
        assert known['logical_room_id'] == identity['id']
        assert known['nav_room_id'] == identity['id']
        assert known['legacy_nav_room_id'] == '3/10/test-room'
        assert known['explored'] == 1
        assert known['frontier'] == 4
        assert 'explorable' in known
        assert not unknown['known']
        assert unknown['room_id'] == '9/9/missing'
