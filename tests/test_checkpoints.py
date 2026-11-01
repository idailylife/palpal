import json
from unittest.mock import patch

import pytest

from tests.support import fake_load_command, fake_save_command, make_checkpoint_state as make_state, make_startup_state, make_debug_state, write_state



def complete_legacy_checkpoint(debug, directory):
    """Build a complete v2 bundle to exercise supported pre-hash checkpoints."""
    path = directory / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest.update(version=2, companion_version=11)
    manifest.pop('integrity', None)
    path.write_text(json.dumps(manifest))
    exploration = directory / 'companion/exploration.json'
    exploration.parent.mkdir(parents=True, exist_ok=True)
    if not exploration.exists():
        exploration.write_text(json.dumps(debug.empty_exploration()))
    if not (directory / 'agent/session.json').exists():
        debug.agent_runtime.export_checkpoint(directory)


def test_startup_waiting_rejects_save_checkpoint(tmp_path):
    debug = make_debug_state(tmp_path)
    write_state(debug, make_startup_state())
    with pytest.raises(RuntimeError, match='startup_waiting'):
        debug.save_checkpoint('cp1', 'CP1')



def test_startup_waiting_allows_load_checkpoint(tmp_path):
    debug = make_debug_state(tmp_path)
    checkpoint_dir = debug.checkpoint_dir('cp1')
    checkpoint_dir.mkdir(parents=True)
    (checkpoint_dir / 'game.rpg').write_bytes(b'checkpoint-rpg')
    (checkpoint_dir / 'manifest.json').write_text(json.dumps({'version': 1, 'label': 'CP1', 'created_at': '2026-01-01T00:00:00Z'}), encoding='utf-8')
    write_state(debug, make_startup_state())
    complete_legacy_checkpoint(debug, checkpoint_dir)
    with patch.object(debug, 'run_slot_command', side_effect=fake_load_command):
        result = debug.load_checkpoint('cp1')
    assert result['ok']
    assert debug.save_slot_path().read_bytes() == b'checkpoint-rpg'



def test_start_new_game_writes_start_slot_zero(tmp_path):
    debug = make_debug_state(tmp_path)
    write_state(debug, make_startup_state())
    previous_session_id = debug.agent_runtime.snapshot()['session_id']

    def run(command, slot):
        assert command == 'start_slot'
        assert slot == 0
        return {'action': {'command': command, 'slot': slot}, 'result': {'ok': True, 'action': command, 'slot': slot, 'status': 'start_scheduled'}}
    with patch.object(debug, 'run_slot_command', side_effect=run):
        result = debug.start_new_game()
    assert result['ok']
    assert result['native_result']['action'] == 'start_slot'
    assert debug.agent_runtime.snapshot()['session_id'] != previous_session_id
    assert debug.agent_runtime.snapshot()['turn'] == 0
    assert debug.agent_runtime.snapshot()['stop_reason'] == 'start_new_game'



def test_start_new_game_allowed_while_game_is_running(tmp_path):
    debug = make_debug_state(tmp_path)
    write_state(debug, make_state(frame=100))
    debug.dialogs_path.write_text('[{"source_key":"old","text":"old"}]', encoding='utf-8')
    debug.events_path.write_text('[{"type":"old"}]', encoding='utf-8')
    debug.agent_log_path.write_text('{"turn":9}\n', encoding='utf-8')
    debug.exploration_path.write_text('{"version":9,"rooms":{},"transitions":{},"object_interactions":{"room":{"75":{"attempt_count":1}}}}', encoding='utf-8')
    debug.run_agent_tool('upsert_task', {
        'description': 'Old game task',
        'rationale': 'This must not survive a new game.',
    })
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
    assert debug.agent_tasks_view()['counts']['total'] == 0
    assert debug.last_raw_frame is None
    assert debug.logical_frame_offset == 0


