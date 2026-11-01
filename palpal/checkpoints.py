"""Checkpoint transactions. Native transport stays live while memory is isolated."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path


class EpochData(dict):
    """A private generation tag which is never serialized into companion data."""
    epoch: int


def memory_access(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self.checkpoint_access():
            for value in (*args, *kwargs.values()):
                if isinstance(value, EpochData) and value.epoch != self.checkpoint_epoch:
                    raise RuntimeError("checkpoint_stale_observation")
            return method(self, *args, **kwargs)
    return guarded


def file_digest(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"checkpoint_invalid_file: {path.name}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return {"bytes": size, "sha256": digest.hexdigest()}


class CheckpointSupport:
    def init_checkpoints(self):
        self.checkpoint_lock = threading.RLock()
        self.checkpoint_epoch = 0
        self.checkpoint_owner = None
        self.checkpoint_operation = None
        self.checkpoint_journal_path = self.runtime_dir / "checkpoint_operation.json"
        if self.checkpoint_journal_path.exists():
            try:
                operation = json.loads(self.checkpoint_journal_path.read_text())
            except (ValueError, OSError):
                operation = {"phase": "journal_unreadable"}
            if not isinstance(operation, dict) or not isinstance(operation.get("phase"), str):
                operation = {"phase": "journal_unreadable"}
            if operation.get("phase") != "completed":
                self.checkpoint_operation = {**operation, "recovery_required": True}

    @contextmanager
    def checkpoint_access(self):
        with self.checkpoint_lock:
            if self.checkpoint_operation and self.checkpoint_owner != threading.get_ident():
                raise RuntimeError("checkpoint_recovery_required")
            yield

    def checkpoint_status(self):
        return copy.deepcopy(self.checkpoint_operation)

    def checkpoint_phase(self, phase, **details):
        self.checkpoint_operation.update(phase=phase, **details)
        self.atomic_write_text(self.checkpoint_journal_path,
                               json.dumps(self.checkpoint_operation, ensure_ascii=False) + "\n")

    @contextmanager
    def checkpoint_transaction(self, kind, checkpoint_id=None):
        with self.checkpoint_lock:
            if self.checkpoint_operation and kind != "load":
                raise RuntimeError("checkpoint_recovery_required")
            if self.agent_runtime.is_running():
                raise RuntimeError("agent_session_running")
            if self.dialog_advance_lock.locked() or self.spatial_execution is not None:
                raise RuntimeError("checkpoint_control_busy")
            previous_operation = self.checkpoint_status()
            operation_id = uuid.uuid4().hex
            work = self.runtime_dir / "checkpoint_operations" / operation_id
            work.mkdir(parents=True)
            self.checkpoint_owner = threading.get_ident()
            if kind in {"load", "new_game"}:
                self.checkpoint_epoch += 1
            self.checkpoint_operation = {
                "id": operation_id, "kind": kind, "checkpoint_id": checkpoint_id,
                "work_dir": str(work), "recovery_required": False,
                "previous_operation": previous_operation,
            }
            try:
                self.checkpoint_phase("preflight")
                yield work
                self.checkpoint_phase("completed")
                self.checkpoint_operation = None
            except Exception as exc:
                # Preflight is read-only. After quiescing, retain every recovery
                # artifact and leave native and agent execution blocked.
                if self.checkpoint_operation.get("phase") == "preflight":
                    if previous_operation:
                        self.checkpoint_operation = previous_operation
                        self.checkpoint_phase(previous_operation["phase"])
                    else:
                        self.checkpoint_phase("completed", error=str(exc))
                        self.checkpoint_operation = None
                else:
                    # A resume may have reached native even when its reply was
                    # lost. Re-establish pause before exposing recovery_required.
                    pause_details = {}
                    try:
                        self.checkpoint_native_control("pause", timeout=1.0)
                    except Exception as pause_error:
                        pause_details["pause_error"] = str(pause_error)
                    self.checkpoint_phase(self.checkpoint_operation["phase"],
                                          recovery_required=True, error=str(exc), **pause_details)
                raise
            finally:
                self.checkpoint_owner = None

    def checkpoint_native_control(self, command, timeout=5.0, *, argument=None):
        started = time.time()
        request_id = uuid.uuid4().hex
        request = self.runtime_dir / "checkpoint_control.txt"
        result_path = self.runtime_dir / "checkpoint_control_result.json"
        suffix = f" {argument}" if argument is not None else ""
        self.atomic_write_text(request, f"{command} {request_id}{suffix}\n", encoding="utf-8")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = json.loads(result_path.read_text())
            except (OSError, ValueError):
                result = {}
            if result.get("request_id") == request_id:
                if result.get("status") != {"pause": "paused", "resume": "resumed", "validate": "validated"}[command]:
                    raise RuntimeError(str(result.get("error") or result.get("status") or "native_checkpoint_control_failed"))
                if command == "pause" and "frame" in result:
                    state = self.read_live_state()
                    if ((self.live_state_received_at or 0) < started or state.get("frame") != result["frame"]
                            or (state.get("in_main_game") and not (state.get("checkpoint") or {}).get("paused"))):
                        time.sleep(0.02)
                        continue
                return result
            time.sleep(0.02)
        raise TimeoutError(f"checkpoint_{command}_timeout")

    def checkpoint_backup(self, work):
        from palpal.debug_server import CHECKPOINT_COMPANION_FILES
        backup = work / "backup"
        backup.mkdir()
        for relative in (*CHECKPOINT_COMPANION_FILES, Path("companion_owner.json")):
            source = self.runtime_dir / relative
            if source.is_file():
                target = backup / "companion" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        # SQLite's backup API includes committed WAL pages.
        if self.agent_runtime.sqlite_path.exists():
            with sqlite3.connect(self.agent_runtime.sqlite_path) as source:
                with sqlite3.connect(backup / "session.sqlite3") as target:
                    source.backup(target)
        for source in (self.agent_runtime.control_path, self.agent_runtime.events_path):
            if source.exists():
                shutil.copy2(source, backup / source.name)
        if self.agent_runtime.tool_outputs_root.exists():
            shutil.copytree(self.agent_runtime.tool_outputs_root, backup / "tool_outputs")
        if self.save_slot_path().exists():
            shutil.copy2(self.save_slot_path(), backup / "previous-slot.rpg")
        if self.read_live_state().get("in_main_game") and not self.checkpoint_operation.get("previous_operation"):
            self.checkpoint_backup_native(backup)
        return backup

    def checkpoint_backup_native(self, backup):
        self.run_slot_command("save_slot", self.checkpoint_slot)
        shutil.copy2(self.save_slot_path(), backup / "game.rpg")

    def checkpoint_validate(self, directory):
        from palpal.debug_server import CHECKPOINT_COMPANION_FILES, EXPLORATION_VERSION
        manifest = json.loads((directory / "manifest.json").read_text())
        if not isinstance(manifest, dict) or manifest.get("version") not in {2, 3}:
            raise ValueError("checkpoint_manifest_incompatible")
        inventory = manifest.get("integrity")
        if manifest["version"] == 3 and not isinstance(inventory, dict):
            raise ValueError("checkpoint_integrity_missing")
        if inventory is not None:
            actual = {p.relative_to(directory).as_posix(): file_digest(p)
                      for p in directory.rglob("*") if p.is_file() and p != directory / "manifest.json"}
            if actual != inventory:
                raise ValueError("checkpoint_integrity_mismatch")
        if not file_digest(directory / "game.rpg")["bytes"]:
            raise ValueError("checkpoint_game_empty")
        exploration = directory / "companion/exploration.json"
        data = json.loads(exploration.read_text())
        if (not isinstance(data, dict) or data.get("version") != EXPLORATION_VERSION
                or manifest.get("companion_version") != EXPLORATION_VERSION):
            raise ValueError("checkpoint_exploration_incompatible")
        for key in ("rooms", "room_memos", "tasks", "transitions"):
            if not isinstance(data.get(key, {}), dict):
                raise ValueError(f"checkpoint_invalid_{key}")
        # Normalization must not silently discard memory from this checkpoint.
        normalized = self.normalize_exploration(copy.deepcopy(data))
        for key in ("room_memos", "tasks"):
            if normalized.get(key) != data.get(key, {}):
                raise ValueError(f"checkpoint_invalid_{key}")
        declared = manifest.get("companion", {})
        for relative in CHECKPOINT_COMPANION_FILES:
            path = directory / "companion" / relative
            if declared.get(relative.as_posix(), {}).get("available") and not path.is_file():
                raise ValueError(f"checkpoint_missing_companion: {relative}")
            if path.exists():
                file_digest(path)
                if path.suffix == ".json" and path != exploration:
                    if not isinstance(json.loads(path.read_text()), list):
                        raise ValueError(f"checkpoint_invalid_companion: {relative}")
        self.agent_runtime.validate_checkpoint(directory)
        return manifest

    def save_checkpoint(self, checkpoint_id=None, label=None):
        from palpal.debug_server import EXPLORATION_VERSION, utc_timestamp
        with self.checkpoint_transaction("save") as work:
            state = self.read_live_state()
            self.preflight_checkpoint_action(state)
            checkpoint_id = self.checkpoint_id(checkpoint_id)
            destination = self.checkpoint_dir(checkpoint_id)
            if destination.exists():
                raise FileExistsError(f"checkpoint already exists: {checkpoint_id}")
            self.checkpoint_phase("quiesce", checkpoint_id=checkpoint_id)
            self.checkpoint_native_control("pause")
            self.checkpoint_phase("save")
            state = self.read_live_state()
            before = self.exploration_file_signature(self.save_slot_path().stat()) if self.save_slot_path().exists() else None
            result = self.run_slot_command("save_slot", self.checkpoint_slot)
            if not self.save_slot_path().exists() or self.exploration_file_signature(self.save_slot_path().stat()) == before:
                raise RuntimeError("checkpoint_save_not_written")
            stage = work / "checkpoint"
            self.copy_game_save_to_checkpoint(stage, self.checkpoint_slot)
            if not self.exploration_path.exists():
                self.write_exploration(self.empty_exploration())
            companion = self.copy_companion_to_checkpoint(stage)
            bundle = self.agent_runtime.export_checkpoint(stage)
            manifest = {
                "version": 3, "id": checkpoint_id, "label": (label or "").strip()[:120],
                "created_at": utc_timestamp(), "companion_version": EXPLORATION_VERSION,
                "checkpoint_slot": self.checkpoint_slot, "state": self.checkpoint_context(state),
                "native_result": result["result"], "companion": companion, "agent_bundle": bundle,
                "integrity": {p.relative_to(stage).as_posix(): file_digest(p)
                              for p in stage.rglob("*") if p.is_file()},
            }
            self.atomic_write_text(stage / "manifest.json", json.dumps(manifest, ensure_ascii=False) + "\n")
            self.checkpoint_validate(stage)
            self.checkpoint_phase("publish")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, destination)
            self.write_companion_owner("save_checkpoint")
            self.checkpoint_phase("resume")
            self.checkpoint_native_control("resume")
            return {"ok": True, "checkpoint": self.checkpoint_manifest(destination)}

    def load_checkpoint(self, checkpoint_id):
        with self.checkpoint_transaction("load", checkpoint_id) as work:
            state = self.read_live_state()
            self.preflight_checkpoint_action(state, allow_startup_waiting=True)
            source = self.checkpoint_dir(checkpoint_id)
            # Stage and validate before changing the slot or sending any command.
            stage = work / "checkpoint"
            shutil.copytree(source, stage, symlinks=True)
            manifest = self.checkpoint_validate(stage)
            self.checkpoint_native_control("validate", argument=stage / "game.rpg")
            self.checkpoint_phase("quiesce")
            self.checkpoint_native_control("pause")
            self.checkpoint_phase("backup")
            self.checkpoint_backup(work)
            self.cancel_runtime_observations("checkpoint_restored")
            self.mark_checkpoint_frame_reset_pending(checkpoint_id, state)
            self.checkpoint_phase("native_load")
            self.copy_game_save_from_checkpoint(stage, self.checkpoint_slot)
            native = self.run_slot_command("load_slot", self.checkpoint_slot)
            self.checkpoint_phase("companion_restore")
            restored = self.restore_companion_from_checkpoint(stage)
            self.mark_checkpoint_frame_reset_pending(checkpoint_id, state)
            self.checkpoint_phase("agent_restore")
            self.agent_runtime.restore_checkpoint(stage, checkpoint_id, state.get("game_instance_id"))
            self.write_companion_owner("load_checkpoint")
            self.checkpoint_phase("verify")
            saved = json.loads((stage / "companion/exploration.json").read_text())
            actual = self.read_exploration()
            for key in ("room_memos", "tasks"):
                if actual.get(key) != saved.get(key, {}):
                    raise RuntimeError(f"checkpoint_restore_mismatch: {key}")
            self.checkpoint_phase("resume")
            self.checkpoint_native_control("resume")
            return {"ok": True, "checkpoint": {**manifest, "id": checkpoint_id},
                    "restored": restored, "native_result": native["result"]}
