const stateEl = document.getElementById("json");
const summaryEl = document.getElementById("summary");
const objectsEl = document.getElementById("objects");
const screenEl = document.getElementById("screen");
const connectionEl = document.getElementById("connection");
const overlay = document.getElementById("overlay");
const ctx = overlay.getContext("2d");

let latestState = null;

function setConnection(text) {
  connectionEl.textContent = text;
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
  renderSummary(state);
  renderObjects(state);
  drawOverlay(state);
}

async function refreshScreen() {
  screenEl.src = `/screen.bmp?t=${Date.now()}`;
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
  ws.onopen = () => setConnection("live");
  ws.onmessage = (event) => {
    render(JSON.parse(event.data));
    refreshScreen();
  };
  ws.onclose = () => {
    setConnection("reconnecting");
    setTimeout(connectStream, 1000);
  };
  ws.onerror = () => setConnection("error");
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
setInterval(refreshScreen, 500);

