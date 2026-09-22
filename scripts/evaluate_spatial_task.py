"""Evaluate a goal in isolated native games using the configured Agent providers.

All assets and saves are copied to temporary directories. This explicitly calls
external model/vision APIs. It never connects to the user-owned debug server.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from palpal.debug_server import DebugState


def trial(base, args):
    assets = base / 'assets'
    shutil.copytree(ROOT / 'data/pal', assets)
    shutil.copyfile(args.fixture, assets / '1.rpg')
    runtime = base / 'runtime'
    runtime.mkdir()
    debug = DebugState(runtime, assets, ROOT / 'vendor/sdlpal/unix/sdlpal')
    result = {'success': False, 'evidence': str(base)}
    process = None
    with (base / 'native.log').open('w') as log:
        try:
            debug.start_live_bridge()
            env = {**os.environ, 'PALPAL_AGENT': '1', 'PALPAL_RUNTIME_DIR': str(runtime),
                   'PALPAL_AGENT_SOCKET': str(runtime / 'live.sock'), 'PALPAL_SAVE_SLOT': '1',
                   'PALPAL_START_MODE': 'load', 'PALPAL_HEADLESS': '1', 'SDL_AUDIODRIVER': 'dummy'}
            process = subprocess.Popen([str(debug.game_binary)], cwd=assets, env=env, stdout=log, stderr=log)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not debug.snapshot().get('in_main_game'):
                time.sleep(.1)
            time.sleep(3)  # Native startup may publish state before its first playable frame.
            state = debug.snapshot()
            if not state.get('in_main_game'):
                raise RuntimeError('game_did_not_start')
            def amount(state):
                return sum(i.get('amount', 0) for i in state.get('inventory', []) if i.get('item_id') == args.success_item_id)
            if amount(state):
                raise RuntimeError('fixture_already_contains_success_item')
            cycle = debug.new_agent_tool_cycle(state)
            reviewed = debug.run_agent_tool('get_tasks', {'include_closed': True, 'detail': 'summary', 'limit': 100}, state, tool_cycle=cycle)
            task = debug.run_agent_tool('upsert_task', {'description': args.goal, 'rationale': '用户指定的隔离验收目标。',
                'priority': 100, 'review_token': reviewed['review_token']}, state, tool_cycle=cycle)
            if not task.get('ok'):
                raise RuntimeError(f'task_setup_failed:{task}')
            debug.agent_runtime.start(None, args.max_turns)
            deadline = time.monotonic() + args.timeout
            last_turn = None
            while time.monotonic() < deadline:
                state = debug.snapshot()
                session = debug.agent_runtime.snapshot()
                if amount(state) > 0:
                    result.update(success=True, inventory_before=0, inventory_after=amount(state))
                    break
                if session.get('turn') != last_turn:
                    last_turn = session.get('turn')
                    print(json.dumps({'trial': base.name, 'turn': last_turn, 'phase': session.get('phase')}), flush=True)
                if not debug.agent_runtime.is_running():
                    result['stop_reason'] = session.get('stop_reason') or session.get('phase')
                    break
                time.sleep(.5)
            else:
                result['stop_reason'] = 'evaluation_timeout'
            result['session'] = {k: debug.agent_runtime.snapshot().get(k) for k in ('model', 'turn', 'phase', 'stop_reason')}
        except Exception as exc:
            result['error'] = str(exc)
        finally:
            debug.agent_runtime.interrupt()
            future = debug.agent_runtime.run_future
            if future is not None:
                try:
                    future.result(timeout=10)
                except Exception:
                    pass
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            debug.stop_live_bridge()
    (base / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--goal', default='取得鹿茸，完成当前采药任务中的鹿茸目标。')
    parser.add_argument('--success-item-id', type=int, default=283)
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--max-turns', type=int, default=40)
    parser.add_argument('--timeout', type=int, default=1200)
    args = parser.parse_args()
    if not args.fixture.is_file() or args.runs < 1 or args.timeout < 1:
        parser.error('existing fixture and positive runs/timeout required')
    base = Path(tempfile.mkdtemp(prefix='palpal-spatial-evaluation-'))
    print(f'EVIDENCE {base}', flush=True)
    results = []
    for i in range(args.runs):
        directory = base / f'trial-{i + 1}'
        directory.mkdir()
        result = trial(directory, args)
        results.append(result)
        (base / 'report.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if all(r['success'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
