import json
from pathlib import Path

from palpal.debug_server import DebugState


def grid_bits(width, height, blocked=()):
    blocked = set(blocked)
    chars = []
    total = width * height
    for index in range(0, total, 4):
        value = 0
        for offset in range(4):
            local = index + offset
            if local >= total:
                break
            node = (local % width, local // width)
            if node not in blocked:
                value |= 1 << offset
        chars.append("0123456789abcdef"[value])
    return "".join(chars)


def make_state(width=3, height=3, start=(1, 1), objects=None, blocked=(), viewport=None, frame=1):
    state = {
        "frame": frame,
        "in_main_game": True,
        "in_battle": False,
        "scene_id": 3,
        "map_id": 10,
        "room": {"key": "test-room"},
        "party_position": {"x": start[0] * 16, "y": start[1] * 8},
        "party_direction": 3,
        "objects": objects or [],
        "nav_grid": {
            "origin": {"x": 0, "y": 0},
            "step": {"x": 16, "y": 8},
            "width": width,
            "height": height,
            "bits": grid_bits(width, height, blocked=blocked),
        },
    }
    if viewport is not None:
        state["viewport"] = viewport
    return state


def make_checkpoint_state(frame=42):
    return {
        "frame": frame,
        "in_main_game": True,
        "in_battle": False,
        "scene_id": 3,
        "map_id": 10,
        "room": {"key": "test-room"},
        "party_position": {"x": 16, "y": 8},
        "party_direction": 3,
    }


def make_startup_state():
    return {
        "status": "startup_waiting",
        "frame": 0,
        "in_main_game": False,
        "in_battle": False,
        "entering_scene": False,
        "startup_mode": "wait",
    }


def make_object(
    object_id,
    *,
    x=32,
    y=0,
    sprite_id=1,
    state=1,
    trigger_mode=1,
    trigger_script=60,
    auto_script=0,
    blocking=False,
    interactable=True,
    **extra,
):
    return {
        "id": object_id,
        "x": x,
        "y": y,
        "sprite_id": sprite_id,
        "state": state,
        "trigger_mode": trigger_mode,
        "trigger_script": trigger_script,
        "auto_script": auto_script,
        "blocking": blocking,
        "interactable": interactable,
        **extra,
    }


def make_touch_object(object_id, x=32, y=0, sprite_id=1):
    return make_object(
        object_id,
        x=x,
        y=y,
        sprite_id=sprite_id,
        trigger_mode=4,
    )


def write_state(debug, state=None):
    debug.set_live_state(state or make_state())


def make_debug_state(tmp):
    root = Path(tmp)
    asset_dir = root / "assets"
    asset_dir.mkdir(exist_ok=True)
    debug = DebugState(root / "runtime", asset_dir, root / "sdlpal")
    # Unit fixtures have no native process; integration tests exercise the real
    # file transport separately. Keep checkpoint pause/backup explicitly local.
    debug.checkpoint_native_control = lambda command, **kwargs: {
        "status": {"pause": "paused", "resume": "resumed", "validate": "validated"}[command],
    }
    debug.checkpoint_backup_native = lambda target: (target / "game.rpg").write_bytes(b"live-game-backup")
    return debug


def seed_exploration_room(debug, state, visited):
    identity = debug.room_identity(state)
    data = debug.empty_exploration()
    room = debug.exploration_room(data, identity)
    room["visited"] = [f"{x},{y}" for x, y in visited]
    debug.write_exploration(data)


def fake_save_command(debug):
    def run(command, slot):
        assert command == "save_slot"
        debug.save_slot_path(slot).write_bytes(b"checkpoint-rpg")
        return {
            "action": {"command": command, "slot": slot},
            "result": {"ok": True, "action": command, "slot": slot, "status": "saved"},
        }

    return run


def fake_load_command(command, slot):
    assert command == "load_slot"
    return {
        "action": {"command": command, "slot": slot},
        "result": {"ok": True, "action": command, "slot": slot, "status": "loaded"},
    }


def create_agent_task(debug, arguments, state=None):
    """Create through the production closed-ledger review contract."""
    cycle = debug.new_agent_tool_cycle(state)
    reviewed = debug.run_agent_tool(
        "get_tasks",
        {"include_closed": True, "detail": "summary", "offset": 0, "limit": 100},
        state,
        tool_cycle=cycle,
    )
    assert reviewed["complete"] is True
    room_id = arguments.get("room_id")
    if isinstance(room_id, str) and room_id:
        debug.run_agent_tool(
            "get_room_memory",
            {"room_id": room_id},
            state,
            tool_cycle=cycle,
        )
    return debug.run_agent_tool(
        "upsert_task",
        {**arguments, "review_token": reviewed["review_token"]},
        state,
        tool_cycle=cycle,
    )


class FakeJSONResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def deepseek_tool_response(name, arguments, *, call_id="call_tool", usage=None):
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    payload = {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }],
            },
        }],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload
