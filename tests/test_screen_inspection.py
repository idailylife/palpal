import base64
import json
import sys
import time
import types

import pytest

from palpal import debug_server
from palpal.screen_inspection import (
    DEFAULT_POE_VISION_TIMEOUT,
    ScreenInspector,
    normalize_visual_report,
    valid_poe_api_key,
)
from tests.support import make_debug_state, make_state


def vision_response(content, *, prompt_tokens=21, completion_tokens=7):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))],
        usage=types.SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        ),
    )


def fresh_frame(debug, png=b"test-png", sequence=3):
    debug.live_info = lambda kind: {
        "available": True,
        "sequence": sequence,
        "stale": False,
        "age_seconds": 0.1,
    }
    debug.latest_frame_png = lambda: (sequence, png)


def test_poe_key_configuration_accepts_environment_or_env_file_and_rejects_whitespace():
    assert valid_poe_api_key({}, environ={"POE_API_KEY": "env-key"})
    assert valid_poe_api_key({"POE_API_KEY": "file-key"}, environ={})
    assert not valid_poe_api_key({}, environ={})
    assert not valid_poe_api_key({"POE_API_KEY": "   "}, environ={})
    # Existing config_value semantics give a non-empty process value priority,
    # including a whitespace-only value which is then rejected rather than bypassed.
    assert not valid_poe_api_key(
        {"POE_API_KEY": "file-key"}, environ={"POE_API_KEY": "   "}
    )


def test_main_rejects_missing_poe_key_before_server_bind_or_state_creation(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("POE_API_KEY", raising=False)
    monkeypatch.setattr(debug_server, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["palpal.debug_server", "--port", "0"])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("server bind or DebugState construction happened")

    monkeypatch.setattr(debug_server, "Server", forbidden)
    monkeypatch.setattr(debug_server, "DebugState", forbidden)
    assert debug_server.main() == 2
    error = capsys.readouterr().err
    assert "POE_API_KEY is required for Agent screen inspection" in error
    assert "file-key" not in error


def test_main_accepts_dotenv_key_and_starts_components_in_order(tmp_path, monkeypatch):
    monkeypatch.delenv("POE_API_KEY", raising=False)
    (tmp_path / ".env").write_text("POE_API_KEY=file-key\n", encoding="utf-8")
    monkeypatch.setattr(debug_server, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["palpal.debug_server", "--port", "0"])
    calls = []

    class FakeState:
        runtime_dir = tmp_path / "runtime"

        def __init__(self, *_args):
            calls.append("state")

        def start_live_bridge(self):
            calls.append("listener")

    class FakeServer:
        def __init__(self, *_args):
            calls.append("bind")
            self.state = None

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            calls.append("close")

    monkeypatch.setattr(debug_server, "DebugState", FakeState)
    monkeypatch.setattr(debug_server, "Server", FakeServer)
    assert debug_server.main() == 130
    assert calls == ["bind", "state", "listener", "close"]


def test_inspect_screen_tool_is_always_registered_with_strict_schema(tmp_path):
    debug = make_debug_state(tmp_path)
    specs = {item["function"]["name"]: item["function"] for item in debug.agent_tool_specs()}
    schema = specs["inspect_screen"]["parameters"]
    assert schema["properties"]["focus"]["enum"] == ["room_layout", "passage", "ui_text"]
    assert schema["properties"]["question"]["maxLength"] == 200
    assert schema["additionalProperties"] is False
    assert "inspect_screen" not in {"get_battle", "get_item_details", "submit_battle_action"}

    for arguments in (
        {"focus": "unknown"},
        {"question": "x" * 201},
        {"question": 3},
        {"extra": True},
    ):
        result = debug.run_agent_tool("inspect_screen", arguments, make_state())
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize(
    ("frame_info", "code"),
    [
        ({"available": False, "sequence": 0}, "frame_unavailable"),
        ({"available": True, "sequence": 1, "stale": True}, "frame_stale"),
    ],
)
def test_unavailable_or_stale_frame_never_calls_poe(tmp_path, monkeypatch, frame_info, code):
    debug = make_debug_state(tmp_path)
    debug.live_info = lambda kind: frame_info
    monkeypatch.setattr(
        debug.screen_inspector,
        "_request",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Poe called")),
    )
    result = debug.run_agent_tool(
        "inspect_screen", {"focus": "passage"}, make_state(), tool_cycle=debug.new_agent_tool_cycle()
    )
    assert result["ok"] is False
    assert result["error"]["code"] == code


def test_request_uses_poe_chat_image_options_and_bounded_context(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    original_config = debug.config_value
    debug.config_value = lambda key, default=None: {
        "POE_API_KEY": "secret-test-key",
        "POE_VISION_BASE_URL": "https://poe.test/v1/",
        "POE_VISION_TIMEOUT": "invalid",
    }.get(key, original_config(key, default))
    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured["request"] = kwargs
            return vision_response('{"summary":"room","room_type":"hall"}')

    class OpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.chat = types.SimpleNamespace(completions=Completions())

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=OpenAI))
    response = debug.screen_inspector._request(
        model="gemini-3.7-flash",
        png=b"png-bytes",
        focus="passage",
        question="Is there a wall gap?",
        state={
            **make_state(),
            "dialog": {"active": True, "text": "must not be copied"},
            "choice": {"active": False},
            "shop": {"active": False},
        },
    )
    assert response.choices[0].message.content.startswith("{")
    assert captured["client"] == {
        "api_key": "secret-test-key",
        "base_url": "https://poe.test/v1",
        "timeout": DEFAULT_POE_VISION_TIMEOUT,
    }
    request = captured["request"]
    assert request["model"] == "gemini-3.7-flash"
    assert request["stream"] is False
    assert "max_tokens" not in request
    assert request["extra_body"] == {"web_search": False, "thinking_level": "low"}
    content = request["messages"][1]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert base64.b64decode(content[1]["image_url"]["url"].split(",", 1)[1]) == b"png-bytes"
    context = json.loads(content[0]["text"])
    assert context["focus"] == "passage"
    assert context["game_context"]["dialog_active"] is True
    assert "must not be copied" not in content[0]["text"]
    assert "secret-test-key" not in json.dumps(request)


