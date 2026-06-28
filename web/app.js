const stateEl = document.getElementById("json");
const jsonDetailsEl = document.getElementById("jsonDetails");
const jsonLiveEl = document.getElementById("jsonLive");
const jsonRefreshEl = document.getElementById("jsonRefresh");
const jsonStatusEl = document.getElementById("jsonStatus");
const agentEl = document.getElementById("agent");
const summaryEl = document.getElementById("summary");
const checkpointsEl = document.getElementById("checkpoints");
const objectsEl = document.getElementById("objects");
const navigationEl = document.getElementById("navigation");
const explorationEl = document.getElementById("exploration");
const dialogsEl = document.getElementById("dialogs");
const eventsEl = document.getElementById("events");
const inventoryEl = document.getElementById("inventory");
const equipmentEl = document.getElementById("equipment");
const frameCanvas = document.getElementById("frame");
const fastDialogButton = document.getElementById("fastDialog");
const connectionEl = document.getElementById("connection");
const runtimeStatusEl = document.getElementById("runtimeStatus");
const overlay = document.getElementById("overlay");
const frameCtx = frameCanvas.getContext("2d");
const ctx = overlay.getContext("2d");

let latestState = null;
let selectedRoute = null;
let activeNavigation = null;
let navigationRefreshInFlightKey = null;
let navigationRenderKey = null;
let navigationLatestEnvironmentKey = null;
let navigationEnvironmentChangedAt = 0;
let navigationStableEnvironmentKey = null;
let navigationRefreshTimer = null;
let navigationRouteRequestId = 0;
let navigationDiagnosticKey = null;
let explorationRenderKey = null;
let summaryRenderKey = null;
let dialogsRenderKey = null;
let eventsRenderKey = null;
let agentRenderKey = null;
let agentRequestInFlight = false;
let agentLastDecision = null;
let agentError = null;
let checkpoints = [];
let checkpointsLoaded = false;
let checkpointBusy = null;
let checkpointStatus = "";
let checkpointError = null;
let checkpointRenderKey = null;
let inventoryRenderKey = null;
let equipmentRenderKey = null;
let objectsRenderKey = null;
let jsonRenderText = "";
let jsonLastRenderedAt = 0;
let jsonPendingTimer = null;
const navigationCache = new Map();
let stateConnected = false;
let frameConnected = false;
let dialogAdvanceActive = false;
let dialogAdvanceSending = false;
let dialogAdvanceCount = 0;
let dialogAdvanceStartMarker = null;
const repeatableButtons = new Set(["up", "down", "left", "right"]);
const actionCooldownMs = 180;
const dialogAdvanceDelayMs = 500;
const dialogAdvanceMaxPresses = 100;
const jsonUpdateIntervalMs = 1000;
const navigationSettleMs = 250;
const lastActionAt = new Map();
const directionButtons = { 2: "up", 3: "right", 0: "down", 1: "left" };

function navigationTargets(state) {
  return new Map((state._navigation?.targets || []).map((target) => [target.id, target]));
}

function navigationDiagnosticContext(state) {
  const pos = state?.party_position || {};
  return {
    frame: state?.frame ?? null,
    scene_id: state?.scene_id ?? null,
    map_id: state?.map_id ?? null,
    room_key: state?.room?.key ?? null,
    region_key: state?.region_key ?? null,
    party_position: { x: pos.x ?? null, y: pos.y ?? null },
    dialog_active: Boolean(state?.dialog?.active),
    dialog_text: state?.dialog?.latest_event?.text ?? null,
    active_navigation: activeNavigation ? { ...activeNavigation } : null,
    runtime_stale: Boolean(state?._debug?.runtime_stale),
  };
}

function navigationEnvironmentContext(state) {
  const pos = state?.party_position || {};
  const dialog = state?.dialog || {};
  const latest = dialog.latest_event || {};
  const objects = (state?.objects || [])
    .filter((obj) => obj.interactable)
    .map((obj) => ({
      id: Number(obj.id || 0),
      x: obj.x ?? null,
      y: obj.y ?? null,
      state: obj.state ?? null,
      trigger_mode: obj.trigger_mode ?? null,
      trigger_script: obj.trigger_script ?? null,
      sprite_id: obj.sprite_id ?? null,
      blocking: Boolean(obj.blocking),
      interactable: Boolean(obj.interactable),
      vanish_time: obj.vanish_time ?? null,
    }))
    .sort((a, b) => a.id - b.id);
  return {
    room: {
      scene_id: state?.scene_id ?? null,
      map_id: state?.map_id ?? null,
      key: state?.room?.key ?? null,
    },
    party_position: {
      x: pos.x ?? null,
      y: pos.y ?? null,
    },
    dialog: {
      active: Boolean(dialog.active),
      latest_event: {
        sequence: latest.sequence ?? null,
        message_id: latest.message_id ?? null,
        event_object_id: latest.event_object_id ?? null,
        text: latest.text ?? null,
      },
    },
    objects,
  };
}

function stableJsonStringify(value) {
  if (Array.isArray(value)) {
    return `[${value.map((item) => stableJsonStringify(item)).join(",")}]`;
  }
  if (value && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJsonStringify(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function navigationEnvironmentKey(state) {
  return stableJsonStringify(navigationEnvironmentContext(state));
}

function updateNavigationEnvironmentKey(state) {
  const key = navigationEnvironmentKey(state);
  if (navigationLatestEnvironmentKey !== key) {
    navigationLatestEnvironmentKey = key;
    navigationEnvironmentChangedAt = performance.now();
    navigationStableEnvironmentKey = null;
  }
  return key;
}

function navigationCanRefresh(state) {
  return Boolean(
    state?._navigation &&
    state._navigation.status !== "no_nav_grid" &&
    !state?._debug?.runtime_stale
  );
}

function navigationFullReady(state) {
  return Boolean(
    state?._navigation?.environment_key &&
    state._navigation.environment_key === navigationEnvironmentKey(state) &&
    state._navigation.status === "ok"
  );
}

function navigationTargetActionable(state, targetId) {
  if (!navigationFullReady(state) || state?.dialog?.active || state?.in_battle || state?._debug?.runtime_stale) {
    return false;
  }
  const target = navigationTargets(state).get(targetId);
  return Boolean(target && ![
    "pending",
    "unknown",
    "in_dialog",
    "unreachable_static",
    "blocked_by_transition",
    "needs_direction",
    "no_nav_grid",
    "target_inactive",
  ].includes(target.status));
}

function sendNavigationDiagnostic(type, details = {}) {
  fetch("/diagnostics/navigation", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      type,
      time: new Date().toISOString(),
      details,
    }),
  }).catch((error) => console.warn("navigation diagnostic failed", error));
}

navigationEl.addEventListener("click", (event) => {
  const goButton = event.target.closest("[data-nav-go]");
  if (goButton && navigationEl.contains(goButton)) {
    event.stopPropagation();
    navigateToTarget(Number(goButton.dataset.navGo));
    return;
  }

  const targetEl = event.target.closest(".navTarget");
  if (targetEl && navigationEl.contains(targetEl)) {
    selectNavigationTarget(Number(targetEl.dataset.navId));
  }
});

navigationEl.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  if (event.target.closest("[data-nav-go]")) return;

  const targetEl = event.target.closest(".navTarget");
  if (targetEl && navigationEl.contains(targetEl)) {
    event.preventDefault();
    selectNavigationTarget(Number(targetEl.dataset.navId));
  }
});

inventoryEl.addEventListener("click", (event) => {
  const actionEl = event.target.closest("[data-use-item]");
  if (!actionEl || !inventoryEl.contains(actionEl)) return;
  useInventoryItem(Number(actionEl.dataset.useItem), actionEl.dataset.useTarget);
});

agentEl.addEventListener("click", (event) => {
  const actionEl = event.target.closest("[data-agent-action]");
  if (!actionEl || !agentEl.contains(actionEl)) return;
  if (actionEl.dataset.agentAction === "decide") {
    askAgent();
  }
});

checkpointsEl?.addEventListener("click", (event) => {
  const actionEl = event.target.closest("[data-checkpoint-action]");
  if (!actionEl || !checkpointsEl.contains(actionEl)) return;
  const action = actionEl.dataset.checkpointAction;
  const id = actionEl.dataset.checkpointId;
  if (action === "save") {
    saveCheckpoint();
  } else if (action === "load" && id) {
    loadCheckpoint(id);
  } else if (action === "delete" && id) {
    deleteCheckpoint(id);
  } else if (action === "refresh") {
    refreshCheckpoints();
  } else if (action === "start-new") {
    startNewGame();
  }
});

fastDialogButton?.addEventListener("click", () => {
  fastDialogButton.blur();
  if (dialogAdvanceActive) {
    stopDialogAdvance();
  } else {
    startDialogAdvance();
  }
});

jsonLiveEl?.addEventListener("change", () => {
  if (jsonLiveEl.checked && latestState) {
    renderJson(latestState, { force: true });
  } else {
    updateJsonStatus();
  }
});

jsonRefreshEl?.addEventListener("click", () => {
  if (latestState) renderJson(latestState, { force: true });
});

