import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from palpal.debug_server import DebugState


def make_state(frame=42):
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


class CheckpointTests(unittest.TestCase):
    def make_debug_state(self, tmp):
        root = Path(tmp)
        asset_dir = root / "assets"
        asset_dir.mkdir()
        return DebugState(root / "runtime", asset_dir, root / "sdlpal")

    def write_state(self, debug, state=None):
        debug.state_path.write_text(json.dumps(state or make_state()), encoding="utf-8")

    def fake_save_command(self, debug):
        def run(command, slot):
            self.assertEqual(command, "save_slot")
            debug.save_slot_path(slot).write_bytes(b"checkpoint-rpg")
            return {
                "action": {"command": command, "slot": slot},
                "result": {"ok": True, "action": command, "slot": slot, "status": "saved"},
            }

        return run

    def fake_load_command(self, command, slot):
        self.assertEqual(command, "load_slot")
        return {
            "action": {"command": command, "slot": slot},
            "result": {"ok": True, "action": command, "slot": slot, "status": "load_scheduled"},
        }

    def test_startup_waiting_rejects_save_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            self.write_state(debug, make_startup_state())

            with self.assertRaisesRegex(RuntimeError, "startup_waiting"):
                debug.save_checkpoint("cp1", "CP1")

    def test_startup_waiting_allows_load_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            checkpoint_dir = debug.checkpoint_dir("cp1")
            checkpoint_dir.mkdir(parents=True)
            (checkpoint_dir / "game.rpg").write_bytes(b"checkpoint-rpg")
            (checkpoint_dir / "manifest.json").write_text(
                json.dumps({"version": 1, "label": "CP1", "created_at": "2026-01-01T00:00:00Z"}),
                encoding="utf-8",
            )
            self.write_state(debug, make_startup_state())

            with patch.object(debug, "run_slot_command", side_effect=self.fake_load_command):
                result = debug.load_checkpoint("cp1")

            self.assertTrue(result["ok"])
            self.assertEqual(debug.save_slot_path().read_bytes(), b"checkpoint-rpg")

    def test_start_new_game_writes_start_slot_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            self.write_state(debug, make_startup_state())

            def run(command, slot):
                self.assertEqual(command, "start_slot")
                self.assertEqual(slot, 0)
                return {
                    "action": {"command": command, "slot": slot},
                    "result": {"ok": True, "action": command, "slot": slot, "status": "start_scheduled"},
                }

            with patch.object(debug, "run_slot_command", side_effect=run):
                result = debug.start_new_game()

            self.assertTrue(result["ok"])
            self.assertEqual(result["native_result"]["action"], "start_slot")

    def test_wait_for_slot_result_accepts_slot_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            start_time = 0.0
            debug.action_result_path.write_text(
                json.dumps({"ok": True, "action": "start_slot", "slot": 0}),
                encoding="utf-8",
            )

            result = debug.wait_for_slot_result("start_slot", 0, start_time, timeout=0.1)

            self.assertTrue(result["ok"])
            self.assertEqual(result["slot"], 0)

    def test_save_checkpoint_writes_manifest_game_and_existing_companion_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            self.write_state(debug)
            debug.dialogs_path.write_text('[{"source_key":"d1","text":"hi"}]', encoding="utf-8")
            debug.exploration_path.write_text('{"version":2,"rooms":{},"transitions":{}}', encoding="utf-8")
            debug.navigation_log_path.parent.mkdir(parents=True, exist_ok=True)
            debug.navigation_log_path.write_text('{"type":"route"}\n', encoding="utf-8")

            with patch.object(debug, "run_slot_command", side_effect=self.fake_save_command(debug)):
                result = debug.save_checkpoint("before-door", "Before Door")

            checkpoint = result["checkpoint"]
            checkpoint_dir = debug.checkpoint_dir("before-door")
            self.assertTrue(result["ok"])
            self.assertEqual(checkpoint["version"], 1)
            self.assertEqual(checkpoint["label"], "Before Door")
            self.assertEqual(checkpoint["state"]["scene_id"], 3)
            self.assertEqual((checkpoint_dir / "game.rpg").read_bytes(), b"checkpoint-rpg")
            self.assertTrue((checkpoint_dir / "companion" / "dialog_history.json").exists())
            self.assertTrue((checkpoint_dir / "companion" / "exploration.json").exists())
            self.assertTrue((checkpoint_dir / "companion" / "logs" / "navigation.log").exists())
            self.assertFalse((checkpoint_dir / "state.json").exists())
            self.assertFalse((checkpoint_dir / "screen.bmp").exists())

    def test_load_checkpoint_restores_companion_files_and_refreshes_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            self.write_state(debug)
            with patch.object(debug, "run_slot_command", side_effect=self.fake_save_command(debug)):
                debug.save_checkpoint("cp1", "CP1")

            debug.dialogs_path.write_text('[{"source_key":"old","text":"old"}]', encoding="utf-8")
            debug.events_path.write_text('[{"type":"old"}]', encoding="utf-8")
            debug.agent_log_path.write_text('{"turn":99}\n', encoding="utf-8")
            debug.dialog_history = [{"source_key": "old", "text": "old"}]
            debug.event_log = [{"type": "old"}]

            checkpoint_dir = debug.checkpoint_dir("cp1")
            (checkpoint_dir / "companion" / "dialog_history.json").write_text(
                '[{"source_key":"new","text":"new"}]',
                encoding="utf-8",
            )
            (checkpoint_dir / "companion" / "events.json").write_text('[{"type":"new"}]', encoding="utf-8")

            with patch.object(debug, "run_slot_command", side_effect=self.fake_load_command):
                result = debug.load_checkpoint("cp1")

            self.assertTrue(result["ok"])
            self.assertEqual(debug.save_slot_path().read_bytes(), b"checkpoint-rpg")
            self.assertEqual(debug.dialog_history, [{"source_key": "new", "text": "new"}])
            self.assertEqual(debug.event_log, [{"type": "new"}])
            self.assertFalse(debug.agent_log_path.exists())

    def test_delete_checkpoint_only_removes_target_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            first = debug.checkpoint_dir("first")
            second = debug.checkpoint_dir("second")
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "manifest.json").write_text('{"version":1}', encoding="utf-8")
            (second / "manifest.json").write_text('{"version":1}', encoding="utf-8")

            result = debug.delete_checkpoint("first")

            self.assertTrue(result["ok"])
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())


if __name__ == "__main__":
    unittest.main()
