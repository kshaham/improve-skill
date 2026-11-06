"use strict";
// Quick improvement categories: each tile prefers its areas and names a theme
// the skill hunts for (references/categories.md). Ids match improve_control.THEMES.
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
