import json
import os
import tempfile
from unittest.mock import patch

import pytest

from conftest import make_debug_state, make_state, make_touch_object, write_state



def test_deepseek_tool_decision_records_usage_trace_and_no_action():

    class FakeResponse:

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(self.payload).encode('utf-8')
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[make_touch_object(52, sprite_id=0)])
        write_state(debug, state)
        captured = []
        timeouts = []
        responses = [{'choices': [{'message': {'tool_calls': [{'id': 'call_state', 'type': 'function', 'function': {'name': 'get_state_summary', 'arguments': '{}'}}]}}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}, {'choices': [{'message': {'tool_calls': [{'id': 'call_nav', 'type': 'function', 'function': {'name': 'get_navigation', 'arguments': '{"mode":"full","limit":5}'}}]}}], 'usage': {'prompt_tokens': 6, 'completion_tokens': 2, 'total_tokens': 8}}, {'choices': [{'message': {'tool_calls': [{'id': 'call_submit', 'type': 'function', 'function': {'name': 'submit_decision', 'arguments': json.dumps({'summary': 'Leave the room to greet guests.', 'recommended_intent': 'enter_transition', 'recommended_action': {'tool': 'navigation_step', 'args': {'id': 52}}, 'evidence': {'target': {'id': 52}}, 'risk': 'May leave the room.', 'alternatives': []})}}]}}], 'usage': {'prompt_tokens': 4, 'completion_tokens': 3, 'total_tokens': 7, 'prompt_cache_hit_tokens': 64, 'prompt_cache_miss_tokens': 56}}]

        def fake_urlopen(request, timeout):
            captured.append(json.loads(request.data.decode('utf-8')))
            timeouts.append(timeout)
            return FakeResponse(responses.pop(0))
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', fake_urlopen):
            decision = debug.deepseek_decide_agent(state)
        assert not responses
        assert decision['usage']['prompt_cache_hit_tokens'] == 64
        assert decision['usage']['prompt_tokens'] == 20
        assert decision['recommended_action']['args'] == {'id': 52}
        assert decision['state_context']['room_id'] == debug.room_identity(state)['id']
        assert decision['state_context']['room_key'] == 'test-room'
        assert decision['state_context']['region_key'] == 'test-room'
        assert decision['state_context']['nav_room_id'] == '3/10/test-room'
        assert decision['state_context']['frame'] == 1
        assert [message['role'] for message in captured[0]['messages']] == ['system', 'system', 'user']
        assert 'response_format' not in captured[0]
        assert 'tools' in captured[0]
        assert [trace['name'] for trace in decision['tool_trace']] == ['get_state_summary', 'get_navigation', 'submit_decision']
        assert not debug.action_path.exists()
        log = debug.read_agent_log(limit=1)
        assert log[0]['backend'] == 'deepseek'
        assert log[0]['state_context']['room_key'] == 'test-room'
        assert log[0]['decision']['state_context']['frame'] == 1
        assert log[0]['tool_trace'][1]['name'] == 'get_navigation'
        assert log[0]['decision']['usage']['prompt_cache_miss_tokens'] == 56
        assert log[0]['llm_call_id'] == decision['llm_call_id']
        llm_log = debug.read_agent_llm_log(limit=10)
        assert len(llm_log) == 3
        assert all((entry['ok'] for entry in llm_log))
        assert {entry['llm_call_id'] for entry in llm_log} == {decision['llm_call_id']}
        assert llm_log[0]['request']['model'] == 'deepseek-v4-pro'
        assert [message['role'] for message in llm_log[0]['request']['messages']] == ['system', 'system', 'user']
        assert 'Authorization' not in llm_log[0]
        assert llm_log[-1]['response']['usage']['total_tokens'] == 7
        assert timeouts[0] >= 89.0



def test_deepseek_unknown_tool_returns_clear_error():

    class FakeResponse:

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({'choices': [{'message': {'tool_calls': [{'id': 'call_bad', 'type': 'function', 'function': {'name': 'write_action', 'arguments': '{}'}}]}}]}).encode('utf-8')
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', lambda request, timeout: FakeResponse()):
            with pytest.raises(RuntimeError, match='unsupported agent tool'):
                debug.deepseek_decide_agent(state)
        assert not debug.action_path.exists()



def test_deepseek_missing_submit_decision_returns_clear_error():

    class FakeResponse:

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({'choices': [{'message': {'content': '{"summary":"old path"}'}}]}).encode('utf-8')
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', lambda request, timeout: FakeResponse()):
            with pytest.raises(RuntimeError, match='did not call submit_decision'):
                debug.deepseek_decide_agent(state)



def test_deepseek_exceeding_tool_rounds_returns_clear_error():

    class FakeResponse:

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({'choices': [{'message': {'tool_calls': [{'id': 'call_state', 'type': 'function', 'function': {'name': 'get_state_summary', 'arguments': '{}'}}]}}]}).encode('utf-8')
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', lambda request, timeout: FakeResponse()):
            with pytest.raises(RuntimeError, match='within 8 tool rounds'):
                debug.deepseek_decide_agent(state)
        assert not debug.action_path.exists()



def test_deepseek_submit_decision_allowed_after_query_tool_budget():

    class FakeResponse:

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({'choices': [{'message': {'tool_calls': [{'id': f'call_state_{index}', 'type': 'function', 'function': {'name': 'get_state_summary', 'arguments': '{}'}} for index in range(40)] + [{'id': 'call_submit', 'type': 'function', 'function': {'name': 'submit_decision', 'arguments': json.dumps({'summary': 'Wait after inspection.', 'recommended_intent': 'wait_runtime', 'recommended_action': {'tool': 'no_op', 'args': {}}, 'evidence': {}, 'risk': 'None.', 'alternatives': []})}}]}}]}).encode('utf-8')
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[make_touch_object(10)])
        write_state(debug, state)
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', lambda request, timeout: FakeResponse()):
            decision = debug.deepseek_decide_agent(state)
        assert decision['recommended_intent'] == 'wait_runtime'
        assert len(decision['tool_trace']) == 41
        assert decision['tool_trace'][-1]['name'] == 'submit_decision'
        assert not debug.action_path.exists()



