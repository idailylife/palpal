from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import socket
import struct
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
VALID_BUTTONS = {
    "up",
    "down",
    "left",
    "right",
    "menu",
    "search",
    "page_up",
    "page_down",
    "repeat",
    "auto",
    "defend",
    "use_item",
    "throw_item",
    "flee",
    "status",
    "force",
    "home",
    "end",
}
STALE_AFTER_SECONDS = 2.0
EXPLORATION_VERSION = 1
DEFAULT_EXPLORE_STEPS = 1
MAX_EXPLORE_STEPS = 4
MAX_NAVIGATION_SEGMENT_STEPS = 8
OPENING_LI_XIAOYAO_MONOLOGUE_MESSAGES = {1956, 1957, 1958, 1962, 1963, 1964}
MOVE_DELTAS = {
    "up": (1, -1),
    "right": (1, 1),
    "down": (-1, 1),
    "left": (-1, -1),
}
DIR_TO_BUTTON = {2: "up", 3: "right", 0: "down", 1: "left"}
BUTTON_TO_DIR = {button: direction for direction, button in DIR_TO_BUTTON.items()}


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "waiting_for_game", "message": "state.json not written yet"}
    except json.JSONDecodeError as exc:
        return {"status": "state_parse_error", "error": str(exc)}


def read_optional_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def trigger_kind(mode: int | None) -> str:
    if mode is None or mode <= 0:
        return "none"
    if mode < 4:
        return "search"
    return "touch"


def trigger_radius(mode: int | None) -> int:
    if mode is None:
        return 0
    if mode >= 4:
        return (mode - 4) * 32 + 16
    return 0


def transition_candidate(obj: dict) -> bool:
    mode = int(obj.get("trigger_mode") or 0)
    return trigger_kind(mode) == "touch" and int(obj.get("sprite_id") or 0) == 0


def pal_distance(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) * 2


def search_samples(x: int, y: int, direction: int) -> list[tuple[int, int]]:
    x_offset = 16 if direction in {2, 3} else -16
    y_offset = 8 if direction in {3, 0} else -8
    samples = [(x, y)]
    for _ in range(4):
        samples.append((x + x_offset, y + y_offset))
        samples.append((x, y + y_offset * 2))
        samples.append((x + 2 * x_offset, y))
        x += x_offset
        y += y_offset
    return samples


def search_hits(node: tuple[int, int], direction: int, obj: dict) -> bool:
    mode = int(obj.get("trigger_mode") or 0)
    if mode <= 0 or mode >= 4:
        return False
    ex = int(obj.get("x", 0)) // 32
    ey = int(obj.get("y", 0)) // 16
    eh = 1 if int(obj.get("x", 0)) % 32 else 0
    max_index = mode * 6 - 4
    for index, (sx, sy) in enumerate(search_samples(node[0], node[1], direction)):
        if index > max_index:
            continue
        dx = sx // 32
        dy = sy // 16
        dh = 1 if sx % 32 else 0
        if dx == ex and dy == ey and dh == eh:
            return True
    return False


def decode_nav_grid(state: dict) -> dict | None:
    grid = state.get("nav_grid") or {}
    origin = grid.get("origin") or {}
    step = grid.get("step") or {}
    width = int(grid.get("width") or 0)
    height = int(grid.get("height") or 0)
    bits = str(grid.get("bits") or "")
    if width <= 0 or height <= 0 or not bits:
        return None
    return {
        "origin": (int(origin.get("x") or 0), int(origin.get("y") or 0)),
        "step": (int(step.get("x") or 16), int(step.get("y") or 8)),
        "width": width,
        "height": height,
        "bits": bits,
    }


