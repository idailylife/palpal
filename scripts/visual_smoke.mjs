#!/usr/bin/env node

import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const chromePath = process.env.PALPAL_CHROME_BIN || defaultChromePath();
const targetUrl = process.env.PALPAL_VISUAL_URL || process.argv[2] || "http://127.0.0.1:8765";
const timeoutMs = Number(process.env.PALPAL_VISUAL_TIMEOUT_MS || 15000);

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

    const normalAgentState = await cdp.send(
      "Runtime.evaluate",
      {
        returnByValue: true,
        expression: `(() => {
          document.getElementById("normalTab")?.click();
          const select = document.querySelector("[data-agent-model]");
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
    const normalAgentResult = normalAgentState.result?.value;
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
              const connection = document.getElementById("connection")?.textContent || "";
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
              const live = connection.includes("state live") && connection.includes("frames live");
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

    console.log(
      `visual smoke ok: ${smoke.width}x${smoke.height}, non-black pixels=${smoke.nonBlack}, tabs/model controls/timeline/checkpoint state preservation ok, ${smoke.connection}`
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
