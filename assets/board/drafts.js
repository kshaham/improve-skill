"use strict";
let draftWorkspace = null,
  draftContext = {},
  draftStorageError = "",
  draftReadError = false;

function draftKey(descriptor = requestDraft, context = draftContext) {
  return JSON.stringify([
    descriptor.run_id,
    descriptor.type,
    descriptor.task_key || descriptor.task_keys || "",
    descriptor.decision || "",
    context.followup || "",
  ]);
}

function savedDrafts() {
  if (!draftWorkspace) return {};
  draftReadError = false;
  try {
    const raw = localStorage.getItem(draftWorkspace);
    if (!raw) return {};
    if (raw.length > 2 * 1024 * 1024) throw new Error("Draft storage is full");
    const value = JSON.parse(raw);
    if (
      !value ||
      value.version !== 1 ||
      !value.items ||
      typeof value.items !== "object" ||
      Array.isArray(value.items)
    )
      throw new Error("Saved drafts could not be read");
    for (const [key, row] of Object.entries(value.items)) {
      if (
        !row?.descriptor ||
        !["task", "guidance", "priority", "decision", "note"].includes(
          row.descriptor.type,
        ) ||
        (row.descriptor.task_keys !== undefined &&
          (row.descriptor.type !== "priority" ||
            !Array.isArray(row.descriptor.task_keys) ||
            row.descriptor.task_keys.length < 1 ||
            row.descriptor.task_keys.length > 50 ||
            !row.descriptor.task_keys.every(
              (key) => typeof key === "string" && key.length <= 500,
            ) ||
            new Set(row.descriptor.task_keys).size !==
              row.descriptor.task_keys.length ||
            (row.retry &&
              (!Array.isArray(row.retry.ids) ||
                row.retry.ids.length !== row.descriptor.task_keys.length ||
                !row.retry.ids.every((id) => typeof id === "string"))))) ||
        !row.context ||
        typeof row.context !== "object" ||
        !row.fields ||
        !["title", "text", "area", "priority"].every(
          (field) => typeof row.fields[field] === "string",
        ) ||
        row.fields.title.length > 200 ||
        row.fields.text.length > 8000 ||
        key !== draftKey(row.descriptor, row.context)
      )
        throw new Error("Saved drafts could not be read");
    }
    return value.items;
  } catch (error) {
    draftReadError = true;
    draftStorageError = error.message;
    return {};
  }
}

function storeDrafts(items) {
  if (!draftWorkspace || draftReadError) return;
  try {
    if (Object.keys(items).length > 50)
      throw new Error(
        "Fifty drafts are already saved. Submit or discard an older draft first",
      );
    localStorage.setItem(draftWorkspace, JSON.stringify({ version: 1, items }));
    draftStorageError = "";
  } catch (error) {
    draftStorageError = error.message;
  }
}

function prepareDrafts() {
  const workspace = snapshot?.workspace_id;
  if (!workspace || !/^[a-f0-9]{64}$/.test(workspace)) return;
  const key = "improve:drafts:v1:" + workspace;
  if (draftWorkspace !== key) {
    draftWorkspace = key;
    draftStorageError = "";
  }
  const count = Object.keys(savedDrafts()).length;
  $("resume-draft").hidden = count === 0;
  $("resume-draft").textContent =
    `Resume draft${count > 1 ? " (" + count + ")" : ""}`;
  $("resume-draft").disabled = sending || !connected;
}

function draftStatus() {
  const stale = requestDraft?.run_id !== (snapshot?.current.control_id || null);
  $("draft-status").textContent = stale
    ? "This draft belongs to an earlier run. It cannot be sent to the current run; copy its text into a new request."
    : draftStorageError
      ? "Draft is only in this open form: " +
        draftStorageError +
        ". Copy your text before leaving."
      : "Drafts stay in this browser until sent or discarded. Closing keeps your draft.";
  $("request-submit").disabled =
    sending || !connected || !snapshot?.controls.requests_available || stale;
}

function saveDraft() {
  if (!requestDraft || !draftWorkspace) return;
  const fields = {
    title: $("request-task-title").value,
    text: $("request-text").value,
    area: $("request-area").value,
    priority: $("request-priority").value,
  };
  const items = savedDrafts();
  if (
    !fields.title &&
    !fields.text &&
    ["task", "guidance", "note"].includes(requestDraft.type)
  ) {
    delete items[draftKey()];
    storeDrafts(items);
    prepareDrafts();
    draftStatus();
    return;
  }
  items[draftKey()] = {
    descriptor: requestDraft,
    context: draftContext,
    fields,
    retry: requestRetry,
    at: Date.now(),
  };
  storeDrafts(items);
  prepareDrafts();
  draftStatus();
}

function forgetDraft() {
  if (!requestDraft) return;
  const items = savedDrafts();
  delete items[draftKey()];
  storeDrafts(items);
  requestDraft = null;
  requestRetry = null;
  prepareDrafts();
}

function restoreDraft(row) {
  requestDraft = row.descriptor;
  draftContext = row.context;
  requestRetry = row.retry;
  $("request-context").textContent = draftContext.text || "Saved request";
  if (requestDraft.task_keys)
    $("request-title").textContent =
      "Set priority for " + requestDraft.task_keys.length + " tasks";
  $("request-task-title").value = row.fields.title;
  $("request-text").value = row.fields.text;
  $("request-area").value = row.fields.area;
  $("request-priority").value = row.fields.priority;
  draftStatus();
}

function initDrafts() {
  $("request-form").addEventListener("input", saveDraft);
  $("request-form").addEventListener("change", saveDraft);
  $("request-dialog").addEventListener("close", () => {
    if (requestDraft && !sending) saveDraft();
  });
  $("discard-draft").addEventListener("click", () => {
    forgetDraft();
    $("request-dialog").close();
  });
  $("resume-draft").addEventListener("click", () => {
    const row = Object.values(savedDrafts()).sort((a, b) => b.at - a.at)[0];
    if (!row) return;
    openRequest(row.descriptor.type, null, row.descriptor.decision, row);
  });
  window.addEventListener("storage", (event) => {
    if (event.key === draftWorkspace) prepareDrafts();
  });
}
