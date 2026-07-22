from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable


GOAL = "通关游戏"
RUNNING_PHASES = {"running", "interrupting"}
EXECUTABLE_TOOLS = {"press_buttons", "advance_dialog", "navigation_step", "use_item"}
CHECKPOINT_BUNDLE_VERSION = 2
SUPPORTED_CHECKPOINT_BUNDLE_VERSIONS = {1, CHECKPOINT_BUNDLE_VERSION}
DEFAULT_COMPACT_AT_TOKENS = 128_000
TOOL_OUTPUT_FULL_TOKENS = 8_000
TOOL_OUTPUT_PRESSURE_TOKENS = 96_000
TOOL_OUTPUT_PRESSURE_TARGET_TOKENS = 80_000
TOOL_OUTPUT_RECENT_FULL = 6
TOOL_OUTPUT_PAGE_CHARS = 6_000
TOOL_OUTPUT_REF_RE = re.compile(r"tool-[0-9a-f]{32}\.txt")


class ReplanRequired(RuntimeError):
    """A safe, transient state change invalidated a recommendation."""


def _now() -> float:
    return time.time()


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class AgentRuntime:
    """Persistent, single-controller agent loop for the synchronous debug server.

    The runtime owns a dedicated asyncio loop. Model work can use the Agents SDK while
    game execution remains serialized through DebugState's existing file bridge.
    """

    def __init__(self, debug_state: Any, runtime_dir: Path):
        self.debug = debug_state
        self.root = runtime_dir / "agent"
        self.root.mkdir(parents=True, exist_ok=True)
        self.control_path = self.root / "control.json"
        self.events_path = self.root / "events.jsonl"
        self.sqlite_path = self.root / "session.sqlite3"
        self.tool_outputs_root = self.root / "tool_outputs"
        self.tool_outputs_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.run_future: Future | None = None
        self.active_stream: Any = None
        self.generation = 0
        # Tests may replace this with a sync or async callable(state, model, feedback).
        self.decision_provider: Callable[..., Any] | None = None
        self.state = self._load_or_create()

    def _blank(
        self,
        reason: str = "created",
        *,
        game_instance_id: str | None = None,
        parent_session_id: str | None = None,
        checkpoint_id: str | None = None,
        restored_from_session_id: str | None = None,
    ) -> dict:
        now = _stamp()
        return {
            "session_id": uuid.uuid4().hex,
            "game_instance_id": game_instance_id,
            "parent_session_id": parent_session_id,
            "checkpoint_id": checkpoint_id,
            "restored_from_session_id": restored_from_session_id,
            "goal": GOAL,
            "model": self.debug.deepseek_model(),
            "status": "paused",
            "phase": "idle",
            "turn": 0,
            "run_turn": 0,
            "max_turns": 10,
            "pending_decision": None,
            "last_execution": None,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "last_request_usage": {},
            "context_estimate_tokens": 0,
            "compactions": 0,
            "stop_reason": reason,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "ended_at": now,
            "last_event_seq": self._last_event_seq(),
            "generation": 0,
            "pending_game_instance_id": None,
        }

    def _load_or_create(self) -> dict:
        try:
            value = json.loads(self.control_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("control state is not an object")
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            value = self._blank()
            self._persist(value)
            return value
        if value.get("status") in RUNNING_PHASES:
            value.update(status="paused", phase="paused", stop_reason="server_restarted", ended_at=_stamp())
            self._persist(value)
            self._event_for(value, "pause", "paused", "Server restarted; automatic play did not resume.")
        value.setdefault("last_request_usage", {})
        value.setdefault("context_estimate_tokens", 0)
        value.setdefault("game_instance_id", None)
        value.setdefault("parent_session_id", None)
        value.setdefault("checkpoint_id", None)
        value.setdefault("restored_from_session_id", None)
        value.setdefault("pending_game_instance_id", None)
        return value

    def _last_event_seq(self) -> int:
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
            return int(json.loads(lines[-1]).get("seq") or 0) if lines else 0
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return 0

    def _atomic_json(self, path: Path, value: dict) -> None:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _atomic_text(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(value, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _valid_output_ref(output_ref: Any) -> bool:
        return isinstance(output_ref, str) and TOOL_OUTPUT_REF_RE.fullmatch(output_ref) is not None

    def _session_tool_outputs_dir(self, session_id: str | None = None) -> Path:
        wanted = str(session_id or self.state.get("session_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", wanted):
            raise ValueError("invalid session id")
        return self.tool_outputs_root / wanted

    def _archive_tool_output(self, raw_output: str) -> str:
        output_ref = f"tool-{uuid.uuid4().hex}.txt"
        self._atomic_text(self._session_tool_outputs_dir() / output_ref, raw_output)
        return output_ref

    def get_tool_output(self, output_ref: Any, offset: Any = 0) -> dict:
        if not self._valid_output_ref(output_ref):
            return {"ok": False, "error": "invalid_output_ref"}
        try:
            offset = int(offset or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_offset"}
        if offset < 0:
            return {"ok": False, "error": "invalid_offset"}
        path = self._session_tool_outputs_dir() / output_ref
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"ok": False, "error": "tool_output_not_found", "output_ref": output_ref}
        except (OSError, UnicodeError):
            return {"ok": False, "error": "tool_output_unreadable", "output_ref": output_ref}
        total_chars = len(content)
        start = min(offset, total_chars)
        end = min(total_chars, start + TOOL_OUTPUT_PAGE_CHARS)
        complete = end >= total_chars
        return {
            "ok": True,
            "output_ref": output_ref,
            "offset": start,
            "content": content[start:end],
            "next_offset": None if complete else end,
            "total_chars": total_chars,
            "complete": complete,
        }

    def _persist(self, value: dict | None = None) -> None:
        value = value or self.state
        value["updated_at"] = _stamp()
        self._atomic_json(self.control_path, value)

    def snapshot(self) -> dict:
        with self.lock:
            # API keys, prompts, and raw requests are never stored in this object.
            return json.loads(json.dumps(self.state, ensure_ascii=False))

    def compact_snapshot(self) -> dict:
        state = self.snapshot()
        return {key: state.get(key) for key in (
            "session_id", "goal", "model", "status", "phase", "turn", "run_turn",
            "max_turns", "stop_reason", "usage", "compactions", "last_event_seq",
            "last_request_usage", "context_estimate_tokens", "game_instance_id",
            "parent_session_id", "checkpoint_id", "restored_from_session_id",
        )}

    def _event_for(
        self,
        state: dict,
        event_type: str,
        phase: str,
        summary: str,
        details: dict | None = None,
        *,
        started: float | None = None,
    ) -> dict:
        with self.lock:
            seq = max(int(state.get("last_event_seq") or 0), self._last_event_seq()) + 1
            event = {
                "seq": seq,
                "session_id": state.get("session_id"),
                "turn": int(state.get("turn") or 0),
                "type": event_type,
                "phase": phase,
                "time": _stamp(),
                "elapsed": round(max(0.0, _now() - started), 3) if started else None,
                "summary": str(summary)[:1000],
                "details": self._safe_details(details or {}),
            }
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            state["last_event_seq"] = seq
            if state is getattr(self, "state", None):
                self._persist()
            return event

    @classmethod
    def _safe_details(cls, value: Any, depth: int = 0) -> Any:
        if depth > 6:
            return "[truncated]"
        if isinstance(value, dict):
            result = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 100:
                    result["_truncated"] = True
                    break
                normalized = str(key).lower().replace("-", "_")
                if normalized in {"api_key", "authorization", "system_prompt", "raw_request"}:
                    result[str(key)] = "[redacted]"
                else:
                    result[str(key)] = cls._safe_details(item, depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            return [cls._safe_details(item, depth + 1) for item in value[:100]]
        if isinstance(value, str):
            return value if len(value) <= 8000 else value[:8000] + "…[truncated]"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:1000]

    def emit(self, event_type: str, phase: str, summary: str, details: dict | None = None, *, started: float | None = None) -> dict:
        return self._event_for(self.state, event_type, phase, summary, details, started=started)

    def events(self, after: int = 0, limit: int = 200, session_id: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        wanted = session_id or self.state.get("session_id")
        result: list[dict] = []
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("session_id") == wanted and int(event.get("seq") or 0) > int(after):
                result.append(event)
        return result[:limit]

    def event_turn_page(
        self,
        before_turn: int | None = None,
        turn_limit: int = 5,
        session_id: str | None = None,
    ) -> dict:
        """Return complete timeline turn groups, newest page first by cursor."""
        turn_limit = max(1, min(int(turn_limit), 50))
        wanted = session_id or self.state.get("session_id")
        events = self._all_session_events(str(wanted or ""))
        turns: dict[int, list[dict]] = {}
        normalized_events: list[tuple[int, dict]] = []
        for event in events:
            try:
                turn = max(0, int(event.get("turn") or 0))
            except (TypeError, ValueError):
                turn = 0
            turns.setdefault(turn, []).append(event)
            normalized_events.append((turn, event))

        numbered_turns = sorted(turn for turn in turns if turn > 0)
        candidates = numbered_turns
        if before_turn is not None:
            candidates = [turn for turn in candidates if turn < int(before_turn)]
        selected_turns = candidates[-turn_limit:]
        reached_oldest_turn = not numbered_turns or (
            bool(selected_turns) and selected_turns[0] == numbered_turns[0]
        )
        include_session = 0 in turns and reached_oldest_turn
        selected = set(selected_turns)
        page_events = [
            event for turn, event in normalized_events
            if turn in selected or (include_session and turn == 0)
        ]
        has_more = bool(
            (selected_turns and any(turn < selected_turns[0] for turn in numbered_turns))
            or (0 in turns and not include_session)
        )
        return {
            "session_id": wanted,
            "events": page_events,
            "has_more": has_more,
            "oldest_turn": selected_turns[0] if selected_turns else None,
            "turn_limit": turn_limit,
        }

    def _all_session_events(self, session_id: str) -> list[dict]:
        result: list[dict] = []
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return result
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("session_id") == session_id:
                result.append(event)
        return result

    async def _sdk_session_items(self, session_id: str) -> list[dict]:
        try:
            from agents import SQLiteSession
        except ImportError:
            return []
        session = SQLiteSession(session_id, self.sqlite_path)
        return list(await session.get_items())

    async def _replace_sdk_session_items(self, session_id: str, items: list[dict]) -> None:
        try:
            from agents import SQLiteSession
        except ImportError:
            if items:
                raise RuntimeError("openai-agents is required to restore checkpoint transcript")
            return
        session = SQLiteSession(session_id, self.sqlite_path)
        await session.clear_session()
        if items:
            await session.add_items(items)

    def _checkpoint_tool_outputs(self, checkpoint_dir: Path, session_id: str) -> list[dict]:
        source_dir = self._session_tool_outputs_dir(session_id)
        if not source_dir.is_dir():
            return []
        target_dir = checkpoint_dir / "agent" / "tool_outputs"
        archived: list[dict] = []
        for source in sorted(source_dir.iterdir(), key=lambda item: item.name):
            if source.is_symlink() or not source.is_file() or not self._valid_output_ref(source.name):
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / source.name
            tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                shutil.copyfile(source, tmp)
                os.replace(tmp, target)
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
            archived.append({"output_ref": source.name, "bytes": source.stat().st_size})
        return archived

    def _restore_checkpoint_tool_outputs(
        self,
        checkpoint_dir: Path,
        entries: list[dict],
        session_id: str,
    ) -> None:
        if not entries:
            return
        source_dir = checkpoint_dir / "agent" / "tool_outputs"
        target_dir = self._session_tool_outputs_dir(session_id)
        for entry in entries:
            output_ref = entry.get("output_ref") if isinstance(entry, dict) else None
            if not self._valid_output_ref(output_ref):
                raise ValueError("invalid checkpoint tool output reference")
            source = source_dir / output_ref
            if not source.is_file():
                raise ValueError("checkpoint tool output is missing")
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / output_ref
            tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                shutil.copyfile(source, tmp)
                os.replace(tmp, target)
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass

    def export_checkpoint(self, checkpoint_dir: Path) -> dict:
        with self.lock:
            if self.is_running():
                raise RuntimeError("agent_session_running")
            control = self.snapshot()
        transcript = self._submit(self._sdk_session_items(control["session_id"])).result(timeout=30)
        events = self._all_session_events(control["session_id"])
        tool_outputs = self._checkpoint_tool_outputs(checkpoint_dir, control["session_id"])
        bundle = {
            "version": CHECKPOINT_BUNDLE_VERSION,
            "saved_at": _stamp(),
            "source_session_id": control["session_id"],
            "control": control,
            "transcript": transcript,
            "events": events,
            "tool_outputs": tool_outputs,
        }
        path = checkpoint_dir / "agent" / "session.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_json(path, bundle)
        return {
            "version": CHECKPOINT_BUNDLE_VERSION,
            "source_session_id": control["session_id"],
            "turn": int(control.get("turn") or 0),
            "transcript_items": len(transcript),
            "events": len(events),
            "tool_outputs": len(tool_outputs),
        }

    def _append_restored_events(self, events: list[dict], session_id: str) -> None:
        with self.lock:
            seq = self._last_event_seq()
            with self.events_path.open("a", encoding="utf-8") as handle:
                for source in events:
                    if not isinstance(source, dict):
                        continue
                    seq += 1
                    event = {
                        "seq": seq,
                        "session_id": session_id,
                        "turn": int(source.get("turn") or 0),
                        "type": str(source.get("type") or "event"),
                        "phase": str(source.get("phase") or "restored"),
                        "time": source.get("time") or _stamp(),
                        "elapsed": source.get("elapsed"),
                        "summary": str(source.get("summary") or "Restored activity.")[:1000],
                        "details": self._safe_details(source.get("details") or {}),
                    }
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.state["last_event_seq"] = seq
            self._persist()

    def restore_checkpoint(self, checkpoint_dir: Path, checkpoint_id: str, game_instance_id: str | None) -> dict:
        path = checkpoint_dir / "agent" / "session.json"
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(bundle, dict) or int(bundle.get("version") or 0) not in SUPPORTED_CHECKPOINT_BUNDLE_VERSIONS:
                raise ValueError("unsupported agent checkpoint bundle")
            control = bundle.get("control")
            transcript = bundle.get("transcript")
            events = bundle.get("events")
            tool_outputs = bundle.get("tool_outputs", [])
            if (
                not isinstance(control, dict)
                or not isinstance(transcript, list)
                or not isinstance(events, list)
                or not isinstance(tool_outputs, list)
            ):
                raise ValueError("invalid agent checkpoint bundle")
            checkpoint_outputs_dir = checkpoint_dir / "agent" / "tool_outputs"
            for entry in tool_outputs:
                output_ref = entry.get("output_ref") if isinstance(entry, dict) else None
                source = checkpoint_outputs_dir / output_ref if self._valid_output_ref(output_ref) else None
                if source is None or source.is_symlink() or not source.is_file():
                    raise ValueError("invalid agent checkpoint tool output")
        except FileNotFoundError:
            return self.reset(
                "checkpoint_loaded_without_agent_bundle",
                game_instance_id=game_instance_id,
                checkpoint_id=checkpoint_id,
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return self.reset(
                "checkpoint_agent_bundle_incompatible",
                game_instance_id=game_instance_id,
                checkpoint_id=checkpoint_id,
            )

        source_session_id = str(bundle.get("source_session_id") or control.get("session_id") or "") or None
        with self.lock:
            if self.is_running():
                raise RuntimeError("agent_session_running")
            previous_session_id = self.state.get("session_id")
            self.generation += 1
            restored = self._blank(
                "checkpoint_restored",
                game_instance_id=game_instance_id,
                parent_session_id=previous_session_id,
                checkpoint_id=checkpoint_id,
                restored_from_session_id=source_session_id,
            )
            for key in (
                "model", "turn", "max_turns", "pending_decision", "last_execution",
                "usage", "last_request_usage", "context_estimate_tokens", "compactions",
            ):
                if key in control:
                    restored[key] = control[key]
            restored.update(status="paused", phase="paused", run_turn=0, stop_reason="checkpoint_restored", generation=self.generation)
            self.state = restored
            self._persist()
        try:
            self._restore_checkpoint_tool_outputs(checkpoint_dir, tool_outputs, restored["session_id"])
            self._submit(self._replace_sdk_session_items(restored["session_id"], transcript)).result(timeout=30)
        except Exception:
            return self.reset(
                "checkpoint_agent_transcript_restore_failed",
                game_instance_id=game_instance_id,
                checkpoint_id=checkpoint_id,
                parent_session_id=previous_session_id,
                restored_from_session_id=source_session_id,
            )
        self._append_restored_events(events, restored["session_id"])
        self.emit(
            "session",
            "paused",
            f"Restored Agent branch from checkpoint {checkpoint_id}.",
            {"checkpoint_id": checkpoint_id, "source_session_id": source_session_id},
        )
        return self.snapshot()

    def _ensure_loop(self) -> None:
        with self.lock:
            if self.loop and self.thread and self.thread.is_alive():
                return
            ready = threading.Event()

            def run_loop() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self.loop = loop
                ready.set()
                loop.run_forever()

            self.thread = threading.Thread(target=run_loop, name="palpal-agent-runtime", daemon=True)
            self.thread.start()
        ready.wait(2.0)

    def _submit(self, coroutine: Any) -> Future:
        self._ensure_loop()
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def is_running(self) -> bool:
        return self.state.get("status") in RUNNING_PHASES

    def reset(
        self,
        reason: str = "reset",
        *,
        game_instance_id: str | None = None,
        parent_session_id: str | None = None,
        checkpoint_id: str | None = None,
        restored_from_session_id: str | None = None,
    ) -> dict:
        self.debug.observations.cancel_all("agent_reset", owner="navigation")
        self.debug.observations.cancel_all("agent_reset", owner="dialog_advance")
        with self.lock:
            if self.is_running():
                raise RuntimeError("agent_session_running")
            if game_instance_id is None:
                game_instance_id = self.state.get("game_instance_id")
            self.generation += 1
            self.state = self._blank(
                reason,
                game_instance_id=game_instance_id,
                parent_session_id=parent_session_id,
                checkpoint_id=checkpoint_id,
                restored_from_session_id=restored_from_session_id,
            )
            self.state["generation"] = self.generation
            self._persist()
            self.emit("session", "paused", f"New session created: {reason}.")
            return self.snapshot()

    def observe_game_instance(self, value: Any) -> None:
        instance_id = str(value or "").strip()[:128]
        if not instance_id:
            return
        with self.lock:
            current = str(self.state.get("game_instance_id") or "")
            if current == instance_id:
                return
            if not current and int(self.state.get("turn") or 0) == 0:
                self.state["game_instance_id"] = instance_id
                self._persist()
                return
            if self.is_running():
                was_running = True
                self.state["pending_game_instance_id"] = instance_id
                self.state.update(status="interrupting", phase="interrupting", stop_reason="game_instance_changed")
                self.cancel_event.set()
                self._persist()
                stream = self.active_stream
                loop = self.loop
            else:
                was_running = False
                stream = None
                loop = None
        if stream is not None and loop is not None:
            loop.call_soon_threadsafe(stream.cancel, "immediate")
        if was_running:
            self.emit("interrupt", "interrupting", "Game instance changed; discarding the previous session after cancellation.")
            return
        self.reset("game_instance_changed", game_instance_id=instance_id)

    def start(self, model: str | None, max_turns: int, *, decide_only: bool = False) -> dict:
        max_turns = max(1, min(int(max_turns), 100))
        with self.lock:
            if self.is_running():
                raise RuntimeError("agent_session_running")
            self.cancel_event.clear()
            self.generation += 1
            generation = self.generation
            self.state.update(
                model=self.debug.deepseek_model(model),
                status="running",
                phase="deciding",
                run_turn=0,
                max_turns=1 if decide_only else max_turns,
                stop_reason=None,
                started_at=_stamp(),
                ended_at=None,
                generation=generation,
            )
            self._persist()
            self.emit("run", "deciding", "Agent run started.", {"max_turns": self.state["max_turns"], "decide_only": decide_only})
            self.run_future = self._submit(self._run(generation, decide_only=decide_only))
            return self.snapshot()

    def interrupt(self) -> dict:
        with self.lock:
            if not self.is_running():
                return self.snapshot()
            self.cancel_event.set()
            self.state.update(status="interrupting", phase="interrupting", stop_reason="interrupt_requested")
            self._persist()
            stream = self.active_stream
            loop = self.loop
        self.debug.cancel_dialog_advance()
        self.debug.observations.cancel_all("agent_interrupt", owner="navigation")
        if stream is not None and loop is not None:
            loop.call_soon_threadsafe(stream.cancel, "immediate")
        self.emit("interrupt", "interrupting", "Immediate interrupt requested.")
        return self.snapshot()

    async def execute_pending(self) -> dict:
        with self.lock:
            if self.is_running():
                raise RuntimeError("agent_session_running")
            decision = self.state.get("pending_decision")
            if not isinstance(decision, dict):
                raise RuntimeError("no_pending_decision")
            self.state.update(status="running", phase="executing", stop_reason=None)
            self._persist()
        try:
            result = await asyncio.to_thread(self._execute_decision, decision)
            with self.lock:
                self.state.update(last_execution=result, pending_decision=None)
            self._pause("manual_execute_complete")
            return result
        except Exception as exc:
            self.emit("error", "error", f"Execution failed: {exc}")
            self._pause("execution_failed")
            raise

    def execute(self) -> dict:
        future = self._submit(self.execute_pending())
        return future.result(timeout=300)

    def _pause(
        self,
        reason: str,
        *,
        details: dict | None = None,
        summary: str | None = None,
    ) -> None:
        with self.lock:
            self.state.update(status="paused", phase="paused", stop_reason=reason, ended_at=_stamp())
            self._persist()
        self.emit("pause", "paused", summary or f"Agent paused: {reason}.", details)
        with self.lock:
            pending_instance = self.state.get("pending_game_instance_id")
            if pending_instance:
                self.state["pending_game_instance_id"] = None
                self._persist()
        if pending_instance:
            self.reset("game_instance_changed", game_instance_id=str(pending_instance))

    async def _run(self, generation: int, *, decide_only: bool) -> None:
        feedback = "Start or continue pursuing the permanent goal. Inspect the latest stable game state."
        replans = 0
        try:
            while int(self.state.get("run_turn") or 0) < int(self.state.get("max_turns") or 1):
                if self.cancel_event.is_set() or generation != self.generation:
                    self._pause("interrupted")
                    return
                before = await asyncio.to_thread(self.debug.snapshot)
                if self._unsafe_state_reason(before):
                    self._pause(self._unsafe_state_reason(before) or "unsafe_state")
                    return
                started = _now()
                self.state["phase"] = "deciding"
                self._persist()
                self.emit("model", "request", "Requesting the next recommendation.", {"model": self.state.get("model")})
                decision = await self._decide(before, feedback)
                if self.cancel_event.is_set() or generation != self.generation:
                    self.emit("interrupt", "cancelled", "A late model result was discarded.")
                    self._pause("interrupted")
                    return
                latest = await asyncio.to_thread(self.debug.snapshot)
                validation_rejection = self._decision_validation_rejection(decision)
                if validation_rejection:
                    replans += 1
                    self._merge_usage(decision.get("usage") or {})
                    self.emit(
                        "decision",
                        "discarded",
                        "Recommendation rejected by latest-state validation.",
                        {
                            "validation": validation_rejection,
                            "recommended_action": decision.get("recommended_action"),
                        },
                        started=started,
                    )
                    if replans >= 3:
                        self._pause(
                            "replan_exhausted",
                            details={"validation": validation_rejection, "replans": replans},
                            summary=(
                                "Agent paused: no executable recommendation after "
                                f"{replans} replans."
                            ),
                        )
                        return
                    feedback = self._validation_replan_feedback(validation_rejection, latest)
                    continue
                replan_reason = self._decision_replan_reason(decision, latest)
                if replan_reason:
                    replans += 1
                    self._merge_usage(decision.get("usage") or {})
                    self.emit(
                        "decision", "discarded", "Recommendation became stale before execution.",
                        {"reason": replan_reason, "recommended_action": decision.get("recommended_action")},
                        started=started,
                    )
                    if replans >= 3:
                        self._pause("state_changing")
                        return
                    feedback = self._replan_feedback(replan_reason, latest)
                    continue
                await asyncio.to_thread(self.debug.append_agent_decision, latest, decision)
                with self.lock:
                    self.state["turn"] = int(self.state.get("turn") or 0) + 1
                    self.state["run_turn"] = int(self.state.get("run_turn") or 0) + 1
                    self.state["pending_decision"] = decision
                    self._merge_usage(decision.get("usage") or {})
                    self._persist()
                self.emit("decision", "ready", decision.get("summary") or "Recommendation ready.", self._decision_event_details(decision), started=started)
                if decide_only:
                    self._pause("awaiting_manual_execute")
                    return
                action = decision.get("recommended_action") or {}
                if action.get("tool") in {None, "no_op"} or decision.get("recommended_intent") == "wait_runtime":
                    wait_reason = (action.get("args") or {}).get("reason") or "agent_wait"
                    self._pause(
                        "agent_wait",
                        details={"reason": wait_reason, "recommended_action": action},
                        summary="Agent paused: agent requested wait.",
                    )
                    return
                self.state["phase"] = "executing"
                self._persist()
                try:
                    result = await asyncio.to_thread(self._execute_decision, decision)
                except ReplanRequired as exc:
                    replans += 1
                    with self.lock:
                        self.state["turn"] = max(0, int(self.state.get("turn") or 0) - 1)
                        self.state["run_turn"] = max(0, int(self.state.get("run_turn") or 0) - 1)
                        self.state["pending_decision"] = None
                        self._persist()
                    self.emit("decision", "discarded", "Recommendation became stale before execution.", {"reason": str(exc)})
                    if replans >= 3:
                        self._pause("state_changing")
                        return
                    feedback = self._replan_feedback(str(exc), await asyncio.to_thread(self.debug.snapshot))
                    continue
                except Exception as exc:
                    self.emit("error", "error", f"Execution failed: {exc}")
                    self._pause("execution_failed")
                    return
                with self.lock:
                    self.state.update(last_execution=result, pending_decision=None)
                    self._persist()
                replans = 0
                feedback = "Execution result and stable state delta:\n" + json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            self._pause("turn_limit")
        except asyncio.CancelledError:
            self._pause("interrupted")
        except Exception as exc:
            self.emit("error", "error", f"Agent run failed: {exc}")
            self._pause("error")
        finally:
            self.active_stream = None

    def _unsafe_state_reason(self, state: dict) -> str | None:
        if state.get("status") in {"waiting_for_game", "state_parse_error"}:
            return str(state.get("status"))
        debug = state.get("_debug") or {}
        if debug.get("runtime_stale"):
            return "runtime_stale"
        if state.get("in_battle"):
            return "battle_unsupported"
        if debug.get("action_pending"):
            return "action_pending"
        if state.get("in_main_game") is False:
            return "not_in_main_game"
        return None

    async def _decide(self, state: dict, feedback: str) -> dict:
        if self.decision_provider:
            value = self.decision_provider(state, self.state.get("model"), feedback)
            if asyncio.iscoroutine(value):
                value = await value
            return value
        if not self.debug.config_value("DEEPSEEK_API_KEY"):
            return await asyncio.to_thread(self.debug.decide_agent, state, self.state.get("model"))
        return await self._sdk_decide(state, feedback)

    async def _sdk_decide(self, state: dict, feedback: str) -> dict:
        """Use the Agents SDK's streamed Chat Completions runner and SQLiteSession."""
        try:
            from agents import (Agent, AsyncOpenAI, FunctionTool, ModelSettings,
                                OpenAIChatCompletionsModel, Runner, SQLiteSession,
                                set_tracing_disabled)
            from agents.agent import StopAtTools
        except ImportError as exc:
            raise RuntimeError("openai-agents is required for persistent DeepSeek sessions") from exc

        set_tracing_disabled(disabled=True)
        runtime = self
        submitted_decision: dict | None = None

        def make_tool(spec: dict) -> Any:
            function = spec["function"]
            name = function["name"]

            async def invoke(_context: Any, raw_arguments: str) -> str:
                nonlocal submitted_decision
                arguments = runtime._parse_tool_arguments(raw_arguments)
                started = _now()
                result = runtime.debug.run_agent_tool(name, arguments, runtime.debug.snapshot())
                runtime.emit(
                    "decision" if name == "submit_decision" else "tool",
                    "submitted" if name == "submit_decision" else "completed",
                    "Recommendation submitted." if name == "submit_decision" else f"Query tool {name} completed.",
                    {"name": name, "arguments": arguments, "result": result},
                    started=started,
                )
                if name == "submit_decision":
                    if not result.get("ok"):
                        raise ValueError(json.dumps(result, ensure_ascii=False))
                    submitted_decision = result["decision"]
                    return json.dumps({"ok": True, "accepted": True}, ensure_ascii=False)
                return json.dumps(result, ensure_ascii=False)

            return FunctionTool(
                name=name,
                description=function.get("description") or name,
                params_json_schema=function.get("parameters") or {"type": "object", "properties": {}},
                on_invoke_tool=invoke,
                strict_json_schema=False,
            )

        tools = [make_tool(spec) for spec in self.debug.agent_tool_specs()]

        api_key = self.debug.config_value("DEEPSEEK_API_KEY")
        base_url = (self.debug.config_value("DEEPSEEK_BASE_URL", "https://api.deepseek.com") or "https://api.deepseek.com").rstrip("/")
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        model = OpenAIChatCompletionsModel(
            model=self.state.get("model"),
            openai_client=client,
            buffer_streamed_tool_calls=True,
        )
        instructions = self.debug.agent_system_prompt() + "\n\n" + self.debug.agent_policy_prompt() + """

Use the available read-only query tools as needed. End every decision by calling
submit_decision. Never execute game controls yourself. The permanent objective is to
finish the game.
"""
        agent = Agent(
            name="PALPAL persistent player",
            instructions=instructions,
            model=model,
            tools=tools,
            tool_use_behavior=StopAtTools(stop_at_tool_names=["submit_decision"]),
            model_settings=ModelSettings(temperature=0.2, include_usage=True),
        )
        session = SQLiteSession(self.state["session_id"], self.sqlite_path)
        prompt = json.dumps({
            "feedback": feedback,
            "context": self.debug.agent_tool_initial_context(state),
        }, ensure_ascii=False, separators=(",", ":"))
        await self._maybe_compact_sdk(session, client, prompt)
        stream = Runner.run_streamed(agent, prompt, session=session, max_turns=8)
        self.active_stream = stream
        async for event in stream.stream_events():
            if self.cancel_event.is_set():
                stream.cancel("immediate")
            event_type = getattr(event, "type", "")
            name = getattr(event, "name", "")
            if event_type == "run_item_stream_event" and name in {"tool_called", "tool_output"}:
                self.emit("model", name, name.replace("_", " ").title())
        output = submitted_decision if submitted_decision is not None else stream.final_output
        if isinstance(output, str):
            output = json.loads(output)
        if not isinstance(output, dict):
            raise RuntimeError("submit_decision did not return a decision object")
        output.update(backend="deepseek", model=self.state.get("model"))
        usage = getattr(getattr(stream, "context_wrapper", None), "usage", None)
        if usage:
            request_usage = {
                "prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
                "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            }
            output["usage"] = request_usage
            self.state["last_request_usage"] = request_usage
        await self._sanitize_sdk_session(session)
        output = self.debug.attach_decision_context(state, output)
        return output

    async def _maybe_compact_sdk(self, session: Any, client: Any, next_prompt: str) -> None:
        await self._sanitize_sdk_session(session, next_prompt=next_prompt)
        threshold = self._compact_at_tokens()
        items = await session.get_items()
        fixed_overhead = self.debug.agent_system_prompt() + self.debug.agent_policy_prompt() + json.dumps(self.debug.agent_tool_specs(), ensure_ascii=False)
        estimate = self._estimate_context_tokens(items, next_prompt + fixed_overhead)
        self.state["context_estimate_tokens"] = estimate
        self._persist()
        if estimate < max(1000, threshold):
            return
        if len(items) < 6:
            return
        if self.cancel_event.is_set():
            raise RuntimeError("interrupted")
        self.emit("compression", "starting", "Context compression started.", {"estimated_tokens": estimate, "items": len(items)})
        recent_start = self._recent_cycle_start(items, cycles=2)
        if recent_start <= 0:
            return
        transcript = json.dumps(items[:recent_start], ensure_ascii=False)
        response = await client.chat.completions.create(
            model=self.state.get("model"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize this PALPAL agent transcript compactly. Preserve the permanent goal, "
                        "tasks, plot evidence, decisions and execution results, known routes, unresolved "
                        "risks, and latest stable state. Mark object semantics as inference or annotation."
                    ),
                },
                {"role": "user", "content": transcript},
            ],
            temperature=0.1,
        )
        summary = response.choices[0].message.content
        if not summary:
            raise RuntimeError("context summarizer returned no content")
        if self.cancel_event.is_set():
            raise RuntimeError("interrupted")
        replacement = [
            {"role": "user", "content": "Persistent context summary:\n" + summary},
            {"role": "assistant", "content": "I will preserve this context while continuing the permanent goal."},
            *items[recent_start:],
        ]
        try:
            await session.clear_session()
            await session.add_items(replacement)
        except Exception:
            # Restore the complete transcript if the atomic-style replacement fails.
            await session.clear_session()
            await session.add_items(items)
            raise
        self.state["compactions"] = int(self.state.get("compactions") or 0) + 1
        self.state["context_estimate_tokens"] = self._estimate_context_tokens(replacement, next_prompt + fixed_overhead)
        self._persist()
        self.emit("compression", "completed", "Context compression completed.", {"kept_recent_items": len(items) - recent_start, "kept_recent_cycles": 2})

    def _compact_at_tokens(self) -> int:
        raw = self.debug.config_value(
            "PALPAL_AGENT_COMPACT_AT_TOKENS",
            str(DEFAULT_COMPACT_AT_TOKENS),
        )
        try:
            threshold = int(raw or DEFAULT_COMPACT_AT_TOKENS)
        except (TypeError, ValueError):
            return DEFAULT_COMPACT_AT_TOKENS
        return threshold if threshold > 0 else DEFAULT_COMPACT_AT_TOKENS

    def _merge_usage(self, usage: dict) -> None:
        normalized: dict[str, int] = {}
        target = self.state.setdefault("usage", {})
        aliases = {"input_tokens": "prompt_tokens", "output_tokens": "completion_tokens"}
        for key, value in usage.items():
            key = aliases.get(key, key)
            if key.endswith("tokens") and isinstance(value, (int, float)):
                target[key] = int(target.get(key) or 0) + int(value)
                normalized[key] = int(value)
        if normalized:
            self.state["last_request_usage"] = normalized

    @staticmethod
    def _estimate_context_tokens(items: list[dict], next_prompt: str) -> int:
        serialized = json.dumps(items, ensure_ascii=False, separators=(",", ":")) + next_prompt
        return AgentRuntime._estimate_text_tokens(serialized)

    @staticmethod
    def _estimate_text_tokens(value: str) -> int:
        # UTF-8 bytes avoid treating CJK text as if every character were a fraction
        # of one token. Three bytes per token is intentionally dependency-free and
        # conservative for the mixed Chinese/JSON transcripts PALPAL produces.
        return max(1, (len(value.encode("utf-8")) + 2) // 3)

    @staticmethod
    def _recent_cycle_start(items: list[dict], cycles: int = 2) -> int:
        user_indices = [
            index for index, item in enumerate(items)
            if item.get("role") == "user" or (item.get("type") == "message" and item.get("role") == "user")
        ]
        if len(user_indices) < cycles:
            return max(0, len(items) - 4)
        return user_indices[-cycles]

    @staticmethod
    def _parse_tool_arguments(raw_arguments: str | None) -> dict:
        raw = (raw_arguments or "{}").strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            # One conservative repair pass for common streamed/truncated JSON endings.
            repaired = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
            repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
            repaired = re.sub(r":\s*([,}])", r":null\1", repaired)
            in_string = False
            escaped = False
            braces: list[str] = []
            for char in repaired:
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                elif char == '"':
                    in_string = True
                elif char in "{[":
                    braces.append(char)
                elif char in "}]" and braces:
                    braces.pop()
            if in_string:
                repaired += '"'
            repaired += "".join("}" if char == "{" else "]" for char in reversed(braces))
            value = json.loads(repaired)
        if not isinstance(value, dict):
            raise ValueError("tool arguments must be a JSON object")
        return value

    async def _sanitize_sdk_session(self, session: Any, next_prompt: str = "") -> None:
        """Archive oversized/old tool evidence while retaining reloadable previews."""
        items = await session.get_items()
        changed = False
        replacement: list[dict] = []
        for item in items:
            current = dict(item)
            if current.get("type") == "function_call_output":
                output = current.get("output")
                if isinstance(output, str) and self._tool_output_needs_archive(output):
                    current["output"] = self._archived_tool_output_preview(output, reason="output_limit")
                    changed = True
            replacement.append(current)

        fixed_overhead = self.debug.agent_system_prompt() + self.debug.agent_policy_prompt() + json.dumps(self.debug.agent_tool_specs(), ensure_ascii=False)
        estimate = self._estimate_context_tokens(replacement, next_prompt + fixed_overhead)
        if estimate >= TOOL_OUTPUT_PRESSURE_TOKENS:
            output_indices = [
                index for index, item in enumerate(replacement)
                if item.get("type") == "function_call_output" and isinstance(item.get("output"), str)
            ]
            protected = set(output_indices[-TOOL_OUTPUT_RECENT_FULL:])
            for index in output_indices:
                if estimate <= TOOL_OUTPUT_PRESSURE_TARGET_TOKENS:
                    break
                if index in protected:
                    continue
                output = replacement[index].get("output")
                if not isinstance(output, str) or self._is_archived_tool_preview(output):
                    continue
                replacement[index] = dict(replacement[index])
                replacement[index]["output"] = self._archived_tool_output_preview(output, reason="context_pressure")
                changed = True
                estimate = self._estimate_context_tokens(replacement, next_prompt + fixed_overhead)

        if not changed:
            self.state["context_estimate_tokens"] = estimate
            self._persist()
            return
        try:
            await session.clear_session()
            await session.add_items(replacement)
        except Exception:
            await session.clear_session()
            await session.add_items(items)
            raise
        self.state["context_estimate_tokens"] = self._estimate_context_tokens(
            replacement,
            next_prompt + fixed_overhead,
        )
        self._persist()

    def _tool_output_needs_archive(self, output: str) -> bool:
        return (
            self._estimate_text_tokens(output) > TOOL_OUTPUT_FULL_TOKENS
            and not self._is_archived_tool_preview(output)
        )

    @staticmethod
    def _is_archived_tool_preview(output: str) -> bool:
        try:
            value = json.loads(output)
        except (TypeError, json.JSONDecodeError):
            return False
        return (
            isinstance(value, dict)
            and value.get("truncated") is True
            and value.get("archived") is True
            and AgentRuntime._valid_output_ref(value.get("output_ref"))
        )

    def _archived_tool_output_preview(self, output: str, *, reason: str) -> str:
        output_ref = self._archive_tool_output(output)
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            parsed = None
        is_json = parsed is not None
        preview: dict[str, Any] = {
            "ok": parsed.get("ok", True) if isinstance(parsed, dict) else True,
            "truncated": True,
            "archived": True,
            "reason": reason,
            "format": "json" if is_json else "text",
            "output_ref": output_ref,
            "original_chars": len(output),
            "original_bytes": len(output.encode("utf-8")),
            "estimated_tokens": self._estimate_text_tokens(output),
            "head": output[:2000],
            "tail": output[-2000:] if len(output) > 2000 else "",
        }
        if isinstance(parsed, dict):
            preview["keys"] = sorted(str(key) for key in parsed.keys())[:40]
            key_fields: dict[str, Any] = {}
            for key in (
                "ok", "error", "mode", "kind", "id", "object_id", "room_id",
                "status", "total", "returned", "events_total", "history_total",
            ):
                value = parsed.get(key)
                if value is None or isinstance(value, (bool, int, float)):
                    if key in parsed:
                        key_fields[key] = value
                elif isinstance(value, str) and len(value) <= 300:
                    key_fields[key] = value
            for key, value in parsed.items():
                if isinstance(value, list):
                    key_fields.setdefault(f"{key}_count", len(value))
            if key_fields:
                preview["key_fields"] = key_fields
        return json.dumps(preview, ensure_ascii=False, separators=(",", ":"))

    def _decision_event_details(self, decision: dict) -> dict:
        return {key: decision.get(key) for key in (
            "recommended_intent", "recommended_action", "evidence", "risk", "alternatives", "tool_trace", "usage"
        ) if key in decision}

    def _context_matches(self, decision: dict, state: dict) -> bool:
        expected = decision.get("state_context") or {}
        if not expected:
            return True
        current = self.debug.state_context(state)
        for key in ("scene_id", "map_id", "room_id", "room_key", "region_key"):
            if expected.get(key) is not None and current.get(key) != expected.get(key):
                return False
        return True

    def _decision_replan_reason(self, decision: dict, state: dict) -> str | None:
        if not self._context_matches(decision, state):
            return "state_context_stale"
        action = decision.get("recommended_action") or {}
        tool = action.get("tool")
        dialog_active = bool((state.get("dialog") or {}).get("active"))
        if dialog_active and tool != "advance_dialog":
            return "in_dialog"
        if not dialog_active and tool == "advance_dialog":
            return "dialog_inactive"
        return None

    @staticmethod
    def _decision_validation_rejection(decision: dict) -> dict | None:
        validation = decision.get("validation")
        if not isinstance(validation, dict) or validation.get("status") != "rejected":
            return None
        return dict(validation)

    def _replan_feedback(self, reason: str, state: dict) -> str:
        return "Recommendation invalidated by live state; replan without executing:\n" + json.dumps({
            "reason": reason,
            "state": self._compact_state(state),
        }, ensure_ascii=False, separators=(",", ":"))

    def _validation_replan_feedback(self, validation: dict, state: dict) -> str:
        return "Recommendation rejected by server validation; inspect the latest navigation or inventory and choose a currently executable action:\n" + json.dumps({
            "validation": validation,
            "state": self._compact_state(state),
        }, ensure_ascii=False, separators=(",", ":"))

    def _execute_decision(self, decision: dict) -> dict:
        before = self.debug.snapshot()
        if self.cancel_event.is_set():
            raise RuntimeError("interrupted")
        reason = self._unsafe_state_reason(before)
        if reason and reason != "not_in_main_game":
            raise RuntimeError(reason)
        if not self._context_matches(decision, before):
            raise ReplanRequired("state_context_stale")
        action = decision.get("recommended_action") or {}
        tool = action.get("tool")
        args = action.get("args") or {}
        replan_reason = self._decision_replan_reason(decision, before)
        if replan_reason:
            raise ReplanRequired(replan_reason)
        if tool not in EXECUTABLE_TOOLS:
            raise RuntimeError(f"unsupported_action:{tool}")
        started = _now()
        self.emit("action", "validating", f"Validated {tool}.", {"tool": tool, "args": args})
        if tool == "press_buttons":
            buttons = args.get("buttons") or []
            if isinstance(buttons, str):
                buttons = [buttons]
            action_result = self.debug.write_action([str(item).lower() for item in buttons], int(args.get("frames") or 1))
            after = self._wait_action_and_stable()
        elif tool == "advance_dialog":
            action_result = self.debug.advance_dialog(args)
            if not action_result.get("ok"):
                raise RuntimeError(action_result.get("status") or "dialog_advance_failed")
            after = self._wait_state_quiet()
        elif tool == "use_item":
            action_result = self.debug.write_use_item_action(args.get("item_id"), args.get("role_id"), args.get("target"))
            after = self._wait_action_and_stable()
        else:
            action_result = self._navigate(int(args.get("id") or 0))
            after = self._wait_state_quiet()
        observation_id = action_result.get("observation_id") if isinstance(action_result, dict) else None
        observation = self.debug.observations.consume(observation_id, owner="navigation")
        if isinstance(action_result, dict) and observation is not None:
            action_result = {**action_result, "observation": observation}
        delta = self.state_delta(before, after)
        result = {"ok": True, "tool": tool, "args": args, "result": self._compact_action_result(action_result), "delta": delta, "state": self._compact_state(after)}
        self.emit("action", "completed", f"Executed {tool}.", {"result": action_result}, started=started)
        self.emit("state_diff", "observed", "Observed stable state changes.", delta)
        return result

    def _wait_action_and_stable(self, timeout: float = 10.0, quiet_window: float = 2.0) -> dict:
        deadline = time.monotonic() + timeout
        while self.debug.action_path.exists() and time.monotonic() < deadline:
            if self.cancel_event.is_set():
                raise RuntimeError("interrupted")
            time.sleep(0.04)
        if self.debug.action_path.exists():
            raise RuntimeError("action_timeout")
        return self._wait_state_quiet(timeout=max(0.1, deadline - time.monotonic()), quiet_window=quiet_window)

    def _wait_state_quiet(self, timeout: float = 10.0, quiet_window: float = 2.0) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        quiet_since = time.monotonic()
        latest = self.debug.snapshot()
        while time.monotonic() < deadline:
            if self.cancel_event.is_set():
                raise RuntimeError("interrupted")
            latest = self.debug.snapshot()
            signature = self._state_signature(latest)
            if signature == last:
                if time.monotonic() - quiet_since >= quiet_window:
                    return latest
            else:
                last = signature
                quiet_since = time.monotonic()
            time.sleep(0.1)
        raise RuntimeError("state_stability_timeout")

    def _navigate(self, target_id: int) -> dict:
        if target_id <= 0:
            raise RuntimeError("navigation_target_missing")
        progress = []
        terminal = {"arrived", "transitioned", "interacting"}
        observing = {"transition_observing"}
        for step in range(1, 241):
            if self.cancel_event.is_set():
                raise RuntimeError("interrupted")
            state = self.debug.snapshot()
            result = self.debug.navigation_step(state, target_id)
            progress.append({key: result.get(key) for key in ("status", "move", "segment_steps", "remaining_steps", "required_direction", "observation_id")})
            self.emit("navigation", "progress", f"Navigation step {step}: {result.get('status')}.", {"step": step, "target_id": target_id, "result": progress[-1]})
            status = result.get("status")
            if status == "entering_scene":
                self._wait_scene_transition()
                return {"ok": True, "status": "transitioned", "steps": step, "target_id": target_id, "progress": progress[-20:]}
            if status == "in_dialog":
                return {
                    "ok": True,
                    "status": "interrupted_by_dialog",
                    "steps": step,
                    "target_id": target_id,
                    "progress": progress[-20:],
                }
            if not result.get("ok"):
                raise RuntimeError(f"navigation_{status or 'failed'}")
            if result.get("action"):
                self._wait_action_and_stable(quiet_window=0.2)
            if status in terminal:
                terminal_result = {
                    "ok": True,
                    "status": status,
                    "steps": step,
                    "target_id": target_id,
                    "progress": progress[-20:],
                }
                if result.get("observation_id"):
                    terminal_result["observation_id"] = result["observation_id"]
                return terminal_result
            if status in observing:
                observation_id = result.get("observation_id")
                if observation_id:
                    self.debug.observations.wait(
                        observation_id,
                        timeout=0.12,
                        cancel_event=self.cancel_event,
                    )
                else:
                    time.sleep(0.12)
                continue
        raise RuntimeError("navigation_step_limit")

    def _wait_scene_transition(self, timeout: float = 10.0) -> dict:
        """Wait until SDLPAL finishes a scene transition before stable observation."""
        deadline = time.monotonic() + timeout
        latest = self.debug.snapshot()
        while latest.get("entering_scene") and time.monotonic() < deadline:
            if self.cancel_event.is_set():
                raise RuntimeError("interrupted")
            time.sleep(0.1)
            latest = self.debug.snapshot()
        if latest.get("entering_scene"):
            raise RuntimeError("navigation_transition_timeout")
        return latest

    @staticmethod
    def _state_signature(state: dict) -> tuple:
        pos = state.get("party_position") or {}
        dialog = state.get("dialog") or {}
        latest_dialog_event = dialog.get("latest_event")
        if not isinstance(latest_dialog_event, dict):
            latest_dialog_event = {}
        debug = state.get("_debug") or {}
        recent_events = debug.get("recent_events") or []
        latest_event = recent_events[-1] if recent_events and isinstance(recent_events[-1], dict) else {}
        inventory = tuple(
            (item.get("item_id"), item.get("amount"), item.get("amount_in_use"))
            for item in state.get("inventory") or [] if isinstance(item, dict)
        )
        objects = tuple(
            (item.get("id"), item.get("state"), item.get("vanish_time"), item.get("interactable"), item.get("blocking"))
            for item in state.get("objects") or [] if isinstance(item, dict)
        )
        return (
            state.get("scene_id"), state.get("map_id"), state.get("room_key"),
            pos.get("x"), pos.get("y"), state.get("party_direction"),
            bool(dialog.get("active")), dialog.get("message_id"), state.get("in_battle"),
            state.get("entering_scene"), latest_dialog_event.get("sequence"),
            latest_event.get("time"), latest_event.get("type"), latest_event.get("object_id"),
            inventory, objects,
        )

    @staticmethod
    def _compact_state(state: dict) -> dict:
        pos = state.get("party_position") or {}
        dialog = state.get("dialog") or {}
        latest = dialog.get("latest_event") or {}
        return {
            "scene_id": state.get("scene_id"), "map_id": state.get("map_id"),
            "room_key": state.get("room_key") or (state.get("room") or {}).get("key"),
            "region_key": state.get("region_key"),
            "position": {"x": pos.get("x"), "y": pos.get("y"), "direction": state.get("party_direction")},
            "dialog": {"active": bool(dialog.get("active")), "message_id": latest.get("message_id"), "event_object_id": latest.get("event_object_id"), "text": latest.get("text")},
            "in_battle": bool(state.get("in_battle")), "entering_scene": bool(state.get("entering_scene")),
        }

    @staticmethod
    def _compact_action_result(result: Any) -> Any:
        if not isinstance(result, dict):
            return result
        compact = {
            key: result.get(key)
            for key in ("ok", "status", "presses", "steps", "target_id", "frames", "buttons", "observation_id")
            if key in result
        }
        observation = result.get("observation")
        if isinstance(observation, dict):
            compact["observation"] = {
                key: observation.get(key)
                for key in ("id", "kind", "status", "reason", "result")
                if observation.get(key) is not None
            }
        dialogs = result.get("dialogs")
        if isinstance(dialogs, list):
            compact["dialogs"] = [
                {key: item.get(key) for key in ("sequence", "end_sequence", "speaker_hint", "text", "message_id", "event_object_id") if key in item}
                for item in dialogs[-12:] if isinstance(item, dict)
            ]
            compact["dialog_count"] = len(dialogs)
        return compact

    @staticmethod
    def state_delta(before: dict, after: dict) -> dict:
        def position(value: dict) -> dict:
            pos = value.get("party_position") or {}
            return {"x": pos.get("x"), "y": pos.get("y"), "direction": value.get("party_direction")}

        def inventory(value: dict) -> dict[int, int]:
            return {int(item.get("item_id") or 0): int(item.get("amount") or 0) for item in value.get("inventory") or []}

        def objects(value: dict) -> dict[int, tuple]:
            return {int(item.get("id") or 0): (item.get("state"), item.get("vanish_time"), item.get("interactable"), item.get("blocking")) for item in value.get("objects") or []}

        delta: dict[str, Any] = {}
        fields = {
            "position": (position(before), position(after)),
            "room": ({key: before.get(key) for key in ("scene_id", "map_id", "room_key", "region_key")}, {key: after.get(key) for key in ("scene_id", "map_id", "room_key", "region_key")}),
            "dialog": (AgentRuntime._compact_state(before)["dialog"], AgentRuntime._compact_state(after)["dialog"]),
        }
        for key, (old, new) in fields.items():
            if old != new:
                delta[key] = {"before": old, "after": new}
        before_inventory, after_inventory = inventory(before), inventory(after)
        inventory_changes = [
            {"item_id": item_id, "before": before_inventory.get(item_id, 0), "after": after_inventory.get(item_id, 0)}
            for item_id in sorted(set(before_inventory) | set(after_inventory))
            if before_inventory.get(item_id, 0) != after_inventory.get(item_id, 0)
        ]
        if inventory_changes:
            delta["inventory_changes"] = inventory_changes
        before_objects, after_objects = objects(before), objects(after)
        object_changes = [
            {"id": object_id, "before": before_objects.get(object_id), "after": after_objects.get(object_id)}
            for object_id in sorted(set(before_objects) | set(after_objects))
            if before_objects.get(object_id) != after_objects.get(object_id)
        ]
        if object_changes:
            delta["object_changes"] = object_changes
        before_debug, after_debug = before.get("_debug") or {}, after.get("_debug") or {}
        for source, key in (("recent_dialogs", "new_dialogs"), ("recent_events", "new_events")):
            old_items = before_debug.get(source) or []
            new_items = after_debug.get(source) or []
            old_keys = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in old_items if isinstance(item, dict)}
            additions = [item for item in new_items if isinstance(item, dict) and json.dumps(item, ensure_ascii=False, sort_keys=True) not in old_keys]
            if additions:
                delta[key] = additions[-12:]
        return delta or {"stable": True, "changes": 0}
