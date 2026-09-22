import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";

const source = await readFile(new URL("../web/app.js", import.meta.url), "utf8");
const functionSource = source.slice(source.indexOf("async function waitForNavigationProgress("),
  source.indexOf("async function navigationStep("));

async function run(states, overrides = {}) {
  let ticks = 0;
  const urls = [];
  const context = vm.createContext({
    activeNavigation: {}, latestState: {},
    performance: { now: () => ticks },
    sleep: async () => { ticks += 35; },
    partyPositionKey: state => `${state.party_position.x},${state.party_position.y}`,
    routeNodePositionKey: node => node ? `${node.x},${node.y}` : null,
    partyDirectionButton: state => state.party_direction,
    fetch: async url => {
      urls.push(url);
      assert.ok(states.length, "unexpected additional poll");
      return { json: async () => states.shift() };
    },
    ...overrides,
  });
  vm.runInContext(functionSource, context);
  const result = await context.waitForNavigationProgress({
    status: "stepping", expected_node: { x: 32, y: 16 },
    action: { game_instance_id: "first", action_generation_before: 4, action_handshake_supported: true },
  }, "0,0");
  assert.ok(urls.every(url => url === "/navigation/status"));
  return result;
}

function state(overrides = {}) {
  return { game_instance_id: "first", party_position: { x: 32, y: 16 },
    state_info: { available: true, stale: false },
    control: { phase: "ready", navigation_ready: true },
    agent_action: { generation: 5, pending: false }, ...overrides };
}

test("arrival waits for the correct generation to finish", async () => {
  assert.equal(await run([
    state({ agent_action: { generation: 4, pending: false } }),
    state({ agent_action: { generation: 5, pending: true }, control: { phase: "action_pending" } }),
    state(),
  ]), true);
});

test("partial movement can replan after completion", async () => {
  assert.equal(await run([state({ party_position: { x: 16, y: 8 } })]), true);
  assert.equal(await run([state({ party_position: { x: 0, y: 0 } })]), false);
});

test("stale, superseded, different-instance and control-boundary observations stop", async () => {
  for (const change of [
    { state_info: { available: true, stale: true } },
    { game_instance_id: "second" },
    { agent_action: { generation: 6, pending: false } },
    ...["battle", "dialog", "choice", "shop"].map(phase => ({ control: { phase } })),
  ]) assert.equal(await run([state(change)]), false);
});

test("an obsolete browser navigation run cannot consume a response", async () => {
  let context;
  // Replace the run while the request is pending, matching interrupt/reselection.
  const sandbox = { activeNavigation: {}, latestState: {}, performance: { now: () => 0 },
    sleep: async () => {}, routeNodePositionKey: () => "32,16",
    fetch: async () => {
      context.activeNavigation = {};
      return { json: async () => state() };
    },
  };
  context = vm.createContext(sandbox);
  vm.runInContext(functionSource, context);
  assert.equal(await context.waitForNavigationProgress({ expected_node: {} }, "0,0"), false);
});
