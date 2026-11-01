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
GET  /navigation/status        # lightweight native generation, position, and control
GET  /navigation/route?id=123
GET  /navigation/transitions
POST /navigation/step          {"id":123}
```

Both navigation entrypoints wait for the matching native action generation;
ordinary movement does not wait for global state quietness. Observed moving
targets use at most two moves per segment (one near the goal). A route that
changes repeatedly before dispatch returns `replanning` without writing an
action. Control boundaries and interaction results still use their normal
observation lifecycle.

Run the isolated server-gap benchmark with existing history, without operating
the live game:

```sh
.venv/bin/python -m scripts.benchmark_navigation --history runtime/exploration.json --concurrent
node --test tests/navigation_progress.test.mjs
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
`deepseek-v4-pro`, `deepseek-v4-flash`, or
`deepseek-v4.1-flash-expires-on-0910`), and `DEEPSEEK_TIMEOUT` to 90 seconds.
The debug server also requires `POE_API_KEY` at startup so the exploration Agent
can call the read-only `inspect_screen` tool when structured evidence cannot
explain the visible room layout, a suspected passage, or an object's appearance. `POE_VISION_MODEL`
defaults to `gemini-3.8-flash`, `POE_VISION_BASE_URL` to
`https://api.poe.com/v1`, and `POE_VISION_TIMEOUT` to 30 seconds. Values may be
supplied through the process environment or the project `.env`; keys are never
persisted in Agent data.

For an object's appearance, call `inspect_screen` with `focus="object"` and
`target_id`. No approach or interaction is required. The native renderer binds
IDs to surviving visible pixels in the same frame; the vision model describes
that marked object, never guesses unmarked IDs or hidden gameplay facts.
`inspect_target` reports available image evidence and existing appearance notes;
`get_navigation` includes compact cached appearance hints. These are explicitly
`visual_annotation`, not confirmed identities, quest outcomes, or Room Memo facts.

First visibility makes no model request. The server retains at most 128 recent
object crops (8 MiB total) in memory for the current logical room, allowing an
on-demand historical query after an object runs away. Historical evidence is
dated and does not establish current location. Only normalized descriptions,
confidence and provenance persist in `exploration.json` and checkpoints; images
are cleared on room/instance changes and checkpoint restoration. Changed sprites
require new evidence. Old native binaries without pixel bindings return
`object_visual_unavailable`. Ordinary layout and UI inspection remain available.

Native frames retain the existing PFRM image payload and may append a `POBJ`
trailer: magic, little-endian uint32 JSON length, version-1 JSON capture context
and object descriptors, then a 320x200 little-endian uint16 pixel-owner mask
(zero means background or occluder). The trailer travels atomically with its
image. Frame-repeat detection includes this metadata; PNG decoding and its cache
use only image bytes. UI-overlaid or otherwise unbound frames omit the trailer.

Navigation completion `trigger_completed` and its observed object changes are
preserved in Agent results. This means the native trigger completed, rather than
only reaching a location; it does not by itself establish an object's identity
or task success.

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

Each formal battle generation has its own child SDK session. Battle prompts use
a compact player-visible projection: cross-battle combat experience is included
once, later command windows append only newly visible outcomes, and native
action audits remain in Agent control/events rather than duplicating the battle
snapshot in the model transcript. Battle sessions retain exact provider token
counts independently from the main Agent. Set
`PALPAL_BATTLE_COMPACT_AT_TOKENS` or
`PALPAL_BATTLE_COMPACT_RETAIN_TOKENS` to override their explicit compression
threshold and retained-tail budget; when unset they inherit the corresponding
`PALPAL_AGENT_COMPACT_*` values. Ordinary battle turns remain append-only, while
threshold-triggered compression atomically replaces older child history with a
structured action summary and recent complete command cycles.

The persistent model sees these tools: `upsert_task`, `upsert_room_memo`, `get_tasks`,
`get_navigation`, `inspect_target`, `preview_item_placement`, `get_observations`, `get_shop`,
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
`navigate` adds `target_id`; `move_to` adds world coordinates `x`, `y` and an
optional `face` (`up`, `down`, `left`, `right`); `use_item` adds `item_id` and exactly one of
`role_id` and `target: "all"`; `equip_item` adds `item_id` and `role_id`;
`buy_item` and `sell_item` add `item_id`; `select_choice` adds `option_index`; and
`advance_dialog` and `leave_shop` need no additional fields. Runtime
synchronization may still wait internally, but `wait` is not a model action. The
server derives the internal intent, evidence, risk, and executable preview; the
former nested decision payload is rejected.

