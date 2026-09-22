import json
import os
from unittest.mock import patch

import pytest

from tests.support import (
    FakeJSONResponse,
    deepseek_tool_response,
    make_debug_state,
    make_state,
    make_touch_object,
    write_state,
)



def test_deepseek_tool_decision_records_usage_trace_and_no_action(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[make_touch_object(52, sprite_id=0)])
    write_state(debug, state)
    captured = []
    timeouts = []
    responses = [{'choices': [{'message': {'tool_calls': [{'id': 'call_obs', 'type': 'function', 'function': {'name': 'get_observations', 'arguments': '{"kind":"events","limit":1}'}}]}}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}, {'choices': [{'message': {'tool_calls': [{'id': 'call_nav', 'type': 'function', 'function': {'name': 'get_navigation', 'arguments': '{"limit":5}'}}]}}], 'usage': {'prompt_tokens': 6, 'completion_tokens': 2, 'total_tokens': 8}}, {'choices': [{'message': {'tool_calls': [{'id': 'call_submit', 'type': 'function', 'function': {'name': 'submit_decision', 'arguments': json.dumps({'action': 'navigate', 'target_id': 52, 'reason': 'Leave the room to greet guests.'})}}]}}], 'usage': {'prompt_tokens': 4, 'completion_tokens': 3, 'total_tokens': 7, 'prompt_cache_hit_tokens': 64, 'prompt_cache_miss_tokens': 56}}]

    def fake_urlopen(request, timeout):
        captured.append(json.loads(request.data.decode('utf-8')))
        timeouts.append(timeout)
        return FakeJSONResponse(responses.pop(0))
    with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', fake_urlopen):
        decision = debug.deepseek_decide_agent(state)
    assert not responses
    assert decision['usage']['prompt_cache_hit_tokens'] == 64
    assert decision['usage']['prompt_tokens'] == 20
    assert decision['recommended_action']['args'] == {'id': 52}
    assert decision['state_context']['room_id'] == debug.room_identity(state)['id']
    assert decision['state_context']['room_key'] == 'test-room'
    assert decision['state_context']['region_key'] == 'test-room'
    assert decision['state_context']['nav_room_id'] == decision['state_context']['logical_room_id']
    assert decision['state_context']['legacy_nav_room_id'] == '3/10/test-room'
    assert decision['state_context']['frame'] == 1
    assert [message['role'] for message in captured[0]['messages']] == ['system', 'system', 'user']
    assert 'response_format' not in captured[0]
    assert 'tools' in captured[0]
    assert [trace['name'] for trace in decision['tool_trace']] == ['get_observations', 'get_navigation', 'submit_decision']
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


def test_deepseek_tool_round_cannot_create_before_review_result_is_visible(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    write_state(debug, state)
    requests = []

    def calls(*entries):
        return {"choices": [{"message": {"tool_calls": list(entries)}}]}

    def call(call_id, name, arguments):
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }

    def fake_urlopen(request, timeout):
        payload = json.loads(request.data.decode("utf-8"))
        requests.append(payload)
        round_number = len(requests)
        if round_number == 1:
            return FakeJSONResponse(calls(
                call("audit", "get_tasks", {"include_closed": True, "detail": "summary"}),
                call("premature", "upsert_task", {
                    "id": "bag-clue", "description": "Investigate the cloth bag",
                    "rationale": "The clue will affect later action.",
                }),
            ))
        if round_number == 2:
            token = None
            for message in reversed(payload["messages"]):
                if message.get("role") != "tool":
                    continue
                result = json.loads(message["content"])
                if result.get("review_token"):
                    token = result["review_token"]
                    break
            assert token
            return FakeJSONResponse(calls(call("create", "upsert_task", {
                "id": "bag-clue", "description": "Investigate the cloth bag",
                "rationale": "The clue will affect later action.", "review_token": token,
            })))
        return FakeJSONResponse(calls(call(
            "submit", "submit_decision", {"action": "advance_dialog", "reason": "Continue."},
        )))

    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), patch(
        "urllib.request.urlopen", fake_urlopen
    ):
        decision = debug.deepseek_decide_agent(state)

    assert decision["recommended_intent"] == "advance_dialog"
    trace = decision["tool_trace"]
    premature_result = json.loads(requests[1]["messages"][-1]["content"])
    assert premature_result["error"]["code"] == "task_creation_review_required"
    assert [entry["name"] for entry in trace] == ["get_tasks", "upsert_task", "upsert_task", "submit_decision"]
    assert [task["id"] for task in debug.task_ledger()] == ["bag-clue"]



