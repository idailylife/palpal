import json
import os
import tempfile
import time

from conftest import make_debug_state, make_state, write_state



def test_agent_decide_does_not_write_action_file():
    target = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 21, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[target])
        write_state(debug, state)
        decision = debug.rules_decide_agent(state)
        assert decision['recommended_intent'] == 'inspect_target'
        assert not debug.action_path.exists()



def test_agent_decide_reports_stale_runtime():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        write_state(debug, state)
        old = time.time() - 10
        os.utime(debug.state_path, (old, old))
        decision = debug.rules_decide_agent(state)
        assert decision['recommended_intent'] == 'wait_runtime'
        assert decision['recommended_action']['args']['reason'] == 'runtime_stale'
        assert not debug.action_path.exists()



def test_agent_decide_prioritizes_active_dialog():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        state['dialog'] = {'active': True, 'position': 'lower', 'latest_event': {'message_id': 88, 'event_object_id': 7, 'text': '我们走吧。'}}
        write_state(debug, state)
        decision = debug.rules_decide_agent(state)
        assert decision['recommended_intent'] == 'advance_dialog'
        assert decision['recommended_action']['tool'] == 'advance_dialog'
        assert decision['recommended_action']['args'] == {}
        assert not debug.action_path.exists()



def test_advance_dialog_clamps_options():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        options = debug.normalize_dialog_advance_args({'max_presses': 999, 'delay_ms': 1, 'stop_on_repeat': False})
        assert options == {'max_presses': 100, 'delay_ms': 100, 'stop_on_repeat': False}



def test_advance_dialog_stops_when_dialog_inactive():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        write_state(debug, state)
        debug.frame_path.write_bytes(b'frame')
        result = debug.advance_dialog({'max_presses': 5})
        assert result['ok']
        assert result['status'] == 'dialog_inactive'
        assert result['presses'] == 0
        assert not debug.action_path.exists()



def test_agent_decision_log_records_turn_and_decision():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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



def test_agent_normalizes_dialog_intent_to_advance_dialog():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        decision = debug.normalize_agent_decision({'summary': 'Dialog is active.', 'recommended_intent': 'advance_dialog', 'recommended_action': {'tool': 'press_buttons', 'args': {'buttons': ['A', 'bad'], 'frames': 1}}, 'evidence': {}, 'risk': 'None.', 'alternatives': []})
        assert decision['recommended_action']['tool'] == 'advance_dialog'
        assert decision['recommended_action']['args'] == {'max_presses': 100, 'delay_ms': 500, 'stop_on_repeat': True}
        assert not debug.action_path.exists()



def test_agent_normalizes_explore_room_to_wait_runtime():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        decision = debug.normalize_agent_decision({'summary': 'Explore.', 'recommended_intent': 'explore_room', 'recommended_action': {'tool': 'no_op', 'args': {}}, 'evidence': {}, 'risk': 'Low.', 'alternatives': []})
        assert decision['recommended_intent'] == 'wait_runtime'
        assert decision['recommended_action']['tool'] == 'no_op'
        assert decision['recommended_action']['args'] == {}
        assert 'normalized unknown intent to wait_runtime' in decision['evidence']['normalization_warnings']
        assert not debug.action_path.exists()



def test_agent_rejects_model_exploration_step():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        decision = debug.normalize_agent_decision({'summary': 'Explore farther.', 'recommended_intent': 'explore_room', 'recommended_action': {'tool': 'exploration_step', 'args': {'max_steps': 12}}, 'evidence': {}, 'risk': 'Low.', 'alternatives': []})
        assert decision['recommended_intent'] == 'wait_runtime'
        assert decision['recommended_action']['tool'] == 'no_op'
        assert decision['recommended_action']['args']['reason'] == 'unsupported_tool'
        assert decision['recommended_action']['args']['tool'] == 'exploration_step'
        assert 'normalized unknown intent to wait_runtime' in decision['evidence']['normalization_warnings']
        assert 'normalized unknown action tool to no_op' in decision['evidence']['normalization_warnings']
        assert not debug.action_path.exists()



