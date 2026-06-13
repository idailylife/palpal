import json
import os
import tempfile
import time
import unittest
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


def make_state(width=3, height=3, start=(1, 1), objects=None):
    return {
        "frame": 1,
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
            "bits": grid_bits(width, height),
        },
    }


class ExplorationTests(unittest.TestCase):
    def make_debug_state(self, tmp):
        return DebugState(Path(tmp), Path(tmp) / "assets", Path(tmp) / "sdlpal")

    def write_state(self, debug, state):
        debug.state_path.write_text(json.dumps(state), encoding="utf-8")

    def test_update_marks_current_and_frontier(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            self.write_state(debug, state)

            summary = debug.update_exploration(state)

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["visited"], 1)
            self.assertEqual(summary["frontier"], 4)
            self.assertFalse(summary["complete"])

    def test_touch_trigger_nodes_are_not_frontier(self):
        touch_on_up_node = {
            "id": 48,
            "x": 32,
            "y": 0,
            "state": 1,
            "trigger_mode": 4,
            "trigger_script": 60,
            "auto_script": 0,
            "blocking": False,
            "interactable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(objects=[touch_on_up_node])
            self.write_state(debug, state)

            summary = debug.update_exploration(state)
            frontier = {(node["grid_x"], node["grid_y"]) for node in summary["nodes"]["frontier"]}

            self.assertNotIn((2, 0), frontier)
            self.assertGreater(summary["blocked_edges"], 0)

    def test_step_sends_only_one_safe_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            self.write_state(debug, state)
            debug.update_exploration(state)

            result = debug.exploration_step(state, max_steps=4)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "stepping")
            self.assertEqual(len(result["action"]["buttons"]), 1)
            self.assertLessEqual(len(result["route"]["planned_moves"]), 4)
            self.assertTrue(debug.action_path.exists())

    def test_stale_state_stops_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            self.write_state(debug, state)
            old = time.time() - 10
            os.utime(debug.state_path, (old, old))

            result = debug.exploration_step(state)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "state_stale")

    def test_dialog_stops_exploration_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            state["dialog"] = {"active": True}
            self.write_state(debug, state)

            result = debug.exploration_step(state)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "in_dialog")

    def test_reset_removes_exploration_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            self.write_state(debug, state)
            debug.update_exploration(state)
            self.assertTrue(debug.exploration_path.exists())

            result = debug.reset_exploration()

            self.assertTrue(result["ok"])
            self.assertFalse(debug.exploration_path.exists())

    def test_state_events_record_inventory_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            debug.record_state_events(state)

            changed = make_state()
            changed["inventory"] = [{"slot": 0, "item_id": 99, "amount": 1, "amount_in_use": 0}]
            debug.record_state_events(changed)

            self.assertEqual(len(debug.event_log), 1)
            self.assertEqual(debug.event_log[0]["type"], "inventory_changed")
            self.assertEqual(debug.event_log[0]["item_id"], 99)
            self.assertEqual(debug.event_log[0]["delta"], 1)
            self.assertTrue(debug.events_path.exists())

    def test_dialog_event_is_persisted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            state["dialog"] = {
                "active": True,
                "position": "lower",
                "latest_event": {
                    "sequence": 1,
                    "frame": 42,
                    "scene_id": 3,
                    "map_id": 10,
                    "event_object_id": 7,
                    "message_id": 88,
                    "dialog_position": "lower",
                    "speaker_hint": "赵灵儿",
                    "text": "我们走吧。",
                },
            }

            debug.record_dialog_event(state)
            debug.record_dialog_event(state)

            self.assertEqual(len(debug.dialog_history), 1)
            self.assertEqual(debug.dialog_history[0]["speaker_hint"], "赵灵儿")
            self.assertEqual(debug.dialog_history[0]["text"], "我们走吧。")
            self.assertEqual(debug.dialog_history[0]["room_key"], "test-room")
            self.assertTrue(debug.dialogs_path.exists())

    def test_dialogs_view_filters_by_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            first = make_state()
            first["dialog"] = {
                "latest_event": {
                    "sequence": 1,
                    "frame": 1,
                    "message_id": 1,
                    "event_object_id": 10,
                    "speaker_hint": "李逍遥",
                    "text": "好。",
                },
            }
            second = make_state()
            second["dialog"] = {
                "latest_event": {
                    "sequence": 2,
                    "frame": 2,
                    "message_id": 2,
                    "event_object_id": 11,
                    "speaker_hint": "赵灵儿",
                    "text": "谢谢。",
                },
            }

            debug.record_dialog_event(first)
            debug.record_dialog_event(second)
            view = debug.dialogs_view(speaker="赵灵儿")

            self.assertEqual(view["total"], 1)
            self.assertEqual(view["dialogs"][0]["text"], "谢谢。")

    def test_write_use_item_action_for_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)

            result = debug.write_use_item_action(item_id=88, role_id=0)

            self.assertEqual(result["command"], "use_item")
            self.assertEqual(result["item_id"], 88)
            self.assertEqual(result["role_id"], 0)
            self.assertEqual(result["target"], "role")
            self.assertEqual(debug.action_path.read_text(encoding="ascii"), "use_item 88 0\n")

    def test_write_use_item_action_for_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)

            result = debug.write_use_item_action(item_id=89, target="all")

            self.assertEqual(result["target"], "all")
            self.assertIsNone(result["role_id"])
            self.assertEqual(debug.action_path.read_text(encoding="ascii"), "use_item 89 all\n")

    def test_opening_inner_monologue_is_attributed_to_li_xiaoyao(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            state["scene_id"] = 1
            state["map_id"] = 12
            state["dialog"] = {
                "latest_event": {
                    "sequence": 1,
                    "frame": 58,
                    "scene_id": 1,
                    "map_id": 12,
                    "event_object_id": 5,
                    "message_id": 1957,
                    "dialog_position": "lower",
                    "speaker_hint": None,
                    "text": "昨晚做好的密道正好派上用场",
                },
            }

            debug.record_dialog_event(state)

            self.assertEqual(debug.dialog_history[0]["speaker_hint"], "李逍遙")

    def test_reward_dialog_is_attributed_to_system(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            state["dialog"] = {
                "latest_event": {
                    "sequence": 1,
                    "frame": 2060,
                    "scene_id": 3,
                    "map_id": 10,
                    "event_object_id": 57,
                    "message_id": 631,
                    "dialog_position": "center_window",
                    "speaker_hint": None,
                    "text": "得到５００文钱",
                },
            }

            debug.record_dialog_event(state)

            self.assertEqual(debug.dialog_history[0]["speaker_hint"], "系统")

    def test_navigation_step_sends_first_move_to_reachable_target(self):
        target = {
            "id": 48,
            "x": 32,
            "y": 0,
            "sprite_id": 21,
            "state": 1,
            "trigger_mode": 4,
            "trigger_script": 60,
            "auto_script": 0,
            "blocking": False,
            "interactable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(objects=[target])
            self.write_state(debug, state)

            result = debug.navigation_step(state, target_id=48)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "stepping")
            self.assertEqual(result["action"]["buttons"], ["up"])
            self.assertTrue(debug.action_path.exists())

    def test_navigation_step_sends_straight_segment_until_turn(self):
        target = {
            "id": 48,
            "x": 64,
            "y": 0,
            "sprite_id": 21,
            "state": 1,
            "trigger_mode": 4,
            "trigger_script": 60,
            "auto_script": 0,
            "blocking": False,
            "interactable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(width=5, height=5, start=(0, 4), objects=[target])
            self.write_state(debug, state)

            result = debug.navigation_step(state, target_id=48)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "stepping")
            self.assertEqual(result["action"]["buttons"], ["up"])
            self.assertEqual(result["action"]["frames"], 1)
            self.assertEqual(result["segment_steps"], 4)
            self.assertEqual(result["segment_moves"], ["up", "up", "up", "up"])
            self.assertEqual(result["expected_node"], {"x": 64, "y": 0})

    def test_navigation_step_interacts_with_search_target_at_current_node(self):
        target = {
            "id": 63,
            "x": 32,
            "y": 0,
            "state": 1,
            "trigger_mode": 1,
            "trigger_script": 100,
            "auto_script": 0,
            "blocking": False,
            "interactable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(start=(2, 0), objects=[target])
            self.write_state(debug, state)

            result = debug.navigation_step(state, target_id=63)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "interacting")
            self.assertEqual(result["action"]["buttons"], ["search"])
            self.assertTrue(debug.action_path.exists())

    def test_navigation_step_stops_when_target_becomes_inactive(self):
        target = {
            "id": 63,
            "x": 32,
            "y": 0,
            "state": 0,
            "trigger_mode": 1,
            "trigger_script": 100,
            "auto_script": 0,
            "blocking": False,
            "interactable": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(start=(2, 0), objects=[target])
            self.write_state(debug, state)

            result = debug.navigation_step(state, target_id=63)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "target_inactive")
            self.assertFalse(debug.action_path.exists())

    def test_navigation_step_reports_search_target_needs_direction(self):
        target = {
            "id": 10,
            "x": 32,
            "y": 0,
            "state": 1,
            "trigger_mode": 1,
            "trigger_script": 100,
            "auto_script": 0,
            "blocking": False,
            "interactable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(start=(1, 1), objects=[target])
            state["party_direction"] = 3
            self.write_state(debug, state)

            result = debug.navigation_step(state, target_id=10)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "needs_direction")
            self.assertEqual(result["required_direction"], "up")
            self.assertFalse(debug.action_path.exists())

    def test_route_reports_interactable_blocker(self):
        door = {
            "id": 74,
            "x": 16,
            "y": 8,
            "state": 2,
            "trigger_mode": 1,
            "trigger_script": 8149,
            "auto_script": 0,
            "blocking": True,
            "interactable": True,
        }
        target = {
            "id": 52,
            "x": 32,
            "y": 0,
            "sprite_id": 21,
            "state": 1,
            "trigger_mode": 4,
            "trigger_script": 4631,
            "auto_script": 0,
            "blocking": False,
            "interactable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(width=3, height=3, start=(0, 2), objects=[door, target])
            self.write_state(debug, state)

            result = debug.route_to_object(state, target_id=52)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "blocked_by_interactable")
            self.assertEqual(result["blocker"]["id"], 74)
            self.assertEqual(result["interaction"]["required_direction"], "up")
            self.assertEqual(result["interaction"]["route"]["steps"], 0)

            navigation = debug.navigation(state)
            target_status = {target["id"]: target for target in navigation["targets"]}
            self.assertEqual(target_status[52]["status"], "blocked_by_interactable")
            self.assertEqual(target_status[52]["blocker"]["id"], 74)

    def test_navigation_step_interacts_with_blocking_door(self):
        door = {
            "id": 74,
            "x": 16,
            "y": 8,
            "state": 2,
            "trigger_mode": 1,
            "trigger_script": 8149,
            "auto_script": 0,
            "blocking": True,
            "interactable": True,
        }
        target = {
            "id": 52,
            "x": 32,
            "y": 0,
            "sprite_id": 21,
            "state": 1,
            "trigger_mode": 4,
            "trigger_script": 4631,
            "auto_script": 0,
            "blocking": False,
            "interactable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(width=3, height=3, start=(0, 2), objects=[door, target])
            self.write_state(debug, state)

            result = debug.navigation_step(state, target_id=52)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "interacting")
            self.assertEqual(result["blocker"]["id"], 74)
            self.assertEqual(result["action"]["buttons"], ["up", "search"])
            self.assertTrue(debug.action_path.exists())

    def test_invisible_touch_target_is_reported_as_transition(self):
        target = {
            "id": 52,
            "x": 32,
            "y": 0,
            "sprite_id": 0,
            "state": 1,
            "trigger_mode": 4,
            "trigger_script": 4631,
            "auto_script": 0,
            "blocking": False,
            "interactable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(objects=[target])
            self.write_state(debug, state)

            route = debug.route_to_object(state, target_id=52)
            self.assertEqual(route["status"], "transition")
            self.assertTrue(route["transition_candidate"])
            self.assertEqual(route["route"]["steps"], 1)

            navigation = debug.navigation(state)
            target_status = {target["id"]: target for target in navigation["targets"]}
            self.assertEqual(target_status[52]["status"], "transition")
            self.assertTrue(target_status[52]["transition_candidate"])
            self.assertEqual(target_status[52]["steps"], 1)

            step = debug.navigation_step(state, target_id=52)
            self.assertTrue(step["ok"])
            self.assertEqual(step["status"], "stepping")
            self.assertEqual(step["action"]["buttons"], ["up"])
            self.assertTrue(debug.action_path.exists())

    def test_dialog_stops_navigation_step(self):
        target = {
            "id": 48,
            "x": 32,
            "y": 0,
            "state": 1,
            "trigger_mode": 4,
            "trigger_script": 60,
            "auto_script": 0,
            "blocking": False,
            "interactable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(objects=[target])
            state["dialog"] = {"active": True}
            self.write_state(debug, state)

            result = debug.navigation_step(state, target_id=48)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "in_dialog")
            self.assertFalse(debug.action_path.exists())


if __name__ == "__main__":
    unittest.main()
