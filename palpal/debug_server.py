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
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


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


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "waiting_for_game", "message": "state.json not written yet"}
    except json.JSONDecodeError as exc:
        return {"status": "state_parse_error", "error": str(exc)}


def websocket_frame(payload: bytes) -> bytes:
    header = bytearray([0x81])
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

    @property
    def state_path(self) -> Path:
        return self.runtime_dir / "state.json"

    @property
    def screen_path(self) -> Path:
        return self.runtime_dir / "screen.bmp"

    @property
    def action_path(self) -> Path:
        return self.runtime_dir / "action.txt"

    def snapshot(self) -> dict:
        state = read_json(self.state_path)
        state["_debug"] = {
            "runtime_dir": str(self.runtime_dir),
            "asset_dir": str(self.asset_dir),
            "game_binary": str(self.game_binary),
            "screen_available": self.screen_path.exists(),
            "recent_actions": self.action_log[-20:],
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
        return entry


class Handler(BaseHTTPRequestHandler):
    server_version = "PALPALDebug/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/state":
            self.send_json(self.server.state.snapshot())
            return
        if parsed.path == "/screen.bmp":
            self.send_file(self.server.state.screen_path, "image/bmp", no_cache=True)
            return
        if parsed.path == "/stream":
            self.handle_websocket()
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

    def handle_websocket(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(HTTPStatus.BAD_REQUEST, "missing Sec-WebSocket-Key")
            return
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        last = None
        self.connection.settimeout(1.0)
        while True:
            payload = json.dumps(self.server.state.snapshot(), ensure_ascii=False).encode("utf-8")
            if payload != last:
                try:
                    self.connection.sendall(websocket_frame(payload))
                except (BrokenPipeError, ConnectionResetError, socket.timeout):
                    return
                last = payload
            time.sleep(0.25)

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

