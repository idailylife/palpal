import json
import tempfile
from unittest.mock import patch

import pytest

from conftest import fake_load_command, fake_save_command, make_checkpoint_state as make_state, make_startup_state, make_debug_state, write_state



def test_startup_waiting_rejects_save_checkpoint():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        write_state(debug, make_startup_state())
        with pytest.raises(RuntimeError, match='startup_waiting'):
            debug.save_checkpoint('cp1', 'CP1')



def test_startup_waiting_allows_load_checkpoint():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        checkpoint_dir = debug.checkpoint_dir('cp1')
        checkpoint_dir.mkdir(parents=True)
        (checkpoint_dir / 'game.rpg').write_bytes(b'checkpoint-rpg')
        (checkpoint_dir / 'manifest.json').write_text(json.dumps({'version': 1, 'label': 'CP1', 'created_at': '2026-01-01T00:00:00Z'}), encoding='utf-8')
        write_state(debug, make_startup_state())
        with patch.object(debug, 'run_slot_command', side_effect=fake_load_command):
            result = debug.load_checkpoint('cp1')
        assert result['ok']
        assert debug.save_slot_path().read_bytes() == b'checkpoint-rpg'



def test_start_new_game_writes_start_slot_zero():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        write_state(debug, make_startup_state())

        def run(command, slot):
            assert command == 'start_slot'
            assert slot == 0
            return {'action': {'command': command, 'slot': slot}, 'result': {'ok': True, 'action': command, 'slot': slot, 'status': 'start_scheduled'}}
        with patch.object(debug, 'run_slot_command', side_effect=run):
            result = debug.start_new_game()
        assert result['ok']
        assert result['native_result']['action'] == 'start_slot'



def test_start_new_game_allowed_while_game_is_running():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        write_state(debug, make_state(frame=100))
        debug.dialogs_path.write_text('[{"source_key":"old","text":"old"}]', encoding='utf-8')
        debug.events_path.write_text('[{"type":"old"}]', encoding='utf-8')
        debug.agent_log_path.write_text('{"turn":9}\n', encoding='utf-8')
        debug.exploration_path.write_text('{"version":6,"rooms":{},"transitions":{},"object_interactions":{"room":{"75":{"attempt_count":1}}}}', encoding='utf-8')
        debug.snapshot()

        def run(command, slot):
            assert command == 'start_slot'
            assert slot == 0
            return {'action': {'command': command, 'slot': slot}, 'result': {'ok': True, 'action': command, 'slot': slot, 'status': 'start_scheduled'}}
        with patch.object(debug, 'run_slot_command', side_effect=run):
            result = debug.start_new_game()
        assert result['ok']
        assert result['native_result']['action'] == 'start_slot'
        assert not debug.dialogs_path.exists()
        assert not debug.events_path.exists()
        assert not debug.agent_log_path.exists()
        assert not debug.exploration_path.exists()
        assert debug.last_raw_frame is None
        assert debug.logical_frame_offset == 0


def test_start_new_game_is_allowed_while_a_dialog_is_active():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        write_state(debug, {**make_state(frame=100), 'dialog': {'active': True}})

        with patch.object(debug, 'run_slot_command', return_value={'result': {'ok': True}}) as run:
            result = debug.start_new_game()

        assert result['ok']
        run.assert_called_once_with('start_slot', 0)



def test_wait_for_slot_result_accepts_slot_zero():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        start_time = 0.0
        debug.action_result_path.write_text(json.dumps({'ok': True, 'action': 'start_slot', 'slot': 0}), encoding='utf-8')
        result = debug.wait_for_slot_result('start_slot', 0, start_time, timeout=0.1)
        assert result['ok']
        assert result['slot'] == 0



