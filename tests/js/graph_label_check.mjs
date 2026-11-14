// Self-checking harness for graph label crowding in assets/board/graph.js, run by
// tests/test_board_js.py. With node labels at a constant 11px, labels in a dense core ran over
// each other and over neighbouring dots, and long labels could run past the canvas edge.
// Usage: node graph_label_check.mjs <graph-layout.js> <graph.js>
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const context = vm.createContext({ console, Math, document: {}, window: {}, matchMedia: () => ({ matches: false }) });
vm.runInContext(readFileSync(process.argv[2], "utf8"), context);
vm.runInContext(readFileSync(process.argv[3], "utf8") + "\n;this.graphCrowdedLabels = graphCrowdedLabels;", context);
const { graphCrowdedLabels } = context;
assert.equal(typeof graphCrowdedLabels, "function");

const bounds = { width: 800, height: 500 };
// A label box: left edge, vertical centre, width; the label's own node is at (x - 8, y).
const label = (id, x, y, width = 100) => ({ id, x, y, width, height: 14 });
const dot = (id, x, y, r = 3) => ({ id, x, y, r });

// Labels arrive most important first; a later label that overlaps an earlier one is hidden.
let hidden = graphCrowdedLabels([label("a", 100, 100), label("b", 150, 104), label("c", 100, 200)], [], bounds);
assert.deepEqual([...hidden], ["b"]);

// A label that covers another prominent node (radius 5px or more) is hidden; its own dot never
// counts. The smallest dots do not: in a dense core every label crosses one, and the label's halo
// keeps both readable, so counting them hid 35 of 36 labels.
hidden = graphCrowdedLabels([label("a", 100, 100)], [dot("a", 92, 100, 12), dot("x", 160, 101, 6)], bounds);
assert.deepEqual([...hidden], ["a"]);
hidden = graphCrowdedLabels([label("a", 100, 100)], [dot("a", 92, 100), dot("x", 160, 101, 3.5)], bounds);
assert.deepEqual([...hidden], [], "a small dot under a label does not hide it");
hidden = graphCrowdedLabels([label("a", 100, 100)], [dot("a", 92, 100), dot("x", 160, 140)], bounds);
assert.deepEqual([...hidden], []);

// A label that would leave the canvas is hidden.
hidden = graphCrowdedLabels([label("edge", 750, 100), label("top", 100, 3)], [], bounds);
assert.deepEqual([...hidden].sort(), ["edge", "top"]);

// A hidden label does not block a later one.
hidden = graphCrowdedLabels(
  // c overlaps a's box, but a is hidden (it covers z), so c is shown.
  [label("a", 100, 100), label("b", 750, 300), label("c", 150, 104)],
  [dot("z", 120, 100, 8)],
  bounds,
);
assert.deepEqual([...hidden].sort(), ["a", "b"]);

console.log("graph label checks passed");
