from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import mimetypes
import os
import re
import select
import shutil
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections import OrderedDict, deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from palpal.agent_runtime import AgentRuntime
from palpal.observations import ObservationCoordinator

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
EXPLORATION_VERSION = 11
LOGICAL_ROOM_ID_RE = re.compile(r"^S\d+/M\d+:[0-9A-F]{12}$")
MAX_OBJECT_INTERACTION_HISTORY = 20
MAX_TASK_HISTORY = 20
MAX_ACTIVE_TASK_CONTEXT = 8
MAX_ACTIVE_TASK_DESCRIPTION = 160
TRANSITION_OBSERVATION_TIMEOUT_SECONDS = 4.0
GAME_CONTROL_TIMEOUT_SECONDS = 30.0
TRANSITION_STABLE_SNAPSHOTS = 2
MAX_NAVIGATION_SEGMENT_STEPS = 8
VIEWPORT_WIDTH = 320
VIEWPORT_HEIGHT = 200
OPENING_LI_XIAOYAO_MONOLOGUE_MESSAGES = {1956, 1957, 1958, 1962, 1963, 1964}
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
ALLOWED_DEEPSEEK_MODELS = frozenset({"deepseek-v4-pro", "deepseek-v4-flash"})
DEFAULT_DEEPSEEK_TIMEOUT = 90.0
MAX_AGENT_TOOL_ROUNDS = 8
MAX_AGENT_TOOL_CALLS = 40
MAX_AGENT_DECISION_REPAIRS = 1
DEFAULT_DIALOG_ADVANCE_DELAY_MS = 500
DEFAULT_DIALOG_ADVANCE_MAX_PRESSES = 100
DIALOG_ADVANCE_BUTTON = "search"
DIALOG_ADVANCE_ACTION_TIMEOUT = 2.5
DIALOG_ADVANCE_INACTIVE_CONFIRM_POLLS = 3
DIALOG_ADVANCE_INACTIVE_CONFIRM_INTERVAL = 0.25
DIALOG_ADVANCE_VISIBILITY_POLL_INTERVAL = 0.1
DIALOG_ADVANCE_VISIBILITY_TIMEOUT = 2.0
CHECKPOINT_MANIFEST_VERSION = 2
DEFAULT_CHECKPOINT_SLOT = 99
LIVE_MAGIC = b"PLIV"
LIVE_VERSION = 1
LIVE_HEADER = struct.Struct("<4sHHIIQQ")
LIVE_MAX_PAYLOAD = 2 * 1024 * 1024
LIVE_MESSAGE_HELLO = 1
LIVE_MESSAGE_STATE = 2
LIVE_MESSAGE_FRAME = 3
LIVE_MESSAGE_HEARTBEAT = 4
LIVE_MESSAGE_FRAME_REPEAT = 5
LIVE_PNG_CACHE_SIZE = 16
NAVIGATION_GRAPH_CACHE_SIZE = 16
STATE_STREAM_SNAPSHOT_INTERVAL_SECONDS = 0.5


def is_logical_room_id(value: object) -> bool:
    return isinstance(value, str) and LOGICAL_ROOM_ID_RE.fullmatch(value) is not None
CHECKPOINT_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
OBJECT_OBSERVATION_FIELDS = (
    "state",
    "interactable",
    "blocking",
    "vanish_time",
    "sprite_id",
    "trigger_mode",
    "trigger_script",
    "auto_script",
)
OBJECT_INTERACTION_COMPATIBILITY_FIELDS = (
    "state",
    "interactable",
    "blocking",
    "vanish_time",
    "sprite_id",
    "trigger_mode",
    "trigger_script",
)
TRANSITION_KEY_FIELDS = (
    "scene_id",
    "map_id",
    "x",
    "y",
    "trigger_mode",
    "trigger_script",
)
CHECKPOINT_COMPANION_FILES = (
    Path("dialog_history.json"),
    Path("exploration.json"),
    Path("events.json"),
    Path("agent_log.jsonl"),
    Path("agent_llm_log.jsonl"),
    Path("logs") / "navigation.log",
)
CHECKPOINT_COMPANION_RESET_FILES = frozenset(CHECKPOINT_COMPANION_FILES)
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
        return {"status": "waiting_for_game", "message": "JSON file not available yet"}
    except json.JSONDecodeError as exc:
        return {"status": "state_parse_error", "error": str(exc)}


def read_optional_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def read_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    values = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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


def facing_event_object_hits(
    position: tuple[int, int],
    direction: int,
    obj: dict,
    distance_limit: int,
) -> bool:
    """Mirror script opcode 0x81's facing-object distance predicate."""
    if int(obj.get("state") or 0) <= 0 or distance_limit <= 0:
        return False
    anchor_x = int(obj.get("x") or 0) + (16 if direction in {1, 0} else -16)
    anchor_y = int(obj.get("y") or 0) + (8 if direction in {1, 2} else -8)
    return pal_distance(position, (anchor_x, anchor_y)) < distance_limit


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


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def rgba_to_png(width: int, height: int, rgba: bytes | memoryview) -> bytes:
    if width <= 0 or height <= 0:
        raise ValueError("invalid frame dimensions")
    row_size = width * 4
    if len(rgba) != row_size * height:
        raise ValueError("invalid RGBA payload size")

    scanlines = bytearray((row_size + 1) * height)
    for y in range(height):
        source_start = y * row_size
        target_start = y * (row_size + 1)
        scanlines[target_start] = 0
        scanlines[target_start + 1 : target_start + 1 + row_size] = rgba[source_start : source_start + row_size]

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    compressed = zlib.compress(bytes(scanlines))
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", compressed)
        + png_chunk(b"IEND", b"")
    )


def indexed_to_png(
    width: int,
    height: int,
    palette_rgba: bytes | memoryview,
    pixels: bytes | memoryview,
) -> bytes:
    if width <= 0 or height <= 0:
        raise ValueError("invalid frame dimensions")
    if len(palette_rgba) == 0 or len(palette_rgba) > 256 * 4 or len(palette_rgba) % 4:
        raise ValueError("invalid indexed palette")
    if len(pixels) != width * height:
        raise ValueError("invalid indexed payload size")

    palette_count = len(palette_rgba) // 4
    scanlines = bytearray((width + 1) * height)
    for y in range(height):
        source_start = y * width
        target_start = y * (width + 1)
        scanlines[target_start + 1 : target_start + 1 + width] = pixels[source_start : source_start + width]

    palette = bytearray(palette_count * 3)
    alpha = bytearray(palette_count)
    has_alpha = False
    for index in range(palette_count):
        palette[index * 3 : index * 3 + 3] = palette_rgba[index * 4 : index * 4 + 3]
        alpha[index] = palette_rgba[index * 4 + 3]
        has_alpha = has_alpha or alpha[index] != 0xFF

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"PLTE", bytes(palette))
        + (png_chunk(b"tRNS", bytes(alpha)) if has_alpha else b"")
        + png_chunk(b"IDAT", zlib.compress(bytes(scanlines)))
        + png_chunk(b"IEND", b"")
    )


def pfrm_layout(raw: bytes) -> tuple[int, int, int, memoryview, memoryview | None]:
    if len(raw) < 24 or raw[:4] != b"PFRM":
        raise ValueError("invalid frame header")
    version = struct.unpack_from("<H", raw, 4)[0]
    width = struct.unpack_from("<H", raw, 6)[0]
    height = struct.unpack_from("<H", raw, 8)[0]
    pixel_format = struct.unpack_from("<H", raw, 10)[0]
    payload_length = struct.unpack_from("<I", raw, 20)[0]
    if version == 1 and pixel_format == 1:
        if payload_length != width * height * 4:
            raise ValueError("invalid frame payload length")
        if len(raw) < 24 + payload_length:
            raise ValueError("truncated frame payload")
        payload = memoryview(raw)[24 : 24 + payload_length]
        return width, height, pixel_format, payload, None
    if version == 2 and pixel_format == 2:
        palette_count = struct.unpack_from("<I", raw, 12)[0]
        palette_size = palette_count * 4
        if palette_count <= 0 or palette_count > 256 or payload_length != palette_size + width * height:
            raise ValueError("invalid frame payload length")
        if len(raw) < 24 + payload_length:
            raise ValueError("truncated frame payload")
        payload = memoryview(raw)[24 : 24 + payload_length]
        return width, height, pixel_format, payload[palette_size:], payload[:palette_size]
    raise ValueError("unsupported frame format")


def pfrm_to_png(raw: bytes) -> bytes:
    width, height, pixel_format, pixels, palette = pfrm_layout(raw)
    if pixel_format == 1:
        return rgba_to_png(width, height, pixels)
    if palette is None:
        raise ValueError("invalid indexed palette")
    return indexed_to_png(width, height, palette, pixels)


def pfrm_png_cache_key(raw: bytes) -> bytes:
    digest = hashlib.sha256()
    if len(raw) >= 24 and raw[:4] == b"PFRM":
        digest.update(raw[:16])
        digest.update(raw[20:])
    else:
        digest.update(raw)
    return digest.digest()


class LiveBridge:
    def __init__(self, state: "DebugState", path: Path):
        self.state = state
        self.path = path
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.listener: socket.socket | None = None
        self.owns_path = False

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self.run, name="palpal-live-bridge", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        listener = self.listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        if self.owns_path:
            self.path.unlink(missing_ok=True)
            self.owns_path = False

    def prepare_path(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.1)
        try:
            probe.connect(str(self.path))
        except OSError:
            self.path.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"live socket is already in use: {self.path}")
        finally:
            probe.close()

    def run(self) -> None:
        try:
            self.prepare_path()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener = listener
            listener.bind(str(self.path))
            self.owns_path = True
            listener.listen(1)
            listener.settimeout(0.5)
            self.state.set_live_listener_status(True)
            while not self.stop_event.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                self.handle_connection(connection)
        except Exception as exc:
            self.state.set_live_listener_status(False, str(exc))
        finally:
            self.state.set_live_connected(False)
            listener = self.listener
            self.listener = None
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass
            if self.owns_path:
                self.path.unlink(missing_ok=True)
                self.owns_path = False

    def recv_exact(self, connection: socket.socket, size: int) -> bytes | None:
        data = bytearray()
        while len(data) < size and not self.stop_event.is_set():
            try:
                chunk = connection.recv(size - len(data))
            except socket.timeout:
                continue
            if not chunk:
                return None
            data.extend(chunk)
        return bytes(data) if len(data) == size else None

    def handle_connection(self, connection: socket.socket) -> None:
        connection.settimeout(0.5)
        self.state.set_live_connected(True)
        try:
            while not self.stop_event.is_set():
                header = self.recv_exact(connection, LIVE_HEADER.size)
                if header is None:
                    return
                magic, version, message_type, flags, payload_length, sequence, monotonic_ms = LIVE_HEADER.unpack(header)
                if magic != LIVE_MAGIC or version != LIVE_VERSION:
                    self.state.record_live_protocol_error("invalid_header")
                    return
                if flags != 0 or payload_length > LIVE_MAX_PAYLOAD:
                    self.state.record_live_protocol_error("invalid_payload_length")
                    return
                payload = self.recv_exact(connection, payload_length)
                if payload is None:
                    return
                self.state.ingest_live_message(message_type, sequence, monotonic_ms, payload)
        finally:
            self.state.set_live_connected(False)
            try:
                connection.close()
            except OSError:
                pass


class DebugState:
    def __init__(self, runtime_dir: Path, asset_dir: Path, game_binary: Path):
        self.runtime_dir = runtime_dir
        self.asset_dir = asset_dir
        self.game_binary = game_binary
        self.env_file = read_env_file(ROOT / ".env")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.action_log: list[dict] = []
        self.event_log: list[dict] = self.read_events()
        if self.events_path.exists():
            self.write_events()
        self.dialog_history: list[dict] = self.read_dialog_history()
        self.dialog_source_keys: set[str] = self.dialog_history_source_keys(self.dialog_history)
        self.last_observed_state: dict | None = None
        self.last_stable_room_state: dict | None = None
        self.pending_transition_observation: dict | None = None
        self.pending_interaction_observation: dict | None = None
        self.observations = ObservationCoordinator()
        self.navigation_transition_requests: dict[int, str] = {}
        self.dialog_advance_observation_id: str | None = None
        self.last_snapshot_frame: int | None = None
        self.logical_frame_offset = 0
        self.last_raw_frame: int | None = None
        self.last_logical_frame: int | None = None
        self.pending_checkpoint_frame_reset: dict | None = None
        self.state_lock = threading.RLock()
        self.live_lock = threading.RLock()
        self.live_condition = threading.Condition(self.live_lock)
        self.live_state: dict | None = None
        self.live_state_received_at: float | None = None
        self.live_state_sequence = 0
        self.live_frame: bytes | None = None
        self.live_frame_received_at: float | None = None
        self.live_frame_sequence = 0
        self.live_png: bytes | None = None
        self.live_png_frame: bytes | None = None
        self.live_png_sequence = 0
        self.live_png_cache: dict[bytes, bytes] = {}
        self.navigation_graph_lock = threading.RLock()
        self.navigation_graph_cache: OrderedDict[
            tuple[tuple[int, int], tuple[int, int], int, int, str],
            dict[tuple[int, int], tuple[tuple[str, tuple[int, int]], ...]],
        ] = OrderedDict()
        self.live_game_instance_id: str | None = None
        self.live_bridge: LiveBridge | None = None
        self.state_stream_snapshot_lock = threading.Lock()
        self.state_stream_snapshot_at = 0.0
        self.state_stream_snapshot_payload: bytes | None = None
        self.state_stream_snapshot_action_pending: bool | None = None
        self.live_status = {
            "listening": False,
            "connected": False,
            "listener_error": None,
            "connections": 0,
            "protocol_errors": 0,
            "last_protocol_error": None,
            "last_message_at": None,
            "last_sequence": 0,
            "state_messages": 0,
            "frame_messages": 0,
            "heartbeats": 0,
        }
        self.action_lock = threading.RLock()
        self.pending_written_action: dict | None = None
        self.exploration_lock = threading.Lock()
        self.dialog_advance_lock = threading.Lock()
        self.dialog_advance_cancel = threading.Event()
        self.dialog_advance_status_lock = threading.Lock()
        self.dialog_advance_status = self.empty_dialog_advance_status()
        self.agent_status_lock = threading.Lock()
        self.agent_status = self.empty_agent_status()
        self.recover_object_interactions_from_agent_llm_log()
        self.agent_runtime = AgentRuntime(self, self.runtime_dir)

    def config_value(self, key: str, default: str | None = None) -> str | None:
        return os.environ.get(key) or self.env_file.get(key) or default

    @property
    def live_socket_path(self) -> Path:
        configured = self.config_value("PALPAL_AGENT_SOCKET")
        return Path(configured).resolve() if configured else self.runtime_dir / "live.sock"

    def start_live_bridge(self) -> None:
        if self.live_bridge is not None:
            return
        self.live_bridge = LiveBridge(self, self.live_socket_path)
        self.live_bridge.start()

    def stop_live_bridge(self) -> None:
        bridge = self.live_bridge
        self.live_bridge = None
        if bridge is not None:
            bridge.stop()

    def set_live_listener_status(self, listening: bool, error: str | None = None) -> None:
        with self.live_condition:
            self.live_status["listening"] = listening
            self.live_status["listener_error"] = error
            self.live_condition.notify_all()

    def set_live_connected(self, connected: bool) -> None:
        with self.live_condition:
            if connected and not self.live_status["connected"]:
                self.live_status["connections"] += 1
            self.live_status["connected"] = connected
            self.live_condition.notify_all()

    def record_live_protocol_error(self, reason: str) -> None:
        with self.live_condition:
            self.live_status["protocol_errors"] += 1
            self.live_status["last_protocol_error"] = reason
            self.live_condition.notify_all()

    def ingest_live_message(
        self,
        message_type: int,
        sequence: int,
        monotonic_ms: int,
        payload: bytes,
    ) -> None:
        now = time.time()
        if message_type == LIVE_MESSAGE_HELLO:
            try:
                hello = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.record_live_protocol_error("invalid_hello")
                return
            game_instance_id = str(hello.get("game_instance_id") or "")
            with self.live_condition:
                if game_instance_id and game_instance_id != self.live_game_instance_id:
                    self.live_state = None
                    self.live_state_received_at = None
                    self.live_state_sequence = 0
                    self.live_frame = None
                    self.live_frame_received_at = None
                    self.live_frame_sequence = 0
                    self.live_png = None
                    self.live_png_frame = None
                    self.live_png_sequence = 0
                    self.live_png_cache.clear()
                self.live_game_instance_id = game_instance_id or None
                self.live_status["last_message_at"] = now
                self.live_status["last_sequence"] = sequence
                self.live_condition.notify_all()
            return
        if message_type == LIVE_MESSAGE_STATE:
            try:
                state = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.record_live_protocol_error("invalid_state_json")
                return
            if not isinstance(state, dict):
                self.record_live_protocol_error("invalid_state_type")
                return
            self.set_live_state(state, sequence=sequence, received_at=now)
            return
        if message_type == LIVE_MESSAGE_FRAME:
            try:
                pfrm_layout(payload)
            except ValueError:
                self.record_live_protocol_error("invalid_frame")
                return
            self.set_live_frame(payload, sequence=sequence, received_at=now)
            return
        if message_type == LIVE_MESSAGE_FRAME_REPEAT:
            if payload:
                self.record_live_protocol_error("invalid_frame_repeat")
                return
            with self.live_lock:
                frame = self.live_frame
            if frame is None:
                self.record_live_protocol_error("frame_repeat_without_frame")
                return
            self.set_live_frame(frame, sequence=sequence, received_at=now)
            return
        if message_type == LIVE_MESSAGE_HEARTBEAT:
            with self.live_condition:
                self.live_status["heartbeats"] += 1
                self.live_status["last_message_at"] = now
                self.live_status["last_sequence"] = sequence
                self.live_condition.notify_all()
            return
        self.record_live_protocol_error("unknown_message_type")

    def set_live_state(
        self,
        state: dict,
        *,
        sequence: int | None = None,
        received_at: float | None = None,
    ) -> None:
        with self.live_condition:
            next_sequence = sequence if sequence is not None else self.live_state_sequence + 1
            self.live_state = copy.deepcopy(state)
            self.live_state_received_at = received_at if received_at is not None else time.time()
            self.live_state_sequence = int(next_sequence)
            self.live_status["state_messages"] += 1
            self.live_status["last_message_at"] = self.live_state_received_at
            self.live_status["last_sequence"] = int(next_sequence)
            self.live_condition.notify_all()
        self.reconcile_pending_written_action(state)

    def set_live_frame(
        self,
        frame: bytes,
        *,
        sequence: int | None = None,
        received_at: float | None = None,
    ) -> None:
        frame = bytes(frame)
        png_cache_key = pfrm_png_cache_key(frame)
        with self.live_condition:
            next_sequence = sequence if sequence is not None else self.live_frame_sequence + 1
            self.live_frame = frame
            self.live_frame_received_at = received_at if received_at is not None else time.time()
            self.live_frame_sequence = int(next_sequence)
            cached_png = self.live_png_cache.get(png_cache_key)
            if cached_png is not None:
                self.live_png_cache.pop(png_cache_key)
                self.live_png_cache[png_cache_key] = cached_png
                self.live_png = cached_png
                self.live_png_frame = png_cache_key
                self.live_png_sequence = int(next_sequence)
            else:
                self.live_png = None
                self.live_png_frame = None
                self.live_png_sequence = 0
            self.live_status["frame_messages"] += 1
            self.live_status["last_message_at"] = self.live_frame_received_at
            self.live_status["last_sequence"] = int(next_sequence)
            self.live_condition.notify_all()

    def read_live_state(self) -> dict:
        with self.live_lock:
            if self.live_state is None:
                return {
                    "status": "waiting_for_game",
                    "message": "no live state received from the game",
                }
            return copy.deepcopy(self.live_state)

    @staticmethod
    def native_action_status(state: dict | None) -> dict | None:
        action = state.get("agent_action") if isinstance(state, dict) else None
        if not isinstance(action, dict):
            return None
        try:
            generation = int(action.get("generation"))
            remaining_steps = max(0, int(action.get("remaining_steps") or 0))
        except (TypeError, ValueError):
            return None
        return {
            "generation": generation,
            "pending": bool(action.get("pending")),
            "remaining_steps": remaining_steps,
            "waiting_for_move": bool(action.get("waiting_for_move")),
        }

    def reconcile_pending_written_action(self, state: dict) -> None:
        with self.action_lock:
            pending = self.pending_written_action
            if not isinstance(pending, dict):
                return
            instance_id = str(state.get("game_instance_id") or "")
            if instance_id != str(pending.get("game_instance_id") or ""):
                self.pending_written_action = None
                return
            native = self.native_action_status(state)
            if native is not None and native["generation"] != pending.get("generation_before"):
                self.pending_written_action = None

    def action_pending(self, state: dict | None = None) -> bool:
        if state is None:
            state = self.read_live_state()
        native = self.native_action_status(state)
        with self.action_lock:
            local_pending = isinstance(self.pending_written_action, dict)
        return self.action_path.exists() or local_pending or bool(native and native["pending"])

    def control_state(self, state: dict, state_info: dict | None = None) -> dict:
        """Derive the client/game control contract from the compatible native fields."""
        has_transport_info = isinstance(state_info, dict)
        state_info = state_info if has_transport_info else {}
        trigger = self.native_trigger_generation(state)
        trigger_details = {}
        if trigger is not None:
            trigger_details = {
                "trigger_generation": int(trigger.get("generation") or 0),
                "event_object_id": int(trigger.get("event_object_id") or 0),
            }

        unavailable = (
            (has_transport_info and (
                not state_info.get("available")
                or state_info.get("stale", True)
            ))
            or state.get("status") in {"waiting_for_game", "state_parse_error", "startup_waiting"}
            or state.get("in_main_game") is False
        )
        dialog_active = bool((state.get("dialog") or {}).get("active"))
        choice_active = bool((state.get("choice") or {}).get("active"))
        shop_active = bool((state.get("shop") or {}).get("active"))
        if unavailable:
            owner, phase, allowed = "game", "unavailable", []
        elif state.get("in_battle"):
            battle = state.get("battle") if isinstance(state.get("battle"), dict) else None
            if battle is None:
                owner, phase, allowed = "game", "battle", []
            elif self.action_pending(state):
                owner, phase, allowed = "game", "battle", []
            elif battle.get("phase") in {"command", "reward"}:
                owner, phase, allowed = "client", "battle", ["battle_action", "press_buttons"]
            else:
                owner, phase, allowed = "game", "battle", []
        elif state.get("entering_scene"):
            owner, phase, allowed = "game", "entering_scene", []
        elif self.action_pending(state):
            owner, phase, allowed = "game", "action_pending", []
        elif choice_active:
            owner, phase, allowed = "client", "choice", ["choice_select", "press_buttons"]
        elif dialog_active:
            owner, phase, allowed = "client", "dialog", ["advance_dialog"]
        elif shop_active:
            owner, phase, allowed = "client", "shop", ["shop_buy", "shop_leave"]
        elif trigger is not None and trigger.get("completed") is not True:
            owner, phase, allowed = "game", "game_script", []
        else:
            owner, phase, allowed = (
                "client",
                "ready",
                ["press_buttons", "navigation_step", "use_item", "equip_item"],
            )
        control = {
            "owner": owner,
            "phase": phase,
            "allowed_actions": allowed,
            "navigation_ready": phase == "ready",
        }
        if state.get("in_battle"):
            control["battle_state_available"] = isinstance(state.get("battle"), dict)
        if phase == "game_script":
            control.update(trigger_details)
        return control

    @staticmethod
    def game_control_progress_signature(state: dict) -> tuple:
        """Return meaningful control progress without treating frame ticks as progress."""
        trigger = state.get("trigger_observation") if isinstance(state.get("trigger_observation"), dict) else {}
        destination = trigger.get("destination") if isinstance(trigger.get("destination"), dict) else {}
        action = state.get("agent_action") if isinstance(state.get("agent_action"), dict) else {}
        dialog = state.get("dialog") if isinstance(state.get("dialog"), dict) else {}
        dialog_event = dialog.get("latest_event") if isinstance(dialog.get("latest_event"), dict) else {}
        battle = state.get("battle") if isinstance(state.get("battle"), dict) else {}
        shop = state.get("shop") if isinstance(state.get("shop"), dict) else {}
        position = state.get("party_position") if isinstance(state.get("party_position"), dict) else {}
        return (
            str(state.get("game_instance_id") or ""),
            int(trigger.get("generation") or 0),
            int(trigger.get("event_object_id") or 0),
            bool(trigger.get("completed")),
            destination.get("scene_id"),
            destination.get("map_id"),
            destination.get("x"),
            destination.get("y"),
            int(action.get("generation") or 0),
            bool(action.get("pending")),
            int(action.get("remaining_steps") or 0),
            bool(action.get("waiting_for_move")),
            state.get("scene_id"),
            state.get("map_id"),
            bool(state.get("entering_scene")),
            position.get("x"),
            position.get("y"),
            bool(dialog.get("active")),
            dialog_event.get("sequence"),
            dialog_event.get("message_id"),
            bool(state.get("in_battle")),
            battle.get("generation"),
            battle.get("command_generation"),
            battle.get("phase"),
            bool(shop.get("active")),
        )

    def action_control_error(
        self,
        state: dict,
        action: str,
        state_info: dict | None = None,
    ) -> str | None:
        control = self.control_state(state, state_info)
        if state.get("in_battle") and control.get("battle_state_available") is False:
            return "battle_state_unavailable"
        if action in control["allowed_actions"]:
            return None
        phase = str(control["phase"])
        return {
            "unavailable": "runtime_stale",
            "battle": "in_battle",
            "battle_state_unavailable": "battle_state_unavailable",
            "entering_scene": "entering_scene",
            "action_pending": "action_pending",
            "dialog": "in_dialog",
            "choice": "in_choice",
            "shop": "in_shop",
            "game_script": "game_controlled",
        }.get(phase, "action_not_allowed")

    def action_handshake(self, state: dict | None = None) -> dict:
        state = state if isinstance(state, dict) else self.read_live_state()
        native = self.native_action_status(state)
        return {
            "supported": native is not None,
            "game_instance_id": str(state.get("game_instance_id") or ""),
            "generation": native["generation"] if native is not None else None,
        }

    def begin_written_action(self, entry: dict) -> None:
        handshake = self.action_handshake()
        entry.update(
            {
                "game_instance_id": handshake["game_instance_id"],
                "action_generation_before": handshake["generation"],
                "action_handshake_supported": handshake["supported"],
            }
        )
        self.pending_written_action = (
            {
                "game_instance_id": handshake["game_instance_id"],
                "generation_before": handshake["generation"],
                "written_at": entry["time"],
            }
            if handshake["supported"]
            else None
        )
        if not handshake["supported"]:
            self.append_navigation_diagnostic(
                "action_handshake_compatibility_mode",
                {"game_instance_id": handshake["game_instance_id"]},
            )

    def live_info(self, kind: str) -> dict:
        with self.live_lock:
            if kind == "state":
                received_at = self.live_state_received_at
                sequence = self.live_state_sequence
                available = self.live_state is not None
            elif kind == "frame":
                received_at = self.live_frame_received_at
                sequence = self.live_frame_sequence
                available = self.live_frame is not None
            else:
                raise ValueError(f"unknown live info kind: {kind}")
        if not available or received_at is None:
            return {"available": False, "sequence": sequence}
        age = max(0.0, time.time() - received_at)
        return {
            "available": True,
            "mtime": received_at,
            "received_at": received_at,
            "sequence": sequence,
            "age_seconds": age,
            "stale": age > STALE_AFTER_SECONDS,
        }

    def live_transport_snapshot(self) -> dict:
        with self.live_lock:
            status = dict(self.live_status)
            status.update(
                {
                    "socket_path": str(self.live_socket_path),
                    "game_instance_id": self.live_game_instance_id,
                    "state_sequence": self.live_state_sequence,
                    "frame_sequence": self.live_frame_sequence,
                }
            )
            return status

    def latest_frame_png(self) -> tuple[int, bytes] | None:
        with self.live_lock:
            if self.live_frame is None:
                return None
            sequence = self.live_frame_sequence
            frame = self.live_frame
            png_cache_key = pfrm_png_cache_key(frame)
            cached_png = self.live_png_cache.get(png_cache_key)
            if cached_png is not None:
                self.live_png_cache.pop(png_cache_key)
                self.live_png_cache[png_cache_key] = cached_png
                self.live_png = cached_png
                self.live_png_frame = png_cache_key
                self.live_png_sequence = sequence
                return sequence, cached_png
        png = pfrm_to_png(frame)
        with self.live_lock:
            self.live_png_cache[png_cache_key] = png
            while len(self.live_png_cache) > LIVE_PNG_CACHE_SIZE:
                self.live_png_cache.pop(next(iter(self.live_png_cache)))
            if self.live_frame is not None and pfrm_png_cache_key(self.live_frame) == png_cache_key:
                self.live_png = png
                self.live_png_frame = png_cache_key
                sequence = self.live_frame_sequence
                self.live_png_sequence = sequence
            return sequence, png

    def wait_for_frame(self, after_sequence: int, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        with self.live_condition:
            while self.live_frame_sequence <= after_sequence:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.live_condition.wait(remaining)
            return self.live_frame_sequence

    def atomic_write_text(self, path: Path, text: str, *, encoding: str = "utf-8") -> None:
        tmp = path.with_name(
            f"{path.name}.{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}.tmp"
        )
        try:
            tmp.write_text(text, encoding=encoding)
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def empty_agent_status(self) -> dict:
        return {
            "running": False,
            "backend": None,
            "model": None,
            "phase": "idle",
            "round": 0,
            "tool_calls": 0,
            "current_tool": None,
            "message": "No agent request is running.",
            "started_at": None,
            "updated_at": None,
            "error": None,
        }

    def agent_status_snapshot(self) -> dict:
        with self.agent_status_lock:
            return dict(self.agent_status)

    def empty_dialog_advance_status(self) -> dict:
        return {
            "running": False,
            "status": "idle",
            "presses": 0,
            "max_presses": DEFAULT_DIALOG_ADVANCE_MAX_PRESSES,
            "delay_ms": DEFAULT_DIALOG_ADVANCE_DELAY_MS,
            "stop_on_repeat": True,
            "started_at": None,
            "updated_at": None,
        }

    def dialog_advance_status_snapshot(self) -> dict:
        with self.dialog_advance_status_lock:
            return dict(self.dialog_advance_status)

    def begin_dialog_advance_status(self, options: dict) -> dict:
        now = time.time()
        status = self.empty_dialog_advance_status()
        status.update(
            {
                **options,
                "running": True,
                "status": "running",
                "presses": 0,
                "started_at": now,
                "updated_at": now,
            }
        )
        with self.dialog_advance_status_lock:
            self.dialog_advance_status = status
            return dict(self.dialog_advance_status)

    def update_dialog_advance_status(self, **updates) -> dict:
        with self.dialog_advance_status_lock:
            status = dict(self.dialog_advance_status)
            status.update(updates)
            status["updated_at"] = time.time()
            self.dialog_advance_status = status
            return dict(self.dialog_advance_status)

    def finish_dialog_advance_status(self, final_status: str, presses: int, options: dict) -> dict:
        with self.dialog_advance_status_lock:
            status = dict(self.dialog_advance_status)
            status.update(
                {
                    **options,
                    "running": False,
                    "status": final_status,
                    "presses": presses,
                    "updated_at": time.time(),
                }
            )
            if not status.get("started_at"):
                status["started_at"] = status["updated_at"]
            self.dialog_advance_status = status
            return dict(self.dialog_advance_status)

    def begin_agent_status(self, backend: str, model: str | None = None) -> dict:
        now = time.time()
        status = self.empty_agent_status()
        status.update(
            {
                "running": True,
                "backend": backend,
                "model": model,
                "phase": "preparing",
                "message": "Preparing agent request.",
                "started_at": now,
                "updated_at": now,
            }
        )
        with self.agent_status_lock:
            self.agent_status = status
            return dict(self.agent_status)

    def update_agent_status(self, **updates) -> dict:
        with self.agent_status_lock:
            status = dict(self.agent_status)
            status.update(updates)
            status["updated_at"] = time.time()
            self.agent_status = status
            return dict(self.agent_status)

    def finish_agent_status(self, error: str | None = None) -> dict:
        now = time.time()
        with self.agent_status_lock:
            status = dict(self.agent_status)
            status.update(
                {
                    "running": False,
                    "phase": "error" if error else "completed",
                    "current_tool": None,
                    "message": error or "Agent decision ready. No actions were executed.",
                    "updated_at": now,
                    "error": error,
                }
            )
            if not status.get("started_at"):
                status["started_at"] = now
            self.agent_status = status
            return dict(self.agent_status)

    @property
    def checkpoints_path(self) -> Path:
        return self.runtime_dir / "checkpoints"

    @property
    def checkpoint_slot(self) -> int:
        raw = self.config_value("PALPAL_CHECKPOINT_SLOT", str(DEFAULT_CHECKPOINT_SLOT))
        try:
            slot = int(raw or DEFAULT_CHECKPOINT_SLOT)
        except ValueError:
            slot = DEFAULT_CHECKPOINT_SLOT
        return max(0, min(slot, 999))

    def save_slot_path(self, slot: int | None = None) -> Path:
        return self.asset_dir / f"{self.checkpoint_slot if slot is None else int(slot)}.rpg"

    @property
    def screen_path(self) -> Path:
        return self.runtime_dir / "screen.bmp"

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

    @property
    def agent_log_path(self) -> Path:
        return self.runtime_dir / "agent_log.jsonl"

    @property
    def agent_llm_log_path(self) -> Path:
        return self.runtime_dir / "agent_llm_log.jsonl"

    def companion_live_path(self, relative_path: Path) -> Path:
        return self.runtime_dir / relative_path

    def companion_checkpoint_path(self, checkpoint_dir: Path, relative_path: Path) -> Path:
        return checkpoint_dir / "companion" / relative_path

    def checkpoint_id(self, value: str | None = None, generate: bool = True) -> str:
        if value:
            checkpoint_id = CHECKPOINT_ID_RE.sub("-", value.strip()).strip(".-")
            if checkpoint_id:
                return checkpoint_id[:80]
        if not generate:
            raise ValueError("checkpoint id is required")
        return time.strftime("%Y%m%d-%H%M%S", time.localtime()) + f"-{int((time.time() % 1) * 1000):03d}"

    def checkpoint_dir(self, checkpoint_id: str) -> Path:
        checkpoint_id = self.checkpoint_id(checkpoint_id, generate=False)
        path = (self.checkpoints_path / checkpoint_id).resolve()
        checkpoints_root = self.checkpoints_path.resolve()
        if checkpoints_root != path and checkpoints_root not in path.parents:
            raise ValueError("invalid checkpoint id")
        return path

    def checkpoint_file_status(self, checkpoint_dir: Path) -> dict:
        files: dict[str, dict] = {}
        game_path = checkpoint_dir / "game.rpg"
        files["game.rpg"] = self.file_info(game_path)
        for relative_path in CHECKPOINT_COMPANION_FILES:
            key = f"companion/{relative_path.as_posix()}"
            files[key] = self.file_info(self.companion_checkpoint_path(checkpoint_dir, relative_path))
        files["agent/session.json"] = self.file_info(checkpoint_dir / "agent" / "session.json")
        return files

    def checkpoint_context(self, state: dict) -> dict:
        identity = self.room_identity(state)
        room = state.get("room") if isinstance(state.get("room"), dict) else {}
        return {
            "scene_id": state.get("scene_id"),
            "map_id": state.get("map_id"),
            "room_id": identity["id"] if identity else None,
            "logical_room_id": identity["id"] if identity else None,
            "logical_room_label": identity.get("logical_room_label") if identity else None,
            "room_key": identity["room_key"] if identity else room.get("key"),
            "region_key": identity["region_key"] if identity else state.get("region_key"),
            "nav_room_id": identity["nav_room_id"] if identity else None,
            "legacy_room_id": identity.get("legacy_room_id") if identity else None,
            "legacy_nav_room_id": identity.get("legacy_nav_room_id") if identity else None,
            "frame": state.get("frame"),
            "logical_frame": state.get("logical_frame", state.get("frame")),
            "raw_frame": state.get("raw_frame"),
            "dialog_active": bool((state.get("dialog") or {}).get("active")),
            "entering_scene": bool(state.get("entering_scene")),
        }

    def checkpoint_manifest(self, checkpoint_dir: Path) -> dict | None:
        try:
            manifest = json.loads((checkpoint_dir / "manifest.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        if not isinstance(manifest, dict):
            return None
        manifest["id"] = checkpoint_dir.name
        manifest["files"] = self.checkpoint_file_status(checkpoint_dir)
        return manifest

    def list_checkpoints(self) -> dict:
        self.checkpoints_path.mkdir(parents=True, exist_ok=True)
        checkpoints = []
        for path in sorted(self.checkpoints_path.iterdir(), key=lambda item: item.name, reverse=True):
            if not path.is_dir():
                continue
            manifest = self.checkpoint_manifest(path)
            if manifest is not None:
                checkpoints.append(manifest)
        checkpoints.sort(key=lambda item: (item.get("created_at") or "", item.get("id") or ""), reverse=True)
        return {"ok": True, "checkpoints": checkpoints}

    def preflight_checkpoint_action(self, state: dict, *, allow_startup_waiting: bool = False) -> None:
        state_info = self.live_info("state")
        if not state_info.get("available") or state_info.get("stale"):
            raise RuntimeError("state_stale")
        if state.get("status") in {"waiting_for_game", "state_parse_error"}:
            raise RuntimeError(str(state.get("status")))
        if state.get("status") == "startup_waiting":
            if allow_startup_waiting:
                if self.action_pending(state):
                    raise RuntimeError("action_pending")
                return
            raise RuntimeError("startup_waiting")
        if state.get("in_main_game") is False:
            raise RuntimeError("not_in_main_game")
        control = self.control_state(state, state_info)
        if control["owner"] == "game" and control["phase"] not in {"battle", "entering_scene", "action_pending"}:
            raise RuntimeError("game_controlled")
        if state.get("in_battle"):
            raise RuntimeError("in_battle")
        if (state.get("dialog") or {}).get("active"):
            raise RuntimeError("in_dialog")
        if control["phase"] == "shop":
            raise RuntimeError("in_shop")
        if state.get("entering_scene"):
            raise RuntimeError("entering_scene")
        if self.action_pending(state):
            raise RuntimeError("action_pending")

    def preflight_start_new_game(self, state: dict) -> None:
        """Validate a reset without requiring the game to be at a safe save point."""
        state_info = self.live_info("state")
        if not state_info.get("available") or state_info.get("stale"):
            raise RuntimeError("state_stale")
        if state.get("status") in {"waiting_for_game", "state_parse_error"}:
            raise RuntimeError(str(state.get("status")))
        if state.get("in_battle"):
            raise RuntimeError("in_battle")
        if state.get("entering_scene"):
            raise RuntimeError("entering_scene")
        if self.action_pending(state):
            raise RuntimeError("action_pending")

    def refresh_companion_caches(self) -> None:
        self.cancel_runtime_observations("checkpoint_restored")
        self.event_log = self.read_events()
        self.dialog_history = self.read_dialog_history()
        self.dialog_source_keys = self.dialog_history_source_keys(self.dialog_history)
        self.last_observed_state = None
        self.last_stable_room_state = None
        self.pending_transition_observation = None
        self.pending_interaction_observation = None

    def cancel_runtime_observations(self, reason: str) -> None:
        self.observations.clear_all(reason)
        self.navigation_transition_requests.clear()
        self.dialog_advance_observation_id = None
        self.pending_transition_observation = None
        self.pending_interaction_observation = None

    def int_frame(self, value) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def logical_frame_for_raw(self, raw_frame) -> int | None:
        raw = self.int_frame(raw_frame)
        if raw is None:
            return None
        return raw + self.logical_frame_offset

    def seed_timeline_from_state(self, state: dict) -> None:
        raw_frame = self.int_frame(state.get("raw_frame", state.get("frame")))
        if raw_frame is None:
            return
        logical_frame = raw_frame + self.logical_frame_offset
        if self.last_raw_frame is None:
            self.last_raw_frame = raw_frame
        if self.last_logical_frame is None:
            self.last_logical_frame = logical_frame
        self.last_snapshot_frame = self.last_logical_frame

    def mark_checkpoint_frame_reset_pending(self, checkpoint_id: str, state: dict) -> None:
        previous_logical = self.last_logical_frame
        previous_raw = self.last_raw_frame
        if state.get("in_main_game") is not False and state.get("status") not in {"startup_waiting", "waiting_for_game"}:
            self.seed_timeline_from_state(state)
            previous_logical = self.last_logical_frame
            previous_raw = self.last_raw_frame
        companion_logical = self.max_companion_logical_frame()
        candidates = [value for value in (previous_logical, companion_logical) if value is not None]
        self.pending_checkpoint_frame_reset = {
            "checkpoint_id": checkpoint_id,
            "previous_raw_frame": previous_raw,
            "previous_logical_frame": max(candidates) if candidates else None,
            "time": time.time(),
        }

    def apply_frame_timeline(self, state: dict) -> None:
        raw_frame = self.int_frame(state.get("raw_frame", state.get("frame")))
        if raw_frame is None:
            return

        frame_restarted = self.last_raw_frame is not None and raw_frame + 5 < self.last_raw_frame
        pending_checkpoint_reset = self.pending_checkpoint_frame_reset is not None
        pending_previous_logical = (
            self.pending_checkpoint_frame_reset.get("previous_logical_frame")
            if pending_checkpoint_reset
            else None
        )
        pending_needs_adjustment = (
            pending_checkpoint_reset
            and pending_previous_logical is not None
            and raw_frame + self.logical_frame_offset <= pending_previous_logical
        )
        if (frame_restarted or pending_needs_adjustment) and self.pending_checkpoint_frame_reset is not None:
            previous_logical = self.last_logical_frame
            checkpoint = self.pending_checkpoint_frame_reset
            baseline_logical = checkpoint.get("previous_logical_frame")
            if previous_logical is not None:
                baseline_logical = (
                    max(previous_logical, baseline_logical)
                    if baseline_logical is not None
                    else previous_logical
                )
            if baseline_logical is not None and raw_frame + self.logical_frame_offset <= baseline_logical:
                self.logical_frame_offset = baseline_logical + 1 - raw_frame
            self.pending_checkpoint_frame_reset = None
            logical_frame = raw_frame + self.logical_frame_offset
            state["raw_frame"] = raw_frame
            state["logical_frame"] = logical_frame
            state["frame"] = logical_frame
            self.append_navigation_diagnostic(
                "timeline_adjusted",
                {
                    "reason": "checkpoint_restore",
                    "checkpoint_id": checkpoint.get("checkpoint_id"),
                    "raw_frame": raw_frame,
                    "previous_raw_frame": checkpoint.get("previous_raw_frame"),
                    "previous_logical_frame": checkpoint.get("previous_logical_frame"),
                    "logical_frame": logical_frame,
                    "logical_frame_offset": self.logical_frame_offset,
                },
                state=state,
            )
        elif frame_restarted:
            state["raw_frame"] = raw_frame
            state["logical_frame"] = raw_frame
            state["frame"] = raw_frame
            self.clear_runtime_history(reason="raw_frame_restarted", state=state)
            self.logical_frame_offset = 0
            self.pending_checkpoint_frame_reset = None

        logical_frame = raw_frame + self.logical_frame_offset
        state["raw_frame"] = raw_frame
        state["logical_frame"] = logical_frame
        state["frame"] = logical_frame
        self.last_raw_frame = raw_frame
        self.last_logical_frame = logical_frame
        self.last_snapshot_frame = logical_frame

    def max_companion_logical_frame(self) -> int | None:
        values: list[int] = []
        for entry in self.dialog_history:
            if not isinstance(entry, dict):
                continue
            for key in ("end_logical_frame", "logical_frame", "end_frame", "frame"):
                frame = self.int_frame(entry.get(key))
                if frame is not None:
                    values.append(frame)
                    break
        for entry in self.event_log:
            if not isinstance(entry, dict):
                continue
            frame = self.int_frame(entry.get("logical_frame", entry.get("frame")))
            if frame is not None:
                values.append(frame)
        for entry in self.read_agent_log(limit=500):
            frame = self.int_frame(entry.get("logical_frame", entry.get("frame")))
            if frame is not None:
                values.append(frame)
        return max(values) if values else None

    def write_slot_action(self, command: str, slot: int) -> dict:
        if command not in {"save_slot", "load_slot", "start_slot"}:
            raise ValueError("unsupported slot command")
        with self.action_lock:
            if self.action_pending():
                raise RuntimeError("action_pending")
            try:
                self.action_result_path.unlink()
            except FileNotFoundError:
                pass
            entry = {"time": time.time(), "command": command, "slot": int(slot)}
            self.begin_written_action(entry)
            try:
                self.atomic_write_text(self.action_path, f"{command} {int(slot)}\n", encoding="ascii")
            except Exception:
                self.pending_written_action = None
                raise
            self.action_log.append(entry)
            self.append_navigation_diagnostic("action_written", {"action": entry})
            return entry

    def wait_for_slot_result(self, command: str, slot: int, start_time: float, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = self.file_info(self.action_result_path)
            if info.get("available") and float(info.get("mtime") or 0) >= start_time:
                result = read_optional_json(self.action_result_path)
                try:
                    result_slot = int(result.get("slot"))
                except (TypeError, ValueError):
                    result_slot = -1
                if result.get("action") == command and result_slot == int(slot):
                    return result
            time.sleep(0.03)
        raise TimeoutError(f"{command} {slot} did not complete")

    def run_slot_command(self, command: str, slot: int) -> dict:
        start_time = time.time()
        action = self.write_slot_action(command, slot)
        result = self.wait_for_slot_result(command, slot, start_time)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or result.get("status") or "slot_command_failed"))
        return {"action": action, "result": result}

    def copy_game_save_to_checkpoint(self, checkpoint_dir: Path, slot: int) -> dict:
        source = self.save_slot_path(slot)
        if not source.exists():
            raise FileNotFoundError(f"save slot file not found: {source}")
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        target = checkpoint_dir / "game.rpg"
        tmp = checkpoint_dir / "game.rpg.tmp"
        shutil.copy2(source, tmp)
        os.replace(tmp, target)
        return self.file_info(target)

    def copy_game_save_from_checkpoint(self, checkpoint_dir: Path, slot: int) -> dict:
        source = checkpoint_dir / "game.rpg"
        if not source.exists():
            raise FileNotFoundError("checkpoint game.rpg is missing")
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        target = self.save_slot_path(slot)
        tmp = target.with_suffix(target.suffix + ".tmp")
        shutil.copy2(source, tmp)
        os.replace(tmp, target)
        return self.file_info(target)

    def copy_companion_to_checkpoint(self, checkpoint_dir: Path) -> dict:
        copied: dict[str, dict] = {}
        companion_dir = checkpoint_dir / "companion"
        companion_dir.mkdir(parents=True, exist_ok=True)
        for relative_path in CHECKPOINT_COMPANION_FILES:
            source = self.companion_live_path(relative_path)
            key = relative_path.as_posix()
            if not source.exists():
                copied[key] = {"available": False}
                continue
            target = self.companion_checkpoint_path(checkpoint_dir, relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            shutil.copy2(source, tmp)
            os.replace(tmp, target)
            copied[key] = self.file_info(target)
        return copied

    def checkpoint_companion_compatible(self, checkpoint_dir: Path, manifest: dict | None = None) -> bool:
        if isinstance(manifest, dict):
            try:
                if int(manifest.get("companion_version")) == EXPLORATION_VERSION:
                    return True
            except (TypeError, ValueError):
                pass
        exploration = read_optional_json(self.companion_checkpoint_path(checkpoint_dir, Path("exploration.json")))
        try:
            return int(exploration.get("version")) == EXPLORATION_VERSION
        except (TypeError, ValueError):
            return False

    def restore_companion_from_checkpoint(self, checkpoint_dir: Path) -> dict:
        restored: dict[str, dict] = {}
        manifest = self.checkpoint_manifest(checkpoint_dir)
        compatible = self.checkpoint_companion_compatible(checkpoint_dir, manifest)
        for relative_path in CHECKPOINT_COMPANION_FILES:
            source = self.companion_checkpoint_path(checkpoint_dir, relative_path)
            target = self.companion_live_path(relative_path)
            key = relative_path.as_posix()
            if not compatible and relative_path in CHECKPOINT_COMPANION_RESET_FILES:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                restored[key] = {
                    "available": False,
                    "discarded": True,
                    "reason": "incompatible_companion_version",
                    "expected_version": EXPLORATION_VERSION,
                }
                continue
            if not source.exists():
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                restored[key] = {"available": False}
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            shutil.copy2(source, tmp)
            os.replace(tmp, target)
            restored[key] = self.file_info(target)
        self.refresh_companion_caches()
        if compatible:
            self.recover_object_interactions_from_agent_llm_log()
        return restored

    def save_checkpoint(self, checkpoint_id: str | None = None, label: str | None = None) -> dict:
        state = self.read_live_state()
        self.preflight_checkpoint_action(state)
        checkpoint_id = self.checkpoint_id(checkpoint_id)
        checkpoint_dir = self.checkpoint_dir(checkpoint_id)
        if checkpoint_dir.exists():
            raise FileExistsError(f"checkpoint already exists: {checkpoint_id}")
        checkpoint_dir.mkdir(parents=True, exist_ok=False)

        try:
            slot = self.checkpoint_slot
            command_result = self.run_slot_command("save_slot", slot)
            self.copy_game_save_to_checkpoint(checkpoint_dir, slot)
            companion = self.copy_companion_to_checkpoint(checkpoint_dir)
            agent_bundle = self.agent_runtime.export_checkpoint(checkpoint_dir)
            manifest = {
                "version": CHECKPOINT_MANIFEST_VERSION,
                "id": checkpoint_id,
                "label": (label or "").strip()[:120],
                "created_at": utc_timestamp(),
                "companion_version": EXPLORATION_VERSION,
                "checkpoint_slot": slot,
                "state": self.checkpoint_context(state),
                "native_result": command_result["result"],
                "companion": companion,
                "agent_bundle": agent_bundle,
            }
            tmp = checkpoint_dir / "manifest.json.tmp"
            tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, checkpoint_dir / "manifest.json")
            return {"ok": True, "checkpoint": self.checkpoint_manifest(checkpoint_dir)}
        except Exception:
            shutil.rmtree(checkpoint_dir, ignore_errors=True)
            raise

    def load_checkpoint(self, checkpoint_id: str) -> dict:
        state = self.read_live_state()
        self.preflight_checkpoint_action(state, allow_startup_waiting=True)
        checkpoint_dir = self.checkpoint_dir(checkpoint_id)
        manifest = self.checkpoint_manifest(checkpoint_dir)
        if manifest is None:
            raise FileNotFoundError(f"checkpoint not found: {checkpoint_id}")

        slot = self.checkpoint_slot
        self.copy_game_save_from_checkpoint(checkpoint_dir, slot)
        command_result = self.run_slot_command("load_slot", slot)
        restored = self.restore_companion_from_checkpoint(checkpoint_dir)
        self.refresh_companion_caches()
        self.mark_checkpoint_frame_reset_pending(checkpoint_id, state)
        self.append_navigation_diagnostic(
            "checkpoint_restored",
            {"checkpoint_id": checkpoint_id, "restored": restored},
            state=state,
        )
        result = {
            "ok": True,
            "checkpoint": self.checkpoint_manifest(checkpoint_dir),
            "restored": restored,
            "native_result": command_result["result"],
        }
        self.agent_runtime.restore_checkpoint(
            checkpoint_dir,
            checkpoint_id,
            state.get("game_instance_id"),
        )
        return result

    def start_new_game(self) -> dict:
        state = self.read_live_state()
        self.preflight_start_new_game(state)
        command_result = self.run_slot_command("start_slot", 0)
        self.clear_runtime_history(reason="start_new_game", state=state)
        self.logical_frame_offset = 0
        self.last_raw_frame = None
        self.last_logical_frame = None
        self.last_snapshot_frame = None
        self.pending_checkpoint_frame_reset = None
        result = {
            "ok": True,
            "native_result": command_result["result"],
        }
        self.agent_runtime.reset("start_new_game", game_instance_id=state.get("game_instance_id"))
        return result

    def delete_checkpoint(self, checkpoint_id: str) -> dict:
        checkpoint_dir = self.checkpoint_dir(checkpoint_id)
        if not checkpoint_dir.exists() or not checkpoint_dir.is_dir():
            raise FileNotFoundError(f"checkpoint not found: {checkpoint_id}")
        shutil.rmtree(checkpoint_dir)
        return {"ok": True, "id": checkpoint_id}

    def read_agent_log(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        try:
            lines = self.agent_log_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        entries = []
        for line in lines[-limit:]:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and self.agent_log_entry_compatible(entry):
                entries.append(entry)
        return entries

    def current_agent_turn(self) -> int:
        latest = self.read_agent_log(limit=1)
        if not latest:
            return 0
        try:
            return max(0, int(latest[-1].get("turn") or 0))
        except (TypeError, ValueError):
            return 0

    def read_agent_llm_log(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        try:
            lines = self.agent_llm_log_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        entries = []
        for line in lines[-limit:]:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        return entries

    def agent_log_entry_compatible(self, entry: dict) -> bool:
        episode = entry.get("episode")
        if isinstance(episode, str) and episode.startswith("lr0:"):
            return False
        if isinstance(episode, str) and episode.count("/") == 2 and "nav_room_id" not in entry:
            return False
        return True

    def state_context(self, state: dict) -> dict:
        identity = self.room_identity(state)
        room = state.get("room") if isinstance(state.get("room"), dict) else {}
        return {
            "scene_id": state.get("scene_id"),
            "map_id": state.get("map_id"),
            "room_id": identity["id"] if identity else None,
            "logical_room_id": identity["id"] if identity else None,
            "logical_room_label": identity.get("logical_room_label") if identity else None,
            "room_key": identity["room_key"] if identity else room.get("key"),
            "region_key": identity["region_key"] if identity else state.get("region_key"),
            "nav_room_id": identity["nav_room_id"] if identity else None,
            "legacy_room_id": identity.get("legacy_room_id") if identity else None,
            "legacy_nav_room_id": identity.get("legacy_nav_room_id") if identity else None,
            "frame": state.get("frame"),
            "logical_frame": state.get("logical_frame", state.get("frame")),
            "raw_frame": state.get("raw_frame"),
        }

    def attach_decision_context(self, state: dict, decision: dict) -> dict:
        decision["state_context"] = self.state_context(state)
        return decision

    def append_agent_decision(self, state: dict, decision: dict) -> dict:
        self.attach_decision_context(state, decision)
        latest = self.read_agent_log(limit=1)
        turn = int(latest[-1].get("turn") or 0) + 1 if latest else 1
        room = state.get("room") if isinstance(state.get("room"), dict) else {}
        identity = self.room_identity(state)
        episode = identity["id"] if identity else "/".join(
            str(value)
            for value in (
                state.get("scene_id", "unknown"),
                state.get("map_id", "unknown"),
                room.get("key") or state.get("region_key") or "unknown",
            )
        )
        entry = {
            "time": time.time(),
            "session_id": getattr(getattr(self, "agent_runtime", None), "state", {}).get("session_id"),
            "episode": episode,
            "turn": turn,
            "frame": state.get("frame"),
            "logical_frame": state.get("logical_frame", state.get("frame")),
            "raw_frame": state.get("raw_frame"),
            "scene_id": state.get("scene_id"),
            "map_id": state.get("map_id"),
            "room_key": identity["room_key"] if identity else room.get("key"),
            "region_key": identity["region_key"] if identity else state.get("region_key"),
            "nav_room_id": identity["nav_room_id"] if identity else None,
            "logical_room_id": identity["id"] if identity else None,
            "logical_room_label": identity.get("logical_room_label") if identity else None,
            "legacy_room_id": identity.get("legacy_room_id") if identity else None,
            "legacy_nav_room_id": identity.get("legacy_nav_room_id") if identity else None,
            "state_context": decision.get("state_context"),
            "decision": decision,
        }
        for key in ("backend", "model", "usage", "tool_trace", "llm_call_id"):
            if key in decision:
                entry[key] = decision[key]
        with self.agent_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return entry

    def append_agent_llm_log(self, state: dict, entry: dict) -> dict:
        payload = {
            "time": time.time(),
            "session_id": getattr(getattr(self, "agent_runtime", None), "state", {}).get("session_id"),
            "state_context": self.state_context(state),
            **entry,
        }
        with self.agent_llm_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return payload

    def append_agent_error(
        self,
        state: dict,
        backend: str,
        model: str | None,
        error: str,
        usage: dict | None = None,
        tool_trace: list[dict] | None = None,
        tool_calls: int | None = None,
        rounds: int | None = None,
        llm_call_id: str | None = None,
    ) -> dict:
        latest = self.read_agent_log(limit=1)
        turn = int(latest[-1].get("turn") or 0) + 1 if latest else 1
        room = state.get("room") or {}
        identity = self.room_identity(state)
        episode = identity["id"] if identity else "/".join(
            str(value)
            for value in (
                state.get("scene_id", "unknown"),
                state.get("map_id", "unknown"),
                room.get("key") or state.get("region_key") or "unknown",
            )
        )
        entry = {
            "time": time.time(),
            "session_id": getattr(getattr(self, "agent_runtime", None), "state", {}).get("session_id"),
            "episode": episode,
            "turn": turn,
            "frame": state.get("frame"),
            "logical_frame": state.get("logical_frame", state.get("frame")),
            "raw_frame": state.get("raw_frame"),
            "scene_id": state.get("scene_id"),
            "map_id": state.get("map_id"),
            "room_key": identity["room_key"] if identity else room.get("key"),
            "region_key": identity["region_key"] if identity else state.get("region_key"),
            "nav_room_id": identity["nav_room_id"] if identity else None,
            "state_context": self.state_context(state),
            "ok": False,
            "backend": backend,
            "error": error,
        }
        if model:
            entry["model"] = model
        if usage:
            entry["usage"] = usage
        if tool_trace is not None:
            entry["tool_trace"] = tool_trace
        if tool_calls is not None:
            entry["tool_calls"] = tool_calls
        if rounds is not None:
            entry["rounds"] = rounds
        if llm_call_id:
            entry["llm_call_id"] = llm_call_id
        with self.agent_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return entry

    def clear_runtime_history(self, reason: str = "reset", state: dict | None = None) -> None:
        with self.state_lock:
            self.cancel_runtime_observations(reason)
            self.action_log = []
            self.event_log = []
            self.dialog_history = []
            self.dialog_source_keys = set()
            self.last_observed_state = None
            self.last_stable_room_state = None
            for path in (
                self.action_result_path,
                self.agent_log_path,
                self.agent_llm_log_path,
                self.events_path,
                self.dialogs_path,
                self.exploration_path,
                self.navigation_log_path,
            ):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            self.append_navigation_diagnostic(
                "runtime_history_cleared",
                {"reason": reason},
                state=state,
            )

    def reset_history_if_frame_restarted(self, state: dict) -> None:
        self.apply_frame_timeline(state)

    def read_events(self) -> list[dict]:
        try:
            data = json.loads(self.events_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        cleaned = []
        for event in data:
            if not isinstance(event, dict):
                continue
            if event.get("type") == "object_changed":
                changes = event.get("changes") if isinstance(event.get("changes"), dict) else {}
                if changes and not any(key != "auto_script" for key in changes):
                    continue
            cleaned.append(event)
        return cleaned[-200:]

    def write_events(self) -> None:
        self.atomic_write_text(
            self.events_path,
            json.dumps(self.event_log[-200:], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def read_dialog_history(self) -> list[dict]:
        try:
            data = json.loads(self.dialogs_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return self.clean_dialog_history_entries(data) if isinstance(data, list) else []

    def write_dialog_history(self) -> None:
        self.atomic_write_text(
            self.dialogs_path,
            json.dumps(self.dialog_history[-2000:], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def append_event(self, state: dict, event_type: str, payload: dict) -> dict:
        identity = self.room_identity(state)
        event = {
            "time": time.time(),
            "frame": state.get("frame"),
            "logical_frame": state.get("logical_frame", state.get("frame")),
            "raw_frame": state.get("raw_frame"),
            "scene_id": state.get("scene_id"),
            "map_id": state.get("map_id"),
            "room_id": identity["id"] if identity else None,
            "logical_room_id": identity["id"] if identity else None,
            "logical_room_label": identity.get("logical_room_label") if identity else None,
            "room_key": identity["room_key"] if identity else (state.get("room") or {}).get("key"),
            "region_key": identity["region_key"] if identity else state.get("region_key"),
            "nav_room_id": identity["nav_room_id"] if identity else None,
            "legacy_room_id": identity.get("legacy_room_id") if identity else None,
            "legacy_nav_room_id": identity.get("legacy_nav_room_id") if identity else None,
            "type": event_type,
            **payload,
        }
        self.event_log.append(event)
        self.event_log = self.event_log[-200:]
        self.write_events()
        if event_type in {"interaction_effect", "interaction_no_effect"} or self.object_change_cleared_blocker(event):
            self.record_persistent_object_event(event, state=state)
        return event

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
                    "logical_frame": state.get("logical_frame", state.get("frame")),
                    "raw_frame": state.get("raw_frame"),
                    "scene_id": state.get("scene_id"),
                    "map_id": state.get("map_id"),
                    "room_key": (state.get("room") or {}).get("key"),
                    "region_key": state.get("region_key"),
                    "party_position": {"x": pos.get("x"), "y": pos.get("y")},
                    "dialog_active": bool(dialog.get("active")),
                    "dialog_latest_text": (dialog.get("latest_event") or {}).get("text"),
                    "in_battle": bool(state.get("in_battle")),
                    "in_main_game": state.get("in_main_game"),
                    "action_pending": self.action_pending(state),
                }
            )
        self.navigation_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.navigation_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def dialog_source_key(self, event: dict, state: dict | None = None) -> str:
        scene_id = event.get("scene_id")
        map_id = event.get("map_id")
        if isinstance(state, dict):
            scene_id = scene_id if scene_id is not None else state.get("scene_id")
            map_id = map_id if map_id is not None else state.get("map_id")
        event_frame = event.get("raw_frame", event.get("frame"))
        return ":".join(
            ["dialog", str(scene_id or ""), str(map_id or ""), str(event_frame or "")]
            + [str(event.get(key) or "") for key in ("event_object_id", "message_id", "text")]
        )

    def room_id_prefix(self, value) -> tuple[int, int] | None:
        parts = str(value or "").split("/")
        if len(parts) < 2:
            return None
        try:
            return int(parts[0]), int(parts[1])
        except (TypeError, ValueError):
            return None

    def dialog_room_metadata_consistent(self, entry: dict) -> bool:
        if not isinstance(entry, dict):
            return False
        scene_id = self.int_or_none(entry.get("scene_id"))
        map_id = self.int_or_none(entry.get("map_id"))
        prefixes = []
        for key in ("nav_room_id", "room_id"):
            prefix = self.room_id_prefix(entry.get(key))
            if prefix is not None:
                prefixes.append(prefix)
        if scene_id is not None and map_id is not None:
            if any(prefix != (scene_id, map_id) for prefix in prefixes):
                return False
        nav_room_id = entry.get("nav_room_id")
        room_id = entry.get("room_id")
        if nav_room_id and room_id and str(room_id) != str(nav_room_id) and not str(room_id).startswith(f"{nav_room_id}/"):
            return False
        return True

    def dialog_has_room_metadata(self, entry: dict) -> bool:
        return any(entry.get(key) for key in ("room_id", "nav_room_id", "room_key", "region_key"))

    def dialog_event_matches_current_identity(self, state: dict, event: dict) -> bool:
        identity = self.room_identity(state)
        if identity is None:
            return False
        scene_id = event.get("scene_id")
        map_id = event.get("map_id")
        if scene_id is not None and scene_id != identity["scene_id"]:
            return False
        if map_id is not None and map_id != identity["map_id"]:
            return False
        return True

    def clean_dialog_history_entries(self, entries: list[dict]) -> list[dict]:
        cleaned: list[dict] = []
        by_source_key: dict[str, dict] = {}
        for raw in entries:
            if not isinstance(raw, dict):
                continue
            entry = dict(raw)
            if not self.dialog_room_metadata_consistent(entry):
                for key in ("room_id", "room_key", "region_key", "nav_room_id"):
                    entry.pop(key, None)
            source_key = self.dialog_source_key(entry)
            entry["source_key"] = source_key
            entry["source_keys"] = [source_key]
            existing = by_source_key.get(source_key)
            if existing is not None:
                if (
                    not self.dialog_has_room_metadata(existing)
                    and self.dialog_has_room_metadata(entry)
                    and self.dialog_room_metadata_consistent(entry)
                ):
                    existing.update({key: entry.get(key) for key in ("room_id", "room_key", "region_key", "nav_room_id") if entry.get(key)})
                continue
            by_source_key[source_key] = entry
            cleaned.append(entry)
        return cleaned

    def dialog_entry_stable_source_key(self, entry: dict) -> str | None:
        if not isinstance(entry, dict) or not entry.get("text"):
            return None
        return self.dialog_source_key(entry)

    def dialog_source_keys_for_entry(self, entry: dict) -> set[str]:
        keys: set[str] = set()
        source_keys = entry.get("source_keys")
        if isinstance(source_keys, list):
            keys.update(str(key) for key in source_keys if key)
        if entry.get("source_key"):
            keys.add(str(entry.get("source_key")))
        stable_key = self.dialog_entry_stable_source_key(entry)
        if stable_key:
            keys.add(stable_key)
        return keys

    def dialog_event_frame(self, state: dict, event: dict) -> tuple[int | None, int | None]:
        raw_frame = self.int_frame(event.get("frame", state.get("raw_frame", state.get("frame"))))
        logical_frame = self.logical_frame_for_raw(raw_frame)
        if logical_frame is None:
            logical_frame = self.int_frame(state.get("logical_frame", state.get("frame")))
        return raw_frame, logical_frame

    def dialog_history_source_keys(self, history: list[dict]) -> set[str]:
        keys: set[str] = set()
        for entry in history:
            keys.update(self.dialog_source_keys_for_entry(entry))
        return keys

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

    def dialog_text_is_speaker_title(self, event: dict, speaker_hint: str | None) -> bool:
        if not speaker_hint:
            return False
        text = str(event.get("text") or "").strip()
        title = text.rstrip(":：∶").strip()
        return bool(title) and title == speaker_hint

    def int_or_none(self, value) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def dialog_message_ids(self, event: dict) -> list[int]:
        message_id = self.int_or_none(event.get("message_id"))
        return [] if message_id is None else [message_id]

    def dialog_turn_from_event(self, state: dict, dialog: dict, event: dict, source_key: str) -> dict:
        sequence = self.int_or_none(event.get("sequence"))
        raw_frame, logical_frame = self.dialog_event_frame(state, event)
        frame = logical_frame if logical_frame is not None else state.get("frame")
        identity = self.room_identity(state) if self.dialog_event_matches_current_identity(state, event) else None
        return {
            "time": time.time(),
            "source_key": source_key,
            "source_keys": [source_key],
            "sequence": event.get("sequence"),
            "start_sequence": sequence if sequence is not None else event.get("sequence"),
            "end_sequence": sequence if sequence is not None else event.get("sequence"),
            "frame": frame,
            "end_frame": frame,
            "logical_frame": frame,
            "end_logical_frame": frame,
            "raw_frame": raw_frame,
            "end_raw_frame": raw_frame,
            "scene_id": event.get("scene_id", state.get("scene_id")),
            "map_id": event.get("map_id", state.get("map_id")),
            "room_id": identity["id"] if identity else None,
            "logical_room_id": identity["id"] if identity else None,
            "logical_room_label": identity.get("logical_room_label") if identity else None,
            "room_key": identity["room_key"] if identity else None,
            "region_key": identity["region_key"] if identity else None,
            "nav_room_id": identity["nav_room_id"] if identity else None,
            "legacy_room_id": identity.get("legacy_room_id") if identity else None,
            "legacy_nav_room_id": identity.get("legacy_nav_room_id") if identity else None,
            "agent_turn": self.current_agent_turn(),
            "event_object_id": event.get("event_object_id"),
            "message_id": event.get("message_id"),
            "message_ids": self.dialog_message_ids(event),
            "dialog_position": event.get("dialog_position") or dialog.get("position"),
            "speaker_hint": self.dialog_speaker_hint(event),
            "text": str(event.get("text") or ""),
        }

    def dialog_events_are_contiguous(self, previous: dict, current: dict) -> bool:
        previous_sequence = self.int_or_none(previous.get("end_sequence", previous.get("sequence")))
        current_sequence = self.int_or_none(current.get("start_sequence", current.get("sequence")))
        if previous_sequence is not None and current_sequence is not None:
            return current_sequence == previous_sequence + 1

        previous_message_ids = previous.get("message_ids")
        previous_message_id = None
        if isinstance(previous_message_ids, list) and previous_message_ids:
            previous_message_id = self.int_or_none(previous_message_ids[-1])
        if previous_message_id is None:
            previous_message_id = self.int_or_none(previous.get("message_id"))
        current_message_ids = current.get("message_ids")
        current_message_id = None
        if isinstance(current_message_ids, list) and current_message_ids:
            current_message_id = self.int_or_none(current_message_ids[0])
        if current_message_id is None:
            current_message_id = self.int_or_none(current.get("message_id"))
        if previous_message_id is not None and current_message_id is not None:
            return current_message_id == previous_message_id + 1
        return False

    def can_merge_dialog_turn(self, previous: dict, current: dict) -> bool:
        if not previous or previous.get("speaker_hint") == "系统" or current.get("speaker_hint") == "系统":
            return False
        for key in ("speaker_hint", "event_object_id", "scene_id", "map_id", "room_id", "room_key", "region_key"):
            if previous.get(key) != current.get(key):
                return False
        return self.dialog_events_are_contiguous(previous, current)

    def merge_dialog_turn(self, previous: dict, current: dict) -> None:
        previous["text"] = f"{previous.get('text') or ''}{current.get('text') or ''}"
        previous["updated_at"] = current.get("time")
        previous["end_sequence"] = current.get("end_sequence")
        previous["end_frame"] = current.get("end_frame", current.get("frame"))
        previous["end_logical_frame"] = current.get("end_logical_frame", current.get("logical_frame", current.get("end_frame", current.get("frame"))))
        previous["end_raw_frame"] = current.get("end_raw_frame", current.get("raw_frame"))
        previous["message_id"] = current.get("message_id")
        previous["dialog_position"] = current.get("dialog_position") or previous.get("dialog_position")
        source_keys = previous.setdefault("source_keys", [])
        if not isinstance(source_keys, list):
            source_keys = [previous.get("source_key")]
            previous["source_keys"] = source_keys
        for key in current.get("source_keys") or [current.get("source_key")]:
            if key and key not in source_keys:
                source_keys.append(key)
        message_ids = previous.setdefault("message_ids", [])
        if not isinstance(message_ids, list):
            message_ids = [previous.get("message_id")] if previous.get("message_id") is not None else []
            previous["message_ids"] = message_ids
        for message_id in current.get("message_ids") or []:
            if message_id not in message_ids:
                message_ids.append(message_id)

    def record_dialog_event(self, state: dict) -> None:
        if state.get("status") in {"waiting_for_game", "state_parse_error"}:
            return

        dialog = state.get("dialog") or {}
        events = dialog.get("events")
        if not isinstance(events, list):
            return

        changed = False
        processed_source_keys: set[str] = set()
        for event in events:
            if not isinstance(event, dict) or not event.get("text"):
                continue

            source_key = self.dialog_source_key(event, state)
            if source_key in self.dialog_source_keys:
                continue

            entry = self.dialog_turn_from_event(state, dialog, event, source_key)
            if self.dialog_text_is_speaker_title(event, entry.get("speaker_hint")):
                self.dialog_source_keys.add(source_key)
                processed_source_keys.add(source_key)
                changed = True
                continue
            if self.dialog_history and self.can_merge_dialog_turn(self.dialog_history[-1], entry):
                self.merge_dialog_turn(self.dialog_history[-1], entry)
            else:
                self.dialog_history.append(entry)
            self.dialog_source_keys.add(source_key)
            processed_source_keys.add(source_key)
            changed = True

        if changed:
            self.dialog_history = self.dialog_history[-2000:]
            self.dialog_source_keys = self.dialog_history_source_keys(self.dialog_history)
            self.dialog_source_keys.update(processed_source_keys)
            self.write_dialog_history()

    def dialog_advance_source_keys(self, entry: dict | None) -> list[str]:
        if not isinstance(entry, dict):
            return []
        keys = []
        if entry.get("source_key"):
            keys.append(str(entry.get("source_key")))
        source_keys = entry.get("source_keys")
        if isinstance(source_keys, list):
            keys.extend(str(key) for key in source_keys if key)
        return list(dict.fromkeys(keys))

    def dialog_advance_marker_from_entry(self, entry: dict | None) -> dict | None:
        if not isinstance(entry, dict):
            return None
        try:
            entry_time = float(entry.get("time"))
        except (TypeError, ValueError):
            entry_time = None
        return {"time": entry_time, "source_keys": self.dialog_advance_source_keys(entry)}

    def dialog_advance_entry_matches_marker(self, entry: dict, marker: dict | None) -> bool:
        marker_keys = marker.get("source_keys") if isinstance(marker, dict) else None
        if not isinstance(marker_keys, list) or not marker_keys:
            return False
        entry_keys = set(self.dialog_advance_source_keys(entry))
        return any(key in entry_keys for key in marker_keys)

    def dialog_advance_initial_marker(self, state: dict) -> dict | None:
        if not self.dialog_history:
            return None
        latest = self.dialog_history[-1]
        latest_text = str(latest.get("text") or "").strip()
        active_text = str(((state.get("dialog") or {}).get("latest_event") or {}).get("text") or "").strip()
        if (
            (state.get("dialog") or {}).get("active")
            and active_text
            and latest_text
            and (latest_text == active_text or latest_text.endswith(active_text))
        ):
            return self.dialog_advance_marker_from_entry(self.dialog_history[-2] if len(self.dialog_history) >= 2 else None)
        return self.dialog_advance_marker_from_entry(latest)

    def dialog_advance_recent_dialogs(self, marker: dict | None) -> list[dict]:
        if not marker:
            return self.dialog_history[:]
        for index in range(len(self.dialog_history) - 1, -1, -1):
            if self.dialog_advance_entry_matches_marker(self.dialog_history[index], marker):
                return self.dialog_history[index + 1 :]
        marker_time = marker.get("time") if isinstance(marker, dict) else None
        if marker_time is not None:
            recent = []
            for entry in self.dialog_history:
                try:
                    entry_time = float(entry.get("time"))
                except (TypeError, ValueError):
                    continue
                if entry_time > marker_time:
                    recent.append(entry)
            return recent
        return self.dialog_history[:]

    def dialog_advance_identity(self, entry: dict) -> str | None:
        if not isinstance(entry, dict) or entry.get("speaker_hint") == "系统":
            return None
        scene_id = entry.get("scene_id", "")
        map_id = entry.get("map_id", "")
        room_key = entry.get("room_key", "")
        event_object_id = entry.get("event_object_id")
        if event_object_id not in (None, ""):
            return f"object:{scene_id}:{map_id}:{room_key}:{event_object_id}"
        speaker = entry.get("speaker_hint")
        if speaker:
            return f"speaker:{scene_id}:{map_id}:{room_key}:{speaker}"
        return None

    def dialog_advance_reached_repeated_npc_line(self, marker: dict | None) -> bool:
        candidates = []
        for entry in self.dialog_advance_recent_dialogs(marker):
            identity = self.dialog_advance_identity(entry)
            text = str(entry.get("text") or "").strip()
            if identity and text:
                candidates.append({"identity": identity, "text": text})
        if len(candidates) < 2:
            return False
        previous = candidates[-2]
        current = candidates[-1]
        return previous["identity"] == current["identity"] and previous["text"] == current["text"]

    def normalized_dialog_speaker(self, value) -> str:
        return str(value or "").strip().replace("遥", "遙")

    def dialog_advance_party_speakers(self, state: dict) -> set[str]:
        names = {"李逍遙"}
        party = state.get("party") if isinstance(state, dict) and isinstance(state.get("party"), list) else []
        for role in party:
            if not isinstance(role, dict):
                continue
            for key in ("name", "role_name", "display_name"):
                name = self.normalized_dialog_speaker(role.get(key))
                if name:
                    names.add(name)
        return names

    def dialog_advance_event_info_available(self, event: dict) -> bool:
        if not isinstance(event, dict) or not str(event.get("text") or "").strip():
            return False
        return any(event.get(key) not in (None, "") for key in ("speaker_hint", "event_object_id", "message_id"))

    def dialog_advance_event_observation_key(self, state: dict, event: dict) -> tuple | None:
        if not isinstance(event, dict) or not str(event.get("text") or "").strip():
            return None
        identity = self.room_identity(state) if isinstance(state, dict) else None
        room_id = identity["id"] if identity else event.get("room_id") or event.get("room_key") or ""
        return (
            room_id,
            event.get("event_object_id"),
            event.get("message_id"),
            str(event.get("text") or "").strip(),
            event.get("sequence"),
            event.get("frame"),
        )

    def dialog_advance_opponent_event_key(self, state: dict, event: dict) -> tuple | None:
        if not isinstance(event, dict):
            return None
        text = str(event.get("text") or "").strip()
        if not text:
            return None
        speaker = self.dialog_speaker_hint(event)
        normalized_speaker = self.normalized_dialog_speaker(speaker)
        if not normalized_speaker or normalized_speaker == "系统":
            return None
        if normalized_speaker in self.dialog_advance_party_speakers(state):
            return None

        identity = self.room_identity(state) if isinstance(state, dict) else None
        room_id = identity["id"] if identity else event.get("room_id") or event.get("room_key") or ""
        return (
            room_id,
            event.get("event_object_id"),
            normalized_speaker,
            event.get("message_id"),
            text,
        )

    def dialog_advance_observe_events(
        self,
        state: dict,
        observed_event_keys: set[tuple],
        seen_opponent_keys: set[tuple],
        *,
        detect_repeats: bool,
    ) -> dict:
        dialog = state.get("dialog") if isinstance(state, dict) else {}
        events = dialog.get("events") if isinstance(dialog, dict) else None
        if not isinstance(events, list):
            return {"has_event_info": False, "repeated": False}

        has_event_info = False
        repeated = False
        for event in events:
            if not isinstance(event, dict):
                continue
            if self.dialog_advance_event_info_available(event):
                has_event_info = True
            observation_key = self.dialog_advance_event_observation_key(state, event)
            if observation_key is None or observation_key in observed_event_keys:
                continue
            observed_event_keys.add(observation_key)

            opponent_key = self.dialog_advance_opponent_event_key(state, event)
            if opponent_key is None:
                continue
            if detect_repeats and opponent_key in seen_opponent_keys:
                repeated = True
            seen_opponent_keys.add(opponent_key)
        return {"has_event_info": has_event_info, "repeated": repeated}

    def dialog_advance_active_event_key(self, state: dict) -> str | None:
        dialog = state.get("dialog") if isinstance(state, dict) else {}
        if not isinstance(dialog, dict) or not dialog.get("active"):
            return None
        latest = dialog.get("latest_event")
        if not isinstance(latest, dict) or not str(latest.get("text") or "").strip():
            return None
        return self.dialog_source_key(latest, state)

    def dialogs_view(self, limit: int = 100, speaker: str | None = None) -> dict:
        limit = max(1, min(int(limit), 500))
        dialogs = self.dialog_history
        if speaker:
            dialogs = [entry for entry in dialogs if entry.get("speaker_hint") == speaker]
        return {"dialogs": dialogs[-limit:], "total": len(dialogs)}

    def dialog_speakers_view(self, limit: int = 100) -> dict:
        limit = max(1, min(int(limit), 300))
        speakers: dict[str, dict] = {}
        for index, entry in enumerate(self.dialog_history):
            speaker = str(entry.get("speaker_hint") or "").strip()
            if not speaker or speaker == "系统":
                continue
            summary = speakers.setdefault(
                speaker,
                {
                    "speaker_hint": speaker,
                    "last_time": None,
                    "last_frame": None,
                    "last_end_frame": None,
                    "last_sequence": None,
                    "last_end_sequence": None,
                    "last_scene_id": None,
                    "last_map_id": None,
                    "last_room_key": None,
                    "dialog_count": 0,
                    "_last_index": -1,
                },
            )
            summary["dialog_count"] += 1
            summary["last_time"] = entry.get("time")
            summary["last_frame"] = entry.get("frame")
            summary["last_end_frame"] = entry.get("end_frame")
            summary["last_sequence"] = entry.get("start_sequence", entry.get("sequence"))
            summary["last_end_sequence"] = entry.get("end_sequence", entry.get("sequence"))
            summary["last_scene_id"] = entry.get("scene_id")
            summary["last_map_id"] = entry.get("map_id")
            summary["last_room_key"] = entry.get("room_key")
            summary["_last_index"] = index

        def sort_value(value) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return float("-inf")

        ordered = sorted(
            speakers.values(),
            key=lambda item: (
                sort_value(item.get("last_time")),
                sort_value(item.get("last_end_frame")),
                sort_value(item.get("last_frame")),
                sort_value(item.get("last_end_sequence")),
                item.get("_last_index", -1),
            ),
            reverse=True,
        )
        for item in ordered:
            item.pop("_last_index", None)
        return {"speakers": ordered[:limit], "total": len(ordered), "returned": min(len(ordered), limit)}

    @staticmethod
    def item_approach_requirements(state: dict) -> list[dict]:
        requirements = []
        for item in state.get("inventory") or []:
            if not isinstance(item, dict) or item.get("usable") is not True:
                continue
            try:
                item_id = int(item.get("item_id") or 0)
                available = int(item.get("amount") or 0) - int(item.get("amount_in_use") or 0)
            except (TypeError, ValueError):
                continue
            effect = item.get("use_effect") if isinstance(item.get("use_effect"), dict) else {}
            if item_id <= 0 or available <= 0:
                continue
            for requirement in effect.get("requirements") or []:
                if not isinstance(requirement, dict) or requirement.get("kind") != "face_event_object":
                    continue
                try:
                    object_id = int(requirement.get("event_object_id") or 0)
                    distance_level = int(requirement.get("distance_level") or 0)
                    script_entry = int(requirement.get("script_entry") or 0)
                    failure_script_entry = int(requirement.get("failure_script_entry") or 0)
                except (TypeError, ValueError):
                    continue
                if object_id <= 0 or distance_level < 0:
                    continue
                requirements.append({
                    "kind": "face_event_object",
                    "item_id": item_id,
                    "item_name": str(item.get("name") or ""),
                    "event_object_id": object_id,
                    "distance_level": distance_level,
                    "distance_limit": distance_level * 32 + 16,
                    "script_entry": script_entry,
                    "failure_script_entry": failure_script_entry,
                    "conditional": requirement.get("conditional") is True,
                    "requirements_complete": effect.get("requirements_complete") is True,
                })
        return sorted(
            requirements,
            key=lambda entry: (
                entry["event_object_id"],
                entry["item_id"],
                entry["script_entry"],
            ),
        )

    def approach_requirements_by_target(
        self,
        state: dict,
        item_id: int | None = None,
    ) -> dict[int, list[dict]]:
        result: dict[int, list[dict]] = {}
        for requirement in self.item_approach_requirements(state):
            if item_id is not None and requirement["item_id"] != item_id:
                continue
            result.setdefault(requirement["event_object_id"], []).append(requirement)
        return result

    def item_approach_affordances(
        self,
        state: dict,
        target_id: int,
        requirements: list[dict] | None = None,
        *,
        grid: dict | None = None,
        reachable_order: list[tuple[int, int]] | None = None,
        reachable_distance: dict[tuple[int, int], int] | None = None,
        fallback_order: list[tuple[int, int]] | None = None,
    ) -> list[dict]:
        candidates = requirements
        if candidates is None:
            candidates = self.approach_requirements_by_target(state).get(target_id, [])
        affordances = []
        target = self.target_object(state, target_id)
        for requirement in candidates:
            if (
                grid is not None
                and reachable_order is not None
                and reachable_distance is not None
                and target is not None
            ):
                goals, directions, _ = self.approach_goal_nodes(
                    state,
                    grid,
                    target,
                    [requirement],
                )
                goal = next((node for node in reachable_order if node in goals), None)
                fallback_goal = (
                    next((node for node in fallback_order if node in goals), None)
                    if goal is None and fallback_order is not None
                    else None
                )
                selected_goal = goal or fallback_goal
                status = "reachable" if goal is not None else "blocked_by_transition" if fallback_goal is not None else "unreachable_static"
                affordances.append({
                    **dict(requirement),
                    "kind": "item_approach",
                    "status": status,
                    "steps": int(reachable_distance[goal]) if goal is not None else None,
                    "required_direction": (
                        DIR_TO_BUTTON.get(
                            directions.get(selected_goal, int(state.get("party_direction") or 0)),
                            "down",
                        )
                        if selected_goal is not None
                        else None
                    ),
                })
            else:
                route = self.route_to_object(
                    state,
                    target_id,
                    avoid_transitions=True,
                    approach_item_id=int(requirement.get("item_id") or 0),
                    approach_requirement=requirement,
                )
                if route.get("status") != "reachable":
                    continue
                affordance = route.get("affordance")
                if isinstance(affordance, dict):
                    affordances.append(affordance)
        return sorted(
            affordances,
            key=lambda entry: (
                0 if entry.get("status") == "reachable" else 1,
                int(entry.get("steps")) if entry.get("steps") is not None else 1 << 30,
                int(entry.get("item_id") or 0),
                int(entry.get("script_entry") or 0),
            ),
        )

    def matched_item_event_object(self, state: dict, item_id: int) -> dict | None:
        try:
            normalized_item_id = int(item_id)
        except (TypeError, ValueError):
            return None
        pos = state.get("party_position") or {}
        position = (int(pos.get("x") or 0), int(pos.get("y") or 0))
        direction = int(state.get("party_direction") or 0)
        objects = {
            int(obj.get("id") or 0): obj
            for obj in state.get("objects") or []
            if isinstance(obj, dict) and int(obj.get("state") or 0) > 0
        }
        matches = []
        for requirement in self.item_approach_requirements(state):
            if int(requirement.get("item_id") or 0) != normalized_item_id:
                continue
            object_id = int(requirement.get("event_object_id") or 0)
            target = objects.get(object_id)
            if target is None or not facing_event_object_hits(
                position,
                direction,
                target,
                int(requirement.get("distance_limit") or 0),
            ):
                continue
            matches.append((int(requirement.get("script_entry") or 0), object_id, requirement))
        if not matches:
            return None
        _, object_id, requirement = min(matches, key=lambda entry: (entry[0], entry[1]))
        return {"event_object_id": object_id, "requirement": dict(requirement)}

    @staticmethod
    def item_object_effect_observation(
        before: dict,
        after: dict,
        matched: dict | None,
    ) -> dict:
        if not isinstance(matched, dict):
            return {
                "matched_event_object_id": None,
                "effect_observed": None,
                "object_delta": None,
            }
        object_id = int(matched.get("event_object_id") or 0)

        def object_snapshot(state: dict) -> dict | None:
            obj = next(
                (
                    entry for entry in state.get("objects") or []
                    if isinstance(entry, dict) and int(entry.get("id") or 0) == object_id
                ),
                None,
            )
            if obj is None:
                return None
            return {key: obj.get(key) for key in (
                "state", "trigger_mode", "trigger_script", "auto_script",
            )}

        before_object = object_snapshot(before)
        after_object = object_snapshot(after)
        changed = {}
        if before_object is None or after_object is None:
            if before_object != after_object:
                changed["existence"] = {
                    "before": before_object is not None,
                    "after": after_object is not None,
                }
        else:
            for key in ("state", "trigger_mode", "trigger_script", "auto_script"):
                if before_object.get(key) != after_object.get(key):
                    changed[key] = {
                        "before": before_object.get(key),
                        "after": after_object.get(key),
                    }

        before_trigger = before.get("trigger_observation") if isinstance(before.get("trigger_observation"), dict) else None
        after_trigger = after.get("trigger_observation") if isinstance(after.get("trigger_observation"), dict) else None
        trigger_changed = (
            isinstance(after_trigger, dict)
            and int(after_trigger.get("event_object_id") or 0) == object_id
            and after_trigger != before_trigger
        )
        if trigger_changed:
            changed["trigger_observation"] = {
                "before": before_trigger,
                "after": after_trigger,
            }

        same_room = (
            before.get("scene_id") == after.get("scene_id")
            and before.get("map_id") == after.get("map_id")
        )
        effect_observed = True if changed else False if same_room and after_object is not None else None
        return {
            "matched_event_object_id": object_id,
            "effect_observed": effect_observed,
            "object_delta": changed or {},
        }

    def navigation_environment(self, state: dict) -> dict:
        pos = state.get("party_position") or {}
        dialog = state.get("dialog") or {}
        identity = self.room_identity(state)
        objects = []
        for obj in state.get("objects") or []:
            if not obj.get("interactable"):
                continue
            objects.append(
                {
                    "id": int(obj.get("id") or 0),
                    "x": obj.get("x"),
                    "y": obj.get("y"),
                    "state": obj.get("state"),
                    "trigger_mode": obj.get("trigger_mode"),
                    "trigger_script": obj.get("trigger_script"),
                    "sprite_id": obj.get("sprite_id"),
                    "blocking": bool(obj.get("blocking")),
                    "interactable": bool(obj.get("interactable")),
                }
            )
        objects.sort(key=lambda obj: obj["id"])
        return {
            "room": {
                "scene_id": state.get("scene_id"),
                "map_id": state.get("map_id"),
                "logical_room_id": identity["id"] if identity else None,
                "logical_room_label": identity.get("logical_room_label") if identity else None,
                "key": (state.get("room") or {}).get("key"),
                "room_key": identity["room_key"] if identity else (state.get("room") or {}).get("key"),
                "region_key": identity["region_key"] if identity else state.get("region_key"),
                "legacy_room_id": identity.get("legacy_room_id") if identity else None,
                "legacy_nav_room_id": identity.get("legacy_nav_room_id") if identity else None,
            },
            "party_position": {
                "x": pos.get("x"),
                "y": pos.get("y"),
            },
            "dialog": {
                "active": bool(dialog.get("active")),
            },
            "objects": objects,
            "approach_requirements": self.item_approach_requirements(state),
        }

    def navigation_environment_payload(self, state: dict) -> dict:
        environment = self.navigation_environment(state)
        return {
            "environment": environment,
            "environment_key": json.dumps(
                environment,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }

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
            observed[obj_id] = {key: obj.get(key) for key in OBJECT_OBSERVATION_FIELDS}
            observed[obj_id]["interactable"] = bool(obj.get("interactable"))
            observed[obj_id]["blocking"] = bool(obj.get("blocking"))
        return observed

    def observed_party_sprites(self, state: dict) -> dict[int, dict]:
        observed = {}
        for index, role in enumerate(state.get("party") or []):
            if not isinstance(role, dict):
                continue
            try:
                role_id = int(role.get("role_id"))
                scene_sprite_id = int(role.get("scene_sprite_id"))
            except (TypeError, ValueError):
                continue
            observed[role_id] = {
                "party_index": index,
                "name": role.get("name"),
                "scene_sprite_id": scene_sprite_id,
            }
        return observed

    def record_state_events(self, state: dict) -> None:
        if state.get("status") in {"waiting_for_game", "state_parse_error"}:
            return

        current = {
            "cash": int(state.get("cash") or 0),
            "inventory": self.observed_inventory(state),
            "objects": self.observed_objects(state),
            "party_sprites": self.observed_party_sprites(state),
        }
        previous = self.last_observed_state
        self.last_observed_state = current
        if previous is None:
            return

        cash_delta = current["cash"] - previous["cash"]
        if cash_delta:
            self.append_event(state, "cash_changed", {"delta": cash_delta, "cash": current["cash"]})

        role_ids = set(previous.get("party_sprites", {})) & set(current["party_sprites"])
        for role_id in sorted(role_ids):
            before = previous["party_sprites"][role_id]
            after = current["party_sprites"][role_id]
            if before["scene_sprite_id"] != after["scene_sprite_id"]:
                self.append_event(
                    state,
                    "party_sprite_changed",
                    {
                        "role_id": role_id,
                        "party_index": after["party_index"],
                        "name": after["name"],
                        "from_scene_sprite_id": before["scene_sprite_id"],
                        "to_scene_sprite_id": after["scene_sprite_id"],
                    },
                )

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
                for key in OBJECT_OBSERVATION_FIELDS
                if key != "auto_script" and before.get(key) != after.get(key)
            }
            if changes:
                self.append_event(state, "object_changed", {"object_id": obj_id, "changes": changes})

    def inventory_delta(self, before: dict[int, dict], after: dict[int, dict]) -> list[dict]:
        changes = []
        for item_id in sorted(set(before) | set(after)):
            before_amount = int(before.get(item_id, {}).get("amount") or 0)
            after_amount = int(after.get(item_id, {}).get("amount") or 0)
            delta = after_amount - before_amount
            if delta:
                changes.append({"item_id": item_id, "delta": delta, "amount": after_amount})
        return changes

    def object_snapshot_for_interaction(self, state: dict, object_id: int) -> dict | None:
        obj = self.target_object(state, object_id)
        if obj is None:
            return None
        return {
            **{key: obj.get(key) for key in OBJECT_OBSERVATION_FIELDS},
            "interactable": bool(obj.get("interactable")),
            "blocking": bool(obj.get("blocking")),
        }

    def object_snapshot_delta(self, before: dict | None, after: dict | None) -> dict:
        if before is None and after is None:
            return {}
        if before is None:
            return {"presence": {"from": False, "to": True}}
        if after is None:
            return {"presence": {"from": True, "to": False}}
        changes = {}
        for key in sorted(set(before) | set(after)):
            if before.get(key) != after.get(key):
                changes[key] = {"from": before.get(key), "to": after.get(key)}
        return changes

    def mark_pending_interaction_observation(
        self,
        state: dict,
        object_id: int,
        *,
        via_target_id: int | None = None,
        action: dict | None = None,
        status: str = "interacting",
    ) -> str:
        if isinstance(self.pending_interaction_observation, dict):
            self.observations.cancel(
                self.pending_interaction_observation.get("observation_id"),
                "superseded",
            )
        identity = self.room_identity(state)
        payload = {
            "object_id": int(object_id),
            "via_target_id": int(via_target_id or object_id),
            "status": status,
            "sent_at": time.time(),
            "frame": self.int_frame(state.get("frame")),
            "logical_frame": self.int_frame(state.get("logical_frame", state.get("frame"))),
            "scene_id": state.get("scene_id"),
            "map_id": state.get("map_id"),
            "room_id": identity.get("id") if identity else None,
            "logical_room_id": identity.get("id") if identity else None,
            "room_key": identity.get("room_key") if identity else (state.get("room") or {}).get("key"),
            "region_key": identity.get("region_key") if identity else state.get("region_key"),
            "nav_room_id": identity.get("nav_room_id") if identity else None,
            "legacy_room_id": identity.get("legacy_room_id") if identity else None,
            "legacy_nav_room_id": identity.get("legacy_nav_room_id") if identity else None,
            "inventory": self.observed_inventory(state),
            "object": self.object_snapshot_for_interaction(state, int(object_id)),
            "action": action,
            "armed": action is not None,
        }
        observation_id = self.observations.register(
            "interaction",
            owner="navigation",
            timeout=60.0,
            payload=payload,
        )
        payload["observation_id"] = observation_id
        self.pending_interaction_observation = payload
        return observation_id

    def observe_pending_interaction_outcome(self, state: dict) -> dict | None:
        pending = self.pending_interaction_observation
        if not isinstance(pending, dict):
            return None
        try:
            object_id = int(pending.get("object_id") or 0)
        except (TypeError, ValueError):
            self.observations.cancel(pending.get("observation_id"), "invalid_interaction_observation")
            self.pending_interaction_observation = None
            return None
        if object_id <= 0:
            self.observations.cancel(pending.get("observation_id"), "invalid_interaction_observation")
            self.pending_interaction_observation = None
            return None
        if not pending.get("armed"):
            return None

        if self.action_pending(state):
            return None
        dialog = state.get("dialog") if isinstance(state.get("dialog"), dict) else {}
        if dialog.get("active"):
            return None

        current_frame = self.int_frame(state.get("logical_frame", state.get("frame")))
        start_frame = self.int_frame(pending.get("logical_frame", pending.get("frame")))
        if current_frame is not None and start_frame is not None and current_frame <= start_frame:
            return None

        current_inventory = self.observed_inventory(state)
        inventory_changes = self.inventory_delta(pending.get("inventory") or {}, current_inventory)
        finished_object = self.object_snapshot_for_interaction(state, object_id)
        object_changes = self.object_snapshot_delta(
            pending.get("object") if isinstance(pending.get("object"), dict) else None,
            finished_object,
        )
        room_changed = (
            state.get("scene_id") != pending.get("scene_id")
            or state.get("map_id") != pending.get("map_id")
            or (self.room_identity(state) or {}).get("id") != pending.get("room_id")
        )

        base_payload = {
            "object_id": object_id,
            "target_id": object_id,
            "via_target_id": pending.get("via_target_id"),
            "started_frame": pending.get("frame"),
            "started_logical_frame": pending.get("logical_frame"),
            "started_at": pending.get("sent_at"),
            "started_room_id": pending.get("room_id"),
            "started_nav_room_id": pending.get("nav_room_id"),
            "started_object": pending.get("object"),
            "finished_object": finished_object,
            "action": pending.get("action"),
        }
        if inventory_changes or object_changes or room_changed:
            payload = {
                **base_payload,
                "inventory_changes": inventory_changes,
                "object_changes": object_changes,
                "room_changed": room_changed,
            }
            self.append_event(state, "interaction_effect", payload)
            self.observations.complete(
                pending.get("observation_id"),
                {"type": "interaction_effect", "event": payload},
            )
            self.pending_interaction_observation = None
            return payload

        elapsed_frames = (current_frame - start_frame) if current_frame is not None and start_frame is not None else None
        elapsed_time = time.time() - float(pending.get("sent_at") or time.time())
        if (elapsed_frames is not None and elapsed_frames >= 8) or elapsed_time >= 0.7:
            payload = {
                **base_payload,
                "elapsed_frames": elapsed_frames,
                "elapsed_seconds": elapsed_time,
                "reason": "no_state_change",
            }
            self.append_event(state, "interaction_no_effect", payload)
            self.observations.complete(
                pending.get("observation_id"),
                {"type": "interaction_no_effect", "event": payload},
            )
            self.pending_interaction_observation = None
            return payload
        return None

    def object_history_view(self, state: dict, object_id: int, limit: int = 20) -> dict:
        current = self.target_object(state, object_id)
        current_payload = None
        if current is not None:
            current_payload = {
                key: current.get(key)
                for key in (
                    "id",
                    "x",
                    "y",
                    "sprite_id",
                    "state",
                    "trigger_mode",
                    "trigger_script",
                    "auto_script",
                    "blocking",
                    "interactable",
                    "vanish_time",
                )
            }
            current_payload["kind"] = trigger_kind(int(current.get("trigger_mode") or 0))
            current_payload["transition_candidate"] = transition_candidate(current)

        persistent = self.persistent_object_entry(state, object_id)
        history = []
        persistent_keys: set[str] = set()
        if persistent is not None:
            for detail in persistent.get("history") or []:
                if not isinstance(detail, dict):
                    continue
                history.append(detail)
                if detail.get("event_key"):
                    persistent_keys.add(str(detail.get("event_key")))
        for event in self.event_log:
            if int(event.get("object_id") or 0) != object_id:
                continue
            if self.object_event_key(event) in persistent_keys:
                continue
            history.append({**event, "source": "events"})
        for dialog in self.dialog_history:
            if int(dialog.get("event_object_id") or 0) != object_id:
                continue
            history.append(
                {
                    "source": "dialogs",
                    "type": "dialog",
                    "time": dialog.get("time"),
                    "frame": dialog.get("frame"),
                    "end_frame": dialog.get("end_frame"),
                    "scene_id": dialog.get("scene_id"),
                    "map_id": dialog.get("map_id"),
                    "room_id": dialog.get("room_id"),
                    "room_key": dialog.get("room_key"),
                    "region_key": dialog.get("region_key"),
                    "nav_room_id": dialog.get("nav_room_id"),
                    "event_object_id": dialog.get("event_object_id"),
                    "message_id": dialog.get("message_id"),
                    "message_ids": dialog.get("message_ids"),
                    "speaker_hint": dialog.get("speaker_hint"),
                    "text": dialog.get("text"),
                }
            )
        history.sort(key=lambda entry: (entry.get("time") is None, entry.get("time") or 0, entry.get("frame") or 0))
        rows = history[-limit:]
        return {
            "object_id": object_id,
            "current": current_payload,
            "interaction_summary": persistent,
            "history": rows,
            "total": len(history),
            "returned": len(rows),
        }

    def target_object(self, state: dict, target_id: int) -> dict | None:
        for obj in state.get("objects") or []:
            if int(obj.get("id") or 0) == target_id:
                return obj
        return None

    def grid_window_for_world_margin(
        self,
        grid: dict,
        target: tuple[int, int],
        x_margin: int,
        y_margin: int,
    ) -> tuple[int, int, int, int]:
        ox, oy = grid["origin"]
        sx, sy = grid["step"]
        if sx <= 0 or sy <= 0:
            return (0, grid["width"] - 1, 0, grid["height"] - 1)

        def lower_index(value: int, origin: int, step: int, margin: int, limit: int) -> int:
            return max(0, min(limit - 1, -(-((value - margin) - origin) // step)))

        def upper_index(value: int, origin: int, step: int, margin: int, limit: int) -> int:
            return max(0, min(limit - 1, ((value + margin) - origin) // step))

        return (
            lower_index(target[0], ox, sx, x_margin, grid["width"]),
            upper_index(target[0], ox, sx, x_margin, grid["width"]),
            lower_index(target[1], oy, sy, y_margin, grid["height"]),
            upper_index(target[1], oy, sy, y_margin, grid["height"]),
        )

    def touch_boundary_nodes_for_object(self, obj: dict, grid: dict) -> set[tuple[int, int]]:
        nodes: set[tuple[int, int]] = set()
        mode = int(obj.get("trigger_mode") or 0)
        radius = trigger_radius(mode)
        if radius <= 0:
            return nodes
        target = (int(obj.get("x") or 0), int(obj.get("y") or 0))
        x_min, x_max, y_min, y_max = self.grid_window_for_world_margin(
            grid,
            target,
            radius - 1,
            (radius - 1) // 2,
        )

        for y in range(y_min, y_max + 1):
            for x in range(x_min, x_max + 1):
                node = (x, y)
                if nav_bit(grid, node) and pal_distance(world_pos(grid, node), target) < radius:
                    nodes.add(node)
        return nodes

    def touch_boundary_nodes(
        self,
        state: dict,
        grid: dict,
        target_id: int | None,
        touch_nodes_by_id: dict[int, set[tuple[int, int]]] | None = None,
    ) -> set[tuple[int, int]]:
        nodes: set[tuple[int, int]] = set()
        if touch_nodes_by_id is not None:
            for object_id, object_nodes in touch_nodes_by_id.items():
                if int(object_id) == target_id:
                    continue
                nodes.update(object_nodes)
            return nodes
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

    def legacy_room_identity(self, state: dict) -> dict | None:
        if state.get("status") in {"waiting_for_game", "state_parse_error"}:
            return None
        scene_id = state.get("scene_id")
        map_id = state.get("map_id")
        if scene_id is None or map_id is None:
            return None
        room = state.get("room") if isinstance(state.get("room"), dict) else {}
        room_key = room.get("key") or "unknown"
        region_key = state.get("region_key") or room_key
        nav_room_id = f"{scene_id}/{map_id}/{room_key}"
        return {
            "id": f"{nav_room_id}/{region_key}",
            "scene_id": scene_id,
            "map_id": map_id,
            "room_key": room_key,
            "region_key": region_key,
            "nav_room_id": nav_room_id,
        }

    def logical_room_cut_nodes(self, state: dict, grid: dict) -> set[tuple[int, int]]:
        nodes: set[tuple[int, int]] = set()
        for obj in state.get("objects") or []:
            if not obj.get("interactable") or not transition_candidate(obj):
                continue
            nodes.update(self.touch_boundary_nodes_for_object(obj, grid))
        return nodes

    def logical_room_seed(
        self,
        grid: dict,
        current: tuple[int, int] | None,
        stable_nodes: set[tuple[int, int]],
    ) -> tuple[tuple[int, int] | None, bool]:
        if current in stable_nodes:
            return current, False
        if not stable_nodes:
            return None, current is not None
        if current is None:
            return sorted(stable_nodes)[0], True
        seed = min(
            stable_nodes,
            key=lambda node: (
                abs(node[0] - current[0]) + abs(node[1] - current[1]),
                pal_distance(world_pos(grid, node), world_pos(grid, current)),
                node[1],
                node[0],
            ),
        )
        return seed, True

    def logical_room_component(self, grid: dict, seed: tuple[int, int], stable_nodes: set[tuple[int, int]]) -> set[tuple[int, int]]:
        queue = deque([seed])
        component = {seed}
        while queue:
            node = queue.popleft()
            for delta in MOVE_DELTAS.values():
                next_node = (node[0] + delta[0], node[1] + delta[1])
                if next_node in component or next_node not in stable_nodes:
                    continue
                component.add(next_node)
                queue.append(next_node)
        return component

    def logical_room_component_for_state(self, state: dict, grid: dict) -> tuple[set[tuple[int, int]], set[tuple[int, int]], tuple[int, int] | None]:
        passable = {
            (x, y)
            for y in range(grid["height"])
            for x in range(grid["width"])
            if nav_bit(grid, (x, y))
        }
        portal_cut = self.logical_room_cut_nodes(state, grid) & passable
        stable_nodes = passable - portal_cut
        pos = state.get("party_position") if isinstance(state.get("party_position"), dict) else {}
        current = grid_node(grid, int(pos.get("x") or 0), int(pos.get("y") or 0))
        seed, _ = self.logical_room_seed(grid, current, stable_nodes)
        component = self.logical_room_component(grid, seed, stable_nodes) if seed is not None else set()
        if not component and current in passable:
            component = {current}
        return component, portal_cut, current

    def static_room_component_for_state(
        self,
        state: dict,
        grid: dict,
    ) -> tuple[set[tuple[int, int]], tuple[int, int] | None]:
        """Return the native static walkable component without event-object cuts."""
        passable = {
            (x, y)
            for y in range(grid["height"])
            for x in range(grid["width"])
            if nav_bit(grid, (x, y))
        }
        pos = state.get("party_position") if isinstance(state.get("party_position"), dict) else {}
        current = grid_node(grid, int(pos.get("x") or 0), int(pos.get("y") or 0))
        seed, _ = self.logical_room_seed(grid, current, passable)
        component = self.logical_room_component(grid, seed, passable) if seed is not None else set()
        if not component and current in passable:
            component = {current}
        return component, current

    def logical_room_hash(self, scene_id, map_id, grid: dict, nodes: set[tuple[int, int]]) -> str:
        absolute_nodes = [list(world_pos(grid, node)) for node in sorted(nodes, key=lambda item: (item[1], item[0]))]
        payload = [scene_id, map_id, absolute_nodes]
        return hashlib.sha256(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()

    def room_state_stable(self, state: dict) -> bool:
        return (
            isinstance(state, dict)
            and state.get("status") not in {"waiting_for_game", "state_parse_error", "startup_waiting"}
            and state.get("in_main_game") is not False
            and not state.get("entering_scene")
            and decode_nav_grid(state) is not None
        )

    def logical_room_identity(self, state: dict) -> dict | None:
        if not self.room_state_stable(state):
            return None
        cached = state.get("_logical_room") if isinstance(state.get("_logical_room"), dict) else None
        if cached is not None and cached.get("id"):
            return cached
        legacy = self.legacy_room_identity(state)
        if legacy is None:
            return None
        scene_id = legacy["scene_id"]
        map_id = legacy["map_id"]
        grid = decode_nav_grid(state)
        if grid is None:
            payload = [scene_id, map_id, legacy["id"]]
            hash12 = hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()[:12].upper()
            logical_room_id = f"S{scene_id}/M{map_id}:{hash12}"
            return {
                "id": logical_room_id,
                "label": logical_room_id,
                "scene_id": scene_id,
                "map_id": map_id,
                "hash12": hash12,
                "algorithm": "static-component-v1",
                "fallback": "legacy_no_nav_grid",
                "component_size": 0,
                "portal_cut_size": 0,
                "seed_adjusted": False,
                "seed_node": None,
                "current_node": None,
                "legacy_room_id": legacy["id"],
                "legacy_nav_room_id": legacy["nav_room_id"],
                "room_key": legacy["room_key"],
                "region_key": legacy["region_key"],
            }

        component, current = self.static_room_component_for_state(state, grid)
        portal_cut = self.logical_room_cut_nodes(state, grid) & component
        seed_payload = None
        seed_adjusted = False
        if component:
            if current in component:
                seed = current
            else:
                seed, seed_adjusted = self.logical_room_seed(grid, current, component)
            seed_payload = node_payload(grid, seed) if seed is not None else None
        hash12 = self.logical_room_hash(scene_id, map_id, grid, component)[:12].upper()
        logical_room_id = f"S{scene_id}/M{map_id}:{hash12}"
        return {
            "id": logical_room_id,
            "label": logical_room_id,
            "scene_id": scene_id,
            "map_id": map_id,
            "hash12": hash12,
            "algorithm": "static-component-v1",
            "fallback": None,
            "component_size": len(component),
            "portal_cut_size": len(portal_cut),
            "seed_adjusted": seed_adjusted,
            "seed_node": seed_payload,
            "current_node": node_payload(grid, current) if current is not None else None,
            "legacy_room_id": legacy["id"],
            "legacy_nav_room_id": legacy["nav_room_id"],
            "room_key": legacy["room_key"],
            "region_key": legacy["region_key"],
        }

    def attach_logical_room_fields(self, state: dict) -> None:
        if not self.room_state_stable(state):
            for key in ("_logical_room", "logical_room_id", "logical_room_label", "legacy_room_id", "legacy_nav_room_id"):
                state.pop(key, None)
            return
        identity = self.logical_room_identity(state)
        if identity is None:
            return
        state["_logical_room"] = identity
        state["logical_room_id"] = identity["id"]
        state["logical_room_label"] = identity["label"]
        state["legacy_room_id"] = identity["legacy_room_id"]
        state["legacy_nav_room_id"] = identity["legacy_nav_room_id"]

    def room_identity(self, state: dict) -> dict | None:
        logical = self.logical_room_identity(state)
        if logical is None:
            return None
        return {
            "id": logical["id"],
            "scene_id": logical["scene_id"],
            "map_id": logical["map_id"],
            "room_key": logical["room_key"],
            "region_key": logical["region_key"],
            "nav_room_id": logical["id"],
            "logical_room_id": logical["id"],
            "logical_room_label": logical["label"],
            "legacy_room_id": logical["legacy_room_id"],
            "legacy_nav_room_id": logical["legacy_nav_room_id"],
        }

    def empty_exploration(self) -> dict:
        return {
            "version": EXPLORATION_VERSION,
            "rooms": {},
            "transitions": {},
            "object_interactions": {},
            "tasks": {},
        }

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
        transitions = data.get("transitions")
        if not isinstance(transitions, dict):
            data["transitions"] = {}
        object_interactions = data.get("object_interactions")
        if not isinstance(object_interactions, dict):
            data["object_interactions"] = {}
        # Tasks are companion memory inferred by the agent, rather than game facts.
        # Keep current exploration files usable when the additive task ledger is absent.
        tasks = data.get("tasks")
        if not isinstance(tasks, dict):
            data["tasks"] = {}
        return data

    def write_exploration(self, data: dict) -> None:
        self.atomic_write_text(
            self.exploration_path,
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def task_view(self, task: dict, *, include_history: bool = True) -> dict:
        """Return a compact, JSON-safe task record without treating it as engine state."""
        result = {
            key: task.get(key)
            for key in (
                "id", "description", "priority", "status", "object_id",
                "transition_key", "room_id", "rationale", "created_at",
                "updated_at", "created_agent_turn", "updated_agent_turn",
                "last_reviewed_at", "last_reviewed_agent_turn",
            )
        }
        result["last_reviewed_at"] = task.get("last_reviewed_at", task.get("updated_at"))
        result["last_reviewed_agent_turn"] = task.get(
            "last_reviewed_agent_turn", task.get("updated_agent_turn")
        )
        if include_history:
            result["history"] = list(task.get("history") or [])[-MAX_TASK_HISTORY:]
        return result

    def task_ledger(self, *, include_closed: bool = False, limit: int | None = 20) -> list[dict]:
        with self.exploration_lock:
            data = self.read_exploration()
            tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
            records = [task for task in tasks.values() if isinstance(task, dict)]
        if not include_closed:
            records = [task for task in records if task.get("status") == "active"]
        def sort_key(task: dict) -> tuple[int, float, str]:
            try:
                priority = int(task.get("priority") or 0)
            except (TypeError, ValueError):
                priority = 0
            try:
                updated_at = float(task.get("updated_at") or 0)
            except (TypeError, ValueError):
                updated_at = 0
            return (-priority, -updated_at, str(task.get("id") or ""))
        records.sort(key=sort_key)
        selected = records if limit is None else records[:max(0, int(limit))]
        return [self.task_view(task) for task in selected]

    def agent_tasks_view(self) -> dict:
        grouped = {status: [] for status in ("active", "completed", "abandoned")}
        for task in self.task_ledger(include_closed=True, limit=None):
            status = task.get("status")
            if status not in grouped:
                continue
            grouped[status].append({
                key: task.get(key)
                for key in ("id", "description", "priority", "status")
            })
        tasks = [
            task
            for status in ("active", "completed", "abandoned")
            for task in grouped[status]
        ]
        counts = {status: len(records) for status, records in grouped.items()}
        counts["total"] = len(tasks)
        return {"ok": True, "counts": counts, "tasks": tasks}

    def task_review_snapshot(self, state: dict | None = None) -> dict:
        """Capture net game state used to decide whether a task needs review."""
        state = state if isinstance(state, dict) else self.read_live_state()
        inventory = self.observed_inventory(state)
        sprites = self.observed_party_sprites(state)
        return {
            "cash": int(state.get("cash") or 0),
            "inventory": {
                str(item_id): int(item.get("amount") or 0)
                for item_id, item in inventory.items()
            },
            "party_sprites": {
                str(role_id): int(role.get("scene_sprite_id") or 0)
                for role_id, role in sprites.items()
            },
        }

    @staticmethod
    def task_evidence_time(entry: dict) -> float | None:
        for key in ("updated_at", "time", "observed_at"):
            try:
                return float(entry.get(key))
            except (TypeError, ValueError):
                continue
        return None

    def task_review_for(self, task: dict, state: dict | None = None) -> dict | None:
        if task.get("status") != "active":
            return None
        try:
            reviewed_at = float(task.get("last_reviewed_at") or task.get("updated_at") or 0)
        except (TypeError, ValueError):
            reviewed_at = 0.0
        reasons: list[dict] = []
        latest = reviewed_at

        try:
            object_id = int(task.get("object_id") or 0)
        except (TypeError, ValueError):
            object_id = 0
        if object_id > 0:
            for entry in self.dialog_history:
                observed_at = self.task_evidence_time(entry)
                if observed_at is None or observed_at <= reviewed_at:
                    continue
                try:
                    matches = int(entry.get("event_object_id") or 0) == object_id
                except (TypeError, ValueError):
                    matches = False
                if matches:
                    reasons.append({"kind": "dialog", "object_id": object_id})
                    latest = max(latest, observed_at)
                    break
            for entry in self.event_log:
                if entry.get("type") not in {"object_changed", "interaction_effect", "interaction_no_effect"}:
                    continue
                observed_at = self.task_evidence_time(entry)
                if observed_at is None or observed_at <= reviewed_at:
                    continue
                try:
                    entry_id = int(entry.get("object_id") or entry.get("target_id") or 0)
                except (TypeError, ValueError):
                    entry_id = 0
                if entry_id == object_id:
                    reasons.append({"kind": str(entry.get("type")), "object_id": object_id})
                    latest = max(latest, observed_at)
                    break

        transition_key = task.get("transition_key")
        room_id = task.get("room_id")
        if transition_key or room_id:
            data = self.read_exploration()
            transitions = data.get("transitions") if isinstance(data.get("transitions"), dict) else {}
            for by_room in transitions.values():
                if not isinstance(by_room, dict):
                    continue
                for record in by_room.values():
                    if not isinstance(record, dict):
                        continue
                    observed_at = self.task_evidence_time(record)
                    if observed_at is None or observed_at <= reviewed_at:
                        continue
                    key_match = bool(transition_key) and record.get("transition_key") == transition_key
                    room_match = bool(room_id) and str(room_id) in {
                        str(record.get("from_room_id")), str(record.get("from_logical_room_id")),
                        str(record.get("to_room_id")), str(record.get("to_logical_room_id")),
                    }
                    if key_match or room_match:
                        reasons.append({
                            "kind": "transition",
                            "transition_key": record.get("transition_key"),
                            "room_id": room_id if room_match else None,
                        })
                        latest = max(latest, observed_at)
                        break

        current = self.task_review_snapshot(state)
        baseline = task.get("_review_snapshot") if isinstance(task.get("_review_snapshot"), dict) else {}
        if not baseline:
            # Reconstruct the review-time net state for tasks created before
            # snapshots were introduced. This is intentionally numeric only.
            baseline = json.loads(json.dumps(current))
            reconstructed_sprite_roles: set[str] = set()
            for entry in self.event_log:
                observed_at = self.task_evidence_time(entry)
                if observed_at is None or observed_at <= reviewed_at:
                    continue
                event_type = entry.get("type")
                if event_type == "cash_changed":
                    try:
                        baseline["cash"] -= int(entry.get("delta") or 0)
                        latest = max(latest, observed_at)
                    except (TypeError, ValueError):
                        pass
                elif event_type == "inventory_changed":
                    try:
                        item_id = str(int(entry.get("item_id") or 0))
                        baseline["inventory"][item_id] = int(baseline["inventory"].get(item_id) or 0) - int(entry.get("delta") or 0)
                        latest = max(latest, observed_at)
                    except (TypeError, ValueError):
                        pass
                elif event_type == "party_sprite_changed":
                    try:
                        role_id = str(int(entry.get("role_id")))
                        if role_id not in reconstructed_sprite_roles:
                            baseline["party_sprites"][role_id] = int(entry.get("from_scene_sprite_id"))
                            reconstructed_sprite_roles.add(role_id)
                        latest = max(latest, observed_at)
                    except (TypeError, ValueError):
                        pass
        if baseline:
            if int(current.get("cash") or 0) != int(baseline.get("cash") or 0):
                reasons.append({
                    "kind": "cash_net_changed",
                    "from": int(baseline.get("cash") or 0),
                    "to": int(current.get("cash") or 0),
                })
            before_inventory = baseline.get("inventory") if isinstance(baseline.get("inventory"), dict) else {}
            after_inventory = current.get("inventory") if isinstance(current.get("inventory"), dict) else {}
            changed_items = sorted(
                key for key in set(before_inventory) | set(after_inventory)
                if int(before_inventory.get(key) or 0) != int(after_inventory.get(key) or 0)
            )
            if changed_items:
                reasons.append({"kind": "inventory_net_changed", "item_ids": changed_items[:12]})
            before_sprites = baseline.get("party_sprites") if isinstance(baseline.get("party_sprites"), dict) else {}
            after_sprites = current.get("party_sprites") if isinstance(current.get("party_sprites"), dict) else {}
            changed_roles = sorted(
                key for key in set(before_sprites) & set(after_sprites)
                if int(before_sprites.get(key) or 0) != int(after_sprites.get(key) or 0)
            )
            if changed_roles:
                reasons.append({
                    "kind": "party_sprite_net_changed",
                    "roles": [
                        {"role_id": role_id, "from": before_sprites.get(role_id), "to": after_sprites.get(role_id)}
                        for role_id in changed_roles[:8]
                    ],
                })
            if reasons and latest <= reviewed_at:
                latest = max(reviewed_at, float(self.live_info("state").get("mtime") or reviewed_at))

        if not reasons:
            return None
        return {"id": task.get("id"), "reasons": reasons, "latest_evidence_at": latest}

    def task_review_context(self, state: dict | None = None) -> dict:
        with self.exploration_lock:
            data = self.read_exploration()
            tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
            active = [task for task in tasks.values() if isinstance(task, dict) and task.get("status") == "active"]
        reviews = [review for task in active if (review := self.task_review_for(task, state)) is not None]
        reviews.sort(key=lambda item: str(item.get("id") or ""))
        return {"required": bool(reviews), "tasks": reviews}

    def new_agent_tool_cycle(self, state: dict | None = None) -> dict:
        review = self.task_review_context(state)
        return {
            "tasks_queried": False,
            "required_task_ids": [str(item.get("id")) for item in review.get("tasks") or []],
            "reviewed_task_ids": [],
            "equipment_review_item_id": None,
        }

    def upsert_agent_task(self, arguments: dict) -> dict:
        task_id = str(arguments.get("id") or "").strip()
        status = arguments.get("status")
        if status is not None and status not in {"active", "completed", "abandoned"}:
            return {"ok": False, "error": "status must be active, completed, or abandoned"}
        now = time.time()
        turn = self.current_agent_turn()
        with self.exploration_lock:
            data = self.read_exploration()
            tasks = data.setdefault("tasks", {})
            existing = tasks.get(task_id) if task_id else None
            if not isinstance(existing, dict):
                existing = None
            if existing is None:
                description = arguments.get("description")
                rationale = arguments.get("rationale")
                if (
                    not isinstance(description, str)
                    or not description.strip()
                    or not isinstance(rationale, str)
                    or not rationale.strip()
                ):
                    if task_id:
                        return {
                            "ok": False,
                            "error": (
                                "unknown task id; creating it requires non-empty description and rationale; "
                                "call get_tasks to verify existing ids"
                            ),
                        }
                    return {
                        "ok": False,
                        "error": "creating a task requires non-empty description and rationale",
                    }
                if not task_id:
                    task_id = "task-" + hashlib.sha256(
                        f"{now}:{turn}:{description}".encode("utf-8")
                    ).hexdigest()[:16]
                existing = {
                    "id": task_id, "description": description.strip(), "rationale": rationale.strip(),
                    "priority": 0, "status": status or "active", "created_at": now,
                    "created_agent_turn": turn, "history": [],
                }
                operation = "created"
            else:
                operation = "updated"
            changed = {}
            for key in ("description", "rationale", "status", "transition_key", "room_id"):
                if key not in arguments:
                    continue
                value = arguments.get(key)
                if key in {"description", "rationale"}:
                    if not isinstance(value, str) or not value.strip():
                        return {"ok": False, "error": f"{key} must be a non-empty string"}
                    value = value.strip()
                elif key in {"transition_key", "room_id"} and value is not None:
                    if not isinstance(value, str) or not value.strip():
                        return {"ok": False, "error": f"{key} must be a non-empty string or null"}
                    value = value.strip()
                if existing.get(key) != value:
                    changed[key] = {"from": existing.get(key), "to": value}
                    existing[key] = value
            for key in ("priority", "object_id"):
                if key not in arguments:
                    continue
                try:
                    if isinstance(arguments.get(key), bool):
                        raise ValueError
                    value = int(arguments.get(key))
                except (TypeError, ValueError):
                    return {"ok": False, "error": f"{key} must be an integer"}
                if key == "object_id" and value <= 0:
                    return {"ok": False, "error": "object_id must be positive"}
                if existing.get(key) != value:
                    changed[key] = {"from": existing.get(key), "to": value}
                    existing[key] = value
            if not existing.get("history"):
                changed = {"created": True, **changed}
            existing["updated_at"] = now
            existing["updated_agent_turn"] = turn
            existing["last_reviewed_at"] = now
            existing["last_reviewed_agent_turn"] = turn
            existing["_review_snapshot"] = self.task_review_snapshot()
            existing.setdefault("history", []).append({
                "time": now, "agent_turn": turn, "changes": changed,
                "status": existing.get("status"),
            })
            existing["history"] = existing["history"][-MAX_TASK_HISTORY:]
            tasks[task_id] = existing
            self.write_exploration(data)
            return {"ok": True, "operation": operation, "task": self.task_view(existing)}

    def active_task_summary(self, limit: int = MAX_ACTIVE_TASK_CONTEXT) -> list[dict]:
        limit = max(0, min(int(limit), MAX_ACTIVE_TASK_CONTEXT))
        def compact_description(value) -> str:
            description = str(value or "")
            if len(description) <= MAX_ACTIVE_TASK_DESCRIPTION:
                return description
            return description[:MAX_ACTIVE_TASK_DESCRIPTION - 1] + "…"
        return [
            {
                **{key: task.get(key) for key in ("id", "priority", "object_id", "transition_key", "room_id")},
                "description": compact_description(task.get("description")),
            }
            for task in self.task_ledger(limit=limit)
        ]

    def active_task_context(self, state: dict | None = None) -> dict:
        return {
            "active_tasks": self.active_task_summary(),
            "active_tasks_total": len(self.task_ledger(limit=None)),
            "task_review": self.task_review_context(state),
        }

    def object_event_key(self, event: dict) -> str:
        payload = {
            key: event.get(key)
            for key in (
                "type",
                "time",
                "frame",
                "logical_frame",
                "scene_id",
                "map_id",
                "logical_room_id",
                "room_id",
                "room_key",
                "object_id",
                "target_id",
                "changes",
                "object_changes",
                "inventory_changes",
                "started_frame",
                "started_logical_frame",
                "reason",
            )
            if key in event
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def object_event_room_id(self, event: dict, state: dict | None = None) -> str | None:
        for key in ("logical_room_id", "room_id"):
            value = event.get(key)
            if is_logical_room_id(value):
                return value
        if isinstance(state, dict):
            identity = self.room_identity(state)
            if identity is not None:
                return identity["id"]
        return None

    def snapshot_after_object_changes(self, before: dict | None, changes: dict | None) -> dict | None:
        if not isinstance(before, dict) and not isinstance(changes, dict):
            return None
        snapshot = dict(before or {})
        for key, change in (changes or {}).items():
            if isinstance(change, dict) and "to" in change:
                snapshot[key] = change.get("to")
        return snapshot

    def record_persistent_object_event(
        self,
        event: dict,
        *,
        state: dict | None = None,
        recovered: bool = False,
    ) -> bool:
        event_type = event.get("type")
        cleared_blocker = self.object_event_cleared_blocker(event)
        if event_type not in {"interaction_effect", "interaction_no_effect"} and not cleared_blocker:
            return False
        try:
            object_id = int(event.get("object_id") or event.get("target_id") or 0)
        except (TypeError, ValueError):
            return False
        room_id = self.object_event_room_id(event, state)
        if object_id <= 0 or not room_id:
            return False

        before = event.get("started_object") if isinstance(event.get("started_object"), dict) else None
        after = event.get("finished_object") if isinstance(event.get("finished_object"), dict) else None
        if after is None and event_type == "interaction_effect":
            after = self.snapshot_after_object_changes(before, event.get("object_changes"))
        if after is None and event_type == "interaction_no_effect":
            after = dict(before) if isinstance(before, dict) else None
        if isinstance(state, dict):
            current = self.object_snapshot_for_interaction(state, object_id)
            if current is not None:
                after = current
        if cleared_blocker and after is None:
            after = self.snapshot_after_object_changes(None, event.get("changes"))

        event_key = self.object_event_key(event)
        detail = {
            **event,
            "source": "agent_llm_log" if recovered else "exploration",
            "event_key": event_key,
        }
        with self.exploration_lock:
            data = self.read_exploration()
            rooms = data.setdefault("object_interactions", {})
            objects = rooms.setdefault(room_id, {})
            entry = objects.setdefault(
                str(object_id),
                {
                    "object_id": object_id,
                    "room_id": room_id,
                    "attempt_count": 0,
                    "effect_count": 0,
                    "no_effect_count": 0,
                    "cleared_blocker": False,
                    "history": [],
                },
            )
            history = entry.get("history") if isinstance(entry.get("history"), list) else []
            if any(item.get("event_key") == event_key for item in history if isinstance(item, dict)):
                return False
            if event_type in {"interaction_effect", "interaction_no_effect"}:
                entry["attempt_count"] = int(entry.get("attempt_count") or 0) + 1
                count_key = "effect_count" if event_type == "interaction_effect" else "no_effect_count"
                entry[count_key] = int(entry.get(count_key) or 0) + 1
                entry["last_result"] = "effect" if event_type == "interaction_effect" else "no_effect"
                entry["last_event"] = detail
                entry["last_before"] = before
                entry["last_after"] = after
            if cleared_blocker:
                entry["cleared_blocker"] = True
                entry["cleared_at"] = event.get("time")
                entry["cleared_frame"] = event.get("logical_frame", event.get("frame"))
                entry["cleared_snapshot"] = after
            entry["trigger_script"] = (after or before or {}).get("trigger_script", entry.get("trigger_script"))
            entry["updated_at"] = event.get("time") or time.time()
            history.append(detail)
            entry["history"] = history[-MAX_OBJECT_INTERACTION_HISTORY:]
            objects[str(object_id)] = entry
            self.write_exploration(data)
        return True

    def persistent_object_entry(self, state: dict, object_id: int) -> dict | None:
        identity = self.room_identity(state)
        if identity is None:
            return None
        data = self.read_exploration()
        rooms = data.get("object_interactions") if isinstance(data.get("object_interactions"), dict) else {}
        objects = rooms.get(identity["id"]) if isinstance(rooms.get(identity["id"]), dict) else {}
        entry = objects.get(str(object_id)) if isinstance(objects, dict) else None
        return entry if isinstance(entry, dict) else None

    def object_snapshot_compatible(self, current: dict | None, recorded: dict | None) -> bool:
        if not isinstance(current, dict) or not isinstance(recorded, dict):
            return False
        compared = False
        for key in OBJECT_INTERACTION_COMPATIBILITY_FIELDS:
            if key not in recorded or recorded.get(key) is None:
                continue
            compared = True
            if current.get(key) != recorded.get(key):
                return False
        return compared

    def recover_object_interactions_from_agent_llm_log(self) -> int:
        try:
            stat = self.agent_llm_log_path.stat()
        except FileNotFoundError:
            return 0
        with self.exploration_lock:
            data = self.read_exploration()
            recovery = data.get("object_interaction_recovery")
            if not isinstance(recovery, dict):
                recovery = {}
            if (
                recovery.get("version") == 2
                and
                recovery.get("log_size") == stat.st_size
                and recovery.get("log_mtime_ns") == stat.st_mtime_ns
            ):
                return 0
            recovered_keys = set(recovery.get("event_keys") or [])

        recovered = 0
        try:
            lines = self.agent_llm_log_path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, UnicodeDecodeError):
            return 0
        for line in lines:
            try:
                log_entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            request = log_entry.get("request") if isinstance(log_entry, dict) else None
            messages = request.get("messages") if isinstance(request, dict) else None
            if not isinstance(messages, list):
                continue
            state_context = log_entry.get("state_context") if isinstance(log_entry.get("state_context"), dict) else {}
            for message in messages:
                if not isinstance(message, dict) or message.get("role") != "tool":
                    continue
                try:
                    result = json.loads(message.get("content") or "")
                except (TypeError, json.JSONDecodeError):
                    continue
                history_current = (
                    result.get("current")
                    if message.get("name") == "inspect_target" and isinstance(result.get("current"), dict)
                    else None
                )

                def structured_events(value):
                    if isinstance(value, dict):
                        if value.get("type") in {"interaction_effect", "interaction_no_effect", "object_changed"}:
                            yield value
                        for nested in value.values():
                            yield from structured_events(nested)
                    elif isinstance(value, list):
                        for nested in value:
                            yield from structured_events(nested)

                for raw_event in structured_events(result):
                    if not isinstance(raw_event, dict):
                        continue
                    event = dict(raw_event)
                    event.pop("source", None)
                    if event.get("type") not in {"interaction_effect", "interaction_no_effect", "object_changed"}:
                        continue
                    if event.get("type") == "object_changed" and not self.object_event_cleared_blocker(event):
                        continue
                    if history_current is not None:
                        try:
                            history_object_id = int(result.get("object_id") or history_current.get("id") or 0)
                            event_object_id = int(event.get("object_id") or event.get("target_id") or 0)
                        except (TypeError, ValueError):
                            history_object_id = event_object_id = 0
                        if history_object_id > 0 and history_object_id == event_object_id:
                            event.setdefault("finished_object", {
                                key: history_current.get(key)
                                for key in OBJECT_OBSERVATION_FIELDS
                                if key in history_current
                            } | {
                                "interactable": bool(history_current.get("interactable")),
                                "blocking": bool(history_current.get("blocking")),
                            })
                    for key in (
                        "scene_id", "map_id", "logical_room_id", "room_id", "room_key",
                        "region_key", "nav_room_id", "logical_frame", "frame",
                    ):
                        if event.get(key) is None and state_context.get(key) is not None:
                            event[key] = state_context.get(key)
                    event_key = self.object_event_key(event)
                    if event_key in recovered_keys:
                        continue
                    if self.record_persistent_object_event(event, recovered=True):
                        recovered += 1
                    recovered_keys.add(event_key)

        with self.exploration_lock:
            data = self.read_exploration()
            data["object_interaction_recovery"] = {
                "version": 2,
                "log_size": stat.st_size,
                "log_mtime_ns": stat.st_mtime_ns,
                "event_keys": sorted(recovered_keys),
                "recovered_at": time.time(),
            }
            self.write_exploration(data)
        return recovered

    def room_exploration_summary_from_data(self, data: dict, room_id: str | None) -> dict:
        if not room_id:
            return {
                "known": False,
                "room_id": None,
                "scene_id": None,
                "map_id": None,
                "logical_room_id": None,
                "logical_room_label": None,
                "room_key": None,
                "region_key": None,
                "nav_room_id": None,
                "legacy_room_id": None,
                "legacy_nav_room_id": None,
                "complete": False,
                "explored": 0,
                "explorable": 0,
                "frontier": 0,
                "updated_at": None,
            }
        rooms = data.get("rooms") if isinstance(data.get("rooms"), dict) else {}
        room = rooms.get(room_id) if isinstance(rooms, dict) else None
        if not isinstance(room, dict):
            return {
                "known": False,
                "room_id": room_id,
                "scene_id": None,
                "map_id": None,
                "logical_room_id": room_id,
                "logical_room_label": None,
                "room_key": None,
                "region_key": None,
                "nav_room_id": None,
                "legacy_room_id": None,
                "legacy_nav_room_id": None,
                "complete": False,
                "explored": 0,
                "explorable": 0,
                "frontier": 0,
                "updated_at": None,
            }
        return {
            "known": True,
            "room_id": room_id,
            "scene_id": room.get("scene_id"),
            "map_id": room.get("map_id"),
            "logical_room_id": room.get("logical_room_id") or room_id,
            "logical_room_label": room.get("logical_room_label"),
            "room_key": room.get("room_key"),
            "region_key": room.get("region_key"),
            "nav_room_id": room.get("nav_room_id"),
            "legacy_room_id": room.get("legacy_room_id"),
            "legacy_nav_room_id": room.get("legacy_nav_room_id"),
            "complete": bool(room.get("complete")),
            "explored": len(room.get("visited") or []),
            "explorable": int(room.get("explorable") or 0),
            "frontier": len(room.get("frontier") or []),
            "updated_at": room.get("updated_at"),
        }

    def destination_priority(self, summary: dict) -> str:
        if not summary.get("known"):
            return "unknown"
        if summary.get("complete"):
            return "complete"
        if int(summary.get("explored") or 0) <= 0:
            return "unexplored"
        return "partial"

    def transition_object_payload(self, obj: dict | None, object_id: int) -> dict:
        obj = obj if isinstance(obj, dict) else {}
        return {
            "id": int(obj.get("id") or object_id),
            "x": self.int_or_none(obj.get("x")),
            "y": self.int_or_none(obj.get("y")),
            "trigger_mode": self.int_or_none(obj.get("trigger_mode")),
            "trigger_script": self.int_or_none(obj.get("trigger_script")),
            "auto_script": self.int_or_none(obj.get("auto_script")),
            "sprite_id": self.int_or_none(obj.get("sprite_id")),
            "blocking": bool(obj.get("blocking")),
            "interactable": bool(obj.get("interactable")),
        }

    def transition_key_fields(self, identity: dict, obj: dict | None, object_id: int) -> dict:
        payload = self.transition_object_payload(obj, object_id)
        return {
            "scene_id": identity.get("scene_id"),
            "map_id": identity.get("map_id"),
            "x": payload.get("x"),
            "y": payload.get("y"),
            "trigger_mode": payload.get("trigger_mode"),
            "trigger_script": payload.get("trigger_script"),
        }

    def transition_key_from_fields(self, fields: dict) -> str:
        values = [fields.get(key) for key in TRANSITION_KEY_FIELDS]
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"))

    def transition_observed_state(self, state: dict | None, identity: dict | None = None) -> dict | None:
        if not isinstance(state, dict):
            return None
        identity = identity or self.room_identity(state)
        pos = state.get("party_position") if isinstance(state.get("party_position"), dict) else {}
        dialog = state.get("dialog") if isinstance(state.get("dialog"), dict) else {}
        latest = dialog.get("latest_event") if isinstance(dialog.get("latest_event"), dict) else {}
        room = state.get("room") if isinstance(state.get("room"), dict) else {}
        return {
            "scene_id": state.get("scene_id"),
            "map_id": state.get("map_id"),
            "room_id": identity.get("id") if identity else None,
            "logical_room_id": identity.get("id") if identity else None,
            "logical_room_label": identity.get("logical_room_label") if identity else None,
            "room_key": identity.get("room_key") if identity else room.get("key"),
            "region_key": identity.get("region_key") if identity else state.get("region_key"),
            "nav_room_id": identity.get("nav_room_id") if identity else None,
            "legacy_room_id": identity.get("legacy_room_id") if identity else None,
            "legacy_nav_room_id": identity.get("legacy_nav_room_id") if identity else None,
            "party_position": {"x": pos.get("x"), "y": pos.get("y")},
            "party_direction": state.get("party_direction"),
            "frame": state.get("frame"),
            "logical_frame": state.get("logical_frame", state.get("frame")),
            "raw_frame": state.get("raw_frame"),
            "dialog": {
                "active": bool(dialog.get("active")),
                "latest_text": latest.get("text"),
                "message_id": latest.get("message_id"),
                "event_object_id": latest.get("event_object_id"),
            },
        }

    def transition_observation_from_state(self, state: dict, object_id: int) -> dict | None:
        identity = self.room_identity(state)
        if identity is None:
            return None
        obj = self.target_object(state, object_id)
        fields = self.transition_key_fields(identity, obj, object_id)
        return {
            "from_room": identity,
            "object": self.transition_object_payload(obj, object_id),
            "transition_key": self.transition_key_from_fields(fields),
            "transition_key_fields": fields,
            "observed_from_state": self.transition_observed_state(state, identity),
        }

    def transition_semantic_room_key(self, scene_id, map_id, semantic_room) -> str | None:
        name = str(semantic_room or "").strip()
        if not name:
            return None
        return json.dumps([scene_id, map_id, name], ensure_ascii=False, separators=(",", ":"))

    def recent_semantic_room_keys(self, data: dict, recent_room_ids: set[str]) -> set[str]:
        keys: set[str] = set()
        transitions = data.get("transitions") if isinstance(data.get("transitions"), dict) else {}
        for by_room in transitions.values():
            if not isinstance(by_room, dict):
                continue
            for record in by_room.values():
                if not isinstance(record, dict):
                    continue
                if str(record.get("from_room_id")) in recent_room_ids:
                    key = self.transition_semantic_room_key(
                        record.get("from_scene_id"),
                        record.get("from_map_id"),
                        record.get("from_semantic_room"),
                    )
                    if key:
                        keys.add(key)
                if str(record.get("to_room_id")) in recent_room_ids:
                    key = self.transition_semantic_room_key(
                        record.get("to_scene_id"),
                        record.get("to_map_id"),
                        record.get("to_semantic_room"),
                    )
                    if key:
                        keys.add(key)
        return keys

    def transition_record_for_object(
        self,
        data: dict,
        from_logical_room_id: str | None,
        object_id: int,
        state: dict | None = None,
        transition_key: str | None = None,
    ) -> dict | None:
        if not from_logical_room_id:
            return None
        transitions = data.get("transitions") if isinstance(data, dict) else {}
        by_room = transitions.get(from_logical_room_id) if isinstance(transitions, dict) else None
        if not isinstance(by_room, dict):
            return None
        exact_lookup = transition_key is not None or isinstance(state, dict)
        if transition_key is None and isinstance(state, dict):
            observation = self.transition_observation_from_state(state, object_id)
            transition_key = observation.get("transition_key") if isinstance(observation, dict) else None
        if transition_key and isinstance(by_room.get(transition_key), dict):
            return by_room[transition_key]
        if exact_lookup:
            return None
        matches = [
            record
            for record in by_room.values()
            if isinstance(record, dict) and int(record.get("object_id") or 0) == int(object_id)
        ]
        return matches[0] if len(matches) == 1 and state is None else None

    def recent_room_ids(self, state: dict, limit: int = 8) -> list[str]:
        identity = self.room_identity(state)
        current_room_id = identity["id"] if identity else None
        recent: list[str] = []
        seen: set[str] = set()
        for entry in reversed(self.read_agent_log(limit=limit)):
            room_id = entry.get("episode")
            if not isinstance(room_id, str) or not room_id:
                continue
            if room_id == current_room_id or room_id in seen:
                continue
            seen.add(room_id)
            recent.append(room_id)
        return recent

    def transition_destination_payload(
        self,
        data: dict,
        from_logical_room_id: str | None,
        object_id: int,
        recent_room_ids: list[str] | None = None,
        state: dict | None = None,
        transition_key: str | None = None,
        recent_semantic_room_keys: set[str] | None = None,
    ) -> dict:
        recent = set(recent_room_ids or [])
        payload = {
            "known_destination": False,
            "known_destination_room_id": None,
            "known_destination_observed_at": None,
            "known_destination_transition_key": None,
            "destination_exploration": self.room_exploration_summary_from_data(data, None),
            "destination_priority": "unknown",
            "returns_to_recent_room": False,
        }
        if not from_logical_room_id:
            return payload
        record = self.transition_record_for_object(data, from_logical_room_id, object_id, state, transition_key)
        if not isinstance(record, dict) or not record.get("to_room_id"):
            return payload
        if record.get("confirmation") not in {"native_trigger", "human_confirmed"}:
            payload["unconfirmed_destination"] = True
            return payload
        destination = self.room_exploration_summary_from_data(data, str(record.get("to_room_id")))
        to_semantic_key = self.transition_semantic_room_key(
            record.get("to_scene_id"),
            record.get("to_map_id"),
            record.get("to_semantic_room"),
        )
        returns_to_recent_room = str(record.get("to_room_id")) in recent
        if not returns_to_recent_room and to_semantic_key:
            semantic_keys = recent_semantic_room_keys
            if semantic_keys is None:
                semantic_keys = self.recent_semantic_room_keys(data, recent)
            returns_to_recent_room = to_semantic_key in semantic_keys
        payload.update(
            {
                "known_destination": True,
                "known_destination_room_id": record.get("to_room_id"),
                "known_destination_observed_at": record.get("observed_at"),
                "known_destination_transition_key": record.get("transition_key"),
                "destination_exploration": destination,
                "destination_priority": self.destination_priority(destination),
                "returns_to_recent_room": returns_to_recent_room,
                "transition_key": record.get("transition_key"),
                "transition_key_fields": record.get("transition_key_fields"),
                "from_semantic_room": record.get("from_semantic_room"),
                "to_semantic_room": record.get("to_semantic_room"),
                "confidence": record.get("confidence"),
                "intra_room_transition": bool(record.get("intra_room_transition")),
                "from_location_id": record.get("from_location_id") or record.get("from_room_id"),
                "to_location_id": record.get("to_location_id") or record.get("to_room_id"),
                "from_subregion_id": record.get("from_subregion_id"),
                "to_subregion_id": record.get("to_subregion_id"),
                "native_destination": record.get("native_destination"),
            }
        )
        if returns_to_recent_room:
            payload["agent_priority"] = "avoid_backtrack"
            payload["deprioritize_reason"] = "该传送目标会回到最近访问过的房间。"
        return payload

    def navigation_transition_context(self, state: dict) -> dict:
        identity = self.room_identity(state)
        recent_room_ids = self.recent_room_ids(state)
        recent = set(recent_room_ids)
        with self.exploration_lock:
            data = self.read_exploration()
        return {
            "identity": identity,
            "data": data,
            "recent_room_ids": recent_room_ids,
            "recent_semantic_room_keys": self.recent_semantic_room_keys(data, recent),
        }

    def transition_destination_for_state(self, state: dict, object_id: int, context: dict | None = None) -> dict:
        context = context if isinstance(context, dict) else None
        identity = context.get("identity") if context else self.room_identity(state)
        data = context.get("data") if context else None
        recent_room_ids = context.get("recent_room_ids") if context else None
        recent_semantic_room_keys = context.get("recent_semantic_room_keys") if context else None
        obj = self.target_object(state, object_id)
        transition_key = None
        if identity is not None:
            transition_key = self.transition_key_from_fields(self.transition_key_fields(identity, obj, object_id))
        if data is None:
            with self.exploration_lock:
                data = self.read_exploration()
        if recent_room_ids is None:
            recent_room_ids = self.recent_room_ids(state)
        return self.transition_destination_payload(
            data,
            identity["id"] if identity else None,
            object_id,
            recent_room_ids,
            state if transition_key is None else None,
            transition_key,
            recent_semantic_room_keys,
        )

    def record_transition_destination(
        self,
        from_identity: dict,
        object_id: int,
        to_identity: dict,
        *,
        from_object: dict | None = None,
        from_state: dict | None = None,
        to_state: dict | None = None,
        transition_key: str | None = None,
        transition_key_fields: dict | None = None,
        observed_from_state: dict | None = None,
        confirmation: str = "human_confirmed",
        intra_room_transition: bool = False,
        native_destination: dict | None = None,
    ) -> dict:
        observed_at = time.time()
        transition_key_fields = transition_key_fields or self.transition_key_fields(from_identity, from_object, object_id)
        transition_key = transition_key or self.transition_key_from_fields(transition_key_fields)
        with self.exploration_lock:
            data = self.read_exploration()
            transitions = data.setdefault("transitions", {})
            by_room = transitions.setdefault(from_identity["id"], {})
            existing = by_room.get(transition_key) if isinstance(by_room.get(transition_key), dict) else {}
            record = {
                **existing,
                "transition_key": transition_key,
                "transition_key_fields": transition_key_fields,
                "object_id": int(object_id),
                "object": self.transition_object_payload(from_object, object_id),
                "from_room_id": from_identity["id"],
                "from_logical_room_id": from_identity["id"],
                "from_scene_id": from_identity["scene_id"],
                "from_map_id": from_identity["map_id"],
                "from_room_key": from_identity["room_key"],
                "from_region_key": from_identity["region_key"],
                "from_nav_room_id": from_identity["nav_room_id"],
                "from_legacy_room_id": from_identity.get("legacy_room_id"),
                "from_legacy_nav_room_id": from_identity.get("legacy_nav_room_id"),
                "to_room_id": to_identity["id"],
                "to_logical_room_id": to_identity["id"],
                "to_scene_id": to_identity["scene_id"],
                "to_map_id": to_identity["map_id"],
                "to_room_key": to_identity["room_key"],
                "to_region_key": to_identity["region_key"],
                "to_nav_room_id": to_identity["nav_room_id"],
                "to_legacy_room_id": to_identity.get("legacy_room_id"),
                "to_legacy_nav_room_id": to_identity.get("legacy_nav_room_id"),
                "observed": True,
                "confirmation": confirmation,
                "observed_at": observed_at,
                "observed_from_state": observed_from_state or self.transition_observed_state(from_state, from_identity),
                "observed_to_state": self.transition_observed_state(to_state, to_identity),
                "intra_room_transition": bool(intra_room_transition),
                "native_destination": native_destination,
            }
            if intra_room_transition:
                from_subregion_id = f".{int(object_id)}"
                nearest = self.nearest_transition_candidate(to_state or {}, native_destination)
                to_subregion_id = f".{nearest[0]}" if nearest else None
                record.update({
                    "from_subregion_id": from_subregion_id,
                    "to_subregion_id": to_subregion_id,
                    "from_location_id": f"{from_identity['id']}{from_subregion_id}",
                    "to_location_id": f"{to_identity['id']}{to_subregion_id}" if to_subregion_id else to_identity["id"],
                    "subregion_basis": "native_trigger_destination_nearest_active_transition" if nearest else None,
                    "subregion_match_distance": nearest[1] if nearest else None,
                })
            else:
                record.update({
                    "from_subregion_id": None,
                    "to_subregion_id": None,
                    "from_location_id": from_identity["id"],
                    "to_location_id": to_identity["id"],
                })
            if existing.get("to_room_id") and existing.get("to_room_id") != to_identity["id"]:
                previous = {key: value for key, value in existing.items() if key != "observations"}
                observations = existing.get("observations") if isinstance(existing.get("observations"), list) else []
                record["observations"] = [*observations, previous]
            by_room[transition_key] = record
            self.write_exploration(data)
        return record

    def annotate_transition(
        self,
        transition_key: str,
        *,
        from_semantic_room: str | None = None,
        to_semantic_room: str | None = None,
        confidence: str = "human_confirmed",
        notes: str | None = None,
    ) -> dict:
        transition_key = str(transition_key or "").strip()
        if not transition_key:
            raise ValueError("transition_key is required")
        confidence = str(confidence or "human_confirmed").strip() or "human_confirmed"
        with self.exploration_lock:
            data = self.read_exploration()
            transitions = data.get("transitions") if isinstance(data.get("transitions"), dict) else {}
            for by_room in transitions.values():
                if not isinstance(by_room, dict) or transition_key not in by_room:
                    continue
                record = by_room.get(transition_key)
                if not isinstance(record, dict):
                    break
                if from_semantic_room is not None:
                    record["from_semantic_room"] = str(from_semantic_room).strip() or None
                if to_semantic_room is not None:
                    record["to_semantic_room"] = str(to_semantic_room).strip() or None
                record["confidence"] = confidence
                record["confirmation"] = "human_confirmed"
                record["semantic_updated_at"] = time.time()
                if notes is not None:
                    record["notes"] = str(notes).strip()
                self.write_exploration(data)
                return record
        raise KeyError(f"unknown transition_key: {transition_key}")

    def native_trigger_observation(self, state: dict) -> dict | None:
        observation = state.get("trigger_observation") if isinstance(state.get("trigger_observation"), dict) else None
        if not observation or observation.get("completed") is not True:
            return None
        try:
            generation = int(observation.get("generation") or 0)
            object_id = int(observation.get("event_object_id") or 0)
        except (TypeError, ValueError):
            return None
        if generation <= 0 or object_id <= 0:
            return None
        return observation

    def native_trigger_generation(self, state: dict) -> dict | None:
        observation = state.get("trigger_observation") if isinstance(state.get("trigger_observation"), dict) else None
        if not observation:
            return None
        try:
            generation = int(observation.get("generation") or 0)
            object_id = int(observation.get("event_object_id") or 0)
        except (TypeError, ValueError):
            return None
        return observation if generation > 0 and object_id > 0 else None

    def clear_pending_transition_observation(self, object_id: int | None = None) -> None:
        pending = self.pending_transition_observation
        if object_id is None or (isinstance(pending, dict) and pending.get("object_id") == object_id):
            if isinstance(pending, dict):
                self.observations.cancel(pending.get("observation_id"), "transition_cleared")
                self.finish_navigation_route(
                    pending.get("from_room"),
                    pending.get("object_id"),
                    "transition_cleared",
                )
            self.pending_transition_observation = None

    def mark_pending_transition_observation(
        self,
        state: dict,
        object_id: int,
        *,
        owner: str = "navigation",
    ) -> str | None:
        observation = self.transition_observation_from_state(state, object_id)
        if observation is None:
            return None
        if isinstance(self.pending_transition_observation, dict):
            self.clear_pending_transition_observation()
        native = self.native_trigger_observation(state)
        try:
            baseline_generation = int(native.get("generation") or 0) if native else 0
        except (TypeError, ValueError):
            baseline_generation = 0
        payload = {
            **observation,
            "frozen_source_state": state,
            "game_instance_id": str(state.get("game_instance_id") or ""),
            "object_id": int(object_id),
            "sent_at": time.time(),
            "baseline_trigger_generation": baseline_generation,
            "trigger_generation": None,
            "control_started_at": None,
            "control_last_progress_at": None,
            "control_progress_signature": None,
            "landing_started_at": None,
            "candidate_room_id": None,
            "stable_snapshots": 0,
            "last_frame": None,
        }
        observation_id = self.observations.register(
            "transition",
            owner=owner,
            timeout=TRANSITION_OBSERVATION_TIMEOUT_SECONDS,
            payload=payload,
        )
        payload["observation_id"] = observation_id
        payload["owner"] = owner
        self.pending_transition_observation = payload
        if owner == "navigation":
            previous_id = self.navigation_transition_requests.get(int(object_id))
            if previous_id and previous_id != observation_id:
                self.observations.cancel(previous_id, "superseded")
            self.navigation_transition_requests[int(object_id)] = observation_id
        return observation_id

    def native_transition_observation_owner(
        self,
        state: dict,
        object_id: int,
        source: dict,
    ) -> tuple[str, dict]:
        """Associate a native touch trigger with the route that just reached it."""
        identity = self.room_identity(state)
        details = {
            "owner": "background",
            "route_age_seconds": None,
            "expected_position": None,
            "source_position": {"x": source.get("x"), "y": source.get("y")},
            "association_rejected_reason": None,
        }
        if identity is None:
            details["association_rejected_reason"] = "navigation_source_room_missing"
            return "background", details

        with self.exploration_lock:
            data = self.read_exploration()
            room = (data.get("rooms") or {}).get(identity["id"])
            last_route = room.get("last_route") if isinstance(room, dict) else None

        if not isinstance(last_route, dict) or not last_route.get("pending"):
            details["association_rejected_reason"] = "navigation_route_not_pending"
            return "background", details

        try:
            route_target_id = int(last_route.get("target_id") or 0)
        except (TypeError, ValueError):
            route_target_id = 0
        if route_target_id != object_id:
            details["association_rejected_reason"] = "navigation_target_mismatch"
            return "background", details

        target_selection = (
            last_route.get("target_selection")
            if isinstance(last_route.get("target_selection"), dict)
            else {}
        )
        if target_selection.get("kind") != "touch":
            details["association_rejected_reason"] = "navigation_target_not_touch"
            return "background", details

        try:
            route_age = time.time() - float(last_route.get("updated_at") or 0)
        except (TypeError, ValueError):
            route_age = TRANSITION_OBSERVATION_TIMEOUT_SECONDS + 1
        details["route_age_seconds"] = route_age
        if route_age < 0 or route_age > TRANSITION_OBSERVATION_TIMEOUT_SECONDS:
            details["association_rejected_reason"] = "navigation_route_outside_observation_window"
            return "background", details

        expected = last_route.get("expected_node") if isinstance(last_route.get("expected_node"), dict) else {}
        details["expected_position"] = {"x": expected.get("x"), "y": expected.get("y")}
        try:
            expected_matches = (
                int(expected.get("x")) == int(source.get("x"))
                and int(expected.get("y")) == int(source.get("y"))
            )
        except (TypeError, ValueError):
            expected_matches = False
        if not expected_matches:
            details["association_rejected_reason"] = "navigation_expected_source_mismatch"
            return "background", details

        try:
            frozen_source_matches = (
                int(room.get("scene_id")) == int(source.get("scene_id"))
                and int(room.get("map_id")) == int(source.get("map_id"))
                and int(identity.get("scene_id")) == int(source.get("scene_id"))
                and int(identity.get("map_id")) == int(source.get("map_id"))
            )
        except (AttributeError, TypeError, ValueError):
            frozen_source_matches = False
        if not frozen_source_matches:
            details["association_rejected_reason"] = "navigation_source_room_mismatch"
            return "background", details

        details["owner"] = "navigation"
        return "navigation", details

    def discover_native_transition_observation(self, state: dict) -> None:
        if isinstance(self.pending_transition_observation, dict):
            return
        previous = self.last_stable_room_state
        native = self.native_trigger_generation(state)
        if not isinstance(previous, dict) or native is None:
            return
        previous_native = self.native_trigger_generation(previous)
        try:
            generation = int(native.get("generation") or 0)
            previous_generation = int(previous_native.get("generation") or 0) if previous_native else 0
            object_id = int(native.get("event_object_id") or 0)
            trigger_script = int(native.get("trigger_script") or 0)
        except (TypeError, ValueError):
            return
        if generation <= previous_generation or object_id <= 0:
            return
        source = native.get("source") if isinstance(native.get("source"), dict) else {}
        obj = self.target_object(previous, object_id)
        if not isinstance(obj, dict):
            self.append_navigation_diagnostic(
                "native_transition_rejected",
                {"reason": "source_object_missing", "object_id": object_id, "generation": generation},
                state,
            )
            return
        try:
            source_matches = (
                int(source.get("scene_id")) == int(previous.get("scene_id"))
                and int(source.get("map_id")) == int(previous.get("map_id"))
            )
        except (TypeError, ValueError):
            self.append_navigation_diagnostic(
                "native_transition_rejected",
                {"reason": "invalid_source_scene", "object_id": object_id, "generation": generation},
                state,
            )
            return
        if not source_matches:
            self.append_navigation_diagnostic(
                "native_transition_rejected",
                {"reason": "source_scene_mismatch", "object_id": object_id, "generation": generation, "source": source},
                state,
            )
            return
        if trigger_script != int(obj.get("trigger_script") or 0):
            self.append_navigation_diagnostic(
                "native_transition_rejected",
                {"reason": "trigger_script_mismatch", "object_id": object_id, "generation": generation},
                state,
            )
            return
        from_identity = self.room_identity(previous)
        if from_identity is None:
            return
        owner, owner_details = self.native_transition_observation_owner(previous, object_id, source)
        self.append_navigation_diagnostic(
            "native_transition_owner",
            {
                "object_id": object_id,
                "generation": generation,
                **owner_details,
            },
            state,
        )
        self.mark_pending_transition_observation(previous, object_id, owner=owner)
        pending = self.pending_transition_observation
        if not isinstance(pending, dict):
            return
        pending["trigger_generation"] = generation
        pending["control_started_at"] = time.time()
        pending["control_last_progress_at"] = pending["control_started_at"]
        pending["control_progress_signature"] = self.game_control_progress_signature(state)
        self.observations.suspend_timeout(pending.get("observation_id"))
        observed_from = pending.get("observed_from_state") if isinstance(pending, dict) else None
        if isinstance(observed_from, dict):
            observed_from["party_position"] = {"x": source.get("x"), "y": source.get("y")}
        self.append_navigation_diagnostic(
            "native_transition_detected",
            {
                "object_id": object_id,
                "generation": generation,
                "trigger_script": trigger_script,
                "source": source,
                "from_room_id": from_identity["id"],
                "completed": native.get("completed") is True,
                "owner": owner,
            },
            state,
        )

    def nearest_transition_candidate(self, state: dict, position: dict | None = None) -> tuple[int, float] | None:
        pos = position if isinstance(position, dict) else (
            state.get("party_position") if isinstance(state.get("party_position"), dict) else {}
        )
        try:
            px, py = int(pos.get("x")), int(pos.get("y"))
        except (TypeError, ValueError):
            return None
        candidates = []
        for obj in state.get("objects") if isinstance(state.get("objects"), list) else []:
            if (
                not isinstance(obj, dict)
                or not transition_candidate(obj)
                or int(obj.get("state") or 0) <= 0
            ):
                continue
            try:
                object_id = int(obj.get("id") or 0)
                dx, dy = int(obj.get("x")) - px, int(obj.get("y")) - py
            except (TypeError, ValueError):
                continue
            if object_id > 0:
                candidates.append((dx * dx + dy * dy, object_id))
        if not candidates:
            return None
        distance_squared, object_id = min(candidates, key=lambda item: (item[0], item[1]))
        return object_id, distance_squared ** 0.5

    def state_at_native_destination(self, state: dict, destination: dict | None) -> dict:
        if not isinstance(destination, dict):
            return state
        try:
            x, y = int(destination.get("x")), int(destination.get("y"))
        except (TypeError, ValueError):
            return state
        seeded = dict(state)
        seeded["party_position"] = {"x": x, "y": y}
        for key in ("_logical_room", "logical_room_id", "logical_room_label", "legacy_room_id", "legacy_nav_room_id"):
            seeded.pop(key, None)
        return seeded

    def observe_pending_transition_destination(self, state: dict) -> dict | None:
        pending = self.pending_transition_observation
        if not isinstance(pending, dict):
            return None
        try:
            object_id = int(pending.get("object_id") or 0)
            sent_at = float(pending.get("sent_at") or 0)
            baseline_generation = int(pending.get("baseline_trigger_generation") or 0)
        except (TypeError, ValueError):
            self.pending_transition_observation = None
            return None
        now = time.time()
        from_identity = pending.get("from_room") if isinstance(pending.get("from_room"), dict) else None
        if object_id <= 0 or from_identity is None:
            self.observations.cancel(pending.get("observation_id"), "invalid_transition_observation")
            self.finish_navigation_route(from_identity, object_id, "transition_invalid")
            self.pending_transition_observation = None
            return None
        native_generation = self.native_trigger_generation(state)
        if pending.get("trigger_generation") is None:
            if native_generation is None:
                if sent_at and now - sent_at > TRANSITION_OBSERVATION_TIMEOUT_SECONDS:
                    self.append_navigation_diagnostic(
                        "native_transition_rejected",
                        {"reason": "trigger_confirmation_timeout", "object_id": object_id},
                        state,
                    )
                    self.observations.cancel(
                        pending.get("observation_id"),
                        "trigger_confirmation_timeout",
                    )
                    self.finish_navigation_route(
                        pending.get("from_room"),
                        object_id,
                        "transition_timeout",
                    )
                    self.pending_transition_observation = None
                return None
            try:
                generation = int(native_generation.get("generation") or 0)
                native_object_id = int(native_generation.get("event_object_id") or 0)
                native_script = int(native_generation.get("trigger_script") or 0)
                expected_script = int((pending.get("object") or {}).get("trigger_script") or 0)
            except (TypeError, ValueError):
                return None
            if generation <= baseline_generation or native_object_id != object_id or native_script != expected_script:
                return None
            pending["trigger_generation"] = generation
            pending["control_started_at"] = now
            pending["control_last_progress_at"] = now
            pending["control_progress_signature"] = self.game_control_progress_signature(state)
            self.observations.suspend_timeout(pending.get("observation_id"))
        elif native_generation is None:
            return None
        else:
            try:
                if (
                    int(native_generation.get("generation") or 0) != int(pending.get("trigger_generation") or 0)
                    or int(native_generation.get("event_object_id") or 0) != object_id
                ):
                    return None
            except (TypeError, ValueError):
                return None
        if native_generation.get("completed") is not True:
            progress_signature = self.game_control_progress_signature(state)
            control = self.control_state(state)
            if control.get("phase") in {"dialog", "battle", "shop"}:
                pending["control_last_progress_at"] = now
                pending["control_progress_signature"] = progress_signature
                return None
            if progress_signature != pending.get("control_progress_signature"):
                pending["control_last_progress_at"] = now
                pending["control_progress_signature"] = progress_signature
                return None
            control_last_progress_at = float(
                pending.get("control_last_progress_at")
                or pending.get("control_started_at")
                or now
            )
            if now - control_last_progress_at <= GAME_CONTROL_TIMEOUT_SECONDS:
                return None
            self.append_navigation_diagnostic(
                "game_control_timeout",
                {
                    "object_id": object_id,
                    "generation": pending.get("trigger_generation"),
                    "timeout_seconds": GAME_CONTROL_TIMEOUT_SECONDS,
                },
                state,
            )
            self.observations.cancel(pending.get("observation_id"), "game_control_timeout")
            self.finish_navigation_route(pending.get("from_room"), object_id, "game_control_timeout")
            if self.navigation_transition_requests.get(object_id) == pending.get("observation_id"):
                self.navigation_transition_requests.pop(object_id, None)
            self.pending_transition_observation = None
            return None
        if pending.get("landing_started_at") is None:
            pending["landing_started_at"] = now
            self.observations.reschedule(
                pending.get("observation_id"),
                TRANSITION_OBSERVATION_TIMEOUT_SECONDS,
            )
        elif now - float(pending.get("landing_started_at") or now) > TRANSITION_OBSERVATION_TIMEOUT_SECONDS:
            self.append_navigation_diagnostic(
                "native_transition_rejected",
                {"reason": "destination_unstable_timeout", "object_id": object_id},
                state,
            )
            self.observations.cancel(pending.get("observation_id"), "destination_unstable_timeout")
            self.finish_navigation_route(pending.get("from_room"), object_id, "transition_timeout")
            self.pending_transition_observation = None
            return None
        native = native_generation
        destination = native.get("destination") if isinstance(native.get("destination"), dict) else None
        if destination is not None:
            try:
                destination_matches_state = (
                    int(destination.get("scene_id")) == int(state.get("scene_id"))
                    and int(destination.get("map_id")) == int(state.get("map_id"))
                )
            except (TypeError, ValueError):
                return None
            if not destination_matches_state:
                return None
        destination_state = self.state_at_native_destination(state, destination)
        to_identity = self.room_identity(destination_state)
        if to_identity is None:
            return None
        if state.get("entering_scene"):
            return None
        from_pos = (pending.get("observed_from_state") or {}).get("party_position") or {}
        to_pos = destination if destination is not None else (
            state.get("party_position") if isinstance(state.get("party_position"), dict) else {}
        )
        position_changed = (from_pos.get("x"), from_pos.get("y")) != (to_pos.get("x"), to_pos.get("y"))
        same_room = to_identity["id"] == from_identity.get("id")
        if same_room and not position_changed:
            self.observations.cancel(pending.get("observation_id"), "no_position_change")
            self.pending_transition_observation = None
            return None
        frame = self.int_frame(state.get("logical_frame", state.get("frame")))
        if frame is None or frame == pending.get("last_frame"):
            return None
        pending["last_frame"] = frame
        if pending.get("candidate_room_id") == to_identity["id"]:
            pending["stable_snapshots"] = int(pending.get("stable_snapshots") or 0) + 1
        else:
            pending["candidate_room_id"] = to_identity["id"]
            pending["stable_snapshots"] = 1
        if pending["stable_snapshots"] < TRANSITION_STABLE_SNAPSHOTS:
            return None
        record = self.record_transition_destination(
            from_identity,
            object_id,
            to_identity,
            from_object=pending.get("object") if isinstance(pending.get("object"), dict) else None,
            to_state=state,
            transition_key=pending.get("transition_key"),
            transition_key_fields=pending.get("transition_key_fields") if isinstance(pending.get("transition_key_fields"), dict) else None,
            observed_from_state=pending.get("observed_from_state") if isinstance(pending.get("observed_from_state"), dict) else None,
            confirmation="native_trigger",
            intra_room_transition=same_room,
            native_destination=destination,
        )
        self.append_navigation_diagnostic(
            "native_transition_confirmed",
            {
                "object_id": object_id,
                "generation": pending.get("trigger_generation"),
                "from_room_id": from_identity.get("id"),
                "to_room_id": to_identity.get("id"),
                "transition_key": record.get("transition_key"),
            },
            state,
        )
        observation_id = pending.get("observation_id")
        self.observations.complete(observation_id, {"transition": record})
        if pending.get("owner") == "background":
            self.observations.consume(observation_id, owner="background")
        self.finish_navigation_route(from_identity, object_id, "transitioned")
        self.pending_transition_observation = None
        return record

    def exploration_room(self, data: dict, identity: dict) -> dict:
        rooms = data.setdefault("rooms", {})
        room = rooms.get(identity["id"])
        if not isinstance(room, dict):
            room = {
                "scene_id": identity["scene_id"],
                "map_id": identity["map_id"],
                "logical_room_id": identity["id"],
                "logical_room_label": identity.get("logical_room_label"),
                "room_key": identity["room_key"],
                "region_key": identity["region_key"],
                "nav_room_id": identity["nav_room_id"],
                "legacy_room_id": identity.get("legacy_room_id"),
                "legacy_nav_room_id": identity.get("legacy_nav_room_id"),
                "visited": [],
                "frontier": [],
                "explorable": 0,
                "blocked_edges": [],
                "failed_nodes": [],
                "last_route": None,
                "complete": False,
                "updated_at": None,
            }
            rooms[identity["id"]] = room
        return room

    def safe_exploration_nodes(self, state: dict, grid: dict) -> set[tuple[int, int]]:
        component, _, _ = self.logical_room_component_for_state(state, grid)
        return component

    def viewport_bounds(self, state: dict, margin: int = 0) -> tuple[int, int, int, int] | None:
        viewport = state.get("viewport") or {}
        try:
            left = int(viewport.get("x")) - margin
            top = int(viewport.get("y")) - margin
        except (TypeError, ValueError):
            return None
        return (left, top, left + VIEWPORT_WIDTH + margin * 2, top + VIEWPORT_HEIGHT + margin * 2)

    def viewport_nodes(self, state: dict, grid: dict, nodes: set[tuple[int, int]], margin: int = 0) -> set[tuple[int, int]]:
        bounds = self.viewport_bounds(state, margin)
        if bounds is None:
            return set()
        left, top, right, bottom = bounds
        visible = set()
        for node in nodes:
            x, y = world_pos(grid, node)
            if left <= x <= right and top <= y <= bottom:
                visible.add(node)
        return visible

    def reachable_safe_nodes(
        self,
        grid: dict,
        start: tuple[int, int],
        safe: set[tuple[int, int]],
    ) -> set[tuple[int, int]]:
        if start not in safe:
            return set()
        queue = deque([start])
        reachable = {start}
        while queue:
            node = queue.popleft()
            for delta in MOVE_DELTAS.values():
                next_node = (node[0] + delta[0], node[1] + delta[1])
                if next_node in reachable or next_node not in safe:
                    continue
                reachable.add(next_node)
                queue.append(next_node)
        return reachable

    def route_nodes_through(
        self,
        route: dict,
        current: tuple[int, int],
    ) -> set[tuple[int, int]]:
        route_node_list = [
            (int(node.get("grid_x", -1)), int(node.get("grid_y", -1)))
            for node in route.get("nodes", [])
            if isinstance(node, dict)
        ]
        confirmed: set[tuple[int, int]] = set()
        found_current = False
        for node in route_node_list:
            confirmed.add(node)
            if node == current:
                found_current = True
                break
        if not found_current:
            return set()
        return confirmed

    def remember_navigation_route(
        self,
        state: dict,
        target_id: int,
        route: dict,
        segment_steps: int,
        remaining_steps: int,
        status: str,
    ) -> None:
        grid = decode_nav_grid(state)
        identity = self.room_identity(state)
        if grid is None or identity is None:
            return

        route_nodes: list[dict] = []
        for payload in route.get("nodes") or []:
            if not isinstance(payload, dict):
                continue
            try:
                node = grid_node(grid, int(payload.get("x")), int(payload.get("y")))
            except (TypeError, ValueError):
                node = None
            if node is not None:
                route_nodes.append(node_payload(grid, node))
        if not route_nodes:
            return

        expected_node = route_nodes[segment_steps] if len(route_nodes) > segment_steps else route_nodes[-1]
        target = self.target_object(state, target_id) or {}
        target_selection = {
            "id": target_id,
            "kind": trigger_kind(int(target.get("trigger_mode") or 0)),
            "status": status,
        }

        with self.exploration_lock:
            data = self.read_exploration()
            room = self.exploration_room(data, identity)
            room["last_route"] = {
                "pending": True,
                "target_id": target_id,
                "target_selection": target_selection,
                "segment_steps": segment_steps,
                "remaining_steps": remaining_steps,
                "expected_node": expected_node,
                "nodes": route_nodes,
                "updated_at": time.time(),
            }
            self.write_exploration(data)

    def finish_navigation_route(
        self,
        from_identity: dict | None,
        target_id,
        outcome: str,
    ) -> None:
        if not isinstance(from_identity, dict) or not from_identity.get("id"):
            return
        try:
            target_id = int(target_id or 0)
        except (TypeError, ValueError):
            return
        if target_id <= 0:
            return
        with self.exploration_lock:
            data = self.read_exploration()
            room = (data.get("rooms") or {}).get(from_identity["id"])
            if not isinstance(room, dict):
                return
            last_route = room.get("last_route")
            try:
                remembered_target_id = int(last_route.get("target_id") or 0) if isinstance(last_route, dict) else 0
            except (TypeError, ValueError):
                remembered_target_id = 0
            if (
                not isinstance(last_route, dict)
                or not last_route.get("pending")
                or remembered_target_id != target_id
            ):
                return
            last_route["pending"] = False
            last_route["outcome"] = outcome
            last_route["completed_at"] = time.time()
            room["updated_at"] = last_route["completed_at"]
            self.write_exploration(data)

    def pending_touch_route_confirmation(self, state: dict, target_id: int) -> dict | None:
        """Return a final touch route that is still awaiting its native trigger."""
        identity = self.room_identity(state)
        grid = decode_nav_grid(state)
        if identity is None or grid is None:
            return None
        pos = state.get("party_position") if isinstance(state.get("party_position"), dict) else {}
        try:
            current = grid_node(grid, int(pos.get("x") or 0), int(pos.get("y") or 0))
        except (TypeError, ValueError):
            return None
        if current is None:
            return None

        with self.exploration_lock:
            data = self.read_exploration()
            room = (data.get("rooms") or {}).get(identity["id"])
            last_route = room.get("last_route") if isinstance(room, dict) else None
            if not isinstance(last_route, dict) or not last_route.get("pending"):
                return None
            try:
                remembered_target_id = int(last_route.get("target_id") or 0)
                remaining_steps = int(last_route.get("remaining_steps") or 0)
                route_age = time.time() - float(last_route.get("updated_at") or 0)
                expected = (
                    last_route.get("expected_node")
                    if isinstance(last_route.get("expected_node"), dict)
                    else {}
                )
                expected_node = (int(expected.get("grid_x", -1)), int(expected.get("grid_y", -1)))
            except (TypeError, ValueError):
                return None
            selection = (
                last_route.get("target_selection")
                if isinstance(last_route.get("target_selection"), dict)
                else {}
            )
            if (
                remembered_target_id != int(target_id)
                or remaining_steps != 0
                or selection.get("kind") != "touch"
                or route_age < 0
                or route_age > TRANSITION_OBSERVATION_TIMEOUT_SECONDS
                or current != expected_node
            ):
                return None
            return dict(last_route)

    def exploration_safe_and_reachable(
        self,
        state: dict,
        grid: dict,
        room: dict,
        current: tuple[int, int] | None,
    ) -> tuple[set[tuple[int, int]], set[tuple[int, int]], set[tuple[int, int]]]:
        safe = self.safe_exploration_nodes(state, grid)
        failed_nodes = {parse_node_key(key) for key in room.get("failed_nodes", [])}
        failed_nodes = {node for node in failed_nodes if node is not None}
        if current is not None:
            failed_nodes.discard(current)
        safe.difference_update(failed_nodes)
        reachable_safe = self.reachable_safe_nodes(grid, current, safe) if current is not None and current in safe else set()
        return safe, reachable_safe, failed_nodes

    def recompute_exploration_room(self, state: dict, grid: dict, room: dict, current: tuple[int, int] | None) -> bool:
        old = (
            tuple(room.get("visited") or []),
            tuple(room.get("frontier") or []),
            room.get("explorable"),
            tuple(room.get("blocked_edges") or []),
            tuple(room.get("failed_nodes") or []),
            json.dumps(room.get("last_route"), sort_keys=True),
            bool(room.get("complete")),
        )

        safe, reachable_safe, failed_nodes = self.exploration_safe_and_reachable(state, grid, room, current)
        static_component, _ = self.static_room_component_for_state(state, grid)
        explored = {parse_node_key(key) for key in room.get("visited", [])}
        explored = {node for node in explored if node is not None and node in static_component}
        if current is not None and current in safe:
            explored.add(current)

        last_route = room.get("last_route")
        if isinstance(last_route, dict) and last_route.get("pending"):
            expected = last_route.get("expected_node") or {}
            expected_node = (int(expected.get("grid_x", -1)), int(expected.get("grid_y", -1)))
            route_node_set = self.route_nodes_through(last_route, current) if current is not None else set()
            if route_node_set:
                explored.update(node for node in route_node_set if node in reachable_safe)
            pending_transition = self.pending_transition_observation
            try:
                pending_object_id = (
                    int(pending_transition.get("object_id") or 0)
                    if isinstance(pending_transition, dict)
                    else 0
                )
                remembered_target_id = int(last_route.get("target_id") or 0)
                remaining_steps = int(last_route.get("remaining_steps") or 0)
                route_age = time.time() - float(last_route.get("updated_at") or 0)
            except (TypeError, ValueError):
                pending_object_id = 0
                remembered_target_id = -1
                remaining_steps = -1
                route_age = TRANSITION_OBSERVATION_TIMEOUT_SECONDS + 1
            selection = (
                last_route.get("target_selection")
                if isinstance(last_route.get("target_selection"), dict)
                else {}
            )
            final_touch_route = selection.get("kind") == "touch" and remaining_steps == 0
            same_live_transition = (
                isinstance(pending_transition, dict)
                and pending_object_id == remembered_target_id
                and isinstance(pending_transition.get("from_room"), dict)
                and pending_transition["from_room"].get("id") == room.get("logical_room_id")
            )
            confirmation_expired = (
                not self.action_pending(state)
                and not same_live_transition
                and route_age > TRANSITION_OBSERVATION_TIMEOUT_SECONDS
            )
            if current == expected_node:
                explored.update(node for node in self.route_nodes_through(last_route, current) if node in reachable_safe)
                if not final_touch_route:
                    last_route["pending"] = False
                    last_route["outcome"] = "expected_node_reached"
                    last_route["completed_at"] = time.time()
                elif confirmation_expired:
                    last_route["pending"] = False
                    last_route["outcome"] = "confirmation_expired"
                    last_route["completed_at"] = time.time()
            elif confirmation_expired:
                last_route["pending"] = False
                last_route["outcome"] = "confirmation_expired"
                last_route["completed_at"] = time.time()

        frontier: set[tuple[int, int]] = set()
        blocked_edges: set[str] = set()
        for node in explored:
            if node not in reachable_safe:
                continue
            for delta in MOVE_DELTAS.values():
                next_node = (node[0] + delta[0], node[1] + delta[1])
                if next_node in reachable_safe:
                    if next_node not in explored:
                        frontier.add(next_node)
                else:
                    blocked_edges.add(edge_key(node, next_node))

        room["visited"] = sorted(node_key(node) for node in explored)
        room["frontier"] = sorted(node_key(node) for node in frontier)
        room["explorable"] = len(reachable_safe)
        room["blocked_edges"] = sorted(blocked_edges)
        room["failed_nodes"] = sorted(node_key(node) for node in failed_nodes)
        room["complete"] = bool(explored) and not frontier

        new = (
            tuple(room.get("visited") or []),
            tuple(room.get("frontier") or []),
            room.get("explorable"),
            tuple(room.get("blocked_edges") or []),
            tuple(room.get("failed_nodes") or []),
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
            return {"status": "no_nav_grid", "visited": 0, "explored": 0, "explorable": 0, "frontier": 0, "blocked_edges": 0, "complete": False}
        if identity is None:
            return {"status": "no_room", "visited": 0, "explored": 0, "explorable": 0, "frontier": 0, "blocked_edges": 0, "complete": False}

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
        current_node = None
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

        _, explorable_nodes, _ = self.exploration_safe_and_reachable(state, grid, room, current_node) if grid is not None else (set(), set(), set())
        visited_nodes = nodes_for(room.get("visited") or [])
        frontier_nodes = nodes_for(room.get("frontier") or [])
        explorable_payload = [node_payload(grid, node) for node in sorted(explorable_nodes, key=lambda item: (item[1], item[0]))] if grid is not None else []

        return {
            "status": status,
            "room_id": identity["id"] if identity else None,
            "scene_id": identity["scene_id"] if identity else None,
            "map_id": identity["map_id"] if identity else None,
            "logical_room_id": identity["id"] if identity else None,
            "logical_room_label": identity.get("logical_room_label") if identity else None,
            "room_key": identity["room_key"] if identity else None,
            "region_key": identity["region_key"] if identity else None,
            "nav_room_id": identity["nav_room_id"] if identity else None,
            "legacy_room_id": identity.get("legacy_room_id") if identity else None,
            "legacy_nav_room_id": identity.get("legacy_nav_room_id") if identity else None,
            "visited": len(room.get("visited") or []),
            "explored": len(room.get("visited") or []),
            "explorable": len(explorable_nodes),
            "frontier": len(room.get("frontier") or []),
            "blocked_edges": len(room.get("blocked_edges") or []),
            "failed_nodes": len(room.get("failed_nodes") or []),
            "complete": bool(room.get("complete")),
            "updated_at": room.get("updated_at"),
            "current": current,
            "nodes": {
                "visited": visited_nodes,
                "explored": visited_nodes,
                "explorable": explorable_payload,
                "frontier": frontier_nodes,
                "failed": nodes_for(room.get("failed_nodes") or []),
            },
            "last_route": room.get("last_route"),
        }

    def current_exploration(self, state: dict) -> dict:
        grid = decode_nav_grid(state)
        identity = self.room_identity(state)
        if grid is None:
            return {"status": "no_nav_grid", "visited": 0, "explored": 0, "explorable": 0, "frontier": 0, "blocked_edges": 0, "complete": False}
        if identity is None:
            return {"status": "no_room", "visited": 0, "explored": 0, "explorable": 0, "frontier": 0, "blocked_edges": 0, "complete": False}
        with self.exploration_lock:
            data = self.read_exploration()
            room = self.exploration_room(data, identity)
            return self.exploration_view(state, data, grid, room, status="ok")

    def room_exploration_summary(self, room_id: str | None, state: dict | None = None) -> dict:
        if not room_id and state is not None:
            identity = self.room_identity(state)
            room_id = identity["id"] if identity else None
        if not room_id:
            return {
                "known": False,
                "room_id": None,
                "scene_id": None,
                "map_id": None,
                "logical_room_id": None,
                "logical_room_label": None,
                "room_key": None,
                "region_key": None,
                "nav_room_id": None,
                "legacy_room_id": None,
                "legacy_nav_room_id": None,
                "complete": False,
                "explored": 0,
                "explorable": 0,
                "frontier": 0,
                "updated_at": None,
            }

        if state is not None:
            identity = self.room_identity(state)
            if identity is not None and identity["id"] == room_id:
                self.update_exploration(state)

        with self.exploration_lock:
            data = self.read_exploration()
            return self.room_exploration_summary_from_data(data, room_id)

    def transition_nav_room_id(self, from_room_id: str | None, state: dict | None = None) -> str | None:
        if from_room_id:
            return str(from_room_id)
        if state is not None:
            identity = self.room_identity(state)
            return identity["id"] if identity else None
        return None

    def known_transitions_summary(self, from_room_id: str | None, state: dict | None = None) -> dict:
        from_nav_room_id = self.transition_nav_room_id(from_room_id, state)
        if not from_nav_room_id:
            return {"from_room_id": from_room_id, "from_logical_room_id": None, "from_nav_room_id": None, "known_transitions": {}, "transitions": []}
        with self.exploration_lock:
            data = self.read_exploration()
            transitions = data.get("transitions") if isinstance(data.get("transitions"), dict) else {}
            by_transition = transitions.get(from_nav_room_id) if isinstance(transitions, dict) else {}
            if not isinstance(by_transition, dict):
                by_transition = {}
            recent = set(self.recent_room_ids(state, limit=8)) if state is not None else set()
            result = {}
            rows = []
            def transition_sort_key(item: tuple[object, object]) -> tuple[int, int, int, int, str]:
                record = item[1] if isinstance(item[1], dict) else {}
                return (
                    int(record.get("object_id") or 0),
                    int(record.get("object", {}).get("x") or record.get("transition_key_fields", {}).get("x") or 0),
                    int(record.get("object", {}).get("y") or record.get("transition_key_fields", {}).get("y") or 0),
                    int(record.get("object", {}).get("trigger_script") or record.get("transition_key_fields", {}).get("trigger_script") or 0),
                    str(item[0]),
                )

            recent_semantic = self.recent_semantic_room_keys(data, recent)
            by_object: dict[str, list[dict]] = {}
            for object_key, record in sorted(by_transition.items(), key=transition_sort_key):
                if not isinstance(record, dict):
                    continue
                if state is not None:
                    try:
                        record_object_id = int(record.get("object_id") or 0)
                    except (TypeError, ValueError):
                        record_object_id = 0
                    current_object = self.target_object(state, record_object_id) if record_object_id > 0 else None
                    if current_object is not None:
                        identity = self.room_identity(state)
                        current_key = self.transition_key_from_fields(
                            self.transition_key_fields(identity, current_object, record_object_id)
                        ) if identity is not None else None
                        recorded_key = record.get("transition_key") or str(object_key)
                        if current_key != recorded_key:
                            continue
                payload = {
                    "transition_key": record.get("transition_key") or str(object_key),
                    "transition_key_fields": record.get("transition_key_fields"),
                    "object_id": record.get("object_id"),
                    "object": record.get("object"),
                    "from_room_id": record.get("from_room_id"),
                    "from_logical_room_id": record.get("from_logical_room_id") or record.get("from_room_id"),
                    "from_scene_id": record.get("from_scene_id"),
                    "from_map_id": record.get("from_map_id"),
                    "from_room_key": record.get("from_room_key"),
                    "from_region_key": record.get("from_region_key"),
                    "from_nav_room_id": record.get("from_nav_room_id"),
                    "from_legacy_room_id": record.get("from_legacy_room_id"),
                    "from_legacy_nav_room_id": record.get("from_legacy_nav_room_id"),
                    "from_semantic_room": record.get("from_semantic_room"),
                    "to_room_id": record.get("to_room_id"),
                    "to_logical_room_id": record.get("to_logical_room_id") or record.get("to_room_id"),
                    "to_scene_id": record.get("to_scene_id"),
                    "to_map_id": record.get("to_map_id"),
                    "to_room_key": record.get("to_room_key"),
                    "to_region_key": record.get("to_region_key"),
                    "to_nav_room_id": record.get("to_nav_room_id"),
                    "to_legacy_room_id": record.get("to_legacy_room_id"),
                    "to_legacy_nav_room_id": record.get("to_legacy_nav_room_id"),
                    "to_semantic_room": record.get("to_semantic_room"),
                    "confidence": record.get("confidence"),
                    "confirmation": record.get("confirmation"),
                    "observed": bool(record.get("observed")),
                    "observed_at": record.get("observed_at"),
                    "observed_from_state": record.get("observed_from_state"),
                    "observed_to_state": record.get("observed_to_state"),
                    "intra_room_transition": bool(record.get("intra_room_transition")),
                    "from_subregion_id": record.get("from_subregion_id"),
                    "to_subregion_id": record.get("to_subregion_id"),
                    "from_location_id": record.get("from_location_id") or record.get("from_room_id"),
                    "to_location_id": record.get("to_location_id") or record.get("to_room_id"),
                    "subregion_basis": record.get("subregion_basis"),
                    "subregion_match_distance": record.get("subregion_match_distance"),
                    "native_destination": record.get("native_destination"),
                }
                confirmed = record.get("confirmation") in {"native_trigger", "human_confirmed"}
                destination = self.room_exploration_summary_from_data(
                    data,
                    str(record.get("to_room_id")) if confirmed else None,
                )
                to_semantic_key = self.transition_semantic_room_key(
                    record.get("to_scene_id"),
                    record.get("to_map_id"),
                    record.get("to_semantic_room"),
                )
                returns_to_recent_room = (not record.get("intra_room_transition")) and confirmed and (str(record.get("to_room_id")) in recent or (
                    bool(to_semantic_key) and to_semantic_key in recent_semantic
                ))
                payload.update(
                    {
                        "to_logical_room_label": destination.get("logical_room_label") or (
                            f"S{record.get('to_scene_id')}/M{record.get('to_map_id')}:"
                            f"{str(record.get('to_room_id')).rsplit(':', 1)[-1][:12].upper()}"
                            if record.get("to_scene_id") is not None
                            and record.get("to_map_id") is not None
                            and record.get("to_room_id")
                            else None
                        ),
                        "destination_exploration": destination,
                        "destination_priority": self.destination_priority(destination),
                        "returns_to_recent_room": returns_to_recent_room,
                    }
                )
                if not confirmed:
                    payload["unconfirmed_destination"] = True
                if returns_to_recent_room:
                    payload["agent_priority"] = "avoid_backtrack"
                    payload["deprioritize_reason"] = "该传送目标会回到最近访问过的房间。"
                result[str(payload["transition_key"])] = payload
                by_object.setdefault(str(record.get("object_id")), []).append(payload)
                rows.append(payload)
            return {
                "from_room_id": from_room_id,
                "from_logical_room_id": from_nav_room_id,
                "from_nav_room_id": from_nav_room_id,
                "known_transitions": result,
                "known_transitions_by_object": by_object,
                "transitions": rows,
            }

    def goal_nodes(self, state: dict, grid: dict, target: dict) -> tuple[set[tuple[int, int]], dict[tuple[int, int], int]]:
        goals: set[tuple[int, int]] = set()
        search_dirs: dict[tuple[int, int], int] = {}
        mode = int(target.get("trigger_mode") or 0)
        kind = trigger_kind(mode)
        target_pos = (int(target.get("x") or 0), int(target.get("y") or 0))

        if kind == "touch":
            return self.touch_boundary_nodes_for_object(target, grid), search_dirs
        if kind != "search":
            return goals, search_dirs

        x_min, x_max, y_min, y_max = self.grid_window_for_world_margin(grid, target_pos, 80, 40)
        for y in range(y_min, y_max + 1):
            for x in range(x_min, x_max + 1):
                node = (x, y)
                if not nav_bit(grid, node):
                    continue
                pos = world_pos(grid, node)
                for direction in (2, 3, 0, 1):
                    if search_hits(pos, direction, target):
                        goals.add(node)
                        search_dirs[node] = direction
                        break
        return goals, search_dirs

    def approach_goal_nodes(
        self,
        state: dict,
        grid: dict,
        target: dict,
        requirements: list[dict],
    ) -> tuple[set[tuple[int, int]], dict[tuple[int, int], int], dict[tuple[int, int], dict]]:
        goals: set[tuple[int, int]] = set()
        directions: dict[tuple[int, int], int] = {}
        details: dict[tuple[int, int], dict] = {}
        if int(target.get("state") or 0) <= 0:
            return goals, directions, details

        current_direction = int(state.get("party_direction") or 0)
        direction_order = [current_direction] + [direction for direction in (2, 3, 0, 1) if direction != current_direction]
        for requirement in requirements:
            distance_limit = int(requirement.get("distance_limit") or 0)
            if distance_limit <= 0:
                continue
            target_pos = (int(target.get("x") or 0), int(target.get("y") or 0))
            x_min, x_max, y_min, y_max = self.grid_window_for_world_margin(
                grid,
                target_pos,
                distance_limit + 16,
                (distance_limit + 16) // 2,
            )
            for y in range(y_min, y_max + 1):
                for x in range(x_min, x_max + 1):
                    node = (x, y)
                    if not nav_bit(grid, node):
                        continue
                    position = world_pos(grid, node)
                    for direction in direction_order:
                        if not facing_event_object_hits(position, direction, target, distance_limit):
                            continue
                        goals.add(node)
                        directions.setdefault(node, direction)
                        details.setdefault(node, requirement)
                        break
        return goals, directions, details

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
        graph = self.navigation_graph(grid)

        while queue and goal is None:
            node = queue.popleft()
            for move, next_node in graph.get(node, ()):
                if next_node in seen or next_node in blocked:
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

    def route_to_object(
        self,
        state: dict,
        target_id: int,
        avoid_transitions: bool = True,
        context: dict | None = None,
        approach_item_id: int | None = None,
        approach_requirement: dict | None = None,
    ) -> dict:
        context = context if isinstance(context, dict) else None
        grid = context.get("grid") if context else decode_nav_grid(state)
        target_by_id = context.get("target_by_id") if context else None
        target = target_by_id.get(target_id) if isinstance(target_by_id, dict) else self.target_object(state, target_id)
        pos = state.get("party_position") or {}
        if grid is None:
            return {"ok": False, "status": "no_nav_grid", "target_id": target_id}
        if target is None:
            return {"ok": False, "status": "unknown_target", "target_id": target_id}
        if not target.get("interactable") or int(target.get("state") or 0) <= 0:
            return {
                "ok": approach_item_id is not None,
                "status": "approach_requirement_missing" if approach_item_id is not None else "target_inactive",
                "target_id": target_id,
            }
        destination_payload = self.transition_destination_for_state(state, target_id) if transition_candidate(target) else {}
        start = grid_node(grid, int(pos.get("x") or 0), int(pos.get("y") or 0))
        if start is None or not nav_bit(grid, start):
            return {"ok": False, "status": "bad_start", "target_id": target_id}

        native_kind = trigger_kind(int(target.get("trigger_mode") or 0))
        kind = native_kind
        approach_details_by_node: dict[tuple[int, int], dict] = {}
        touch_nodes_by_id = context.get("touch_nodes_by_id") if context else None
        if approach_item_id is not None or isinstance(approach_requirement, dict):
            requirements = (
                [approach_requirement]
                if isinstance(approach_requirement, dict)
                else self.approach_requirements_by_target(state, approach_item_id).get(target_id, [])
            )
            if not requirements:
                return {
                    "ok": True,
                    "target_id": target_id,
                    "status": "approach_requirement_missing",
                    "kind": native_kind,
                }
            kind = "approach"
            goals, search_dirs, approach_details_by_node = self.approach_goal_nodes(
                state,
                grid,
                target,
                requirements,
            )
        elif kind == "none":
            requirements = self.approach_requirements_by_target(state).get(target_id, [])
            if not requirements:
                return {
                    "ok": True,
                    "target_id": target_id,
                    "status": "unreachable_static",
                    "kind": kind,
                }
            kind = "approach"
            goals, search_dirs, approach_details_by_node = self.approach_goal_nodes(
                state,
                grid,
                target,
                requirements,
            )
        elif kind == "touch" and isinstance(touch_nodes_by_id, dict):
            goals = set(touch_nodes_by_id.get(target_id, set()))
            search_dirs = {}
        else:
            goal_cache = context.setdefault("goal_nodes_by_id", {}) if context is not None else None
            cached_goals = goal_cache.get(target_id) if isinstance(goal_cache, dict) else None
            if cached_goals is not None:
                goals, search_dirs = cached_goals
            else:
                goals, search_dirs = self.goal_nodes(state, grid, target)
                if isinstance(goal_cache, dict):
                    goal_cache[target_id] = (goals, search_dirs)
        if not goals:
            return {
                "ok": True,
                "target_id": target_id,
                "status": "unreachable_static",
                "kind": kind,
                **destination_payload,
            }

        touch_nodes_by_id = context.get("touch_nodes_by_id") if context else None
        transition_blocked = self.touch_boundary_nodes(state, grid, target_id, touch_nodes_by_id) if avoid_transitions else set()
        base_blocking_by_node = context.get("blocking_by_node") if context else None
        if isinstance(base_blocking_by_node, dict):
            blocking_by_node = {
                node: obj
                for node, obj in base_blocking_by_node.items()
                if int(obj.get("id") or 0) != target_id
            }
        else:
            blocking_by_node = self.blocking_nodes_by_object(
                state,
                grid,
                None if kind == "approach" else target_id,
            )
        dynamic_blocked = set(blocking_by_node)
        blocked = set(transition_blocked) | dynamic_blocked
        route = self.route_between_nodes(grid, start, goals, blocked)

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
                            **destination_payload,
                        }
            return {
                "ok": True,
                "target_id": target_id,
                "status": "unreachable_static",
                "kind": kind,
                "blocked_by_transition": avoid_transitions,
                **destination_payload,
            }

        action = "touch" if kind == "touch" else "approach" if kind == "approach" else "search"
        result = {
            "ok": True,
            "target_id": target_id,
            "status": "transition" if kind != "approach" and transition_candidate(target) else "reachable",
            "kind": kind,
            "action": action,
            "route": route,
        }
        if kind != "approach" and transition_candidate(target):
            result["transition_candidate"] = True
            result.update(destination_payload)
        if kind == "search":
            route_nodes = route.get("nodes") or []
            route_goal = None
            if route_nodes:
                route_goal = grid_node(grid, int(route_nodes[-1]["x"]), int(route_nodes[-1]["y"]))
            direction = search_dirs.get(route_goal, int(state.get("party_direction") or 0))
            result["required_direction"] = DIR_TO_BUTTON.get(direction, "down")
        elif kind == "approach":
            route_nodes = route.get("nodes") or []
            route_goal = None
            if route_nodes:
                route_goal = grid_node(grid, int(route_nodes[-1]["x"]), int(route_nodes[-1]["y"]))
            direction = search_dirs.get(route_goal, int(state.get("party_direction") or 0))
            requirement = approach_details_by_node.get(route_goal, {})
            result["required_direction"] = DIR_TO_BUTTON.get(direction, "down")
            result["approach"] = dict(requirement)
            result["affordance"] = {
                **dict(requirement),
                "kind": "item_approach",
                "status": result["status"],
                "steps": int(route.get("steps") or 0),
                "required_direction": result["required_direction"],
            }
        return result

    def navigation_step(
        self,
        state: dict,
        target_id: int,
        approach_item_id: int | None = None,
    ) -> dict:
        state_info = self.live_info("state")
        if not state_info.get("available") or state_info.get("stale"):
            return {"ok": False, "status": "state_stale", "state_file": state_info, "target_id": target_id}
        control = self.control_state(state, state_info)
        phase = control["phase"]
        if phase in {"action_pending", "game_script", "entering_scene"}:
            return {
                "ok": True,
                "status": "waiting_for_control",
                "control": control,
                "target_id": target_id,
            }
        if phase == "battle":
            return {"ok": False, "status": "in_battle", "target_id": target_id}
        if phase == "dialog":
            return {"ok": False, "status": "in_dialog", "target_id": target_id}
        if phase == "shop":
            return {"ok": False, "status": "in_shop", "target_id": target_id}
        if state.get("in_main_game") is False:
            return {"ok": False, "status": "not_in_main_game", "target_id": target_id}
        if not self.room_state_stable(state):
            return {"ok": False, "status": "entering_scene" if state.get("entering_scene") else "unstable_room_state", "target_id": target_id}

        with self.state_lock:
            observation_id = self.navigation_transition_requests.get(target_id)
            request = self.observations.get(observation_id)
            if observation_id and request is None:
                self.navigation_transition_requests.pop(target_id, None)
                pending = self.pending_transition_observation
                if isinstance(pending, dict) and pending.get("observation_id") == observation_id:
                    self.pending_transition_observation = None
            if request is not None and request.get("status") == "completed":
                completed = self.observations.consume(observation_id, owner="navigation")
                self.navigation_transition_requests.pop(target_id, None)
                transition = (completed or {}).get("result", {}).get("transition")
                if isinstance(transition, dict):
                    return {
                        "ok": True,
                        "status": "transitioned",
                        "target_id": target_id,
                        "transition": transition,
                        "observation_id": observation_id,
                    }
            if request is not None and request.get("status") in {"cancelled", "timed_out"}:
                self.observations.consume(observation_id, owner="navigation")
                self.navigation_transition_requests.pop(target_id, None)
                pending = self.pending_transition_observation
                if isinstance(pending, dict) and pending.get("observation_id") == observation_id:
                    self.pending_transition_observation = None
                request = None

            pending = self.pending_transition_observation
            if (
                request is not None
                and request.get("status") == "pending"
                and isinstance(pending, dict)
                and pending.get("object_id") == target_id
            ):
                self.observe_pending_transition_destination(state)
                request = self.observations.get(observation_id)
                if request is not None and request.get("status") == "completed":
                    completed = self.observations.consume(observation_id, owner="navigation")
                    self.navigation_transition_requests.pop(target_id, None)
                    transition = (completed or {}).get("result", {}).get("transition")
                    return {
                        "ok": True,
                        "status": "transitioned",
                        "target_id": target_id,
                        "transition": transition,
                        "observation_id": observation_id,
                    }
                pending = self.pending_transition_observation
                if request is not None and request.get("status") == "pending":
                    return {
                        "ok": True,
                        "status": "transition_observing",
                        "target_id": target_id,
                        "observation_id": observation_id,
                        "stable_snapshots": int((pending or {}).get("stable_snapshots") or 0),
                        "required_snapshots": TRANSITION_STABLE_SNAPSHOTS,
                    }

        route = self.route_to_object(
            state,
            target_id,
            avoid_transitions=True,
            approach_item_id=approach_item_id,
        )
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
                segment_moves = moves[:segment_steps]
                action = self.write_action(segment_moves, 1)
                route_nodes = interaction_route.get("nodes") or []
                expected_node = route_nodes[segment_steps] if len(route_nodes) > segment_steps else None
                remaining_steps = max(0, len(moves) - segment_steps)
                self.remember_navigation_route(
                    state,
                    target_id,
                    interaction_route,
                    segment_steps,
                    remaining_steps,
                    "blocked_by_interactable",
                )
                return {
                    "ok": True,
                    "status": "stepping",
                    "target_id": target_id,
                    "move": move,
                    "segment_steps": segment_steps,
                    "segment_moves": segment_moves,
                    "expected_node": expected_node,
                    "action": action,
                    "remaining_steps": remaining_steps,
                    "route": interaction_route,
                    "blocked_route": route.get("blocked_route"),
                    "blocker": route.get("blocker"),
                }

            direction = interaction.get("required_direction")
            buttons = [button for button in (direction, "search") if button]
            blocker = route.get("blocker") or {}
            blocker_id = int(blocker.get("id") or target_id)
            with self.state_lock:
                observation_id = self.mark_pending_interaction_observation(
                    state,
                    blocker_id,
                    via_target_id=target_id,
                    status="interacting",
                )
            try:
                action = self.write_action(buttons, 1)
            except Exception:
                self.observations.cancel(observation_id, "action_write_failed")
                if (
                    isinstance(self.pending_interaction_observation, dict)
                    and self.pending_interaction_observation.get("observation_id") == observation_id
                ):
                    self.pending_interaction_observation = None
                raise
            self.observations.update_payload(observation_id, action=action, armed=True)
            if isinstance(self.pending_interaction_observation, dict):
                self.pending_interaction_observation["action"] = action
                self.pending_interaction_observation["armed"] = True
            return {
                "ok": True,
                "status": "interacting",
                "target_id": target_id,
                "observation_id": observation_id,
                "action": action,
                "route": route.get("blocked_route"),
                "blocker": route.get("blocker"),
                "required_direction": direction,
            }

        if route.get("status") not in {"reachable", "transition"}:
            self.clear_pending_transition_observation(target_id)
            return {
                "ok": False,
                "status": route.get("status") or "unreachable",
                "target_id": target_id,
                "route": route.get("route"),
                "blocker": route.get("blocker"),
            }

        moves = route.get("route", {}).get("moves") or []
        if not moves:
            if route.get("kind") == "approach":
                target = self.target_object(state, target_id)
                pos = state.get("party_position") or {}
                current_pos = (int(pos.get("x") or 0), int(pos.get("y") or 0))
                current_direction = int(state.get("party_direction") or 0)
                approach = route.get("approach") if isinstance(route.get("approach"), dict) else {}
                distance_limit = int(approach.get("distance_limit") or 0)
                if target is not None and facing_event_object_hits(
                    current_pos,
                    current_direction,
                    target,
                    distance_limit,
                ):
                    return {
                        "ok": True,
                        "status": "approached",
                        "target_id": target_id,
                        "route": route.get("route"),
                        "required_direction": DIR_TO_BUTTON.get(current_direction, "down"),
                        "approach": approach,
                        "affordance": route.get("affordance"),
                    }
                required_direction = route.get("required_direction")
                current_direction_button = DIR_TO_BUTTON.get(current_direction, "down")
                if required_direction and required_direction != current_direction_button:
                    action = self.write_action([required_direction], 1)
                    return {
                        "ok": True,
                        "status": "facing",
                        "target_id": target_id,
                        "action": action,
                        "route": route.get("route"),
                        "required_direction": required_direction,
                        "approach": approach,
                        "affordance": route.get("affordance"),
                    }
                return {
                    "ok": False,
                    "status": "needs_direction",
                    "target_id": target_id,
                    "route": route.get("route"),
                    "required_direction": required_direction,
                    "approach": approach,
                    "affordance": route.get("affordance"),
                }
            if route.get("kind") == "search":
                target = self.target_object(state, target_id)
                pos = state.get("party_position") or {}
                current_pos = (int(pos.get("x") or 0), int(pos.get("y") or 0))
                current_direction = int(state.get("party_direction") or 0)
                if target is not None and search_hits(current_pos, current_direction, target):
                    with self.state_lock:
                        observation_id = self.mark_pending_interaction_observation(
                            state,
                            target_id,
                            via_target_id=target_id,
                            status="interacting",
                        )
                    try:
                        action = self.write_action(["search"], 1)
                    except Exception:
                        self.observations.cancel(observation_id, "action_write_failed")
                        if (
                            isinstance(self.pending_interaction_observation, dict)
                            and self.pending_interaction_observation.get("observation_id") == observation_id
                        ):
                            self.pending_interaction_observation = None
                        raise
                    self.observations.update_payload(observation_id, action=action, armed=True)
                    if isinstance(self.pending_interaction_observation, dict):
                        self.pending_interaction_observation["action"] = action
                        self.pending_interaction_observation["armed"] = True
                    return {
                        "ok": True,
                        "status": "interacting",
                        "target_id": target_id,
                        "observation_id": observation_id,
                        "action": action,
                        "route": route.get("route"),
                        "required_direction": DIR_TO_BUTTON.get(current_direction, "down"),
                    }
                required_direction = route.get("required_direction")
                current_direction_button = DIR_TO_BUTTON.get(current_direction, "down")
                if required_direction and required_direction != current_direction_button:
                    action = self.write_action([required_direction], 1)
                    return {
                        "ok": True,
                        "status": "facing",
                        "target_id": target_id,
                        "action": action,
                        "route": route.get("route"),
                        "required_direction": required_direction,
                    }
                return {
                    "ok": False,
                    "status": "needs_direction",
                    "target_id": target_id,
                    "route": route.get("route"),
                    "required_direction": required_direction,
                }
            if (
                route.get("kind") == "touch"
                and self.pending_touch_route_confirmation(state, target_id) is not None
            ):
                return {
                    "ok": True,
                    "status": "transition_observing",
                    "target_id": target_id,
                    "stable_snapshots": 0,
                    "required_snapshots": TRANSITION_STABLE_SNAPSHOTS,
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
        segment_moves = moves[:segment_steps]
        route_nodes = route.get("route", {}).get("nodes") or []
        expected_node = route_nodes[segment_steps] if len(route_nodes) > segment_steps else None
        remaining_steps = max(0, len(moves) - segment_steps)
        observation_id = None
        if route.get("status") == "transition" and segment_steps >= len(moves):
            with self.state_lock:
                observation_id = self.mark_pending_transition_observation(state, target_id)
        try:
            action = self.write_action(segment_moves, 1)
        except Exception:
            if observation_id:
                with self.state_lock:
                    self.observations.cancel(observation_id, "action_write_failed")
                    self.navigation_transition_requests.pop(target_id, None)
                    pending = self.pending_transition_observation
                    if isinstance(pending, dict) and pending.get("observation_id") == observation_id:
                        self.pending_transition_observation = None
            raise
        self.remember_navigation_route(
            state,
            target_id,
            route.get("route") or {},
            segment_steps,
            remaining_steps,
            route.get("status") or "reachable",
        )
        result = {
            "ok": True,
            "status": "stepping",
            "target_id": target_id,
            "move": move,
            "segment_steps": segment_steps,
            "segment_moves": segment_moves,
            "expected_node": expected_node,
            "action": action,
            "remaining_steps": remaining_steps,
            "route": route.get("route"),
        }
        if route.get("approach") is not None:
            result["approach"] = route.get("approach")
        if route.get("affordance") is not None:
            result["affordance"] = route.get("affordance")
        if observation_id:
            result["observation_id"] = observation_id
        return result

    def reachable_nodes(
        self,
        grid: dict,
        start: tuple[int, int],
        blocked: set[tuple[int, int]],
    ) -> tuple[list[tuple[int, int]], dict[tuple[int, int], int]]:
        order, distance, _ = self.navigation_distance_field(grid, start, blocked)
        return order, distance

    def navigation_graph(
        self,
        grid: dict,
    ) -> dict[tuple[int, int], tuple[tuple[str, tuple[int, int]], ...]]:
        key = (
            grid["origin"],
            grid["step"],
            int(grid["width"]),
            int(grid["height"]),
            str(grid["bits"]),
        )
        with self.navigation_graph_lock:
            cached = self.navigation_graph_cache.get(key)
            if cached is not None:
                self.navigation_graph_cache.move_to_end(key)
                return cached

        graph: dict[tuple[int, int], tuple[tuple[str, tuple[int, int]], ...]] = {}
        for y in range(grid["height"]):
            for x in range(grid["width"]):
                node = (x, y)
                if not nav_bit(grid, node):
                    continue
                neighbors = []
                for move, delta in MOVE_DELTAS.items():
                    next_node = (x + delta[0], y + delta[1])
                    if nav_bit(grid, next_node):
                        neighbors.append((move, next_node))
                graph[node] = tuple(neighbors)

        with self.navigation_graph_lock:
            existing = self.navigation_graph_cache.get(key)
            if existing is not None:
                self.navigation_graph_cache.move_to_end(key)
                return existing
            self.navigation_graph_cache[key] = graph
            while len(self.navigation_graph_cache) > NAVIGATION_GRAPH_CACHE_SIZE:
                self.navigation_graph_cache.popitem(last=False)
        return graph

    def navigation_distance_field(
        self,
        grid: dict,
        start: tuple[int, int],
        blocked: set[tuple[int, int]],
    ) -> tuple[
        list[tuple[int, int]],
        dict[tuple[int, int], int],
        dict[tuple[int, int], tuple[tuple[int, int], str]],
    ]:
        blocked = set(blocked)
        blocked.discard(start)
        queue = deque([start])
        order = [start]
        distance = {start: 0}
        parent: dict[tuple[int, int], tuple[tuple[int, int], str]] = {}
        graph = self.navigation_graph(grid)

        while queue:
            node = queue.popleft()
            for move, next_node in graph.get(node, ()):
                if next_node in distance or next_node in blocked:
                    continue
                distance[next_node] = distance[node] + 1
                parent[next_node] = (node, move)
                order.append(next_node)
                queue.append(next_node)
        return order, distance, parent

    def touch_terminal_goal(
        self,
        grid: dict,
        start: tuple[int, int],
        order: list[tuple[int, int]],
        distance: dict[tuple[int, int], int],
        goals: set[tuple[int, int]],
        terminal_blocked: set[tuple[int, int]],
    ) -> tuple[tuple[int, int], tuple[int, int] | None, int] | None:
        if start in goals:
            return start, None, 0
        graph = self.navigation_graph(grid)
        for node in order:
            for _, next_node in graph.get(node, ()):
                if next_node in goals and next_node not in terminal_blocked:
                    return next_node, node, distance[node] + 1
        return None

    def distance_field_path(
        self,
        start: tuple[int, int],
        goal: tuple[int, int],
        parent: dict[tuple[int, int], tuple[tuple[int, int], str]],
        terminal_parent: tuple[int, int] | None = None,
    ) -> list[tuple[int, int]]:
        current = terminal_parent if terminal_parent is not None else goal
        nodes = [current]
        while current != start:
            current = parent[current][0]
            nodes.append(current)
        nodes.reverse()
        if terminal_parent is not None:
            nodes.append(goal)
        return nodes

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
        control = self.control_state(state)
        if not control["navigation_ready"]:
            return {
                "status": "waiting_for_control",
                "control_phase": control["phase"],
                "targets": [],
            }
        self.attach_logical_room_fields(state)
        environment_payload = self.navigation_environment_payload(state)
        if not self.room_state_stable(state):
            return {"status": "entering_scene" if state.get("entering_scene") else "unstable_room_state", "targets": [], **environment_payload}
        grid = decode_nav_grid(state)
        if grid is None:
            return {"status": "no_nav_grid", "targets": [], **environment_payload}
        pos = state.get("party_position") or {}
        start = grid_node(grid, int(pos.get("x") or 0), int(pos.get("y") or 0))
        if start is None or not nav_bit(grid, start):
            return {"status": "bad_start", "targets": [], **environment_payload}

        interactables = [obj for obj in state.get("objects") or [] if obj.get("interactable")]
        transition_context = self.navigation_transition_context(state)
        affordance_context = self.navigation_affordance_context(state)
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
        safe_order, safe_distance, _ = self.navigation_distance_field(
            grid,
            start,
            all_touch_nodes | dynamic_blocked,
        )
        no_dynamic_order, no_dynamic_distance, no_dynamic_parent = self.navigation_distance_field(
            grid,
            start,
            all_touch_nodes,
        )
        no_touch_order, no_touch_distance, _ = self.navigation_distance_field(
            grid,
            start,
            dynamic_blocked,
        )
        approach_requirements = self.approach_requirements_by_target(state)
        interaction_by_blocker_id: dict[int, dict | None] = {}

        targets = []
        for obj in interactables:
            target_id = int(obj.get("id") or 0)
            native_kind = trigger_kind(int(obj.get("trigger_mode") or 0))
            kind = native_kind
            status = "unreachable_static"
            steps = None
            required_direction = None
            blocker = None
            goals: set[tuple[int, int]] = set()
            strict_goal: tuple[int, int] | None = None
            relaxed_goal: tuple[int, int] | None = None
            relaxed_terminal_parent: tuple[int, int] | None = None
            no_touch_goal: tuple[int, int] | None = None
            approach_details_by_node: dict[tuple[int, int], dict] = {}
            approach = None

            if kind == "none" and approach_requirements.get(target_id):
                kind = "approach"
                goals, approach_dirs, approach_details_by_node = self.approach_goal_nodes(
                    state,
                    grid,
                    obj,
                    approach_requirements[target_id],
                )
                strict_goal = next((node for node in safe_order if node in goals), None)
                relaxed_goal = next((node for node in no_dynamic_order if node in goals), None)
                no_touch_goal = next((node for node in no_touch_order if node in goals), None)
                selected_goal = strict_goal or relaxed_goal or no_touch_goal
                if selected_goal is not None:
                    direction = approach_dirs.get(selected_goal, int(state.get("party_direction") or 0))
                    required_direction = DIR_TO_BUTTON.get(direction, "down")
                    approach = dict(approach_details_by_node.get(selected_goal) or {})
                elif approach_requirements[target_id]:
                    approach = dict(approach_requirements[target_id][0])
                if strict_goal is not None:
                    status = "reachable"
                    steps = safe_distance[strict_goal]
            elif kind == "search":
                goals, search_dirs = self.goal_nodes(state, grid, obj)
                strict_goal = next((node for node in safe_order if node in goals), None)
                relaxed_goal = next((node for node in no_dynamic_order if node in goals), None)
                no_touch_goal = next((node for node in no_touch_order if node in goals), None)
                if strict_goal is not None:
                    direction = search_dirs.get(strict_goal, int(state.get("party_direction") or 0))
                    status = "reachable"
                    steps = safe_distance[strict_goal]
                    required_direction = DIR_TO_BUTTON.get(direction, "down")
            elif kind == "touch":
                goals = touch_nodes_by_id.get(target_id, set())
                target_node = grid_node(grid, int(obj.get("x") or 0), int(obj.get("y") or 0))
                target_dynamic_blocked = set(dynamic_blocked)
                if target_node is not None:
                    target_dynamic_blocked.discard(target_node)
                strict_terminal = self.touch_terminal_goal(
                    grid,
                    start,
                    safe_order,
                    safe_distance,
                    goals,
                    target_dynamic_blocked,
                )
                relaxed_terminal = self.touch_terminal_goal(
                    grid,
                    start,
                    no_dynamic_order,
                    no_dynamic_distance,
                    goals,
                    set(),
                )
                no_touch_terminal = self.touch_terminal_goal(
                    grid,
                    start,
                    no_touch_order,
                    no_touch_distance,
                    goals,
                    target_dynamic_blocked,
                )
                if strict_terminal is not None:
                    strict_goal, _, steps = strict_terminal
                    status = "transition" if transition_candidate(obj) else "reachable"
                if relaxed_terminal is not None:
                    relaxed_goal, relaxed_terminal_parent, _ = relaxed_terminal
                if no_touch_terminal is not None:
                    no_touch_goal = no_touch_terminal[0]

            if strict_goal is None and relaxed_goal is not None:
                relaxed_path = self.distance_field_path(
                    start,
                    relaxed_goal,
                    no_dynamic_parent,
                    relaxed_terminal_parent,
                )
                blocking_object = next(
                    (
                        blocking_by_node.get(node)
                        for node in relaxed_path
                        if blocking_by_node.get(node) is not None
                        and int((blocking_by_node.get(node) or {}).get("id") or 0) != target_id
                    ),
                    None,
                )
                blocker_id = int((blocking_object or {}).get("id") or 0)
                if blocking_object is not None:
                    if blocker_id not in interaction_by_blocker_id:
                        transition_blocked = set(all_touch_nodes)
                        if kind == "touch":
                            transition_blocked.difference_update(goals)
                        interaction_by_blocker_id[blocker_id] = self.blocker_interaction_plan(
                            state,
                            grid,
                            start,
                            blocking_object,
                            transition_blocked,
                            dynamic_blocked,
                        )
                    interaction = interaction_by_blocker_id[blocker_id]
                    if interaction is not None:
                        status = "blocked_by_interactable"
                        steps = (interaction.get("route") or {}).get("steps")
                        required_direction = interaction.get("required_direction")
                        blocker = interaction.get("blocker")
            elif strict_goal is None and no_touch_goal is not None:
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
                target.update(self.transition_destination_for_state(state, target_id, transition_context))
            if blocker is not None:
                target["blocker"] = blocker
            if approach is not None:
                target["approach"] = approach
            affordances = self.item_approach_affordances(
                state,
                target_id,
                approach_requirements.get(target_id, []),
                grid=grid,
                reachable_order=safe_order,
                reachable_distance=safe_distance,
                fallback_order=no_touch_order,
            )
            if affordances:
                target["affordances"] = affordances
                if native_kind == "none":
                    selected = affordances[0]
                    target.update({
                        "kind": "approach",
                        "status": selected["status"],
                        "steps": selected["steps"],
                        "required_direction": selected["required_direction"],
                        "approach": selected,
                    })
            target = self.decorate_target_affordance(state, target, affordance_context)
            targets.append(target)
        return {
            "status": "ok",
            **environment_payload,
            "grid": {
                "origin": {"x": grid["origin"][0], "y": grid["origin"][1]},
                "step": {"x": grid["step"][0], "y": grid["step"][1]},
                "width": grid["width"],
                "height": grid["height"],
            },
            "targets": targets,
        }

    def lightweight_navigation(self, state: dict) -> dict:
        control = self.control_state(state)
        if not control["navigation_ready"]:
            return {
                "status": "waiting_for_control",
                "control_phase": control["phase"],
                "targets": [],
            }
        self.attach_logical_room_fields(state)
        if not self.room_state_stable(state):
            return {"status": "entering_scene" if state.get("entering_scene") else "unstable_room_state", "targets": []}
        grid = decode_nav_grid(state)
        if grid is None:
            return {"status": "no_nav_grid", "targets": []}
        targets = []
        affordance_context = self.navigation_affordance_context(state)
        for obj in state.get("objects") or []:
            if not obj.get("interactable"):
                continue
            target = {
                "id": int(obj.get("id") or 0),
                "kind": trigger_kind(int(obj.get("trigger_mode") or 0)),
                "status": "transition" if transition_candidate(obj) else "unknown",
                "steps": None,
                "required_direction": None,
                **({"transition_candidate": True} if transition_candidate(obj) else {}),
            }
            targets.append({**target, **self.target_affordance(state, target["id"], affordance_context)})
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
        state = self.read_live_state()
        state_info = self.live_info("state")
        control = self.control_state(state, state_info)
        stable_room_state = self.room_state_stable(state)
        client_stable_room_state = stable_room_state and control["owner"] == "client"
        if client_stable_room_state:
            self.attach_logical_room_fields(state)
        frame_info = self.live_info("frame")
        with self.state_lock:
            if state_info.get("available"):
                self.reset_history_if_frame_restarted(state)
                self.discover_native_transition_observation(state)
                self.observe_pending_transition_destination(state)
                if client_stable_room_state:
                    self.last_stable_room_state = state
                elif state.get("entering_scene"):
                    self.append_navigation_diagnostic("unstable_room_state_ignored", {"reason": "entering_scene"}, state)
                self.record_state_events(state)
                self.agent_runtime.observe_equipment_state(state)
                self.record_dialog_event(state)
                self.observe_dialog_gates(state)
                self.observe_pending_interaction_outcome(state)
            if state_info.get("available") and client_stable_room_state:
                state["_navigation"] = self.lightweight_navigation(state)
                if self.pending_transition_observation is None:
                    state["_exploration"] = self.update_exploration(state)
                else:
                    frozen_source = self.pending_transition_observation.get("frozen_source_state")
                    state["_exploration"] = self.current_exploration(frozen_source if isinstance(frozen_source, dict) else state)
            recent_actions = list(self.action_log[-20:])
            recent_events = list(self.event_log[-20:])
            recent_dialogs = list(self.dialog_history[-20:])
            timeline = {
                "raw_frame": state.get("raw_frame"),
                "logical_frame": state.get("logical_frame", state.get("frame")),
                "logical_frame_offset": self.logical_frame_offset,
                "last_raw_frame": self.last_raw_frame,
                "last_logical_frame": self.last_logical_frame,
                "pending_checkpoint_frame_reset": self.pending_checkpoint_frame_reset is not None,
            }
        state["_debug"] = {
            "runtime_dir": str(self.runtime_dir),
            "asset_dir": str(self.asset_dir),
            "game_binary": str(self.game_binary),
            "state_file": state_info,
            "frame_file": frame_info,
            "frame_available": frame_info["available"],
            "live_transport": self.live_transport_snapshot(),
            "state_stale": state_info.get("stale", True),
            "frame_stale": frame_info.get("stale", True),
            "runtime_stale": state_info.get("stale", True),
            "action_pending": self.action_pending(state),
            "stale_after_seconds": STALE_AFTER_SECONDS,
            "recent_actions": recent_actions,
            "recent_events": recent_events,
            "recent_dialogs": recent_dialogs,
            "action_result": read_optional_json(self.action_result_path),
            "action_result_file": self.file_info(self.action_result_path),
            "agent_config": {
                "models": sorted(ALLOWED_DEEPSEEK_MODELS),
                "default_model": self.deepseek_model(),
            },
            "recent_agent_decisions": self.read_agent_log(limit=20),
            "agent_status": self.agent_status_snapshot(),
            "agent_session": self.agent_runtime.compact_snapshot(),
            "dialog_advance_status": self.dialog_advance_status_snapshot(),
            "timeline": timeline,
        }
        state["control"] = control
        self.agent_runtime.observe_game_instance(state.get("game_instance_id"))
        state["_debug"]["agent_session"] = self.agent_runtime.compact_snapshot()
        return state

    def state_stream_payload(self) -> bytes:
        with self.state_stream_snapshot_lock:
            now = time.monotonic()
            action_pending = self.action_pending()
            if (
                self.state_stream_snapshot_payload is not None
                and now - self.state_stream_snapshot_at < STATE_STREAM_SNAPSHOT_INTERVAL_SECONDS
                and self.state_stream_snapshot_action_pending == action_pending
            ):
                return self.state_stream_snapshot_payload
            payload = json.dumps(self.snapshot(), ensure_ascii=False).encode("utf-8")
            self.state_stream_snapshot_payload = payload
            self.state_stream_snapshot_at = time.monotonic()
            self.state_stream_snapshot_action_pending = action_pending
            return payload

    def dialog_advance_int(self, value, default: int, lower: int, upper: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(lower, min(parsed, upper))

    def normalize_dialog_advance_args(self, args: dict | None = None) -> dict:
        args = args if isinstance(args, dict) else {}
        return {
            "max_presses": self.dialog_advance_int(
                args.get("max_presses"),
                DEFAULT_DIALOG_ADVANCE_MAX_PRESSES,
                1,
                DEFAULT_DIALOG_ADVANCE_MAX_PRESSES,
            ),
            "delay_ms": self.dialog_advance_int(args.get("delay_ms"), DEFAULT_DIALOG_ADVANCE_DELAY_MS, 100, 2000),
            "stop_on_repeat": args.get("stop_on_repeat") is not False,
        }

    def wait_for_action_clear(self, timeout: float = DIALOG_ADVANCE_ACTION_TIMEOUT) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.action_pending():
                return True
            time.sleep(0.05)
        return not self.action_pending()

    def dialog_advance_stop_status(self, state: dict) -> str | None:
        if state.get("status") == "waiting_for_game":
            return "waiting_for_game"
        if state.get("status") == "state_parse_error":
            return "state_parse_error"
        if state.get("_debug", {}).get("runtime_stale"):
            return "runtime_stale"
        if state.get("in_battle"):
            return "in_battle"
        if state.get("in_main_game") is False:
            return "not_in_main_game"
        if bool((state.get("choice") or {}).get("active")):
            return "choice_active"
        if not (state.get("dialog") or {}).get("active"):
            return "dialog_inactive"
        return None

    def dialog_advance_visible(self, state: dict) -> bool:
        dialog = state.get("dialog") if isinstance(state, dict) else {}
        if not isinstance(dialog, dict) or not dialog.get("active"):
            return False
        if "visible" in dialog:
            return bool(dialog.get("visible"))
        return not bool(dialog.get("held"))

    def observe_dialog_gates(self, state: dict, *, confirm_inactive: bool = False) -> None:
        for request in self.observations.active(kind="dialog_gate"):
            observation_id = request["id"]
            if self.dialog_advance_cancel.is_set():
                self.observations.cancel(observation_id, "cancelled")
                continue
            stop_status = self.dialog_advance_stop_status(state)
            if stop_status == "dialog_inactive":
                if not confirm_inactive:
                    continue
                payload = request.get("payload", {})
                if not payload.get("inactive_started"):
                    self.observations.update_payload(
                        observation_id,
                        inactive_started=True,
                        inactive_polls=0,
                    )
                    continue
                inactive_polls = int(payload.get("inactive_polls") or 0) + 1
                if inactive_polls >= DIALOG_ADVANCE_INACTIVE_CONFIRM_POLLS:
                    self.observations.complete(
                        observation_id,
                        {"status": "dialog_inactive", "state": state},
                    )
                else:
                    self.observations.update_payload(
                        observation_id,
                        inactive_polls=inactive_polls,
                    )
                continue
            if stop_status:
                self.observations.complete(
                    observation_id,
                    {"status": stop_status, "state": state},
                )
                continue
            if request.get("payload", {}).get("inactive_started"):
                self.observations.update_payload(
                    observation_id,
                    inactive_started=False,
                    inactive_polls=0,
                )
            if self.dialog_advance_visible(state):
                self.observations.complete(
                    observation_id,
                    {"status": "ready", "state": state},
                )

    def dialog_advance_wait_for_visible(self, state: dict) -> tuple[dict, str | None]:
        observation_id = self.observations.register(
            "dialog_gate",
            owner="dialog_advance",
            timeout=DIALOG_ADVANCE_VISIBILITY_TIMEOUT,
            payload={"inactive_started": False, "inactive_polls": 0},
        )
        self.dialog_advance_observation_id = observation_id
        try:
            self.observe_dialog_gates(state, confirm_inactive=True)
            while True:
                request = self.observations.get(observation_id)
                if request is None:
                    return state, "cancelled"
                status = request.get("status")
                if status == "completed":
                    completed = self.observations.consume(observation_id, owner="dialog_advance")
                    result = (completed or {}).get("result") or {}
                    observed_state = result.get("state") if isinstance(result.get("state"), dict) else state
                    observed_status = result.get("status")
                    return observed_state, None if observed_status == "ready" else observed_status
                if status == "timed_out":
                    self.observations.consume(observation_id, owner="dialog_advance")
                    return state, "dialog_visibility_timeout"
                if status == "cancelled":
                    self.observations.consume(observation_id, owner="dialog_advance")
                    return state, "cancelled"

                wait_interval = (
                    DIALOG_ADVANCE_INACTIVE_CONFIRM_INTERVAL
                    if request.get("payload", {}).get("inactive_started")
                    else DIALOG_ADVANCE_VISIBILITY_POLL_INTERVAL
                )
                self.observations.wait(
                    observation_id,
                    timeout=wait_interval,
                    cancel_event=self.dialog_advance_cancel,
                )
                request = self.observations.get(observation_id)
                if request is not None and request.get("status") == "pending":
                    state = self.snapshot()
                    self.observe_dialog_gates(state, confirm_inactive=True)
        finally:
            if self.dialog_advance_observation_id == observation_id:
                self.dialog_advance_observation_id = None

    def advance_dialog(self, args: dict | None = None) -> dict:
        options = self.normalize_dialog_advance_args(args)
        if not self.dialog_advance_lock.acquire(blocking=False):
            return {"ok": False, "status": "already_running", **options}
        try:
            self.dialog_advance_cancel.clear()
            self.begin_dialog_advance_status(options)
            presses = 0
            dialogs_at_start = len(self.dialog_history)

            def finish_result(status: str, *, ok: bool = True) -> dict:
                self.finish_dialog_advance_status(status, presses, options)
                return {
                    "ok": ok,
                    "status": status,
                    "presses": presses,
                    "dialogs": self.dialog_history[dialogs_at_start:],
                    **options,
                }

            state = self.snapshot()
            if bool((state.get("choice") or {}).get("active")):
                return finish_result("choice_active")
            state, stop_status = self.dialog_advance_wait_for_visible(state)
            if stop_status:
                return finish_result(stop_status, ok=stop_status in {"dialog_inactive", "choice_active", "cancelled"})

            previous_active_key = self.dialog_advance_active_event_key(state)
            repeated_active_presses = 0
            observed_event_keys: set[tuple] = set()
            seen_opponent_keys: set[tuple] = set()
            event_observation = self.dialog_advance_observe_events(
                state,
                observed_event_keys,
                seen_opponent_keys,
                detect_repeats=False,
            )
            has_dialog_event_info = bool(event_observation["has_event_info"])

            while True:
                if self.dialog_advance_cancel.is_set():
                    return finish_result("cancelled")
                if bool((state.get("choice") or {}).get("active")):
                    return finish_result("choice_active")
                state, stop_status = self.dialog_advance_wait_for_visible(state)
                if stop_status:
                    return finish_result(stop_status, ok=stop_status in {"dialog_inactive", "choice_active", "cancelled"})

                event_observation = self.dialog_advance_observe_events(
                    state,
                    observed_event_keys,
                    seen_opponent_keys,
                    detect_repeats=presses > 0,
                )
                has_dialog_event_info = has_dialog_event_info or bool(event_observation["has_event_info"])
                if options["stop_on_repeat"] and event_observation["repeated"]:
                    return finish_result("repeated_dialog_opponent_line")
                active_key = self.dialog_advance_active_event_key(state)
                if presses > 0:
                    if not has_dialog_event_info and active_key and active_key == previous_active_key:
                        repeated_active_presses += 1
                    else:
                        repeated_active_presses = 0
                        previous_active_key = active_key
                    if options["stop_on_repeat"] and repeated_active_presses >= 2:
                        return finish_result("repeated_dialog")

                if presses >= options["max_presses"]:
                    return finish_result("max_presses")
                if self.action_pending(state):
                    return finish_result("action_pending_timeout", ok=False)

                self.write_action(
                    [DIALOG_ADVANCE_BUTTON],
                    1,
                    control_action="advance_dialog",
                    control_state=state,
                )
                presses += 1
                self.update_dialog_advance_status(presses=presses)
                if not self.wait_for_action_clear():
                    return finish_result("action_pending_timeout", ok=False)

                time.sleep(options["delay_ms"] / 1000)
                if self.dialog_advance_cancel.is_set():
                    return finish_result("cancelled")
                state = self.snapshot()
        finally:
            self.dialog_advance_lock.release()

    def cancel_dialog_advance(self) -> dict:
        running = self.dialog_advance_lock.locked()
        if running:
            self.dialog_advance_cancel.set()
            self.observations.cancel(self.dialog_advance_observation_id, "cancelled")
            self.update_dialog_advance_status(status="cancelling")
        return {"ok": True, "status": "cancelling" if running else "idle", "running": running}

    def action_preview(self, tool: str, args: dict) -> dict:
        return {"tool": tool, "args": args, "dry_run": True}

    def decision_base(self, state: dict, navigation: dict, dialogs: dict, events: list[dict]) -> dict:
        room = state.get("room") or {}
        identity = self.room_identity(state)
        pos = state.get("party_position") or {}
        dialog = state.get("dialog") or {}
        return {
            "backend": "rules",
            "summary": "",
            "recommended_intent": "wait_runtime",
            "recommended_action": self.action_preview("no_op", {}),
            "evidence": {
                "status": state.get("status", "ok"),
                "frame": state.get("frame"),
                "scene_id": state.get("scene_id"),
                "map_id": state.get("map_id"),
                "room_id": identity["id"] if identity else None,
                "logical_room_id": identity["id"] if identity else None,
                "logical_room_label": identity.get("logical_room_label") if identity else None,
                "room": room.get("key"),
                "room_key": identity["room_key"] if identity else room.get("key"),
                "region_key": identity["region_key"] if identity else state.get("region_key"),
                "nav_room_id": identity["nav_room_id"] if identity else None,
                "legacy_room_id": identity.get("legacy_room_id") if identity else None,
                "legacy_nav_room_id": identity.get("legacy_nav_room_id") if identity else None,
                "position": {"x": pos.get("x"), "y": pos.get("y")},
                "dialog_active": bool(dialog.get("active")),
                "navigation_status": navigation.get("status"),
                "navigation_targets": len(navigation.get("targets") or []),
                "recent_dialogs": len(dialogs.get("dialogs") or []),
                "recent_events": len(events),
            },
            "risk": "未识别到明确风险。",
            "alternatives": [],
        }

    def decision_for_route(self, state: dict, target: dict, route: dict) -> tuple[dict, str]:
        current_direction = DIR_TO_BUTTON.get(int(state.get("party_direction") or 0), "down")
        moves = (route.get("route") or {}).get("moves") or []
        kind = route.get("kind") or target.get("kind")
        required_direction = route.get("required_direction") or target.get("required_direction")
        action = self.action_preview("navigation_step", {"id": target.get("id")})
        if moves:
            return action, f"移动 {moves[0]}"
        if kind == "search":
            if required_direction and required_direction != current_direction:
                return action, f"面向 {required_direction}"
            return action, "search"
        return action, "接触目标"

    def executable_task_targets(self, targets: list[dict]) -> list[tuple[dict, dict, str]]:
        """Match active task annotations to targets that can be acted on now."""
        matches = []
        pending_review_ids = {
            str(item.get("id"))
            for item in self.task_review_context().get("tasks") or []
        }
        for task in self.task_ledger():
            # The deterministic Rules backend cannot maintain inferred tasks.
            # Stale tasks therefore lose their override power until an LLM reviews them.
            if str(task.get("id")) in pending_review_ids:
                continue
            for target in targets:
                if target.get("status") not in {"reachable", "transition"}:
                    continue
                try:
                    target_id = int(target.get("id") or 0)
                except (TypeError, ValueError):
                    target_id = 0
                reason = None
                if task.get("object_id") and target_id == task.get("object_id"):
                    reason = "object_id"
                elif task.get("transition_key") and target.get("transition_key") == task.get("transition_key"):
                    reason = "transition_key"
                elif (
                    task.get("room_id")
                    and target.get("transition_candidate")
                    and target.get("known_destination_room_id") == task.get("room_id")
                ):
                    reason = "room_id"
                if reason:
                    matches.append((task, target, reason))
        def key(item):
            task, target, _ = item
            try:
                steps = int(target.get("steps"))
            except (TypeError, ValueError):
                steps = 999999
            try:
                priority = int(task.get("priority") or 0)
            except (TypeError, ValueError):
                priority = 0
            try:
                target_id = int(target.get("id") or 0)
            except (TypeError, ValueError):
                target_id = 0
            return (-priority, steps, target_id)
        return sorted(matches, key=key)

    def rules_decide_agent(self, state: dict | None = None) -> dict:
        self.update_agent_status(
            backend="rules",
            model=None,
            phase="inspecting_state",
            current_tool=None,
            message="Inspecting local state.",
        )
        state = state or self.read_live_state()
        state_info = self.live_info("state")
        frame_info = self.live_info("frame")
        navigation = self.navigation(state)
        dialogs = self.dialogs_view(limit=20)
        events = self.event_log[-20:]
        decision = self.decision_base(state, navigation, dialogs, events)

        if state.get("status") == "waiting_for_game":
            decision.update(
                {
                    "summary": "游戏尚未写入状态快照。",
                    "recommended_intent": "wait_runtime",
                    "recommended_action": self.action_preview("no_op", {"reason": "waiting_for_game"}),
                    "risk": "当前没有可靠的游戏状态可用。",
                }
            )
            self.append_agent_decision(state, decision)
            return decision

        if state.get("status") == "state_parse_error":
            decision.update(
                {
                    "summary": "最新状态快照无法解析。",
                    "recommended_intent": "wait_runtime",
                    "recommended_action": self.action_preview("no_op", {"reason": "state_parse_error"}),
                    "risk": "基于格式错误的状态行动不安全。",
                }
            )
            self.append_agent_decision(state, decision)
            return decision

        if not state_info.get("available") or state_info.get("stale"):
            decision["evidence"]["state_file"] = state_info
            decision["evidence"]["frame_file"] = frame_info
            decision.update(
                {
                    "summary": "运行时状态已过期或不可用。",
                    "recommended_intent": "wait_runtime",
                    "recommended_action": self.action_preview("no_op", {"reason": "runtime_stale"}),
                    "risk": "可见游戏状态可能已经与实时 socket 数据不一致。",
                }
            )
            self.append_agent_decision(state, decision)
            return decision

        if state.get("in_battle"):
            decision.update(
                {
                    "summary": "队伍似乎处于战斗中。",
                    "recommended_intent": "wait_runtime",
                    "recommended_action": self.action_preview("no_op", {"reason": "battle_state_not_modeled"}),
                    "risk": "战斗 UI 状态暴露不足，无法给出可靠的 dry-run 建议。",
                }
            )
            self.append_agent_decision(state, decision)
            return decision

        choice = state.get("choice") if isinstance(state.get("choice"), dict) else None
        if choice and choice.get("active"):
            decision.update(
                {
                    "summary": "剧情选单正在等待显式选择。",
                    "recommended_intent": "wait_runtime",
                    "recommended_action": self.action_preview("no_op", {"reason": "choice_requires_model"}),
                    "evidence": {**decision["evidence"], "choice": choice},
                    "risk": "规则后端不会替模型猜测剧情选择。",
                }
            )
            self.append_agent_decision(state, decision)
            return decision

        dialog = state.get("dialog") or {}
        if dialog.get("active"):
            latest = dialog.get("latest_event") or {}
            text = latest.get("text")
            decision.update(
                {
                    "summary": "对话处于激活状态；最安全的下一步是阅读或推进对话。",
                    "recommended_intent": "advance_dialog",
                    "recommended_action": self.action_preview("advance_dialog", {}),
                    "evidence": {
                        **decision["evidence"],
                        "dialog": {
                            "position": dialog.get("position"),
                            "visible": dialog.get("visible"),
                            "text": text,
                            "message_id": latest.get("message_id"),
                            "event_object_id": latest.get("event_object_id"),
                        },
                    },
                    "risk": "推进对话可能关闭当前对话或触发下一句，但这是正常确认操作。",
                    "alternatives": [
                        {
                            "intent": "wait_runtime",
                            "action": self.action_preview("no_op", {"reason": "inspect_dialog_text"}),
                            "reason": "如果当前文本仍需人工查看，则暂停。",
                        }
                    ],
                }
            )
            self.append_agent_decision(state, decision)
            return decision

        targets = navigation.get("targets") or []
        task_targets = self.executable_task_targets(targets)
        if task_targets:
            task, target, match_reason = task_targets[0]
            route = self.route_to_object(state, int(target.get("id") or 0), avoid_transitions=True)
            action, next_step = self.decision_for_route(state, target, route)
            decision.update(
                {
                    "summary": f"执行 active 任务 {task.get('id')} 关联的对象 #{target.get('id')}。",
                    "recommended_intent": "enter_transition" if target.get("transition_candidate") else "inspect_target",
                    "recommended_action": action,
                    "evidence": {**decision["evidence"], "task": self.task_view(task), "task_match": match_reason,
                                 "target": {"id": target.get("id"), "steps": target.get("steps"), "transition_candidate": target.get("transition_candidate"), "route_steps": (route.get("route") or {}).get("steps"), "next_step": next_step}},
                    "risk": "任务是 agent 记忆；该建议仅因其已关联到当前可执行目标而优先。",
                    "alternatives": [],
                }
            )
            self.append_agent_decision(state, decision)
            return decision

        reachable = [
            target for target in targets
            if target.get("status") == "reachable"
            and not target.get("transition_candidate")
            and target.get("agent_priority") != "avoid_repeat"
        ]
        priority_rank = {"normal": 0, "soft_avoid": 1}
        reachable.sort(
            key=lambda target: (
                priority_rank.get(str(target.get("agent_priority") or "normal"), 0),
                target.get("steps") is None,
                target.get("steps") or 0,
                target.get("id") or 0,
            )
        )
        transitions = [
            target for target in targets
            if target.get("status") in {"reachable", "transition"} and target.get("transition_candidate")
        ]
        def transition_sort_key(target: dict) -> tuple[int, int, int, int]:
            destination_rank = {
                "unknown": 0,
                "unexplored": 1,
                "partial": 2,
                "complete": 3,
            }.get(str(target.get("destination_priority") or "unknown"), 0)
            backtrack_rank = 1 if target.get("returns_to_recent_room") or target.get("agent_priority") == "avoid_backtrack" else 0
            try:
                steps = int(target.get("steps"))
            except (TypeError, ValueError):
                steps = 999999
            return (backtrack_rank, destination_rank, steps, int(target.get("id") or 0))

        transitions.sort(key=transition_sort_key)
        if transitions:
            target = transitions[0]
            route = self.route_to_object(state, int(target.get("id") or 0), avoid_transitions=True)
            action, next_step = self.decision_for_route(state, target, route)
            decision.update(
                {
                    "summary": f"当前最明确的可达传送候选是对象 #{target.get('id')}。",
                    "recommended_intent": "enter_transition",
                    "recommended_action": action,
                    "evidence": {
                        **decision["evidence"],
                        "target": {
                            "id": target.get("id"),
                            "kind": target.get("kind"),
                            "status": target.get("status"),
                            "steps": target.get("steps"),
                            "known_destination_room_id": target.get("known_destination_room_id"),
                            "destination_priority": target.get("destination_priority"),
                            "destination_exploration": target.get("destination_exploration"),
                            "returns_to_recent_room": target.get("returns_to_recent_room"),
                            "agent_priority": target.get("agent_priority"),
                            "route_steps": (route.get("route") or {}).get("steps"),
                            "next_step": next_step,
                        },
                    },
                    "risk": "这很可能会离开当前房间；除非没有更好的传送目标，否则应降低最近访问过的目的地优先级。",
                    "alternatives": [
                        {
                            "intent": "wait_runtime",
                            "action": self.action_preview("no_op", {"reason": "transition_risk_review"}),
                            "reason": "如果离开房间前需要人工复核，则暂停。",
                        }
                    ],
                }
            )
            self.append_agent_decision(state, decision)
            return decision

        if reachable:
            target = reachable[0]
            route = self.route_to_object(state, int(target.get("id") or 0), avoid_transitions=True)
            action, next_step = self.decision_for_route(state, target, route)
            decision.update(
                {
                    "summary": f"没有可执行任务或可达传送；检查对象 #{target.get('id')}。",
                    "recommended_intent": "inspect_target",
                    "recommended_action": action,
                    "evidence": {**decision["evidence"], "target": {"id": target.get("id"), "kind": target.get("kind"), "status": target.get("status"), "steps": target.get("steps"), "required_direction": target.get("required_direction"), "interaction_state": target.get("interaction_state"), "agent_priority": target.get("agent_priority"), "route_steps": (route.get("route") or {}).get("steps"), "next_step": next_step}},
                    "risk": "对象语义来自触发元数据推断，不是权威标签。",
                    "alternatives": [],
                }
            )
            self.append_agent_decision(state, decision)
            return decision

        exploration = self.current_exploration(state)
        decision.update(
            {
                "summary": "没有明确的可达目标。",
                "recommended_intent": "wait_runtime",
                "recommended_action": self.action_preview("no_op", {"reason": "no_reachable_target"}),
                "evidence": {
                    **decision["evidence"],
                    "exploration": self.agent_exploration_summary(exploration),
                    "target_statuses": [
                        {"id": target.get("id"), "status": target.get("status"), "kind": target.get("kind")}
                        for target in targets[:20]
                    ],
                    "recent_dialog_targets": [
                        {
                            "id": target.get("id"),
                            "kind": target.get("kind"),
                            "interaction_state": target.get("interaction_state"),
                            "reason": target.get("deprioritize_reason"),
                        }
                        for target in targets[:20]
                        if target.get("agent_priority") == "avoid_repeat"
                        and target.get("interaction_state") == "recent_dialog"
                    ],
                    "soft_avoid_targets": [
                        {
                            "id": target.get("id"),
                            "kind": target.get("kind"),
                            "interaction_state": target.get("interaction_state"),
                            "reason": target.get("deprioritize_reason"),
                        }
                        for target in targets[:20]
                        if target.get("agent_priority") == "soft_avoid"
                    ],
                    "previously_cleared_targets": [
                        {
                            "id": target.get("id"),
                            "kind": target.get("kind"),
                            "interaction_state": target.get("interaction_state"),
                            "reason": target.get("deprioritize_reason"),
                        }
                        for target in targets[:20]
                        if target.get("interaction_state") == "previously_cleared"
                    ],
                },
                "risk": "当前没有明确的对象或传送目标可安全行动。",
                "alternatives": [
                    {
                        "intent": "wait_runtime",
                        "action": self.action_preview("no_op", {"reason": "human_review"}),
                        "reason": "如果调试界面显示存在未建模交互，则暂停。",
                    }
                ],
            }
        )
        self.append_agent_decision(state, decision)
        return decision

    def dialog_feedback_by_object(self, dialogs: list[dict], limit_per_object: int = 4) -> dict[int, list[dict]]:
        feedback: dict[int, list[dict]] = {}
        for entry in dialogs:
            try:
                obj_id = int(entry.get("event_object_id") or 0)
            except (TypeError, ValueError):
                continue
            if obj_id <= 0:
                continue
            feedback.setdefault(obj_id, []).append(
                {
                    "frame": entry.get("frame"),
                    "end_frame": entry.get("end_frame"),
                    "raw_frame": entry.get("raw_frame"),
                    "end_raw_frame": entry.get("end_raw_frame"),
                    "speaker_hint": entry.get("speaker_hint"),
                    "text": entry.get("text"),
                    "message_id": entry.get("message_id"),
                    "message_ids": entry.get("message_ids"),
                    "room_id": entry.get("room_id"),
                    "logical_room_id": entry.get("logical_room_id") or entry.get("room_id"),
                    "room_key": entry.get("room_key"),
                    "region_key": entry.get("region_key"),
                    "nav_room_id": entry.get("nav_room_id"),
                    "legacy_room_id": entry.get("legacy_room_id"),
                    "legacy_nav_room_id": entry.get("legacy_nav_room_id"),
                    "agent_turn": entry.get("agent_turn"),
                    "turn": entry.get("turn"),
                }
            )
        return {obj_id: entries[-limit_per_object:] for obj_id, entries in feedback.items()}

    def dialog_entry_matches_current_room(self, state: dict, entry: dict) -> bool:
        identity = self.room_identity(state)
        if identity is None:
            return False
        if not self.dialog_room_metadata_consistent(entry):
            return False
        if entry.get("scene_id") not in (None, identity["scene_id"]):
            return False
        if entry.get("map_id") not in (None, identity["map_id"]):
            return False

        entry_logical_room_id = entry.get("logical_room_id")
        if entry_logical_room_id:
            return str(entry_logical_room_id) == str(identity["id"])

        entry_room_id = entry.get("room_id")
        if is_logical_room_id(entry_room_id):
            return str(entry_room_id) == str(identity["id"])

        entry_nav_room_id = entry.get("nav_room_id")
        if entry_nav_room_id:
            if str(entry_nav_room_id) == str(identity["nav_room_id"]):
                return True
            legacy_nav_room_id = identity.get("legacy_nav_room_id")
            return bool(legacy_nav_room_id) and str(entry_nav_room_id) == str(legacy_nav_room_id)

        if entry_room_id:
            legacy_room_id = identity.get("legacy_room_id")
            legacy_nav_room_id = identity.get("legacy_nav_room_id")
            entry_room_id = str(entry_room_id)
            return entry_room_id == str(legacy_room_id) or (
                bool(legacy_nav_room_id) and entry_room_id.startswith(f"{legacy_nav_room_id}/")
            )

        entry_room_key = entry.get("room_key")
        entry_region_key = entry.get("region_key")
        return (
            bool(entry_room_key)
            and bool(entry_region_key)
            and str(entry_room_key) == str(identity["room_key"])
            and str(entry_region_key) == str(identity["region_key"])
        )

    def dialog_entry_has_different_full_room(self, state: dict, entry: dict) -> bool:
        identity = self.room_identity(state)
        if identity is None:
            return False
        if not self.dialog_room_metadata_consistent(entry):
            return False
        if entry.get("scene_id") not in (None, identity["scene_id"]):
            return False
        if entry.get("map_id") not in (None, identity["map_id"]):
            return False
        entry_logical_room_id = entry.get("logical_room_id")
        if entry_logical_room_id:
            return str(entry_logical_room_id) != str(identity["id"])

        entry_room_id = entry.get("room_id")
        if entry_room_id:
            if is_logical_room_id(entry_room_id):
                return str(entry_room_id) != str(identity["id"])
            legacy_room_id = identity.get("legacy_room_id")
            legacy_nav_room_id = identity.get("legacy_nav_room_id")
            return not (
                str(entry_room_id) == str(legacy_room_id)
                or (bool(legacy_nav_room_id) and str(entry_room_id).startswith(f"{legacy_nav_room_id}/"))
            )
        entry_nav_room_id = entry.get("nav_room_id")
        if entry_nav_room_id:
            legacy_nav_room_id = identity.get("legacy_nav_room_id")
            return str(entry_nav_room_id) not in {str(identity["nav_room_id"]), str(legacy_nav_room_id)}
        return False

    def same_room_dialog_feedback_by_object(
        self,
        state: dict,
        dialogs: list[dict],
        limit_per_object: int = 4,
    ) -> dict[int, list[dict]]:
        same_room = [entry for entry in dialogs if self.dialog_entry_matches_current_room(state, entry)]
        return self.dialog_feedback_by_object(same_room, limit_per_object=limit_per_object)

    def dialog_feedback_context(self, state: dict) -> dict:
        current_turn = self.current_agent_turn()
        same_room: list[dict] = []
        stale_different_room: list[dict] = []
        changed = False
        for entry in self.dialog_history:
            if not isinstance(entry, dict):
                continue
            if self.dialog_entry_matches_current_room(state, entry):
                same_room.append(entry)
                if entry.get("agent_turn") is None and entry.get("turn") is None:
                    entry["agent_turn"] = current_turn
                    changed = True
            elif self.dialog_entry_has_different_full_room(state, entry):
                stale_different_room.append(entry)
        if changed:
            self.write_dialog_history()
        return {
            "same_room_dialog_feedback_by_object": self.dialog_feedback_by_object(same_room),
            "stale_different_room_dialog_feedback_by_object": self.dialog_feedback_by_object(
                stale_different_room,
                limit_per_object=1,
            ),
        }

    def navigation_affordance_context(self, state: dict) -> dict:
        return {
            **self.dialog_feedback_context(state),
            "recently_cleared_target_ids": self.recently_cleared_target_ids(state),
            "recent_interaction_outcomes_by_target": self.recent_interaction_outcomes_by_target(state),
        }

    def latest_feedback_agent_turn(self, feedback: list[dict]) -> int | None:
        latest_turn = None
        for entry in feedback:
            turn = self.int_frame(entry.get("agent_turn", entry.get("turn")))
            if turn is not None:
                latest_turn = turn if latest_turn is None else max(latest_turn, turn)
        return latest_turn

    def ensure_dialog_feedback_turn(self, state: dict, target_id: int) -> None:
        current_turn = self.current_agent_turn()
        changed = False
        for entry in self.dialog_history:
            if not isinstance(entry, dict) or not self.dialog_entry_matches_current_room(state, entry):
                continue
            try:
                obj_id = int(entry.get("event_object_id") or 0)
            except (TypeError, ValueError):
                continue
            if obj_id != target_id:
                continue
            if entry.get("agent_turn") is None and entry.get("turn") is None:
                entry["agent_turn"] = current_turn
                changed = True
        if changed:
            self.write_dialog_history()

    def dialog_feedback_decay(self, feedback: list[dict]) -> dict:
        current_turn = self.current_agent_turn()
        feedback_turn = self.latest_feedback_agent_turn(feedback)
        if feedback_turn is None:
            feedback_turn = current_turn
        turns_since = max(0, current_turn - feedback_turn)
        if turns_since >= 3:
            return {
                "interaction_state": "recent_dialog",
                "agent_priority": "soft_avoid",
                "dialog_feedback_state": "soft_avoid",
                "dialog_feedback_turns_since": turns_since,
            }
        if turns_since == 2:
            return {
                "interaction_state": "recent_dialog",
                "agent_priority": "soft_avoid",
                "dialog_feedback_state": "soft_avoid",
                "dialog_feedback_turns_since": turns_since,
            }
        return {
            "interaction_state": "recent_dialog",
            "agent_priority": "avoid_repeat",
            "dialog_feedback_state": "recent_dialog",
            "dialog_feedback_turns_since": turns_since,
        }

    def stale_different_room_dialog_feedback_for_target(self, state: dict, target_id: int) -> list[dict]:
        stale = []
        for entry in self.dialog_history:
            if not isinstance(entry, dict) or not self.dialog_entry_has_different_full_room(state, entry):
                continue
            try:
                obj_id = int(entry.get("event_object_id") or 0)
            except (TypeError, ValueError):
                continue
            if obj_id == target_id:
                stale.append(entry)
        return self.dialog_feedback_by_object(stale, limit_per_object=1).get(target_id, [])

    def target_changed_after_dialog_feedback(self, state: dict, target_id: int, feedback: list[dict]) -> bool:
        latest_frame = None
        for entry in feedback:
            frame = self.int_frame(entry.get("end_frame", entry.get("frame")))
            if frame is not None:
                latest_frame = frame if latest_frame is None else max(latest_frame, frame)
        for event in reversed(self.event_log[-100:]):
            if event.get("type") != "object_changed":
                continue
            if not self.object_event_matches_nav_room(state, event):
                continue
            try:
                if int(event.get("object_id") or 0) != target_id:
                    continue
            except (TypeError, ValueError):
                continue
            event_frame = self.int_frame(event.get("frame"))
            if latest_frame is None or event_frame is None or event_frame >= latest_frame:
                return True
        return False

    def dialog_feedback_for_target(self, state: dict, target_id: int, context: dict | None = None) -> list[dict]:
        if isinstance(context, dict) and isinstance(context.get("same_room_dialog_feedback_by_object"), dict):
            feedback = context["same_room_dialog_feedback_by_object"].get(target_id, [])
        else:
            self.ensure_dialog_feedback_turn(state, target_id)
            feedback = self.same_room_dialog_feedback_by_object(state, self.dialog_history).get(target_id, [])
        if feedback and not self.target_changed_after_dialog_feedback(state, target_id, feedback):
            return feedback
        return []

    def event_matches_room(self, state: dict, event: dict) -> bool:
        if event.get("scene_id") != state.get("scene_id") or event.get("map_id") != state.get("map_id"):
            return False
        identity = self.room_identity(state)
        if identity is not None:
            event_logical_room_id = event.get("logical_room_id")
            if event_logical_room_id:
                return str(event_logical_room_id) == str(identity["id"])
            event_room_id = event.get("room_id")
            if event_room_id:
                if is_logical_room_id(event_room_id):
                    return str(event_room_id) == str(identity["id"])
                return str(event_room_id) == str(identity.get("legacy_room_id"))
        room_key = identity["room_key"] if identity else (state.get("room") or {}).get("key")
        region_key = identity["region_key"] if identity else state.get("region_key")
        event_room_key = event.get("room_key")
        event_region_key = event.get("region_key")
        if event_room_key and room_key and event_room_key != room_key:
            return False
        if event_region_key and region_key and event_region_key != region_key:
            return False
        return True

    def object_event_matches_nav_room(self, state: dict, event: dict) -> bool:
        if event.get("scene_id") != state.get("scene_id") or event.get("map_id") != state.get("map_id"):
            return False
        identity = self.room_identity(state)
        room = state.get("room") if isinstance(state.get("room"), dict) else {}
        room_key = identity["room_key"] if identity else room.get("key")
        nav_room_id = identity["nav_room_id"] if identity else None

        event_logical_room_id = event.get("logical_room_id")
        if identity is not None and event_logical_room_id:
            return str(event_logical_room_id) == str(identity["id"])

        event_room_id = event.get("room_id")
        if identity is not None and event_room_id:
            event_room_id = str(event_room_id)
            if is_logical_room_id(event_room_id):
                return event_room_id == identity["id"]
            legacy_room_id = identity.get("legacy_room_id")
            legacy_nav_room_id = identity.get("legacy_nav_room_id")
            return event_room_id == str(legacy_room_id) or (
                bool(legacy_nav_room_id) and event_room_id.startswith(f"{legacy_nav_room_id}/")
            )

        event_nav_room_id = event.get("nav_room_id")
        if nav_room_id and event_nav_room_id:
            legacy_nav_room_id = identity.get("legacy_nav_room_id") if identity else None
            return str(event_nav_room_id) in {str(nav_room_id), str(legacy_nav_room_id)}

        event_room_key = event.get("room_key")
        if room_key and event_room_key:
            return str(event_room_key) == str(room_key)

        region_key = identity["region_key"] if identity else state.get("region_key")
        event_region_key = event.get("region_key")
        if region_key and event_region_key:
            return str(event_region_key) == str(region_key)
        return True

    def object_change_cleared_blocker(self, event: dict) -> bool:
        if event.get("type") != "object_changed":
            return False
        changes = event.get("changes") if isinstance(event.get("changes"), dict) else {}
        blocking = changes.get("blocking") if isinstance(changes.get("blocking"), dict) else {}
        state = changes.get("state") if isinstance(changes.get("state"), dict) else {}
        if blocking.get("from") is True and blocking.get("to") is False:
            return True
        try:
            return int(state.get("from")) > 1 and int(state.get("to")) == 1
        except (TypeError, ValueError):
            return False

    def object_event_cleared_blocker(self, event: dict) -> bool:
        if self.object_change_cleared_blocker(event):
            return True
        if event.get("type") != "interaction_effect":
            return False
        changes = event.get("object_changes") if isinstance(event.get("object_changes"), dict) else {}
        blocking = changes.get("blocking") if isinstance(changes.get("blocking"), dict) else {}
        state = changes.get("state") if isinstance(changes.get("state"), dict) else {}
        if blocking.get("from") is True and blocking.get("to") is False:
            return True
        try:
            return int(state.get("from")) > 1 and int(state.get("to")) == 1
        except (TypeError, ValueError):
            return False

    def recently_cleared_target_ids(self, state: dict, event_limit: int = 50) -> set[int]:
        objects_by_id = {
            int(obj.get("id") or 0): obj
            for obj in state.get("objects") or []
            if isinstance(obj, dict) and int(obj.get("id") or 0) > 0
        }
        cleared: set[int] = set()
        cleared_scripts: set[int] = set()
        identity = self.room_identity(state)
        data = self.read_exploration()
        interaction_rooms = data.get("object_interactions") if isinstance(data.get("object_interactions"), dict) else {}
        persistent_objects = (
            interaction_rooms.get(identity["id"])
            if identity is not None and isinstance(interaction_rooms.get(identity["id"]), dict)
            else {}
        )
        persistent_known_ids = {
            int(raw_id) for raw_id in persistent_objects
            if str(raw_id).isdigit()
        }
        for raw_id, entry in persistent_objects.items():
            if not isinstance(entry, dict) or not entry.get("cleared_blocker"):
                continue
            try:
                obj_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            obj = objects_by_id.get(obj_id)
            if obj is None or trigger_kind(int(obj.get("trigger_mode") or 0)) != "search":
                continue
            if obj.get("blocking") is True:
                continue
            current = self.object_snapshot_for_interaction(state, obj_id)
            if not self.object_snapshot_compatible(current, entry.get("cleared_snapshot")):
                continue
            cleared.add(obj_id)
            script = int(obj.get("trigger_script") or 0)
            if script > 0:
                cleared_scripts.add(script)
        for event in self.event_log[-event_limit:]:
            if not self.object_event_matches_nav_room(state, event) or not self.object_change_cleared_blocker(event):
                continue
            try:
                obj_id = int(event.get("object_id") or 0)
            except (TypeError, ValueError):
                continue
            if obj_id in persistent_known_ids:
                continue
            obj = objects_by_id.get(obj_id)
            if obj is None or trigger_kind(int(obj.get("trigger_mode") or 0)) != "search":
                continue
            if obj.get("blocking") is True:
                continue
            cleared.add(obj_id)
            script = int(obj.get("trigger_script") or 0)
            if script > 0:
                cleared_scripts.add(script)

        if cleared_scripts:
            for obj_id, obj in objects_by_id.items():
                if trigger_kind(int(obj.get("trigger_mode") or 0)) != "search":
                    continue
                if obj.get("blocking") is True:
                    continue
                if int(obj.get("trigger_script") or 0) in cleared_scripts:
                    cleared.add(obj_id)
        return cleared

    def recent_interaction_outcomes_by_target(self, state: dict, event_limit: int = 80) -> dict[int, dict]:
        outcomes: dict[int, dict] = {}
        identity = self.room_identity(state)
        data = self.read_exploration()
        interaction_rooms = data.get("object_interactions") if isinstance(data.get("object_interactions"), dict) else {}
        persistent_objects = (
            interaction_rooms.get(identity["id"])
            if identity is not None and isinstance(interaction_rooms.get(identity["id"]), dict)
            else {}
        )
        persistent_known_ids = {
            int(raw_id) for raw_id in persistent_objects
            if str(raw_id).isdigit()
        }
        for raw_id, entry in persistent_objects.items():
            if not isinstance(entry, dict) or not entry.get("last_result"):
                continue
            try:
                obj_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            current = self.object_snapshot_for_interaction(state, obj_id)
            recorded = entry.get("last_after") or entry.get("last_before")
            if not self.object_snapshot_compatible(current, recorded):
                continue
            last_event = entry.get("last_event") if isinstance(entry.get("last_event"), dict) else {}
            outcomes[obj_id] = {
                **last_event,
                "type": "interaction_no_effect" if entry.get("last_result") == "no_effect" else "interaction_effect",
                "persistent": True,
            }
        for event in reversed(self.event_log[-event_limit:]):
            if event.get("type") not in {"interaction_effect", "interaction_no_effect"}:
                continue
            if not self.object_event_matches_nav_room(state, event):
                continue
            try:
                obj_id = int(event.get("object_id") or event.get("target_id") or 0)
            except (TypeError, ValueError):
                continue
            if obj_id <= 0 or obj_id in outcomes:
                continue
            if obj_id in persistent_known_ids:
                continue
            current = self.object_snapshot_for_interaction(state, obj_id)
            recorded = event.get("finished_object") or event.get("started_object")
            if isinstance(recorded, dict) and not self.object_snapshot_compatible(current, recorded):
                continue
            outcomes[obj_id] = event
        return outcomes

    def target_affordance(self, state: dict, target_id: int, context: dict | None = None) -> dict:
        recently_cleared = (
            context.get("recently_cleared_target_ids")
            if isinstance(context, dict) and isinstance(context.get("recently_cleared_target_ids"), set)
            else self.recently_cleared_target_ids(state)
        )
        if target_id in recently_cleared:
            return {
                "interaction_state": "previously_cleared",
                "agent_priority": "avoid_repeat",
                "deprioritize_reason": "该目标曾从阻挡状态变为非阻挡状态，且当前快照仍兼容。",
            }
        if isinstance(context, dict) and isinstance(context.get("recent_interaction_outcomes_by_target"), dict):
            outcomes = context["recent_interaction_outcomes_by_target"]
        else:
            outcomes = self.recent_interaction_outcomes_by_target(state)
        outcome = outcomes.get(target_id)
        if outcome:
            no_effect = outcome.get("type") == "interaction_no_effect"
            return {
                "interaction_state": "previously_tried",
                "agent_priority": "avoid_repeat",
                "interaction_feedback_state": "no_effect" if no_effect else "effect",
                "deprioritize_reason": (
                    "该目标此前已尝试交互，但没有观察到状态、物品或房间变化。"
                    if no_effect
                    else "该目标此前交互后已经产生了状态、物品或房间变化。"
                ),
                "previous_interaction_outcome": outcome,
            }
        dialog_feedback = self.dialog_feedback_for_target(state, target_id, context)
        if dialog_feedback:
            decay = self.dialog_feedback_decay(dialog_feedback)
            reason = (
                "该目标在本房间的数轮 agent 决策前产生过对话；只有在状态可能有进展后才应重试。"
                if decay["agent_priority"] == "soft_avoid"
                else "该目标最近在本房间产生过对话，之后未观察到状态变化。"
            )
            return {
                "interaction_state": decay["interaction_state"],
                "agent_priority": decay["agent_priority"],
                "dialog_feedback_state": decay["dialog_feedback_state"],
                "dialog_feedback_turns_since": decay["dialog_feedback_turns_since"],
                "deprioritize_reason": reason,
                "recent_dialog_feedback": dialog_feedback,
            }
        if isinstance(context, dict) and isinstance(context.get("stale_different_room_dialog_feedback_by_object"), dict):
            stale_feedback = context["stale_different_room_dialog_feedback_by_object"].get(target_id, [])
        else:
            stale_feedback = self.stale_different_room_dialog_feedback_for_target(state, target_id)
        if stale_feedback:
            return {
                "interaction_state": "fresh",
                "agent_priority": "normal",
                "dialog_feedback_state": "stale_different_room",
            }
        return {
            "interaction_state": "fresh",
            "agent_priority": "normal",
        }

    def decorate_target_affordance(self, state: dict, target: dict, context: dict | None = None) -> dict:
        try:
            target_id = int(target.get("id") or target.get("target_id") or 0)
        except (TypeError, ValueError):
            return target
        if target_id <= 0:
            return target
        affordance = self.target_affordance(state, target_id, context)
        if target.get("agent_priority") == "avoid_backtrack" and affordance.get("agent_priority") == "normal":
            affordance = {key: value for key, value in affordance.items() if key not in {"agent_priority", "deprioritize_reason"}}
        return {**target, **affordance}

    def agent_recent_decision_context(self, limit: int = 5) -> list[dict]:
        context = []
        for entry in self.read_agent_log(limit=limit):
            decision = entry.get("decision") if isinstance(entry.get("decision"), dict) else {}
            evidence = decision.get("evidence") if isinstance(decision.get("evidence"), dict) else {}
            target = evidence.get("target") if isinstance(evidence.get("target"), dict) else {}
            context.append(
                {
                    "turn": entry.get("turn"),
                    "frame": entry.get("frame"),
                    "intent": decision.get("recommended_intent"),
                    "summary": decision.get("summary"),
                    "recommended_action": decision.get("recommended_action"),
                    "target_id": target.get("id"),
                    "risk": decision.get("risk"),
                }
            )
        return context

    def agent_state_summary(self, state: dict) -> dict:
        pos = state.get("party_position") or {}
        dialog = state.get("dialog") or {}
        latest = dialog.get("latest_event") if isinstance(dialog.get("latest_event"), dict) else {}
        room = state.get("room") if isinstance(state.get("room"), dict) else {}
        identity = self.room_identity(state)
        room_payload = dict(room)
        if identity is not None:
            room_payload.update(
                {
                    "id": identity["id"],
                    "key": identity["room_key"],
                    "logical_room_id": identity["id"],
                    "logical_room_label": identity.get("logical_room_label"),
                    "room_key": identity["room_key"],
                    "region_key": identity["region_key"],
                    "nav_room_id": identity["nav_room_id"],
                    "legacy_room_id": identity.get("legacy_room_id"),
                    "legacy_nav_room_id": identity.get("legacy_nav_room_id"),
                }
            )
        party = state.get("party") if isinstance(state.get("party"), list) else []
        inventory = state.get("inventory") if isinstance(state.get("inventory"), list) else []
        try:
            party_direction = int(state.get("party_direction") or 0)
        except (TypeError, ValueError):
            party_direction = 0
        state_info = self.live_info("state")
        frame_info = self.live_info("frame")
        shop = state.get("shop") if isinstance(state.get("shop"), dict) else None
        choice = state.get("choice") if isinstance(state.get("choice"), dict) else None
        return {
            "status": state.get("status", "ok"),
            "frame": state.get("frame"),
            "scene_id": state.get("scene_id"),
            "map_id": state.get("map_id"),
            "room": room_payload,
            "room_id": identity["id"] if identity else None,
            "logical_room_id": identity["id"] if identity else None,
            "logical_room_label": identity.get("logical_room_label") if identity else None,
            "room_key": identity["room_key"] if identity else room.get("key"),
            "region_key": identity["region_key"] if identity else state.get("region_key"),
            "nav_room_id": identity["nav_room_id"] if identity else None,
            "legacy_room_id": identity.get("legacy_room_id") if identity else None,
            "legacy_nav_room_id": identity.get("legacy_nav_room_id") if identity else None,
            "in_main_game": state.get("in_main_game"),
            "in_battle": state.get("in_battle"),
            "party_position": {"x": pos.get("x"), "y": pos.get("y")},
            "party_direction": state.get("party_direction"),
            "party_direction_button": DIR_TO_BUTTON.get(party_direction, "down"),
            "cash": state.get("cash"),
            "shop": self.agent_shop(state).get("shop") if shop and shop.get("active") else None,
            "choice": choice if choice and choice.get("active") else None,
            "party": [
                {
                    "role_id": role.get("role_id"),
                    "scene_sprite_id": role.get("scene_sprite_id"),
                    "level": role.get("level"),
                    "hp": role.get("hp"),
                    "max_hp": role.get("max_hp"),
                    "mp": role.get("mp"),
                    "max_mp": role.get("max_mp"),
                }
                for role in party[:6]
                if isinstance(role, dict)
            ],
            "inventory_count": len(inventory),
            "dialog": {
                "active": bool(dialog.get("active")),
                "position": dialog.get("position"),
                "visible": dialog.get("visible"),
                "latest_text": latest.get("text"),
                "message_id": latest.get("message_id"),
                "event_object_id": latest.get("event_object_id"),
            },
            "state_file": state_info,
            "frame_file": frame_info,
            "state_stale": state_info.get("stale", True),
            "frame_stale": frame_info.get("stale", True),
            "runtime_stale": state_info.get("stale", True),
            "action_pending": self.action_pending(state),
            "exploration": self.agent_exploration_summary(self.agent_current_exploration(state)),
        }

    def agent_shop(self, state: dict, item_id: int | None = None) -> dict:
        shop = state.get("shop") if isinstance(state.get("shop"), dict) else None
        if not shop or shop.get("active") is not True:
            return {"ok": False, "error": "shop_inactive", "shop": None}

        items = shop.get("items") if isinstance(shop.get("items"), list) else []
        compact_keys = (
            "index", "item_id", "name", "price", "inventory_count",
            "equipped_count", "owned_count", "affordable", "selected",
            "usable", "equipable", "sellable",
        )
        compact_items = [
            {key: item.get(key) for key in compact_keys if key in item}
            for item in items
            if isinstance(item, dict)
        ]
        summary = {
            "active": True,
            "mode": shop.get("mode"),
            "store_id": shop.get("store_id"),
            "phase": shop.get("phase"),
            "selected_index": shop.get("selected_index"),
            "selected_item_id": shop.get("selected_item_id"),
            "cash": state.get("cash"),
            "items": compact_items,
        }
        if item_id is None:
            return {"ok": True, "shop": summary}
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            item_id = 0
        candidate = next(
            (
                item for item in items
                if isinstance(item, dict) and int(item.get("item_id") or 0) == item_id
            ),
            None,
        )
        if item_id <= 0 or not isinstance(candidate, dict):
            return {"ok": False, "error": "item_not_offered", "item_id": item_id, "shop": summary}
        detail = dict(candidate)
        result = {"ok": True, "shop": summary, "item": detail}
        if candidate.get("equipable") is True:
            result["equipment_comparisons"] = self.equipment_comparisons(state, candidate)
        return result

    @staticmethod
    def compact_inventory_item(item: dict) -> dict:
        return {
            "slot": item.get("slot"),
            "item_id": item.get("item_id"),
            "name": item.get("name"),
            "amount": item.get("amount"),
            "amount_in_use": item.get("amount_in_use"),
            "equipped_count": item.get("equipped_count"),
            "usable": item.get("usable"),
            "throwable": item.get("throwable"),
            "apply_to_all": item.get("apply_to_all"),
            "target_mode": (
                "implicit_event_context"
                if item.get("apply_to_all") is True
                else "party_role"
            ),
            "sellable": item.get("sellable"),
            "consuming": item.get("consuming"),
            "available_amount": max(
                0,
                int(item.get("amount") or 0) - int(item.get("amount_in_use") or 0),
            ),
            "flags": item.get("flags"),
            "price": item.get("price"),
            "effect_available": bool(item.get("use_effect") or item.get("throw_effect")),
        }

    def agent_inventory(
        self,
        state: dict,
        limit: int = 100,
        item_id: int | None = None,
    ) -> dict:
        inventory = state.get("inventory") if isinstance(state.get("inventory"), list) else []
        if item_id is not None:
            try:
                normalized_item_id = int(item_id)
            except (TypeError, ValueError):
                normalized_item_id = 0
            candidate = next(
                (
                    item for item in inventory
                    if isinstance(item, dict)
                    and int(item.get("item_id") or 0) == normalized_item_id
                ),
                None,
            )
            if normalized_item_id <= 0 or not isinstance(candidate, dict):
                return {
                    "ok": False,
                    "error": "item_not_in_inventory",
                    "item_id": normalized_item_id,
                }
            detail = self.compact_inventory_item(candidate)
            for key in ("script_on_use", "script_on_throw", "use_effect", "throw_effect"):
                if key in candidate:
                    detail[key] = candidate.get(key)
            return {"ok": True, "item": detail}
        items = []
        for item in inventory[:limit]:
            if not isinstance(item, dict):
                continue
            items.append(self.compact_inventory_item(item))
        return {"inventory": items, "total": len(inventory), "returned": len(items)}

    @staticmethod
    def compact_equipment_item(item: dict | None) -> dict | None:
        if not isinstance(item, dict):
            return None
        return {
            key: item.get(key)
            for key in (
                "item_id", "name", "price", "amount", "amount_in_use",
                "equipped_count", "total_count", "usable", "equipable",
                "equipable_role_ids", "equipment_effect",
            )
            if key in item
        }

    @staticmethod
    def equipment_item_by_id(state: dict, item_id: int) -> dict | None:
        for item in state.get("inventory") or []:
            if isinstance(item, dict) and int(item.get("item_id") or 0) == item_id:
                return item
        for role in state.get("party") or []:
            if not isinstance(role, dict):
                continue
            for item in role.get("equipment") or []:
                if isinstance(item, dict) and int(item.get("item_id") or 0) == item_id:
                    return item
        return None

    @staticmethod
    def equipment_trait_map(effect: dict | None) -> dict[str, dict]:
        if not isinstance(effect, dict):
            return {}
        result = {}
        for trait in effect.get("traits") or []:
            if not isinstance(trait, dict):
                continue
            kind = str(trait.get("kind") or "")
            if kind:
                key = kind
                if kind == "status":
                    key = f"{kind}:{trait.get('status_id')}"
                elif kind == "persistent_poison":
                    key = f"{kind}:{trait.get('poison_id')}"
                result[key] = trait
        return result

    def equipment_comparisons(self, state: dict, candidate: dict) -> list[dict]:
        effect = candidate.get("equipment_effect") if isinstance(candidate.get("equipment_effect"), dict) else {}
        try:
            slot_id = int(effect.get("slot_id"))
        except (TypeError, ValueError):
            slot_id = -1
        eligible_ids = set()
        for value in candidate.get("equipable_role_ids") or []:
            try:
                eligible_ids.add(int(value))
            except (TypeError, ValueError):
                pass
        stat_keys = (
            "attack_strength", "magic_strength", "defense",
            "dexterity", "flee_rate", "poison_resistance",
        )
        candidate_modifiers = effect.get("stat_modifiers") if isinstance(effect.get("stat_modifiers"), dict) else {}
        candidate_elements = effect.get("elemental_resistance") if isinstance(effect.get("elemental_resistance"), list) else []
        candidate_traits = self.equipment_trait_map(effect)
        comparisons = []
        for role in state.get("party") or []:
            if not isinstance(role, dict):
                continue
            try:
                role_id = int(role.get("role_id"))
            except (TypeError, ValueError):
                continue
            if role_id not in eligible_ids:
                continue
            current_item = None
            for item in role.get("equipment") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    current_slot = int(item.get("slot_id"))
                except (TypeError, ValueError):
                    current_slot = -1
                if current_slot == slot_id:
                    current_item = item
                    break
            current_effect = (
                current_item.get("equipment_effect")
                if isinstance(current_item, dict) and isinstance(current_item.get("equipment_effect"), dict)
                else {}
            )
            current_modifiers = (
                current_effect.get("stat_modifiers")
                if isinstance(current_effect.get("stat_modifiers"), dict)
                else {}
            )
            stat_delta = {
                key: int(candidate_modifiers.get(key) or 0) - int(current_modifiers.get(key) or 0)
                for key in stat_keys
            }
            current_elements = (
                current_effect.get("elemental_resistance")
                if isinstance(current_effect.get("elemental_resistance"), list)
                else []
            )
            elemental_delta = [
                int(candidate_elements[index] if index < len(candidate_elements) else 0)
                - int(current_elements[index] if index < len(current_elements) else 0)
                for index in range(5)
            ]
            current_stats = {
                key: role.get(key)
                for key in stat_keys
                if role.get(key) is not None
            }
            resulting_stats = {
                key: int(current_stats[key]) + stat_delta[key]
                for key in ("attack_strength", "magic_strength", "defense", "dexterity", "flee_rate")
                if key in current_stats
            }
            old_traits = self.equipment_trait_map(current_effect)
            gained_effects = [
                trait for kind, trait in candidate_traits.items()
                if kind not in old_traits
            ]
            lost_effects = [
                trait for kind, trait in old_traits.items()
                if kind not in candidate_traits
            ]
            changed_effects = [
                {
                    "kind": candidate_traits[kind].get("kind") or old_traits[kind].get("kind"),
                    "from": old_traits[kind],
                    "to": candidate_traits[kind],
                }
                for kind in sorted(set(old_traits) & set(candidate_traits))
                if old_traits[kind] != candidate_traits[kind]
            ]
            comparisons.append(
                {
                    "role_id": role_id,
                    "role_name": role.get("name"),
                    "current_item": self.compact_equipment_item(current_item),
                    "current_stats": current_stats,
                    "stat_delta": stat_delta,
                    "elemental_resistance_delta": elemental_delta,
                    "resulting_stats": resulting_stats,
                    "gained_effects": gained_effects,
                    "lost_effects": lost_effects,
                    "changed_effects": changed_effects,
                }
            )
        return comparisons

    def agent_equipment(self, state: dict, item_id: int | None = None) -> dict:
        pending = self.agent_runtime.pending_equipment_review()
        if item_id is None and isinstance(pending, dict):
            try:
                item_id = int(pending.get("item_id") or 0)
            except (TypeError, ValueError):
                item_id = 0
        if item_id is not None:
            try:
                item_id = int(item_id)
            except (TypeError, ValueError):
                item_id = 0
            candidate = self.equipment_item_by_id(state, item_id)
            if item_id <= 0 or not isinstance(candidate, dict):
                return {"ok": False, "error": "equipment item was not found"}
            if candidate.get("equipable") is not True:
                return {"ok": False, "error": "item is not equipable", "item_id": item_id}
            available = 0
            for item in state.get("inventory") or []:
                if isinstance(item, dict) and int(item.get("item_id") or 0) == item_id:
                    available = max(
                        0,
                        int(item.get("amount") or 0) - int(item.get("amount_in_use") or 0),
                    )
                    break
            return {
                "ok": True,
                "pending_review": pending if isinstance(pending, dict) and int(pending.get("item_id") or 0) == item_id else None,
                "candidate": self.compact_equipment_item(candidate),
                "available_count": available,
                "comparisons": self.equipment_comparisons(state, candidate),
            }

        current = []
        for role in state.get("party") or []:
            if not isinstance(role, dict):
                continue
            current.append(
                {
                    "role_id": role.get("role_id"),
                    "role_name": role.get("name"),
                    "equipment": [
                        self.compact_equipment_item(item)
                        for item in role.get("equipment") or []
                        if isinstance(item, dict)
                    ],
                }
            )
        candidates = [
            self.compact_equipment_item(item)
            for item in state.get("inventory") or []
            if isinstance(item, dict) and item.get("equipable") is True
        ]
        return {
            "ok": True,
            "pending_review": None,
            "current_equipment": current,
            "candidates": candidates[:40],
            "candidates_total": len(candidates),
        }

    def agent_current_exploration(self, state: dict) -> dict:
        existing = state.get("_exploration")
        if isinstance(existing, dict):
            return existing
        return self.current_exploration(state)

    def agent_exploration_summary(self, exploration: dict) -> dict:
        last_route = exploration.get("last_route") if isinstance(exploration.get("last_route"), dict) else {}
        target_selection = (
            last_route.get("target_selection")
            if isinstance(last_route.get("target_selection"), dict)
            else None
        )
        return {
            "status": exploration.get("status"),
            "room_id": exploration.get("room_id"),
            "logical_room_id": exploration.get("logical_room_id") or exploration.get("room_id"),
            "logical_room_label": exploration.get("logical_room_label"),
            "room_key": exploration.get("room_key"),
            "region_key": exploration.get("region_key"),
            "nav_room_id": exploration.get("nav_room_id"),
            "legacy_room_id": exploration.get("legacy_room_id"),
            "legacy_nav_room_id": exploration.get("legacy_nav_room_id"),
            "explored": exploration.get("explored", exploration.get("visited")),
            "visited": exploration.get("visited"),
            "frontier": exploration.get("frontier"),
            "complete": exploration.get("complete"),
            "last_route": {
                "segment_steps": last_route.get("segment_steps"),
                "remaining_steps": last_route.get("remaining_steps"),
                "target_selection": target_selection,
            },
        }

    def agent_tool_specs(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "upsert_task",
                    "description": (
                        "创建或更新本地 agent 任务记忆，不执行游戏动作。已有 id 会更新；未知自选 id 在同时提供 "
                        "description 和 rationale 时按该 id 创建；省略 id 创建时自动生成 task-* id。更新前优先用 get_tasks 核对账本。"
                        "对已有 id 不提供变化字段表示明确确认继续，也会刷新该任务的证据审计快照。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "minLength": 1}, "description": {"type": "string", "minLength": 1},
                            "priority": {"type": "integer"},
                            "status": {"type": "string", "enum": ["active", "completed", "abandoned"]},
                            "object_id": {"type": "integer", "minimum": 1},
                            "transition_key": {"type": "string"}, "room_id": {"type": "string"},
                            "rationale": {"type": "string", "minLength": 1},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_tasks",
                    "description": "查询任务账本的完整记录。默认只返回 active；include_closed=true 返回 completed 和 abandoned；写入前用它核对已有 id。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "include_closed": {"type": "boolean", "default": False},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_navigation",
                    "description": (
                        "返回紧凑的可交互候选、路线成本、交互状态和已知传送目的地。"
                        "affordances 中 kind=item_approach 表示物品脚本要求靠近该对象；"
                        "选择该路线时提交 navigate 并携带对应 item_id。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 40, "default": 20}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "inspect_target",
                    "description": "返回一个目标的当前状态、最近交互/对话证据和紧凑路线。",
                    "parameters": {
                        "type": "object",
                        "properties": {"id": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8}},
                        "required": ["id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_observations",
                    "description": "返回最近对话、状态事件或说话者摘要；默认返回对话和事件。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["both", "dialogs", "events", "speakers"], "default": "both"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
                            "speaker": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_shop",
                    "description": (
                        "查询当前购买菜单。无参数返回商品、价格、持有数和可负担状态；"
                        "指定 item_id 返回完整商品元数据，装备类商品还会比较各角色当前同槽装备。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"item_id": {"type": "integer", "minimum": 1}},
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_equipment",
                    "description": (
                        "比较装备。无参数时优先返回最早待审查的新装备；也可指定背包中的 item_id。"
                        "返回每个可装备角色的当前同槽装备、属性差值、换装后属性和特殊效果变化。"
                        "装备与普通使用是独立能力；usable=true 不排除 equipable=true。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "item_id": {"type": "integer", "minimum": 1},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_inventory",
                    "description": (
                        "返回当前只读物品栏详情，包括可用数量、目标派发模式和是否消耗。"
                        "指定 item_id 时返回脚本解析出的 use_effect/throw_effect；"
                        "preview_complete=false 表示存在未解析或条件效果，不可把它当作完整描述。"
                        "target_mode=implicit_event_context 时用 target='all'，底层以 0xFFFF 沿用最近事件对象；"
                        "兼容字段名 all 不一定表示全队效果。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 40},
                            "item_id": {"type": "integer", "minimum": 1},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_room_memory",
                    "description": "返回当前或指定房间的探索摘要和已知传送目的地。",
                    "parameters": {
                        "type": "object",
                        "properties": {"room_id": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_tool_output",
                    "description": "分页读取当前 Agent session 中已归档的完整工具输出；每次最多返回 6000 个字符。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "output_ref": {"type": "string"},
                            "offset": {"type": "integer", "minimum": 0, "default": 0},
                        },
                        "required": ["output_ref"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "submit_decision",
                    "description": "提交最小 dry-run 动作；仅填写当前 action 所需字段。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": [
                                    "wait", "advance_dialog", "select_choice", "navigate", "use_item",
                                    "equip_item", "buy_item", "leave_shop",
                                ],
                            },
                            "target_id": {"type": "integer", "minimum": 1},
                            "item_id": {"type": "integer", "minimum": 1},
                            "role_id": {"type": "integer", "minimum": 0},
                            "target": {"type": "string", "enum": ["all"]},
                            "option_index": {"type": "integer", "minimum": 0},
                            "reason": {"type": "string", "minLength": 1, "maxLength": 300},
                        },
                        "required": ["action", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def agent_tool_initial_context(self, state: dict) -> dict:
        context = {
            "state_summary": self.agent_state_summary(state),
            **self.active_task_context(state),
        }
        dialog = state.get("dialog") if isinstance(state.get("dialog"), dict) else {}
        review = self.agent_runtime.pending_equipment_review()
        if (
            isinstance(review, dict)
            and state.get("in_main_game")
            and not state.get("in_battle")
            and not dialog.get("active")
            and not self.live_info("state").get("stale", True)
        ):
            context["equipment_review"] = {
                "pending": True,
                "item_id": review.get("item_id"),
                "name": review.get("name"),
                "new_count": review.get("new_count"),
            }
        return context

    def agent_tool_messages(self, state: dict) -> list[dict]:
        return [
            {
                "role": "system",
                "content": (
                    "你是 PALPAL 的只读游戏自动化顾问。"
                    "按需使用提供的工具检查当前本地状态。"
                    "你必须通过调用 submit_decision 提交 dry-run 建议来结束本轮。"
                    "绝不执行动作，绝不要求网络搜索，也绝不暴露密钥或 prompts。"
                ),
            },
            {"role": "system", "content": self.agent_policy_prompt()},
            {
                "role": "user",
                "content": json.dumps(self.agent_tool_initial_context(state), ensure_ascii=False, sort_keys=True),
            },
        ]

    def agent_tool_limit(self, value, default: int, upper: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(1, min(parsed, upper))

    def compact_agent_navigation(self, state: dict, limit: int = 20) -> dict:
        navigation = self.navigation(state)
        target_keys = (
            "id", "kind", "status", "steps", "remaining_steps", "segment_steps",
            "required_direction", "transition_candidate", "known_destination",
            "to_room_id", "to_semantic_room", "confidence", "destination_priority",
            "destination_exploration", "returns_to_recent_room", "agent_priority",
            "interaction_state", "previously_tried", "previously_cleared",
            "soft_avoid", "deprioritize_reason", "blocker",
            "transition_key", "to_room_key", "to_region_key", "to_nav_room_id",
            "to_logical_room_id", "dialog_feedback_state", "recent_dialog_feedback",
            "stale_dialog_feedback", "previous_interaction_outcome",
            "known_destination_room_id", "known_destination_room_key",
            "approach", "affordances",
        )
        targets = [
            {key: target.get(key) for key in target_keys if target.get(key) is not None}
            for target in (navigation.get("targets") or [])[:limit]
            if isinstance(target, dict)
        ]
        return {
            "status": navigation.get("status"),
            "targets": targets,
            "targets_returned": len(targets),
            "targets_total": len(navigation.get("targets") or []),
        }

    def run_agent_tool(
        self,
        name: str,
        arguments: dict | None = None,
        state: dict | None = None,
        *,
        tool_cycle: dict | None = None,
    ) -> dict:
        arguments = arguments if isinstance(arguments, dict) else {}
        state = state or self.read_live_state()
        if name == "upsert_task":
            result = self.upsert_agent_task(arguments)
            if result.get("ok") and isinstance(tool_cycle, dict) and tool_cycle.get("tasks_queried"):
                task_id = str((result.get("task") or {}).get("id") or "")
                if task_id in set(tool_cycle.get("required_task_ids") or []):
                    reviewed = tool_cycle.setdefault("reviewed_task_ids", [])
                    if task_id not in reviewed:
                        reviewed.append(task_id)
            return result
        if name == "get_tasks":
            if isinstance(tool_cycle, dict):
                tool_cycle["tasks_queried"] = True
            include_closed = arguments.get("include_closed") is True
            limit = self.agent_tool_limit(arguments.get("limit"), 20, 100)
            all_tasks = self.task_ledger(include_closed=include_closed, limit=None)
            tasks = all_tasks[:limit]
            return {
                "ok": True,
                "include_closed": include_closed,
                "tasks": tasks,
                "returned": len(tasks),
                "total": len(all_tasks),
            }
        if name == "get_navigation":
            limit = self.agent_tool_limit(arguments.get("limit"), 20, 40)
            return {"ok": True, "navigation": self.compact_agent_navigation(state, limit)}
        if name == "inspect_target":
            try:
                target_id = int(arguments.get("id") or 0)
            except (TypeError, ValueError):
                target_id = 0
            if target_id <= 0:
                return {"ok": False, "error": "inspect_target requires a positive id"}
            limit = self.agent_tool_limit(arguments.get("limit"), 8, 20)
            history = self.object_history_view(state, target_id, limit=limit)
            route = self.route_to_object(state, target_id, avoid_transitions=True)
            route.update(self.target_affordance(state, target_id))
            route_keys = (
                "ok", "kind", "status", "steps", "remaining_steps", "segment_steps",
                "required_direction", "move", "blocker", "agent_priority",
                "interaction_state", "known_destination", "known_destination_room_id",
                "known_destination_observed_at", "destination_priority",
                "destination_exploration", "transition_key", "to_room_id",
                "to_semantic_room", "confidence", "returns_to_recent_room",
                "approach",
            )
            return {
                "ok": True,
                "id": target_id,
                "current": history.get("current"),
                "history": (history.get("history") or [])[-limit:],
                "history_total": history.get("total", len(history.get("history") or [])),
                "interaction_summary": history.get("interaction_summary"),
                "route": {key: route.get(key) for key in route_keys if key in route},
            }
        if name == "get_observations":
            kind = arguments.get("kind") if arguments.get("kind") in {"both", "dialogs", "events", "speakers"} else "both"
            limit = self.agent_tool_limit(arguments.get("limit"), 10, 30)
            if kind == "speakers":
                return {"ok": True, "kind": kind, **self.dialog_speakers_view(limit=limit)}
            speaker = arguments.get("speaker")
            speaker = str(speaker).strip() if speaker is not None else None
            result = {"ok": True, "kind": kind}
            if kind in {"both", "dialogs"}:
                result.update(self.dialogs_view(limit=limit, speaker=speaker or None))
            if kind in {"both", "events"}:
                result["events"] = self.event_log[-limit:]
                result["events_total"] = len(self.event_log)
            return result
        if name == "get_inventory":
            limit = self.agent_tool_limit(arguments.get("limit"), 40, 100)
            result = self.agent_inventory(
                state,
                limit=limit,
                item_id=arguments.get("item_id"),
            )
            if "ok" in result:
                return result
            return {"ok": True, **result}
        if name == "get_shop":
            return self.agent_shop(state, arguments.get("item_id"))
        if name == "get_equipment":
            result = self.agent_equipment(state, arguments.get("item_id"))
            candidate = result.get("candidate") if isinstance(result.get("candidate"), dict) else {}
            if result.get("ok") and isinstance(tool_cycle, dict) and candidate.get("item_id"):
                tool_cycle["equipment_review_item_id"] = int(candidate["item_id"])
            return result
        if name == "get_room_memory":
            room_id = arguments.get("room_id")
            room_id = str(room_id) if room_id is not None else None
            return {
                "ok": True,
                "exploration": self.room_exploration_summary(room_id, state),
                "transitions": self.known_transitions_summary(room_id, state),
            }
        if name == "get_tool_output":
            return self.agent_runtime.get_tool_output(
                arguments.get("output_ref"),
                arguments.get("offset", 0),
            )
        if name == "submit_decision":
            submission_error = self.agent_decision_submission_error(arguments)
            if submission_error is not None:
                return submission_error
            action = arguments.get("action")
            if action in {"navigate", "use_item"} and isinstance(tool_cycle, dict):
                required = set(tool_cycle.get("required_task_ids") or [])
                reviewed = set(tool_cycle.get("reviewed_task_ids") or [])
                missing = sorted(required - reviewed)
                if required and (not tool_cycle.get("tasks_queried") or missing):
                    return {
                        "ok": False,
                        "error": {
                            "code": "task_review_required",
                            "message": (
                                "New plot evidence requires task review before navigate/use_item. "
                                "Call get_tasks, then upsert_task once for every affected task; an unchanged upsert confirms continue."
                            ),
                            "required_task_ids": sorted(required),
                            "missing_task_ids": missing,
                            "get_tasks_called": bool(tool_cycle.get("tasks_queried")),
                        },
                    }
            decision = self.normalize_agent_submission(arguments, state)
            self.attach_decision_context(state, decision)
            self.validate_agent_decision(decision)
            validation = decision.get("validation") if isinstance(decision.get("validation"), dict) else {}
            reviewed_item_id = tool_cycle.get("equipment_review_item_id") if isinstance(tool_cycle, dict) else None
            if (
                reviewed_item_id
                and validation.get("status") != "rejected"
                and action != "equip_item"
            ):
                self.agent_runtime.resolve_equipment_review(reviewed_item_id)
            return {"ok": True, "decision": decision}
        raise RuntimeError(f"unsupported agent tool: {name}")

    def parse_agent_tool_arguments(self, tool_call: dict) -> dict:
        function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        raw_arguments = function.get("arguments")
        if raw_arguments is None or raw_arguments == "":
            return {}
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if not isinstance(raw_arguments, str):
            raise RuntimeError(f"tool {function.get('name') or 'unknown'} arguments were not JSON")
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"tool {function.get('name') or 'unknown'} arguments were invalid JSON") from exc
        if not isinstance(arguments, dict):
            raise RuntimeError(f"tool {function.get('name') or 'unknown'} arguments must be a JSON object")
        return arguments

    def tool_result_message(self, tool_call: dict, result: dict) -> dict:
        function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        return {
            "role": "tool",
            "tool_call_id": str(tool_call.get("id") or function.get("name") or "tool_call"),
            "name": str(function.get("name") or "unknown"),
            "content": json.dumps(result, ensure_ascii=False, sort_keys=True),
        }

    def compact_agent_tool_arguments(self, name: str, arguments: dict) -> dict:
        if name == "submit_decision":
            return {key: arguments.get(key) for key in ("action", "target_id", "item_id", "role_id", "target", "option_index", "reason") if key in arguments}
        payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        if len(payload.encode("utf-8")) <= 800:
            return arguments
        return {
            "_truncated": True,
            "bytes": len(payload.encode("utf-8")),
            "keys": sorted(str(key) for key in arguments.keys())[:20],
        }

    def agent_tool_trace_entry(self, name: str, arguments: dict, result: dict | None = None, error: str | None = None) -> dict:
        entry = {"name": name, "arguments": self.compact_agent_tool_arguments(name, arguments)}
        if result is not None:
            payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
            entry["result_bytes"] = len(payload.encode("utf-8"))
            if isinstance(result, dict) and result.get("error"):
                entry["error"] = str(result.get("error"))
        if error:
            entry["error"] = error
        return entry

    def merge_agent_usage(self, total: dict, response_data: dict) -> dict:
        usage = self.model_usage(response_data)
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                total[key] = total.get(key, 0) + value
            else:
                total[key] = value
        return total

    def agent_context(self, state: dict, navigation: dict, dialogs: dict, events: list[dict]) -> dict:
        pos = state.get("party_position") or {}
        dialog = state.get("dialog") or {}
        latest = dialog.get("latest_event") or {}
        choice = state.get("choice") if isinstance(state.get("choice"), dict) else None
        identity = self.room_identity(state)
        room = state.get("room") if isinstance(state.get("room"), dict) else {}
        room_payload = dict(room)
        if identity is not None:
            room_payload.update(
                {
                    "id": identity["id"],
                    "key": identity["room_key"],
                    "logical_room_id": identity["id"],
                    "logical_room_label": identity.get("logical_room_label"),
                    "room_key": identity["room_key"],
                    "region_key": identity["region_key"],
                    "nav_room_id": identity["nav_room_id"],
                    "legacy_room_id": identity.get("legacy_room_id"),
                    "legacy_nav_room_id": identity.get("legacy_nav_room_id"),
                }
            )
        recent_dialogs = (dialogs.get("dialogs") or [])[-10:]
        feedback_by_object = self.same_room_dialog_feedback_by_object(state, recent_dialogs)
        targets = []
        for target in (navigation.get("targets") or [])[:20]:
            target_payload = dict(target)
            try:
                target_id = int(target.get("id") or 0)
            except (TypeError, ValueError):
                target_id = 0
            if target_id in feedback_by_object:
                target_payload["recent_dialog_feedback"] = feedback_by_object[target_id]
                dialog_reason = "最近对话由该对象在本房间产生；请将其视为房间范围内的反馈。"
                existing_reason = target_payload.get("deprioritize_reason")
                target_payload["deprioritize_reason"] = (
                    f"{existing_reason} {dialog_reason}" if existing_reason else dialog_reason
                )
            targets.append(target_payload)
        return {
            "state": {
                "status": state.get("status", "ok"),
                "frame": state.get("frame"),
                "scene_id": state.get("scene_id"),
                "map_id": state.get("map_id"),
                "room": room_payload,
                "room_id": identity["id"] if identity else None,
                "logical_room_id": identity["id"] if identity else None,
                "logical_room_label": identity.get("logical_room_label") if identity else None,
                "room_key": identity["room_key"] if identity else room.get("key"),
                "region_key": identity["region_key"] if identity else state.get("region_key"),
                "nav_room_id": identity["nav_room_id"] if identity else None,
                "legacy_room_id": identity.get("legacy_room_id") if identity else None,
                "legacy_nav_room_id": identity.get("legacy_nav_room_id") if identity else None,
                "in_main_game": state.get("in_main_game"),
                "in_battle": state.get("in_battle"),
                "cash": state.get("cash"),
                "shop": state.get("shop") if isinstance(state.get("shop"), dict) else None,
                "party_position": {"x": pos.get("x"), "y": pos.get("y")},
                "party_direction": state.get("party_direction"),
                "party": [
                    {
                        "role_id": role.get("role_id"),
                        "scene_sprite_id": role.get("scene_sprite_id"),
                    }
                    for role in state.get("party", [])
                    if isinstance(role, dict)
                ],
                "dialog": {
                    "active": bool(dialog.get("active")),
                    "position": dialog.get("position"),
                    "visible": dialog.get("visible"),
                    "latest_text": latest.get("text"),
                    "message_id": latest.get("message_id"),
                    "event_object_id": latest.get("event_object_id"),
                },
                "choice": choice,
            },
            "navigation": {
                "status": navigation.get("status"),
                "targets": targets,
            },
            "exploration": self.agent_exploration_summary(self.agent_current_exploration(state)),
            **self.active_task_context(state),
            "dialogs": recent_dialogs,
            "dialog_feedback_by_object": {str(obj_id): entries for obj_id, entries in feedback_by_object.items()},
            "events": events[-10:],
            "recent_agent_decisions": self.agent_recent_decision_context(limit=5),
        }

    def agent_system_prompt(self) -> str:
        return (
            "你是 PALPAL 的只读游戏自动化顾问。"
            "必须调用 submit_decision 结束本轮，只提交最小动作和一句简短 reason。"
            "dialog.active=true 时提交 {action:'advance_dialog',reason:'...'}。"
            "choice.active=true 时必须阅读 prompt 和 options，并提交 "
            "{action:'select_choice',option_index:<index>,reason:'...'}；不得依赖默认项。"
            "检查对象或进入传送时提交 {action:'navigate',target_id:<id>,reason:'...'}；服务端根据目标推导语义。"
            "选择 affordances 中的 item_approach 时提交 "
            "{action:'navigate',target_id:<id>,item_id:<affordance.item_id>,reason:'...'}；"
            "该动作只移动到满足物品脚本距离的站位并调整朝向，完成后仍需另行提交 use_item。"
            "等待时提交 {action:'wait',reason:'...'}。"
            "shop.active=true 时先调用 get_shop；购买一件商品提交 "
            "{action:'buy_item',item_id:<id>,reason:'...'}，不购买并退出提交 "
            "{action:'leave_shop',reason:'...'}。商店选择必须由模型根据现金、持有物和装备比较显式决定。"
            "使用普通队员物品时提交 {action:'use_item',item_id:<id>,role_id:<role>,reason:'...'}；"
            "target_mode=implicit_event_context 时改为 {action:'use_item',item_id:<id>,target:'all',reason:'...'}。"
            "equipment_review.pending=true 时先调用 get_equipment；综合角色定位、属性差值和特殊效果决定是否提交 "
            "{action:'equip_item',item_id:<id>,role_id:<role>,reason:'...'}，不要仅按价格判断。"
            "注意 target:'all' 是兼容字段名：对 target_mode=implicit_event_context 的物品，SDLPAL 实际以 0xFFFF "
            "运行使用脚本并沿用最近事件对象，不一定表示对全体队员生效。"
            "不要提交 intent、recommended_action、evidence、risk、alternatives、dry_run 或原始按键。"
            "不要用固定价格阈值、物品名称规则或规则后端替代模型的购买/退出决策。"
            "不要执行游戏控制，不要声称动作已经执行，也不要包含密钥或 prompts。"
        )

    def agent_policy_prompt(self) -> str:
        return (
            "决策策略和现场指南。"
            "只返回 dry-run 决策；绝不执行动作。"
            "不要编造所给字段之外的对象标签、剧情事实或房间身份。"
            "speaker_hint 只是对话元数据；绝不要据此推断类似 ID X (speaker) 的标签。"
            "最近对话历史是角色/对象时间线上下文；重复反馈只限定在逻辑房间和目标对象范围内。"
            "recent_dialog_feedback 表示该对象在此逻辑房间产生过对话；除非状态变化或其他具体进展使重试有意义，否则不要再次建议与同一目标交互。"
            "比较目标时，分别报告 recent_dialog、soft_avoid、previously_cleared 和 previously_tried。"
            "将 agent_priority、interaction_state、recent_dialog_feedback、对象历史、目的地探索状态、路线成本和最近决策作为决策信号；不要把任何单一信号变成无条件优先规则。"
            "距离是成本信号，不是优先级规则；当近处目标重复陈旧或无效果行为时，它仍可能比更远目标或传送更差。"
            "对于刚刚交互过、可能没有产生有效变化，或可能改变了 trigger kind、script、transition status 的目标，使用 inspect_target。"
            "若当前目标 transition_candidate=true、最近对象历史显示它由非传送目标变为传送目标、且 known_destination=false，"
            "应将它视为刚解锁的探索路线并优先建议 enter_transition；解锁动作本身产生的 previously_tried 或 avoid_repeat 不应降低它的优先级。"
            "一旦该传送已有 known destination，则停止使用刚解锁优先级，恢复目的地探索价值、任务和回退风险的正常比较。"
            "当最近建议看起来反复访问同一房间、目标或传送循环时，依据持久 session 中的最近决策和 get_room_memory 判断。"
            "传送目标包含 transition_key、to_semantic_room、confidence、destination_priority、destination_exploration 和 returns_to_recent_room。"
            "任务是 agent 从对话和事件推断的记忆，不是引擎事实。自动上下文只是 active 任务的精简索引；需要完整描述、理由、历史或关闭任务时先调用 get_tasks。"
            "NPC 的明确委托、剧情携带、交付和阶段性目标应创建细粒度 task；普通探索不必为每个对象创建 task，账本为空也要按探索价值行动。"
            "发现、调整、完成或作废任务时调用 upsert_task；更新前先用 get_tasks 核对已有 id，不要仅凭时间自动关闭任务。"
            "upsert_task 可用稳定自选 id：未知 id 需同时给出完整 description 和 rationale 才会按该 id 创建；省略 id 则自动生成 task-* id。"
            "task_review.required=true 表示新剧情证据晚于相关 active task 的上次审计。对话仍可继续 advance_dialog；对话结束后若要 navigate 或 use_item，"
            "必须先调用 get_tasks，再对 task_review.tasks 中每个 id 调用 upsert_task，选择确认继续、改写、completed 或 abandoned。无字段变化的 upsert_task 表示确认继续。"
            "新证据改变对象身份或任务阶段时，应关闭或改写旧 task，并按需创建后继 task；把新对话与稳定状态变化写入 rationale，但不要把 sprite 数字硬编码成剧情语义。"
            "背包里没有对应道具，不能否定由相邻对话和稳定 scene_sprite_id 净变化共同证明的剧情携带状态；短暂 sprite 动画若恢复原值则不算阶段变化。"
            "只有 active 任务明确关联到当前可执行对象、当前可达传送，或可达且已知通往任务 room_id 的传送时，才可压过探索策略；不可执行任务不得阻塞探索。"
            "没有可执行 active 任务时，非回退的 unknown 目的地具有最高探索价值，其后是 unexplored、partial、complete；unknown 不代表独立风险。"
            "比较传送时，要在这些目的地探索价值、循环风险、当前房间剩余有用目标之间权衡。"
            "有人类确认的 to_semantic_room 时，优先用它理解房间连接；不要只根据 object id 推断目的地。"
            "将 returns_to_recent_room 和 agent_priority avoid_backtrack 视为回退风险信号；如果选择它们，需要明确说明理由。"
            "对话反馈可以创建或改变目标，但要从提供的对话和状态推断目标，不要匹配固定剧情短语。"
            "如果 dialog.active 为 true，优先选择 advance_dialog。如果 runtime stale、waiting 或 parse-error，建议 wait_runtime。"
            "如果 choice.active=true，必须根据问题与选项文本显式选择 select_choice；此时不要推进对话或导航。"
            "如果 shop.active=true，先用 get_shop 查看商品；装备类候选要查看 equipment_comparisons，"
            "综合现金、已有数量、角色定位、属性差值、特殊效果和任务上下文后，显式选择买一件或退出。"
            "不确定商品用途或收益时可以退出，但不得把价格或名称当作固定购买规则。"
            "只有在主游戏、非战斗、非对话状态下才可建议 use_inventory_item。先调用 get_inventory，确认物品 usable、"
            "available_amount 大于零，并严格匹配 apply_to_all 的目标形式。可结合物品名称、队伍 HP/MP、任务与剧情上下文"
            "保守推断用途；用途或收益证据不足时不要仅因 usable=true 就建议使用，并在 reason 中说明关键权衡。"
            "当 NPC 明确索要某物、背包存在匹配的 target_mode=implicit_event_context 物品、且刚与该 NPC 交互并结束对话时，"
            "应优先考虑 submit_decision {action:'use_item',item_id:<id>,target:'all',reason:'...'}；这会让 0xFFFF 使用脚本沿用最近 NPC/事件对象上下文。"
            "不要因 target_object_id 被拒绝就推断剧情物品只能靠重复 NPC 交互自动使用。进程重启或载入存档不会恢复最近事件对象，"
            "因此应先重新与目标 NPC 交互，再使用这种物品。普通全局效果脚本可能忽略事件上下文，不要把所有 apply_to_all 物品都视为剧情物品。"
            "对于任何明确的 search/touch 或 transition 目标，提交 navigate 和 target_id；"
            "若选择同一目标上的 item_approach affordance，必须同时提交该 affordance 的 item_id。"
            "不要建议无目标的房间探索；如果没有明确目标，提交 wait。"
            "使用 get_navigation 比较传送目的地；只对你认真考虑的一两个目标调用 inspect_target。"
            "解释所选目标背后的权衡，包括相关的陈旧反馈、重复/无效果交互风险、离开房间风险和回退风险。"
        )

    def agent_messages(self, state: dict, navigation: dict, dialogs: dict, events: list[dict]) -> list[dict]:
        return [
            {"role": "system", "content": self.agent_system_prompt()},
            {"role": "system", "content": self.agent_policy_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    self.agent_context(state, navigation, dialogs, events),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
        ]

    def model_usage(self, response_data: dict) -> dict:
        usage = response_data.get("usage") if isinstance(response_data, dict) else None
        if not isinstance(usage, dict):
            return {}
        keys = (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        )
        return {key: usage[key] for key in keys if key in usage}

    def normalize_agent_submission(self, raw: dict, state: dict) -> dict:
        """Expand the model's minimal action into the stable internal decision shape."""
        action = raw["action"]
        reason = str(raw["reason"]).strip()
        evidence: dict = {"submitted_action": action}
        risk = "Server validation will run again immediately before execution."
        if action == "wait":
            intent, preview = "wait_runtime", self.action_preview("no_op", {"reason": "agent_wait"})
            risk = "No game action will be executed."
        elif action == "advance_dialog":
            intent, preview = "advance_dialog", self.action_preview("advance_dialog", self.normalize_dialog_advance_args({}))
            risk = "Dialogue advancement uses the server safety limits and stable-state observer."
        elif action == "select_choice":
            option_index = int(raw["option_index"])
            choice = state.get("choice") if isinstance(state.get("choice"), dict) else {}
            option = next(
                (item for item in choice.get("options") or [] if int(item.get("index", -1)) == option_index),
                None,
            )
            intent = "select_choice"
            preview = self.action_preview("choice_select", {
                "generation": int(choice.get("generation") or 0),
                "option_index": option_index,
            })
            evidence["choice"] = {"prompt": choice.get("prompt"), "option": option}
            risk = "The active choice generation and selected option will be revalidated before confirmation."
        elif action == "navigate":
            target_id = int(raw["target_id"])
            target = next((item for item in state.get("objects") or [] if str(item.get("id")) == str(target_id)), None)
            is_transition = bool(target and transition_candidate(target))
            navigation_target = next(
                (
                    item for item in self.navigation(state).get("targets") or []
                    if int(item.get("id") or 0) == target_id
                ),
                {},
            )
            approach = navigation_target.get("approach") if isinstance(navigation_target.get("approach"), dict) else {}
            affordances = navigation_target.get("affordances") if isinstance(navigation_target.get("affordances"), list) else []
            args = {"id": target_id}
            approach_item_id = int(raw.get("item_id") or 0)
            if approach_item_id > 0:
                args["item_id"] = approach_item_id
            matching_affordance = next(
                (
                    entry for entry in affordances
                    if isinstance(entry, dict) and int(entry.get("item_id") or 0) == approach_item_id
                ),
                None,
            )
            intent = "approach_target" if matching_affordance is not None else "enter_transition" if is_transition else "inspect_target"
            preview = self.action_preview("navigation_step", args)
            evidence["target"] = {
                "id": target_id,
                "transition_candidate": is_transition,
                "kind": navigation_target.get("kind"),
            }
            if approach:
                evidence["approach"] = approach
            if affordances:
                evidence["affordances"] = affordances
            risk = "May leave the current room." if is_transition else "The target and route will be revalidated before movement."
        elif action == "use_item":
            args = {"item_id": int(raw["item_id"])}
            if "role_id" in raw:
                args["role_id"] = int(raw["role_id"])
            else:
                args["target"] = "all"
            intent, preview = "use_inventory_item", self.action_preview("use_item", args)
            evidence["item_id"] = args["item_id"]
            risk = "Item availability, target mode, and consumption risk will be validated against the latest inventory."
        elif action == "buy_item":
            item_id = int(raw["item_id"])
            args = {"item_id": item_id}
            shop = self.agent_shop(state, item_id)
            intent, preview = "purchase_shop_item", self.action_preview("shop_buy", args)
            evidence["item_id"] = item_id
            evidence["shop_item"] = shop.get("item")
            evidence["equipment_comparisons"] = shop.get("equipment_comparisons")
            risk = "The active store, offered item, price, and cash will be revalidated before buying exactly one item."
        elif action == "leave_shop":
            intent, preview = "leave_shop", self.action_preview("shop_leave", {})
            risk = "The active purchase menu will be closed without buying an item."
        else:
            item_id = int(raw["item_id"])
            role_id = int(raw["role_id"])
            args = {"item_id": item_id, "role_id": role_id}
            equipment = self.agent_equipment(state, item_id)
            comparison = next(
                (
                    entry for entry in equipment.get("comparisons") or []
                    if isinstance(entry, dict) and int(entry.get("role_id", -1)) == role_id
                ),
                None,
            )
            current_item = comparison.get("current_item") if isinstance(comparison, dict) else None
            if isinstance(current_item, dict):
                args["expected_slot_item_id"] = int(current_item.get("item_id") or 0)
            intent, preview = "equip_inventory_item", self.action_preview("equip_item", args)
            evidence["item_id"] = item_id
            evidence["role_id"] = role_id
            evidence["equipment_comparison"] = comparison
            risk = "Inventory, role eligibility, and the compared equipment slot will be revalidated immediately before equipping."
        return {
            "summary": reason,
            "recommended_intent": intent,
            "recommended_action": preview,
            "evidence": evidence,
            "risk": risk,
            "alternatives": [],
        }

    def agent_decision_submission_error(self, raw: dict) -> dict | None:
        """Return a repairable error for the minimal submit_decision contract."""
        issues: list[dict] = []
        allowed_keys = {"action", "target_id", "item_id", "role_id", "target", "option_index", "reason"}
        for key in raw:
            if key not in allowed_keys:
                issues.append({"field": key, "code": "unsupported_field", "message": f"{key} is not part of the minimal decision contract"})
        action = raw.get("action")
        allowed_actions = {
            "wait", "advance_dialog", "select_choice", "navigate", "use_item", "equip_item",
            "buy_item", "leave_shop",
        }
        if action not in allowed_actions:
            issues.append({
                "field": "action",
                "code": "invalid_action",
                "message": (
                    "action must be wait, advance_dialog, select_choice, navigate, use_item, "
                    "equip_item, buy_item, or leave_shop"
                ),
            })
        if not isinstance(raw.get("reason"), str) or not raw.get("reason", "").strip():
            issues.append({"field": "reason", "code": "invalid_reason", "message": "reason must be a non-empty string"})
        elif len(raw["reason"]) > 300:
            issues.append({"field": "reason", "code": "reason_too_long", "message": "reason must be at most 300 characters"})

        def positive_int(field: str, *, allow_zero: bool = False) -> int | None:
            try:
                value = int(raw.get(field))
            except (TypeError, ValueError):
                return None
            return value if value >= (0 if allow_zero else 1) else None

        if action == "navigate" and positive_int("target_id") is None:
            issues.append({"field": "target_id", "code": "invalid_target_id", "message": "navigate requires a positive target_id"})
        if action == "navigate" and "item_id" in raw and positive_int("item_id") is None:
            issues.append({"field": "item_id", "code": "invalid_item_id", "message": "navigate approach item_id must be positive"})
        if action == "select_choice" and positive_int("option_index", allow_zero=True) is None:
            issues.append({"field": "option_index", "code": "invalid_option_index", "message": "select_choice requires a non-negative option_index"})
        if action == "use_item":
            if positive_int("item_id") is None:
                issues.append({"field": "item_id", "code": "invalid_item_id", "message": "use_item requires a positive item_id"})
            has_role = "role_id" in raw and positive_int("role_id", allow_zero=True) is not None
            has_all = raw.get("target") == "all"
            if has_role == has_all:
                issues.append({"field": "role_id|target", "code": "invalid_item_target", "message": "use_item requires exactly one of role_id or target='all'"})
        if action == "equip_item":
            if positive_int("item_id") is None:
                issues.append({"field": "item_id", "code": "invalid_item_id", "message": "equip_item requires a positive item_id"})
            if positive_int("role_id", allow_zero=True) is None:
                issues.append({"field": "role_id", "code": "invalid_role_id", "message": "equip_item requires a non-negative role_id"})
        if action == "buy_item" and positive_int("item_id") is None:
            issues.append({"field": "item_id", "code": "invalid_item_id", "message": "buy_item requires a positive item_id"})
        relevant_keys = {
            "wait": {"action", "reason"},
            "advance_dialog": {"action", "reason"},
            "select_choice": {"action", "option_index", "reason"},
            "navigate": {"action", "target_id", "item_id", "reason"},
            "use_item": {"action", "item_id", "role_id", "target", "reason"},
            "equip_item": {"action", "item_id", "role_id", "reason"},
            "buy_item": {"action", "item_id", "reason"},
            "leave_shop": {"action", "reason"},
        }.get(action)
        if relevant_keys:
            for key in raw.keys() - relevant_keys:
                if key in allowed_keys:
                    issues.append({"field": key, "code": "irrelevant_field", "message": f"{key} is not used by action={action}"})
        if not issues:
            return None
        return {"ok": False, "error": {"code": "invalid_decision_submission", "message": "submit_decision was not accepted. Correct the listed fields and call submit_decision again.", "issues": issues}}

    def shop_action_validation_error(
        self,
        state: dict,
        action: str,
        args: dict,
        check_pending: bool = True,
    ) -> str | None:
        if state.get("status") not in {None, "ok"}:
            return str(state.get("status"))
        if not state.get("in_main_game"):
            return "not_in_main_game"
        if self.live_info("state").get("stale", True):
            return "runtime_stale"
        control_error = self.action_control_error(state, action)
        if control_error is not None and (check_pending or control_error != "action_pending"):
            return control_error
        shop = state.get("shop") if isinstance(state.get("shop"), dict) else None
        if not shop or shop.get("active") is not True or shop.get("mode") != "buy":
            return "shop_inactive"
        if action == "shop_leave":
            return None
        if action != "shop_buy":
            return "invalid_shop_action"
        try:
            item_id = int(args.get("item_id") or 0)
        except (TypeError, ValueError):
            item_id = 0
        item = next(
            (
                entry for entry in shop.get("items") or []
                if isinstance(entry, dict) and int(entry.get("item_id") or 0) == item_id
            ),
            None,
        )
        if item_id <= 0 or not isinstance(item, dict):
            return "item_not_offered"
        try:
            price = int(item.get("price") or 0)
            cash = int(state.get("cash") or 0)
        except (TypeError, ValueError):
            return "invalid_shop_state"
        if price > cash or item.get("affordable") is not True:
            return "insufficient_cash"
        return None

    def use_item_validation_error(self, state: dict, args: dict, check_pending: bool = True) -> str | None:
        if state.get("status") not in {None, "ok"}:
            return str(state.get("status"))
        if not state.get("in_main_game"):
            return "not_in_main_game"
        if self.live_info("state").get("stale", True):
            return "runtime_stale"
        control_error = self.action_control_error(state, "use_item")
        if control_error is not None and (check_pending or control_error != "action_pending"):
            return control_error

        try:
            item_id = int(args.get("item_id") or 0)
        except (TypeError, ValueError):
            item_id = 0
        inventory = state.get("inventory") if isinstance(state.get("inventory"), list) else []
        item = None
        for entry in inventory:
            if not isinstance(entry, dict):
                continue
            try:
                entry_item_id = int(entry.get("item_id") or 0)
            except (TypeError, ValueError):
                continue
            if entry_item_id == item_id:
                item = entry
                break
        if item_id <= 0 or item is None:
            return "not_in_inventory"
        try:
            available = int(item.get("amount") or 0) - int(item.get("amount_in_use") or 0)
        except (TypeError, ValueError):
            available = 0
        if available <= 0:
            return "not_available"
        if item.get("usable") is not True:
            return "not_usable"

        apply_to_all = item.get("apply_to_all") is True
        if apply_to_all:
            if args.get("target") != "all" or "role_id" in args:
                return "target_must_use_implicit_context"
            return None
        if args.get("target") == "all" or "role_id" not in args:
            return "target_must_be_role"
        try:
            role_id = int(args.get("role_id"))
        except (TypeError, ValueError):
            return "invalid_role_id"
        party = state.get("party") if isinstance(state.get("party"), list) else []
        party_role_ids = set()
        for role in party:
            if not isinstance(role, dict) or "role_id" not in role:
                continue
            try:
                party_role_ids.add(int(role.get("role_id")))
            except (TypeError, ValueError):
                continue
        if role_id not in party_role_ids:
            return "role_not_in_party"
        return None

    def equip_item_validation_error(self, state: dict, args: dict, check_pending: bool = True) -> str | None:
        if state.get("status") not in {None, "ok"}:
            return str(state.get("status"))
        if not state.get("in_main_game"):
            return "not_in_main_game"
        if self.live_info("state").get("stale", True):
            return "runtime_stale"
        control_error = self.action_control_error(state, "equip_item")
        if control_error is not None and (check_pending or control_error != "action_pending"):
            return control_error
        try:
            item_id = int(args.get("item_id") or 0)
            role_id = int(args.get("role_id"))
        except (TypeError, ValueError):
            return "invalid_equipment_target"
        candidate = next(
            (
                item for item in state.get("inventory") or []
                if isinstance(item, dict) and int(item.get("item_id") or 0) == item_id
            ),
            None,
        )
        if item_id <= 0 or not isinstance(candidate, dict):
            return "not_in_inventory"
        try:
            available = int(candidate.get("amount") or 0) - int(candidate.get("amount_in_use") or 0)
        except (TypeError, ValueError):
            available = 0
        if available <= 0:
            return "not_available"
        if candidate.get("equipable") is not True:
            return "not_equipable"
        eligible = set()
        for value in candidate.get("equipable_role_ids") or []:
            try:
                eligible.add(int(value))
            except (TypeError, ValueError):
                pass
        if role_id not in eligible:
            return "role_not_eligible"
        role = next(
            (
                entry for entry in state.get("party") or []
                if isinstance(entry, dict) and int(entry.get("role_id", -1)) == role_id
            ),
            None,
        )
        if not isinstance(role, dict):
            return "role_not_in_party"
        effect = candidate.get("equipment_effect") if isinstance(candidate.get("equipment_effect"), dict) else {}
        try:
            slot_id = int(effect.get("slot_id"))
        except (TypeError, ValueError):
            slot_id = -1
        if slot_id < 0:
            return "invalid_equip_script"
        if "expected_slot_item_id" in args:
            current = next(
                (
                    item for item in role.get("equipment") or []
                    if isinstance(item, dict) and int(item.get("slot_id", -1)) == slot_id
                ),
                None,
            )
            try:
                current_item_id = int((current or {}).get("item_id") or 0)
                expected_item_id = int(args.get("expected_slot_item_id") or 0)
            except (TypeError, ValueError):
                return "equipment_context_stale"
            if current_item_id != expected_item_id:
                return "equipment_context_stale"
        return None

    @staticmethod
    def reject_agent_decision(decision: dict, reason: str, **details) -> None:
        decision["validation"] = {
            "status": "rejected",
            "reason": reason,
            **details,
        }

    def validate_agent_decision(self, decision: dict) -> dict:
        action = decision.get("recommended_action") if isinstance(decision.get("recommended_action"), dict) else {}
        tool = action.get("tool")
        latest_state = self.read_live_state()
        shop = latest_state.get("shop") if isinstance(latest_state.get("shop"), dict) else {}
        shop_active = shop.get("active") is True
        choice = latest_state.get("choice") if isinstance(latest_state.get("choice"), dict) else None
        choice_active = bool(choice and choice.get("active"))
        if tool == "choice_select":
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            error = self.choice_action_validation_error(
                latest_state,
                int(args.get("generation") or 0),
                int(args.get("option_index", -1)),
            )
            if error is None:
                return decision
            self.reject_agent_decision(decision, "choice_action_invalid", error=error)
            decision["recommended_intent"] = "wait_runtime"
            decision["recommended_action"] = self.action_preview("no_op", {"reason": error})
            return decision
        if choice_active:
            self.reject_agent_decision(decision, "in_choice", action=tool)
            decision["recommended_intent"] = "wait_runtime"
            decision["recommended_action"] = self.action_preview(
                "no_op", {"reason": "in_choice", "required_action": "select_choice"}
            )
            decision["risk"] = "A story choice is active; only an explicit select_choice decision is executable."
            return decision
        if tool in {"shop_buy", "shop_leave"}:
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            error = self.shop_action_validation_error(latest_state, tool, args)
            if error is None:
                return decision
            self.reject_agent_decision(
                decision,
                "shop_action_invalid",
                action=tool,
                item_id=args.get("item_id"),
                error=error,
            )
            decision.setdefault("evidence", {})["validation_state_context"] = self.state_context(latest_state)
            decision["recommended_intent"] = "wait_runtime"
            decision["recommended_action"] = self.action_preview(
                "no_op", {"reason": "shop_action_invalid", "error": error}
            )
            decision["risk"] = (
                f"{decision.get('risk') or ''} Latest-state validation rejected the shop action: {error}."
            ).strip()
            return decision
        if shop_active:
            self.reject_agent_decision(
                decision,
                "in_shop",
                action=tool,
                store_id=shop.get("store_id"),
                phase=shop.get("phase"),
            )
            decision.setdefault("evidence", {})["validation_state_context"] = self.state_context(latest_state)
            decision["recommended_intent"] = "wait_runtime"
            decision["recommended_action"] = self.action_preview(
                "no_op", {"reason": "in_shop", "required_actions": ["buy_item", "leave_shop"]}
            )
            decision["risk"] = "A purchase menu is active; only an explicit model buy_item or leave_shop decision is executable."
            return decision
        if action.get("tool") == "equip_item":
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            error = self.equip_item_validation_error(latest_state, args)
            if error is None:
                return decision
            self.reject_agent_decision(
                decision,
                "equip_item_invalid",
                item_id=args.get("item_id"),
                role_id=args.get("role_id"),
                error=error,
            )
            decision.setdefault("evidence", {})["validation_state_context"] = self.state_context(latest_state)
            decision["recommended_intent"] = "wait_runtime"
            decision["recommended_action"] = self.action_preview(
                "no_op", {"reason": "equip_item_invalid", "error": error}
            )
            decision["risk"] = (
                f"{decision.get('risk') or ''} Latest-state validation rejected equipment change: {error}."
            ).strip()
            return decision
        if action.get("tool") == "use_item":
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            error = self.use_item_validation_error(latest_state, args)
            if error is None:
                return decision
            evidence = decision.setdefault("evidence", {})
            if not isinstance(evidence, dict):
                evidence = {}
                decision["evidence"] = evidence
            warnings = evidence.get("validation_warnings")
            if not isinstance(warnings, list):
                warnings = []
                evidence["validation_warnings"] = warnings
            warnings.append("use_item was not valid in the latest state; downgraded to wait_runtime")
            evidence["validation_state_context"] = self.state_context(latest_state)
            evidence["original_recommendation"] = {
                "recommended_intent": decision.get("recommended_intent"),
                "recommended_action": action,
            }
            decision["recommended_intent"] = "wait_runtime"
            decision["recommended_action"] = self.action_preview(
                "no_op", {"reason": "use_item_invalid", "error": error}
            )
            self.reject_agent_decision(
                decision,
                "use_item_invalid",
                item_id=args.get("item_id"),
                error=error,
            )
            decision["risk"] = (
                f"{decision.get('risk') or ''} Latest-state validation rejected item use: {error}."
            ).strip()
            return decision
        if action.get("tool") != "navigation_step":
            return decision
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        try:
            target_id = int(args.get("id") or 0)
            approach_item_id = int(args.get("item_id") or 0) or None
        except (TypeError, ValueError):
            target_id = 0
            approach_item_id = None
        if target_id <= 0:
            return decision

        latest_state = self.read_live_state()
        route = self.route_to_object(
            latest_state,
            target_id,
            avoid_transitions=True,
            approach_item_id=approach_item_id,
        )
        route_status = route.get("status")
        if route_status in {"reachable", "transition", "blocked_by_interactable"}:
            return decision

        evidence = decision.setdefault("evidence", {})
        if not isinstance(evidence, dict):
            evidence = {}
            decision["evidence"] = evidence
        warnings = evidence.get("validation_warnings")
        if not isinstance(warnings, list):
            warnings = []
            evidence["validation_warnings"] = warnings
        warnings.append("navigation_step target was not valid in the latest state; downgraded to wait_runtime")
        evidence["validation_target_id"] = target_id
        evidence["validation_route_status"] = route_status
        evidence["validation_route_ok"] = route.get("ok")
        evidence["validation_state_context"] = self.state_context(latest_state)
        evidence["original_recommendation"] = {
            "recommended_intent": decision.get("recommended_intent"),
            "recommended_action": action,
        }
        decision["recommended_intent"] = "wait_runtime"
        decision["recommended_action"] = self.action_preview(
            "no_op",
            {
                "reason": "navigation_target_invalid",
                "target_id": target_id,
                "route_status": route_status,
            },
        )
        self.reject_agent_decision(
            decision,
            "navigation_target_invalid",
            target_id=target_id,
            route_status=route_status,
            route_ok=route.get("ok"),
        )
        if decision.get("risk"):
            decision["risk"] = f"{decision['risk']} Latest-state validation rejected the navigation target."
        else:
            decision["risk"] = "Latest-state validation rejected the navigation target."
        return decision

    def normalize_action_args(self, tool: str, args: dict) -> dict:
        if tool == "press_buttons":
            aliases = {
                "a": "search",
                "confirm": "search",
                "ok": "search",
                "enter": "search",
                "b": "menu",
                "cancel": "menu",
                "escape": "menu",
            }
            raw_buttons = args.get("buttons", [])
            if isinstance(raw_buttons, str):
                raw_buttons = [raw_buttons]
            buttons = []
            if isinstance(raw_buttons, list):
                for button in raw_buttons:
                    normalized = aliases.get(str(button).strip().lower(), str(button).strip().lower())
                    if normalized in VALID_BUTTONS:
                        buttons.append(normalized)
            try:
                frames = int(args.get("frames", 1))
            except (TypeError, ValueError):
                frames = 1
            return {"buttons": buttons or ["search"], "frames": max(1, min(frames, 300))}
        if tool == "advance_dialog":
            return self.normalize_dialog_advance_args(args)
        if tool == "navigation_step":
            try:
                result = {"id": int(args.get("id") or args.get("target_id") or 0)}
            except (TypeError, ValueError):
                return {"id": 0}
            try:
                item_id = int(args.get("item_id") or 0)
            except (TypeError, ValueError):
                item_id = 0
            if item_id > 0:
                result["item_id"] = item_id
            return result
        if tool == "use_item":
            try:
                item_id = int(args.get("item_id") or 0)
            except (TypeError, ValueError):
                item_id = 0
            if args.get("target") == "all":
                return {"item_id": item_id, "target": "all"}
            try:
                role_id = int(args.get("role_id"))
            except (TypeError, ValueError):
                role_id = -1
            return {"item_id": item_id, "role_id": role_id}
        if tool == "equip_item":
            try:
                item_id = int(args.get("item_id") or 0)
                role_id = int(args.get("role_id"))
            except (TypeError, ValueError):
                item_id, role_id = 0, -1
            result = {"item_id": item_id, "role_id": role_id}
            if "expected_slot_item_id" in args:
                try:
                    result["expected_slot_item_id"] = int(args.get("expected_slot_item_id") or 0)
                except (TypeError, ValueError):
                    pass
            return result
        if tool == "shop_buy":
            try:
                return {"item_id": int(args.get("item_id") or 0)}
            except (TypeError, ValueError):
                return {"item_id": 0}
        if tool == "shop_leave":
            return {}
        return dict(args)

    def parse_model_json(self, content: str) -> dict:
        content = content.strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
        data = json.loads(content.strip())
        if not isinstance(data, dict):
            raise ValueError("model did not return a JSON object")
        return data

    def deepseek_model(self, requested_model: str | None = None) -> str:
        configured = self.config_value("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL) or DEFAULT_DEEPSEEK_MODEL
        for candidate in (requested_model, configured, DEFAULT_DEEPSEEK_MODEL):
            model = str(candidate or "").strip()
            if model in ALLOWED_DEEPSEEK_MODELS:
                return model
        return DEFAULT_DEEPSEEK_MODEL

    def deepseek_decide_agent(self, state: dict | None = None, model: str | None = None) -> dict:
        state = state or self.read_live_state()
        api_key = self.config_value("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        base_url = (self.config_value("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL) or DEFAULT_DEEPSEEK_BASE_URL).rstrip("/")
        model = self.deepseek_model(model)
        try:
            timeout = float(self.config_value("DEEPSEEK_TIMEOUT", str(DEFAULT_DEEPSEEK_TIMEOUT)) or DEFAULT_DEEPSEEK_TIMEOUT)
        except ValueError:
            timeout = DEFAULT_DEEPSEEK_TIMEOUT
        if timeout <= 0:
            timeout = DEFAULT_DEEPSEEK_TIMEOUT

        llm_call_id = f"{int(time.time() * 1000)}-{threading.get_ident()}"
        messages = self.agent_tool_messages(state)
        tools = self.agent_tool_specs()
        usage_total: dict = {}
        tool_trace: list[dict] = []
        total_tool_calls = 0
        rounds_attempted = 0
        decision_repairs = 0
        tool_cycle = self.new_agent_tool_cycle(state)

        try:
            for _round_index in range(MAX_AGENT_TOOL_ROUNDS):
                rounds_attempted = _round_index + 1
                self.update_agent_status(
                    backend="deepseek",
                    model=model,
                    phase="waiting_for_model",
                    round=rounds_attempted,
                    tool_calls=total_tool_calls,
                    current_tool=None,
                    message=f"Round {rounds_attempted}: waiting for model response.",
                )

                payload = {
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": 0.2,
                }
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                request_started = time.monotonic()
                request = urllib.request.Request(
                    f"{base_url}/chat/completions",
                    data=raw,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(request, timeout=max(0.1, timeout)) as response:
                        response_body = response.read().decode("utf-8")
                    response_data = json.loads(response_body)
                    self.append_agent_llm_log(
                        state,
                        {
                            "ok": True,
                            "backend": "deepseek",
                            "model": model,
                            "llm_call_id": llm_call_id,
                            "round": rounds_attempted,
                            "url": f"{base_url}/chat/completions",
                            "elapsed_ms": round((time.monotonic() - request_started) * 1000, 3),
                            "request": payload,
                            "response": response_data,
                        },
                    )
                except urllib.error.HTTPError as exc:
                    detail = ""
                    try:
                        detail = exc.read().decode("utf-8", errors="replace")
                    except Exception:
                        detail = ""
                    self.append_agent_llm_log(
                        state,
                        {
                            "ok": False,
                            "backend": "deepseek",
                            "model": model,
                            "llm_call_id": llm_call_id,
                            "round": rounds_attempted,
                            "url": f"{base_url}/chat/completions",
                            "elapsed_ms": round((time.monotonic() - request_started) * 1000, 3),
                            "request": payload,
                            "http_status": exc.code,
                            "response_text": detail,
                            "error": f"HTTP {exc.code}",
                        },
                    )
                    suffix = f": {detail[:500]}" if detail else ""
                    raise RuntimeError(f"DeepSeek tool-call request failed with HTTP {exc.code}{suffix}") from exc
                except json.JSONDecodeError as exc:
                    self.append_agent_llm_log(
                        state,
                        {
                            "ok": False,
                            "backend": "deepseek",
                            "model": model,
                            "llm_call_id": llm_call_id,
                            "round": rounds_attempted,
                            "url": f"{base_url}/chat/completions",
                            "elapsed_ms": round((time.monotonic() - request_started) * 1000, 3),
                            "request": payload,
                            "response_text": response_body,
                            "error": f"invalid JSON response: {exc}",
                        },
                    )
                    raise RuntimeError("DeepSeek response was not valid JSON") from exc
                except (TimeoutError, socket.timeout) as exc:
                    self.append_agent_llm_log(
                        state,
                        {
                            "ok": False,
                            "backend": "deepseek",
                            "model": model,
                            "llm_call_id": llm_call_id,
                            "round": rounds_attempted,
                            "url": f"{base_url}/chat/completions",
                            "elapsed_ms": round((time.monotonic() - request_started) * 1000, 3),
                            "request": payload,
                            "error": f"timed out after {timeout:g}s",
                        },
                    )
                    raise RuntimeError(f"DeepSeek request timed out after {timeout:g}s") from exc
                except urllib.error.URLError as exc:
                    reason = getattr(exc, "reason", None)
                    if isinstance(reason, (TimeoutError, socket.timeout)):
                        self.append_agent_llm_log(
                            state,
                            {
                                "ok": False,
                                "backend": "deepseek",
                                "model": model,
                                "llm_call_id": llm_call_id,
                                "round": rounds_attempted,
                                "url": f"{base_url}/chat/completions",
                                "elapsed_ms": round((time.monotonic() - request_started) * 1000, 3),
                                "request": payload,
                                "error": f"timed out after {timeout:g}s",
                            },
                        )
                        raise RuntimeError(f"DeepSeek request timed out after {timeout:g}s") from exc
                    self.append_agent_llm_log(
                        state,
                        {
                            "ok": False,
                            "backend": "deepseek",
                            "model": model,
                            "llm_call_id": llm_call_id,
                            "round": rounds_attempted,
                            "url": f"{base_url}/chat/completions",
                            "elapsed_ms": round((time.monotonic() - request_started) * 1000, 3),
                            "request": payload,
                            "error": str(reason or exc),
                        },
                    )
                    raise RuntimeError("DeepSeek request failed before receiving a response") from exc

                self.merge_agent_usage(usage_total, response_data)
                self.update_agent_status(
                    phase="processing_response",
                    round=rounds_attempted,
                    tool_calls=total_tool_calls,
                    current_tool=None,
                    message=f"Round {rounds_attempted}: processing model tool calls.",
                )
                choices = response_data.get("choices") if isinstance(response_data, dict) else None
                if not choices:
                    raise RuntimeError("DeepSeek response did not include choices")
                message = ((choices[0] or {}).get("message") or {})
                if not isinstance(message, dict):
                    raise RuntimeError("DeepSeek response did not include a valid assistant message")
                tool_calls = message.get("tool_calls")
                if not isinstance(tool_calls, list) or not tool_calls:
                    content = message.get("content")
                    detail = " without tool calls"
                    if isinstance(content, str) and content.strip():
                        detail += f"; assistant content was ignored ({len(content)} chars)"
                    raise RuntimeError(f"DeepSeek response did not call submit_decision{detail}")

                assistant_message = {
                    "role": "assistant",
                    "content": message.get("content") if isinstance(message.get("content"), str) else None,
                    "tool_calls": tool_calls,
                }
                messages.append(assistant_message)

                for tool_call in tool_calls:
                    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                    name = str(function.get("name") or "")
                    try:
                        arguments = self.parse_agent_tool_arguments(tool_call)
                    except RuntimeError as exc:
                        if name != "submit_decision":
                            raise
                        arguments = {}
                        result = {
                            "ok": False,
                            "error": {
                                "code": "invalid_decision_arguments",
                                "message": f"submit_decision arguments were invalid: {exc}. Return a JSON object matching the declared schema and call submit_decision again.",
                            },
                        }
                        total_tool_calls += 1
                        tool_trace.append(self.agent_tool_trace_entry(name, arguments, result))
                        decision_repairs += 1
                        if decision_repairs > MAX_AGENT_DECISION_REPAIRS:
                            raise RuntimeError(f"submit_decision remained invalid after {MAX_AGENT_DECISION_REPAIRS} repair attempt")
                        messages.append(self.tool_result_message(tool_call, result))
                        self.update_agent_status(
                            phase="waiting_for_model",
                            round=rounds_attempted,
                            tool_calls=total_tool_calls,
                            current_tool=None,
                            message="Decision submission needs one format correction.",
                        )
                        break
                    total_tool_calls += 1
                    self.update_agent_status(
                        phase="submitting_decision" if name == "submit_decision" else "running_tool",
                        round=rounds_attempted,
                        tool_calls=total_tool_calls,
                        current_tool=name or "unknown",
                        message=(
                            "Validating final decision."
                            if name == "submit_decision"
                            else f"Calling agent tool {name or 'unknown'}."
                        ),
                    )
                    if total_tool_calls > MAX_AGENT_TOOL_CALLS and name != "submit_decision":
                        error = f"Agent exceeded {MAX_AGENT_TOOL_CALLS} query tool calls without submit_decision"
                        tool_trace.append(self.agent_tool_trace_entry(name, arguments, error=error))
                        raise RuntimeError(error)

                    if name == "submit_decision":
                        result = self.run_agent_tool(name, arguments, state, tool_cycle=tool_cycle)
                        tool_trace.append(self.agent_tool_trace_entry(name, arguments, result))
                        if not result.get("ok"):
                            decision_repairs += 1
                            if decision_repairs > MAX_AGENT_DECISION_REPAIRS:
                                raise RuntimeError(f"submit_decision remained invalid after {MAX_AGENT_DECISION_REPAIRS} repair attempt")
                            messages.append(self.tool_result_message(tool_call, result))
                            self.update_agent_status(
                                phase="waiting_for_model",
                                round=rounds_attempted,
                                tool_calls=total_tool_calls,
                                current_tool=None,
                                message="Decision submission needs one format correction.",
                            )
                            break
                        decision = result["decision"]
                        decision["backend"] = "deepseek"
                        decision["model"] = model
                        decision["llm_call_id"] = llm_call_id
                        decision["tool_trace"] = tool_trace
                        if usage_total:
                            decision["usage"] = usage_total
                        self.append_agent_decision(state, decision)
                        return decision

                    try:
                        result = self.run_agent_tool(name, arguments, state, tool_cycle=tool_cycle)
                    except Exception as exc:
                        tool_trace.append(self.agent_tool_trace_entry(name, arguments, error=str(exc)))
                        raise RuntimeError(f"Agent tool {name or 'unknown'} failed: {exc}") from exc
                    tool_trace.append(self.agent_tool_trace_entry(name, arguments, result))
                    messages.append(self.tool_result_message(tool_call, result))

            raise RuntimeError(f"Agent did not call submit_decision within {MAX_AGENT_TOOL_ROUNDS} tool rounds")
        except Exception as exc:
            self.append_agent_error(
                state,
                backend="deepseek",
                model=model,
                error=str(exc),
                usage=usage_total,
                tool_trace=tool_trace,
                tool_calls=total_tool_calls,
                rounds=rounds_attempted,
                llm_call_id=llm_call_id,
            )
            raise

    def decide_agent(self, state: dict | None = None, model: str | None = None) -> dict:
        model = self.deepseek_model(model)
        use_deepseek = bool(self.config_value("DEEPSEEK_API_KEY"))
        self.begin_agent_status("deepseek" if use_deepseek else "rules", model if use_deepseek else None)
        try:
            if use_deepseek:
                decision = self.deepseek_decide_agent(state, model=model)
            else:
                decision = self.rules_decide_agent(state)
        except Exception as exc:
            self.finish_agent_status(error=str(exc))
            raise
        self.finish_agent_status()
        return decision

    def write_action(
        self,
        buttons: list[str],
        frames: int,
        *,
        control_action: str = "press_buttons",
        control_state: dict | None = None,
    ) -> dict:
        invalid = [button for button in buttons if button not in VALID_BUTTONS]
        if invalid:
            raise ValueError(f"invalid buttons: {', '.join(invalid)}")
        frames = max(1, min(int(frames), 300))
        with self.action_lock:
            state = control_state if isinstance(control_state, dict) else self.read_live_state()
            state_info = None if control_state is not None else self.live_info("state")
            control_error = self.action_control_error(state, control_action, state_info)
            if control_error is not None:
                raise RuntimeError(control_error)

            lines = [f"frames {frames}"]
            lines.extend(f"button {button}" for button in buttons)
            entry = {"time": time.time(), "buttons": buttons, "frames": frames}
            self.begin_written_action(entry)
            try:
                self.atomic_write_text(self.action_path, "\n".join(lines) + "\n", encoding="ascii")
            except Exception:
                self.pending_written_action = None
                raise
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
            if role_id is None:
                raise ValueError("role_id is required for a single-target item")
            role_value = int(role_id)
            if role_value < 0:
                raise ValueError("role_id must be non-negative")
            target_value = str(role_value)

        with self.action_lock:
            state = self.read_live_state()
            args = {"item_id": item_id, "target": "all"} if target_value == "all" else {
                "item_id": item_id,
                "role_id": role_value,
            }
            error = self.use_item_validation_error(state, args)
            if error is not None:
                raise RuntimeError(error)
            try:
                self.action_result_path.unlink()
            except FileNotFoundError:
                pass

            entry = {
                "time": time.time(),
                "command": "use_item",
                "item_id": item_id,
                "role_id": role_value,
                "target": "all" if target_value == "all" else "role",
            }
            self.begin_written_action(entry)
            try:
                self.atomic_write_text(
                    self.action_path,
                    f"use_item {item_id} {target_value}\n",
                    encoding="ascii",
                )
            except Exception:
                self.pending_written_action = None
                raise
            self.action_log.append(entry)
            self.append_navigation_diagnostic("action_written", {"action": entry})
            return entry

    def wait_for_item_action_result(
        self,
        action: str,
        item_id: int,
        role_id: int,
        start_time: float,
        timeout: float = 5.0,
    ) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = self.file_info(self.action_result_path)
            if info.get("available") and float(info.get("mtime") or 0) >= start_time:
                result = read_optional_json(self.action_result_path)
                try:
                    result_item_id = int(result.get("item_id"))
                    result_role_id = int(result.get("role_id"))
                except (TypeError, ValueError):
                    result_item_id = result_role_id = -1
                if (
                    result.get("action") == action
                    and result_item_id == item_id
                    and result_role_id == role_id
                ):
                    return result
            time.sleep(0.03)
        raise TimeoutError(f"{action} {item_id} {role_id} did not complete")

    def current_action_result(self) -> dict:
        return read_optional_json(self.action_result_path)

    def write_equip_item_action(
        self,
        item_id: int,
        role_id: int,
        expected_slot_item_id: int | None = None,
    ) -> dict:
        try:
            item_id = int(item_id)
            role_id = int(role_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("item_id and role_id must be integers") from exc
        args = {"item_id": item_id, "role_id": role_id}
        if expected_slot_item_id is not None:
            args["expected_slot_item_id"] = int(expected_slot_item_id)
        start_time = time.time()
        with self.action_lock:
            state = self.read_live_state()
            error = self.equip_item_validation_error(state, args)
            if error is not None:
                raise RuntimeError(error)
            try:
                self.action_result_path.unlink()
            except FileNotFoundError:
                pass
            entry = {
                "time": start_time,
                "command": "equip_item",
                "item_id": item_id,
                "role_id": role_id,
            }
            self.begin_written_action(entry)
            try:
                self.atomic_write_text(
                    self.action_path,
                    f"equip_item {item_id} {role_id}\n",
                    encoding="ascii",
                )
            except Exception:
                self.pending_written_action = None
                raise
            self.action_log.append(entry)
            self.append_navigation_diagnostic("action_written", {"action": entry})
        result = self.wait_for_item_action_result(
            "equip_item", item_id, role_id, start_time
        )
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or result.get("status") or "equip_item_failed"))
        return {**entry, "result": result}

    def wait_for_shop_action_result(
        self,
        action: str,
        item_id: int,
        start_time: float,
        timeout: float = 5.0,
    ) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = self.file_info(self.action_result_path)
            if info.get("available") and float(info.get("mtime") or 0) >= start_time:
                result = read_optional_json(self.action_result_path)
                try:
                    result_item_id = int(result.get("item_id") or 0)
                except (TypeError, ValueError):
                    result_item_id = -1
                if result.get("action") == action and result_item_id == item_id:
                    return result
            time.sleep(0.03)
        raise TimeoutError(f"{action} {item_id} did not complete")

    def choice_action_validation_error(self, state: dict, generation: int, option_index: int) -> str | None:
        choice = state.get("choice") if isinstance(state.get("choice"), dict) else None
        if not choice or choice.get("active") is not True:
            return "choice_inactive"
        if generation != int(choice.get("generation") or 0):
            return "stale_choice_command"
        options = choice.get("options") if isinstance(choice.get("options"), list) else []
        if option_index not in {
            int(option.get("index")) for option in options
            if isinstance(option, dict) and option.get("index") is not None
        }:
            return "invalid_choice_option"
        if choice.get("command_pending"):
            return "choice_action_pending"
        control_error = self.action_control_error(state, "choice_select")
        return control_error

    def wait_for_choice_action_result(
        self,
        generation: int,
        start_time: float,
        timeout: float = 5.0,
    ) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = self.file_info(self.action_result_path)
            if info.get("available") and float(info.get("mtime") or 0) >= start_time:
                result = read_optional_json(self.action_result_path)
                if (
                    result.get("action") == "choice_select"
                    and int(result.get("generation") or 0) == generation
                ):
                    return result
            time.sleep(0.02)
        raise TimeoutError(f"choice_select generation {generation} did not complete")

    def write_choice_action(self, generation: int, option_index: int) -> dict:
        try:
            generation = int(generation)
            option_index = int(option_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("generation and option_index must be integers") from exc
        start_time = time.time()
        with self.action_lock:
            state = self.read_live_state()
            error = self.choice_action_validation_error(state, generation, option_index)
            if error is not None:
                raise RuntimeError(error)
            choice = state["choice"]
            try:
                self.action_result_path.unlink()
            except FileNotFoundError:
                pass
            entry = {
                "time": start_time,
                "command": "choice_select",
                "generation": generation,
                "option_index": option_index,
                "prompt": choice.get("prompt"),
                "option": next(
                    (item for item in choice.get("options") or [] if int(item.get("index", -1)) == option_index),
                    None,
                ),
            }
            self.begin_written_action(entry)
            try:
                self.atomic_write_text(
                    self.action_path,
                    f"choice_select {generation} {option_index}\n",
                    encoding="ascii",
                )
            except Exception:
                self.pending_written_action = None
                raise
            self.action_log.append(entry)
            self.append_navigation_diagnostic("action_written", {"action": entry})
        result = self.wait_for_choice_action_result(generation, start_time)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or result.get("status") or "choice_select_failed"))
        return {**entry, "result": result}

    def write_shop_action(self, action: str, item_id: int | None = None) -> dict:
        if action not in {"shop_buy", "shop_leave"}:
            raise ValueError("action must be shop_buy or shop_leave")
        try:
            normalized_item_id = int(item_id or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("item_id must be an integer") from exc
        args = {"item_id": normalized_item_id} if action == "shop_buy" else {}
        start_time = time.time()
        with self.action_lock:
            state = self.read_live_state()
            error = self.shop_action_validation_error(state, action, args)
            if error is not None:
                raise RuntimeError(error)
            try:
                self.action_result_path.unlink()
            except FileNotFoundError:
                pass
            entry = {
                "time": start_time,
                "command": action,
                "item_id": normalized_item_id,
            }
            self.begin_written_action(entry)
            line = (
                f"shop_buy {normalized_item_id}\n"
                if action == "shop_buy"
                else "shop_leave 1\n"
            )
            try:
                self.atomic_write_text(self.action_path, line, encoding="ascii")
            except Exception:
                self.pending_written_action = None
                raise
            self.action_log.append(entry)
            self.append_navigation_diagnostic("action_written", {"action": entry})
        result = self.wait_for_shop_action_result(
            action, normalized_item_id, start_time
        )
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or result.get("status") or f"{action}_failed"))
        return {**entry, "result": result}

    def battle_action_validation_error(self, state: dict, args: dict) -> str | None:
        if not state.get("in_battle"):
            return "not_in_battle"
        battle = state.get("battle") if isinstance(state.get("battle"), dict) else None
        if battle is None:
            return "battle_state_unavailable"
        try:
            battle_generation = int(args.get("battle_generation"))
            command_generation = int(args.get("command_generation"))
            player_index = (
                int(args["player_index"])
                if args.get("player_index") is not None
                else None
            )
            object_id = int(args.get("object_id") or 0)
            target_index = int(args.get("target_index", -2))
        except (TypeError, ValueError):
            return "invalid_battle_action"
        if (
            battle_generation != int(battle.get("generation") or 0)
            or command_generation != int(battle.get("command_generation") or 0)
            or (
                player_index is not None
                and player_index != int(battle.get("current_player_index") or 0)
            )
        ):
            return "stale_battle_command"
        if self.action_pending(state):
            return "action_pending"
        action_type = str(args.get("action_type") or "")
        available = battle.get("available_actions") if isinstance(battle.get("available_actions"), list) else []
        match = next(
            (
                action for action in available
                if isinstance(action, dict)
                and action.get("action_type") == action_type
                and int(action.get("object_id") or 0) == object_id
            ),
            None,
        )
        if match is None:
            return "action_unavailable"
        mode = match.get("target_mode")
        if mode == "all" and target_index != -1:
            return "invalid_target"
        if mode == "none" and target_index != -2:
            return "invalid_target"
        if mode == "single" and target_index not in {
            int(value) for value in match.get("valid_target_indices") or []
        }:
            return "invalid_target"
        return None

    def wait_for_battle_action_result(
        self,
        battle_generation: int,
        command_generation: int,
        start_time: float,
        timeout: float = 5.0,
    ) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = self.file_info(self.action_result_path)
            if info.get("available") and float(info.get("mtime") or 0) >= start_time:
                result = read_optional_json(self.action_result_path)
                if (
                    result.get("action") == "battle_action"
                    and int(result.get("battle_generation") or 0) == battle_generation
                    and int(result.get("command_generation") or 0) == command_generation
                ):
                    return result
            time.sleep(0.02)
        raise TimeoutError("battle action did not complete")

    def write_battle_action(self, args: dict) -> dict:
        start_time = time.time()
        with self.action_lock:
            state = self.read_live_state()
            error = self.battle_action_validation_error(state, args)
            if error is not None:
                raise RuntimeError(error)
            battle = state["battle"]
            battle_generation = int(battle["generation"])
            command_generation = int(battle["command_generation"])
            player_index = int(battle.get("current_player_index") or 0)
            action_type = str(args["action_type"])
            object_id = int(args.get("object_id") or 0)
            target_index = int(args.get("target_index", -2))
            try:
                self.action_result_path.unlink()
            except FileNotFoundError:
                pass
            entry = {
                "time": start_time,
                "command": "battle_action",
                "battle_generation": battle_generation,
                "command_generation": command_generation,
                "player_index": player_index,
                "action_type": action_type,
                "object_id": object_id,
                "target_index": target_index,
            }
            self.begin_written_action(entry)
            try:
                self.atomic_write_text(
                    self.action_path,
                    (
                        f"battle_action {battle_generation} {command_generation} "
                        f"{player_index} {action_type} {object_id} {target_index}\n"
                    ),
                    encoding="ascii",
                )
            except Exception:
                self.pending_written_action = None
                raise
            self.action_log.append(entry)
            self.append_navigation_diagnostic("action_written", {"action": entry})
        result = self.wait_for_battle_action_result(
            battle_generation, command_generation, start_time
        )
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or result.get("status") or "battle_action_failed"))
        return {**entry, "result": result}


class Handler(BaseHTTPRequestHandler):
    server_version = "PALPALDebug/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/state":
            self.send_json(self.server.state.snapshot())
            return
        if parsed.path == "/navigation":
            state = self.server.state.read_live_state()
            control = self.server.state.control_state(
                state,
                self.server.state.live_info("state"),
            )
            if not control["navigation_ready"]:
                self.send_json({
                    "status": "waiting_for_control",
                    "control_phase": control["phase"],
                    "targets": [],
                })
            else:
                self.send_json(self.server.state.navigation(state))
            return
        if parsed.path == "/navigation/transitions":
            state = self.server.state.read_live_state()
            self.server.state.attach_logical_room_fields(state)
            identity = self.server.state.room_identity(state)
            from_room_id = identity["id"] if identity else None
            payload = self.server.state.known_transitions_summary(from_room_id, state)
            payload["transitions"] = [
                transition
                for transition in payload.get("transitions", [])
                if transition.get("confirmation") in {"native_trigger", "human_confirmed"}
            ]
            payload["known_transitions"] = {
                transition["transition_key"]: transition
                for transition in payload["transitions"]
            }
            payload["known_transitions_by_object"] = {}
            for transition in payload["transitions"]:
                payload["known_transitions_by_object"].setdefault(
                    str(transition.get("object_id")), []
                ).append(transition)
            self.send_json(payload)
            return
        if parsed.path == "/navigation/route":
            query = parse_qs(parsed.query)
            target = int((query.get("id") or ["0"])[0])
            approach_item = int((query.get("item_id") or ["0"])[0]) or None
            state = self.server.state.read_live_state()
            control = self.server.state.control_state(
                state,
                self.server.state.live_info("state"),
            )
            if not control["navigation_ready"]:
                self.send_json({
                    "ok": True,
                    "status": "waiting_for_control",
                    "control": control,
                    "target_id": target,
                })
                return
            self.server.state.attach_logical_room_fields(state)
            route = self.server.state.route_to_object(
                state,
                target,
                avoid_transitions=True,
                approach_item_id=approach_item,
            )
            if route.get("status") == "unreachable_static":
                relaxed = self.server.state.route_to_object(
                    state,
                    target,
                    avoid_transitions=False,
                    approach_item_id=approach_item,
                )
                if relaxed.get("status") == "reachable":
                    route["status"] = "blocked_by_transition"
                    route["blocked_route"] = relaxed.get("route")
            self.send_json(route)
            return
        if parsed.path == "/exploration":
            state = self.server.state.read_live_state()
            self.server.state.attach_logical_room_fields(state)
            self.server.state.update_exploration(state)
            self.send_json(self.server.state.current_exploration(state))
            return
        if parsed.path == "/events":
            self.send_json({"events": self.server.state.event_log[-100:]})
            return
        if parsed.path == "/dialogs":
            state = self.server.state.read_live_state()
            self.server.state.attach_logical_room_fields(state)
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
        if parsed.path == "/agent/log":
            query = parse_qs(parsed.query)
            raw_limit = (query.get("limit") or ["100"])[0]
            try:
                limit = int(raw_limit)
            except ValueError:
                limit = 100
            self.send_json({"decisions": self.server.state.read_agent_log(limit=limit)})
            return
        if parsed.path == "/agent/tasks":
            self.send_json(self.server.state.agent_tasks_view())
            return
        if parsed.path == "/agent/llm-log":
            query = parse_qs(parsed.query)
            raw_limit = (query.get("limit") or ["100"])[0]
            try:
                limit = int(raw_limit)
            except ValueError:
                limit = 100
            self.send_json({"requests": self.server.state.read_agent_llm_log(limit=limit)})
            return
        if parsed.path == "/agent/session":
            self.send_json({"ok": True, "session": self.server.state.agent_runtime.snapshot()})
            return
        if parsed.path == "/agent/session/events":
            query = parse_qs(parsed.query)
            if "turn_limit" in query or "before_turn" in query:
                try:
                    turn_limit = int((query.get("turn_limit") or ["5"])[0])
                    before_raw = (query.get("before_turn") or [None])[0]
                    before_turn = int(before_raw) if before_raw is not None else None
                except ValueError:
                    self.send_json({"ok": False, "error": "before_turn and turn_limit must be integers"}, HTTPStatus.BAD_REQUEST)
                    return
                self.send_json({
                    "ok": True,
                    **self.server.state.agent_runtime.event_turn_page(before_turn, turn_limit),
                })
                return
            try:
                after = int((query.get("after") or ["0"])[0])
                limit = int((query.get("limit") or ["200"])[0])
            except ValueError:
                self.send_json({"ok": False, "error": "after and limit must be integers"}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"ok": True, "events": self.server.state.agent_runtime.events(after, limit)})
            return
        if parsed.path == "/agent/session/stream":
            self.handle_agent_events_websocket(parsed)
            return
        if parsed.path == "/checkpoints":
            self.send_json(self.server.state.list_checkpoints())
            return
        if parsed.path == "/screen.bmp":
            self.send_file(self.server.state.screen_path, "image/bmp", no_cache=True)
            return
        if parsed.path == "/frame.png":
            self.send_frame_png()
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
            if self.reject_if_agent_running():
                return
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
            if self.reject_if_agent_running():
                return
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

        if self.path == "/inventory/equip":
            if self.reject_if_agent_running():
                return
            try:
                payload = self.read_body_json()
                result = self.server.state.write_equip_item_action(
                    payload.get("item_id"),
                    payload.get("role_id"),
                )
                self.send_json({"ok": True, "action": result})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if self.path in {"/shop/buy", "/shop/leave"}:
            if self.reject_if_agent_running():
                return
            try:
                payload = self.read_body_json()
                action = "shop_buy" if self.path == "/shop/buy" else "shop_leave"
                result = self.server.state.write_shop_action(
                    action,
                    payload.get("item_id") if action == "shop_buy" else None,
                )
                self.send_json({"ok": True, "action": result})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/battle/action":
            if self.reject_if_agent_running():
                return
            try:
                result = self.server.state.write_battle_action(self.read_body_json())
                self.send_json({"ok": True, "action": result})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return

        if self.path == "/dialog/advance":
            if self.reject_if_agent_running():
                return
            try:
                payload = self.read_body_json()
                result = self.server.state.advance_dialog(payload)
                status = HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT
                self.send_json(result, status)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/dialog/advance/stop":
            if self.reject_if_agent_running():
                return
            result = self.server.state.cancel_dialog_advance()
            self.send_json(result)
            return

        if self.path == "/agent/decide":
            try:
                payload = self.read_body_json()
                state = self.server.state.snapshot()
                decision = self.server.state.decide_agent(state, model=payload.get("model"))
                self.send_json({"ok": True, "decision": decision})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/agent/session/run":
            try:
                payload = self.read_body_json()
                session = self.server.state.agent_runtime.start(
                    payload.get("model"), payload.get("max_turns", 10)
                )
                self.send_json({"ok": True, "session": session}, HTTPStatus.ACCEPTED)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return

        if self.path == "/agent/session/decide":
            try:
                payload = self.read_body_json()
                session = self.server.state.agent_runtime.start(payload.get("model"), 1, decide_only=True)
                self.send_json({"ok": True, "session": session}, HTTPStatus.ACCEPTED)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return

        if self.path == "/agent/session/interrupt":
            self.send_json({"ok": True, "session": self.server.state.agent_runtime.interrupt()})
            return

        if self.path == "/agent/session/execute":
            try:
                result = self.server.state.agent_runtime.execute()
                self.send_json({"ok": True, "execution": result, "session": self.server.state.agent_runtime.snapshot()})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return

        if self.path == "/agent/session/reset":
            try:
                session = self.server.state.agent_runtime.reset("user_reset")
                self.send_json({"ok": True, "session": session})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return

        if self.path == "/navigation/step":
            if self.reject_if_agent_running():
                return
            try:
                payload = self.read_body_json()
                target = int(payload.get("id") or 0)
                approach_item = int(payload.get("item_id") or 0) or None
                state = self.server.state.read_live_state()
                self.server.state.attach_logical_room_fields(state)
                result = self.server.state.navigation_step(
                    state,
                    target,
                    approach_item_id=approach_item,
                )
                self.server.state.append_navigation_diagnostic(
                    "navigation_step",
                    {
                        "target_id": target,
                        "approach_item_id": approach_item,
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
                state = self.server.state.read_live_state()
                self.server.state.attach_logical_room_fields(state)
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

        if self.path == "/transitions/annotate":
            try:
                payload = self.read_body_json()
                record = self.server.state.annotate_transition(
                    payload.get("transition_key"),
                    from_semantic_room=payload.get("from_semantic_room"),
                    to_semantic_room=payload.get("to_semantic_room"),
                    confidence=payload.get("confidence") or "human_confirmed",
                    notes=payload.get("notes"),
                )
                self.send_json({"ok": True, "transition": record})
            except KeyError as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/checkpoints/save":
            if self.reject_if_agent_running():
                return
            try:
                payload = self.read_body_json()
                result = self.server.state.save_checkpoint(payload.get("id"), payload.get("label"))
                self.send_json(result)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return

        if self.path == "/checkpoints/load":
            if self.reject_if_agent_running():
                return
            try:
                payload = self.read_body_json()
                result = self.server.state.load_checkpoint(str(payload.get("id") or ""))
                self.send_json(result)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return

        if self.path == "/startup/new":
            if self.reject_if_agent_running():
                return
            try:
                result = self.server.state.start_new_game()
                self.send_json(result)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
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

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/checkpoints/"):
            checkpoint_id = parsed.path.removeprefix("/checkpoints/")
            try:
                result = self.server.state.delete_checkpoint(checkpoint_id)
                self.send_json(result)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def read_body_json(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(size) if size else b"{}"
        return json.loads(raw.decode("utf-8"))

    def reject_if_agent_running(self) -> bool:
        if not self.server.state.agent_runtime.is_running():
            return False
        self.send_json(
            {"ok": False, "error": "agent_session_running", "code": "agent_session_running"},
            HTTPStatus.CONFLICT,
        )
        return True

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
        self.send_bytes(raw, content_type, no_cache=no_cache)

    def send_bytes(self, raw: bytes, content_type: str, no_cache: bool = False) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        if no_cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_frame_png(self) -> None:
        try:
            latest = self.server.state.latest_frame_png()
        except ValueError as exc:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        if latest is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        _, png = latest
        self.send_bytes(png, "image/png", no_cache=True)

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
            payload = self.server.state.state_stream_payload()
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
        last_sequence = 0
        self.connection.settimeout(1.0)
        while True:
            try:
                sequence = self.server.state.wait_for_frame(last_sequence, 0.25)
                if sequence <= last_sequence:
                    continue
                latest = self.server.state.latest_frame_png()
                if latest is None:
                    continue
                sequence, payload = latest
                self.connection.sendall(websocket_frame(payload, opcode=0x2))
                last_sequence = sequence
            except ValueError:
                last_sequence = sequence
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                return

    def handle_agent_events_websocket(self, parsed) -> None:
        query = parse_qs(parsed.query)
        try:
            after = int((query.get("after") or ["0"])[0])
        except ValueError:
            after = 0
        current_session_id = str(self.server.state.agent_runtime.snapshot().get("session_id") or "")
        session_id = str((query.get("session_id") or [current_session_id])[0] or "")
        if not self.handle_websocket_upgrade():
            return
        if not session_id or session_id != current_session_id:
            return
        self.connection.settimeout(1.0)
        while True:
            events, session_current = self.server.state.agent_runtime.wait_for_events(
                after=after,
                limit=200,
                session_id=session_id,
                timeout=0.25,
            )
            if not session_current:
                return
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                if readable:
                    preview = self.connection.recv(2, socket.MSG_PEEK)
                    if not preview or (preview[0] & 0x0F) == 0x8:
                        return
            except (OSError, ValueError):
                return
            for event in events:
                try:
                    self.connection.sendall(
                        websocket_frame(json.dumps(event, ensure_ascii=False).encode("utf-8"), opcode=0x1)
                    )
                except (BrokenPipeError, ConnectionResetError, socket.timeout):
                    return
                after = max(after, int(event.get("seq") or 0))

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")


class Server(ThreadingHTTPServer):
    state: DebugState

    def server_close(self) -> None:
        state = getattr(self, "state", None)
        if state is not None:
            state.cancel_runtime_observations("server_closed")
            state.stop_live_bridge()
        super().server_close()


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
    server.state.start_live_bridge()
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
