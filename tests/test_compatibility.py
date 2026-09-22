import json

from tests.support import make_debug_state, make_state



def test_old_exploration_version_is_discarded(tmp_path):
    debug = make_debug_state(tmp_path)
    debug.exploration_path.write_text(json.dumps({'version': 8, 'rooms': {'transient': {'scene_id': 3}}}), encoding='utf-8')
    data = debug.read_exploration()
    assert data['version'] == 11
    assert data['rooms'] == {}
    assert data['transitions'] == {}



def test_old_agent_log_room_ids_are_ignored(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    debug.agent_log_path.write_text(json.dumps({'turn': 1, 'episode': 'lr0:3:10:' + 'a' * 64, 'decision': {}, 'nav_room_id': 'lr0:3:10:' + 'a' * 64}) + '\n' + json.dumps({'turn': 2, 'episode': debug.room_identity(state)['id'], 'decision': {}, 'nav_room_id': debug.room_identity(state)['id']}) + '\n', encoding='utf-8')
    log = debug.read_agent_log(limit=10)
    assert len(log) == 1
    assert log[0]['turn'] == 2



def test_legacy_dialog_feedback_requires_room_and_region_match(tmp_path):
    target = {'id': 57, 'x': 16, 'y': 8, 'state': 1, 'trigger_mode': 1, 'trigger_script': 100, 'auto_script': 0, 'blocking': False, 'interactable': True}
    weak_target = {'id': 58, 'x': 32, 'y': 16, 'state': 1, 'trigger_mode': 1, 'trigger_script': 101, 'auto_script': 0, 'blocking': False, 'interactable': True}
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[target, weak_target])
    state['region_key'] = '3:10:upper-region'
    identity = debug.room_identity(state)
    debug.dialog_history = [{'frame': 50, 'room_key': identity['room_key'], 'region_key': identity['region_key'], 'scene_id': identity['scene_id'], 'map_id': identity['map_id'], 'event_object_id': 57, 'message_id': 588, 'speaker_hint': '李大娘', 'text': '還不快出來幫忙招呼客人', 'agent_turn': 1}, {'frame': 51, 'room_key': identity['room_key'], 'scene_id': identity['scene_id'], 'map_id': identity['map_id'], 'event_object_id': 58, 'message_id': 589, 'speaker_hint': '李大娘', 'text': 'weak legacy entry', 'agent_turn': 1}]
    debug.agent_log_path.write_text(json.dumps({'turn': 1, 'episode': identity['id'], 'nav_room_id': identity['nav_room_id'], 'decision': {}}) + '\n', encoding='utf-8')
    navigation = debug.run_agent_tool('get_navigation', {}, state)['navigation']
    targets = {item['id']: item for item in navigation['targets']}
    assert targets[57]['interaction_state'] == 'recent_dialog'
    assert targets[57]['agent_priority'] == 'avoid_repeat'
    assert targets[58]['interaction_state'] == 'fresh'
    assert targets[58]['agent_priority'] == 'normal'
    assert 'recent_dialog_feedback' not in targets[58]
