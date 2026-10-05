"use strict";
let connected = false,
  sending = false,
  controlsToken = null,
  requestDraft = null,
  requestRetry = null,
  requestOffset = 0,
  requestAnchor = null,
  requestSignature = "",
  stopRunId = null,
  reportLoad = 0,
  loadedReportRun = null;
const requestLabels = {
  task: "New task",
  guidance: "Guidance",
  priority: "Priority change",
  decision: "Proposal decision",
  note: "Task note",
};
const REQUEST_PAGE_SIZE = 20;

async function boardJSON(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(path, {
      ...options,
      cache: "no-store",
      signal: controller.signal,
    });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 403) controlsToken = null;
      const error = new Error(
        data.error || "The board could not save this action.",
      );
      error.status = response.status;
      throw error;
    }
    return data;
  } finally {
    clearTimeout(timeout);
  }
}

async function boardAction(path, data) {
  if (!connected)
    throw new Error(
      "Reconnect the board before sending changes. Your draft is still here.",
    );
  for (let attempt = 0; attempt < 2; attempt++) {
    if (!controlsToken) {
      const session = await boardJSON("/api/session", {
        headers: { "X-Improve-Client": "board" },
      });
      controlsToken = session.token;
    }
    try {
      return await boardJSON(path, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Improve-Board-Token": controlsToken,
        },
        body: JSON.stringify(data),
      });
    } catch (error) {
      // A restarted service rejects the old capability before applying anything.
      // Refresh it once; never replay timeouts, lost responses, or validation failures.
      if (error.status !== 403 || attempt === 1) throw error;
    }
  }
}

function feedback(message, error = false) {
  $("control-feedback").hidden = false;
  $("control-feedback").textContent = message;
  $("control-feedback").classList.toggle("form-error", error);
}

function renderControls() {
  const controls = snapshot?.controls || {},
    run = snapshot?.current || {};
  const allowed =
    connected && !sending && controls.available && !controls.stop_requested;
  const expired = !run.clock || run.clock.remaining_seconds <= 0;
  $("pause-run").disabled = !allowed || expired;
  $("pause-run").textContent = controls.pause_requested
    ? "Resume run"
    : "Pause after cycle";
  $("stop-run").disabled = !allowed;
  $("confirm-stop").disabled = !allowed;
  for (const id of [
    "send-guidance",
    "new-task",
    "request-submit",
    "change-priority",
    "approve-proposal",
    "reject-proposal",
    "follow-up-task",
    "add-task-note",
  ])
    $(id).disabled = !connected || sending || !controls.requests_available;
  $("open-report").disabled =
    !connected || !snapshot?.runs.some((run) => run.report_available);
  $("control-state").textContent = !connected
    ? "Disconnected"
    : run.outcome
      ? "Run ended"
      : controls.stop_requested
        ? "Stopping"
        : run.phase === "paused" && run.daemon_running
          ? "Paused"
          : controls.pause_requested && run.daemon_running
            ? "Pause requested"
            : run.daemon_running
              ? "Current run"
              : "Requests available";
  $("control-note").textContent = !connected
    ? "Reconnecting… Your saved requests and task history are preserved."
    : controls.reason ||
      "Restart the board service to load its updated controls.";
  const pending = (snapshot?.requests || []).filter(
    (row) => row.status === "pending",
  ).length;
  $("requests-count").textContent = countText(pending);
  $("requests-tab").title =
    countText(pending) + " pending requests in selected runs";
  prepareDrafts();
  if ($("request-dialog").open) draftStatus();
  $("discard-draft").disabled = sending;
  renderSelection();
}

function filteredRequests() {
  const query = $("search").value.trim().toLowerCase();
  return [...(snapshot?.requests || [])]
    .reverse()
    .sort(
      (a, b) =>
        (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0),
    )
    .filter(
      (row) =>
        ($("request-status").value === "all" ||
          row.status === $("request-status").value) &&
        (!query || readable(row).toLowerCase().includes(query)),
    );
}