def test_deepseek_exceeding_tool_calls_writes_failure_log():

    class FakeResponse:

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({'choices': [{'message': {'tool_calls': [{'id': f'call_state_{index}', 'type': 'function', 'function': {'name': 'get_state_summary', 'arguments': '{}'}} for index in range(41)]}}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}).encode('utf-8')
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', lambda request, timeout: FakeResponse()):
            with pytest.raises(RuntimeError, match='exceeded 40 query tool calls'):
                debug.deepseek_decide_agent(state)
        log = debug.read_agent_log(limit=1)
        assert log[0]['ok'] == False
        assert log[0]['tool_calls'] == 41
        assert len(log[0]['tool_trace']) == 41
        assert 'exceeded 40 query tool calls' in log[0]['tool_trace'][-1]['error']
        assert log[0]['usage']['total_tokens'] == 12
        assert not debug.action_path.exists()



def test_deepseek_submit_decision_downgrades_stale_navigation_target():

    class FakeResponse:

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(self.payload).encode('utf-8')
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        original_state = make_state(objects=[make_touch_object(52)])
        latest_state = make_state(objects=[])
        write_state(debug, original_state)
        response = {'choices': [{'message': {'tool_calls': [{'id': 'call_submit', 'type': 'function', 'function': {'name': 'submit_decision', 'arguments': json.dumps({'summary': 'Inspect object #52.', 'recommended_intent': 'inspect_target', 'recommended_action': {'tool': 'navigation_step', 'args': {'id': 52}}, 'evidence': {'target': {'id': 52}}, 'risk': 'Low.', 'alternatives': []})}}]}}]}

        def fake_urlopen(request, timeout):
            write_state(debug, latest_state)
            return FakeResponse(response)
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', fake_urlopen):
            decision = debug.deepseek_decide_agent(original_state)
        assert decision['recommended_intent'] == 'wait_runtime'
        assert decision['recommended_action']['tool'] == 'no_op'
        assert decision['recommended_action']['args']['target_id'] == 52
        assert decision['evidence']['validation_target_id'] == 52
        assert decision['evidence']['validation_route_status'] == 'unknown_target'
        assert decision['evidence']['validation_state_context']['room_key'] == 'test-room'
        assert decision['evidence']['original_recommendation']['recommended_action']['args'] == {'id': 52}
        assert not debug.action_path.exists()



def test_deepseek_submit_decision_downgrades_illegal_action():

    class FakeResponse:

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({'choices': [{'message': {'tool_calls': [{'id': 'call_submit', 'type': 'function', 'function': {'name': 'submit_decision', 'arguments': json.dumps({'summary': 'Inspect target.', 'recommended_intent': 'inspect_target', 'recommended_action': {'tool': 'write_action', 'args': {'id': 10}}, 'evidence': {'target': {'id': 10}}, 'risk': 'Low.', 'alternatives': []})}}]}}]}).encode('utf-8')
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[make_touch_object(10)])
        write_state(debug, state)
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', lambda request, timeout: FakeResponse()):
            decision = debug.deepseek_decide_agent(state)
        assert decision['recommended_intent'] == 'inspect_target'
        assert decision['recommended_action']['tool'] == 'navigation_step'
        assert decision['recommended_action']['args'] == {'id': 10}
        assert not debug.action_path.exists()



