import copy
import json
import struct
import zlib

import pytest

from palpal import object_vision
from palpal.agent_runtime import AgentRuntime
from palpal.debug_server import pfrm_png_cache_key, pfrm_to_png
from palpal.object_vision import object_frame_metadata
from tests.support import make_debug_state, make_state
from tests.test_screen_inspection import vision_response


def scene(objects=None):
    state = make_state(objects=objects if objects is not None else [
        {"id": 797, "x": 16, "y": 8, "sprite_id": 266,
         "state": 2, "trigger_mode": 7, "trigger_script": 100, "interactable": True},
    ])
    state["game_instance_id"] = "test-game"
    return state


def frame(state, ids=(797,), *, sequence=1, legacy=False, meta_overrides=None):
    palette = b"\x00\x00\x00\xff\xff\x00\x00\xff\xff\xff\xff\xff"
    pixels = bytearray(64000)
    mask = bytearray(128000)
    for n, object_id in enumerate(ids):
        for x in (100 + n * 3, 101 + n * 3):
            pixels[100 * 320 + x] = 2
            struct.pack_into("<H", mask, (100 * 320 + x) * 2, object_id)
    pixels[100 * 320 + 99] = 1  # neighboring red pixel must not become target evidence
    payload = palette + pixels
    raw = b"PFRM" + struct.pack("<HHHHIII", 2, 320, 200, 2, 3, sequence, len(payload)) + payload
    if legacy:
        return raw
    meta = {k: copy.deepcopy(state[k]) for k in ("game_instance_id", "scene_id", "map_id", "room", "party_position")}
    meta.update(version=1, available=True, objects=[
        {"id": o["id"], "sprite_id": o["sprite_id"], "drawn_pixels": 3}
        for o in state["objects"]
    ])
    meta.update(meta_overrides or {})
    encoded = json.dumps(meta).encode()
    return raw + b"POBJ" + struct.pack("<I", len(encoded)) + encoded + mask


def setup(tmp_path, monkeypatch):
    debug = make_debug_state(tmp_path)
    state = scene()
    debug.set_live_state(state)
    calls = []

    def request(**kwargs):
        calls.append(kwargs)
        return vision_response(json.dumps({"description": "外观像一只鹿", "confidence": "high",
                                           "uncertainties": ["身体部分被遮挡"], "object_id": 999}))

    monkeypatch.setattr(debug.screen_inspector, "_request", request)
    return debug, state, calls


def inspect(debug, state, cycle=None, **extra):
    return debug.run_agent_tool("inspect_screen", {"focus": "object", "target_id": 797, **extra},
                                state, tool_cycle=cycle)


def test_visible_capture_is_local_then_on_demand_and_persistent(tmp_path, monkeypatch):
    debug, state, calls = setup(tmp_path, monkeypatch)
    before = copy.deepcopy(debug.read_exploration())
    debug.set_live_frame(frame(state), sequence=8)
    assert not calls
    assert debug.read_exploration() == before
    view = debug.run_agent_tool("inspect_target", {"id": 797}, state)["visual"]
    assert view["evidence_status"] == "current"
    result = inspect(debug, state, {})
    assert result["ok"] and not result["historical"]
    assert len(calls) == 1
    assert result["annotation"]["object_id"] == 797  # model's forged ID discarded
    assert "object_id" not in result["annotation"]["report"]
    assert result["annotation"]["source"]["partially_occluded"]
    again = inspect(debug, state, {})
    assert again["cached"] and len(calls) == 1
    durable = debug.read_exploration()
    for key in ("room_memos", "tasks", "transitions", "object_interactions"):
        assert durable.get(key) == before.get(key)
    assert "buffers" not in json.dumps(durable)
    assert "base64" not in json.dumps(result)
    restarted = make_debug_state(tmp_path)
    restarted.set_live_state(state)
    assert inspect(restarted, state)["annotation"] == result["annotation"]
    assert inspect(restarted, state)["historical"]
    assert debug.compact_agent_navigation(state)["targets"][0]["appearance"]["authority"] == "visual_annotation"


def test_object_runs_away_before_on_demand_query(tmp_path, monkeypatch):
    debug, state, calls = setup(tmp_path, monkeypatch)
    debug.set_live_frame(frame(state), sequence=8)
    observed_at = debug.object_vision.view(state, 797)["observed_at"]
    away = copy.deepcopy(state)
    away["objects"][0]["x"] = 1000
    debug.set_live_state(away)
    debug.set_live_frame(frame(away, ids=(), sequence=2), sequence=9)
    result = inspect(debug, away, {})
    assert result["ok"] and result["historical"]
    assert result["annotation"]["source"]["observed_at"] == observed_at
    assert result["annotation"]["source"]["frame_sequence"] == 8
    assert len(calls) == 1


