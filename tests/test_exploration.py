import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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


def make_state(width=3, height=3, start=(1, 1), objects=None, blocked=(), viewport=None):
    state = {
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
            "bits": grid_bits(width, height, blocked=blocked),
        },
    }
    if viewport is not None:
        state["viewport"] = viewport
    return state


def make_touch_object(object_id, x=32, y=0, sprite_id=1):
    return {
        "id": object_id,
        "x": x,
        "y": y,
        "sprite_id": sprite_id,
        "state": 1,
        "trigger_mode": 4,
        "trigger_script": 60,
        "auto_script": 0,
        "blocking": False,
        "interactable": True,
    }


class ExplorationTests(unittest.TestCase):
    def make_debug_state(self, tmp):
        return DebugState(Path(tmp), Path(tmp) / "assets", Path(tmp) / "sdlpal")

    def write_state(self, debug, state):
        debug.state_path.write_text(json.dumps(state), encoding="utf-8")

    def seed_exploration_room(self, debug, state, visited):
        identity = debug.room_identity(state)
        data = debug.empty_exploration()
        room = debug.exploration_room(data, identity)
        room["visited"] = [f"{x},{y}" for x, y in visited]
        debug.write_exploration(data)

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

    def test_update_marks_visible_reachable_nodes_explorable_not_visited(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(width=6, height=6, start=(2, 2), viewport={"x": 16, "y": 8})
            self.write_state(debug, state)

            summary = debug.update_exploration(state)
            visited = {(node["grid_x"], node["grid_y"]) for node in summary["nodes"]["visited"]}
            explorable = {(node["grid_x"], node["grid_y"]) for node in summary["nodes"]["explorable"]}
            frontier = {(node["grid_x"], node["grid_y"]) for node in summary["nodes"]["frontier"]}

            self.assertEqual(visited, {(2, 2)})
            self.assertIn((1, 1), explorable)
            self.assertIn((5, 5), explorable)
            self.assertGreater(summary["explorable"], summary["visited"])
            self.assertIn((1, 1), frontier)
            self.assertIn((3, 1), frontier)
            self.assertFalse(summary["complete"])

    def test_visible_unreachable_nodes_are_not_marked_visited(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            island = {(1, 1), (2, 2), (3, 3), (8, 1), (9, 2), (10, 3)}
            blocked = {(x, y) for y in range(6) for x in range(12) if (x, y) not in island}
            state = make_state(width=12, height=6, start=(1, 1), blocked=blocked, viewport={"x": 0, "y": 0})
            self.write_state(debug, state)

            identity = debug.room_identity(state)
            data = debug.empty_exploration()
            room = debug.exploration_room(data, identity)
            room["visited"] = ["8,1", "9,2"]
            debug.write_exploration(data)

            summary = debug.update_exploration(state)
            visited = {(node["grid_x"], node["grid_y"]) for node in summary["nodes"]["visited"]}
            explorable = {(node["grid_x"], node["grid_y"]) for node in summary["nodes"]["explorable"]}
            frontier = {(node["grid_x"], node["grid_y"]) for node in summary["nodes"]["frontier"]}

            self.assertIn((1, 1), visited)
            self.assertIn((2, 2), explorable)
            self.assertNotIn((2, 2), visited)
            self.assertNotIn((8, 1), visited)
            self.assertNotIn((9, 2), visited)
            self.assertNotIn((10, 3), explorable)
            self.assertNotIn((10, 3), frontier)

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

    def test_blocking_nodes_are_not_exploration_frontier(self):
        blocker = {
            "id": 74,
            "x": 32,
            "y": 0,
            "state": 2,
            "trigger_mode": 1,
            "trigger_script": 8149,
            "auto_script": 0,
            "blocking": True,
            "interactable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(objects=[blocker])
            self.write_state(debug, state)

            summary = debug.update_exploration(state)
            frontier = {(node["grid_x"], node["grid_y"]) for node in summary["nodes"]["frontier"]}

            self.assertNotIn((2, 0), frontier)
            self.assertGreater(summary["blocked_edges"], 0)

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
                "events": [
                    {
                        "sequence": 1,
                        "frame": 42,
                        "scene_id": 3,
                        "map_id": 10,
                        "event_object_id": 7,
                        "message_id": 88,
                        "dialog_position": "lower",
                        "speaker_hint": "赵灵儿",
                        "text": "我们走吧。",
                    }
                ],
            }

            debug.record_dialog_event(state)
            debug.record_dialog_event(state)

            self.assertEqual(len(debug.dialog_history), 1)
            self.assertEqual(debug.dialog_history[0]["speaker_hint"], "赵灵儿")
            self.assertEqual(debug.dialog_history[0]["text"], "我们走吧。")
            self.assertEqual(debug.dialog_history[0]["room_key"], "test-room")
            self.assertTrue(debug.dialogs_path.exists())

    def test_dialog_speaker_title_event_is_not_persisted_as_body_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            state["dialog"] = {
                "events": [
                    {
                        "sequence": 1,
                        "frame": 7842,
                        "message_id": 635,
                        "event_object_id": 57,
                        "speaker_hint": "李大娘",
                        "text": "李大娘：",
                    },
                    {
                        "sequence": 2,
                        "frame": 7843,
                        "message_id": 636,
                        "event_object_id": 57,
                        "speaker_hint": "李大娘",
                        "text": "逍遥你来的正好，快把门口",
                    },
                ],
            }

            debug.record_dialog_event(state)
            debug.record_dialog_event(state)

            self.assertEqual(len(debug.dialog_history), 1)
            self.assertEqual(debug.dialog_history[0]["speaker_hint"], "李大娘")
            self.assertEqual(debug.dialog_history[0]["text"], "逍遥你来的正好，快把门口")

    def test_dialog_event_stream_merges_contiguous_lines_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            state["dialog"] = {
                "active": False,
                "position": "upper",
                "latest_event": {"sequence": 3, "message_id": 638, "event_object_id": 57, "text": "碍咱们做生意"},
                "events": [
                    {"sequence": 1, "frame": 7843, "message_id": 636, "event_object_id": 57, "speaker_hint": "李大娘", "text": "逍遥你来的正好，快把门口"},
                    {"sequence": 2, "frame": 7843, "message_id": 637, "event_object_id": 57, "speaker_hint": "李大娘", "text": "那个臭要饭的赶走，免得妨"},
                    {"sequence": 3, "frame": 7843, "message_id": 638, "event_object_id": 57, "speaker_hint": "李大娘", "text": "碍咱们做生意"},
                ],
            }

            debug.record_dialog_event(state)
            debug.record_dialog_event(state)

            self.assertEqual(len(debug.dialog_history), 1)
            self.assertEqual(debug.dialog_history[0]["message_ids"], [636, 637, 638])
            self.assertEqual(debug.dialog_history[0]["start_sequence"], 1)
            self.assertEqual(debug.dialog_history[0]["end_sequence"], 3)
            self.assertEqual(
                debug.dialog_history[0]["text"],
                "逍遥你来的正好，快把门口那个臭要饭的赶走，免得妨碍咱们做生意",
            )

    def test_dialog_event_stream_splits_when_speaker_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            state["dialog"] = {
                "events": [
                    {"sequence": 1, "frame": 10, "message_id": 636, "event_object_id": 57, "speaker_hint": "李大娘", "text": "逍遥你来的正好，"},
                    {"sequence": 2, "frame": 10, "message_id": 637, "event_object_id": 57, "speaker_hint": "李大娘", "text": "快把门口的人赶走。"},
                    {"sequence": 3, "frame": 11, "message_id": 638, "event_object_id": 57, "speaker_hint": "李逍遥", "text": "啊！"},
                    {"sequence": 4, "frame": 11, "message_id": 639, "event_object_id": 57, "speaker_hint": "李逍遥", "text": "我马上就去。"},
                ],
            }

            debug.record_dialog_event(state)

            self.assertEqual([entry["speaker_hint"] for entry in debug.dialog_history], ["李大娘", "李逍遥"])
            self.assertEqual(debug.dialog_history[0]["text"], "逍遥你来的正好，快把门口的人赶走。")
            self.assertEqual(debug.dialog_history[1]["text"], "啊！我马上就去。")

    def test_system_dialog_event_stays_separate_from_character_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            state["dialog"] = {
                "events": [
                    {"sequence": 1, "frame": 20, "message_id": 630, "event_object_id": 57, "speaker_hint": "李大娘", "text": "拿去吧。"},
                    {"sequence": 2, "frame": 21, "message_id": 631, "event_object_id": 57, "speaker_hint": None, "text": "得到５００文钱"},
                    {"sequence": 3, "frame": 22, "message_id": 632, "event_object_id": 57, "speaker_hint": "李大娘", "text": "别乱花。"},
                ],
            }

            debug.record_dialog_event(state)

            self.assertEqual([entry["speaker_hint"] for entry in debug.dialog_history], ["李大娘", "系统", "李大娘"])
            self.assertEqual([entry["text"] for entry in debug.dialog_history], ["拿去吧。", "得到５００文钱", "别乱花。"])

    def test_dialogs_view_filters_by_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            first = make_state()
            first["dialog"] = {
                "events": [
                    {
                        "sequence": 1,
                        "frame": 1,
                        "message_id": 1,
                        "event_object_id": 10,
                        "speaker_hint": "李逍遥",
                        "text": "好。",
                    }
                ],
            }
            second = make_state()
            second["dialog"] = {
                "events": [
                    {
                        "sequence": 2,
                        "frame": 2,
                        "message_id": 2,
                        "event_object_id": 11,
                        "speaker_hint": "赵灵儿",
                        "text": "谢谢。",
                    }
                ],
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
                "events": [
                    {
                        "sequence": 1,
                        "frame": 58,
                        "scene_id": 1,
                        "map_id": 12,
                        "event_object_id": 5,
                        "message_id": 1957,
                        "dialog_position": "lower",
                        "speaker_hint": None,
                        "text": "昨晚做好的密道正好派上用场",
                    }
                ],
            }

            debug.record_dialog_event(state)

            self.assertEqual(debug.dialog_history[0]["speaker_hint"], "李逍遙")

    def test_reward_dialog_is_attributed_to_system(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            state["dialog"] = {
                "events": [
                    {
                        "sequence": 1,
                        "frame": 2060,
                        "scene_id": 3,
                        "map_id": 10,
                        "event_object_id": 57,
                        "message_id": 631,
                        "dialog_position": "center_window",
                        "speaker_hint": None,
                        "text": "得到５００文钱",
                    }
                ],
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
            self.assertEqual(result["action"]["buttons"], ["up", "up", "up", "up"])
            self.assertEqual(result["action"]["frames"], 1)
            self.assertEqual(result["segment_steps"], 4)
            self.assertEqual(result["segment_moves"], ["up", "up", "up", "up"])
            self.assertEqual(result["expected_node"], {"x": 64, "y": 0})
            self.assertEqual(result["remaining_steps"], 0)

    def test_navigation_step_sends_straight_transition_segment(self):
        target = {
            "id": 52,
            "x": 64,
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
            state = make_state(width=5, height=5, start=(0, 4), objects=[target])
            self.write_state(debug, state)

            result = debug.navigation_step(state, target_id=52)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "stepping")
            self.assertEqual(result["action"]["buttons"], ["up", "up", "up", "up"])
            self.assertEqual(result["segment_steps"], 4)
            self.assertEqual(result["segment_moves"], ["up", "up", "up", "up"])
            self.assertEqual(result["expected_node"], {"x": 64, "y": 0})
            self.assertEqual(result["remaining_steps"], 0)

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

    def test_navigation_step_faces_search_target_when_already_at_goal(self):
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

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "facing")
            self.assertEqual(result["action"]["buttons"], ["up"])
            self.assertEqual(result["required_direction"], "up")
            self.assertTrue(debug.action_path.exists())

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

    def test_navigation_response_identifies_environment_without_animation_frame(self):
        target = make_touch_object(52, x=32, y=0, sprite_id=21)
        target["current_frame"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(objects=[target])

            navigation = debug.navigation(state)
            self.assertIn("environment", navigation)
            self.assertIn("environment_key", navigation)

            animation_state = json.loads(json.dumps(state))
            animation_state["objects"][0]["current_frame"] = 2
            animation_state["objects"][0]["auto_script"] = 9999
            animation_navigation = debug.navigation(animation_state)
            self.assertEqual(navigation["environment_key"], animation_navigation["environment_key"])

            changed_state = json.loads(json.dumps(state))
            changed_state["objects"][0]["state"] = 2
            changed_navigation = debug.navigation(changed_state)
            self.assertNotEqual(navigation["environment_key"], changed_navigation["environment_key"])

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

    def test_navigation_transition_records_observed_destination(self):
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

            step = debug.navigation_step(state, target_id=52)
            self.assertTrue(step["ok"])

            next_state = make_state(objects=[])
            next_state["scene_id"] = 4
            next_state["map_id"] = 11
            next_state["room"] = {"key": "next-room"}
            self.write_state(debug, next_state)
            debug.snapshot()

            data = debug.read_exploration()
            record = data["transitions"]["3/10/test-room"]["52"]
            self.assertTrue(record["observed"])
            self.assertEqual(record["from_room_id"], "3/10/test-room")
            self.assertEqual(record["to_room_id"], "4/11/next-room")

    def test_navigation_and_route_include_known_transition_destination(self):
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
            from_identity = debug.room_identity(state)
            to_state = make_state(objects=[])
            to_state["scene_id"] = 4
            to_state["map_id"] = 11
            to_state["room"] = {"key": "next-room"}
            to_identity = debug.room_identity(to_state)
            debug.record_transition_destination(from_identity, 52, to_identity)
            data = debug.read_exploration()
            room = debug.exploration_room(data, to_identity)
            room["visited"] = ["1,1", "2,2"]
            room["frontier"] = ["3,3"]
            room["explorable"] = 10
            room["complete"] = False
            debug.write_exploration(data)

            navigation = debug.run_agent_tool("get_navigation", {"mode": "full"}, state)["navigation"]
            target_status = {target["id"]: target for target in navigation["targets"]}
            route = debug.run_agent_tool("get_route", {"id": 52}, state)["route"]
            known = debug.run_agent_tool("get_known_transitions", {}, state)

            self.assertTrue(target_status[52]["known_destination"])
            self.assertEqual(target_status[52]["known_destination_room_id"], "4/11/next-room")
            self.assertEqual(target_status[52]["destination_priority"], "partial")
            self.assertEqual(target_status[52]["destination_exploration"]["frontier"], 1)
            self.assertTrue(route["known_destination"])
            self.assertEqual(route["known_destination_room_id"], "4/11/next-room")
            self.assertEqual(route["destination_priority"], "partial")
            self.assertEqual(known["known_transitions"]["52"]["to_room_id"], "4/11/next-room")
            self.assertEqual(known["known_transitions"]["52"]["destination_priority"], "partial")
            self.assertEqual(known["known_transitions"]["52"]["destination_exploration"]["explored"], 2)

    def test_unknown_transition_destination_is_explicit(self):
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

            route = debug.run_agent_tool("get_route", {"id": 52}, state)["route"]

            self.assertFalse(route["known_destination"])
            self.assertIsNone(route["known_destination_room_id"])
            self.assertIsNone(route["known_destination_observed_at"])
            self.assertEqual(route["destination_priority"], "unknown")
            self.assertFalse(route["destination_exploration"]["known"])

    def test_recent_transition_destination_is_marked_as_backtrack(self):
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
            previous = make_state(objects=[])
            previous["scene_id"] = 1
            previous["map_id"] = 12
            previous["room"] = {"key": "previous-room"}
            previous_identity = debug.room_identity(previous)
            debug.record_transition_destination(debug.room_identity(state), 52, previous_identity)
            debug.agent_log_path.write_text(
                json.dumps({"turn": 1, "episode": previous_identity["id"], "decision": {}}) + "\n",
                encoding="utf-8",
            )

            navigation = debug.run_agent_tool("get_navigation", {"mode": "full"}, state)["navigation"]
            target_status = {target["id"]: target for target in navigation["targets"]}
            known = debug.run_agent_tool("get_known_transitions", {}, state)

            self.assertTrue(target_status[52]["returns_to_recent_room"])
            self.assertEqual(target_status[52]["agent_priority"], "avoid_backtrack")
            self.assertTrue(known["known_transitions"]["52"]["returns_to_recent_room"])
            self.assertEqual(known["known_transitions"]["52"]["agent_priority"], "avoid_backtrack")

    def test_rules_prefer_unknown_transition_over_recent_backtrack(self):
        backtrack = {
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
        unknown = {
            "id": 53,
            "x": 64,
            "y": 0,
            "sprite_id": 0,
            "state": 1,
            "trigger_mode": 4,
            "trigger_script": 4639,
            "auto_script": 0,
            "blocking": False,
            "interactable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(width=5, height=3, objects=[backtrack, unknown])
            self.write_state(debug, state)
            previous = make_state(objects=[])
            previous["scene_id"] = 1
            previous["map_id"] = 12
            previous["room"] = {"key": "previous-room"}
            previous_identity = debug.room_identity(previous)
            debug.record_transition_destination(debug.room_identity(state), 52, previous_identity)
            debug.agent_log_path.write_text(
                json.dumps({"turn": 1, "episode": previous_identity["id"], "decision": {}}) + "\n",
                encoding="utf-8",
            )

            decision = debug.rules_decide_agent(state)

            self.assertEqual(decision["recommended_intent"], "enter_transition")
            self.assertEqual(decision["recommended_action"]["args"]["id"], 53)
            self.assertEqual(decision["evidence"]["target"]["destination_priority"], "unknown")

    def test_get_room_exploration_returns_compact_known_and_unknown_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            self.write_state(debug, state)
            debug.update_exploration(state)

            known = debug.run_agent_tool("get_room_exploration", {"room_id": "3/10/test-room"}, state)["exploration"]
            unknown = debug.run_agent_tool("get_room_exploration", {"room_id": "9/9/missing"}, state)["exploration"]

            self.assertTrue(known["known"])
            self.assertEqual(known["room_id"], "3/10/test-room")
            self.assertEqual(known["scene_id"], 3)
            self.assertEqual(known["map_id"], 10)
            self.assertEqual(known["room_key"], "test-room")
            self.assertEqual(known["explored"], 1)
            self.assertEqual(known["frontier"], 4)
            self.assertIn("explorable", known)
            self.assertFalse(unknown["known"])
            self.assertEqual(unknown["room_id"], "9/9/missing")

    def test_exploration_without_transition_records_still_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            debug.exploration_path.write_text(
                json.dumps({"version": 2, "rooms": {"3/10/test-room": {"scene_id": 3}}}),
                encoding="utf-8",
            )

            data = debug.read_exploration()
            known = debug.run_agent_tool("get_known_transitions", {"from_room_id": "3/10/test-room"}, make_state())

            self.assertEqual(data["transitions"], {})
            self.assertEqual(known["known_transitions"], {})

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

    def test_agent_decide_does_not_write_action_file(self):
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

            decision = debug.rules_decide_agent(state)

            self.assertEqual(decision["recommended_intent"], "inspect_target")
            self.assertFalse(debug.action_path.exists())

    def test_agent_decide_reports_stale_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            self.write_state(debug, state)
            old = time.time() - 10
            os.utime(debug.state_path, (old, old))

            decision = debug.rules_decide_agent(state)

            self.assertEqual(decision["recommended_intent"], "wait_runtime")
            self.assertEqual(decision["recommended_action"]["args"]["reason"], "runtime_stale")
            self.assertFalse(debug.action_path.exists())

    def test_agent_decide_prioritizes_active_dialog(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            state["dialog"] = {
                "active": True,
                "position": "lower",
                "latest_event": {"message_id": 88, "event_object_id": 7, "text": "我们走吧。"},
            }
            self.write_state(debug, state)

            decision = debug.rules_decide_agent(state)

            self.assertEqual(decision["recommended_intent"], "advance_dialog")
            self.assertEqual(decision["recommended_action"]["args"]["buttons"], ["search"])
            self.assertFalse(debug.action_path.exists())

    def test_agent_decide_recommends_reachable_non_transition_target(self):
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

            decision = debug.rules_decide_agent(state)

            self.assertEqual(decision["recommended_intent"], "inspect_target")
            self.assertEqual(decision["recommended_action"]["tool"], "navigation_step")
            self.assertEqual(decision["recommended_action"]["args"], {"id": 48})
            self.assertEqual(decision["evidence"]["target"]["id"], 48)
            self.assertFalse(debug.action_path.exists())

    def test_agent_decide_warns_on_transition_target(self):
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

            decision = debug.rules_decide_agent(state)

            self.assertEqual(decision["recommended_intent"], "enter_transition")
            self.assertEqual(decision["recommended_action"]["tool"], "navigation_step")
            self.assertEqual(decision["recommended_action"]["args"], {"id": 52})
            self.assertIn("leaves the current room", decision["risk"])
            self.assertEqual(decision["evidence"]["target"]["id"], 52)
            self.assertFalse(debug.action_path.exists())

    def test_agent_decision_log_records_turn_and_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            self.write_state(debug, state)

            decision = debug.rules_decide_agent(state)
            log = debug.read_agent_log(limit=10)

            self.assertEqual(decision["recommended_intent"], "wait_runtime")
            self.assertEqual(decision["recommended_action"]["tool"], "no_op")
            self.assertEqual(decision["recommended_action"]["args"], {"reason": "no_reachable_target"})
            self.assertEqual(len(log), 1)
            self.assertEqual(log[0]["turn"], 1)
            self.assertEqual(log[0]["episode"], "3/10/test-room")
            self.assertEqual(log[0]["decision"]["recommended_intent"], decision["recommended_intent"])
            self.assertNotIn("api_key", json.dumps(log[0]).lower())
            self.assertNotIn("prompt", json.dumps(log[0]).lower())

    def test_snapshot_clears_runtime_history_when_frame_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            state["frame"] = 100
            self.write_state(debug, state)
            debug.snapshot()
            debug.append_agent_decision(state, {"recommended_intent": "inspect_target"})
            debug.event_log = [{"type": "inventory_changed"}]
            debug.write_events()
            debug.dialog_history = [{"text": "old dialog"}]
            debug.write_dialog_history()

            restarted = make_state()
            restarted["frame"] = 1
            self.write_state(debug, restarted)
            snapshot = debug.snapshot()

            self.assertEqual(snapshot["frame"], 1)
            self.assertEqual(debug.read_agent_log(limit=10), [])
            self.assertEqual(debug.event_log, [])
            self.assertEqual(debug.dialog_history, [])
            self.assertFalse(debug.agent_log_path.exists())
            self.assertFalse(debug.events_path.exists())
            self.assertFalse(debug.dialogs_path.exists())

    def test_agent_normalizes_model_button_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)

            decision = debug.normalize_agent_decision(
                {
                    "summary": "Dialog is active.",
                    "recommended_intent": "advance_dialog",
                    "recommended_action": {"tool": "press_buttons", "args": {"buttons": ["A", "bad"], "frames": 1}},
                    "evidence": {},
                    "risk": "None.",
                    "alternatives": [],
                }
            )

            self.assertEqual(decision["recommended_action"]["args"]["buttons"], ["search"])
            self.assertFalse(debug.action_path.exists())

    def test_agent_normalizes_target_intent_to_navigation_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)

            decision = debug.normalize_agent_decision(
                {
                    "summary": "Target 10 should be inspected.",
                    "recommended_intent": "inspect_target",
                    "recommended_action": {"tool": "exploration_step", "args": {"max_steps": 4}},
                    "evidence": {"target": {"id": 10}},
                    "risk": "Low.",
                    "alternatives": [],
                }
            )

            self.assertEqual(decision["recommended_action"]["tool"], "navigation_step")
            self.assertEqual(decision["recommended_action"]["args"], {"id": 10})
            self.assertIn("normalization_warnings", decision["evidence"])
            self.assertFalse(debug.action_path.exists())

    def test_agent_normalizes_explore_room_to_wait_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)

            decision = debug.normalize_agent_decision(
                {
                    "summary": "Explore.",
                    "recommended_intent": "explore_room",
                    "recommended_action": {"tool": "no_op", "args": {}},
                    "evidence": {},
                    "risk": "Low.",
                    "alternatives": [],
                }
            )

            self.assertEqual(decision["recommended_intent"], "wait_runtime")
            self.assertEqual(decision["recommended_action"]["tool"], "no_op")
            self.assertEqual(decision["recommended_action"]["args"], {})
            self.assertIn("normalized unknown intent to wait_runtime", decision["evidence"]["normalization_warnings"])
            self.assertFalse(debug.action_path.exists())

    def test_agent_rejects_model_exploration_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)

            decision = debug.normalize_agent_decision(
                {
                    "summary": "Explore farther.",
                    "recommended_intent": "explore_room",
                    "recommended_action": {"tool": "exploration_step", "args": {"max_steps": 12}},
                    "evidence": {},
                    "risk": "Low.",
                    "alternatives": [],
                }
            )

            self.assertEqual(decision["recommended_intent"], "wait_runtime")
            self.assertEqual(decision["recommended_action"]["tool"], "no_op")
            self.assertEqual(decision["recommended_action"]["args"]["reason"], "unsupported_tool")
            self.assertEqual(decision["recommended_action"]["args"]["tool"], "exploration_step")
            self.assertIn("normalized unknown intent to wait_runtime", decision["evidence"]["normalization_warnings"])
            self.assertIn("normalized unknown action tool to no_op", decision["evidence"]["normalization_warnings"])
            self.assertFalse(debug.action_path.exists())

    def test_agent_rejects_targeted_explore_room(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)

            decision = debug.normalize_agent_decision(
                {
                    "summary": "Explore toward object #74.",
                    "recommended_intent": "explore_room",
                    "recommended_action": {"tool": "exploration_step", "args": {"max_steps": 1}},
                    "evidence": {"target": {"id": 74}},
                    "risk": "Low.",
                    "alternatives": [],
                }
            )

            self.assertEqual(decision["recommended_intent"], "wait_runtime")
            self.assertEqual(decision["recommended_action"]["tool"], "no_op")
            self.assertEqual(decision["recommended_action"]["args"]["reason"], "unsupported_tool")
            self.assertEqual(decision["recommended_action"]["args"]["tool"], "exploration_step")
            self.assertIn("normalized unknown intent to wait_runtime", decision["evidence"]["normalization_warnings"])
            self.assertIn("normalized unknown action tool to no_op", decision["evidence"]["normalization_warnings"])
            self.assertFalse(debug.action_path.exists())

    def test_agent_does_not_infer_target_from_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)

            decision = debug.normalize_agent_decision(
                {
                    "summary": "Target 10 should be inspected.",
                    "recommended_intent": "inspect_target",
                    "recommended_action": {"tool": "exploration_step", "args": {}},
                    "evidence": {},
                    "risk": "Low.",
                    "alternatives": [],
                }
            )

            self.assertEqual(decision["recommended_intent"], "wait_runtime")
            self.assertEqual(decision["recommended_action"]["tool"], "no_op")
            self.assertEqual(decision["recommended_action"]["args"]["reason"], "missing target id for inspect_target")
            self.assertFalse(debug.action_path.exists())

    def test_agent_normalizes_alternatives_to_match_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)

            decision = debug.normalize_agent_decision(
                {
                    "summary": "Inspect.",
                    "recommended_intent": "inspect_target",
                    "recommended_action": {"tool": "navigation_step", "args": {"id": 10}},
                    "evidence": {"target": {"id": 10}},
                    "risk": "Low.",
                    "alternatives": [
                        {
                            "intent": "inspect_target",
                            "action": {"tool": "exploration_step", "args": {"max_steps": 1}},
                            "evidence": {"target": {"id": 11}},
                            "reason": "Other target.",
                        },
                        {
                            "intent": "explore_room",
                            "action": {"tool": "navigation_step", "args": {"id": 12}},
                            "reason": "Map first.",
                        },
                    ],
                }
            )

            self.assertEqual(decision["alternatives"][0]["intent"], "inspect_target")
            self.assertEqual(decision["alternatives"][0]["action"]["tool"], "navigation_step")
            self.assertEqual(decision["alternatives"][0]["action"]["args"], {"id": 11})
            self.assertEqual(decision["alternatives"][1]["intent"], "wait_runtime")
            self.assertEqual(decision["alternatives"][1]["action"]["tool"], "no_op")
            self.assertEqual(decision["alternatives"][1]["action"]["args"], {"reason": "wait_runtime"})
            self.assertFalse(debug.action_path.exists())

    def test_agent_context_marks_targets_with_recent_dialog_feedback(self):
        target = {
            "id": 10,
            "x": 16,
            "y": 8,
            "state": 1,
            "trigger_mode": 1,
            "trigger_script": 100,
            "auto_script": 0,
            "blocking": False,
            "interactable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(objects=[target])
            dialogs = {
                "dialogs": [
                    {
                        "frame": 50,
                        "event_object_id": 10,
                        "message_id": 588,
                        "speaker_hint": "李大娘",
                        "text": "還不快出來幫忙招呼客人",
                    }
                ]
            }

            context = debug.agent_context(state, debug.navigation(state), dialogs, events=[])
            target_context = {item["id"]: item for item in context["navigation"]["targets"]}

            self.assertIn("recent_dialog_feedback", target_context[10])
            self.assertEqual(target_context[10]["recent_dialog_feedback"][0]["text"], "還不快出來幫忙招呼客人")
            self.assertIn("same object", target_context[10]["deprioritize_reason"])

    def test_agent_tools_mark_recently_cleared_search_targets(self):
        left_door = {
            "id": 74,
            "x": 16,
            "y": 8,
            "state": 1,
            "trigger_mode": 1,
            "trigger_script": 8149,
            "auto_script": 0,
            "blocking": False,
            "interactable": True,
        }
        right_door = {
            "id": 75,
            "x": 32,
            "y": 16,
            "state": 1,
            "trigger_mode": 1,
            "trigger_script": 8149,
            "auto_script": 0,
            "blocking": False,
            "interactable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(objects=[left_door, right_door])
            self.write_state(debug, state)
            debug.event_log = [
                {
                    "frame": 20,
                    "scene_id": 3,
                    "map_id": 10,
                    "room_key": "test-room",
                    "type": "object_changed",
                    "object_id": 75,
                    "changes": {
                        "state": {"from": 2, "to": 1},
                        "blocking": {"from": True, "to": False},
                    },
                }
            ]

            navigation = debug.run_agent_tool("get_navigation", {"mode": "full"}, state)["navigation"]
            targets = {target["id"]: target for target in navigation["targets"]}
            route = debug.run_agent_tool("get_route", {"id": 75}, state)["route"]

            self.assertEqual(targets[75]["interaction_state"], "recently_cleared")
            self.assertEqual(targets[75]["agent_priority"], "avoid_repeat")
            self.assertEqual(targets[74]["interaction_state"], "recently_cleared")
            self.assertEqual(targets[74]["agent_priority"], "avoid_repeat")
            self.assertEqual(route["interaction_state"], "recently_cleared")
            self.assertEqual(route["agent_priority"], "avoid_repeat")

    def test_agent_rules_skip_recently_cleared_search_target(self):
        opened_door = {
            "id": 75,
            "x": 16,
            "y": 8,
            "state": 1,
            "trigger_mode": 1,
            "trigger_script": 8149,
            "auto_script": 0,
            "blocking": False,
            "interactable": True,
        }
        fresh_target = {
            "id": 80,
            "x": 32,
            "y": 16,
            "state": 1,
            "trigger_mode": 1,
            "trigger_script": 9000,
            "auto_script": 0,
            "blocking": False,
            "interactable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(objects=[opened_door, fresh_target])
            self.write_state(debug, state)
            debug.event_log = [
                {
                    "frame": 20,
                    "scene_id": 3,
                    "map_id": 10,
                    "room_key": "test-room",
                    "type": "object_changed",
                    "object_id": 75,
                    "changes": {
                        "state": {"from": 2, "to": 1},
                        "blocking": {"from": True, "to": False},
                    },
                }
            ]

            decision = debug.rules_decide_agent(state)

            self.assertEqual(decision["recommended_intent"], "inspect_target")
            self.assertEqual(decision["recommended_action"]["args"], {"id": 80})
            self.assertEqual(decision["evidence"]["target"]["interaction_state"], "fresh")
            self.assertFalse(debug.action_path.exists())

    def test_agent_context_keeps_story_dialog_and_transition_candidates(self):
        transition = {
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
        search = {
            "id": 10,
            "x": 16,
            "y": 8,
            "state": 1,
            "trigger_mode": 1,
            "trigger_script": 100,
            "auto_script": 0,
            "blocking": False,
            "interactable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(objects=[transition, search])
            dialogs = {
                "dialogs": [
                    {
                        "frame": 50,
                        "event_object_id": 10,
                        "message_id": 588,
                        "speaker_hint": "李大娘",
                        "text": "還不快出來幫忙招呼客人",
                    },
                    {
                        "frame": 51,
                        "event_object_id": 10,
                        "message_id": 590,
                        "speaker_hint": "李逍遙",
                        "text": "啊！　．．我馬上就去",
                    },
                ]
            }

            context = debug.agent_context(state, debug.navigation(state), dialogs, events=[])
            targets = {item["id"]: item for item in context["navigation"]["targets"]}

            self.assertTrue(targets[52]["transition_candidate"])
            self.assertEqual(targets[10]["recent_dialog_feedback"][0]["text"], "還不快出來幫忙招呼客人")
            self.assertEqual(context["dialogs"][-1]["text"], "啊！　．．我馬上就去")
            self.assertNotIn("rules", context)

    def test_agent_messages_put_stable_policy_before_dynamic_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            messages = debug.agent_messages(state, debug.navigation(state), {"dialogs": []}, events=[])

            self.assertEqual([message["role"] for message in messages], ["system", "system", "user"])
            self.assertIn("JSON object", messages[0]["content"])
            self.assertIn("greet guests", messages[1]["content"])
            dynamic = json.loads(messages[2]["content"])
            self.assertIn("state", dynamic)
            self.assertIn("exploration", dynamic)
            self.assertIn("last_route", dynamic["exploration"])
            self.assertNotIn("rules", dynamic)

    def test_agent_tool_specs_include_queries_and_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            tools = debug.agent_tool_specs()
            names = [tool["function"]["name"] for tool in tools]
            by_name = {tool["function"]["name"]: tool for tool in tools}

            self.assertEqual(
                set(names),
                {
                    "get_state_summary",
                    "get_navigation",
                    "get_dialogs",
                    "get_dialog_speakers",
                    "get_inventory",
                    "get_events",
                    "get_agent_history",
                    "get_route",
                    "get_room_exploration",
                    "get_known_transitions",
                    "submit_decision",
                },
            )
            dialog_properties = by_name["get_dialogs"]["function"]["parameters"]["properties"]
            self.assertIn("speaker", dialog_properties)
            self.assertEqual(dialog_properties["speaker"]["type"], "string")
            context = debug.agent_tool_initial_context(make_state())
            self.assertIn("get_dialog_speakers", context["available_query_tools"])
            self.assertEqual(names[-1], "submit_decision")

    def test_agent_get_dialog_speakers_returns_recent_speakers(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            debug.dialog_history = [
                {
                    "speaker_hint": "李大娘",
                    "time": 10.0,
                    "frame": 100,
                    "end_frame": 101,
                    "start_sequence": 1,
                    "end_sequence": 2,
                    "scene_id": 3,
                    "map_id": 10,
                    "room_key": "inn",
                    "text": "早点回来。",
                },
                {
                    "speaker_hint": "赵灵儿",
                    "time": 20.0,
                    "frame": 200,
                    "end_frame": 201,
                    "start_sequence": 3,
                    "end_sequence": 3,
                    "scene_id": 4,
                    "map_id": 11,
                    "room_key": "garden",
                    "text": "谢谢。",
                },
                {
                    "speaker_hint": "李大娘",
                    "time": 30.0,
                    "frame": 300,
                    "end_frame": 302,
                    "start_sequence": 4,
                    "end_sequence": 5,
                    "scene_id": 5,
                    "map_id": 12,
                    "room_key": "street",
                    "text": "快去吧。",
                },
            ]

            result = debug.run_agent_tool("get_dialog_speakers", {}, state)

            self.assertTrue(result["ok"])
            self.assertEqual(result["total"], 2)
            self.assertEqual(result["returned"], 2)
            self.assertEqual([speaker["speaker_hint"] for speaker in result["speakers"]], ["李大娘", "赵灵儿"])
            self.assertEqual(result["speakers"][0]["dialog_count"], 2)
            self.assertEqual(result["speakers"][0]["last_time"], 30.0)
            self.assertEqual(result["speakers"][0]["last_frame"], 300)
            self.assertEqual(result["speakers"][0]["last_end_frame"], 302)
            self.assertEqual(result["speakers"][0]["last_sequence"], 4)
            self.assertEqual(result["speakers"][0]["last_end_sequence"], 5)
            self.assertEqual(result["speakers"][0]["last_scene_id"], 5)
            self.assertEqual(result["speakers"][0]["last_map_id"], 12)
            self.assertEqual(result["speakers"][0]["last_room_key"], "street")

    def test_agent_get_dialog_speakers_excludes_system_and_empty_speakers(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            debug.dialog_history = [
                {"speaker_hint": "系统", "time": 1.0, "text": "得到５００文钱"},
                {"speaker_hint": "", "time": 2.0, "text": "empty"},
                {"time": 3.0, "text": "missing"},
                {"speaker_hint": "李逍遥", "time": 4.0, "frame": 40, "text": "好。"},
            ]

            result = debug.run_agent_tool("get_dialog_speakers", {}, state)

            self.assertEqual(result["total"], 1)
            self.assertEqual(result["returned"], 1)
            self.assertEqual(result["speakers"][0]["speaker_hint"], "李逍遥")

    def test_agent_get_dialogs_filters_by_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            debug.dialog_history = [
                {"speaker_hint": "李大娘", "time": 10.0, "frame": 100, "end_frame": 101, "text": "早点回来。"},
                {"speaker_hint": "赵灵儿", "time": 20.0, "frame": 200, "end_frame": 201, "text": "谢谢。"},
                {"speaker_hint": "李大娘", "time": 30.0, "frame": 300, "end_frame": 301, "text": "快去吧。"},
            ]

            result = debug.run_agent_tool("get_dialogs", {"speaker": "李大娘", "limit": 10}, state)

            self.assertTrue(result["ok"])
            self.assertEqual(result["total"], 2)
            self.assertEqual([dialog["speaker_hint"] for dialog in result["dialogs"]], ["李大娘", "李大娘"])
            self.assertEqual([dialog["text"] for dialog in result["dialogs"]], ["早点回来。", "快去吧。"])
            self.assertEqual(result["dialogs"][0]["time"], 10.0)
            self.assertEqual(result["dialogs"][0]["frame"], 100)
            self.assertEqual(result["dialogs"][0]["end_frame"], 101)

    def test_agent_get_inventory_returns_current_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            state["inventory"] = [
                {
                    "slot": 0,
                    "item_id": 91,
                    "name": "十里香",
                    "price": 640,
                    "flags": ["usable", "apply_to_all"],
                    "usable": True,
                    "apply_to_all": True,
                    "sellable": True,
                    "amount": 1,
                    "amount_in_use": 0,
                    "equipped_count": 0,
                },
                {
                    "slot": 1,
                    "item_id": 88,
                    "name": "止血草",
                    "price": 50,
                    "flags": ["usable"],
                    "usable": True,
                    "apply_to_all": False,
                    "sellable": True,
                    "amount": 5,
                    "amount_in_use": 0,
                    "equipped_count": 0,
                },
            ]

            result = debug.run_agent_tool("get_inventory", {}, state)

            self.assertTrue(result["ok"])
            self.assertEqual(result["total"], 2)
            self.assertEqual(result["returned"], 2)
            self.assertEqual(
                result["inventory"][0],
                {
                    "slot": 0,
                    "item_id": 91,
                    "name": "十里香",
                    "amount": 1,
                    "amount_in_use": 0,
                    "equipped_count": 0,
                    "usable": True,
                    "apply_to_all": True,
                    "sellable": True,
                    "flags": ["usable", "apply_to_all"],
                    "price": 640,
                },
            )

    def test_agent_get_inventory_limit_and_empty_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            state["inventory"] = [
                {"slot": 0, "item_id": 1, "name": "one", "amount": 1},
                {"slot": 1, "item_id": 2, "name": "two", "amount": 2},
            ]

            limited = debug.run_agent_tool("get_inventory", {"limit": 1}, state)
            empty = debug.run_agent_tool("get_inventory", {}, {**state, "inventory": []})

            self.assertEqual(limited["total"], 2)
            self.assertEqual(limited["returned"], 1)
            self.assertEqual([item["item_id"] for item in limited["inventory"]], [1])
            self.assertEqual(empty["inventory"], [])
            self.assertEqual(empty["total"], 0)
            self.assertEqual(empty["returned"], 0)

    def test_agent_tools_return_json_serializable_dicts(self):
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
            debug.event_log = [{"type": "inventory_changed"}]
            debug.dialog_history = [{"text": "hello"}]

            calls = [
                ("get_state_summary", {}),
                ("get_navigation", {"mode": "lightweight", "limit": 5}),
                ("get_navigation", {"mode": "full", "limit": 5}),
                ("get_dialogs", {"limit": 1}),
                ("get_dialog_speakers", {"limit": 1}),
                ("get_inventory", {"limit": 1}),
                ("get_events", {"limit": 1}),
                ("get_agent_history", {"limit": 1}),
                ("get_route", {"id": 48}),
                ("get_room_exploration", {"room_id": "3/10/test-room"}),
                ("get_known_transitions", {}),
                (
                    "submit_decision",
                    {
                        "summary": "Wait.",
                        "recommended_intent": "wait_runtime",
                        "recommended_action": {"tool": "no_op", "args": {}},
                        "evidence": {},
                        "risk": "None.",
                        "alternatives": [],
                    },
                ),
            ]

            for name, arguments in calls:
                result = debug.run_agent_tool(name, arguments, state)
                self.assertIsInstance(result, dict)
                json.dumps(result)

            self.assertFalse(debug.action_path.exists())

    def test_deepseek_tool_decision_records_usage_trace_and_no_action(self):
        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(objects=[make_touch_object(52, sprite_id=0)])
            self.write_state(debug, state)
            captured = []
            timeouts = []
            responses = [
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_state",
                                        "type": "function",
                                        "function": {"name": "get_state_summary", "arguments": "{}"},
                                    }
                                ]
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                },
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_nav",
                                        "type": "function",
                                        "function": {"name": "get_navigation", "arguments": '{"mode":"full","limit":5}'},
                                    }
                                ]
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8},
                },
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_submit",
                                        "type": "function",
                                        "function": {
                                            "name": "submit_decision",
                                            "arguments": json.dumps(
                                                {
                                                    "summary": "Leave the room to greet guests.",
                                                    "recommended_intent": "enter_transition",
                                                    "recommended_action": {"tool": "navigation_step", "args": {"id": 52}},
                                                    "evidence": {"target": {"id": 52}},
                                                    "risk": "May leave the room.",
                                                    "alternatives": [],
                                                }
                                            ),
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 3,
                        "total_tokens": 7,
                        "prompt_cache_hit_tokens": 64,
                        "prompt_cache_miss_tokens": 56,
                    },
                },
            ]

            def fake_urlopen(request, timeout):
                captured.append(json.loads(request.data.decode("utf-8")))
                timeouts.append(timeout)
                return FakeResponse(responses.pop(0))

            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), patch("urllib.request.urlopen", fake_urlopen):
                decision = debug.deepseek_decide_agent(state)

            self.assertFalse(responses)
            self.assertEqual(decision["usage"]["prompt_cache_hit_tokens"], 64)
            self.assertEqual(decision["usage"]["prompt_tokens"], 20)
            self.assertEqual(decision["recommended_action"]["args"], {"id": 52})
            self.assertEqual(decision["state_context"], {"scene_id": 3, "map_id": 10, "room_key": "test-room", "frame": 1})
            self.assertEqual([message["role"] for message in captured[0]["messages"]], ["system", "system", "user"])
            self.assertNotIn("response_format", captured[0])
            self.assertIn("tools", captured[0])
            self.assertEqual([trace["name"] for trace in decision["tool_trace"]], ["get_state_summary", "get_navigation", "submit_decision"])
            self.assertFalse(debug.action_path.exists())
            log = debug.read_agent_log(limit=1)
            self.assertEqual(log[0]["backend"], "deepseek")
            self.assertEqual(log[0]["state_context"]["room_key"], "test-room")
            self.assertEqual(log[0]["decision"]["state_context"]["frame"], 1)
            self.assertEqual(log[0]["tool_trace"][1]["name"], "get_navigation")
            self.assertEqual(log[0]["decision"]["usage"]["prompt_cache_miss_tokens"], 56)
            self.assertGreaterEqual(timeouts[0], 89.0)

    def test_deepseek_unknown_tool_returns_clear_error(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {
                                            "id": "call_bad",
                                            "type": "function",
                                            "function": {"name": "write_action", "arguments": "{}"},
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()

            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), patch("urllib.request.urlopen", lambda request, timeout: FakeResponse()):
                with self.assertRaisesRegex(RuntimeError, "unsupported agent tool"):
                    debug.deepseek_decide_agent(state)

            self.assertFalse(debug.action_path.exists())

    def test_deepseek_missing_submit_decision_returns_clear_error(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": '{"summary":"old path"}'}}]}).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()

            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), patch("urllib.request.urlopen", lambda request, timeout: FakeResponse()):
                with self.assertRaisesRegex(RuntimeError, "did not call submit_decision"):
                    debug.deepseek_decide_agent(state)

    def test_deepseek_exceeding_tool_rounds_returns_clear_error(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {
                                            "id": "call_state",
                                            "type": "function",
                                            "function": {"name": "get_state_summary", "arguments": "{}"},
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()

            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), patch("urllib.request.urlopen", lambda request, timeout: FakeResponse()):
                with self.assertRaisesRegex(RuntimeError, "within 8 tool rounds"):
                    debug.deepseek_decide_agent(state)

            self.assertFalse(debug.action_path.exists())

    def test_deepseek_submit_decision_allowed_after_query_tool_budget(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {
                                            "id": f"call_state_{index}",
                                            "type": "function",
                                            "function": {"name": "get_state_summary", "arguments": "{}"},
                                        }
                                        for index in range(40)
                                    ]
                                    + [
                                        {
                                            "id": "call_submit",
                                            "type": "function",
                                            "function": {
                                                "name": "submit_decision",
                                                "arguments": json.dumps(
                                                    {
                                                        "summary": "Wait after inspection.",
                                                        "recommended_intent": "wait_runtime",
                                                        "recommended_action": {"tool": "no_op", "args": {}},
                                                        "evidence": {},
                                                        "risk": "None.",
                                                        "alternatives": [],
                                                    }
                                                ),
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(objects=[make_touch_object(10)])
            self.write_state(debug, state)

            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), patch("urllib.request.urlopen", lambda request, timeout: FakeResponse()):
                decision = debug.deepseek_decide_agent(state)

            self.assertEqual(decision["recommended_intent"], "wait_runtime")
            self.assertEqual(len(decision["tool_trace"]), 41)
            self.assertEqual(decision["tool_trace"][-1]["name"], "submit_decision")
            self.assertFalse(debug.action_path.exists())

    def test_deepseek_exceeding_tool_calls_writes_failure_log(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {
                                            "id": f"call_state_{index}",
                                            "type": "function",
                                            "function": {"name": "get_state_summary", "arguments": "{}"},
                                        }
                                        for index in range(41)
                                    ]
                                }
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()

            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), patch("urllib.request.urlopen", lambda request, timeout: FakeResponse()):
                with self.assertRaisesRegex(RuntimeError, "exceeded 40 query tool calls"):
                    debug.deepseek_decide_agent(state)

            log = debug.read_agent_log(limit=1)
            self.assertEqual(log[0]["ok"], False)
            self.assertEqual(log[0]["tool_calls"], 41)
            self.assertEqual(len(log[0]["tool_trace"]), 41)
            self.assertIn("exceeded 40 query tool calls", log[0]["tool_trace"][-1]["error"])
            self.assertEqual(log[0]["usage"]["total_tokens"], 12)
            self.assertFalse(debug.action_path.exists())

    def test_deepseek_submit_decision_downgrades_stale_navigation_target(self):
        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            original_state = make_state(objects=[make_touch_object(52)])
            latest_state = make_state(objects=[])
            self.write_state(debug, original_state)

            response = {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_submit",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_decision",
                                        "arguments": json.dumps(
                                            {
                                                "summary": "Inspect object #52.",
                                                "recommended_intent": "inspect_target",
                                                "recommended_action": {"tool": "navigation_step", "args": {"id": 52}},
                                                "evidence": {"target": {"id": 52}},
                                                "risk": "Low.",
                                                "alternatives": [],
                                            }
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ]
            }

            def fake_urlopen(request, timeout):
                self.write_state(debug, latest_state)
                return FakeResponse(response)

            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), patch("urllib.request.urlopen", fake_urlopen):
                decision = debug.deepseek_decide_agent(original_state)

            self.assertEqual(decision["recommended_intent"], "wait_runtime")
            self.assertEqual(decision["recommended_action"]["tool"], "no_op")
            self.assertEqual(decision["recommended_action"]["args"]["target_id"], 52)
            self.assertEqual(decision["evidence"]["validation_target_id"], 52)
            self.assertEqual(decision["evidence"]["validation_route_status"], "unknown_target")
            self.assertEqual(decision["evidence"]["validation_state_context"]["room_key"], "test-room")
            self.assertEqual(decision["evidence"]["original_recommendation"]["recommended_action"]["args"], {"id": 52})
            self.assertFalse(debug.action_path.exists())

    def test_deepseek_submit_decision_downgrades_illegal_action(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {
                                            "id": "call_submit",
                                            "type": "function",
                                            "function": {
                                                "name": "submit_decision",
                                                "arguments": json.dumps(
                                                    {
                                                        "summary": "Inspect target.",
                                                        "recommended_intent": "inspect_target",
                                                        "recommended_action": {"tool": "write_action", "args": {"id": 10}},
                                                        "evidence": {"target": {"id": 10}},
                                                        "risk": "Low.",
                                                        "alternatives": [],
                                                    }
                                                ),
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(objects=[make_touch_object(10)])
            self.write_state(debug, state)

            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), patch("urllib.request.urlopen", lambda request, timeout: FakeResponse()):
                decision = debug.deepseek_decide_agent(state)

            self.assertEqual(decision["recommended_intent"], "inspect_target")
            self.assertEqual(decision["recommended_action"]["tool"], "navigation_step")
            self.assertEqual(decision["recommended_action"]["args"], {"id": 10})
            self.assertFalse(debug.action_path.exists())

    def test_deepseek_targeted_explore_room_is_rejected(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {
                                            "id": "call_submit",
                                            "type": "function",
                                            "function": {
                                                "name": "submit_decision",
                                                "arguments": json.dumps(
                                                    {
                                                        "summary": "Explore one step toward object #74.",
                                                        "recommended_intent": "explore_room",
                                                        "recommended_action": {"tool": "exploration_step", "args": {"max_steps": 1}},
                                                        "evidence": {"target": {"id": 74}},
                                                        "risk": "Low.",
                                                        "alternatives": [],
                                                    }
                                                ),
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(objects=[make_touch_object(74)])
            self.write_state(debug, state)

            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), patch("urllib.request.urlopen", lambda request, timeout: FakeResponse()):
                decision = debug.deepseek_decide_agent(state)

            self.assertEqual(decision["recommended_intent"], "wait_runtime")
            self.assertEqual(decision["recommended_action"]["tool"], "no_op")
            self.assertEqual(decision["recommended_action"]["args"]["reason"], "unsupported_tool")
            self.assertEqual(decision["recommended_action"]["args"]["tool"], "exploration_step")
            self.assertFalse(debug.action_path.exists())
            log = debug.read_agent_log(limit=1)
            self.assertEqual(log[0]["decision"]["recommended_action"], {"tool": "no_op", "args": {"reason": "unsupported_tool", "tool": "exploration_step"}, "dry_run": True})

    def test_deepseek_untargeted_explore_room_is_rejected(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {
                                            "id": "call_submit",
                                            "type": "function",
                                            "function": {
                                                "name": "submit_decision",
                                                "arguments": json.dumps(
                                                    {
                                                        "summary": "Explore current room coverage.",
                                                        "recommended_intent": "explore_room",
                                                        "recommended_action": {"tool": "exploration_step", "args": {"max_steps": 1}},
                                                        "evidence": {},
                                                        "risk": "Low.",
                                                        "alternatives": [],
                                                    }
                                                ),
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()

            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), patch("urllib.request.urlopen", lambda request, timeout: FakeResponse()):
                decision = debug.deepseek_decide_agent(state)

            self.assertEqual(decision["recommended_intent"], "wait_runtime")
            self.assertEqual(decision["recommended_action"]["tool"], "no_op")
            self.assertEqual(decision["recommended_action"]["args"]["reason"], "unsupported_tool")
            self.assertEqual(decision["recommended_action"]["args"]["tool"], "exploration_step")
            self.assertFalse(debug.action_path.exists())
            log = debug.read_agent_log(limit=1)
            self.assertEqual(log[0]["decision"]["recommended_action"], {"tool": "no_op", "args": {"reason": "unsupported_tool", "tool": "exploration_step"}, "dry_run": True})


if __name__ == "__main__":
    unittest.main()
