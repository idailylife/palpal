"""Incremental, player-visible battle evidence; no hidden enemy statistics."""

from __future__ import annotations

import copy


def outcome_delta(outcomes: list, after: int) -> tuple[list[dict], dict]:
    by_sequence = {
        int(item.get("sequence") or 0): item
        for item in outcomes if isinstance(item, dict)
        and int(item.get("sequence") or 0) > after
    }
    missing = []
    expected = after + 1
    ordered = []
    for sequence, item in sorted(by_sequence.items()):
        if sequence > expected:
            missing.append({"first": expected, "last": sequence - 1})
        ordered.append(item)
        expected = sequence + 1
    return ordered, {
        "after_sequence": after,
        "through_sequence": expected - 1,
        "complete": not missing,
        "missing_ranges": missing,
    }


def survival_risk(view: dict, previous: dict | None) -> tuple[dict, dict]:
    """Build a candidate checkpoint. Caller commits only after successful delivery.

    A window spans new outcomes between decisions, not each character's command
    selection. Re-reading the same window therefore cannot erase its evidence.
    HP loss/recovery are observed deltas, not uncapped damage or future bounds.
    """
    state = copy.deepcopy(previous or {})
    outcomes, delivery = outcome_delta(
        view.get("recent_outcomes") or [], int(state.get("last_sequence") or 0),
    )
    players = view.get("players") or []
    baseline = state.get("players")
    maxima = state.setdefault("observed_maxima", {})
    changed_without_outcome = baseline is not None and any(
        (baseline.get(str(p["index"])) or {}).get("hp") != p.get("hp") for p in players
    )
    if outcomes or changed_without_outcome:
        rows = {}
        for player in players:
            index = str(player["index"])
            before = (baseline or {}).get(index)
            rows[index] = {
                "index": player["index"],
                "hp_before": before.get("hp") if before else None,
                "hp_after": player.get("hp"),
                "observed_hp_loss": 0,
                "observed_hp_recovery": 0,
            }
        enemy_actions = 0
        for outcome in outcomes:
            if outcome.get("actor_side") == "enemy":
                enemy_actions += 1
            for change in outcome.get("player_changes") or []:
                index = str(change.get("index"))
                hp = change.get("hp")
                if index not in rows:
                    continue
                if isinstance(hp, dict):
                    delta = int(hp.get("after") or 0) - int(hp.get("before") or 0)
                elif "hp_delta" in change:
                    # Historical compact prompts retain only this visible delta.
                    delta = int(change["hp_delta"])
                else:
                    continue
                row = rows[index]
                row["observed_hp_loss"] += max(0, -delta)
                row["observed_hp_recovery"] += max(0, delta)
                peak = maxima.setdefault(index, {"single_outcome_hp_loss": 0, "window_hp_loss": 0})
                peak["single_outcome_hp_loss"] = max(peak["single_outcome_hp_loss"], -delta)
        for index, row in rows.items():
            peak = maxima.setdefault(index, {"single_outcome_hp_loss": 0, "window_hp_loss": 0})
            peak["window_hp_loss"] = max(peak["window_hp_loss"], row["observed_hp_loss"])
            row["net_hp_loss"] = (
                row["hp_before"] - row["hp_after"]
                if row["hp_before"] is not None and row["hp_after"] is not None else None
            )
            row["unexplained_hp_delta"] = (
                row["hp_after"] - row["hp_before"]
                - (row["observed_hp_recovery"] - row["observed_hp_loss"])
                if row["net_hp_loss"] is not None else None
            )
        accounted = baseline is not None and delivery["complete"] and all(
            row["unexplained_hp_delta"] == 0 for row in rows.values()
        )
        state["last_window"] = {
            **delivery,
            "baseline_available": baseline is not None,
            "enemy_action_count": enemy_actions,
            "players": list(rows.values()),
            "hp_accounting": "matched" if accounted else "unresolved",
        }
        state["last_sequence"] = delivery["through_sequence"]
        state["history_incomplete"] = bool(state.get("history_incomplete")) or not delivery["complete"]
    state["players"] = {
        str(player["index"]): {"hp": player.get("hp")}
        for player in players
    }
    summary = {
        "history_incomplete": bool(state.get("history_incomplete")),
        "observed_maxima_are_not_damage_limits": True,
        "last_window": state.get("last_window"),
        "players": [{
            **{key: player[key] for key in (
                "index", "hp", "max_hp", "mp", "max_mp", "status_effects", "poisons",
            ) if key in player},
            "within_observed_single_loss": 0 < player.get("hp", 0) <= maxima.get(
                str(player["index"]), {}).get("single_outcome_hp_loss", 0),
            "observed_maxima": maxima.get(str(player["index"]), {
                "single_outcome_hp_loss": 0, "window_hp_loss": 0,
            }),
        } for player in players],
    }
    return copy.deepcopy(summary), state


def decision_feedback(view: dict, new_outcomes: list[dict]) -> dict:
    """Only derived visible facts; zero healing is not a claim about intent."""
    events = []
    for outcome in new_outcomes:
        event = {"sequence": outcome["sequence"]}
        changes = outcome.get("player_changes") or []
        event["affected_players"] = [c["index"] for c in changes if "index" in c]
        if outcome.get("actor_side") == "player":
            event["effective_hp_recovery"] = sum(max(0, c.get("hp_delta", 0)) for c in changes)
        # Refer to the ordered detailed event for status and stat deltas rather
        # than duplicating them in a second full transcript.
        events.append(event)
    return {
        "status": "effects_observed" if events else "no_new_effects",
        "submission_is_not_execution": True,
        "events": events,
    }


def survival_review(view: dict, action: dict) -> dict | None:
    """Request deliberation, never predict damage or choose an action."""
    legal = next((a for a in view.get("available_actions", [])
                  if a.get("action_type") == action.get("action_type")
                  and a.get("object_id", 0) == action.get("object_id", 0)), {})
    offensive = action.get("action_type") in ("attack", "throw_item") or (
        action.get("action_type") in ("magic", "coop_magic")
        and legal.get("target_side") == "enemy")
    risk = view.get("survival_risk") or {}
    endangered = [p for p in risk.get("players", [])
                  if p.get("within_observed_single_loss")]
    if not offensive or not endangered:
        return None
    return {
        "ok": False, "accepted": False, "status": "survival_review_required",
        "endangered_players": endangered,
        "history_incomplete": risk.get("history_incomplete", True),
        "instruction": (
            "No native action has been submitted. Reconsider once using current legal "
            "recovery/protection actions and allies' committed actions. Compare recovery "
            "timing and evidence for a kill. Unknown enemy HP cannot establish a finishing "
            "blow; previous attack order does not establish healing order. Observed damage "
            "is not a future upper bound. Submit your final legal choice; it may remain "
            "offensive and will not require another review in this command window."
        ),
    }