def test_provider_delay_does_not_rebind_to_new_sprite(tmp_path, monkeypatch):
    debug, state, calls = setup(tmp_path, monkeypatch)
    debug.set_live_frame(frame(state))
    request = debug.screen_inspector._request
    changed = copy.deepcopy(state)
    changed["objects"][0]["sprite_id"] = 300

    def delayed(**kwargs):
        debug.set_live_state(changed)
        debug.set_live_frame(frame(changed, sequence=2))
        return request(**kwargs)

    monkeypatch.setattr(debug.screen_inspector, "_request", delayed)
    result = inspect(debug, state)
    assert result["historical"] and result["annotation"]["sprite_id"] == 266
    view = debug.object_vision.view(changed, 797)
    assert view["annotation"] is None
    assert view["prior_annotations"][0]["sprite_id"] == 266


def test_room_change_and_instance_change_clear_images(tmp_path, monkeypatch):
    debug, state, _ = setup(tmp_path, monkeypatch)
    debug.set_live_frame(frame(state))
    other = copy.deepcopy(state)
    other["map_id"] = 11
    debug.set_live_state(other)
    assert not debug.object_vision.images
    debug.set_live_state(state)
    assert not inspect(debug, state)["ok"]
    debug.set_live_frame(frame(state))
    other = {**state, "game_instance_id": "new-game"}
    debug.set_live_state(other)
    assert not debug.object_vision.images


@pytest.mark.parametrize("key", ["dialog", "choice", "shop", "in_battle", "entering_scene"])
def test_obscured_frames_are_not_captured(tmp_path, monkeypatch, key):
    debug, state, calls = setup(tmp_path, monkeypatch)
    state[key] = {"active": True} if key in {"dialog", "choice", "shop"} else True
    debug.set_live_state(state)
    debug.set_live_frame(frame(state))
    assert not debug.object_vision.images
    assert not inspect(debug, state)["ok"]
    assert not calls


def test_absent_and_legacy_and_mismatched_frames_do_not_guess(tmp_path, monkeypatch):
    debug, state, calls = setup(tmp_path, monkeypatch)
    for raw in (frame(state, legacy=True), frame(state, ids=()),
                frame(state, meta_overrides={"room": {"key": "another-room"}})):
        debug.set_live_frame(raw)
        assert not inspect(debug, state)["ok"]
    assert not calls


def test_repeat_preserves_observation_time_and_metadata_affects_binding(tmp_path, monkeypatch):
    debug, state, _ = setup(tmp_path, monkeypatch)
    raw = frame(state)
    debug.set_live_frame(raw, received_at=100)
    debug.set_live_frame(raw, received_at=200)
    assert debug.object_vision.images[(797, 266)]["observed_at"] == 100
    moved = frame(state, ids=(999,))
    assert pfrm_to_png(raw) == pfrm_to_png(moved)
    assert pfrm_png_cache_key(raw) == pfrm_png_cache_key(moved)
    debug.set_live_frame(moved)
    assert (797, 266) not in debug.object_vision.visible


def test_crop_contains_only_surviving_target_pixels_in_right_panel(tmp_path, monkeypatch):
    debug, state, _ = setup(tmp_path, monkeypatch)
    debug.set_live_frame(frame(state))
    evidence = debug.object_vision.images[(797, 266)]
    png = debug.object_vision.png(evidence)
    width, height = struct.unpack_from(">II", png, 16)
    chunks = []
    offset = 8
    while offset < len(png):
        length = struct.unpack_from(">I", png, offset)[0]
        if png[offset + 4:offset + 8] == b"IDAT":
            chunks.append(png[offset + 8:offset + 8 + length])
        offset += 12 + length
    data = zlib.decompress(b"".join(chunks))
    right = b"".join(data[y * (width * 4 + 1) + 1 + width * 2:(y + 1) * (width * 4 + 1)]
                     for y in range(height))
    assert b"\xff\x00\x00\xff" not in right
    assert right.count(b"\xff\xff\xff\xff") == 2


def test_provider_failure_is_recoverable_and_no_fact_is_written(tmp_path, monkeypatch):
    debug, state, _ = setup(tmp_path, monkeypatch)
    debug.set_live_frame(frame(state))

    def fail(**kwargs):
        raise TimeoutError()

    monkeypatch.setattr(debug.screen_inspector, "_request", fail)
    status = copy.deepcopy(debug.agent_runtime.state)
    result = inspect(debug, state, {})
    assert result["error"]["code"] == "poe_timeout"
    assert not debug.read_exploration()["object_visual_annotations"]
    assert debug.agent_runtime.state.get("pause_reason") == status.get("pause_reason")