`move_to` plans position and facing together using ordinary movement, without
searching on arrival. Coordinates must be exact navigation-grid positions;
blocked destinations and impossible facing are rejected. Crossing a trigger
boundary or changing rooms ends the movement so the Agent can reassess.
`preview_item_placement(item_id, x?, y?, face?)` reports the player's stance,
actual adjacent landing position, terrain validity, and stance reachability.
Omitted coordinates use the current position; omitted facing checks all four
directions. A legal placement does not establish task effectiveness. Item action
results include the placed object's actual coordinates and state changes.

`inspect_target` includes `motion` sampled from native state updates, with
`motion_offset` / `motion_limit` pagination (default: most recent 32 points).
Samples preserve turns and trigger changes; they describe observed positions,
not future scripted routes. The cache retains eight rooms and up to 512 samples
per object, reports truncation/gaps, and resets on load/restart. Navigation
trigger completion observes subsequent movement for up to eight seconds,
stopping after 0.4 seconds without position changes or on a control interruption.
Action results retain a bounded motion summary; full query output uses the
existing tool-output archive.

`motion.moved` describes historical movement. `current_status` reports
`moving`, `stationary`, or `unknown`: stationary requires at least 0.4 seconds
of continuing state samples at the same position. A stream gap over one second,
an absent object, or a checkpoint restore invalidates that conclusion.
`observed_stationary_seconds` measures the continuously observed interval.
`latest_action_association` and action-result `motion_association` retain the
action, confirmed native trigger, subsequent movement, and observed stopping.
They do not predict a future route or autonomous patrol. Agent navigation and
coordinate movement consume trigger completion once per execution, bound to
the game instance, room, target, and native generation. Later target movement
or route invalidation cannot overwrite that confirmed success.

`observe_target(id, seconds=2)` samples for 0.4–5 seconds without sending keys.
It reports the observation interval, player/target positions, distance in world
pixels, intervening native triggers, movement (including returning to the
starting position), and interruption reasons. Missing samples, room changes,
player movement, or control interruptions prevent a completed observation.
`motion.observation_windows` retains the latest 16 passive/action observations
per object for comparison; `movement_rule=not_inferred` explicitly leaves
mechanism inference to the Agent. No task-specific movement strategy is supplied.
`inspect_target` refreshes the target from the live state; `position_is_current`,
`position_is_stationary`, and `sample_frame` qualify its coordinates. Navigation
action results include `target_observation` after bounded movement observation,
even when a route fails. This does not guarantee a moving target has stopped.

`scripts/evaluate_spatial_task.py --fixture /path/to/game.rpg` runs three
independent Agent trials from a copied save. It copies resources and writes all
runtime evidence to disposable directories, uses the configured external model
and vision providers, and checks the requested inventory outcome. The default
goal is obtaining deer antler; `--goal` and `--success-item-id` select another
inventory-based task. It does not connect to the user-owned debug server.

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
window. Its item tool exposes counts, player-visible menu descriptions, sanitized
DOS handbook-equivalent effects, and legal battle uses, never raw scripts or hidden
enemy values. Without `DEEPSEEK_API_KEY` it falls back to a conservative default
policy. Pre-battle prompts are handled by the main exploration Agent before the
battle Agent takes over. While the battle Agent owns control, manual game actions
are rejected.

The browser keeps a short recent-outcome list. Battle Agent prompts instead
deliver every retained outcome after their delivery cursor, with explicit
`outcome_delivery` missing-sequence ranges if the native 32-outcome buffer has
overrun. Repeated refreshes do not resend delivered outcomes. The cursor and
`survival_risk` observation checkpoint advance only after the model request
completes; existing session messages are not rewritten.

`survival_risk` distinguishes observed HP loss, recovery, and net HP loss between
decision windows, records enemy action counts, and retains observed damage
maxima; current friendly statuses live in `players`. These windows can span several rounds
when characters cannot act. Missing evidence stays explicitly incomplete, and
observed maxima are not upper damage limits. The Agent is prompted to assess
survival until its next action without guessing an unknown enemy's remaining
HP; action validation does not impose a new combat policy.

