"use strict";
const selectedWork = new Set();
let selectionRun = null,
  workPageKeys = [],
  threadTask = null,
  threadLimit = 10,
  threadSignature = "";

function selectableTask(task) {
  return task.run_id === "current" && !FINISHED.has(task.status);
}

function clearSelection() {
  selectedWork.clear();
  renderSelection();
}

function selectTaskRow(item, task) {
  if (!selectableTask(task)) return;
  item.classList.add("selectable-task");
  const check = element("input", "task-select");
  check.type = "checkbox";
  check.dataset.key = task.key;
  check.setAttribute("aria-label", "Select " + task.title);
  check.addEventListener("change", () => {
    if (check.checked && selectedWork.size >= 50) {
      check.checked = false;
      feedback("Select up to 50 tasks at a time.", true);
      return;
    }
    if (check.checked) selectedWork.add(task.key);
    else selectedWork.delete(task.key);
    renderSelection();
  });
  item.prepend(check);
}

function renderSelection() {
  if (!snapshot) return;
  const identity = snapshot.current.control_id || null;
  if (selectionRun !== identity) {
    selectedWork.clear();
    selectionRun = identity;
  }
  const available = new Set(
    snapshot.tasks.filter(selectableTask).map((task) => task.key),
  );
  for (const key of selectedWork)
    if (!available.has(key)) selectedWork.delete(key);
  const canChange =
    connected && !sending && snapshot.controls.requests_available;
  const shown = new Set(
    orderedWork(
      filteredTasks().filter((task) => !FINISHED.has(task.status)),
    ).map((task) => task.key),
  );
  const hidden = [...selectedWork].filter((key) => !shown.has(key)).length;
  $("selection-count").textContent =
    `${selectedWork.size} selected${hidden ? " · " + hidden + " outside these filters" : ""}`;
  $("bulk-priority").disabled = !canChange || selectedWork.size === 0;
  $("clear-selection").disabled = selectedWork.size === 0;
  const eligiblePage = workPageKeys.filter((key) => available.has(key));
  $("select-page").disabled = !canChange || eligiblePage.length === 0;
  $("select-page").textContent =
    eligiblePage.length && eligiblePage.every((key) => selectedWork.has(key))
      ? "Deselect page"
      : "Select page";
  for (const check of $("work-list").querySelectorAll(".task-select")) {
    check.checked = selectedWork.has(check.dataset.key);
    check.disabled = !canChange;
  }
}

function openBulkPriority() {
  const tasks = snapshot.tasks.filter(
    (task) => selectedWork.has(task.key) && selectableTask(task),
  );
  if (!tasks.length) return;
  openRequest("priority");
  requestDraft = {
    type: "priority",
    run_id: snapshot.current.control_id || null,
    task_keys: tasks.map((task) => task.key).sort(),
  };
  requestRetry = null;
  draftContext = {
    text:
      `Change priority for these ${tasks.length} current tasks:\n\n` +
      tasks.map((task) => "• " + task.title.slice(0, 200)).join("\n"),
  };
  $("request-context").textContent = draftContext.text;
  $("request-title").textContent =
    "Set priority for " + tasks.length + " tasks";
  $("request-text").value = "";
  $("request-priority").value = "normal";
  const saved = savedDrafts()[draftKey()];
  if (saved) restoreDraft(saved);
  draftStatus();
}

function renderTaskThread(task) {
  if (threadTask !== task.key) {
    threadTask = task.key;
    threadLimit = 10;
    threadSignature = "";
  }
  const rows = [...(snapshot.requests || [])]
    .reverse()
    .filter((row) => row.task_keys?.includes(task.key));
  $("task-thread-section").hidden = rows.length === 0;
  $("task-thread-count").textContent =
    `${Math.min(threadLimit, rows.length)} of ${rows.length} requests`;
  $("task-thread-more").hidden = rows.length <= threadLimit;
  const page = rows.slice(0, threadLimit),
    signature = JSON.stringify(page);
  if (signature === threadSignature) return;
  threadSignature = signature;
  $("task-thread").replaceChildren(
    ...page.map((row) => {
      const item = element("article", "thread-item");
      item.append(
        element("strong", "", requestLabels[row.type] + " · " + row.status),
      );
      const meta = [
        row.created_at ? new Date(row.created_at).toLocaleString() : "",
        row.priority,
        row.decision,
      ].filter(Boolean);
      item.append(element("p", "request-meta", meta.join(" · ")));
      if (row.text) item.append(element("p", "request-message", row.text));
      item.append(
        element(
          "p",
          "request-message",
          row.response ||
            (row.source_run === "current"
              ? "Awaiting a skill checkpoint."
              : "Archived without a recorded response."),
        ),
      );
      return item;
    }),
  );
}

function initTriage() {
  $("clear-selection").addEventListener("click", clearSelection);
  $("select-page").addEventListener("click", () => {
    const keys = workPageKeys.filter((key) =>
      snapshot.tasks.some((task) => task.key === key && selectableTask(task)),
    );
    if (keys.every((key) => selectedWork.has(key)))
      keys.forEach((key) => selectedWork.delete(key));
    else {
      if (new Set([...selectedWork, ...keys]).size > 50) {
        feedback(
          "Select up to 50 tasks at a time. Clear some selections first.",
          true,
        );
        return;
      }
      keys.forEach((key) => selectedWork.add(key));
    }
    renderSelection();
  });
  $("bulk-priority").addEventListener("click", openBulkPriority);
  $("task-thread-more").addEventListener("click", () => {
    threadLimit += 10;
    const task = snapshot.tasks.find((task) => task.key === selectedTask);
    if (task) renderTaskThread(task);
  });
}