jsonDetailsEl?.addEventListener("toggle", () => {
  if (jsonDetailsEl.open && latestState && !jsonRenderText) {
    renderJson(latestState, { force: true });
  } else {
    updateJsonStatus();
  }
});

function setPanelHtml(element, html, key, currentKey, onChange) {
  if (currentKey === key) return currentKey;
  element.innerHTML = html;
  if (onChange) onChange();
  return key;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function statusText(status) {
  const labels = {
    reachable: "reachable",
    transition: "transition",
    blocked_by_interactable: "needs action",
    blocked_by_transition: "transition",
    needs_direction: "needs facing",
    target_inactive: "gone",
    unreachable_static: "unreachable",
    no_nav_grid: "no grid",
    pending: "checking",
    unknown: "checking",
    in_dialog: "dialog",
  };
  return labels[status] || status || "unknown";
}

function statusClass(status) {
  if (status === "reachable") return "tagReachable";
  if (status === "transition") return "tagTransition";
  if (status === "blocked_by_interactable") return "tagTransition";
  if (status === "blocked_by_transition") return "tagTransition";
  if (status === "needs_direction") return "tagTransition";
  if (status === "target_inactive") return "tagUnreachable";
  if (status === "unreachable_static") return "tagUnreachable";
  if (status === "in_dialog") return "tagTransition";
  return "";
}

function navKindText(nav, obj) {
  if (nav?.transition_candidate || nav?.status === "transition" || nav?.status === "blocked_by_transition") return "transition";
  if (nav?.kind === "search") return "search";
  if (nav?.kind === "touch") return "touch";
  const mode = Number(obj?.trigger_mode || 0);
  if (mode >= 4) return "touch";
  if (mode > 0) return "search";
  return "object";
}

function navKindClass(kind) {
  if (kind === "transition") return "tagTransition";
  if (kind === "search") return "tagSearch";
  if (kind === "touch") return "tagTouch";
  return "";
}

function explorationStatusText(status) {
  const labels = {
    ok: "ready",
    stepping: "stepping",
    complete: "complete",
    action_pending: "pending",
    no_nav_grid: "no grid",
    no_visible_frontier: "no visible frontier",
    route_blocked: "route blocked",
    state_stale: "stale",
    in_battle: "battle",
    in_dialog: "dialog",
    interrupted_by_dialog: "dialog interrupted",
    interrupted_by_battle: "battle interrupted",
    interrupted_by_room_change: "room changed",
    interrupted_not_in_main_game: "runtime interrupted",
    unsafe_start: "unsafe",
    position_deviated: "deviated",
    stopped: "stopped",
    started: "started",
    stop_requested: "stopping",
    not_running: "stopped",
    running: "running",
    stopping: "stopping",
    room_changed: "room changed",
    not_in_main_game: "runtime interrupted",
  };
  return labels[status] || status || "unknown";
}

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
    ["room", state.room?.key],
    ["battle", state.in_battle],
    ["dialog", state.dialog?.active ? state.dialog.position || "active" : false],
    ["position", `${pos.x ?? "-"}, ${pos.y ?? "-"}`],
    ["viewport", `${viewport.x ?? "-"}, ${viewport.y ?? "-"}`],
    ["cash", state.cash],
    ["objects", state.objects?.length ?? 0],
    ["interactable", (state.objects || []).filter((obj) => obj.interactable).length],
  ];
  const renderKey = JSON.stringify(rows);
  summaryRenderKey = setPanelHtml(summaryEl, rows
    .map(([key, value]) => `<div>${key}</div><strong>${value ?? "-"}</strong>`)
    .join(""), renderKey, summaryRenderKey);
}

function checkpointFileSummary(checkpoint) {
  const files = checkpoint.files || {};
  const companionKeys = Object.keys(files).filter((key) => key.startsWith("companion/"));
  const companionAvailable = companionKeys.filter((key) => files[key]?.available).length;
  return {
    gameAvailable: Boolean(files["game.rpg"]?.available),
    companionAvailable,
    companionTotal: companionKeys.length,
  };
}

function formatCheckpointTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function renderCheckpoints(state) {
  if (!checkpointsEl) return;
  const existingMenu = checkpointsEl.querySelector(".checkpointMenu");
  const menuOpen = Boolean(existingMenu?.open);
  const existingInput = document.getElementById("checkpointLabel");
  const existingLabel = existingInput?.value || "";
  const labelHadFocus = document.activeElement === existingInput;
  const labelSelectionStart = existingInput?.selectionStart ?? existingLabel.length;
  const labelSelectionEnd = existingInput?.selectionEnd ?? labelSelectionStart;
  const stateBusy = Boolean(checkpointBusy);
  const startupWaiting = state?.status === "startup_waiting";
  const stableMainGame = state?.in_main_game !== false && !state?._debug?.runtime_stale && !state?.dialog?.active && !state?.in_battle && !state?.entering_scene && !state?._debug?.action_pending;
  const canSave = !stateBusy && stableMainGame;
  const canLoad = !stateBusy && !state?._debug?.action_pending && (startupWaiting || stableMainGame);
  const canStartNew = startupWaiting && !stateBusy && !state?._debug?.action_pending;
  const renderKey = JSON.stringify({
    loaded: checkpointsLoaded,
    busy: checkpointBusy,
    status: checkpointStatus,
    error: checkpointError,
    canSave,
    canLoad,
    canStartNew,
    startupWaiting,
    checkpoints: checkpoints.map((checkpoint) => [
      checkpoint.id,
      checkpoint.label,
      checkpoint.created_at,
      checkpoint.state?.scene_id,
      checkpoint.state?.map_id,
      checkpoint.state?.room_key,
      checkpoint.state?.frame,
      checkpoint.state?.dialog_active,
      checkpoint.state?.entering_scene,
      checkpointFileSummary(checkpoint),
    ]),
    menuOpen,
  });
  if (checkpointRenderKey === renderKey) return;

  const defaultStatus = startupWaiting
    ? "Game is waiting for a checkpoint or new game."
    : (checkpointsLoaded ? `${checkpoints.length} saved` : "Loading...");
  const statusText = checkpointError || checkpointStatus || defaultStatus;
  const buttonText = checkpointBusy
    ? checkpointStatus || "Checkpoint busy..."
    : checkpointsLoaded
      ? `Checkpoints (${checkpoints.length})`
      : "Checkpoints";
  const listHtml = checkpointsLoaded && checkpoints.length
    ? checkpoints.map((checkpoint) => {
        const fileSummary = checkpointFileSummary(checkpoint);
        const label = checkpoint.label || checkpoint.id;
        const scene = checkpoint.state || {};
        const meta = [
          `scene ${scene.scene_id ?? "-"}/${scene.map_id ?? "-"}`,
          `room ${scene.room_key ?? "-"}`,
          `frame ${scene.frame ?? "-"}`,
          scene.dialog_active ? "dialog" : "",
          scene.entering_scene ? "entering" : "",
        ].filter(Boolean).join(" / ");
        return `<div class="checkpointItem">
          <div class="checkpointMeta">
            <strong>${escapeHtml(label)}</strong>
            <span>${escapeHtml(checkpoint.id)}<small>${escapeHtml(formatCheckpointTime(checkpoint.created_at))}</small></span>
            <span>${escapeHtml(meta)}</span>
            <span>
              <span class="tag ${fileSummary.gameAvailable ? "tagReachable" : "tagUnreachable"}">game ${fileSummary.gameAvailable ? "ok" : "missing"}</span>
              <span class="tag">companion ${fileSummary.companionAvailable}/${fileSummary.companionTotal}</span>
            </span>
          </div>
          <div class="checkpointActions">
            <button class="miniButton" data-checkpoint-action="load" data-checkpoint-id="${escapeHtml(checkpoint.id)}" ${canLoad ? "" : "disabled"}>Load</button>
            <button class="miniButton" data-checkpoint-action="delete" data-checkpoint-id="${escapeHtml(checkpoint.id)}" ${stateBusy ? "disabled" : ""}>Delete</button>
          </div>
        </div>`;
      }).join("")
    : '<div class="navEmpty">No checkpoints yet.</div>';

  checkpointsEl.innerHTML = `
    <details class="checkpointMenu" ${menuOpen ? "open" : ""}>
      <summary>${escapeHtml(buttonText)}</summary>
      <div class="checkpointMenuPanel">
        <div class="checkpointControls">
          <input id="checkpointLabel" type="text" maxlength="120" placeholder="short label" value="${escapeHtml(existingLabel)}" ${stateBusy ? "disabled" : ""} />
          <button id="checkpointSave" data-checkpoint-action="save" ${canSave ? "" : "disabled"}>${checkpointBusy === "saving" ? "Saving..." : "Save"}</button>
        </div>
        ${startupWaiting ? `<button class="startupButton" data-checkpoint-action="start-new" ${canStartNew ? "" : "disabled"}>${checkpointBusy === "starting" ? "Starting..." : "Start New Game"}</button>` : ""}
        <div class="checkpointMenuMeta">
          <div id="checkpointStatus" class="${checkpointError ? "checkpointStatus is-error" : "checkpointStatus"}">${escapeHtml(statusText)}</div>
          <button class="miniButton" data-checkpoint-action="refresh" ${stateBusy ? "disabled" : ""}>Refresh</button>
        </div>
        <div class="checkpointList">${listHtml}</div>
      </div>
    </details>
  `;
  if (labelHadFocus && !stateBusy) {
    const nextInput = document.getElementById("checkpointLabel");
    if (nextInput) {
      nextInput.focus();
      nextInput.setSelectionRange(labelSelectionStart, labelSelectionEnd);
    }
  }
  checkpointRenderKey = renderKey;
}

