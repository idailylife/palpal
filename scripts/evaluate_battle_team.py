"""Observation-only team regression: no game control, no live session mutations.

Historical observations/actions are fixed, not a simulated counterfactual battle.
Both variants receive the same past visible evidence, without original reasoning.
Future queries/samples are unavailable. command_status is reconstructed only from
accepted actions since the last observed resolution, never from stale action fields.
"""

import argparse
import asyncio
import copy
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from agents import Agent, AsyncOpenAI, FunctionTool, ModelSettings, OpenAIResponsesModel, Runner, set_tracing_disabled
from agents.agent import ToolsToFinalOutputResult

from palpal.agent_runtime import BattleDelegate, BATTLE_INSTRUCTIONS
from palpal.battle_items import BattleItemDetails, ITEM_DETAILS_DESCRIPTION, ITEM_DETAILS_SCHEMA
from palpal.battle_observations import survival_risk
from palpal.debug_server import ROOT, read_env_file

COMMANDS = (6, 9, 10, 12, 13, 18)


def views_with_pending(fixture, command):
    pending = {}
    actions = {a["command_generation"]: a for a in fixture["submitted_actions"]}
    for source in fixture["views"]:
        n = source["command_generation"]
        if n > command:
            break
        if source.get("new_outcomes"):
            pending = {}
        view = copy.deepcopy(source)
        for p in view["players"]:
            unable = p["hp"] == 0 or any(e.get("key") in {"sleep", "paralyzed", "confused"}
                                                   for e in p.get("status_effects") or [])
            p["command_status"] = ("unable" if unable else "committed" if p["index"] in pending
                                   else "selecting" if p["index"] == view["current_player_index"] else "unselected")
            if p["index"] in pending:
                p["submitted_action"] = copy.deepcopy(pending[p["index"]])
        yield view
        if n in actions:
            pending[view["current_player_index"]] = {k: actions[n][k] for k in ("action_type", "object_id", "target_index")}


def item_at(fixture, command, item_id, current):
    record = fixture["item_details"].get(str(item_id))
    if record is None or record["queried_at"] > command:
        return None
    item = copy.deepcopy(record["output"]["item"])
    # Count executed consumptions, not merely accepted commands. The prompt for
    # a second party member may reserve an item without consuming it yet.
    consumed = sum(1 for v in fixture["views"]
                   if record["queried_at"] < v["command_generation"] <= command
                   for o in v.get("new_outcomes") or []
                   if o.get("object_id") == item_id and o.get("action_type") in {"use_item", "throw_item"})
    available = int(item.get("available_amount", item.get("amount", 0)) or 0)
    available = max(0, available - consumed)
    if "amount" in item:
        item["amount"] = available
    reserved = sum(1 for p in current["players"] if p.get("command_status") == "committed"
                   and (p.get("submitted_action") or p.get("committed_action") or {}).get("object_id") == item_id
                   and (p.get("submitted_action") or p.get("committed_action") or {}).get("action_type") in {"use_item", "throw_item"})
    item["available_amount"] = max(0, available - reserved)
    return item


def replay_inputs(fixture, command, revised):
    messages = []
    checkpoint = None
    for view in views_with_pending(fixture, command):
        n = view["command_generation"]
        if revised:
            source = copy.deepcopy(view)
            source["recent_outcomes"] = source.pop("new_outcomes", [])
            source.pop("survival_risk", None)
            source["survival_risk"], checkpoint = survival_risk(source, checkpoint)
            view = BattleDelegate._agent_battle_view(source, include_experience=True,
                                                    after_outcome_sequence=source["outcome_delivery"]["after_sequence"])
        else:
            for p in view["players"]:
                p.pop("command_status", None)
        messages.append({"role": "user", "content": json.dumps({"battle": view}, ensure_ascii=False, separators=(",", ":"))})
        # Replay only queries that were already observed before this decision;
        # the model can ask for this command's known details via its real tools.
        if n < command:
            known = [r["output"] for r in fixture["item_details"].values() if r["queried_at"] == n]
            if known:
                messages.append({"role": "user", "content": json.dumps({"previous_item_query_results": known}, ensure_ascii=False)})
    return messages, view


