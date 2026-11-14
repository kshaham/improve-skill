"use strict";
// Run health: live heartbeat and cycle ages, supervisor counters, the cycle timeline,
// opt-in browser notifications with an in-page event log, and the journal, daemon log,
// and quarantine views.
const DEFAULT_CYCLE_TIMEOUT = 3600;
const TIMELINE_BARS = 24;
const NOTIFY_KEY = "improve-board-notify";
const NOTICE_LIMIT = 20;
const NOTIFIED_LIMIT = 400;
const LOG_TAIL_LINES = 400;
const LOG_REFRESH_MS = 5000;
// Liveness has its own small endpoint: the snapshot ETag ignores heartbeats, so a paused run
// answers 304 instead of resending the whole board every few seconds.
const LIVENESS_POLL_MS = 5000,
  LIVENESS_HIDDEN_POLL_MS = 15000;
let liveness = null,
  livenessTimer = null;
let notifyEnabled = false,
  notifiedKeys = new Set(),
  noticeLog = [],
  timelineSignature = "",
  textDialogKind = null,
  textDialogTimer = null,
  textDialogLoad = 0;

function formatDuration(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  if (seconds < 60) return seconds + " s";
  const hours = Math.floor(seconds / 3600),
    minutes = Math.floor((seconds % 3600) / 60);
  if (!hours) return minutes + " min";
  return hours + " h " + String(minutes).padStart(2, "0") + " min";
}

// Ages are reported by the server; the browser adds the time since that report. The newest
// report wins: /api/health between snapshots, or a fresh snapshot.
function currentLiveness(run) {
  const sameRun =
    liveness && (liveness.control_id == null || run.control_id == null || liveness.control_id === run.control_id);
  if (sameRun && liveness.receivedAt > snapshotBaseAt)
    return {
      heartbeatAt: liveness.heartbeat_at,
      heartbeat: liveness.heartbeat_age_seconds,
      cycle: liveness.cycle_age_seconds,
      baseAt: liveness.receivedAt,
    };
  return {
    heartbeatAt: run.heartbeat_at,
    heartbeat: run.heartbeat_age_seconds,
    cycle: run.cycle_age_seconds,
    baseAt: snapshotBaseAt,
  };
}

function liveAge(baseSeconds, baseAt) {
  if (!Number.isFinite(baseSeconds)) return null;
  return baseSeconds + (Date.now() - baseAt) / 1000;
}

function renderCheckpoint() {
  if (!snapshot) return;
  const run = snapshot.current;
  const formatTime = (value) =>
    Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : "not recorded";
  const checkpoints = ["Work checkpoint: " + formatTime(run.last_activity_at)];
  if (run.supervised)
    checkpoints.push("Supervisor checkpoint: " + formatTime(currentLiveness(run).heartbeatAt));
  $("run-checkpoint").textContent = checkpoints.join(" · ");
}

async function pollLiveness() {
  clearTimeout(livenessTimer);
  try {
    const data = await boardJSON("/api/health");
    liveness = { ...data, receivedAt: Date.now() };
    renderHealthAges();
    renderCheckpoint();
  } catch (error) {
    // The snapshot poll owns connection state and its messages; keep the last liveness.
    void error;
  } finally {
    livenessTimer = setTimeout(pollLiveness, document.hidden ? LIVENESS_HIDDEN_POLL_MS : LIVENESS_POLL_MS);
  }
}

function cycleTimeout(run) {
  return Number.isFinite(run.cycle_timeout_seconds) && run.cycle_timeout_seconds > 0
    ? run.cycle_timeout_seconds
    : DEFAULT_CYCLE_TIMEOUT;
}

function ageLevel(age, amber, red) {
  return age > red ? "red" : age > amber ? "amber" : "ok";
}

function setAge(id, text, level) {
  const node = $(id);
  node.textContent = text;
  node.className = "health-age" + (level ? " level-" + level : "");
  node.hidden = !text;
}