@pytest.mark.parametrize("configured", ["0", "-1", "nan", "inf", "not-a-number"])
def test_invalid_vision_timeout_falls_back_to_default(tmp_path, configured):
    debug = make_debug_state(tmp_path)
    original_config = debug.config_value
    debug.config_value = lambda key, default=None: (
        configured if key == "POE_VISION_TIMEOUT" else original_config(key, default)
    )
    assert debug.screen_inspector.timeout() == DEFAULT_POE_VISION_TIMEOUT


def test_report_normalization_handles_json_fences_invalid_text_and_limits():
    fenced = normalize_visual_report(
        "```json\n" + json.dumps({
            "summary": "s" * 700,
            "room_type": "hall",
            "visible_exits": ["east"] * 20,
            "passage_cues": [{
                "location": "north wall",
                "cue": "break",
                "confidence": "certain",
            }],
            "visible_text": ["menu"],
            "uncertainties": ["occluded"],
        }) + "\n```"
    )
    assert len(fenced["summary"]) == 500
    assert len(fenced["visible_exits"]) == 8
    assert fenced["passage_cues"][0]["confidence"] == "low"

    fallback = normalize_visual_report("not json " + "x" * 800)
    assert len(fallback["summary"]) == 500
    assert fallback["visible_exits"] == []
    assert fallback["uncertainties"] == ["unstructured_model_output"]


