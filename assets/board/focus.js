"use strict";
// Area focus: steer which categories the skill works on, from the board.
const FOCUS_AREAS = [
  ["features", "Features", "New or finished user-facing behavior", 262],
  ["ui", "UI", "Layout, states, and visual polish", 292],
  ["accessibility", "Accessibility", "Keyboard, contrast, screen readers", 200],
  ["assets", "Assets", "Images, icons, fonts, media", 322],
  ["performance", "Performance", "Speed, memory, and load time", 32],
  ["gate-speed", "Gate speed", "Faster builds and test runs", 48],
  ["security", "Security", "Vulnerabilities and privacy", 352],
  ["resilience", "Resilience", "Errors, retries, and recovery", 12],
  ["concurrency", "Concurrency", "Races, locking, async safety", 230],
  ["quality", "Quality", "Bugs, dead code, clarity", 150],
  ["coverage", "Coverage", "Missing and weak tests", 172],
  ["contracts", "Contracts", "APIs, schemas, interfaces", 186],
  ["docs", "Docs", "Guides, references, comments", 88],
  ["general", "General", "Anything that fits no other area", 240],
];
const FOCUS_PRESETS = [
  ["balanced", "Balanced", {}],
  ["product", "Ship features", { features: "prefer", ui: "prefer" }],
  [
    "harden",
    "Harden",
    {
      security: "prefer",
      resilience: "prefer",
      concurrency: "prefer",
      docs: "skip",
      assets: "skip",
    },
  ],
  [
    "polish",
    "Polish UI",
    { ui: "prefer", accessibility: "prefer", assets: "prefer" },
  ],
  ["speed", "Speed up", { performance: "prefer", "gate-speed": "prefer" }],
  [
    "tests",
    "Test debt",
    { coverage: "prefer", quality: "prefer", features: "skip" },
  ],
];
const FOCUS_COLLAPSED_KEY = "improve-board-focus-collapsed";
const FOCUS_LEVEL_LABELS = { prefer: "Prefer", normal: "Normal", skip: "Skip" };
let focusSelection = {},
  focusDirty = false,
  focusRetry = null,
  focusSending = false;

function areaHue(area) {
  return FOCUS_AREAS.find((row) => row[0] === area)?.[3] ?? 240;
}