Classic round-end poison/status settlement now has its own `round_end` outcome
with an `environment` actor, separate from enemy attacks. `hp_accounting` compares
window start/end HP with recorded losses and recoveries; `unexplained_hp_delta`
reports discrepancies without guessing their cause. `outcome_delivery.complete`
only means sequence delivery is contiguous, not that every effect was captured.
`decision_feedback` references execution-ordered outcomes and highlights effective
HP recovery (including zero) and every affected party member. No new effects means
no new execution evidence, not that a submitted action has succeeded.

Native `command_status` distinguishes `committed`, `selecting`, `unselected` and
`unable`. Only committed actions enter the model prompt as `committed_action`;
old retained action fields are not treated as this round's plan. Older native builds
without this field are explicitly `unknown`. Model-only `enemy_roster_changes`
describes appearances/disappearances, distinct from per-outcome damage changes.
The revised prompt allocates survival, recovery, area and single-target actions
across the whole party rather than prescribing a fixed healer or favorite hero.

Battle `get_item_details` accepts exactly one of `item_id`, `item_ids` (up to 32
distinct positive IDs), or `cursor`. A batch reads one state/knowledge snapshot,
deduplicates in request order and returns per-item errors. A large response is
returned as JSON text fragments: follow `next_cursor` with the same tool until
`complete=true`, concatenate `text`, then parse the complete JSON. Cursors are
valid within that decision and retain the original snapshot; no archive-only
reference or silent truncation is used. Single-item callers retain their result shape.

`python -m scripts.evaluate_battle_team --output /tmp/team-evaluation.json`
compares the frozen previous prompt/input with the revised version at six team
decision points, five repetitions each (60 decisions), using
`deepseek-v4.1-flash-expires-on-0910` by default. It records actions, tool counts,
provider token usage and latency. It does not add future item samples or invent
missing poison events; historical discrepancies remain unresolved. Native command
commitments are reconstructed from observed accepted commands within each unresolved
window. These observation replays do not establish whole-battle win rates.

`python -m scripts.evaluate_battle_survival --output /tmp/battle-evaluation.json`
runs an optional external-model replay of three recorded danger points, three
times per prompt variant. It sends player-visible fixture data to the configured
DeepSeek endpoint, never to the game, and does not alter live sessions. The replay
uses observation history without original reasoning; unavailable historical item
details remain unavailable. Inspect its saved model reasoning as well as actions:
legal submissions alone do not prove improved decisions or a winning strategy.

## State Model

The exported state currently supports map exploration and the implemented
inventory, equipment, shop, and battle workflows:

- frame and mode flags
- `scene_id`, `map_id`, and `region_key`
- viewport and party position
- party members, HP/MP, level, money, and equipped items
- inventory entries with item metadata and usable/equippable properties
- active buy/sell shop mode, selection phase, items, prices, and available quantities
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

## Checkpoint recovery

Checkpoint saves pause the native game while capturing the game file, companion
memory and Agent session. New checkpoints include SHA-256 hashes. Loading checks
the complete bundle and native save format before changing the live game; complete
version 2 bundles remain supported, while incomplete or incompatible bundles are
rejected instead of starting an empty Agent session.

If an operation fails after pausing, the game controls remain blocked. The current
phase and error appear in the checkpoint panel; recovery files remain under
`runtime/checkpoint_operations/`, and `runtime/checkpoint_operation.json` preserves
the block across debug-server restarts. A deliberate load retry can clear the block
after a successful restore. A normal `./run` refuses to clear runtime history while
recovery is pending; use `./run debug` to inspect or retry with the existing game.
Checkpoint slots must be in the range 1–255; slot 0 is reserved for a new game.

After building, the optional native smoke check copies resources and a source save
into a temporary directory, verifies memo restoration and native I/O errors, and
stops its test game:

```sh
.venv/bin/python -m scripts.checkpoint_smoke --game-save /path/to/checkpoint/game.rpg
```

## Licensing Notes

SDLPAL is GPL-3.0. This project vendors and modifies SDLPAL, so changes to the
runtime should be treated as GPL-3.0 compatible work.

The DOS game resources are not licensed by this repository. Keep resources in
gitignored local paths such as `data/pal/`, and do not commit or redistribute
them from this project.
