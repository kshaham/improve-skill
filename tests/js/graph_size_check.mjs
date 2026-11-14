// Self-checking harness for importance-sized graph nodes in assets/board/graph.js, run by
// tests/test_board_js.py. Nodes used to be sized in layout units and scaled with the view, so at
// 220 nodes (fit scale about 0.2) every dot was 1-3px on screen. Sizes are now screen pixels:
// most nodes stay small, the most important few are large, and zooming changes them gently.
// Usage: node graph_size_check.mjs <graph-layout.js> <graph.js>
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const context = vm.createContext({ console, Math, document: {}, window: {}, matchMedia: () => ({ matches: false }) });
vm.runInContext(readFileSync(process.argv[2], "utf8"), context);
vm.runInContext(
  readFileSync(process.argv[3], "utf8") +
    "\n;this.graphPercentiles = graphPercentiles; this.graphNodeRadius = graphNodeRadius; this.graphEdgeWidth = graphEdgeWidth;",
  context,
);
const { graphPercentiles, graphNodeRadius, graphEdgeWidth } = context;
assert.equal(typeof graphPercentiles, "function");
assert.equal(typeof graphNodeRadius, "function");

// Percentiles: pinned first, then by score; the top node is 1 and the last is 0.
const nodes = Array.from({ length: 220 }, (_, index) => ({ id: "n" + index, score: (index % 50) / 50 }));
nodes.push({ id: "pin", score: 0.01, pinned: true });
const ranks = graphPercentiles(nodes);
assert.equal(ranks.get("pin"), 1, "a pinned node ranks first whatever its score");
assert.ok(ranks.get("n49") > ranks.get("n48"), "a higher score ranks higher");
assert.equal(graphPercentiles([{ id: "solo", score: 0 }]).get("solo"), 1, "a single node is the top node");
// Ties share the middle of their group, so a graph whose scores are all equal (a fresh one is
// all zero) draws every node at a middle size rather than the smallest.
const flat = graphPercentiles(Array.from({ length: 37 }, (_, index) => ({ id: "f" + index, score: 0 })));
assert.deepEqual([...new Set(flat.values())], [0.5]);
assert.equal(ranks.get("n0"), ranks.get("n50"), "equal scores draw at an equal size");
assert.ok(ranks.get("n0") < ranks.get("n1"), "the lowest tie group still ranks below the rest");
// Knowledge is ranked among knowledge, so the most valuable notes are drawn large even beside
// files that score higher.
const mixed = graphPercentiles([
  ...Array.from({ length: 30 }, (_, index) => ({ id: "file" + index, type: "file", score: 0.9 + index / 1000 })),
  { id: "best-note", type: "insight", score: 0.6 },
  { id: "weak-note", type: "insight", score: 0.1 },
  { id: "mid-note", type: "insight", score: 0.3 },
]);
assert.equal(mixed.get("best-note"), 1, "the strongest note is a top node");
assert.equal(mixed.get("weak-note"), 0);
assert.equal(mixed.get("file29"), 1, "the strongest file is a top node too");

// In a dense view (220 nodes) most nodes stay small and the top few are large.
const dense = (percentile, zoomRatio = 1) => graphNodeRadius(percentile, zoomRatio, 220);
assert.ok(dense(0.5) <= 4.5, `median node ${dense(0.5)}px would crowd a dense view`);
assert.ok(dense(1) >= 12, `top node ${dense(1)}px is not prominent`);
assert.ok(dense(0) >= 2.5, `smallest node ${dense(0)}px is too small to see or click`);
for (let step = 0; step < 10; step++)
  assert.ok(dense((step + 1) / 10) > dense(step / 10), "radius grows with importance");

// Zoom: out shrinks toward a floor, in grows toward a ceiling, never unbounded.
assert.ok(dense(1, 0.25) < dense(1), "zooming out shrinks nodes");
assert.ok(dense(0, 0.01) >= 1.5, "zoomed far out, nodes keep a visible floor");
assert.ok(dense(1, 4) > dense(1), "zooming in grows nodes");
assert.ok(dense(1, 100) <= 30, "zoomed far in, nodes stay bounded");

// A small graph has room, so its nodes are larger than in a dense one; the layout can spread a
// small graph as widely as a large one, so the count decides, not the fit scale.
assert.ok(graphNodeRadius(0.5, 1, 37) >= 6, `a 37-node view draws ${graphNodeRadius(0.5, 1, 37)}px nodes`);
assert.ok(graphNodeRadius(1, 1, 37) <= 26, "even sparse, the top node is bounded");
assert.equal(graphNodeRadius(0.5, 1, 500), dense(0.5), "denser than 220 never shrinks below the dense size");

// Edges keep a constant screen width (non-scaling stroke). The heaviest edge stays narrower than
// the smallest node at any zoom, so a node never looks like a bump on a band and a press on it
// grabs the node rather than the edge.
const smallestNode = 2 * graphNodeRadius(0, 0.0001, 500);
for (const weight of [undefined, 0, 1, 3, 10, 1000]) {
  const width = graphEdgeWidth(weight);
  assert.ok(width > 0.5, `edge weight ${weight} draws ${width}px, too faint`);
  assert.ok(width < smallestNode, `edge weight ${weight} draws ${width}px, wider than a ${smallestNode}px node`);
}
assert.ok(graphEdgeWidth(5) > graphEdgeWidth(1), "heavier edges draw wider");

console.log("graph size checks passed");
