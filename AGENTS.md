# AGENTS.md

This file is for coding agents and future maintainers working on `palpal`.

## Project Goal

Expose the DOS *Legend of Sword and Fairy* game state and controls so an LLM
agent can automate play while a human can inspect the live game, state, and
actions in a browser.

The current implementation uses a patched SDLPAL runtime plus a small local web
debug server. Prefer extending this path before introducing another emulator or
automation layer.

## Important Paths

- `vendor/sdlpal/`: vendored SDLPAL source.
- `vendor/sdlpal/agent.c`: native file bridge for state, screenshots, and actions.
- `vendor/sdlpal/agent.h`: native bridge declarations.
- `vendor/sdlpal/game.c`: patched main game loop for agent mode.
- `vendor/sdlpal/input.c`: patched input loop so action files work during wait loops.
- `vendor/sdlpal/main.c`: patched startup to skip splash/menu in agent mode.
- `palpal/debug_server.py`: HTTP/WebSocket debug server.
- `web/`: browser debug UI.
- `scripts/fetch_assets.py`: local-only asset fetch helper.
- `run`: project command wrapper for assets/build/debug/game/dev.
- `CMakeLists.txt`: native CMake build for the patched SDLPAL target and local helper targets.
- `runtime/`: generated state/action/screenshot files; gitignored.
- `data/pal/`: local game resources; gitignored.

## Non-Negotiable Constraints

- Do not commit game resources, generated runtime files, object files, binaries,
  or save files.
- Keep the SDLPAL patch narrow and easy to audit.
- Preserve the file bridge unless there is a clear reason to replace it with an
  in-process API.
- Treat object labels/categories as annotations, not as facts provided by the
  engine.
- Do not assume `(scene_id, map_id)` uniquely identifies a room.
- Avoid large refactors inside SDLPAL unless they are required for state/control
  exposure.

## Build And Run

Fetch local resources:

```sh
./run assets
```

Build SDLPAL:

```sh
./run build
```

Run the debug server and game together:

```sh
./run
```

`./run dev` is an explicit alias for the same workflow. The game runs in the
background and the debug server stays attached to the terminal.

Run only the debug server, for troubleshooting:

```sh
./run debug
```

Run only the game in agent mode, for troubleshooting:

```sh
./run game
```

Run both together explicitly:

```sh
./run dev
```

Open:

```text
http://127.0.0.1:8765
```

## Native Agent Bridge

Agent mode is enabled by environment variables:

```sh
PALPAL_AGENT=1
PALPAL_RUNTIME_DIR=/absolute/path/to/runtime
PALPAL_AGENT_SOCKET=/absolute/path/to/runtime/live.sock
PALPAL_SAVE_SLOT=0
```

The native bridge streams over `live.sock`:

- structured JSON state snapshots.
- indexed 320x200 frames with their 256-color palette.

The native bridge consumes:

- `action.txt`: line-oriented action request.

Action format:

```txt
frames 1
button search
button up
```

`agent.c` is responsible for parsing actions, writing JSON manually, and
non-blocking Unix-socket transport. Keep this dependency-free unless there is a
strong reason to add a C JSON or image library. The Python server encodes
indexed frames as PNG for the browser.

## State Exposure Priorities

Current priority is map exploration and debugging:

- party position and direction
- scene/map/region
- party stats
- inventory
- event objects
- interactable/blocking flags
- trigger and auto script ids
- local walkability

Next useful extensions:

- battle UI state and enemy/player action state
- stable object annotation table
- script opcode tracing
- save/load/reset endpoints wired through the native bridge
- richer region graph for navigation

## Verification Checklist

Run after native changes:

```sh
./run build
```

Run after Python/server changes:

```sh
PYTHONPYCACHEPREFIX=.pycache python3 -m py_compile palpal/debug_server.py scripts/fetch_assets.py
```

Run Python tests:

```sh
.venv/bin/python -m pytest
.venv/bin/python -m pytest --cov
```

Smoke test the server:

```sh
./run debug
curl http://127.0.0.1:8765/state
curl -X POST http://127.0.0.1:8765/action \
  -H 'Content-Type: application/json' \
  -d '{"buttons":["search"],"frames":1}'
```

When checking the debug server from an agent sandbox, `curl` may fail with HTTP
code `000` or exit code `7` even while the browser UI is working. Confirm the
server is listening first:

```sh
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

If the listener exists, retry the read-only localhost request with the agent's
permission escalation mechanism before concluding the debug server is down.

Smoke test native streaming:

```sh
./run dev
curl http://127.0.0.1:8765/state
curl http://127.0.0.1:8765/frame.png -o /tmp/palpal-frame.png
test ! -e runtime/state.json
test ! -e runtime/frame.rgba
```

Stop any test game/debug processes before finishing work.

## Common Pitfalls

- SDLPAL wait loops often call `PAL_ProcessEvent()` instead of returning to the
  main frame loop. Keep action polling available there.
- `screen.bmp` may be written before the game has advanced to a normal frame;
  this is still useful for early debug visibility.
- SDL2 and SDL3 return values differ for some functions. Check compatibility
  wrappers before changing native code.
- Object names like "door", "cabinet", or "monster" are not first-class engine
  fields. Infer carefully and expose confidence/annotations separately.
- Build artifacts under `vendor/sdlpal` are noisy. Do not treat generated `.o`,
  `.d`, or `sdlpal` binary files as source changes.
