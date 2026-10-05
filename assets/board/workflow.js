"use strict";
let workOffset = 0,
  workAnchor = null,
  workSignature = "",
  pendingView = null;
const viewFields = {
  run: ["run", "all"],
  area: ["area", "all"],
  q: ["search", ""],
  priority: ["priority-filter", "all"],
  layout: ["work-layout", "columns"],
  sort: ["work-sort", "priority"],
  outcome: ["history-outcome", "all"],
  status: ["request-status", "all"],
};

function userPriority(task) {
  return ["high", "normal", "low"].includes(task.user_priority)
    ? task.user_priority
    : "normal";
}

function orderedWork(tasks) {
  const sort = $("work-sort").value;
  const rank = { high: 0, normal: 1, low: 2 };
  const focus = (task) =>
    typeof task.focus_priority === "number" &&
    Number.isFinite(task.focus_priority)
      ? task.focus_priority
      : 999;
  return [...tasks].sort((a, b) => {
    if (sort === "title")
      return a.title.localeCompare(b.title) || a.key.localeCompare(b.key);
    if (sort === "updated")
      return (
        (recordedDate(b).epoch ?? -Infinity) -
          (recordedDate(a).epoch ?? -Infinity) || a.key.localeCompare(b.key)
      );
    return (
      rank[userPriority(a)] - rank[userPriority(b)] ||
      focus(a) - focus(b) ||
      a.key.localeCompare(b.key)
    );
  });
}

function resetWork() {
  workOffset = 0;
  workAnchor = null;
}

function renderWorklist(rows) {
  if (workAnchor) {
    const index = rows.findIndex((task) => task.key === workAnchor);
    if (index >= 0) workOffset = index;
  }
  if (workOffset >= rows.length)
    workOffset = Math.max(
      0,
      Math.floor((rows.length - 1) / PAGE_SIZE) * PAGE_SIZE,
    );
  const page = rows.slice(workOffset, workOffset + PAGE_SIZE);
  workAnchor = workOffset > 0 ? page[0]?.key : null;
  $("work-range").textContent = rows.length
    ? `${countText(workOffset + 1)}–${countText(workOffset + page.length)} of ${countText(rows.length)} active tasks`
    : "0 active tasks";
  $("work-first").disabled = $("work-prev").disabled = workOffset === 0;
  $("work-next").disabled = workOffset + page.length >= rows.length;
  const signature = JSON.stringify(page);
  if (signature !== workSignature) {
    workSignature = signature;
    replaceRows("work-list", page);
  }
}

function viewNotice(message) {
  $("view-notice").textContent = message;
  $("view-notice").hidden = !message;
}

function writeViewLocation() {
  if (pendingView) return;
  const params = new URLSearchParams();
  if (activeView !== "board") params.set("view", activeView);
  for (const [key, [id, fallback]] of Object.entries(viewFields)) {
    const value = $(id).value;
    if (value !== fallback) params.set(key, value);
  }
  if (selectedTask) {
    params.set("task", selectedTask);
    if (selectedTask.startsWith("current:"))
      params.set("task_run", snapshot.current.control_id || "~");
  }
  const fragment = params.toString();
  history.replaceState(
    null,
    "",
    location.pathname + location.search + (fragment ? "#" + fragment : ""),
  );
}

function readViewLocation() {
  pendingView = new URLSearchParams(location.hash.slice(1));
  viewNotice("");
  if ($("task-dialog").open) $("task-dialog").close();
  selectedTask = null;
  activeView = ["board", "history", "requests"].includes(
    pendingView.get("view"),
  )
    ? pendingView.get("view")
    : "board";
  for (const [key, [id, fallback]] of Object.entries(viewFields)) {
    const value = (pendingView.get(key) ?? fallback).slice(
      0,
      key === "q" ? 1000 : 200,
    );
    if (
      ["run", "area"].includes(key) &&
      ![...$(id).options].some((option) => option.value === value)
    ) {
      const option = element("option", "", value);
      option.value = value;
      $(id).append(option);
      delete $(id).dataset.options;
    }
    $(id).value = value;
    if ($(id).value !== value) $(id).value = fallback;
  }
  resetHistory();
  resetWork();
  requestOffset = 0;
  requestAnchor = null;
}

function finishViewRestore() {
  if (!pendingView) return;
  const desired = pendingView;
  pendingView = null;
  // Options arrive with the first snapshot, after the bookmarked values were read.
  const area = desired.get("area");
  if (area && [...$("area").options].some((option) => option.value === area))
    $("area").value = area;
  const key = desired.get("task");
  if (!key) return;
  const task = snapshot.tasks.find((task) => task.key === key);
  const sameRun =
    !key.startsWith("current:") ||
    desired.get("task_run") === (snapshot.current.control_id || "~");
  if (task && sameRun) openTask(task);
  else
    viewNotice(
      "This task link is no longer available in the selected run. Choose its archived run to find older work.",
    );
}

async function copyTaskLink() {
  writeViewLocation();
  $("task-link").value = location.href;
  $("task-link-field").hidden = false;
  try {
    await navigator.clipboard.writeText(location.href);
    $("task-link-note").textContent =
      "Task link copied. It opens on this computer while the board is running.";
  } catch {
    $("task-link-note").textContent =
      "Copy this task link. It opens on this computer while the board is running.";
    $("task-link").focus();
    $("task-link").select();
  }
}

function initWorkflow() {
  readViewLocation();
  window.addEventListener("hashchange", () => {
    readViewLocation();
    refresh();
  });
  for (const id of ["priority-filter", "work-layout", "work-sort"])
    $(id).addEventListener("change", changeFilters);
  for (const [id, direction] of [
    ["work-first", 0],
    ["work-prev", -1],
    ["work-next", 1],
  ]) {
    $(id).addEventListener("click", () => {
      workOffset =
        direction === 0 ? 0 : Math.max(0, workOffset + direction * PAGE_SIZE);
      workAnchor = null;
      renderCards();
    });
  }
  $("clear-filters").addEventListener("click", () => {
    for (const id of [
      "area",
      "priority-filter",
      "history-outcome",
      "request-status",
    ])
      $(id).value = "all";
    $("search").value = "";
    viewNotice("");
    changeFilters();
  });
  $("copy-task-link").addEventListener("click", copyTaskLink);
}