function focusSummary(areas = {}) {
  const pick = (level) =>
    FOCUS_AREAS.filter((row) => areas[row[0]] === level).map((row) => row[1]);
  const prefer = pick("prefer"),
    skip = pick("skip");
  if (!prefer.length && !skip.length) return "Balanced across all areas";
  return [
    prefer.length ? "Prefer " + prefer.join(", ") : "",
    skip.length ? "Skip " + skip.join(", ") : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

function savedFocus() {
  // The latest current-run focus request wins until the skill records a newer one.
  const rows = (snapshot?.controls?.requests || []).filter(
    (row) => row.type === "focus" && row.status !== "declined",
  );
  const latest = rows[rows.length - 1];
  const applied = snapshot?.current?.focus || {};
  if (latest && (latest.status === "pending" || !applied.request_id))
    return { areas: latest.areas || {}, request: latest };
  return { areas: applied.areas || {}, request: null, applied };
}

function sameFocus(left, right) {
  return FOCUS_AREAS.every(
    ([area]) =>
      (left[area] || "normal") === (right[area] || "normal"),
  );
}

function focusCounts() {
  const counts = {};
  for (const task of snapshot?.tasks || []) {
    if (task.run_id !== "current") continue;
    const row = (counts[task.area] ||= { open: 0, done: 0 });
    if (FINISHED.has(task.status)) row.done++;
    else row.open++;
  }
  return counts;
}

function buildFocusGrid() {
  const grid = $("focus-grid");
  for (const [area, name, hint] of FOCUS_AREAS) {
    const tile = element("fieldset", "focus-tile");
    tile.dataset.area = area;
    tile.style.setProperty("--hue", areaHue(area));
    const legend = element("legend", "focus-name");
    const filter = element("button", "focus-filter", name);
    filter.type = "button";
    filter.title = "Show " + name + " tasks on the board";
    filter.addEventListener("click", () => filterArea(area));
    legend.append(filter);
    const meter = element("div", "focus-meter");
    meter.append(element("span", "focus-bar"));
    const levels = element("div", "focus-levels");
    for (const level of ["prefer", "normal", "skip"]) {
      const label = element("label", "focus-level " + level);
      const input = element("input");
      input.type = "radio";
      input.name = "focus-" + area;
      input.value = level;
      input.addEventListener("change", () => {
        focusSelection = { ...focusSelection, [area]: level };
        if (level === "normal") delete focusSelection[area];
        focusDirty = true;
        renderFocus();
      });
      label.append(input, element("span", "", FOCUS_LEVEL_LABELS[level]));
      levels.append(label);
    }
    tile.append(
      legend,
      element("p", "focus-hint", hint),
      meter,
      element("p", "focus-count"),
      levels,
    );
    grid.append(tile);
  }
  const presets = $("focus-presets");
  for (const [id, name, areas] of FOCUS_PRESETS) {
    const button = element("button", "focus-preset", name);
    button.type = "button";
    button.dataset.preset = id;
    button.addEventListener("click", () => {
      focusSelection = { ...areas };
      focusDirty = true;
      renderFocus();
    });
    presets.append(button);
  }
}

function filterArea(area) {
  if (![...$("area").options].some((option) => option.value === area)) {
    focusFeedback("No tasks are recorded in this area yet.");
    return;
  }
  $("area").value = area;
  selectView("board");
  changeFilters();
  $("task-toolbar").scrollIntoView({ behavior: "smooth", block: "start" });
}

function focusFeedback(message, error = false) {
  const note = $("focus-feedback");
  note.hidden = false;
  note.textContent = message;
  note.classList.toggle("form-error", error);
}

function renderFocus() {
  if (!snapshot) return;
  const saved = savedFocus();
  if (!focusDirty) focusSelection = { ...saved.areas };
  const counts = focusCounts(),
    live = ["current", "all"].includes(snapshot.selected_run);
  const maxOpen = Math.max(1, ...Object.values(counts).map((row) => row.open));
  const skipped = FOCUS_AREAS.filter(
    ([area]) => focusSelection[area] === "skip",
  ).length;
  for (const tile of $("focus-grid").children) {
    const area = tile.dataset.area,
      level = focusSelection[area] || "normal",
      count = counts[area] || { open: 0, done: 0 };
    tile.dataset.level = level;
    for (const input of tile.querySelectorAll("input"))
      input.checked = input.value === level;
    tile.querySelector(".focus-bar").style.width =
      Math.round((count.open / maxOpen) * 100) + "%";
    tile.querySelector(".focus-count").textContent = !live
      ? "Select the current run for counts"
      : count.open || count.done
        ? `${countText(count.open)} open · ${countText(count.done)} finished`
        : "No tasks yet";
  }
  for (const button of $("focus-presets").children) {
    const preset = FOCUS_PRESETS.find((row) => row[0] === button.dataset.preset);
    button.setAttribute("aria-pressed", String(sameFocus(preset[2], focusSelection)));
  }
  const changed = !sameFocus(focusSelection, saved.areas);
  if (focusDirty && !changed) focusDirty = false;
  const allowed =
    connected && !focusSending && !!snapshot.controls?.requests_available;
  $("focus-save").disabled = !allowed || !changed || skipped === FOCUS_AREAS.length;
  $("focus-reset").disabled = !changed;
  for (const input of $("focus-grid").querySelectorAll("input"))
    input.disabled = !allowed;
  for (const button of $("focus-presets").children) button.disabled = !allowed;
  const state = $("focus-state");
  state.className = "focus-state";
  if (changed) {
    state.textContent = "Unsaved changes";
    state.classList.add("unsaved");
  } else if (saved.request?.status === "pending") {
    state.textContent = "Waiting for the next checkpoint";
    state.classList.add("pending");
  } else if (saved.request || saved.applied?.request_id) {
    state.textContent = "Applied";
    state.classList.add("applied");
  } else state.textContent = "Default";
  $("focus-summary").textContent =
    skipped === FOCUS_AREAS.length
      ? "Leave at least one area available for work."
      : focusSummary(focusSelection) +
        (saved.applied?.excluded?.length
          ? ` · Run scope still excludes: ${saved.applied.excluded.join("; ")}`
          : "");
  highlightFocusedCards();
}

function highlightFocusedCards() {
  const areas = savedFocus().areas;
  for (const card of document.querySelectorAll("[data-area-focus]"))
    card.dataset.areaFocus = areas[card.dataset.area] || "normal";
}

async function saveFocus() {
  if (focusSending) return;
  const payload = {
    type: "focus",
    run_id: snapshot.current.control_id || null,
    areas: { ...focusSelection },
    text: $("focus-note").value,
  };
  const signature = JSON.stringify(payload);
  if (focusRetry?.signature !== signature)
    focusRetry = { signature, id: crypto.randomUUID() };
  payload.id = focusRetry.id;
  focusSending = true;
  renderFocus();
  try {
    await boardAction("/api/requests", payload);
    focusDirty = false;
    focusRetry = null;
    $("focus-note").value = "";
    focusFeedback(
      "Focus saved. The skill applies it before choosing its next task.",
    );
    await refresh();
  } catch (error) {
    focusFeedback(error.message, true);
  } finally {
    focusSending = false;
    renderFocus();
  }
}

function initFocus() {
  buildFocusGrid();
  $("focus-save").addEventListener("click", saveFocus);
  $("focus-reset").addEventListener("click", () => {
    focusDirty = false;
    $("focus-feedback").hidden = true;
    renderFocus();
  });
  const setCollapsed = (collapsed) => {
    $("focus-panel").classList.toggle("collapsed", collapsed);
    $("focus-toggle").setAttribute("aria-expanded", String(!collapsed));
    $("focus-toggle").textContent = collapsed ? "Show" : "Hide";
  };
  try {
    const stored = localStorage.getItem(FOCUS_COLLAPSED_KEY);
    // Phones start collapsed so the board stays within reach.
    setCollapsed(stored === null ? document.documentElement.classList.contains("mobile") : stored === "1");
  } catch (error) {
    // Blocked storage only loses the remembered panel state.
    setCollapsed(false);
  }
  $("focus-toggle").addEventListener("click", () => {
    const collapsed = !$("focus-panel").classList.contains("collapsed");
    setCollapsed(collapsed);
    try {
      localStorage.setItem(FOCUS_COLLAPSED_KEY, collapsed ? "1" : "0");
    } catch (error) {
      focusFeedback("Browser storage is blocked; the panel opens expanded next time.");
    }
  });
}
