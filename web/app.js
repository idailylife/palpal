const stateEl = document.getElementById("json");
const jsonDetailsEl = document.getElementById("jsonDetails");
const jsonLiveEl = document.getElementById("jsonLive");
const jsonRefreshEl = document.getElementById("jsonRefresh");
const jsonStatusEl = document.getElementById("jsonStatus");
const layoutEl = document.getElementById("layout");
const navigationPaneToggleEl = document.getElementById("navigationPaneToggle");
const agentEl = document.getElementById("agent");
const normalTabEl = document.getElementById("normalTab");
const agentTabEl = document.getElementById("agentTab");
const normalPanelEl = document.getElementById("normalPanel");
const agentPanelEl = document.getElementById("agentPanel");
const agentSessionHeaderEl = document.getElementById("agentSessionHeader");
const agentSessionControlsEl = document.getElementById("agentSessionControls");
const agentTimelineEl = document.getElementById("agentTimeline");
const agentJumpLatestEl = document.getElementById("agentJumpLatest");
const summaryEl = document.getElementById("summary");
const checkpointsEl = document.getElementById("checkpoints");
const objectsEl = document.getElementById("objects");
const navigationEl = document.getElementById("navigation");
const explorationEl = document.getElementById("exploration");
const dialogsEl = document.getElementById("dialogs");
const eventsEl = document.getElementById("events");
const inventoryEl = document.getElementById("inventory");
const equipmentEl = document.getElementById("equipment");
const battleSectionEl = document.getElementById("battleSection");
const battleStatusEl = document.getElementById("battleStatus");
const frameCanvas = document.getElementById("frame");
const fastDialogButton = document.getElementById("fastDialog");
const transitionsButton = document.getElementById("transitionsButton");
const transitionsDialog = document.getElementById("transitionsDialog");
const transitionsClose = document.getElementById("transitionsClose");
const transitionsRoom = document.getElementById("transitionsRoom");
const transitionsContent = document.getElementById("transitionsContent");
const goalsDialog = document.getElementById("goalsDialog");
const goalsClose = document.getElementById("goalsClose");
const goalsSummary = document.getElementById("goalsSummary");
const goalsContent = document.getElementById("goalsContent");
const roomMemoDialog = document.getElementById("roomMemoDialog");
const roomMemoTitle = document.getElementById("roomMemoTitle");
const roomMemoClose = document.getElementById("roomMemoClose");
const roomMemoRoom = document.getElementById("roomMemoRoom");
const roomMemoContent = document.getElementById("roomMemoContent");
const statusChipEls = Object.fromEntries(
  [...document.querySelectorAll("[data-status-kind]")]
    .map((element) => [element.dataset.statusKind, element]),
);
const statusValueEls = {
  state: document.getElementById("stateStatusValue"),
  frames: document.getElementById("framesStatusValue"),
  game: document.getElementById("gameStatusValue"),
  agent: document.getElementById("agentStatusValue"),
};
const disconnectAlertEl = document.getElementById("disconnectAlert");
const disconnectMessageEl = document.getElementById("disconnectMessage");
const disconnectRefreshEl = document.getElementById("disconnectRefresh");
const overlay = document.getElementById("overlay");
const logicalRoomEl = document.getElementById("logicalRoom");
const frameCtx = frameCanvas.getContext("2d");
const ctx = overlay.getContext("2d");
const defaultDocumentTitle = document.title;

let latestState = null;
let selectedRoute = null;
let activeNavigation = null;
let navigationRefreshInFlightKey = null;
let navigationRefreshInFlightToken = null;
let navigationRenderKey = null;
let navigationLatestEnvironmentKey = null;
let navigationEnvironmentChangedAt = 0;
let navigationStableEnvironmentKey = null;
let navigationRefreshTimer = null;
let navigationRouteRequestId = 0;
let navigationDiagnosticKey = null;
let navigationRefreshGeneration = 0;
let navigationRetryIndex = 0;
let navigationRetryDueAt = null;
let navigationRetryRemainingMs = null;
let navigationFailureWarned = false;
let agentNavigationResult = null;
let agentNavigationBase = null;
let agentNavigationBaseEnvironmentKey = null;
let latestLightweightNavigation = null;
let latestLightweightNavigationEnvironmentKey = null;
let explorationRenderKey = null;
let summaryRenderKey = null;
let dialogsRenderKey = null;
let eventsRenderKey = null;
let agentRenderKey = null;
let agentRequestInFlight = false;
let agentExecutionInFlight = false;
let agentLastDecision = null;
let agentError = null;
let agentActionStatus = "";
let agentResultTitlePending = false;
let agentResultTitleTimer = null;
let selectedAgentModel = null;
let agentSession = null;
let agentSessionEvents = [];
let agentEventSeq = 0;
let agentSessionRefreshTimer = null;
let agentSessionRefreshInFlight = false;
let agentSessionRefreshQueued = false;
let agentHasOlderEvents = false;
let agentOldestTurn = null;
let agentOlderEventsLoading = false;
let agentHistoryExpanded = false;
let agentUnread = 0;
let agentSessionHeaderRenderKey = null;
let agentSessionControlsRenderKey = null;
let agentTimelineRenderKey = null;
let agentRenderDeferred = false;
let agentTasks = [];
let agentTaskCounts = { active: 0, completed: 0, abandoned: 0, total: 0 };
let agentTasksLoaded = false;
let agentTasksLoading = false;
let agentTasksError = null;
let agentTasksRequestId = 0;
let goalsDialogTrigger = null;
let roomMemo = null;
let roomMemoRoomId = null;
let roomMemoRecent = [];
let roomMemoLoading = false;
let roomMemoError = null;
let roomMemoRequestId = 0;
let roomMemoDialogTrigger = null;
let memoMode = "room";
let battleMemo = null;
let navigationPaneHidden = localStorage.getItem("palpal.navigationPaneHidden") === "true";
let selectedStateTab = localStorage.getItem("palpal.stateTab") === "agent" ? "agent" : "normal";
let agentMaxTurns = Math.max(1, Math.min(Number(localStorage.getItem("palpal.agentMaxTurns") || 10), 100));
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
const navigationCacheLimit = 32;

function getCachedNavigation(key) {
  const navigation = navigationCache.get(key);
  if (!navigation) return null;
  navigationCache.delete(key);
  navigationCache.set(key, navigation);
  return navigation;
}

function cacheNavigation(key, navigation) {
  if (!key) return;
  navigationCache.delete(key);
  navigationCache.set(key, navigation);
  while (navigationCache.size > navigationCacheLimit) {
    navigationCache.delete(navigationCache.keys().next().value);
  }
}
let stateConnected = false;
let frameConnected = false;
let dialogAdvanceActive = false;
let dialogAdvanceSending = false;
let dialogAdvanceCount = 0;
let dialogAdvanceObservedStartedAt = null;
let reconnectDelaysMs = [1000, 2000, 4000, 8000, 16000, 30000];
let pageLifecycleStopped = false;
const exhaustedTransports = new Set();
const reconnectChannels = new Set();
const repeatableButtons = new Set(["up", "down", "left", "right"]);
const actionCooldownMs = 180;
const dialogAdvanceDelayMs = 500;
const dialogAdvanceMaxPresses = 100;
const jsonUpdateIntervalMs = 1000;
const agentTimelinePageTurns = 5;

function showAgentResultInTitle() {
  clearAgentResultTitle();
  agentResultTitlePending = true;
  let highlighted = false;
  const updateTitle = () => {
    highlighted = !highlighted;
    document.title = `${highlighted ? "🤖" : "✨"} ${defaultDocumentTitle}`;
  };
  updateTitle();
  agentResultTitleTimer = window.setInterval(updateTitle, 700);
}

function clearAgentResultTitle() {
  if (agentResultTitleTimer !== null) {
    window.clearInterval(agentResultTitleTimer);
    agentResultTitleTimer = null;
  }
  if (!agentResultTitlePending && document.title === defaultDocumentTitle) return;
  agentResultTitlePending = false;
  document.title = defaultDocumentTitle;
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    pauseDisconnectedWork();
    return;
  }
  clearAgentResultTitle();
  resumeDisconnectedWork();
});
window.addEventListener("focus", clearAgentResultTitle);
document.addEventListener("pointerdown", clearAgentResultTitle);
document.addEventListener("keydown", clearAgentResultTitle);
window.addEventListener("pagehide", (event) => {
  if (event.persisted) {
    pauseDisconnectedWork();
  } else {
    stopPageConnections();
  }
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) resumeDisconnectedWork();
});
const navigationSettleMs = 400;
const fallbackAgentModels = ["deepseek-v4-pro", "deepseek-v4-flash"];
const hiddenAgentModels = new Set(["deepseek-v4.1-flash-expires-on-0910"]);
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
    }))
    .sort((a, b) => a.id - b.id);
  const approachRequirements = (state?.inventory || [])
    .filter((item) => item?.usable === true && Number(item.amount || 0) - Number(item.amount_in_use || 0) > 0)
    .flatMap((item) => (item.use_effect?.requirements || [])
      .filter((requirement) => requirement?.kind === "face_event_object" && Number(requirement.event_object_id || 0) > 0)
      .map((requirement) => ({
        kind: "face_event_object",
        item_id: Number(item.item_id || 0),
        item_name: String(item.name || ""),
        event_object_id: Number(requirement.event_object_id || 0),
        distance_level: Number(requirement.distance_level || 0),
        distance_limit: Number(requirement.distance_level || 0) * 32 + 16,
        script_entry: Number(requirement.script_entry || 0),
        failure_script_entry: Number(requirement.failure_script_entry || 0),
        conditional: requirement.conditional === true,
        requirements_complete: item.use_effect?.requirements_complete === true,
      })))
    .filter((requirement) => requirement.item_id > 0 && requirement.distance_level >= 0)
    .sort((a, b) => a.event_object_id - b.event_object_id || a.item_id - b.item_id || a.script_entry - b.script_entry);
  return {
    room: {
      scene_id: state?.scene_id ?? null,
      map_id: state?.map_id ?? null,
      logical_room_id: state?.logical_room_id ?? state?._logical_room?.id ?? null,
      logical_room_label: state?.logical_room_label ?? state?._logical_room?.label ?? null,
      key: state?.room?.key ?? null,
      room_key: state?.room?.key ?? null,
      region_key: state?.region_key ?? null,
      legacy_room_id: state?.legacy_room_id ?? state?._logical_room?.legacy_room_id ?? null,
      legacy_nav_room_id: state?.legacy_nav_room_id ?? state?._logical_room?.legacy_nav_room_id ?? null,
    },
    party_position: {
      x: pos.x ?? null,
      y: pos.y ?? null,
    },
    dialog: {
      active: Boolean(dialog.active),
    },
    objects,
    approach_requirements: approachRequirements,
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
    resetNavigationRefreshRetry();
  }
  return key;
}

function resetNavigationRefreshRetry() {
  if (exhaustedTransports.has("navigation")) return;
  if (navigationRefreshTimer !== null) clearTimeout(navigationRefreshTimer);
  navigationRefreshTimer = null;
  navigationRetryIndex = 0;
  navigationRetryDueAt = null;
  navigationRetryRemainingMs = null;
  navigationFailureWarned = false;
  navigationRefreshGeneration += 1;
}

function deferNavigationRefreshForManualInput() {
  navigationEnvironmentChangedAt = performance.now();
  navigationStableEnvironmentKey = null;
  if (navigationRefreshTimer !== null) clearTimeout(navigationRefreshTimer);
  navigationRefreshTimer = null;
  navigationRetryDueAt = null;
  navigationRetryRemainingMs = null;
  navigationRetryIndex = 0;
  navigationFailureWarned = false;
  navigationRefreshGeneration += 1;
  if (latestState) scheduleNavigationRefresh(latestState);
}

function navigationCanRefresh(state) {
  return Boolean(
    state?._navigation &&
    state._navigation.status !== "no_nav_grid" &&
    state?.control?.navigation_ready === true &&
    !state?._debug?.runtime_stale &&
    !sessionRunning()
  );
}

function agentNavigationFromEvent(event) {
  if (
    event?.type !== "tool"
    || event.phase !== "completed"
    || event.details?.name !== "get_navigation"
  ) {
    return null;
  }
  const navigation = event.details?.result?.navigation;
  if (!navigation || typeof navigation !== "object") return null;
  return {
    sessionId: event.session_id,
    seq: Number(event.seq || 0),
    navigation,
  };
}

function restoreAgentNavigationResult() {
  agentNavigationResult = null;
  for (let index = agentSessionEvents.length - 1; index >= 0; index -= 1) {
    const candidate = agentNavigationFromEvent(agentSessionEvents[index]);
    if (candidate) {
      agentNavigationResult = candidate;
      break;
    }
  }
}

function applyAgentNavigation(state, environmentKey = navigationEnvironmentKey(state)) {
  if (!sessionRunning()) return;
  const navigation = agentNavigationResult?.navigation;
  if (
    agentNavigationResult?.sessionId !== agentSession?.session_id
    || navigation?.environment_key !== environmentKey
  ) {
    return;
  }
  const resultsById = new Map(
    (navigation.targets || []).map((target) => [Number(target.id || 0), target])
  );
  const base = agentNavigationBaseEnvironmentKey === environmentKey
    ? agentNavigationBase
    : state._navigation;
  state._navigation = {
    ...base,
    status: navigation.status,
    environment_key: navigation.environment_key,
    targets: (base?.targets || []).map((target) => {
      const result = resultsById.get(Number(target.id || 0));
      return result ? { ...target, ...result } : { ...target, status: "pending" };
    }),
  };
}

function clearAgentNavigationLayer() {
  agentNavigationResult = null;
  if (latestState && agentNavigationBase) latestState._navigation = agentNavigationBase;
  agentNavigationBase = null;
  agentNavigationBaseEnvironmentKey = null;
  navigationEnvironmentChangedAt = performance.now();
  navigationStableEnvironmentKey = null;
  resetNavigationRefreshRetry();
}

function navigationFullReady(state) {
  return Boolean(
    state?._navigation?.environment_key &&
    state._navigation.environment_key === navigationEnvironmentKey(state) &&
    state._navigation.status === "ok"
  );
}