function renderRequests() {
  const rows = filteredRequests();
  if (requestAnchor) {
    const anchored = rows.findIndex((row) => row.key === requestAnchor);
    if (anchored >= 0) requestOffset = anchored;
  }
  if (requestOffset >= rows.length)
    requestOffset = Math.max(
      0,
      Math.floor((rows.length - 1) / REQUEST_PAGE_SIZE) * REQUEST_PAGE_SIZE,
    );
  const page = rows.slice(requestOffset, requestOffset + REQUEST_PAGE_SIZE);
  requestAnchor = requestOffset > 0 ? page[0]?.key : null;
  $("visible-count").textContent =
    countText(rows.length) + " matching requests";
  $("request-range").textContent = rows.length
    ? `${countText(requestOffset + 1)}–${countText(requestOffset + page.length)} of ${countText(rows.length)} requests`
    : "0 requests";
  $("request-prev").disabled = requestOffset === 0;
  $("request-next").disabled = requestOffset + page.length >= rows.length;
  $("requests-empty").hidden = !!rows.length;
  $("requests-empty").textContent =
    $("request-status").value === "all" && !$("search").value.trim()
      ? "No requests in these runs. New tasks and guidance are saved to the current run."
      : "No requests match these filters.";
  const taskMap = new Map(snapshot.tasks.map((task) => [task.key, task]));
  const linkedTasks = (row) =>
    (row.task_keys || []).map((key) => taskMap.get(key)).filter(Boolean);
  const signature = JSON.stringify(page.map((row) => [row, linkedTasks(row)]));
  if (signature === requestSignature) return;
  requestSignature = signature;
  const opened = new Set(
    [...$("request-list").querySelectorAll("details[open]")].map(
      (item) => item.dataset.id,
    ),
  );
  const focused = $("request-list").contains(document.activeElement)
    ? document.activeElement.closest("details")?.dataset.id
    : null;
  $("request-list").replaceChildren(
    ...page.map((row) => {
      const item = element("details", "request-item");
      item.dataset.id = row.key;
      item.open = opened.has(row.key);
      const summary = element("summary");
      const title = element("span", "request-heading");
      title.append(
        element(
          "span",
          "request-kind",
          requestLabels[row.type] +
            " · " +
            (row.source_run === "current" ? "Current run" : row.source_run),
        ),
        element("strong", "", row.title || row.task_title || row.text),
      );
      summary.append(
        title,
        element(
          "span",
          "request-badge " + row.status,
          { pending: "Pending", applied: "Applied", declined: "Declined" }[
            row.status
          ],
        ),
      );
      const body = element("div", "request-body");
      const details = [
        row.created_at ? new Date(row.created_at).toLocaleString() : "",
        row.area,
        row.priority ? row.priority + " priority" : "",
        row.decision,
      ].filter(Boolean);
      body.append(element("p", "request-meta", details.join(" · ")));
      if (row.text) body.append(element("p", "request-message", row.text));
      body.append(
        element("strong", "", "Skill response"),
        element(
          "p",
          "request-message",
          row.response ||
            (row.source_run === "current"
              ? "Waiting for the next skill checkpoint."
              : "Archived without a recorded response. This request will not be applied to the current run."),
        ),
      );
      const linked = linkedTasks(row);
      if (linked.length) {
        body.append(element("strong", "", "Related work"));
        const links = element("div", "request-task-links");
        for (const task of linked) {
          const button = element(
            "button",
            "request-task-link",
            labels[task.status] + " · " + task.title,
          );
          button.type = "button";
          button.dataset.taskKey = task.key;
          button.addEventListener("click", () => openTask(task));
          links.append(button);
        }
        body.append(links);
      }
      item.append(summary, body);
      return item;
    }),
  );
  if (focused)
    [...$("request-list").children]
      .find((item) => item.dataset.id === focused)
      ?.querySelector("summary")
      .focus({ preventScroll: true });
}

