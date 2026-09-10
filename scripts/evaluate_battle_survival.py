"""Isolated observation replay. Calls the model, never the game or live sessions.

Run: .venv/bin/python -m scripts.evaluate_battle_survival --output /tmp/battle-evaluation.json
Each variant sees the same recorded observations, without the original reasoning.
This evaluates decisions, not counterfactual battle victories.
"""

import argparse
import asyncio
import copy
import json
from pathlib import Path

from agents import Agent, AsyncOpenAI, FunctionTool, ModelSettings, OpenAIResponsesModel, Runner, set_tracing_disabled
from agents.agent import ToolsToFinalOutputResult

from palpal.agent_runtime import BATTLE_SURVIVAL_INSTRUCTIONS
from palpal.battle_observations import outcome_delta, survival_risk
from palpal.debug_server import ROOT, read_env_file


def instructions(revised):
    # Frozen historical experiment: later production prompt edits must not
    # silently change what the September 6 survival baseline means.
    original = json.loads((ROOT / "tests/fixtures/fox_team_decisions.json").read_text())["baseline_instructions"]
    return original if revised else original.removesuffix(BATTLE_SURVIVAL_INSTRUCTIONS)


def replay_inputs(fixture, command, revised):
    checkpoint = None
    after = 0
    messages = []
    current = None
    for source in fixture["views"]:
        if source["command_generation"] > command:
            break
        view = copy.deepcopy(source)
        summary, checkpoint = survival_risk(view, checkpoint)
        outcomes, delivery = outcome_delta(view.pop("recent_outcomes"), after)
        after = delivery["through_sequence"]
        if outcomes:
            view["new_outcomes"] = outcomes
        if revised:
            view["outcome_delivery"] = delivery
            view["survival_risk"] = summary
        messages.append({"role": "user", "content": json.dumps({"battle": view}, ensure_ascii=False)})
        current = view
    return messages, current


async def evaluate(fixture, command, revised, repetition, model):
    messages, current = replay_inputs(fixture, command, revised)
    selected = None

    async def get_battle(_context, _raw):
        return json.dumps({"ok": True, "battle": current}, ensure_ascii=False)

    async def get_item(_context, raw):
        item_id = int(json.loads(raw)["item_id"])
        record = fixture["item_details"].get(str(item_id))
        if record is None:
            return json.dumps({"ok": False, "error": "details_not_preserved_in_replay"})
        item = copy.deepcopy(record["item"])
        # Adjust from the recorded query, not from today's inventory.
        delta = sum(
            1 if record["queried_at"] <= a["command_generation"] < command else -1
            for a in fixture["submitted_actions"]
            if a["object_id"] == item_id and a["action_type"] in {"use_item", "throw_item"}
            and min(command, record["queried_at"]) <= a["command_generation"] < max(command, record["queried_at"])
        )
        item["amount"] -= delta
        item["available_amount"] = item["amount"]
        legal = [a for a in current["available_actions"] if a.get("object_id") == item_id]
        return json.dumps({"ok": True, "item": item, "legal_battle_actions": legal}, ensure_ascii=False)

    async def submit(_context, raw):
        nonlocal selected
        action = json.loads(raw)
        legal = any(
            a["action_type"] == action["action_type"] and a.get("object_id", 0) == action["object_id"]
            and action["target_index"] in a["submission_target_indices"]
            for a in current["available_actions"]
        )
        if legal:
            selected = action
        return json.dumps({"ok": legal, "accepted": legal})

    def tool(name, callback, properties):
        return FunctionTool(name=name, description=name, on_invoke_tool=callback,
                            params_json_schema={"type": "object", "properties": properties,
                                                "required": list(properties), "additionalProperties": False})

    def finish(_context, _results):
        return ToolsToFinalOutputResult(is_final_output=selected is not None, final_output=json.dumps(selected))

    agent = Agent(name="Battle observation replay", instructions=instructions(revised), model=model,
                  tools=[tool("get_battle", get_battle, {}),
                         tool("get_item_details", get_item, {"item_id": {"type": "integer"}}),
                         tool("submit_battle_action", submit, {
                             "action_type": {"type": "string"}, "object_id": {"type": "integer"},
                             "target_index": {"type": "integer"},
                         })], tool_use_behavior=finish,
                  model_settings=ModelSettings(reasoning={"effort": "high"}))
    result = await Runner.run(agent, messages, max_turns=6)
    items = [item.raw_item.model_dump() if hasattr(item.raw_item, "model_dump") else item.raw_item
             for item in result.new_items]
    return {"command": command, "variant": "revised" if revised else "baseline",
            "repetition": repetition, "action": selected, "model_output": items}


async def main(args):
    import os
    env = read_env_file(ROOT / ".env")
    key = os.environ.get("DEEPSEEK_API_KEY") or env.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is required")
    set_tracing_disabled(True)
    fixture = json.loads((ROOT / "tests/fixtures/fox_battle_decisions.json").read_text())
    client = AsyncOpenAI(api_key=key, base_url=os.environ.get("DEEPSEEK_BASE_URL") or env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com", timeout=180, max_retries=1)
    model = OpenAIResponsesModel(model=args.model, openai_client=client)
    semaphore = asyncio.Semaphore(3)
    results = []

    async def run(command, revised, repetition):
        async with semaphore:
            try:
                row = await evaluate(fixture, command, revised, repetition, model)
            except Exception as exc:
                row = {"command": command, "variant": "revised" if revised else "baseline",
                       "repetition": repetition, "error": type(exc).__name__}
            results.append(row)
            args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2))
            print(json.dumps({k: v for k, v in row.items() if k != "model_output"}, ensure_ascii=False), flush=True)

    try:
        await asyncio.gather(*(run(command, revised, repetition)
                               for command in (25, 29, 36) for revised in (False, True)
                               for repetition in range(1, args.repetitions + 1)))
    finally:
        await client.close()
    if any(row.get("error") or row.get("action") is None for row in results):
        raise SystemExit("Replay incomplete; inspect the output errors. No decision-quality pass is claimed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--repetitions", type=int, default=3)
    asyncio.run(main(parser.parse_args()))