def test_deepseek_timeout_applies_to_each_model_round(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    timeouts = []
    responses = [
        {'choices': [{'message': {'tool_calls': [{'id': 'call_obs', 'type': 'function', 'function': {'name': 'get_observations', 'arguments': '{"kind":"events","limit":1}'}}]}}]},
        {'choices': [{'message': {'tool_calls': [{'id': 'call_nav', 'type': 'function', 'function': {'name': 'get_navigation', 'arguments': '{"limit":5}'}}]}}]},
        {'choices': [{'message': {'tool_calls': [{'id': 'call_submit', 'type': 'function', 'function': {'name': 'submit_decision', 'arguments': json.dumps({'action': 'advance_dialog', 'reason': 'Continue.'})}}]}}]},
    ]
    clock = [100.0]

    def fake_monotonic():
        clock[0] += 1.0
        return clock[0]

    def fake_urlopen(request, timeout):
        timeouts.append(timeout)
        return FakeJSONResponse(responses.pop(0))
    with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key', 'DEEPSEEK_TIMEOUT': '3'}), patch('palpal.debug_server.time.monotonic', fake_monotonic), patch('urllib.request.urlopen', fake_urlopen):
        decision = debug.deepseek_decide_agent(state)
    assert decision['recommended_intent'] == 'advance_dialog'
    assert timeouts == [3.0, 3.0, 3.0]



@pytest.mark.parametrize('model', ['deepseek-v4-flash', 'deepseek-v4.1-flash-expires-on-0910'])
def test_deepseek_decide_agent_accepts_whitelisted_model(tmp_path, model):
    debug = make_debug_state(tmp_path)
    state = make_state()
    captured = []
    response = deepseek_tool_response(
        'submit_decision',
        {'action': 'advance_dialog', 'reason': 'Continue.'},
        call_id='call_submit',
    )

    def fake_urlopen(request, timeout):
        captured.append(json.loads(request.data.decode('utf-8')))
        return FakeJSONResponse(response)
    with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', fake_urlopen):
        decision = debug.deepseek_decide_agent(state, model=model)
    assert captured[0]['model'] == model
    assert decision['model'] == model
    assert debug.read_agent_llm_log(limit=1)[0]['request']['model'] == model



def test_deepseek_decide_agent_rejects_unknown_model_name(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    captured = []
    response = deepseek_tool_response(
        'submit_decision',
        {'action': 'advance_dialog', 'reason': 'Continue.'},
        call_id='call_submit',
    )

    def fake_urlopen(request, timeout):
        captured.append(json.loads(request.data.decode('utf-8')))
        return FakeJSONResponse(response)
    with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key', 'DEEPSEEK_MODEL': 'not-a-real-model'}), patch('urllib.request.urlopen', fake_urlopen):
        decision = debug.deepseek_decide_agent(state, model='bad-model')
    assert captured[0]['model'] == 'deepseek-v4-pro'
    assert decision['model'] == 'deepseek-v4-pro'



def test_deepseek_unknown_tool_returns_clear_error(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    response = deepseek_tool_response('write_action', {}, call_id='call_bad')
    with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', lambda request, timeout: FakeJSONResponse(response)):
        with pytest.raises(RuntimeError, match='unsupported agent tool'):
            debug.deepseek_decide_agent(state)
    assert not debug.action_path.exists()



def test_deepseek_missing_submit_decision_returns_clear_error(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    response = {'choices': [{'message': {'content': '{"summary":"old path"}'}}]}
    with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', lambda request, timeout: FakeJSONResponse(response)):
        with pytest.raises(RuntimeError, match='did not call submit_decision'):
            debug.deepseek_decide_agent(state)



def test_deepseek_exceeding_tool_rounds_returns_clear_error(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    response = deepseek_tool_response(
        'get_observations',
        {'kind': 'events', 'limit': 1},
        call_id='call_obs',
    )
    with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', lambda request, timeout: FakeJSONResponse(response)):
        with pytest.raises(RuntimeError, match='within 8 tool rounds'):
            debug.deepseek_decide_agent(state)
    assert not debug.action_path.exists()



def test_deepseek_submit_decision_allowed_after_query_tool_budget(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[make_touch_object(10)])
    write_state(debug, state)
    response = {
        'choices': [{'message': {'tool_calls': [
            {'id': f'call_obs_{index}', 'type': 'function', 'function': {'name': 'get_observations', 'arguments': '{"kind":"events","limit":1}'}}
            for index in range(40)
        ] + [{
            'id': 'call_submit',
            'type': 'function',
            'function': {'name': 'submit_decision', 'arguments': json.dumps({'action': 'navigate', 'target_id': 10, 'reason': 'Inspect the reachable target.'})},
        }]}}],
    }
    with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', lambda request, timeout: FakeJSONResponse(response)):
        decision = debug.deepseek_decide_agent(state)
    assert decision['recommended_intent'] == 'inspect_target'
    assert len(decision['tool_trace']) == 41
    assert decision['tool_trace'][-1]['name'] == 'submit_decision'
    assert not debug.action_path.exists()



def test_deepseek_exceeding_tool_calls_writes_failure_log(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    response = {
        'choices': [{'message': {'tool_calls': [
            {'id': f'call_obs_{index}', 'type': 'function', 'function': {'name': 'get_observations', 'arguments': '{"kind":"events","limit":1}'}}
            for index in range(41)
        ]}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12},
    }
    with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', lambda request, timeout: FakeJSONResponse(response)):
        with pytest.raises(RuntimeError, match='exceeded 40 query tool calls'):
            debug.deepseek_decide_agent(state)
    log = debug.read_agent_log(limit=1)
    assert log[0]['ok'] == False
    assert log[0]['tool_calls'] == 41
    assert len(log[0]['tool_trace']) == 41
    assert 'exceeded 40 query tool calls' in log[0]['tool_trace'][-1]['error']
    assert log[0]['usage']['total_tokens'] == 12
    assert not debug.action_path.exists()



def test_deepseek_submit_decision_downgrades_stale_navigation_target(tmp_path):
    debug = make_debug_state(tmp_path)
    original_state = make_state(objects=[make_touch_object(52)])
    latest_state = make_state(objects=[])
    write_state(debug, original_state)
    response = {'choices': [{'message': {'tool_calls': [{'id': 'call_submit', 'type': 'function', 'function': {'name': 'submit_decision', 'arguments': json.dumps({'action': 'navigate', 'target_id': 52, 'reason': 'Inspect object #52.'})}}]}}]}

    def fake_urlopen(request, timeout):
        write_state(debug, latest_state)
        return FakeJSONResponse(response)
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



@pytest.mark.parametrize(
    "invalid_submission",
    [
        {'action': 'wait', 'reason': 'Wait.'},
        {'action': 'write_action', 'target_id': 10, 'reason': 'Inspect target.'},
        {'action': 'navigate', 'reason': 'Inspect target.'},
    ],
    ids=["removed-wait", "illegal-action", "missing-target"],
)
def test_deepseek_submit_decision_repairs_invalid_submission(tmp_path, invalid_submission):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[make_touch_object(10)])
    write_state(debug, state)
    responses = [
        deepseek_tool_response('submit_decision', invalid_submission, call_id='call_bad_submit'),
        deepseek_tool_response(
            'submit_decision',
            {'action': 'navigate', 'target_id': 10, 'reason': 'Inspect target.'},
            call_id='call_fixed_submit',
        ),
    ]
    captured = []

    def fake_urlopen(request, timeout):
        captured.append(json.loads(request.data.decode('utf-8')))
        return FakeJSONResponse(responses.pop(0))
    with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', fake_urlopen):
        decision = debug.deepseek_decide_agent(state)
    assert decision['recommended_intent'] == 'inspect_target'
    assert decision['recommended_action']['tool'] == 'navigation_step'
    assert decision['recommended_action']['args'] == {'id': 10}
    assert [entry['name'] for entry in decision['tool_trace']] == ['submit_decision', 'submit_decision']
    repair_message = captured[1]['messages'][-1]
    assert repair_message['role'] == 'tool'
    expected_error = 'invalid_action' if invalid_submission.get('action') in {'wait', 'write_action'} else 'invalid_decision_submission'
    assert json.loads(repair_message['content'])['error']['code'] == expected_error
    assert not debug.action_path.exists()



def test_deepseek_submit_decision_fails_after_one_unsuccessful_repair(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state(objects=[make_touch_object(74)])
    write_state(debug, state)
    response = deepseek_tool_response(
        'submit_decision',
        {'action': 'explore', 'target_id': 74, 'reason': 'Explore one step.'},
        call_id='call_submit',
    )
    with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), patch('urllib.request.urlopen', lambda request, timeout: FakeJSONResponse(response)):
        with pytest.raises(RuntimeError, match='remained invalid after 1 repair attempt'):
            debug.deepseek_decide_agent(state)
    assert not debug.action_path.exists()
    log = debug.read_agent_log(limit=1)
    assert log[0]['ok'] is False
    assert [entry['name'] for entry in log[0]['tool_trace']] == ['submit_decision', 'submit_decision']