function openRequest(kind, task = null, decision = null, restored = null) {
  requestDraft = {
    type: kind,
    run_id: snapshot.current.control_id || null,
    task_key: kind === "task" ? undefined : task?.key,
    decision,
  };
  requestRetry = null;
  $("request-form").reset();
  $("request-error").hidden = true;
  $("request-title").textContent =
    kind === "decision"
      ? decision === "approve"
        ? "Approve proposal"
        : "Decline proposal"
      : requestLabels[kind];
  $("request-context").textContent = task
    ? task.title
    : kind === "guidance"
      ? "Tell the skill what to focus on, change, or avoid. It will review your guidance before choosing its next task."
      : "Describe a result you want. The skill will turn this request into a task with acceptance checks.";
  draftContext = {
    text: $("request-context").textContent,
    followup: kind === "task" ? task?.key : null,
  };
  $("request-title-field").hidden = kind !== "task";
  $("request-task-title").required = kind === "task";
  $("request-area-field").hidden = kind !== "task";
  $("request-priority-field").hidden = !["task", "priority"].includes(kind);
  $("request-text").required = ["guidance", "note"].includes(kind);
  $("request-priority").value = task?.user_priority || "normal";
  if (kind === "task" && task) {
    $("request-title").textContent = "Create follow-up task";
    $("request-task-title").value = ("Follow up: " + task.title).slice(0, 200);
    $("request-area").value = [...$("request-area").options].some(
      (option) => option.value === task.area,
    )
      ? task.area
      : "general";
    $("request-text").value =
      `Follow-up to ${(task.key + ": " + task.title).slice(0, 2000)}\nRecorded status: ${labels[task.status]}${task.commit ? "\nCommit: " + readable(task.commit).slice(0, 200) : ""}\n\nDesired result:\n`;
    draftContext.text =
      "Create a separate task in the current run. The original task and its evidence stay unchanged.";
    $("request-context").textContent = draftContext.text;
  }
  const saved = restored || savedDrafts()[draftKey()];
  if (saved) restoreDraft(saved);
  if (task) {
    $("task-dialog").close();
    selectedTask = null;
    writeViewLocation();
  }
  $("request-dialog").showModal();
  draftStatus();
  (kind === "task"
    ? $("request-task-title")
    : kind === "priority"
      ? $("request-priority")
      : $("request-text")
  ).focus();
}

async function submitRequest(event) {
  event.preventDefault();
  if (sending || !requestDraft) return;
  const payload = { ...requestDraft, text: $("request-text").value };
  if (payload.type === "task")
    Object.assign(payload, {
      title: $("request-task-title").value,
      area: $("request-area").value,
    });
  if (["task", "priority"].includes(payload.type))
    payload.priority = $("request-priority").value;
  const signature = JSON.stringify(payload);
  if (requestRetry?.signature !== signature)
    requestRetry = {
      signature,
      id: crypto.randomUUID(),
      ids: payload.task_keys?.map(() => crypto.randomUUID()),
    };
  payload.id = requestRetry.id;
  saveDraft();
  sending = true;
  renderControls();
  $("request-error").hidden = true;
  try {
    const batch = Array.isArray(payload.task_keys);
    const result = await boardAction(
      batch ? "/api/requests/batch" : "/api/requests",
      batch
        ? {
            run_id: payload.run_id,
            priority: payload.priority,
            text: payload.text,
            items: payload.task_keys.map((task_key, index) => ({
              task_key,
              id: requestRetry.ids[index],
            })),
          }
        : payload,
    );
    if (batch) selectedWork.clear();
    forgetDraft();
    $("request-dialog").close();
    requestOffset = 0;
    requestAnchor = null;
    $("request-status").value = "all";
    $("search").value = "";
    $("run").value = "current";
    selectView("requests");
    $("requests-tab").focus();
    feedback(result.message);
    await refresh();
  } catch (error) {
    $("request-error").textContent = error.message;
    $("request-error").hidden = false;
  } finally {
    sending = false;
    renderControls();
  }
}

async function controlRun(action, runId) {
  if (sending) return;
  sending = true;
  renderControls();
  try {
    const result = await boardAction("/api/control", { action, run_id: runId });
    if (action === "stop") $("stop-dialog").close();
    feedback(result.message);
    await refresh();
  } catch (error) {
    if (action === "stop") {
      $("stop-error").textContent = error.message;
      $("stop-error").hidden = false;
    } else feedback(error.message, true);
  } finally {
    sending = false;
    renderControls();
  }
}

