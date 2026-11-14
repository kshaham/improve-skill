"""Board JavaScript checks that need only Node, not a browser: every board script parses, and
the drafts store survives cancelled Retry/Withdraw forms and bad rows. Skipped without Node."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from support import budget

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "board"
HARNESS = Path(__file__).resolve().parent / "js"


def find_node():
    """Node on PATH, or the Homebrew location used on the development Macs."""
    for candidate in (os.environ.get("IMPROVE_TEST_NODE"), shutil.which("node"), "/opt/homebrew/bin/node",
                      "/usr/local/bin/node"):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


NODE = find_node()


@unittest.skipUnless(NODE, "node is not installed; board JavaScript checks skipped")
class BoardScriptTests(unittest.TestCase):
    def node(self, *args):
        return subprocess.run([NODE, *map(str, args)], capture_output=True, text=True, timeout=budget(30))

    def test_every_board_script_parses(self):
        scripts = sorted(ASSETS.glob("*.js"))
        self.assertGreater(len(scripts), 10)
        for script in scripts:
            with self.subTest(script=script.name):
                result = self.node("--check", script)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_drafts_survive_cancelled_forms_and_bad_rows(self):
        result = self.node(HARNESS / "drafts_check.mjs", ASSETS / "drafts.js")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["checks"]), 5)


    def test_graph_messages_match_the_current_view(self):
        result = self.node(HARNESS / "graph_wording_check.mjs", ASSETS / "graph-layout.js", ASSETS / "graph.js")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("graph wording checks passed", result.stdout)
        source = (ASSETS / "graph.js").read_text()
        # The node_missing handler uses the tested helper with the live order and search text.
        self.assertIn('graphStatus(graphFallbackStatus(graphOrder(), $("graph-query").value.trim()));', source)

    def test_graph_nodes_are_sized_by_importance_in_screen_pixels(self):
        result = self.node(HARNESS / "graph_size_check.mjs", ASSETS / "graph-layout.js", ASSETS / "graph.js")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("graph size checks passed", result.stdout)
        source = (ASSETS / "graph.js").read_text()
        # Nodes sit outside the zoomed viewport, so the zoom does not shrink them to dots.
        self.assertIn("canvas.replaceChildren(viewport, nodeLayer);", source)
        # Edges stay in the zoomed group but keep their screen width.
        self.assertIn('"vector-effect": "non-scaling-stroke"', source)
        self.assertIn('"stroke-width": graphEdgeWidth(edge.weight).toFixed(2)', source)
        self.assertIn("(point.x * scale + x)", source)
        # The hit area is a rect, so it inherits none of the .graph-node circle rules. Hit areas
        # sit in their own layer beneath every node, so a visible node always wins and the DOM
        # (and keyboard tab) order stays most important first.
        self.assertIn('svg("rect", { class: "graph-hit"', source)
        self.assertIn("nodeLayer.append(hitLayer);", source)
        self.assertIn('graphElements.nodes.get(event.target.closest?.(".graph-hit")?.dataset.id)', source)
        css = (ASSETS / "board.css").read_text()
        rule = css[css.index(".graph-hit {"):]
        rule = rule[:rule.index("}")]
        self.assertIn("fill: transparent;", rule)
        self.assertIn("pointer-events: all;", rule)
        self.assertIn("sizeGraphNodes();", source[source.index("function positionGraph()"):])
        # The fit reserves screen room for labels (at most 30% of a phone canvas) and the legend,
        # and zooming out can always reach a fit below the usual 0.15 floor.
        fit = source[source.index("function fitGraph()"):source.index("function zoomGraph(")]
        self.assertIn("Math.min(GRAPH_LABEL_ROOM, width * 0.3)", fit)
        self.assertIn("- GRAPH_LEGEND_ROOM", fit)
        self.assertIn("Math.max(Math.min(0.15, graphViewport.fit),", source)

    def test_graph_rings_scale_with_small_nodes_and_edges_meet_centres(self):
        # Rings are screen strokes; on a 2.5px node a 2.5px ring hid the node's own colour, so a
        # blocked task read as a Blocker. Every ring is capped at 30% of the node radius.
        css = (ASSETS / "board.css").read_text()
        for selector, width in [(".graph-node circle {", "1.5px"), (".graph-node.type-task circle {", "2.5px"),
                                (".graph-node.pinned circle {", "2.5px"), (".graph-node:focus-visible circle {", "3.5px")]:
            rule = css[css.index(selector):]
            rule = rule[:rule.index("}")]
            self.assertIn(f"stroke-width: min({width}, calc(var(--node-r, 10px) * 0.3));", rule, selector)
        source = (ASSETS / "graph.js").read_text()
        self.assertIn('setProperty("--node-r", radius.toFixed(1) + "px")', source)
        # Nodes use the same rounded viewport values as the edge transform, so edges meet centres.
        position = source[source.index("function positionGraph()"):source.index("function animateGraph()")]
        self.assertIn("const scale = Number(graphViewport.scale.toFixed(3)),", position)
        # Edge endpoints are written to 0.1 layout units; node points are rounded the same way.
        self.assertIn("const point = { x: Number(raw.x.toFixed(1)), y: Number(raw.y.toFixed(1)) };", position)
        self.assertIn('line.setAttribute("x1", a.x.toFixed(1));', position)

    def test_crowded_graph_labels_wait_for_hover_or_selection(self):
        result = self.node(HARNESS / "graph_label_check.mjs", ASSETS / "graph-layout.js", ASSETS / "graph.js")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("graph label checks passed", result.stdout)
        source = (ASSETS / "graph.js").read_text()
        self.assertIn("crowdGraphLabels(placed);", source[source.index("function positionGraph()"):])
        css = (ASSETS / "board.css").read_text()
        self.assertIn(
            ".graph-node.labelled.label-crowded:not(:hover):not(:focus-visible):not(.selected):not(.linked) text {\n  display: none;",
            css,
        )

    def test_key_knowledge_glows_without_an_extra_circle(self):
        source = (ASSETS / "graph.js").read_text()
        self.assertIn('(key.has(node.id) ? " key" : "")', source)
        self.assertIn("new Set([...key, ...", source, "key notes are labelled first")
        self.assertIn('"Key knowledge"', source[source.index("function renderLegend()"):])
        css = (ASSETS / "board.css").read_text()
        rule = css[css.index(".graph-node.key circle {"):]
        rule = rule[:rule.index("}")]
        self.assertIn("filter: drop-shadow(", rule)
        self.assertNotIn("stroke", rule, "selection and focus keep the stroke")

    def test_graph_labels_hold_still_while_the_layout_moves(self):
        # Recomputing crowding every frame hid a dragged node's label and made neighbours flicker.
        source = (ASSETS / "graph.js").read_text()
        position = source[source.index("function positionGraph()"):source.index("function animateGraph()")]
        self.assertIn("if (!graphFrame) crowdGraphLabels(placed);", position)
        tick = source[source.index("const tick = () => {"):]
        tick = tick[:tick.index("};")]
        self.assertLess(tick.index("if (moving) graphFrame = requestAnimationFrame(tick);"), tick.index("positionGraph();"),
                        "the next frame is requested before positioning, so a moving layout skips crowding")

    def test_a_redrawn_graph_decides_label_crowding_at_once(self):
        # A redraw while the old animation still runs built groups without label-crowded, and
        # crowding waited about two seconds for the layout to settle.
        source = (ASSETS / "graph.js").read_text()
        draw = source[source.index("function drawGraph()"):source.index("function renderLegend()")]
        self.assertIn("graphCrowdNow = true;\n  positionGraph();", draw)
        self.assertIn("if (graphCrowdNow) {\n    graphCrowdNow = false;\n    crowdGraphLabels(placed);", source)

if __name__ == "__main__":
    unittest.main()