def grid_node(grid: dict, x: int, y: int) -> tuple[int, int] | None:
    ox, oy = grid["origin"]
    sx, sy = grid["step"]
    if (x - ox) % sx != 0 or (y - oy) % sy != 0:
        return None
    node = ((x - ox) // sx, (y - oy) // sy)
    if 0 <= node[0] < grid["width"] and 0 <= node[1] < grid["height"]:
        return node
    return None


def world_pos(grid: dict, node: tuple[int, int]) -> tuple[int, int]:
    ox, oy = grid["origin"]
    sx, sy = grid["step"]
    return (ox + node[0] * sx, oy + node[1] * sy)


def nav_bit(grid: dict, node: tuple[int, int]) -> bool:
    x, y = node
    if x < 0 or y < 0 or x >= grid["width"] or y >= grid["height"]:
        return False
    index = y * grid["width"] + x
    char_index = index // 4
    if char_index >= len(grid["bits"]):
        return False
    value = int(grid["bits"][char_index], 16)
    return bool(value & (1 << (index % 4)))


def reconstruct_route(
    grid: dict,
    start: tuple[int, int],
    goal: tuple[int, int],
    parent: dict[tuple[int, int], tuple[tuple[int, int], str]],
) -> dict:
    nodes = [goal]
    moves: list[str] = []
    current = goal
    while current != start:
        prev, move = parent[current]
        moves.append(move)
        nodes.append(prev)
        current = prev
    nodes.reverse()
    moves.reverse()
    return {
        "nodes": [{"x": x, "y": y} for x, y in (world_pos(grid, node) for node in nodes)],
        "moves": moves,
        "steps": len(moves),
    }


def node_key(node: tuple[int, int]) -> str:
    return f"{node[0]},{node[1]}"


def parse_node_key(key: str) -> tuple[int, int] | None:
    try:
        x, y = key.split(",", 1)
        return (int(x), int(y))
    except (TypeError, ValueError):
        return None


def edge_key(a: tuple[int, int], b: tuple[int, int]) -> str:
    return f"{node_key(a)}>{node_key(b)}"


def node_payload(grid: dict, node: tuple[int, int]) -> dict:
    x, y = world_pos(grid, node)
    return {"grid_x": node[0], "grid_y": node[1], "x": x, "y": y}


def websocket_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    header = bytearray([0x80 | (opcode & 0x0F)])
    size = len(payload)
    if size < 126:
        header.append(size)
    elif size < 65536:
        header.append(126)
        header.extend(struct.pack("!H", size))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", size))
    return bytes(header) + payload


class DebugState:
    def __init__(self, runtime_dir: Path, asset_dir: Path, game_binary: Path):
        self.runtime_dir = runtime_dir
        self.asset_dir = asset_dir
        self.game_binary = game_binary
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.action_log: list[dict] = []
        self.event_log: list[dict] = self.read_events()
        self.dialog_history: list[dict] = self.read_dialog_history()
        self.last_dialog_key: str | None = self.dialog_history[-1].get("source_key") if self.dialog_history else None
        self.last_observed_state: dict | None = None
        self.exploration_lock = threading.Lock()

    @property
    def state_path(self) -> Path:
        return self.runtime_dir / "state.json"

    @property
    def screen_path(self) -> Path:
        return self.runtime_dir / "screen.bmp"

    @property
    def frame_path(self) -> Path:
        return self.runtime_dir / "frame.rgba"

    @property
    def action_path(self) -> Path:
        return self.runtime_dir / "action.txt"

    @property
    def action_result_path(self) -> Path:
        return self.runtime_dir / "action_result.json"

    @property
    def navigation_log_path(self) -> Path:
        return self.runtime_dir / "logs" / "navigation.log"

    @property
    def exploration_path(self) -> Path:
        return self.runtime_dir / "exploration.json"

    @property
    def events_path(self) -> Path:
        return self.runtime_dir / "events.json"

    @property
    def dialogs_path(self) -> Path:
        return self.runtime_dir / "dialog_history.json"

    def read_events(self) -> list[dict]:
        try:
            data = json.loads(self.events_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def write_events(self) -> None:
        tmp = self.runtime_dir / "events.json.tmp"
        tmp.write_text(json.dumps(self.event_log[-200:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.events_path)

    def read_dialog_history(self) -> list[dict]:
        try:
            data = json.loads(self.dialogs_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def write_dialog_history(self) -> None:
        tmp = self.runtime_dir / "dialog_history.json.tmp"
        tmp.write_text(
            json.dumps(self.dialog_history[-2000:], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.dialogs_path)

    def append_event(self, state: dict, event_type: str, payload: dict) -> None:
        event = {
            "time": time.time(),
            "frame": state.get("frame"),
            "scene_id": state.get("scene_id"),
            "map_id": state.get("map_id"),
            "room_key": (state.get("room") or {}).get("key"),
            "type": event_type,
            **payload,
        }
        self.event_log.append(event)
        self.event_log = self.event_log[-200:]
        self.write_events()

    def append_navigation_diagnostic(self, event_type: str, payload: dict | None = None, state: dict | None = None) -> None:
        payload = payload or {}
        entry = {
            "time": time.time(),
            "type": event_type,
            **payload,
        }
        if state is not None:
            pos = state.get("party_position") or {}
            dialog = state.get("dialog") or {}
            entry.update(
                {
                    "frame": state.get("frame"),
                    "scene_id": state.get("scene_id"),
                    "map_id": state.get("map_id"),
                    "room_key": (state.get("room") or {}).get("key"),
                    "region_key": state.get("region_key"),
                    "party_position": {"x": pos.get("x"), "y": pos.get("y")},
                    "dialog_active": bool(dialog.get("active")),
                    "dialog_latest_text": (dialog.get("latest_event") or {}).get("text"),
                    "in_battle": bool(state.get("in_battle")),
                    "in_main_game": state.get("in_main_game"),
                    "action_pending": self.action_path.exists(),
                }
            )
        self.navigation_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.navigation_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def dialog_source_key(self, event: dict) -> str:
        return ":".join(
            str(event.get(key))
            for key in ("sequence", "frame", "message_id", "event_object_id", "text")
        )

    def dialog_speaker_hint(self, event: dict) -> str | None:
        speaker = event.get("speaker_hint")
        if speaker:
            return speaker
        text = str(event.get("text") or "")
        if text.startswith(("得到", "失去", "获得", "取得")):
            return "系统"
        if (
            event.get("scene_id") == 1
            and event.get("map_id") == 12
            and event.get("event_object_id") == 5
            and event.get("message_id") in OPENING_LI_XIAOYAO_MONOLOGUE_MESSAGES
        ):
            return "李逍遙"
        return None

    def record_dialog_event(self, state: dict) -> None:
        if state.get("status") in {"waiting_for_game", "state_parse_error"}:
            return

        dialog = state.get("dialog") or {}
        event = dialog.get("latest_event")
        if not isinstance(event, dict) or not event.get("text"):
            return

        source_key = self.dialog_source_key(event)
        if source_key == self.last_dialog_key:
            return

        entry = {
            "time": time.time(),
            "source_key": source_key,
            "sequence": event.get("sequence"),
            "frame": event.get("frame", state.get("frame")),
            "scene_id": event.get("scene_id", state.get("scene_id")),
            "map_id": event.get("map_id", state.get("map_id")),
            "room_key": (state.get("room") or {}).get("key"),
            "region_key": state.get("region_key"),
            "event_object_id": event.get("event_object_id"),
            "message_id": event.get("message_id"),
            "dialog_position": event.get("dialog_position") or dialog.get("position"),
            "speaker_hint": self.dialog_speaker_hint(event),
            "text": event.get("text"),
        }
        self.dialog_history.append(entry)
        self.dialog_history = self.dialog_history[-2000:]
        self.last_dialog_key = source_key
        self.write_dialog_history()

    def dialogs_view(self, limit: int = 100, speaker: str | None = None) -> dict:
        limit = max(1, min(int(limit), 500))
        dialogs = self.dialog_history
        if speaker:
            dialogs = [entry for entry in dialogs if entry.get("speaker_hint") == speaker]
        return {"dialogs": dialogs[-limit:], "total": len(dialogs)}

    def observed_inventory(self, state: dict) -> dict[int, dict]:
        return {
            int(item.get("item_id") or 0): {
                "amount": int(item.get("amount") or 0),
                "amount_in_use": int(item.get("amount_in_use") or 0),
            }
            for item in state.get("inventory") or []
            if int(item.get("item_id") or 0) != 0
        }

    def observed_objects(self, state: dict) -> dict[int, dict]:
        observed = {}
        for obj in state.get("objects") or []:
            obj_id = int(obj.get("id") or 0)
            observed[obj_id] = {
                "state": obj.get("state"),
                "interactable": bool(obj.get("interactable")),
                "blocking": bool(obj.get("blocking")),
                "vanish_time": obj.get("vanish_time"),
                "sprite_id": obj.get("sprite_id"),
            }
        return observed

    def record_state_events(self, state: dict) -> None:
        if state.get("status") in {"waiting_for_game", "state_parse_error"}:
            return

        current = {
            "cash": int(state.get("cash") or 0),
            "inventory": self.observed_inventory(state),
            "objects": self.observed_objects(state),
        }
        previous = self.last_observed_state
        self.last_observed_state = current
        if previous is None:
            return

        cash_delta = current["cash"] - previous["cash"]
        if cash_delta:
            self.append_event(state, "cash_changed", {"delta": cash_delta, "cash": current["cash"]})

        item_ids = set(previous["inventory"]) | set(current["inventory"])
        for item_id in sorted(item_ids):
            before = previous["inventory"].get(item_id, {}).get("amount", 0)
            after = current["inventory"].get(item_id, {}).get("amount", 0)
            delta = after - before
            if delta:
                self.append_event(
                    state,
                    "inventory_changed",
                    {"item_id": item_id, "delta": delta, "amount": after},
                )

        object_ids = set(previous["objects"]) & set(current["objects"])
        for obj_id in sorted(object_ids):
            before = previous["objects"][obj_id]
            after = current["objects"][obj_id]
            changes = {
                key: {"from": before.get(key), "to": after.get(key)}
                for key in ("state", "interactable", "blocking", "vanish_time", "sprite_id")
                if before.get(key) != after.get(key)
            }
            if changes:
                self.append_event(state, "object_changed", {"object_id": obj_id, "changes": changes})

    def target_object(self, state: dict, target_id: int) -> dict | None:
        for obj in state.get("objects") or []:
            if int(obj.get("id") or 0) == target_id:
                return obj
        return None

    def touch_boundary_nodes_for_object(self, obj: dict, grid: dict) -> set[tuple[int, int]]:
        nodes: set[tuple[int, int]] = set()
        mode = int(obj.get("trigger_mode") or 0)
        radius = trigger_radius(mode)
        if radius <= 0:
            return nodes
        target = (int(obj.get("x") or 0), int(obj.get("y") or 0))
        for y in range(grid["height"]):
            for x in range(grid["width"]):
                node = (x, y)
                if nav_bit(grid, node) and pal_distance(world_pos(grid, node), target) < radius:
                    nodes.add(node)
        return nodes

    def touch_boundary_nodes(self, state: dict, grid: dict, target_id: int | None) -> set[tuple[int, int]]:
        nodes: set[tuple[int, int]] = set()
        for obj in state.get("objects") or []:
            if not obj.get("interactable"):
                continue
            if int(obj.get("id") or 0) == target_id:
                continue
            nodes.update(self.touch_boundary_nodes_for_object(obj, grid))
        return nodes

    def blocking_nodes_by_object(self, state: dict, grid: dict, target_id: int | None = None) -> dict[tuple[int, int], dict]:
        nodes: dict[tuple[int, int], dict] = {}
        for obj in state.get("objects") or []:
            obj_id = int(obj.get("id") or 0)
            if obj_id == target_id or not obj.get("blocking"):
                continue
            node = grid_node(grid, int(obj.get("x") or 0), int(obj.get("y") or 0))
            if node is not None and nav_bit(grid, node):
                nodes.setdefault(node, obj)
        return nodes

    def room_identity(self, state: dict) -> dict | None:
        if state.get("status") in {"waiting_for_game", "state_parse_error"}:
            return None
        scene_id = state.get("scene_id")
        map_id = state.get("map_id")
        if scene_id is None or map_id is None:
            return None
        room = state.get("room") or {}
        room_key = room.get("key") or state.get("region_key") or "unknown"
        return {
            "id": f"{scene_id}/{map_id}/{room_key}",
            "scene_id": scene_id,
            "map_id": map_id,
            "room_key": room_key,
        }

    def empty_exploration(self) -> dict:
        return {"version": EXPLORATION_VERSION, "rooms": {}}

    def read_exploration(self) -> dict:
        try:
            data = json.loads(self.exploration_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return self.empty_exploration()
        if not isinstance(data, dict) or data.get("version") != EXPLORATION_VERSION:
            return self.empty_exploration()
        rooms = data.get("rooms")
        if not isinstance(rooms, dict):
            data["rooms"] = {}
        return data

    def write_exploration(self, data: dict) -> None:
        tmp = self.runtime_dir / "exploration.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.exploration_path)

    def exploration_room(self, data: dict, identity: dict) -> dict:
        rooms = data.setdefault("rooms", {})
        room = rooms.get(identity["id"])
        if not isinstance(room, dict):
            room = {
                "scene_id": identity["scene_id"],
                "map_id": identity["map_id"],
                "room_key": identity["room_key"],
                "visited": [],
                "frontier": [],
                "blocked_edges": [],
                "last_route": None,
                "complete": False,
                "updated_at": None,
            }
            rooms[identity["id"]] = room
        return room

    def safe_exploration_nodes(self, state: dict, grid: dict) -> set[tuple[int, int]]:
        blocked = self.touch_boundary_nodes(state, grid, target_id=None)
        safe: set[tuple[int, int]] = set()
        for y in range(grid["height"]):
            for x in range(grid["width"]):
                node = (x, y)
                if nav_bit(grid, node) and node not in blocked:
                    safe.add(node)
        return safe

    def recompute_exploration_room(self, state: dict, grid: dict, room: dict, current: tuple[int, int] | None) -> bool:
        old = (
            tuple(room.get("visited") or []),
            tuple(room.get("frontier") or []),
            tuple(room.get("blocked_edges") or []),
            json.dumps(room.get("last_route"), sort_keys=True),
            bool(room.get("complete")),
        )

        safe = self.safe_exploration_nodes(state, grid)
        visited = {parse_node_key(key) for key in room.get("visited", [])}
        visited = {node for node in visited if node is not None}
        if current is not None and current in safe:
            visited.add(current)

        last_route = room.get("last_route")
        if isinstance(last_route, dict) and last_route.get("pending"):
            expected = last_route.get("expected_node") or {}
            expected_node = (int(expected.get("grid_x", -1)), int(expected.get("grid_y", -1)))
            if current == expected_node:
                last_route["pending"] = False

        frontier: set[tuple[int, int]] = set()
        blocked_edges: set[str] = set()
        for node in visited:
            if node not in safe:
                continue
            for delta in MOVE_DELTAS.values():
                next_node = (node[0] + delta[0], node[1] + delta[1])
                if next_node in safe:
                    if next_node not in visited:
                        frontier.add(next_node)
                else:
                    blocked_edges.add(edge_key(node, next_node))

        room["visited"] = sorted(node_key(node) for node in visited)
        room["frontier"] = sorted(node_key(node) for node in frontier)
        room["blocked_edges"] = sorted(blocked_edges)
        room["complete"] = len(frontier) == 0 and bool(visited)

        new = (
            tuple(room.get("visited") or []),
            tuple(room.get("frontier") or []),
            tuple(room.get("blocked_edges") or []),
            json.dumps(room.get("last_route"), sort_keys=True),
            bool(room.get("complete")),
        )
        if new != old:
            room["updated_at"] = time.time()
            return True
        return False

    def update_exploration(self, state: dict) -> dict:
        grid = decode_nav_grid(state)
        identity = self.room_identity(state)
        if grid is None:
            return {"status": "no_nav_grid", "visited": 0, "frontier": 0, "blocked_edges": 0, "complete": False}
        if identity is None:
            return {"status": "no_room", "visited": 0, "frontier": 0, "blocked_edges": 0, "complete": False}

        pos = state.get("party_position") or {}
        current = grid_node(grid, int(pos.get("x") or 0), int(pos.get("y") or 0))
        with self.exploration_lock:
            data = self.read_exploration()
            room = self.exploration_room(data, identity)
            changed = self.recompute_exploration_room(state, grid, room, current)
            if changed:
                self.write_exploration(data)
            return self.exploration_view(state, data, grid, room, status="ok")

    def exploration_view(self, state: dict, data: dict, grid: dict, room: dict, status: str = "ok") -> dict:
        identity = self.room_identity(state)
        current = None
        pos = state.get("party_position") or {}
        if grid is not None:
            current_node = grid_node(grid, int(pos.get("x") or 0), int(pos.get("y") or 0))
            if current_node is not None:
                current = node_payload(grid, current_node)

        def nodes_for(keys: list[str]) -> list[dict]:
            nodes = []
            for key in keys:
                node = parse_node_key(key)
                if node is not None:
                    nodes.append(node_payload(grid, node))
            return nodes

        return {
            "status": status,
            "room_id": identity["id"] if identity else None,
            "scene_id": identity["scene_id"] if identity else None,
            "map_id": identity["map_id"] if identity else None,
            "room_key": identity["room_key"] if identity else None,
            "visited": len(room.get("visited") or []),
            "frontier": len(room.get("frontier") or []),
            "blocked_edges": len(room.get("blocked_edges") or []),
            "complete": bool(room.get("complete")),
            "updated_at": room.get("updated_at"),
            "current": current,
            "nodes": {
                "visited": nodes_for(room.get("visited") or []),
                "frontier": nodes_for(room.get("frontier") or []),
            },
            "last_route": room.get("last_route"),
        }

    def current_exploration(self, state: dict) -> dict:
        grid = decode_nav_grid(state)
        identity = self.room_identity(state)
        if grid is None:
            return {"status": "no_nav_grid", "visited": 0, "frontier": 0, "blocked_edges": 0, "complete": False}
        if identity is None:
            return {"status": "no_room", "visited": 0, "frontier": 0, "blocked_edges": 0, "complete": False}
        with self.exploration_lock:
            data = self.read_exploration()
            room = self.exploration_room(data, identity)
            return self.exploration_view(state, data, grid, room, status="ok")

    def route_to_frontier(
        self,
        grid: dict,
        start: tuple[int, int],
        frontier: set[tuple[int, int]],
        safe: set[tuple[int, int]],
    ) -> dict | None:
        queue = deque([start])
        seen = {start}
        parent: dict[tuple[int, int], tuple[tuple[int, int], str]] = {}
        goal: tuple[int, int] | None = start if start in frontier else None

        while queue and goal is None:
            node = queue.popleft()
            for move, delta in MOVE_DELTAS.items():
                next_node = (node[0] + delta[0], node[1] + delta[1])
                if next_node in seen or next_node not in safe:
                    continue
                seen.add(next_node)
                parent[next_node] = (node, move)
                if next_node in frontier:
                    goal = next_node
                    break
                queue.append(next_node)

        if goal is None:
            return None
        return reconstruct_route(grid, start, goal, parent)

    def exploration_step(self, state: dict, max_steps: int = DEFAULT_EXPLORE_STEPS) -> dict:
        state_info = self.file_info(self.state_path)
        if not state_info.get("available") or state_info.get("stale"):
            return {"ok": False, "status": "state_stale", "state_file": state_info}
        if state.get("in_battle"):
            return {"ok": False, "status": "in_battle"}
        if (state.get("dialog") or {}).get("active"):
            return {"ok": False, "status": "in_dialog"}
        if state.get("in_main_game") is False:
            return {"ok": False, "status": "not_in_main_game"}

        grid = decode_nav_grid(state)
        identity = self.room_identity(state)
        if grid is None:
            return {"ok": False, "status": "no_nav_grid"}
        if identity is None:
            return {"ok": False, "status": "no_room"}

        pos = state.get("party_position") or {}
        current = grid_node(grid, int(pos.get("x") or 0), int(pos.get("y") or 0))
        if current is None or not nav_bit(grid, current):
            return {"ok": False, "status": "bad_start"}

        max_steps = max(1, min(int(max_steps), MAX_EXPLORE_STEPS))
        with self.exploration_lock:
            data = self.read_exploration()
            room = self.exploration_room(data, identity)
            self.recompute_exploration_room(state, grid, room, current)

            last_route = room.get("last_route")
            if isinstance(last_route, dict) and last_route.get("pending"):
                expected = last_route.get("expected_node") or {}
                start = last_route.get("start_node") or {}
                expected_node = (int(expected.get("grid_x", -1)), int(expected.get("grid_y", -1)))
                start_node = (int(start.get("grid_x", -1)), int(start.get("grid_y", -1)))
                if current == expected_node:
                    last_route["pending"] = False
                elif current == start_node and time.time() - float(last_route.get("sent_at") or 0) < 1.5:
                    self.write_exploration(data)
                    return {"ok": False, "status": "action_pending", "exploration": self.exploration_view(state, data, grid, room)}
                else:
                    self.write_exploration(data)
                    return {
                        "ok": False,
                        "status": "position_deviated",
                        "current": node_payload(grid, current),
                        "expected": expected,
                    }

            safe = self.safe_exploration_nodes(state, grid)
            if current not in safe:
                return {"ok": False, "status": "unsafe_start", "current": node_payload(grid, current)}

            frontier = {parse_node_key(key) for key in room.get("frontier", [])}
            frontier = {node for node in frontier if node is not None and node in safe}
            if not frontier:
                room["complete"] = True
                room["updated_at"] = time.time()
                self.write_exploration(data)
                return {"ok": True, "status": "complete", "exploration": self.exploration_view(state, data, grid, room)}

            route = self.route_to_frontier(grid, current, frontier, safe)
            if route is None or not route.get("moves"):
                return {"ok": False, "status": "no_reachable_frontier", "exploration": self.exploration_view(state, data, grid, room)}

            planned_moves = route["moves"][:max_steps]
            sent_moves = planned_moves[:1]
            expected = (current[0] + MOVE_DELTAS[sent_moves[0]][0], current[1] + MOVE_DELTAS[sent_moves[0]][1])
            last_route = {
                "nodes": [node_payload(grid, current), node_payload(grid, expected)],
                "moves": sent_moves,
                "planned_moves": planned_moves,
                "target": route["nodes"][-1],
                "start_node": node_payload(grid, current),
                "expected_node": node_payload(grid, expected),
                "pending": True,
                "sent_at": time.time(),
            }
            room["last_route"] = last_route
            room["updated_at"] = time.time()
            self.write_exploration(data)

        action = self.write_action(sent_moves, 1)
        return {
            "ok": True,
            "status": "stepping",
            "action": action,
            "route": last_route,
            "exploration": self.current_exploration(state),
        }

    def reset_exploration(self) -> dict:
        with self.exploration_lock:
            try:
                self.exploration_path.unlink()
            except FileNotFoundError:
                pass
        return {"ok": True, "status": "reset"}


    def goal_nodes(self, state: dict, grid: dict, target: dict) -> tuple[set[tuple[int, int]], dict[tuple[int, int], int]]:
        goals: set[tuple[int, int]] = set()
        search_dirs: dict[tuple[int, int], int] = {}
        mode = int(target.get("trigger_mode") or 0)
        kind = trigger_kind(mode)
        target_pos = (int(target.get("x") or 0), int(target.get("y") or 0))

        for y in range(grid["height"]):
            for x in range(grid["width"]):
                node = (x, y)
                if not nav_bit(grid, node):
                    continue
                pos = world_pos(grid, node)
                if kind == "touch":
                    if pal_distance(pos, target_pos) < trigger_radius(mode):
                        goals.add(node)
                elif kind == "search":
                    for direction in (2, 3, 0, 1):
                        if search_hits(pos, direction, target):
                            goals.add(node)
                            search_dirs[node] = direction
                            break
        return goals, search_dirs

    def route_between_nodes(
        self,
        grid: dict,
        start: tuple[int, int],
        goals: set[tuple[int, int]],
        blocked: set[tuple[int, int]],
    ) -> dict | None:
        blocked = set(blocked)
        blocked.discard(start)
        queue = deque([start])
        seen = {start}
        parent: dict[tuple[int, int], tuple[tuple[int, int], str]] = {}
        goal: tuple[int, int] | None = start if start in goals else None

        while queue and goal is None:
            node = queue.popleft()
            for move, delta in MOVE_DELTAS.items():
                next_node = (node[0] + delta[0], node[1] + delta[1])
                if next_node in seen or next_node in blocked or not nav_bit(grid, next_node):
                    continue
                seen.add(next_node)
                parent[next_node] = (node, move)
                if next_node in goals:
                    goal = next_node
                    break
                queue.append(next_node)

        if goal is None:
            return None
        return reconstruct_route(grid, start, goal, parent)

    def blocker_interaction_plan(
        self,
        state: dict,
        grid: dict,
        start: tuple[int, int],
        blocker: dict,
        transition_blocked: set[tuple[int, int]],
        dynamic_blocked: set[tuple[int, int]],
    ) -> dict | None:
        mode = int(blocker.get("trigger_mode") or 0)
        if not blocker.get("interactable") or trigger_kind(mode) != "search":
            return None

        blocker_node = grid_node(grid, int(blocker.get("x") or 0), int(blocker.get("y") or 0))
        goals, search_dirs = self.goal_nodes(state, grid, blocker)
        if not goals:
            return None

        blocked = set(transition_blocked) | set(dynamic_blocked)
        if blocker_node is not None:
            blocked.discard(blocker_node)
        route = self.route_between_nodes(grid, start, goals, blocked)
        if route is None:
            return None

        route_nodes = route.get("nodes") or []
        if not route_nodes:
            return None
        goal_node = grid_node(grid, int(route_nodes[-1]["x"]), int(route_nodes[-1]["y"]))
        if goal_node is None:
            return None
        direction = search_dirs.get(goal_node, int(state.get("party_direction") or 0))
        return {
            "object_id": int(blocker.get("id") or 0),
            "required_direction": DIR_TO_BUTTON.get(direction, "down"),
            "route": route,
            "blocker": {
                "id": int(blocker.get("id") or 0),
                "x": int(blocker.get("x") or 0),
                "y": int(blocker.get("y") or 0),
                "state": int(blocker.get("state") or 0),
                "blocking": bool(blocker.get("blocking")),
                "trigger_mode": mode,
                "trigger_script": int(blocker.get("trigger_script") or 0),
            },
        }

    def route_to_object(self, state: dict, target_id: int, avoid_transitions: bool = True) -> dict:
        grid = decode_nav_grid(state)
        target = self.target_object(state, target_id)
        pos = state.get("party_position") or {}
        if grid is None:
            return {"ok": False, "status": "no_nav_grid", "target_id": target_id}
        if target is None:
            return {"ok": False, "status": "unknown_target", "target_id": target_id}
        if not target.get("interactable"):
            return {"ok": False, "status": "target_inactive", "target_id": target_id}
        start = grid_node(grid, int(pos.get("x") or 0), int(pos.get("y") or 0))
        if start is None or not nav_bit(grid, start):
            return {"ok": False, "status": "bad_start", "target_id": target_id}

        goals, search_dirs = self.goal_nodes(state, grid, target)
        if not goals:
            return {
                "ok": True,
                "target_id": target_id,
                "status": "unreachable_static",
                "kind": trigger_kind(int(target.get("trigger_mode") or 0)),
            }

        transition_blocked = self.touch_boundary_nodes(state, grid, target_id) if avoid_transitions else set()
        blocking_by_node = self.blocking_nodes_by_object(state, grid, target_id)
        dynamic_blocked = set(blocking_by_node)
        blocked = set(transition_blocked) | dynamic_blocked
        route = self.route_between_nodes(grid, start, goals, blocked)

        kind = trigger_kind(int(target.get("trigger_mode") or 0))
        if route is None:
            relaxed_route = self.route_between_nodes(grid, start, goals, transition_blocked)
            if relaxed_route is not None:
                for payload in relaxed_route.get("nodes") or []:
                    node = grid_node(grid, int(payload["x"]), int(payload["y"]))
                    blocker = blocking_by_node.get(node) if node is not None else None
                    if blocker is None:
                        continue
                    interaction = self.blocker_interaction_plan(
                        state,
                        grid,
                        start,
                        blocker,
                        transition_blocked,
                        dynamic_blocked,
                    )
                    if interaction is not None:
                        return {
                            "ok": True,
                            "target_id": target_id,
                            "status": "blocked_by_interactable",
                            "kind": kind,
                            "blocked_route": relaxed_route,
                            "interaction": interaction,
                            "blocker": interaction["blocker"],
                        }
            return {
                "ok": True,
                "target_id": target_id,
                "status": "unreachable_static",
                "kind": kind,
                "blocked_by_transition": avoid_transitions,
            }

        action = "touch" if kind == "touch" else "search"
        result = {
            "ok": True,
            "target_id": target_id,
            "status": "transition" if transition_candidate(target) else "reachable",
            "kind": kind,
            "action": action,
            "route": route,
        }
        if transition_candidate(target):
            result["transition_candidate"] = True
        if kind == "search":
            route_nodes = route.get("nodes") or []
            route_goal = None
            if route_nodes:
                route_goal = grid_node(grid, int(route_nodes[-1]["x"]), int(route_nodes[-1]["y"]))
            direction = search_dirs.get(route_goal, int(state.get("party_direction") or 0))
            result["required_direction"] = DIR_TO_BUTTON.get(direction, "down")
        return result

    def navigation_step(self, state: dict, target_id: int) -> dict:
        state_info = self.file_info(self.state_path)
        if not state_info.get("available") or state_info.get("stale"):
            return {"ok": False, "status": "state_stale", "state_file": state_info, "target_id": target_id}
        if state.get("in_battle"):
            return {"ok": False, "status": "in_battle", "target_id": target_id}
        if (state.get("dialog") or {}).get("active"):
            return {"ok": False, "status": "in_dialog", "target_id": target_id}
        if state.get("in_main_game") is False:
            return {"ok": False, "status": "not_in_main_game", "target_id": target_id}
        if self.action_path.exists():
            return {"ok": False, "status": "action_pending", "target_id": target_id}

        route = self.route_to_object(state, target_id, avoid_transitions=True)
        if route.get("status") == "blocked_by_interactable":
            interaction = route.get("interaction") or {}
            interaction_route = interaction.get("route") or {}
            moves = interaction_route.get("moves") or []
            if moves:
                move = moves[0]
                if move not in MOVE_DELTAS:
                    return {"ok": False, "status": "bad_route", "target_id": target_id}
                segment_steps = 1
                for next_move in moves[1:MAX_NAVIGATION_SEGMENT_STEPS]:
                    if next_move != move:
                        break
                    segment_steps += 1
                action = self.write_action([move], 1)
                route_nodes = interaction_route.get("nodes") or []
                expected_node = route_nodes[segment_steps] if len(route_nodes) > segment_steps else None
                return {
                    "ok": True,
                    "status": "stepping",
                    "target_id": target_id,
                    "move": move,
                    "segment_steps": segment_steps,
                    "segment_moves": moves[:segment_steps],
                    "expected_node": expected_node,
                    "action": action,
                    "remaining_steps": len(moves),
                    "route": interaction_route,
                    "blocked_route": route.get("blocked_route"),
                    "blocker": route.get("blocker"),
                }

            direction = interaction.get("required_direction")
            buttons = [button for button in (direction, "search") if button]
            action = self.write_action(buttons, 1)
            return {
                "ok": True,
                "status": "interacting",
                "target_id": target_id,
                "action": action,
                "route": route.get("blocked_route"),
                "blocker": route.get("blocker"),
                "required_direction": direction,
            }

        if route.get("status") not in {"reachable", "transition"}:
            return {
                "ok": False,
                "status": route.get("status") or "unreachable",
                "target_id": target_id,
                "route": route.get("route"),
                "blocker": route.get("blocker"),
            }

        moves = route.get("route", {}).get("moves") or []
        if not moves:
            if route.get("kind") == "search":
                target = self.target_object(state, target_id)
                pos = state.get("party_position") or {}
                current_pos = (int(pos.get("x") or 0), int(pos.get("y") or 0))
                current_direction = int(state.get("party_direction") or 0)
                if target is not None and search_hits(current_pos, current_direction, target):
                    action = self.write_action(["search"], 1)
                    return {
                        "ok": True,
                        "status": "interacting",
                        "target_id": target_id,
                        "action": action,
                        "route": route.get("route"),
                        "required_direction": DIR_TO_BUTTON.get(current_direction, "down"),
                    }
                return {
                    "ok": False,
                    "status": "needs_direction",
                    "target_id": target_id,
                    "route": route.get("route"),
                    "required_direction": route.get("required_direction"),
                }
            return {"ok": True, "status": "arrived", "target_id": target_id, "route": route.get("route")}

        move = moves[0]
        if move not in MOVE_DELTAS:
            return {"ok": False, "status": "bad_route", "target_id": target_id}
        segment_steps = 1
        for next_move in moves[1:MAX_NAVIGATION_SEGMENT_STEPS]:
            if next_move != move:
                break
            segment_steps += 1
        action = self.write_action([move], 1)
        route_nodes = route.get("route", {}).get("nodes") or []
        expected_node = route_nodes[segment_steps] if len(route_nodes) > segment_steps else None
        segment_moves = moves[:segment_steps]
        return {
            "ok": True,
            "status": "stepping",
            "target_id": target_id,
            "move": move,
            "segment_steps": segment_steps,
            "segment_moves": segment_moves,
            "expected_node": expected_node,
            "action": action,
            "remaining_steps": len(moves),
            "route": route.get("route"),
        }

    def reachable_nodes(
        self,
        grid: dict,
        start: tuple[int, int],
        blocked: set[tuple[int, int]],
    ) -> tuple[list[tuple[int, int]], dict[tuple[int, int], int]]:
        blocked = set(blocked)
        blocked.discard(start)
        queue = deque([start])
        order = [start]
        distance = {start: 0}

        while queue:
            node = queue.popleft()
            for delta in MOVE_DELTAS.values():
                next_node = (node[0] + delta[0], node[1] + delta[1])
                if next_node in distance or next_node in blocked or not nav_bit(grid, next_node):
                    continue
                distance[next_node] = distance[node] + 1
                order.append(next_node)
                queue.append(next_node)
        return order, distance

    def first_search_goal(
        self,
        grid: dict,
        nodes: list[tuple[int, int]],
        target: dict,
    ) -> tuple[tuple[int, int], int] | None:
        for node in nodes:
            pos = world_pos(grid, node)
            for direction in (2, 3, 0, 1):
                if search_hits(pos, direction, target):
                    return node, direction
        return None

    def search_goal_key(self, obj: dict) -> tuple[int, int, int]:
        x = int(obj.get("x", 0))
        return (x // 32, int(obj.get("y", 0)) // 16, 1 if x % 32 else 0)

    def search_goal_index(
        self,
        grid: dict,
        nodes: list[tuple[int, int]],
    ) -> dict[int, dict[tuple[int, int, int], tuple[tuple[int, int], int]]]:
        indexes: dict[int, dict[tuple[int, int, int], tuple[tuple[int, int], int]]] = {
            1: {},
            2: {},
            3: {},
        }
        for node in nodes:
            pos = world_pos(grid, node)
            for direction in (2, 3, 0, 1):
                samples = search_samples(pos[0], pos[1], direction)
                for mode in (1, 2, 3):
                    max_index = mode * 6 - 4
                    index = indexes[mode]
                    for sample_index, (sx, sy) in enumerate(samples):
                        if sample_index > max_index:
                            break
                        key = (sx // 32, sy // 16, 1 if sx % 32 else 0)
                        if key not in index:
                            index[key] = (node, direction)
        return indexes

    def navigation(self, state: dict) -> dict:
        grid = decode_nav_grid(state)
        if grid is None:
            return {"status": "no_nav_grid", "targets": []}
        pos = state.get("party_position") or {}
        start = grid_node(grid, int(pos.get("x") or 0), int(pos.get("y") or 0))
        if start is None or not nav_bit(grid, start):
            return {"status": "bad_start", "targets": []}

        interactables = [obj for obj in state.get("objects") or [] if obj.get("interactable")]
        touch_nodes_by_id = {
            int(obj.get("id") or 0): self.touch_boundary_nodes_for_object(obj, grid)
            for obj in interactables
            if trigger_kind(int(obj.get("trigger_mode") or 0)) == "touch"
        }
        all_touch_nodes: set[tuple[int, int]] = set()
        for nodes in touch_nodes_by_id.values():
            all_touch_nodes.update(nodes)

        blocking_by_node = self.blocking_nodes_by_object(state, grid)
        dynamic_blocked = set(blocking_by_node)
        safe_order, safe_distance = self.reachable_nodes(grid, start, all_touch_nodes | dynamic_blocked)
        no_dynamic_order, no_dynamic_distance = self.reachable_nodes(grid, start, all_touch_nodes)
        no_touch_order, no_touch_distance = self.reachable_nodes(grid, start, dynamic_blocked)
        safe_search_index = self.search_goal_index(grid, safe_order)
        no_dynamic_search_index = self.search_goal_index(grid, no_dynamic_order)
        no_touch_search_index = self.search_goal_index(grid, no_touch_order)

        targets = []
        for obj in interactables:
            target_id = int(obj.get("id") or 0)
            kind = trigger_kind(int(obj.get("trigger_mode") or 0))
            status = "unreachable_static"
            steps = None
            required_direction = None
            blocker = None

            if kind == "search":
                mode = int(obj.get("trigger_mode") or 0)
                key = self.search_goal_key(obj)
                goal = safe_search_index.get(mode, {}).get(key)
                if goal is not None:
                    node, direction = goal
                    status = "reachable"
                    steps = safe_distance[node]
                    required_direction = DIR_TO_BUTTON.get(direction, "down")
                elif no_dynamic_search_index.get(mode, {}).get(key) is not None:
                    route = self.route_to_object(state, target_id, avoid_transitions=True)
                    if route.get("status") == "blocked_by_interactable":
                        status = "blocked_by_interactable"
                        interaction = route.get("interaction") or {}
                        steps = (interaction.get("route") or {}).get("steps")
                        required_direction = interaction.get("required_direction")
                        blocker = route.get("blocker")
                elif no_touch_search_index.get(mode, {}).get(key) is not None:
                    status = "blocked_by_transition"
            elif kind == "touch":
                if transition_candidate(obj):
                    route = self.route_to_object(state, target_id, avoid_transitions=True)
                    status = route.get("status") or "unreachable_static"
                    steps = (route.get("route") or {}).get("steps")
                else:
                    goals = touch_nodes_by_id.get(target_id, set())
                    target_dynamic_blocked = set(dynamic_blocked)
                    target_node = grid_node(grid, int(obj.get("x") or 0), int(obj.get("y") or 0))
                    if target_node is not None:
                        target_dynamic_blocked.discard(target_node)
                    blocked = (all_touch_nodes - goals) | target_dynamic_blocked
                    touch_order, touch_distance = self.reachable_nodes(grid, start, blocked)
                    goal = next((node for node in touch_order if node in goals), None)
                    if goal is not None:
                        status = "reachable"
                        steps = touch_distance[goal]
                    else:
                        no_dynamic_blocked = all_touch_nodes - goals
                        no_dynamic_touch_order, _ = self.reachable_nodes(grid, start, no_dynamic_blocked)
                        if any(node in goals for node in no_dynamic_touch_order):
                            route = self.route_to_object(state, target_id, avoid_transitions=True)
                            if route.get("status") == "blocked_by_interactable":
                                status = "blocked_by_interactable"
                                interaction = route.get("interaction") or {}
                                steps = (interaction.get("route") or {}).get("steps")
                                required_direction = interaction.get("required_direction")
                                blocker = route.get("blocker")
                        elif any(node in no_touch_distance for node in goals):
                            status = "blocked_by_transition"

            target = {
                "id": target_id,
                "kind": kind,
                "status": status,
                "steps": steps,
                "required_direction": required_direction,
            }
            if transition_candidate(obj):
                target["transition_candidate"] = True
            if blocker is not None:
                target["blocker"] = blocker
            targets.append(target)
        return {
            "status": "ok",
            "grid": {
                "origin": {"x": grid["origin"][0], "y": grid["origin"][1]},
                "step": {"x": grid["step"][0], "y": grid["step"][1]},
                "width": grid["width"],
                "height": grid["height"],
            },
            "targets": targets,
        }

    def lightweight_navigation(self, state: dict) -> dict:
        grid = decode_nav_grid(state)
        if grid is None:
            return {"status": "no_nav_grid", "targets": []}
        targets = []
        for obj in state.get("objects") or []:
            if not obj.get("interactable"):
                continue
            targets.append(
                {
                    "id": int(obj.get("id") or 0),
                    "kind": trigger_kind(int(obj.get("trigger_mode") or 0)),
                    "status": "transition" if transition_candidate(obj) else "unknown",
                    "steps": None,
                    "required_direction": None,
                    **({"transition_candidate": True} if transition_candidate(obj) else {}),
                }
            )
        return {
            "status": "pending",
            "grid": {
                "origin": {"x": grid["origin"][0], "y": grid["origin"][1]},
                "step": {"x": grid["step"][0], "y": grid["step"][1]},
                "width": grid["width"],
                "height": grid["height"],
            },
            "targets": targets,
        }

    def file_info(self, path: Path) -> dict:
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return {"available": False}
        age = max(0.0, time.time() - mtime)
        return {
            "available": True,
            "mtime": mtime,
            "age_seconds": age,
            "stale": age > STALE_AFTER_SECONDS,
        }

    def snapshot(self) -> dict:
        state = read_json(self.state_path)
        state_info = self.file_info(self.state_path)
        frame_info = self.file_info(self.frame_path)
        if state_info.get("available"):
            self.record_state_events(state)
            self.record_dialog_event(state)
        if state_info.get("available") and "nav_grid" in state:
            state["_navigation"] = self.lightweight_navigation(state)
            state["_exploration"] = self.update_exploration(state)
        state["_debug"] = {
            "runtime_dir": str(self.runtime_dir),
            "asset_dir": str(self.asset_dir),
            "game_binary": str(self.game_binary),
            "state_file": state_info,
            "frame_file": frame_info,
            "frame_available": frame_info["available"],
            "runtime_stale": state_info.get("stale", True) or frame_info.get("stale", True),
            "stale_after_seconds": STALE_AFTER_SECONDS,
            "recent_actions": self.action_log[-20:],
            "recent_events": self.event_log[-20:],
            "recent_dialogs": self.dialog_history[-20:],
            "action_result": read_optional_json(self.action_result_path),
            "action_result_file": self.file_info(self.action_result_path),
        }
        return state

    def write_action(self, buttons: list[str], frames: int) -> dict:
        invalid = [button for button in buttons if button not in VALID_BUTTONS]
        if invalid:
            raise ValueError(f"invalid buttons: {', '.join(invalid)}")
        frames = max(1, min(int(frames), 300))

        lines = [f"frames {frames}"]
        lines.extend(f"button {button}" for button in buttons)
        tmp = self.runtime_dir / "action.txt.tmp"
        tmp.write_text("\n".join(lines) + "\n", encoding="ascii")
        os.replace(tmp, self.action_path)

        entry = {"time": time.time(), "buttons": buttons, "frames": frames}
        self.action_log.append(entry)
        self.append_navigation_diagnostic("action_written", {"action": entry})
        return entry

    def write_use_item_action(self, item_id: int, role_id: int | None = None, target: str | None = None) -> dict:
        item_id = int(item_id)
        if item_id <= 0:
            raise ValueError("item_id must be positive")

        if target == "all":
            target_value = "all"
            role_value = None
        else:
            role_value = 0 if role_id is None else int(role_id)
            if role_value < 0:
                raise ValueError("role_id must be non-negative")
            target_value = str(role_value)

        lines = [f"use_item {item_id} {target_value}"]
        tmp = self.runtime_dir / "action.txt.tmp"
        tmp.write_text("\n".join(lines) + "\n", encoding="ascii")
        os.replace(tmp, self.action_path)

        entry = {
            "time": time.time(),
            "command": "use_item",
            "item_id": item_id,
            "role_id": role_value,
            "target": "all" if target_value == "all" else "role",
        }
        self.action_log.append(entry)
        self.append_navigation_diagnostic("action_written", {"action": entry})
        return entry


class Handler(BaseHTTPRequestHandler):
    server_version = "PALPALDebug/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/state":
            self.send_json(self.server.state.snapshot())
            return
        if parsed.path == "/navigation":
            state = read_json(self.server.state.state_path)
            self.send_json(self.server.state.navigation(state))
            return
        if parsed.path == "/navigation/route":
            query = parse_qs(parsed.query)
            target = int((query.get("id") or ["0"])[0])
            state = read_json(self.server.state.state_path)
            route = self.server.state.route_to_object(state, target, avoid_transitions=True)
            if route.get("status") == "unreachable_static":
                relaxed = self.server.state.route_to_object(state, target, avoid_transitions=False)
                if relaxed.get("status") == "reachable":
                    route["status"] = "blocked_by_transition"
                    route["blocked_route"] = relaxed.get("route")
            self.send_json(route)
            return
        if parsed.path == "/exploration":
            state = read_json(self.server.state.state_path)
            self.server.state.update_exploration(state)
            self.send_json(self.server.state.current_exploration(state))
            return
        if parsed.path == "/events":
            self.send_json({"events": self.server.state.event_log[-100:]})
            return
        if parsed.path == "/dialogs":
            state = read_json(self.server.state.state_path)
            self.server.state.record_dialog_event(state)
            query = parse_qs(parsed.query)
            raw_limit = (query.get("limit") or ["100"])[0]
            try:
                limit = int(raw_limit)
            except ValueError:
                limit = 100
            speaker = (query.get("speaker") or [None])[0]
            self.send_json(self.server.state.dialogs_view(limit=limit, speaker=speaker))
            return
        if parsed.path == "/screen.bmp":
            self.send_file(self.server.state.screen_path, "image/bmp", no_cache=True)
            return
        if parsed.path == "/stream":
            self.handle_state_websocket()
            return
        if parsed.path == "/frames":
            self.handle_frame_websocket()
            return

        rel = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        path = (WEB_DIR / rel).resolve()
        if WEB_DIR not in path.parents and path != WEB_DIR:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        self.send_file(path, mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    def do_POST(self) -> None:
        if self.path == "/action":
            try:
                payload = self.read_body_json()
                buttons = payload.get("buttons", [])
                if isinstance(buttons, str):
                    buttons = [buttons]
                result = self.server.state.write_action(buttons, payload.get("frames", 1))
                self.send_json({"ok": True, "action": result})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/inventory/use":
            try:
                payload = self.read_body_json()
                result = self.server.state.write_use_item_action(
                    payload.get("item_id"),
                    payload.get("role_id"),
                    payload.get("target"),
                )
                self.send_json({"ok": True, "action": result})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/navigation/step":
            try:
                payload = self.read_body_json()
                target = int(payload.get("id") or 0)
                state = read_json(self.server.state.state_path)
                result = self.server.state.navigation_step(state, target)
                self.server.state.append_navigation_diagnostic(
                    "navigation_step",
                    {
                        "target_id": target,
                        "ok": bool(result.get("ok")),
                        "status": result.get("status"),
                        "move": result.get("move"),
                        "segment_steps": result.get("segment_steps"),
                        "remaining_steps": result.get("remaining_steps"),
                        "required_direction": result.get("required_direction"),
                        "blocker_id": (result.get("blocker") or {}).get("id"),
                    },
                    state,
                )
                status = HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT
                self.send_json(result, status)
            except Exception as exc:
                self.server.state.append_navigation_diagnostic("navigation_step_error", {"error": str(exc)})
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/diagnostics/navigation":
            try:
                payload = self.read_body_json()
                state = read_json(self.server.state.state_path)
                event_type = str(payload.get("type") or "client_event")
                details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
                self.server.state.append_navigation_diagnostic(
                    f"client_{event_type}",
                    {
                        "details": details,
                        "client_time": payload.get("time"),
                    },
                    state,
                )
                self.send_json({"ok": True})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/exploration/step":
            try:
                payload = self.read_body_json()
                state = read_json(self.server.state.state_path)
                result = self.server.state.exploration_step(state, payload.get("max_steps", DEFAULT_EXPLORE_STEPS))
                status = HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT
                self.send_json(result, status)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/exploration/reset":
            self.send_json(self.server.state.reset_exploration())
            return

        if self.path in {"/reset", "/save", "/load"}:
            self.send_json(
                {
                    "ok": False,
                    "error": f"{self.path} is reserved but not implemented by the file bridge yet",
                },
                HTTPStatus.NOT_IMPLEMENTED,
            )
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def read_body_json(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(size) if size else b"{}"
        return json.loads(raw.decode("utf-8"))

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_file(self, path: Path, content_type: str, no_cache: bool = False) -> None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        if no_cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def handle_websocket_upgrade(self) -> bool:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(HTTPStatus.BAD_REQUEST, "missing Sec-WebSocket-Key")
            return False
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        return True

    def handle_state_websocket(self) -> None:
        if not self.handle_websocket_upgrade():
            return
        last = None
        self.connection.settimeout(1.0)
        while True:
            payload = json.dumps(self.server.state.snapshot(), ensure_ascii=False).encode("utf-8")
            if payload != last:
                try:
                    self.connection.sendall(websocket_frame(payload, opcode=0x1))
                except (BrokenPipeError, ConnectionResetError, socket.timeout):
                    return
                last = payload
            time.sleep(0.1)

    def handle_frame_websocket(self) -> None:
        if not self.handle_websocket_upgrade():
            return
        last_signature = None
        self.connection.settimeout(1.0)
        while True:
            try:
                stat = self.server.state.frame_path.stat()
            except FileNotFoundError:
                time.sleep(0.05)
                continue

            signature = (stat.st_mtime_ns, stat.st_size)
            if signature != last_signature:
                try:
                    payload = self.server.state.frame_path.read_bytes()
                    self.connection.sendall(websocket_frame(payload, opcode=0x2))
                except (FileNotFoundError, BrokenPipeError, ConnectionResetError, socket.timeout):
                    return
                last_signature = signature
            time.sleep(0.02)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")


class Server(ThreadingHTTPServer):
    state: DebugState


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--runtime-dir", default="runtime")
    parser.add_argument("--asset-dir", default="data/pal")
    parser.add_argument("--game-binary", default="vendor/sdlpal/unix/sdlpal")
    args = parser.parse_args()

    server = Server((args.host, args.port), Handler)
    server.state = DebugState(
        Path(args.runtime_dir).resolve(),
        Path(args.asset_dir).resolve(),
        Path(args.game_binary).resolve(),
    )
    print(f"debug console: http://{args.host}:{args.port}")
    print(f"runtime dir: {server.state.runtime_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