async def evaluate(fixture, command, revised, repetition, model):
    messages, current = replay_inputs(fixture, command, revised)
    source_current = list(views_with_pending(fixture, command))[-1]
    inventory = [item for key in fixture["item_details"]
                 if (item := item_at(fixture, command, int(key), source_current)) is not None]
    samples = [a for r in fixture["item_details"].values() if r["queried_at"] <= command
               for a in r["output"].get("observed_effects") or []]
    reader = BattleItemDetails(SimpleNamespace(
        snapshot=lambda: {"inventory": inventory, "battle": current},
        player_battle_inventory_item=copy.deepcopy,
        combat_experience=lambda battle: {"support_actions": samples},
    ))
    selected = None
    tool_calls = []
    async def get_battle(_context, _raw):
        tool_calls.append("get_battle")
        return json.dumps({"ok": True, "battle": current}, ensure_ascii=False)
    async def get_item(_context, raw):
        tool_calls.append("get_item_details")
        # A missing historical record is not evidence of absent inventory.
        # This also handles error records inside frozen JSON fragments.
        return reader.query(json.loads(raw)).replace("item_not_in_inventory", "details_not_preserved_in_replay")
    async def submit(_context, raw):
        nonlocal selected
        tool_calls.append("submit_battle_action")
        action = json.loads(raw)
        legal = any(a["action_type"] == action.get("action_type") and a.get("object_id", 0) == action.get("object_id")
                    and action.get("target_index") in a["submission_target_indices"] for a in current["available_actions"])
        if legal:
            selected = action
        return json.dumps({"ok": legal, "accepted": legal})
    def tool(name, callback, schema, description):
        return FunctionTool(name=name, description=description, on_invoke_tool=callback,
                            params_json_schema=schema, strict_json_schema=False)
    empty = {"type": "object", "properties": {}, "additionalProperties": False}
    single = {"type": "object", "properties": {"item_id": {"type": "integer", "minimum": 1}},
              "required": ["item_id"], "additionalProperties": False}
    action_schema = {"type": "object", "properties": {
        "action_type": {"type": "string"}, "object_id": {"type": "integer"}, "target_index": {"type": "integer"}},
        "required": ["action_type", "object_id", "target_index"], "additionalProperties": False}
    def finish(_context, _results):
        return ToolsToFinalOutputResult(is_final_output=selected is not None, final_output=json.dumps(selected))
    agent = Agent(name="Team observation replay", model=model,
                  instructions=BATTLE_INSTRUCTIONS if revised else fixture["baseline_instructions"],
                  tools=[tool("get_battle", get_battle, empty, "Refresh only if the current snapshot may be stale."),
                         tool("get_item_details", get_item, ITEM_DETAILS_SCHEMA if revised else single,
                              ITEM_DETAILS_DESCRIPTION if revised else "Read inventory, sanitized effects and legal uses for one item_id. preview_complete=false is incomplete."),
                         tool("submit_battle_action", submit, action_schema, "Submit exactly one legal action from available_actions.")],
                  tool_use_behavior=finish, model_settings=ModelSettings(reasoning={"effort": "high"}, include_usage=True))
    started = time.monotonic()
    result = await Runner.run(agent, messages, max_turns=6)
    usage = result.context_wrapper.usage
    return {"command": command, "variant": "revised" if revised else "baseline", "repetition": repetition,
            "action": selected, "latency_seconds": round(time.monotonic() - started, 2),
            "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "tool_calls": tool_calls, "prompt_chars": sum(len(m["content"]) for m in messages),
            "model_output": [i.raw_item.model_dump() if hasattr(i.raw_item, "model_dump") else i.raw_item for i in result.new_items]}


async def main(args):
    env = read_env_file(ROOT / ".env")
    key = os.environ.get("DEEPSEEK_API_KEY") or env.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is required")
    set_tracing_disabled(True)
    fixture = json.loads((ROOT / "tests/fixtures/fox_team_decisions.json").read_text())
    client = AsyncOpenAI(api_key=key, base_url=os.environ.get("DEEPSEEK_BASE_URL") or env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
                         timeout=180, max_retries=1)
    model = OpenAIResponsesModel(model=args.model, openai_client=client)
    semaphore = asyncio.Semaphore(3)
    results = []
    async def run(command, revised, repetition):
        async with semaphore:
            try:
                row = await evaluate(fixture, command, revised, repetition, model)
            except Exception as exc:
                row = {"command": command, "variant": "revised" if revised else "baseline", "repetition": repetition,
                       "error": type(exc).__name__}
            results.append(row)
            args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2))
            print(json.dumps({k: v for k, v in row.items() if k != "model_output"}, ensure_ascii=False), flush=True)
    try:
        await asyncio.gather(*(run(c, v == "revised", n) for c in args.commands for v in args.variants
                               for n in (args.repetition_ids or range(1, args.repetitions + 1))))
    finally:
        await client.close()
    if any(r.get("error") or not r.get("action") for r in results):
        raise SystemExit("Replay incomplete; no quality pass claimed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="deepseek-v4.1-flash-expires-on-0910")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--commands", type=int, nargs="+", choices=COMMANDS, default=list(COMMANDS))
    parser.add_argument("--variants", nargs="+", choices=("baseline", "revised"), default=["baseline", "revised"])
    parser.add_argument("--repetition-ids", type=int, nargs="+")
    asyncio.run(main(parser.parse_args()))
