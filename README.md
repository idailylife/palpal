# palpal

`palpal` is a local automation and debugging harness for the DOS version of
*Legend of Sword and Fairy* / *仙剑奇侠传*. It uses a patched SDLPAL runtime to
expose game state, accept semantic controls, and show a browser-based debug
console for humans and LLM agents.

## What This Provides

- Native SDLPAL agent mode that exports live game state to `runtime/state.json`.
- Latest rendered game frame exported to `runtime/screen.bmp`.
- File-based action injection through `runtime/action.txt`.
- HTTP endpoints for state and actions.
- WebSocket stream for live debug updates.
- Browser debug UI with game frame, state summary, object list, and raw JSON.
- Local-only asset fetch helper for DOS game resources.

Game resources are not committed to this repository. They are fetched or copied
only into gitignored local directories.

## Requirements

- macOS or Linux shell environment.
- `make`
- `python3`
- `git`
- SDL3 development package available to `pkg-config sdl3`

On macOS with Homebrew, SDL3 can usually be installed with:

```sh
brew install sdl3
```

## Quick Start

Fetch local game resources:

```sh
make assets
```

Build the patched SDLPAL binary:

```sh
make build-game
```

Start the debug server:

```sh
make debug
```

In another terminal, start the game in agent mode:

```sh
make run-game
```

Open:

```text
http://127.0.0.1:8765
```

The SDLPAL window is still the authoritative running game. The browser console
mirrors the latest exported frame, live state JSON, map objects, and recent
actions.

## Runtime Files

The native agent bridge is intentionally file-based for now. This keeps the
patch to SDLPAL small and easy to inspect.

SDLPAL writes:

- `runtime/state.json`: live game state snapshot.
- `runtime/screen.bmp`: latest exported 320x200 frame.

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

Live stream:

```text
ws://127.0.0.1:8765/stream
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
make assets        # fetch local gitignored DOS resources
make build-game    # build vendor/sdlpal/unix/sdlpal
make debug         # run browser debug server on 127.0.0.1:8765
make run-game      # run SDLPAL with PALPAL_AGENT=1
make clean-runtime # remove runtime files
```

## Licensing Notes

SDLPAL is GPL-3.0. This project vendors and modifies SDLPAL, so changes to the
runtime should be treated as GPL-3.0 compatible work.

The DOS game resources are not licensed by this repository. Keep resources in
gitignored local paths such as `data/pal/`, and do not commit or redistribute
them from this project.

