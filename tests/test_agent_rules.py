import json
import os
import time

import pytest

from tests.support import make_debug_state, make_state, write_state


def equipment_item(
    item_id,
    name,
    *,
    slot_id=0,
    modifiers=None,
    traits=None,
    usable=False,
    amount=1,
    eligible=(0,),
):
    return {
        'item_id': item_id,
        'name': name,
        'amount': amount,
        'amount_in_use': 0,
        'equipped_count': 0,
        'total_count': amount,
        'usable': usable,
        'equipable': True,
        'equipable_role_ids': list(eligible),
        'equipment_effect': {
            'preview_complete': True,
            'slot_id': slot_id,
            'slot_key': 'head',
            'stat_modifiers': {
                'attack_strength': 0,
                'magic_strength': 0,
                'defense': 0,
                'dexterity': 0,
                'flee_rate': 0,
                'poison_resistance': 0,
                **(modifiers or {}),
            },
            'elemental_resistance': [0, 0, 0, 0, 0],
            'traits': traits or [],
            'unparsed_effects': [],
        },
    }


def shop_state(*, cash=600, items=None, phase='select', selected_index=0):
    state = make_state()
    offers = items or [
        {
            **equipment_item(166, '木劍', slot_id=3, modifiers={'attack_strength': 2}),
            'index': 0,
            'price': 200,
            'inventory_count': 0,
            'equipped_count': 1,
            'owned_count': 1,
            'affordable': cash >= 200,
            'selected': selected_index == 0,
        },
        {
            **equipment_item(201, '皮帽', modifiers={'defense': 5}),
            'index': 1,
            'price': 400,
            'inventory_count': 0,
            'equipped_count': 0,
            'owned_count': 0,
            'affordable': cash >= 400,
            'selected': selected_index == 1,
        },
    ]
    state['cash'] = cash
    state['party'] = [{
        'role_id': 0,
        'name': '李逍遥',
        'attack_strength': 35,
        'magic_strength': 20,
        'defense': 41,
        'dexterity': 31,
        'flee_rate': 32,
        'poison_resistance': 0,
        'equipment': [{
            **equipment_item(196, '头巾', modifiers={'defense': 2}),
            'slot_id': 0,
            'slot_key': 'head',
        }],
    }]
    state['shop'] = {
        'active': True,
        'mode': 'buy',
        'store_id': 7,
        'phase': phase,
        'selected_index': selected_index,
        'selected_item_id': offers[selected_index]['item_id'],
        'items': offers,
    }
    return state


