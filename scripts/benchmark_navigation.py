"""Isolated navigation processing benchmark (no game, network, or live writes).

Run: .venv/bin/python -m scripts.benchmark_navigation --history runtime/exploration.json
The optional history file is copied into a temporary test runtime. Synthetic native
completions measure server gaps, not game movement speed or NPC capture rate.
"""

import argparse
import copy
import json
import math
import statistics
import tempfile
import threading
import time
from pathlib import Path

from palpal.debug_server import DebugState


def synthetic_state(width, height, count):
    objects = [dict(id=810 + i, x=(width - 8) * 16, y=(height - 8 - i * 2) * 8,
                    sprite_id=150, state=2, trigger_mode=2, trigger_script=14191,
                    auto_script=14202, blocking=True, interactable=True) for i in range(count)]
    cells = width * height
    bits = "f" * (cells // 4) + (format((1 << (cells % 4)) - 1, "x") if cells % 4 else "")
    return dict(frame=1, game_instance_id="benchmark", in_main_game=True, in_battle=False,
                scene_id=3, map_id=10, room={"key": "benchmark"}, party_direction=3,
                party_position={"x": 32, "y": 16}, objects=objects,
                nav_grid=dict(origin={"x": 0, "y": 0}, step={"x": 16, "y": 8},
                              width=width, height=height, bits=bits),
                agent_action=dict(generation=0, pending=False, remaining_steps=0, waiting_for_move=False))


def measure(width, height, count, samples, history, concurrent):
    with tempfile.TemporaryDirectory(prefix="palpal-navigation-benchmark-") as directory:
        root = Path(directory)
        debug = DebugState(root / "runtime", root / "assets", root / "game")
        if history is not None:
            debug.write_exploration(copy.deepcopy(history))
        state = synthetic_state(width, height, count)
        debug.set_live_state(state)
        debug.navigation_snapshot()
        debug.route_to_object(state, 810)
        stop = threading.Event()
        errors = []

        def render():
            try:
                while not stop.is_set():
                    debug.snapshot()
                    stop.wait(0.05)
            except Exception as error:
                errors.append(str(error))

        thread = threading.Thread(target=render)
        if concurrent:
            thread.start()
        gaps = []
        observation = []
        planning = []
        try:
            for index in range(samples + 2):
                # Start each measurement immediately after a compatible native completion.
                state["frame"] += 1
                state["party_position"] = {"x": (2 + index % 10) * 16, "y": (2 + index % 10) * 8}
                debug.set_live_state(state)
                completed = time.time()
                started = time.perf_counter()
                current = debug.navigation_snapshot()
                observed = time.perf_counter()
                result = debug.navigation_step(current, 810)
                finished = time.perf_counter()
                if result.get("status") != "stepping":
                    raise RuntimeError(result)
                if index >= 2:
                    gaps.append((result["action"]["time"] - completed) * 1000)
                    observation.append((observed - started) * 1000)
                    planning.append((finished - observed) * 1000)
                debug.action_path.unlink()
                state["agent_action"]["generation"] += 1
                state["party_position"] = result["expected_node"]
                debug.set_live_state(state)
        finally:
            stop.set()
            if concurrent:
                thread.join()
        if errors:
            raise RuntimeError(errors)
        ordered = sorted(gaps)
        return dict(grid=[width, height], targets=count, concurrent=concurrent, samples=samples,
                    gap_p50_ms=round(statistics.median(gaps), 2),
                    gap_p95_ms=round(ordered[math.ceil(len(ordered) * .95) - 1], 2),
                    gap_max_ms=round(max(gaps), 2),
                    observation_mean_ms=round(statistics.mean(observation), 2),
                    planning_and_persistence_mean_ms=round(statistics.mean(planning), 2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--concurrent", action="store_true")
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("--samples must be at least 2")
    history = json.loads(args.history.read_text()) if args.history else None
    for width, height, count in [(24, 32, 4), (118, 242, 26), (236, 242, 52)]:
        print(json.dumps(measure(width, height, count, args.samples, history, args.concurrent)), flush=True)


if __name__ == "__main__":
    main()