function taskControls(task) {
  $("add-task-note").hidden = task.run_id !== "current";
  $("add-task-note").onclick = () => openRequest("note", task);
  $("follow-up-task").onclick = () => openRequest("task", task);
  const editable = task.run_id === "current" && !FINISHED.has(task.status);
  $("task-controls").hidden = !editable;
  $("approve-proposal").hidden = $("reject-proposal").hidden =
    task.status !== "proposed";
  $("change-priority").onclick = () => openRequest("priority", task);
  $("approve-proposal").onclick = () =>
    openRequest("decision", task, "approve");
  $("reject-proposal").onclick = () => openRequest("decision", task, "reject");
}

async function loadReport() {
  const load = ++reportLoad;
  const runId = $("report-run").value;
  const run = snapshot.runs.find((item) => item.id === runId);
  loadedReportRun = null;
  $("report-text").textContent = "Loading report…";
  $("download-report").disabled = true;
  $("report-note").textContent =
    (run?.label || runId) +
    " · " +
    (run?.summary_pending
      ? "A report retry is pending. This is the last saved report."
      : "Saved from this run’s recorded work.");
  try {
    const report = await boardJSON(
      "/api/report?run=" + encodeURIComponent(runId),
    );
    if (load !== reportLoad) return;
    $("report-text").textContent = report.text;
    loadedReportRun = report.run_id;
    $("download-report").disabled = false;
  } catch (error) {
    if (load === reportLoad) $("report-text").textContent = error.message;
  }
}

function initControls() {
  for (const area of [
    "general",
    "features",
    "ui",
    "assets",
    "performance",
    "quality",
    "security",
    "coverage",
    "concurrency",
    "resilience",
    "gate-speed",
    "docs",
    "accessibility",
    "contracts",
  ]) {
    const option = element("option", "", area);
    option.value = area;
    $("request-area").append(option);
  }
  $("new-task").addEventListener("click", () => openRequest("task"));
  $("send-guidance").addEventListener("click", () => openRequest("guidance"));
  $("request-form").addEventListener("submit", submitRequest);
  $("requests-tab").addEventListener("click", () => selectView("requests"));
  $("request-status").addEventListener("change", () => {
    requestOffset = 0;
    requestAnchor = null;
    renderRequests();
    writeViewLocation();
  });
  for (const [id, direction] of [
    ["request-prev", -1],
    ["request-next", 1],
  ])
    $(id).addEventListener("click", () => {
      requestOffset = Math.max(
        0,
        requestOffset + direction * REQUEST_PAGE_SIZE,
      );
      requestAnchor = null;
      renderRequests();
    });
  $("pause-run").addEventListener("click", () =>
    controlRun(
      snapshot.controls.pause_requested ? "resume" : "pause",
      snapshot.current.control_id,
    ),
  );
  $("stop-run").addEventListener("click", () => {
    stopRunId = snapshot.current.control_id;
    $("stop-error").hidden = true;
    $("stop-dialog").showModal();
  });
  $("confirm-stop").addEventListener("click", () =>
    controlRun("stop", stopRunId),
  );
  document
    .querySelectorAll("[data-close]")
    .forEach((button) =>
      button.addEventListener("click", () => $(button.dataset.close).close()),
    );
  $("open-report").addEventListener("click", () => {
    const reports = snapshot.runs.filter((run) => run.report_available);
    options(
      $("report-run"),
      reports.map((run) => [run.id, run.label]),
    );
    $("report-run").value =
      reports.find((run) => run.id === $("run").value)?.id ||
      reports[0]?.id ||
      "";
    $("report-dialog").showModal();
    loadReport();
  });
  $("report-run").addEventListener("change", loadReport);
  $("report-dialog").addEventListener("close", () => {
    reportLoad++;
  });
  $("download-report").addEventListener("click", () => {
    if (!loadedReportRun) return;
    const url = URL.createObjectURL(
      new Blob([$("report-text").textContent], {
        type: "text/markdown;charset=utf-8",
      }),
    );
    const link = element("a");
    link.href = url;
    link.download = "improve-" + loadedReportRun + "-report.md";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  initDrafts();
  initTriage();
}
