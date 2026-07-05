import json
import tempfile
import threading

from conftest import make_debug_state, make_state, write_state



def test_state_events_record_inventory_change():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        debug.record_state_events(state)
        changed = make_state()
        changed['inventory'] = [{'slot': 0, 'item_id': 99, 'amount': 1, 'amount_in_use': 0}]
        debug.record_state_events(changed)
        assert len(debug.event_log) == 1
        assert debug.event_log[0]['type'] == 'inventory_changed'
        assert debug.event_log[0]['item_id'] == 99
        assert debug.event_log[0]['delta'] == 1
        assert debug.events_path.exists()



def test_state_events_record_trigger_metadata_change():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        closed = make_state(objects=[{'id': 10, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}])
        opened = make_state(objects=[{'id': 10, 'x': 32, 'y': 0, 'sprite_id': 0, 'state': 1, 'trigger_mode': 4, 'trigger_script': 4520, 'auto_script': 0, 'blocking': False, 'interactable': True}])
        debug.record_state_events(closed)
        debug.record_state_events(opened)
        assert len(debug.event_log) == 1
        event = debug.event_log[0]
        assert event['type'] == 'object_changed'
        assert event['object_id'] == 10
        assert event['changes']['trigger_mode'] == {'from': 1, 'to': 4}
        assert event['changes']['trigger_script'] == {'from': 100, 'to': 4520}



def test_dialog_event_is_persisted_once():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        state['dialog'] = {'active': True, 'position': 'lower', 'events': [{'sequence': 1, 'frame': 42, 'scene_id': 3, 'map_id': 10, 'event_object_id': 7, 'message_id': 88, 'dialog_position': 'lower', 'speaker_hint': '赵灵儿', 'text': '我们走吧。'}]}
        debug.record_dialog_event(state)
        debug.record_dialog_event(state)
        assert len(debug.dialog_history) == 1
        assert debug.dialog_history[0]['speaker_hint'] == '赵灵儿'
        assert debug.dialog_history[0]['text'] == '我们走吧。'
        assert debug.dialog_history[0]['room_key'] == 'test-room'
        assert debug.dialogs_path.exists()


def test_dialog_history_write_is_safe_when_requests_overlap():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        debug.dialog_history = [{'speaker_hint': '赵灵儿', 'text': '我们走吧。'}]
        errors = []
        barrier = threading.Barrier(8)

        def write_many():
            barrier.wait()
            for _ in range(50):
                try:
                    debug.write_dialog_history()
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=write_many) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert json.loads(debug.dialogs_path.read_text(encoding='utf-8')) == debug.dialog_history
        assert list(debug.runtime_dir.glob('dialog_history.json.*.tmp')) == []



def test_dialog_speaker_title_event_is_not_persisted_as_body_text():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        state['dialog'] = {'events': [{'sequence': 1, 'frame': 7842, 'message_id': 635, 'event_object_id': 57, 'speaker_hint': '李大娘', 'text': '李大娘：'}, {'sequence': 2, 'frame': 7843, 'message_id': 636, 'event_object_id': 57, 'speaker_hint': '李大娘', 'text': '逍遥你来的正好，快把门口'}]}
        debug.record_dialog_event(state)
        debug.record_dialog_event(state)
        assert len(debug.dialog_history) == 1
        assert debug.dialog_history[0]['speaker_hint'] == '李大娘'
        assert debug.dialog_history[0]['text'] == '逍遥你来的正好，快把门口'



def test_dialog_event_stream_merges_contiguous_lines_once():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        state['dialog'] = {'active': False, 'position': 'upper', 'latest_event': {'sequence': 3, 'message_id': 638, 'event_object_id': 57, 'text': '碍咱们做生意'}, 'events': [{'sequence': 1, 'frame': 7843, 'message_id': 636, 'event_object_id': 57, 'speaker_hint': '李大娘', 'text': '逍遥你来的正好，快把门口'}, {'sequence': 2, 'frame': 7843, 'message_id': 637, 'event_object_id': 57, 'speaker_hint': '李大娘', 'text': '那个臭要饭的赶走，免得妨'}, {'sequence': 3, 'frame': 7843, 'message_id': 638, 'event_object_id': 57, 'speaker_hint': '李大娘', 'text': '碍咱们做生意'}]}
        debug.record_dialog_event(state)
        debug.record_dialog_event(state)
        assert len(debug.dialog_history) == 1
        assert debug.dialog_history[0]['message_ids'] == [636, 637, 638]
        assert debug.dialog_history[0]['start_sequence'] == 1
        assert debug.dialog_history[0]['end_sequence'] == 3
        assert debug.dialog_history[0]['text'] == '逍遥你来的正好，快把门口那个臭要饭的赶走，免得妨碍咱们做生意'