function renderDialogCurrent(state) {
  const dialog = state.dialog || {};
  const active = Boolean(dialog.active);
  const visibility = dialog.visible ? "visible" : dialog.held ? "held" : "hidden";
  const meta = active
    ? [
        dialog.position || "active",
        `line ${dialog.current_line ?? "-"}`,
        visibility,
        dialog.playing_rng ? "rng" : "",
      ].filter(Boolean).join(" / ")
    : "inactive";
  const text = active ? (dialog.latest_event?.text || "") : "";
  const key = JSON.stringify({
    active,
    meta,
    text,
  });
  const html = `<div class="dialogCurrent ${active ? "is-active" : ""}">
    <strong>${active ? '<span class="tag tagDialog">active</span>' : '<span class="tag">inactive</span>'}</strong>
    <span>${escapeHtml(meta)}${text ? `<small>${escapeHtml(text)}</small>` : ""}</span>
  </div>`;
  return { html, key };
}

function canAdvanceDialog(state) {
  return Boolean(state?.dialog?.active) && !state?.in_battle && !state?._debug?.runtime_stale;
}

function dialogText(dialog) {
  return String(dialog?.text || "").trim();
}

function dialogSourceKeys(dialog) {
  const keys = [];
  if (dialog?.source_key) keys.push(String(dialog.source_key));
  if (Array.isArray(dialog?.source_keys)) {
    for (const key of dialog.source_keys) {
      if (key) keys.push(String(key));
    }
  }
  return [...new Set(keys)];
}

function dialogMarkerFromEntry(dialog) {
  if (!dialog) return null;
  return {
    time: Number.isFinite(Number(dialog.time)) ? Number(dialog.time) : null,
    sourceKeys: dialogSourceKeys(dialog),
  };
}

function dialogEntryMatchesMarker(dialog, marker) {
  if (!dialog || !marker?.sourceKeys?.length) return false;
  const keys = new Set(dialogSourceKeys(dialog));
  return marker.sourceKeys.some((key) => keys.has(key));
}

function dialogAdvanceInitialMarker(state) {
  const dialogs = state?._debug?.recent_dialogs || [];
  if (!dialogs.length) return null;

  const latestDialog = dialogs[dialogs.length - 1];
  const latestText = dialogText(latestDialog);
  const activeText = String(state?.dialog?.latest_event?.text || "").trim();
  if (state?.dialog?.active && activeText && latestText && (latestText === activeText || latestText.endsWith(activeText))) {
    return dialogMarkerFromEntry(dialogs[dialogs.length - 2]);
  }
  return dialogMarkerFromEntry(latestDialog);
}

function dialogIdentity(dialog) {
  if (!dialog || dialog.speaker_hint === "\u7cfb\u7edf") return null;
  const sceneId = dialog.scene_id ?? "";
  const mapId = dialog.map_id ?? "";
  const roomKey = dialog.room_key ?? "";
  if (dialog.event_object_id != null && dialog.event_object_id !== "") {
    return `object:${sceneId}:${mapId}:${roomKey}:${dialog.event_object_id}`;
  }
  if (dialog.speaker_hint) {
    return `speaker:${sceneId}:${mapId}:${roomKey}:${dialog.speaker_hint}`;
  }
  return null;
}

function dialogAdvanceRecentDialogs(state) {
  const dialogs = state?._debug?.recent_dialogs || [];
  const marker = dialogAdvanceStartMarker;
  if (!marker) return dialogs.slice();

  const markerIndex = dialogs.findIndex((dialog) => dialogEntryMatchesMarker(dialog, marker));
  if (markerIndex >= 0) return dialogs.slice(markerIndex + 1);

  if (marker.time != null) {
    return dialogs.filter((dialog) => {
      const time = Number(dialog?.time);
      return Number.isFinite(time) && time > marker.time;
    });
  }
  return dialogs.slice();
}

function dialogAdvanceReachedRepeatedNpcLine(state) {
  const candidates = dialogAdvanceRecentDialogs(state)
    .map((dialog) => ({
      identity: dialogIdentity(dialog),
      text: dialogText(dialog),
    }))
    .filter((dialog) => dialog.identity && dialog.text);
  if (candidates.length < 2) return false;

  const previous = candidates[candidates.length - 2];
  const current = candidates[candidates.length - 1];
  return previous.identity === current.identity && previous.text === current.text;
}

function renderFastDialogButton(state) {
  if (!fastDialogButton) return;

  const available = canAdvanceDialog(state);
  if (dialogAdvanceActive && !available) {
    dialogAdvanceActive = false;
    dialogAdvanceStartMarker = null;
  }

  fastDialogButton.disabled = !dialogAdvanceActive && !available;
  fastDialogButton.classList.toggle("is-running", dialogAdvanceActive);
  fastDialogButton.textContent = dialogAdvanceActive
    ? `Advancing ${dialogAdvanceCount}/${dialogAdvanceMaxPresses}`
    : "Fast Dialog";
}

function startDialogAdvance() {
  if (!canAdvanceDialog(latestState)) return;
  dialogAdvanceActive = true;
  dialogAdvanceCount = 0;
  dialogAdvanceStartMarker = dialogAdvanceInitialMarker(latestState);
  renderFastDialogButton(latestState);
  maybeContinueDialogAdvance(latestState);
}

function stopDialogAdvance() {
  dialogAdvanceActive = false;
  dialogAdvanceStartMarker = null;
  renderFastDialogButton(latestState);
}

function scheduleDialogAdvanceRefresh() {
  setTimeout(() => {
    if (!dialogAdvanceActive) return;
    refreshState().catch((error) => {
      console.warn("dialog advance refresh failed", error);
      stopDialogAdvance();
    });
  }, dialogAdvanceDelayMs);
}

function maybeContinueDialogAdvance(state) {
  if (!dialogAdvanceActive || dialogAdvanceSending) return;
  if (!canAdvanceDialog(state) || dialogAdvanceCount >= dialogAdvanceMaxPresses) {
    stopDialogAdvance();
    return;
  }
  if (dialogAdvanceReachedRepeatedNpcLine(state)) {
    stopDialogAdvance();
    return;
  }
  if (state?._debug?.action_pending) return;
  void advanceDialogOnce();
}

async function advanceDialogOnce() {
  if (!dialogAdvanceActive || dialogAdvanceSending) return;
  dialogAdvanceSending = true;
  renderFastDialogButton(latestState);

  try {
    flashButton("search");
    await sendAction(["search"], 1);
    dialogAdvanceCount += 1;
  } catch (error) {
    console.warn("dialog advance failed", error);
    stopDialogAdvance();
  } finally {
    dialogAdvanceSending = false;
    renderFastDialogButton(latestState);
    if (dialogAdvanceActive) {
      scheduleDialogAdvanceRefresh();
    }
  }
}

function renderDialogs(state) {
  const current = renderDialogCurrent(state);
  const dialogs = (state._debug?.recent_dialogs || []).slice(-8).reverse();
  if (!dialogs.length) {
    dialogsRenderKey = setPanelHtml(
      dialogsEl,
      `${current.html}<div class="navEmpty">No dialog history yet.</div>`,
      `${current.key}:empty`,
      dialogsRenderKey,
    );
    return;
  }

  const renderKey = JSON.stringify([
    current.key,
    dialogs.map((dialog) => [
      dialog.source_key,
      dialog.frame,
      dialog.speaker_hint,
      dialog.text,
      dialog.scene_id,
      dialog.map_id,
      dialog.event_object_id,
      dialog.message_id,
      dialog.message_ids,
    ]),
  ]);
  dialogsRenderKey = setPanelHtml(dialogsEl, current.html + dialogs
    .map((dialog) => {
      const speaker = dialog.speaker_hint ? escapeHtml(dialog.speaker_hint) : "unknown";
      const messageMeta = dialog.message_ids?.length
        ? `msgs #${dialog.message_ids.join(", #")}`
        : (dialog.message_id != null ? `msg #${dialog.message_id}` : "");
      const meta = [
        `frame ${dialog.frame ?? "-"}`,
        `scene ${dialog.scene_id ?? "-"}/${dialog.map_id ?? "-"}`,
        dialog.event_object_id ? `obj #${dialog.event_object_id}` : "",
        messageMeta,
      ].filter(Boolean).join(" / ");
      return `<div class="dialogEvent">
        <strong>${speaker}</strong>
        <span>${escapeHtml(dialog.text)}<small>${escapeHtml(meta)}</small></span>
      </div>`;
    })
    .join(""), renderKey, dialogsRenderKey);
}