def test_save_checkpoint_writes_manifest_game_and_existing_companion_files():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        write_state(debug)
        debug.dialogs_path.write_text('[{"source_key":"d1","text":"hi"}]', encoding='utf-8')
        debug.exploration_path.write_text('{"version":6,"rooms":{},"transitions":{}}', encoding='utf-8')
        debug.navigation_log_path.parent.mkdir(parents=True, exist_ok=True)
        debug.navigation_log_path.write_text('{"type":"route"}\n', encoding='utf-8')
        with patch.object(debug, 'run_slot_command', side_effect=fake_save_command(debug)):
            result = debug.save_checkpoint('before-door', 'Before Door')
        checkpoint = result['checkpoint']
        checkpoint_dir = debug.checkpoint_dir('before-door')
        assert result['ok']
        assert checkpoint['version'] == 1
        assert checkpoint['companion_version'] == 6
        assert checkpoint['label'] == 'Before Door'
        assert checkpoint['state']['scene_id'] == 3
        assert checkpoint['state']['room_id'].startswith('lr0:3:10:')
        assert checkpoint['state']['logical_room_id'] == checkpoint['state']['room_id']
        assert checkpoint['state']['legacy_room_id'] == '3/10/test-room/test-room'
        assert checkpoint['state']['nav_room_id'] == checkpoint['state']['logical_room_id']
        assert checkpoint['state']['legacy_nav_room_id'] == '3/10/test-room'
        assert (checkpoint_dir / 'game.rpg').read_bytes() == b'checkpoint-rpg'
        assert (checkpoint_dir / 'companion' / 'dialog_history.json').exists()
        assert (checkpoint_dir / 'companion' / 'exploration.json').exists()
        assert (checkpoint_dir / 'companion' / 'logs' / 'navigation.log').exists()
        assert not (checkpoint_dir / 'state.json').exists()
        assert not (checkpoint_dir / 'screen.bmp').exists()



def test_load_checkpoint_restores_companion_files_and_refreshes_caches():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        write_state(debug)
        debug.exploration_path.write_text('{"version":6,"rooms":{},"transitions":{}}', encoding='utf-8')
        with patch.object(debug, 'run_slot_command', side_effect=fake_save_command(debug)):
            debug.save_checkpoint('cp1', 'CP1')
        debug.dialogs_path.write_text('[{"source_key":"old","text":"old"}]', encoding='utf-8')
        debug.events_path.write_text('[{"type":"old"}]', encoding='utf-8')
        debug.agent_log_path.write_text('{"turn":99}\n', encoding='utf-8')
        debug.dialog_history = [{'source_key': 'old', 'text': 'old'}]
        debug.event_log = [{'type': 'old'}]
        checkpoint_dir = debug.checkpoint_dir('cp1')
        (checkpoint_dir / 'companion' / 'dialog_history.json').write_text('[{"source_key":"new","text":"new"}]', encoding='utf-8')
        (checkpoint_dir / 'companion' / 'events.json').write_text('[{"type":"new"}]', encoding='utf-8')
        with patch.object(debug, 'run_slot_command', side_effect=fake_load_command):
            result = debug.load_checkpoint('cp1')
        assert result['ok']
        assert debug.save_slot_path().read_bytes() == b'checkpoint-rpg'
        assert debug.dialog_history == [{'source_key': 'dialog::::::new', 'text': 'new', 'source_keys': ['dialog::::::new']}]
        assert debug.event_log == [{'type': 'new'}]
        assert not debug.agent_log_path.exists()



def test_checkpoint_load_keeps_history_and_continues_logical_frame_after_raw_rollback():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(frame=100)
        write_state(debug, state)
        debug.snapshot()
        checkpoint_dir = debug.checkpoint_dir('cp1')
        (checkpoint_dir / 'companion').mkdir(parents=True)
        (checkpoint_dir / 'game.rpg').write_bytes(b'checkpoint-rpg')
        (checkpoint_dir / 'manifest.json').write_text(json.dumps({'version': 1, 'label': 'CP1', 'created_at': '2026-01-01T00:00:00Z', 'companion_version': 6}), encoding='utf-8')
        (checkpoint_dir / 'companion' / 'dialog_history.json').write_text('[{"source_key":"d1","text":"checkpoint dialog","room_key":"test-room","event_object_id":25}]', encoding='utf-8')
        (checkpoint_dir / 'companion' / 'events.json').write_text('[{"type":"checkpoint_event"}]', encoding='utf-8')
        (checkpoint_dir / 'companion' / 'exploration.json').write_text('{"version":6,"rooms":{},"transitions":{}}', encoding='utf-8')
        (checkpoint_dir / 'companion' / 'agent_log.jsonl').write_text('{"turn":7}\n', encoding='utf-8')
        with patch.object(debug, 'run_slot_command', side_effect=fake_load_command), patch.object(debug, 'clear_runtime_history', wraps=debug.clear_runtime_history) as clear_history:
            result = debug.load_checkpoint('cp1')
            rolled_back = make_state(frame=1)
            write_state(debug, rolled_back)
            snapshot = debug.snapshot()
        assert result['ok']
        assert snapshot['raw_frame'] == 1
        assert snapshot['logical_frame'] == 101
        assert snapshot['frame'] == 101
        assert debug.dialog_history[0]['text'] == 'checkpoint dialog'
        assert debug.event_log == [{'type': 'checkpoint_event'}]
        assert debug.read_agent_log(limit=10)[0]['turn'] == 7
        clear_history.assert_not_called()



