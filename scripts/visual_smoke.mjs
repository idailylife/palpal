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
    ws.onmessage = (event) => this.handleMessage(event.data);
  }

  handleMessage(raw) {
    const message = JSON.parse(raw);
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
      `visual smoke ok: ${smoke.width}x${smoke.height}, non-black pixels=${smoke.nonBlack}, ${smoke.connection}`
    );
    ws.close();
  } finally {
    browser.chrome.kill("SIGTERM");
    await rm(browser.profileDir, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(`visual smoke failed: ${error.message}`);
  process.exit(1);
});