def test_dialog_event_stream_splits_when_speaker_changes():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        state['dialog'] = {'events': [{'sequence': 1, 'frame': 10, 'message_id': 636, 'event_object_id': 57, 'speaker_hint': '李大娘', 'text': '逍遥你来的正好，'}, {'sequence': 2, 'frame': 10, 'message_id': 637, 'event_object_id': 57, 'speaker_hint': '李大娘', 'text': '快把门口的人赶走。'}, {'sequence': 3, 'frame': 11, 'message_id': 638, 'event_object_id': 57, 'speaker_hint': '李逍遥', 'text': '啊！'}, {'sequence': 4, 'frame': 11, 'message_id': 639, 'event_object_id': 57, 'speaker_hint': '李逍遥', 'text': '我马上就去。'}]}
        debug.record_dialog_event(state)
        assert [entry['speaker_hint'] for entry in debug.dialog_history] == ['李大娘', '李逍遥']
        assert debug.dialog_history[0]['text'] == '逍遥你来的正好，快把门口的人赶走。'
        assert debug.dialog_history[1]['text'] == '啊！我马上就去。'



def test_system_dialog_event_stays_separate_from_character_turn():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        state['dialog'] = {'events': [{'sequence': 1, 'frame': 20, 'message_id': 630, 'event_object_id': 57, 'speaker_hint': '李大娘', 'text': '拿去吧。'}, {'sequence': 2, 'frame': 21, 'message_id': 631, 'event_object_id': 57, 'speaker_hint': None, 'text': '得到５００文钱'}, {'sequence': 3, 'frame': 22, 'message_id': 632, 'event_object_id': 57, 'speaker_hint': '李大娘', 'text': '别乱花。'}]}
        debug.record_dialog_event(state)
        assert [entry['speaker_hint'] for entry in debug.dialog_history] == ['李大娘', '系统', '李大娘']
        assert [entry['text'] for entry in debug.dialog_history] == ['拿去吧。', '得到５００文钱', '别乱花。']



def test_dialogs_view_filters_by_speaker():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        first = make_state()
        first['dialog'] = {'events': [{'sequence': 1, 'frame': 1, 'message_id': 1, 'event_object_id': 10, 'speaker_hint': '李逍遥', 'text': '好。'}]}
        second = make_state()
        second['dialog'] = {'events': [{'sequence': 2, 'frame': 2, 'message_id': 2, 'event_object_id': 11, 'speaker_hint': '赵灵儿', 'text': '谢谢。'}]}
        debug.record_dialog_event(first)
        debug.record_dialog_event(second)
        view = debug.dialogs_view(speaker='赵灵儿')
        assert view['total'] == 1
        assert view['dialogs'][0]['text'] == '谢谢。'



def test_opening_inner_monologue_is_attributed_to_li_xiaoyao():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        state['scene_id'] = 1
        state['map_id'] = 12
        state['dialog'] = {'events': [{'sequence': 1, 'frame': 58, 'scene_id': 1, 'map_id': 12, 'event_object_id': 5, 'message_id': 1957, 'dialog_position': 'lower', 'speaker_hint': None, 'text': '昨晚做好的密道正好派上用场'}]}
        debug.record_dialog_event(state)
        assert debug.dialog_history[0]['speaker_hint'] == '李逍遙'



def test_reward_dialog_is_attributed_to_system():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        state['dialog'] = {'events': [{'sequence': 1, 'frame': 2060, 'scene_id': 3, 'map_id': 10, 'event_object_id': 57, 'message_id': 631, 'dialog_position': 'center_window', 'speaker_hint': None, 'text': '得到５００文钱'}]}
        debug.record_dialog_event(state)
        assert debug.dialog_history[0]['speaker_hint'] == '系统'



def test_snapshot_clears_runtime_history_when_frame_restarts():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        state['frame'] = 100
        write_state(debug, state)
        debug.snapshot()
        debug.append_agent_decision(state, {'recommended_intent': 'inspect_target'})
        debug.event_log = [{'type': 'inventory_changed'}]
        debug.write_events()
        debug.dialog_history = [{'text': 'old dialog'}]
        debug.write_dialog_history()
        restarted = make_state()
        restarted['frame'] = 1
        write_state(debug, restarted)
        snapshot = debug.snapshot()
        assert snapshot['frame'] == 1
        assert debug.read_agent_log(limit=10) == []
        assert debug.event_log == []
        assert debug.dialog_history == []
        assert not debug.agent_log_path.exists()
        assert not debug.events_path.exists()
        assert not debug.dialogs_path.exists()