def test_deepseek_targeted_explore_room_is_rejected():

    class FakeResponse:

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({'choices': [{'message': {'tool_calls': [{'id': 'call_submit', 'type': 'function', 'function': {'name': 'submit_decision', 'arguments': json.dumps({'summary': 'Explore one step toward object #74.', 'recommended_intent': 'explore_room', 'recommended_action': {'tool': 'exploration_step', 'args': {'max_steps': 1}}, 'evidence': {'target': {'id': 74}}, 'risk': 'Low.', 'alternatives': []})}}]}}]}).encode('utf-8')
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state(objects=[make_touch_object(74)])
        write_state(debug, state)
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', lambda request, timeout: FakeResponse()):
            decision = debug.deepseek_decide_agent(state)
        assert decision['recommended_intent'] == 'wait_runtime'
        assert decision['recommended_action']['tool'] == 'no_op'
        assert decision['recommended_action']['args']['reason'] == 'unsupported_tool'
        assert decision['recommended_action']['args']['tool'] == 'exploration_step'
        assert not debug.action_path.exists()
        log = debug.read_agent_log(limit=1)
        assert log[0]['decision']['recommended_action'] == {'tool': 'no_op', 'args': {'reason': 'unsupported_tool', 'tool': 'exploration_step'}, 'dry_run': True}



def test_deepseek_untargeted_explore_room_is_rejected():

    class FakeResponse:

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({'choices': [{'message': {'tool_calls': [{'id': 'call_submit', 'type': 'function', 'function': {'name': 'submit_decision', 'arguments': json.dumps({'summary': 'Explore current room coverage.', 'recommended_intent': 'explore_room', 'recommended_action': {'tool': 'exploration_step', 'args': {'max_steps': 1}}, 'evidence': {}, 'risk': 'Low.', 'alternatives': []})}}]}}]}).encode('utf-8')
    with tempfile.TemporaryDirectory() as tmp:
        debug = make_debug_state(tmp)
        state = make_state()
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', lambda request, timeout: FakeResponse()):
            decision = debug.deepseek_decide_agent(state)
        assert decision['recommended_intent'] == 'wait_runtime'
        assert decision['recommended_action']['tool'] == 'no_op'
        assert decision['recommended_action']['args']['reason'] == 'unsupported_tool'
        assert decision['recommended_action']['args']['tool'] == 'exploration_step'
        assert not debug.action_path.exists()
        log = debug.read_agent_log(limit=1)
        assert log[0]['decision']['recommended_action'] == {'tool': 'no_op', 'args': {'reason': 'unsupported_tool', 'tool': 'exploration_step'}, 'dry_run': True}
