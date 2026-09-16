from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable

from palpal.battle_observations import decision_feedback, outcome_delta, survival_risk
from palpal.battle_items import BattleItemDetails, ITEM_DETAILS_DESCRIPTION, ITEM_DETAILS_SCHEMA


RUNNING_PHASES = {"running", "interrupting"}
EXECUTABLE_TOOLS = {
    "press_buttons", "advance_dialog", "choice_select", "navigation_step", "move_to", "use_item",
    "equip_item", "shop_buy", "shop_sell", "shop_leave",
}
CHECKPOINT_BUNDLE_VERSION = 2
DEFAULT_COMPACT_AT_TOKENS = 200_000
DEFAULT_COMPACT_RETAIN_TOKENS = 24_000
BATTLE_EXPERIENCE_SAMPLE_LIMIT = 3
BATTLE_EXPERIENCE_ACTION_LIMIT = 24
TOOL_OUTPUT_FULL_TOKENS = 8_000
TOOL_OUTPUT_PAGE_CHARS = 6_000
TOOL_OUTPUT_REF_RE = re.compile(r"tool-[0-9a-f]{32}\.txt")
EVENT_CACHE_SIZE = 1_000
EVENT_TAIL_CHUNK_SIZE = 64 * 1024
BATTLE_INSTRUCTION_LIMIT = 100
BATTLE_CONTROL_TIMEOUT_SECONDS = 30.0
AGENT_REASONING_EFFORT = "high"
SUMMARY_REASONING_EFFORT = "low"

# Frozen survival-replay variant only; production uses BATTLE_INSTRUCTIONS below.
BATTLE_SURVIVAL_INSTRUCTIONS = (
    "Before attacking, assess whether every living ally can survive until the next "
    "opportunity to act, including consecutive enemy actions and turns lost to sleep "
    "or other visible controls. survival_risk summarizes observed decision windows, "
    "which may span multiple rounds; cumulative HP loss is separate from healing and "
    "net HP loss. Observed maxima are samples, never upper damage limits. Missing "
    "outcomes mean unknown additional effects, not zero damage. Consider current legal "
    "healing, defense and supported item uses, their timing, and recovery reserves. "
    "Do not accept lethal exposure on the speculation that an unknown-HP enemy is "
    "nearly defeated. If the party is endangered, prioritize a supported survival "
    "action over speculative finishing damage."
)

BATTLE_INSTRUCTIONS = """Goal and evidence
Win while preserving the whole party's ability to act and recover. Use player-visible
facts only: names do not establish effects, weaknesses or damage. Unknown enemy HP
does not justify a near-defeat claim. Observed damage is a sample, not an upper bound.

Review effects
Current players and legal actions are authoritative. Read new_outcomes in execution
order and the latest survival_risk window: effective recovery, total incoming damage,
new controls, poison and knockouts. Delivery completeness concerns sequence delivery
only; unresolved HP accounting means additional unknown effects, not zero damage.
An accepted submission is not execution. Reassess when enemy_roster_changes appears.

Team decisions
Account for allies' committed_action before assigning healing, control recovery,
area attacks and single-target damage; avoid redundant healing. First reduce death
and lost-action risk across the party, including consecutive enemy actions and poison.
If not everyone can be protected, preserve the team's feasible recovery and action
capacity, not a fixed favored character. Do not rely on a later rescue or speculative
finishing blow. Compare area damage and removal of enemy actions, not only boss damage;
value waking an ally and an item's other supported uses as well as direct damage.
Action order varies with action type and condition; the last round is not a fixed order.
Waking an ally during resolution does not allow a fresh spell selection that round.
When safe, purposefully sample a promising untested spell and update comparisons using
observed effects, MP and consumable scarcity. Saving resources must not override survival.

Tools
The snapshot is current; use get_battle only if stale. Query only unknown information
that could change this decision, batching item IDs. Use supported item effects, never
blind-test scarce items. Choose one current legal action and a target_index from its
submission_target_indices. Call submit_battle_action without preceding narration.
"""

PROVIDER_USAGE_SCOPES = ("main_decision", "battle", "compaction", "vision")


class ReplanRequired(RuntimeError):
    """A safe, transient state change invalidated a recommendation."""


class BattleStarted(RuntimeError):
    """A new formal battle superseded ordinary model work without consuming a turn."""

    def __init__(self, state: dict):
        super().__init__("battle_started")
        self.state = state


