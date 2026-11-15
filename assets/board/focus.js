"use strict";
// Parallel helpers: a switch and a 1-10 slider at the top of the focus panel. The choice travels
// with the focus request as `agents` (references/board.md); off sends no field, and the
// assistant then decides for itself when a helper is worth it.
const AGENTS_MAX = 10;
const AGENTS_DEFAULT = 5;
let focusAgents = 0, // What a save would send: 0 is off.
  agentsChoice = AGENTS_DEFAULT; // The slider's value, kept while the switch is off.

function agentsLabel(count) {
  return count === 1 ? "1 agent" : `${count} agents`;
}

function buildAgents() {
  // One seat per possible helper; the lit ones are the team.
  for (let index = 0; index < AGENTS_MAX; index++) {
    const seat = element("i");
    seat.style.setProperty("--i", index);
    $("agents-track").append(seat);
  }
  $("agents-enabled").addEventListener("change", (event) => {
    focusAgents = event.target.checked ? agentsChoice : 0;
    focusDirty = true;
    renderFocus();
  });
  $("agents-count").addEventListener("input", (event) => {
    agentsChoice = Math.min(AGENTS_MAX, Math.max(1, Number(event.target.value) || AGENTS_DEFAULT));
    focusAgents = agentsChoice;
    focusDirty = true;
    renderFocus();
  });
}

function renderAgents(allowed) {
  const on = focusAgents > 0;
  if (on) agentsChoice = focusAgents;
  const control = $("agents-control"),
    slider = $("agents-count");
  control.dataset.on = String(on);
  control.style.setProperty("--ratio", String((agentsChoice - 1) / (AGENTS_MAX - 1)));
  $("agents-enabled").checked = on;
  $("agents-enabled").disabled = !allowed;
  slider.disabled = !allowed || !on;
  if (Number(slider.value) !== agentsChoice) slider.value = String(agentsChoice);
  slider.setAttribute("aria-valuetext", agentsLabel(agentsChoice));
  [...$("agents-track").children].forEach((seat, index) =>
    seat.toggleAttribute("data-lit", on && index < agentsChoice),
  );
  tickText($("agents-value"), String(agentsChoice));
  $("agents-note").textContent = on
    ? `Up to ${agentsLabel(agentsChoice)} at a time, for finding and reviewing work. This uses more tokens.`
    : "Off: the assistant decides when a helper is worth it.";
}

// Area focus: steer which categories the skill works on, from the board.
const FOCUS_AREAS = [
  ["features", "Features", "New or finished user-facing behavior", 206],
  ["ui", "UI", "Layout, states, and visual polish", 340],
  ["accessibility", "Accessibility", "Keyboard, contrast, screen readers", 194],
  ["assets", "Assets", "Images, icons, fonts, media", 76],
  ["performance", "Performance", "Speed, memory, and load time", 28],
  ["gate-speed", "Gate speed", "Faster builds and test runs", 42],
  ["security", "Security", "Vulnerabilities and privacy", 356],
  ["resilience", "Resilience", "Errors, retries, and recovery", 14],
  ["concurrency", "Concurrency", "Races, locking, async safety", 220],
  ["quality", "Quality", "Bugs, dead code, clarity", 134],
  ["coverage", "Coverage", "Missing and weak tests", 166],
  ["contracts", "Contracts", "APIs, schemas, interfaces", 182],
  ["docs", "Docs", "Guides, references, comments", 54],
  ["general", "General", "Anything that fits no other area", 100],
];
const FOCUS_COLLAPSED_KEY = "improve-board-focus-collapsed";
const FOCUS_LEVEL_LABELS = { prefer: "Prefer", normal: "Normal", skip: "Skip" };
let focusSelection = {},
  focusDirty = false,
  focusRetry = null,
  focusSending = false;

function areaHue(area) {
  return FOCUS_AREAS.find((row) => row[0] === area)?.[3] ?? 100;
}

