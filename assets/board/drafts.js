"use strict";
let draftWorkspace = null,
  draftContext = {},
  draftStorageError = "",
  draftReadError = false;

function draftKey(descriptor = requestDraft, context = draftContext) {
  return JSON.stringify([
    descriptor.run_id,
    descriptor.type,
    descriptor.task_key || descriptor.task_keys || descriptor.target_id || "",
    descriptor.decision || "",
    context.followup || "",
  ]);
}

// Every request type the form can carry. A type missing here used to make the whole store
// unreadable, after which no draft could be saved again (retry ids lost with them).
const DRAFT_TYPES = [
  "task",
  "guidance",
  "priority",
  "decision",
  "note",
  "retry",
  "withdraw",
];
const DRAFT_LIMIT = 50,
  DRAFT_STORE_VERSION = 1,
  DRAFT_STORE_BYTES = 2 * 1024 * 1024;

function validRetry(row) {
  const retry = row.retry;
  if (retry == null) return true;
  if (typeof retry !== "object" || typeof retry.id !== "string") return false;
  const keys = row.descriptor.task_keys;
  if (keys === undefined) return true;
  return (
    Array.isArray(retry.ids) &&
    retry.ids.length === keys.length &&
    retry.ids.every((id) => typeof id === "string")
  );
}

// One saved row; an invalid row is dropped on its own instead of failing every draft.
function validDraft(key, row) {
  const descriptor = row?.descriptor;
  if (!descriptor || typeof descriptor !== "object") return false;
  if (!DRAFT_TYPES.includes(descriptor.type)) return false;
  if (descriptor.run_id != null && typeof descriptor.run_id !== "string") return false;
  if (descriptor.task_keys !== undefined) {
    const keys = descriptor.task_keys;
    if (
      descriptor.type !== "priority" ||
      !Array.isArray(keys) ||
      keys.length < 1 ||
      keys.length > DRAFT_LIMIT ||
      !keys.every((item) => typeof item === "string" && item.length <= 500) ||
      new Set(keys).size !== keys.length
    )
      return false;
  }
  if (descriptor.type === "retry" && typeof descriptor.task_key !== "string") return false;
  if (descriptor.type === "withdraw" && typeof descriptor.target_id !== "string") return false;
  if (!row.context || typeof row.context !== "object") return false;
  const fields = row.fields;
  if (
    !fields ||
    !["title", "text", "area", "priority"].every((field) => typeof fields[field] === "string") ||
    fields.title.length > 200 ||
    fields.text.length > 8000
  )
    return false;
  return validRetry(row) && key === draftKey(descriptor, row.context);
}

function savedDrafts() {
  if (!draftWorkspace) return {};
  draftReadError = false;
  let raw = null;
  try {
    raw = localStorage.getItem(draftWorkspace);
  } catch (error) {
    draftReadError = true;
    draftStorageError = error.message;
    return {};
  }
  if (!raw) return {};
  let value = null;
  try {
    // Fifty drafts at their size limits stay far below this; a larger value is not ours.
    if (raw.length <= DRAFT_STORE_BYTES) value = JSON.parse(raw);
  } catch (error) {
    // Unreadable as a whole: nothing in it can be recovered, so it is replaced below.
    void error;
  }
  if (value && Number.isInteger(value.version) && value.version > DRAFT_STORE_VERSION) {
    // Written by a newer board; never overwrite it with this older format.
    draftReadError = true;
    draftStorageError = "Drafts were saved by a newer board version";
    return {};
  }
  const items = {};
  let dropped = 0;
  if (value && value.items && typeof value.items === "object" && !Array.isArray(value.items))
    for (const [key, row] of Object.entries(value.items)) {
      if (validDraft(key, row)) items[key] = row;
      else dropped += 1;
    }
  else dropped += 1;
  // Rewrite without the bad rows, so one bad row never blocks saving the others.
  if (dropped) storeDrafts(items);
  return items;
}

function storeDrafts(items) {
  if (!draftWorkspace || draftReadError) return;
  try {
    if (Object.keys(items).length > DRAFT_LIMIT)
      throw new Error(
        "Fifty drafts are already saved. Submit or discard an older draft first",
      );
    localStorage.setItem(
      draftWorkspace,
      JSON.stringify({ version: DRAFT_STORE_VERSION, items }),
    );
    draftStorageError = "";
  } catch (error) {
    draftStorageError = error.message;
  }
}

// Nothing typed: closing or cancelling the form leaves no draft behind, for every type.
// A priority request's content is its chosen level, so it is empty only while unchanged.
function draftIsEmpty(descriptor, context, fields) {
  if (fields.title.trim() || fields.text.trim()) return false;
  if (descriptor.type !== "priority") return true;
  return (
    typeof context?.initial_priority === "string" &&
    fields.priority === context.initial_priority
  );
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
  if (draftIsEmpty(requestDraft, draftContext, fields)) {
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
