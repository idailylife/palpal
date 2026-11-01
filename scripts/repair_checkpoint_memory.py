"""Recover missing companion memory into new checkpoints; never load a game.

Usage: python -m scripts.repair_checkpoint_memory --baseline PATH --target PATH
The baseline must be an exact ancestor in the target's saved Agent event history.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
import shutil

from palpal.agent_runtime import AgentRuntime
from palpal.checkpoints import file_digest
from palpal.debug_server import DebugState


def validator():
    # No constructors: do not create a runtime, open sockets or change live state.
    checker = object.__new__(DebugState)
    checker.agent_runtime = object.__new__(AgentRuntime)
    return checker


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def inventory(directory):
    return {p.relative_to(directory).as_posix(): file_digest(p)
            for p in sorted(directory.rglob("*")) if p.is_file()}


def merge_history(older, newer):
    """Keep both histories, deduplicating identical records without truncation."""
    result, seen = [], set()
    for item in [*older, *newer]:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            result.append(copy.deepcopy(item))
            seen.add(key)
    return result


def overlay(older, newer):
    if isinstance(older, dict) and isinstance(newer, dict):
        merged = copy.deepcopy(older)
        for key, value in newer.items():
            merged[key] = overlay(merged[key], value) if key in merged else copy.deepcopy(value)
        return merged
    if isinstance(older, list) and isinstance(newer, list):
        return merge_history(older, newer)
    return copy.deepcopy(newer)


def merge_memory(baseline, target):
    merged = overlay(baseline, target)
    # Current task fields and lifecycle remain authoritative. Only recover its
    # historical evidence; do not overwrite current goals with old descriptions.
    for task_id, task in target.get("tasks", {}).items():
        previous = baseline.get("tasks", {}).get(task_id, {})
        current = copy.deepcopy(task)
        for key in ("history", "origin_evidence", "recent_evidence"):
            current[key] = merge_history(previous.get(key) or [], task.get(key) or [])
        merged["tasks"][task_id] = current
    for room_id, room in merged["rooms"].items():
        previous = baseline["rooms"].get(room_id, {})
        latest = target["rooms"].get(room_id)
        room["visited"] = sorted(set(previous.get("visited", [])) | set((latest or {}).get("visited", [])))
        room["frontier"] = sorted(set((latest or previous).get("frontier", [])) - set(room["visited"]))
        # Routes and failures are execution caches, not permanent room facts.
        room.pop("last_route", None)
        room["failed_nodes"] = []
        room["blocked_edges"] = []
    for room_id, records in merged.get("object_interactions", {}).items():
        for object_id, record in records.items():
            # Bounded original histories cannot prove an exact aggregate sum.
            previous = baseline.get("object_interactions", {}).get(room_id, {}).get(object_id, {})
            current = target.get("object_interactions", {}).get(room_id, {}).get(object_id, {})
            history = record.get("history", [])
            for key, kind in (("attempt_count", None), ("effect_count", "interaction_effect"),
                              ("no_effect_count", "interaction_no_effect")):
                observed = len(history) if kind is None else sum(item.get("type") == kind for item in history)
                record[key] = max(int(previous.get(key) or 0), int(current.get(key) or 0), observed)
    merged["tasks_revision"] = max(baseline.get("tasks_revision", 0), target.get("tasks_revision", 0)) + 1
    # Do not revive an old observable-state epoch or an in-flight battle.
    merged["interaction_observable_context"] = copy.deepcopy(target["interaction_observable_context"])
    merged["combat_knowledge"]["active_battles"] = {}
    return merged


def stats(data):
    return {key: len(data.get(key, {})) for key in
            ("room_memos", "rooms", "tasks", "transitions", "object_interactions", "task_inbox")}


def repair(baseline_dir, target_dir, output_root=None):
    baseline_dir, target_dir = baseline_dir.resolve(), target_dir.resolve()
    output_root = (output_root or target_dir.parent).resolve()
    destination = output_root / f"{target_dir.name}-memory-repaired"
    if destination.exists():
        raise FileExistsError(destination)
    checker = validator()
    base_manifest = checker.checkpoint_validate(baseline_dir)
    manifest = checker.checkpoint_validate(target_dir)
    base_bundle = read_json(baseline_dir / "agent/session.json")
    bundle = read_json(target_dir / "agent/session.json")
    if (baseline_dir == target_dir or base_bundle["source_session_id"] != bundle["source_session_id"]
            or bundle["events"][:len(base_bundle["events"])] != base_bundle["events"]
            or base_manifest["created_at"] > manifest["created_at"]):
        raise ValueError("baseline_is_not_a_proven_ancestor")
    base_before, target_before = inventory(baseline_dir), inventory(target_dir)
    base = checker.normalize_exploration(read_json(baseline_dir / "companion/exploration.json"))
    current = checker.normalize_exploration(read_json(target_dir / "companion/exploration.json"))
    recovered = merge_memory(base, current)
    # Respect explicit deletions on this saved branch; absence alone is not one.
    later_events = bundle["events"][len(base_bundle["events"]):]
    for event in later_events:
        detail = event.get("details") or {}
        result = detail.get("result") or {}
        if detail.get("name") == "upsert_room_memo" and result.get("ok") and result.get("operation") == "cleared":
            recovered["room_memos"].pop(result.get("room_id"), None)
        elif detail.get("name") == "upsert_room_memo" and result.get("ok") and isinstance(result.get("memo"), dict):
            recovered["room_memos"][result["room_id"]] = copy.deepcopy(result["memo"])
    recovered["room_memos"].update(copy.deepcopy(current["room_memos"]))
    recovered_ids = sorted(set(recovered["tasks"]) - set(current["tasks"]))
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    repair_id = uuid.uuid4().hex
    report = {
        "version": 1, "repair_id": repair_id, "repaired_at": stamp,
        "baseline": baseline_dir.name, "original_checkpoint": target_dir.name,
        "repaired_checkpoint": destination.name, "proven_ancestor_event_count": len(base_bundle["events"]),
        "before": stats(current), "after": stats(recovered), "restored_task_ids": recovered_ids,
        "current_task_fields_preserved": True, "game_file_preserved": True,
        "original_transcript_and_events_preserved_as_prefix": True,
        "historical_task_statuses_require_review": True,
        "cleared_caches": ["room.last_route", "room.blocked_edges", "room.failed_nodes", "combat_knowledge.active_battles"],
        "evidence_limits": [
            "Restored tasks retain their last known baseline status; current executability is unconfirmed.",
            "Historical transitions establish prior traversal, not current reachability.",
            "No new room annotations or predicted routes were inferred.",
            "All post-baseline evidence comes from the target checkpoint, not the current live runtime.",
            "Interaction aggregate counts are conservative lower bounds when original histories were truncated.",
        ],
        "baseline_inventory": base_before, "original_inventory": target_before,
    }
    note = (
        f"Checkpoint memory recovery notice (not a new game observation): {destination.name}. "
        f"Missing memory was recovered from proven ancestor {baseline_dir.name}. "
        f"There are now {len(recovered['room_memos'])} Room Memos and {len(recovered['tasks'])} tasks. "
        "The target checkpoint's current task descriptions, progress and lifecycle remain authoritative. "
        f"Historical-only restored tasks are {', '.join(recovered_ids)}. "
        "Their statuses describe the last known baseline state; review the full ledger with get_tasks before acting. "
        "Verify current task status, object state and route reachability against current evidence. "
        "Preserved old route failures were cleared as execution caches. See recovery/report.json for provenance."
    )
    bundle["transcript"].append({"role": "user", "content": note})
    seq = max((event.get("seq", 0) for event in bundle["events"]), default=0) + 1
    bundle["events"].append({
        "seq": seq, "session_id": bundle["source_session_id"], "turn": bundle["control"]["turn"],
        "type": "session", "phase": "memory_repaired", "time": stamp, "elapsed": None,
        "summary": f"Checkpoint companion memory repaired from {baseline_dir.name}; historical tasks require review.",
        "details": {"repair_id": repair_id, "report": "recovery/report.json", "before": report["before"], "after": report["after"]},
    })
    bundle["control"].update(task_reconciliation_pending=True, task_reconciliation_reason="checkpoint_memory_repaired")
    output_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="palpal-memory-repair-", dir=output_root.parent) as work:
        stage = Path(work) / "checkpoint"
        shutil.copytree(target_dir, stage)
        def write(relative, data):
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write("companion/exploration.json", recovered)
        write("agent/session.json", bundle)
        write("recovery/report.json", report)
        manifest.update(version=3, id=destination.name, label=f"{manifest.get('label') or target_dir.name}（记忆修复）", created_at=stamp)
        manifest["memory_repair"] = {key: report[key] for key in
                                     ("repair_id", "baseline", "original_checkpoint", "repaired_at", "before", "after")}
        manifest["agent_bundle"].update(transcript_items=len(bundle["transcript"]), events=len(bundle["events"]))
        manifest["integrity"] = {key: value for key, value in inventory(stage).items() if key != "manifest.json"}
        write("manifest.json", manifest)
        checker.checkpoint_validate(stage)
        assert file_digest(stage / "game.rpg") == target_before["game.rpg"]
        changed = {"manifest.json", "companion/exploration.json", "agent/session.json"}
        assert all(file_digest(stage / key) == value for key, value in target_before.items() if key not in changed)
        assert inventory(baseline_dir) == base_before
        assert inventory(target_dir) == target_before
        os.replace(stage, destination)
    return {key: report[key] for key in ("repaired_checkpoint", "before", "after", "restored_task_ids")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--target", type=Path, action="append", required=True)
    parser.add_argument("--output-root", type=Path)
    options = parser.parse_args()
    for target in options.target:
        print(json.dumps(repair(options.baseline, target, options.output_root), ensure_ascii=False), flush=True)
