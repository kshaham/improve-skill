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
  fetchPromise = null,
  lastSnapshotAt = 0,
  snapshotBaseAt = 0,
  pollOk = false,
  pollFailures = 0,
  boardEtag = null,
  detailSignature = "",
  dialogOpener = null,
  columnLimits = {};
const FINISHED = new Set(["done", "rejected"]);
const PAGE_SIZE = 20,
  RECENT_LIMIT = 5,
  COLUMN_PAGE = 100;
// Polling: a failed poll keeps the controls usable while the last snapshot is fresh.
const POLL_MS = 3000,
  HIDDEN_POLL_MS = 10000,
  POLL_TIMEOUT_MS = 7000,
  MAX_BACKOFF_MS = 30000,
  FRESH_WINDOW_MS = 30000;
const countText = (value) => value.toLocaleString();
const searchTexts = new WeakMap();
function dataIsFresh() {
  return lastSnapshotAt > 0 && Date.now() - lastSnapshotAt < FRESH_WINDOW_MS;
}
function dataAgeSeconds() {
  return lastSnapshotAt ? Math.max(0, Math.round((Date.now() - lastSnapshotAt) / 1000)) : null;
}
function staleReason() {
  const age = dataAgeSeconds();
  return age === null
    ? "Connecting… Your saved requests and task history are preserved."
    : "No fresh data for " + age + " s. Controls return when the board reconnects; drafts are kept.";
}
// `connected` (controls.js) means "safe to act": the last poll succeeded, or data is recent.
function updateConnected() {
  const next = pollOk || dataIsFresh();
  if (next !== connected) {
    connected = next;
    renderControls();
  }
  return connected;
}
function renderUpdatedAge() {
  const age = dataAgeSeconds();
  if (age === null) {
    $("updated").textContent = "Waiting for data";
    return;
  }
  $("updated").textContent = "Updated " + age + " s ago" + (pollOk ? "" : " · last refresh failed");
  $("updated").title = "Last snapshot " + new Date(lastSnapshotAt).toLocaleTimeString();
}
function rememberOpener(node) {
  dialogOpener = node?.dataset?.key || node?.id || null;
}
function restoreOpenerFocus() {
  if (!dialogOpener || document.querySelector("dialog[open]")) return;
  const target =
    document.querySelector('[data-key="' + CSS.escape(dialogOpener) + '"]') ||
    document.getElementById(dialogOpener);
  dialogOpener = null;
  target?.focus({ preventScroll: true });
}
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
// The searchable text of a task is built once per snapshot, not once per keystroke.
function searchText(task) {
  let text = searchTexts.get(task);
  if (text === undefined) {
    text = readable(task).toLowerCase();
    searchTexts.set(task, text);
  }
  return text;
}
function filteredTasks() {
  const query = $("search").value.trim().toLowerCase();
  const area = $("area").value,
    priority = $("priority-filter").value;
  return snapshot.tasks.filter(
    (task) =>
      (area === "all" || task.area === area) &&
      (priority === "all" || userPriority(task) === priority) &&
      (!query || searchText(task).includes(query)),
  );
}
function openTaskFrom(opener, task) {
  if (!$("task-dialog").open) rememberOpener(opener);
  openTask(task);
}
function openTask(task) {
  if (selectedTask !== task.key) $("task-link-field").hidden = true;
  selectedTask = task.key;
  taskControls(task);
  renderTaskThread(task);
  $("detail-meta").textContent = [task.id, labels[task.status], task.area].join(
    " · ",
  );
  $("detail-title").textContent = task.title;
  // A blocked task leads with why, and who decided: the worker or the supervisor.
  const blockedReason = task.status === "blocked" ? readable(task.blocked_reason) : "";
  $("detail-blocked").hidden = !blockedReason;
  $("detail-blocked").textContent = blockedReason
    ? "Blocked" +
      (task.blocked_by ? " by the " + readable(task.blocked_by) : "") +
      (Number.isFinite(Date.parse(task.blocked_at))
        ? " at " + new Date(task.blocked_at).toLocaleString()
        : "") +
      (task.block_count > 1 ? " (" + countText(task.block_count) + " times)" : "") +
      ": " +
      blockedReason
    : "";
  const sections = [
    ["Run", task.run_id === "current" ? "Current run" : task.run_id],
    ["Supervisor note", task.supervisor_note],
    ["Description", task.claim !== task.title ? task.claim : null],
    ["Evidence", task.evidence],
    ["Acceptance checks", task.acceptance],
    ["Verification", task.verification || task.last_verification],
    ["Notes", task.note],
    [
      "User notes",
      Array.isArray(task.user_notes)
        ? task.user_notes
            .map((note) =>
              typeof note?.text === "string"
                ? (Number.isFinite(Date.parse(note.at))
                    ? new Date(note.at).toLocaleString() + "\n"
                    : "") + note.text
                : readable(note),
            )
            .join("\n\n")
        : task.user_notes,
    ],
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
  ].filter(([, value]) => value != null && value !== "");
  // Polls re-run openTask; unchanged content keeps its selection and scroll position.
  const signature = JSON.stringify([task.key, sections]);
  if (signature !== detailSignature) {
    detailSignature = signature;
    $("detail-content").replaceChildren(
      ...sections.map(([title, value]) => {
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
  }
  renderTaskKnowledge(task);
  if (!$("task-dialog").open) $("task-dialog").showModal();
  writeViewLocation();
}
function cardNote(task) {
  return (
    (task.status === "blocked" && task.blocked_reason) ||
    task.note ||
    task.evidence ||
    task.acceptance ||
    task.file ||
    ""
  );
}
function cardPriority(task) {
  if (task.user_priority) return task.user_priority;
  if (task.focus_priority != null || task.priority != null)
    return "P" + readable(task.focus_priority ?? task.priority).replace(/^p/i, "");
  return "";
}
// Everything a card shows; the board rebuilds only when one of these changes.
function cardSignatureOf(task) {
  return [
    task.key,
    task.status,
    task.title,
    task.area,
    cardPriority(task),
    cardNote(task),
    task.commit || "",
    savedFocus().areas[task.area] || "normal",
  ];
}
function taskCard(task) {
  const card = element("article", "task-card");
  card.tabIndex = 0;
  card.setAttribute("role", "button");
  card.dataset.key = task.key;
  card.dataset.area = task.area;
  card.dataset.areaFocus = savedFocus().areas[task.area] || "normal";
  card.style.setProperty("--hue", areaHue(task.area));
  const priority = cardPriority(task);
  card.setAttribute(
    "aria-label",
    [task.id, task.title, priority ? "priority " + priority : "", labels[task.status]]
      .filter(Boolean)
      .join(", "),
  );
  const eyebrow = element("div", "card-eyebrow");
  eyebrow.append(element("span", "task-id", task.id));
  if (priority) eyebrow.append(element("span", "priority", priority));
  card.append(eyebrow, element("h3", "", task.title));
  const note = cardNote(task);
  if (note) card.append(element("p", "card-note", readable(note)));
  const footer = element("div", "card-footer");
  const areaTag = element("span", "area-tag", task.area);
  areaTag.style.setProperty("--hue", areaHue(task.area));
  footer.append(areaTag);
  if (typeof task.commit === "string" && task.commit)
    footer.append(element("span", "commit-tag", task.commit.slice(0, 7)));
  else if (task.run_id !== "current")
    footer.append(element("span", "", "Archived"));
  else footer.append(element("span", "", "View details ↗"));
  card.append(footer);
  card.addEventListener("click", () => openTaskFrom(card, task));
  card.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    openTaskFrom(card, task);
  });
  return card;
}
function recordedDate(task) {
  const fields = FINISHED.has(task.status)
    ? ["completed_at", "updated_at", "created_at"]
    : ["updated_at", "created_at", "completed_at"];
  for (const field of fields) {
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
    task.status === "done" ? "Completed" : labels[task.status],
  );
  const area = element("span", "row-area", task.area);
  area.style.setProperty("--hue", areaHue(task.area));
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
  row.append(
    title,
    outcome,
    area,
    date,
    FINISHED.has(task.status)
      ? commit
      : element(
          "span",
          "row-priority " + userPriority(task),
          userPriority(task),
        ),
  );
  row.addEventListener("click", () => openTaskFrom(row, task));
  item.append(row);
  return item;
}
function replaceRows(id, tasks) {
  const focusSelector = document.activeElement?.classList.contains(
    "task-select",
  )
    ? ".task-select"
    : ".task-row";
  const focus = $(id).contains(document.activeElement)
    ? document.activeElement.dataset.key
    : null;
  $(id).replaceChildren(
    ...tasks.map((task) => {
      const item = taskRow(task);
      if (id === "work-list") selectTaskRow(item, task);
      return item;
    }),
  );
  if (focus)
    [...$(id).querySelectorAll(focusSelector)]
      .find((row) => row.dataset.key === focus)
      ?.focus({ preventScroll: true });
}
function showMoreButton(column, remaining) {
  const more = element(
    "button",
    "show-more",
    "Show " + countText(Math.min(COLUMN_PAGE, remaining)) + " more of " +
      countText(remaining) + " remaining",
  );
  more.type = "button";
  more.dataset.column = column.id;
  more.addEventListener("click", () => {
    columnLimits[column.id] = (columnLimits[column.id] || COLUMN_PAGE) + COLUMN_PAGE;
    cardSignature = "";
    renderCards();
  });
  return more;
}
function renderBoard(tasks) {
  const columns = snapshot.columns.filter((column) => !FINISHED.has(column.id));
  const signature = JSON.stringify([tasks.map(cardSignatureOf), columns, columnLimits]);
  if (signature === cardSignature) return;
  cardSignature = signature;
  const focus = document.activeElement?.dataset.key || document.activeElement?.dataset.column;
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
      const shown = items.slice(0, columnLimits[column.id] || COLUMN_PAGE);
      list.append(...shown.map(taskCard));
      if (items.length > shown.length)
        list.append(showMoreButton(column, items.length - shown.length));
      if (!items.length)
        list.append(element("div", "column-empty", "No tasks here"));
      section.append(heading, list);
      return section;
    }),
  );
  $("board").scrollLeft = scrollLeft;
  if (focus)
    [...document.querySelectorAll(".task-card, .show-more")]
      .find((card) => card.dataset.key === focus || card.dataset.column === focus)
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
    active = orderedWork(tasks.filter((task) => !FINISHED.has(task.status)));
  $("visible-count").textContent = countText(tasks.length) + " matching tasks";
  $("board-count").textContent = countText(active.length);
  $("history-count").textContent = countText(tasks.length - active.length);
  $("board-view").hidden = activeView !== "board";
  $("history-view").hidden = activeView !== "history";
  $("requests-view").hidden = activeView !== "requests";
  $("overview-view").hidden = activeView !== "overview";
  $("graph-view").hidden = activeView !== "graph";
  $("area-filter").hidden = ["requests", "overview", "graph"].includes(activeView);
  $("priority-filter-label").hidden = ["requests", "overview", "graph"].includes(
    activeView,
  );
  $("search").closest("label").hidden = ["overview", "graph"].includes(activeView);
  $("clear-filters").hidden = ["overview", "graph"].includes(activeView);
  $("work-sort-note").hidden = $("work-sort").value !== "priority";
  $("search").placeholder =
    activeView === "requests"
      ? "Search requests and responses…"
      : "Search tasks, files, evidence…";
  $("search").setAttribute(
    "aria-label",
    activeView === "requests" ? "Search requests" : "Search tasks",
  );
  $("export").textContent =
    activeView === "requests"
      ? "Export requests ↗"
      : activeView === "overview"
        ? "Export overview ↗"
        : activeView === "graph"
          ? "Export graph ↗"
          : "Export tasks ↗";
  $("export").title =
    activeView === "graph"
      ? "Export the nodes and links currently shown"
      : activeView === "overview"
      ? "Export area totals and all matching recorded activity"
      : activeView === "requests"
        ? "Export all matching requests, including every page"
        : activeView === "history"
          ? "Export all matching history, including every page"
          : $("work-layout").value === "rows"
            ? "Export every matching active task, including every worklist page"
            : "Export all matching tasks, including finished work";
  renderGraphBadge();
  for (const view of ["board", "history", "overview", "requests", "graph"]) {
    $(view + "-tab").setAttribute("aria-selected", String(activeView === view));
    $(view + "-tab").tabIndex = activeView === view ? 0 : -1;
  }
  if (activeView === "board") {
    $("history-list").replaceChildren();
    historySignature = "";
    $("empty").hidden = snapshot.tasks.length !== 0;
    $("active-empty").hidden = !snapshot.tasks.length || active.length !== 0;
    $("active-empty-note").textContent =
      $("search").value ||
      $("area").value !== "all" ||
      $("priority-filter").value !== "all"
        ? "No active tasks match these filters. Finished matches are available in History."
        : "Completed and rejected work is available in History.";
    const rows = $("work-layout").value === "rows";
    $("board").hidden = !active.length || rows;
    $("worklist").hidden = !rows;
    if (rows) {
      $("board").replaceChildren();
      cardSignature = "";
      renderWorklist(active);
    } else {
      $("work-list").replaceChildren();
      workSignature = "";
      renderBoard(active);
    }
    renderRecent(tasks);
  } else {
    $("board").replaceChildren();
    $("work-list").replaceChildren();
    workSignature = "";
    $("recent-list").replaceChildren();
    cardSignature = recentSignature = "";
    if (activeView === "history") renderHistory(tasks);
    else {
      $("history-list").replaceChildren();
      historySignature = "";
      if (activeView === "overview") renderOverview();
      else if (activeView === "graph") renderGraphView();
      else renderRequests();
    }
  }
  writeViewLocation();
  renderSelection(tasks);
  renderMobile(tasks);
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
  // Learning and graph counters are fetched only for the tabs that show them.
  if (
    (view === "overview" && !snapshot?.learning) ||
    (view === "graph" && !snapshot?.graph)
  )
    refresh();
}
function changeFilters() {
  resetHistory();
  resetWork();
  requestOffset = 0;
  requestAnchor = null;
  columnLimits = {};
  renderCards();
}
function historyPage(direction) {
  historyOffset =
    direction === 0 ? 0 : Math.max(0, historyOffset + direction * PAGE_SIZE);
  historyAnchor = null;
  renderCards();
}
// The server's clock drives the countdown; the browser only adds the time since the snapshot.
function remaining() {
  if (!snapshot) return;
  const run = snapshot.current,
    clock = run.clock;
  if (!clock || !Number.isFinite(clock.remaining_seconds) || run.outcome) {
    $("remaining").textContent = "";
    return;
  }
  const seconds = Math.max(
    0,
    Math.ceil(clock.remaining_seconds - (Date.now() - snapshotBaseAt) / 1000),
  );
  const h = Math.floor(seconds / 3600),
    m = Math.floor((seconds % 3600) / 60),
    s = seconds % 60;
  $("remaining").textContent = seconds
    ? (h ? h + "h " : "") + m + "m " + s + "s left"
    : "Deadline reached";
  // Only a deadline this page watched run out is an event; an already-past one is just state.
  if (!seconds && clock.remaining_seconds > 0) healthDeadlineReached(run);
}
function tickClock() {
  remaining();
  renderUpdatedAge();
  renderHealthAges();
  updateConnected();
  if (!connected && snapshot) $("control-note").textContent = staleReason();
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
  renderHealthExtras(run);
}
function render(data) {
  if (
    snapshot &&
    snapshot.current.control_id !== data.current.control_id &&
    selectedTask?.startsWith("current:")
  ) {
    $("task-dialog").close();
    selectedTask = null;
    viewNotice(
      "The current run changed. Reopen a task from the updated board.",
    );
  }
  // Sections the poll did not request keep their last known value until their tab asks again.
  if (snapshot) {
    if (data.learning == null) data.learning = snapshot.learning;
    if (data.graph == null) data.graph = snapshot.graph;
    detectTransitions(snapshot, data);
  }
  snapshot = data;
  $("repo").textContent = data.repo;
  document.title =
    (healthAlert(data.current) ? "⚠ " : "") + data.repo + " · Improve board";
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
  finishViewRestore();
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
        ? run.phase === "crashed"
          ? "Daemon crashed"
          : "Daemon not running"
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
  renderUpdatedAge();
  renderCards();
  remaining();
  if ($("task-dialog").open && selectedTask) {
    const task = data.tasks.find((item) => item.key === selectedTask);
    if (task) openTask(task);
    else $("task-dialog").close();
  }
}
function boardUrl(run) {
  const include = [];
  if (activeView === "overview") include.push("learning");
  if (activeView === "graph" || !snapshot || $("task-dialog").open)
    include.push("graph");
  return (
    "/api/board?run=" + encodeURIComponent(run) + "&include=" + include.join(",")
  );
}
async function responseJSON(response) {
  try {
    return await response.json();
  } catch (error) {
    void error;
    return { error: "The board answered with status " + response.status + "." };
  }
}
function markPollSuccess() {
  pollOk = true;
  pollFailures = 0;
  lastSnapshotAt = Date.now();
  $("connection").textContent = "Board connected";
  $("connection").classList.remove("offline");
  if (snapshot) {
    $("warning").hidden = !snapshot.warnings.length;
    $("warning").textContent = snapshot.warnings.join("\n");
  }
  updateConnected();
}
function markPollFailure(message) {
  pollOk = false;
  pollFailures += 1;
  $("connection").textContent = "Disconnected · retrying";
  $("connection").classList.add("offline");
  $("warning").hidden = false;
  const age = dataAgeSeconds();
  $("warning").textContent =
    "The board could not refresh. " +
    (snapshot ? "Showing the last received tasks (" + age + " s old). " : "") +
    message;
  updateConnected();
  renderUpdatedAge();
}
function pollDelay(requestedRun) {
  if ($("run").value !== requestedRun) return 0;
  if (!pollOk)
    return Math.min(MAX_BACKOFF_MS, POLL_MS * 2 ** Math.max(0, pollFailures - 1));
  return document.hidden ? HIDDEN_POLL_MS : POLL_MS;
}
async function poll() {
  $("refresh").disabled = true;
  clearTimeout(timer);
  const requestedRun = $("run").value;
  const url = boardUrl(requestedRun);
  const controller = new AbortController(),
    timeout = setTimeout(() => controller.abort(), POLL_TIMEOUT_MS);
  try {
    if (navigator.onLine === false) {
      markPollFailure("This browser is offline.");
      return;
    }
    const headers = {};
    if (boardEtag && boardEtag.url === url)
      headers["If-None-Match"] = boardEtag.value;
    const response = await fetch(url, {
      cache: "no-store",
      signal: controller.signal,
      headers,
    });
    if ($("run").value !== requestedRun) return;
    if (response.status === 304) {
      // Nothing changed on disk; the snapshot stays and only its age resets. A bookmark
      // read since the last render still needs one to apply its filters and task.
      markPollSuccess();
      if (pendingView && snapshot) render(snapshot);
      return;
    }
    const data = await responseJSON(response);
    if (!response.ok && data.code === "unknown_run") {
      $("run").value = "all";
      pendingView = null;
      viewNotice(
        "The bookmarked run is no longer available. Showing all available runs.",
      );
      writeViewLocation();
      return;
    }
    if (!response.ok)
      throw new Error(data.error || "Unable to read the ledger");
    boardEtag = { url, value: response.headers.get("ETag") };
    snapshotBaseAt = Date.now();
    markPollSuccess();
    render(data);
  } catch (error) {
    markPollFailure(
      error.name === "AbortError"
        ? "The board took more than " + POLL_TIMEOUT_MS / 1000 + " s to answer."
        : error.message,
    );
  } finally {
    clearTimeout(timeout);
    $("refresh").disabled = false;
    timer = setTimeout(refresh, pollDelay(requestedRun));
  }
}
// Callers that just acted get a poll that starts after their action, even when one is in flight.
function refresh() {
  if (fetchPromise) return fetchPromise.then(() => refresh());
  fetchPromise = poll().finally(() => {
    fetchPromise = null;
  });
  return fetchPromise;
}
$("search").addEventListener("input", changeFilters);
$("area").addEventListener("change", changeFilters);
$("run").addEventListener("change", () => {
  clearSelection();
  resetActivity();
  resetHistory();
  resetWork();
  requestOffset = 0;
  requestAnchor = null;
  columnLimits = {};
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
  const views = ["board", "history", "overview", "requests", "graph"];
  const view =
    event.key === "Home"
      ? "board"
      : event.key === "End"
        ? "graph"
        : views[
            (views.indexOf(activeView) +
              (event.key === "ArrowRight" ? 1 : views.length - 1)) %
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
  if ($("task-dialog").open) return;
  selectedTask = null;
  taskKnowledgeKey = "";
  detailSignature = "";
  $("task-link-field").hidden = true;
  writeViewLocation();
  restoreOpenerFocus();
});
$("export").addEventListener("click", () => {
  if (!snapshot) return;
  if (activeView === "overview") return exportOverview();
  if (activeView === "graph") return exportGraph();
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
                  activeView === "history"
                    ? historyTasks()
                    : $("work-layout").value === "rows"
                      ? orderedWork(
                          filteredTasks().filter(
                            (task) => !FINISHED.has(task.status),
                          ),
                        )
                      : filteredTasks(),
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
window.addEventListener("online", () => refresh());
window.addEventListener("offline", () => {
  clearTimeout(timer);
  markPollFailure("This browser is offline.");
  timer = setTimeout(refresh, MAX_BACKOFF_MS);
});
setInterval(tickClock, 1000);
initTheme();
initControls();
initHealth();
initWorkflow();
initGraph();
refresh();