function eventText(event) {
  if (event.type === "inventory_changed") {
    const sign = event.delta > 0 ? "+" : "";
    return `item #${event.item_id} ${sign}${event.delta} -> ${event.amount}`;
  }
  if (event.type === "cash_changed") {
    const sign = event.delta > 0 ? "+" : "";
    return `cash ${sign}${event.delta} -> ${event.cash}`;
  }
  if (event.type === "object_changed") {
    const changes = Object.entries(event.changes || {})
      .map(([key, value]) => `${key} ${value.from} -> ${value.to}`)
      .join(", ");
    return `object #${event.object_id}: ${changes}`;
  }
  return event.type || "event";
}

function renderEvents(state) {
  const events = (state._debug?.recent_events || []).slice(-8).reverse();
  if (!events.length) {
    eventsRenderKey = setPanelHtml(eventsEl, '<div class="navEmpty">No observed events yet.</div>', "empty", eventsRenderKey);
    return;
  }
  const renderKey = JSON.stringify(events.map((event) => [
    event.type,
    event.frame,
    event.item_id,
    event.delta,
    event.amount,
    event.cash,
    event.object_id,
    event.changes,
  ]));
  eventsRenderKey = setPanelHtml(eventsEl, events
    .map((event) => `<div class="event">
      <strong>${event.type}</strong>
      <span>${eventText(event)}<small>frame ${event.frame ?? "-"}</small></span>
    </div>`)
    .join(""), renderKey, eventsRenderKey);
}

function actionText(action) {
  if (!action) return "-";
  const args = action.args ? JSON.stringify(action.args) : "{}";
  return `${action.tool || "unknown"} ${args}`;
}

function agentRuntimeKey(state) {
  const room = state?.room || {};
  return [
    state?.scene_id ?? "",
    state?.map_id ?? "",
    room.key ?? state?.region_key ?? "",
  ].join("/");
}

function evidenceItems(evidence) {
  if (!evidence || typeof evidence !== "object") return "";
  return Object.entries(evidence)
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .slice(0, 10)
    .map(([key, value]) => {
      const text = typeof value === "object" ? JSON.stringify(value) : String(value);
      return `<li><strong>${escapeHtml(key)}</strong>: ${escapeHtml(text)}</li>`;
    })
    .join("");
}

function renderAgentDecision(decision) {
  if (!decision) return '<div class="navEmpty">No agent decisions yet.</div>';
  const alternatives = decision.alternatives || [];
  const toolsUsed = Array.isArray(decision.tool_trace) ? decision.tool_trace.length : 0;
  return `<div class="agentDecision">
    <div class="agentSummary">
      <span class="tag tagDialog">${escapeHtml(decision.recommended_intent || "unknown")}</span>
      <strong>${escapeHtml(decision.summary || "No summary.")}</strong>
    </div>
    ${toolsUsed ? `<div class="agentTools">Tools used: ${toolsUsed}</div>` : ""}
    <div class="agentAction">
      <span>Recommended action</span>
      <code>${escapeHtml(actionText(decision.recommended_action))}</code>
    </div>
    <div class="agentRisk">
      <span>Risk</span>
      <p>${escapeHtml(decision.risk || "-")}</p>
    </div>
    <details>
      <summary>Evidence</summary>
      <ul>${evidenceItems(decision.evidence)}</ul>
    </details>
    ${alternatives.length ? `<details>
      <summary>Alternatives</summary>
      <div class="agentAlternatives">${alternatives.map((alternative) => `
        <div>
          <strong>${escapeHtml(alternative.intent || "alternative")}</strong>
          <code>${escapeHtml(actionText(alternative.action))}</code>
          <span>${escapeHtml(alternative.reason || "")}</span>
        </div>
      `).join("")}</div>
    </details>` : ""}
  </div>`;
}

function renderAgentError(error) {
  return `<div class="agentDecision">
    <div class="agentSummary">
      <span class="tag tagDialog">error</span>
      <strong>${escapeHtml(error || "Agent request failed.")}</strong>
    </div>
  </div>`;
}

function agentButtonText(status) {
  if (status?.running) {
    const round = Number(status.round || 0);
    return round > 0 ? `Round ${round}...` : "Thinking...";
  }
  return agentRequestInFlight ? "Thinking..." : "Ask Agent";
}

function agentStatusText(status) {
  if (agentError) return agentError;
  if (status?.running) return status.message || "Waiting for agent.";
  if (agentRequestInFlight) return "Waiting for agent.";
  if (status?.phase === "completed") return status.message || "Decision ready. No actions were executed.";
  if (status?.phase === "error") return status.error || status.message || "Agent request failed.";
  return "No actions are executed.";
}

function renderAgentProgress(status) {
  if (!status?.running) return "";
  const fields = [
    status.backend ? `backend ${status.backend}` : "",
    status.phase ? `phase ${status.phase}` : "",
    Number(status.round || 0) > 0 ? `round ${status.round}` : "",
    Number(status.tool_calls || 0) > 0 ? `tools ${status.tool_calls}` : "",
    status.current_tool ? `current ${status.current_tool}` : "",
  ].filter(Boolean);
  if (!fields.length) return "";
  return `<div class="agentProgress">${escapeHtml(fields.join(" · "))}</div>`;
}

function renderAgent(state) {
  const log = state._debug?.recent_agent_decisions || [];
  const agentStatus = state._debug?.agent_status || {};
  const historyOpen = Boolean(agentEl.querySelector(".agentHistory")?.open);
  const decisionLog = log.filter((entry) => entry?.decision);
  const latestLogged = decisionLog.length ? decisionLog[decisionLog.length - 1] : null;
  const decision = agentError ? null : (agentLastDecision || latestLogged?.decision || null);
  const history = decisionLog.slice(-8).reverse();
  const agentBusy = agentRequestInFlight || Boolean(agentStatus.running);
  const status = agentBusy ? "loading" : agentError ? "error" : decision ? "ready" : "idle";
  const renderKey = JSON.stringify({
    status,
    error: agentError,
    agentStatus: [
      agentStatus.running,
      agentStatus.backend,
      agentStatus.model,
      agentStatus.phase,
      agentStatus.round,
      agentStatus.tool_calls,
      agentStatus.current_tool,
      agentStatus.message,
      agentStatus.error,
      agentStatus.updated_at,
    ],
    decision,
    stateContext: agentRuntimeKey(state),
    history: history.map((entry) => [
      entry.turn,
      entry.time,
      entry.decision?.recommended_intent,
      entry.decision?.summary,
      entry.decision?.tool_trace?.length,
    ]),
  });
  agentRenderKey = setPanelHtml(agentEl, `
    <div class="agentControls">
      <button data-agent-action="decide" ${agentBusy ? "disabled" : ""}>${escapeHtml(agentButtonText(agentStatus))}</button>
      <span class="agentStatusText">${escapeHtml(agentStatusText(agentStatus))}</span>
    </div>
    ${renderAgentProgress(agentStatus)}
    ${agentError ? renderAgentError(agentError) : renderAgentDecision(decision)}
    <details class="agentHistory" ${historyOpen ? "open" : ""}>
      <summary>Decision History <span>${history.length}</span></summary>
      ${history.length ? history.map((entry) => `
        <div class="agentHistoryItem">
          <strong>#${entry.turn ?? "-"} ${escapeHtml(entry.decision?.recommended_intent || "unknown")}</strong>
          <span>${escapeHtml(entry.decision?.summary || "")}</span>
        </div>
      `).join("") : '<div class="navEmpty">No agent decisions yet.</div>'}
    </details>
  `, renderKey, agentRenderKey);
}

function itemFlagLabels(item) {
  return [
    item.usable ? "usable" : "",
    item.equipable ? "equip" : "",
    item.throwable ? "throw" : "",
    item.consuming ? "consume" : "",
    item.apply_to_all ? "all" : "",
    item.sellable ? "sell" : "",
  ].filter(Boolean);
}

