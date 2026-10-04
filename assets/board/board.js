"use strict";
const $ = (id) => document.getElementById(id);
let snapshot = null,
  cardSignature = "",
  selectedTask = null,
  timer = null,
  fetching = false;
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
  if (task.focus_priority != null || task.priority != null)
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
function renderCards() {
  if (!snapshot) return;
  const tasks = filteredTasks();
  $("visible-count").textContent =
    tasks.length + (tasks.length === 1 ? " task" : " tasks");
  const signature = JSON.stringify([tasks, snapshot.columns]);
  if (signature === cardSignature) return;
  cardSignature = signature;
  const focus = document.activeElement?.dataset.key;
  const scrollLeft = $("board").scrollLeft;
  $("board").replaceChildren(
    ...snapshot.columns.map((column) => {
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
  $("empty").hidden = snapshot.tasks.length !== 0;
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
  $("total").textContent = data.tasks.length;
  $("working").textContent = data.counts.in_progress;
  $("completed").textContent = data.counts.done;
  $("blocked").textContent = data.counts.blocked + data.counts.proposed;
  $("run-count").textContent =
    data.selected_run === "all"
      ? "Across " +
        data.runs.length +
        (data.runs.length === 1 ? " run" : " runs")
      : "In selected run";
  const run = data.current;
  $("run-state").textContent =
    run.outcome ||
    (run.daemon_running
      ? run.phase === "account-limit"
        ? "Waiting for account limit"
        : "Running"
      : run.started_at
        ? "Last run checkpoint"
        : "Tracking tasks");
  $("run-detail").textContent =
    run.next_action ||
    (run.outcome
      ? "The run has ended. Its task history stays available."
      : "Tasks update from the project ledger.");
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
    if ($("run").value === requestedRun) render(data);
    $("connection").textContent = "Board connected";
    $("connection").classList.remove("offline");
  } catch (error) {
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
$("search").addEventListener("input", renderCards);
$("area").addEventListener("change", renderCards);
$("run").addEventListener("change", refresh);
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
          tasks: filteredTasks(),
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
  link.download = "improve-tasks.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
setInterval(remaining, 1000);
refresh();
