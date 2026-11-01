"""Isolated native checkpoint verification using a read-only source save.

Run from the project root:
    python -m scripts.checkpoint_smoke --game-save /path/to/checkpoint/game.rpg
All resources and save slots used by the test are copied into a temporary folder.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from palpal.debug_server import DebugState

ROOT = Path(__file__).resolve().parents[1]

def wait_until(call, timeout=15):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = call()
        if value:
            return value
        time.sleep(.03)
    raise TimeoutError('native smoke condition timed out')

def main(game_save=None, *, checkpoint=None, verify_only=False):
    with tempfile.TemporaryDirectory(prefix='pcp-', dir='/tmp') as tmp:
        base = Path(tmp)
        assets = base/'assets'; assets.mkdir()
        for path in (ROOT/'data/pal').iterdir():
            if path.is_file() and path.suffix.lower() != '.rpg':
                shutil.copy2(path, assets/path.name)
        # Override copied configuration so save paths can only point at this fixture.
        (assets / 'sdlpal.cfg').write_text(f"GamePath={assets}\nSavePath={assets}\n")
        runtime = base/'runtime'; runtime.mkdir()
        os.environ['PALPAL_AGENT_SOCKET'] = str(runtime/'live.sock')
        os.environ['PALPAL_CHECKPOINT_SLOT'] = '99'
        d = DebugState(runtime, assets, ROOT/'vendor/sdlpal/unix/sdlpal')
        d.start_live_bridge()
        cp = d.checkpoint_dir('native-source')
        if checkpoint is not None:
            shutil.copytree(checkpoint, cp)
        else:
            (cp/'companion').mkdir(parents=True)
            shutil.copy2(game_save, cp/'game.rpg')
            (cp/'companion/exploration.json').write_text(json.dumps(d.empty_exploration()))
            (cp/'manifest.json').write_text(json.dumps({'version':2,'companion_version':11}))
            d.agent_runtime.export_checkpoint(cp)
        env = dict(os.environ, PALPAL_AGENT='1', PALPAL_HEADLESS='1', PALPAL_START_MODE='wait',
                   PALPAL_RUNTIME_DIR=str(runtime), PALPAL_SAVE_SLOT='0',
                   PALPAL_GAME_INSTANCE_ID='isolated-checkpoint-smoke', SDL_AUDIODRIVER='dummy')
        game_log = (base/'game.log').open('wb')
        game = subprocess.Popen([str(d.game_binary)], cwd=assets, env=env, stdout=game_log, stderr=subprocess.STDOUT)
        try:
            wait_until(lambda: d.read_live_state().get('status') == 'startup_waiting')
            loaded = d.load_checkpoint('native-source')
            assert loaded['native_result']['status'] == 'loaded', loaded
            wait_until(lambda: d.read_live_state().get('in_main_game') and not d.read_live_state().get('entering_scene'))
            wait_until(lambda: not d.action_pending())
            if checkpoint is not None:
                expected = json.loads((cp/'companion/exploration.json').read_text())
                actual = d.read_exploration()
                assert actual['room_memos'] == expected['room_memos']
                assert actual['tasks'] == expected['tasks']
                expected_state = json.loads((cp/'manifest.json').read_text())['state']
                assert d.read_live_state()['scene_id'] == expected_state['scene_id']
                assert d.read_live_state()['map_id'] == expected_state['map_id']
                print(json.dumps({'checkpoint':checkpoint.name, 'native_load':'passed',
                                  'memos':len(actual['room_memos']), 'tasks':len(actual['tasks']),
                                  'scene_id':expected_state['scene_id'], 'map_id':expected_state['map_id']}), flush=True)
            if verify_only:
                return
            state = d.snapshot()
            memo = d.run_agent_tool('upsert_room_memo', {'text':'isolated native checkpoint memo'}, state)
            assert memo['ok']
            memo_room_id = memo['room_id']
            saved = d.save_checkpoint('roundtrip')
            assert saved['checkpoint']['native_result']['status'] == 'saved'
            wait_until(lambda: not d.action_pending())
            state = d.snapshot()
            d.run_agent_tool('upsert_room_memo', {'text':'later annotation'}, state)
            loaded = d.load_checkpoint('roundtrip')
            assert loaded['native_result']['status'] == 'loaded'
            assert d.room_memo(memo_room_id)['text'] == 'isolated native checkpoint memo'
            print(json.dumps({'native_roundtrip':'passed', 'save_status':saved['checkpoint']['native_result']['status'],
                              'load_status':loaded['native_result']['status'], 'memos':len(d.read_exploration()['room_memos'])}), flush=True)
            # Native write failure: an existing slot directory cannot be replaced.
            wait_until(lambda: not d.action_pending())
            d.checkpoint_native_control('pause')
            d.save_slot_path().unlink()
            d.save_slot_path().mkdir()
            try:
                d.run_slot_command('save_slot', 99)
            except RuntimeError as error:
                assert str(error) == 'save_io_error', str(error)
                print(json.dumps({'native_save_failure':'correctly rejected', 'error':str(error)}), flush=True)
            else:
                raise AssertionError('native save failure was reported as success')
            # A malformed native save must be rejected without loading defaults.
            scene = d.read_live_state()['scene_id']
            try:
                d.run_slot_command('load_slot', 99)
            except RuntimeError as error:
                assert str(error) == 'invalid_save', str(error)
                assert d.read_live_state()['scene_id'] == scene
                print(json.dumps({'native_bad_load':'correctly rejected', 'scene_preserved':scene}), flush=True)
            else:
                raise AssertionError('invalid native save was loaded')
        except BaseException:
            game_log.flush()
            print('NATIVE LOG', (base/'game.log').read_text(errors='replace')[-2500:])
            print('STATE', json.dumps(d.read_live_state(),ensure_ascii=False)[:1000])
            for name in ('action.txt','action_result.json','checkpoint_control.txt','checkpoint_control_result.json','checkpoint_operation.json'):
                p=runtime/name
                print(name, p.read_text()[:1500] if p.exists() else '<absent>')
            raise
        finally:
            game.terminate()
            try: game.wait(5)
            except subprocess.TimeoutExpired:
                game.kill(); game.wait(5)
            game_log.close()
            d.stop_live_bridge()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--game-save', type=Path)
    source.add_argument('--checkpoint', type=Path)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    main(args.game_save.resolve() if args.game_save else None,
         checkpoint=args.checkpoint.resolve() if args.checkpoint else None,
         verify_only=args.verify_only)