function focusSummary(areas = {}, themes = [], agents = 0) {
  const pick = (level) =>
    FOCUS_AREAS.filter((row) => areas[row[0]] === level).map((row) => row[1]);
  const prefer = pick("prefer"),
    skip = pick("skip");
  const balanced = !prefer.length && !skip.length && !themes.length;
  return [
    balanced ? "Balanced across all areas" : "",
    themes.length ? "Categories: " + themes.map(categoryName).join(", ") : "",
    prefer.length ? "Prefer " + prefer.join(", ") : "",
    skip.length ? "Skip " + skip.join(", ") : "",
    agents ? "In parallel: " + agentsLabel(agents) : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

function savedFocus() {
  // The latest current-run focus request wins until the skill records a newer one; a
  // declined or withdrawn request never shows as the focus.
  const rows = (snapshot?.controls?.requests || []).filter(
    (row) => row.type === "focus" && !["declined", "withdrawn"].includes(row.status),
  );
  const latest = rows[rows.length - 1];
  const applied = snapshot?.current?.focus || {};
  if (latest && (latest.status === "pending" || !applied.request_id))
    return { areas: latest.areas || {}, themes: latest.themes || [], agents: latest.agents || 0, request: latest };
  return { areas: applied.areas || {}, themes: applied.themes || [], agents: applied.agents || 0, request: null, applied };
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
  buildCategories();
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
  if (!focusDirty) {
    focusSelection = { ...saved.areas };
    focusThemes = [...saved.themes];
    focusAgents = saved.agents;
  }
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
  const changed =
    !sameFocus(focusSelection, saved.areas) ||
    !sameThemes(focusThemes, saved.themes) ||
    focusAgents !== saved.agents;
  if (focusDirty && !changed) focusDirty = false;
  const allowed =
    connected && !focusSending && !!snapshot.controls?.requests_available;
  $("focus-save").disabled = !allowed || !changed || skipped === FOCUS_AREAS.length;
  $("focus-reset").disabled = !changed;
  for (const input of $("focus-grid").querySelectorAll("input"))
    input.disabled = !allowed;
  renderCategories(allowed);
  renderAgents(allowed);
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
      : focusSummary(focusSelection, focusThemes, focusAgents) +
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
    themes: [...focusThemes],
    text: $("focus-note").value,
  };
  if (focusAgents) payload.agents = focusAgents;
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
  buildAgents();
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

// Quick improvement categories: each tile prefers its areas and names a theme
// the skill hunts for (references/board.md, "Improvement categories"). Ids match improve_control.THEMES.
const IMPROVEMENT_CATEGORIES = [
  ["ux", "UI/UX polish", "◧", "Hierarchy, spacing, empty/loading/error states, clearer flows", ["ui", "accessibility"]],
  ["animation", "Animation & transitions", "∿", "Smooth motion, feedback, reduced-motion support, no jank", ["ui", "performance"]],
  ["security", "Security hardening", "⛨", "Input validation, auth checks, secrets, vulnerable dependencies", ["security"]],
  ["scale", "Scaling & production", "▲", "Caching, query cost, timeouts, limits, graceful degradation", ["performance", "resilience", "concurrency"]],
  ["performance", "Speed & performance", "⚡", "Load time, rendering, memory, hot paths", ["performance", "gate-speed"]],
  ["accessibility", "Accessibility", "◎", "Keyboard, screen readers, contrast, focus order", ["accessibility"]],
  ["reliability", "Reliability & errors", "↻", "Error handling, retries, recovery, crash fixes", ["resilience", "quality"]],
  ["observability", "Logging & monitoring", "≋", "Structured logs, metrics, health checks, actionable errors", ["resilience", "quality"]],
  ["tests", "Test coverage", "✓", "Missing tests, weak assertions, flaky suites", ["coverage", "quality"]],
  ["features", "Feature completeness", "✚", "Finish half-built features and missing edge cases", ["features"]],
  ["mobile", "Mobile & responsive", "▯", "Small screens, touch targets, viewport and safe areas", ["ui", "accessibility"]],
  ["dx", "Developer experience", "⌘", "Faster builds, clearer scripts, easier onboarding", ["gate-speed", "docs", "quality"]],
  ["docs", "Documentation", "¶", "Guides, references, accurate comments", ["docs"]],
  ["api", "APIs & data contracts", "⇄", "Schema validation, versioning, typed interfaces", ["contracts"]],
  ["code-health", "Code health", "♻", "Dead code, duplication, complexity, clear names", ["quality"]],
  ["assets", "Assets & media", "▣", "Image optimization, icons, fonts, media loading", ["assets", "performance"]],
  ["privacy", "Privacy & compliance", "⚿", "Personal data handling, retention, consent", ["security", "contracts"]],
  ["i18n", "Internationalization", "文", "Hard-coded strings, locale formats, right-to-left", ["ui", "features"]],
  ["seo", "SEO & discoverability", "⌕", "Metadata, semantic markup, link previews, sitemaps", ["ui", "performance"]],
  ["concurrency", "Concurrency & races", "⇉", "Races, locking, async ordering, shared state", ["concurrency"]],
];
let focusThemes = [];

function categoryName(id) {
  return IMPROVEMENT_CATEGORIES.find((row) => row[0] === id)?.[1] || id;
}

function sameThemes(left = [], right = []) {
  return left.length === right.length && left.every((theme) => right.includes(theme));
}

function toggleCategory(id) {
  const category = IMPROVEMENT_CATEGORIES.find((row) => row[0] === id);
  if (!category) return;
  const next = { ...focusSelection };
  if (focusThemes.includes(id)) {
    focusThemes = focusThemes.filter((theme) => theme !== id);
    // Drop a preference only when no other chosen category still needs that area.
    const stillWanted = new Set(
      IMPROVEMENT_CATEGORIES.filter((row) => focusThemes.includes(row[0])).flatMap((row) => row[4]),
    );
    for (const area of category[4])
      if (next[area] === "prefer" && !stillWanted.has(area)) delete next[area];
  } else {
    focusThemes = IMPROVEMENT_CATEGORIES.map((row) => row[0]).filter(
      (theme) => theme === id || focusThemes.includes(theme),
    );
    for (const area of category[4]) next[area] = "prefer";
  }
  focusSelection = next;
  focusDirty = true;
  renderFocus();
}

function buildCategories() {
  const grid = $("focus-categories");
  for (const [id, name, glyph, hint, areas] of IMPROVEMENT_CATEGORIES) {
    const tile = element("button", "category-tile");
    tile.type = "button";
    tile.dataset.theme = id;
    tile.setAttribute("aria-pressed", "false");
    tile.style.setProperty("--hue", areaHue(areas[0]));
    tile.title = "Prefers: " + areas.join(", ");
    tile.append(
      element("span", "category-glyph", glyph),
      element("span", "category-name", name),
      element("span", "category-hint", hint),
    );
    tile.addEventListener("click", () => toggleCategory(id));
    grid.append(tile);
  }
  $("focus-balanced").addEventListener("click", () => {
    focusSelection = {};
    focusThemes = [];
    focusDirty = true;
    renderFocus();
  });
}

function renderCategories(allowed) {
  for (const tile of $("focus-categories").children) {
    tile.setAttribute("aria-pressed", String(focusThemes.includes(tile.dataset.theme)));
    tile.disabled = !allowed;
  }
  $("focus-balanced").disabled = !allowed;
  $("focus-category-count").textContent = focusThemes.length
    ? `${focusThemes.length} selected`
    : "Pick one or more";
}
