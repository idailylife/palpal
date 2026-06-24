import json
import os
import tempfile
import threading
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

    def pending_exploration_step(self, debug, state, visited=None):
        if visited is not None:
            self.seed_exploration_room(debug, state, visited)
        result = debug.exploration_step(state, max_steps=2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "stepping")
        try:
            debug.action_path.unlink()
        except FileNotFoundError:
            pass
        return result

    def wait_for_sweep(self, debug, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = debug.exploration_sweep_status()
            if not status["running"]:
                return status
            time.sleep(0.02)
        self.fail("sweep did not finish before timeout")

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

    def test_step_sends_straight_safe_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            corridor = {(0, 4), (1, 3), (2, 2), (3, 1), (4, 0)}
            blocked = {(x, y) for y in range(5) for x in range(5) if (x, y) not in corridor}
            state = make_state(width=5, height=5, start=(0, 4), blocked=blocked)
            self.write_state(debug, state)
            self.seed_exploration_room(debug, state, [(0, 4), (1, 3), (2, 2), (3, 1)])

            result = debug.exploration_step(state, max_steps=4)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "stepping")
            self.assertEqual(result["action"]["buttons"], ["up", "up", "up", "up"])
            self.assertEqual(result["segment_steps"], 4)
            self.assertEqual(result["segment_moves"], ["up", "up", "up", "up"])
            self.assertEqual(result["planned_moves"], ["up", "up", "up", "up"])
            self.assertEqual(result["target_frontier"], {"x": 64, "y": 0})
            self.assertEqual(result["route"]["expected_node"], {"grid_x": 4, "grid_y": 0, "x": 64, "y": 0})
            self.assertTrue(debug.action_path.exists())

    def test_exploration_prefers_far_continuous_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            corridor = {(i, 25 - i) for i in range(23)}
            corridor.add((1, 26))
            blocked = {(x, y) for y in range(27) for x in range(27) if (x, y) not in corridor}
            state = make_state(width=27, height=27, start=(0, 25), blocked=blocked, viewport={"x": 0, "y": 0})
            self.write_state(debug, state)
            self.seed_exploration_room(debug, state, [(i, 25 - i) for i in range(21)])

            result = debug.exploration_step(state, max_steps=3)

            self.assertTrue(result["ok"])
            self.assertEqual(result["target_selection"], "distant_exploration_goal")
            self.assertEqual(result["action"]["buttons"], ["up", "up", "up"])
            self.assertEqual(result["target_goal"], {"x": 352, "y": 24})
            self.assertEqual(result["remaining_steps"], 19)

    def test_exploration_step_continues_along_distant_corridor(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            corridor = {(i, 25 - i) for i in range(23)}
            blocked = {(x, y) for y in range(26) for x in range(23) if (x, y) not in corridor}
            state = make_state(width=23, height=26, start=(0, 25), blocked=blocked, viewport={"x": 0, "y": 0})
            self.write_state(debug, state)
            self.seed_exploration_room(debug, state, [(i, 25 - i) for i in range(21)])

            result = debug.exploration_step(state, max_steps=4)

            self.assertTrue(result["ok"])
            self.assertEqual(result["target_selection"], "distant_exploration_goal")
            self.assertEqual(result["segment_moves"], ["up", "up", "up", "up"])
            self.assertEqual(result["route"]["expected_node"], {"grid_x": 4, "grid_y": 21, "x": 64, "y": 168})

    def test_nearby_remaining_frontier_keeps_exploration_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            safe_nodes = {(0, 0), (1, 1), (2, 2), (0, 2), (2, 0)}
            blocked = {(x, y) for y in range(3) for x in range(3) if (x, y) not in safe_nodes}
            state = make_state(width=3, height=3, start=(1, 1), blocked=blocked)
            self.write_state(debug, state)
            visited = [node for node in safe_nodes if node != (0, 0)]
            self.seed_exploration_room(debug, state, visited)

            summary = debug.update_exploration(state)

            frontier = {(node["grid_x"], node["grid_y"]) for node in summary["nodes"]["frontier"]}
            self.assertFalse(summary["complete"])
            self.assertEqual(summary["explored"], 4)
            self.assertEqual(summary["explorable"], 5)
            self.assertIn((0, 0), frontier)

    def test_exploration_step_cleans_up_nearby_remaining_frontier(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            safe_nodes = {(0, 0), (1, 1), (2, 2), (0, 2), (2, 0)}
            blocked = {(x, y) for y in range(3) for x in range(3) if (x, y) not in safe_nodes}
            state = make_state(width=3, height=3, start=(1, 1), blocked=blocked)
            self.write_state(debug, state)
            visited = [node for node in safe_nodes if node != (0, 0)]
            self.seed_exploration_room(debug, state, visited)

            result = debug.exploration_step(state, max_steps=4)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "stepping")
            self.assertEqual(result["action"]["buttons"], ["left"])
            self.assertEqual(result["segment_steps"], 1)
            self.assertEqual(result["route"]["expected_node"], {"grid_x": 0, "grid_y": 0, "x": 0, "y": 0})

    def test_blocked_nearby_node_does_not_prevent_distant_goal(self):
        blocker = {
            "id": 74,
            "x": 16,
            "y": 40,
            "state": 2,
            "trigger_mode": 1,
            "trigger_script": 8149,
            "auto_script": 0,
            "blocking": True,
            "interactable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            corridor = {(0, 4), (1, 3), (2, 2), (3, 1), (4, 0), (1, 5)}
            blocked = {(x, y) for y in range(6) for x in range(5) if (x, y) not in corridor}
            state = make_state(width=5, height=6, start=(0, 4), objects=[blocker], blocked=blocked, viewport={"x": 0, "y": 32})
            self.write_state(debug, state)
            self.seed_exploration_room(debug, state, [(0, 4), (1, 3), (2, 2), (3, 1)])

            result = debug.exploration_step(state, max_steps=2)

            self.assertTrue(result["ok"])
            self.assertEqual(result["target_selection"], "distant_exploration_goal")
            self.assertEqual(result["action"]["buttons"], ["up", "up"])
            self.assertTrue(debug.action_path.exists())

    def test_viewport_does_not_limit_distant_exploration_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            corridor = {(0, 4), (1, 3), (2, 2), (3, 1), (4, 0), (1, 5)}
            blocked = {(x, y) for y in range(6) for x in range(5) if (x, y) not in corridor}
            state = make_state(width=5, height=6, start=(0, 4), blocked=blocked, viewport={"x": 1000, "y": 1000})
            self.write_state(debug, state)
            self.seed_exploration_room(debug, state, [(0, 4), (1, 3), (2, 2), (3, 1)])

            result = debug.exploration_step(state, max_steps=2)

            self.assertTrue(result["ok"])
            self.assertEqual(result["target_selection"], "distant_exploration_goal")
            self.assertEqual(result["action"]["buttons"], ["up", "up"])
            self.assertTrue(debug.action_path.exists())

    def test_no_viewport_uses_same_distant_goal_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            corridor = {(0, 4), (1, 3), (2, 2), (3, 1), (4, 0), (1, 5)}
            blocked = {(x, y) for y in range(6) for x in range(5) if (x, y) not in corridor}
            state = make_state(width=5, height=6, start=(0, 4), blocked=blocked)
            self.write_state(debug, state)
            self.seed_exploration_room(debug, state, [(0, 4), (1, 3), (2, 2), (3, 1)])

            result = debug.exploration_step(state, max_steps=2)

            self.assertTrue(result["ok"])
            self.assertEqual(result["target_selection"], "distant_exploration_goal")
            self.assertEqual(result["action"]["buttons"], ["up", "up"])
            self.assertEqual(result["target_goal"], {"x": 64, "y": 0})

    def test_partial_pending_exploration_progress_sticks_to_target_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            corridor = {(0, 4), (1, 3), (2, 2), (3, 1), (4, 0)}
            blocked = {(x, y) for y in range(5) for x in range(5) if (x, y) not in corridor}
            state = make_state(width=5, height=5, start=(1, 3), blocked=blocked)
            self.write_state(debug, state)

            identity = debug.room_identity(state)
            data = debug.empty_exploration()
            room = debug.exploration_room(data, identity)
            room["visited"] = ["0,4"]
            room["frontier"] = ["1,3"]
            room["last_route"] = {
                "nodes": [
                    {"grid_x": 0, "grid_y": 4, "x": 0, "y": 32},
                    {"grid_x": 1, "grid_y": 3, "x": 16, "y": 24},
                    {"grid_x": 2, "grid_y": 2, "x": 32, "y": 16},
                    {"grid_x": 3, "grid_y": 1, "x": 48, "y": 8},
                    {"grid_x": 4, "grid_y": 0, "x": 64, "y": 0},
                ],
                "start_node": {"grid_x": 0, "grid_y": 4, "x": 0, "y": 32},
                "expected_node": {"grid_x": 4, "grid_y": 0, "x": 64, "y": 0},
                "target_frontier": {"x": 64, "y": 0},
                "target_goal": {"x": 64, "y": 0},
                "pending": True,
                "sent_at": time.time() - 10,
            }
            debug.write_exploration(data)

            result = debug.exploration_step(state, max_steps=4)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "stepping")
            self.assertEqual(result["target_selection"], "sticky_exploration_goal")
            self.assertEqual(result["action"]["buttons"], ["up", "up", "up"])
            self.assertEqual(result["target_goal"], {"x": 64, "y": 0})
            self.assertTrue(result["route"]["pending"])
            room = debug.exploration_room(debug.read_exploration(), identity)
            self.assertEqual(room["last_route"]["start_node"], {"grid_x": 1, "grid_y": 3, "x": 16, "y": 24})

    def test_completed_segment_continues_to_sticky_goal_instead_of_far_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            main = {(0, 4), (1, 3), (2, 2), (3, 1), (4, 0)}
            branch = {(3, 3), (4, 4), (5, 5), (6, 6)}
            safe_nodes = main | branch
            blocked = {(x, y) for y in range(7) for x in range(7) if (x, y) not in safe_nodes}
            state = make_state(width=7, height=7, start=(2, 2), blocked=blocked)
            self.write_state(debug, state)

            identity = debug.room_identity(state)
            data = debug.empty_exploration()
            room = debug.exploration_room(data, identity)
            room["visited"] = ["0,4"]
            room["last_route"] = {
                "nodes": [
                    {"grid_x": 0, "grid_y": 4, "x": 0, "y": 32},
                    {"grid_x": 1, "grid_y": 3, "x": 16, "y": 24},
                    {"grid_x": 2, "grid_y": 2, "x": 32, "y": 16},
                ],
                "start_node": {"grid_x": 0, "grid_y": 4, "x": 0, "y": 32},
                "expected_node": {"grid_x": 2, "grid_y": 2, "x": 32, "y": 16},
                "target_frontier": {"x": 64, "y": 0},
                "target_goal": {"x": 64, "y": 0},
                "pending": True,
                "sent_at": time.time() - 10,
            }
            debug.write_exploration(data)

            result = debug.exploration_step(state, max_steps=4)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "stepping")
            self.assertEqual(result["target_selection"], "sticky_exploration_goal")
            self.assertEqual(result["action"]["buttons"], ["up", "up"])
            self.assertEqual(result["target_goal"], {"x": 64, "y": 0})
            room = debug.exploration_room(debug.read_exploration(), identity)
            self.assertIn("0,4", room["visited"])
            self.assertIn("1,3", room["visited"])
            self.assertIn("2,2", room["visited"])
            self.assertNotIn("3,1", room["visited"])
            self.assertNotIn("3,3", room["visited"])

    def test_deviated_pending_exploration_records_only_current_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            safe_nodes = {(0, 4), (1, 3), (2, 2), (3, 3)}
            blocked = {(x, y) for y in range(5) for x in range(5) if (x, y) not in safe_nodes}
            state = make_state(width=5, height=5, start=(3, 3), blocked=blocked)
            self.write_state(debug, state)

            identity = debug.room_identity(state)
            data = debug.empty_exploration()
            room = debug.exploration_room(data, identity)
            room["visited"] = ["0,4"]
            room["last_route"] = {
                "nodes": [
                    {"grid_x": 0, "grid_y": 4, "x": 0, "y": 32},
                    {"grid_x": 1, "grid_y": 3, "x": 16, "y": 24},
                    {"grid_x": 2, "grid_y": 2, "x": 32, "y": 16},
                ],
                "start_node": {"grid_x": 0, "grid_y": 4, "x": 0, "y": 32},
                "expected_node": {"grid_x": 2, "grid_y": 2, "x": 32, "y": 16},
                "target_frontier": {"x": 32, "y": 16},
                "target_goal": {"x": 32, "y": 16},
                "pending": True,
                "sent_at": time.time() - 10,
            }
            debug.write_exploration(data)

            result = debug.exploration_step(state, max_steps=4)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "position_deviated")
            room = debug.exploration_room(debug.read_exploration(), identity)
            self.assertIn("0,4", room["visited"])
            self.assertIn("3,3", room["visited"])
            self.assertNotIn("1,3", room["visited"])
            self.assertNotIn("2,2", room["visited"])
            self.assertFalse(room["last_route"]["pending"])
            self.assertEqual(
                room["last_route"]["position_deviated"]["current"],
                {"grid_x": 3, "grid_y": 3, "x": 48, "y": 24},
            )

    def test_interrupted_route_does_not_reuse_sticky_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            main = {(0, 4), (1, 3), (2, 2), (3, 1), (4, 0)}
            branch = {(3, 3), (4, 4), (5, 5), (6, 6)}
            safe_nodes = main | branch
            blocked = {(x, y) for y in range(7) for x in range(7) if (x, y) not in safe_nodes}
            state = make_state(width=7, height=7, start=(2, 2), blocked=blocked)
            self.write_state(debug, state)

            identity = debug.room_identity(state)
            data = debug.empty_exploration()
            room = debug.exploration_room(data, identity)
            room["visited"] = ["0,4", "1,3", "2,2"]
            room["last_route"] = {
                "nodes": [
                    {"grid_x": 0, "grid_y": 4, "x": 0, "y": 32},
                    {"grid_x": 1, "grid_y": 3, "x": 16, "y": 24},
                    {"grid_x": 2, "grid_y": 2, "x": 32, "y": 16},
                ],
                "start_node": {"grid_x": 0, "grid_y": 4, "x": 0, "y": 32},
                "expected_node": {"grid_x": 2, "grid_y": 2, "x": 32, "y": 16},
                "target_frontier": {"x": 64, "y": 0},
                "target_goal": {"x": 64, "y": 0},
                "pending": False,
                "interrupted_at": time.time() - 1,
                "interrupt": {"reason": "dialog"},
            }
            debug.write_exploration(data)

            result = debug.exploration_step(state, max_steps=4)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "stepping")
            self.assertEqual(result["target_selection"], "distant_exploration_goal")
            self.assertEqual(result["action"]["buttons"], ["right", "right", "right", "right"])
            self.assertEqual(result["target_goal"], {"x": 96, "y": 48})

    def test_completed_route_keeps_remaining_frontier_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            corridor = {(i, 6 - i) for i in range(7)}
            blocked = {(x, y) for y in range(7) for x in range(7) if (x, y) not in corridor}
            state = make_state(width=7, height=7, start=(0, 6), blocked=blocked, viewport={"x": 0, "y": 0})
            self.write_state(debug, state)
            self.seed_exploration_room(debug, state, [(0, 6), (1, 5), (2, 4), (3, 3)])

            result = debug.exploration_step(state, max_steps=4)
            self.assertTrue(result["ok"])
            self.assertEqual(result["route"]["expected_node"], {"grid_x": 4, "grid_y": 2, "x": 64, "y": 16})
            try:
                debug.action_path.unlink()
            except FileNotFoundError:
                pass

            moved = make_state(width=7, height=7, start=(4, 2), blocked=blocked, viewport={"x": 0, "y": 0})
            self.write_state(debug, moved)
            summary = debug.update_exploration(moved)
            visited = {(node["grid_x"], node["grid_y"]) for node in summary["nodes"]["visited"]}
            explorable = {(node["grid_x"], node["grid_y"]) for node in summary["nodes"]["explorable"]}
            frontier = {(node["grid_x"], node["grid_y"]) for node in summary["nodes"]["frontier"]}

            self.assertIn((4, 2), visited)
            self.assertNotIn((5, 1), visited)
            self.assertIn((5, 1), explorable)
            self.assertIn((5, 1), frontier)
            self.assertFalse(summary["complete"])

    def test_stuck_pending_exploration_marks_failed_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            corridor = {(0, 4), (1, 3), (2, 2), (3, 1), (4, 0)}
            blocked = {(x, y) for y in range(5) for x in range(5) if (x, y) not in corridor}
            state = make_state(width=5, height=5, start=(0, 4), blocked=blocked)
            self.write_state(debug, state)

            identity = debug.room_identity(state)
            data = debug.empty_exploration()
            room = debug.exploration_room(data, identity)
            room["visited"] = ["0,4"]
            room["frontier"] = ["1,3"]
            room["last_route"] = {
                "nodes": [
                    {"grid_x": 0, "grid_y": 4, "x": 0, "y": 32},
                    {"grid_x": 1, "grid_y": 3, "x": 16, "y": 24},
                    {"grid_x": 2, "grid_y": 2, "x": 32, "y": 16},
                ],
                "start_node": {"grid_x": 0, "grid_y": 4, "x": 0, "y": 32},
                "expected_node": {"grid_x": 2, "grid_y": 2, "x": 32, "y": 16},
                "target_frontier": {"x": 32, "y": 16},
                "pending": True,
                "sent_at": time.time() - 10,
            }
            debug.write_exploration(data)

            result = debug.exploration_step(state, max_steps=4)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "route_blocked")
            self.assertEqual(result["blocked_node"], {"grid_x": 1, "grid_y": 3, "x": 16, "y": 24})
            self.assertFalse(debug.action_path.exists())
            room = debug.exploration_room(debug.read_exploration(), identity)
            self.assertIn("1,3", room["failed_nodes"])
            self.assertFalse(room["last_route"]["pending"])

    def test_step_stops_segment_at_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            route_nodes = {(0, 4), (1, 3), (2, 4), (2, 2), (0, 2), (3, 5), (4, 6)}
            blocked = {(x, y) for y in range(7) for x in range(7) if (x, y) not in route_nodes}
            state = make_state(width=7, height=7, start=(0, 4), blocked=blocked)
            self.write_state(debug, state)
            self.seed_exploration_room(debug, state, [(0, 4), (1, 3), (2, 2), (0, 2)])

            result = debug.exploration_step(state, max_steps=4)

            self.assertTrue(result["ok"])
            self.assertEqual(result["action"]["buttons"], ["up"])
            self.assertEqual(result["segment_steps"], 1)
            self.assertEqual(result["planned_moves"][:2], ["up", "right"])
            self.assertEqual(result["route"]["expected_node"], {"grid_x": 1, "grid_y": 3, "x": 16, "y": 24})

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

    def test_action_pending_stops_exploration_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state()
            self.write_state(debug, state)
            debug.action_path.write_text("frames 1\nbutton up\n", encoding="ascii")

            result = debug.exploration_step(state)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "action_pending")

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

    def test_pending_exploration_is_interrupted_by_dialog(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(width=5, height=5, start=(0, 4))
            self.write_state(debug, state)
            self.pending_exploration_step(debug, state)

            state["dialog"] = {
                "active": True,
                "latest_event": {"text": "Hello.", "event_object_id": 42},
            }
            self.write_state(debug, state)
            result = debug.exploration_step(state)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "interrupted_by_dialog")
            self.assertEqual(result["interrupt"]["reason"], "dialog")
            self.assertEqual(result["interrupt"]["dialog_text"], "Hello.")
            self.assertEqual(result["interrupt"]["event_object_id"], 42)
            self.assertFalse(debug.action_path.exists())
            room = debug.exploration_room(debug.read_exploration(), debug.room_identity(state))
            self.assertFalse(room["last_route"]["pending"])

    def test_pending_exploration_is_interrupted_by_battle(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(width=5, height=5, start=(0, 4))
            self.write_state(debug, state)
            self.pending_exploration_step(debug, state)

            state["in_battle"] = True
            self.write_state(debug, state)
            result = debug.exploration_step(state)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "interrupted_by_battle")
            self.assertEqual(result["interrupt"]["reason"], "battle")
            self.assertFalse(debug.action_path.exists())
            room = debug.exploration_room(debug.read_exploration(), debug.room_identity(state))
            self.assertFalse(room["last_route"]["pending"])

    def test_pending_exploration_is_interrupted_by_room_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(width=5, height=5, start=(0, 4))
            self.write_state(debug, state)
            self.pending_exploration_step(debug, state)

            changed = make_state(width=5, height=5, start=(0, 4))
            changed["scene_id"] = 4
            changed["map_id"] = 11
            changed["room"] = {"key": "new-room"}
            self.write_state(debug, changed)
            result = debug.exploration_step(changed)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "interrupted_by_room_change")
            self.assertEqual(result["interrupt"]["reason"], "room_changed")
            self.assertEqual(result["interrupt"]["previous_scene_id"], 3)
            self.assertFalse(debug.action_path.exists())
            old_room = debug.read_exploration()["rooms"]["3/10/test-room"]
            self.assertFalse(old_room["last_route"]["pending"])

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

    def test_sweep_initial_status_is_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)

            status = debug.exploration_sweep_status()

            self.assertFalse(status["running"])
            self.assertIsNone(status["room_id"])
            self.assertEqual(status["steps_sent"], 0)
            self.assertEqual(status["segments_sent"], 0)

    def test_sweep_finishes_complete_room(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(width=1, height=1, start=(0, 0))
            self.write_state(debug, state)

            result = debug.start_exploration_sweep(state)
            status = self.wait_for_sweep(debug)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "started")
            self.assertFalse(status["running"])
            self.assertEqual(status["finished_reason"], "complete")
            self.assertEqual(status["segments_sent"], 0)
            self.assertEqual(status["steps_sent"], 0)

    def test_wait_for_exploration_segment_ignores_mid_route_position_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            start = make_state(width=4, height=4, start=(0, 0))
            mid = make_state(width=4, height=4, start=(1, 1))
            self.write_state(debug, mid)
            segment = {
                "route": {
                    "nodes": [
                        {"grid_x": 0, "grid_y": 0, "x": 0, "y": 0},
                        {"grid_x": 1, "grid_y": 1, "x": 16, "y": 8},
                        {"grid_x": 2, "grid_y": 2, "x": 32, "y": 16},
                    ],
                    "expected_node": {"grid_x": 2, "grid_y": 2, "x": 32, "y": 16},
                }
            }

            started = time.monotonic()
            result = debug.wait_for_exploration_segment(start, segment, timeout=0.16)

            self.assertIsNone(result)
            self.assertGreaterEqual(time.monotonic() - started, 0.12)

    def test_wait_for_exploration_segment_returns_after_expected_node_arrival(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            start = make_state(width=4, height=4, start=(0, 0))
            mid = make_state(width=4, height=4, start=(1, 1))
            expected = make_state(width=4, height=4, start=(2, 2))
            self.write_state(debug, mid)
            segment = {
                "route": {
                    "nodes": [
                        {"grid_x": 0, "grid_y": 0, "x": 0, "y": 0},
                        {"grid_x": 1, "grid_y": 1, "x": 16, "y": 8},
                        {"grid_x": 2, "grid_y": 2, "x": 32, "y": 16},
                    ],
                    "expected_node": {"grid_x": 2, "grid_y": 2, "x": 32, "y": 16},
                }
            }
            timer = threading.Timer(0.08, lambda: self.write_state(debug, expected))
            timer.start()

            started = time.monotonic()
            result = debug.wait_for_exploration_segment(start, segment, timeout=1.0)
            timer.join()

            self.assertIsNone(result)
            self.assertLess(time.monotonic() - started, 0.5)

    def test_wait_for_exploration_segment_times_out_after_consumed_unfinished_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            start = make_state(width=4, height=4, start=(0, 0))
            mid = make_state(width=4, height=4, start=(1, 1))
            self.write_state(debug, mid)
            debug.action_path.write_text("frames 1\nbutton down\n", encoding="ascii")
            segment = {
                "route": {
                    "nodes": [
                        {"grid_x": 0, "grid_y": 0, "x": 0, "y": 0},
                        {"grid_x": 1, "grid_y": 1, "x": 16, "y": 8},
                        {"grid_x": 2, "grid_y": 2, "x": 32, "y": 16},
                    ],
                    "expected_node": {"grid_x": 2, "grid_y": 2, "x": 32, "y": 16},
                }
            }
            timer = threading.Timer(0.08, debug.action_path.unlink)
            timer.start()

            result = debug.wait_for_exploration_segment(start, segment, timeout=0.16)
            timer.join()

            self.assertIsNone(result)

    def test_normalize_agent_decision_accepts_exploration_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)

            decision = debug.normalize_agent_decision(
                {
                    "summary": "Cover the remaining room frontier.",
                    "recommended_intent": "explore_room",
                    "recommended_action": {"tool": "exploration_sweep", "args": {"max_steps": 99}},
                    "evidence": {},
                    "risk": "Sweep stays in the current room.",
                    "alternatives": [],
                }
            )

            self.assertEqual(decision["recommended_action"], {"tool": "exploration_sweep", "args": {}, "dry_run": True})
            self.assertIn("normalization_warnings", decision["evidence"])

    def test_normalize_agent_decision_does_not_retarget_sweep_from_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)

            decision = debug.normalize_agent_decision(
                {
                    "summary": "Object 25 was just interacted with; sweep the remaining room.",
                    "recommended_intent": "explore_room",
                    "recommended_action": {"tool": "exploration_sweep", "args": {}},
                    "evidence": {},
                    "risk": "Sweep stays in the current room.",
                    "alternatives": [],
                }
            )

            self.assertEqual(decision["recommended_intent"], "explore_room")
            self.assertEqual(decision["recommended_action"], {"tool": "exploration_sweep", "args": {}, "dry_run": True})
            self.assertNotIn("normalization_warnings", decision["evidence"])

    def test_rules_agent_prefers_sweep_for_frontier_only_room(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug = self.make_debug_state(tmp)
            state = make_state(width=3, height=3, start=(1, 1), objects=[])
            self.write_state(debug, state)
            state["_exploration"] = debug.update_exploration(state)

            decision = debug.rules_decide_agent(state)

            self.assertEqual(decision["recommended_intent"], "explore_room")
            self.assertEqual(decision["recommended_action"], {"tool": "exploration_sweep", "args": {}, "dry_run": True})

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
            debug.record_transition_destination(from_identity, 52, debug.room_identity(to_state))

            navigation = debug.run_agent_tool("get_navigation", {"mode": "full"}, state)["navigation"]
            target_status = {target["id"]: target for target in navigation["targets"]}
            route = debug.run_agent_tool("get_route", {"id": 52}, state)["route"]
            known = debug.run_agent_tool("get_known_transitions", {}, state)

            self.assertTrue(target_status[52]["known_destination"])
            self.assertEqual(target_status[52]["known_destination_room_id"], "4/11/next-room")
            self.assertTrue(route["known_destination"])
            self.assertEqual(route["known_destination_room_id"], "4/11/next-room")
            self.assertEqual(known["known_transitions"]["52"]["to_room_id"], "4/11/next-room")

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

            self.assertEqual(decision["recommended_intent"], "explore_room")
            self.assertEqual(decision["recommended_action"]["tool"], "exploration_step")
            self.assertEqual(decision["recommended_action"]["args"], {})
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

    def test_agent_normalizes_explore_room_to_default_exploration_step(self):
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

            self.assertEqual(decision["recommended_action"]["tool"], "exploration_step")
            self.assertEqual(decision["recommended_action"]["args"], {})
            self.assertFalse(debug.action_path.exists())

    def test_agent_ignores_model_exploration_step_count(self):
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

            self.assertEqual(decision["recommended_action"]["tool"], "exploration_step")
            self.assertEqual(decision["recommended_action"]["args"], {})
            self.assertIn("ignored agent-provided exploration_step args", decision["evidence"]["normalization_warnings"])
            self.assertFalse(debug.action_path.exists())

    def test_agent_keeps_targeted_explore_room_as_exploration_step(self):
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

            self.assertEqual(decision["recommended_intent"], "explore_room")
            self.assertEqual(decision["recommended_action"]["tool"], "exploration_step")
            self.assertEqual(decision["recommended_action"]["args"], {})
            self.assertEqual(decision["evidence"]["normalization_warnings"], ["ignored agent-provided exploration_step args"])
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
            self.assertEqual(decision["alternatives"][1]["intent"], "explore_room")
            self.assertEqual(decision["alternatives"][1]["action"]["tool"], "exploration_step")
            self.assertEqual(decision["alternatives"][1]["action"]["args"], {})
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
            names = [tool["function"]["name"] for tool in debug.agent_tool_specs()]

            self.assertEqual(
                set(names),
                {
                    "get_state_summary",
                    "get_navigation",
                    "get_dialogs",
                    "get_events",
                    "get_agent_history",
                    "get_route",
                    "get_room_exploration",
                    "get_known_transitions",
                    "submit_decision",
                },
            )
            self.assertEqual(names[-1], "submit_decision")

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

            self.assertEqual(decision["recommended_intent"], "wait_runtime")
            self.assertEqual(decision["recommended_action"]["tool"], "no_op")
            self.assertEqual(decision["recommended_action"]["args"]["reason"], "unsupported_tool")
            self.assertEqual(decision["recommended_action"]["args"]["tool"], "write_action")
            self.assertFalse(debug.action_path.exists())

    def test_deepseek_targeted_explore_room_keeps_exploration_step(self):
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

            self.assertEqual(decision["recommended_intent"], "explore_room")
            self.assertEqual(decision["recommended_action"]["tool"], "exploration_step")
            self.assertEqual(decision["recommended_action"]["args"], {})
            self.assertFalse(debug.action_path.exists())
            log = debug.read_agent_log(limit=1)
            self.assertEqual(log[0]["decision"]["recommended_action"], {"tool": "exploration_step", "args": {}, "dry_run": True})

    def test_deepseek_untargeted_explore_room_keeps_empty_exploration_step(self):
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

            self.assertEqual(decision["recommended_intent"], "explore_room")
            self.assertEqual(decision["recommended_action"]["tool"], "exploration_step")
            self.assertEqual(decision["recommended_action"]["args"], {})
            self.assertFalse(debug.action_path.exists())
            log = debug.read_agent_log(limit=1)
            self.assertEqual(log[0]["decision"]["recommended_action"], {"tool": "exploration_step", "args": {}, "dry_run": True})


if __name__ == "__main__":
    unittest.main()
