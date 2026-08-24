# palpal

`palpal` is a local automation and debugging harness for the DOS version of
*Legend of Sword and Fairy* / *仙剑奇侠传*. It uses a patched SDLPAL runtime to
expose game state, accept semantic controls, and show a browser-based debug
console for humans and LLM agents.

## What This Provides

- Native SDLPAL agent mode that streams live game state over a local Unix socket.
- Indexed 320x200 frames encoded as PNG by the debug server.
- File-based action injection and completion results through
  `runtime/action.txt` and `runtime/action_result.json`.
- HTTP endpoints for state, actions, navigation, inventory/equipment,
  checkpoints, and persistent Agent sessions.
- Battle state exposure plus a dedicated in-battle Agent that picks legal
  actions during fights, with a conservative default policy when no model key
  is configured.
- WebSocket streams for live state, frames, and Agent events.
- Browser debug UI with the game frame, state and object inspection,
  navigation, equipment, checkpoints, Agent timeline/goals, and raw JSON.
- Local-only asset fetch helper for DOS game resources.

Game resources are not committed to this repository. They are fetched or copied
only into gitignored local directories.

## Requirements

- macOS or Linux shell environment.
- `cmake`
- Python 3.10 or newer
- `node` and Google Chrome for `./run visual-smoke`
- `git`
- C/C++ compiler toolchain.
- `pkg-config`
- SDL3 development package available to `pkg-config sdl3`

On macOS with Homebrew, the native requirements can usually be installed with:

```sh
brew install cmake pkg-config sdl3
```

Install the Python server, Agents SDK, and development dependencies in a
Python 3.10+ virtual environment:

```sh
python3 -m pip install -e '.[dev]'
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

## Runtime Data

High-frequency state and frame data use `runtime/live.sock` by default. The
socket carries JSON state and the game's native indexed pixels plus palette;
the payload is never written to a regular file. Set `PALPAL_AGENT_SOCKET` when
the default path is unsuitable. SDLPAL reconnects automatically when the debug
server starts or restarts.

The native bridge and debug server keep lower-frequency coordination and durable
data under `runtime/`:

- `action.txt`: next action for SDLPAL to consume.
- `action_result.json`: native completion details for item, equipment, shop,
  checkpoint, and battle actions.
- `exploration.json`, `events.json`, and `dialog_history.json`: learned map,
  room-scoped Agent memos, state-event, and dialog history.
- `logs/`: game, debug-server, and navigation diagnostics.
- `agent/`: persistent Agent session metadata, activity events, SDK transcript,
  and archived tool outputs.
- `checkpoints/`: local game saves plus their matching exploration and Agent
  session bundles.

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

## Core HTTP API

The following are the main public endpoints; the browser UI also uses
specialized diagnostics and history endpoints.

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

Read a frame or navigation state:

```text
GET  /frame.png
GET  /navigation
GET  /navigation/route?id=123
GET  /navigation/transitions
POST /navigation/step          {"id":123}
```

Room exploration reports `visited`/`explored`, `explorable`, and `frontier`
geometry under the stable `logical_room_id`. A zero `frontier` means only that
no adjacent unvisited grid node is currently known; it is not a dead-end or
story-completion signal. Directed transitions retain total traversal counts,
last traversal time, recent-backtrack evidence, and up to 16 active-task
structure contexts. Agent navigation derives `traversal_context.status` from
that evidence instead of assigning a geometric destination priority. The
context changes when active task identity, description, priority, status, or
object/transition/room binding changes; rationale-only reviews and dismissed or
duplicate dialog evidence do not reopen every route.

Use or equip an inventory item:

```text
POST /inventory/use            {"item_id":123,"role_id":0}
POST /inventory/use            {"item_id":123,"target":"all"}
POST /inventory/equip          {"item_id":123,"role_id":0}
```

Manage local checkpoints:

```text
GET    /checkpoints
POST   /checkpoints/save       {"label":"before boss"}
POST   /checkpoints/load       {"id":"checkpoint-id"}
DELETE /checkpoints/checkpoint-id
POST   /startup/new
```

Live stream:

```text
ws://127.0.0.1:8765/stream
```

Live PNG frame stream:

```text
ws://127.0.0.1:8765/frames
```

### Persistent Agent Session

The Web UI's Agent tab runs a persistent, server-side loop that chooses useful
local actions from current state, exploration value, and maintained tasks. Only
one session can control the game at a time. The exploration Agent and the
in-battle Agent run on the OpenAI Agents SDK over DeepSeek's OpenAI-compatible
Responses endpoint. The debug server needs `DEEPSEEK_API_KEY` to start an Agent
session or delegate battle commands; `DEEPSEEK_BASE_URL` defaults to
`https://api.deepseek.com`, `DEEPSEEK_MODEL` to `deepseek-v4-pro` (allowed
`deepseek-v4-pro` or `deepseek-v4-flash`), and `DEEPSEEK_TIMEOUT` to 90 seconds.
The debug server also requires `POE_API_KEY` at startup so the exploration Agent
can call the read-only `inspect_screen` tool when structured evidence cannot
explain the visible room layout or a suspected passage. `POE_VISION_MODEL`
defaults to `gemini-3.7-flash`, `POE_VISION_BASE_URL` to
`https://api.poe.com/v1`, and `POE_VISION_TIMEOUT` to 30 seconds. Values may be
supplied through the process environment or the project `.env`; keys are never
persisted in Agent data.

