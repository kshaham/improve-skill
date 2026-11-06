"use strict";
// Mobile layout: chosen automatically for phones, overridable per browser.
const LAYOUT_KEY = "improve-board-layout";
const MOBILE_QUERIES = [
  "(max-width: 760px)",
  "(pointer: coarse) and (max-width: 1100px)",
];
const SWIPE_DISTANCE = 60;
let mobileColumn = "ready",
  layoutPreference = "auto",
  swipeStart = null,
  mobileReady = false;

function readLayoutPreference() {
  const requested = new URLSearchParams(location.search).get("layout");
  if (["auto", "mobile", "desktop"].includes(requested)) return requested;
  try {
    const stored = localStorage.getItem(LAYOUT_KEY);
    return ["mobile", "desktop"].includes(stored) ? stored : "auto";
  } catch (error) {
    // Blocked storage falls back to automatic detection every load.
    void error;
    return "auto";
  }
}

function deviceLooksMobile() {
  if (navigator.userAgentData?.mobile) return true;
  return MOBILE_QUERIES.some((query) => matchMedia(query).matches);
}

function isMobileLayout() {
  return layoutPreference === "mobile" ||
    (layoutPreference === "auto" && deviceLooksMobile());
}

function applyLayout() {
  const mobile = isMobileLayout();
  document.documentElement.classList.toggle("mobile", mobile);
  document.documentElement.dataset.layout = mobile ? "mobile" : "desktop";
  const toggle = document.getElementById("layout-toggle");
  if (toggle)
    toggle.textContent = mobile ? "Switch to desktop view" : "Switch to mobile view";
  // board.js declares the snapshot later; only touch columns after initMobile().
  if (mobile && mobileReady) markMobileColumn();
}

// Runs before first paint of the board so phones never flash the desktop layout.
layoutPreference = readLayoutPreference();
applyLayout();

function setLayoutPreference(preference) {
  layoutPreference = preference;
  try {
    if (preference === "auto") localStorage.removeItem(LAYOUT_KEY);
    else localStorage.setItem(LAYOUT_KEY, preference);
  } catch (error) {
    void error;
    feedback("Browser storage is blocked; the layout choice lasts until reload.", true);
  }
  applyLayout();
}

function columnIds() {
  return (snapshot?.columns || [])
    .filter((column) => !FINISHED.has(column.id))
    .map((column) => column.id);
}

function markMobileColumn() {
  const board = document.getElementById("board");
  if (!board) return;
  const ids = columnIds();
  if (ids.length && !ids.includes(mobileColumn)) mobileColumn = ids[0];
  for (const section of board.querySelectorAll(".column"))
    section.toggleAttribute("data-active", section.dataset.status === mobileColumn);
  for (const chip of document.querySelectorAll("#mobile-columns button"))
    chip.setAttribute("aria-pressed", String(chip.dataset.status === mobileColumn));
}

function boardTasks() {
  return filteredTasks().filter((task) => !FINISHED.has(task.status));
}

function renderMobile() {
  if (!snapshot) return;
  const chips = $("mobile-columns");
  chips.hidden = activeView !== "board" || $("work-layout").value === "rows";
  const tasks = boardTasks();
  const signature = JSON.stringify(
    columnIds().map((id) => [id, tasks.filter((task) => task.status === id).length]),
  );
  if (chips.dataset.signature !== signature) {
    chips.dataset.signature = signature;
    // New data or filters: never leave the phone on an empty column when others match.
    const counts = Object.fromEntries(JSON.parse(signature));
    if (!counts[mobileColumn]) {
      const firstMatch = columnIds().find((id) => counts[id] > 0);
      if (firstMatch) mobileColumn = firstMatch;
    }
    chips.replaceChildren(
      ...snapshot.columns
        .filter((column) => !FINISHED.has(column.id))
        .map((column) => {
          const chip = element("button", "mobile-chip");
          chip.type = "button";
          chip.dataset.status = column.id;
          chip.append(
            element("span", "", column.label),
            element(
              "span",
              "mobile-chip-count",
              countText(tasks.filter((task) => task.status === column.id).length),
            ),
          );
          chip.addEventListener("click", () => showColumn(column.id));
          return chip;
        }),
    );
  }
  const pending = (snapshot.requests || []).filter((row) => row.status === "pending").length;
  $("nav-requests-count").textContent = pending ? countText(pending) : "";
  $("nav-requests-count").hidden = !pending;
  for (const button of document.querySelectorAll("#mobile-nav [data-view]"))
    button.setAttribute("aria-current", String(button.dataset.view === activeView));
  markMobileColumn();
}

function showColumn(status) {
  mobileColumn = status;
  markMobileColumn();
  document.querySelector(`#mobile-columns [data-status="${status}"]`)
    ?.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
}

function stepColumn(direction) {
  const ids = columnIds();
  const next = ids.indexOf(mobileColumn) + direction;
  if (next >= 0 && next < ids.length) showColumn(ids[next]);
}

function navigate(view) {
  if (view === "focus") {
    const panel = $("focus-panel");
    if (panel.classList.contains("collapsed")) $("focus-toggle").click();
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  selectView(view);
  $(view + "-tab").scrollIntoView({ behavior: "smooth", block: "start" });
  renderMobile();
}

function initMobile() {
  mobileReady = true;
  for (const query of MOBILE_QUERIES)
    matchMedia(query).addEventListener("change", applyLayout);
  $("layout-toggle").addEventListener("click", () =>
    setLayoutPreference(isMobileLayout() ? "desktop" : "mobile"),
  );
  for (const button of document.querySelectorAll("#mobile-nav [data-view]"))
    button.addEventListener("click", () => navigate(button.dataset.view));
  $("nav-steer").addEventListener("click", () => openVoice());
  // Rendering replaces the columns; re-mark the visible one each time.
  new MutationObserver(() => isMobileLayout() && markMobileColumn()).observe($("board"), {
    childList: true,
  });
  $("board").addEventListener("touchstart", (event) => {
    const touch = event.touches[0];
    swipeStart = event.touches.length === 1 ? { x: touch.clientX, y: touch.clientY } : null;
  }, { passive: true });
  $("board").addEventListener("touchend", (event) => {
    if (!swipeStart || !isMobileLayout()) return;
    const touch = event.changedTouches[0];
    const dx = touch.clientX - swipeStart.x,
      dy = touch.clientY - swipeStart.y;
    swipeStart = null;
    if (Math.abs(dx) > SWIPE_DISTANCE && Math.abs(dx) > Math.abs(dy) * 1.5)
      stepColumn(dx < 0 ? 1 : -1);
  }, { passive: true });
  applyLayout();
}