def test_dialog_dedupe_uses_room_object_message_and_text_not_frame_or_sequence():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        first = make_state()
        first['frame'] = 10
        first['dialog'] = {'events': [{'sequence': 1, 'frame': 10, 'message_id': 588, 'event_object_id': 25, 'speaker_hint': '李大娘', 'text': '呵～累了，想早點休息'}]}
        second = make_state()
        second['frame'] = 20
        second['dialog'] = {'events': [{'sequence': 99, 'frame': 20, 'message_id': 588, 'event_object_id': 25, 'speaker_hint': '李大娘', 'text': '呵～累了，想早點休息'}]}
        debug.apply_frame_timeline(first)
        debug.record_dialog_event(first)
        debug.apply_frame_timeline(second)
        debug.record_dialog_event(second)
        assert len(debug.dialog_history) == 1
        assert debug.dialog_history[0]['text'] == '呵～累了，想早點休息'
        assert debug.dialog_history[0]['raw_frame'] == 10



def test_dialog_dedupe_keeps_identical_lines_in_different_full_rooms():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        lower = make_state()
        lower['region_key'] = '3:10:lower-region'
        lower['dialog'] = {'events': [{'sequence': 1, 'frame': 10, 'message_id': 588, 'event_object_id': 57, 'speaker_hint': '李大娘', 'text': '呵～累了，想早點休息'}]}
        upper = make_state()
        upper['region_key'] = '3:10:upper-region'
        upper['dialog'] = {'events': [{'sequence': 1, 'frame': 20, 'message_id': 588, 'event_object_id': 57, 'speaker_hint': '李大娘', 'text': '呵～累了，想早點休息'}]}
        debug.apply_frame_timeline(lower)
        debug.record_dialog_event(lower)
        debug.apply_frame_timeline(upper)
        debug.record_dialog_event(upper)
        assert len(debug.dialog_history) == 2
        assert debug.dialog_history[0]['room_id'] != debug.dialog_history[1]['room_id']



def test_recent_dialog_feedback_decays_by_agent_turn():
    dialog_target = {'id': 57, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    fresh_target = {'id': 58, 'x': 32, 'y': 16, 'state': 1, 'trigger_mode': 1, 'trigger_script': 101, 'auto_script': 0, 'blocking': False, 'interactable': True}
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[dialog_target, fresh_target])
        write_state(debug, state)
        identity = debug.room_identity(state)
        debug.dialog_history = [{'frame': 50, 'room_id': identity['id'], 'room_key': identity['room_key'], 'region_key': identity['region_key'], 'nav_room_id': identity['nav_room_id'], 'scene_id': identity['scene_id'], 'map_id': identity['map_id'], 'event_object_id': 57, 'message_id': 588, 'speaker_hint': '苗人頭領', 'text': '你們跑不掉了', 'agent_turn': 1}]
        debug.agent_log_path.write_text('\n'.join((json.dumps({'turn': turn, 'episode': identity['id'], 'nav_room_id': identity['nav_room_id'], 'decision': {}}) for turn in (1, 2, 3))) + '\n', encoding='utf-8')
        navigation = debug.run_agent_tool('get_navigation', {'mode': 'full'}, state)['navigation']
        targets = {item['id']: item for item in navigation['targets']}
        assert targets[57]['agent_priority'] == 'soft_avoid'
        assert targets[57]['dialog_feedback_state'] == 'soft_avoid'
        decision = debug.rules_decide_agent(state)
        assert decision['recommended_action']['args'] == {'id': 58}
        debug.agent_log_path.write_text('\n'.join((json.dumps({'turn': turn, 'episode': identity['id'], 'nav_room_id': identity['nav_room_id'], 'decision': {}}) for turn in (1, 2, 3, 4))) + '\n', encoding='utf-8')
        navigation = debug.run_agent_tool('get_navigation', {'mode': 'full'}, state)['navigation']
        targets = {item['id']: item for item in navigation['targets']}
        assert targets[57]['interaction_state'] == 'fresh'
        assert targets[57]['agent_priority'] == 'normal'
        assert 'recent_dialog_feedback' not in targets[57]