function renderHealthAges() {
  if (!snapshot) return;
  const run = snapshot.current;
  const live = run.daemon_running && !run.outcome;
  const timeout = cycleTimeout(run);
  const ages = currentLiveness(run);
  const heartbeat = liveAge(ages.heartbeat, ages.baseAt);
  if (heartbeat === null) setAge("health-heartbeat", "", "");
  else
    setAge(
      "health-heartbeat",
      "Supervisor heartbeat " + formatDuration(heartbeat) + " ago",
      live ? ageLevel(heartbeat, timeout, 2 * timeout) : "",
    );
  const cycle = liveAge(ages.cycle, ages.baseAt);
  const cycleRunning = live && run.child_pid != null && cycle !== null;
  if (cycleRunning)
    setAge(
      "health-cycle",
      "Cycle running for " + formatDuration(cycle) + " of " + formatDuration(timeout) + " limit" +
        (Number.isFinite(run.cycle_timeout_seconds) ? "" : " (assumed)"),
      ageLevel(cycle, 0.75 * timeout, timeout),
    );
  else if (live && run.phase === "parked")
    setAge("health-cycle", "Parked" + (run.park_until ? " until " + healthTime(run.park_until) : ""), "amber");
  else if (live) setAge("health-cycle", "No cycle running", "");
  else setAge("health-cycle", "", "");
}

function healthTime(value) {
  return Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : "not recorded";
}

function renderHealthCounters(run) {
  const items = [
    ["Quarantines", run.quarantines],
    ["Recoveries", run.recoveries_total],
    ["Crashes", run.crashes],
    ["Limit waited", run.limit_waited_seconds ? formatDuration(run.limit_waited_seconds) : 0],
  ];
  if (run.parks) items.push(["Parks", run.parks]);
  if (run.idle_cycles) items.push(["Idle cycles", run.idle_cycles]);
  if (Number.isFinite(run.next_limit_wait) && run.next_limit_wait > 0)
    items.push(["Next limit wait", formatDuration(run.next_limit_wait)]);
  if (run.park_until) items.push(["Parked until", healthTime(run.park_until)]);
  if (run.child_pid != null) items.push(["Child PID", run.child_pid]);
  if (run.ended_at) items.push(["Ended", healthTime(run.ended_at)]);
  const signature = JSON.stringify(items);
  const list = $("health-counters");
  if (list.dataset.signature === signature) return;
  list.dataset.signature = signature;
  list.replaceChildren(
    ...items.map(([label, value]) => {
      const item = element("li");
      item.append(element("span", "", label), element("strong", "", String(value ?? 0)));
      if (["Quarantines", "Crashes"].includes(label) && value > 0) item.classList.add("attention");
      return item;
    }),
  );
}

function renderCycleTimeline(run) {
  const rows = Array.isArray(run.cycle_history) ? run.cycle_history.slice(-TIMELINE_BARS) : [];
  const signature = JSON.stringify(rows);
  if (signature === timelineSignature) return;
  timelineSignature = signature;
  const timeline = $("cycle-timeline");
  timeline.hidden = !rows.length;
  const longest = Math.max(1, ...rows.map((row) => row.seconds || 0));
  timeline.replaceChildren(
    ...rows.map((row) => {
      const failed = Number.isFinite(row.rc) && row.rc !== 0;
      const bar = element(
        "span",
        "cycle-bar " + (failed ? "failed" : row.limited ? "limited" : row.advanced ? "advanced" : "idle"),
      );
      bar.style.height = Math.max(18, Math.round(((row.seconds || 0) / longest) * 100)) + "%";
      bar.title = [
        row.started_at ? healthTime(row.started_at) : "Start not recorded",
        row.seconds != null ? formatDuration(row.seconds) : "",
        failed ? "exit " + row.rc : row.limited ? "account limit" : row.advanced ? "advanced" : "no progress",
        row.outcome || "",
      ]
        .filter(Boolean)
        .join(" · ");
      return bar;
    }),
  );
  timeline.setAttribute(
    "aria-label",
    "Last " + rows.length + " cycles: " +
      rows.filter((row) => row.advanced).length + " advanced, " +
      rows.filter((row) => Number.isFinite(row.rc) && row.rc !== 0).length + " failed",
  );
}

function healthAlert(run) {
  return (
    (typeof run.outcome === "string" && run.outcome.startsWith("halted")) ||
    run.phase === "parked" ||
    (run.phase === "crashed" && !run.daemon_running)
  );
}

// Notifications

function readNotifyPreference() {
  try {
    return localStorage.getItem(NOTIFY_KEY) === "1";
  } catch (error) {
    void error;
    return false;
  }
}

function saveNotifyPreference(enabled) {
  try {
    if (enabled) localStorage.setItem(NOTIFY_KEY, "1");
    else localStorage.removeItem(NOTIFY_KEY);
  } catch (error) {
    void error;
  }
}

