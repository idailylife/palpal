const stateEl = document.getElementById("json");
const jsonLiveEl = document.getElementById("jsonLive");
const jsonRefreshEl = document.getElementById("jsonRefresh");
const jsonStatusEl = document.getElementById("jsonStatus");
const summaryEl = document.getElementById("summary");
const objectsEl = document.getElementById("objects");
const navigationEl = document.getElementById("navigation");
const explorationEl = document.getElementById("exploration");
const dialogStatusEl = document.getElementById("dialogStatus");
const dialogsEl = document.getElementById("dialogs");
const eventsEl = document.getElementById("events");
const inventoryEl = document.getElementById("inventory");
const equipmentEl = document.getElementById("equipment");
const frameCanvas = document.getElementById("frame");
const connectionEl = document.getElementById("connection");
const runtimeStatusEl = document.getElementById("runtimeStatus");
const overlay = document.getElementById("overlay");
const frameCtx = frameCanvas.getContext("2d");
const ctx = overlay.getContext("2d");

let latestState = null;
let selectedRoute = null;
let lastExploreResult = null;
let activeNavigation = null;
let navigationRefreshCompletedKey = null;
let navigationRefreshInFlightKey = null;
let navigationRenderKey = null;
let latestFullNavigation = null;
let latestFullNavigationStableKey = null;
let navigationRouteRequestId = 0;
let navigationDiagnosticKey = null;
let explorationRenderKey = null;
let summaryRenderKey = null;
let dialogRenderKey = null;
let dialogsRenderKey = null;
let eventsRenderKey = null;
let inventoryRenderKey = null;
let equipmentRenderKey = null;
let objectsRenderKey = null;
let jsonRenderText = "";
let jsonLastRenderedAt = 0;
let jsonPendingTimer = null;
const navigationCache = new Map();
let stateConnected = false;
let frameConnected = false;
const repeatableButtons = new Set(["up", "down", "left", "right"]);
const actionCooldownMs = 180;
const jsonUpdateIntervalMs = 1000;
const lastActionAt = new Map();

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

explorationEl.addEventListener("click", (event) => {
  const actionEl = event.target.closest("[data-explore-action]");
  if (!actionEl || !explorationEl.contains(actionEl)) return;
  if (actionEl.dataset.exploreAction === "step") {
    exploreStep();
  } else if (actionEl.dataset.exploreAction === "reset") {
    resetExploration();
  }
});

