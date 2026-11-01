"""Failure and interleaving coverage for checkpoint transactions."""
import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from palpal.debug_server import DebugState
from tests.support import create_agent_task, fake_load_command, fake_save_command, make_debug_state, make_state, write_state


def saved_debug(tmp_path):
    debug = make_debug_state(tmp_path)
    state = dict(make_state(frame=100), game_instance_id="test-game")
    write_state(debug, state)
    debug.snapshot()
    memo = debug.run_agent_tool("upsert_room_memo", {"text": "known inn"}, state)
    assert memo["ok"]
    task = create_agent_task(debug, {"description": "Find the inn", "rationale": "Recorded dialogue"}, state)
    assert task["ok"]
    with patch.object(debug, "run_slot_command", side_effect=fake_save_command(debug)):
        debug.save_checkpoint("good")
    return debug, state


@pytest.mark.parametrize("damage", ["hash", "missing", "version", "bundle", "output", "control", "events"])
def test_preflight_failure_never_changes_live_data_or_sends_load(tmp_path, damage):
    debug, _ = saved_debug(tmp_path)
    cp = debug.checkpoint_dir("good")
    exploration = cp / "companion/exploration.json"
    if damage == "hash":
        exploration.write_text(exploration.read_text() + " ")
    elif damage == "missing":
        exploration.unlink()
    else:
        # Exercise actual schema checks in supported pre-hash v2 checkpoints.
        manifest = json.loads((cp / "manifest.json").read_text())
        manifest.update(version=2)
        manifest.pop("integrity")
        (cp / "manifest.json").write_text(json.dumps(manifest))
        if damage == "version":
            data = json.loads(exploration.read_text())
            data["version"] = 2
            exploration.write_text(json.dumps(data))
        elif damage == "bundle":
            (cp / "agent/session.json").write_text("{broken")
        elif damage == "output":
            path = cp / "agent/session.json"
            bundle = json.loads(path.read_text())
            bundle["transcript"] = [{"role": "user", "content": "tool-" + "a" * 32 + ".txt"}]
            path.write_text(json.dumps(bundle))
        else:
            path = cp / "agent/session.json"
            bundle = json.loads(path.read_text())
            if damage == "control":
                bundle["control"]["turn"] = "broken turn"
            else:
                bundle["events"] = [{"seq": "broken sequence"}]
            path.write_text(json.dumps(bundle))
    before = debug.exploration_path.read_bytes()
    session = debug.agent_runtime.snapshot()
    with patch.object(debug, "run_slot_command") as native, patch.object(debug, "checkpoint_native_control") as control:
        with pytest.raises((ValueError, FileNotFoundError)):
            debug.load_checkpoint("good")
    native.assert_not_called()
    control.assert_not_called()
    assert debug.exploration_path.read_bytes() == before
    assert debug.agent_runtime.snapshot() == session
    assert debug.checkpoint_status() is None


def test_stale_read_cannot_overwrite_loaded_companion(tmp_path):
    debug, state = saved_debug(tmp_path)
    old = debug.read_exploration()
    old["room_memos"] = {}
    old["tasks"] = {}
    with patch.object(debug, "run_slot_command", side_effect=fake_load_command):
        debug.load_checkpoint("good")
    with pytest.raises(RuntimeError, match="stale_observation"):
        debug.write_exploration(old)
    assert len(debug.read_exploration()["room_memos"]) == 1
    assert len(debug.read_exploration()["tasks"]) == 1


def test_restore_waits_for_existing_exploration_writer(tmp_path):
    debug, state = saved_debug(tmp_path)
    debug.write_exploration(debug.empty_exploration())
    reached, release, restoring = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original_write = debug.write_exploration
    original_restore = debug.restore_companion_from_checkpoint

    def gated_write(data):
        if threading.current_thread().name == "old-writer":
            reached.set()
            assert release.wait(3)
        return original_write(data)

    def restore(directory):
        restoring.set()
        return original_restore(directory)

    def invoke(call):
        try:
            call()
        except Exception as exc:
            errors.append(exc)

    with patch.object(debug, "write_exploration", side_effect=gated_write), patch.object(
        debug, "restore_companion_from_checkpoint", side_effect=restore,
    ), patch.object(debug, "run_slot_command", side_effect=fake_load_command):
        writer = threading.Thread(target=lambda: invoke(lambda: debug.update_exploration(state)), name="old-writer")
        loader = threading.Thread(target=lambda: invoke(lambda: debug.load_checkpoint("good")))
        writer.start()
        assert reached.wait(2)
        loader.start()
        assert not restoring.wait(0.1)
        release.set()
        writer.join(5)
        loader.join(5)
    assert not writer.is_alive() and not loader.is_alive()
    assert not errors
    assert restoring.is_set()
    assert len(debug.read_exploration()["room_memos"]) == 1
    assert len(debug.read_exploration()["tasks"]) == 1


