#!/usr/bin/env node

import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const chromePath = process.env.PALPAL_CHROME_BIN || defaultChromePath();
const targetUrl = process.env.PALPAL_VISUAL_URL || process.argv[2] || "http://127.0.0.1:8765";
const timeoutMs = Number(process.env.PALPAL_VISUAL_TIMEOUT_MS || 15000);
const roomMemoOnly = process.env.PALPAL_VISUAL_ROOM_MEMO_ONLY === "1";

function defaultChromePath() {
  if (process.platform === "darwin") {
    return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  }
  return "google-chrome";
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitFor(condition, message) {
  const start = Date.now();
  let lastError;
  while (Date.now() - start < timeoutMs) {
    try {
      const value = await condition();
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await sleep(100);
  }
  throw new Error(`${message}${lastError ? `: ${lastError.message}` : ""}`);
}

class CdpClient {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.exceptions = [];
    ws.onmessage = (event) => this.handleMessage(event.data);
  }

  handleMessage(raw) {
    const message = JSON.parse(raw);
    if (message.method === "Runtime.exceptionThrown") {
      this.exceptions.push(message.params?.exceptionDetails?.text || "uncaught exception");
    }
    if (!message.id) return;
    const pending = this.pending.get(message.id);
    if (!pending) return;
    this.pending.delete(message.id);
    if (message.error) {
      pending.reject(new Error(message.error.message || JSON.stringify(message.error)));
    } else {
      pending.resolve(message.result || {});
    }
  }

  send(method, params = {}, sessionId = undefined) {
    const id = this.nextId++;
    const message = { id, method, params };
    if (sessionId) message.sessionId = sessionId;
    this.ws.send(JSON.stringify(message));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
  }
}

async function readDevToolsPort(profileDir) {
  const activePort = path.join(profileDir, "DevToolsActivePort");
  return waitFor(async () => {
    const raw = await readFile(activePort, "utf8");
    const [port, browserPath] = raw.trim().split("\n");
    if (!port || !browserPath) return null;
    return { port, browserPath };
  }, "Timed out waiting for Chrome DevToolsActivePort");
}

async function launchChrome() {
  const profileDir = await mkdtemp(path.join(tmpdir(), "palpal-chrome-"));
  const chrome = spawn(
    chromePath,
    [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      "--remote-debugging-address=127.0.0.1",
      "--remote-debugging-port=0",
      `--user-data-dir=${profileDir}`,
      "about:blank",
    ],
    { stdio: ["ignore", "pipe", "pipe"] }
  );

  chrome.stdout.on("data", () => {});
  chrome.stderr.on("data", () => {});

  const { port, browserPath } = await readDevToolsPort(profileDir);
  return {
    chrome,
    profileDir,
    wsUrl: `ws://127.0.0.1:${port}${browserPath}`,
  };
}

async function connectWebSocket(url) {
  const ws = new WebSocket(url);
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = () => reject(new Error(`Failed to connect to ${url}`));
  });
  return ws;
}