def test_checkpoint_load_from_startup_uses_companion_history_for_logical_frame_baseline():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        checkpoint_dir = debug.checkpoint_dir('cp1')
        (checkpoint_dir / 'companion').mkdir(parents=True)
        (checkpoint_dir / 'game.rpg').write_bytes(b'checkpoint-rpg')
        (checkpoint_dir / 'manifest.json').write_text(json.dumps({'version': 1, 'label': 'CP1', 'created_at': '2026-01-01T00:00:00Z', 'companion_version': 6}), encoding='utf-8')
        (checkpoint_dir / 'companion' / 'dialog_history.json').write_text(json.dumps([{'source_key': 'd1', 'text': '呵～累了，想早點休息', 'room_key': 'test-room', 'event_object_id': 25, 'logical_frame': 4659, 'end_logical_frame': 4659}], ensure_ascii=False), encoding='utf-8')
        (checkpoint_dir / 'companion' / 'events.json').write_text('[]', encoding='utf-8')
        (checkpoint_dir / 'companion' / 'exploration.json').write_text('{"version":6,"rooms":{},"transitions":{}}', encoding='utf-8')
        (checkpoint_dir / 'companion' / 'agent_log.jsonl').write_text(json.dumps({'turn': 6, 'frame': 4729, 'logical_frame': 4729, 'episode': '1/12/test-room/test-room', 'nav_room_id': '1/12/test-room'}) + '\n', encoding='utf-8')
        write_state(debug, make_startup_state())
        with patch.object(debug, 'run_slot_command', side_effect=fake_load_command), patch.object(debug, 'clear_runtime_history', wraps=debug.clear_runtime_history) as clear_history:
            result = debug.load_checkpoint('cp1')
            resumed = make_state(frame=1)
            write_state(debug, resumed)
            snapshot = debug.snapshot()
        assert result['ok']
        assert snapshot['raw_frame'] == 1
        assert snapshot['logical_frame'] == 4730
        assert snapshot['frame'] == 4730
        assert debug.dialog_history[0]['text'] == '呵～累了，想早點休息'
        clear_history.assert_not_called()



def test_load_checkpoint_discards_incompatible_companion_files():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        write_state(debug)
        checkpoint_dir = debug.checkpoint_dir('old-cp')
        (checkpoint_dir / 'companion').mkdir(parents=True)
        (checkpoint_dir / 'game.rpg').write_bytes(b'checkpoint-rpg')
        (checkpoint_dir / 'manifest.json').write_text(json.dumps({'version': 1, 'label': 'Old', 'created_at': '2026-01-01T00:00:00Z'}), encoding='utf-8')
        (checkpoint_dir / 'companion' / 'exploration.json').write_text('{"version":2,"rooms":{"old":{}},"transitions":{}}', encoding='utf-8')
        (checkpoint_dir / 'companion' / 'dialog_history.json').write_text('[{"source_key":"new","text":"new"}]', encoding='utf-8')
        debug.dialogs_path.write_text('[{"source_key":"live","text":"live"}]', encoding='utf-8')
        debug.events_path.write_text('[{"type":"live"}]', encoding='utf-8')
        debug.agent_log_path.write_text('{"turn":99}\n', encoding='utf-8')
        debug.exploration_path.write_text('{"version":6,"rooms":{"live":{}},"transitions":{}}', encoding='utf-8')
        with patch.object(debug, 'run_slot_command', side_effect=fake_load_command):
            result = debug.load_checkpoint('old-cp')
        assert result['ok']
        assert result['restored']['exploration.json']['discarded']
        assert debug.dialog_history == []
        assert debug.event_log == []
        assert not debug.dialogs_path.exists()
        assert not debug.events_path.exists()
        assert not debug.agent_log_path.exists()
        assert not debug.exploration_path.exists()



def test_delete_checkpoint_only_removes_target_directory():
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        first = debug.checkpoint_dir('first')
        second = debug.checkpoint_dir('second')
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        (first / 'manifest.json').write_text('{"version":1}', encoding='utf-8')
        (second / 'manifest.json').write_text('{"version":1}', encoding='utf-8')
        result = debug.delete_checkpoint('first')
        assert result['ok']
        assert not first.exists()
        assert second.exists()