def test_low_frame_during_restore_never_clears_history(tmp_path):
    debug, state = saved_debug(tmp_path)
    original_restore = debug.restore_companion_from_checkpoint

    def observe_low_frame(directory):
        result = original_restore(directory)
        write_state(debug, dict(state, frame=1))
        debug.snapshot()
        return result

    with patch.object(debug, "restore_companion_from_checkpoint", side_effect=observe_low_frame), patch.object(
        debug, "run_slot_command", side_effect=fake_load_command,
    ), patch.object(debug, "clear_runtime_history", wraps=debug.clear_runtime_history) as cleared:
        debug.load_checkpoint("good")
        snapshot = debug.snapshot()
    cleared.assert_not_called()
    assert snapshot["logical_frame"] > 100
    assert len(debug.read_exploration()["room_memos"]) == 1
    assert len(debug.read_exploration()["tasks"]) == 1


@pytest.mark.parametrize("failure", ["companion", "agent", "resume"])
def test_partial_failure_keeps_backup_and_blocks_execution_across_restart(tmp_path, failure):
    debug, state = saved_debug(tmp_path)
    debug.run_agent_tool("upsert_room_memo", {"text": "live annotation"}, state)
    live = debug.exploration_path.read_bytes()
    original_control = debug.checkpoint_native_control
    controls = []
    def control(command, **kwargs):
        controls.append(command)
        if command == "resume" and failure == "resume":
            raise TimeoutError("injected resume failure")
        return original_control(command, **kwargs)
    target = "restore_companion_from_checkpoint" if failure == "companion" else "checkpoint_native_control"
    if failure == "agent":
        context = patch.object(debug.agent_runtime, "restore_checkpoint", side_effect=OSError("injected agent failure"))
    elif failure == "companion":
        context = patch.object(debug, target, side_effect=OSError("injected companion failure"))
    else:
        context = patch.object(debug, target, side_effect=control)
    with context, patch.object(debug, "run_slot_command", side_effect=fake_load_command):
        with pytest.raises((OSError, TimeoutError)):
            debug.load_checkpoint("good")
    status = debug.checkpoint_status()
    assert status["recovery_required"]
    if failure == "resume":
        assert controls[-2:] == ["resume", "pause"]
    backup = Path(status["work_dir"]) / "backup"
    assert (backup / "game.rpg").exists()
    assert (backup / "companion/exploration.json").read_bytes() == live
    assert debug.snapshot()["checkpoint_operation"]["recovery_required"]
    with pytest.raises(RuntimeError, match="recovery_required"):
        debug.write_action(["search"], 1)
    with pytest.raises(RuntimeError, match="recovery_required"):
        debug.agent_runtime.start(None, 1)
    debug.events_path.write_text('[{"type":"preserved-event"}]')
    restarted = DebugState(debug.runtime_dir, debug.asset_dir, debug.game_binary)
    assert restarted.checkpoint_status()["recovery_required"]
    assert json.loads(restarted.events_path.read_text()) == [{"type": "preserved-event"}]
    with pytest.raises(RuntimeError, match="recovery_required"):
        restarted.agent_runtime.start(None, 1)


def test_invalid_retry_does_not_clear_existing_recovery_block(tmp_path):
    debug, _ = saved_debug(tmp_path)
    with patch.object(debug, "restore_companion_from_checkpoint", side_effect=OSError("injected")), patch.object(
        debug, "run_slot_command", side_effect=fake_load_command,
    ):
        with pytest.raises(OSError):
            debug.load_checkpoint("good")
    previous = debug.checkpoint_status()
    with pytest.raises(FileNotFoundError):
        debug.load_checkpoint("missing")
    assert debug.checkpoint_status() == previous


def test_sqlite_replacement_rolls_back_on_insert_error(tmp_path):
    debug = make_debug_state(tmp_path)
    runtime = debug.agent_runtime
    sid = runtime.snapshot()["session_id"]
    before = [{"role": "user", "content": "keep this history"}]
    runtime._submit(runtime._replace_sdk_session_items(sid, before)).result(5)
    with sqlite3.connect(runtime.sqlite_path) as connection:
        connection.execute("""CREATE TRIGGER reject_history BEFORE INSERT ON agent_messages
            WHEN NEW.message_data LIKE '%FAIL_INSERT%'
            BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END""")
    with pytest.raises(sqlite3.DatabaseError, match="injected"):
        runtime._submit(runtime._replace_sdk_session_items(sid, [{"role": "user", "content": "FAIL_INSERT"}])).result(5)
    assert runtime._submit(runtime._sdk_session_items(sid)).result(5) == before