def test_agent_rejects_targeted_explore_room():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        decision = debug.normalize_agent_decision({'summary': 'Explore toward object #74.', 'recommended_intent': 'explore_room', 'recommended_action': {'tool': 'exploration_step', 'args': {'max_steps': 1}}, 'evidence': {'target': {'id': 74}}, 'risk': 'Low.', 'alternatives': []})
        assert decision['recommended_intent'] == 'wait_runtime'
        assert decision['recommended_action']['tool'] == 'no_op'
        assert decision['recommended_action']['args']['reason'] == 'unsupported_tool'
        assert decision['recommended_action']['args']['tool'] == 'exploration_step'
        assert 'normalized unknown intent to wait_runtime' in decision['evidence']['normalization_warnings']
        assert 'normalized unknown action tool to no_op' in decision['evidence']['normalization_warnings']
        assert not debug.action_path.exists()



def test_agent_does_not_infer_target_from_summary():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        decision = debug.normalize_agent_decision({'summary': 'Target 10 should be inspected.', 'recommended_intent': 'inspect_target', 'recommended_action': {'tool': 'exploration_step', 'args': {}}, 'evidence': {}, 'risk': 'Low.', 'alternatives': []})
        assert decision['recommended_intent'] == 'wait_runtime'
        assert decision['recommended_action']['tool'] == 'no_op'
        assert decision['recommended_action']['args']['reason'] == 'missing target id for inspect_target'
        assert not debug.action_path.exists()



def test_agent_normalizes_alternatives_to_match_intent():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        decision = debug.normalize_agent_decision({'summary': 'Inspect.', 'recommended_intent': 'inspect_target', 'recommended_action': {'tool': 'navigation_step', 'args': {'id': 10}}, 'evidence': {'target': {'id': 10}}, 'risk': 'Low.', 'alternatives': [{'intent': 'inspect_target', 'action': {'tool': 'exploration_step', 'args': {'max_steps': 1}}, 'evidence': {'target': {'id': 11}}, 'reason': 'Other target.'}, {'intent': 'explore_room', 'action': {'tool': 'navigation_step', 'args': {'id': 12}}, 'reason': 'Map first.'}]})
        assert decision['alternatives'][0]['intent'] == 'inspect_target'
        assert decision['alternatives'][0]['action']['tool'] == 'navigation_step'
        assert decision['alternatives'][0]['action']['args'] == {'id': 11}
        assert decision['alternatives'][1]['intent'] == 'wait_runtime'
        assert decision['alternatives'][1]['action']['tool'] == 'no_op'
        assert decision['alternatives'][1]['action']['args'] == {'reason': 'wait_runtime'}
        assert not debug.action_path.exists()



def test_agent_context_marks_targets_with_recent_dialog_feedback():
    target = {'id': 10, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[target])
        identity = debug.room_identity(state)
        dialogs = {'dialogs': [{'frame': 50, 'scene_id': 3, 'map_id': 10, 'room_id': identity['id'], 'room_key': identity['room_key'], 'region_key': identity['region_key'], 'nav_room_id': identity['nav_room_id'], 'event_object_id': 10, 'message_id': 588, 'speaker_hint': '李大娘', 'text': '還不快出來幫忙招呼客人'}]}
        context = debug.agent_context(state, debug.navigation(state), dialogs, events=[])
        target_context = {item['id']: item for item in context['navigation']['targets']}
        assert 'recent_dialog_feedback' in target_context[10]
        assert target_context[10]['recent_dialog_feedback'][0]['text'] == '還不快出來幫忙招呼客人'
        assert '本房间' in target_context[10]['deprioritize_reason']