```text
GET  /agent/session
POST /agent/session/run       {"model":"deepseek-v4-pro","max_turns":10}
POST /agent/session/interrupt
POST /agent/session/decide
POST /agent/session/execute
POST /agent/session/reset
GET  /agent/session/events?after=0&limit=200
GET  /agent/session/events?turn_limit=5&before_turn=12
WS   /agent/session/stream?after=0
```

The Web UI initially loads the latest five complete turn groups. Scrolling to
the top loads earlier groups in five-turn pages; the `before_turn` cursor is
omitted for the newest page. The original `after`/`limit` event query remains
available for forward event consumers and WebSocket catch-up.

Session control metadata, the SDK SQLite transcript, and replayable activity
events live under gitignored `runtime/agent/`. A server restart pauses an
unfinished run; it never resumes automatically. Manual game actions and
checkpoint writes return `409 agent_session_running` while automatic play is
active.

Agent sessions follow the game lifecycle rather than the debug-server process.
Each SDLPAL process exports a generated `game_instance_id`: restarting only the
server preserves the current session, while restarting the game or choosing
Start New Game creates a clean session. `PALPAL_GAME_INSTANCE_ID` is normally
set by `./run` and is only needed when launching the native binary manually.

Checkpoints include a local Agent bundle containing the saved session state,
SDK transcript, activity timeline, and any archived tool outputs. Loading one
creates a new branch session from that exact save point, so decisions made after
the checkpoint do not leak into the restored game. Only the current Agent bundle
format is supported.

Set `PALPAL_AGENT_COMPACT_AT_TOKENS` to change the context compression
threshold (default `200000` tokens). After each Agent run, PALPAL records the
exact input-token count returned by DeepSeek for the final model request. If it
reaches the threshold, compression runs before the next model request and clears
the observation until the next real request supplies a fresh count. The newest
complete user-message cycle is always retained. A second complete cycle is also
retained when both cycles fit within `PALPAL_AGENT_COMPACT_RETAIN_TOKENS`
(default `24000` estimated tokens); otherwise it is summarized with older
history. Calls and outputs are never split within a retained cycle, so the newest
cycle may exceed the budget. The UI reports cumulative billed tokens, the latest
agent-run usage, the latest exact prompt count, and the last compression's
before/after estimates and retained-cycle decision separately. Timeline details
include the estimate composition and budget decision.
The structured task ledger remains the sole authority for task status; a
compression summary does not replace it. After compression or checkpoint
restore, the next Agent run must reconcile a compact index of active,
completed, and abandoned tasks before navigation or item use resumes.

The persistent model sees fourteen tools: `upsert_task`, `upsert_room_memo`, `get_tasks`,
`get_navigation`, `inspect_target`, `get_observations`, `get_shop`,
`get_equipment`, `get_inventory`, `get_room_memory`, `inspect_screen`,
`get_tool_output`, `review_task_inbox`, and `submit_decision`. `get_tasks`
remains backward compatible by defaulting to active tasks with full records, and
also supports summary/full detail plus offset pagination. Creating a task
requires a complete, fresh, closed-inclusive summary review; its final page
returns a review token that cannot be used by a parallel write from the same
model batch. A transition to `completed` records its wall-clock time, Agent
turn, logical frame, and raw native frame when available; completed records are
returned newest-completion first regardless of task priority.