@pytest.mark.parametrize("result_kind", ["scheduled", "other_request"])
def test_slot_wait_rejects_nonfinal_or_unrelated_success(tmp_path, result_kind):
    debug = make_debug_state(tmp_path)
    debug.slot_request_id = "a" * 32
    debug.action_result_path.write_text(json.dumps({
        "ok": True, "action": "load_slot", "slot": 99,
        "status": "load_scheduled" if result_kind == "scheduled" else "loaded",
        "request_id": "a" * 32 if result_kind == "scheduled" else "b" * 32,
    }))
    with pytest.raises(TimeoutError):
        debug.wait_for_slot_result("load_slot", 99, 0, timeout=0.04)


def test_correlated_native_failure_acknowledges_consumed_request(tmp_path):
    debug = make_debug_state(tmp_path)
    write_state(debug, make_state())
    failure = {"ok": False, "action": "save_slot", "slot": 99, "error": "save_io_error"}
    with patch.object(debug, "wait_for_slot_result", return_value=failure):
        with pytest.raises(RuntimeError, match="save_io_error"):
            debug.run_slot_command("save_slot", 99)
    assert debug.pending_written_action is None


def test_save_does_not_publish_an_old_slot_on_native_false_success(tmp_path):
    debug, _ = saved_debug(tmp_path)
    def false_success(command, slot):
        return {"result": {"ok": True, "status": "saved"}}
    with patch.object(debug, "run_slot_command", side_effect=false_success):
        with pytest.raises(RuntimeError, match="not_written"):
            debug.save_checkpoint("bad")
    assert not debug.checkpoint_dir("bad").exists()
    assert debug.checkpoint_status()["recovery_required"]


def test_reset_preserves_memory_if_automatic_archive_fails(tmp_path):
    debug, _ = saved_debug(tmp_path)
    before = debug.exploration_path.read_bytes()
    with patch.object(debug, "save_checkpoint", side_effect=OSError("disk full")):
        with pytest.raises(RuntimeError, match="archive_failed"):
            debug.reset_agent_session()
    assert debug.exploration_path.read_bytes() == before


@pytest.mark.parametrize("controller", ["dialog", "navigation"])
def test_checkpoint_refuses_active_manual_controller(tmp_path, controller):
    debug, _ = saved_debug(tmp_path)
    if controller == "dialog":
        debug.dialog_advance_lock.acquire()
    else:
        debug.spatial_execution = {"id": "manual-navigation"}
    try:
        with patch.object(debug, "run_slot_command") as native:
            with pytest.raises(RuntimeError, match="control_busy"):
                debug.load_checkpoint("good")
        native.assert_not_called()
        assert debug.checkpoint_status() is None
    finally:
        if controller == "dialog":
            debug.dialog_advance_lock.release()


def test_explicit_retry_after_native_failure_keeps_previous_backup(tmp_path):
    debug, state = saved_debug(tmp_path)
    with patch.object(debug, "run_slot_command", side_effect=RuntimeError("native_load_failed")):
        with pytest.raises(RuntimeError):
            debug.load_checkpoint("good")
    previous = debug.checkpoint_status()
    write_state(debug, dict(state, entering_scene=True, checkpoint={"paused": True, "load_failed": True}))
    with patch.object(debug, "run_slot_command", side_effect=fake_load_command):
        result = debug.load_checkpoint("good")
    assert result["ok"]
    assert debug.checkpoint_status() is None
    assert (Path(previous["work_dir"]) / "backup/companion/exploration.json").exists()


@pytest.mark.parametrize("journal", ["{broken", "[]", "{}"])
def test_corrupt_operation_journal_is_inspectable_and_blocks_execution(tmp_path, journal):
    debug = make_debug_state(tmp_path)
    debug.checkpoint_journal_path.write_text(journal)
    restarted = DebugState(debug.runtime_dir, debug.asset_dir, debug.game_binary)
    assert restarted.snapshot()["checkpoint_operation"]["recovery_required"]
    with pytest.raises(RuntimeError, match="recovery_required"):
        restarted.agent_runtime.start(None, 1)