def test_agent_context_does_not_label_object_from_speaker_hint():
    target = {'id': 57, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[target])
        identity = debug.room_identity(state)
        dialogs = {'dialogs': [{'frame': 50, 'scene_id': identity['scene_id'], 'map_id': identity['map_id'], 'room_id': identity['id'], 'room_key': identity['room_key'], 'region_key': identity['region_key'], 'nav_room_id': identity['nav_room_id'], 'event_object_id': 57, 'message_id': 588, 'speaker_hint': '苗人頭領', 'text': '你們跑不掉了'}]}
        context = debug.agent_context(state, debug.navigation(state), dialogs, events=[])
        payload = json.dumps(context, ensure_ascii=False)
        assert '苗人頭領' in payload
        assert 'ID 57 (苗人頭領)' not in payload
        assert '#57 (苗人頭領)' not in payload



def test_agent_tools_mark_recently_cleared_search_targets():
    left_door = {'id': 74, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    right_door = {'id': 75, 'x': 32, 'y': 16, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[left_door, right_door])
        write_state(debug, state)
        identity = debug.room_identity(state)
        debug.agent_log_path.write_text('\n'.join((json.dumps({'turn': turn, 'episode': identity['id'], 'nav_room_id': identity['nav_room_id'], 'decision': {}}) for turn in (1, 2, 3, 4))) + '\n', encoding='utf-8')
        debug.event_log = [{'frame': 20, 'scene_id': 3, 'map_id': 10, 'room_key': 'test-room', 'type': 'object_changed', 'object_id': 75, 'changes': {'state': {'from': 2, 'to': 1}, 'blocking': {'from': True, 'to': False}}}]
        navigation = debug.run_agent_tool('get_navigation', {'mode': 'full'}, state)['navigation']
        targets = {target['id']: target for target in navigation['targets']}
        route = debug.run_agent_tool('get_route', {'id': 75}, state)['route']
        assert targets[75]['interaction_state'] == 'previously_cleared'
        assert targets[75]['agent_priority'] == 'avoid_repeat'
        assert targets[74]['interaction_state'] == 'previously_cleared'
        assert targets[74]['agent_priority'] == 'avoid_repeat'
        assert route['interaction_state'] == 'previously_cleared'
        assert route['agent_priority'] == 'avoid_repeat'


def test_interaction_no_effect_is_recorded_and_deprioritizes_target():
    target = {'id': 75, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[target], frame=20)
        write_state(debug, state)
        debug.mark_pending_interaction_observation(
            state,
            75,
            action={'buttons': ['search'], 'frames': 1},
        )
        next_state = make_state(objects=[target], frame=30)
        debug.observe_pending_interaction_outcome(next_state)

        assert debug.event_log[-1]['type'] == 'interaction_no_effect'
        assert debug.event_log[-1]['object_id'] == 75
        navigation = debug.run_agent_tool('get_navigation', {'mode': 'full'}, next_state)['navigation']
        targets = {target['id']: target for target in navigation['targets']}
        history = debug.run_agent_tool('get_object_history', {'id': 75}, next_state)
        assert targets[75]['interaction_state'] == 'previously_tried'
        assert targets[75]['agent_priority'] == 'avoid_repeat'
        assert history['history'][-1]['type'] == 'interaction_no_effect'


def test_interaction_effect_attributes_inventory_change_to_target():
    target = {'id': 75, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[target], frame=20)
        state['inventory'] = []
        debug.mark_pending_interaction_observation(
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
        route = debug.run_agent_tool('get_route', {'id': 75}, next_state)['route']
        assert route['interaction_state'] == 'previously_tried'
        assert route['agent_priority'] == 'avoid_repeat'



def test_recently_cleared_search_targets_survive_region_changes():
    left_door = {'id': 74, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    right_door = {'id': 75, 'x': 32, 'y': 16, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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
        navigation = debug.run_agent_tool('get_navigation', {'mode': 'full'}, state)['navigation']
        targets = {target['id']: target for target in navigation['targets']}
        route = debug.run_agent_tool('get_route', {'id': 74}, state)['route']
        assert targets[75]['interaction_state'] == 'previously_cleared'
        assert targets[75]['agent_priority'] == 'avoid_repeat'
        assert targets[74]['interaction_state'] == 'previously_cleared'
        assert targets[74]['agent_priority'] == 'avoid_repeat'
        assert route['interaction_state'] == 'previously_cleared'
        assert route['agent_priority'] == 'avoid_repeat'



def test_recently_cleared_search_targets_do_not_cross_nav_rooms():
    door = {'id': 75, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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
        navigation = debug.run_agent_tool('get_navigation', {'mode': 'full'}, state)['navigation']
        targets = {target['id']: target for target in navigation['targets']}
        assert targets[75]['interaction_state'] == 'fresh'
        assert targets[75]['agent_priority'] == 'normal'



def test_agent_rules_skip_recently_cleared_search_target():
    opened_door = {'id': 75, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 8149, 'auto_script': 0, 'blocking': False, 'interactable': True}
    fresh_target = {'id': 80, 'x': 32, 'y': 16, 'state': 1, 'trigger_mode': 1, 'trigger_script': 9000, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[opened_door, fresh_target])
        write_state(debug, state)
        debug.event_log = [{'frame': 20, 'scene_id': 3, 'map_id': 10, 'room_key': 'test-room', 'type': 'object_changed', 'object_id': 75, 'changes': {'state': {'from': 2, 'to': 1}, 'blocking': {'from': True, 'to': False}}}]
        decision = debug.rules_decide_agent(state)
        assert decision['recommended_intent'] == 'inspect_target'
        assert decision['recommended_action']['args'] == {'id': 80}
        assert decision['evidence']['target']['interaction_state'] == 'fresh'
        assert not debug.action_path.exists()



def test_agent_messages_put_stable_policy_before_dynamic_context():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        messages = debug.agent_messages(state, debug.navigation(state), {'dialogs': []}, events=[])
        assert [message['role'] for message in messages] == ['system', 'system', 'user']
        assert 'JSON object' in messages[0]['content']
        assert '距离是成本信号，不是优先级规则' in messages[1]['content']
        assert '不要匹配固定剧情短语' in messages[1]['content']
        assert '由非传送目标变为传送目标' in messages[1]['content']
        assert 'known_destination=false' in messages[1]['content']
        assert 'previously_tried 或 avoid_repeat 不应降低它的优先级' in messages[1]['content']
        assert '0xFFFF' in messages[1]['content']
        assert "use_item {item_id:<id>,target:'all'}" in messages[1]['content']
        assert '不要因 target_object_id 被拒绝' in messages[1]['content']
        assert '进程重启或载入存档不会恢复最近事件对象' in messages[1]['content']
        assert 'prefer the nearest reachable non-transition target' not in messages[1]['content']
        assert 'greet guests' not in messages[1]['content']
        assert 'secret passage' not in messages[1]['content']
        dynamic = json.loads(messages[2]['content'])
        assert 'state' in dynamic
        assert 'exploration' in dynamic
        assert 'last_route' in dynamic['exploration']
        assert 'rules' not in dynamic


def test_agent_context_exposes_newly_unlocked_unknown_transition_signals():
    opened = {'id': 10, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4520, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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



def test_agent_tool_specs_include_queries_and_submit():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        tools = debug.agent_tool_specs()
        names = [tool['function']['name'] for tool in tools]
        by_name = {tool['function']['name']: tool for tool in tools}
        assert set(names) == {'get_tasks', 'upsert_task', 'get_state_summary', 'get_navigation', 'get_dialogs', 'get_dialog_speakers', 'get_inventory', 'get_events', 'get_object_history', 'get_agent_history', 'get_route', 'get_room_exploration', 'get_known_transitions', 'submit_decision'}
        dialog_properties = by_name['get_dialogs']['function']['parameters']['properties']
        assert 'speaker' in dialog_properties
        assert dialog_properties['speaker']['type'] == 'string'
        object_history_properties = by_name['get_object_history']['function']['parameters']['properties']
        assert 'object_id' in object_history_properties
        assert 'event_object_id' in object_history_properties
        assert '返回导航目标' in by_name['get_navigation']['function']['description']
        assert '提交最终 dry-run 决策' in by_name['submit_decision']['function']['description']
        context = debug.agent_tool_initial_context(make_state())
        assert 'get_tasks' in context['available_query_tools']
        assert context['active_tasks'] == []
        assert 'get_dialog_speakers' in context['available_query_tools']
        assert 'get_object_history' in context['available_query_tools']
        assert any('所有建议' in item for item in context['constraints'])
        assert names[-1] == 'submit_decision'



def test_agent_get_object_history_returns_state_events_and_dialogs():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[{'id': 10, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4520, 'auto_script': 0, 'blocking': False, 'interactable': True}])
        debug.event_log = [
            {'type': 'object_changed', 'object_id': 9, 'changes': {'state': {'from': 1, 'to': 0}}, 'time': 1.0, 'frame': 1},
            {'type': 'object_changed', 'object_id': 10, 'changes': {'trigger_mode': {'from': 1, 'to': 4}}, 'time': 2.0, 'frame': 2},
        ]
        debug.dialog_history = [
            {'event_object_id': 10, 'speaker_hint': '系统', 'text': '机关打开了。', 'time': 3.0, 'frame': 3, 'message_id': 123},
            {'event_object_id': 11, 'speaker_hint': '系统', 'text': '其他对象。', 'time': 4.0, 'frame': 4, 'message_id': 124},
        ]
        result = debug.run_agent_tool('get_object_history', {'object_id': 10}, state)
        assert result['ok']
        assert result['object_id'] == 10
        assert result['current']['kind'] == 'touch'
        assert result['current']['transition_candidate']
        assert result['total'] == 2
        assert [entry['source'] for entry in result['history']] == ['events', 'dialogs']
        assert result['history'][0]['changes']['trigger_mode'] == {'from': 1, 'to': 4}
        assert result['history'][1]['text'] == '机关打开了。'



def test_agent_get_dialog_speakers_returns_recent_speakers():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        debug.dialog_history = [{'speaker_hint': '李大娘', 'time': 10.0, 'frame': 100, 'end_frame': 101, 'start_sequence': 1, 'end_sequence': 2, 'scene_id': 3, 'map_id': 10, 'room_key': 'inn', 'text': '早点回来。'}, {'speaker_hint': '赵灵儿', 'time': 20.0, 'frame': 200, 'end_frame': 201, 'start_sequence': 3, 'end_sequence': 3, 'scene_id': 4, 'map_id': 11, 'room_key': 'garden', 'text': '谢谢。'}, {'speaker_hint': '李大娘', 'time': 30.0, 'frame': 300, 'end_frame': 302, 'start_sequence': 4, 'end_sequence': 5, 'scene_id': 5, 'map_id': 12, 'room_key': 'street', 'text': '快去吧。'}]
        result = debug.run_agent_tool('get_dialog_speakers', {}, state)
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



def test_agent_get_dialog_speakers_excludes_system_and_empty_speakers():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        debug.dialog_history = [{'speaker_hint': '系统', 'time': 1.0, 'text': '得到５００文钱'}, {'speaker_hint': '', 'time': 2.0, 'text': 'empty'}, {'time': 3.0, 'text': 'missing'}, {'speaker_hint': '李逍遥', 'time': 4.0, 'frame': 40, 'text': '好。'}]
        result = debug.run_agent_tool('get_dialog_speakers', {}, state)
        assert result['total'] == 1
        assert result['returned'] == 1
        assert result['speakers'][0]['speaker_hint'] == '李逍遥'



def test_agent_get_dialogs_filters_by_speaker():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        debug.dialog_history = [{'speaker_hint': '李大娘', 'time': 10.0, 'frame': 100, 'end_frame': 101, 'text': '早点回来。'}, {'speaker_hint': '赵灵儿', 'time': 20.0, 'frame': 200, 'end_frame': 201, 'text': '谢谢。'}, {'speaker_hint': '李大娘', 'time': 30.0, 'frame': 300, 'end_frame': 301, 'text': '快去吧。'}]
        result = debug.run_agent_tool('get_dialogs', {'speaker': '李大娘', 'limit': 10}, state)
        assert result['ok']
        assert result['total'] == 2
        assert [dialog['speaker_hint'] for dialog in result['dialogs']] == ['李大娘', '李大娘']
        assert [dialog['text'] for dialog in result['dialogs']] == ['早点回来。', '快去吧。']
        assert result['dialogs'][0]['time'] == 10.0
        assert result['dialogs'][0]['frame'] == 100
        assert result['dialogs'][0]['end_frame'] == 101



def test_agent_get_inventory_returns_current_items():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        state['inventory'] = [{'slot': 0, 'item_id': 91, 'name': '十里香', 'price': 640, 'flags': ['usable', 'apply_to_all'], 'usable': True, 'apply_to_all': True, 'sellable': True, 'amount': 1, 'amount_in_use': 0, 'equipped_count': 0}, {'slot': 1, 'item_id': 88, 'name': '止血草', 'price': 50, 'flags': ['usable'], 'usable': True, 'apply_to_all': False, 'sellable': True, 'amount': 5, 'amount_in_use': 0, 'equipped_count': 0}]
        result = debug.run_agent_tool('get_inventory', {}, state)
        assert result['ok']
        assert result['total'] == 2
        assert result['returned'] == 2
        assert result['inventory'][0] == {'slot': 0, 'item_id': 91, 'name': '十里香', 'amount': 1, 'amount_in_use': 0, 'equipped_count': 0, 'usable': True, 'apply_to_all': True, 'target_mode': 'implicit_event_context', 'sellable': True, 'consuming': None, 'available_amount': 1, 'flags': ['usable', 'apply_to_all'], 'price': 640}
        assert result['inventory'][1]['target_mode'] == 'party_role'


def test_agent_tool_schema_explains_implicit_event_context_item_target():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        submit = next(
            spec['function'] for spec in debug.agent_tool_specs()
            if spec['function']['name'] == 'submit_decision'
        )
        action = submit['parameters']['properties']['recommended_action']
        description = action['description'] + ' ' + action['properties']['args']['description']
        assert 'implicit_event_context' in description
        assert '0xFFFF' in description
        assert 'most recent event object' in description
        assert 'target_object_id is not supported' in description


def test_agent_accepts_valid_single_target_item_recommendation():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        state['party'] = [{'role_id': 0, 'hp': 12, 'max_hp': 100}]
        state['inventory'] = [{'item_id': 88, 'name': '止血草', 'amount': 2, 'amount_in_use': 0, 'usable': True, 'apply_to_all': False, 'consuming': True}]
        write_state(debug, state)
        result = debug.run_agent_tool('submit_decision', {
            'summary': 'Use a healing item on the injured party member.',
            'recommended_intent': 'use_inventory_item',
            'recommended_action': {'tool': 'use_item', 'args': {'item_id': 88, 'role_id': 0}},
            'evidence': {'item_id': 88, 'hp': 12, 'max_hp': 100},
            'risk': 'The item purpose is inferred from its name.',
            'alternatives': [],
        }, state)
        assert result['ok']
        assert result['decision']['recommended_intent'] == 'use_inventory_item'
        assert result['decision']['recommended_action']['tool'] == 'use_item'
        assert result['decision']['recommended_action']['args'] == {'item_id': 88, 'role_id': 0}
        assert not debug.action_path.exists()


def test_agent_rejects_item_target_that_disagrees_with_inventory_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        state['party'] = [{'role_id': 0}]
        state['inventory'] = [{'item_id': 91, 'amount': 1, 'amount_in_use': 0, 'usable': True, 'apply_to_all': True}]
        write_state(debug, state)
        result = debug.run_agent_tool('submit_decision', {
            'summary': 'Use the item.',
            'recommended_intent': 'use_inventory_item',
            'recommended_action': {'tool': 'use_item', 'args': {'item_id': 91, 'role_id': 0}},
            'evidence': {'item_id': 91},
            'risk': 'Unknown effect.',
            'alternatives': [],
        }, state)
        assert result['ok']
        decision = result['decision']
        assert decision['recommended_intent'] == 'wait_runtime'
        assert decision['recommended_action']['tool'] == 'no_op'
        assert decision['recommended_action']['args'] == {'reason': 'use_item_invalid', 'error': 'target_must_use_implicit_context'}
        assert not debug.action_path.exists()


def test_agent_accepts_implicit_event_context_item_recommendation():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        state['party'] = [{'role_id': 0}]
        state['inventory'] = [{
            'item_id': 272, 'name': '桂花酒', 'amount': 1, 'amount_in_use': 0,
            'usable': True, 'apply_to_all': True, 'consuming': False,
        }]
        write_state(debug, state)
        result = debug.run_agent_tool('submit_decision', {
            'summary': 'Use the wine in the current NPC event context.',
            'recommended_intent': 'use_inventory_item',
            'recommended_action': {'tool': 'use_item', 'args': {'item_id': 272, 'target': 'all'}},
            'evidence': {'item_id': 272, 'recent_npc_interaction': 63},
            'risk': 'The all field means implicit event context here.',
            'alternatives': [],
        }, state)
        assert result['ok']
        assert result['decision']['recommended_action']['args'] == {'item_id': 272, 'target': 'all'}


def test_agent_rejects_world_object_item_target_with_context_repair_guidance():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        error = debug.agent_decision_submission_error({
            'summary': 'Give the wine to the NPC.',
            'recommended_intent': 'use_inventory_item',
            'recommended_action': {'tool': 'use_item', 'args': {'item_id': 272, 'target_object_id': 63}},
            'evidence': {'target_id': 63},
            'risk': 'Low.',
            'alternatives': [],
        })
        issue = error['error']['issues'][0]
        assert issue['code'] == 'unsupported_item_object_target'
        assert "target='all'" in issue['message']
        assert '0xFFFF' in issue['message']
        assert 'most recent event object' in issue['message']



def test_agent_get_inventory_limit_and_empty_inventory():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
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



def test_agent_tools_return_json_serializable_dicts():
    target = {'id': 48, 'x': 32, 'y': 0, 'sprite_id': 21, 'state': 1, 'trigger_mode': 4, 'trigger_script': 60, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[target])
        write_state(debug, state)
        debug.event_log = [{'type': 'inventory_changed'}]
        debug.dialog_history = [{'text': 'hello'}]
        calls = [('get_tasks', {}), ('upsert_task', {'description': 'Inspect object 48', 'rationale': 'Observed in local event history.', 'object_id': 48}), ('get_state_summary', {}), ('get_navigation', {'mode': 'lightweight', 'limit': 5}), ('get_navigation', {'mode': 'full', 'limit': 5}), ('get_dialogs', {'limit': 1}), ('get_dialog_speakers', {'limit': 1}), ('get_inventory', {'limit': 1}), ('get_events', {'limit': 1}), ('get_object_history', {'id': 48, 'limit': 1}), ('get_agent_history', {'limit': 1}), ('get_route', {'id': 48}), ('get_room_exploration', {'room_id': debug.room_identity(state)['id']}), ('get_known_transitions', {}), ('submit_decision', {'summary': 'Wait.', 'recommended_intent': 'wait_runtime', 'recommended_action': {'tool': 'no_op', 'args': {}}, 'evidence': {}, 'risk': 'None.', 'alternatives': []})]
        for (name, arguments) in calls:
            result = debug.run_agent_tool(name, arguments, state)
            assert isinstance(result, dict)
            json.dumps(result)
        assert not debug.action_path.exists()
