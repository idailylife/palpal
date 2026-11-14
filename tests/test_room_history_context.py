import copy

from tests.support import make_debug_state, make_state, write_state


def test_history_is_directed_bounded_and_survives_companion_reload(tmp_path):
    debug = make_debug_state(tmp_path)
    destination = make_state()
    for i in range(10):
        source = make_state(objects=[{'id': i + 1, 'x': 32, 'y': 0, 'state': 1,
                                     'trigger_mode': 5, 'trigger_script': i + 10}])
        source['map_id'] = 80 + i
        debug.record_transition_destination(
            debug.room_identity(source), i + 1, debug.room_identity(destination),
            from_object=source['objects'][0], from_state=source, to_state=destination,
            confirmation='native_trigger', native_destination={'x': 16, 'y': 8})
    write_state(debug, destination)
    debug.update_exploration(destination)
    before = debug.agent_tool_initial_context(destination)['room_history']
    assert before['incoming']['total'] == 10
    assert before['incoming']['truncated']
    assert len(before['incoming']['records']) == 8
    assert before['incoming']['records'][0]['object_id'] == 10
    assert before['outgoing']['total'] == 0  # Never synthesize reverse edges.
    assert before['exploration']['explored'] == 1
    assert 'is_new_room' not in before
    # Recreate server without relying on transient model history.
    restored = make_debug_state(tmp_path)
    write_state(restored, destination)
    assert restored.agent_tool_initial_context(destination)['room_history'] == before
    shifted = copy.deepcopy(destination)
    shifted['region_key'] = 'another-region'
    assert restored.agent_tool_initial_context(shifted)['room_history'] == before


def test_memo_review_is_nonblocking_and_ignores_repeated_or_item_dialog(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    write_state(debug, state)
    room_id = debug.room_identity(state)['id']
    def dialog(text, timestamp, **extra):
        return dict(room_id=room_id, event_object_id=4, time=timestamp,
                    text=text, dialog_position='upper', **extra)
    debug.dialog_history = [dialog('此处是渡口', 1), dialog('獲得草藥', 2)]
    review = debug.agent_tool_initial_context(state)['room_memo_review']
    assert review['reason'] == 'missing_memo'
    assert not review['blocking']
    assert [e['text'] for e in review['evidence']] == ['此处是渡口']
    debug.upsert_room_memo({'text': 'NPC 称此处是渡口。'}, state)
    memo_time = debug.room_memo(room_id)['updated_at']
    debug.dialog_history.append(dialog('此处是渡口', memo_time + 1))
    assert not debug.agent_tool_initial_context(state)['room_memo_review']['suggested']
    debug.dialog_history.append(dialog('渡船今日停航', memo_time + 2))
    review = debug.agent_tool_initial_context(state)['room_memo_review']
    assert review['reason'] == 'new_semantic_evidence'
    assert [e['text'] for e in review['evidence']] == ['渡船今日停航']
    debug.upsert_room_memo({'operation': 'clear'}, state)
    assert debug.agent_tool_initial_context(state)['room_memo_review']['reason'] == 'missing_memo'


def test_memory_is_withheld_on_owner_mismatch(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    monkeypatch.setattr(debug, 'companion_owner_matches', lambda: False)
    context = debug.agent_tool_initial_context(make_state())
    assert context['isolation_error'] == 'companion_owner_mismatch'
    assert 'room_history' not in context
    assert 'room_memo_review' not in context


def test_checkpoint_restore_rebuilds_context_without_transcript_memory(tmp_path):
    from unittest.mock import patch
    from tests.support import fake_save_command, fake_load_command, create_agent_task
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[{'id': 1, 'x': 32, 'y': 0, 'state': 1,
                                'trigger_mode': 5, 'trigger_script': 10}])
    destination = copy.deepcopy(state)
    destination['map_id'] = 81
    write_state(debug, state)
    debug.update_exploration(state)
    debug.record_transition_destination(
        debug.room_identity(state), 1, debug.room_identity(destination),
        from_object=state['objects'][0], from_state=state, to_state=destination,
        confirmation='native_trigger')
    create_agent_task(debug, {'description': 'New objective', 'rationale': 'Inspect history'}, state)
    edge = debug.agent_tool_initial_context(state)['room_history']['outgoing']['records'][0]
    assert edge['traversal']['total_count'] == 1
    assert edge['traversal']['current_context_count'] == 0
    debug.upsert_room_memo({'text': '地点身份尚待核实。'}, state)
    expected = debug.agent_tool_initial_context(state)
    with patch.object(debug, 'run_slot_command', side_effect=fake_save_command(debug)):
        debug.save_checkpoint('history-test')
    debug.write_exploration(debug.empty_exploration())
    with patch.object(debug, 'run_slot_command', side_effect=fake_load_command):
        debug.load_checkpoint('history-test')
    actual = debug.agent_tool_initial_context(state)
    assert actual['room_history'] == expected['room_history']
    assert actual['room_memo'] == expected['room_memo']


def test_historical_exit_survives_script_change_without_claiming_current_route(tmp_path):
    debug = make_debug_state(tmp_path)
    source = make_state(objects=[{'id': 1, 'x': 32, 'y': 0, 'state': 1,
                                 'trigger_mode': 5, 'trigger_script': 10}])
    destination = make_state()
    destination['map_id'] = 81
    debug.record_transition_destination(
        debug.room_identity(source), 1, debug.room_identity(destination),
        from_object=source['objects'][0], from_state=source, to_state=destination,
        confirmation='native_trigger')
    source['objects'][0]['trigger_script'] = 20
    write_state(debug, source)
    history = debug.agent_tool_initial_context(source)['room_history']
    assert history['outgoing']['total'] == 1
    edge = history['outgoing']['records'][0]
    assert edge['to_room_id'] == debug.room_identity(destination)['id']
    assert edge['matches_current_binding'] is False
    assert debug.known_transitions_summary(debug.room_identity(source)['id'], source)['transitions'] == []
