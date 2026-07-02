# palpal

`palpal` is a local automation and debugging harness for the DOS version of
*Legend of Sword and Fairy* / *仙剑奇侠传*. It uses a patched SDLPAL runtime to
expose game state, accept semantic controls, and show a browser-based debug
console for humans and LLM agents.

## What This Provides

- Native SDLPAL agent mode that exports live game state to `runtime/state.json`.
- Latest rendered game frame exported to `runtime/frame.rgba`.
- File-based action injection through `runtime/action.txt`.
- HTTP endpoints for state and actions.
- WebSocket stream for live debug updates.
- Browser debug UI with game frame, state summary, object list, and raw JSON.
- Local-only asset fetch helper for DOS game resources.

Game resources are not committed to this repository. They are fetched or copied
only into gitignored local directories.

## Requirements

- macOS or Linux shell environment.
- `cmake`
- `python3`
- `node` and Google Chrome for `./run visual-smoke`
- `git`
- C/C++ compiler toolchain.
- `pkg-config`
- SDL3 development package available to `pkg-config sdl3`

On macOS with Homebrew, the native requirements can usually be installed with:

```sh
brew install cmake pkg-config sdl3
```

## Quick Start

Fetch local game resources:

```sh
./run assets
```

Build the patched SDLPAL binary with CMake:

```sh
./run build
```

Start the headless debug server and game together:

```sh
./run
```

`./run dev` is kept as an explicit alias. The game runs in the background while
the browser debug server stays attached to the terminal. Press `Ctrl+C` to stop
both processes.

For debugging, you can still run them in separate terminals:

```sh
./run debug
./run game
```

Open:

```text
http://127.0.0.1:8765
```

The native SDLPAL runtime remains the authoritative running game. The browser
console mirrors the latest exported frame, live state JSON, map objects, and
recent actions.

By default, `./run`, `./run dev`, and `./run game` use true headless agent mode
and do not create an SDL game window. Use `PALPAL_HEADLESS=0 ./run game` when
you explicitly want a visible SDL window for video debugging.

## CMake

```sh
cmake -S . -B build
cmake --build build --target sdlpal
```

Useful CMake targets:

```sh
cmake --build build --target assets
cmake --build build --target debug
cmake --build build --target game
cmake --build build --target dev
cmake --build build --target clean-runtime
```

`./run build` is just the shorter form of configuring `build/` and building the
`sdlpal` target. The generated executable is written to
`vendor/sdlpal/unix/sdlpal`.

Compiler warnings for the vendored SDLPAL tree are quiet by default. To audit
them explicitly, reconfigure with strict warnings:

```sh
cmake -S . -B build -DPALPAL_STRICT_WARNINGS=ON
cmake --build build --target sdlpal
```

## Runtime Files

The native agent bridge is intentionally file-based for now. This keeps the
patch to SDLPAL small and easy to inspect.

SDLPAL writes:

- `runtime/state.json`: live game state snapshot.
- `runtime/frame.rgba`: latest exported 320x200 RGBA frame.

The debug server writes:

- `runtime/action.txt`: next action for SDLPAL to consume.

Example action file:

```txt
frames 3
button up
button search
```

Supported buttons:

```text
up down left right menu search page_up page_down repeat auto defend
use_item throw_item flee status force home end
```

## HTTP API

Get current state:

```sh
curl http://127.0.0.1:8765/state
```

Send an action:

```sh
curl -X POST http://127.0.0.1:8765/action \
  -H 'Content-Type: application/json' \
  -d '{"buttons":["up"],"frames":2}'
```

`/action` writes one pending `runtime/action.txt` command at a time. If SDLPAL
has not consumed the previous action yet, the server returns `400` with
`action_pending` instead of overwriting it.

Live stream:

```text
ws://127.0.0.1:8765/stream
```

Live raw frame stream:

```text
ws://127.0.0.1:8765/frames
```

Reserved but not implemented yet:

- `POST /reset`
- `POST /save`
- `POST /load`

## State Model

The exported state currently focuses on map exploration:

- frame and mode flags
- `scene_id`, `map_id`, and `region_key`
- viewport and party position
- party members, HP/MP, level, and money
- inventory entries
- current scene event objects
- nearby blocked map tiles

Do not treat `(scene_id, map_id)` as a unique room id. A location should be
identified by scene, map, coordinates/region, and relevant event/story state.

Objects such as NPCs, doors, monsters, cabinets, and trigger points are exposed
as raw event objects. The engine can reliably expose coordinates, sprite ids,
trigger scripts, blocking state, and interaction state. Human-readable labels
and semantic categories should be added as a separate annotation layer.

## Useful Commands

```sh
./run assets # fetch local gitignored DOS resources
./run build  # configure and build vendor/sdlpal/unix/sdlpal with CMake
./run        # run browser debug server and game together
./run debug  # run browser debug server on 127.0.0.1:8765
./run game   # run SDLPAL with PALPAL_AGENT=1
./run dev    # explicit alias for ./run
./run visual-smoke  # verify the browser canvas renders live raw frames
./run clean  # remove runtime files
.venv/bin/python -m pytest      # run Python tests
.venv/bin/python -m pytest --cov  # run tests with coverage
```

`./run visual-smoke` starts the headless game and debug server, opens the UI in
headless Chrome through the Chrome DevTools Protocol, verifies that both
WebSocket streams are live, and checks that the frame canvas has non-black
pixels. It stops the game and server before exiting.

## Licensing Notes

SDLPAL is GPL-3.0. This project vendors and modifies SDLPAL, so changes to the
runtime should be treated as GPL-3.0 compatible work.

The DOS game resources are not licensed by this repository. Keep resources in
gitignored local paths such as `data/pal/`, and do not commit or redistribute
them from this project.