inventoryEl.addEventListener("click", (event) => {
  const actionEl = event.target.closest("[data-use-item]");
  if (!actionEl || !inventoryEl.contains(actionEl)) return;
  useInventoryItem(Number(actionEl.dataset.useItem), actionEl.dataset.useTarget);
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
    unknown: "checking",
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
    state_stale: "stale",
    in_battle: "battle",
    in_dialog: "dialog",
    unsafe_start: "unsafe",
    position_deviated: "deviated",
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

function renderDialogStatus(state) {
  const dialog = state.dialog || {};
  const active = Boolean(dialog.active);
  const rows = active
    ? [
        ["status", '<span class="tag tagDialog">active</span>'],
        ["visible", dialog.visible ? "yes" : dialog.held ? "held" : "no"],
        ["position", dialog.position || "-"],
        ["line", dialog.current_line ?? "-"],
        ["text", `${dialog.text_position?.x ?? "-"}, ${dialog.text_position?.y ?? "-"}`],
        ["rng", dialog.playing_rng ? "yes" : "no"],
      ]
    : [["status", '<span class="tag">inactive</span>']];

  const renderKey = JSON.stringify({
    active,
    rows,
  });
  dialogRenderKey = setPanelHtml(dialogStatusEl, rows
    .map(([key, value]) => `<div>${key}</div><strong>${value ?? "-"}</strong>`)
    .join(""), renderKey, dialogRenderKey, () => {
      dialogStatusEl.className = `dialogStatus ${active ? "is-active" : ""}`;
    });
}

function renderDialogs(state) {
  const dialogs = (state._debug?.recent_dialogs || []).slice(-8).reverse();
  if (!dialogs.length) {
    dialogsRenderKey = setPanelHtml(dialogsEl, '<div class="navEmpty">No dialog history yet.</div>', "empty", dialogsRenderKey);
    return;
  }

  const renderKey = JSON.stringify(dialogs.map((dialog) => [
    dialog.source_key,
    dialog.frame,
    dialog.speaker_hint,
    dialog.text,
    dialog.scene_id,
    dialog.map_id,
    dialog.event_object_id,
    dialog.message_id,
  ]));
  dialogsRenderKey = setPanelHtml(dialogsEl, dialogs
    .map((dialog) => {
      const speaker = dialog.speaker_hint ? escapeHtml(dialog.speaker_hint) : "unknown";
      const meta = [
        `frame ${dialog.frame ?? "-"}`,
        `scene ${dialog.scene_id ?? "-"}/${dialog.map_id ?? "-"}`,
        dialog.event_object_id ? `obj #${dialog.event_object_id}` : "",
        dialog.message_id != null ? `msg #${dialog.message_id}` : "",
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
    const canGo = ![
      "unreachable_static",
      "blocked_by_transition",
      "needs_direction",
      "no_nav_grid",
      "target_inactive",
    ].includes(target.status);
    const goEnabled = canGo && !activeNavigation && !state.dialog?.active;
    if (goEnabled) {
      enabledCount += 1;
    } else {
      if (!canGo) disabledReasons.add(`status:${target.status || "unknown"}`);
      if (activeNavigation) disabledReasons.add("active_navigation");
      if (state.dialog?.active) disabledReasons.add("dialog_active");
    }
  }
  const diagnosticKey = JSON.stringify({
    total: targets.length,
    enabled: enabledCount,
    activeNavigation,
    dialog: Boolean(state.dialog?.active),
    statuses: targets.map((target) => `${target.id}:${target.status}`).join("|"),
  });
  if (navigationDiagnosticKey !== diagnosticKey) {
    navigationDiagnosticKey = diagnosticKey;
    sendNavigationDiagnostic("navigation_buttons", {
      ...navigationDiagnosticContext(state),
      total: targets.length,
      enabled: enabledCount,
      disabled: targets.length - enabledCount,
      disabled_reasons: Array.from(disabledReasons).sort(),
      target_statuses: targets.map((target) => ({ id: target.id, status: target.status, kind: target.kind })),
    });
  }

  navigationEl.innerHTML = targets
    .map((target) => {
      const isActive = activeNavigation?.targetId === target.id;
      const detail = [
        target.kind,
        Number.isFinite(target.steps) ? `${target.steps} steps` : "",
        target.required_direction ? `face ${target.required_direction}` : "",
      ]
        .filter(Boolean)
        .join(" · ");
      const canGo = ![
        "unreachable_static",
        "blocked_by_transition",
        "needs_direction",
        "no_nav_grid",
        "target_inactive",
      ].includes(target.status);
      const goEnabled = canGo && !activeNavigation && !state.dialog?.active;
      const goLabel = isActive ? navigationRunText(activeNavigation.status) : target.status === "transition" ? "Enter" : "Go";
      return `<div class="navTarget ${selectedRoute?.target_id === target.id ? "is-selected" : ""}" data-nav-id="${target.id}" role="button" tabindex="0">
        <div class="navTargetSelect">
          <strong>#${target.id}</strong>
          <span>
            <span class="tag ${statusClass(target.status)}">${statusText(target.status)}</span>
            ${detail ? `<small>${detail}</small>` : ""}
          </span>
        </div>
        <button class="navGo" data-nav-go="${target.id}" ${goEnabled ? "" : "disabled"}>${goLabel}</button>
      </div>`;
    })
    .join("");
  navigationRenderKey = renderKey;
}

function navigationRefreshSignature(state) {
  const pos = state.party_position || {};
  return [
    navigationStableSignature(state),
    pos.x ?? "",
    pos.y ?? "",
  ].join("/");
}

function navigationStableSignature(state) {
  const objects = (state.objects || [])
    .filter((obj) => obj.interactable)
    .map((obj) => `${obj.id}:${obj.x}:${obj.y}:${obj.state}:${obj.trigger_mode}:${obj.trigger_script}:${obj.sprite_id}:${obj.blocking ? 1 : 0}`)
    .join("|");
  return [
    state.scene_id ?? "",
    state.map_id ?? "",
    state.room?.key ?? "",
    state.dialog?.active ? "dialog" : "free",
    objects,
  ].join("/");
}

async function refreshNavigation(state) {
  if (!state._navigation || state._navigation.status === "no_nav_grid") return;
  const key = navigationRefreshSignature(state);
  if (navigationRefreshInFlightKey === key || (navigationRefreshCompletedKey === key && navigationCache.has(key))) return;
  navigationRefreshInFlightKey = key;
  try {
    const response = await fetch("/navigation");
    if (!response.ok) {
      throw new Error(`navigation failed: ${response.status}`);
    }
    const navigation = await response.json();
    navigationCache.set(key, navigation);
    latestFullNavigation = navigation;
    latestFullNavigationStableKey = navigationStableSignature(state);
    navigationRefreshCompletedKey = key;
    if (latestState && navigationRefreshSignature(latestState) === key) {
      latestState._navigation = navigation;
      renderNavigation(latestState);
      drawOverlay(latestState);
    }
  } catch (error) {
    console.warn(error);
  } finally {
    if (navigationRefreshInFlightKey === key) {
      navigationRefreshInFlightKey = null;
    }
  }
}

function navigationRunText(status) {
  const labels = {
    starting: "Go...",
    stepping: "Moving",
    interacting: "Action",
    arrived: "Arrived",
    action_pending: "Pending",
  };
  return labels[status] || "Stop";
}

function renderExploration(state) {
  const exploration = state._exploration;
  if (!exploration) {
    explorationRenderKey = setPanelHtml(explorationEl, '<div class="navEmpty">No exploration state yet.</div>', "empty", explorationRenderKey);
    return;
  }

  const resultStatus = lastExploreResult?.status;
  const complete = exploration.complete ? '<span class="tag tagReachable">complete</span>' : "";
  const status = resultStatus || exploration.status;
  const renderKey = JSON.stringify({
    resultStatus,
    status: exploration.status,
    visited: exploration.visited,
    frontier: exploration.frontier,
    blocked: exploration.blocked_edges,
    complete: exploration.complete,
    dialog: Boolean(state.dialog?.active),
  });
  explorationRenderKey = setPanelHtml(explorationEl, `
    <div class="explorationStats">
      <div><span>visited</span><strong>${exploration.visited ?? 0}</strong></div>
      <div><span>frontier</span><strong>${exploration.frontier ?? 0}</strong></div>
      <div><span>blocked</span><strong>${exploration.blocked_edges ?? 0}</strong></div>
    </div>
    <div class="explorationStatus">
      <span class="tag">${explorationStatusText(status)}</span>
      ${complete}
    </div>
    <div class="explorationButtons">
      <button data-explore-action="step" ${exploration.status !== "ok" || exploration.complete || state.dialog?.active ? "disabled" : ""}>Explore Step</button>
      <button data-explore-action="reset">Reset Map</button>
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
  for (const node of exploration.nodes?.visited || []) {
    const x = (node.x - viewport.x) * scaleX;
    const y = (node.y - viewport.y) * scaleY;
    if (x < -10 || y < -10 || x > overlay.width + 10 || y > overlay.height + 10) continue;
    ctx.fillStyle = "rgba(126, 227, 174, 0.24)";
    ctx.fillRect(x - 5, y - 5, 10, 10);
  }

  for (const node of exploration.nodes?.frontier || []) {
    const x = (node.x - viewport.x) * scaleX;
    const y = (node.y - viewport.y) * scaleY;
    if (x < -10 || y < -10 || x > overlay.width + 10 || y > overlay.height + 10) continue;
    ctx.strokeStyle = "rgba(255, 214, 102, 0.95)";
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
  const elapsed = performance.now() - jsonLastRenderedAt;
  const delay = Math.max(0, jsonUpdateIntervalMs - elapsed);
  jsonPendingTimer = setTimeout(() => {
    jsonPendingTimer = null;
    if (latestState) renderJson(latestState);
  }, delay);
}

function renderJson(state, options = {}) {
  const force = Boolean(options.force);
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
  const navKey = navigationRefreshSignature(state);
  const cachedNavigation = navigationCache.get(navKey);
  if (cachedNavigation) {
    state._navigation = cachedNavigation;
    latestFullNavigation = cachedNavigation;
    latestFullNavigationStableKey = navigationStableSignature(state);
  } else if (
    state._navigation?.status === "pending" &&
    latestFullNavigation &&
    latestFullNavigationStableKey === navigationStableSignature(state)
  ) {
    state._navigation = latestFullNavigation;
  }
  latestState = state;
  renderJson(state);
  renderRuntimeStatus(state);
  renderSummary(state);
  renderDialogStatus(state);
  renderDialogs(state);
  renderEvents(state);
  renderInventory(state);
  renderEquipment(state);
  renderNavigation(state);
  renderExploration(state);
  renderObjects(state);
  drawOverlay(state);
  refreshNavigation(state);
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
  render(await response.json());
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

function updateNavigationTarget(targetId, patch) {
  if (!latestState?._navigation?.targets) return;
  latestState._navigation = {
    ...latestState._navigation,
    targets: latestState._navigation.targets.map((target) =>
      target.id === targetId ? { ...target, ...patch } : target
    ),
  };
  navigationCache.set(navigationRefreshSignature(latestState), latestState._navigation);
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

async function playNavigationSegment(stepResult, startKey) {
  const firstProgressed = await waitForPositionChange(startKey);
  if (!firstProgressed) {
    return false;
  }

  const moves = stepResult.segment_moves || [stepResult.move].filter(Boolean);
  for (let index = 1; index < moves.length; index += 1) {
    await sleep(110);
    const startKey = partyPositionKey(latestState);
    await sendAction([moves[index]], 1);
    const progressed = await waitForPositionChange(startKey);
    if (!progressed) {
      return false;
    }
  }
  const expectedKey = routeNodePositionKey(stepResult.expected_node || stepResult.route?.nodes?.[1]);
  return expectedKey ? partyPositionKey(latestState) === expectedKey : true;
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
        await waitForInteractionResult(result);
        await refreshState();
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

async function exploreStep() {
  lastExploreResult = { status: "stepping" };
  if (latestState) renderExploration(latestState);
  const response = await fetch("/exploration/step", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ max_steps: 1 }),
  });
  lastExploreResult = await response.json();
  if (latestState && lastExploreResult.exploration) {
    latestState._exploration = lastExploreResult.exploration;
    render(latestState);
  }
}

async function resetExploration() {
  await fetch("/exploration/reset", { method: "POST" });
  lastExploreResult = null;
  await refreshState();
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
  sendAction([buttonName], 1);
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

window.addEventListener("keydown", (event) => {
  const target = event.target instanceof Element ? event.target : null;
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
setConnection();