function renderInventory(state) {
  const items = state.inventory || [];
  if (!items.length) {
    inventoryRenderKey = setPanelHtml(inventoryEl, '<div class="navEmpty">Inventory is empty.</div>', "empty", inventoryRenderKey);
    return;
  }

  const firstRole = state.party?.[0]?.role_id ?? 0;
  const disabled = Boolean(state.in_battle || state.dialog?.active || state._debug?.runtime_stale);
  const renderKey = JSON.stringify(items.map((item) => [
    item.slot,
    item.item_id,
    item.name,
    item.amount,
    item.amount_in_use,
    item.equipped_count,
    item.flags,
    disabled,
  ]));
  inventoryRenderKey = setPanelHtml(inventoryEl, items.map((item) => {
    const flags = itemFlagLabels(item).map((flag) => `<span class="tag">${flag}</span>`).join("");
    const target = item.apply_to_all ? "all" : String(firstRole);
    const useButton = item.usable
      ? `<button class="miniButton" data-use-item="${item.item_id}" data-use-target="${target}" ${disabled ? "disabled" : ""}>Use</button>`
      : "";
    return `<div class="inventoryItem">
      <strong>${escapeHtml(item.name || `#${item.item_id}`)}</strong>
      <span>id=${item.item_id} amount=${item.amount ?? 0} equipped=${item.equipped_count ?? 0}<br>${flags}</span>
      ${useButton}
    </div>`;
  }).join(""), renderKey, inventoryRenderKey);
}

function renderEquipment(state) {
  const party = state.party || [];
  if (!party.length) {
    equipmentRenderKey = setPanelHtml(equipmentEl, '<div class="navEmpty">No party data.</div>', "empty", equipmentRenderKey);
    return;
  }

  const renderKey = JSON.stringify(party.map((role) => [
    role.role_id,
    role.name,
    (role.equipment || []).map((item) => [item.slot_key, item.item_id, item.name]),
  ]));
  equipmentRenderKey = setPanelHtml(equipmentEl, party.map((role) => `
    <div class="equipmentRole">
      <strong>${escapeHtml(role.name || `role #${role.role_id}`)}</strong>
      <div>${(role.equipment || []).map((item) => `
        <span><em>${escapeHtml(item.slot_key)}</em>${escapeHtml(item.name || (item.item_id ? `#${item.item_id}` : "-"))}</span>
      `).join("")}</div>
    </div>
  `).join(""), renderKey, equipmentRenderKey);
}

function renderObjects(state) {
  const objects = (state.objects || []).slice(0, 80);
  const navTargets = navigationTargets(state);
  const renderKey = JSON.stringify(objects.map((obj) => {
    const nav = navTargets.get(obj.id);
    return [
      obj.id,
      obj.x,
      obj.y,
      obj.sprite_id,
      obj.current_frame,
      obj.state,
      obj.interactable,
      obj.blocking,
      nav?.status,
      nav?.kind,
      nav?.transition_candidate,
    ];
  }));
  objectsRenderKey = setPanelHtml(objectsEl, objects
    .map((obj) => {
      const nav = navTargets.get(obj.id);
      const kind = navKindText(nav, obj);
      const tags = [
        obj.interactable ? '<span class="tag tagInteractable">interactable</span>' : "",
        obj.interactable ? `<span class="tag ${navKindClass(kind)}">${kind}</span>` : "",
        obj.blocking ? '<span class="tag">blocking</span>' : "",
        nav ? `<span class="tag ${statusClass(nav.status)}">${statusText(nav.status)}</span>` : "",
      ]
        .filter(Boolean)
        .join("");
      return `<div class="object ${obj.interactable ? "isInteractable" : ""}">
        <strong>#${obj.id}</strong>
        <span>x=${obj.x} y=${obj.y} sprite=${obj.sprite_id} frame=${obj.current_frame ?? "-"} state=${obj.state}<br>${tags}</span>
      </div>`;
    })
    .join(""), renderKey, objectsRenderKey);
}

function renderNavigation(state) {
  const targets = state._navigation?.targets || [];
  const fullReady = navigationFullReady(state);
  if (!targets.length) {
    const diagnosticKey = JSON.stringify({
      total: 0,
      dialog: Boolean(state.dialog?.active),
      activeNavigation,
      runtimeStale: Boolean(state._debug?.runtime_stale),
    });
    if (navigationDiagnosticKey !== diagnosticKey) {
      navigationDiagnosticKey = diagnosticKey;
      sendNavigationDiagnostic("navigation_buttons", {
        ...navigationDiagnosticContext(state),
        total: 0,
        enabled: 0,
        disabled: 0,
        disabled_reasons: ["no_targets"],
      });
    }
    const emptyKey = "empty";
    if (navigationRenderKey !== emptyKey) {
      navigationEl.innerHTML = '<div class="navEmpty">No navigation grid yet.</div>';
      navigationRenderKey = emptyKey;
    }
    return;
  }

  const renderKey = JSON.stringify({
    selected: selectedRoute?.target_id ?? null,
    activeTarget: activeNavigation?.targetId ?? null,
    activeStatus: activeNavigation?.status ?? null,
    dialog: Boolean(state.dialog?.active),
    navigationStatus: state._navigation?.status ?? null,
    fullReady,
    targets: targets.map((target) => [
      target.id,
      target.kind,
      target.status,
      target.steps,
      target.required_direction,
      target.blocker?.id ?? null,
    ]),
  });
  if (navigationRenderKey === renderKey) return;

  let enabledCount = 0;
  const disabledReasons = new Set();
  for (const target of targets) {
    const displayStatus = state.dialog?.active ? "in_dialog" : fullReady ? target.status : "pending";
    const canGo = ![
      "unreachable_static",
      "blocked_by_transition",
      "needs_direction",
      "no_nav_grid",
      "target_inactive",
      "pending",
      "unknown",
      "in_dialog",
    ].includes(displayStatus);
    const goEnabled = navigationTargetActionable(state, target.id) && !activeNavigation;
    if (goEnabled) {
      enabledCount += 1;
    } else {
      if (!fullReady) disabledReasons.add("navigation_pending");
      if (!canGo) disabledReasons.add(`status:${displayStatus || "unknown"}`);
      if (activeNavigation) disabledReasons.add("active_navigation");
      if (state.dialog?.active) disabledReasons.add("dialog_active");
      if (state.in_battle) disabledReasons.add("in_battle");
      if (state._debug?.runtime_stale) disabledReasons.add("runtime_stale");
    }
  }
  const diagnosticKey = JSON.stringify({
    total: targets.length,
    enabled: enabledCount,
    activeNavigation,
    dialog: Boolean(state.dialog?.active),
    fullReady,
    statuses: targets.map((target) => `${target.id}:${state.dialog?.active ? "in_dialog" : fullReady ? target.status : "pending"}`).join("|"),
  });
  if (navigationDiagnosticKey !== diagnosticKey) {
    navigationDiagnosticKey = diagnosticKey;
    sendNavigationDiagnostic("navigation_buttons", {
      ...navigationDiagnosticContext(state),
      total: targets.length,
      enabled: enabledCount,
      disabled: targets.length - enabledCount,
      disabled_reasons: Array.from(disabledReasons).sort(),
      target_statuses: targets.map((target) => ({
        id: target.id,
        status: state.dialog?.active ? "in_dialog" : fullReady ? target.status : "pending",
        kind: target.kind,
      })),
    });
  }

  navigationEl.innerHTML = targets
    .map((target) => {
      const isActive = activeNavigation?.targetId === target.id;
      const displayStatus = state.dialog?.active ? "in_dialog" : fullReady ? target.status : "pending";
      const displayFullDetails = fullReady && !state.dialog?.active;
      const detail = [
        displayFullDetails ? target.kind : "",
        displayFullDetails && Number.isFinite(target.steps) ? `${target.steps} steps` : "",
        displayFullDetails && target.required_direction ? `face ${target.required_direction}` : "",
      ]
        .filter(Boolean)
        .join(" · ");
      const goEnabled = navigationTargetActionable(state, target.id) && !activeNavigation;
      const goLabel = isActive ? navigationRunText(activeNavigation.status) : displayStatus === "transition" ? "Enter" : "Go";
      return `<div class="navTarget ${selectedRoute?.target_id === target.id ? "is-selected" : ""}" data-nav-id="${target.id}" role="button" tabindex="0">
        <div class="navTargetSelect">
          <strong>#${target.id}</strong>
          <span>
            <span class="tag ${statusClass(displayStatus)}">${statusText(displayStatus)}</span>
            ${detail ? `<small>${detail}</small>` : ""}
          </span>
        </div>
        <button class="navGo" data-nav-go="${target.id}" ${goEnabled ? "" : "disabled"}>${goLabel}</button>
      </div>`;
    })
    .join("");
  navigationRenderKey = renderKey;
}

function scheduleNavigationRefresh(state) {
  if (navigationRefreshTimer) {
    clearTimeout(navigationRefreshTimer);
    navigationRefreshTimer = null;
  }
  if (!navigationCanRefresh(state)) return;

  const key = navigationEnvironmentKey(state);
  const now = performance.now();
  const stableFor = now - navigationEnvironmentChangedAt;
  const waitMs = Math.max(0, navigationSettleMs - stableFor);
  if (waitMs > 0) {
    navigationRefreshTimer = setTimeout(() => {
      navigationRefreshTimer = null;
      if (latestState) scheduleNavigationRefresh(latestState);
    }, waitMs);
    return;
  }

  navigationStableEnvironmentKey = key;
  const cachedNavigation = navigationCache.get(key);
  if (cachedNavigation) {
    if (latestState && latestState._navigation?.environment_key !== key) {
      latestState._navigation = cachedNavigation;
      renderNavigation(latestState);
      renderObjects(latestState);
      drawOverlay(latestState);
    }
    return;
  }

  if (navigationRefreshInFlightKey) return;
  refreshNavigationForKey(key);
}

async function refreshNavigationForKey(requestKey) {
  if (navigationRefreshInFlightKey) return;
  navigationRefreshInFlightKey = requestKey;
  try {
    const response = await fetch("/navigation");
    if (!response.ok) {
      throw new Error(`navigation failed: ${response.status}`);
    }
    const navigation = await response.json();
    const returnedKey = navigation.environment_key || requestKey;
    navigationCache.set(returnedKey, navigation);
    if (
      latestState &&
      returnedKey === navigationStableEnvironmentKey &&
      navigationEnvironmentKey(latestState) === returnedKey
    ) {
      latestState._navigation = navigation;
      renderNavigation(latestState);
      renderObjects(latestState);
      drawOverlay(latestState);
    }
  } catch (error) {
    console.warn(error);
  } finally {
    if (navigationRefreshInFlightKey === requestKey) {
      navigationRefreshInFlightKey = null;
    }
    if (latestState && navigationEnvironmentKey(latestState) !== requestKey) {
      scheduleNavigationRefresh(latestState);
    }
  }
}

function navigationRunText(status) {
  const labels = {
    starting: "Go...",
    stepping: "Moving",
    facing: "Facing",
    interacting: "Action",
    arrived: "Arrived",
    action_pending: "Pending",
  };
  return labels[status] || "Stop";
}

function explorationCoverage(exploration) {
  const explored = Number(exploration?.explored ?? exploration?.visited ?? 0);
  const explorable = Number(exploration?.explorable ?? 0);
  if (!Number.isFinite(explored) || !Number.isFinite(explorable) || explorable <= 0) {
    return { explored: Math.max(0, explored || 0), explorable: Math.max(0, explorable || 0), percent: 0 };
  }
  const percent = Math.max(0, Math.min(100, Math.round((explored / explorable) * 100)));
  return { explored, explorable, percent };
}

function renderExploration(state) {
  const exploration = state._exploration;
  if (!exploration) {
    explorationRenderKey = setPanelHtml(explorationEl, '<div class="navEmpty">No exploration state yet.</div>', "empty", explorationRenderKey);
    return;
  }

  const complete = exploration.complete ? '<span class="tag tagReachable">complete</span>' : "";
  const status = exploration.status;
  const coverage = explorationCoverage(exploration);
  const renderKey = JSON.stringify({
    status: exploration.status,
    visited: exploration.visited,
    explored: exploration.explored,
    explorable: exploration.explorable,
    frontier: exploration.frontier,
    blocked: exploration.blocked_edges,
    complete: exploration.complete,
    dialog: Boolean(state.dialog?.active),
    coveragePercent: coverage.percent,
  });
  explorationRenderKey = setPanelHtml(explorationEl, `
    <div class="explorationStats">
      <div><span>explored</span><strong>${exploration.explored ?? exploration.visited ?? 0}</strong></div>
      <div><span>edge</span><strong>${exploration.frontier ?? 0}</strong></div>
      <div><span>explorable</span><strong>${exploration.explorable ?? 0}</strong></div>
    </div>
    <div class="explorationProgress">
      <div class="explorationProgressMeta">
        <span>room coverage</span>
        <strong>${coverage.percent}%</strong>
      </div>
      <div class="explorationProgressTrack" aria-label="room exploration coverage" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${coverage.percent}" role="progressbar">
        <span style="width: ${coverage.percent}%"></span>
      </div>
      <small>${coverage.explored} / ${coverage.explorable} safe nodes explored</small>
    </div>
    <div class="explorationStatus">
      <span class="tag">${explorationStatusText(status)}</span>
      ${complete}
    </div>
  `, renderKey, explorationRenderKey);
}

function objectScreenPosition(obj, viewport, scaleX, scaleY) {
  return {
    x: (obj.x - viewport.x) * scaleX,
    y: (obj.y - viewport.y) * scaleY,
  };
}

function markerStyle(obj) {
  const status = obj.nav?.status;
  const kind = navKindText(obj.nav, obj);
  const colors = {
    reachable: "#7ee3ae",
    transition: "#ff8a66",
    blocked_by_interactable: "#ffd666",
    blocked_by_transition: "#ff8a66",
    needs_direction: "#ffd666",
    target_inactive: "#9aa3ad",
    unreachable_static: "#9aa3ad",
    unknown: "#cbd6df",
  };
  return {
    kind,
    color: colors[status] || (obj.interactable ? "#ffd666" : "#9aa3ad"),
    label: kind === "transition" ? "T" : kind === "search" ? "S" : kind === "touch" ? "G" : "!",
  };
}

function drawDiamond(x, y, radius) {
  ctx.beginPath();
  ctx.moveTo(x, y - radius);
  ctx.lineTo(x + radius, y);
  ctx.lineTo(x, y + radius);
  ctx.lineTo(x - radius, y);
  ctx.closePath();
}

function drawInteractableMarker(obj, x, y) {
  const style = markerStyle(obj);
  ctx.save();
  ctx.lineWidth = 2;
  ctx.fillStyle = "rgba(5, 6, 7, 0.78)";
  ctx.strokeStyle = style.color;
  if (style.kind === "transition") {
    drawDiamond(x, y, 10);
  } else if (style.kind === "search") {
    ctx.beginPath();
    ctx.rect(x - 8, y - 8, 16, 16);
  } else {
    ctx.beginPath();
    ctx.arc(x, y, 8, 0, Math.PI * 2);
  }
  ctx.fill();
  ctx.stroke();

  ctx.fillStyle = style.color;
  ctx.font = "bold 12px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(style.label, x, y - 1);

  ctx.font = "bold 11px system-ui, sans-serif";
  ctx.textAlign = "left";
  ctx.textBaseline = "top";
  ctx.strokeStyle = "rgba(5, 6, 7, 0.9)";
  ctx.lineWidth = 3;
  const label = `#${obj.id} ${style.kind}`;
  ctx.strokeText(label, x + 10, y - 8);
  ctx.fillText(label, x + 10, y - 8);
  ctx.restore();
}

function drawRoute(state) {
  const route = selectedRoute?.route || selectedRoute?.blocked_route;
  const nodes = route?.nodes || [];
  if (nodes.length < 2) return;

  const viewport = state.viewport || { x: 0, y: 0 };
  const scaleX = overlay.width / 320;
  const scaleY = overlay.height / 200;
  const isBlocked = selectedRoute?.status === "blocked_by_transition";

  ctx.save();
  ctx.strokeStyle = isBlocked ? "#ff8a66" : "#66e3ff";
  ctx.lineWidth = 3;
  if (isBlocked) ctx.setLineDash([7, 5]);
  ctx.beginPath();
  nodes.forEach((node, index) => {
    const x = (node.x - viewport.x) * scaleX;
    const y = (node.y - viewport.y) * scaleY;
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.restore();
}

function drawExploration(state) {
  const exploration = state._exploration;
  if (!exploration) return;
  const viewport = state.viewport || { x: 0, y: 0 };
  const scaleX = overlay.width / 320;
  const scaleY = overlay.height / 200;

  ctx.save();
  for (const node of exploration.nodes?.explorable || []) {
    const x = (node.x - viewport.x) * scaleX;
    const y = (node.y - viewport.y) * scaleY;
    if (x < -10 || y < -10 || x > overlay.width + 10 || y > overlay.height + 10) continue;
    ctx.fillStyle = "rgba(102, 227, 255, 0.08)";
    ctx.fillRect(x - 4, y - 4, 8, 8);
  }

  for (const node of exploration.nodes?.explored || exploration.nodes?.visited || []) {
    const x = (node.x - viewport.x) * scaleX;
    const y = (node.y - viewport.y) * scaleY;
    if (x < -10 || y < -10 || x > overlay.width + 10 || y > overlay.height + 10) continue;
    ctx.fillStyle = "rgba(126, 227, 174, 0.34)";
    ctx.fillRect(x - 5, y - 5, 10, 10);
  }

  for (const node of exploration.nodes?.frontier || []) {
    const x = (node.x - viewport.x) * scaleX;
    const y = (node.y - viewport.y) * scaleY;
    if (x < -10 || y < -10 || x > overlay.width + 10 || y > overlay.height + 10) continue;
    ctx.strokeStyle = "rgba(255, 214, 102, 0.62)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(x, y, 5, 0, Math.PI * 2);
    ctx.stroke();
  }

  const route = exploration.last_route;
  const nodes = route?.nodes || [];
  if (nodes.length >= 2) {
    ctx.strokeStyle = route.pending ? "#7ee3ae" : "rgba(126, 227, 174, 0.55)";
    ctx.lineWidth = 3;
    ctx.setLineDash(route.pending ? [] : [4, 4]);
    ctx.beginPath();
    nodes.forEach((node, index) => {
      const x = (node.x - viewport.x) * scaleX;
      const y = (node.y - viewport.y) * scaleY;
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
  ctx.restore();
}

function drawOverlay(state) {
  ctx.clearRect(0, 0, overlay.width, overlay.height);
  const viewport = state.viewport || { x: 0, y: 0 };
  const scaleX = overlay.width / 320;
  const scaleY = overlay.height / 200;
  const navTargets = navigationTargets(state);
  const hideMapHints = Boolean(state.dialog?.active);

  if (!hideMapHints) {
    drawExploration(state);
    drawRoute(state);

    for (const obj of state.objects || []) {
      const { x, y } = objectScreenPosition(obj, viewport, scaleX, scaleY);
      if (x < -20 || y < -20 || x > overlay.width + 20 || y > overlay.height + 20) continue;
      const nav = navTargets.get(obj.id);
      if (obj.interactable) {
        drawInteractableMarker({ ...obj, nav }, x, y);
      } else {
        ctx.fillStyle = "rgba(159, 177, 193, 0.38)";
        ctx.beginPath();
        ctx.arc(x, y, 3, 0, Math.PI * 2);
        ctx.fill();
      }
    }
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

function updateJsonStatus() {
  if (!jsonStatusEl) return;
  if (jsonDetailsEl && !jsonDetailsEl.open) {
    jsonStatusEl.textContent = "hidden";
    return;
  }
  if (!jsonLiveEl?.checked) {
    jsonStatusEl.textContent = "paused";
    return;
  }
  if (!jsonLastRenderedAt) {
    jsonStatusEl.textContent = "pending";
    return;
  }
  jsonStatusEl.textContent = "live";
}

function scheduleJsonRender() {
  if (jsonPendingTimer) return;
  if (jsonDetailsEl && !jsonDetailsEl.open) return;
  const elapsed = performance.now() - jsonLastRenderedAt;
  const delay = Math.max(0, jsonUpdateIntervalMs - elapsed);
  jsonPendingTimer = setTimeout(() => {
    jsonPendingTimer = null;
    if (latestState) renderJson(latestState);
  }, delay);
}

function renderJson(state, options = {}) {
  const force = Boolean(options.force);
  if (!force && jsonDetailsEl && !jsonDetailsEl.open) {
    updateJsonStatus();
    return;
  }
  if (!force && jsonLiveEl && !jsonLiveEl.checked) {
    updateJsonStatus();
    return;
  }

  const now = performance.now();
  if (!force && jsonLastRenderedAt && now - jsonLastRenderedAt < jsonUpdateIntervalMs) {
    scheduleJsonRender();
    updateJsonStatus();
    return;
  }

  const text = JSON.stringify(state, null, 2);
  if (jsonRenderText !== text) {
    stateEl.textContent = text;
    jsonRenderText = text;
  }
  jsonLastRenderedAt = now;
  updateJsonStatus();
}

function render(state) {
  const navKey = updateNavigationEnvironmentKey(state);
  const cachedNavigation = navigationCache.get(navKey);
  if (cachedNavigation) {
    state._navigation = cachedNavigation;
  }
  latestState = state;
  renderJson(state);
  renderRuntimeStatus(state);
  renderSummary(state);
  renderCheckpoints(state);
  renderAgent(state);
  renderFastDialogButton(state);
  renderDialogs(state);
  renderEvents(state);
  renderInventory(state);
  renderEquipment(state);
  renderNavigation(state);
  renderExploration(state);
  renderObjects(state);
  drawOverlay(state);
  scheduleNavigationRefresh(state);
  maybeContinueDialogAdvance(state);
}

async function useInventoryItem(itemId, target) {
  const payload = { item_id: itemId };
  if (target === "all") {
    payload.target = "all";
  } else {
    payload.role_id = Number(target || 0);
  }
  await fetch("/inventory/use", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function selectNavigationTarget(targetId) {
  if (!navigationTargetActionable(latestState, targetId)) {
    sendNavigationDiagnostic("navigation_route_ignored", {
      ...navigationDiagnosticContext(latestState),
      target_id: targetId,
      reason: "navigation_not_ready",
    });
    return;
  }
  const requestId = ++navigationRouteRequestId;
  selectedRoute = { target_id: targetId, status: "loading" };
  if (latestState) {
    renderNavigation(latestState);
    drawOverlay(latestState);
  }

  try {
    const response = await fetch(`/navigation/route?id=${encodeURIComponent(targetId)}`);
    const route = await response.json();
    if (requestId !== navigationRouteRequestId) return;
    selectedRoute = route;
  } catch (error) {
    if (requestId !== navigationRouteRequestId) return;
    console.warn(error);
    selectedRoute = { target_id: targetId, status: "error" };
  }

  if (latestState) {
    renderNavigation(latestState);
    drawOverlay(latestState);
  }
}

async function refreshState() {
  const response = await fetch("/state");
  const state = await response.json();
  render(state);
}

async function refreshCheckpoints() {
  try {
    const response = await fetch("/checkpoints");
    const result = await response.json();
    if (!response.ok || result.ok === false) {
      throw new Error(result.error || `checkpoints failed: ${response.status}`);
    }
    checkpoints = result.checkpoints || [];
    checkpointsLoaded = true;
    checkpointError = null;
  } catch (error) {
    checkpointError = error instanceof Error ? error.message : String(error);
  } finally {
    if (latestState) renderCheckpoints(latestState);
  }
}

function resetLocalAfterCheckpointLoad() {
  navigationCache.clear();
  selectedRoute = null;
  activeNavigation = null;
  navigationRefreshInFlightKey = null;
  navigationRenderKey = null;
  navigationLatestEnvironmentKey = null;
  navigationStableEnvironmentKey = null;
  navigationRouteRequestId += 1;
  if (navigationRefreshTimer) {
    clearTimeout(navigationRefreshTimer);
    navigationRefreshTimer = null;
  }
  agentError = null;
  agentLastDecision = null;
}

async function saveCheckpoint() {
  if (checkpointBusy) return;
  const label = document.getElementById("checkpointLabel")?.value || "";
  checkpointBusy = "saving";
  checkpointStatus = "Saving...";
  checkpointError = null;
  if (latestState) renderCheckpoints(latestState);
  try {
    const response = await fetch("/checkpoints/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label }),
    });
    const result = await response.json();
    if (!response.ok || result.ok === false) {
      throw new Error(result.error || `save failed: ${response.status}`);
    }
    checkpointStatus = "Saved.";
    await refreshCheckpoints();
  } catch (error) {
    checkpointError = error instanceof Error ? error.message : String(error);
  } finally {
    checkpointBusy = null;
    if (latestState) renderCheckpoints(latestState);
  }
}

async function loadCheckpoint(id) {
  if (checkpointBusy) return;
  if (!window.confirm(`Load checkpoint ${id}? Current live game state will be replaced.`)) return;
  checkpointBusy = `loading:${id}`;
  checkpointStatus = "Loading...";
  checkpointError = null;
  if (latestState) renderCheckpoints(latestState);
  try {
    const response = await fetch("/checkpoints/load", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    const result = await response.json();
    if (!response.ok || result.ok === false) {
      throw new Error(result.error || `load failed: ${response.status}`);
    }
    checkpointStatus = "Loaded.";
    resetLocalAfterCheckpointLoad();
    await refreshCheckpoints();
    await refreshState();
  } catch (error) {
    checkpointError = error instanceof Error ? error.message : String(error);
  } finally {
    checkpointBusy = null;
    if (latestState) renderCheckpoints(latestState);
  }
}

async function startNewGame() {
  if (checkpointBusy) return;
  checkpointBusy = "starting";
  checkpointStatus = "Starting new game...";
  checkpointError = null;
  if (latestState) renderCheckpoints(latestState);
  try {
    const response = await fetch("/startup/new", { method: "POST" });
    const result = await response.json();
    if (!response.ok || result.ok === false) {
      throw new Error(result.error || `start failed: ${response.status}`);
    }
    checkpointStatus = "Started.";
    resetLocalAfterCheckpointLoad();
    await refreshState();
  } catch (error) {
    checkpointError = error instanceof Error ? error.message : String(error);
  } finally {
    checkpointBusy = null;
    if (latestState) renderCheckpoints(latestState);
  }
}

async function deleteCheckpoint(id) {
  if (checkpointBusy) return;
  if (!window.confirm(`Delete checkpoint ${id}?`)) return;
  checkpointBusy = `deleting:${id}`;
  checkpointStatus = "Deleting...";
  checkpointError = null;
  if (latestState) renderCheckpoints(latestState);
  try {
    const response = await fetch(`/checkpoints/${encodeURIComponent(id)}`, { method: "DELETE" });
    const result = await response.json();
    if (!response.ok || result.ok === false) {
      throw new Error(result.error || `delete failed: ${response.status}`);
    }
    checkpointStatus = "Deleted.";
    await refreshCheckpoints();
  } catch (error) {
    checkpointError = error instanceof Error ? error.message : String(error);
  } finally {
    checkpointBusy = null;
    if (latestState) renderCheckpoints(latestState);
  }
}

async function askAgent() {
  if (agentRequestInFlight) return;
  agentRequestInFlight = true;
  agentError = null;
  if (latestState) renderAgent(latestState);

  try {
    const response = await fetch("/agent/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const result = await response.json();
    if (!response.ok || !result.ok) {
      throw new Error(result.error || `agent failed: ${response.status}`);
    }
    agentLastDecision = result.decision;
    await refreshState();
  } catch (error) {
    agentLastDecision = null;
    agentError = error instanceof Error ? error.message : String(error);
  } finally {
    agentRequestInFlight = false;
    if (latestState) renderAgent(latestState);
  }
}

function partyPositionKey(state) {
  const pos = state?.party_position || {};
  return `${pos.x ?? "-"},${pos.y ?? "-"}`;
}

function routeNodePositionKey(node) {
  if (!node) return null;
  return `${node.x ?? "-"},${node.y ?? "-"}`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForPositionChange(startKey, timeoutMs = 900) {
  const deadline = performance.now() + timeoutMs;
  while (performance.now() < deadline) {
    await sleep(35);
    const response = await fetch("/state");
    const state = await response.json();
    render(state);
    if (partyPositionKey(state) !== startKey) {
      return true;
    }
    if (state._debug?.runtime_stale || state.in_battle || state.dialog?.active) {
      return false;
    }
  }
  return false;
}

async function waitForNavigationProgress(stepResult, startKey) {
  const expectedKey = routeNodePositionKey(stepResult.expected_node || stepResult.route?.nodes?.[1]);
  const deadline = performance.now() + 2500;
  while (performance.now() < deadline) {
    await sleep(35);
    const response = await fetch("/state");
    const state = await response.json();
    render(state);
    const key = partyPositionKey(state);
    if (expectedKey ? key === expectedKey : key !== startKey) {
      return true;
    }
    if (state._debug?.runtime_stale || state.in_battle) {
      return false;
    }
  }
  return false;
}

async function navigationStep(targetId) {
  const response = await fetch("/navigation/step", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: targetId }),
  });
  const result = await response.json();
  sendNavigationDiagnostic("navigation_step_result", {
    ...navigationDiagnosticContext(latestState),
    target_id: targetId,
    http_status: response.status,
    ok: Boolean(result.ok),
    status: result.status ?? null,
    move: result.move ?? null,
    segment_steps: result.segment_steps ?? null,
    remaining_steps: result.remaining_steps ?? null,
    required_direction: result.required_direction ?? null,
    blocker_id: result.blocker?.id ?? null,
  });
  return result;
}

function partyDirectionButton(state) {
  return directionButtons[Number(state?.party_direction)];
}

function updateNavigationTarget(targetId, patch) {
  if (!latestState?._navigation?.targets) return;
  latestState._navigation = {
    ...latestState._navigation,
    targets: latestState._navigation.targets.map((target) =>
      target.id === targetId ? { ...target, ...patch } : target
    ),
  };
  navigationCache.set(navigationEnvironmentKey(latestState), latestState._navigation);
}

async function waitForInteractionResult(stepResult, timeoutMs = 2500) {
  const blocker = stepResult.blocker || {};
  const blockerId = blocker.id;
  const deadline = performance.now() + timeoutMs;
  sendNavigationDiagnostic("interaction_wait_start", {
    ...navigationDiagnosticContext(latestState),
    target_id: stepResult.target_id ?? null,
    blocker_id: blockerId ?? null,
    timeout_ms: timeoutMs,
  });
  while (performance.now() < deadline) {
    await sleep(80);
    const response = await fetch("/state");
    const state = await response.json();
    render(state);
    if (state._debug?.runtime_stale || state.in_battle || state.dialog?.active) {
      sendNavigationDiagnostic("interaction_wait_stop", {
        ...navigationDiagnosticContext(state),
        target_id: stepResult.target_id ?? null,
        blocker_id: blockerId ?? null,
        reason: state._debug?.runtime_stale ? "runtime_stale" : state.in_battle ? "in_battle" : "dialog_active",
      });
      return false;
    }
    if (!blockerId) {
      sendNavigationDiagnostic("interaction_wait_stop", {
        ...navigationDiagnosticContext(state),
        target_id: stepResult.target_id ?? null,
        reason: "no_blocker",
      });
      return true;
    }
    const current = (state.objects || []).find((obj) => obj.id === blockerId);
    if (!current || current.blocking === false || current.state !== blocker.state) {
      sendNavigationDiagnostic("interaction_wait_stop", {
        ...navigationDiagnosticContext(state),
        target_id: stepResult.target_id ?? null,
        blocker_id: blockerId,
        reason: !current ? "blocker_missing" : current.blocking === false ? "blocker_unblocked" : "blocker_state_changed",
      });
      return true;
    }
  }
  sendNavigationDiagnostic("interaction_wait_stop", {
    ...navigationDiagnosticContext(latestState),
    target_id: stepResult.target_id ?? null,
    blocker_id: blockerId ?? null,
    reason: "timeout",
  });
  return false;
}

async function waitForFacing(requiredDirection) {
  if (!requiredDirection) return true;
  const deadline = performance.now() + 1200;
  while (performance.now() < deadline) {
    await sleep(35);
    const response = await fetch("/state");
    const state = await response.json();
    render(state);
    if (partyDirectionButton(state) === requiredDirection) {
      return true;
    }
    if (state._debug?.runtime_stale || state.in_battle) {
      return false;
    }
  }
  return false;
}

async function playNavigationSegment(stepResult, startKey) {
  return waitForNavigationProgress(stepResult, startKey);
}

async function navigateToTarget(targetId) {
  if (activeNavigation) {
    sendNavigationDiagnostic("navigation_click_ignored", {
      ...navigationDiagnosticContext(latestState),
      target_id: targetId,
      reason: "active_navigation",
    });
    return;
  }
  if (!navigationTargetActionable(latestState, targetId)) {
    sendNavigationDiagnostic("navigation_click_ignored", {
      ...navigationDiagnosticContext(latestState),
      target_id: targetId,
      reason: "navigation_not_ready",
    });
    return;
  }
  activeNavigation = { targetId, status: "starting" };
  sendNavigationDiagnostic("navigation_start", {
    ...navigationDiagnosticContext(latestState),
    target_id: targetId,
  });
  if (latestState) renderNavigation(latestState);

  const unlockNavigation = (reason = "complete") => {
    if (!activeNavigation) return;
    sendNavigationDiagnostic("navigation_unlock", {
      ...navigationDiagnosticContext(latestState),
      target_id: activeNavigation.targetId,
      status: activeNavigation.status,
      reason,
    });
    activeNavigation = null;
    if (latestState) renderNavigation(latestState);
  };

  try {
    for (let index = 0; index < 240; index += 1) {
      const startKey = partyPositionKey(latestState);
      const result = await navigationStep(targetId);
      activeNavigation.status = result.status;
      if (result.route) selectedRoute = { ...result, target_id: targetId };
      if (latestState) {
        renderNavigation(latestState);
        drawOverlay(latestState);
      }

      if (result.status === "arrived") {
        unlockNavigation("arrived");
        await refreshState();
        return;
      }

      if (!result.ok && result.status === "action_pending") {
        await sleep(120);
        continue;
      }

      if (!result.ok) {
        updateNavigationTarget(targetId, {
          status: result.status || "unreachable_static",
          blocker: result.blocker,
        });
      }

      if (result.ok && result.status === "interacting") {
        const interactionProgressed = await waitForInteractionResult(result);
        await refreshState();
        if (!result.blocker?.id) {
          unlockNavigation(interactionProgressed ? "interacted" : "interaction_done");
          return;
        }
        continue;
      }

      if (result.ok && result.status === "facing") {
        const faced = await waitForFacing(result.required_direction);
        if (!faced) {
          unlockNavigation("facing_failed");
          await refreshState();
          return;
        }
        continue;
      }

      if (!result.ok || result.status !== "stepping") {
        unlockNavigation(result.status || "not_stepping");
        await refreshState();
        return;
      }

      const progressed = await playNavigationSegment(result, startKey);
      if (!progressed) {
        unlockNavigation("progress_failed");
        await refreshState();
        return;
      }
    }
  } finally {
    unlockNavigation("finally");
  }
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
  const response = await fetch("/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ buttons, frames }),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok || result.ok === false) {
    throw new Error(result.error || `action failed: ${response.status}`);
  }
  return result;
}

function flashButton(buttonName) {
  const button = document.querySelector(`[data-button="${buttonName}"]`);
  if (!button) return;
  button.classList.add("is-active");
  setTimeout(() => button.classList.remove("is-active"), 120);
}

function sendButton(buttonName) {
  const now = performance.now();
  if (!repeatableButtons.has(buttonName)) {
    const last = lastActionAt.get(buttonName) || 0;
    if (now - last < actionCooldownMs) return;
  }
  lastActionAt.set(buttonName, now);
  flashButton(buttonName);
  sendAction([buttonName], 1).catch((error) => console.warn("action failed", error));
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
  button.addEventListener("click", () => {
    button.blur();
    sendButton(button.dataset.button);
  });
});

function isEditableKeyTarget(target) {
  return Boolean(target?.closest("input, textarea, select, [contenteditable]:not([contenteditable='false'])"));
}

function checkpointMenuActive() {
  const openMenu = checkpointsEl?.querySelector(".checkpointMenu[open]");
  return Boolean(openMenu);
}

function shouldIgnoreGameKeydown(event) {
  const target = event.target instanceof Element ? event.target : null;
  if (event.defaultPrevented) return true;
  if (isEditableKeyTarget(target)) return true;
  if (checkpointMenuActive()) return true;
  return false;
}

window.addEventListener("keydown", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (shouldIgnoreGameKeydown(event)) return;

  if (
    (event.key === "Enter" || event.key === " ") &&
    target?.closest("button, summary")
  ) {
    return;
  }

  const map = {
    ArrowUp: "up",
    ArrowDown: "down",
    ArrowLeft: "left",
    ArrowRight: "right",
    Enter: "search",
    " ": "search",
    Escape: "menu",
    PageUp: "page_up",
    PageDown: "page_down",
    Home: "home",
    End: "end",
  };
  const button = map[event.key];
  if (button) {
    if (event.repeat && !repeatableButtons.has(button)) return;
    event.preventDefault();
    sendButton(button);
  }
});

connectStream();
connectFrames();
refreshCheckpoints();
setConnection();
