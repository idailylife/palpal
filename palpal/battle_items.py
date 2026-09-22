"""Battle-local batch lookup and directly readable, snapshot-stable pagination."""

import json
import uuid


ITEM_DETAILS_DESCRIPTION = (
    "Read current quantities, DOS manual/handbook-equivalent descriptions, supported use/throw "
    "effects and visible samples for item_ids "
    "(up to 32 distinct IDs); legacy item_id is also accepted. Supply exactly one of "
    "item_id, item_ids, or cursor (valid within this decision). preview_complete=false means unknown additional effects, "
    "not no effect. If complete=false, follow next_cursor with this tool; concatenate text "
    "fragments when content_format=json_fragment. Do not infer effects from names."
)
ITEM_DETAILS_SCHEMA = {
    "type": "object",
    "properties": {
        "item_id": {"type": "integer", "minimum": 1},
        "item_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1},
        "cursor": {"type": "string"},
    },
    "additionalProperties": False,
}

ITEM_EXPERIENCE_SAMPLE_LIMIT = 3
ITEM_DETAIL_KEYS = (
    "item_id", "name", "description", "available_amount", "consuming", "use_effect", "throw_effect",
)
LEGAL_ACTION_KEYS = (
    "action_type", "target_side", "target_mode", "submission_target_indices",
)


def _compact_item(item):
    return {key: item[key] for key in ITEM_DETAIL_KEYS if key in item}


def _compact_player_change(change):
    compact = {
        key: change[key]
        for key in ("index", "role_id", "knocked_out")
        if key in change and (key != "knocked_out" or change[key])
    }
    for stat in ("hp", "mp"):
        value = change.get(stat)
        if isinstance(value, dict):
            try:
                before = int(value.get("before") or 0)
                after = int(value.get("after") or 0)
            except (TypeError, ValueError):
                continue
            if before != after:
                compact[f"{stat}_delta"] = after - before
        elif f"{stat}_delta" in change:
            compact[f"{stat}_delta"] = change[f"{stat}_delta"]
    for prefix in ("status", "poisons"):
        before = change.get(f"{prefix}_before")
        after = change.get(f"{prefix}_after")
        if before != after:
            compact[f"{prefix}_before"] = before or []
            compact[f"{prefix}_after"] = after or []
    return compact


def _compact_sample(sample):
    compact = {}
    enemy_changes = [
        {
            key: change[key]
            for key in ("slot_index", "object_id", "amount", "kind", "defeated")
            if key in change
        }
        for change in sample.get("enemy_changes") or []
        if isinstance(change, dict)
    ]
    if enemy_changes:
        compact["enemy_changes"] = enemy_changes
    player_changes = [
        _compact_player_change(change)
        for change in sample.get("player_changes") or []
        if isinstance(change, dict)
    ]
    player_changes = [change for change in player_changes if len(change) > 1]
    if player_changes:
        compact["player_changes"] = player_changes
    if sample.get("no_numeric_change_observed"):
        compact["no_numeric_change_observed"] = True
    return compact


def _compact_observed_effects(entries, item_id):
    grouped = {}
    for entry_order, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("object_id") != item_id:
            continue
        action_type = entry.get("action_type")
        if action_type not in {"use_item", "throw_item"}:
            continue
        enemy_object_id = int(entry.get("enemy_object_id") or 0)
        key = (action_type, enemy_object_id)
        group = grouped.setdefault(key, {"sample_count": 0, "samples": []})
        try:
            updated_at = float(entry.get("updated_at") or 0)
        except (TypeError, ValueError):
            updated_at = 0
        for sample_order, sample in enumerate(entry.get("samples") or []):
            if not isinstance(sample, dict):
                continue
            group["sample_count"] += 1
            compact = _compact_sample(sample)
            if compact:
                group["samples"].append(((updated_at, entry_order, sample_order), compact))

    observed = []
    for (action_type, enemy_object_id), group in grouped.items():
        result = {"action_type": action_type}
        if enemy_object_id:
            result["enemy_object_id"] = enemy_object_id
        result["sample_count"] = group["sample_count"]
        result["recent_samples"] = [
            sample for _, sample in sorted(group["samples"], key=lambda pair: pair[0], reverse=True)
            [:ITEM_EXPERIENCE_SAMPLE_LIMIT]
        ]
        observed.append(result)
    return observed


class BattleItemDetails:
    def __init__(self, debug, *, page_chars=12000):
        self.debug = debug
        self.page_chars = page_chars
        self.pages = {}

    def _page(self, encoded):
        if len(encoded) <= self.page_chars:
            return encoded
        # Frozen fragments also support a single unusually large item without
        # truncating its effects or requiring an unavailable archive tool.
        fragments = [encoded[n:n + self.page_chars] for n in range(0, len(encoded), self.page_chars)]
        cursors = [uuid.uuid4().hex for _ in fragments]
        responses = [json.dumps({
            "ok": True, "content_format": "json_fragment", "text": fragment,
            "complete": n == len(fragments) - 1,
            "next_cursor": cursors[n + 1] if n + 1 < len(fragments) else None,
        }, ensure_ascii=False, separators=(",", ":")) for n, fragment in enumerate(fragments)]
        self.pages.update(zip(cursors, responses))
        return responses[0]

    def query(self, args):
        if not isinstance(args, dict) or set(args) - {"item_id", "item_ids", "cursor"} or len(args) != 1:
            return json.dumps({"ok": False, "error": "supply_one_of_item_id_item_ids_cursor"})
        if "cursor" in args:
            cursor = args["cursor"]
            return self.pages.get(cursor, json.dumps({"ok": False, "error": "invalid_cursor_requery_items"})) if isinstance(cursor, str) else json.dumps({"ok": False, "error": "invalid_cursor"})
        single = "item_id" in args
        ids = [args["item_id"]] if single else args["item_ids"]
        if not isinstance(ids, list) or not ids or any(type(i) is not int or i < 1 for i in ids):
            return json.dumps({"ok": False, "error": "invalid_item_id"})
        ids = list(dict.fromkeys(ids))
        if len(ids) > 32:
            return json.dumps({"ok": False, "error": "too_many_item_ids", "maximum": 32})
        state = self.debug.snapshot()
        inventory = {item.get("item_id"): item for item in state.get("inventory") or [] if isinstance(item, dict)}
        battle = state.get("battle") or {}
        experience = self.debug.combat_experience(battle)
        samples = list(experience.get("support_actions") or []) + [
            action for memory in experience.get("enemy_memories") or []
            for action in memory.get("player_actions") or []
        ]
        results = []
        for item_id in ids:
            candidate = inventory.get(item_id)
            if candidate is None:
                results.append({"ok": False, "error": "item_not_in_inventory", "item_id": item_id})
                continue
            results.append({
                "ok": True,
                "item": _compact_item(self.debug.player_battle_inventory_item(candidate)),
                "observed_effects": _compact_observed_effects(samples, item_id),
                "legal_battle_actions": [
                    {key: action[key] for key in LEGAL_ACTION_KEYS if key in action}
                    for action in battle.get("available_actions") or []
                    if isinstance(action, dict) and action.get("object_id") == item_id
                    and action.get("action_type") in {"use_item", "throw_item"}
                ],
            })
        output = results[0] if single else {"ok": True, "results": results, "complete": True}
        return self._page(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
