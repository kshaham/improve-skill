"use strict";
let activityOffset = 0,
  activityAnchor = null,
  activitySignature = "";

function overviewData() {
  const areas = new Map(),
    events = [];
  let done = 0,
    recordedVerification = 0,
    highWaiting = 0,
    undated = 0;
  const addEvent = (event, at) => {
    const epoch = Date.parse(at);
    if (Number.isFinite(epoch)) events.push({ ...event, at, epoch });
  };
  for (const task of snapshot.tasks) {
    const area = areas.get(task.area) || {
      area: task.area,
      total: 0,
      active: 0,
      done: 0,
      attention: 0,
    };
    area.total++;
    if (!FINISHED.has(task.status)) area.active++;
    if (["blocked", "proposed"].includes(task.status)) area.attention++;
    if (task.status === "done") {
      area.done++;
      done++;
      const verification = task.verification || task.last_verification;
      if (
        typeof verification === "string"
          ? verification.trim()
          : verification && Object.keys(verification).length
      )
        recordedVerification++;
    }
    if (userPriority(task) === "high" && task.status === "ready") highWaiting++;
    areas.set(task.area, area);
    const recorded = recordedDate(task);
    if (recorded.epoch === null) undated++;
    else
      addEvent(
        {
          key: "task:" + task.key,
          kind: "task",
          target: task.key,
          title: task.title,
          run: task.run_id,
          action:
            recorded.field === "completed_at" && task.status === "done"
              ? "Task completed"
              : recorded.field === "created_at"
                ? "Task recorded"
                : "Task updated",
          detail: labels[task.status] + " · " + task.area,
        },
        task[recorded.field],
      );
  }
  for (const request of snapshot.requests || []) {
    const base = {
      kind: "request",
      target: request.key,
      title: request.title || request.task_title || request.text,
      run: request.source_run,
      detail: requestLabels[request.type],
    };
    addEvent(
      { ...base, key: "submitted:" + request.key, action: "Request submitted" },
      request.created_at,
    );
    if (request.status !== "pending")
      addEvent(
        {
          ...base,
          key: "response:" + request.key,
          action: "Request " + request.status,
        },
        request.responded_at,
      );
  }
  return {
    counts: {
      tasks: snapshot.tasks.length,
      done,
      recorded_verification: recordedVerification,
      high_priority_queued: highWaiting,
      pending_requests: (snapshot.requests || []).filter(
        (row) => row.status === "pending",
      ).length,
      undated_tasks: undated,
    },
    areas: [...areas.values()].sort(
      (a, b) => b.total - a.total || a.area.localeCompare(b.area),
    ),
    activity: events.sort(
      (a, b) => b.epoch - a.epoch || a.key.localeCompare(b.key),
    ),
  };
}

function filteredActivity(data) {
  return data.activity.filter(
    (event) =>
      $("activity-type").value === "all" ||
      event.kind === $("activity-type").value,
  );
}

function openActivity(event) {
  if (event.kind === "task") {
    const task = snapshot.tasks.find((task) => task.key === event.target);
    if (task) openTask(task);
    return;
  }
  $("search").value = "";
  $("request-status").value = "all";
  const index = filteredRequests().findIndex((row) => row.key === event.target);
  requestOffset =
    Math.floor(Math.max(0, index) / REQUEST_PAGE_SIZE) * REQUEST_PAGE_SIZE;
  requestAnchor = null;
  selectView("requests");
  const details = [...$("request-list").children].find(
    (item) => item.dataset.id === event.target,
  );
  if (details) {
    details.open = true;
    details.querySelector("summary").focus();
  }
}

