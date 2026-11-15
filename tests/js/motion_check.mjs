// Self-checking harness for assets/board/motion.js, run by tests/test_board_motion.py.
// The board rebuilds a list whenever its data changes; these checks pin that only new cards
// enter, only changed statuses sweep, and an unchanged figure does not tick.
// Usage: node motion_check.mjs <path to motion.js>
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(process.argv[2], "utf8");
const context = vm.createContext({ console, window: { addEventListener() {} }, document: {} });
vm.runInContext(
  source + "\nthis.api = { newEntries, statusChanges, markEntrances, markMoves, tickText };",
  context,
);
const { newEntries, statusChanges, markEntrances, markMoves, tickText } = context.api;
const checks = [];

function card(key) {
  const properties = new Map();
  return { dataset: { key }, style: { setProperty: (name, value) => properties.set(name, value) }, properties };
}

// A first render shows everything; a rebuild with the same keys shows nothing new.
assert.deepEqual([...newEntries("board", ["a", "b", "c"])], [0, 1, 2]);
assert.deepEqual([...newEntries("board", ["a", "b", "c"])], []);
assert.deepEqual([...newEntries("board", ["c", "d", "a"])], [1]);
assert.deepEqual([...newEntries("history", ["a"])], [0], "each list remembers its own keys");
checks.push("only new keys enter");

// A filtered-out card that returns was seen before, so it does not replay.
newEntries("board", ["a"]);
assert.deepEqual([...newEntries("board", ["a", "b", "c", "d"])], []);
checks.push("a returning card does not replay");

const cards = ["x", "y", "z"].map(card);
markEntrances("rows", cards);
assert.deepEqual(cards.map((item) => "enter" in item.dataset), [true, true, true]);
assert.deepEqual(cards.map((item) => item.properties.get("--i")), [0, 1, 2]);
const again = ["x", "y", "w"].map(card);
markEntrances("rows", again);
assert.deepEqual(again.map((item) => "enter" in item.dataset), [false, false, true]);
assert.equal(again[2].properties.get("--i"), 0, "the stagger counts new items only");
const many = Array.from({ length: 40 }, (_, index) => card("m" + index));
markEntrances("many", many);
assert.equal(Math.max(...many.map((item) => item.properties.get("--i"))), 10, "the stagger is capped");
checks.push("entrances are marked and staggered");

// A first sighting is not a change; a different status is, once.
const tasks = [{ key: "t1", status: "ready" }, { key: "t2", status: "ready" }];
assert.equal(statusChanges(tasks).size, 0);
tasks[0].status = "in_progress";
assert.deepEqual([...statusChanges(tasks)], ["t1"]);
assert.equal(statusChanges(tasks).size, 0);
const moved = [card("t1"), card("t2")];
tasks[1].status = "blocked";
markMoves(moved, tasks);
assert.deepEqual(moved.map((item) => "moved" in item.dataset), [false, true]);
checks.push("only changed statuses sweep");

const figure = { textContent: "5", dataset: {}, offsetWidth: 1 };
tickText(figure, "5");
assert.equal("tick" in figure.dataset, false);
tickText(figure, "6");
assert.equal(figure.textContent, "6");
assert.equal("tick" in figure.dataset, true);
checks.push("a figure ticks only when it changes");

console.log(JSON.stringify({ ok: true, checks }));
