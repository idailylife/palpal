const stateEl = document.getElementById("json");
const summaryEl = document.getElementById("summary");
const objectsEl = document.getElementById("objects");
const frameCanvas = document.getElementById("frame");
const connectionEl = document.getElementById("connection");
const runtimeStatusEl = document.getElementById("runtimeStatus");
const overlay = document.getElementById("overlay");
const frameCtx = frameCanvas.getContext("2d");
const ctx = overlay.getContext("2d");

let latestState = null;
let stateConnected = false;
let frameConnected = false;

function setConnection() {
  const state = stateConnected ? "state live" : "state reconnecting";
  const frame = frameConnected ? "frames live" : "frames reconnecting";
  connectionEl.textContent = `${state} / ${frame}`;
}

function formatAge(seconds) {
  if (!Number.isFinite(seconds)) return "unknown";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

function renderRuntimeStatus(state) {
  const debug = state._debug || {};
  const stateFile = debug.state_file || {};
  const frameFile = debug.frame_file || {};
  const messages = [];

  if (state.status === "waiting_for_game" || !stateFile.available) {
    messages.push("Waiting for game state. Start the game with ./run game, or use ./run dev to run the debug server and game together.");
  } else if (stateFile.stale) {
    messages.push(`Game state is stale (${formatAge(stateFile.age_seconds)} old). The game process is probably not running or not writing runtime/state.json.`);
  }

  if (!frameFile.available) {
    messages.push("No frame.rgba yet.");
  } else if (frameFile.stale) {
    messages.push(`Raw frame is stale (${formatAge(frameFile.age_seconds)} old).`);
  }

  runtimeStatusEl.hidden = messages.length === 0;
  runtimeStatusEl.textContent = messages.join(" ");
}

function renderSummary(state) {
  const pos = state.party_position || {};
  const viewport = state.viewport || {};
  const rows = [
    ["frame", state.frame],
    ["scene/map", `${state.scene_id ?? "-"} / ${state.map_id ?? "-"}`],
    ["region", state.region_key],
    ["battle", state.in_battle],
    ["position", `${pos.x ?? "-"}, ${pos.y ?? "-"}`],
    ["viewport", `${viewport.x ?? "-"}, ${viewport.y ?? "-"}`],
    ["cash", state.cash],
    ["objects", state.objects?.length ?? 0],
  ];
  summaryEl.innerHTML = rows
    .map(([key, value]) => `<div>${key}</div><strong>${value ?? "-"}</strong>`)
    .join("");
}

function renderObjects(state) {
  const objects = (state.objects || []).slice(0, 80);
  objectsEl.innerHTML = objects
    .map((obj) => {
      const tags = [
        obj.blocking ? "blocking" : "",
        obj.interactable ? "interactable" : "",
      ]
        .filter(Boolean)
        .join(", ");
      return `<div class="object">
        <strong>#${obj.id}</strong>
        <span>x=${obj.x} y=${obj.y} sprite=${obj.sprite_id} state=${obj.state}<br>${tags}</span>
      </div>`;
    })
    .join("");
}

function drawOverlay(state) {
  ctx.clearRect(0, 0, overlay.width, overlay.height);
  const viewport = state.viewport || { x: 0, y: 0 };
  const scaleX = overlay.width / 320;
  const scaleY = overlay.height / 200;

  ctx.fillStyle = "rgba(255, 214, 102, 0.85)";
  for (const obj of state.objects || []) {
    const x = (obj.x - viewport.x) * scaleX;
    const y = (obj.y - viewport.y) * scaleY;
    if (x < -20 || y < -20 || x > overlay.width + 20 || y > overlay.height + 20) continue;
    ctx.beginPath();
    ctx.arc(x, y, obj.interactable ? 5 : 3, 0, Math.PI * 2);
    ctx.fill();
  }

  const pos = state.party_position;
  if (pos) {
    ctx.strokeStyle = "#66e3ff";
    ctx.lineWidth = 2;
    const x = (pos.x - viewport.x) * scaleX;
    const y = (pos.y - viewport.y) * scaleY;
    ctx.strokeRect(x - 8, y - 14, 16, 16);
  }
}

function render(state) {
  latestState = state;
  stateEl.textContent = JSON.stringify(state, null, 2);
  renderRuntimeStatus(state);
  renderSummary(state);
  renderObjects(state);
  drawOverlay(state);
}

function renderFrame(buffer) {
  const view = new DataView(buffer);
  if (
    buffer.byteLength < 24 ||
    view.getUint8(0) !== 0x50 ||
    view.getUint8(1) !== 0x46 ||
    view.getUint8(2) !== 0x52 ||
    view.getUint8(3) !== 0x4d
  ) {
    return;
  }

  const version = view.getUint16(4, true);
  const width = view.getUint16(6, true);
  const height = view.getUint16(8, true);
  const format = view.getUint16(10, true);
  const payloadLength = view.getUint32(20, true);
  if (version !== 1 || format !== 1 || payloadLength !== width * height * 4) {
    return;
  }
  if (buffer.byteLength < 24 + payloadLength) {
    return;
  }

  if (frameCanvas.width !== width || frameCanvas.height !== height) {
    frameCanvas.width = width;
    frameCanvas.height = height;
  }

  const pixels = new Uint8ClampedArray(buffer, 24, payloadLength);
  frameCtx.putImageData(new ImageData(pixels, width, height), 0, 0);
}

async function sendAction(buttons, frames = 1) {
  await fetch("/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ buttons, frames }),
  });
}

function connectStream() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${protocol}//${location.host}/stream`);
  ws.onopen = () => {
    stateConnected = true;
    setConnection();
  };
  ws.onmessage = (event) => {
    render(JSON.parse(event.data));
  };
  ws.onclose = () => {
    stateConnected = false;
    setConnection();
    setTimeout(connectStream, 1000);
  };
  ws.onerror = () => {
    stateConnected = false;
    setConnection();
  };
}

function connectFrames() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${protocol}//${location.host}/frames`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    frameConnected = true;
    setConnection();
  };
  ws.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) {
      renderFrame(event.data);
    }
  };
  ws.onclose = () => {
    frameConnected = false;
    setConnection();
    setTimeout(connectFrames, 1000);
  };
  ws.onerror = () => {
    frameConnected = false;
    setConnection();
  };
}

document.querySelectorAll("[data-button]").forEach((button) => {
  button.addEventListener("click", () => sendAction([button.dataset.button], 1));
});

window.addEventListener("keydown", (event) => {
  const map = {
    ArrowUp: "up",
    ArrowDown: "down",
    ArrowLeft: "left",
    ArrowRight: "right",
    Enter: "search",
    " ": "search",
    Escape: "menu",
  };
  const button = map[event.key];
  if (button) {
    event.preventDefault();
    sendAction([button], 1);
  }
});

connectStream();
connectFrames();
setConnection();