`upsert_room_memo` keeps at most one 800-character Agent-written semantic note
per stable `logical_room_id`. The current room's memo (or `null`) is included in
each turn's initial context; `get_room_memory` returns it for explicitly queried
historical rooms together with compact exploration counts, room-scoped dialog
samples, and separately directed `outgoing` and confirmed `incoming` transition
records. Speaker hints remain metadata rather than engine identities. These notes
are fallible location annotations, not substitutes for exploration evidence,
inventory state, temporary coordinates, or actionable leads, which still belong
in independent tasks. Before adding or changing a task's non-empty `room_id`, the
Agent must query that room in the same tool cycle. Memos survive Agent session
reset, follow checkpoint companion branches, and are cleared by Start New Game.
The Agent tab exposes the current room's memo plus the five most recently added
other-room memos through a read-only `Room Memo` dialog.

Each merged speaker turn becomes an independent persistent task-inbox item even
when several speakers share one native trigger object. After dialogue ends, the
Agent must resolve every pending item before any other game action. Explicit
summons, commitments, and other unfinished follow-ups use `new_task`; existing
task progress uses `task_updated`. The corresponding `upsert_task` call must
include `source_inbox_ids`, so failed writes leave the evidence pending instead
of allowing exploration to continue. Only chatter, duplicate evidence, or an
instruction with an observed completed effect uses `no_follow_up`. Query outputs
up to about 8000 estimated tokens stay
verbatim. Larger JSON or text outputs are atomically archived under the current
session's `runtime/agent/tool_outputs/` directory; the transcript keeps a valid
JSON preview with key fields, head/tail excerpts, original size, and a
reloadable `output_ref`. `get_tool_output` reads only the current session's
archive in pages of at most 6000 characters.

At 96000 estimated context tokens, older tool results are archived until the
estimate is near 80000 while the six most recent complete tool results remain
inline. This tool-output pressure heuristic remains separate from the exact,
next-request 200k whole-session compression trigger and avoids rewriting the
transcript on every turn. There is no permanent macro goal in agent instructions
or session metadata; an empty task ledger still permits normal exploration.
After actions, the runtime requires a two-second quiet state before starting the
next decision.

`submit_decision` accepts only a minimal action plus a short reason.
`navigate` adds `target_id`; `use_item` adds `item_id` and exactly one of
`role_id` and `target: "all"`; `equip_item` adds `item_id` and `role_id`;
`buy_item` adds `item_id`; `select_choice` adds `option_index`; and
`advance_dialog` and `leave_shop` need no additional fields. Runtime
synchronization may still wait internally, but `wait` is not a model action. The
server derives the internal intent, evidence, risk, and executable preview; the
former nested decision payload is rejected.

The checkpoint endpoints above are implemented. These separate root-level
bridge endpoints remain reserved but are not implemented yet:

- `POST /reset`
- `POST /save`
- `POST /load`

### Battle State and Automation

While `in_battle` is true, the exported state includes the battle `phase`
(`pre_battle`, `command`, `reward`, `ending`), `is_boss`,
`current_player_index`, party `players`, `enemies`, and an `available_actions`
list with `target_mode`, `valid_target_indices`, and
`submission_target_indices`. This root `battle` object is a full debugging view.
`_debug.battle_player_view` is the player-visible projection used by the browser
and battle Agent: it includes party HP/MP, enemy names, legal targets, and the MP
cost and menu descriptions shown for battle magic and items. Friendly players
also expose active `status_effects` with semantic keys and remaining rounds plus
named `poisons`; enemy internal effects remain hidden. The root non-battle
`party` entries expose the same friendly effects so persistent high-level poison
and equipment effects remain visible after battle. Exact enemy HP, resistances,
technique probabilities, and other internal combat values remain omitted. The runtime hands the
fight to a dedicated battle Agent that exposes `get_battle`, `get_item_details`, and
`submit_battle_action` and must submit exactly one legal action per command
round. Its item tool exposes counts, player-visible menu descriptions, and legal
battle uses but not script-derived effects. Without `DEEPSEEK_API_KEY` it falls back to a conservative default
policy. Pre-battle prompts are handled by the main exploration Agent before the
battle Agent takes over. While the battle Agent owns control, manual game actions
are rejected.

## State Model

The exported state currently supports map exploration and the implemented
inventory, equipment, shop, and battle workflows:

- frame and mode flags
- `scene_id`, `map_id`, and `region_key`
- viewport and party position
- party members, HP/MP, level, money, and equipped items
- inventory entries with item metadata and usable/equippable properties
- active shop mode, selection phase, offered items, and prices
- battle mode, phase, party/enemy combatants, and legal actions during fights
- dialog state and recent dialog events
- current scene event objects
- nearby blocked map tiles and the local navigation grid

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
./run visual-smoke  # verify the browser canvas renders live PNG frames
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