function navigationTargetActionable(state, targetId, itemId = null) {
  if (
    !navigationFullReady(state)
    || state?.control?.navigation_ready !== true
    || !(state?.control?.allowed_actions || []).includes("navigation_step")
    || state?.dialog?.active
    || state?.in_battle
    || state?._debug?.runtime_stale
    || sessionRunning()
  ) {
    return false;
  }
  const target = navigationTargets(state).get(targetId);
  const status = itemId == null
    ? target?.status
    : (target?.affordances || []).find((entry) => Number(entry.item_id || 0) === Number(itemId))?.status;
  return Boolean(target && status && ![
    "pending",
    "unknown",
    "in_dialog",
    "unreachable_static",
    "blocked_by_transition",
    "needs_direction",
    "no_nav_grid",
    "target_inactive",
  ].includes(status));
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
    navigateToTarget(
      Number(goButton.dataset.navGo),
      Number(goButton.dataset.navItem || 0) || null,
    );
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
  const shopEl = event.target.closest("[data-shop-action]");
  if (shopEl && inventoryEl.contains(shopEl)) {
    const action = shopEl.dataset.shopAction;
    const path = action === "buy" ? "/shop/buy" : "/shop/leave";
    const payload = action === "buy"
      ? { item_id: Number(shopEl.dataset.shopItem) }
      : {};
    fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(async (response) => {
        const result = await response.json().catch(() => ({}));
        if (!response.ok || result.ok === false) {
          throw new Error(result.error || `shop action failed: ${response.status}`);
        }
        await refreshState();
      })
      .catch((error) => {
        agentActionStatus = `Shop action failed: ${error instanceof Error ? error.message : String(error)}`;
        if (latestState) renderAgent(latestState);
      });
    return;
  }
  const actionEl = event.target.closest("[data-use-item]");
  if (!actionEl || !inventoryEl.contains(actionEl)) return;
  useInventoryItem(Number(actionEl.dataset.useItem), actionEl.dataset.useTarget)
    .catch((error) => {
      agentActionStatus = `Action failed: ${error instanceof Error ? error.message : String(error)}`;
      if (latestState) renderAgent(latestState);
    });
});

agentEl?.addEventListener("click", (event) => {
  const actionEl = event.target.closest("[data-agent-action]");
  if (!actionEl || !agentEl.contains(actionEl)) return;
  if (actionEl.dataset.agentAction === "decide") {
    askAgent();
  } else if (actionEl.dataset.agentAction === "execute") {
    executeAgentRecommendation();
  } else if (actionEl.dataset.agentAction === "open") {
    selectStateTab("agent", true);
  }
});

agentEl?.addEventListener("change", (event) => {
  const modelEl = event.target.closest("[data-agent-model]");
  if (!modelEl || !agentEl.contains(modelEl)) return;
  selectedAgentModel = modelEl.value;
});

agentEl?.addEventListener("focusout", (event) => {
  if (!event.target.closest("[data-agent-model]") || !agentRenderDeferred) return;
  setTimeout(() => {
    if (agentRenderDeferred && latestState && agentEl.querySelector("[data-agent-model]") !== document.activeElement) {
      renderAgent(latestState);
    }
  }, 0);
});