function renderNotifyToggle() {
  const toggle = $("notify-toggle");
  const supported = "Notification" in window;
  toggle.disabled = !supported;
  toggle.setAttribute("aria-pressed", String(notifyEnabled && supported));
  toggle.textContent = !supported
    ? "Notifications unavailable"
    : notifyEnabled
      ? "Notifications on"
      : "Notify me";
  toggle.title = !supported
    ? "This browser has no notification support; the event log below still records changes."
    : notifyEnabled
      ? "Browser notifications for halts, limits, failures, blocked tasks, and request responses. Activate to turn off."
      : "Get a browser notification when the run halts, parks, hits a limit, fails repeatedly, blocks a task, or answers a request.";
}

async function toggleNotifications() {
  if (!("Notification" in window)) return;
  if (notifyEnabled) {
    notifyEnabled = false;
    saveNotifyPreference(false);
    renderNotifyToggle();
    return;
  }
  let permission = Notification.permission;
  if (permission === "default") {
    try {
      permission = await Notification.requestPermission();
    } catch (error) {
      void error;
      permission = "denied";
    }
  }
  if (permission !== "granted") {
    feedback("Notifications are blocked in this browser; the event log below still records changes.", true);
    return;
  }
  notifyEnabled = true;
  saveNotifyPreference(true);
  renderNotifyToggle();
}

function renderNoticeLog() {
  $("notice-count").textContent = countText(noticeLog.length);
  $("notice-log").hidden = !noticeLog.length;
  $("notice-list").replaceChildren(
    ...noticeLog.map((entry) => {
      const item = element("li");
      item.append(
        element("time", "", new Date(entry.at).toLocaleTimeString()),
        element("strong", "", entry.title),
        element("span", "", entry.body),
      );
      return item;
    }),
  );
}

// Each key notifies once per page load; the log keeps the newest entries.
function noteEvent(key, title, body) {
  if (notifiedKeys.has(key)) return;
  notifiedKeys.add(key);
  if (notifiedKeys.size > NOTIFIED_LIMIT) notifiedKeys.delete(notifiedKeys.values().next().value);
  noticeLog.unshift({ at: Date.now(), title, body });
  noticeLog.length = Math.min(noticeLog.length, NOTICE_LIMIT);
  renderNoticeLog();
  if (notifyEnabled && "Notification" in window && Notification.permission === "granted") {
    try {
      new Notification(title, { body, tag: key });
    } catch (error) {
      void error;
    }
  }
}

function detectTransitions(previous, data) {
  const before = previous.current,
    after = data.current,
    run = after.control_id || "run";
  if (!before.outcome && after.outcome)
    noteEvent("outcome:" + run + ":" + after.outcome, "Run " + after.outcome, data.repo + " · the improvement run ended.");
  if (before.phase !== after.phase && ["account-limit", "parked"].includes(after.phase))
    noteEvent(
      "phase:" + run + ":" + after.phase + ":" + (after.retry_at || after.park_until || ""),
      after.phase === "parked" ? "Run parked" : "Waiting for account limit",
      after.phase === "parked"
        ? "Repeated failures; the daemon retries " + (after.park_until ? "at " + healthTime(after.park_until) : "later") + "."
        : "The assistant hit its usage limit; the run resumes " + (after.retry_at ? "at " + healthTime(after.retry_at) : "later") + ".",
    );
  if ((before.consecutive_failures || 0) < 3 && (after.consecutive_failures || 0) >= 3)
    noteEvent(
      "failures:" + run + ":" + after.cycle,
      "Failure streak",
      after.consecutive_failures + " consecutive cycles failed or made no progress.",
    );
  const previousTasks = new Map(previous.tasks.map((task) => [task.key, task]));
  for (const task of data.tasks) {
    const earlier = previousTasks.get(task.key);
    if (earlier && earlier.status !== "blocked" && task.status === "blocked")
      noteEvent("blocked:" + task.key + ":" + (task.updated_at || ""), "Task blocked", task.id + " · " + task.title);
  }
  const previousRequests = new Map(previous.requests.map((row) => [row.key, row]));
  for (const row of data.requests) {
    const earlier = previousRequests.get(row.key);
    if (earlier && earlier.status === "pending" && row.status !== "pending")
      noteEvent(
        "request:" + row.key + ":" + row.status,
        "Request " + row.status,
        (row.title || row.task_title || row.text || row.type).slice(0, 120),
      );
  }
}