def _now() -> float:
    return time.time()


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class BattleDelegate:
    """Single-controller child workflow for one native battle generation."""

    def __init__(self, runtime: "AgentRuntime"):
        self.runtime = runtime
        self.debug = runtime.debug
        self.before: dict | None = None
        self.actions: list[dict] = []

    def _control(self) -> dict:
        value = self.runtime.state.get("battle_delegate")
        return value if isinstance(value, dict) else {}

    def _persist_control(self, **updates: Any) -> dict:
        with self.runtime.lock:
            value = dict(self._control())
            value.update(updates)
            self.runtime.state["battle_delegate"] = value
            self.runtime._persist()
            return value

    @staticmethod
    def _group_id(control: dict) -> str:
        return (
            f"battle:{control.get('game_instance_id') or '-'}:"
            f"{int(control.get('battle_generation') or 0)}:"
            f"{control.get('child_session_id') or '-'}"
        )

    def _emit(self, phase: str, summary: str, details: dict | None = None) -> dict:
        control = self._control()
        payload = {
            "battle_delegate": {
                "child_session_id": control.get("child_session_id"),
                "battle_generation": int(control.get("battle_generation") or 0),
                "group_id": self._group_id(control),
                "depth": 1,
                "status": control.get("status"),
            },
            **(details or {}),
        }
        return self.runtime.emit("battle", phase, summary, payload)

    @staticmethod
    def _encounter_enemies(battle: dict) -> list[dict]:
        enemies = []
        for entry in battle.get("enemies") or []:
            if not isinstance(entry, dict) or entry.get("present") is False:
                continue
            try:
                object_id = int(entry.get("object_id") or 0)
                slot_index = int(entry.get("slot_index") or 0)
            except (TypeError, ValueError):
                continue
            if object_id <= 0:
                continue
            enemies.append({
                "slot_index": slot_index,
                "object_id": object_id,
                "name": str(entry.get("name") or ""),
            })
        return enemies

    @classmethod
    def _enemy_roster(cls, battle: dict) -> list[dict]:
        return cls._encounter_enemies(battle)

    def _with_enemy_changes(self, battle: dict) -> dict:
        view = copy.deepcopy(battle)
        previous = self._control().get("last_sent_enemies")
        if not isinstance(previous, list):
            return view
        current = self._enemy_roster(view)
        previous_by_key = {
            (int(enemy.get("slot_index") or 0), int(enemy.get("object_id") or 0)): enemy
            for enemy in previous if isinstance(enemy, dict)
        }
        current_by_key = {
            (int(enemy.get("slot_index") or 0), int(enemy.get("object_id") or 0)): enemy
            for enemy in current
        }
        appeared = [current_by_key[key] for key in current_by_key.keys() - previous_by_key.keys()]
        disappeared = [previous_by_key[key] for key in previous_by_key.keys() - current_by_key.keys()]
        if appeared or disappeared:
            view["enemy_changes"] = {
                "appeared": sorted(appeared, key=lambda item: int(item.get("slot_index") or 0)),
                "disappeared": sorted(disappeared, key=lambda item: int(item.get("slot_index") or 0)),
            }
        return view

    def _mark_enemies_sent(self, battle: dict) -> None:
        self._persist_control(last_sent_enemies=self._enemy_roster(battle))

    @staticmethod
    def _compact_player_change(change: dict) -> dict:
        compact = {
            key: change.get(key)
            for key in ("index", "role_id", "knocked_out")
            if key in change and (key != "knocked_out" or change.get(key))
        }
        for stat in ("hp", "mp"):
            value = change.get(stat)
            if not isinstance(value, dict):
                if f"{stat}_delta" in change:
                    compact[f"{stat}_delta"] = change[f"{stat}_delta"]
                continue
            try:
                before = int(value.get("before") or 0)
                after = int(value.get("after") or 0)
            except (TypeError, ValueError):
                continue
            if before != after:
                compact[f"{stat}_delta"] = after - before
        for prefix in ("status", "poisons"):
            before = change.get(f"{prefix}_before")
            after = change.get(f"{prefix}_after")
            if before != after:
                compact[f"{prefix}_before"] = before or []
                compact[f"{prefix}_after"] = after or []
        return compact

    @classmethod
    def _compact_outcome(cls, outcome: dict, *, include_identity: bool = True) -> dict:
        compact: dict[str, Any] = {}
        if "sequence" in outcome:
            compact["sequence"] = int(outcome.get("sequence") or 0)
        if include_identity:
            actor = outcome.get("actor") if isinstance(outcome.get("actor"), dict) else {}
            compact["actor"] = {
                key: actor.get(key)
                for key in ("index", "role_id", "object_id", "name") if key in actor
            }
            for key in ("action_type", "object_id", "name", "mp_cost", "actor_side"):
                if key in outcome:
                    compact[key] = outcome.get(key)
            target = outcome.get("target") if isinstance(outcome.get("target"), dict) else {}
            if target:
                compact["target"] = {
                    key: target.get(key)
                    for key in ("index", "side", "object_id", "name", "mode")
                    if key in target
                }
        enemy_changes = [
            {
                key: change.get(key)
                for key in ("slot_index", "object_id", "amount", "kind", "defeated")
                if key in change
            }
            for change in outcome.get("enemy_changes") or []
            if isinstance(change, dict)
        ]
        if enemy_changes:
            compact["enemy_changes"] = enemy_changes
        player_changes = [
            cls._compact_player_change(change)
            for change in outcome.get("player_changes") or []
            if isinstance(change, dict)
        ]
        player_changes = [change for change in player_changes if len(change) > 1]
        if player_changes:
            compact["player_changes"] = player_changes
        if outcome.get("no_numeric_change_observed"):
            compact["no_numeric_change_observed"] = True
        return compact

    @classmethod
    def _compact_experience(cls, battle: dict, experience: dict) -> dict:
        available = {
            (str(action.get("action_type") or ""), int(action.get("object_id") or 0))
            for action in battle.get("available_actions") or []
            if isinstance(action, dict)
        }
        party_role_ids = {
            int(player.get("role_id") or 0)
            for player in battle.get("players") or []
            if isinstance(player, dict)
        }
        current_generation = int(battle.get("generation") or 0)
        def compact_actions(entries: list) -> list[dict]:
            actions = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                action_type = str(entry.get("action_type") or "")
                object_id = int(entry.get("object_id") or 0)
                actor_role_id = int(entry.get("actor_role_id") or 0)
                if action_type in {"use_item", "throw_item"}:
                    if (action_type, object_id) not in available:
                        continue
                elif actor_role_id not in party_role_ids:
                    continue
                historical_samples = [
                    sample for sample in entry.get("samples") or []
                    if isinstance(sample, dict)
                    and int(sample.get("battle_generation") or 0) != current_generation
                ]
                if not historical_samples:
                    continue
                actions.append({
                    key: entry.get(key)
                    for key in ("actor_role_id", "actor_name", "action_type", "object_id", "name")
                    if key in entry
                } | {
                    "sample_count": len(historical_samples),
                    "recent_samples": [
                        cls._compact_outcome(sample, include_identity=False)
                        for sample in historical_samples[-BATTLE_EXPERIENCE_SAMPLE_LIMIT:]
                    ],
                })
                if len(actions) >= BATTLE_EXPERIENCE_ACTION_LIMIT:
                    break
            return actions

        enemy_memories = []
        for memory in experience.get("enemy_memories") or []:
            if not isinstance(memory, dict):
                continue
            effects = [
                cls._compact_outcome(sample)
                for sample in memory.get("enemy_effects") or []
                if isinstance(sample, dict)
                and int(sample.get("battle_generation") or 0) != current_generation
            ][-BATTLE_EXPERIENCE_SAMPLE_LIMIT:]
            enemy_memories.append({
                key: copy.deepcopy(memory.get(key))
                for key in ("object_id", "name", "current_count", "hp_estimate")
                if key in memory
            } | {
                "player_actions": compact_actions(memory.get("player_actions") or []),
                "enemy_effects": effects,
            })
        return {
            "available": bool(experience.get("available")),
            "enemy_memories": enemy_memories,
            "support_actions": compact_actions(experience.get("support_actions") or []),
        }

    @staticmethod
    def _compact_action(action: dict) -> dict:
        compact = {
            key: action.get(key)
            for key in (
                "action_type", "object_id", "name", "target_side", "target_mode",
                "description", "mp_cost",
            )
            if key in action
        }
        if "submission_target_indices" in action:
            compact["submission_target_indices"] = action.get("submission_target_indices") or []
        elif action.get("target_mode") == "all":
            compact["submission_target_indices"] = [-1]
        elif action.get("target_mode") == "none":
            compact["submission_target_indices"] = [-2]
        else:
            compact["submission_target_indices"] = action.get("valid_target_indices") or []
        return compact

    @classmethod
    def _agent_battle_view(
        cls,
        view: dict,
        *,
        include_experience: bool,
        after_outcome_sequence: int,
    ) -> dict:
        compact = {
            key: copy.deepcopy(value)
            for key, value in view.items()
            if key not in {"available_actions", "recent_outcomes", "experience"}
        }
        compact["available_actions"] = [
            cls._compact_action(action)
            for action in view.get("available_actions") or []
            if isinstance(action, dict)
        ]
        outcomes, delivery = outcome_delta(view.get("recent_outcomes") or [], after_outcome_sequence)
        compact["outcome_delivery"] = delivery
        new_outcomes = [
            cls._compact_outcome(outcome)
            for outcome in outcomes
        ]
        if new_outcomes:
            compact["new_outcomes"] = new_outcomes
        if "enemy_changes" in compact:
            compact["enemy_roster_changes"] = compact.pop("enemy_changes")
        for player in compact.get("players") or []:
            old_action = player.pop("submitted_action", None)
            status = player.setdefault("command_status", "unknown")
            if status == "committed" and old_action:
                player["committed_action"] = old_action
        risk = compact.get("survival_risk")
        if isinstance(risk, dict):
            risk["players"] = [{"index": p["index"], "observed_maxima": p["observed_maxima"]}
                               for p in risk.get("players") or []]
        compact["decision_feedback"] = decision_feedback(compact, new_outcomes)
        experience = view.get("experience")
        if include_experience and isinstance(experience, dict):
            compact["experience"] = cls._compact_experience(view, experience)
        return compact

    @staticmethod
    def _max_outcome_sequence(battle: dict) -> int:
        return max(
            (
                int(outcome.get("sequence") or 0)
                for outcome in battle.get("new_outcomes") or []
                if isinstance(outcome, dict)
            ),
            default=0,
        )

    def _trigger_context(self, state: dict) -> dict | None:
        observation = (
            state.get("trigger_observation")
            if isinstance(state.get("trigger_observation"), dict)
            else None
        )
        if not observation or observation.get("completed") is not False:
            return None
        source = observation.get("source") if isinstance(observation.get("source"), dict) else {}
        try:
            generation = int(observation.get("generation") or 0)
            object_id = int(observation.get("event_object_id") or 0)
            trigger_script = int(observation.get("trigger_script") or 0)
            source_scene = int(source.get("scene_id"))
            source_map = int(source.get("map_id"))
            state_scene = int(state.get("scene_id"))
            state_map = int(state.get("map_id"))
        except (TypeError, ValueError):
            return None
        if generation <= 0 or object_id <= 0 or source_scene != state_scene or source_map != state_map:
            return None

        context = {
            "relationship": "active_trigger_at_battle_start",
            "generation": generation,
            "event_object_id": object_id,
            "trigger_script": trigger_script,
            "source": {
                "scene_id": source_scene,
                "map_id": source_map,
                "x": source.get("x"),
                "y": source.get("y"),
            },
        }
        identity = self.debug.room_identity(state)
        if isinstance(identity, dict) and identity.get("logical_room_id"):
            context["logical_room_id"] = identity["logical_room_id"]
        exploration = self.debug.agent_current_exploration(state)
        last_route = (
            exploration.get("last_route")
            if isinstance(exploration, dict) and isinstance(exploration.get("last_route"), dict)
            else {}
        )
        target = (
            last_route.get("target_selection")
            if isinstance(last_route.get("target_selection"), dict)
            else None
        )
        try:
            route_target_id = int((target or {}).get("id") or 0)
        except (TypeError, ValueError):
            route_target_id = 0
        if target is not None and route_target_id == object_id:
            context["navigation_target"] = {
                "id": route_target_id,
                "kind": target.get("kind"),
                "status": target.get("status"),
            }
        return context

    def _begin(self, state: dict) -> dict:
        battle = state["battle"]
        generation = int(battle["generation"])
        instance_id = str(state.get("game_instance_id") or "")
        existing = self._control()
        reusable = (
            int(existing.get("battle_generation") or 0) == generation
            and str(existing.get("game_instance_id") or "") == instance_id
            and existing.get("child_session_id")
        )
        child_id = str(existing["child_session_id"]) if reusable else uuid.uuid4().hex
        instruction_count = int(existing.get("instruction_count") or 0) if reusable else 0
        last_prompt_tokens = existing.get("last_prompt_tokens") if reusable else None
        experience_sent = bool(existing.get("experience_sent")) if reusable else False
        last_sent_outcome_sequence = (
            int(existing.get("last_sent_outcome_sequence") or 0) if reusable else 0
        )
        compactions = int(existing.get("compactions") or 0) if reusable else 0
        last_compaction = existing.get("last_compaction") if reusable else None
        self.before = json.loads(json.dumps(battle, ensure_ascii=False))
        self.actions = list(existing.get("actions") or []) if reusable else []
        encounter_enemies = (
            list(existing.get("encounter_enemies") or [])
            if reusable
            else self._encounter_enemies(battle)
        )
        trigger_context = existing.get("trigger_context") if reusable else self._trigger_context(state)
        control = self._persist_control(
            child_session_id=child_id,
            parent_session_id=self.runtime.state.get("session_id"),
            game_instance_id=instance_id,
            battle_generation=generation,
            status="running",
            phase=str(battle.get("phase") or "resolving"),
            instruction_count=instruction_count,
            instruction_limit=BATTLE_INSTRUCTION_LIMIT,
            last_prompt_tokens=last_prompt_tokens,
            experience_sent=experience_sent,
            last_sent_outcome_sequence=last_sent_outcome_sequence,
            survival_observations=(existing.get("survival_observations") if reusable else None),
            compactions=compactions,
            last_compaction=last_compaction,
            stop_reason=None,
            started_at=existing.get("started_at") if reusable else _stamp(),
            ended_at=None,
            summary=None,
            actions=self.actions,
            encounter_enemies=encounter_enemies,
            last_sent_enemies=(existing.get("last_sent_enemies") if reusable else None),
            trigger_context=trigger_context,
        )
        self._emit(
            "started",
            "Battle Agent resumed." if reusable else "Battle Agent started.",
            {"phase": control.get("phase"), "instruction_count": instruction_count},
        )
        return control

    @staticmethod
    def _battle_from(state: dict) -> dict | None:
        return state.get("battle") if isinstance(state.get("battle"), dict) else None

    @staticmethod
    def _matching_last_battle(state: dict, generation: int) -> dict | None:
        last = state.get("last_battle")
        if not isinstance(last, dict):
            return None
        try:
            return last if int(last.get("generation") or 0) == generation else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _command_window(state: dict) -> dict:
        battle = state.get("battle") if isinstance(state.get("battle"), dict) else {}
        return {
            "game_instance_id": str(state.get("game_instance_id") or ""),
            "battle_generation": int(battle.get("generation") or 0),
            "command_generation": int(battle.get("command_generation") or 0),
            "player_index": int(battle.get("current_player_index") or 0),
        }

    def _wait_for_control_or_end(
        self,
        generation: int,
        after_command_generation: int | None = None,
    ) -> dict:
        deadline = time.monotonic() + BATTLE_CONTROL_TIMEOUT_SECONDS
        while True:
            if self.runtime.cancel_event.is_set():
                raise RuntimeError("interrupted")
            state = self.debug.snapshot()
            expected_instance = str(self._control().get("game_instance_id") or "")
            if (
                expected_instance
                and str(state.get("game_instance_id") or "") != expected_instance
            ):
                raise RuntimeError("game_instance_changed")
            if self._matching_last_battle(state, generation) is not None:
                return state
            battle = self._battle_from(state)
            if battle is None:
                if not state.get("in_battle"):
                    return state
            elif int(battle.get("generation") or 0) != generation:
                raise RuntimeError("battle_generation_changed")
            elif battle.get("phase") == "reward":
                return state
            elif battle.get("phase") == "command":
                command_generation = int(battle.get("command_generation") or 0)
                if (
                    after_command_generation is None
                    or command_generation > after_command_generation
                ):
                    return state
            if time.monotonic() >= deadline:
                raise RuntimeError("battle_control_timeout")
            time.sleep(0.05)

    @staticmethod
    def _default_action(battle: dict) -> dict:
        available = [
            action for action in battle.get("available_actions") or []
            if isinstance(action, dict)
        ]
        if not available:
            raise RuntimeError("battle_not_commandable")
        action = next(
            (item for item in available if item.get("action_type") == "continue"),
            None,
        )
        if action is None:
            action = next(
                (item for item in available if item.get("action_type") == "attack"),
                available[0],
            )
        mode = action.get("target_mode")
        targets = action.get("valid_target_indices") or []
        return {
            "action_type": action.get("action_type"),
            "object_id": int(action.get("object_id") or 0),
            "target_index": -1 if mode == "all" else (-2 if mode == "none" else int(targets[0])),
        }

    async def _provider_action(self, state: dict, feedback: str) -> dict:
        provider = self.runtime.battle_decision_provider
        full_battle = self.debug.current_player_battle_view(state) or {}
        decision_battle = self._with_enemy_changes(full_battle)
        if provider is None:
            return await self._sdk_action(state, feedback, battle_view=decision_battle)
        decision_battle["survival_risk"], risk_checkpoint = survival_risk(
            decision_battle, self._control().get("survival_observations"),
        )
        player_state = dict(state)
        player_state["battle"] = decision_battle
        player_state["inventory"] = [
            self.debug.player_battle_inventory_item(item)
            for item in state.get("inventory") or []
            if isinstance(item, dict)
        ]
        player_state.pop("_debug", None)
        value = provider(player_state, self.runtime.state.get("model"), feedback)
        if asyncio.iscoroutine(value):
            value = await value
        if not isinstance(value, dict):
            raise RuntimeError("invalid_battle_submission")
        self._persist_control(survival_observations=risk_checkpoint)
        self._mark_enemies_sent(decision_battle)
        return value.get("args") if isinstance(value.get("args"), dict) else value

    async def _sdk_action(
        self, state: dict, feedback: str, *, battle_view: dict | None = None,
    ) -> dict:
        if not self.debug.config_value("DEEPSEEK_API_KEY"):
            action = self._default_action(state["battle"])
            self._emit(
                "tool_output",
                "Default battle policy selected an action.",
                {"policy": "default", "output": action},
            )
            return action
        try:
            from agents import (Agent, AsyncOpenAI, FunctionTool, ModelSettings,
                                OpenAIResponsesModel, Runner, SQLiteSession,
                                set_tracing_disabled)
            from agents.agent import ToolsToFinalOutputResult
        except ImportError as exc:
            raise RuntimeError("openai-agents is required for battle delegation") from exc

        set_tracing_disabled(disabled=True)
        submitted: dict | None = None
        runtime = self.runtime
        frozen_window = self._command_window(state)
        control = self._control()
        full_player_battle = battle_view or self._with_enemy_changes(
            self.debug.current_player_battle_view(state) or {}
        )
        full_player_battle["survival_risk"], risk_checkpoint = survival_risk(
            full_player_battle, control.get("survival_observations"),
        )
        player_battle = self._agent_battle_view(
            full_player_battle,
            include_experience=not bool(control.get("experience_sent")),
            after_outcome_sequence=int(control.get("last_sent_outcome_sequence") or 0),
        )
        latest_sent_battle = full_player_battle
        prompt_outcome_sequence = max(
            int(control.get("last_sent_outcome_sequence") or 0),
            self._max_outcome_sequence(player_battle),
        )

        async def get_battle(_context: Any, _raw: str) -> str:
            nonlocal latest_sent_battle, prompt_outcome_sequence, risk_checkpoint
            self._emit(
                "tool_called",
                "Battle Agent called get_battle.",
                {"tool": "get_battle", "arguments": {}},
            )
            try:
                latest = self.debug.snapshot()
                latest_view = self._with_enemy_changes(
                    self.debug.current_player_battle_view(latest) or {}
                )
                # Never mix another battle into this delegate's observation state.
                if (
                    str(latest.get("game_instance_id") or "") != str(state.get("game_instance_id") or "")
                    or latest_view.get("generation") != full_player_battle.get("generation")
                ):
                    raise RuntimeError("battle_generation_changed")
                latest_view["survival_risk"], risk_checkpoint = survival_risk(
                    latest_view, control.get("survival_observations"),
                )
                output = {
                    "ok": True,
                    "battle": self._agent_battle_view(
                        latest_view,
                        include_experience=not bool(self._control().get("experience_sent")),
                        after_outcome_sequence=prompt_outcome_sequence,
                    ),
                }
                prompt_outcome_sequence = max(
                    prompt_outcome_sequence,
                    self._max_outcome_sequence(output["battle"]),
                )
                self._mark_enemies_sent(latest_view)
                latest_sent_battle = latest_view
            except Exception as exc:
                self._emit(
                    "tool_output",
                    "get_battle failed.",
                    {"tool": "get_battle", "output": {"ok": False, "error": str(exc)}},
                )
                raise
            self._emit(
                "tool_output",
                "get_battle returned the latest battle state.",
                {"tool": "get_battle", "output": output},
            )
            # The native outcome buffer is bounded. Deliver it directly: this
            # child has no get_tool_output tool to retrieve an archived preview.
            return json.dumps(output, ensure_ascii=False, separators=(",", ":"))

        item_details = BattleItemDetails(self.debug)

        async def get_item_details(_context: Any, raw: str) -> str:
            self._emit("tool_called", "Battle Agent called get_item_details.",
                       {"tool": "get_item_details", "raw_arguments": raw})
            try:
                args = runtime._parse_tool_arguments(raw)
                encoded = item_details.query(args)
            except (TypeError, ValueError):
                encoded = json.dumps({"ok": False, "error": "invalid_item_arguments"})
            self._emit("tool_output", "get_item_details returned a directly readable result.",
                       {"tool": "get_item_details", "output": json.loads(encoded)})
            return encoded

        async def submit(_context: Any, raw: str) -> str:
            nonlocal submitted
            try:
                args = runtime._parse_tool_arguments(raw)
            except Exception as exc:
                self._emit(
                    "tool_called",
                    "Battle Agent called submit_battle_action.",
                    {"tool": "submit_battle_action", "raw_arguments": raw},
                )
                self._emit(
                    "tool_output",
                    "submit_battle_action arguments were invalid.",
                    {"tool": "submit_battle_action", "raw_arguments": raw, "error": str(exc)},
                )
                raise
            self._emit(
                "tool_called",
                "Battle Agent called submit_battle_action.",
                {"tool": "submit_battle_action", "arguments": args},
            )
            try:
                args = {
                    **args,
                    **frozen_window,
                }
                error = self.debug.battle_action_validation_error(state, args)
            except Exception as exc:
                self._emit(
                    "tool_output",
                    "submit_battle_action failed.",
                    {"tool": "submit_battle_action", "output": {"ok": False, "error": str(exc)}},
                )
                raise
            if error:
                output = {"ok": False, "error": error}
                self._emit(
                    "tool_output",
                    "submit_battle_action was rejected.",
                    {"tool": "submit_battle_action", "output": output},
                )
                return json.dumps(output, ensure_ascii=False)
            submitted = args
            output = {"ok": True, "accepted": True}
            self._emit(
                "tool_output",
                "submit_battle_action was accepted.",
                {"tool": "submit_battle_action", "output": output},
            )
            return json.dumps(output, ensure_ascii=False)

        get_tool = FunctionTool(
            name="get_battle",
            description=(
                "Refresh the structured battle state only when the supplied snapshot may be stale; "
                "the current prompt already contains the current legal actions."
            ),
            params_json_schema={"type": "object", "properties": {}, "additionalProperties": False},
            on_invoke_tool=get_battle,
            strict_json_schema=False,
        )
        item_tool = FunctionTool(
            name="get_item_details",
            description=ITEM_DETAILS_DESCRIPTION,
            params_json_schema=ITEM_DETAILS_SCHEMA,
            on_invoke_tool=get_item_details,
            strict_json_schema=False,
        )
        submit_tool = FunctionTool(
            name="submit_battle_action",
            description="Submit exactly one legal action from available_actions.",
            params_json_schema={
                "type": "object",
                "properties": {
                    "action_type": {"type": "string"},
                    "object_id": {"type": "integer"},
                    "target_index": {"type": "integer"},
                },
                "required": ["action_type", "object_id", "target_index"],
                "additionalProperties": False,
            },
            on_invoke_tool=submit,
            strict_json_schema=False,
        )

        def finish(_context: Any, results: list[Any]) -> Any:
            for result in results:
                if getattr(getattr(result, "tool", None), "name", None) != "submit_battle_action":
                    continue
                try:
                    output = json.loads(result.output)
                except (AttributeError, TypeError, json.JSONDecodeError):
                    continue
                if output.get("accepted"):
                    return ToolsToFinalOutputResult(is_final_output=True, final_output=result.output)
            return ToolsToFinalOutputResult(is_final_output=False, final_output=None)

        client = AsyncOpenAI(
            api_key=self.debug.config_value("DEEPSEEK_API_KEY"),
            base_url=(self.debug.config_value("DEEPSEEK_BASE_URL", "https://api.deepseek.com") or "https://api.deepseek.com").rstrip("/"),
        )
        model = OpenAIResponsesModel(
            model=runtime.state.get("model"),
            openai_client=client,
        )
        agent = Agent(
            name="PALPAL battle child",
            instructions=BATTLE_INSTRUCTIONS,
            model=model,
            tools=[get_tool, item_tool, submit_tool],
            tool_use_behavior=finish,
            model_settings=ModelSettings(
                include_usage=True,
                reasoning={"effort": AGENT_REASONING_EFFORT},
                tool_choice="auto",
            ),
            reset_tool_choice=False,
        )
        session = SQLiteSession(self._control()["child_session_id"], runtime.sqlite_path)
        await runtime._migrate_sdk_session_history(session)
        prompt_value = {"battle": player_battle}
        if feedback:
            prompt_value["feedback"] = feedback
        prompt = json.dumps(prompt_value, ensure_ascii=False, separators=(",", ":"))
        await self._maybe_compact_sdk(session, prompt)
        for attempt in range(2):
            stream = Runner.run_streamed(
                agent, prompt, session=session, max_turns=6,
            )
            runtime.active_stream = stream
            request_completed = False
            try:
                async for _event in stream.stream_events():
                    if runtime.cancel_event.is_set():
                        stream.cancel("immediate")
                request_completed = True
            finally:
                usage = getattr(getattr(stream, "context_wrapper", None), "usage", None)
                if usage is not None:
                    entries = runtime._record_provider_usage(
                        usage,
                        "battle",
                        model=runtime.state.get("model"),
                    )
                    if entries and entries[-1]["input_tokens"] > 0:
                        self._persist_control(last_prompt_tokens=entries[-1]["input_tokens"])
                if request_completed:
                    updates: dict[str, Any] = {}
                    updates["survival_observations"] = risk_checkpoint
                    if "experience" in player_battle:
                        updates["experience_sent"] = True
                    if prompt_outcome_sequence:
                        updates["last_sent_outcome_sequence"] = max(
                            prompt_outcome_sequence,
                            int(self._control().get("last_sent_outcome_sequence") or 0),
                        )
                    self._mark_enemies_sent(latest_sent_battle)
                    if updates:
                        self._persist_control(**updates)
            if submitted is not None:
                break
            if attempt == 0:
                self._emit(
                    "protocol_retry",
                    "Battle Agent ended without an accepted submit_battle_action; correcting once.",
                    {"api": "responses", "reasoning_effort": AGENT_REASONING_EFFORT},
                )
                prompt = (
                    "Your previous response ended without an accepted submit_battle_action tool call. "
                    "Continue from the persisted tool results and call submit_battle_action after any "
                    "required queries. Do not end with plain text."
                )
        if submitted is None:
            raise RuntimeError(
                "submit_battle_action_protocol_error: no accepted submit_battle_action "
                "after correction retry"
            )
        return submitted

    def _summary(self, state: dict, instruction_count: int) -> dict:
        control = self._control()
        expected_generation = int(control.get("battle_generation") or 0)
        candidate = state.get("last_battle") if isinstance(state.get("last_battle"), dict) else {}
        try:
            candidate_generation = int(candidate.get("generation") or 0)
        except (TypeError, ValueError):
            candidate_generation = 0
        last = candidate if candidate_generation == expected_generation else {}
        result = str(last.get("result") or "terminated")
        players = []
        for player in last.get("players") or []:
            if not isinstance(player, dict):
                continue
            players.append({
                key: player.get(key) for key in (
                    "index", "role_id", "name", "hp_before", "hp_after",
                    "mp_before", "mp_after", "level_before", "level_after",
                    "knocked_out", "status_before", "status_after", "new_magics",
                )
            })
        used_items: dict[str, int] = {}
        for action in self.actions:
            if action.get("action_type") in {"use_item", "throw_item"}:
                key = str(int(action.get("object_id") or 0))
                used_items[key] = used_items.get(key, 0) + 1
        native_enemies = []
        for entry in last.get("enemies") or []:
            if not isinstance(entry, dict):
                continue
            try:
                object_id = int(entry.get("object_id") or 0)
            except (TypeError, ValueError):
                continue
            if object_id <= 0:
                continue
            native_enemies.append({
                "object_id": object_id,
                "name": str(entry.get("name") or ""),
            })
        fallback_enemies = []
        for entry in control.get("encounter_enemies") or []:
            if not isinstance(entry, dict):
                continue
            try:
                object_id = int(entry.get("object_id") or 0)
            except (TypeError, ValueError):
                continue
            if object_id <= 0:
                continue
            fallback_enemies.append({
                "object_id": object_id,
                "name": str(entry.get("name") or ""),
            })
        enemy_details = native_enemies or fallback_enemies
        enemy_identity_source = (
            "last_battle"
            if native_enemies
            else "battle_start_fallback"
            if fallback_enemies
            else "unavailable"
        )
        return {
            "battle_generation": last.get("generation") or expected_generation,
            "result": result,
            "is_boss": bool(last.get("is_boss")),
            "enemies": [entry["name"] for entry in enemy_details],
            "enemy_details": enemy_details,
            "enemy_identity_source": enemy_identity_source,
            "trigger_context": control.get("trigger_context"),
            "instruction_count": instruction_count,
            "duration_ms": last.get("duration_ms"),
            "players": players,
            "used_items": [
                {"object_id": int(object_id), "count": count}
                for object_id, count in sorted(used_items.items(), key=lambda item: int(item[0]))
            ],
            "exp_gained": int(last.get("exp_gained") or 0),
            "cash_gained": int(last.get("cash_gained") or 0),
        }

    def _compact_at_tokens(self) -> int:
        raw = self.debug.config_value("PALPAL_BATTLE_COMPACT_AT_TOKENS", "")
        if not raw:
            raw = self.debug.config_value(
                "PALPAL_AGENT_COMPACT_AT_TOKENS", str(DEFAULT_COMPACT_AT_TOKENS)
            )
        try:
            threshold = int(raw or DEFAULT_COMPACT_AT_TOKENS)
        except (TypeError, ValueError):
            return DEFAULT_COMPACT_AT_TOKENS
        return threshold if threshold > 0 else DEFAULT_COMPACT_AT_TOKENS

    def _compact_retain_tokens(self) -> int:
        raw = self.debug.config_value("PALPAL_BATTLE_COMPACT_RETAIN_TOKENS", "")
        if not raw:
            raw = self.debug.config_value(
                "PALPAL_AGENT_COMPACT_RETAIN_TOKENS", str(DEFAULT_COMPACT_RETAIN_TOKENS)
            )
        try:
            budget = int(raw or DEFAULT_COMPACT_RETAIN_TOKENS)
        except (TypeError, ValueError):
            return DEFAULT_COMPACT_RETAIN_TOKENS
        return budget if budget > 0 else DEFAULT_COMPACT_RETAIN_TOKENS

    async def _maybe_compact_sdk(self, session: Any, next_prompt: str) -> bool:
        control = self._control()
        observed_tokens = control.get("last_prompt_tokens")
        threshold = self._compact_at_tokens()
        retain_budget = self._compact_retain_tokens()
        if not isinstance(observed_tokens, int) or observed_tokens < max(1000, threshold):
            return False
        items = await session.get_items()
        if len(items) < 6:
            return False
        recent_start, retained_cycles = self.runtime._compaction_retained_start(
            items, retain_budget
        )
        if recent_start <= 0:
            return False
        retained_items = items[recent_start:]
        before = self._context_estimate_breakdown(items, next_prompt)
        retained_estimated_tokens = self.runtime._estimate_items_tokens(retained_items)
        starting_details = {
            "observed_prompt_tokens": observed_tokens,
            "compact_at_tokens": threshold,
            "retain_tokens": retain_budget,
            "items": len(items),
        }
        self._emit(
            "compression_started",
            "Battle context compression started.",
            starting_details,
        )
        summary = {
            "battle_history_summary": {
                "game_instance_id": control.get("game_instance_id"),
                "battle_generation": int(control.get("battle_generation") or 0),
                "instruction_count": int(control.get("instruction_count") or 0),
                "submitted_actions": copy.deepcopy(control.get("actions") or self.actions),
                "note": (
                    "Older model transcript was compacted. The newest battle prompt and "
                    "player-visible outcomes are authoritative."
                ),
            }
        }
        replacement = [
            {
                "role": "user",
                "content": json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            },
            {
                "role": "assistant",
                "content": "I will use the current battle snapshot and legal actions.",
            },
            *retained_items,
        ]
        replacement_breakdown = self._context_estimate_breakdown(
            replacement, next_prompt
        )
        transcript_replaced = False
        try:
            await session.clear_session()
            transcript_replaced = True
            await session.add_items(replacement)
        except asyncio.CancelledError:
            if transcript_replaced:
                await session.clear_session()
                await session.add_items(items)
            self._emit(
                "compression_cancelled",
                "Battle context compression cancelled.",
                starting_details,
            )
            raise
        except Exception as exc:
            if transcript_replaced:
                await session.clear_session()
                await session.add_items(items)
            self._emit(
                "compression_failed",
                f"Battle context compression failed: {exc}",
                {**starting_details, "error": str(exc)},
            )
            raise RuntimeError("battle_context_compaction_failed") from exc
        details = {
            **starting_details,
            "before_estimated_tokens": before["estimated_tokens"],
            "before_composition": before["composition"],
            "retained_cycles": retained_cycles,
            "retained_items": len(retained_items),
            "retained_estimated_tokens": retained_estimated_tokens,
            "retained_budget_exceeded": retained_estimated_tokens > retain_budget,
            "replacement_estimated_tokens": replacement_breakdown["estimated_tokens"],
            "replacement_composition": replacement_breakdown["composition"],
        }
        self._persist_control(
            last_prompt_tokens=None,
            compactions=int(control.get("compactions") or 0) + 1,
            last_compaction=details,
        )
        self._emit(
            "compression_completed",
            "Battle context compression completed.",
            details,
        )
        return True

    def _context_estimate_breakdown(self, items: list[dict], next_prompt: str) -> dict:
        composition = self.runtime._item_estimate_composition(items)
        composition["next_prompt"] = self.runtime._estimate_text_tokens(next_prompt)
        return {
            "estimated_tokens": sum(composition.values()),
            "composition": composition,
        }

    async def run(self, initial_state: dict) -> dict:
        control = self._begin(initial_state)
        generation = int(control["battle_generation"])
        invalid = 0
        feedback = ""
        while True:
            state = await asyncio.to_thread(self._wait_for_control_or_end, generation)
            battle = self._battle_from(state)
            if (
                battle is None
                or int(battle.get("generation") or 0) != generation
                or self._matching_last_battle(state, generation) is not None
            ):
                if (
                    battle is not None
                    and self._matching_last_battle(state, generation) is None
                ):
                    raise RuntimeError("battle_generation_changed")
                summary = self._summary(state, int(self._control().get("instruction_count") or 0))
                self.debug.record_battle_reappearance_candidate(summary, state)
                self._persist_control(
                    status="completed",
                    phase="ended",
                    stop_reason=summary["result"],
                    ended_at=_stamp(),
                    summary=summary,
                    actions=self.actions,
                )
                self._emit(
                    "completed",
                    (
                        f"Battle {summary.get('result')}: "
                        f"{', '.join(summary.get('enemies') or ['unknown enemies'])}."
                    ),
                    {"summary": summary},
                )
                return summary
            count = int(self._control().get("instruction_count") or 0)
            if count >= BATTLE_INSTRUCTION_LIMIT:
                raise RuntimeError("battle_instruction_limit")
            self._persist_control(phase=str(battle.get("phase") or "resolving"))
            request_details = {"instruction": count + 1}
            if feedback:
                request_details["feedback"] = feedback
            if (
                self.runtime.battle_decision_provider is None
                and self.debug.config_value("DEEPSEEK_API_KEY")
            ):
                request_details.update(
                    api="responses",
                    reasoning_effort=AGENT_REASONING_EFFORT,
                )
            self._emit(
                "request",
                f"Battle Agent requested action {count + 1}.",
                request_details,
            )
            native_action_started = False
            frozen_window = self._command_window(state)
            try:
                args = await self._provider_action(state, feedback)
                latest = await asyncio.to_thread(self.debug.snapshot)
                latest_window = self._command_window(latest)
                if latest_window["game_instance_id"] != frozen_window["game_instance_id"]:
                    raise RuntimeError("game_instance_changed")
                latest_battle = self._battle_from(latest)
                if latest_battle is None:
                    continue
                if latest_window["battle_generation"] != frozen_window["battle_generation"]:
                    if self._matching_last_battle(latest, generation) is not None:
                        continue
                    raise RuntimeError("battle_generation_changed")
                if (
                    latest_window["command_generation"] != frozen_window["command_generation"]
                    or latest_window["player_index"] != frozen_window["player_index"]
                ):
                    self._emit(
                        "resample",
                        "Battle decision became stale before submission; sampling the current command window.",
                        {"expected": frozen_window, "current": latest_window},
                    )
                    feedback = (
                        "The command window advanced while deciding. Discard the old decision, "
                        "reread the current battle, and submit one legal action."
                    )
                    continue
                args = {
                    **args,
                    **frozen_window,
                }
                self._emit(
                    "tool_called",
                    "Battle Agent submitted the native battle action.",
                    {"tool": "native_battle_action", "arguments": args},
                )
                native_action_started = True
                result = await asyncio.to_thread(self.debug.write_battle_action, args)
                self._emit(
                    "tool_output",
                    "Native battle action accepted.",
                    {"tool": "native_battle_action", "output": result},
                )
            except Exception as exc:
                decision_failed = str(exc).startswith("submit_battle_action_protocol_error:")
                stale_command = str(exc) == "stale_battle_command"
                if native_action_started:
                    self._emit(
                        "tool_output",
                        (
                            "Native battle action became stale before acceptance."
                            if stale_command
                            else "Native battle action failed."
                        ),
                        {"tool": "native_battle_action", "output": {"ok": False, "error": str(exc)}},
                    )
                if stale_command:
                    self._emit(
                        "resample",
                        "The native command window advanced before the action write; no action was sent.",
                        {"error": "stale_battle_command"},
                    )
                    feedback = (
                        "The command window advanced before native acceptance. Reread the current "
                        "battle and submit one legal action."
                    )
                    continue
                invalid += 1
                self._emit(
                    "retry",
                    (
                        f"Battle decision attempt {invalid} did not submit an action; replanning."
                        if decision_failed
                        else f"Battle action attempt {invalid} failed; replanning."
                    ),
                    {
                        "attempt": invalid,
                        "error": str(exc),
                        "failure_stage": "decision" if decision_failed else "execution",
                    },
                )
                if invalid >= 3:
                    raise RuntimeError("battle_replan_exhausted") from exc
                feedback = f"Previous submission failed ({exc}); reread battle and submit a current legal action."
                continue
            invalid = 0
            count += 1
            action = {
                key: result.get(key) for key in (
                    "battle_generation", "command_generation", "player_index",
                    "action_type", "object_id", "target_index",
                )
            }
            self.actions.append(action)
            self._persist_control(
                instruction_count=count,
                phase="resolving",
                actions=self.actions,
            )
            await asyncio.to_thread(
                self._wait_for_control_or_end,
                generation,
                int(frozen_window["command_generation"]),
            )
            feedback = ""


