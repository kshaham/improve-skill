// Self-checking harness for assets/board/drafts.js, run by tests/test_board_js.py.
// It loads the real drafts.js into a sandbox with a stub DOM and localStorage, then replays
// the review's failure (a cancelled Retry/Withdraw form made every saved draft unreadable)
// and the rules that replace it. Usage: node drafts_check.mjs <path to drafts.js>
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(process.argv[2], "utf8");
const WORKSPACE = "a".repeat(64);
const STORE_KEY = "improve:drafts:v1:" + WORKSPACE;

function sandbox() {
  const store = new Map();
  const elements = new Map();
  const context = vm.createContext({
    console,
    localStorage: {
      getItem: (key) => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => store.set(key, String(value)),
      removeItem: (key) => store.delete(key),
    },
    window: { addEventListener() {} },
  });
  context.$ = (id) => {
    if (!elements.has(id))
      elements.set(id, { value: "", hidden: false, disabled: false, textContent: "" });
    return elements.get(id);
  };
  // Globals that controls.js and board.js own in the page.
  vm.runInContext(
    "var requestDraft = null, requestRetry = null, sending = false, connected = true;" +
      `var snapshot = { workspace_id: "${WORKSPACE}", current: { control_id: "run-1" },` +
      " controls: { requests_available: true } };",
    context,
  );
  vm.runInContext(source, context, { filename: "drafts.js" });
  const run = (code) => vm.runInContext(code, context);
  run("prepareDrafts()");
  return {
    store,
    run,
    open(descriptor, extra = {}) {
      run(
        `requestDraft = ${JSON.stringify(descriptor)};` +
          `draftContext = ${JSON.stringify({ text: "context", followup: null, ...extra })};`,
      );
    },
    type(fields) {
      for (const [id, value] of Object.entries({
        "request-task-title": "",
        "request-text": "",
        "request-area": "general",
        "request-priority": "normal",
        ...fields,
      }))
        context.$(id).value = value;
      run("saveDraft()");
    },
    drafts: () => run("savedDrafts()"),
    readError: () => run("draftReadError"),
    message: () => run("draftStorageError"),
    rawItems: () => JSON.parse(store.get(STORE_KEY)).items,
  };
}

const checks = [];
function check(name, body) {
  body();
  checks.push(name);
}

check("cancelled retry and withdraw forms keep earlier drafts readable", () => {
  const page = sandbox();
  page.open({ type: "guidance", run_id: "run-1" });
  page.type({ "request-text": "keep this guidance" });
  page.open({ type: "retry", run_id: "run-1", task_key: "current:T1" });
  page.type({});
  page.open({ type: "withdraw", run_id: "run-1", target_id: "req-1" });
  page.type({});
  assert.equal(page.readError(), false);
  assert.equal(Object.keys(page.drafts()).length, 1, "only the guidance draft remains");
  page.open({ type: "guidance", run_id: "run-1" }, { followup: "x" });
  page.type({ "request-text": "a second draft" });
  assert.equal(Object.keys(page.drafts()).length, 2, "new drafts are still saved");
});

check("typed retry and withdraw drafts are saved and read back", () => {
  const page = sandbox();
  page.open({ type: "retry", run_id: "run-1", task_key: "current:T1" });
  page.run('requestRetry = { signature: "s", id: "retry-id-1" };');
  page.type({ "request-text": "runner is back" });
  page.open({ type: "withdraw", run_id: "run-1", target_id: "req-1" });
  page.type({ "request-text": "no longer needed" });
  const rows = Object.values(page.drafts());
  assert.deepEqual(rows.map((row) => row.descriptor.type).sort(), ["retry", "withdraw"]);
  assert.equal(rows.find((row) => row.descriptor.type === "retry").retry.id, "retry-id-1");
});

check("empty means delete for every type; an unchanged priority is empty", () => {
  const page = sandbox();
  for (const descriptor of [
    { type: "task", run_id: "run-1" },
    { type: "guidance", run_id: "run-1" },
    { type: "note", run_id: "run-1", task_key: "current:T1" },
    { type: "decision", run_id: "run-1", task_key: "current:T2", decision: "approve" },
    { type: "retry", run_id: "run-1", task_key: "current:T3" },
    { type: "withdraw", run_id: "run-1", target_id: "req-9" },
  ]) {
    page.open(descriptor);
    page.type({ "request-text": "draft text" });
    assert.equal(Object.keys(page.drafts()).length, 1, descriptor.type + " is saved");
    page.type({ "request-text": "   " });
    assert.equal(Object.keys(page.drafts()).length, 0, descriptor.type + " is deleted when cleared");
  }
  page.open({ type: "priority", run_id: "run-1", task_key: "current:T4" }, { initial_priority: "normal" });
  page.type({ "request-priority": "normal" });
  assert.equal(Object.keys(page.drafts()).length, 0, "an untouched priority form saves nothing");
  page.type({ "request-priority": "urgent" });
  assert.equal(Object.keys(page.drafts()).length, 1, "a chosen priority is content");
});

check("invalid rows are dropped one at a time and the store stays writable", () => {
  const page = sandbox();
  page.open({ type: "guidance", run_id: "run-1" });
  page.type({ "request-text": "good draft" });
  const items = page.rawItems();
  items["not-a-key"] = { descriptor: { type: "guidance", run_id: "run-1" }, context: {}, fields: {} };
  items[JSON.stringify(["run-1", "mystery", "", "", ""])] = {
    descriptor: { type: "mystery", run_id: "run-1" },
    context: {},
    fields: { title: "", text: "x", area: "", priority: "" },
  };
  items[JSON.stringify(["run-1", "retry", "", "", ""])] = {
    descriptor: { type: "retry", run_id: "run-1" },
    context: {},
    fields: { title: "", text: "x", area: "", priority: "" },
  };
  page.store.set(STORE_KEY, JSON.stringify({ version: 1, items }));
  const readable = page.drafts();
  assert.equal(page.readError(), false);
  assert.deepEqual(Object.values(readable).map((row) => row.fields.text), ["good draft"]);
  assert.equal(Object.keys(page.rawItems()).length, 1, "bad rows are removed from storage");
  page.open({ type: "note", run_id: "run-1", task_key: "current:T1" });
  page.type({ "request-text": "still saving" });
  assert.equal(Object.keys(page.drafts()).length, 2);
});

check("a corrupt store is replaced; a newer version is never overwritten", () => {
  const page = sandbox();
  page.store.set(STORE_KEY, "{not json");
  page.open({ type: "guidance", run_id: "run-1" });
  page.type({ "request-text": "after corruption" });
  assert.equal(Object.keys(page.drafts()).length, 1);
  const newer = JSON.stringify({ version: 2, items: { anything: true } });
  page.store.set(STORE_KEY, newer);
  page.type({ "request-text": "must not clobber" });
  assert.equal(page.readError(), true);
  assert.match(page.message(), /newer board/);
  assert.equal(page.store.get(STORE_KEY), newer);
});

console.log(JSON.stringify({ ok: true, checks }));