async function main() {
  const browser = await launchChrome();
  try {
    const ws = await connectWebSocket(browser.wsUrl);
    const cdp = new CdpClient(ws);
    const { targetId } = await cdp.send("Target.createTarget", { url: targetUrl });
    const { sessionId } = await cdp.send("Target.attachToTarget", {
      targetId,
      flatten: true,
    });
    await cdp.send("Page.enable", {}, sessionId);
    await cdp.send("Runtime.enable", {}, sessionId);

    await waitFor(async () => {
      const result = await cdp.send(
        "Runtime.evaluate",
        {
          expression: "document.readyState",
          returnByValue: true,
        },
        sessionId
      );
      return result.result?.value === "complete";
    }, "Timed out waiting for page load");

    const manualInput = await cdp.send(
      "Runtime.evaluate",
      {
        awaitPromise: true,
        returnByValue: true,
        expression: `(async () => {
          const originalFetch = window.fetch;
          const originalState = latestState;
          const originalCache = new Map(navigationCache);
          const actions = [];
          let navigationCalls = 0;
          try {
            latestState = JSON.parse(JSON.stringify(originalState || {
              party_position: { x: 0, y: 0 },
              dialog: { active: false },
              objects: [],
            }));
            latestState._debug = { ...(latestState._debug || {}), runtime_stale: false };
            latestState.control = {
              ...(latestState.control || {}),
              allowed_actions: ["press_buttons"],
            };
            latestState._navigation = { status: "pending", targets: [] };
            navigationCache.clear();
            navigationLatestEnvironmentKey = navigationEnvironmentKey(latestState);
            navigationEnvironmentChangedAt = performance.now();
            navigationStableEnvironmentKey = null;
            resetNavigationRefreshRetry();
            updateExternalControls(latestState);
            window.fetch = async (url, options) => {
              if (url === "/action") {
                actions.push(JSON.parse(options.body).buttons);
                return { ok: true, json: async () => ({ ok: true }) };
              }
              if (url === "/navigation") {
                navigationCalls += 1;
                return {
                  ok: true,
                  json: async () => ({
                    status: "ok",
                    targets: [],
                    environment_key: navigationEnvironmentKey(latestState),
                  }),
                };
              }
              return originalFetch(url, options);
            };
            document.querySelector('[data-button="left"]')?.click();
            window.dispatchEvent(new KeyboardEvent("keydown", {
              key: "ArrowRight",
              bubbles: true,
            }));
            window.dispatchEvent(new KeyboardEvent("keydown", {
              key: "ArrowRight",
              repeat: true,
              bubbles: true,
            }));
            await new Promise((resolve) => setTimeout(resolve, 50));
            return {
              actions,
              navigationCalls,
              settleRemaining: navigationSettleMs - (performance.now() - navigationEnvironmentChangedAt),
            };
          } finally {
            if (navigationRefreshTimer !== null) clearTimeout(navigationRefreshTimer);
            navigationRefreshTimer = null;
            navigationCache.clear();
            originalCache.forEach((value, key) => navigationCache.set(key, value));
            window.fetch = originalFetch;
            latestState = originalState;
          }
        })()`,
      },
      sessionId
    );
    const manualInputActions = manualInput.result?.value;
    if (
      JSON.stringify(manualInputActions?.actions)
      !== JSON.stringify([["left"], ["right"], ["right"]])
      || manualInputActions.navigationCalls !== 0
      || manualInputActions.settleRemaining <= 250
    ) {
      throw new Error(`manual input smoke failed (${JSON.stringify({
        value: manualInputActions,
        exception: manualInput.exceptionDetails,
      })})`);
    }

    const controlTakeover = await cdp.send(
      "Runtime.evaluate",
      {
        returnByValue: true,
        expression: `(() => {
          const originalState = latestState;
          const originalSession = agentSession;
          const originalExhausted = new Set(exhaustedTransports);
          const left = document.querySelector('[data-button="left"]');
          const originalLeftDisabled = left?.disabled;
          const originalLeftMarker = left?.dataset.controlDisabled;
          const semanticButton = document.createElement("button");
          semanticButton.dataset.useItem = "999";
          semanticButton.disabled = true;
          document.body.appendChild(semanticButton);
          try {
            const state = JSON.parse(JSON.stringify(originalState || {}));
            state.party_position = state.party_position || { x: 0, y: 0 };
            state.objects = [];
            state.inventory = [];
            state.shop = null;
            state.dialog = { active: false };
            state.in_battle = false;
            state.control = {
              owner: "client",
              phase: "ready",
              navigation_ready: true,
              allowed_actions: [
                "press_buttons", "navigation_step", "use_item",
                "shop_buy", "shop_leave", "advance_dialog",
              ],
            };
            state._debug = {
              ...(state._debug || {}),
              runtime_stale: false,
            };
            state._navigation = {
              status: "ok",
              targets: [{ id: 999, kind: "touch", status: "reachable", steps: 1 }],
            };
            state._navigation.environment_key = navigationEnvironmentKey(state);
            latestState = state;
            navigationRenderKey = null;
            const session = {
              ...(originalSession || {}),
              session_id: originalSession?.session_id || "control-smoke",
              status: "paused",
            };
            agentSession = session;
            left.disabled = false;
            delete left.dataset.controlDisabled;
            renderNavigation(state);
            updateExternalControls(state);

            setAgentSession({ ...session, status: "running" });
            updateExternalControls(state);
            const locked = left.disabled && left.dataset.controlDisabled === "control";
            updateExternalControls(state);
            const markerPreserved = left.dataset.controlDisabled === "control";

            state._navigation.targets[0].steps = 2;
            renderNavigation(state);
            updateExternalControls(state);
            updateExternalControls(state);
            const navigationLocked = document.querySelector('[data-nav-go="999"]')?.disabled === true;

            setAgentSession({ ...session, status: "interrupting" });
            updateExternalControls(state);
            const interruptingStayedLocked = left.disabled === true
              && left.dataset.controlDisabled === "control";
            setAgentSession({ ...session, status: "paused", stop_reason: "execution_failed" });
            const navigationButton = document.querySelector('[data-nav-go="999"]');
            const manualRestored = left.disabled === false && !left.dataset.controlDisabled;
            const navigationRestored = navigationButton?.disabled === false;
            const semanticPreserved = semanticButton.disabled === true && !semanticButton.dataset.controlDisabled;

            exhaustedTransports.add("state");
            updateExternalControls(state);
            setAgentSession({ ...session, status: "running" });
            setAgentSession({ ...session, status: "paused" });
            const disconnectStayedLocked = left.disabled === true;

            return {
              locked,
              markerPreserved,
              navigationLocked,
              interruptingStayedLocked,
              manualRestored,
              navigationRestored,
              semanticPreserved,
              disconnectStayedLocked,
            };
          } finally {
            semanticButton.remove();
            exhaustedTransports.clear();
            originalExhausted.forEach((name) => exhaustedTransports.add(name));
            agentSession = originalSession;
            latestState = originalState;
            navigationRenderKey = null;
            if (originalState) render(originalState);
            if (left) {
              left.disabled = originalLeftDisabled;
              if (originalLeftMarker === undefined) delete left.dataset.controlDisabled;
              else left.dataset.controlDisabled = originalLeftMarker;
            }
          }
        })()`,
      },
      sessionId
    );
    const controlTakeoverState = controlTakeover.result?.value;
    if (
      !controlTakeoverState
      || Object.values(controlTakeoverState).some((value) => value !== true)
    ) {
      throw new Error(`manual takeover smoke failed (${JSON.stringify({
        value: controlTakeoverState,
        exception: controlTakeover.exceptionDetails,
      })})`);
    }

    const tabs = await cdp.send(
      "Runtime.evaluate",
      {
        returnByValue: true,
        expression: `(() => {
          const normal = document.getElementById("normalTab");
          const agent = document.getElementById("agentTab");
          const normalPanel = document.getElementById("normalPanel");
          const agentPanel = document.getElementById("agentPanel");
          const initial = normal?.getAttribute("aria-selected") === "true"
            && agent?.getAttribute("aria-selected") === "false"
            && !normalPanel?.hidden && agentPanel?.hidden;
          agent?.click();
          const switched = normal?.getAttribute("aria-selected") === "false"
            && agent?.getAttribute("aria-selected") === "true"
            && normalPanel?.hidden && !agentPanel?.hidden
            && localStorage.getItem("palpal.stateTab") === "agent";
          return { initial, switched, hasTimeline: Boolean(document.getElementById("agentTimeline")) };
        })()`,
      },
      sessionId
    );
    const tabState = tabs.result?.value;
    if (!tabState?.initial || !tabState?.switched || !tabState?.hasTimeline) {
      throw new Error(`agent tab accessibility smoke failed (${JSON.stringify(tabState)})`);
    }

    await cdp.send("Page.reload", {}, sessionId);
    await waitFor(async () => {
      const result = await cdp.send(
        "Runtime.evaluate",
        {
          returnByValue: true,
          expression: `document.readyState === "complete"
            && document.getElementById("agentTab")?.getAttribute("aria-selected") === "true"
            && !document.getElementById("agentPanel")?.hidden`,
        },
        sessionId
      );
      return result.result?.value === true;
    }, "Timed out waiting for Agent tab localStorage restore");

    const modelControl = await cdp.send(
      "Runtime.evaluate",
      {
        returnByValue: true,
        expression: `(() => {
          const select = document.querySelector("[data-session-model]");
          if (!select || select.options.length < 2) return { ready: false };
          const next = [...select.options].find((option) => option.value !== select.value)?.value;
          select.value = next;
          select.dispatchEvent(new Event("change", { bubbles: true }));
          select.focus();
          renderAgentSession();
          const current = document.querySelector("[data-session-model]");
          return {
            ready: true,
            sameNode: current === select,
            retainedValue: current?.value === next,
            retainedFocus: document.activeElement === select,
          };
        })()`,
      },
      sessionId
    );
    const modelControlState = modelControl.result?.value;
    if (
      !modelControlState?.ready
      || !modelControlState.sameNode
      || !modelControlState.retainedValue
      || !modelControlState.retainedFocus
    ) {
      throw new Error(`agent model control stability smoke failed (${JSON.stringify(modelControlState)})`);
    }

    const goalsModal = await cdp.send(
      "Runtime.evaluate",
      {
        awaitPromise: true,
        returnByValue: true,
        expression: `(async () => {
          const originalFetch = window.fetch;
          const originalSession = agentSession;
          try {
            window.fetch = async (url, options) => {
              if (url === "/agent/tasks") {
                return {
                  ok: true,
                  json: async () => ({
                    ok: true,
                    counts: { active: 2, completed: 1, abandoned: 1, total: 4 },
                    tasks: [
                      { id: "active-1", description: "Reach the courtyard", priority: 8, status: "active" },
                      { id: "active-2", description: "Inspect the cabinet", priority: 2, status: "active" },
                      { id: "completed-1", description: "Speak to the innkeeper", priority: 5, status: "completed" },
                      { id: "abandoned-1", description: "Retry the inert door", priority: 1, status: "abandoned" },
                    ],
                  }),
                };
              }
              return originalFetch(url, options);
            };
            agentSession = { ...agentSession, status: "running", phase: "deciding" };
            agentSessionControlsRenderKey = null;
            renderAgentSession();
            const buttons = [...document.querySelectorAll(".agentSessionButtons button")];
            const decide = buttons.find((button) => button.textContent.includes("Decide Once"));
            const reset = buttons.find((button) => button.textContent.includes("Reset Session"));
            const goals = buttons.find((button) => button.textContent.includes("Goals"));
            const goalsRect = goals?.getBoundingClientRect();
            const resetRect = reset?.getBoundingClientRect();
            const decideRect = decide?.getBoundingClientRect();
            const sixthButton = buttons.indexOf(goals) === 5;
            const expectedPosition = goalsRect && resetRect && decideRect
              && goalsRect.left > resetRect.left && goalsRect.top > decideRect.top;
            const enabledWhileRunning = goals?.disabled === false;
            const permanentGoalRemoved = !document.getElementById("agentSessionHeader")?.textContent.includes("Permanent goal")
              && !document.getElementById("agentSessionHeader")?.textContent.includes("通关游戏");
            goals?.focus();
            goals?.click();
            await new Promise((resolve) => setTimeout(resolve, 0));
            const dialog = document.getElementById("goalsDialog");
            const opened = dialog.open;
            const groupHeadings = [...dialog.querySelectorAll(".goalsGroupHeader h3")].map((node) => node.textContent);
            const cardStatuses = [...dialog.querySelectorAll(".goalStatus")].map((node) => node.textContent);
            const summary = document.getElementById("goalsSummary")?.textContent;
            const countLabel = goals?.textContent;
            const ignoresGameKeys = shouldIgnoreGameKeydown(new KeyboardEvent("keydown", { key: "ArrowUp" }));
            dialog.click();
            await new Promise((resolve) => setTimeout(resolve, 0));
            const closedFromBackdrop = !dialog.open;
            const restoredFocus = document.activeElement === goals;
            return {
              sixthButton,
              expectedPosition,
              enabledWhileRunning,
              permanentGoalRemoved,
              opened,
              closedFromBackdrop,
              groupHeadings,
              cardStatuses,
              summary,
              countLabel,
              ignoresGameKeys,
              restoredFocus,
            };
          } finally {
            window.fetch = originalFetch;
            agentSession = originalSession;
            agentSessionControlsRenderKey = null;
            renderAgentSession();
          }
        })()`,
      },
      sessionId
    );
    const goalsModalResult = goalsModal.result?.value;
    if (
      !goalsModalResult?.sixthButton
      || !goalsModalResult.expectedPosition
      || !goalsModalResult.enabledWhileRunning
      || !goalsModalResult.permanentGoalRemoved
      || !goalsModalResult.opened
      || !goalsModalResult.closedFromBackdrop
      || JSON.stringify(goalsModalResult.groupHeadings) !== JSON.stringify(["Active", "Dormant", "Completed", "Abandoned"])
      || JSON.stringify(goalsModalResult.cardStatuses) !== JSON.stringify(["Active", "Active", "Completed", "Abandoned"])
      || goalsModalResult.summary !== "2 active · 0 dormant · 1 completed · 1 abandoned"
      || !goalsModalResult.countLabel?.includes("Goals (2)")
      || !goalsModalResult.ignoresGameKeys
      || !goalsModalResult.restoredFocus
    ) {
      throw new Error(`agent goals modal smoke failed (${JSON.stringify(goalsModalResult)})`);
    }

    const roomMemoModal = await cdp.send(
      "Runtime.evaluate",
      {
        awaitPromise: true,
        returnByValue: true,
        expression: `(async () => {
          const originalFetch = window.fetch;
          try {
            window.fetch = async (url, options) => {
              if (url === "/agent/room-memo") {
                return {
                  ok: true,
                  json: async () => ({
                    ok: true,
                    room_id: "S3/M10:CURRENT000001",
                    memo: { text: "Current inn room.", source: "agent_annotation", updated_agent_turn: 9 },
                    recent_memos: Array.from({ length: 5 }, (_, index) => ({
                      room_id: "S1/M1:RECENT" + String(index + 1).padStart(6, "0"),
                      text: "Recent room memo " + (index + 1),
                      source: "agent_annotation",
                      updated_agent_turn: 8 - index,
                    })),
                  }),
                };
              }
              return originalFetch(url, options);
            };
            const button = [...document.querySelectorAll(".agentSessionButtons button")]
              .find((item) => item.textContent.includes("Room Memo"));
            button?.focus();
            button?.click();
            await new Promise((resolve) => setTimeout(resolve, 0));
            const dialog = document.getElementById("roomMemoDialog");
            const opened = dialog.open;
            const headings = [...dialog.querySelectorAll(".roomMemoSection h3")].map((node) => node.textContent);
            const cards = [...dialog.querySelectorAll(".roomMemoCard")];
            const recentRooms = [...dialog.querySelectorAll(".roomMemoRecentList .roomMemoCardRoom")]
              .map((node) => node.textContent);
            const currentText = cards[0]?.querySelector("p")?.textContent;
            const recentCount = dialog.querySelector(".roomMemoSectionHeader span")?.textContent;
            const ignoresGameKeys = shouldIgnoreGameKeydown(new KeyboardEvent("keydown", { key: "ArrowUp" }));
            dialog.click();
            await new Promise((resolve) => setTimeout(resolve, 0));
            return {
              opened,
              closedFromBackdrop: dialog.open === false,
              headings,
              cardCount: cards.length,
              recentRooms,
              currentText,
              recentCount,
              ignoresGameKeys,
              restoredFocus: document.activeElement === button,
            };
          } finally {
            window.fetch = originalFetch;
          }
        })()`,
      },
      sessionId
    );
    const roomMemoModalResult = roomMemoModal.result?.value;
    if (
      !roomMemoModalResult?.opened
      || !roomMemoModalResult.closedFromBackdrop
      || JSON.stringify(roomMemoModalResult.headings) !== JSON.stringify(["Current room", "Recently added"])
      || roomMemoModalResult.cardCount !== 6
      || roomMemoModalResult.recentRooms.length !== 5
      || roomMemoModalResult.recentRooms.includes("S3/M10:CURRENT000001")
      || roomMemoModalResult.currentText !== "Current inn room."
      || roomMemoModalResult.recentCount !== "5 / 5"
      || !roomMemoModalResult.ignoresGameKeys
      || !roomMemoModalResult.restoredFocus
    ) {
      throw new Error(`room memo modal smoke failed (${JSON.stringify(roomMemoModalResult)})`);
    }
    const battleMemoModal = await cdp.send(
      "Runtime.evaluate",
      {
        awaitPromise: true,
        returnByValue: true,
        expression: `(async () => {
          const originalFetch = window.fetch;
          const originalState = latestState;
          try {
            window.fetch = async (url, options) => {
              if (url === "/agent/battle-memo") {
                return {
                  ok: true,
                  json: async () => ({
                    ok: true,
                    status: "in_battle",
                    battle_generation: 7,
                    is_boss: true,
                    current_enemies: [{ slot_index: 0, object_id: 81, name: "灯笼" }],
                    recent_outcomes: [{
                      actor: { name: "李逍遥" }, actor_side: "player",
                      action_type: "magic", name: "炎咒", mp_cost: 8,
                      enemy_changes: [{ object_id: 81, name: "灯笼", amount: 23, kind: "damage" }],
                      player_changes: [], no_numeric_change_observed: false,
                    }],
                    enemy_memories: [{
                      object_id: 81, name: "灯笼", current_count: 1,
                      hp_estimate: { status: "estimated", lower_exclusive: 100, upper_inclusive: 123, sample_count: 1 },
                      player_actions: [{
                        actor_role_id: 0, actor_name: "李逍遥", action_type: "magic",
                        object_id: 327, name: "炎咒", enemy_object_id: 81,
                        samples: [{
                          mp_cost: 8,
                          enemy_changes: [{ object_id: 81, name: "灯笼", amount: 23, kind: "damage" }],
                          player_changes: [],
                        }],
                      }],
                      enemy_effects: [],
                    }],
                    support_experience: [],
                  }),
                };
              }
              return originalFetch(url, options);
            };
            render({
              ...latestState,
              in_battle: true,
              battle: {
                generation: 7, phase: "command", players: [], enemies: [],
                available_actions: [], visible_outcomes: [],
              },
            });
            const button = [...document.querySelectorAll(".agentSessionButtons button")]
              .find((item) => item.textContent.includes("Battle Memo"));
            button?.focus();
            button?.click();
            await new Promise((resolve) => setTimeout(resolve, 0));
            const dialog = document.getElementById("roomMemoDialog");
            const content = document.getElementById("roomMemoContent")?.textContent || "";
            const result = {
              buttonLabel: button?.textContent,
              opened: dialog.open,
              title: document.getElementById("roomMemoTitle")?.textContent,
              closeLabel: document.getElementById("roomMemoClose")?.getAttribute("aria-label"),
              enemySummary: document.getElementById("roomMemoRoom")?.textContent,
              hasAction: content.includes("炎咒") && content.includes("23"),
              hasHpEstimate: content.includes("100 < HP ≤ 123"),
              hasSupport: content.includes("Support actions"),
              excludesEncounterHistory: !content.includes("Encounter history"),
              excludesOtherLineups: !content.includes("Recent room memo"),
            };
            dialog.click();
            await new Promise((resolve) => setTimeout(resolve, 0));
            result.closedFromBackdrop = !dialog.open;
            result.restoredFocus = document.activeElement === button;
            return result;
          } finally {
            window.fetch = originalFetch;
            if (originalState) render(originalState);
          }
        })()`,
      },
      sessionId
    );
    const battleMemoModalResult = battleMemoModal.result?.value;
    if (
      !battleMemoModalResult?.buttonLabel?.includes("Battle Memo")
      || !battleMemoModalResult.opened
      || battleMemoModalResult.title !== "Battle Memo"
      || battleMemoModalResult.closeLabel !== "Close battle memo"
      || !battleMemoModalResult.enemySummary?.includes("Boss · 1 current enemy")
      || !battleMemoModalResult.hasAction
      || !battleMemoModalResult.hasHpEstimate
      || !battleMemoModalResult.hasSupport
      || !battleMemoModalResult.excludesEncounterHistory
      || !battleMemoModalResult.excludesOtherLineups
      || !battleMemoModalResult.closedFromBackdrop
      || !battleMemoModalResult.restoredFocus
    ) {
      throw new Error(`battle memo modal smoke failed (${JSON.stringify(battleMemoModalResult)})`);
    }
    if (roomMemoOnly) {
      if (cdp.exceptions.length) {
        throw new Error(`console exceptions: ${cdp.exceptions.join("; ")}`);
      }
      console.log("memo visual smoke ok: room history and per-enemy battle experience");
      ws.close();
      return;
    }

    const timelineState = await cdp.send(
      "Runtime.evaluate",
      {
        returnByValue: true,
        expression: `(() => {
          agentSessionEvents = Array.from({ length: 24 }, (_, index) => ({
            seq: index + 1,
            turn: Math.floor(index / 4) + 1,
            type: "tool",
            phase: "completed",
            summary: "Synthetic event " + (index + 1),
            details: { index, payload: "x".repeat(80) },
          }));
          agentTimelineRenderKey = null;
          renderAgentTimeline();
          const timeline = document.getElementById("agentTimeline");
          timeline.scrollTop = 80;
          const details = timeline.querySelector('[data-event-seq="2"] details');
          details.open = true;
          const originalArticle = details.closest(".agentActivity");
          renderAgentTimeline();
          const unchangedArticle = timeline.querySelector('[data-event-seq="2"]');
          const unchanged = unchangedArticle === originalArticle
            && unchangedArticle.querySelector("details").open
            && timeline.scrollTop === 80;
          agentSessionEvents.push({
            seq: 25,
            turn: 7,
            type: "pause",
            phase: "paused",
            summary: "Synthetic appended event",
            details: { reason: "smoke" },
          });
          renderAgentTimeline();
          const updatedArticle = timeline.querySelector('[data-event-seq="2"]');
          const originalSession = agentSession;
          agentSession = { ...agentSession, status: "paused" };
          agentSessionEvents.push({
            seq: 26,
            turn: 8,
            type: "model",
            phase: "request",
            summary: "Historical request",
            details: {},
          });
          renderAgentTimeline();
          const pausedCurrent = timeline.querySelectorAll(".agentActivity.is-current").length;
          const pausedAnimated = [...timeline.querySelectorAll(".agentActivityIcon")]
            .filter((icon) => getComputedStyle(icon).animationName === "agentPulse").length;
          agentSession = { ...agentSession, status: "running" };
          renderAgentTimeline();
          const runningCurrent = timeline.querySelectorAll(".agentActivity.is-current").length;
          const runningAnimated = [...timeline.querySelectorAll(".agentActivityIcon")]
            .filter((icon) => getComputedStyle(icon).animationName === "agentPulse").length;
          agentSession = originalSession;
          agentSessionEvents.pop();
          renderAgentTimeline();
          return {
            unchanged,
            openAfterAppend: updatedArticle?.querySelector("details")?.open === true,
            scrollAfterAppend: timeline.scrollTop,
            appendRendered: Boolean(timeline.querySelector('[data-event-seq="25"]')),
            pausedCurrent,
            pausedAnimated,
            runningCurrent,
            runningAnimated,
          };
        })()`,
      },
      sessionId
    );
    const timelineResult = timelineState.result?.value;
    if (
      !timelineResult?.unchanged
      || !timelineResult.openAfterAppend
      || timelineResult.scrollAfterAppend !== 80
      || !timelineResult.appendRendered
      || timelineResult.pausedCurrent !== 0
      || timelineResult.pausedAnimated !== 0
      || timelineResult.runningCurrent !== 1
      || timelineResult.runningAnimated !== 1
    ) {
      throw new Error(`agent timeline stability smoke failed (${JSON.stringify(timelineResult)})`);
    }

    const timelineNavigationSteps = await cdp.send(
      "Runtime.evaluate",
      {
        returnByValue: true,
        expression: `(() => {
          const stepping = (seq, step, turn = 1) => ({
            seq,
            turn,
            type: "navigation",
            phase: "progress",
            summary: "Navigation step " + step + ": stepping.",
            details: { step, result: { status: "stepping", move: "right" } },
          });
          agentSessionEvents = [stepping(1, 1)];
          agentTimelineRenderKey = null;
          renderAgentTimeline();
          const timeline = document.getElementById("agentTimeline");
          const initialSeqs = [...timeline.querySelectorAll(".agentActivity[data-event-seq]")]
            .map((node) => Number(node.dataset.eventSeq));
          agentSessionEvents.push(stepping(2, 2));
          renderAgentTimeline();
          const liveSeqs = [...timeline.querySelectorAll(".agentActivity[data-event-seq]")]
            .map((node) => Number(node.dataset.eventSeq));
          agentSessionEvents.push(
            {
              seq: 3,
              turn: 1,
              type: "tool",
              phase: "completed",
              summary: "Navigation tool checkpoint.",
              details: {},
            },
            stepping(4, 3),
            stepping(5, 4),
            {
              seq: 6,
              turn: 1,
              type: "navigation",
              phase: "progress",
              summary: "Navigation step 5: facing.",
              details: { step: 5, result: { status: "facing" } },
            },
            stepping(7, 1, 2),
            stepping(8, 2, 2),
          );
          renderAgentTimeline();
          const visibleActivities = [...timeline.querySelectorAll(".agentActivity[data-event-seq]")];
          return {
            initialSeqs,
            liveSeqs,
            visibleSeqs: visibleActivities.map((node) => Number(node.dataset.eventSeq)),
            visibleSummaries: visibleActivities.map((node) => node.querySelector("strong")?.textContent),
            originalSeqs: agentSessionEvents.map((event) => event.seq),
            latestStep: JSON.parse(timeline.querySelector('[data-event-seq="5"] details pre')?.textContent || "null")?.step,
          };
        })()`,
      },
      sessionId
    );
    const timelineNavigationResult = timelineNavigationSteps.result?.value;
    if (
      JSON.stringify(timelineNavigationResult?.initialSeqs) !== JSON.stringify([1])
      || JSON.stringify(timelineNavigationResult.liveSeqs) !== JSON.stringify([2])
      || JSON.stringify(timelineNavigationResult.visibleSeqs) !== JSON.stringify([2, 3, 5, 6, 8])
      || JSON.stringify(timelineNavigationResult.visibleSummaries) !== JSON.stringify([
        "Navigation step 2: stepping.",
        "Navigation tool checkpoint.",
        "Navigation step 4: stepping.",
        "Navigation step 5: facing.",
        "Navigation step 2: stepping.",
      ])
      || JSON.stringify(timelineNavigationResult.originalSeqs) !== JSON.stringify([1, 2, 3, 4, 5, 6, 7, 8])
      || timelineNavigationResult.latestStep !== 4
    ) {
      throw new Error(`agent timeline navigation-step compaction smoke failed (${JSON.stringify(timelineNavigationResult)})`);
    }

    const timelinePagingState = await cdp.send(
      "Runtime.evaluate",
      {
        returnByValue: true,
        expression: `(() => {
          agentSessionEvents = Array.from({ length: 24 }, (_, index) => ({
            seq: index + 1,
            turn: Math.floor(index / 4) + 1,
            type: "tool",
            phase: "completed",
            summary: "Paged event " + (index + 1),
            details: {},
          }));
          agentHasOlderEvents = false;
          agentHistoryExpanded = false;
          trimAgentTimelineToLatestTurns();
          agentTimelineRenderKey = null;
          renderAgentTimeline();
          const timeline = document.getElementById("agentTimeline");
          const trimmedHeadings = [...timeline.querySelectorAll(".agentTurn h3")].map((node) => node.textContent);
          const hasLoadButton = Boolean(timeline.querySelector("[data-agent-load-earlier]"));
          const anchorScrollHeight = timeline.scrollHeight;
          agentSessionEvents = mergeAgentSessionEvents(Array.from({ length: 4 }, (_, index) => ({
            seq: index + 1,
            turn: 1,
            type: "tool",
            phase: "completed",
            summary: "Earlier event " + (index + 1),
            details: {},
          })));
          agentHasOlderEvents = false;
          agentHistoryExpanded = true;
          agentTimelineRenderKey = null;
          renderAgentTimeline({ anchorScrollHeight, anchorScrollTop: 0 });
          const loadedHeadings = [...timeline.querySelectorAll(".agentTurn h3")].map((node) => node.textContent);
          return {
            trimmedHeadings,
            hasLoadButton,
            loadedHeadings,
            scrollAnchored: Math.abs(timeline.scrollTop - (timeline.scrollHeight - anchorScrollHeight)) <= 1,
          };
        })()`,
      },
      sessionId
    );
    const timelinePagingResult = timelinePagingState.result?.value;
    if (
      JSON.stringify(timelinePagingResult?.trimmedHeadings) !== JSON.stringify(["Turn 2", "Turn 3", "Turn 4", "Turn 5", "Turn 6"])
      || !timelinePagingResult.hasLoadButton
      || JSON.stringify(timelinePagingResult.loadedHeadings) !== JSON.stringify(["Turn 1", "Turn 2", "Turn 3", "Turn 4", "Turn 5", "Turn 6"])
      || !timelinePagingResult.scrollAnchored
    ) {
      throw new Error(`agent timeline paging smoke failed (${JSON.stringify(timelinePagingResult)})`);
    }

    const normalAgentResult = await waitFor(async () => {
      const result = await cdp.send(
        "Runtime.evaluate",
        {
          returnByValue: true,
          expression: `(() => {
            document.getElementById("normalTab")?.click();
            const select = document.querySelector("[data-agent-model]");
            if (!document.getElementById("agent")) return { ready: true, retained: true, legacyRemoved: true };
            if (!select) return { ready: false };
            select.focus();
            agentError = "synthetic deferred render";
            renderAgent(latestState);
            const retained = document.querySelector("[data-agent-model]") === select
              && document.activeElement === select
              && agentRenderDeferred;
            select.blur();
            agentError = null;
            agentRenderDeferred = false;
            agentRenderKey = null;
            renderAgent(latestState);
            return { ready: true, retained };
          })()`,
        },
        sessionId
      );
      if (result.exceptionDetails) {
        throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text || "normal Agent controls evaluation failed");
      }
      const value = result.result?.value;
      return value?.ready ? value : null;
    }, "Timed out waiting for normal Agent controls");
    if (!normalAgentResult?.ready || !normalAgentResult.retained) {
      throw new Error(`normal agent model stability smoke failed (${JSON.stringify(normalAgentResult)})`);
    }

    const checkpointModal = await waitFor(async () => {
      const result = await cdp.send(
        "Runtime.evaluate",
        {
          returnByValue: true,
          expression: `(() => {
            const normal = document.getElementById("normalTab");
            checkpoints = Array.from({ length: 12 }, (_, index) => ({
              id: "smoke-" + index,
              label: "Smoke checkpoint " + index,
              created_at: "2026-01-01T00:00:00Z",
              state: { scene_id: 1, map_id: 12, room_key: "smoke", frame: index },
              files: {},
            }));
            checkpointsLoaded = true;
            checkpointRenderKey = null;
            renderCheckpoints(latestState);
            let menu = document.querySelector(".checkpointMenu");
            let summary = menu?.querySelector(":scope > summary");
            if (!normal || !menu || !summary) return null;
            normal.click();
            summary.click();
            let panel = menu.querySelector(".checkpointMenuPanel");
            let backdrop = menu.querySelector(".checkpointBackdrop");
            if (!menu.open || !panel || !backdrop) return { ready: false };
            const rect = panel.getBoundingClientRect();
            const viewportWidth = document.documentElement.clientWidth;
            const viewportHeight = document.documentElement.clientHeight;
            const centered = Math.abs(rect.left + rect.width / 2 - viewportWidth / 2) < 2
              && Math.abs(rect.top + rect.height / 2 - viewportHeight / 2) < 2;
            const dialogSemantics = panel.getAttribute("role") === "dialog"
              && panel.getAttribute("aria-modal") === "true";
            const list = menu.querySelector(".checkpointList");
            list.scrollTop = 60;
            checkpointStatus = "Synthetic refresh";
            renderCheckpoints(latestState);
            menu = document.querySelector(".checkpointMenu");
            panel = menu.querySelector(".checkpointMenuPanel");
            backdrop = menu.querySelector(".checkpointBackdrop");
            summary = menu.querySelector(":scope > summary");
            const listScrollPreserved = menu.querySelector(".checkpointList")?.scrollTop === 60;
            panel.click();
            const insideStayedOpen = menu.open;
            backdrop.click();
            const outsideClosed = !menu.open;
            summary.click();
            window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
            const escapeClosed = !menu.open;
            return {
              ready: true,
              centered,
              dialogSemantics,
              listScrollPreserved,
              insideStayedOpen,
              outsideClosed,
              escapeClosed,
              geometry: { left: rect.left, top: rect.top, width: rect.width, height: rect.height, viewportWidth, viewportHeight },
            };
          })()`,
        },
        sessionId
      );
      return result.result?.value;
    }, "Timed out waiting for checkpoint modal");
    if (
      !checkpointModal?.ready
      || !checkpointModal.centered
      || !checkpointModal.dialogSemantics
      || !checkpointModal.listScrollPreserved
      || !checkpointModal.insideStayedOpen
      || !checkpointModal.outsideClosed
      || !checkpointModal.escapeClosed
    ) {
      throw new Error(`checkpoint modal smoke failed (${JSON.stringify(checkpointModal)})`);
    }

    const checkpointSessionSync = await cdp.send(
      "Runtime.evaluate",
      {
        awaitPromise: true,
        returnByValue: true,
        expression: `(async () => {
          const calls = [];
          const originalFetch = window.fetch;
          const originalConfirm = window.confirm;
          const originalRefreshCheckpoints = refreshCheckpoints;
          const originalRefreshState = refreshState;
          const originalRefreshAgentSession = refreshAgentSession;
          const originalRefreshAgentTasks = refreshAgentTasks;
          try {
            window.confirm = () => true;
            window.fetch = async (url) => {
              calls.push("fetch:" + url);
              return { ok: true, json: async () => ({ ok: true }) };
            };
            refreshCheckpoints = async () => calls.push("refreshCheckpoints");
            refreshState = async () => calls.push("refreshState");
            refreshAgentSession = async () => calls.push("refreshAgentSession");
            refreshAgentTasks = async () => calls.push("refreshAgentTasks");
            checkpointBusy = null;
            await loadCheckpoint("session-sync-smoke");
            return { calls, checkpointStatus, checkpointError };
          } finally {
            window.fetch = originalFetch;
            window.confirm = originalConfirm;
            refreshCheckpoints = originalRefreshCheckpoints;
            refreshState = originalRefreshState;
            refreshAgentSession = originalRefreshAgentSession;
            refreshAgentTasks = originalRefreshAgentTasks;
            checkpointBusy = null;
            checkpointError = null;
          }
        })()`,
      },
      sessionId
    );
    const checkpointSessionSyncResult = checkpointSessionSync.result?.value;
    if (
      JSON.stringify(checkpointSessionSyncResult?.calls) !== JSON.stringify([
        "fetch:/checkpoints/load",
        "refreshCheckpoints",
        "refreshState",
        "refreshAgentSession",
        "refreshAgentTasks",
      ])
      || checkpointSessionSyncResult.checkpointStatus !== "Loaded."
      || checkpointSessionSyncResult.checkpointError !== null
    ) {
      throw new Error(`checkpoint session sync smoke failed (${JSON.stringify(checkpointSessionSyncResult)})`);
    }

    const smoke = await waitFor(async () => {
      const result = await cdp.send(
        "Runtime.evaluate",
        {
          awaitPromise: true,
          returnByValue: true,
          expression: `new Promise((resolve) => {
            const started = performance.now();
            const check = () => {
              const frame = document.getElementById("frame");
              const stateStatus = document.getElementById("stateStatusValue")?.textContent || "";
              const frameStatus = document.getElementById("framesStatusValue")?.textContent || "";
              const connection = "state " + stateStatus.toLowerCase() + " / frames " + frameStatus.toLowerCase();
              if (!frame) {
                resolve({ ok: false, reason: "missing #frame canvas" });
                return;
              }
              const ctx = frame.getContext("2d", { willReadFrequently: true });
              const data = ctx.getImageData(0, 0, frame.width, frame.height).data;
              let nonBlack = 0;
              for (let i = 0; i < data.length; i += 4) {
                if (data[i] || data[i + 1] || data[i + 2]) nonBlack++;
              }
              const live = stateStatus === "Live" && frameStatus === "Live";
              if (live && nonBlack > 100) {
                resolve({ ok: true, connection, nonBlack, width: frame.width, height: frame.height });
              } else if (performance.now() - started > ${timeoutMs}) {
                resolve({ ok: false, reason: "canvas or websocket status did not become live", connection, nonBlack });
              } else {
                setTimeout(check, 100);
              }
            };
            check();
          })`,
        },
        sessionId
      );
      const value = result.result?.value;
      if (value?.ok) return value;
      if (value?.reason) throw new Error(`${value.reason} (${JSON.stringify(value)})`);
      return null;
    }, "Timed out waiting for visual smoke pass");

    const boundedWebSocketRetry = await cdp.send(
      "Runtime.evaluate",
      {
        awaitPromise: true,
        returnByValue: true,
        expression: `(async () => {
          const originalDelays = reconnectDelaysMs;
          let created = 0;
          let exhausted = 0;
          class ClosingSocket {
            constructor() {
              this.readyState = 0;
              created += 1;
              setTimeout(() => {
                this.readyState = 3;
                this.onclose?.();
              }, 0);
            }
            close() {
              this.readyState = 3;
            }
          }
          reconnectDelaysMs = [30];
          const channel = createReconnectChannel("retry-smoke", {
            url: () => "ws://retry-smoke",
            createSocket: () => new ClosingSocket(),
            onExhaust: () => { exhausted += 1; },
          });
          try {
            channel.start();
            channel.start();
            await new Promise((resolve) => setTimeout(resolve, 8));
            channel.pause();
            const createdBeforePause = created;
            await new Promise((resolve) => setTimeout(resolve, 45));
            const paused = created === createdBeforePause;
            channel.resume();
            await new Promise((resolve) => setTimeout(resolve, 45));
            const createdAtExhaustion = created;
            channel.start();
            channel.replace();
            channel.resume();
            await new Promise((resolve) => setTimeout(resolve, 15));
            return {
              paused,
              created,
              createdAtExhaustion,
              exhausted,
              status: channel.status(),
            };
          } finally {
            channel.stop();
            reconnectChannels.delete(channel);
            reconnectDelaysMs = originalDelays;
          }
        })()`,
      },
      sessionId
    );
    const boundedWebSocketRetryResult = boundedWebSocketRetry.result?.value;
    if (
      !boundedWebSocketRetryResult?.paused
      || boundedWebSocketRetryResult.created !== 2
      || boundedWebSocketRetryResult.createdAtExhaustion !== 2
      || boundedWebSocketRetryResult.exhausted !== 1
      || !boundedWebSocketRetryResult.status?.exhausted
      || boundedWebSocketRetryResult.status?.retryPending
    ) {
      throw new Error(`bounded websocket retry smoke failed (${JSON.stringify(boundedWebSocketRetryResult)})`);
    }

    const agentSessionRefreshSingleFlight = await cdp.send(
      "Runtime.evaluate",
      {
        awaitPromise: true,
        returnByValue: true,
        expression: `(async () => {
          const originalFetch = window.fetch;
          const resolvers = [];
          let calls = 0;
          let active = 0;
          let maxActive = 0;
          try {
            if (agentSessionRefreshTimer !== null) clearTimeout(agentSessionRefreshTimer);
            agentSessionRefreshTimer = null;
            agentSessionRefreshQueued = false;
            agentSessionRefreshInFlight = false;
            window.fetch = async (url, options) => {
              if (url !== "/agent/session") return originalFetch(url, options);
              calls += 1;
              active += 1;
              maxActive = Math.max(maxActive, active);
              await new Promise((resolve) => resolvers.push(resolve));
              active -= 1;
              return {
                ok: true,
                json: async () => ({ ok: true, session: agentSession }),
              };
            };
            for (let index = 0; index < 100; index += 1) scheduleAgentSessionStateRefresh();
            await new Promise((resolve) => setTimeout(resolve, 130));
            const firstStarted = calls === 1 && active === 1;
            for (let index = 0; index < 100; index += 1) scheduleAgentSessionStateRefresh();
            resolvers.shift()?.();
            await new Promise((resolve) => setTimeout(resolve, 130));
            const trailingStarted = calls === 2 && active === 1;
            resolvers.shift()?.();
            await new Promise((resolve) => setTimeout(resolve, 20));
            return { calls, maxActive, firstStarted, trailingStarted, active };
          } finally {
            resolvers.splice(0).forEach((resolve) => resolve());
            if (agentSessionRefreshTimer !== null) clearTimeout(agentSessionRefreshTimer);
            agentSessionRefreshTimer = null;
            agentSessionRefreshQueued = false;
            agentSessionRefreshInFlight = false;
            window.fetch = originalFetch;
          }
        })()`,
      },
      sessionId
    );
    const agentSessionRefreshSingleFlightResult = agentSessionRefreshSingleFlight.result?.value;
    if (
      agentSessionRefreshSingleFlightResult?.calls !== 2
      || agentSessionRefreshSingleFlightResult.maxActive !== 1
      || !agentSessionRefreshSingleFlightResult.firstStarted
      || !agentSessionRefreshSingleFlightResult.trailingStarted
      || agentSessionRefreshSingleFlightResult.active !== 0
    ) {
      throw new Error(`agent session refresh single-flight smoke failed (${
        JSON.stringify(agentSessionRefreshSingleFlightResult)
      })`);
    }

    const agentNavigationReuse = await cdp.send(
      "Runtime.evaluate",
      {
        awaitPromise: true,
        returnByValue: true,
        expression: `(async () => {
          const originalFetch = window.fetch;
          const originalState = latestState;
          const originalSession = agentSession;
          const originalEvents = agentSessionEvents;
          const originalResult = agentNavigationResult;
          const originalBase = agentNavigationBase;
          const originalBaseKey = agentNavigationBaseEnvironmentKey;
          const originalCache = new Map(navigationCache);
          let navigationCalls = 0;
          let mismatchDiagnostics = 0;
          try {
            if (navigationRefreshTimer !== null) clearTimeout(navigationRefreshTimer);
            navigationRefreshTimer = null;
            navigationCache.clear();
            const state = JSON.parse(JSON.stringify(originalState));
            state.party_position = { ...(state.party_position || {}), x: 160, y: 80 };
            state.dialog = { ...(state.dialog || {}), active: false };
            state.in_battle = false;
            state._debug = { ...(state._debug || {}), runtime_stale: false };
            state.control = {
              ...(state.control || {}),
              navigation_ready: true,
              allowed_actions: [...new Set([...(state.control?.allowed_actions || []), "navigation_step"])],
            };
            state._navigation = {
              status: "pending",
              targets: [
                { id: 91001, kind: "search", status: "pending" },
                { id: 91002, kind: "touch", status: "pending" },
              ],
            };
            const environmentKey = navigationEnvironmentKey(state);
            agentSession = { ...(originalSession || {}), session_id: "smoke-agent-navigation", status: "running" };
            agentSessionEvents = [{
              session_id: agentSession.session_id,
              seq: 10,
              type: "tool",
              phase: "completed",
              details: {
                name: "get_navigation",
                result: { ok: true, navigation: {
                  status: "ok",
                  environment_key: environmentKey,
                  targets: [{ id: 91001, kind: "search", status: "reachable", steps: 7 }],
                } },
              },
            }];
            restoreAgentNavigationResult();
            window.fetch = async (url, options) => {
              if (url === "/diagnostics/navigation") {
                if (JSON.parse(options?.body || "{}").event === "navigation_key_mismatch") mismatchDiagnostics += 1;
                return { ok: true, json: async () => ({ ok: true }) };
              }
              if (url === "/navigation") {
                navigationCalls += 1;
                return { ok: true, json: async () => ({
                  status: "ok",
                  environment_key: navigationEnvironmentKey(latestState),
                  targets: latestState._navigation.targets.map((target) => ({
                    ...target, status: "reachable", steps: 3,
                  })),
                }) };
              }
              return originalFetch(url, options);
            };

            render(state);
            navigationEnvironmentChangedAt = performance.now() - navigationSettleMs;
            scheduleNavigationRefresh(latestState);
            await new Promise((resolve) => setTimeout(resolve, 0));
            const matched = latestState._navigation.targets.map((target) => [target.id, target.status, target.steps ?? null]);
            const buttonsLocked = [...navigationEl.querySelectorAll("button.navGo")].every((button) => button.disabled);
            const callsWhileRunning = navigationCalls;

            const changed = JSON.parse(JSON.stringify(state));
            changed.party_position.x += 16;
            changed._navigation = {
              status: "pending",
              targets: state._navigation.targets.map((target) => ({ ...target, status: "pending" })),
            };
            render(changed);
            const staleHidden = latestState._navigation.targets.every((target) => target.status === "pending");

            navigationEnvironmentChangedAt = performance.now() - navigationSettleMs;
            setAgentSession({ ...agentSession, status: "paused" });
            if (navigationRefreshTimer !== null) clearTimeout(navigationRefreshTimer);
            navigationRefreshTimer = null;
            navigationEnvironmentChangedAt = performance.now() - navigationSettleMs;
            scheduleNavigationRefresh(latestState);
            await new Promise((resolve) => setTimeout(resolve, 30));
            const resumedKeyMatched = latestState._navigation.environment_key === navigationEnvironmentKey(latestState);
            const resumedButtonEnabled = [...navigationEl.querySelectorAll("button.navGo")].some((button) => !button.disabled);
            const resumed = navigationCalls === 1 && resumedKeyMatched && resumedButtonEnabled;

            navigationCache.clear();
            latestState._navigation = { status: "pending", targets: changed._navigation.targets };
            navigationEnvironmentChangedAt = performance.now() - navigationSettleMs;
            navigationStableEnvironmentKey = navigationEnvironmentKey(latestState);
            let transientCalls = 0;
            window.fetch = async (url, options) => {
              if (url === "/diagnostics/navigation") {
                if (JSON.parse(options?.body || "{}").event === "navigation_key_mismatch") mismatchDiagnostics += 1;
                return { ok: true, json: async () => ({ ok: true }) };
              }
              if (url === "/navigation") {
                transientCalls += 1;
                return { ok: true, json: async () => ({ ok: true, status: "waiting_for_control" }) };
              }
              return originalFetch(url, options);
            };
            await refreshNavigationForKey(navigationEnvironmentKey(latestState));
            await new Promise((resolve) => setTimeout(resolve, 40));
            const transientSettled = transientCalls === 1 && navigationRefreshTimer !== null;
            return { matched, buttonsLocked, callsWhileRunning, staleHidden, resumed, resumedKeyMatched, resumedButtonEnabled, navigationCalls, transientSettled, mismatchDiagnostics };
          } finally {
            if (navigationRefreshTimer !== null) clearTimeout(navigationRefreshTimer);
            navigationRefreshTimer = null;
            window.fetch = originalFetch;
            navigationCache.clear();
            originalCache.forEach((value, key) => navigationCache.set(key, value));
            latestState = originalState;
            agentSession = originalSession;
            agentSessionEvents = originalEvents;
            agentNavigationResult = originalResult;
            agentNavigationBase = originalBase;
            agentNavigationBaseEnvironmentKey = originalBaseKey;
            resetNavigationRefreshRetry();
            if (latestState) render(latestState);
          }
        })()`,
      },
      sessionId
    );
    const agentNavigationReuseResult = agentNavigationReuse.result?.value;
    if (
      JSON.stringify(agentNavigationReuseResult?.matched) !== JSON.stringify([[91001, "reachable", 7], [91002, "pending", null]])
      || !agentNavigationReuseResult.buttonsLocked
      || agentNavigationReuseResult.callsWhileRunning !== 0
      || !agentNavigationReuseResult.staleHidden
      || !agentNavigationReuseResult.resumed
      || !agentNavigationReuseResult.transientSettled
      || agentNavigationReuseResult.mismatchDiagnostics !== 0
    ) {
      throw new Error(`agent navigation reuse smoke failed (${JSON.stringify(agentNavigationReuseResult)})`);
    }

    const navigationSingleFlight = await cdp.send(
      "Runtime.evaluate",
      {
        awaitPromise: true,
        returnByValue: true,
        expression: `(async () => {
          const originalFetch = window.fetch;
          const originalState = latestState;
          const originalCache = new Map(navigationCache);
          let resolveFirst;
          let navigationCalls = 0;
          try {
            if (navigationRefreshTimer !== null) clearTimeout(navigationRefreshTimer);
            navigationRefreshTimer = null;
            navigationCache.clear();
            resetNavigationRefreshRetry();

            const makeState = (x) => {
              const state = JSON.parse(JSON.stringify(originalState));
              state.party_position = { ...(state.party_position || {}), x };
              state._debug = { ...(state._debug || {}), runtime_stale: false };
              state.control = { ...(state.control || {}), navigation_ready: true };
              state._navigation = { status: "pending", targets: [] };
              return state;
            };
            latestState = makeState(101);
            const firstKey = updateNavigationEnvironmentKey(latestState);
            navigationEnvironmentChangedAt = performance.now() - navigationSettleMs;
            const returnedFirstKey = "server-returned-key";
            window.fetch = async (url) => {
              if (url === "/diagnostics/navigation") {
                return { ok: true, json: async () => ({ ok: true }) };
              }
              if (url !== "/navigation") return originalFetch(url);
              navigationCalls += 1;
              if (navigationCalls === 1) {
                return new Promise((resolve) => {
                  resolveFirst = () => resolve({
                    ok: true,
                    json: async () => ({
                      status: "ok",
                      targets: [],
                      environment_key: returnedFirstKey,
                    }),
                  });
                });
              }
              return {
                ok: true,
                json: async () => ({
                  status: "ok",
                  targets: [],
                  environment_key: navigationEnvironmentKey(latestState),
                }),
              };
            };

            scheduleNavigationRefresh(latestState);
            await new Promise((resolve) => setTimeout(resolve, 0));
            for (const x of [102, 103, 104]) {
              latestState = makeState(x);
              updateNavigationEnvironmentKey(latestState);
              scheduleNavigationRefresh(latestState);
            }
            const latestKey = navigationEnvironmentKey(latestState);
            const callsWhileFirstHung = navigationCalls;
            navigationEnvironmentChangedAt = performance.now() - navigationSettleMs;
            resolveFirst();
            await new Promise((resolve) => setTimeout(resolve, 30));
            return {
              callsWhileFirstHung,
              navigationCalls,
              requestCleared: navigationRefreshInFlightKey === null,
              firstRequestKeyCached: navigationCache.has(firstKey),
              returnedFirstKeyCached: navigationCache.has(returnedFirstKey),
              latestKeyCached: navigationCache.has(latestKey),
              latestApplied: latestState._navigation?.environment_key === latestKey,
            };
          } finally {
            if (navigationRefreshTimer !== null) clearTimeout(navigationRefreshTimer);
            navigationRefreshTimer = null;
            navigationCache.clear();
            originalCache.forEach((value, key) => navigationCache.set(key, value));
            window.fetch = originalFetch;
            latestState = originalState;
          }
        })()`,
      },
      sessionId
    );
    const navigationSingleFlightResult = navigationSingleFlight.result?.value;
    if (
      navigationSingleFlightResult?.callsWhileFirstHung !== 1
      || navigationSingleFlightResult.navigationCalls !== 2
      || !navigationSingleFlightResult.requestCleared
      || navigationSingleFlightResult.firstRequestKeyCached
      || !navigationSingleFlightResult.returnedFirstKeyCached
      || !navigationSingleFlightResult.latestKeyCached
      || !navigationSingleFlightResult.latestApplied
    ) {
      throw new Error(`navigation single-flight smoke failed (${JSON.stringify(navigationSingleFlightResult)})`);
    }

    const boundedNavigationRetry = await cdp.send(
      "Runtime.evaluate",
      {
        awaitPromise: true,
        returnByValue: true,
        expression: `(async () => {
          const originalFetch = window.fetch;
          const originalDelays = reconnectDelaysMs;
          const originalState = latestState;
          let navigationCalls = 0;
          try {
            reconnectDelaysMs = [5, 10, 15];
            latestState = JSON.parse(JSON.stringify(originalState));
            latestState._debug = { ...(latestState._debug || {}), runtime_stale: false };
            latestState.control = { ...(latestState.control || {}), navigation_ready: true };
            latestState._navigation = { status: "pending", targets: [] };
            navigationCache.clear();
            navigationLatestEnvironmentKey = navigationEnvironmentKey(latestState);
            navigationEnvironmentChangedAt = performance.now() - navigationSettleMs;
            navigationStableEnvironmentKey = navigationLatestEnvironmentKey;
            resetNavigationRefreshRetry();
            window.fetch = async (url, options) => {
              if (url === "/navigation") {
                navigationCalls += 1;
                throw new TypeError("offline smoke");
              }
              return originalFetch(url, options);
            };
            scheduleNavigationRefresh(latestState);
            await new Promise((resolve) => setTimeout(resolve, 90));
            const callsAtExhaustion = navigationCalls;
            resumeDisconnectedWork();
            scheduleNavigationRefresh(latestState);
            resetLocalAfterCheckpointLoad();
            await new Promise((resolve) => setTimeout(resolve, 25));
            return {
              navigationCalls,
              callsAtExhaustion,
              exhausted: exhaustedTransports.has("navigation"),
              timerCleared: navigationRefreshTimer === null,
              requestCleared: navigationRefreshInFlightKey === null,
              alertVisible: disconnectAlertEl?.hidden === false,
              alertText: disconnectMessageEl?.textContent || "",
              refreshRequired: refreshRequired(),
              controlsDisabled: [...document.querySelectorAll("[data-button]")].every((button) => button.disabled),
            };
          } finally {
            window.fetch = originalFetch;
            reconnectDelaysMs = originalDelays;
            latestState = originalState;
          }
        })()`,
      },
      sessionId
    );
    const boundedNavigationRetryResult = boundedNavigationRetry.result?.value;
    if (
      boundedNavigationRetryResult?.navigationCalls !== 4
      || boundedNavigationRetryResult.callsAtExhaustion !== 4
      || !boundedNavigationRetryResult.exhausted
      || !boundedNavigationRetryResult.timerCleared
      || !boundedNavigationRetryResult.requestCleared
      || !boundedNavigationRetryResult.alertVisible
      || !boundedNavigationRetryResult.alertText.includes("refresh this page")
      || !boundedNavigationRetryResult.refreshRequired
      || !boundedNavigationRetryResult.controlsDisabled
    ) {
      throw new Error(`bounded navigation retry smoke failed (${JSON.stringify(boundedNavigationRetryResult)})`);
    }

    console.log(
      `visual smoke ok: ${smoke.width}x${smoke.height}, non-black pixels=${smoke.nonBlack}, manual input/navigation single-flight/tabs/model controls/goals/timeline paging/checkpoint state preservation/bounded disconnect retries ok, ${smoke.connection}`
    );
    if (cdp.exceptions.length) {
      throw new Error(`console exceptions: ${cdp.exceptions.join("; ")}`);
    }
    ws.close();
  } finally {
    if (browser.chrome.exitCode === null) {
      const exited = new Promise((resolve) => browser.chrome.once("exit", resolve));
      browser.chrome.kill("SIGTERM");
      await Promise.race([exited, sleep(3000)]);
    }
    for (let attempt = 0; attempt < 10; attempt++) {
      try {
        await rm(browser.profileDir, { recursive: true, force: true });
        break;
      } catch (error) {
        if (attempt === 9 || !["ENOTEMPTY", "EBUSY", "EPERM"].includes(error?.code)) throw error;
        await sleep(100);
      }
    }
  }
}

main().catch((error) => {
  console.error(`visual smoke failed: ${error.message}`);
  process.exit(1);
});