def test_budget_and_strict_object_arguments(tmp_path, monkeypatch):
    debug, state, calls = setup(tmp_path, monkeypatch)
    debug.set_live_frame(frame(state))
    assert inspect(debug, state, {"vision_request_used": True})["error"]["code"] == "vision_budget_exhausted"
    for target in (None, 0, -1, True, "797"):
        assert inspect(debug, state, target_id=target)["error"]["code"] == "invalid_arguments"
    assert not calls


def test_byte_and_entry_limits_are_enforced(tmp_path, monkeypatch):
    debug, state, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(object_vision, "MAX_OBJECT_IMAGES", 1)
    other = {**state["objects"][0], "id": 798}
    state["objects"].append(other)
    debug.set_live_state(state)
    debug.set_live_frame(frame(state, ids=(797, 798)))
    assert len(debug.object_vision.images) == 1
    monkeypatch.setattr(object_vision, "MAX_OBJECT_IMAGE_BYTES", 1)
    debug.set_live_frame(frame(state, ids=(798,), sequence=2))
    assert not debug.object_vision.images and debug.object_vision.image_bytes == 0


def test_checkpoint_restores_annotations_not_images(tmp_path, monkeypatch):
    debug, state, _ = setup(tmp_path, monkeypatch)
    debug.set_live_frame(frame(state))
    annotation = inspect(debug, state)["annotation"]
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    debug.copy_companion_to_checkpoint(checkpoint)
    with debug.exploration_lock:
        data = debug.read_exploration()
        data["object_visual_annotations"] = {}
        debug.write_exploration(data)
    debug.restore_companion_from_checkpoint(checkpoint)
    assert debug.object_vision.annotation(state, 797) == annotation
    assert not debug.object_vision.images


def test_old_query_cannot_clear_new_room_or_bind_its_pixels(tmp_path, monkeypatch):
    debug, state, calls = setup(tmp_path, monkeypatch)
    debug.set_live_frame(frame(state))
    other = copy.deepcopy(state)
    other["map_id"] = 11
    debug.set_live_state(other)
    debug.set_live_frame(frame(other))
    context = debug.object_vision.context
    generation = debug.object_vision.generation
    assert not inspect(debug, state)["ok"]
    assert not calls
    assert debug.object_vision.context == context
    assert debug.object_vision.generation == generation
    assert debug.object_vision.images[(797, 266)]["room_id"] == context[1]


def test_restore_during_provider_request_discards_late_result(tmp_path, monkeypatch):
    debug, state, calls = setup(tmp_path, monkeypatch)
    debug.set_live_frame(frame(state))
    original = debug.screen_inspector._request

    def delayed(**kwargs):
        debug.refresh_companion_caches()
        return original(**kwargs)

    monkeypatch.setattr(debug.screen_inspector, "_request", delayed)
    assert inspect(debug, state)["error"]["code"] == "object_visual_context_changed"
    assert not debug.read_exploration()["object_visual_annotations"]


@pytest.mark.parametrize("content", ["not JSON", "[]", '{"description":"鹿"}'])
def test_malformed_model_output_is_not_persisted(tmp_path, monkeypatch, content):
    debug, state, _ = setup(tmp_path, monkeypatch)
    debug.set_live_frame(frame(state))
    monkeypatch.setattr(debug.screen_inspector, "_request", lambda **kwargs: vision_response(content))
    assert inspect(debug, state)["error"]["code"] == "poe_response_invalid"
    assert not debug.read_exploration()["object_visual_annotations"]


def test_invalid_trailer_is_rejected_without_breaking_legacy_image(tmp_path):
    raw = frame(scene())
    assert object_frame_metadata(frame(scene(), legacy=True)) is None
    with pytest.raises(ValueError):
        object_frame_metadata(raw[:-1])
    assert pfrm_to_png(raw).startswith(b"\x89PNG")


def test_trigger_completion_survives_agent_projection():
    result = {"ok": True, "status": "arrived", "target_id": 797,
              "completion": "trigger_completed", "trigger_evidence": {
                  "object_id": 797, "object_changes": {"trigger_script": {"from": 10, "to": 11}}}}
    assert AgentRuntime._compact_action_result(result) == result
    assert "completion" not in AgentRuntime._compact_action_result({"status": "arrived"})