function renderOverview() {
  const data = overviewData();
  $("visible-count").textContent =
    countText(data.counts.tasks) + " tasks in selected runs";
  $("overview-priority").textContent = countText(
    data.counts.high_priority_queued,
  );
  $("overview-pending").textContent = countText(data.counts.pending_requests);
  $("overview-verification").textContent =
    `${countText(data.counts.recorded_verification)} / ${countText(data.counts.done)}`;
  $("overview-note").textContent =
    "Recorded activity uses the latest saved task timestamps and request receipts; it is not a complete change log." +
    (data.counts.undated_tasks
      ? " " +
        countText(data.counts.undated_tasks) +
        (data.counts.undated_tasks === 1
          ? " task has no timestamp and is omitted from the activity feed."
          : " tasks have no timestamp and are omitted from the activity feed.")
      : "");
  $("area-summary").replaceChildren(
    ...data.areas.map((area) => {
      const row = element("tr");
      for (const value of [
        area.area,
        area.total,
        area.active,
        area.done,
        area.attention,
      ])
        row.append(
          element(
            "td",
            "",
            typeof value === "number" ? countText(value) : value,
          ),
        );
      return row;
    }),
  );
  $("overview-empty").hidden = data.areas.length > 0;
  const rows = filteredActivity(data);
  if (activityAnchor) {
    const index = rows.findIndex((row) => row.key === activityAnchor);
    if (index >= 0) activityOffset = index;
  }
  if (activityOffset >= rows.length)
    activityOffset = Math.max(
      0,
      Math.floor((rows.length - 1) / PAGE_SIZE) * PAGE_SIZE,
    );
  const page = rows.slice(activityOffset, activityOffset + PAGE_SIZE);
  activityAnchor = activityOffset > 0 ? page[0]?.key : null;
  $("activity-range").textContent = rows.length
    ? `${countText(activityOffset + 1)}–${countText(activityOffset + page.length)} of ${countText(rows.length)} recorded events`
    : "No recorded activity yet";
  $("activity-first").disabled = $("activity-prev").disabled =
    activityOffset === 0;
  $("activity-next").disabled = activityOffset + page.length >= rows.length;
  const signature = JSON.stringify(page);
  if (signature !== activitySignature) {
    activitySignature = signature;
    const focused = document.activeElement?.dataset.event;
    $("activity-list").replaceChildren(
      ...page.map((event) => {
        const row = element("button", "activity-row");
        row.type = "button";
        row.dataset.event = event.key;
        const text = element("span");
        text.append(
          element("strong", "", event.title),
          element(
            "span",
            "request-meta",
            event.action +
              " · " +
              event.detail +
              " · " +
              (event.run === "current" ? "Current run" : event.run),
          ),
        );
        row.append(
          text,
          element("time", "", new Date(event.epoch).toLocaleString()),
        );
        row.addEventListener("click", () => openActivity(event));
        return row;
      }),
    );
    [...$("activity-list").children]
      .find((row) => row.dataset.event === focused)
      ?.focus({ preventScroll: true });
  }
}

function exportOverview() {
  const data = overviewData();
  const url = URL.createObjectURL(
    new Blob(
      [
        JSON.stringify(
          {
            repo: snapshot.repo,
            selected_run: snapshot.selected_run,
            exported_at: new Date().toISOString(),
            counts: data.counts,
            areas: data.areas,
            activity: filteredActivity(data),
          },
          null,
          2,
        ),
      ],
      { type: "application/json" },
    ),
  );
  const link = element("a");
  link.href = url;
  link.download = "improve-overview.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function resetActivity() {
  activityOffset = 0;
  activityAnchor = null;
}

function initOverview() {
  $("overview-tab").addEventListener("click", () => selectView("overview"));
  $("activity-type").addEventListener("change", () => {
    resetActivity();
    renderOverview();
    writeViewLocation();
  });
  for (const [id, direction] of [
    ["activity-first", 0],
    ["activity-prev", -1],
    ["activity-next", 1],
  ]) {
    $(id).addEventListener("click", () => {
      activityOffset =
        direction === 0
          ? 0
          : Math.max(0, activityOffset + direction * PAGE_SIZE);
      activityAnchor = null;
      renderOverview();
    });
  }
}
