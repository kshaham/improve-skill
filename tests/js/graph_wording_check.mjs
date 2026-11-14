// Self-checking harness for the Knowledge view's wording in assets/board/graph.js, run by
// tests/test_board_js.py. After the Show filter landed, two messages could contradict the view:
// a removed node always said "Showing the strongest nodes", and the strongest-knowledge panel
// suggested choosing Balanced mix even when it was already chosen.
// Usage: node graph_wording_check.mjs <graph-layout.js> <graph.js>
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const context = vm.createContext({ console, Math, document: {}, window: {}, matchMedia: () => ({ matches: false }) });
vm.runInContext(readFileSync(process.argv[2], "utf8"), context);
vm.runInContext(readFileSync(process.argv[3], "utf8") + "\n;this.graphOrderPhrase = graphOrderPhrase; this.knowledgeEmptyHint = knowledgeEmptyHint; this.graphFallbackStatus = graphFallbackStatus;", context);
const { graphOrderPhrase, knowledgeEmptyHint, graphFallbackStatus } = context;
assert.equal(typeof graphOrderPhrase, "function");
assert.equal(typeof knowledgeEmptyHint, "function");
assert.equal(typeof graphFallbackStatus, "function");

// The fallback after a removed node names the order actually in use.
assert.equal(graphOrderPhrase("balanced"), "the strongest nodes");
assert.equal(graphOrderPhrase("important"), "the most important nodes");
assert.equal(graphOrderPhrase("recent"), "the most recently created nodes");
assert.equal(graphOrderPhrase("anything else"), "the strongest nodes");

// The empty-knowledge hint never suggests what is already set.
const base = { recorded: 12, focused: false, searching: false, order: "balanced", knowledgeOn: true };
assert.equal(knowledgeEmptyHint({ ...base, recorded: 0 }), "No knowledge recorded yet.");
assert.match(knowledgeEmptyHint({ ...base, focused: true }), /neighborhood/);
assert.doesNotMatch(knowledgeEmptyHint({ ...base, focused: true }), /Balanced mix|turn on Knowledge/i);
assert.match(knowledgeEmptyHint({ ...base, searching: true }), /search/);
assert.doesNotMatch(knowledgeEmptyHint({ ...base, searching: true }), /Balanced mix|turn on Knowledge/i);
assert.match(knowledgeEmptyHint({ ...base, order: "important" }), /Balanced mix/);
assert.doesNotMatch(knowledgeEmptyHint(base), /Balanced mix|turn on Knowledge/i, "Balanced mix is already chosen");
assert.match(knowledgeEmptyHint({ ...base, knowledgeOn: false }), /turn on Knowledge/i);
assert.doesNotMatch(knowledgeEmptyHint({ ...base, knowledgeOn: false }), /Balanced mix/);
for (const state of [base, { ...base, order: "recent", knowledgeOn: false }])
  assert.ok(knowledgeEmptyHint(state).length > 10, JSON.stringify(state));

// Combined states. Focused requests ignore the type chips, so focus wins. Otherwise a hidden
// Knowledge chip explains an empty panel before a search or order does: notes are removed by type
// before the search runs.
assert.match(knowledgeEmptyHint({ ...base, searching: true, knowledgeOn: false }), /turn on Knowledge/i);
assert.match(knowledgeEmptyHint({ ...base, order: "important", knowledgeOn: false }), /turn on Knowledge/i);
assert.match(knowledgeEmptyHint({ ...base, focused: true, knowledgeOn: false }), /neighborhood/);
assert.match(knowledgeEmptyHint({ ...base, searching: true, order: "recent" }), /search/);

// After a removed node, the retry keeps any search text, so the status names the search when one
// is set and the order otherwise.
assert.match(graphFallbackStatus("balanced", ""), /merged or removed.*Showing the strongest nodes\./);
assert.match(graphFallbackStatus("recent", ""), /Showing the most recently created nodes\./);
assert.match(graphFallbackStatus("important", "cart"), /match “cart”/);
assert.doesNotMatch(graphFallbackStatus("important", "cart"), /most important|strongest/);

// A search under Most important or Recently created cuts the matches with no share per type, so
// notes that match can be pushed out: suggest Balanced mix instead of claiming no match.
const narrowed = knowledgeEmptyHint({ ...base, searching: true, order: "important" });
assert.match(narrowed, /Balanced mix/);
assert.doesNotMatch(narrowed, /No knowledge matches this search/);
assert.equal(knowledgeEmptyHint({ ...base, searching: true }), "No knowledge matches this search.");

console.log("graph wording checks passed");