def test_start_new_game_is_allowed_while_a_dialog_is_active(tmp_path):
    debug = make_debug_state(tmp_path)
    write_state(debug, {**make_state(frame=100), 'dialog': {'active': True}})

    with patch.object(debug, 'run_slot_command', return_value={'result': {'ok': True}}) as run:
        result = debug.start_new_game()

    assert result['ok']
    run.assert_called_once_with('start_slot', 0)


def test_room_memo_follows_checkpoint_branch_survives_session_reset_and_clears_on_new_game(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(frame=100)
    write_state(debug, state)
    state = debug.snapshot()
    room_id = 'S3/M10:0123456789AB'
    saved = debug.run_agent_tool(
        'upsert_room_memo',
        {'room_id': room_id, 'text': 'checkpoint memo'},
        state,
    )

    with patch.object(debug, 'run_slot_command', side_effect=fake_save_command(debug)):
        debug.save_checkpoint('memo-cp', 'Memo CP')

    changed = debug.run_agent_tool(
        'upsert_room_memo',
        {'room_id': room_id, 'text': 'later branch memo'},
        state,
    )
    debug.agent_runtime.reset('user_reset')
    assert debug.room_memo(room_id) == changed['memo']

    with patch.object(debug, 'run_slot_command', side_effect=fake_load_command):
        debug.load_checkpoint('memo-cp')
    assert debug.room_memo(room_id) == saved['memo']

    with patch.object(debug, 'run_slot_command', return_value={'result': {'ok': True}}):
        debug.start_new_game()
    assert not debug.exploration_path.exists()
    assert debug.room_memo(room_id) is None



def test_wait_for_slot_result_accepts_slot_zero(tmp_path):
    debug = make_debug_state(tmp_path)
    start_time = 0.0
    debug.action_result_path.write_text(json.dumps({'ok': True, 'action': 'start_slot', 'slot': 0, 'status': 'started'}), encoding='utf-8')
    result = debug.wait_for_slot_result('start_slot', 0, start_time, timeout=0.1)
    assert result['ok']
    assert result['slot'] == 0



def test_save_checkpoint_writes_manifest_game_and_existing_companion_files(tmp_path):
    debug = make_debug_state(tmp_path)
    write_state(debug)
    debug.dialogs_path.write_text('[{"source_key":"d1","text":"hi"}]', encoding='utf-8')
    debug.exploration_path.write_text('{"version":11,"rooms":{},"transitions":{}}', encoding='utf-8')
    debug.navigation_log_path.parent.mkdir(parents=True, exist_ok=True)
    debug.navigation_log_path.write_text('{"type":"route"}\n', encoding='utf-8')
    with patch.object(debug, 'run_slot_command', side_effect=fake_save_command(debug)):
        result = debug.save_checkpoint('before-door', 'Before Door')
    checkpoint = result['checkpoint']
    checkpoint_dir = debug.checkpoint_dir('before-door')
    assert result['ok']
    assert checkpoint['version'] == 3
    assert checkpoint['companion_version'] == 11
    assert checkpoint['label'] == 'Before Door'
    assert checkpoint['state']['scene_id'] == 3
    assert checkpoint['state']['room_id'].startswith('S3/M10:')
    assert checkpoint['state']['logical_room_id'] == checkpoint['state']['room_id']
    assert checkpoint['state']['legacy_room_id'] == '3/10/test-room/test-room'
    assert checkpoint['state']['nav_room_id'] == checkpoint['state']['logical_room_id']
    assert checkpoint['state']['legacy_nav_room_id'] == '3/10/test-room'
    assert (checkpoint_dir / 'game.rpg').read_bytes() == b'checkpoint-rpg'
    assert (checkpoint_dir / 'companion' / 'dialog_history.json').exists()
    assert (checkpoint_dir / 'companion' / 'exploration.json').exists()
    assert (checkpoint_dir / 'companion' / 'logs' / 'navigation.log').exists()
    assert (checkpoint_dir / 'agent' / 'session.json').exists()
    agent_bundle = json.loads((checkpoint_dir / 'agent' / 'session.json').read_text(encoding='utf-8'))
    assert 'goal' not in agent_bundle['control']
    assert checkpoint['agent_bundle']['version'] == 2
    assert not (checkpoint_dir / 'state.json').exists()
    assert not (checkpoint_dir / 'screen.bmp').exists()



def test_load_checkpoint_restores_companion_files_and_refreshes_caches(tmp_path):
    debug = make_debug_state(tmp_path)
    write_state(debug)
    debug.exploration_path.write_text('{"version":11,"rooms":{},"transitions":{}}', encoding='utf-8')
    with patch.object(debug, 'run_slot_command', side_effect=fake_save_command(debug)):
        debug.save_checkpoint('cp1', 'CP1')
    debug.dialogs_path.write_text('[{"source_key":"old","text":"old"}]', encoding='utf-8')
    debug.events_path.write_text('[{"type":"old"}]', encoding='utf-8')
    debug.agent_log_path.write_text('{"turn":99}\n', encoding='utf-8')
    debug.dialog_history = [{'source_key': 'old', 'text': 'old'}]
    debug.event_log = [{'type': 'old'}]
    observation_id = debug.observations.register(
        'transition', owner='navigation', timeout=30.0, payload={'object_id': 60},
    )
    debug.navigation_transition_requests[60] = observation_id
    debug.pending_transition_observation = {
        'object_id': 60,
        'observation_id': observation_id,
    }
    checkpoint_dir = debug.checkpoint_dir('cp1')
    (checkpoint_dir / 'companion' / 'dialog_history.json').write_text('[{"source_key":"new","text":"new"}]', encoding='utf-8')
    (checkpoint_dir / 'companion' / 'events.json').write_text('[{"type":"new"}]', encoding='utf-8')
    complete_legacy_checkpoint(debug, checkpoint_dir)
    with patch.object(debug, 'run_slot_command', side_effect=fake_load_command):
        result = debug.load_checkpoint('cp1')
    assert result['ok']
    assert debug.save_slot_path().read_bytes() == b'checkpoint-rpg'
    assert debug.dialog_history == [{'source_key': 'dialog::::::new', 'text': 'new', 'source_keys': ['dialog::::::new']}]
    assert debug.event_log == [{'type': 'new'}]
    assert debug.navigation_transition_requests == {}
    assert debug.pending_transition_observation is None
    assert debug.observations.get(observation_id) is None
    assert not debug.agent_log_path.exists()



def test_checkpoint_load_keeps_history_and_continues_logical_frame_after_raw_rollback(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(frame=100)
    write_state(debug, state)
    debug.snapshot()
    checkpoint_dir = debug.checkpoint_dir('cp1')
    (checkpoint_dir / 'companion').mkdir(parents=True)
    (checkpoint_dir / 'game.rpg').write_bytes(b'checkpoint-rpg')
    (checkpoint_dir / 'manifest.json').write_text(json.dumps({'version': 1, 'label': 'CP1', 'created_at': '2026-01-01T00:00:00Z', 'companion_version': 11}), encoding='utf-8')
    (checkpoint_dir / 'companion' / 'dialog_history.json').write_text('[{"source_key":"d1","text":"checkpoint dialog","room_key":"test-room","event_object_id":25}]', encoding='utf-8')
    (checkpoint_dir / 'companion' / 'events.json').write_text('[{"type":"checkpoint_event"}]', encoding='utf-8')
    (checkpoint_dir / 'companion' / 'exploration.json').write_text('{"version":11,"rooms":{},"transitions":{}}', encoding='utf-8')
    (checkpoint_dir / 'companion' / 'agent_log.jsonl').write_text('{"turn":7}\n', encoding='utf-8')
    complete_legacy_checkpoint(debug, checkpoint_dir)
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


def test_v11_checkpoint_lazily_adds_interaction_context_without_rewriting_checkpoint(tmp_path):
    debug = make_debug_state(tmp_path)
    write_state(debug, make_state())
    checkpoint_dir = debug.checkpoint_dir('legacy-v11')
    (checkpoint_dir / 'companion').mkdir(parents=True)
    (checkpoint_dir / 'game.rpg').write_bytes(b'checkpoint-rpg')
    (checkpoint_dir / 'manifest.json').write_text(json.dumps({
        'version': 2,
        'label': 'Legacy v11',
        'created_at': '2026-01-01T00:00:00Z',
        'companion_version': 11,
    }), encoding='utf-8')
    room_id = 'S3/M10:000000000001'
    transition_key = '[3,10,32,0,4,4631]'
    legacy_exploration = json.dumps({
        'version': 11,
        'rooms': {},
        'transitions': {room_id: {transition_key: {
            'transition_key': transition_key,
            'transition_key_fields': {
                'scene_id': 3, 'map_id': 10, 'x': 32, 'y': 0,
                'trigger_mode': 4, 'trigger_script': 4631,
            },
            'object_id': 48,
            'object': {'id': 48, 'x': 32, 'y': 0, 'trigger_script': 4631},
            'from_room_id': room_id,
            'to_room_id': room_id,
            'confirmation': 'native_trigger',
            'observed': True,
            'traversal_count': 3,
            'intra_room_transition': True,
            'native_destination': {'x': 48, 'y': 8},
            'from_subregion_id': '.48',
            'to_subregion_id': '.47',
            'from_location_id': f'{room_id}.48',
            'to_location_id': f'{room_id}.47',
            'subregion_basis': 'legacy-nearest-object',
            'subregion_match_distance': 0,
        }}},
        'object_interactions': {},
        'tasks': {},
    }, separators=(',', ':')).encode()
    checkpoint_exploration = checkpoint_dir / 'companion' / 'exploration.json'
    checkpoint_exploration.write_bytes(legacy_exploration)
    (checkpoint_dir / 'companion' / 'dialog_history.json').write_text('[]', encoding='utf-8')
    (checkpoint_dir / 'companion' / 'events.json').write_text('[]', encoding='utf-8')

    complete_legacy_checkpoint(debug, checkpoint_dir)
    with patch.object(debug, 'run_slot_command', side_effect=fake_load_command):
        result = debug.load_checkpoint('legacy-v11')
    assert result['ok']
    context = debug.interaction_context_snapshot()
    assert context['observable_revision'] == 0
    restored = debug.read_exploration()
    assert isinstance(restored['interaction_observable_context'], dict)
    record = restored['transitions'][room_id][transition_key]
    assert record['transition_key'] == transition_key
    assert record['native_destination'] == {'x': 48, 'y': 8}
    assert record['traversal_count'] == 3
    assert record['destination_endpoint']['kind'] == 'native_landing'
    assert 'from_location_id' not in record
    assert 'to_subregion_id' not in record
    assert checkpoint_exploration.read_bytes() == legacy_exploration



def test_checkpoint_load_from_startup_uses_companion_history_for_logical_frame_baseline(tmp_path):
    debug = make_debug_state(tmp_path)
    checkpoint_dir = debug.checkpoint_dir('cp1')
    (checkpoint_dir / 'companion').mkdir(parents=True)
    (checkpoint_dir / 'game.rpg').write_bytes(b'checkpoint-rpg')
    (checkpoint_dir / 'manifest.json').write_text(json.dumps({'version': 1, 'label': 'CP1', 'created_at': '2026-01-01T00:00:00Z', 'companion_version': 11}), encoding='utf-8')
    (checkpoint_dir / 'companion' / 'dialog_history.json').write_text(json.dumps([{'source_key': 'd1', 'text': '呵～累了，想早點休息', 'room_key': 'test-room', 'event_object_id': 25, 'logical_frame': 4659, 'end_logical_frame': 4659}], ensure_ascii=False), encoding='utf-8')
    (checkpoint_dir / 'companion' / 'events.json').write_text('[]', encoding='utf-8')
    (checkpoint_dir / 'companion' / 'exploration.json').write_text('{"version":11,"rooms":{},"transitions":{}}', encoding='utf-8')
    (checkpoint_dir / 'companion' / 'agent_log.jsonl').write_text(json.dumps({'turn': 6, 'frame': 4729, 'logical_frame': 4729, 'episode': '1/12/test-room/test-room', 'nav_room_id': '1/12/test-room'}) + '\n', encoding='utf-8')
    write_state(debug, make_startup_state())
    complete_legacy_checkpoint(debug, checkpoint_dir)
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



def test_load_checkpoint_rejects_incompatible_companion_before_native_load(tmp_path):
    debug = make_debug_state(tmp_path)
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
    debug.exploration_path.write_text(
        '{"version":11,"rooms":{"live":{}},"room_memos":{"S1/M1:0123456789AB":{"room_id":"S1/M1:0123456789AB","text":"live memo","source":"agent_annotation"}},"transitions":{}}',
        encoding='utf-8',
    )
    complete_legacy_checkpoint(debug, checkpoint_dir)
    before = {path: path.read_bytes() for path in (debug.dialogs_path, debug.events_path, debug.exploration_path)}
    with patch.object(debug, 'run_slot_command') as native:
        with pytest.raises(ValueError, match='exploration_incompatible'):
            debug.load_checkpoint('old-cp')
    native.assert_not_called()
    assert all(path.read_bytes() == content for path, content in before.items())


def test_checkpoint_restores_agent_with_rollback_semantics(tmp_path):
    debug = make_debug_state(tmp_path)
    state = {**make_state(), 'game_instance_id': 'game-a'}
    state['inventory'] = [{
        'item_id': 201,
        'name': '皮帽',
        'amount': 1,
        'amount_in_use': 0,
        'equipable': True,
    }]
    write_state(debug, state)
    debug.snapshot()
    runtime = debug.agent_runtime
    source_session_id = runtime.snapshot()['session_id']
    saved_decision = {
        'summary': 'Saved decision',
        'recommended_intent': 'wait_runtime',
        'recommended_action': {'tool': 'no_op', 'args': {}, 'dry_run': True},
    }
    runtime.state.update(
        turn=4,
        pending_decision=saved_decision,
        usage={'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110},
        last_prompt_tokens=95,
        provider_usage={
            'version': 1,
            'tracking_started_at': '2026-08-15T10:00:00Z',
            'incomplete_before_tracking': True,
            'scopes': {
                'main_decision': {
                    'requests': 2, 'input_tokens': 100, 'cached_tokens': 40,
                    'output_tokens': 10, 'reasoning_tokens': 3, 'total_tokens': 110,
                    'details_complete': True,
                },
                'battle': {
                    'requests': 0, 'input_tokens': 0, 'cached_tokens': 0,
                    'output_tokens': 0, 'reasoning_tokens': 0, 'total_tokens': 0,
                    'details_complete': True,
                },
                'compaction': {
                    'requests': 0, 'input_tokens': 0, 'cached_tokens': 0,
                    'output_tokens': 0, 'reasoning_tokens': 0, 'total_tokens': 0,
                    'details_complete': True,
                },
            },
            'last_request': {
                'scope': 'main_decision', 'model': 'deepseek-chat',
                'time': '2026-08-15T10:00:00Z', 'input_tokens': 60,
                'cached_tokens': 20, 'output_tokens': 6, 'reasoning_tokens': 2,
                'total_tokens': 66, 'details_complete': True,
            },
        },
        last_compaction={
            'time': '2026-08-09T00:00:00Z',
            'trigger_prompt_tokens': 200001,
            'compact_at_tokens': 200000,
            'retain_tokens': 24000,
            'before_estimated_tokens': 190000,
            'before_composition': {'user': 1000},
            'retained_cycles': 1,
            'retained_items': 4,
            'retained_estimated_tokens': 25000,
            'retained_budget_exceeded': True,
            'summary_estimated_tokens': 900,
            'replacement_estimated_tokens': 31000,
            'replacement_composition': {'user': 1000},
        },
        compactions=1,
        equipment_review={
            'ownership_counts': {'201': 1},
            'pending': [{
                'item_id': 201,
                'name': '皮帽',
                'new_count': 1,
                'first_seen_at': '2026-07-25T00:00:00Z',
            }],
        },
        room_inspection={
            'baseline_required': False,
            'last_observed_room_id': 'S3/M10:2F2C967F8E00',
            'pending_room_id': None,
            'inspected_rooms': {
                'S3/M10:2F2C967F8E00': {
                    'inspected_at': '2026-08-25T10:00:00Z',
                    'frame_sequence': 123,
                    'visual_hash': 'room-hash',
                },
            },
        },
        room_entry_context={
            'status': 'entered',
            'source_room_id': 'S3/M9:111111111111',
            'source_target_id': 52,
            'source_transition_key': '[3,9,32,0,4,4631]',
            'destination_room_id': 'S3/M10:2F2C967F8E00',
            'landing_position': {'x': 16, 'y': 8},
            'entered_at': '2026-08-25T10:00:00Z',
            'entered_frame_sequence': 123,
        },
    )
    runtime._persist()
    runtime._submit(runtime._replace_sdk_session_items(source_session_id, [
        {'role': 'user', 'content': 'checkpoint context'},
        {'role': 'assistant', 'content': 'checkpoint response'},
    ])).result(timeout=5)
    archived_output = json.dumps({'ok': True, 'events': [{'text': 'checkpoint evidence'}]})
    output_ref = runtime._archive_tool_output(archived_output)
    runtime.emit('decision', 'ready', 'Saved activity.')

    with patch.object(debug, 'run_slot_command', side_effect=fake_save_command(debug)):
        saved = debug.save_checkpoint('agent-branch', 'Agent Branch')
    assert saved['checkpoint']['agent_bundle']['tool_outputs'] == 1

    runtime.reset('later_manual_reset')
    later_session_id = runtime.snapshot()['session_id']
    runtime.state['turn'] = 9
    runtime._persist()
    runtime.emit('decision', 'ready', 'Post-checkpoint activity.')

    with patch.object(debug, 'run_slot_command', side_effect=fake_load_command):
        debug.load_checkpoint('agent-branch')

    restored = runtime.snapshot()
    assert restored['session_id'] == source_session_id
    assert restored['parent_session_id'] is None
    assert restored['restored_from_session_id'] is None
    assert restored['checkpoint_id'] == 'agent-branch'
    assert restored['game_instance_id'] == 'game-a'
    assert restored['turn'] == 4
    assert restored['run_turn'] == 0
    assert restored['pending_decision'] == saved_decision
    assert restored['usage']['total_tokens'] == 110
    assert restored['last_prompt_tokens'] == 95
    assert restored['provider_usage']['scopes']['main_decision']['cached_tokens'] == 40
    assert restored['provider_usage']['last_request']['scope'] == 'main_decision'
    assert restored['provider_usage']['incomplete_before_tracking'] is True
    assert restored['last_compaction']['trigger_prompt_tokens'] == 200001
    assert restored['last_compaction']['retained_cycles'] == 1
    assert restored['last_compaction']['retained_budget_exceeded'] is True
    assert restored['compactions'] == 1
    assert restored['equipment_review']['pending'][0]['item_id'] == 201
    assert restored['room_inspection']['inspected_rooms']['S3/M10:2F2C967F8E00']['frame_sequence'] == 123
    assert restored['room_entry_context']['source_target_id'] == 52
    assert restored['room_entry_context']['destination_room_id'] == 'S3/M10:2F2C967F8E00'
    assert restored['task_reconciliation_pending'] is True
    assert restored['task_reconciliation_reason'] == 'checkpoint_restored'
    transcript = runtime._submit(runtime._sdk_session_items(restored['session_id'])).result(timeout=5)
    assert [item['content'] for item in transcript] == ['checkpoint context', 'checkpoint response']
    assert runtime.get_tool_output(output_ref)['content'] == archived_output
    summaries = [event['summary'] for event in runtime.events(limit=500)]
    assert 'Saved activity.' in summaries
    assert 'Post-checkpoint activity.' not in summaries
    assert summaries[-1] == 'Restored checkpoint agent-branch.'

    with patch.object(debug, 'run_slot_command', side_effect=fake_load_command):
        debug.load_checkpoint('agent-branch')
    second_restore = runtime.snapshot()
    assert second_restore['session_id'] == source_session_id
    assert second_restore['restored_from_session_id'] is None
    assert second_restore['turn'] == 4
    assert runtime.get_tool_output(output_ref)['content'] == archived_output


@pytest.mark.parametrize("bundle_kind", ["version-one", "corrupt"])
def test_incompatible_agent_bundle_refuses_load_without_reset(tmp_path, bundle_kind):
    debug = make_debug_state(tmp_path)
    state = {**make_state(), 'game_instance_id': 'game-a'}
    write_state(debug, state)
    debug.snapshot()
    runtime = debug.agent_runtime
    previous_session_id = runtime.snapshot()['session_id']
    checkpoint_id = 'agent-v1' if bundle_kind == 'version-one' else 'corrupt-agent'
    checkpoint_dir = debug.checkpoint_dir(checkpoint_id)
    checkpoint_dir.mkdir(parents=True)
    (checkpoint_dir / 'game.rpg').write_bytes(b'checkpoint-rpg')
    (checkpoint_dir / 'manifest.json').write_text(json.dumps({
        'version': 2,
        'label': bundle_kind,
        'created_at': '2026-01-01T00:00:00Z',
    }), encoding='utf-8')
    (checkpoint_dir / 'agent').mkdir()
    agent_bundle = (
        json.dumps({
            'version': 1,
            'source_session_id': 'a' * 32,
            'control': {'session_id': 'a' * 32, 'turn': 3},
            'transcript': [{'role': 'user', 'content': 'legacy transcript'}],
            'events': [],
        })
        if bundle_kind == 'version-one'
        else '{broken'
    )
    (checkpoint_dir / 'agent' / 'session.json').write_text(agent_bundle, encoding='utf-8')

    complete_legacy_checkpoint(debug, checkpoint_dir)
    with patch.object(debug, 'run_slot_command') as native:
        with pytest.raises(ValueError):
            debug.load_checkpoint(checkpoint_id)
    native.assert_not_called()
    assert runtime.snapshot()['session_id'] == previous_session_id
    assert debug.checkpoint_status() is None


def test_incomplete_legacy_checkpoint_refuses_load_without_reset(tmp_path):
    debug = make_debug_state(tmp_path)
    state = {**make_state(), 'game_instance_id': 'game-a'}
    write_state(debug, state)
    debug.snapshot()
    previous_session_id = debug.agent_runtime.snapshot()['session_id']
    debug.agent_runtime.state['turn'] = 3
    debug.agent_runtime._persist()
    checkpoint_dir = debug.checkpoint_dir('legacy')
    checkpoint_dir.mkdir(parents=True)
    (checkpoint_dir / 'game.rpg').write_bytes(b'checkpoint-rpg')
    (checkpoint_dir / 'manifest.json').write_text(json.dumps({
        'version': 1,
        'label': 'Legacy',
        'created_at': '2026-01-01T00:00:00Z',
    }), encoding='utf-8')

    with patch.object(debug, 'run_slot_command') as native:
        with pytest.raises(ValueError, match='manifest_incompatible'):
            debug.load_checkpoint('legacy')
    native.assert_not_called()
    restored = debug.agent_runtime.snapshot()
    assert restored['session_id'] == previous_session_id
    assert restored['turn'] == 3
    assert restored['checkpoint_id'] is None


def test_delete_checkpoint_only_removes_target_directory(tmp_path):
    debug = make_debug_state(tmp_path)
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


def test_prune_orphan_sessions_keeps_only_live_session(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    live_id = runtime.snapshot()["session_id"]
    other_id = "a" * 32
    runtime.emit("decision", "ready", "Live activity.")
    other_state = dict(runtime.snapshot(), session_id=other_id)
    runtime._event_for(other_state, "decision", "ready", "Other activity.")
    runtime._session_tool_outputs_dir(other_id).mkdir(parents=True, exist_ok=True)

    result = debug.prune_orphan_sessions()
    assert other_id in result["pruned_session_ids"]
    assert live_id not in result["pruned_session_ids"]
    assert all(event["session_id"] != other_id for event in runtime._read_events_file())
    assert not runtime._session_tool_outputs_dir(other_id).exists()


def test_reset_agent_session_clears_companion_and_prunes_old(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    write_state(debug, state)
    debug.snapshot()
    runtime = debug.agent_runtime
    old_id = runtime.snapshot()["session_id"]
    runtime.emit("decision", "ready", "Old activity.")
    debug.dialogs_path.write_text('[{"source_key":"old","text":"old"}]', encoding="utf-8")
    debug.events_path.write_text('[{"type":"old"}]', encoding="utf-8")
    debug.agent_log_path.write_text('{"turn":9}\n', encoding="utf-8")
    debug.exploration_path.write_text('{"version":11,"rooms":{},"transitions":{}}', encoding="utf-8")
    debug.snapshot()

    with patch.object(debug, "run_slot_command", side_effect=fake_save_command(debug)):
        result = debug.reset_agent_session()

    assert result["ok"] is True
    assert result["archived_checkpoint_id"] is not None
    new_id = runtime.snapshot()["session_id"]
    assert new_id != old_id
    assert not debug.dialogs_path.exists()
    assert not debug.events_path.exists()
    assert not debug.agent_log_path.exists()
    assert not debug.exploration_path.exists()
    assert debug.agent_tasks_view()["counts"]["total"] == 0
    assert all(event["session_id"] != old_id for event in runtime._read_events_file())
    owner = debug.read_companion_owner()
    assert owner["session_id"] == new_id
    assert owner["reason"] == "reset"


def test_agent_context_withholds_memory_on_owner_mismatch(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    write_state(debug, state)
    debug.snapshot()
    debug.atomic_write_text(
        debug.companion_owner_path,
        json.dumps({
            "session_id": "b" * 32,
            "checkpoint_id": None,
            "generation": 0,
            "reason": "stale",
            "updated_at": "2026-01-01T00:00:00Z",
        }) + "\n",
        encoding="utf-8",
    )
    context = debug.agent_tool_initial_context(state)
    assert context.get("isolation_error") == "companion_owner_mismatch"
    assert context.get("room_memo") is None
    assert context.get("active_tasks") == []


def test_load_later_checkpoint_restores_longer_event_prefix(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    state["game_instance_id"] = "game-a"
    write_state(debug, state)
    debug.snapshot()
    runtime = debug.agent_runtime
    source_id = runtime.snapshot()["session_id"]

    runtime.emit("decision", "ready", "First activity.")
    with patch.object(debug, "run_slot_command", side_effect=fake_save_command(debug)):
        debug.save_checkpoint("cp-a", "A")

    runtime.emit("decision", "ready", "Second activity.")
    with patch.object(debug, "run_slot_command", side_effect=fake_save_command(debug)):
        debug.save_checkpoint("cp-b", "B")

    with patch.object(debug, "run_slot_command", side_effect=fake_load_command):
        debug.load_checkpoint("cp-a")
    summaries = [event["summary"] for event in runtime.events(limit=500)]
    assert "First activity." in summaries
    assert "Second activity." not in summaries

    with patch.object(debug, "run_slot_command", side_effect=fake_load_command):
        debug.load_checkpoint("cp-b")
    summaries = [event["summary"] for event in runtime.events(limit=500)]
    assert "First activity." in summaries
    assert "Second activity." in summaries
    assert runtime.snapshot()["session_id"] == source_id
