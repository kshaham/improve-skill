"use strict";
const $ = (id) => document.getElementById(id);
let snapshot = null,
  cardSignature = "",
  recentSignature = "",
  historySignature = "",
  activeView = "board",
  historyOffset = 0,
  historyAnchor = null,
  selectedTask = null,
  timer = null,
  fetching = false;
const FINISHED = new Set(["done", "rejected"]);
const PAGE_SIZE = 20,
  RECENT_LIMIT = 5;
const countText = (value) => value.toLocaleString();
const labels = {
  ready: "Queued",
  in_progress: "In progress",
  done: "Done",
  blocked: "Blocked",
  proposed: "Proposed",
  rejected: "Rejected",
};
const readable = (value) =>
  value == null
    ? ""
    : typeof value === "string"
      ? value
      : JSON.stringify(value, null, 2);
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}
function options(select, entries) {
  const chosen = select.value;
  const signature = JSON.stringify(entries);
  if (select.dataset.options === signature) return;
  select.replaceChildren(
    ...entries.map(([value, title]) => {
      const option = element("option", "", title);
      option.value = value;
      return option;
    }),
  );
  if (entries.some(([value]) => value === chosen)) select.value = chosen;
  select.dataset.options = signature;
}
function filteredTasks() {
  const query = $("search").value.trim().toLowerCase();
  return snapshot.tasks.filter(
    (task) =>
      ($("area").value === "all" || task.area === $("area").value) &&
      (!query || readable(task).toLowerCase().includes(query)),
  );
}
function openTask(task) {
  selectedTask = task.key;
  taskControls(task);
  $("detail-meta").textContent = [task.id, labels[task.status], task.area].join(
    " · ",
  );
  $("detail-title").textContent = task.title;
  const sections = [
    ["Run", task.run_id === "current" ? "Current run" : task.run_id],
    ["Description", task.claim !== task.title ? task.claim : null],
    ["Evidence", task.evidence],
    ["Acceptance checks", task.acceptance],
    ["Verification", task.verification || task.last_verification],
    ["Notes", task.note],
    ["Failure scenario", task.failure],
    ["Counter-scenario", task.counter],
    [
      "Files",
      task.paths ||
        task.owned_paths ||
        (task.file ? task.file + (task.line ? ":" + task.line : "") : null),
    ],
    ["Commit", task.commit],
    ["Measurements", task.measurement],
    ["Bet phase", task.bet_status],
    ["Journey", task.journey],
    ["Target", task.target],
    ["Claimed gain", task.claimed_gain],
    ["Spike measurements", task.spike],
    ["Implementation pieces", task.pieces],
    ["Landed measurements", task.landed],
    ["Kill criteria", task.kill],
    ["Branch", task.branch],
    ["Updated", task.updated_at || task.completed_at || task.created_at],
  ];
  $("detail-content").replaceChildren(
    ...sections
      .filter(([, value]) => value != null && value !== "")
      .map(([title, value]) => {
        const section = element(
          "section",
          "detail-section" +
            (["Files", "Commit", "Measurements"].includes(title)
              ? " mono"
              : ""),
        );
        section.append(
          element("h3", "", title),
          element("pre", "", readable(value)),
        );
        return section;
      }),
  );
  if (!$("task-dialog").open) $("task-dialog").showModal();
}
function taskCard(task) {
  const card = element("button", "task-card");
  card.type = "button";
  card.dataset.key = task.key;
  card.setAttribute("aria-label", task.title + ", " + labels[task.status]);
  const eyebrow = element("div", "card-eyebrow");
  eyebrow.append(element("span", "task-id", task.id));
  if (task.user_priority)
    eyebrow.append(element("span", "priority", task.user_priority));
  else if (task.focus_priority != null || task.priority != null)
    eyebrow.append(
      element(
        "span",
        "priority",
        "P" + readable(task.focus_priority ?? task.priority).replace(/^p/i, ""),
      ),
    );
  card.append(eyebrow, element("h3", "", task.title));
  const note = task.note || task.evidence || task.acceptance || task.file;
  if (note) card.append(element("p", "card-note", readable(note)));
  const footer = element("div", "card-footer");
  footer.append(element("span", "area-tag", task.area));
  if (typeof task.commit === "string" && task.commit)
    footer.append(element("span", "commit-tag", task.commit.slice(0, 7)));
  else if (task.run_id !== "current")
    footer.append(element("span", "", "Archived"));
  else footer.append(element("span", "", "View details ↗"));
  card.append(footer);
  card.addEventListener("click", () => openTask(task));
  return card;
}
function recordedDate(task) {
  for (const field of ["completed_at", "updated_at", "created_at"]) {
    const epoch = Date.parse(task[field]);
    if (Number.isFinite(epoch)) return { epoch, field };
  }
  return { epoch: null, field: null };
}
function newestFirst(tasks) {
  return [...tasks].sort(
    (a, b) =>
      (recordedDate(b).epoch ?? -Infinity) -
        (recordedDate(a).epoch ?? -Infinity) || a.key.localeCompare(b.key),
  );
}
function historyTasks(tasks = filteredTasks()) {
  const outcome = $("history-outcome").value;
  return newestFirst(
    tasks.filter(
      (task) =>
        FINISHED.has(task.status) &&
        (outcome === "all" || task.status === outcome),
    ),
  );
}
function taskRow(task) {
  const item = element("div", "task-row-item");
  item.setAttribute("role", "listitem");
  const row = element("button", "task-row");
  row.type = "button";
  row.dataset.key = task.key;
  row.setAttribute(
    "aria-label",
    task.title + ", " + labels[task.status] + ", " + task.area,
  );
  const title = element("span", "row-title");
  const name = element("strong", "", task.title);
  name.title = task.title;
  title.append(
    name,
    element(
      "span",
      "row-id",
      task.id + (task.run_id !== "current" ? " · " + task.run_id : ""),
    ),
  );
  const outcome = element(
    "span",
    "row-outcome " + task.status,
    task.status === "done" ? "Completed" : "Rejected",
  );
  const area = element("span", "row-area", task.area);
  const recorded = recordedDate(task),
    date = element("span", "row-date", "Date not recorded");
  if (recorded.epoch !== null) {
    const value = new Date(recorded.epoch);
    date.textContent = value.toLocaleDateString([], {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
    date.title =
      {
        completed_at: "Completed",
        updated_at: "Updated",
        created_at: "Created",
      }[recorded.field] +
      ": " +
      value.toLocaleString();
  }
  const commit = element(
    "span",
    "row-commit",
    typeof task.commit === "string" && task.commit
      ? task.commit.slice(0, 7)
      : "—",
  );
  row.append(title, outcome, area, date, commit);
  row.addEventListener("click", () => openTask(task));
  item.append(row);
  return item;
}
function replaceRows(id, tasks) {
  const focus = $(id).contains(document.activeElement)
    ? document.activeElement.dataset.key
    : null;
  $(id).replaceChildren(...tasks.map(taskRow));
  if (focus)
    [...$(id).querySelectorAll(".task-row")]
      .find((row) => row.dataset.key === focus)
      ?.focus({ preventScroll: true });
}
function renderBoard(tasks) {
  const columns = snapshot.columns.filter((column) => !FINISHED.has(column.id));
  const signature = JSON.stringify([tasks, columns]);
  if (signature === cardSignature) return;
  cardSignature = signature;
  const focus = document.activeElement?.dataset.key;
  const scrollLeft = $("board").scrollLeft;
  $("board").replaceChildren(
    ...columns.map((column) => {
      const section = element("section", "column");
      section.dataset.status = column.id;
      section.setAttribute("aria-label", column.label);
      const heading = element("div", "column-heading");
      const items = tasks.filter((task) => task.status === column.id);
      heading.append(
        element("span", "column-dot"),
        element("span", "", column.label),
        element("span", "column-count", items.length),
      );
      const list = element("div", "card-list");
      list.append(...items.map(taskCard));
      if (!items.length)
        list.append(element("div", "column-empty", "No tasks here"));
      section.append(heading, list);
      return section;
    }),
  );
  $("board").scrollLeft = scrollLeft;
  if (focus)
    [...document.querySelectorAll(".task-card")]
      .find((card) => card.dataset.key === focus)
      ?.focus({ preventScroll: true });
}
function renderRecent(tasks) {
  const done = newestFirst(tasks.filter((task) => task.status === "done"));
  const rejected = tasks.filter((task) => task.status === "rejected").length;
  $("recent-section").hidden = !done.length && !rejected;
  $("recent-summary").textContent = done.length
    ? "Latest " +
      Math.min(RECENT_LIMIT, done.length) +
      " of " +
      countText(done.length) +
      " completed tasks"
    : "No completed tasks match these filters.";
  $("view-completed").hidden = !done.length;
  $("rejected-shortcut").hidden = !rejected;
  $("rejected-shortcut").textContent = countText(rejected) + " rejected";
  const recent = done.slice(0, RECENT_LIMIT),
    signature = JSON.stringify(recent);
  if (signature !== recentSignature) {
    recentSignature = signature;
    replaceRows("recent-list", recent);
  }
}
function renderHistory(tasks) {
  const rows = historyTasks(tasks);
  if (historyOffset > 0 && historyAnchor) {
    const index = rows.findIndex((task) => task.key === historyAnchor);
    if (index >= 0) historyOffset = index;
  }
  if (historyOffset >= rows.length)
    historyOffset = Math.max(
      0,
      Math.floor((rows.length - 1) / PAGE_SIZE) * PAGE_SIZE,
    );
  const page = rows.slice(historyOffset, historyOffset + PAGE_SIZE);
  historyAnchor = historyOffset > 0 ? page[0]?.key : null;
  $("history-empty").hidden = rows.length !== 0;
  $("history-range").textContent = rows.length
    ? countText(historyOffset + 1) +
      "–" +
      countText(historyOffset + page.length) +
      " of " +
      countText(rows.length) +
      " tasks"
    : "0 tasks";
  $("history-first").disabled = $("history-prev").disabled =
    historyOffset === 0;
  $("history-next").disabled = historyOffset + PAGE_SIZE >= rows.length;
  const signature = JSON.stringify(page);
  if (signature !== historySignature) {
    historySignature = signature;
    replaceRows("history-list", page);
  }
}
function renderCards() {
  if (!snapshot) return;
  const tasks = filteredTasks(),
    active = tasks.filter((task) => !FINISHED.has(task.status));
  $("visible-count").textContent = countText(tasks.length) + " matching tasks";
  $("board-count").textContent = countText(active.length);
  $("history-count").textContent = countText(tasks.length - active.length);
  $("board-view").hidden = activeView !== "board";
  $("history-view").hidden = activeView !== "history";
  $("requests-view").hidden = activeView !== "requests";
  $("area-filter").hidden = activeView === "requests";
  $("search").placeholder =
    activeView === "requests"
      ? "Search requests and responses…"
      : "Search tasks, files, evidence…";
  $("search").setAttribute(
    "aria-label",
    activeView === "requests" ? "Search requests" : "Search tasks",
  );
  $("export").textContent =
    activeView === "requests" ? "Export requests ↗" : "Export tasks ↗";
  $("export").title =
    activeView === "requests"
      ? "Export all matching requests, including every page"
      : activeView === "history"
        ? "Export all matching history, including every page"
        : "Export all matching tasks, including finished work";
  for (const view of ["board", "history", "requests"]) {
    $(view + "-tab").setAttribute("aria-selected", String(activeView === view));
    $(view + "-tab").tabIndex = activeView === view ? 0 : -1;
  }
  if (activeView === "board") {
    $("history-list").replaceChildren();
    historySignature = "";
    $("empty").hidden = snapshot.tasks.length !== 0;
    $("active-empty").hidden = !snapshot.tasks.length || active.length !== 0;
    $("active-empty-note").textContent =
      $("search").value || $("area").value !== "all"
        ? "No active tasks match these filters. Finished matches are available in History."
        : "Completed and rejected work is available in History.";
    $("board").hidden = !active.length;
    renderBoard(active);
    renderRecent(tasks);
  } else {
    $("board").replaceChildren();
    $("recent-list").replaceChildren();
    cardSignature = recentSignature = "";
    if (activeView === "history") renderHistory(tasks);
    else {
      $("history-list").replaceChildren();
      historySignature = "";
      renderRequests();
    }
  }
}
function resetHistory() {
  historyOffset = 0;
  historyAnchor = null;
}
function selectView(view, outcome) {
  activeView = view;
  if (outcome) {
    $("history-outcome").value = outcome;
    resetHistory();
  }
  renderCards();
}
function changeFilters() {
  resetHistory();
  requestOffset = 0;
  requestAnchor = null;
  renderCards();
}
function historyPage(direction) {
  historyOffset =
    direction === 0 ? 0 : Math.max(0, historyOffset + direction * PAGE_SIZE);
  historyAnchor = null;
  renderCards();
}
function remaining() {
  if (!snapshot) return;
  const run = snapshot.current,
    deadline = Date.parse(run.deadline);
  if (!Number.isFinite(deadline) || run.outcome) {
    $("remaining").textContent = "";
    return;
  }
  const seconds = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
  const h = Math.floor(seconds / 3600),
    m = Math.floor((seconds % 3600) / 60),
    s = seconds % 60;
  $("remaining").textContent = seconds
    ? (h ? h + "h " : "") + m + "m " + s + "s left"
    : "Deadline reached";
}
function renderHealth(run) {
  $("run-health").hidden = !run.started_at && !run.supervised;
  const metadata = [run.engine, run.model, "Cycle " + run.cycle].filter(
    Boolean,
  );
  if (run.phase) metadata.push("Phase: " + run.phase);
  $("run-meta").textContent = metadata.join(" · ");
  const formatTime = (value) =>
    Number.isFinite(Date.parse(value))
      ? new Date(value).toLocaleString()
      : "not recorded";
  const checkpoints = ["Work checkpoint: " + formatTime(run.last_activity_at)];
  if (run.supervised)
    checkpoints.push("Supervisor checkpoint: " + formatTime(run.heartbeat_at));
  $("run-checkpoint").textContent = checkpoints.join(" · ");
  const notices = [];
  if (run.summary_pending)
    notices.push(
      "Final report pending. Completion of the narrative report has not been verified.",
    );
  if (run.supervised && !run.daemon_running && !run.outcome)
    notices.push(
      "The daemon is not running. This board remains available, but no supervised work is currently running.",
    );
  if (run.retry_at)
    notices.push(
      (run.daemon_running
        ? "Account-limit retry: "
        : "Saved account-limit retry: ") + formatTime(run.retry_at),
    );
  if (run.consecutive_failures)
    notices.push(
      "Consecutive failed or uncheckpointed cycles: " +
        run.consecutive_failures,
    );
  if (run.last_error) notices.push(readable(run.last_error));
  $("run-notice").hidden = !notices.length;
  $("run-notice").textContent = notices.join("\n");
}
function render(data) {
  snapshot = data;
  $("repo").textContent = data.repo;
  document.title = data.repo + " · Improve board";
  options($("run"), [
    ["all", "All runs"],
    ...data.runs.map((run) => [run.id, run.label]),
  ]);
  options($("area"), [
    ["all", "All areas"],
    ...Array.from(new Set(data.tasks.map((task) => task.area)))
      .sort()
      .map((area) => [area, area]),
  ]);
  $("total").textContent = countText(data.tasks.length);
  $("working").textContent = countText(data.counts.in_progress);
  $("completed").textContent = countText(data.counts.done);
  $("blocked").textContent = countText(
    data.counts.blocked + data.counts.proposed,
  );
  $("run-count").textContent =
    data.selected_run === "all"
      ? "Across " +
        data.runs.length +
        (data.runs.length === 1 ? " run" : " runs")
      : "In selected run";
  const run = data.current;
  $("run-state").textContent =
    (run.daemon_running && run.phase === "finalize"
      ? "Writing final report"
      : run.outcome) ||
    (run.daemon_running
      ? data.controls?.stop_requested
        ? "Stopping"
        : run.phase === "paused"
          ? "Paused"
          : data.controls?.pause_requested
            ? "Pause requested"
            : run.phase === "account-limit"
              ? "Waiting for account limit"
              : "Running"
      : run.supervised
        ? "Daemon not running"
        : run.started_at
          ? "Last run checkpoint"
          : "Tracking tasks");
  $("run-detail").textContent =
    run.next_action ||
    (run.outcome
      ? "The run has ended. Its task history stays available."
      : "Tasks update from the project ledger.");
  renderHealth(run);
  renderControls();
  $("warning").hidden = !data.warnings.length;
  $("warning").textContent = data.warnings.join("\n");
  const investigations = data.discovery.slice(0, 6);
  $("discovery-section").hidden = investigations.length === 0;
  $("discovery").replaceChildren(
    ...investigations.map((item) => {
      const node = element("div", "investigation");
      node.append(
        element(
          "strong",
          "",
          item.hypothesis || item.scope || "Discovery pass",
        ),
        element(
          "p",
          "",
          item.evidence || [item.lane, item.status].filter(Boolean).join(" · "),
        ),
      );
      return node;
    }),
  );
  $("updated").textContent =
    "Updated " +
    new Date(data.updated_at).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  renderCards();
  remaining();
  if ($("task-dialog").open && selectedTask) {
    const task = data.tasks.find((item) => item.key === selectedTask);
    if (task) openTask(task);
    else $("task-dialog").close();
  }
}
async function refresh() {
  if (fetching) return;
  fetching = true;
  $("refresh").disabled = true;
  clearTimeout(timer);
  const requestedRun = $("run").value;
  const controller = new AbortController(),
    timeout = setTimeout(() => controller.abort(), 7000);
  try {
    const response = await fetch(
      "/api/board?run=" + encodeURIComponent(requestedRun),
      { cache: "no-store", signal: controller.signal },
    );
    const data = await response.json();
    if (!response.ok)
      throw new Error(data.error || "Unable to read the ledger");
    connected = true;
    if ($("run").value === requestedRun) render(data);
    $("connection").textContent = "Board connected";
    $("connection").classList.remove("offline");
  } catch (error) {
    connected = false;
    renderControls();
    $("connection").textContent = "Disconnected · retrying";
    $("connection").classList.add("offline");
    $("warning").hidden = false;
    $("warning").textContent =
      "The board could not refresh. " +
      (snapshot ? "Showing the last received tasks. " : "") +
      error.message;
  } finally {
    clearTimeout(timeout);
    fetching = false;
    $("refresh").disabled = false;
    timer = setTimeout(
      refresh,
      $("run").value !== requestedRun ? 0 : document.hidden ? 10000 : 3000,
    );
  }
}
$("search").addEventListener("input", changeFilters);
$("area").addEventListener("change", changeFilters);
$("run").addEventListener("change", () => {
  resetHistory();
  requestOffset = 0;
  requestAnchor = null;
  refresh();
});
$("history-outcome").addEventListener("change", changeFilters);
$("board-tab").addEventListener("click", () => selectView("board"));
$("history-tab").addEventListener("click", () => selectView("history"));
$("completed-shortcut").addEventListener("click", () => {
  selectView("history", "done");
  $("history-tab").focus();
});
$("view-completed").addEventListener("click", () => {
  selectView("history", "done");
  $("history-tab").focus();
});
$("rejected-shortcut").addEventListener("click", () => {
  selectView("history", "rejected");
  $("history-tab").focus();
});
$("history-first").addEventListener("click", () => historyPage(0));
$("history-prev").addEventListener("click", () => historyPage(-1));
$("history-next").addEventListener("click", () => historyPage(1));
document.querySelector(".view-tabs").addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const views = ["board", "history", "requests"];
  const view =
    event.key === "Home"
      ? "board"
      : event.key === "End"
        ? "requests"
        : views[
            (views.indexOf(activeView) + (event.key === "ArrowRight" ? 1 : 2)) %
              views.length
          ];
  selectView(view);
  $(view + "-tab").focus();
});
$("refresh").addEventListener("click", refresh);
$("close-dialog").addEventListener("click", () => $("task-dialog").close());
$("task-dialog").addEventListener("click", (event) => {
  if (event.target === $("task-dialog")) $("task-dialog").close();
});
$("task-dialog").addEventListener("close", () => {
  selectedTask = null;
});
$("export").addEventListener("click", () => {
  if (!snapshot) return;
  const blob = new Blob(
    [
      JSON.stringify(
        {
          repo: snapshot.repo,
          exported_at: new Date().toISOString(),
          ...(activeView === "requests"
            ? { requests: filteredRequests() }
            : {
                tasks:
                  activeView === "history" ? historyTasks() : filteredTasks(),
              }),
        },
        null,
        2,
      ),
    ],
    { type: "application/json" },
  );
  const url = URL.createObjectURL(blob),
    link = element("a");
  link.href = url;
  link.download =
    activeView === "requests" ? "improve-requests.json" : "improve-tasks.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
setInterval(remaining, 1000);
initControls();
refresh();