class AgentRuntime:
    """Persistent, single-controller agent loop for the synchronous debug server.

    The runtime owns a dedicated asyncio loop. Model work can use the Agents SDK while
    game execution remains serialized through DebugState's existing file bridge.
    """

    def __init__(self, debug_state: Any, runtime_dir: Path):
        self.debug = debug_state
        self.root = runtime_dir / "agent"
        self.root.mkdir(parents=True, exist_ok=True)
        self.control_path = self.root / "control.json"
        self.events_path = self.root / "events.jsonl"
        self.sqlite_path = self.root / "session.sqlite3"
        self.tool_outputs_root = self.root / "tool_outputs"
        self.tool_outputs_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.event_condition = threading.Condition(self.lock)
        self.event_cache: deque[dict] = deque(maxlen=EVENT_CACHE_SIZE)
        self._event_seq = 0
        self._load_event_tail()
        self.cancel_event = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.run_future: Future | None = None
        self.active_stream: Any = None
        self.active_compression_task: asyncio.Task | None = None
        self.generation = 0
        # Tests may replace this with a sync or async callable(state, model, feedback).
        self.decision_provider: Callable[..., Any] | None = None
        self.battle_decision_provider: Callable[..., Any] | None = None
        self.state = self._load_or_create()
        self.battle_delegate = BattleDelegate(self)

    def _blank(
        self,
        reason: str = "created",
        *,
        game_instance_id: str | None = None,
        parent_session_id: str | None = None,
        checkpoint_id: str | None = None,
        restored_from_session_id: str | None = None,
    ) -> dict:
        now = _stamp()
        return {
            "session_id": uuid.uuid4().hex,
            "game_instance_id": game_instance_id,
            "parent_session_id": parent_session_id,
            "checkpoint_id": checkpoint_id,
            "restored_from_session_id": restored_from_session_id,
            "model": self.debug.deepseek_model(),
            "status": "paused",
            "phase": "idle",
            "turn": 0,
            "run_turn": 0,
            "max_turns": 10,
            "pending_decision": None,
            "last_execution": None,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "last_request_usage": {},
            "last_prompt_tokens": None,
            "provider_usage": {
                "version": 1,
                "tracking_started_at": None,
                "incomplete_before_tracking": False,
                "scopes": {
                    scope: {
                        "requests": 0,
                        "input_tokens": 0,
                        "cached_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "total_tokens": 0,
                        "details_complete": True,
                    }
                    for scope in PROVIDER_USAGE_SCOPES
                },
                "last_request": None,
            },
            "last_compaction": None,
            "compactions": 0,
            "stop_reason": reason,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "ended_at": now,
            "last_event_seq": self._last_event_seq(),
            "generation": 0,
            "pending_game_instance_id": None,
            "equipment_review": {
                "ownership_counts": None,
                "pending": [],
            },
            "battle_delegate": None,
            "task_reconciliation_pending": False,
            "task_reconciliation_reason": None,
            "room_inspection": {
                "baseline_required": True,
                "last_observed_room_id": None,
                "pending_room_id": None,
                "inspected_rooms": {},
            },
            "room_entry_context": None,
        }

    def _load_or_create(self) -> dict:
        try:
            value = json.loads(self.control_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("control state is not an object")
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            value = self._blank()
            self._persist(value)
            return value
        if value.get("status") in RUNNING_PHASES:
            value.update(status="paused", phase="paused", stop_reason="server_restarted", ended_at=_stamp())
            self._persist(value)
            self._event_for(value, "pause", "paused", "Server restarted; automatic play did not resume.")
        legacy_task_state = "task_reconciliation_pending" not in value
        value.setdefault("last_request_usage", {})
        value.setdefault("last_prompt_tokens", None)
        if not isinstance(value.get("provider_usage"), dict):
            legacy_usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
            provider_usage = self._blank()["provider_usage"]
            provider_usage["incomplete_before_tracking"] = bool(
                int(value.get("turn") or 0)
                or any(int(legacy_usage.get(key) or 0) for key in legacy_usage if str(key).endswith("tokens"))
            )
            value["provider_usage"] = provider_usage
        self._normalize_provider_usage(value["provider_usage"])
        value.setdefault("last_compaction", None)
        # Older sessions exposed a local byte-size estimate as if it were context
        # usage. Never migrate that approximation into the provider-observed field.
        value.pop("context_estimate_tokens", None)
        value.setdefault("game_instance_id", None)
        value.setdefault("parent_session_id", None)
        value.setdefault("checkpoint_id", None)
        value.setdefault("restored_from_session_id", None)
        value.setdefault("pending_game_instance_id", None)
        room_inspection = value.get("room_inspection")
        if not isinstance(room_inspection, dict):
            room_inspection = {
                "baseline_required": True,
                "last_observed_room_id": None,
                "pending_room_id": None,
                "inspected_rooms": {},
            }
            value["room_inspection"] = room_inspection
        room_inspection.setdefault("baseline_required", False)
        room_inspection.setdefault("last_observed_room_id", None)
        room_inspection.setdefault("pending_room_id", None)
        if not isinstance(room_inspection.get("inspected_rooms"), dict):
            room_inspection["inspected_rooms"] = {}
        room_entry_context = value.get("room_entry_context")
        if not isinstance(room_entry_context, dict):
            value["room_entry_context"] = None
        elif room_entry_context.get("status") not in {"entered", "progressed", "recovery_pending"}:
            value["room_entry_context"] = None
        if legacy_task_state:
            value["task_reconciliation_pending"] = True
            value["task_reconciliation_reason"] = "legacy_control_upgrade"
        else:
            value["task_reconciliation_pending"] = value.get("task_reconciliation_pending") is True
            value.setdefault("task_reconciliation_reason", None)
        review = value.setdefault("equipment_review", {})
        if not isinstance(review, dict):
            review = {}
            value["equipment_review"] = review
        if review.get("ownership_counts") is not None and not isinstance(review.get("ownership_counts"), dict):
            review["ownership_counts"] = None
        review.setdefault("ownership_counts", None)
        if not isinstance(review.get("pending"), list):
            review["pending"] = []
        review.setdefault("pending", [])
        battle_delegate = value.get("battle_delegate")
        if isinstance(battle_delegate, dict) and battle_delegate.get("status") == "running":
            battle_delegate["status"] = "stopped"
            battle_delegate["phase"] = "stopped"
            battle_delegate["stop_reason"] = "server_restarted"
            battle_delegate["ended_at"] = _stamp()
        elif battle_delegate is not None and not isinstance(battle_delegate, dict):
            value["battle_delegate"] = None
        # Older control files carried a permanent macro goal. It is no longer
        # part of runtime state; silently discard it for in-place compatibility.
        value.pop("goal", None)
        self._persist(value)
        return value

    def _last_event_seq(self) -> int:
        return self._event_seq

    def _load_event_tail(self) -> None:
        """Restore the sequence and bounded live cache without scanning the whole log."""
        recovered: list[dict] = []
        try:
            with self.events_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                position = handle.tell()
                pending = b""
                while position > 0 and len(recovered) < EVENT_CACHE_SIZE:
                    size = min(EVENT_TAIL_CHUNK_SIZE, position)
                    position -= size
                    handle.seek(position)
                    parts = (handle.read(size) + pending).split(b"\n")
                    pending = parts[0]
                    for raw in reversed(parts[1:]):
                        if not raw.strip():
                            continue
                        try:
                            event = json.loads(raw.decode("utf-8"))
                            seq = int(event.get("seq") or 0)
                        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
                            continue
                        if seq > 0:
                            recovered.append(event)
                            if len(recovered) >= EVENT_CACHE_SIZE:
                                break
                if position == 0 and len(recovered) < EVENT_CACHE_SIZE and pending.strip():
                    try:
                        event = json.loads(pending.decode("utf-8"))
                        if int(event.get("seq") or 0) > 0:
                            recovered.append(event)
                    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
                        pass
        except OSError:
            return
        recovered.reverse()
        self.event_cache.extend(recovered)
        self._event_seq = max((int(event.get("seq") or 0) for event in recovered), default=0)

    def _notify_session_changed(self) -> None:
        self.event_condition.notify_all()

    def _atomic_json(self, path: Path, value: dict) -> None:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _atomic_text(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(value, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _valid_output_ref(output_ref: Any) -> bool:
        return isinstance(output_ref, str) and TOOL_OUTPUT_REF_RE.fullmatch(output_ref) is not None

    def _session_tool_outputs_dir(self, session_id: str | None = None) -> Path:
        wanted = str(session_id or self.state.get("session_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", wanted):
            raise ValueError("invalid session id")
        return self.tool_outputs_root / wanted

    def _archive_tool_output(self, raw_output: str) -> str:
        output_ref = f"tool-{uuid.uuid4().hex}.txt"
        self._atomic_text(self._session_tool_outputs_dir() / output_ref, raw_output)
        return output_ref

    def get_tool_output(self, output_ref: Any, offset: Any = 0) -> dict:
        if not self._valid_output_ref(output_ref):
            return {"ok": False, "error": "invalid_output_ref"}
        try:
            offset = int(offset or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_offset"}
        if offset < 0:
            return {"ok": False, "error": "invalid_offset"}
        path = self._session_tool_outputs_dir() / output_ref
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"ok": False, "error": "tool_output_not_found", "output_ref": output_ref}
        except (OSError, UnicodeError):
            return {"ok": False, "error": "tool_output_unreadable", "output_ref": output_ref}
        total_chars = len(content)
        start = min(offset, total_chars)
        end = min(total_chars, start + TOOL_OUTPUT_PAGE_CHARS)
        complete = end >= total_chars
        return {
            "ok": True,
            "output_ref": output_ref,
            "offset": start,
            "content": content[start:end],
            "next_offset": None if complete else end,
            "total_chars": total_chars,
            "complete": complete,
        }

    def _model_tool_result(self, name: str, result: dict) -> dict:
        """Project a full audited tool result only for the Responses model."""
        if not isinstance(result, dict) or result.get("ok") is not True:
            return result
        if name == "upsert_task":
            task = result.get("task") if isinstance(result.get("task"), dict) else {}
            history = task.get("history") if isinstance(task.get("history"), list) else []
            changes = (
                history[-1].get("changes")
                if history and isinstance(history[-1], dict) and isinstance(history[-1].get("changes"), dict)
                else {}
            )
            return {
                "ok": True,
                "operation": result.get("operation"),
                "task": {
                    key: task.get(key)
                    for key in (
                        "id", "status", "priority", "object_id", "transition_key", "room_id",
                    )
                    if key in task
                },
                "changed_fields": sorted(changes),
                "source_inbox_ids": list((changes.get("source_inbox_ids") or {}).get("added") or []),
            }
        if name == "get_navigation":
            projected = dict(result)
            navigation = result.get("navigation")
            if isinstance(navigation, dict):
                projected["navigation"] = {
                    key: value for key, value in navigation.items() if key != "environment_key"
                }
            return projected
        if name != "get_observations":
            return result
        projected = self.debug.project_agent_observations(result)
        raw = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if self._tool_output_needs_archive(raw):
            projected["output_ref"] = self._archive_tool_output(raw)
            projected["full_result_archived"] = True
        return projected

    def _final_model_tool_output(self, result: Any) -> str:
        """Serialize the exact result that the SDK will persist and replay."""
        output = (
            result
            if isinstance(result, str)
            else json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        )
        if self._tool_output_needs_archive(output):
            return self._archived_tool_output_preview(output, reason="output_limit")
        return output

    def _persist(self, value: dict | None = None) -> None:
        value = value or self.state
        value["updated_at"] = _stamp()
        self._atomic_json(self.control_path, value)

    def snapshot(self) -> dict:
        with self.lock:
            # API keys, prompts, and raw requests are never stored in this object.
            return json.loads(json.dumps(self.state, ensure_ascii=False))

    def compact_snapshot(self) -> dict:
        state = self.snapshot()
        compact = {key: state.get(key) for key in (
            "session_id", "model", "status", "phase", "turn", "run_turn",
            "max_turns", "stop_reason", "usage", "compactions", "last_event_seq",
            "last_request_usage", "last_prompt_tokens", "game_instance_id",
            "provider_usage",
            "last_compaction",
            "parent_session_id", "checkpoint_id", "restored_from_session_id",
            "task_reconciliation_pending", "task_reconciliation_reason",
        )}
        compact["equipment_review"] = {
            "pending": len((state.get("equipment_review") or {}).get("pending") or []),
        }
        compact["battle_delegate"] = state.get("battle_delegate")
        return compact

    def task_reconciliation_pending(self) -> bool:
        with self.lock:
            return self.state.get("task_reconciliation_pending") is True

    def task_reconciliation_reason(self) -> str | None:
        with self.lock:
            value = self.state.get("task_reconciliation_reason")
            return str(value) if value else None

    def mark_task_reconciliation_pending(self, reason: str) -> None:
        with self.lock:
            self.state["task_reconciliation_pending"] = True
            self.state["task_reconciliation_reason"] = str(reason)
            self._persist()

    def clear_task_reconciliation(self) -> None:
        with self.lock:
            if self.state.get("task_reconciliation_pending") is not True:
                return
            self.state["task_reconciliation_pending"] = False
            self.state["task_reconciliation_reason"] = None
            self._persist()

    @staticmethod
    def _equipment_ownership(state: dict) -> tuple[dict[str, int], dict[str, int], dict[str, str]]:
        ownership: dict[str, int] = {}
        available: dict[str, int] = {}
        names: dict[str, str] = {}
        for item in state.get("inventory") or []:
            if not isinstance(item, dict) or item.get("equipable") is not True:
                continue
            try:
                item_id = int(item.get("item_id") or 0)
                amount = max(0, int(item.get("amount") or 0))
                in_use = max(0, int(item.get("amount_in_use") or 0))
            except (TypeError, ValueError):
                continue
            if item_id <= 0:
                continue
            key = str(item_id)
            ownership[key] = ownership.get(key, 0) + amount
            available[key] = max(0, amount - in_use)
            names[key] = str(item.get("name") or "")
        for role in state.get("party") or []:
            if not isinstance(role, dict):
                continue
            for item in role.get("equipment") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    item_id = int(item.get("item_id") or 0)
                except (TypeError, ValueError):
                    continue
                if item_id <= 0:
                    continue
                key = str(item_id)
                ownership[key] = ownership.get(key, 0) + 1
                names[key] = str(item.get("name") or names.get(key) or "")
        return ownership, available, names

    def observe_equipment_state(self, state: dict) -> None:
        if state.get("status") in {"waiting_for_game", "state_parse_error"} or not state.get("in_main_game"):
            return
        ownership, available, names = self._equipment_ownership(state)
        with self.lock:
            review = self.state.setdefault("equipment_review", {"ownership_counts": None, "pending": []})
            previous = review.get("ownership_counts")
            if not isinstance(previous, dict):
                review["ownership_counts"] = ownership
                review["pending"] = []
                self._persist()
                return

            pending_by_id = {
                str(entry.get("item_id")): dict(entry)
                for entry in review.get("pending") or []
                if isinstance(entry, dict) and int(entry.get("item_id") or 0) > 0
            }
            changed = previous != ownership
            for key in sorted(set(previous) | set(ownership), key=lambda value: int(value)):
                delta = int(ownership.get(key) or 0) - int(previous.get(key) or 0)
                if delta > 0:
                    entry = pending_by_id.get(key) or {
                        "item_id": int(key),
                        "name": names.get(key) or "",
                        "new_count": 0,
                        "first_seen_at": _stamp(),
                    }
                    entry["name"] = names.get(key) or entry.get("name") or ""
                    entry["new_count"] = int(entry.get("new_count") or 0) + delta
                    pending_by_id[key] = entry
                elif delta < 0 and key in pending_by_id:
                    entry = pending_by_id[key]
                    entry["new_count"] = max(0, int(entry.get("new_count") or 0) + delta)

            for key, entry in list(pending_by_id.items()):
                remaining = min(
                    max(0, int(entry.get("new_count") or 0)),
                    max(0, int(available.get(key) or 0)),
                )
                if remaining <= 0:
                    pending_by_id.pop(key, None)
                else:
                    entry["new_count"] = remaining
                    entry["name"] = names.get(key) or entry.get("name") or ""

            pending = sorted(
                pending_by_id.values(),
                key=lambda entry: (str(entry.get("first_seen_at") or ""), int(entry.get("item_id") or 0)),
            )
            if pending != review.get("pending"):
                changed = True
            review["ownership_counts"] = ownership
            review["pending"] = pending
            if changed:
                self._persist()

    def pending_equipment_review(self) -> dict | None:
        with self.lock:
            pending = (self.state.get("equipment_review") or {}).get("pending") or []
            return json.loads(json.dumps(pending[0], ensure_ascii=False)) if pending else None

    def resolve_equipment_review(self, item_id: Any, *, count: int | None = None) -> None:
        try:
            wanted = int(item_id)
        except (TypeError, ValueError):
            return
        with self.lock:
            review = self.state.get("equipment_review") or {}
            pending = []
            changed = False
            for entry in review.get("pending") or []:
                if not isinstance(entry, dict) or int(entry.get("item_id") or 0) != wanted:
                    pending.append(entry)
                    continue
                changed = True
                if count is not None:
                    remaining = max(0, int(entry.get("new_count") or 0) - max(0, int(count)))
                    if remaining:
                        entry = dict(entry)
                        entry["new_count"] = remaining
                        pending.append(entry)
            if changed:
                review["pending"] = pending
                self.state["equipment_review"] = review
                self._persist()

    def _event_for(
        self,
        state: dict,
        event_type: str,
        phase: str,
        summary: str,
        details: dict | None = None,
        *,
        started: float | None = None,
    ) -> dict:
        with self.lock:
            seq = max(int(state.get("last_event_seq") or 0), self._last_event_seq()) + 1
            event = {
                "seq": seq,
                "session_id": state.get("session_id"),
                "turn": int(state.get("turn") or 0),
                "type": event_type,
                "phase": phase,
                "time": _stamp(),
                "elapsed": round(max(0.0, _now() - started), 3) if started else None,
                "summary": str(summary)[:1000],
                "details": self._safe_details(details or {}),
            }
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            state["last_event_seq"] = seq
            self._event_seq = seq
            self.event_cache.append(event)
            if state is getattr(self, "state", None):
                self._persist()
            self.event_condition.notify_all()
            return event

    @classmethod
    def _safe_details(cls, value: Any, depth: int = 0) -> Any:
        if depth > 6:
            return "[truncated]"
        if isinstance(value, dict):
            result = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 100:
                    result["_truncated"] = True
                    break
                normalized = str(key).lower().replace("-", "_")
                if normalized in {"api_key", "authorization", "system_prompt", "raw_request"}:
                    result[str(key)] = "[redacted]"
                else:
                    result[str(key)] = cls._safe_details(item, depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            return [cls._safe_details(item, depth + 1) for item in value[:100]]
        if isinstance(value, str):
            return value if len(value) <= 8000 else value[:8000] + "…[truncated]"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:1000]

    def emit(self, event_type: str, phase: str, summary: str, details: dict | None = None, *, started: float | None = None) -> dict:
        return self._event_for(self.state, event_type, phase, summary, details, started=started)

    def events(self, after: int = 0, limit: int = 200, session_id: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        wanted = session_id or self.state.get("session_id")
        after = int(after)
        with self.lock:
            cached = list(self.event_cache)
            oldest_cached_seq = int(cached[0].get("seq") or 0) if cached else self._event_seq + 1
            cache_is_complete = len(cached) < EVENT_CACHE_SIZE
            cache_can_answer = cache_is_complete or after >= oldest_cached_seq - 1
            if cache_can_answer:
                return [
                    event for event in cached
                    if event.get("session_id") == wanted and int(event.get("seq") or 0) > after
                ][:limit]
        return self._scan_events(after, limit, str(wanted or ""))

    def _scan_events(self, after: int, limit: int, session_id: str) -> list[dict]:
        result: list[dict] = []
        try:
            handle = self.events_path.open("r", encoding="utf-8")
        except OSError:
            return []
        with handle:
            for line in handle:
                try:
                    event = json.loads(line)
                    seq = int(event.get("seq") or 0)
                except (json.JSONDecodeError, UnicodeError, AttributeError, TypeError, ValueError):
                    continue
                if event.get("session_id") == session_id and seq > after:
                    result.append(event)
                    if len(result) >= limit:
                        break
        return result

    def _migrate_model_transcript_item(self, item: dict) -> dict:
        """Purely remove hidden battle fields from one legacy persisted item."""
        def project(value: Any) -> Any:
            if isinstance(value, list):
                return [project(entry) for entry in value]
            if not isinstance(value, dict):
                return value
            projected = {}
            for key, entry in value.items():
                if key == "battle" and isinstance(entry, dict):
                    visible = self.debug.player_battle_view({"battle": entry}) or {}
                    if isinstance(entry.get("recent_outcomes"), list):
                        visible["recent_outcomes"] = copy.deepcopy(entry["recent_outcomes"])
                    if isinstance(entry.get("experience"), dict):
                        visible["experience"] = copy.deepcopy(entry["experience"])
                    projected[key] = visible
                else:
                    projected[key] = project(entry)
            return projected

        current = dict(item)
        content = current.get("content")
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                return current
            projected = project(parsed)
            if projected != parsed:
                current["content"] = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
        elif isinstance(content, (dict, list)):
            current["content"] = project(content)
        return current

    def wait_for_events(
        self,
        after: int,
        limit: int = 200,
        session_id: str | None = None,
        timeout: float = 0.5,
    ) -> tuple[list[dict], bool]:
        """Wait for live events; the boolean reports whether the fixed session is still current."""
        wanted = str(session_id or self.state.get("session_id") or "")
        events = self.events(after, limit, wanted)
        if events:
            return events, True
        with self.event_condition:
            if str(self.state.get("session_id") or "") != wanted:
                return [], False
            self.event_condition.wait(max(0.0, float(timeout)))
            if str(self.state.get("session_id") or "") != wanted:
                return [], False
        return self.events(after, limit, wanted), True

    def event_turn_page(
        self,
        before_turn: int | None = None,
        turn_limit: int = 5,
        session_id: str | None = None,
    ) -> dict:
        """Return complete timeline turn groups, newest page first by cursor."""
        turn_limit = max(1, min(int(turn_limit), 50))
        wanted = session_id or self.state.get("session_id")
        events = self._all_session_events(str(wanted or ""))
        turns: dict[int, list[dict]] = {}
        normalized_events: list[tuple[int, dict]] = []
        for event in events:
            try:
                turn = max(0, int(event.get("turn") or 0))
            except (TypeError, ValueError):
                turn = 0
            turns.setdefault(turn, []).append(event)
            normalized_events.append((turn, event))

        numbered_turns = sorted(turn for turn in turns if turn > 0)
        candidates = numbered_turns
        if before_turn is not None:
            candidates = [turn for turn in candidates if turn < int(before_turn)]
        selected_turns = candidates[-turn_limit:]
        reached_oldest_turn = not numbered_turns or (
            bool(selected_turns) and selected_turns[0] == numbered_turns[0]
        )
        include_session = 0 in turns and reached_oldest_turn
        selected = set(selected_turns)
        page_events = [
            event for turn, event in normalized_events
            if turn in selected or (include_session and turn == 0)
        ]
        has_more = bool(
            (selected_turns and any(turn < selected_turns[0] for turn in numbered_turns))
            or (0 in turns and not include_session)
        )
        return {
            "session_id": wanted,
            "events": page_events,
            "has_more": has_more,
            "oldest_turn": selected_turns[0] if selected_turns else None,
            "turn_limit": turn_limit,
        }

    def _all_session_events(self, session_id: str) -> list[dict]:
        result: list[dict] = []
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return result
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("session_id") == session_id:
                result.append(event)
        return result

    async def _sdk_session_items(self, session_id: str) -> list[dict]:
        try:
            from agents import SQLiteSession
        except ImportError:
            return []
        session = SQLiteSession(session_id, self.sqlite_path)
        return list(await session.get_items())

    def _migrate_sqlite_history(self, session_id: str) -> bool:
        """Atomically migrate one SQLite transcript, returning whether it ran."""
        connection = sqlite3.connect(str(self.sqlite_path))
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS palpal_session_migrations (
                    session_id TEXT NOT NULL,
                    migration TEXT NOT NULL,
                    migrated_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, migration)
                )
                """
            )
            migration = "append_only_history_v1"
            if connection.execute(
                "SELECT 1 FROM palpal_session_migrations WHERE session_id = ? AND migration = ?",
                (session_id, migration),
            ).fetchone():
                connection.rollback()
                return False
            rows = connection.execute(
                "SELECT id, message_data FROM agent_messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
            for row_id, raw in rows:
                try:
                    item = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(item, dict):
                    continue
                migrated = self._migrate_model_transcript_item(item)
                if migrated != item:
                    connection.execute(
                        "UPDATE agent_messages SET message_data = ? WHERE id = ? AND session_id = ?",
                        (json.dumps(migrated, ensure_ascii=False), row_id, session_id),
                    )
            connection.execute(
                "INSERT INTO palpal_session_migrations (session_id, migration, migrated_at) VALUES (?, ?, ?)",
                (session_id, migration, _stamp()),
            )
            connection.commit()
            return True
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def _migrate_sdk_session_history(self, session: Any) -> bool:
        """Run the one-time history migration before a persisted session is used."""
        session_id = getattr(session, "session_id", None)
        db_path = getattr(session, "db_path", None)
        if not isinstance(session_id, str) or not session_id or db_path is None:
            return False
        if Path(db_path).resolve() != self.sqlite_path.resolve():
            return False
        migrated = await asyncio.to_thread(self._migrate_sqlite_history, session_id)
        if migrated:
            details = None
            control = self.state.get("battle_delegate")
            if (
                isinstance(control, dict)
                and str(control.get("child_session_id") or "") == session_id
            ):
                details = {
                    "battle_delegate": {
                        "child_session_id": session_id,
                        "battle_generation": int(control.get("battle_generation") or 0),
                        "group_id": self.battle_delegate._group_id(control),
                        "depth": 1,
                        "status": control.get("status"),
                    },
                }
            self.emit(
                "session",
                "history_migrated",
                "Session history migrated.",
                details,
            )
        return migrated

    def _clear_history_migration_marker(self, session_id: str) -> None:
        try:
            connection = sqlite3.connect(str(self.sqlite_path))
            try:
                connection.execute(
                    "DELETE FROM palpal_session_migrations WHERE session_id = ?",
                    (session_id,),
                )
                connection.commit()
            finally:
                connection.close()
        except sqlite3.OperationalError:
            return

    async def _replace_sdk_session_items(self, session_id: str, items: list[dict]) -> None:
        try:
            from agents import SQLiteSession
        except ImportError:
            if items:
                raise RuntimeError("openai-agents is required to restore checkpoint transcript")
            return
        session = SQLiteSession(session_id, self.sqlite_path)
        await session.clear_session()
        if items:
            await session.add_items(items)
        self._clear_history_migration_marker(session_id)

    async def _clear_sdk_session(self, session_id: str) -> bool:
        try:
            from agents import SQLiteSession
        except ImportError:
            return False
        session = SQLiteSession(session_id, self.sqlite_path)
        await session.clear_session()
        self._clear_history_migration_marker(session_id)
        return True

    def _checkpoint_tool_outputs(self, checkpoint_dir: Path, session_id: str) -> list[dict]:
        source_dir = self._session_tool_outputs_dir(session_id)
        if not source_dir.is_dir():
            return []
        target_dir = checkpoint_dir / "agent" / "tool_outputs"
        archived: list[dict] = []
        for source in sorted(source_dir.iterdir(), key=lambda item: item.name):
            if source.is_symlink() or not source.is_file() or not self._valid_output_ref(source.name):
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / source.name
            tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                shutil.copyfile(source, tmp)
                os.replace(tmp, target)
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
            archived.append({"output_ref": source.name, "bytes": source.stat().st_size})
        return archived

    def _restore_checkpoint_tool_outputs(
        self,
        checkpoint_dir: Path,
        entries: list[dict],
        session_id: str,
    ) -> None:
        source_dir = checkpoint_dir / "agent" / "tool_outputs"
        target_dir = self._session_tool_outputs_dir(session_id)
        wanted: set[str] = set()
        for entry in entries:
            output_ref = entry.get("output_ref") if isinstance(entry, dict) else None
            if not self._valid_output_ref(output_ref):
                raise ValueError("invalid checkpoint tool output reference")
            source = source_dir / output_ref
            if not source.is_file():
                raise ValueError("checkpoint tool output is missing")
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / output_ref
            tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                shutil.copyfile(source, tmp)
                os.replace(tmp, target)
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
            wanted.add(output_ref)
        # Replace semantics: drop files produced after the checkpoint point.
        if target_dir.is_dir():
            for existing in target_dir.iterdir():
                if (
                    existing.is_file()
                    and existing.name not in wanted
                    and not existing.name.endswith(".tmp")
                ):
                    try:
                        existing.unlink()
                    except FileNotFoundError:
                        pass

    def export_checkpoint(self, checkpoint_dir: Path) -> dict:
        with self.lock:
            if self.is_running():
                raise RuntimeError("agent_session_running")
            control = self.snapshot()
        transcript = self._submit(self._sdk_session_items(control["session_id"])).result(timeout=30)
        events = self._all_session_events(control["session_id"])
        tool_outputs = self._checkpoint_tool_outputs(checkpoint_dir, control["session_id"])
        bundle = {
            "version": CHECKPOINT_BUNDLE_VERSION,
            "saved_at": _stamp(),
            "source_session_id": control["session_id"],
            "control": control,
            "transcript": transcript,
            "events": events,
            "tool_outputs": tool_outputs,
        }
        path = checkpoint_dir / "agent" / "session.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_json(path, bundle)
        return {
            "version": CHECKPOINT_BUNDLE_VERSION,
            "source_session_id": control["session_id"],
            "turn": int(control.get("turn") or 0),
            "transcript_items": len(transcript),
            "events": len(events),
            "tool_outputs": len(tool_outputs),
        }

    def _read_events_file(self) -> list[dict]:
        events: list[dict] = []
        try:
            with self.events_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, UnicodeError):
                        continue
                    if isinstance(event, dict):
                        events.append(event)
        except OSError:
            return []
        return events

    def _rewrite_events_file(self, events: list[dict]) -> None:
        tmp = self.events_path.with_name(f"{self.events_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            os.replace(tmp, self.events_path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        self._event_seq = max((int(event.get("seq") or 0) for event in events), default=0)
        self.event_cache.clear()
        self.event_cache.extend(events[-EVENT_CACHE_SIZE:])
        self.state["last_event_seq"] = self._event_seq
        self._persist()
        self.event_condition.notify_all()

    def _replace_session_events(self, session_id: str, saved_events: list[dict]) -> None:
        """Replace one session's event suffix so the file mirrors the checkpoint prefix."""
        with self.lock:
            kept = [event for event in self._read_events_file() if event.get("session_id") != session_id]
            for source in saved_events:
                if not isinstance(source, dict):
                    continue
                event = {
                    "seq": int(source.get("seq") or 0),
                    "session_id": session_id,
                    "turn": int(source.get("turn") or 0),
                    "type": str(source.get("type") or "event"),
                    "phase": str(source.get("phase") or "restored"),
                    "time": source.get("time") or _stamp(),
                    "elapsed": source.get("elapsed"),
                    "summary": str(source.get("summary") or "Restored activity.")[:1000],
                    "details": self._safe_details(source.get("details") or {}),
                }
                if event["seq"] <= 0:
                    event["seq"] = max((int(existing.get("seq") or 0) for existing in kept), default=0) + 1
                kept.append(event)
            kept.sort(key=lambda item: int(item.get("seq") or 0))
            self._rewrite_events_file(kept)

    def restore_checkpoint(self, checkpoint_dir: Path, checkpoint_id: str, game_instance_id: str | None) -> dict:
        path = checkpoint_dir / "agent" / "session.json"
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(bundle, dict) or int(bundle.get("version") or 0) != CHECKPOINT_BUNDLE_VERSION:
                raise ValueError("unsupported agent checkpoint bundle")
            control = bundle.get("control")
            transcript = bundle.get("transcript")
            events = bundle.get("events")
            tool_outputs = bundle.get("tool_outputs", [])
            if (
                not isinstance(control, dict)
                or not isinstance(transcript, list)
                or not isinstance(events, list)
                or not isinstance(tool_outputs, list)
            ):
                raise ValueError("invalid agent checkpoint bundle")
            checkpoint_outputs_dir = checkpoint_dir / "agent" / "tool_outputs"
            for entry in tool_outputs:
                output_ref = entry.get("output_ref") if isinstance(entry, dict) else None
                source = checkpoint_outputs_dir / output_ref if self._valid_output_ref(output_ref) else None
                if source is None or source.is_symlink() or not source.is_file():
                    raise ValueError("invalid agent checkpoint tool output")
        except FileNotFoundError:
            return self.reset(
                "checkpoint_loaded_without_agent_bundle",
                game_instance_id=game_instance_id,
                checkpoint_id=checkpoint_id,
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return self.reset(
                "checkpoint_agent_bundle_incompatible",
                game_instance_id=game_instance_id,
                checkpoint_id=checkpoint_id,
            )

        source_session_id = str(bundle.get("source_session_id") or control.get("session_id") or "") or None
        with self.lock:
            if self.is_running():
                raise RuntimeError("agent_session_running")
            previous_session_id = self.state.get("session_id")
            # Rollback semantics: resume the checkpoint's own session instead of forking.
            target_session_id = source_session_id or previous_session_id
            self.generation += 1
            restored = self._blank(
                "checkpoint_restored",
                game_instance_id=game_instance_id,
                parent_session_id=None,
                checkpoint_id=checkpoint_id,
                restored_from_session_id=None,
            )
            restored["session_id"] = target_session_id
            for key in (
                "model", "turn", "max_turns", "pending_decision", "last_execution",
                "usage", "last_request_usage", "last_prompt_tokens", "compactions",
                "provider_usage",
                "last_compaction",
                "equipment_review",
                "room_inspection",
                "room_entry_context",
            ):
                if key in control:
                    restored[key] = control[key]
            if "provider_usage" not in control:
                legacy_usage = restored.get("usage") if isinstance(restored.get("usage"), dict) else {}
                restored["provider_usage"]["incomplete_before_tracking"] = bool(
                    int(restored.get("turn") or 0)
                    or any(int(value or 0) for key, value in legacy_usage.items() if str(key).endswith("tokens"))
                )
            self._normalize_provider_usage(restored["provider_usage"])
            restored.update(status="paused", phase="paused", run_turn=0, stop_reason="checkpoint_restored", generation=self.generation)
            restored["task_reconciliation_pending"] = True
            restored["task_reconciliation_reason"] = "checkpoint_restored"
            self.state = restored
            self._persist()
            self._notify_session_changed()
        try:
            self._restore_checkpoint_tool_outputs(checkpoint_dir, tool_outputs, restored["session_id"])
            self._submit(self._replace_sdk_session_items(restored["session_id"], transcript)).result(timeout=30)
        except Exception:
            return self.reset(
                "checkpoint_agent_transcript_restore_failed",
                game_instance_id=game_instance_id,
                checkpoint_id=checkpoint_id,
                parent_session_id=previous_session_id,
                restored_from_session_id=source_session_id,
            )
        self._replace_session_events(restored["session_id"], events)
        self.emit(
            "session",
            "paused",
            f"Restored checkpoint {checkpoint_id}.",
            {"checkpoint_id": checkpoint_id, "source_session_id": source_session_id},
        )
        return self.snapshot()

    def assert_session_scope(self, session_id: str | None) -> None:
        if str(session_id or "") != str(self.state.get("session_id") or ""):
            raise ValueError("session_out_of_scope")

    def discover_session_ids(self) -> set[str]:
        ids: set[str] = set()
        for event in self._read_events_file():
            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id:
                ids.add(session_id)
        if self.tool_outputs_root.is_dir():
            for child in self.tool_outputs_root.iterdir():
                if child.is_dir() and re.fullmatch(r"[0-9a-f]{32}", child.name):
                    ids.add(child.name)
        try:
            import sqlite3
        except ImportError:
            sqlite3 = None
        if sqlite3 is not None:
            try:
                connection = sqlite3.connect(str(self.sqlite_path))
                try:
                    for row in connection.execute("SELECT session_id FROM agent_sessions"):
                        session_id = row[0] if row else None
                        if isinstance(session_id, str) and session_id:
                            ids.add(session_id)
                finally:
                    connection.close()
            except Exception:
                pass
        return ids

    def prune_sessions(self, session_ids: set[str]) -> dict:
        with self.lock:
            if self.is_running():
                raise RuntimeError("agent_session_running")
            live = str(self.state.get("session_id") or "")
            protected = {live}
            battle_delegate = self.state.get("battle_delegate")
            if isinstance(battle_delegate, dict):
                child = battle_delegate.get("child_session_id")
                if isinstance(child, str) and child:
                    protected.add(child)
            target = {str(session_id) for session_id in session_ids if str(session_id)}
            skipped_live = sorted(target & {live})
            skipped_protected = sorted(target & protected)
            target.difference_update(protected)

            events_removed = 0
            if target:
                all_events = self._read_events_file()
                kept = [event for event in all_events if event.get("session_id") not in target]
                events_removed = len(all_events) - len(kept)
                if events_removed:
                    self._rewrite_events_file(kept)

            sqlite_sessions_cleared = 0
            sqlite_skipped: list[str] = []
            for session_id in sorted(target):
                try:
                    cleared = self._submit(self._clear_sdk_session(session_id)).result(timeout=30)
                except Exception:
                    cleared = False
                if cleared:
                    sqlite_sessions_cleared += 1
                else:
                    sqlite_skipped.append(session_id)

            tool_output_dirs_removed = 0
            for session_id in sorted(target):
                try:
                    directory = self._session_tool_outputs_dir(session_id)
                except ValueError:
                    continue
                if directory.is_dir():
                    shutil.rmtree(directory)
                    tool_output_dirs_removed += 1

            return {
                "events_removed": events_removed,
                "sqlite_sessions_cleared": sqlite_sessions_cleared,
                "sqlite_skipped": sqlite_skipped,
                "tool_output_dirs_removed": tool_output_dirs_removed,
                "skipped_live": skipped_live,
                "skipped_protected": skipped_protected,
                "pruned_session_ids": sorted(target),
            }

    def _ensure_loop(self) -> None:
        with self.lock:
            if self.loop and self.thread and self.thread.is_alive():
                return
            ready = threading.Event()

            def run_loop() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self.loop = loop
                ready.set()
                loop.run_forever()

            self.thread = threading.Thread(target=run_loop, name="palpal-agent-runtime", daemon=True)
            self.thread.start()
        ready.wait(2.0)

    def _submit(self, coroutine: Any) -> Future:
        self._ensure_loop()
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def is_running(self) -> bool:
        return self.state.get("status") in RUNNING_PHASES

    def reset(
        self,
        reason: str = "reset",
        *,
        game_instance_id: str | None = None,
        parent_session_id: str | None = None,
        checkpoint_id: str | None = None,
        restored_from_session_id: str | None = None,
    ) -> dict:
        self.debug.observations.cancel_all("agent_reset", owner="navigation")
        self.debug.observations.cancel_all("agent_reset", owner="dialog_advance")
        with self.lock:
            if self.is_running():
                raise RuntimeError("agent_session_running")
            if game_instance_id is None:
                game_instance_id = self.state.get("game_instance_id")
            self.generation += 1
            self.state = self._blank(
                reason,
                game_instance_id=game_instance_id,
                parent_session_id=parent_session_id,
                checkpoint_id=checkpoint_id,
                restored_from_session_id=restored_from_session_id,
            )
            if checkpoint_id is not None or reason.startswith("checkpoint_"):
                self.state["task_reconciliation_pending"] = True
                self.state["task_reconciliation_reason"] = "checkpoint_restored"
            self.state["generation"] = self.generation
            self._persist()
            self._notify_session_changed()
            self.emit("session", "paused", f"New session created: {reason}.")
            return self.snapshot()

    def observe_game_instance(self, value: Any) -> None:
        instance_id = str(value or "").strip()[:128]
        if not instance_id:
            return
        with self.lock:
            current = str(self.state.get("game_instance_id") or "")
            if current == instance_id:
                return
            if not current and int(self.state.get("turn") or 0) == 0:
                self.state["game_instance_id"] = instance_id
                self._persist()
                return
            if self.is_running():
                was_running = True
                self.state["pending_game_instance_id"] = instance_id
                self.state.update(status="interrupting", phase="interrupting", stop_reason="game_instance_changed")
                self.cancel_event.set()
                self._persist()
                stream = self.active_stream
                compression_task = self.active_compression_task
                loop = self.loop
            else:
                was_running = False
                stream = None
                compression_task = None
                loop = None
        if stream is not None and loop is not None:
            loop.call_soon_threadsafe(stream.cancel, "immediate")
        self._cancel_async_task(compression_task)
        if was_running:
            self.emit("interrupt", "interrupting", "Game instance changed; discarding the previous session after cancellation.")
            return
        self.reset("game_instance_changed", game_instance_id=instance_id)

    def start(self, model: str | None, max_turns: int, *, decide_only: bool = False) -> dict:
        max_turns = max(1, min(int(max_turns), 100))
        if decide_only and self.debug.snapshot().get("in_battle"):
            raise RuntimeError("decide_once_unavailable_in_battle")
        with self.lock:
            if self.is_running():
                raise RuntimeError("agent_session_running")
            self.cancel_event.clear()
            self.generation += 1
            generation = self.generation
            self.state.update(
                model=self.debug.deepseek_model(model),
                status="running",
                phase="deciding",
                run_turn=0,
                max_turns=1 if decide_only else max_turns,
                stop_reason=None,
                started_at=_stamp(),
                ended_at=None,
                generation=generation,
            )
            self._persist()
            self.emit("run", "deciding", "Agent run started.", {"max_turns": self.state["max_turns"], "decide_only": decide_only})
            self.run_future = self._submit(self._run(generation, decide_only=decide_only))
            return self.snapshot()

    def interrupt(self) -> dict:
        with self.debug.navigation_action_lock:
            with self.lock:
                if not self.is_running():
                    return self.snapshot()
                self.cancel_event.set()
                self.state.update(status="interrupting", phase="interrupting", stop_reason="interrupt_requested")
                self._persist()
                stream = self.active_stream
                compression_task = self.active_compression_task
                loop = self.loop
            self.debug.cancel_dialog_advance()
            pending = self.debug.pending_transition_observation
            if isinstance(pending, dict) and pending.get("owner") == "navigation":
                self.debug.terminate_pending_transition_observation(
                    pending,
                    "agent_interrupt",
                    "agent_interrupt",
                )
            self.debug.observations.cancel_all("agent_interrupt", owner="navigation")
            self.debug.pending_interaction_observation = None
        delegate = self.state.get("battle_delegate")
        if isinstance(delegate, dict) and delegate.get("status") == "running":
            delegate.update(status="stopped", phase="stopped", stop_reason="interrupted", ended_at=_stamp())
            with self.lock:
                self._persist()
            self.battle_delegate._emit(
                "stopped",
                "Battle Agent stopped by interruption.",
                {"reason": "interrupted"},
            )
        if stream is not None and loop is not None:
            loop.call_soon_threadsafe(stream.cancel, "immediate")
        self._cancel_async_task(compression_task)
        self.emit("interrupt", "interrupting", "Immediate interrupt requested.")
        return self.snapshot()

    @staticmethod
    def _cancel_async_task(task: asyncio.Task | None) -> None:
        if task is None or task.done():
            return
        task.get_loop().call_soon_threadsafe(task.cancel)

    async def execute_pending(self) -> dict:
        with self.lock:
            if self.is_running():
                raise RuntimeError("agent_session_running")
            decision = self.state.get("pending_decision")
            if not isinstance(decision, dict):
                raise RuntimeError("no_pending_decision")
            self.state.update(status="running", phase="executing", stop_reason=None)
            self._persist()
        try:
            result = await asyncio.to_thread(self._execute_decision, decision)
            with self.lock:
                self.state.update(last_execution=result, pending_decision=None)
            action_result = result.get("result") if isinstance(result, dict) else None
            stop_reason = (
                "interrupted_by_battle"
                if isinstance(action_result, dict) and action_result.get("status") == "interrupted_by_battle"
                else "manual_execute_complete"
            )
            self._pause(stop_reason)
            return result
        except Exception as exc:
            self.emit("error", "error", f"Execution failed: {exc}")
            self._pause("execution_failed")
            raise

    def execute(self) -> dict:
        future = self._submit(self.execute_pending())
        return future.result(timeout=300)

    def _pause(
        self,
        reason: str,
        *,
        details: dict | None = None,
        summary: str | None = None,
    ) -> None:
        with self.lock:
            self.state.update(status="paused", phase="paused", stop_reason=reason, ended_at=_stamp())
            self._persist()
        self.emit("pause", "paused", summary or f"Agent paused: {reason}.", details)
        with self.lock:
            pending_instance = self.state.get("pending_game_instance_id")
            if pending_instance:
                self.state["pending_game_instance_id"] = None
                self._persist()
        if pending_instance:
            self.reset("game_instance_changed", game_instance_id=str(pending_instance))

    @staticmethod
    def _room_screen_obscured(state: dict) -> bool:
        control = state.get("control") if isinstance(state.get("control"), dict) else {}
        if control.get("phase") in {"dialog", "choice", "shop", "battle"}:
            return True
        dialog = state.get("dialog") if isinstance(state.get("dialog"), dict) else {}
        choice = state.get("choice") if isinstance(state.get("choice"), dict) else {}
        shop = state.get("shop") if isinstance(state.get("shop"), dict) else {}
        return bool(
            state.get("in_battle")
            or dialog.get("active") is True
            or choice.get("active") is True
            or shop.get("active") is True
        )

    def _observe_room_for_inspection(self, state: dict) -> str | None:
        identity = self.debug.room_identity(state)
        room_id = identity.get("id") if isinstance(identity, dict) else None
        if not isinstance(room_id, str) or not room_id:
            return None
        with self.lock:
            control = self.state.setdefault("room_inspection", {})
            inspected = control.setdefault("inspected_rooms", {})
            previous = control.get("last_observed_room_id")
            if control.get("baseline_required") is True:
                control["baseline_required"] = False
                control["last_observed_room_id"] = room_id
                control["pending_room_id"] = None
                self._persist()
                return room_id
            if previous != room_id:
                control["last_observed_room_id"] = room_id
                control["pending_room_id"] = None if room_id in inspected else room_id
                self._persist()
            elif control.get("pending_room_id") not in {None, room_id}:
                control["pending_room_id"] = None if room_id in inspected else room_id
                self._persist()
        return room_id

    def _record_room_inspection(self, room_id: str, result: dict) -> None:
        if result.get("ok") is not True:
            return
        with self.lock:
            control = self.state.setdefault("room_inspection", {})
            inspected = control.setdefault("inspected_rooms", {})
            inspected[room_id] = {
                "inspected_at": _stamp(),
                "frame_sequence": result.get("frame_sequence"),
                "visual_hash": result.get("visual_hash"),
            }
            if control.get("pending_room_id") == room_id:
                control["pending_room_id"] = None
            self._persist()

    def room_entry_context_for_state(self, state: dict | None = None) -> dict | None:
        context = self.state.get("room_entry_context")
        if not isinstance(context, dict):
            return None
        current = self.debug.room_identity(state) if isinstance(state, dict) else None
        current_room_id = current.get("id") if isinstance(current, dict) else None
        status = context.get("status")
        expected_room_id = (
            context.get("source_room_id")
            if status == "recovery_pending"
            else context.get("destination_room_id")
        )
        if current_room_id and expected_room_id != current_room_id:
            return None
        return copy.deepcopy(context)

    def recent_transition_chain(self, state: dict | None = None) -> list[dict]:
        context = self.room_entry_context_for_state(state)
        if not isinstance(context, dict):
            return []
        chain = [{
            "from_room_id": context.get("source_room_id"),
            "target_id": context.get("source_target_id"),
            "transition_key": context.get("source_transition_key"),
            "to_room_id": context.get("destination_room_id"),
            "entered_at": context.get("entered_at"),
        }]
        if context.get("status") == "recovery_pending":
            chain.append({
                "from_room_id": context.get("destination_room_id"),
                "target_id": context.get("return_target_id"),
                "transition_key": context.get("return_transition_key"),
                "to_room_id": context.get("source_room_id"),
                "returned_at": context.get("returned_at"),
                "classification": "transition_loop",
            })
        return chain

    @staticmethod
    def _position_snapshot(state: dict) -> dict:
        position = state.get("party_position") if isinstance(state.get("party_position"), dict) else {}
        return {"x": position.get("x"), "y": position.get("y")}

    def _transition_key_for_target(self, state: dict, target_id: int) -> str | None:
        observation = self.debug.transition_observation_from_state(state, target_id)
        if not isinstance(observation, dict):
            return None
        value = observation.get("transition_key")
        return str(value) if value else None

    def _set_room_entry_context(self, context: dict | None) -> None:
        with self.lock:
            self.state["room_entry_context"] = copy.deepcopy(context)
            self._persist()

    def _update_room_entry_after_execution(
        self,
        before: dict,
        after: dict,
        tool: str,
        args: dict,
        action_result: dict,
        *,
        entry_return_selected: bool = False,
    ) -> None:
        before_identity = self.debug.room_identity(before)
        after_identity = self.debug.room_identity(after)
        before_room_id = before_identity.get("id") if isinstance(before_identity, dict) else None
        after_room_id = after_identity.get("id") if isinstance(after_identity, dict) else None
        if not before_room_id or not after_room_id:
            return
        try:
            target_id = int(args.get("id") or 0) if tool == "navigation_step" else 0
        except (TypeError, ValueError):
            target_id = 0
        current = self.state.get("room_entry_context")
        current = copy.deepcopy(current) if isinstance(current, dict) else None

        if before_room_id != after_room_id and target_id > 0:
            transition_key = self._transition_key_for_target(before, target_id)
            if (
                current
                and current.get("status") == "entered"
                and current.get("source_room_id") == before_room_id
                and current.get("destination_room_id") == after_room_id
                and int(current.get("source_target_id") or 0) == target_id
                and current.get("source_transition_key") == transition_key
            ):
                return
            if (
                current
                and current.get("status") == "entered"
                and current.get("destination_room_id") == before_room_id
                and current.get("source_room_id") == after_room_id
                and not entry_return_selected
                and not self.debug.active_task_binds_navigation_target(before, target_id)
            ):
                current.update({
                    "status": "recovery_pending",
                    "return_target_id": target_id,
                    "return_transition_key": transition_key,
                    "returned_at": _stamp(),
                    "returned_frame_sequence": after.get("frame"),
                    "return_landing_position": self._position_snapshot(after),
                })
                self._set_room_entry_context(current)

                self.emit(
                    "navigation",
                    "transition_loop",
                    f"Detected immediate room return {before_room_id} -> {after_room_id}; recovery is required.",
                    {"room_entry_context": current},
                )
                return

            recovered = bool(
                current
                and current.get("status") == "recovery_pending"
                and current.get("source_room_id") == before_room_id
                and current.get("destination_room_id") == after_room_id
                and int(current.get("source_target_id") or 0) == target_id
            )
            next_context = {
                "status": "entered",
                "source_room_id": before_room_id,
                "source_region_key": before_identity.get("region_key"),
                "source_target_id": target_id,
                "source_transition_key": transition_key,
                "destination_room_id": after_room_id,
                "destination_region_key": after_identity.get("region_key"),
                "landing_position": self._position_snapshot(after),
                "entered_at": _stamp(),
                "entered_frame_sequence": after.get("frame"),
                "recovered_from_loop": recovered,
            }
            self._set_room_entry_context(next_context)
            self.emit(
                "navigation",
                "room_entry_context",
                f"Recorded room entry {before_room_id} -> {after_room_id}.",
                {"room_entry_context": next_context},
            )
            return

        if (current and current.get("status") == "entered"
                and current.get("destination_room_id") == after_room_id
                and before_room_id == after_room_id and tool == "move_to"
                and self._position_snapshot(before) != self._position_snapshot(after)):
            current.update(status="progressed", progressed_at=_stamp(), progress_action="move_to")
            self._set_room_entry_context(current)

        if (
            current
            and current.get("status") == "entered"
            and current.get("destination_room_id") == after_room_id
            and tool == "navigation_step"
            and target_id > 0
            and not self.debug.navigation_target_is_entry_return(before, target_id)
        ):
            progress = action_result.get("progress") if isinstance(action_result.get("progress"), list) else []
            meaningful = action_result.get("status") in {"arrived", "approached", "interacting"} or any(
                isinstance(item, dict)
                and (
                    item.get("status") in {"stepping", "arrived", "approached", "interacting"}
                    or bool(item.get("move"))
                    or int(item.get("segment_steps") or 0) > 0
                )
                for item in progress
            )
            if meaningful:
                current.update({
                    "status": "progressed",
                    "progressed_at": _stamp(),
                    "progress_target_id": target_id,
                })
                self._set_room_entry_context(current)

    def _record_confirmed_room_entry(
        self,
        before: dict | None,
        after: dict,
        target_id: int,
        transition: dict,
        *,
        intentional_return: bool = False,
    ) -> None:
        if not isinstance(before, dict) or not isinstance(after, dict):
            return
        self._update_room_entry_after_execution(
            before,
            after,
            "navigation_step",
            {"id": int(target_id)},
            {"status": "transitioned", "transition": transition},
            entry_return_selected=intentional_return,
        )

    async def _automatic_room_inspection(self, state: dict) -> str | None:
        room_id = self._observe_room_for_inspection(state)
        control = self.state.get("room_inspection") or {}
        if (
            not room_id
            or control.get("pending_room_id") != room_id
            or self._room_screen_obscured(state)
        ):
            return None
        started = _now()
        arguments = {
            "focus": "room_layout",
            "question": "Describe the current room layout, visible exits, and obvious risks. Do not infer object IDs, scripts, or confirmed transitions.",
        }
        result = await asyncio.to_thread(
            self.debug.run_agent_tool,
            "inspect_screen",
            arguments,
            state,
            tool_cycle=self.debug.new_agent_tool_cycle(state),
        )
        if result.get("ok") is not True:
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            code = str(error.get("code") or "screen_inspection_failed")
            self.emit(
                "tool",
                "automatic_visual_inspection_failed",
                f"Automatic room inspection failed for {room_id}: {code}.",
                {"name": "inspect_screen", "automatic": True, "room_id": room_id, "error_code": code},
                started=started,
            )
            self._pause(
                "automatic_room_inspection_failed",
                details={"room_id": room_id, "error_code": code},
                summary=f"Agent paused: automatic room inspection failed for {room_id} ({code}).",
            )
            return ""
        self._record_room_inspection(room_id, result)
        self.emit(
            "tool",
            "automatic_visual_inspection",
            f"Automatically inspected new room {room_id}.",
            {"name": "inspect_screen", "automatic": True, "room_id": room_id, "result": result},
            started=started,
        )
        return (
            "Automatic low-authority visual inspection of the newly entered room. "
            "Treat it only as visual_annotation and do not write Room Memo, task, or transition facts from it:\n"
            + json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        )

    def _formal_battle_generation(self, state: dict) -> int:
        battle = state.get("battle") if isinstance(state.get("battle"), dict) else None
        if state.get("in_battle") is not True or battle is None:
            return 0
        if self.debug.prebattle_interaction(state) is not None:
            return 0
        return int(battle.get("generation") or 0)

    async def _migrate_main_session_after_takeover(self) -> None:
        try:
            from agents import SQLiteSession
            session = SQLiteSession(self.state["session_id"], self.sqlite_path)
            await self._migrate_sdk_session_history(session)
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            return

    async def _decide_with_battle_watch(self, state: dict, feedback: str) -> dict:
        baseline = self._formal_battle_generation(state)
        task = asyncio.create_task(self._decide(state, feedback))
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=0.05)
            if done:
                break
            latest = await asyncio.to_thread(self.debug.snapshot)
            current = self._formal_battle_generation(latest)
            if current > 0 and current != baseline:
                stream = self.active_stream
                if stream is not None and callable(getattr(stream, "cancel", None)):
                    stream.cancel("immediate")
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, RuntimeError):
                    pass
                await self._migrate_main_session_after_takeover()
                raise BattleStarted(latest)
        return await task

    async def _run(self, generation: int, *, decide_only: bool) -> None:
        feedback = "Inspect the latest stable game state and choose the next useful local action."
        replans = 0
        try:
            while int(self.state.get("run_turn") or 0) < int(self.state.get("max_turns") or 1):
                if self.cancel_event.is_set() or generation != self.generation:
                    self._pause("interrupted")
                    return
                before = await asyncio.to_thread(self.debug.snapshot)
                if self._game_control_active(before):
                    before = await asyncio.to_thread(self._wait_for_control)
                inspection_feedback = await self._automatic_room_inspection(before)
                if inspection_feedback == "":
                    return
                if inspection_feedback:
                    feedback = feedback + "\n\n" + inspection_feedback
                if before.get("in_battle") and self.debug.prebattle_interaction(before) is None:
                    if not isinstance(before.get("battle"), dict):
                        self._pause("battle_state_unavailable")
                        return
                    try:
                        summary = await self.battle_delegate.run(before)
                    except Exception as exc:
                        reason = str(exc) or "battle_delegate_failed"
                        stopped = self.cancel_event.is_set() or reason == "interrupted"
                        terminal_status = "stopped" if stopped else "failed"
                        control = self.state.get("battle_delegate")
                        already_stopped = (
                            isinstance(control, dict)
                            and control.get("status") == "stopped"
                        )
                        if not already_stopped:
                            self.battle_delegate._persist_control(
                                status=terminal_status,
                                phase=terminal_status,
                                stop_reason=reason,
                                ended_at=_stamp(),
                            )
                            self.battle_delegate._emit(
                                terminal_status,
                                f"Battle Agent {terminal_status}: {reason}.",
                                {"reason": reason},
                            )
                        self._pause(
                            reason,
                            details={"battle_delegate": self.state.get("battle_delegate")},
                            summary=f"Agent paused: battle delegation failed ({reason}).",
                        )
                        return
                    if summary.get("result") == "lost":
                        self._pause(
                            "battle_lost",
                            details={"summary": summary},
                            summary="Agent paused after battle defeat.",
                        )
                        return
                    feedback = (
                        "Battle completed; resume the story from this deterministic summary. "
                        "enemy_details/enemies are the authoritative enemy identities. "
                        "trigger_context only identifies a map object or script active when the battle began; "
                        "never describe that trigger object as an enemy unless enemy_details independently says so:\n"
                        + json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
                    )
                    continue
                if self._unsafe_state_reason(before):
                    self._pause(self._unsafe_state_reason(before) or "unsafe_state")
                    return
                started = _now()
                self.state["phase"] = "deciding"
                self._persist()
                request_details = {"model": self.state.get("model")}
                if self.decision_provider is None and self.debug.config_value("DEEPSEEK_API_KEY"):
                    request_details.update(
                        api="responses",
                        reasoning_effort=AGENT_REASONING_EFFORT,
                    )
                self.emit(
                    "model",
                    "request",
                    "Requesting the next recommendation.",
                    request_details,
                )
                try:
                    decision = await self._decide_with_battle_watch(before, feedback)
                except BattleStarted:
                    feedback = "A formal battle started; hand control to the Battle Agent."
                    continue
                if self.cancel_event.is_set() or generation != self.generation:
                    self.emit("interrupt", "cancelled", "A late model result was discarded.")
                    self._pause("interrupted")
                    return
                latest = await asyncio.to_thread(self.debug.snapshot)
                if self._game_control_active(latest):
                    latest = await asyncio.to_thread(self._wait_for_control)
                if self._formal_battle_generation(latest) > 0:
                    feedback = "A formal battle started; discard the ordinary recommendation."
                    continue
                validation_rejection = self._decision_validation_rejection(decision)
                if validation_rejection:
                    replans += 1
                    self._merge_usage(decision.get("usage") or {})
                    self.emit(
                        "decision",
                        "discarded",
                        "Recommendation rejected by latest-state validation.",
                        {
                            "validation": validation_rejection,
                            "recommended_action": decision.get("recommended_action"),
                        },
                        started=started,
                    )
                    if replans >= 3:
                        self._pause(
                            "replan_exhausted",
                            details={"validation": validation_rejection, "replans": replans},
                            summary=(
                                "Agent paused: no executable recommendation after "
                                f"{replans} replans."
                            ),
                        )
                        return
                    feedback = self._validation_replan_feedback(validation_rejection, latest)
                    continue
                replan_reason = self._decision_replan_reason(decision, latest)
                if replan_reason:
                    replans += 1
                    self._merge_usage(decision.get("usage") or {})
                    self.emit(
                        "decision", "discarded", "Recommendation became stale before execution.",
                        {"reason": replan_reason, "recommended_action": decision.get("recommended_action")},
                        started=started,
                    )
                    if replans >= 3:
                        self._pause("state_changing")
                        return
                    feedback = self._replan_feedback(replan_reason, latest)
                    continue
                await asyncio.to_thread(self.debug.append_agent_decision, latest, decision)
                with self.lock:
                    self.state["turn"] = int(self.state.get("turn") or 0) + 1
                    self.state["run_turn"] = int(self.state.get("run_turn") or 0) + 1
                    self.state["pending_decision"] = decision
                    self._merge_usage(decision.get("usage") or {})
                    self._persist()
                self.emit("decision", "ready", decision.get("summary") or "Recommendation ready.", self._decision_event_details(decision), started=started)
                if decide_only:
                    self._pause("awaiting_manual_execute")
                    return
                action = decision.get("recommended_action") or {}
                if action.get("tool") in {None, "no_op"} or decision.get("recommended_intent") == "wait_runtime":
                    wait_reason = (action.get("args") or {}).get("reason") or "no_executable_action"
                    self._pause(
                        wait_reason,
                        details={"reason": wait_reason, "recommended_action": action},
                        summary=f"Agent paused: {wait_reason}.",
                    )
                    return
                self.state["phase"] = "executing"
                self._persist()
                try:
                    result = await asyncio.to_thread(self._execute_decision, decision)
                except BattleStarted:
                    with self.lock:
                        self.state["turn"] = max(0, int(self.state.get("turn") or 0) - 1)
                        self.state["run_turn"] = max(0, int(self.state.get("run_turn") or 0) - 1)
                        self.state["pending_decision"] = None
                        self._persist()
                    feedback = "A formal battle started; hand control to the Battle Agent."
                    continue
                except ReplanRequired as exc:
                    replans += 1
                    with self.lock:
                        self.state["turn"] = max(0, int(self.state.get("turn") or 0) - 1)
                        self.state["run_turn"] = max(0, int(self.state.get("run_turn") or 0) - 1)
                        self.state["pending_decision"] = None
                        self._persist()
                    self.emit("decision", "discarded", "Recommendation became stale before execution.", {"reason": str(exc)})
                    if replans >= 3:
                        self._pause("state_changing")
                        return
                    feedback = self._replan_feedback(str(exc), await asyncio.to_thread(self.debug.snapshot))
                    continue
                except Exception as exc:
                    self.emit("error", "error", f"Execution failed: {exc}")
                    self._pause("execution_failed")
                    return
                with self.lock:
                    self.state.update(last_execution=result, pending_decision=None)
                    self._persist()
                replans = 0
                feedback = "Execution result and stable state delta:\n" + json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            self._pause("turn_limit")
        except asyncio.CancelledError:
            self._pause("interrupted")
        except Exception as exc:
            self.emit("error", "error", f"Agent run failed: {exc}")
            self._pause("error")
        finally:
            self.active_stream = None

    def _unsafe_state_reason(self, state: dict) -> str | None:
        if state.get("status") in {"waiting_for_game", "state_parse_error"}:
            return str(state.get("status"))
        debug = state.get("_debug") or {}
        if debug.get("runtime_stale"):
            return "runtime_stale"
        if state.get("in_battle") and not isinstance(state.get("battle"), dict):
            return "battle_state_unavailable"
        control = state.get("control") if isinstance(state.get("control"), dict) else {}
        if control.get("phase") in {"action_pending", "game_script", "entering_scene"}:
            return "game_controlled"
        if state.get("in_main_game") is False:
            return "not_in_main_game"
        return None

    @staticmethod
    def _game_control_active(state: dict) -> bool:
        control = state.get("control") if isinstance(state.get("control"), dict) else {}
        return control.get("phase") in {"action_pending", "game_script", "entering_scene"}

    def _wait_for_control(self, timeout: float = 30.0) -> dict:
        latest = self.debug.snapshot()
        last_progress_at = time.monotonic()
        progress_signature = self.debug.game_control_progress_signature(latest)
        while self._game_control_active(latest):
            if self.cancel_event.is_set():
                raise RuntimeError("interrupted")
            time.sleep(0.05)
            latest = self.debug.snapshot()
            unsafe_reason = self._unsafe_state_reason(latest)
            if unsafe_reason not in {None, "game_controlled"}:
                raise RuntimeError(unsafe_reason)
            latest_signature = self.debug.game_control_progress_signature(latest)
            if latest_signature != progress_signature:
                progress_signature = latest_signature
                last_progress_at = time.monotonic()
            elif time.monotonic() - last_progress_at >= timeout:
                raise RuntimeError("game_control_timeout")
        return latest

    async def _decide(self, state: dict, feedback: str) -> dict:
        if self.decision_provider:
            value = self.decision_provider(state, self.state.get("model"), feedback)
            if asyncio.iscoroutine(value):
                value = await value
            return value
        if not self.debug.config_value("DEEPSEEK_API_KEY"):
            return await asyncio.to_thread(self.debug.decide_agent, state, self.state.get("model"))
        return await self._sdk_decide(state, feedback)

    async def _sdk_decide(self, state: dict, feedback: str) -> dict:
        """Use the Agents SDK's streamed Responses runner and local SQLiteSession."""
        try:
            from agents import (Agent, AsyncOpenAI, FunctionTool, ModelSettings,
                                OpenAIResponsesModel, Runner, SQLiteSession,
                                set_tracing_disabled)
            from agents.agent import ToolsToFinalOutputResult
        except ImportError as exc:
            raise RuntimeError("openai-agents is required for persistent DeepSeek sessions") from exc

        set_tracing_disabled(disabled=True)
        runtime = self
        submitted_decision: dict | None = None
        tool_cycle = runtime.debug.new_agent_tool_cycle(runtime.debug.snapshot())

        def make_tool(spec: dict) -> Any:
            function = spec["function"]
            name = function["name"]

            async def invoke(_context: Any, raw_arguments: str) -> str:
                nonlocal submitted_decision
                arguments = runtime._parse_tool_arguments(raw_arguments)
                started = _now()
                tool_state = runtime.debug.snapshot()
                if name == "inspect_screen":
                    result = await asyncio.to_thread(
                        runtime.debug.run_agent_tool,
                        name,
                        arguments,
                        tool_state,
                        tool_cycle=tool_cycle,
                    )
                else:
                    result = runtime.debug.run_agent_tool(
                        name,
                        arguments,
                        tool_state,
                        tool_cycle=tool_cycle,
                    )
                submit_ok = name == "submit_decision" and result.get("ok") is True
                submit_error = result.get("error") if name == "submit_decision" else None
                submit_error_code = (
                    submit_error.get("code")
                    if isinstance(submit_error, dict)
                    else submit_error
                )
                runtime.emit(
                    "decision" if name == "submit_decision" else "tool",
                    (
                        "submitted" if submit_ok else "rejected"
                    ) if name == "submit_decision" else "completed",
                    (
                        "Recommendation submitted."
                        if submit_ok
                        else f"Recommendation rejected: {submit_error_code}."
                        if submit_error_code
                        else "Recommendation rejected."
                    ) if name == "submit_decision" else f"Query tool {name} completed.",
                    {"name": name, "arguments": arguments, "result": result},
                    started=started,
                )
                if name == "inspect_screen" and result.get("ok") is True:
                    identity = runtime.debug.room_identity(tool_state)
                    room_id = identity.get("id") if isinstance(identity, dict) else None
                    if isinstance(room_id, str) and room_id:
                        runtime._record_room_inspection(room_id, result)
                if name == "submit_decision":
                    if not result.get("ok"):
                        return json.dumps(result, ensure_ascii=False)
                    submitted_decision = result["decision"]
                    return json.dumps({"ok": True, "accepted": True}, ensure_ascii=False)
                projected = runtime._model_tool_result(name, result)
                return runtime._final_model_tool_output(projected)

            return FunctionTool(
                name=name,
                description=function.get("description") or name,
                params_json_schema=function.get("parameters") or {"type": "object", "properties": {}},
                on_invoke_tool=invoke,
                strict_json_schema=False,
            )

        tools = [make_tool(spec) for spec in self.debug.agent_tool_specs()]

        def finish_on_accepted_submit(_context: Any, tool_results: list[Any]) -> Any:
            for tool_result in tool_results:
                if getattr(getattr(tool_result, "tool", None), "name", None) != "submit_decision":
                    continue
                output = getattr(tool_result, "output", None)
                try:
                    parsed = json.loads(output) if isinstance(output, str) else output
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, dict) and parsed.get("ok") is True and parsed.get("accepted") is True:
                    return ToolsToFinalOutputResult(is_final_output=True, final_output=output)
            return ToolsToFinalOutputResult(is_final_output=False, final_output=None)

        api_key = self.debug.config_value("DEEPSEEK_API_KEY")
        base_url = (self.debug.config_value("DEEPSEEK_BASE_URL", "https://api.deepseek.com") or "https://api.deepseek.com").rstrip("/")
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        model = OpenAIResponsesModel(
            model=self.state.get("model"),
            openai_client=client,
        )
        instructions = self.debug.agent_system_prompt() + "\n\n" + self.debug.agent_policy_prompt() + """

Use the available read-only query tools as needed. End every decision by calling
submit_decision. Never execute game controls yourself. Use current evidence,
exploration value, and maintained local tasks to choose the next action.
"""
        agent = Agent(
            name="PALPAL persistent player",
            instructions=instructions,
            model=model,
            tools=tools,
            tool_use_behavior=finish_on_accepted_submit,
            model_settings=ModelSettings(
                include_usage=True,
                reasoning={"effort": AGENT_REASONING_EFFORT},
                tool_choice="auto",
            ),
            reset_tool_choice=False,
        )
        session = SQLiteSession(self.state["session_id"], self.sqlite_path)
        await self._migrate_sdk_session_history(session)
        def current_prompt() -> str:
            return json.dumps({
                "feedback": feedback,
                "context": self.debug.agent_tool_initial_context(state),
            }, ensure_ascii=False, separators=(",", ":"))

        prompt = current_prompt()
        if await self._maybe_compact_sdk(session, client, prompt):
            prompt = current_prompt()
        streams = []
        for attempt in range(2):
            stream = Runner.run_streamed(
                agent, prompt, session=session, max_turns=16,
            )
            streams.append(stream)
            self.active_stream = stream
            try:
                async for event in stream.stream_events():
                    if self.cancel_event.is_set():
                        stream.cancel("immediate")
                    event_type = getattr(event, "type", "")
                    name = getattr(event, "name", "")
                    if event_type == "run_item_stream_event" and name in {"tool_called", "tool_output"}:
                        self.emit("model", name, name.replace("_", " ").title())
            finally:
                usage = getattr(getattr(stream, "context_wrapper", None), "usage", None)
                if usage is not None:
                    self._record_provider_usage(
                        usage,
                        "main_decision",
                        model=self.state.get("model"),
                        update_last_prompt=True,
                    )
            if submitted_decision is not None:
                break
            if attempt == 0:
                self.emit(
                    "model",
                    "protocol_retry",
                    "Model ended without an accepted submit_decision; correcting once.",
                    {"api": "responses", "reasoning_effort": AGENT_REASONING_EFFORT},
                )
                prompt = (
                    "Your previous response ended without an accepted submit_decision tool call. "
                    "Continue from the persisted tool results and call submit_decision after any "
                    "required maintenance or queries. Do not end with plain text."
                )
        if submitted_decision is None:
            raise RuntimeError(
                "submit_decision_protocol_error: no accepted submit_decision "
                "after correction retry"
            )
        output = submitted_decision
        output.update(backend="deepseek", model=self.state.get("model"))
        usages = [
            usage
            for stream in streams
            if (usage := getattr(getattr(stream, "context_wrapper", None), "usage", None))
        ]
        if usages:
            request_usage = {
                "prompt_tokens": sum(int(getattr(usage, "input_tokens", 0) or 0) for usage in usages),
                "completion_tokens": sum(int(getattr(usage, "output_tokens", 0) or 0) for usage in usages),
                "total_tokens": sum(int(getattr(usage, "total_tokens", 0) or 0) for usage in usages),
            }
            output["usage"] = request_usage
            self.state["last_request_usage"] = request_usage
            self._persist()
        output = self.debug.attach_decision_context(state, output)
        return output

    async def _maybe_compact_sdk(self, session: Any, client: Any, next_prompt: str) -> bool:
        threshold = self._compact_at_tokens()
        retain_budget = self._compact_retain_tokens()
        observed_tokens = self.state.get("last_prompt_tokens")
        if not isinstance(observed_tokens, int) or observed_tokens < max(1000, threshold):
            return False
        items = await session.get_items()
        if len(items) < 6:
            return False
        if self.cancel_event.is_set():
            raise RuntimeError("interrupted")
        recent_start, retained_cycles = self._compaction_retained_start(items, retain_budget)
        if recent_start <= 0:
            return False
        starting_details = {
            "observed_prompt_tokens": observed_tokens,
            "compact_at_tokens": threshold,
            "retain_tokens": retain_budget,
            "items": len(items),
        }
        retained_items = items[recent_start:]
        before = self._context_estimate_breakdown(items, next_prompt)
        retained_estimated_tokens = self._estimate_items_tokens(retained_items)
        retained_budget_exceeded = retained_estimated_tokens > retain_budget
        transcript = json.dumps(items[:recent_start], ensure_ascii=False)
        telemetry_before = {
            key: copy.deepcopy(self.state.get(key))
            for key in (
                "last_prompt_tokens", "compactions", "last_compaction",
                "task_reconciliation_pending", "task_reconciliation_reason",
                "provider_usage",
            )
        }
        request_task: asyncio.Task | None = None
        transcript_replaced = False
        self.emit(
            "compression",
            "starting",
            "Context compression started.",
            starting_details,
        )
        try:
            request_task = asyncio.create_task(client.responses.create(
                model=self.state.get("model"),
                instructions=(
                    "Summarize this PALPAL agent transcript compactly. The structured task ledger is the "
                    "only authority for task status, so do not reconstruct task truth from this summary. Preserve "
                    "plot evidence, decisions and execution results, known routes, unresolved "
                    "risks, and latest stable state. Mark object semantics as inference or annotation."
                ),
                input=transcript,
                reasoning={"effort": SUMMARY_REASONING_EFFORT},
            ))
            with self.lock:
                self.active_compression_task = request_task
                interrupted = self.cancel_event.is_set()
            if interrupted:
                request_task.cancel()
            response = await request_task
            response_usage = getattr(response, "usage", None)
            if response_usage is not None:
                self._record_provider_usage(
                    response_usage,
                    "compaction",
                    model=self.state.get("model"),
                )
            status = getattr(response, "status", None)
            if status != "completed":
                raise RuntimeError(f"context summarizer response status was {status or 'missing'}")
            summary = getattr(response, "output_text", None)
            if not summary:
                raise RuntimeError("context summarizer returned no content")
            if self.cancel_event.is_set():
                raise asyncio.CancelledError
            replacement = [
                {"role": "user", "content": "Persistent context summary:\n" + summary},
                {"role": "assistant", "content": "I will preserve this context while choosing useful local actions."},
                *retained_items,
            ]
            summary_estimated_tokens = self._estimate_text_tokens(summary)
            replacement_breakdown = self._context_estimate_breakdown(replacement, next_prompt)
            await session.clear_session()
            transcript_replaced = True
            if self.cancel_event.is_set():
                raise asyncio.CancelledError
            await session.add_items(replacement)
            if self.cancel_event.is_set():
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            if transcript_replaced:
                await session.clear_session()
                await session.add_items(items)
            with self.lock:
                for key, value in telemetry_before.items():
                    self.state[key] = value
                self._persist()
            self.emit("compression", "cancelled", "Context compression cancelled.", starting_details)
            raise
        except Exception as exc:
            # Restore the complete transcript if the atomic-style replacement fails.
            if transcript_replaced:
                await session.clear_session()
                await session.add_items(items)
            self.emit(
                "compression", "failed", f"Context compression failed: {exc}",
                {**starting_details, "error": str(exc)},
            )
            raise
        finally:
            with self.lock:
                if self.active_compression_task is request_task:
                    self.active_compression_task = None
        self.state["compactions"] = int(self.state.get("compactions") or 0) + 1
        # The replacement has not been sent to DeepSeek yet, so its exact token
        # count is intentionally unknown until the next real model request.
        self.state["last_prompt_tokens"] = None
        self.state["task_reconciliation_pending"] = True
        self.state["task_reconciliation_reason"] = "context_compacted"
        last_compaction = {
            "time": _stamp(),
            "trigger_prompt_tokens": observed_tokens,
            "compact_at_tokens": threshold,
            "retain_tokens": retain_budget,
            "before_estimated_tokens": before["estimated_tokens"],
            "before_composition": before["composition"],
            "retained_cycles": retained_cycles,
            "retained_items": len(retained_items),
            "retained_estimated_tokens": retained_estimated_tokens,
            "retained_budget_exceeded": retained_budget_exceeded,
            "summary_estimated_tokens": summary_estimated_tokens,
            "replacement_estimated_tokens": replacement_breakdown["estimated_tokens"],
            "replacement_composition": replacement_breakdown["composition"],
        }
        self.state["last_compaction"] = last_compaction
        self._persist()
        self.emit(
            "compression",
            "completed",
            "Context compression completed.",
            last_compaction,
        )
        return True

    def _compact_at_tokens(self) -> int:
        raw = self.debug.config_value(
            "PALPAL_AGENT_COMPACT_AT_TOKENS",
            str(DEFAULT_COMPACT_AT_TOKENS),
        )
        try:
            threshold = int(raw or DEFAULT_COMPACT_AT_TOKENS)
        except (TypeError, ValueError):
            return DEFAULT_COMPACT_AT_TOKENS
        return threshold if threshold > 0 else DEFAULT_COMPACT_AT_TOKENS

    def _compact_retain_tokens(self) -> int:
        raw = self.debug.config_value(
            "PALPAL_AGENT_COMPACT_RETAIN_TOKENS",
            str(DEFAULT_COMPACT_RETAIN_TOKENS),
        )
        try:
            budget = int(raw or DEFAULT_COMPACT_RETAIN_TOKENS)
        except (TypeError, ValueError):
            return DEFAULT_COMPACT_RETAIN_TOKENS
        return budget if budget > 0 else DEFAULT_COMPACT_RETAIN_TOKENS

    def _merge_usage(self, usage: dict) -> None:
        normalized: dict[str, int] = {}
        target = self.state.setdefault("usage", {})
        aliases = {"input_tokens": "prompt_tokens", "output_tokens": "completion_tokens"}
        for key, value in usage.items():
            key = aliases.get(key, key)
            if key.endswith("tokens") and isinstance(value, (int, float)):
                target[key] = int(target.get(key) or 0) + int(value)
                normalized[key] = int(value)
        if normalized:
            self.state["last_request_usage"] = normalized

    @staticmethod
    def _normalize_provider_usage(provider_usage: dict) -> None:
        provider_usage["version"] = 1
        provider_usage.setdefault("tracking_started_at", None)
        provider_usage["incomplete_before_tracking"] = provider_usage.get("incomplete_before_tracking") is True
        scopes = provider_usage.setdefault("scopes", {})
        for scope in PROVIDER_USAGE_SCOPES:
            value = scopes.get(scope)
            if not isinstance(value, dict):
                value = {}
                scopes[scope] = value
            for key in (
                "requests", "input_tokens", "cached_tokens", "output_tokens",
                "reasoning_tokens", "total_tokens",
            ):
                value[key] = max(0, int(value.get(key) or 0))
            value["details_complete"] = value.get("details_complete", True) is True
        provider_usage.setdefault("last_request", None)

    @staticmethod
    def _usage_detail_value(details: Any, key: str) -> tuple[int, bool]:
        if details is None:
            return 0, False
        value = getattr(details, key, None)
        if not isinstance(value, (int, float)):
            return 0, False
        return max(0, int(value)), True

    @classmethod
    def _provider_request_entries(cls, usage: Any) -> list[dict]:
        entries = getattr(usage, "request_usage_entries", None)
        if not isinstance(entries, list) or not entries:
            request_count = getattr(usage, "requests", None)
            has_tokens = any(
                int(getattr(usage, key, 0) or 0) > 0
                for key in (
                    "input_tokens", "prompt_tokens", "output_tokens",
                    "completion_tokens", "total_tokens",
                )
            )
            if request_count is not None and int(request_count or 0) != 1:
                return []
            if request_count is None and not has_tokens:
                return []
            entries = [usage]
        normalized = []
        for entry in entries:
            input_tokens = max(0, int(
                getattr(entry, "input_tokens", None)
                or getattr(entry, "prompt_tokens", 0)
                or 0
            ))
            output_tokens = max(0, int(
                getattr(entry, "output_tokens", None)
                or getattr(entry, "completion_tokens", 0)
                or 0
            ))
            total_tokens = max(0, int(getattr(entry, "total_tokens", 0) or 0))
            cached_tokens, cached_complete = cls._usage_detail_value(
                getattr(entry, "input_tokens_details", None)
                or getattr(entry, "prompt_tokens_details", None), "cached_tokens"
            )
            reasoning_tokens, reasoning_complete = cls._usage_detail_value(
                getattr(entry, "output_tokens_details", None)
                or getattr(entry, "completion_tokens_details", None), "reasoning_tokens"
            )
            normalized.append({
                "input_tokens": input_tokens,
                "cached_tokens": cached_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "total_tokens": total_tokens or input_tokens + output_tokens,
                "details_complete": cached_complete and reasoning_complete,
            })
        return normalized

    def _record_provider_usage(
        self,
        usage: Any,
        scope: str,
        *,
        model: str | None = None,
        provider: str = "deepseek",
        update_last_prompt: bool = False,
    ) -> list[dict]:
        entries = self._provider_request_entries(usage)
        if not entries:
            return []
        if scope not in PROVIDER_USAGE_SCOPES:
            raise ValueError(f"unknown provider usage scope: {scope}")
        with self.lock:
            provider_usage = self.state.setdefault("provider_usage", {})
            self._normalize_provider_usage(provider_usage)
            if provider_usage.get("tracking_started_at") is None:
                provider_usage["tracking_started_at"] = _stamp()
            target = provider_usage["scopes"][scope]
            for entry in entries:
                target["requests"] += 1
                for key in (
                    "input_tokens", "cached_tokens", "output_tokens",
                    "reasoning_tokens", "total_tokens",
                ):
                    target[key] += entry[key]
                target["details_complete"] = target["details_complete"] and entry["details_complete"]
                recorded = {
                    "scope": scope,
                    "provider": provider,
                    "model": model or self.state.get("model"),
                    "time": _stamp(),
                    **entry,
                }
                provider_usage["last_request"] = recorded
                self.emit(
                    "usage",
                    "recorded",
                    f"Provider usage recorded for {scope}.",
                    recorded,
                )
            if update_last_prompt and entries[-1]["input_tokens"] > 0:
                self.state["last_prompt_tokens"] = entries[-1]["input_tokens"]
            self._persist()
        return entries

    @staticmethod
    def _last_prompt_tokens_from_usage(usage: Any) -> int | None:
        """Return the provider count for the final model request in one Agent run."""
        entries = getattr(usage, "request_usage_entries", None)
        if isinstance(entries, list) and entries:
            value = getattr(entries[-1], "input_tokens", None)
        elif int(getattr(usage, "requests", 0) or 0) == 1:
            value = getattr(usage, "input_tokens", None)
        else:
            return None
        if not isinstance(value, (int, float)) or int(value) <= 0:
            return None
        return int(value)

    @staticmethod
    def _estimate_context_tokens(items: list[dict], next_prompt: str) -> int:
        serialized = json.dumps(items, ensure_ascii=False, separators=(",", ":")) + next_prompt
        return AgentRuntime._estimate_text_tokens(serialized)

    @staticmethod
    def _estimate_text_tokens(value: str) -> int:
        # UTF-8 bytes avoid treating CJK text as if every character were a fraction
        # of one token. Three bytes per token is intentionally dependency-free and
        # conservative for the mixed Chinese/JSON transcripts PALPAL produces.
        return max(1, (len(value.encode("utf-8")) + 2) // 3)

    @staticmethod
    def _item_estimate_category(item: dict) -> str:
        item_type = item.get("type")
        if item_type == "reasoning":
            return "reasoning"
        if item_type == "function_call":
            return "function_call"
        if item_type == "function_call_output":
            return "function_output"
        return "user" if item.get("role") == "user" else "assistant"

    @classmethod
    def _item_estimate_composition(cls, items: list[dict]) -> dict[str, int]:
        composition = {
            "user": 0,
            "assistant": 0,
            "reasoning": 0,
            "function_call": 0,
            "function_output": 0,
        }
        for item in items:
            if not isinstance(item, dict):
                continue
            serialized = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            composition[cls._item_estimate_category(item)] += cls._estimate_text_tokens(serialized)
        return composition

    @classmethod
    def _estimate_items_tokens(cls, items: list[dict]) -> int:
        return sum(cls._item_estimate_composition(items).values())

    def _context_estimate_breakdown(self, items: list[dict], next_prompt: str) -> dict:
        composition = self._item_estimate_composition(items)
        fixed_prompt = (
            self.debug.agent_system_prompt()
            + self.debug.agent_policy_prompt()
            + json.dumps(self.debug.agent_tool_specs(), ensure_ascii=False, separators=(",", ":"))
        )
        composition["fixed_prompt"] = self._estimate_text_tokens(fixed_prompt)
        composition["next_prompt"] = self._estimate_text_tokens(next_prompt)
        return {
            "estimated_tokens": sum(composition.values()),
            "composition": composition,
        }

    @classmethod
    def _compaction_retained_start(cls, items: list[dict], retain_budget: int) -> tuple[int, int]:
        user_indices = [
            index for index, item in enumerate(items)
            if isinstance(item, dict)
            and (item.get("role") == "user" or (item.get("type") == "message" and item.get("role") == "user"))
        ]
        if not user_indices:
            return max(0, len(items) - 4), 1
        latest_start = user_indices[-1]
        if len(user_indices) < 2:
            return latest_start, 1
        second_start = user_indices[-2]
        if cls._estimate_items_tokens(items[second_start:]) <= retain_budget:
            return second_start, 2
        return latest_start, 1

    @staticmethod
    def _parse_tool_arguments(raw_arguments: str | None) -> dict:
        raw = (raw_arguments or "{}").strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            # One conservative repair pass for common streamed/truncated JSON endings.
            repaired = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
            repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
            repaired = re.sub(r":\s*([,}])", r":null\1", repaired)
            in_string = False
            escaped = False
            braces: list[str] = []
            for char in repaired:
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                elif char == '"':
                    in_string = True
                elif char in "{[":
                    braces.append(char)
                elif char in "}]" and braces:
                    braces.pop()
            if in_string:
                repaired += '"'
            repaired += "".join("}" if char == "{" else "]" for char in reversed(braces))
            value = json.loads(repaired)
        if not isinstance(value, dict):
            raise ValueError("tool arguments must be a JSON object")
        return value

    def _tool_output_needs_archive(self, output: str) -> bool:
        return (
            self._estimate_text_tokens(output) > TOOL_OUTPUT_FULL_TOKENS
            and not self._is_archived_tool_preview(output)
        )

    @staticmethod
    def _is_archived_tool_preview(output: str) -> bool:
        try:
            value = json.loads(output)
        except (TypeError, json.JSONDecodeError):
            return False
        return (
            isinstance(value, dict)
            and value.get("truncated") is True
            and value.get("archived") is True
            and AgentRuntime._valid_output_ref(value.get("output_ref"))
        )

    def _archived_tool_output_preview(
        self,
        output: str,
        *,
        reason: str,
        head_chars: int = 2_000,
        tail_chars: int = 2_000,
        output_ref: str | None = None,
    ) -> str:
        if not self._valid_output_ref(output_ref):
            output_ref = self._archive_tool_output(output)
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            parsed = None
        is_json = parsed is not None
        preview: dict[str, Any] = {
            "ok": parsed.get("ok", True) if isinstance(parsed, dict) else True,
            "truncated": True,
            "archived": True,
            "reason": reason,
            "format": "json" if is_json else "text",
            "output_ref": output_ref,
            "original_chars": len(output),
            "original_bytes": len(output.encode("utf-8")),
            "estimated_tokens": self._estimate_text_tokens(output),
            "head": output[:head_chars],
            "tail": output[-tail_chars:] if len(output) > head_chars else "",
        }
        if isinstance(parsed, dict):
            preview["keys"] = sorted(str(key) for key in parsed.keys())[:40]
            key_fields: dict[str, Any] = {}
            for key in (
                "ok", "error", "mode", "kind", "id", "object_id", "room_id",
                "status", "total", "returned", "events_total", "history_total",
            ):
                value = parsed.get(key)
                if value is None or isinstance(value, (bool, int, float)):
                    if key in parsed:
                        key_fields[key] = value
                elif isinstance(value, str) and len(value) <= 300:
                    key_fields[key] = value
            for key, value in parsed.items():
                if isinstance(value, list):
                    key_fields.setdefault(f"{key}_count", len(value))
            if key_fields:
                preview["key_fields"] = key_fields
        return json.dumps(preview, ensure_ascii=False, separators=(",", ":"))

    def _decision_event_details(self, decision: dict) -> dict:
        return {key: decision.get(key) for key in (
            "recommended_intent", "recommended_action", "evidence", "risk", "alternatives", "tool_trace", "usage"
        ) if key in decision}

    def _context_matches(self, decision: dict, state: dict) -> bool:
        expected = decision.get("state_context") or {}
        if not expected:
            return True
        current = self.debug.state_context(state)
        for key in ("scene_id", "map_id", "room_id", "room_key", "region_key"):
            if expected.get(key) is not None and current.get(key) != expected.get(key):
                return False
        return True

    def _decision_replan_reason(self, decision: dict, state: dict) -> str | None:
        if not self._context_matches(decision, state):
            return "state_context_stale"
        action = decision.get("recommended_action") or {}
        tool = action.get("tool")
        dialog_active = bool((state.get("dialog") or {}).get("active"))
        choice_active = bool((state.get("choice") or {}).get("active"))
        shop = state.get("shop") if isinstance(state.get("shop"), dict) else {}
        shop_active = shop.get("active") is True
        if choice_active and tool != "choice_select":
            return "in_choice"
        if not choice_active and tool == "choice_select":
            return "choice_inactive"
        if dialog_active and tool != "advance_dialog":
            return "in_dialog"
        if not dialog_active and tool == "advance_dialog":
            return "dialog_inactive"
        if shop_active and tool not in {"shop_buy", "shop_sell", "shop_leave"}:
            return "in_shop"
        if not shop_active and tool in {"shop_buy", "shop_sell", "shop_leave"}:
            return "shop_inactive"
        return None

    @staticmethod
    def _decision_validation_rejection(decision: dict) -> dict | None:
        validation = decision.get("validation")
        if not isinstance(validation, dict) or validation.get("status") != "rejected":
            return None
        return dict(validation)

    def _replan_feedback(self, reason: str, state: dict) -> str:
        return "Recommendation invalidated by live state; replan without executing:\n" + json.dumps({
            "reason": reason,
            "state": self._compact_state(state),
        }, ensure_ascii=False, separators=(",", ":"))

    def _validation_replan_feedback(self, validation: dict, state: dict) -> str:
        return "Recommendation rejected by server validation; inspect the latest navigation or inventory and choose a currently executable action:\n" + json.dumps({
            "validation": validation,
            "state": self._compact_state(state),
        }, ensure_ascii=False, separators=(",", ":"))

    def _execute_decision(self, decision: dict) -> dict:
        before = self.debug.snapshot()
        if self.cancel_event.is_set():
            raise RuntimeError("interrupted")
        if self._game_control_active(before):
            before = self._wait_for_control()
        if self._formal_battle_generation(before) > 0:
            raise BattleStarted(before)
        reason = self._unsafe_state_reason(before)
        if reason and reason != "not_in_main_game":
            raise RuntimeError(reason)
        if not self._context_matches(decision, before):
            raise ReplanRequired("state_context_stale")
        action = decision.get("recommended_action") or {}
        tool = action.get("tool")
        args = action.get("args") or {}
        replan_reason = self._decision_replan_reason(decision, before)
        if replan_reason:
            raise ReplanRequired(replan_reason)
        if tool not in EXECUTABLE_TOOLS:
            raise RuntimeError(f"unsupported_action:{tool}")
        entry_return_selected = bool(
            tool == "navigation_step"
            and self.debug.navigation_target_is_entry_return(before, int(args.get("id") or 0))
        )
        if tool == "navigation_step":
            self.debug.bind_room_entry_intent(
                before,
                int(args.get("id") or 0),
                intentional_return=entry_return_selected,
            )
        started = _now()
        motion_cursor = self.debug.motion_history.serial
        matched_item_object = None
        self.emit("action", "validating", f"Validated {tool}.", {"tool": tool, "args": args})
        if tool == "press_buttons":
            buttons = args.get("buttons") or []
            if isinstance(buttons, str):
                buttons = [buttons]
            action_result = self.debug.write_action([str(item).lower() for item in buttons], int(args.get("frames") or 1))
            after = self._wait_action_and_stable(action=action_result)
        elif tool == "move_to":
            action_result = self._move_to(args)
            after = self._wait_state_quiet()
        elif tool == "advance_dialog":
            action_result = self.debug.advance_dialog(args)
            if not action_result.get("ok"):
                raise RuntimeError(action_result.get("status") or "dialog_advance_failed")
            after = self._wait_state_quiet()
        elif tool == "choice_select":
            action_result = self.debug.write_choice_action(
                args.get("generation"), args.get("option_index")
            )
            after = self._wait_action_and_stable(action=action_result)
        elif tool == "use_item":
            matched_item_object = self.debug.matched_item_event_object(
                before,
                int(args.get("item_id") or 0),
            )
            action_result = self.debug.write_use_item_action(args.get("item_id"), args.get("role_id"), args.get("target"))
            after = self._wait_action_and_stable(action=action_result)
            native_result = self.debug.current_action_result()
            try:
                native_item_id = int(native_result.get("item_id") or 0)
            except (TypeError, ValueError):
                native_item_id = 0
            if native_result.get("action") != "use_item" or native_item_id != int(args.get("item_id") or 0):
                native_result = None
            action_result = {
                **action_result,
                **self.debug.item_object_effect_observation(before, after, matched_item_object),
                **({"result": native_result} if isinstance(native_result, dict) else {}),
            }
            if args.get("role_id") is not None and not before.get("in_battle"):
                self.debug.observe_visible_item_use(
                    before,
                    after,
                    int(args.get("item_id") or 0),
                    int(args.get("role_id")),
                )
        elif tool == "equip_item":
            action_result = self.debug.write_equip_item_action(
                args.get("item_id"),
                args.get("role_id"),
                args.get("expected_slot_item_id"),
            )
            after = self._wait_action_and_stable(action=action_result)
        elif tool in {"shop_buy", "shop_sell", "shop_leave"}:
            action_result = self.debug.write_shop_action(
                tool,
                args.get("item_id") if tool in {"shop_buy", "shop_sell"} else None,
            )
            after = self._wait_action_and_stable(action=action_result)
        else:
            action_result = self._navigate(
                int(args.get("id") or 0),
                int(args.get("item_id") or 0) or None,
            )
            after = self._wait_state_quiet()
        motion_target = int((action_result.get("trigger_evidence") or {}).get("object_id") or args.get("id") or 0)
        if (action_result.get("completion") == "trigger_completed"
                or (tool == "navigation_step" and self.debug.motion_history.view(after, motion_target).get("current_status") == "moving")):
            after = self._observe_target_motion(after, motion_target)
        if isinstance(action_result, dict):
            movement_target = args.get("id") if tool == "navigation_step" else (matched_item_object or {}).get("event_object_id")
            movement = self.debug.motion_history.changes(before, motion_cursor, movement_target)
            if movement:
                action_result["object_movement"] = movement
            evidence = action_result.get("trigger_evidence")
            association_target = (evidence or {}).get("object_id") or movement_target
            if association_target:
                current_target = self.debug.target_object(after, association_target) or {}
                action_result["target_observation"] = {
                    "id": association_target, "x": current_target.get("x"), "y": current_target.get("y"),
                    **{k: self.debug.motion_history.view(after, association_target).get(k) for k in
                       ("current_status", "sample_frame", "position_is_current", "position_is_stationary")},
                }
                summary = self.debug.motion_history.associate(after, association_target, tool, evidence, motion_cursor, before)
                if summary:
                    action_result["motion_association"] = summary
        observation_id = action_result.get("observation_id") if isinstance(action_result, dict) else None
        observation = self.debug.observations.consume(observation_id, owner="navigation")
        if isinstance(action_result, dict) and observation is not None:
            action_result = {**action_result, "observation": observation}
        delta = self.state_delta(before, after)
        result = {"ok": True, "tool": tool, "args": args, "result": self._compact_action_result(action_result), "delta": delta, "state": self._compact_state(after)}
        self._update_room_entry_after_execution(
            before,
            after,
            tool,
            args,
            action_result,
            entry_return_selected=entry_return_selected,
        )
        self.emit("action", "completed", f"Executed {tool}.", {"result": action_result}, started=started)
        self.emit("state_diff", "observed", "Observed stable state changes.", delta)
        return result

    def _wait_action_completion(
        self,
        action: dict,
        *,
        receipt_timeout: float = 3.0,
        completion_timeout: float = 10.0,
    ) -> dict:
        if not action.get("action_handshake_supported"):
            self.debug.append_navigation_diagnostic(
                "action_handshake_compatibility_wait",
                {"action": action},
            )
            deadline = time.monotonic() + completion_timeout
            while self.debug.action_path.exists() and time.monotonic() < deadline:
                if self.cancel_event.is_set():
                    raise RuntimeError("interrupted")
                time.sleep(0.04)
            if self.debug.action_path.exists():
                raise RuntimeError("action_timeout")
            return self.debug.snapshot()

        instance_id = str(action.get("game_instance_id") or "")
        generation_before = action.get("action_generation_before")
        receipt_started = time.monotonic()
        receipt_deadline = receipt_started + receipt_timeout
        received_generation = None
        latest = self.debug.read_live_state()
        while time.monotonic() < receipt_deadline:
            if self.cancel_event.is_set():
                raise RuntimeError("interrupted")
            latest = self.debug.read_live_state()
            if str(latest.get("game_instance_id") or "") != instance_id:
                raise RuntimeError("game_instance_changed")
            if self.debug.live_info("state").get("stale", True):
                raise RuntimeError("state_stale")
            native = self.debug.native_action_status(latest)
            if native is not None and native["generation"] != generation_before:
                received_generation = native["generation"]
                break
            self.debug.wait_navigation_update()
        if received_generation is None:
            self.debug.append_navigation_diagnostic(
                "action_generation_timeout",
                {
                    "game_instance_id": instance_id,
                    "generation_before": generation_before,
                    "wait_ms": round((time.monotonic() - receipt_started) * 1000, 1),
                },
                state=latest,
            )
            raise RuntimeError("action_generation_timeout")

        receipt_ms = (time.monotonic() - receipt_started) * 1000
        action["action_generation"] = received_generation
        action["action_receipt_wait_ms"] = round(receipt_ms, 1)
        native = self.debug.native_action_status(latest)
        self.debug.append_navigation_diagnostic(
            "action_generation_confirmed",
            {
                "game_instance_id": instance_id,
                "generation_before": generation_before,
                "generation": received_generation,
                "pending": bool(native and native["pending"]),
                "remaining_steps": native["remaining_steps"] if native else None,
                "wait_ms": round(receipt_ms, 1),
            },
            state=latest,
        )

        completion_started = time.monotonic()
        completion_deadline = completion_started + completion_timeout
        while time.monotonic() < completion_deadline:
            if self.cancel_event.is_set():
                raise RuntimeError("interrupted")
            latest = self.debug.read_live_state()
            if str(latest.get("game_instance_id") or "") != instance_id:
                raise RuntimeError("game_instance_changed")
            if self.debug.live_info("state").get("stale", True):
                raise RuntimeError("state_stale")
            native = self.debug.native_action_status(latest)
            if native is not None and native["generation"] == received_generation and not native["pending"]:
                action["action_completion_wait_ms"] = round(
                    (time.monotonic() - completion_started) * 1000,
                    1,
                )
                self.debug.append_navigation_diagnostic(
                    "action_completion_confirmed",
                    {
                        "game_instance_id": instance_id,
                        "generation": received_generation,
                        "receipt_wait_ms": round(receipt_ms, 1),
                        "completion_wait_ms": round((time.monotonic() - completion_started) * 1000, 1),
                        "remaining_steps": native["remaining_steps"],
                    },
                    state=latest,
                )
                return latest
            self.debug.wait_navigation_update()

        native = self.debug.native_action_status(latest)
        self.debug.append_navigation_diagnostic(
            "action_completion_timeout",
            {
                "game_instance_id": instance_id,
                "generation": received_generation,
                "wait_ms": round((time.monotonic() - completion_started) * 1000, 1),
                "remaining_steps": native["remaining_steps"] if native else None,
                "waiting_for_move": native["waiting_for_move"] if native else None,
            },
            state=latest,
        )
        raise RuntimeError("action_completion_timeout")

    def _wait_action_and_stable(
        self,
        timeout: float = 10.0,
        quiet_window: float = 2.0,
        action: dict | None = None,
    ) -> dict:
        if isinstance(action, dict):
            self._wait_action_completion(action, completion_timeout=timeout)
        else:
            deadline = time.monotonic() + timeout
            while self.debug.action_pending() and time.monotonic() < deadline:
                if self.cancel_event.is_set():
                    raise RuntimeError("interrupted")
                time.sleep(0.04)
            if self.debug.action_pending():
                raise RuntimeError("action_timeout")
        latest = self.debug.snapshot()
        if self._game_control_active(latest):
            self._wait_for_control(timeout=30.0)
        return self._wait_state_quiet(timeout=timeout, quiet_window=quiet_window)

    def _wait_state_quiet(self, timeout: float = 10.0, quiet_window: float = 2.0) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        quiet_since = time.monotonic()
        latest = self.debug.snapshot()
        while time.monotonic() < deadline:
            if self.cancel_event.is_set():
                raise RuntimeError("interrupted")
            latest = self.debug.snapshot()
            if self._game_control_active(latest):
                latest = self._wait_for_control(timeout=30.0)
                deadline = time.monotonic() + timeout
                last = None
                quiet_since = time.monotonic()
                continue
            signature = self._state_signature(latest)
            if signature == last:
                if time.monotonic() - quiet_since >= quiet_window:
                    return latest
            else:
                last = signature
                quiet_since = time.monotonic()
            time.sleep(0.1)
        raise RuntimeError("state_stability_timeout")

    def _wait_navigation_observation(self, target_id: int, observation_id: str) -> None:
        """Drive one transition observation without turning polling into navigation steps."""
        while True:
            if self.cancel_event.is_set():
                raise RuntimeError("interrupted")
            state = self.debug.snapshot()
            if self.cancel_event.is_set():
                raise RuntimeError("interrupted")
            control = self.debug.control_state(state)
            if control.get("phase") in {
                "action_pending", "game_script", "entering_scene", "dialog", "choice", "battle", "shop",
            }:
                return
            request = self.debug.observations.get(observation_id)
            mapped_id = self.debug.navigation_transition_requests.get(target_id)
            if mapped_id != observation_id or request is None or request.get("status") != "pending":
                return
            waited = self.debug.observations.wait(
                observation_id,
                timeout=0.12,
                cancel_event=self.cancel_event,
            )
            if self.cancel_event.is_set():
                raise RuntimeError("interrupted")
            if waited is None or waited.get("status") != "pending":
                return

    def _observe_target_motion(self, state: dict, target_id: int, timeout: float = 8.0) -> dict:
        """Retain post-trigger movement without requiring a moving room to become quiet."""
        room_id = self.debug.room_identity(state).get("id")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cancel_event.is_set():
                raise RuntimeError("interrupted")
            obj = self.debug.target_object(state, target_id)
            if (not obj or not obj.get("state") or self.debug.room_identity(state).get("id") != room_id
                    or state.get("in_battle") or (state.get("dialog") or {}).get("active")
                    or (state.get("choice") or {}).get("active") or (state.get("shop") or {}).get("active")):
                return state
            motion = self.debug.motion_history.view(state, target_id, limit=1)
            if motion.get("current_status") == "stationary":
                return state
            self.debug.wait_navigation_update(timeout=0.05)
            state = self.debug.navigation_snapshot()
        return state

    def _move_to(self, args: dict) -> dict:
        with self.debug.spatial_execution_scope("move_to", cancel_event=self.cancel_event):
            return self._move_to_impl(args)

    def _move_to_impl(self, args: dict) -> dict:
        initial = self.debug.navigation_snapshot()
        identity = self.debug.room_identity(initial)
        trigger = self.debug.native_trigger_generation(initial)
        stalled = 0
        for step in range(240):
            if self.cancel_event.is_set():
                raise RuntimeError("interrupted")
            state = self.debug.navigation_snapshot()
            if self.debug.room_identity(state).get("id") != identity.get("id"):
                return {"ok": True, "status": "route_invalidated", "reason": "room_changed"}
            receipt = self.debug.consume_spatial_completion(state)
            if receipt:
                return {"ok": True, "status": "arrived", "completion": "trigger_completed", **receipt}
            if self.debug.native_trigger_generation(state) != trigger:
                return {"ok": True, "status": "interrupted_by_trigger", "trigger": self.debug.native_trigger_generation(state)}
            result = self.debug.move_to_step(state, args, self.cancel_event)
            if result.get("status") == "cancelled":
                raise RuntimeError("interrupted")
            if result.get("status") == "state_changed":
                continue
            if result.get("status") in {"game_script", "action_pending", "entering_scene"}:
                self._wait_for_control()
                continue
            if not result.get("action"):
                return {**result, "execution_steps": step}
            after = self._wait_action_completion(result["action"])
            if self.cancel_event.is_set():
                raise RuntimeError("interrupted")
            self.emit("navigation", "progress", "Coordinate movement step completed.",
                      {"step": step + 1, "result": result, "actual_position": after.get("party_position")})
            before_pos = (state.get("party_position"), state.get("party_direction"))
            after_pos = (after.get("party_position"), after.get("party_direction"))
            stalled = stalled + 1 if before_pos == after_pos else 0
            if stalled >= 2:
                return {"ok": False, "status": "no_progress", "actual_position": after.get("party_position")}
            if result.get("trigger_ids"):
                settled = self._wait_state_quiet()
                receipt = self.debug.consume_spatial_completion(settled)
                if receipt:
                    return {"ok": True, "status": "arrived", "completion": "trigger_completed", **receipt}
                return {"ok": True, "status": "trigger_boundary_reached", "trigger_ids": result["trigger_ids"],
                        "actual_position": after.get("party_position")}
        return {"ok": False, "status": "movement_step_limit"}

    def _navigate(self, target_id: int, approach_item_id: int | None = None) -> dict:
        with self.debug.spatial_execution_scope("navigate", target_id, self.cancel_event):
            return self._navigate_impl(target_id, approach_item_id)

    def _navigate_impl(self, target_id: int, approach_item_id: int | None = None) -> dict:
        if target_id <= 0:
            raise RuntimeError("navigation_target_missing")
        progress = []
        terminal = {"arrived", "approached", "transitioned", "interacting"}
        no_progress_key = None
        no_progress_count = 0
        movement_started = False
        route_replan_used = False
        step = 0
        while step < 240:
            if self.cancel_event.is_set():
                raise RuntimeError("interrupted")
            state = self.debug.navigation_snapshot()
            if approach_item_id is None:
                result = self.debug.navigation_step(
                    state,
                    target_id,
                    cancel_event=self.cancel_event,
                )
            else:
                result = self.debug.navigation_step(
                    state,
                    target_id,
                    approach_item_id=approach_item_id,
                    cancel_event=self.cancel_event,
                )
            if self.cancel_event.is_set() or result.get("status") == "cancelled":
                raise RuntimeError("interrupted")
            step += 1
            progress.append({key: result.get(key) for key in ("status", "move", "segment_steps", "remaining_steps", "required_direction", "observation_id")})
            self.emit("navigation", "progress", f"Navigation step {step}: {result.get('status')}.", {"step": step, "target_id": target_id, "result": progress[-1]})
            status = result.get("status")
            if status == "waiting_for_control":
                self._wait_for_control()
                continue
            if status == "entering_scene":
                self._wait_scene_transition()
                return {"ok": True, "status": "transitioned", "steps": step, "target_id": target_id, "progress": progress[-20:]}
            if status == "in_dialog":
                return {
                    "ok": True,
                    "status": "interrupted_by_dialog",
                    "steps": step,
                    "target_id": target_id,
                    "progress": progress[-20:],
                }
            if status == "in_choice":
                return {
                    "ok": True,
                    "status": "interrupted_by_choice",
                    "steps": step,
                    "target_id": target_id,
                    "progress": progress[-20:],
                }
            if status == "in_battle":
                return {
                    "ok": True,
                    "status": "interrupted_by_battle",
                    "steps": step,
                    "target_id": target_id,
                    "progress": progress[-20:],
                }
            if status == "in_shop":
                return {
                    "ok": True,
                    "status": "interrupted_by_shop",
                    "steps": step,
                    "target_id": target_id,
                    "progress": progress[-20:],
                }
            if not result.get("ok"):
                if (
                    movement_started
                    and not route_replan_used
                    and status in {"blocked_by_transition", "unreachable_static"}
                ):
                    route_replan_used = True
                    self.debug.append_navigation_diagnostic(
                        "navigation_route_replan",
                        {
                            "target_id": target_id,
                            "reason": status,
                            "attempt": 1,
                        },
                        state=state,
                    )
                    continue
                if (
                    movement_started
                    and route_replan_used
                    and status in {"blocked_by_transition", "unreachable_static"}
                ):
                    return {
                        "ok": True,
                        "status": "route_invalidated",
                        "reason": status,
                        "steps": step,
                        "target_id": target_id,
                        "progress": progress[-20:],
                    }
                raise RuntimeError(f"navigation_{status or 'failed'}")
            if result.get("action"):
                before_position = (state.get("party_position") or {}).copy()
                if status in {"stepping", "facing"}:
                    after_action = self._wait_action_completion(result["action"])
                else:
                    after_action = self._wait_action_and_stable(quiet_window=0.2, action=result["action"])
                expected = result.get("expected_node")
                if isinstance(expected, dict):
                    actual = after_action.get("party_position") or {}
                    before_xy = (
                        int(before_position.get("x") or 0),
                        int(before_position.get("y") or 0),
                    )
                    actual_xy = (int(actual.get("x") or 0), int(actual.get("y") or 0))
                    expected_xy = (int(expected.get("x") or 0), int(expected.get("y") or 0))
                    diagnostic = {
                        "target_id": target_id,
                        "generation": result["action"].get("action_generation"),
                        "expected_position": {"x": expected_xy[0], "y": expected_xy[1]},
                        "actual_position": {"x": actual_xy[0], "y": actual_xy[1]},
                        "start_position": {"x": before_xy[0], "y": before_xy[1]},
                        "segment_steps": result.get("segment_steps"),
                        "remaining_steps": result.get("remaining_steps"),
                    }
                    if actual_xy == expected_xy:
                        self.debug.append_navigation_diagnostic(
                            "navigation_expected_node_reached",
                            diagnostic,
                            state=after_action,
                        )
                        no_progress_key = None
                        no_progress_count = 0
                    elif actual_xy != before_xy:
                        self.debug.append_navigation_diagnostic(
                            "navigation_segment_partial",
                            diagnostic,
                            state=after_action,
                        )
                        no_progress_key = None
                        no_progress_count = 0
                    else:
                        direction = int(state.get("party_direction") or 0)
                        key = (before_xy, direction, expected_xy)
                        no_progress_count = no_progress_count + 1 if key == no_progress_key else 1
                        no_progress_key = key
                        self.debug.append_navigation_diagnostic(
                            "navigation_segment_no_progress",
                            {**diagnostic, "attempt": no_progress_count},
                            state=after_action,
                        )
                        if no_progress_count > 1:
                            raise RuntimeError("navigation_no_progress")
                if status == "stepping":
                    movement_started = True
            if status == "interacting":
                transition_observation_id = self.debug.navigation_transition_requests.get(target_id)
                if transition_observation_id:
                    self._wait_navigation_observation(target_id, transition_observation_id)
                    continue
            if status in terminal:
                terminal_result = {
                    "ok": True,
                    "status": status,
                    "steps": step,
                    "target_id": target_id,
                    "progress": progress[-20:],
                }
                if result.get("observation_id"):
                    terminal_result["observation_id"] = result["observation_id"]
                if result.get("completion"):
                    terminal_result["completion"] = result["completion"]
                if result.get("trigger_evidence"):
                    terminal_result["trigger_evidence"] = result["trigger_evidence"]
                if status == "approached":
                    if isinstance(result.get("approach"), dict):
                        terminal_result["approach"] = result["approach"]
                    if isinstance(result.get("affordance"), dict):
                        terminal_result["affordance"] = result["affordance"]
                return terminal_result
            if status == "transition_observing":
                observation_id = result.get("observation_id")
                if observation_id:
                    self._wait_navigation_observation(target_id, observation_id)
                else:
                    time.sleep(0.12)
                continue
        raise RuntimeError("navigation_step_limit")

    def _wait_scene_transition(self, timeout: float = 10.0) -> dict:
        """Wait until SDLPAL finishes a scene transition before stable observation."""
        deadline = time.monotonic() + timeout
        latest = self.debug.snapshot()
        while latest.get("entering_scene") and time.monotonic() < deadline:
            if self.cancel_event.is_set():
                raise RuntimeError("interrupted")
            time.sleep(0.1)
            latest = self.debug.snapshot()
        if latest.get("entering_scene"):
            raise RuntimeError("navigation_transition_timeout")
        return latest

    @staticmethod
    def _state_signature(state: dict) -> tuple:
        def vanish_phase(value: Any) -> int:
            try:
                countdown = int(value or 0)
            except (TypeError, ValueError):
                countdown = 0
            return -1 if countdown < 0 else 1 if countdown > 0 else 0

        pos = state.get("party_position") or {}
        dialog = state.get("dialog") or {}
        latest_dialog_event = dialog.get("latest_event")
        if not isinstance(latest_dialog_event, dict):
            latest_dialog_event = {}
        debug = state.get("_debug") or {}
        recent_events = debug.get("recent_events") or []
        latest_event = {}
        for event in reversed(recent_events):
            if not isinstance(event, dict):
                continue
            if event.get("type") == "object_changed":
                changes = event.get("changes") if isinstance(event.get("changes"), dict) else {}
                if changes:
                    semantic_change = False
                    for key, change in changes.items():
                        if key == "auto_script":
                            continue
                        if (
                            key == "vanish_time"
                            and isinstance(change, dict)
                            and "from" in change
                            and "to" in change
                            and vanish_phase(change.get("from")) == vanish_phase(change.get("to"))
                        ):
                            continue
                        semantic_change = True
                        break
                    if not semantic_change:
                        continue
            latest_event = event
            break
        inventory = tuple(
            (item.get("item_id"), item.get("amount"), item.get("amount_in_use"))
            for item in state.get("inventory") or [] if isinstance(item, dict)
        )
        party_effects = tuple(
            (
                role.get("role_id"),
                tuple(
                    (
                        effect.get("status_id"), effect.get("key"),
                        effect.get("remaining_rounds"), effect.get("persistent"),
                    )
                    for effect in role.get("status_effects") or []
                    if isinstance(effect, dict)
                ),
                tuple(
                    (poison.get("poison_id"), poison.get("name"))
                    for poison in role.get("poisons") or []
                    if isinstance(poison, dict)
                ),
            )
            for role in state.get("party") or [] if isinstance(role, dict)
        )
        objects = tuple(
            (
                item.get("id"), item.get("state"), vanish_phase(item.get("vanish_time")),
                item.get("interactable"), item.get("blocking"),
            )
            for item in state.get("objects") or [] if isinstance(item, dict)
        )
        shop = state.get("shop") if isinstance(state.get("shop"), dict) else {}
        choice = state.get("choice") if isinstance(state.get("choice"), dict) else {}
        shop_items = tuple(
            (
                item.get("item_id"), item.get("owned_count"),
                item.get("affordable"), item.get("selected"),
            )
            for item in shop.get("items") or [] if isinstance(item, dict)
        )
        return (
            state.get("scene_id"), state.get("map_id"), state.get("room_key"),
            pos.get("x"), pos.get("y"), state.get("party_direction"),
            bool(dialog.get("active")), dialog.get("message_id"), state.get("in_battle"),
            state.get("entering_scene"), latest_dialog_event.get("sequence"),
            latest_event.get("time"), latest_event.get("type"), latest_event.get("object_id"),
            state.get("cash"),
            bool(shop.get("active")), shop.get("mode"), shop.get("store_id"),
            shop.get("phase"), shop.get("selected_item_id"), shop_items,
            bool(choice.get("active")), choice.get("generation"), choice.get("selected_index"),
            inventory, party_effects, objects,
        )

    @staticmethod
    def _compact_state(state: dict) -> dict:
        pos = state.get("party_position") or {}
        dialog = state.get("dialog") or {}
        latest = dialog.get("latest_event") or {}
        shop = state.get("shop") if isinstance(state.get("shop"), dict) else {}
        choice = state.get("choice") if isinstance(state.get("choice"), dict) else {}
        return {
            "scene_id": state.get("scene_id"), "map_id": state.get("map_id"),
            "room_key": state.get("room_key") or (state.get("room") or {}).get("key"),
            "region_key": state.get("region_key"),
            "position": {"x": pos.get("x"), "y": pos.get("y"), "direction": state.get("party_direction")},
            "dialog": {"active": bool(dialog.get("active")), "message_id": latest.get("message_id"), "event_object_id": latest.get("event_object_id"), "text": latest.get("text")},
            "choice": choice if choice.get("active") else None,
            "cash": state.get("cash"),
            "shop": {
                "active": bool(shop.get("active")),
                "mode": shop.get("mode"),
                "store_id": shop.get("store_id"),
                "phase": shop.get("phase"),
                "selected_item_id": shop.get("selected_item_id"),
            },
            "in_battle": bool(state.get("in_battle")), "entering_scene": bool(state.get("entering_scene")),
        }

    @staticmethod
    def _compact_action_result(result: Any) -> Any:
        if not isinstance(result, dict):
            return result
        compact = {
            key: result.get(key)
            for key in (
                "ok", "status", "completion", "trigger_evidence", "presses", "steps", "target_id", "frames",
                "buttons", "observation_id", "command", "item_id", "role_id",
                "slot_id", "slot_key", "previous_item_id", "store_id",
                "price", "cash_before", "cash_after", "owned_before", "owned_after",
                "approach", "affordance", "matched_event_object_id",
                "effect_observed", "object_delta", "object_movement", "motion_association", "target_observation", "cell", "face",
                "actual_position", "trigger_ids", "reason", "execution_steps",
            )
            if key in result
        }
        native_result = result.get("result")
        if isinstance(native_result, dict):
            compact["native_result"] = {
                key: native_result.get(key)
                for key in (
                    "ok", "status", "action", "item_id", "role_id",
                    "slot_id", "slot_key", "previous_item_id", "store_id",
                    "price", "cash_before", "cash_after", "owned_before",
                    "owned_after", "error",
                    "script_success",
                )
                if key in native_result
            }
        observation = result.get("observation")
        if isinstance(observation, dict):
            compact["observation"] = {
                key: observation.get(key)
                for key in ("id", "kind", "status", "reason", "result")
                if observation.get(key) is not None
            }
        dialogs = result.get("dialogs")
        if isinstance(dialogs, list):
            compact["dialogs"] = [
                {key: item.get(key) for key in ("sequence", "end_sequence", "speaker_hint", "text", "message_id", "event_object_id") if key in item}
                for item in dialogs[-12:] if isinstance(item, dict)
            ]
            compact["dialog_count"] = len(dialogs)
        return compact

    @staticmethod
    def state_delta(before: dict, after: dict) -> dict:
        def position(value: dict) -> dict:
            pos = value.get("party_position") or {}
            return {"x": pos.get("x"), "y": pos.get("y"), "direction": value.get("party_direction")}

        def inventory(value: dict) -> dict[int, int]:
            return {int(item.get("item_id") or 0): int(item.get("amount") or 0) for item in value.get("inventory") or []}

        def objects(value: dict) -> dict[int, tuple]:
            return {int(item.get("id") or 0): (item.get("state"), item.get("vanish_time"), item.get("interactable"), item.get("blocking")) for item in value.get("objects") or []}

        def equipment(value: dict) -> dict[tuple[int, int], int]:
            result = {}
            for role in value.get("party") or []:
                if not isinstance(role, dict):
                    continue
                try:
                    role_id = int(role.get("role_id"))
                except (TypeError, ValueError):
                    continue
                for item in role.get("equipment") or []:
                    if not isinstance(item, dict):
                        continue
                    try:
                        slot_id = int(item.get("slot_id"))
                        item_id = int(item.get("item_id") or 0)
                    except (TypeError, ValueError):
                        continue
                    result[(role_id, slot_id)] = item_id
            return result

        delta: dict[str, Any] = {}
        fields = {
            "position": (position(before), position(after)),
            "room": ({key: before.get(key) for key in ("scene_id", "map_id", "room_key", "region_key")}, {key: after.get(key) for key in ("scene_id", "map_id", "room_key", "region_key")}),
            "dialog": (AgentRuntime._compact_state(before)["dialog"], AgentRuntime._compact_state(after)["dialog"]),
            "choice": (AgentRuntime._compact_state(before)["choice"], AgentRuntime._compact_state(after)["choice"]),
            "cash": (before.get("cash"), after.get("cash")),
            "shop": (
                AgentRuntime._compact_state(before)["shop"],
                AgentRuntime._compact_state(after)["shop"],
            ),
        }
        for key, (old, new) in fields.items():
            if old != new:
                delta[key] = {"before": old, "after": new}
        before_inventory, after_inventory = inventory(before), inventory(after)
        inventory_changes = [
            {"item_id": item_id, "before": before_inventory.get(item_id, 0), "after": after_inventory.get(item_id, 0)}
            for item_id in sorted(set(before_inventory) | set(after_inventory))
            if before_inventory.get(item_id, 0) != after_inventory.get(item_id, 0)
        ]
        if inventory_changes:
            delta["inventory_changes"] = inventory_changes
        before_equipment, after_equipment = equipment(before), equipment(after)
        equipment_changes = [
            {
                "role_id": role_id,
                "slot_id": slot_id,
                "before": before_equipment.get((role_id, slot_id), 0),
                "after": after_equipment.get((role_id, slot_id), 0),
            }
            for role_id, slot_id in sorted(set(before_equipment) | set(after_equipment))
            if before_equipment.get((role_id, slot_id), 0) != after_equipment.get((role_id, slot_id), 0)
        ]
        if equipment_changes:
            delta["equipment_changes"] = equipment_changes
        before_objects, after_objects = objects(before), objects(after)
        object_changes = [
            {"id": object_id, "before": before_objects.get(object_id), "after": after_objects.get(object_id)}
            for object_id in sorted(set(before_objects) | set(after_objects))
            if before_objects.get(object_id) != after_objects.get(object_id)
        ]
        if object_changes:
            delta["object_changes"] = object_changes
        before_debug, after_debug = before.get("_debug") or {}, after.get("_debug") or {}
        for source, key in (("recent_dialogs", "new_dialogs"), ("recent_events", "new_events")):
            old_items = before_debug.get(source) or []
            new_items = after_debug.get(source) or []
            old_keys = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in old_items if isinstance(item, dict)}
            additions = [item for item in new_items if isinstance(item, dict) and json.dumps(item, ensure_ascii=False, sort_keys=True) not in old_keys]
            if additions:
                delta[key] = additions[-12:]
        return delta or {"stable": True, "changes": 0}