def test_cache_uses_visual_hash_and_cycle_allows_only_one_uncached_request(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    fresh_frame(debug, b"same-png", 3)
    calls = []

    def request(**kwargs):
        calls.append(kwargs)
        return vision_response(json.dumps({
            "summary": "A possible opening is visible.",
            "room_type": "interior",
            "visible_exits": ["right edge"],
            "passage_cues": [],
            "visible_text": [],
            "uncertainties": ["edge is partly occluded"],
        }))

    monkeypatch.setattr(debug.screen_inspector, "_request", request)
    debug.agent_runtime.state["last_prompt_tokens"] = 777
    cycle = debug.new_agent_tool_cycle()
    first = debug.run_agent_tool(
        "inspect_screen", {"focus": "room_layout"}, make_state(), tool_cycle=cycle
    )
    assert first["ok"] and first["cached"] is False

    fresh_frame(debug, b"same-png", 99)
    cached = debug.run_agent_tool(
        "inspect_screen", {"focus": "room_layout"}, make_state(), tool_cycle=cycle
    )
    assert cached["ok"] and cached["cached"] is True
    assert cached["frame_sequence"] == 99
    blocked = debug.run_agent_tool(
        "inspect_screen", {"focus": "passage"}, make_state(), tool_cycle=cycle
    )
    assert blocked["error"]["code"] == "vision_budget_exhausted"
    assert len(calls) == 1

    second_cycle = debug.new_agent_tool_cycle()
    second = debug.run_agent_tool(
        "inspect_screen", {"focus": "passage"}, make_state(), tool_cycle=second_cycle
    )
    assert second["ok"] and len(calls) == 2
    session = debug.agent_runtime.snapshot()
    assert session["provider_usage"]["scopes"]["vision"]["requests"] == 2
    assert session["provider_usage"]["scopes"]["vision"]["input_tokens"] == 42
    assert session["provider_usage"]["last_request"]["scope"] == "vision"
    assert session["provider_usage"]["last_request"]["provider"] == "poe"
    assert session["provider_usage"]["last_request"]["model"] == "gemini-3.7-flash"
    assert session["last_prompt_tokens"] == 777

    serialized = json.dumps({"result": first, "session": session})
    assert "data:image" not in serialized
    assert base64.b64encode(b"same-png").decode("ascii") not in serialized
    assert "POE_API_KEY" not in serialized
    assert "cache" not in session


def test_vision_cache_is_memory_only_and_bounded_to_sixteen_entries(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    monkeypatch.setattr(
        debug.screen_inspector,
        "_request",
        lambda **_kwargs: vision_response('{"summary":"bounded"}', prompt_tokens=1, completion_tokens=1),
    )
    for index in range(17):
        result = debug.screen_inspector.inspect(
            {},
            make_state(),
            index,
            f"png-{index}".encode(),
            f"{index:064x}",
            debug.new_agent_tool_cycle(),
        )
        assert result["ok"] is True
    assert len(debug.screen_inspector.cache) == 16
    assert ("gemini-3.7-flash", f"{0:064x}", "room_layout", "") not in debug.screen_inspector.cache
    assert "cache" not in debug.agent_runtime.snapshot()


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (types.SimpleNamespace(), "poe_request_failed"),
        (TimeoutError(), "poe_timeout"),
    ],
)
def test_provider_failures_return_safe_tool_errors(tmp_path, monkeypatch, exception, code):
    debug = make_debug_state(tmp_path)
    fresh_frame(debug)
    error = exception if isinstance(exception, Exception) else RuntimeError("provider failed")
    monkeypatch.setattr(
        debug.screen_inspector,
        "_request",
        lambda **_kwargs: (_ for _ in ()).throw(error),
    )
    result = debug.run_agent_tool(
        "inspect_screen", {}, make_state(), tool_cycle=debug.new_agent_tool_cycle()
    )
    assert result["ok"] is False
    assert result["error"]["code"] == code
    assert "provider failed" not in json.dumps(result)
    assert debug.agent_runtime.snapshot()["provider_usage"]["scopes"]["vision"]["requests"] == 1


@pytest.mark.parametrize("status", [401, 402, 404, 408, 429, 500, 503])
def test_provider_http_errors_are_mapped_without_raising(tmp_path, monkeypatch, status):
    debug = make_debug_state(tmp_path)
    fresh_frame(debug)

    class ProviderError(Exception):
        status_code = status

    monkeypatch.setattr(
        debug.screen_inspector,
        "_request",
        lambda **_kwargs: (_ for _ in ()).throw(ProviderError("secret response")),
    )
    result = debug.run_agent_tool(
        "inspect_screen", {}, make_state(), tool_cycle=debug.new_agent_tool_cycle()
    )
    expected = f"poe_http_{status}" if status < 500 else "poe_server_error"
    assert result["error"]["code"] == expected


def test_invalid_provider_response_is_safe_and_records_usage(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    fresh_frame(debug)
    monkeypatch.setattr(
        debug.screen_inspector,
        "_request",
        lambda **_kwargs: types.SimpleNamespace(choices=[], usage=vision_response("x").usage),
    )
    result = debug.run_agent_tool(
        "inspect_screen", {}, make_state(), tool_cycle=debug.new_agent_tool_cycle()
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "poe_response_invalid"
    usage = debug.agent_runtime.snapshot()["provider_usage"]["scopes"]["vision"]
    assert usage["requests"] == 1
    assert usage["total_tokens"] == 28


def test_legacy_provider_usage_is_normalized_with_empty_vision_scope(tmp_path):
    runtime = make_debug_state(tmp_path).agent_runtime
    state = runtime.snapshot()
    state["provider_usage"]["scopes"].pop("vision")
    runtime._atomic_json(runtime.control_path, state)
    runtime.state = runtime._load_or_create()
    vision = runtime.snapshot()["provider_usage"]["scopes"]["vision"]
    assert vision["requests"] == 0
    assert vision["total_tokens"] == 0


def test_chat_usage_aliases_work_without_total_tokens(tmp_path):
    runtime = make_debug_state(tmp_path).agent_runtime
    runtime._record_provider_usage(
        types.SimpleNamespace(prompt_tokens=9, completion_tokens=4),
        "vision",
        model="gemini-test",
        provider="poe",
    )
    vision = runtime.snapshot()["provider_usage"]["scopes"]["vision"]
    assert vision["requests"] == 1
    assert vision["input_tokens"] == 9
    assert vision["output_tokens"] == 4
    assert vision["total_tokens"] == 13