def test_agent_decide_reports_stale_runtime(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    old = time.time() - 10
    debug.set_live_state(state, received_at=old)
    decision = debug.rules_decide_agent(state)
    assert decision['recommended_intent'] == 'wait_runtime'
    assert decision['recommended_action']['args']['reason'] == 'runtime_stale'
    assert not debug.action_path.exists()



def test_agent_decide_allows_fresh_state_when_frame_is_stale(tmp_path):
    target = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 21, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    write_state(debug, state)
    old = time.time() - 10
    debug.set_live_frame(b'frame', received_at=old)

    snapshot = debug.snapshot()
    assert snapshot['_debug']['state_stale'] is False
    assert snapshot['_debug']['frame_stale'] is True
    assert snapshot['_debug']['runtime_stale'] is False

    decision = debug.rules_decide_agent(snapshot)
    assert decision['recommended_intent'] == 'inspect_target'
    assert decision['recommended_action']['args'] == {'id': 48}
    assert not debug.action_path.exists()



def test_agent_decide_prioritizes_active_dialog(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    state['dialog'] = {'active': True, 'position': 'lower', 'latest_event': {'message_id': 88, 'event_object_id': 7, 'text': '我们走吧。'}}
    write_state(debug, state)
    decision = debug.rules_decide_agent(state)
    assert decision['recommended_intent'] == 'advance_dialog'
    assert decision['recommended_action']['tool'] == 'advance_dialog'
    assert decision['recommended_action']['args'] == {}
    assert not debug.action_path.exists()



def test_advance_dialog_clamps_options(tmp_path):
    debug = make_debug_state(tmp_path)
    options = debug.normalize_dialog_advance_args({'max_presses': 999, 'delay_ms': 1, 'stop_on_repeat': False})
    assert options == {'max_presses': 100, 'delay_ms': 100, 'stop_on_repeat': False}



def test_advance_dialog_stops_when_dialog_inactive(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    write_state(debug, state)
    debug.set_live_frame(b'frame')
    result = debug.advance_dialog({'max_presses': 5})
    assert result['ok']
    assert result['status'] == 'dialog_inactive'
    assert result['presses'] == 0
    assert not debug.action_path.exists()



def test_agent_decision_log_records_turn_and_decision(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    write_state(debug, state)
    decision = debug.rules_decide_agent(state)
    log = debug.read_agent_log(limit=10)
    assert decision['recommended_intent'] == 'wait_runtime'
    assert decision['recommended_action']['tool'] == 'no_op'
    assert decision['recommended_action']['args'] == {'reason': 'no_reachable_target'}
    assert len(log) == 1
    assert log[0]['turn'] == 1
    assert log[0]['episode'] == debug.room_identity(state)['id']
    assert log[0]['decision']['recommended_intent'] == decision['recommended_intent']
    assert 'api_key' not in json.dumps(log[0]).lower()
    assert 'prompt' not in json.dumps(log[0]).lower()



def test_agent_expands_minimal_dialog_submission(tmp_path):
    debug = make_debug_state(tmp_path)
    decision = debug.normalize_agent_submission({'action': 'advance_dialog', 'reason': 'Dialog is active.'}, make_state())
    assert decision['recommended_action']['tool'] == 'advance_dialog'
    assert decision['recommended_action']['args'] == {'max_presses': 100, 'delay_ms': 500, 'stop_on_repeat': True}
    assert not debug.action_path.exists()


def test_agent_expands_minimal_wait_submission(tmp_path):
    debug = make_debug_state(tmp_path)
    decision = debug.normalize_agent_submission({'action': 'wait', 'reason': 'No clear target.'}, make_state())
    assert decision['recommended_intent'] == 'wait_runtime'
    assert decision['recommended_action']['tool'] == 'no_op'
    assert decision['recommended_action']['args'] == {'reason': 'agent_wait'}
    assert decision['summary'] == 'No clear target.'
    assert not debug.action_path.exists()


def test_agent_rejects_old_decision_contract(tmp_path):
    debug = make_debug_state(tmp_path)
    result = debug.run_agent_tool('submit_decision', {
        'summary': 'Old shape.',
        'recommended_intent': 'wait_runtime',
        'recommended_action': {'tool': 'no_op', 'args': {}},
        'evidence': {},
        'risk': 'None.',
    }, make_state())
    assert not result['ok']
    assert result['error']['code'] == 'invalid_decision_submission'
    assert {issue['field'] for issue in result['error']['issues']} >= {
        'summary', 'recommended_intent', 'recommended_action', 'evidence', 'risk'
    }



def test_agent_context_marks_targets_with_recent_dialog_feedback(tmp_path):
    target = {'id': 10, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    identity = debug.room_identity(state)
    dialogs = {'dialogs': [{'frame': 50, 'scene_id': 3, 'map_id': 10, 'room_id': identity['id'], 'room_key': identity['room_key'], 'region_key': identity['region_key'], 'nav_room_id': identity['nav_room_id'], 'event_object_id': 10, 'message_id': 588, 'speaker_hint': '李大娘', 'text': '還不快出來幫忙招呼客人'}]}
    context = debug.agent_context(state, debug.navigation(state), dialogs, events=[])
    target_context = {item['id']: item for item in context['navigation']['targets']}
    assert 'recent_dialog_feedback' in target_context[10]
    assert target_context[10]['recent_dialog_feedback'][0]['text'] == '還不快出來幫忙招呼客人'
    assert '本房间' in target_context[10]['deprioritize_reason']



def test_agent_context_does_not_label_object_from_speaker_hint(tmp_path):
    target = {'id': 57, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    identity = debug.room_identity(state)
    dialogs = {'dialogs': [{'frame': 50, 'scene_id': identity['scene_id'], 'map_id': identity['map_id'], 'room_id': identity['id'], 'room_key': identity['room_key'], 'region_key': identity['region_key'], 'nav_room_id': identity['nav_room_id'], 'event_object_id': 57, 'message_id': 588, 'speaker_hint': '苗人頭領', 'text': '你們跑不掉了'}]}
    context = debug.agent_context(state, debug.navigation(state), dialogs, events=[])
    payload = json.dumps(context, ensure_ascii=False)
    assert '苗人頭領' in payload
    assert 'ID 57 (苗人頭領)' not in payload
    assert '#57 (苗人頭領)' not in payload



def test_agent_tools_mark_recently_cleared_search_targets(tmp_path):
    left_door = {'id': 74, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    right_door = {'id': 75, 'x': 32, 'y': 16, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[left_door, right_door])
    write_state(debug, state)
    identity = debug.room_identity(state)
    debug.agent_log_path.write_text('\n'.join((json.dumps({'turn': turn, 'episode': identity['id'], 'nav_room_id': identity['nav_room_id'], 'decision': {}}) for turn in (1, 2, 3, 4))) + '\n', encoding='utf-8')
    debug.event_log = [{'frame': 20, 'scene_id': 3, 'map_id': 10, 'room_key': 'test-room', 'type': 'object_changed', 'object_id': 75, 'changes': {'state': {'from': 2, 'to': 1}, 'blocking': {'from': True, 'to': False}}}]
    navigation = debug.run_agent_tool('get_navigation', {}, state)['navigation']
    targets = {target['id']: target for target in navigation['targets']}
    route = debug.run_agent_tool('inspect_target', {'id': 75}, state)['route']
    assert targets[75]['interaction_state'] == 'previously_cleared'
    assert targets[75]['agent_priority'] == 'avoid_repeat'
    assert targets[74]['interaction_state'] == 'previously_cleared'
    assert targets[74]['agent_priority'] == 'avoid_repeat'
    assert route['interaction_state'] == 'previously_cleared'
    assert route['agent_priority'] == 'avoid_repeat'


def test_interaction_no_effect_is_recorded_and_deprioritizes_target(tmp_path):
    target = {'id': 75, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target], frame=20)
    write_state(debug, state)
    observation_id = debug.mark_pending_interaction_observation(
        state,
        75,
        action={'buttons': ['search'], 'frames': 1},
    )
    next_state = make_state(objects=[target], frame=30)
    debug.observe_pending_interaction_outcome(next_state)

    assert debug.event_log[-1]['type'] == 'interaction_no_effect'
    assert debug.event_log[-1]['object_id'] == 75
    navigation = debug.run_agent_tool('get_navigation', {}, next_state)['navigation']
    targets = {target['id']: target for target in navigation['targets']}
    history = debug.run_agent_tool('inspect_target', {'id': 75}, next_state)
    assert targets[75]['interaction_state'] == 'previously_tried'
    assert targets[75]['agent_priority'] == 'avoid_repeat'
    assert history['history'][-1]['type'] == 'interaction_no_effect'
    completed = debug.observations.consume(observation_id, owner='navigation')
    assert completed['result']['type'] == 'interaction_no_effect'


def test_interaction_effect_attributes_inventory_change_to_target(tmp_path):
    target = {'id': 75, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target], frame=20)
    state['inventory'] = []
    observation_id = debug.mark_pending_interaction_observation(
        state,
        75,
        action={'buttons': ['search'], 'frames': 1},
    )
    next_state = make_state(objects=[target], frame=22)
    next_state['inventory'] = [{'item_id': 201, 'amount': 1, 'amount_in_use': 0}]
    debug.observe_pending_interaction_outcome(next_state)

    assert debug.event_log[-1]['type'] == 'interaction_effect'
    assert debug.event_log[-1]['object_id'] == 75
    assert debug.event_log[-1]['inventory_changes'] == [{'item_id': 201, 'delta': 1, 'amount': 1}]
    completed = debug.observations.consume(observation_id, owner='navigation')
    assert completed['status'] == 'completed'
    assert completed['result']['type'] == 'interaction_effect'
    route = debug.run_agent_tool('inspect_target', {'id': 75}, next_state)['route']
    assert route['interaction_state'] == 'previously_tried'
    assert route['agent_priority'] == 'avoid_repeat'


def test_interaction_observation_does_not_finish_before_action_is_armed(tmp_path):
    target = {'id': 75, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target], frame=20)
    observation_id = debug.mark_pending_interaction_observation(state, 75)
    later = make_state(objects=[target], frame=40)

    assert debug.observe_pending_interaction_outcome(later) is None
    assert debug.event_log == []

    debug.observations.update_payload(
        observation_id,
        action={'buttons': ['search'], 'frames': 1},
        armed=True,
    )
    debug.pending_interaction_observation['action'] = {'buttons': ['search'], 'frames': 1}
    debug.pending_interaction_observation['armed'] = True
    debug.observe_pending_interaction_outcome(later)
    assert debug.event_log[-1]['type'] == 'interaction_no_effect'



def test_recently_cleared_search_targets_survive_region_changes(tmp_path):
    left_door = {'id': 74, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    right_door = {'id': 75, 'x': 32, 'y': 16, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[left_door, right_door])
    state['region_key'] = '3:10:22:44'
    write_state(debug, state)
    identity = debug.room_identity(state)
    debug.event_log = [
            {
                'frame': 20,
                'scene_id': identity['scene_id'],
                'map_id': identity['map_id'],
                'room_id': identity['id'],
                'logical_room_id': identity['id'],
                'room_key': identity['room_key'],
                'region_key': '3:10:22:45',
                'nav_room_id': identity['nav_room_id'],
            'type': 'object_changed',
            'object_id': 75,
            'changes': {'state': {'from': 2, 'to': 1}, 'blocking': {'from': True, 'to': False}},
        }
    ]
    navigation = debug.run_agent_tool('get_navigation', {}, state)['navigation']
    targets = {target['id']: target for target in navigation['targets']}
    route = debug.run_agent_tool('inspect_target', {'id': 74}, state)['route']
    assert targets[75]['interaction_state'] == 'previously_cleared'
    assert targets[75]['agent_priority'] == 'avoid_repeat'
    assert targets[74]['interaction_state'] == 'previously_cleared'
    assert targets[74]['agent_priority'] == 'avoid_repeat'
    assert route['interaction_state'] == 'previously_cleared'
    assert route['agent_priority'] == 'avoid_repeat'



def test_recently_cleared_search_targets_do_not_cross_nav_rooms(tmp_path):
    door = {'id': 75, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[door])
    write_state(debug, state)
    debug.event_log = [
        {
            'frame': 20,
            'scene_id': 3,
            'map_id': 10,
            'room_id': '3/10/other-room/3:10:22:45',
            'room_key': 'other-room',
            'region_key': '3:10:22:45',
            'nav_room_id': '3/10/other-room',
            'type': 'object_changed',
            'object_id': 75,
            'changes': {'state': {'from': 2, 'to': 1}, 'blocking': {'from': True, 'to': False}},
        }
    ]
    navigation = debug.run_agent_tool('get_navigation', {}, state)['navigation']
    targets = {target['id']: target for target in navigation['targets']}
    assert targets[75]['interaction_state'] == 'fresh'
    assert targets[75]['agent_priority'] == 'normal'



def test_agent_rules_skip_recently_cleared_search_target(tmp_path):
    opened_door = {'id': 75, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    fresh_target = {'id': 80, 'x': 32, 'y': 16, 'state': 1, 'trigger_mode': 1, 'trigger_script': 9000, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[opened_door, fresh_target])
    write_state(debug, state)
    debug.event_log = [{'frame': 20, 'scene_id': 3, 'map_id': 10, 'room_key': 'test-room', 'type': 'object_changed', 'object_id': 75, 'changes': {'state': {'from': 2, 'to': 1}, 'blocking': {'from': True, 'to': False}}}]
    decision = debug.rules_decide_agent(state)
    assert decision['recommended_intent'] == 'inspect_target'
    assert decision['recommended_action']['args'] == {'id': 80}
    assert decision['evidence']['target']['interaction_state'] == 'fresh'
    assert not debug.action_path.exists()



def test_agent_messages_put_stable_policy_before_dynamic_context(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    messages = debug.agent_messages(state, debug.navigation(state), {'dialogs': []}, events=[])
    assert [message['role'] for message in messages] == ['system', 'system', 'user']
    assert "{action:'navigate',target_id:<id>,reason:'...'}" in messages[0]['content']
    assert '距离是成本信号，不是优先级规则' in messages[1]['content']
    assert '不要匹配固定剧情短语' in messages[1]['content']
    assert '由非传送目标变为传送目标' in messages[1]['content']
    assert 'known_destination=false' in messages[1]['content']
    assert 'previously_tried 或 avoid_repeat 不应降低它的优先级' in messages[1]['content']
    assert '0xFFFF' in messages[1]['content']
    assert "{action:'use_item',item_id:<id>,target:'all',reason:'...'}" in messages[1]['content']
    assert '不要因 target_object_id 被拒绝' in messages[1]['content']
    assert '进程重启或载入存档不会恢复最近事件对象' in messages[1]['content']
    assert 'prefer the nearest reachable non-transition target' not in messages[1]['content']
    assert 'greet guests' not in messages[1]['content']
    assert 'secret passage' not in messages[1]['content']
    dynamic = json.loads(messages[2]['content'])
    assert 'state' in dynamic
    assert 'exploration' in dynamic
    assert 'last_route' in dynamic['exploration']
    assert 'pending' not in dynamic['exploration']['last_route']
    assert 'rules' not in dynamic


def test_agent_context_exposes_newly_unlocked_unknown_transition_signals(tmp_path):
    opened = {'id': 10, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4520, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[opened])
    identity = debug.room_identity(state)
    debug.event_log = [{
        'type': 'interaction_effect',
        'object_id': 10,
        'target_id': 10,
        'scene_id': identity['scene_id'],
        'map_id': identity['map_id'],
        'logical_room_id': identity['id'],
        'room_id': identity['id'],
        'started_object': {'state': 1, 'sprite_id': 0, 'trigger_mode': 2, 'trigger_script': 4510, 'interactable': True, 'blocking': False},
        'finished_object': {'state': 1, 'sprite_id': 0, 'trigger_mode': 4, 'trigger_script': 4520, 'interactable': True, 'blocking': False},
    }]
    navigation = debug.navigation(state)
    context = debug.agent_context(state, navigation, {'dialogs': []}, debug.event_log)
    target = next(target for target in context['navigation']['targets'] if target['id'] == 10)
    assert target['transition_candidate']
    assert not target['known_destination']
    assert target['interaction_state'] == 'previously_tried'
    assert target['agent_priority'] == 'avoid_repeat'
    assert target['previous_interaction_outcome']['started_object']['trigger_mode'] == 2
    assert target['previous_interaction_outcome']['finished_object']['trigger_mode'] == 4



def test_agent_tool_specs_include_queries_and_submit(tmp_path):
    debug = make_debug_state(tmp_path)
    tools = debug.agent_tool_specs()
    names = [tool['function']['name'] for tool in tools]
    by_name = {tool['function']['name']: tool for tool in tools}
    assert set(names) == {
        'upsert_task', 'get_tasks', 'get_navigation', 'inspect_target',
        'get_observations', 'get_inventory', 'get_shop', 'get_equipment',
        'get_room_memory', 'get_tool_output', 'submit_decision',
    }
    dialog_properties = by_name['get_observations']['function']['parameters']['properties']
    assert 'speaker' in dialog_properties
    assert dialog_properties['speaker']['type'] == 'string'
    object_history_properties = by_name['inspect_target']['function']['parameters']['properties']
    assert set(object_history_properties) == {'id', 'limit'}
    assert '紧凑' in by_name['get_navigation']['function']['description']
    assert '最小 dry-run 动作' in by_name['submit_decision']['function']['description']
    output_properties = by_name['get_tool_output']['function']['parameters']['properties']
    assert set(output_properties) == {'output_ref', 'offset'}
    task_query_properties = by_name['get_tasks']['function']['parameters']['properties']
    assert set(task_query_properties) == {'include_closed', 'limit'}
    assert '未知自选 id' in by_name['upsert_task']['function']['description']
    context = debug.agent_tool_initial_context(make_state())
    assert context['active_tasks'] == []
    assert context['active_tasks_total'] == 0
    assert set(context) == {'state_summary', 'active_tasks', 'active_tasks_total', 'task_review'}
    assert context['task_review'] == {'required': False, 'tasks': []}
    assert names[-1] == 'submit_decision'


def test_agent_get_shop_returns_offers_and_equipment_comparison(tmp_path):
    debug = make_debug_state(tmp_path)
    state = shop_state()

    overview = debug.run_agent_tool('get_shop', {}, state)
    detail = debug.run_agent_tool('get_shop', {'item_id': 201}, state)

    assert overview['ok']
    assert overview['shop']['cash'] == 600
    assert [item['item_id'] for item in overview['shop']['items']] == [166, 201]
    assert overview['shop']['items'][0]['owned_count'] == 1
    assert detail['item']['name'] == '皮帽'
    comparison = detail['equipment_comparisons'][0]
    assert comparison['role_id'] == 0
    assert comparison['current_item']['item_id'] == 196
    assert comparison['stat_delta']['defense'] == 3


def test_agent_shop_decisions_require_active_offered_affordable_item(tmp_path):
    debug = make_debug_state(tmp_path)
    state = shop_state()
    write_state(debug, state)

    bought = debug.run_agent_tool(
        'submit_decision',
        {'action': 'buy_item', 'item_id': 201, 'reason': 'Defense upgrade is worth 400.'},
        state,
    )
    left = debug.run_agent_tool(
        'submit_decision',
        {'action': 'leave_shop', 'reason': 'No other purchase is useful.'},
        state,
    )

    assert bought['decision']['recommended_intent'] == 'purchase_shop_item'
    assert bought['decision']['recommended_action']['tool'] == 'shop_buy'
    assert bought['decision']['recommended_action']['args'] == {'item_id': 201}
    assert bought['decision']['evidence']['equipment_comparisons'][0]['stat_delta']['defense'] == 3
    assert left['decision']['recommended_intent'] == 'leave_shop'
    assert left['decision']['recommended_action']['tool'] == 'shop_leave'

    poor = shop_state(cash=100)
    write_state(debug, poor)
    rejected = debug.run_agent_tool(
        'submit_decision',
        {'action': 'buy_item', 'item_id': 201, 'reason': 'Try to buy it.'},
        poor,
    )['decision']
    assert rejected['recommended_action']['tool'] == 'no_op'
    assert rejected['validation']['reason'] == 'shop_action_invalid'
    assert rejected['validation']['error'] == 'insufficient_cash'


def test_active_shop_rejects_non_shop_model_decisions(tmp_path):
    debug = make_debug_state(tmp_path)
    state = shop_state()
    write_state(debug, state)
    decision = debug.normalize_agent_submission(
        {'action': 'navigate', 'target_id': 9, 'reason': 'Continue exploring.'},
        state,
    )

    result = debug.validate_agent_decision(decision)

    assert result['validation']['reason'] == 'in_shop'
    assert result['recommended_action']['tool'] == 'no_op'
    assert result['recommended_action']['args']['required_actions'] == ['buy_item', 'leave_shop']
    assert not debug.action_path.exists()


def test_removed_agent_tool_names_are_rejected(tmp_path):
    removed = {
        'get_state_summary', 'get_dialogs', 'get_dialog_speakers',
        'get_events', 'get_object_history', 'get_agent_history', 'get_route',
        'get_room_exploration', 'get_known_transitions',
    }
    debug = make_debug_state(tmp_path)
    for name in removed:
        with pytest.raises(RuntimeError, match=f'unsupported agent tool: {name}'):
            debug.run_agent_tool(name, {}, make_state())



def test_agent_inspect_target_returns_state_events_dialogs_and_route(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[{'id': 10, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4520, 'auto_script': 0, 'blocking': False, 'interactable': True}])
    debug.event_log = [
        {'type': 'object_changed', 'object_id': 9, 'changes': {'state': {'from': 1, 'to': 0}}, 'time': 1.0, 'frame': 1},
        {'type': 'object_changed', 'object_id': 10, 'changes': {'trigger_mode': {'from': 1, 'to': 4}}, 'time': 2.0, 'frame': 2},
    ]
    debug.dialog_history = [
        {'event_object_id': 10, 'speaker_hint': '系统', 'text': '机关打开了。', 'time': 3.0, 'frame': 3, 'message_id': 123},
        {'event_object_id': 11, 'speaker_hint': '系统', 'text': '其他对象。', 'time': 4.0, 'frame': 4, 'message_id': 124},
    ]
    result = debug.run_agent_tool('inspect_target', {'id': 10}, state)
    assert result['ok']
    assert result['id'] == 10
    assert result['current']['kind'] == 'touch'
    assert result['current']['transition_candidate']
    assert result['history_total'] == 2
    assert isinstance(result['route'], dict)
    assert [entry['source'] for entry in result['history']] == ['events', 'dialogs']
    assert result['history'][0]['changes']['trigger_mode'] == {'from': 1, 'to': 4}
    assert result['history'][1]['text'] == '机关打开了。'



def test_agent_get_observation_speakers_returns_recent_speakers(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    debug.dialog_history = [{'speaker_hint': '李大娘', 'time': 10.0, 'frame': 100, 'end_frame': 101, 'start_sequence': 1, 'end_sequence': 2, 'scene_id': 3, 'map_id': 10, 'room_key': 'inn', 'text': '早点回来。'}, {'speaker_hint': '赵灵儿', 'time': 20.0, 'frame': 200, 'end_frame': 201, 'start_sequence': 3, 'end_sequence': 3, 'scene_id': 4, 'map_id': 11, 'room_key': 'garden', 'text': '谢谢。'}, {'speaker_hint': '李大娘', 'time': 30.0, 'frame': 300, 'end_frame': 302, 'start_sequence': 4, 'end_sequence': 5, 'scene_id': 5, 'map_id': 12, 'room_key': 'street', 'text': '快去吧。'}]
    result = debug.run_agent_tool('get_observations', {'kind': 'speakers'}, state)
    assert result['ok']
    assert result['total'] == 2
    assert result['returned'] == 2
    assert [speaker['speaker_hint'] for speaker in result['speakers']] == ['李大娘', '赵灵儿']
    assert result['speakers'][0]['dialog_count'] == 2
    assert result['speakers'][0]['last_time'] == 30.0
    assert result['speakers'][0]['last_frame'] == 300
    assert result['speakers'][0]['last_end_frame'] == 302
    assert result['speakers'][0]['last_sequence'] == 4
    assert result['speakers'][0]['last_end_sequence'] == 5
    assert result['speakers'][0]['last_scene_id'] == 5
    assert result['speakers'][0]['last_map_id'] == 12
    assert result['speakers'][0]['last_room_key'] == 'street'



def test_agent_observation_speakers_excludes_system_and_empty_speakers(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    debug.dialog_history = [{'speaker_hint': '系统', 'time': 1.0, 'text': '得到５００文钱'}, {'speaker_hint': '', 'time': 2.0, 'text': 'empty'}, {'time': 3.0, 'text': 'missing'}, {'speaker_hint': '李逍遥', 'time': 4.0, 'frame': 40, 'text': '好。'}]
    result = debug.run_agent_tool('get_observations', {'kind': 'speakers'}, state)
    assert result['total'] == 1
    assert result['returned'] == 1
    assert result['speakers'][0]['speaker_hint'] == '李逍遥'



def test_agent_get_observations_filters_dialogs_by_speaker(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    debug.dialog_history = [{'speaker_hint': '李大娘', 'time': 10.0, 'frame': 100, 'end_frame': 101, 'text': '早点回来。'}, {'speaker_hint': '赵灵儿', 'time': 20.0, 'frame': 200, 'end_frame': 201, 'text': '谢谢。'}, {'speaker_hint': '李大娘', 'time': 30.0, 'frame': 300, 'end_frame': 301, 'text': '快去吧。'}]
    result = debug.run_agent_tool('get_observations', {'kind': 'dialogs', 'speaker': '李大娘', 'limit': 10}, state)
    assert result['ok']
    assert result['total'] == 2
    assert [dialog['speaker_hint'] for dialog in result['dialogs']] == ['李大娘', '李大娘']
    assert [dialog['text'] for dialog in result['dialogs']] == ['早点回来。', '快去吧。']
    assert result['dialogs'][0]['time'] == 10.0
    assert result['dialogs'][0]['frame'] == 100
    assert result['dialogs'][0]['end_frame'] == 101



def test_agent_get_inventory_returns_current_items(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    state['inventory'] = [{'slot': 0, 'item_id': 91, 'name': '十里香', 'price': 640, 'flags': ['usable', 'apply_to_all'], 'usable': True, 'apply_to_all': True, 'sellable': True, 'amount': 1, 'amount_in_use': 0, 'equipped_count': 0}, {'slot': 1, 'item_id': 88, 'name': '止血草', 'price': 50, 'flags': ['usable'], 'usable': True, 'apply_to_all': False, 'sellable': True, 'amount': 5, 'amount_in_use': 0, 'equipped_count': 0}]
    result = debug.run_agent_tool('get_inventory', {}, state)
    assert result['ok']
    assert result['total'] == 2
    assert result['returned'] == 2
    assert result['inventory'][0] == {'slot': 0, 'item_id': 91, 'name': '十里香', 'amount': 1, 'amount_in_use': 0, 'equipped_count': 0, 'usable': True, 'apply_to_all': True, 'target_mode': 'implicit_event_context', 'sellable': True, 'consuming': None, 'available_amount': 1, 'flags': ['usable', 'apply_to_all'], 'price': 640}
    assert result['inventory'][1]['target_mode'] == 'party_role'


def test_agent_get_equipment_compares_candidate_for_eligible_roles(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    current = equipment_item(
        196, '头巾', modifiers={'defense': 2}, traits=[{'kind': 'attack_all', 'value': False}]
    )
    current.update({'slot_id': 0, 'slot_key': 'head'})
    candidate = equipment_item(
        201,
        '皮帽',
        modifiers={'defense': 5, 'dexterity': -1},
        traits=[
            {'kind': 'attack_all', 'value': True},
            {'kind': 'persistent_poison', 'poison_id': 563, 'name': 'HP回补'},
            {'kind': 'persistent_poison', 'poison_id': 564, 'name': 'MP回补'},
        ],
        usable=True,
        eligible=(0, 1),
    )
    state['party'] = [
        {
            'role_id': 0,
            'name': '李逍遥',
            'attack_strength': 35,
            'magic_strength': 20,
            'defense': 41,
            'dexterity': 31,
            'flee_rate': 32,
            'poison_resistance': 0,
            'equipment': [current],
        },
        {
            'role_id': 2,
            'name': '赵灵儿',
            'attack_strength': 20,
            'magic_strength': 40,
            'defense': 30,
            'dexterity': 28,
            'flee_rate': 25,
            'poison_resistance': 0,
            'equipment': [],
        },
    ]
    state['inventory'] = [candidate]

    result = debug.run_agent_tool('get_equipment', {'item_id': 201}, state)

    assert result['ok']
    assert result['candidate']['usable'] is True
    assert result['candidate']['equipable'] is True
    assert result['available_count'] == 1
    assert [entry['role_id'] for entry in result['comparisons']] == [0]
    comparison = result['comparisons'][0]
    assert comparison['current_item']['item_id'] == 196
    assert comparison['stat_delta']['defense'] == 3
    assert comparison['stat_delta']['dexterity'] == -1
    assert comparison['resulting_stats']['defense'] == 44
    assert [effect['poison_id'] for effect in comparison['gained_effects']] == [563, 564]
    assert comparison['changed_effects'][0]['kind'] == 'attack_all'


def test_equipment_validation_uses_equipable_flag_independently_from_usable(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    state['party'] = [{'role_id': 0, 'equipment': []}]
    pearl = equipment_item(260, '圣灵珠', usable=True)
    medicine = {
        'item_id': 105,
        'name': '还神丹',
        'amount': 1,
        'amount_in_use': 0,
        'usable': True,
        'equipable': False,
    }
    state['inventory'] = [pearl, medicine]
    write_state(debug, state)

    assert debug.equip_item_validation_error(state, {'item_id': 260, 'role_id': 0}) is None
    assert debug.equip_item_validation_error(state, {'item_id': 105, 'role_id': 0}) == 'not_equipable'


def test_agent_accepts_equip_item_decision_with_compared_slot_fingerprint(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    current = equipment_item(196, '头巾', modifiers={'defense': 2})
    current.update({'slot_id': 0, 'slot_key': 'head'})
    state['party'] = [{'role_id': 0, 'name': '李逍遥', 'equipment': [current]}]
    state['inventory'] = [equipment_item(201, '皮帽', modifiers={'defense': 5})]
    write_state(debug, state)

    result = debug.run_agent_tool(
        'submit_decision',
        {'action': 'equip_item', 'item_id': 201, 'role_id': 0, 'reason': 'Higher defense.'},
        state,
    )

    assert result['ok']
    decision = result['decision']
    assert decision['recommended_intent'] == 'equip_inventory_item'
    assert decision['recommended_action']['tool'] == 'equip_item'
    assert decision['recommended_action']['args'] == {
        'item_id': 201,
        'role_id': 0,
        'expected_slot_item_id': 196,
    }


def test_pending_equipment_review_is_compact_and_get_equipment_uses_it(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    state['party'] = [{'role_id': 0, 'name': '李逍遥', 'equipment': []}]
    state['inventory'] = [equipment_item(201, '皮帽', modifiers={'defense': 5})]
    write_state(debug, state)
    debug.agent_runtime.state['equipment_review'] = {
        'ownership_counts': {'201': 1},
        'pending': [{
            'item_id': 201,
            'name': '皮帽',
            'new_count': 1,
            'first_seen_at': '2026-07-25T00:00:00Z',
        }],
    }

    context = debug.agent_tool_initial_context(state)
    result = debug.run_agent_tool(
        'get_equipment',
        {},
        state,
        tool_cycle=debug.new_agent_tool_cycle(state),
    )

    assert context['equipment_review'] == {
        'pending': True,
        'item_id': 201,
        'name': '皮帽',
        'new_count': 1,
    }
    assert result['candidate']['item_id'] == 201


def test_agent_tool_schema_explains_implicit_event_context_item_target(tmp_path):
    debug = make_debug_state(tmp_path)
    submit = next(
        spec['function'] for spec in debug.agent_tool_specs()
        if spec['function']['name'] == 'submit_decision'
    )
    properties = submit['parameters']['properties']
    assert properties['action']['enum'] == [
        'wait', 'advance_dialog', 'navigate', 'use_item', 'equip_item',
        'buy_item', 'leave_shop',
    ]
    assert properties['target']['enum'] == ['all']
    assert submit['parameters']['required'] == ['action', 'reason']
    assert submit['parameters']['additionalProperties'] is False
    assert {'summary', 'recommended_intent', 'recommended_action', 'evidence', 'risk', 'alternatives'}.isdisjoint(properties)


def test_agent_accepts_valid_single_target_item_recommendation(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    state['party'] = [{'role_id': 0, 'hp': 12, 'max_hp': 100}]
    state['inventory'] = [{'item_id': 88, 'name': '止血草', 'amount': 2, 'amount_in_use': 0, 'usable': True, 'apply_to_all': False, 'consuming': True}]
    write_state(debug, state)
    result = debug.run_agent_tool('submit_decision', {
        'action': 'use_item',
        'item_id': 88,
        'role_id': 0,
        'reason': 'Heal the injured party member.',
    }, state)
    assert result['ok']
    assert result['decision']['recommended_intent'] == 'use_inventory_item'
    assert result['decision']['recommended_action']['tool'] == 'use_item'
    assert result['decision']['recommended_action']['args'] == {'item_id': 88, 'role_id': 0}
    assert not debug.action_path.exists()


def test_agent_rejects_item_target_that_disagrees_with_inventory_metadata(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    state['party'] = [{'role_id': 0}]
    state['inventory'] = [{'item_id': 91, 'amount': 1, 'amount_in_use': 0, 'usable': True, 'apply_to_all': True}]
    write_state(debug, state)
    result = debug.run_agent_tool('submit_decision', {
        'action': 'use_item',
        'item_id': 91,
        'role_id': 0,
        'reason': 'Use the item.',
    }, state)
    assert result['ok']
    decision = result['decision']
    assert decision['recommended_intent'] == 'wait_runtime'
    assert decision['recommended_action']['tool'] == 'no_op'
    assert decision['recommended_action']['args'] == {'reason': 'use_item_invalid', 'error': 'target_must_use_implicit_context'}
    assert decision['validation'] == {
        'status': 'rejected',
        'reason': 'use_item_invalid',
        'item_id': 91,
        'error': 'target_must_use_implicit_context',
    }
    assert not debug.action_path.exists()


def test_agent_accepts_implicit_event_context_item_recommendation(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    state['party'] = [{'role_id': 0}]
    state['inventory'] = [{
        'item_id': 272, 'name': '桂花酒', 'amount': 1, 'amount_in_use': 0,
        'usable': True, 'apply_to_all': True, 'consuming': False,
    }]
    write_state(debug, state)
    result = debug.run_agent_tool('submit_decision', {
        'action': 'use_item',
        'item_id': 272,
        'target': 'all',
        'reason': 'Use the wine in the current NPC event context.',
    }, state)
    assert result['ok']
    assert result['decision']['recommended_action']['args'] == {'item_id': 272, 'target': 'all'}


def test_agent_rejects_world_object_item_target_from_old_contract(tmp_path):
    debug = make_debug_state(tmp_path)
    error = debug.agent_decision_submission_error({
        'action': 'use_item',
        'item_id': 272,
        'target_object_id': 63,
        'reason': 'Give the wine to the NPC.',
    })
    issue = error['error']['issues'][0]
    assert issue['code'] == 'unsupported_field'
    assert issue['field'] == 'target_object_id'



def test_agent_get_inventory_limit_and_empty_inventory(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    state['inventory'] = [{'slot': 0, 'item_id': 1, 'name': 'one', 'amount': 1}, {'slot': 1, 'item_id': 2, 'name': 'two', 'amount': 2}]
    limited = debug.run_agent_tool('get_inventory', {'limit': 1}, state)
    empty = debug.run_agent_tool('get_inventory', {}, {**state, 'inventory': []})
    assert limited['total'] == 2
    assert limited['returned'] == 1
    assert [item['item_id'] for item in limited['inventory']] == [1]
    assert empty['inventory'] == []
    assert empty['total'] == 0
    assert empty['returned'] == 0



def test_agent_tools_return_json_serializable_dicts(tmp_path):
    target = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 21, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target])
    write_state(debug, state)
    debug.event_log = [{'type': 'inventory_changed'}]
    debug.dialog_history = [{'text': 'hello'}]
    calls = [('upsert_task', {'description': 'Inspect object 48', 'rationale': 'Observed in local event history.', 'object_id': 48}), ('get_tasks', {'include_closed': True, 'limit': 5}), ('get_navigation', {'limit': 5}), ('inspect_target', {'id': 48, 'limit': 1}), ('get_observations', {'kind': 'both', 'limit': 1}), ('get_observations', {'kind': 'speakers', 'limit': 1}), ('get_inventory', {'limit': 1}), ('get_shop', {}), ('get_room_memory', {'room_id': debug.room_identity(state)['id']}), ('get_tool_output', {'output_ref': 'tool-' + ('0' * 32) + '.txt'}), ('submit_decision', {'action': 'wait', 'reason': 'Wait.'})]
    for (name, arguments) in calls:
        result = debug.run_agent_tool(name, arguments, state)
        assert isinstance(result, dict)
        json.dumps(result)
    assert not debug.action_path.exists()