checkpointsEl?.addEventListener("click", (event) => {
  const dismissEl = event.target.closest("[data-checkpoint-dismiss]");
  if (dismissEl && checkpointsEl.contains(dismissEl)) {
    event.preventDefault();
    closeCheckpointMenu();
    return;
  }
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
  if (dialogAdvanceActive || latestState?._debug?.dialog_advance_status?.running) {
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

function logicalRoomId(state) {
  return state?.logical_room_id ?? state?._logical_room?.id ?? null;
}

function renderTransitionLinks(payload) {
  const transitions = Array.isArray(payload.transitions) ? payload.transitions : [];
  transitionsRoom.textContent = payload.from_logical_room_id || "Current logical room unavailable";
  if (!transitions.length) {
    transitionsContent.innerHTML = '<div class="transitionsMessage">No known transitions from this logical room.</div>';
    return;
  }
  transitionsContent.innerHTML = transitions.map((transition) => {
    const objectId = transition.source_endpoint?.object_id ?? transition.object_id ?? transition.object?.id ?? "-";
    let destination;
    if (transition.intra_room_transition) {
      const endpoint = transition.destination_endpoint || {};
      const position = endpoint.position;
      const coordinate = position?.x != null && position?.y != null
        ? ` @ (${position.x},${position.y})`
        : "";
      const target = endpoint.kind === "confirmed_transition_source" && endpoint.object_id != null
        ? ` #${endpoint.object_id}`
        : "";
      destination = `same room${target}${coordinate}`;
    } else {
      destination = transition.to_logical_room_label || transition.to_logical_room_id || transition.to_room_id || "-";
    }
    return `<div class="transitionLink">
      <strong class="transitionPoint">#${escapeHtml(objectId)}</strong>
      <span class="transitionArrow" aria-hidden="true">→</span>
      <strong class="transitionDestination">${escapeHtml(destination)}</strong>
    </div>`;
  }).join("");
}

async function openTransitionsDialog() {
  if (!transitionsDialog || !transitionsContent) return;
  const requestedRoomId = logicalRoomId(latestState);
  transitionsRoom.textContent = requestedRoomId || "Current logical room unavailable";
  transitionsContent.innerHTML = '<div class="transitionsMessage">Loading known transitions…</div>';
  if (!transitionsDialog.open) transitionsDialog.showModal();
  try {
    const response = await fetch("/navigation/transitions");
    if (!response.ok) throw new Error(`request failed: ${response.status}`);
    const payload = await response.json();
    if (!transitionsDialog.open) return;
    if (requestedRoomId !== logicalRoomId(latestState) || payload.from_logical_room_id !== requestedRoomId) {
      transitionsContent.innerHTML = '<div class="transitionsMessage">The current logical room changed. Close and reopen to refresh.</div>';
      return;
    }
    renderTransitionLinks(payload);
  } catch (error) {
    if (!transitionsDialog.open) return;
    const message = error instanceof Error ? error.message : String(error);
    transitionsContent.innerHTML = `<div class="transitionsError">Could not load transitions: ${escapeHtml(message)}</div><button id="transitionsRetry" type="button">Retry</button>`;
    document.getElementById("transitionsRetry")?.addEventListener("click", openTransitionsDialog, { once: true });
  }
}

transitionsButton?.addEventListener("click", () => {
  transitionsButton.blur();
  openTransitionsDialog();
});
transitionsClose?.addEventListener("click", () => transitionsDialog?.close());
transitionsDialog?.addEventListener("click", (event) => {
  if (event.target === transitionsDialog) transitionsDialog.close();
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
    action_pending: "pending",
    waiting_for_control: "game control",
    no_nav_grid: "no grid",
    no_visible_frontier: "no visible frontier",
    route_blocked: "route blocked",
    state_stale: "stale",
    in_battle: "battle",
    in_dialog: "dialog",
    in_choice: "choice",
    interrupted_by_dialog: "dialog interrupted",
    interrupted_by_choice: "choice interrupted",
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

const transportLabels = {
  navigation: "navigation",
  state: "state",
  frames: "frames",
  agent: "Agent events",
};
const serverControlSelector = [
  "[data-button]",
  "[data-use-item]",
  "[data-shop-action]",
  "[data-nav-go]",
  "[data-agent-action]",
  "[data-session-action]",
  "[data-goals-open]",
  "[data-room-memo-open]",
  "[data-agent-load-earlier]",
  "[data-checkpoint-action]",
  "#fastDialog",
  "#transitionsButton",
  "#transitionsRetry",
  "[data-goals-retry]",
  "[data-room-memo-retry]",
].join(",");

function refreshRequired() {
  return exhaustedTransports.size > 0;
}

function disableServerControls() {
  if (!refreshRequired()) return;
  document.querySelectorAll(serverControlSelector).forEach((control) => {
    if ("disabled" in control) control.disabled = true;
  });
}

function renderDisconnectAlert() {
  if (!disconnectAlertEl || !disconnectMessageEl) return;
  const labels = [...exhaustedTransports].map((name) => transportLabels[name] || name);
  disconnectAlertEl.hidden = labels.length === 0;
  if (!labels.length) return;
  disconnectMessageEl.textContent = `PALPAL disconnected (${labels.join(", ")}). Automatic reconnect has stopped. Make sure ./run is running, then refresh this page.`;
  disableServerControls();
}

function markTransportExhausted(name) {
  exhaustedTransports.add(name);
  renderDisconnectAlert();
  renderStatusBar();
}

disconnectRefreshEl?.addEventListener("click", () => window.location.reload());
new MutationObserver(disableServerControls).observe(document.body, { childList: true, subtree: true });

function setStatusChip(kind, value, tone, detail = value) {
  const chip = statusChipEls[kind];
  const valueEl = statusValueEls[kind];
  if (!chip || !valueEl) return;
  if (valueEl.textContent !== value) valueEl.textContent = value;
  if (chip.dataset.tone !== tone) chip.dataset.tone = tone;
  const title = `${chip.querySelector("span")?.textContent || kind}: ${detail}`;
  if (chip.title !== title) chip.title = title;
}

function stateTransportStatus(state) {
  if (exhaustedTransports.has("state")) return ["Disconnected", "error"];
  if (!stateConnected) return ["Reconnecting", "pending"];
  const stateFile = state?._debug?.state_file || {};
  if (!state || state.status === "waiting_for_game" || !stateFile.available) {
    return ["Waiting", "warning"];
  }
  if (stateFile.stale) return ["Stale", "warning"];
  return ["Live", "live"];
}

function frameTransportStatus(state) {
  if (exhaustedTransports.has("frames")) return ["Disconnected", "error"];
  if (!frameConnected) return ["Reconnecting", "pending"];
  const frameFile = state?._debug?.frame_file || {};
  if (!state || !frameFile.available) return ["Waiting", "warning"];
  if (frameFile.stale) return ["Stale", "warning"];
  return ["Live", "live"];
}

function gameStatus(state) {
  const stateFile = state?._debug?.state_file || {};
  if (!state || state.status === "waiting_for_game" || !stateFile.available || stateFile.stale) {
    return ["Waiting", "pending"];
  }

  const control = state.control || {};
  const phase = control.phase || "unavailable";
  if (state.in_battle || phase === "battle") {
    const battlePhase = state.battle?.phase;
    const labels = {
      command: "Command",
      resolving: "Resolving",
      reward: "Reward",
      ending: "Ending",
      pre_battle: "Starting",
    };
    return [labels[battlePhase] || "Battle", battlePhase === "command" || battlePhase === "reward" ? "live" : "busy"];
  }
  if (state.dialog?.active || state._debug?.dialog_advance_status?.running || dialogAdvanceActive || phase === "dialog") {
    return ["Dialog", "busy"];
  }
  if (state.shop?.active || phase === "shop") return ["Shop", "busy"];
  if (state.entering_scene || phase === "entering_scene") return ["Scene Change", "busy"];
  if (control.owner === "game" || ["action_pending", "game_script"].includes(phase)) {
    return ["Game Busy", "busy"];
  }
  if (phase === "ready" || control.owner === "client") return ["Ready", "live"];
  return ["Waiting", "pending"];
}

function agentStatus(state) {
  const debug = state?._debug || {};
  const snapshotSession = debug.agent_session || {};
  const persistentSession = agentSession || snapshotSession;
  const delegate = snapshotSession.battle_delegate || persistentSession.battle_delegate || {};
  if (["running", "interrupting"].includes(delegate.status)) {
    const count = Number(delegate.instruction_count || 0);
    const limit = Number(delegate.instruction_limit || 100);
    return [`Battle ${count}/${limit}`, "busy", `${delegate.status} · ${delegate.phase || "waiting"}`];
  }

  const transient = debug.agent_status || {};
  if (transient.running || agentRequestInFlight || agentExecutionInFlight) {
    const thinking = ["preparing", "waiting_for_model", "processing_response"].includes(transient.phase);
    return [thinking ? "Thinking" : "Running", "busy", transient.message || transient.phase || "running"];
  }

  const status = persistentSession.status;
  if (status === "running") return ["Running", "busy", persistentSession.phase || "running"];
  if (status === "interrupting") return ["Interrupting", "warning"];
  if (status === "paused") return ["Paused", "warning", persistentSession.stop_reason || "paused"];
  if (transient.error || agentError || status === "error") {
    return ["Error", "error", transient.error || agentError || persistentSession.stop_reason || "error"];
  }
  return ["Idle", "idle"];
}

function renderStatusBar(state = latestState) {
  const [stateValue, stateTone] = stateTransportStatus(state);
  const [frameValue, frameTone] = frameTransportStatus(state);
  const [gameValue, gameTone] = gameStatus(state);
  const [agentValue, agentTone, agentDetail] = agentStatus(state);
  setStatusChip("state", stateValue, stateTone);
  setStatusChip("frames", frameValue, frameTone);
  setStatusChip("game", gameValue, gameTone);
  setStatusChip("agent", agentValue, agentTone, agentDetail || agentValue);
}

function createReconnectChannel(name, options) {
  let socket = null;
  let retryTimer = null;
  let retryIndex = 0;
  let retryDueAt = null;
  let retryRemainingMs = null;
  let generation = 0;
  let started = false;
  let exhausted = false;
  let stopped = false;

  const createSocket = options.createSocket || ((url) => new WebSocket(url));
  const exhaust = options.onExhaust || (() => markTransportExhausted(name));

  function clearRetryTimer() {
    if (retryTimer !== null) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    retryDueAt = null;
  }

  function armRetry() {
    if (stopped || exhausted || socket || retryTimer !== null || retryRemainingMs === null) return;
    if (document.hidden) return;
    const delay = Math.max(0, retryRemainingMs);
    retryRemainingMs = null;
    retryDueAt = performance.now() + delay;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      retryDueAt = null;
      connect();
    }, delay);
  }

  function scheduleRetry() {
    if (stopped || exhausted || socket || retryTimer !== null) return;
    if (retryRemainingMs === null) {
      if (retryIndex >= reconnectDelaysMs.length) {
        exhausted = true;
        exhaust();
        return;
      }
      retryRemainingMs = reconnectDelaysMs[retryIndex];
      retryIndex += 1;
    }
    armRetry();
  }

  function discardSocket() {
    generation += 1;
    const previous = socket;
    socket = null;
    if (!previous) return;
    previous.onopen = null;
    previous.onmessage = null;
    previous.onclose = null;
    previous.onerror = null;
    try {
      previous.close();
    } catch (_) {
      // The socket is already closed.
    }
  }

  function connect() {
    if (stopped || exhausted || socket) return;
    started = true;
    if (document.hidden) {
      if (retryRemainingMs === null) retryRemainingMs = 0;
      return;
    }
    const token = ++generation;
    let nextSocket;
    try {
      nextSocket = createSocket(options.url());
    } catch (error) {
      options.onError?.(error);
      scheduleRetry();
      return;
    }
    socket = nextSocket;
    nextSocket.onopen = () => {
      if (token !== generation || socket !== nextSocket) return;
      retryIndex = 0;
      retryRemainingMs = null;
      clearRetryTimer();
      options.onOpen?.();
    };
    nextSocket.onmessage = (event) => {
      if (token !== generation || socket !== nextSocket) return;
      options.onMessage?.(event);
    };
    nextSocket.onclose = () => {
      if (token !== generation || socket !== nextSocket) return;
      socket = null;
      options.onClose?.();
      scheduleRetry();
    };
    nextSocket.onerror = (error) => {
      if (token !== generation || socket !== nextSocket) return;
      options.onError?.(error);
    };
  }

  function start() {
    if (stopped || exhausted) return;
    started = true;
    if (retryRemainingMs !== null) {
      armRetry();
    } else {
      connect();
    }
  }

  function replace() {
    if (stopped || exhausted) return;
    clearRetryTimer();
    retryIndex = 0;
    retryRemainingMs = null;
    discardSocket();
    start();
  }

  function pause() {
    if (retryTimer === null) return;
    retryRemainingMs = Math.max(0, (retryDueAt || performance.now()) - performance.now());
    clearRetryTimer();
  }

  function resume() {
    if (!started || stopped || exhausted || socket || retryTimer !== null) return;
    if (retryRemainingMs !== null) {
      armRetry();
    } else {
      connect();
    }
  }

  function stop() {
    stopped = true;
    clearRetryTimer();
    retryRemainingMs = null;
    discardSocket();
  }

  const channel = {
    start,
    replace,
    pause,
    resume,
    stop,
    status: () => ({
      exhausted,
      retryIndex,
      retryPending: retryTimer !== null || retryRemainingMs !== null,
      connected: Boolean(socket && socket.readyState === 1),
    }),
  };
  reconnectChannels.add(channel);
  return channel;
}

function pauseNavigationRetry() {
  if (navigationRefreshTimer === null) return;
  if (navigationRetryDueAt !== null) {
    navigationRetryRemainingMs = Math.max(0, navigationRetryDueAt - performance.now());
  }
  clearTimeout(navigationRefreshTimer);
  navigationRefreshTimer = null;
  navigationRetryDueAt = null;
}

function pauseDisconnectedWork() {
  reconnectChannels.forEach((channel) => channel.pause());
  pauseNavigationRetry();
}

function resumeDisconnectedWork() {
  if (pageLifecycleStopped) return;
  reconnectChannels.forEach((channel) => channel.resume());
  if (!exhaustedTransports.has("navigation") && latestState && !navigationFullReady(latestState)) {
    scheduleNavigationRefresh(latestState);
  }
}

function stopPageConnections() {
  pageLifecycleStopped = true;
  reconnectChannels.forEach((channel) => channel.stop());
  if (navigationRefreshTimer !== null) clearTimeout(navigationRefreshTimer);
  navigationRefreshTimer = null;
  navigationRetryDueAt = null;
  navigationRetryRemainingMs = null;
  navigationRefreshGeneration += 1;
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

  const logicalRoom = state.logical_room_label || state._logical_room?.label || "-";
  logicalRoomEl.textContent = logicalRoom;
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
  const existingPanel = checkpointsEl.querySelector(".checkpointMenuPanel");
  const existingList = checkpointsEl.querySelector(".checkpointList");
  const existingLabel = existingInput?.value || "";
  const panelScrollTop = existingPanel?.scrollTop || 0;
  const listScrollTop = existingList?.scrollTop || 0;
  const labelHadFocus = document.activeElement === existingInput;
  const labelSelectionStart = existingInput?.selectionStart ?? existingLabel.length;
  const labelSelectionEnd = existingInput?.selectionEnd ?? labelSelectionStart;
  const stateBusy = Boolean(checkpointBusy) || sessionRunning();
  const startupWaiting = state?.status === "startup_waiting";
  const stableMainGame = state?.control?.phase === "ready"
    && state?.in_main_game !== false
    && !state?._debug?.runtime_stale;
  const canSave = !stateBusy && stableMainGame;
  const canLoad = !stateBusy && !state?._debug?.action_pending && (startupWaiting || stableMainGame);
  // Starting over is a reset, not a checkpoint operation: the opening scene
  // immediately shows dialog, so requiring a stable save point here makes the
  // control appear permanently unavailable after the first start.
  const canStartNew = !stateBusy
    && !state?._debug?.action_pending
    && !state?.in_battle
    && !state?.entering_scene
    && (startupWaiting || state?.control?.owner !== "game")
    && (startupWaiting || !state?._debug?.runtime_stale)
    && !["waiting_for_game", "state_parse_error"].includes(state?.status);
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
      <summary aria-haspopup="dialog">${escapeHtml(buttonText)}</summary>
      <div class="checkpointBackdrop" data-checkpoint-dismiss aria-hidden="true"></div>
      <div class="checkpointMenuPanel" role="dialog" aria-modal="true" aria-labelledby="checkpointDialogTitle">
        <div class="checkpointDialogHeader">
          <h2 id="checkpointDialogTitle">Checkpoints</h2>
          <button class="miniButton" type="button" data-checkpoint-dismiss aria-label="Close checkpoints">Close</button>
        </div>
        <div class="checkpointControls">
          <input id="checkpointLabel" type="text" maxlength="120" placeholder="short label" value="${escapeHtml(existingLabel)}" ${stateBusy ? "disabled" : ""} />
          <button id="checkpointSave" data-checkpoint-action="save" ${canSave ? "" : "disabled"}>${checkpointBusy === "saving" ? "Saving..." : "Save"}</button>
        </div>
        <button class="startupButton" data-checkpoint-action="start-new" ${canStartNew ? "" : "disabled"}>${checkpointBusy === "starting" ? "Starting..." : "Start New Game"}</button>
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
  const nextPanel = checkpointsEl.querySelector(".checkpointMenuPanel");
  const nextList = checkpointsEl.querySelector(".checkpointList");
  if (nextPanel) nextPanel.scrollTop = panelScrollTop;
  if (nextList) nextList.scrollTop = listScrollTop;
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
  return Boolean(state?.dialog?.active)
    && (state?.control?.allowed_actions || []).includes("advance_dialog");
}

function renderFastDialogButton(state) {
  if (!fastDialogButton) return;

  const available = canAdvanceDialog(state);
  const status = state?._debug?.dialog_advance_status || {};
  const serverRunning = Boolean(status.running);
  if (serverRunning) {
    dialogAdvanceObservedStartedAt = status.started_at || dialogAdvanceObservedStartedAt;
  }
  const currentServerStatus = serverRunning ||
    (dialogAdvanceActive && dialogAdvanceObservedStartedAt && status.started_at === dialogAdvanceObservedStartedAt);
  const displayedActive = dialogAdvanceActive || serverRunning;
  const displayedCount = currentServerStatus ? Number(status.presses || 0) : dialogAdvanceCount;
  const displayedMax = currentServerStatus ? Number(status.max_presses || dialogAdvanceMaxPresses) : dialogAdvanceMaxPresses;

  if (dialogAdvanceActive && !dialogAdvanceSending && !available && !serverRunning) {
    dialogAdvanceActive = false;
  }

  fastDialogButton.disabled = refreshRequired()
    || (dialogAdvanceSending ? !displayedActive : (!displayedActive && !available));
  fastDialogButton.classList.toggle("is-running", displayedActive);
  fastDialogButton.textContent = displayedActive
    ? `Advancing ${displayedCount}/${displayedMax}`
    : "Fast Dialog";
}

async function startDialogAdvance() {
  if (!canAdvanceDialog(latestState)) return;
  dialogAdvanceActive = true;
  dialogAdvanceSending = true;
  dialogAdvanceCount = 0;
  dialogAdvanceObservedStartedAt = null;
  renderFastDialogButton(latestState);

  try {
    flashButton("search");
    const result = await advanceDialogRequest({
      max_presses: dialogAdvanceMaxPresses,
      delay_ms: dialogAdvanceDelayMs,
      stop_on_repeat: true,
    });
    dialogAdvanceCount = Number(result.presses || 0);
  } catch (error) {
    console.warn("dialog advance failed", error);
  } finally {
    dialogAdvanceActive = false;
    dialogAdvanceSending = false;
    dialogAdvanceObservedStartedAt = null;
    renderFastDialogButton(latestState);
    refreshState().catch((error) => console.warn("dialog advance refresh failed", error));
  }
}

function stopDialogAdvance() {
  dialogAdvanceActive = false;
  renderFastDialogButton(latestState);
  fetch("/dialog/advance/stop", { method: "POST" }).catch((error) => console.warn("dialog advance stop failed", error));
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
  if (event.type === "party_sprite_changed") {
    return `role #${event.role_id} scene sprite #${event.from_scene_sprite_id} -> #${event.to_scene_sprite_id}`;
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
    event.role_id,
    event.from_scene_sprite_id,
    event.to_scene_sprite_id,
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

function agentVisibleDecision(state) {
  if (agentError) return null;
  if (agentLastDecision) return agentLastDecision;
  const log = state?._debug?.recent_agent_decisions || [];
  const decisionLog = log.filter((entry) => entry?.decision);
  return decisionLog.length ? decisionLog[decisionLog.length - 1].decision : null;
}

function agentRuntimeKey(state) {
  const room = state?.room || {};
  return [
    state?.scene_id ?? "",
    state?.map_id ?? "",
    state?.logical_room_id ?? state?._logical_room?.id ?? "",
    room.key ?? state?.region_key ?? "",
  ].join("/");
}

function stateContextFromState(state) {
  if (!state || state.status === "waiting_for_game" || state.status === "state_parse_error") {
    return {};
  }
  const sceneId = state.scene_id;
  const mapId = state.map_id;
  const room = state.room || {};
  const roomKey = room.key || "unknown";
  const regionKey = state.region_key || roomKey;
  const logicalRoomId = state.logical_room_id || state._logical_room?.id || null;
  const navRoomId = logicalRoomId || (sceneId != null && mapId != null ? `${sceneId}/${mapId}/${roomKey}` : null);
  return {
    scene_id: sceneId,
    map_id: mapId,
    room_id: logicalRoomId || (navRoomId ? `${navRoomId}/${regionKey}` : null),
    logical_room_id: logicalRoomId,
    logical_room_label: state.logical_room_label || state._logical_room?.label || null,
    room_key: roomKey,
    region_key: regionKey,
    nav_room_id: navRoomId,
    legacy_room_id: state.legacy_room_id || state._logical_room?.legacy_room_id || null,
    legacy_nav_room_id: state.legacy_nav_room_id || state._logical_room?.legacy_nav_room_id || null,
  };
}

function contextsMatchValue(left, right) {
  if (left == null || left === "" || right == null || right === "") return true;
  return String(left) === String(right);
}

function agentDecisionContextMatches(decision, state) {
  const context = decision?.state_context;
  if (!context || typeof context !== "object") return true;
  const current = stateContextFromState(state);
  return ["scene_id", "map_id", "logical_room_id", "room_id", "nav_room_id"]
    .every((key) => contextsMatchValue(context[key], current[key]));
}

function agentActionExecutable(action) {
  return [
    "press_buttons", "advance_dialog", "navigation_step", "use_item",
    "equip_item", "shop_buy", "shop_leave",
  ].includes(action?.tool);
}

function agentExecutionDisabledReason(decision, state, agentBusy) {
  const action = decision?.recommended_action;
  if (agentBusy || agentExecutionInFlight) return "Agent is busy.";
  if (activeNavigation) return "Navigation is already running.";
  if (dialogAdvanceSending) return "Dialog advance is already running.";
  if (state?._debug?.action_pending) return "An action is already pending.";
  if (!decision || !action) return "No recommendation to execute.";
  if (!agentDecisionContextMatches(decision, state)) return "Decision is for an older room. Ask Agent again.";
  if (action.tool === "no_op") return "Recommendation has no executable action.";
  if (!(state?.control?.allowed_actions || []).includes(action.tool)) return "Game currently owns control.";
  if (!agentActionExecutable(action)) return `Unsupported action tool: ${action.tool || "unknown"}.`;
  if (action.tool === "navigation_step" && !Number(action.args?.id)) return "Navigation target is missing.";
  if (action.tool === "use_item" && !Number(action.args?.item_id)) return "Inventory item is missing.";
  if (
    action.tool === "equip_item"
    && (!Number(action.args?.item_id) || !Number.isInteger(Number(action.args?.role_id)) || Number(action.args.role_id) < 0)
  ) return "Equipment item or role is missing.";
  if (action.tool === "navigation_step" && !navigationTargetActionable(state, Number(action.args.id))) {
    return "Navigation target is not actionable now.";
  }
  return "";
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

function renderAgentDecision(decision, openState = {}) {
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
    <details class="agentEvidence" ${openState.evidence ? "open" : ""}>
      <summary>Evidence</summary>
      <ul>${evidenceItems(decision.evidence)}</ul>
    </details>
    ${alternatives.length ? `<details class="agentAlternatives" ${openState.alternatives ? "open" : ""}>
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

function agentStatusText(status, executeDisabledReason, decision) {
  if (agentError) return agentError;
  if (agentExecutionInFlight) return "Executing recommended action.";
  if (agentActionStatus) return agentActionStatus;
  if (status?.running) return status.message || "Waiting for agent.";
  if (agentRequestInFlight) return "Waiting for agent.";
  if (decision && executeDisabledReason) return executeDisabledReason;
  if (status?.phase === "completed") return status.message || "Decision ready. Execute manually.";
  if (status?.phase === "error") return status.error || status.message || "Agent request failed.";
  return "Ask Agent for a recommendation.";
}

function renderAgentProgress(status) {
  if (!status?.running) return "";
  const fields = [
    status.backend ? `backend ${status.backend}` : "",
    status.model ? `model ${status.model}` : "",
    status.phase ? `phase ${status.phase}` : "",
    Number(status.round || 0) > 0 ? `round ${status.round}` : "",
    Number(status.tool_calls || 0) > 0 ? `tools ${status.tool_calls}` : "",
    status.current_tool ? `current ${status.current_tool}` : "",
  ].filter(Boolean);
  if (!fields.length) return "";
  return `<div class="agentProgress">${escapeHtml(fields.join(" · "))}</div>`;
}

function agentModelOptions(state) {
  const models = state?._debug?.agent_config?.models;
  const visibleModels = Array.isArray(models)
    ? models.filter((model) => !hiddenAgentModels.has(model))
    : [];
  return visibleModels.length ? visibleModels : fallbackAgentModels;
}

function currentAgentModel(state) {
  const models = agentModelOptions(state);
  const configured = state?._debug?.agent_config?.default_model;
  const running = state?._debug?.agent_status?.model;
  if (!selectedAgentModel || !models.includes(selectedAgentModel)) {
    selectedAgentModel = models.includes(running)
      ? running
      : models.includes(configured)
        ? configured
        : models[0];
  }
  return selectedAgentModel;
}

function renderAgent(state) {
  if (!agentEl) return;
  const log = state._debug?.recent_agent_decisions || [];
  const agentStatus = state._debug?.agent_status || {};
  const persistentSession = state._debug?.agent_session || agentSession || {};
  const historyOpen = Boolean(agentEl.querySelector(".agentHistory")?.open);
  const evidenceOpen = Boolean(agentEl.querySelector(".agentEvidence")?.open);
  const alternativesOpen = Boolean(agentEl.querySelector(".agentAlternatives")?.open);
  const modelFocused = agentEl.querySelector("[data-agent-model]") === document.activeElement;
  const decisionLog = log.filter((entry) => entry?.decision);
  const decision = agentVisibleDecision(state);
  const history = decisionLog.slice(-8).reverse();
  const agentBusy = agentRequestInFlight || agentExecutionInFlight || Boolean(agentStatus.running) || ["running", "interrupting"].includes(persistentSession.status);
  const executeDisabledReason = agentExecutionDisabledReason(decision, state, agentBusy);
  const executeDisabled = Boolean(executeDisabledReason);
  const modelOptions = agentModelOptions(state);
  const selectedModel = currentAgentModel(state);
  const modelSelect = `<select class="agentModelSelect" data-agent-model ${agentBusy ? "disabled" : ""} aria-label="Agent model">
    ${modelOptions.map((model) => `<option value="${escapeHtml(model)}" ${model === selectedModel ? "selected" : ""}>${escapeHtml(model)}</option>`).join("")}
  </select>`;
  const status = agentBusy ? "loading" : agentError ? "error" : decision ? "ready" : "idle";
  const renderKey = JSON.stringify({
    status,
    error: agentError,
    agentActionStatus,
    agentExecutionInFlight,
    executeDisabledReason,
    selectedModel,
    modelOptions,
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
    persistentSession,
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
  const html = `
    <div class="agentControls">
      ${modelSelect}
      <button data-agent-action="decide" ${agentBusy || state.in_battle ? "disabled" : ""}>Decide Once</button>
      <button data-agent-action="execute" ${executeDisabled ? "disabled" : ""}>${agentExecutionInFlight ? "Executing..." : "Execute Action"}</button>
      <button data-agent-action="open">Open Agent Mode</button>
      <span class="agentStatusText">${escapeHtml(agentStatusText(agentStatus, executeDisabledReason, decision))}</span>
    </div>
    <div class="agentProgress">Session ${escapeHtml(agentStatusLabel(persistentSession))} · turn ${Number(persistentSession.turn || 0)}</div>
    ${renderAgentProgress(agentStatus)}
    ${agentError ? renderAgentError(agentError) : renderAgentDecision(decision, { evidence: evidenceOpen, alternatives: alternativesOpen })}
    <details class="agentHistory" ${historyOpen ? "open" : ""}>
      <summary>Decision History <span>${history.length}</span></summary>
      ${history.length ? history.map((entry) => `
        <div class="agentHistoryItem">
          <strong>#${entry.turn ?? "-"} ${escapeHtml(entry.decision?.recommended_intent || "unknown")}</strong>
          <span>${escapeHtml(entry.decision?.summary || "")}</span>
        </div>
      `).join("") : '<div class="navEmpty">No agent decisions yet.</div>'}
    </details>
  `;
  if (agentRenderKey !== renderKey && modelFocused) {
    agentRenderDeferred = true;
    return;
  }
  agentRenderDeferred = false;
  agentRenderKey = setPanelHtml(agentEl, html, renderKey, agentRenderKey);
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
  const shop = state.shop || {};
  const shopActive = shop.active === true;
  if (!items.length && !shopActive) {
    inventoryRenderKey = setPanelHtml(inventoryEl, '<div class="navEmpty">Inventory is empty.</div>', "empty", inventoryRenderKey);
    return;
  }

  const firstRole = state.party?.[0]?.role_id ?? 0;
  const disabled = Boolean(!(state.control?.allowed_actions || []).includes("use_item") || sessionRunning());
  const shopBuyDisabled = sessionRunning()
    || !(state.control?.allowed_actions || []).includes("shop_buy");
  const shopLeaveDisabled = sessionRunning()
    || !(state.control?.allowed_actions || []).includes("shop_leave");
  const renderKey = JSON.stringify({
    items: items.map((item) => [
    item.slot,
    item.item_id,
    item.name,
    item.amount,
    item.amount_in_use,
    item.equipped_count,
    item.flags,
    disabled,
    ]),
    shop: shopActive ? {
      phase: shop.phase,
      cash: state.cash,
      buyDisabled: shopBuyDisabled,
      leaveDisabled: shopLeaveDisabled,
      items: (shop.items || []).map((item) => [
        item.item_id, item.name, item.price, item.affordable, item.owned_count,
      ]),
    } : null,
  });
  const shopHtml = shopActive ? `<div class="shopControls">
    <strong>Shop</strong>
    ${(shop.items || []).map((item) => `
      <div class="inventoryItem">
        <span>${escapeHtml(item.name || `#${item.item_id}`)} · ${item.price ?? 0}</span>
        <button class="miniButton" data-shop-action="buy" data-shop-item="${item.item_id}"
          ${shopBuyDisabled || item.affordable !== true ? "disabled" : ""}>Buy</button>
      </div>
    `).join("")}
    <button class="miniButton" data-shop-action="leave" ${shopLeaveDisabled ? "disabled" : ""}>Leave shop</button>
  </div>` : "";
  inventoryRenderKey = setPanelHtml(inventoryEl, shopHtml + items.map((item) => {
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
    role.scene_sprite_id,
    (role.equipment || []).map((item) => [item.slot_key, item.item_id, item.name]),
  ]));
  equipmentRenderKey = setPanelHtml(equipmentEl, party.map((role) => `
    <div class="equipmentRole">
      <strong>${escapeHtml(role.name || `role #${role.role_id}`)}</strong>
      ${role.scene_sprite_id != null ? `<div><em>scene sprite</em>#${role.scene_sprite_id}</div>` : ""}
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
      (nav?.affordances || []).map((entry) => [entry.item_id, entry.script_entry, entry.status, entry.steps]),
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
        nav?.affordances?.length ? `<span class="tag">item ×${nav.affordances.length}</span>` : "",
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
    inBattle: Boolean(state.in_battle),
    runtimeStale: Boolean(state._debug?.runtime_stale),
    agentRunning: sessionRunning(),
    navigationReady: state.control?.navigation_ready === true,
    navigationAllowed: (state.control?.allowed_actions || []).includes("navigation_step"),
    navigationStatus: state._navigation?.status ?? null,
    fullReady,
    targets: targets.map((target) => [
      target.id,
      target.kind,
      target.status,
      target.steps,
      target.required_direction,
      target.blocker?.id ?? null,
      (target.affordances || []).map((entry) => [entry.item_id, entry.script_entry, entry.status, entry.steps, entry.required_direction]),
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
        displayFullDetails && target.to_semantic_room ? `to ${escapeHtml(target.to_semantic_room)}` : "",
      ]
        .filter(Boolean)
        .join(" · ");
      const goEnabled = navigationTargetActionable(state, target.id) && !activeNavigation;
      const goLabel = isActive
        ? navigationRunText(activeNavigation.status)
        : target.kind === "approach"
          ? "Approach"
          : displayStatus === "transition"
            ? "Enter"
            : "Go";
      const itemButtons = (target.affordances || []).map((affordance) => {
        const itemActive = isActive && activeNavigation?.approachItemId === Number(affordance.item_id || 0);
        const label = itemActive
          ? navigationRunText(activeNavigation.status)
          : `Approach · ${escapeHtml(affordance.item_name || `#${affordance.item_id}`)}`;
        const itemEnabled = navigationTargetActionable(state, target.id, affordance.item_id) && !activeNavigation;
        return `<button class="navGo" data-nav-go="${target.id}" data-nav-item="${affordance.item_id}" ${itemEnabled ? "" : "disabled"}>${label}</button>`;
      }).join("");
      return `<div class="navTarget ${selectedRoute?.target_id === target.id ? "is-selected" : ""}" data-nav-id="${target.id}" role="button" tabindex="0">
        <div class="navTargetSelect">
          <strong>#${target.id}</strong>
          <span>
            <span class="tag ${statusClass(displayStatus)}">${statusText(displayStatus)}</span>
            ${detail ? `<small>${detail}</small>` : ""}
          </span>
        </div>
        ${target.kind === "approach" ? "" : `<button class="navGo" data-nav-go="${target.id}" ${goEnabled ? "" : "disabled"}>${goLabel}</button>`}
        ${itemButtons}
      </div>`;
    })
    .join("");
  navigationRenderKey = renderKey;
}

function scheduleNavigationRefresh(state) {
  if (pageLifecycleStopped || exhaustedTransports.has("navigation") || document.hidden) return;
  if (!navigationCanRefresh(state)) return;
  if (navigationRefreshTimer !== null || navigationRefreshInFlightKey) return;

  if (navigationRetryRemainingMs !== null) {
    const delay = Math.max(0, navigationRetryRemainingMs);
    navigationRetryRemainingMs = null;
    navigationRetryDueAt = performance.now() + delay;
    navigationRefreshTimer = setTimeout(() => {
      navigationRefreshTimer = null;
      navigationRetryDueAt = null;
      if (latestState) scheduleNavigationRefresh(latestState);
    }, delay);
    return;
  }

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
  const cached = getCachedNavigation(key);
  if (cached) {
    if (latestState && latestState._navigation?.environment_key !== key) {
      latestState._navigation = cached;
      renderNavigation(latestState);
      renderObjects(latestState);
      drawOverlay(latestState);
    }
    return;
  }

  refreshNavigationForKey(key);
}

function scheduleNavigationRetry() {
  if (
    pageLifecycleStopped
    || exhaustedTransports.has("navigation")
    || navigationRefreshTimer !== null
    || navigationRefreshInFlightKey
  ) {
    return;
  }
  if (navigationRetryRemainingMs === null) {
    if (navigationRetryIndex >= reconnectDelaysMs.length) {
      markTransportExhausted("navigation");
      return;
    }
    navigationRetryRemainingMs = reconnectDelaysMs[navigationRetryIndex];
    navigationRetryIndex += 1;
  }
  if (!document.hidden && latestState) scheduleNavigationRefresh(latestState);
}

async function refreshNavigationForKey(requestKey) {
  if (
    navigationRefreshInFlightKey
    || pageLifecycleStopped
    || exhaustedTransports.has("navigation")
    || document.hidden
  ) {
    return;
  }
  const requestGeneration = ++navigationRefreshGeneration;
  const requestToken = {};
  navigationRefreshInFlightKey = requestKey;
  navigationRefreshInFlightToken = requestToken;
  let failed = false;
  try {
    const response = await fetch("/navigation");
    if (!response.ok) {
      throw new Error(`navigation failed: ${response.status}`);
    }
    const navigation = await response.json();
    const returnedKey = navigation.environment_key;
    if (returnedKey) cacheNavigation(returnedKey, navigation);
    if (returnedKey && returnedKey !== requestKey) {
      sendNavigationDiagnostic("navigation_key_mismatch", {
        request_key_length: requestKey.length,
        returned_key_length: returnedKey?.length || 0,
        request_key_prefix: requestKey.slice(0, 160),
        returned_key_prefix: returnedKey?.slice(0, 160) || "",
      });
    }
    if (!returnedKey) {
      navigationEnvironmentChangedAt = performance.now();
      navigationStableEnvironmentKey = null;
    }
    if (
      latestState &&
      !sessionRunning() &&
      requestGeneration === navigationRefreshGeneration &&
      returnedKey &&
      returnedKey === navigationStableEnvironmentKey &&
      navigationEnvironmentKey(latestState) === returnedKey
    ) {
      latestState._navigation = navigation;
      renderNavigation(latestState);
      renderObjects(latestState);
      drawOverlay(latestState);
    }
    navigationRetryIndex = 0;
    navigationRetryRemainingMs = null;
    navigationFailureWarned = false;
  } catch (error) {
    failed = true;
    if (!navigationFailureWarned) {
      navigationFailureWarned = true;
      console.warn("navigation unavailable; retrying with bounded backoff", error);
    }
  } finally {
    if (navigationRefreshInFlightToken === requestToken) {
      navigationRefreshInFlightToken = null;
      navigationRefreshInFlightKey = null;
    }
    if (!latestState || navigationFullReady(latestState)) return;
    if (failed && requestGeneration === navigationRefreshGeneration) scheduleNavigationRetry();
    else scheduleNavigationRefresh(latestState);
  }
}

function navigationRunText(status) {
  const labels = {
    starting: "Go...",
    stepping: "Moving",
    facing: "Facing",
    interacting: "Action",
    arrived: "Arrived",
    approached: "Ready",
    action_pending: "Pending",
    waiting_for_control: "Game",
    transition_observing: "Observing",
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

  const status = exploration.status;
  const coverage = explorationCoverage(exploration);
  const renderKey = JSON.stringify({
    status: exploration.status,
    visited: exploration.visited,
    explored: exploration.explored,
    explorable: exploration.explorable,
    frontier: exploration.frontier,
    blocked: exploration.blocked_edges,
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

function drawExplorationRoute(state) {
  const exploration = state._exploration;
  if (!exploration) return;
  const viewport = state.viewport || { x: 0, y: 0 };
  const scaleX = overlay.width / 320;
  const scaleY = overlay.height / 200;

  ctx.save();
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
  if (state.in_battle) return;
  const viewport = state.viewport || { x: 0, y: 0 };
  const scaleX = overlay.width / 320;
  const scaleY = overlay.height / 200;
  const navTargets = navigationTargets(state);
  const hideMapHints = Boolean(state.dialog?.active);

  if (!hideMapHints) {
    drawExplorationRoute(state);
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

function playerBattleView(state) {
  const projected = state?._debug?.battle_player_view;
  if (projected) return projected;
  const battle = state?.battle;
  if (!battle) return null;
  return {
    generation: battle.generation,
    command_generation: battle.command_generation,
    result: battle.result,
    phase: battle.phase,
    current_player_index: battle.current_player_index,
    awaiting_ack: battle.awaiting_ack,
    players: (battle.players || []).map((item) => ({
      index: item.index,
      name: item.name,
      hp: item.hp,
      max_hp: item.max_hp,
      mp: item.mp,
      max_mp: item.max_mp,
    })),
    enemies: (battle.enemies || []).map((item) => ({
      slot_index: item.slot_index,
      present: item.present,
      object_id: item.object_id,
      name: item.name,
    })),
    available_actions: (battle.available_actions || []).map((item) => ({
      action_type: item.action_type,
      object_id: item.object_id,
      name: item.name,
      target_side: item.target_side,
      target_mode: item.target_mode,
      valid_target_indices: item.valid_target_indices,
      submission_target_indices: item.submission_target_indices,
      ...(item.action_type === "magic" && item.mp_cost != null ? { mp_cost: item.mp_cost } : {}),
      ...(item.action_type === "magic" && item.description ? { description: item.description } : {}),
    })),
  };
}

function renderBattle(state) {
  const battle = playerBattleView(state);
  const delegate = state._debug?.agent_session?.battle_delegate || {};
  if (battleSectionEl) battleSectionEl.hidden = !battle && !state.last_battle;
  if (navigationEl?.closest("section")) navigationEl.closest("section").hidden = Boolean(state.in_battle);
  if (explorationEl?.closest("section")) explorationEl.closest("section").hidden = Boolean(state.in_battle);
  if (!battleStatusEl) return;
  const namedActions = (battle?.available_actions || [])
    .filter((item) => item.name)
    .map((item) => {
      const label = `${escapeHtml(item.name)}${item.action_type === "magic" && item.mp_cost != null ? ` (${Number(item.mp_cost)} MP)` : ""}`;
      if (item.action_type !== "magic" || !item.description) return label;
      return `${label}<div class="muted">${escapeHtml(item.description).replace(/\r?\n/g, "<br>")}</div>`;
    });
  const current = battle ? `
    <div><strong>Battle · ${escapeHtml(battle.phase || "resolving")}</strong></div>
    <div>${battle.phase === "resolving" ? "Game acting…" : battle.phase === "command" ? `Waiting for player ${Number(battle.current_player_index || 0)}` : battle.phase === "reward" ? "Reward confirmation" : escapeHtml(battle.result || "")}</div>
    <div>Players: ${(battle.players || []).map((item) => `${escapeHtml(item.name || `#${item.index}`)} ${Number(item.hp || 0)}/${Number(item.max_hp || 0)} HP · ${Number(item.mp || 0)}/${Number(item.max_mp || 0)} MP`).join(" · ")}</div>
    <div>Enemies: ${(battle.enemies || []).filter((item) => item.present).map((item) => escapeHtml(item.name || `#${item.slot_index}`)).join(" · ")}</div>
    ${namedActions.length ? `<div>Available: ${namedActions.join(" · ")}</div>` : ""}
    ${delegate.child_session_id ? `<div>Battle Agent: ${escapeHtml(delegate.status || "-")} · ${escapeHtml(delegate.phase || "-")} · ${Number(delegate.instruction_count || 0)}/${Number(delegate.instruction_limit || 100)}</div>` : ""}
  ` : "";
  const last = !battle && state.last_battle ? `
    <div><strong>Last battle: ${escapeHtml(state.last_battle.result || "-")}</strong></div>
    <div>${(state.last_battle.enemies || []).map((item) => escapeHtml(item.name || "")).filter(Boolean).join(" · ")}</div>
    <div>EXP ${Number(state.last_battle.exp_gained || 0)} · Cash ${Number(state.last_battle.cash_gained || 0)}</div>
  ` : "";
  battleStatusEl.innerHTML = current || last;
}

function render(state) {
  const navKey = updateNavigationEnvironmentKey(state);
  latestLightweightNavigation = state._navigation;
  latestLightweightNavigationEnvironmentKey = navKey;
  if (sessionRunning()) {
    agentNavigationBase = latestLightweightNavigation;
    agentNavigationBaseEnvironmentKey = navKey;
    applyAgentNavigation(state, navKey);
  } else {
    const cachedNavigation = getCachedNavigation(navKey);
    if (cachedNavigation) state._navigation = cachedNavigation;
  }
  latestState = state;
  const nextMemoMode = state.in_battle ? "battle" : "room";
  if (roomMemoDialog?.open && memoMode !== nextMemoMode) {
    memoMode = nextMemoMode;
    battleMemo = null;
    refreshRoomMemo();
  }
  renderStatusBar(state);
  renderJson(state);
  renderSummary(state);
  renderCheckpoints(state);
  renderAgent(state);
  renderAgentSession();
  renderBattle(state);
  renderFastDialogButton(state);
  renderDialogs(state);
  renderEvents(state);
  renderInventory(state);
  renderEquipment(state);
  renderNavigation(state);
  updateExternalControls(state);
  renderExploration(state);
  renderObjects(state);
  drawOverlay(state);
  scheduleNavigationRefresh(state);
}

function selectStateTab(name, persist = false) {
  selectedStateTab = name === "agent" ? "agent" : "normal";
  const agentSelected = selectedStateTab === "agent";
  normalTabEl?.setAttribute("aria-selected", String(!agentSelected));
  agentTabEl?.setAttribute("aria-selected", String(agentSelected));
  if (normalTabEl) normalTabEl.tabIndex = agentSelected ? -1 : 0;
  if (agentTabEl) agentTabEl.tabIndex = agentSelected ? 0 : -1;
  if (normalPanelEl) normalPanelEl.hidden = agentSelected;
  if (agentPanelEl) agentPanelEl.hidden = !agentSelected;
  if (persist) localStorage.setItem("palpal.stateTab", selectedStateTab);
  if (agentSelected) {
    agentUnread = 0;
    renderAgentSession();
    if (agentTimelineEl) agentTimelineEl.scrollTop = agentTimelineEl.scrollHeight;
  }
}

function setupStateTabs() {
  const tabs = [normalTabEl, agentTabEl].filter(Boolean);
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => selectStateTab(tab === agentTabEl ? "agent" : "normal", true));
    tab.addEventListener("keydown", (event) => {
      let index = tabs.indexOf(tab);
      if (event.key === "ArrowRight") index = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") index = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") index = 0;
      else if (event.key === "End") index = tabs.length - 1;
      else return;
      event.preventDefault();
      tabs[index].focus();
      selectStateTab(tabs[index] === agentTabEl ? "agent" : "normal", true);
    });
  });
  selectStateTab(selectedStateTab);
}

function setNavigationPaneHidden(hidden, persist = false) {
  navigationPaneHidden = Boolean(hidden);
  layoutEl?.classList.toggle("navigation-hidden", navigationPaneHidden);
  navigationPaneToggleEl?.setAttribute("aria-expanded", String(!navigationPaneHidden));
  if (navigationPaneToggleEl) {
    navigationPaneToggleEl.textContent = navigationPaneHidden ? "Show sidebar" : "Hide sidebar";
  }
  if (persist) localStorage.setItem("palpal.navigationPaneHidden", String(navigationPaneHidden));
}

navigationPaneToggleEl?.addEventListener("click", () => {
  setNavigationPaneHidden(!navigationPaneHidden, true);
});

function sessionRunning() {
  const status = agentSession?.status || latestState?._debug?.agent_session?.status;
  return status === "running" || status === "interrupting";
}

function setAgentSession(nextSession) {
  if (!nextSession) return false;
  const wasRunning = sessionRunning();
  const changed = Boolean(agentSession?.session_id && agentSession.session_id !== nextSession.session_id);
  agentSession = nextSession;
  if (changed) {
    agentSessionEvents = [];
    agentEventSeq = 0;
    agentHasOlderEvents = false;
    agentOldestTurn = null;
    agentOlderEventsLoading = false;
    agentHistoryExpanded = false;
    agentUnread = 0;
    agentNavigationResult = null;
  }
  const running = sessionRunning();
  if (!wasRunning && running) {
    if (navigationRefreshTimer !== null) clearTimeout(navigationRefreshTimer);
    navigationRefreshTimer = null;
    navigationRefreshInFlightKey = null;
    navigationRefreshInFlightToken = null;
    navigationRefreshGeneration += 1;
    if (latestState) {
      const environmentKey = navigationEnvironmentKey(latestState);
      agentNavigationBase = latestLightweightNavigationEnvironmentKey === environmentKey
        ? latestLightweightNavigation
        : latestState._navigation;
      agentNavigationBaseEnvironmentKey = environmentKey;
      latestState._navigation = agentNavigationBase;
      applyAgentNavigation(latestState, agentNavigationBaseEnvironmentKey);
    }
  } else if (wasRunning && !running) {
    clearAgentNavigationLayer();
  }
  renderStatusBar();
  if (latestState && wasRunning !== running) {
    renderCheckpoints(latestState);
    renderFastDialogButton(latestState);
    renderInventory(latestState);
    renderNavigation(latestState);
    updateExternalControls(latestState);
    if (!running) scheduleNavigationRefresh(latestState);
  }
  return changed;
}

function agentEventTurn(event) {
  return Math.max(0, Number(event?.turn || 0));
}

function mergeAgentSessionEvents(events) {
  const merged = new Map();
  [...events, ...agentSessionEvents].forEach((event) => {
    merged.set(Number(event.seq || 0), event);
  });
  return [...merged.values()].sort((left, right) => Number(left.seq || 0) - Number(right.seq || 0));
}

function trimAgentTimelineToLatestTurns() {
  const turns = [...new Set(agentSessionEvents.map(agentEventTurn).filter((turn) => turn > 0))]
    .sort((left, right) => left - right);
  if (turns.length <= agentTimelinePageTurns) return;
  const keptTurns = new Set(turns.slice(-agentTimelinePageTurns));
  agentSessionEvents = agentSessionEvents.filter((event) => keptTurns.has(agentEventTurn(event)));
  agentOldestTurn = Math.min(...keptTurns);
  agentHasOlderEvents = true;
}

async function fetchAgentEventTurnPage(beforeTurn = null) {
  const query = new URLSearchParams({ turn_limit: String(agentTimelinePageTurns) });
  if (beforeTurn != null) query.set("before_turn", String(beforeTurn));
  const response = await fetch(`/agent/session/events?${query}`);
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(result.error || "session events unavailable");
  return result;
}

async function loadLatestAgentEvents() {
  const expectedSessionId = agentSession?.session_id;
  const result = await fetchAgentEventTurnPage();
  if (!expectedSessionId || expectedSessionId !== agentSession?.session_id || result.session_id !== expectedSessionId) return false;
  agentSessionEvents = result.events || [];
  agentHasOlderEvents = Boolean(result.has_more);
  agentOldestTurn = result.oldest_turn == null ? null : Number(result.oldest_turn);
  agentHistoryExpanded = false;
  agentEventSeq = agentSessionEvents.reduce((value, event) => Math.max(value, Number(event.seq || 0)), 0);
  restoreAgentNavigationResult();
  if (latestState && sessionRunning()) {
    if (agentNavigationBaseEnvironmentKey !== navigationEnvironmentKey(latestState)) {
      agentNavigationBase = latestState._navigation;
      agentNavigationBaseEnvironmentKey = navigationEnvironmentKey(latestState);
    }
    latestState._navigation = agentNavigationBase;
    applyAgentNavigation(latestState, agentNavigationBaseEnvironmentKey);
    renderNavigation(latestState);
    renderObjects(latestState);
    drawOverlay(latestState);
  }
  agentTimelineRenderKey = null;
  return true;
}

async function loadEarlierAgentEvents() {
  if (!agentHasOlderEvents || agentOldestTurn == null || agentOlderEventsLoading) return;
  const expectedSessionId = agentSession?.session_id;
  const beforeTurn = agentOldestTurn;
  agentOlderEventsLoading = true;
  const loadingButton = agentTimelineEl?.querySelector("[data-agent-load-earlier]");
  if (loadingButton) {
    loadingButton.disabled = true;
    loadingButton.textContent = "Loading earlier turns…";
  }
  try {
    const result = await fetchAgentEventTurnPage(beforeTurn);
    if (!expectedSessionId || expectedSessionId !== agentSession?.session_id || result.session_id !== expectedSessionId) return;
    const anchorScrollHeight = agentTimelineEl.scrollHeight;
    const anchorScrollTop = agentTimelineEl.scrollTop;
    agentSessionEvents = mergeAgentSessionEvents(result.events || []);
    agentHasOlderEvents = Boolean(result.has_more);
    agentOldestTurn = result.oldest_turn == null ? agentOldestTurn : Number(result.oldest_turn);
    agentHistoryExpanded = true;
    agentTimelineRenderKey = null;
    renderAgentTimeline({ anchorScrollHeight, anchorScrollTop });
  } catch (error) {
    console.warn("earlier agent turns unavailable", error);
    const retryButton = agentTimelineEl?.querySelector("[data-agent-load-earlier]");
    if (retryButton) retryButton.textContent = "Retry loading earlier turns";
  } finally {
    agentOlderEventsLoading = false;
    const button = agentTimelineEl?.querySelector("[data-agent-load-earlier]");
    if (button) {
      button.disabled = false;
      if (button.textContent === "Loading earlier turns…") button.textContent = "Load earlier turns";
    }
  }
}

function updateExternalControls(state) {
  const running = sessionRunning();
  const allowed = new Set(state?.control?.allowed_actions || []);
  document.querySelectorAll("[data-button], [data-use-item], [data-shop-action], [data-nav-go]").forEach((button) => {
    const action = button.matches("[data-button]")
      ? "press_buttons"
      : button.matches("[data-use-item]")
        ? "use_item"
        : button.matches("[data-shop-action]")
          ? (button.dataset.shopAction === "buy" ? "shop_buy" : "shop_leave")
        : "navigation_step";
    if (running || !allowed.has(action)) {
      if (!button.dataset.controlDisabled) {
        button.dataset.controlDisabled = button.disabled ? "already" : "control";
      }
      button.disabled = true;
    } else if (button.dataset.controlDisabled) {
      if (button.dataset.controlDisabled === "control") button.disabled = false;
      delete button.dataset.controlDisabled;
    }
  });
  if (fastDialogButton && (running || !allowed.has("advance_dialog"))) fastDialogButton.disabled = true;
  disableServerControls();
}

function agentStatusLabel(session) {
  const status = session?.status || "loading";
  const stopReasonLabels = {
    agent_wait: "agent requested wait",
    replan_exhausted: "no executable recommendation",
  };
  const stopReason = session?.stop_reason;
  const reason = stopReason ? ` · ${stopReasonLabels[stopReason] || stopReason}` : "";
  return `${status}${reason}`;
}

function renderAgentSession() {
  if (!agentSessionHeaderEl || !agentSessionControlsEl || !agentTimelineEl) return;
  const session = agentSession || latestState?._debug?.agent_session || {};
  const usage = session.usage || {};
  const lastUsage = session.last_request_usage || {};
  const providerUsage = session.provider_usage || {};
  const providerScopes = Object.values(providerUsage.scopes || {});
  const trackedTokens = providerScopes.reduce((sum, scope) => sum + Number(scope?.total_tokens || 0), 0);
  const trackedInput = providerScopes.reduce((sum, scope) => sum + Number(scope?.input_tokens || 0), 0);
  const trackedCached = providerScopes.reduce((sum, scope) => sum + Number(scope?.cached_tokens || 0), 0);
  const usageDetailsComplete = providerScopes.length > 0 && providerScopes.every((scope) => scope?.details_complete === true);
  const cacheRate = trackedInput > 0 ? `${(trackedCached * 100 / trackedInput).toFixed(1)}%${usageDetailsComplete ? "" : " · partial"}` : "-";
  const providerLast = providerUsage.last_request || {};
  const lastCompaction = session.last_compaction || null;
  const running = ["running", "interrupting"].includes(session.status);
  const pending = Boolean(session.pending_decision);
  const battleDelegate = session.battle_delegate || {};
  const inBattle = Boolean(latestState?.in_battle);
  const headerRenderKey = JSON.stringify([
    session.status,
    session.stop_reason,
    session.model,
    session.turn,
    session.run_turn,
    session.max_turns,
    usage.total_tokens,
    providerUsage,
    lastUsage.total_tokens,
    session.last_prompt_tokens,
    lastCompaction,
    session.compactions,
    session.game_instance_id,
    session.parent_session_id,
    session.checkpoint_id,
    session.restored_from_session_id,
    battleDelegate.status,
    battleDelegate.phase,
    battleDelegate.instruction_count,
  ]);
  if (agentSessionHeaderRenderKey !== headerRenderKey) {
    agentSessionHeaderEl.innerHTML = `
      <span class="agentSessionStatus is-${escapeHtml(session.status || "loading")}">${escapeHtml(agentStatusLabel(session))}</span>
      <dl>
        <div><dt>Model</dt><dd>${escapeHtml(session.model || "-")}</dd></div>
        <div><dt>Turn</dt><dd>${Number(session.turn || 0)}</dd></div>
        <div><dt>Run</dt><dd>${Number(session.run_turn || 0)}/${Number(session.max_turns || agentMaxTurns)}</dd></div>
        <div><dt>Tracked tokens</dt><dd>${Number(trackedTokens || usage.total_tokens || 0).toLocaleString()}${providerUsage.incomplete_before_tracking ? " · partial" : ""}</dd></div>
        <div><dt>Last run</dt><dd>${Number(lastUsage.total_tokens || 0).toLocaleString()}</dd></div>
        <div><dt>Tracking since</dt><dd>${escapeHtml(providerUsage.tracking_started_at || "-")}</dd></div>
        <div><dt>Cache rate</dt><dd>${escapeHtml(cacheRate)}</dd></div>
        <div><dt>Last request</dt><dd>${escapeHtml(providerLast.scope || "-")}${providerLast.total_tokens == null ? "" : ` · ${Number(providerLast.total_tokens).toLocaleString()}`}</dd></div>
        <div><dt>Last prompt</dt><dd>${session.last_prompt_tokens == null ? "-" : Number(session.last_prompt_tokens).toLocaleString()}</dd></div>
        <div><dt>Last compact</dt><dd>${lastCompaction ? `${Number(lastCompaction.trigger_prompt_tokens || 0).toLocaleString()} → ~${Number(lastCompaction.replacement_estimated_tokens || 0).toLocaleString()} · ${Number(lastCompaction.retained_cycles || 0)} cycle${Number(lastCompaction.retained_cycles || 0) === 1 ? "" : "s"}` : "-"}</dd></div>
        <div><dt>Compactions</dt><dd>${Number(session.compactions || 0)}</dd></div>
        <div><dt>Lineage</dt><dd>${escapeHtml(session.checkpoint_id ? `checkpoint ${session.checkpoint_id}` : session.parent_session_id ? "session branch" : "game")}</dd></div>
        ${battleDelegate.child_session_id ? `<div><dt>Battle child</dt><dd>${escapeHtml(battleDelegate.status || "-")} · ${escapeHtml(battleDelegate.phase || "-")} · ${Number(battleDelegate.instruction_count || 0)}/${Number(battleDelegate.instruction_limit || 100)}</dd></div>` : ""}
      </dl>`;
    agentSessionHeaderRenderKey = headerRenderKey;
  }
  const model = selectedAgentModel || session.model || fallbackAgentModels[0];
  // Keep native form controls mounted across high-frequency state updates.
  // Replacing an open <select> closes its popup before the user can choose.
  const controlsRenderKey = JSON.stringify([running, pending, Boolean(session.turn), inBattle]);
  if (agentSessionControlsRenderKey !== controlsRenderKey) {
    agentSessionControlsEl.innerHTML = `
      <label>Model<select data-session-model ${running ? "disabled" : ""}>${fallbackAgentModels.map((item) => `<option ${item === model ? "selected" : ""}>${escapeHtml(item)}</option>`).join("")}</select></label>
      <label>Max turns<input data-session-turns type="number" min="1" max="100" value="${agentMaxTurns}" ${running ? "disabled" : ""} /></label>
      <div class="agentSessionButtons">
        <button data-session-action="run" ${running ? "disabled" : ""}>${session.turn ? "Ask Agent / Resume" : "Ask Agent"}</button>
        <button data-session-action="interrupt" ${running ? "" : "disabled"}>Interrupt</button>
        <button data-session-action="decide" ${running || inBattle ? "disabled" : ""}>Decide Once</button>
        <button data-session-action="execute" ${running || !pending ? "disabled" : ""}>Execute</button>
        <button data-session-action="reset" ${running ? "disabled" : ""}>Reset Session</button>
        <button data-goals-open>Goals (<span data-goals-count>…</span>)</button>
        <button class="roomMemoButton" data-room-memo-open>${inBattle ? "Battle Memo" : "Room Memo"}</button>
      </div>`;
    agentSessionControlsRenderKey = controlsRenderKey;
  }
  renderGoalsButtonCount();
  if (roomMemoDialog?.open) {
    roomMemoDialogTrigger = agentSessionControlsEl.querySelector("[data-room-memo-open]") || roomMemoDialogTrigger;
  }
  renderAgentTimeline();
}

function renderGoalsButtonCount() {
  const countEl = agentSessionControlsEl?.querySelector("[data-goals-count]");
  if (!countEl) return;
  countEl.textContent = agentTasksLoaded ? String(Number(agentTaskCounts.active || 0)) : "…";
}

function renderGoalsDialog() {
  if (!goalsSummary || !goalsContent) return;
  if (agentTasksLoading && !agentTasksLoaded) {
    goalsSummary.textContent = "Loading current tool tasks…";
    goalsContent.innerHTML = '<div class="goalsMessage">Loading goals…</div>';
    return;
  }
  if (agentTasksError) {
    goalsSummary.textContent = "Goals unavailable";
    goalsContent.innerHTML = `<div class="goalsError">${escapeHtml(agentTasksError)}<br><button class="goalsRetry" type="button" data-goals-retry>Retry</button></div>`;
    return;
  }
  const counts = agentTaskCounts;
  goalsSummary.textContent = `${Number(counts.active || 0)} active · ${Number(counts.dormant || 0)} dormant · ${Number(counts.completed || 0)} completed · ${Number(counts.abandoned || 0)} abandoned`;
  if (!agentTasks.length) {
    goalsContent.innerHTML = '<div class="goalsEmpty">The Agent has not created any tool tasks yet.</div>';
    return;
  }
  const labels = { active: "Active", dormant: "Dormant", completed: "Completed", abandoned: "Abandoned" };
  goalsContent.innerHTML = ["active", "dormant", "completed", "abandoned"].map((status) => {
    const tasks = agentTasks.filter((task) => task.status === status);
    return `<section class="goalsGroup is-${status}">
      <div class="goalsGroupHeader"><h3>${labels[status]}</h3><span>${tasks.length}</span></div>
      ${tasks.length ? tasks.map((task) => `<article class="goalCard">
        <strong>${escapeHtml(task.description || task.id || "Untitled goal")}</strong>
        <span class="goalStatus is-${status}">${labels[status]}</span>
        <span class="goalPriority">Priority ${Number(task.priority || 0)}</span>
      </article>`).join("") : '<div class="goalsEmpty">None</div>'}
    </section>`;
  }).join("");
}

async function refreshAgentTasks() {
  const requestId = ++agentTasksRequestId;
  agentTasksLoading = true;
  agentTasksError = null;
  if (goalsDialog?.open) renderGoalsDialog();
  try {
    const response = await fetch("/agent/tasks");
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) throw new Error(result.error || `goals unavailable: ${response.status}`);
    if (requestId !== agentTasksRequestId) return;
    agentTasks = Array.isArray(result.tasks) ? result.tasks : [];
    agentTaskCounts = { active: 0, completed: 0, abandoned: 0, total: 0, ...(result.counts || {}) };
    agentTasksLoaded = true;
  } catch (error) {
    if (requestId !== agentTasksRequestId) return;
    agentTasksError = error instanceof Error ? error.message : String(error);
  } finally {
    if (requestId !== agentTasksRequestId) return;
    agentTasksLoading = false;
    renderGoalsButtonCount();
    if (goalsDialog?.open) renderGoalsDialog();
  }
}

function openGoalsDialog(trigger) {
  goalsDialogTrigger = trigger || document.activeElement;
  if (!goalsDialog?.open) goalsDialog?.showModal();
  refreshAgentTasks();
}

function closeGoalsDialog() {
  if (goalsDialog?.open) goalsDialog.close();
}

function renderRoomMemoDialog() {
  if (!roomMemoRoom || !roomMemoContent) return;
  const battleMode = memoMode === "battle";
  if (roomMemoTitle) roomMemoTitle.textContent = battleMode ? "Battle Memo" : "Room Memo";
  if (roomMemoClose) roomMemoClose.setAttribute("aria-label", battleMode ? "Close battle memo" : "Close room memo");
  if (battleMode) {
    const enemies = battleMemo?.current_enemies || [];
    roomMemoRoom.textContent = battleMemo
      ? `${battleMemo.is_boss ? "Boss · " : ""}${enemies.length} current enem${enemies.length === 1 ? "y" : "ies"}`
      : "Current encounter";
    if (roomMemoLoading) {
      roomMemoContent.innerHTML = '<div class="goalsMessage">Loading battle memo…</div>';
      return;
    }
    if (roomMemoError) {
      roomMemoContent.innerHTML = `<div class="goalsError">${escapeHtml(roomMemoError)}<br><button class="goalsRetry" type="button" data-room-memo-retry>Retry</button></div>`;
      return;
    }
    const memories = battleMemo?.enemy_memories || [];
    const support = battleMemo?.support_experience || [];
    const outcomes = battleMemo?.recent_outcomes || [];
    if (!memories.length && !support.length && !outcomes.length) {
      roomMemoContent.innerHTML = '<div class="goalsEmpty">尚无玩家可见的战斗经验</div>';
      return;
    }
    const changeText = (sample) => {
      const numbers = (sample.numbers || []).map((number) => `${number.name || number.side || "target"} ${number.stat || "value"} ${number.kind || "change"} ${Number(number.amount || 0)}`);
      const enemy = (sample.enemy_changes || []).map((change) => `${change.name || `#${change.object_id}`} ${change.kind || "change"} ${Number(change.amount || 0)}`);
      const party = (sample.player_changes || []).flatMap((change) => {
        const values = [];
        if (change.hp && change.hp.before !== change.hp.after) values.push(`HP ${Number(change.hp.before)}→${Number(change.hp.after)}/${Number(change.hp.max || 0)}`);
        if (change.mp && change.mp.before !== change.mp.after) values.push(`MP ${Number(change.mp.before)}→${Number(change.mp.after)}/${Number(change.mp.max || 0)}`);
        return values;
      });
      return [...(numbers.length ? numbers : enemy), ...party].join(" · ") || (sample.no_numeric_change_observed ? "No numeric change observed" : "Visible status change");
    };
    const actionCard = (entry) => {
      const samples = entry.samples || [];
      const latest = samples[samples.length - 1] || {};
      const mp = latest.mp_cost == null ? "" : ` · ${Number(latest.mp_cost)} MP`;
      return `<article class="roomMemoCard"><strong>${escapeHtml(entry.actor_name || `Role #${entry.actor_role_id}`)} · ${escapeHtml(entry.name || entry.action_type || "action")}${mp}</strong><p>${escapeHtml(changeText(latest))}</p><div class="muted">${samples.length} visible sample${samples.length === 1 ? "" : "s"}</div></article>`;
    };
    const enemyCards = memories.map((memory) => {
      const hp = memory.hp_estimate || { status: "unknown" };
      const hpText = hp.status === "estimated"
        ? `HP ${Number(hp.lower_exclusive || 0)} < HP ≤ ${Number(hp.upper_inclusive || 0)} (${Number(hp.sample_count || 0)} samples)`
        : hp.status === "inconsistent" ? `HP samples inconsistent (${Number(hp.sample_count || 0)})` : "HP unknown";
      const actions = (memory.player_actions || []).map(actionCard).join("");
      const effects = (memory.enemy_effects || []).map((sample) => `<article class="roomMemoCard"><strong>Enemy visible effect</strong><p>${escapeHtml(changeText(sample))}</p></article>`).join("");
      return `<section class="roomMemoSection"><h3>${escapeHtml(memory.name || `Enemy #${memory.object_id}`)} ×${Number(memory.current_count || 1)}</h3><p>${escapeHtml(hpText)}</p>${actions || '<div class="goalsEmpty">No player action samples.</div>'}${effects || '<div class="goalsEmpty">No enemy visible-effect samples.</div>'}</section>`;
    }).join("");
    const supportCards = support.map(actionCard).join("");
    const outcomeCards = outcomes.map((sample) => `<article class="roomMemoCard"><strong>${escapeHtml(sample.actor?.name || sample.actor_side || "Actor")} · ${escapeHtml(sample.name || sample.action_type || "action")}</strong><p>${escapeHtml(changeText(sample))}</p></article>`).join("");
    roomMemoContent.innerHTML = `
      <section class="roomMemoSection"><h3>Current visible outcomes</h3>${outcomeCards || '<div class="goalsEmpty">No completed visible action yet.</div>'}</section>
      ${enemyCards}
      <section class="roomMemoSection"><h3>Support actions</h3>${supportCards || '<div class="goalsEmpty">No support-action samples.</div>'}</section>`;
    return;
  }
  roomMemoRoom.textContent = roomMemoRoomId || "Current room unavailable";
  if (roomMemoLoading) {
    roomMemoContent.innerHTML = '<div class="goalsMessage">Loading room memo…</div>';
    return;
  }
  if (roomMemoError) {
    roomMemoContent.innerHTML = `<div class="goalsError">${escapeHtml(roomMemoError)}<br><button class="goalsRetry" type="button" data-room-memo-retry>Retry</button></div>`;
    return;
  }
  const memoCard = (memo, showRoom = false) => `<article class="roomMemoCard">
    ${showRoom ? `<div class="roomMemoCardRoom">${escapeHtml(memo.room_id || "Unknown room")}</div>` : ""}
    <p>${escapeHtml(memo.text || "")}</p>
    <dl>
      <div><dt>Source</dt><dd>${escapeHtml(memo.source || "agent_annotation")}</dd></div>
      <div><dt>Updated turn</dt><dd>${Number(memo.updated_agent_turn || 0)}</dd></div>
    </dl>
  </article>`;
  const currentContent = !roomMemoRoomId
    ? '<div class="goalsEmpty">A stable logical room is not available yet.</div>'
    : roomMemo
      ? memoCard(roomMemo)
      : '<div class="goalsEmpty">The Agent has not written a memo for this room.</div>';
  const recentContent = roomMemoRecent.length
    ? `<div class="roomMemoRecentList">${roomMemoRecent.map((memo) => memoCard(memo, true)).join("")}</div>`
    : '<div class="goalsEmpty">The Agent has not written memos for other rooms yet.</div>';
  roomMemoContent.innerHTML = `
    <section class="roomMemoSection">
      <h3>Current room</h3>
      ${currentContent}
    </section>
    <section class="roomMemoSection">
      <div class="roomMemoSectionHeader"><h3>Recently added</h3><span>${roomMemoRecent.length} / 5</span></div>
      ${recentContent}
    </section>`;
}

async function refreshRoomMemo() {
  const requestId = ++roomMemoRequestId;
  const requestedMode = memoMode;
  roomMemoLoading = true;
  roomMemoError = null;
  renderRoomMemoDialog();
  try {
    const response = await fetch(requestedMode === "battle" ? "/agent/battle-memo" : "/agent/room-memo");
    const result = await response.json().catch(() => ({}));
    if (requestId !== roomMemoRequestId || requestedMode !== memoMode) return;
    if (requestedMode === "battle" && result.status === "not_in_battle") {
      memoMode = "room";
      refreshRoomMemo();
      return;
    }
    if (!response.ok || !result.ok) throw new Error(result.error || `${requestedMode} memo unavailable: ${response.status}`);
    if (requestedMode === "battle") {
      battleMemo = result;
    } else {
      roomMemoRoomId = result.room_id || null;
      roomMemo = result.memo || null;
      roomMemoRecent = Array.isArray(result.recent_memos) ? result.recent_memos.slice(0, 5) : [];
    }
  } catch (error) {
    if (requestId !== roomMemoRequestId || requestedMode !== memoMode) return;
    roomMemoError = error instanceof Error ? error.message : String(error);
  } finally {
    if (requestId !== roomMemoRequestId || requestedMode !== memoMode) return;
    roomMemoLoading = false;
    renderRoomMemoDialog();
  }
}

function openRoomMemoDialog(trigger) {
  roomMemoDialogTrigger = trigger || document.activeElement;
  memoMode = latestState?.in_battle ? "battle" : "room";
  if (!roomMemoDialog?.open) roomMemoDialog?.showModal();
  refreshRoomMemo();
}

function closeRoomMemoDialog() {
  if (roomMemoDialog?.open) roomMemoDialog.close();
}

function restoreRoomMemoFocus(trigger) {
  const focusTarget = trigger?.isConnected
    ? trigger
    : agentSessionControlsEl?.querySelector("[data-room-memo-open]");
  focusTarget?.focus?.();
}

function eventIcon(event) {
  if (event.type === "error" || event.phase === "failed") return "×";
  if (["pause", "interrupt"].includes(event.type) || event.phase === "stopped") return "Ⅱ";
  if (event.phase === "retry") return "↻";
  if (["completed", "observed", "ready"].includes(event.phase)) return "✓";
  if (["request", "progress", "validating", "executing", "deciding"].includes(event.phase)) return "●";
  return "·";
}

const battleTerminalPhases = new Set(["completed", "failed", "stopped"]);
const battleTerminalStatuses = new Set(["completed", "failed", "stopped"]);

function battleDelegateMetadata(event) {
  const metadata = event?.details?.battle_delegate;
  return metadata && typeof metadata === "object" && metadata.group_id ? metadata : null;
}

function agentEventVisibleDetails(event) {
  if (!event?.details || typeof event.details !== "object") return null;
  const details = Object.fromEntries(
    Object.entries(event.details).filter(([key]) => key !== "battle_delegate")
  );
  return Object.keys(details).length ? details : null;
}

function isNavigationSteppingEvent(event) {
  return event?.type === "navigation"
    && event.phase === "progress"
    && event.details?.result?.status === "stepping";
}

function agentTimelineItems(events) {
  const items = [];
  events.forEach((event) => {
    // Provider usage accounting is redundant telemetry (already shown in the Agent
    // session header) and clutters the timeline, splitting Battle Agent groups.
    if (event?.type === "usage") return;
    const metadata = battleDelegateMetadata(event);
    const previous = items.at(-1);
    if (
      !metadata
      && isNavigationSteppingEvent(event)
      && previous?.kind === "event"
      && isNavigationSteppingEvent(previous.event)
    ) {
      previous.event = event;
      return;
    }
    if (metadata && previous?.kind === "battle" && previous.groupId === metadata.group_id) {
      previous.events.push(event);
      return;
    }
    if (metadata) {
      items.push({ kind: "battle", groupId: metadata.group_id, metadata, events: [event] });
    } else {
      items.push({ kind: "event", event });
    }
  });
  return items;
}

function battleTimelineGroupTerminal(group) {
  if (group.events.some((event) => battleTerminalPhases.has(event.phase))) return true;
  const delegate = agentSession?.battle_delegate || latestState?._debug?.agent_session?.battle_delegate || {};
  return (
    delegate.child_session_id === group.metadata.child_session_id
    && Number(delegate.battle_generation || 0) === Number(group.metadata.battle_generation || 0)
    && battleTerminalStatuses.has(delegate.status)
  );
}

function renderAgentActivity(event, currentEventSeq, openDetails) {
  const details = agentEventVisibleDetails(event);
  return `<article class="agentActivity is-${escapeHtml(event.type)} is-${escapeHtml(event.phase)}${Number(event.seq || 0) === currentEventSeq ? " is-current" : ""}" data-event-seq="${Number(event.seq || 0)}">
    <span class="agentActivityIcon" aria-hidden="true">${eventIcon(event)}</span>
    <div><div class="agentActivityTitle"><strong>${escapeHtml(event.summary || event.type)}</strong><time>${escapeHtml(event.time || "")}${event.elapsed != null ? ` · ${event.elapsed}s` : ""}</time></div>
    ${details ? `<details ${openDetails.has(String(event.seq || 0)) ? "open" : ""}><summary>Details</summary><pre>${escapeHtml(JSON.stringify(details, null, 2))}</pre></details>` : ""}</div>
  </article>`;
}

function renderBattleTimelineGroup(group, currentEventSeq, openDetails, openBattleGroups) {
  const terminal = battleTimelineGroupTerminal(group);
  const latest = group.events.at(-1) || {};
  const status = terminal
    ? (battleTerminalPhases.has(latest.phase) ? latest.phase : "stopped")
    : (agentSession?.battle_delegate?.phase || latest.phase || "running");
  const toolCalls = group.events.filter((event) => event.phase === "tool_called").length;
  const shouldOpen = !terminal || openBattleGroups.has(group.groupId);
  return `<details class="agentBattleGroup is-${escapeHtml(status)}" data-battle-group="${escapeHtml(group.groupId)}" data-terminal="${terminal}" ${shouldOpen ? "open" : ""}>
    <summary><span>Battle Agent</span><small>${escapeHtml(status)} · ${toolCalls} tools · ${group.events.length} events</small></summary>
    <div class="agentBattleEvents">
      ${group.events.map((event) => renderAgentActivity(event, currentEventSeq, openDetails)).join("")}
    </div>
  </details>`;
}

function renderAgentTimeline({ anchorScrollHeight = null, anchorScrollTop = 0 } = {}) {
  if (!agentTimelineEl) return;
  const activePhases = new Set(["deciding", "request", "tool_called", "tool_output", "submitted", "validating", "executing", "progress"]);
  const latestEvent = agentSessionEvents.at(-1);
  const currentEventSeq = sessionRunning() && activePhases.has(latestEvent?.phase)
    ? Number(latestEvent?.seq || 0)
    : 0;
  const wasNearBottom = agentTimelineEl.scrollHeight - agentTimelineEl.scrollTop - agentTimelineEl.clientHeight < 60;
  const previousScrollTop = agentTimelineEl.scrollTop;
  const openDetails = new Set(
    [...agentTimelineEl.querySelectorAll(".agentActivity[data-event-seq] > div > details[open]")]
      .map((details) => details.closest(".agentActivity")?.dataset.eventSeq)
      .filter(Boolean)
  );
  const openBattleGroups = new Set(
    [...agentTimelineEl.querySelectorAll(".agentBattleGroup[data-terminal='true'][open]")]
      .map((details) => details.dataset.battleGroup)
      .filter(Boolean)
  );
  const battleDelegate = agentSession?.battle_delegate || latestState?._debug?.agent_session?.battle_delegate || {};
  const timelineRenderKey = `${agentSession?.session_id || ""}:${agentSession?.status || ""}:${agentSessionEvents.length}:${agentSessionEvents[0]?.seq || 0}:${latestEvent?.seq || 0}:${agentHasOlderEvents}:${battleDelegate.child_session_id || ""}:${battleDelegate.status || ""}:${battleDelegate.phase || ""}:${battleDelegate.instruction_count || 0}`;
  if (agentTimelineRenderKey === timelineRenderKey) {
    if (agentJumpLatestEl) {
      agentJumpLatestEl.hidden = agentUnread === 0;
      agentJumpLatestEl.textContent = agentUnread ? `Jump to latest (${agentUnread})` : "Jump to latest";
    }
    return;
  }
  const groups = new Map();
  agentSessionEvents.forEach((event) => {
    const turn = Number(event.turn || 0);
    if (!groups.has(turn)) groups.set(turn, []);
    groups.get(turn).push(event);
  });
  const earlierButton = agentHasOlderEvents
    ? `<button class="agentLoadEarlier" type="button" data-agent-load-earlier>Load earlier turns</button>`
    : "";
  agentTimelineEl.innerHTML = agentSessionEvents.length ? earlierButton + [...groups.entries()].map(([turn, events]) => {
    const items = agentTimelineItems(events);
    if (!items.length) return "";
    return `
    <section class="agentTurn">
      <h3>${turn ? `Turn ${turn}` : "Session"}</h3>
      ${items.map((item) => item.kind === "battle"
        ? renderBattleTimelineGroup(item, currentEventSeq, openDetails, openBattleGroups)
        : renderAgentActivity(item.event, currentEventSeq, openDetails)
      ).join("")}
    </section>`;
  }).join("") : '<div class="agentTimelineEmpty">No activity yet. Ask Agent to start.</div>';
  agentTimelineRenderKey = timelineRenderKey;
  if (anchorScrollHeight != null) {
    agentTimelineEl.scrollTop = anchorScrollTop + agentTimelineEl.scrollHeight - anchorScrollHeight;
  } else if (selectedStateTab === "agent" && wasNearBottom) {
    agentTimelineEl.scrollTop = agentTimelineEl.scrollHeight;
    agentUnread = 0;
  } else {
    agentTimelineEl.scrollTop = previousScrollTop;
  }
  if (agentJumpLatestEl) {
    agentJumpLatestEl.hidden = agentUnread === 0;
    agentJumpLatestEl.textContent = agentUnread ? `Jump to latest (${agentUnread})` : "Jump to latest";
  }
}

async function agentSessionRequest(action) {
  const model = agentSessionControlsEl.querySelector("[data-session-model]")?.value || selectedAgentModel;
  const turns = Math.max(1, Math.min(Number(agentSessionControlsEl.querySelector("[data-session-turns]")?.value || agentMaxTurns), 100));
  agentMaxTurns = turns;
  localStorage.setItem("palpal.agentMaxTurns", String(turns));
  const options = { method: "POST", headers: { "Content-Type": "application/json" } };
  if (["run", "decide"].includes(action)) options.body = JSON.stringify({ model, max_turns: turns });
  const response = await fetch(`/agent/session/${action}`, options);
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(result.error || `${action} failed: ${response.status}`);
  const sessionChanged = setAgentSession(result.session);
  if (sessionChanged) {
    await loadLatestAgentEvents();
    connectAgentEvents();
  }
  if (["run", "decide"].includes(action)) selectStateTab("agent", true);
  renderAgentSession();
}

agentSessionControlsEl?.addEventListener("click", (event) => {
  const roomMemoButton = event.target.closest("[data-room-memo-open]");
  if (roomMemoButton) {
    openRoomMemoDialog(roomMemoButton);
    return;
  }
  const goalsButton = event.target.closest("[data-goals-open]");
  if (goalsButton) {
    openGoalsDialog(goalsButton);
    return;
  }
  const button = event.target.closest("[data-session-action]");
  if (!button) return;
  button.disabled = true;
  agentSessionRequest(button.dataset.sessionAction).catch((error) => {
    button.disabled = false;
    agentError = error instanceof Error ? error.message : String(error);
    renderAgentSession();
  });
});

goalsClose?.addEventListener("click", closeGoalsDialog);
goalsContent?.addEventListener("click", (event) => {
  if (event.target.closest("[data-goals-retry]")) refreshAgentTasks();
});
goalsDialog?.addEventListener("click", (event) => {
  if (event.target === goalsDialog) closeGoalsDialog();
});
goalsDialog?.addEventListener("close", () => {
  const trigger = goalsDialogTrigger;
  goalsDialogTrigger = null;
  trigger?.focus?.();
});

roomMemoClose?.addEventListener("click", closeRoomMemoDialog);
roomMemoContent?.addEventListener("click", (event) => {
  if (event.target.closest("[data-room-memo-retry]")) refreshRoomMemo();
});
roomMemoDialog?.addEventListener("click", (event) => {
  if (event.target === roomMemoDialog) closeRoomMemoDialog();
});
roomMemoDialog?.addEventListener("close", () => {
  const trigger = roomMemoDialogTrigger;
  roomMemoDialogTrigger = null;
  restoreRoomMemoFocus(trigger);
});

agentSessionControlsEl?.addEventListener("change", (event) => {
  const modelEl = event.target.closest("[data-session-model]");
  if (modelEl) selectedAgentModel = modelEl.value;
  const turnsEl = event.target.closest("[data-session-turns]");
  if (turnsEl) {
    agentMaxTurns = Math.max(1, Math.min(Number(turnsEl.value || 10), 100));
    turnsEl.value = String(agentMaxTurns);
    localStorage.setItem("palpal.agentMaxTurns", String(agentMaxTurns));
  }
});

agentJumpLatestEl?.addEventListener("click", () => {
  agentTimelineEl.scrollTop = agentTimelineEl.scrollHeight;
  agentUnread = 0;
  renderAgentTimeline();
});

agentTimelineEl?.addEventListener("click", (event) => {
  if (event.target.closest("[data-agent-load-earlier]")) loadEarlierAgentEvents();
});

agentTimelineEl?.addEventListener("scroll", () => {
  const scrollable = agentTimelineEl.scrollHeight > agentTimelineEl.clientHeight + 1;
  if (scrollable && agentTimelineEl.scrollTop <= 48) loadEarlierAgentEvents();
});

async function refreshAgentSession() {
  if (agentSessionRefreshTimer !== null) {
    clearTimeout(agentSessionRefreshTimer);
    agentSessionRefreshTimer = null;
  }
  agentSessionRefreshQueued = false;
  const response = await fetch("/agent/session");
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(result.error || "session unavailable");
  setAgentSession(result.session);
  if (sessionRunning()) selectStateTab("agent", true);
  await loadLatestAgentEvents();
  renderAgentSession();
  connectAgentEvents();
}

function connectAgentEvents() {
  agentEventChannel.replace();
}

const agentEventChannel = createReconnectChannel("agent", {
  url: () => {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const sessionId = encodeURIComponent(agentSession?.session_id || "");
    return `${protocol}//${location.host}/agent/session/stream?after=${agentEventSeq}&session_id=${sessionId}`;
  },
  onMessage: (message) => {
    const event = JSON.parse(message.data);
    if (event.session_id !== agentSession?.session_id || Number(event.seq || 0) <= agentEventSeq) return;
    agentEventSeq = Number(event.seq || agentEventSeq);
    agentSessionEvents.push(event);
    const navigationResult = agentNavigationFromEvent(event);
    if (navigationResult) {
      agentNavigationResult = navigationResult;
      if (latestState && sessionRunning()) {
        latestState._navigation = agentNavigationBase || latestState._navigation;
        applyAgentNavigation(latestState);
        renderNavigation(latestState);
        renderObjects(latestState);
        drawOverlay(latestState);
      }
    }
    if (!agentHistoryExpanded) trimAgentTimelineToLatestTurns();
    const nearBottom = agentTimelineEl.scrollHeight - agentTimelineEl.scrollTop - agentTimelineEl.clientHeight < 60;
    if (selectedStateTab !== "agent" || !nearBottom) agentUnread += 1;
    renderAgentTimeline();
    if (event.type === "tool" && event.phase === "completed" && event.details?.name === "upsert_task") {
      refreshAgentTasks();
    }
    scheduleAgentSessionStateRefresh();
  },
});

function scheduleAgentSessionStateRefresh() {
  agentSessionRefreshQueued = true;
  if (agentSessionRefreshInFlight || agentSessionRefreshTimer !== null) return;
  agentSessionRefreshTimer = setTimeout(() => {
    agentSessionRefreshTimer = null;
    refreshAgentSessionStateOnly();
  }, 100);
}

async function refreshAgentSessionStateOnly() {
  if (agentSessionRefreshInFlight) {
    agentSessionRefreshQueued = true;
    return;
  }
  agentSessionRefreshQueued = false;
  agentSessionRefreshInFlight = true;
  const expectedSessionId = agentSession?.session_id;
  try {
    const response = await fetch("/agent/session");
    const result = await response.json();
    if (expectedSessionId !== agentSession?.session_id) return;
    const changed = result.ok ? setAgentSession(result.session) : false;
    renderAgentSession();
    if (changed) {
      await loadLatestAgentEvents();
      connectAgentEvents();
    }
  } catch (_) {
    // The websocket reconnect path will retry.
  } finally {
    agentSessionRefreshInFlight = false;
    if (agentSessionRefreshQueued) scheduleAgentSessionStateRefresh();
  }
}

async function useInventoryItem(itemId, target) {
  const payload = { item_id: itemId };
  if (target === "all") {
    payload.target = "all";
  } else {
    payload.role_id = Number(target || 0);
  }
  const response = await fetch("/inventory/use", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok || !result.ok) {
    throw new Error(result.error || `item use failed: ${response.status}`);
  }
  return result;
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
    const target = navigationTargets(latestState).get(targetId);
    const approachItemId = Number(target?.approach?.item_id || 0);
    const itemQuery = approachItemId > 0 ? `&item_id=${encodeURIComponent(approachItemId)}` : "";
    const response = await fetch(`/navigation/route?id=${encodeURIComponent(targetId)}${itemQuery}`);
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
  navigationRenderKey = null;
  navigationLatestEnvironmentKey = null;
  navigationStableEnvironmentKey = null;
  navigationRouteRequestId += 1;
  resetNavigationRefreshRetry();
  agentError = null;
  agentActionStatus = "";
  agentExecutionInFlight = false;
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
    await refreshAgentSession();
    await refreshAgentTasks();
  } catch (error) {
    checkpointError = error instanceof Error ? error.message : String(error);
  } finally {
    checkpointBusy = null;
    if (latestState) renderCheckpoints(latestState);
  }
}

async function startNewGame() {
  if (checkpointBusy) return;
  if (!latestState || latestState.status !== "startup_waiting") {
    if (!window.confirm("Start a new game? Current live game state will be replaced.")) return;
  }
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
    await refreshAgentSession();
    await refreshAgentTasks();
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
  clearAgentResultTitle();
  agentRequestInFlight = true;
  agentError = null;
  agentActionStatus = "";
  if (latestState) renderAgent(latestState);

  try {
    const response = await fetch("/agent/session/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: currentAgentModel(latestState) }),
    });
    const result = await response.json();
    if (!response.ok || !result.ok) {
      throw new Error(result.error || `agent failed: ${response.status}`);
    }
    setAgentSession(result.session);
    selectStateTab("agent", true);
    renderAgentSession();
  } catch (error) {
    agentLastDecision = null;
    agentError = error instanceof Error ? error.message : String(error);
  } finally {
    agentRequestInFlight = false;
    if (latestState) renderAgent(latestState);
  }
}

async function executeAgentRecommendation() {
  if (agentExecutionInFlight) return;
  agentExecutionInFlight = true;
  agentActionStatus = "";
  if (latestState) renderAgent(latestState);

  try {
    const response = await fetch("/agent/session/execute", { method: "POST" });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || `execute failed: ${response.status}`);
    setAgentSession(result.session);
    agentActionStatus = "Executed pending recommendation.";
    await refreshState();
  } catch (error) {
    agentActionStatus = `Action failed: ${error instanceof Error ? error.message : String(error)}`;
  } finally {
    agentExecutionInFlight = false;
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
  let deadline = performance.now() + 2500;
  let controlDeadlineApplied = false;
  while (performance.now() < deadline) {
    await sleep(35);
    const response = await fetch("/state");
    const state = await response.json();
    render(state);
    const key = partyPositionKey(state);
    if (expectedKey ? key === expectedKey : key !== startKey) {
      return true;
    }
    if (["action_pending", "game_script", "entering_scene"].includes(state.control?.phase)) {
      if (!controlDeadlineApplied) {
        deadline = performance.now() + 30000;
        controlDeadlineApplied = true;
      }
      continue;
    }
    if (state._debug?.runtime_stale || state.in_battle) {
      return false;
    }
  }
  return false;
}

async function navigationStep(targetId, approachItemId = null) {
  const payload = { id: targetId };
  if (Number(approachItemId) > 0) payload.item_id = Number(approachItemId);
  const response = await fetch("/navigation/step", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
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
  cacheNavigation(navigationEnvironmentKey(latestState), latestState._navigation);
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

async function navigateToTarget(targetId, requestedItemId = null) {
  if (activeNavigation) {
    sendNavigationDiagnostic("navigation_click_ignored", {
      ...navigationDiagnosticContext(latestState),
      target_id: targetId,
      reason: "active_navigation",
    });
    return;
  }
  if (!navigationTargetActionable(latestState, targetId, requestedItemId)) {
    sendNavigationDiagnostic("navigation_click_ignored", {
      ...navigationDiagnosticContext(latestState),
      target_id: targetId,
      reason: "navigation_not_ready",
    });
    return;
  }
  const target = navigationTargets(latestState).get(targetId);
  const approachItemId = Number(requestedItemId || (target?.kind === "approach" ? target?.approach?.item_id : 0) || 0) || null;
  activeNavigation = { targetId, approachItemId, status: "starting" };
  const navigationRun = activeNavigation;
  sendNavigationDiagnostic("navigation_start", {
    ...navigationDiagnosticContext(latestState),
    target_id: targetId,
  });
  if (latestState) renderNavigation(latestState);

  const unlockNavigation = (reason = "complete") => {
    if (activeNavigation !== navigationRun) return;
    sendNavigationDiagnostic("navigation_unlock", {
      ...navigationDiagnosticContext(latestState),
      target_id: navigationRun.targetId,
      status: navigationRun.status,
      reason,
    });
    activeNavigation = null;
    if (latestState) renderNavigation(latestState);
  };

  try {
    let navigationSteps = 0;
    while (activeNavigation === navigationRun && navigationSteps < 240) {
      const startKey = partyPositionKey(latestState);
      const result = await navigationStep(targetId, navigationRun.approachItemId);
      if (activeNavigation !== navigationRun) return;
      navigationRun.status = result.status;
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

      if (result.status === "approached") {
        unlockNavigation("approached");
        await refreshState();
        return;
      }

      if (result.ok && result.status === "transition_observing") {
        await sleep(160);
        await refreshState();
        continue;
      }

      if (result.ok && result.status === "waiting_for_control") {
        await sleep(80);
        await refreshState();
        continue;
      }

      if (result.ok && result.status === "transitioned") {
        unlockNavigation("transitioned");
        await refreshState();
        return;
      }

      if (!result.ok && result.status === "action_pending") {
        await sleep(120);
        continue;
      }

      navigationSteps += 1;

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

let pendingFramePng = null;
let decodingFramePng = false;

async function drainFramePng() {
  if (decodingFramePng) return;
  decodingFramePng = true;
  try {
    while (pendingFramePng) {
      const buffer = pendingFramePng;
      pendingFramePng = null;
      const bitmap = await createImageBitmap(new Blob([buffer], { type: "image/png" }));
      if (frameCanvas.width !== bitmap.width || frameCanvas.height !== bitmap.height) {
        frameCanvas.width = bitmap.width;
        frameCanvas.height = bitmap.height;
      }
      frameCtx.drawImage(bitmap, 0, 0);
      bitmap.close();
    }
  } catch (error) {
    console.warn("frame decode failed", error);
  } finally {
    decodingFramePng = false;
    if (pendingFramePng) drainFramePng();
  }
}

function renderFrame(buffer) {
  pendingFramePng = buffer;
  drainFramePng();
}

async function sendAction(buttons, frames = 1) {
  const response = await fetch("/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ buttons, frames }),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok || result.ok === false) {
    const error = new Error(result.error || `action failed: ${response.status}`);
    error.code = result.error || null;
    throw error;
  }
  return result;
}

async function advanceDialogRequest(options) {
  const response = await fetch("/dialog/advance", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(options || {}),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok || result.ok === false) {
    throw new Error(result.error || result.status || `dialog advance failed: ${response.status}`);
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
  if (
    sessionRunning()
    || refreshRequired()
    || !(latestState?.control?.allowed_actions || []).includes("press_buttons")
  ) return;
  const now = performance.now();
  if (!repeatableButtons.has(buttonName)) {
    const last = lastActionAt.get(buttonName) || 0;
    if (now - last < actionCooldownMs) return;
  }
  lastActionAt.set(buttonName, now);
  flashButton(buttonName);
  deferNavigationRefreshForManualInput();
  sendAction([buttonName], 1).catch((error) => {
    if (error.code !== "action_pending") console.warn("action failed", error);
  });
}

function connectStream() {
  stateStreamChannel.start();
}

const stateStreamChannel = createReconnectChannel("state", {
  url: () => {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    return `${protocol}//${location.host}/stream`;
  },
  onOpen: () => {
    stateConnected = true;
    renderStatusBar();
  },
  onMessage: (event) => {
    render(JSON.parse(event.data));
  },
  onClose: () => {
    stateConnected = false;
    renderStatusBar();
  },
  onError: () => {
    stateConnected = false;
    renderStatusBar();
  },
});

function connectFrames() {
  frameStreamChannel.start();
}

const frameStreamChannel = createReconnectChannel("frames", {
  url: () => {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    return `${protocol}//${location.host}/frames`;
  },
  createSocket: (url) => {
    const socket = new WebSocket(url);
    socket.binaryType = "arraybuffer";
    return socket;
  },
  onOpen: () => {
    frameConnected = true;
    renderStatusBar();
  },
  onMessage: (event) => {
    if (event.data instanceof ArrayBuffer) {
      renderFrame(event.data);
    }
  },
  onClose: () => {
    frameConnected = false;
    renderStatusBar();
  },
  onError: () => {
    frameConnected = false;
    renderStatusBar();
  },
});

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

function closeCheckpointMenu() {
  const openMenu = checkpointsEl?.querySelector(".checkpointMenu[open]");
  if (!openMenu) return false;
  openMenu.open = false;
  openMenu.querySelector(":scope > summary")?.focus();
  return true;
}

function shouldIgnoreGameKeydown(event) {
  const target = event.target instanceof Element ? event.target : null;
  if (event.defaultPrevented) return true;
  if (isEditableKeyTarget(target)) return true;
  if (checkpointMenuActive()) return true;
  if (transitionsDialog?.open) return true;
  if (goalsDialog?.open) return true;
  if (roomMemoDialog?.open) return true;
  return false;
}

window.addEventListener("keydown", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (event.key === "Escape" && checkpointMenuActive()) {
    event.preventDefault();
    closeCheckpointMenu();
    return;
  }
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

setNavigationPaneHidden(navigationPaneHidden);
setupStateTabs();
refreshAgentSession().catch((error) => console.warn("agent session unavailable", error));
refreshAgentTasks();
connectStream();
connectFrames();
refreshCheckpoints();
renderStatusBar();