function healthDeadlineReached(run) {
  noteEvent("deadline:" + (run.control_id || "run"), "Deadline reached", "The run's wall-clock deadline has passed.");
}

// Journal, daemon log, and quarantine views share one text dialog.

function journalRun() {
  const selected = $("run").value;
  return selected && selected !== "all" ? selected : "current";
}

async function loadTextDialog() {
  const load = ++textDialogLoad;
  const kind = textDialogKind;
  const body = $("text-dialog-body"),
    list = $("text-dialog-list"),
    note = $("text-dialog-note");
  try {
    if (kind === "journal") {
      const run = journalRun();
      const data = await boardJSON("/api/journal?run=" + encodeURIComponent(run));
      if (load !== textDialogLoad) return;
      body.textContent = data.text;
      note.textContent =
        (run === "current" ? "Current run" : run) +
        (data.truncated ? " · showing the last 1 MiB of the journal" : " · hourly journal");
    } else if (kind === "log") {
      const data = await boardJSON("/api/log?lines=" + LOG_TAIL_LINES);
      if (load !== textDialogLoad) return;
      const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
      body.textContent = data.text;
      if (atBottom) body.scrollTop = body.scrollHeight;
      note.textContent =
        "Last " + countText(data.line_count) + " lines of the current run's daemon log · refreshes every 5 s while open";
    } else {
      const data = await boardJSON("/api/quarantine");
      if (load !== textDialogLoad) return;
      note.textContent =
        (data.refs.length
          ? "Work the supervisor set aside when the tree was dirty. Run the command in the repository to restore a snapshot."
          : "No quarantined work. The supervisor saves a ref here when it has to clear a dirty tree.") +
        (data.hidden > 0
          ? " " + countText(data.hidden) + " other ref(s) under refs/improve/quarantine are not named like the supervisor's and are not shown; inspect them with git for-each-ref."
          : "");
      list.replaceChildren(
        ...data.refs.map((ref) => {
          const item = element("div", "quarantine-item");
          const heading = element("div", "quarantine-heading");
          heading.append(
            element("strong", "", ref.ref.replace("refs/improve/quarantine/", "")),
            element("span", "request-meta", [healthTime(ref.date), ref.commit.slice(0, 10)].join(" · ")),
          );
          item.append(heading);
          if (ref.subject) item.append(element("p", "request-message", ref.subject));
          item.append(element("code", "quarantine-command", ref.recover));
          return item;
        }),
      );
    }
  } catch (error) {
    if (load !== textDialogLoad) return;
    body.textContent = "";
    list.replaceChildren();
    note.textContent = error.message;
  }
}

function openTextDialog(kind) {
  rememberOpener(document.activeElement);
  textDialogKind = kind;
  clearInterval(textDialogTimer);
  textDialogTimer = null;
  $("text-dialog-title").textContent =
    kind === "journal" ? "Run journal" : kind === "log" ? "Daemon log" : "Quarantined work";
  $("text-dialog-note").textContent = "Loading…";
  $("text-dialog-body").textContent = "";
  $("text-dialog-body").hidden = kind === "quarantine";
  $("text-dialog-list").hidden = kind !== "quarantine";
  $("text-dialog-list").replaceChildren();
  $("text-dialog").showModal();
  loadTextDialog();
  if (kind === "log") textDialogTimer = setInterval(loadTextDialog, LOG_REFRESH_MS);
}

function renderHealthExtras(run) {
  renderHealthAges();
  renderCheckpoint();
  renderHealthCounters(run);
  renderCycleTimeline(run);
}

function initHealth() {
  notifyEnabled = readNotifyPreference();
  if (notifyEnabled && (!("Notification" in window) || Notification.permission !== "granted"))
    notifyEnabled = false;
  renderNotifyToggle();
  renderNoticeLog();
  $("notify-toggle").addEventListener("click", toggleNotifications);
  $("open-journal").addEventListener("click", () => openTextDialog("journal"));
  $("open-log").addEventListener("click", () => openTextDialog("log"));
  $("open-quarantine").addEventListener("click", () => openTextDialog("quarantine"));
  $("text-dialog-reload").addEventListener("click", loadTextDialog);
  pollLiveness();
  $("text-dialog").addEventListener("close", () => {
    clearInterval(textDialogTimer);
    textDialogTimer = null;
    textDialogLoad++;
    restoreOpenerFocus();
  });
}
