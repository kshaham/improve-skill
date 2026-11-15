"""The board's motion system: one set of scales, animations that never touch layout, a complete
reduced-motion switch, and entrances that play only for what is new."""
import json
from pathlib import Path
import re
import subprocess
import unittest

from support import budget
from test_board_js import NODE

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "assets" / "board"
CSS = (BOARD / "board.css").read_text()
HARNESS = Path(__file__).resolve().parent / "js"
# Properties whose animation forces layout on every frame.
LAYOUT = {"width", "height", "top", "left", "right", "bottom", "margin", "padding", "inset", "font-size",
          "min-width", "max-width", "min-height", "max-height", "gap", "flex", "grid-template-columns"}


def keyframes():
    """Each @keyframes name with the property names its steps set."""
    found = {}
    for match in re.finditer(r"@keyframes\s+([\w-]+)\s*\{", CSS):
        depth, index = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(CSS[index], 0)
            index += 1
        body = CSS[match.end():index - 1]
        found[match.group(1)] = set(re.findall(r"(?:^|[;{\s])([a-z-]+)\s*:", body))
    return found


class ScaleTests(unittest.TestCase):
    def test_spacing_type_and_motion_scales_exist_once(self):
        scales = CSS[CSS.index("/* Scales that do not change with the theme"):]
        block = scales[scales.index(":root {"):scales.index("\n}")]
        for name in ("--space-1", "--space-4", "--space-8", "--text-xs", "--text-md", "--text-3xl", "--font-mono",
                     "--ease-out", "--ease-in-out", "--ease-spring", "--dur-1", "--dur-2", "--dur-3", "--stagger"):
            self.assertEqual(len(re.findall(r"\n\s*" + re.escape(name) + r":", CSS)), 1, name)
            self.assertIn(name + ":", block)
        spaces = [int(value) for value in re.findall(r"--space-\d: (\d+)px", block)]
        self.assertTrue(all(value % 4 == 0 for value in spaces), "spacing sits on a 4px grid")
        self.assertEqual(re.search(r"--text-xs: (\d+)px", block).group(1), "11", "the type scale starts at the 11px floor")

    def test_motion_uses_the_scales(self):
        motion = CSS[CSS.index("/* Motion. Each animation says"):]
        timed = [value for value in re.findall(r"(?<![-\w])(?:animation|transition):\s*([^;]+);", motion)
                 if value.strip() != "none"]
        self.assertGreaterEqual(len(timed), 6)
        for value in timed:
            self.assertRegex(value, r"var\(--(dur|ease)|linear|\d+m?s", value)
        self.assertIn("calc(var(--i, 0) * var(--stagger))", motion)


class KeyframeTests(unittest.TestCase):
    def test_no_animation_moves_layout(self):
        frames = keyframes()
        self.assertGreaterEqual(len(frames), 10)
        for name, properties in frames.items():
            self.assertEqual(properties & LAYOUT, set(), f"@keyframes {name} animates layout")

    def test_every_keyframes_rule_is_used(self):
        for name in keyframes():
            uses = re.findall(r"animation(?:-name)?:[^;]*\b" + re.escape(name) + r"\b", CSS)
            self.assertTrue(uses, f"@keyframes {name} is never used")

    def test_every_animation_names_a_keyframes_rule(self):
        frames = keyframes()
        for value in re.findall(r"(?<![-\w])animation:\s*([^;]+);", CSS):
            for part in value.split(","):
                words = [word for word in re.findall(r"[a-z][\w-]*", part)
                         if word not in ("none", "linear", "ease", "ease-out", "ease-in-out", "infinite", "both",
                                         "backwards", "forwards", "var", "ms", "s", "important")
                         and not word.startswith(("dur-", "ease-"))]
                for word in words:
                    self.assertIn(word, frames, f"animation: {value}")


class ReducedMotionTests(unittest.TestCase):
    def test_reduced_motion_turns_everything_off(self):
        block = CSS[CSS.index("@media (prefers-reduced-motion: reduce) {"):]
        block = block[:block.index("\n}\n")]
        everything = re.search(r"\*,\s*\*::before,\s*\*::after \{([^}]*)\}", block).group(1)
        self.assertIn("animation: none !important;", everything)
        self.assertIn("transition: none !important;", everything)
        transitions = re.search(r"::view-transition-new\(\*\) \{([^}]*)\}", block).group(1)
        self.assertIn("animation: none !important;", transitions)
        theme = (BOARD / "theme.js").read_text()
        self.assertIn('window.matchMedia("(prefers-reduced-motion: reduce)").matches', theme)
        self.assertIn("!document.startViewTransition || still", theme, "no view transition without support or with reduced motion")

    def test_entrances_wait_for_a_mark(self):
        # An entrance on every card would replay whenever a list is rebuilt.
        for selector in (".task-card", ".task-row"):
            for body in re.findall(r"\n" + re.escape(selector) + r" \{([^}]*)\}", CSS):
                self.assertNotIn("animation", body, selector)
        self.assertIn(".task-card[data-enter],\n.task-row[data-enter] {", CSS)
        self.assertIn(".task-card[data-moved]::before {", CSS)


class ReviewFindingTests(unittest.TestCase):
    """Three holes an independent review found in the first version of the redesign."""

    def test_nothing_sticks_underneath_the_top_bar(self):
        # The sticky bar covered the History pager and the top of the wiki page list.
        self.assertRegex(CSS, r"\.history-pagination \{\s*position: sticky;\s*top: var\(--sticky-top\);")
        self.assertRegex(CSS, r"\.wiki-pages \{\s*position: sticky;\s*top: calc\(var\(--sticky-top\) \+ 16px\);")
        self.assertIn("--sticky-top: var(--topbar-h);", CSS)
        self.assertRegex(CSS, r"html\.mobile \{\s*--sticky-top: 0px;", "phones have no sticky bar")
        self.assertRegex(CSS, r"html \{\s*scroll-padding-top: calc\(var\(--sticky-top\)")
        self.assertRegex(CSS, r"\.topbar \{\s*height: var\(--topbar-h\);")
        # A media query that changes the bar's height must change the token, not the bar.
        self.assertNotRegex(CSS, r"\.topbar \{\s*height: \d+px;\s*\}")
        self.assertRegex(CSS, r":root \{\s*--topbar-h: 52px;")

    def test_graph_nodes_grow_in_only_the_first_time_they_are_drawn(self):
        # Every knowledge write redraws the graph; an entrance on every node replayed each time.
        self.assertNotRegex(CSS, r"\n\.graph-node circle \{[^}]*animation")
        self.assertIn(".graph-node[data-enter] circle {", CSS)
        graph = (BOARD / "graph.js").read_text()
        self.assertIn('newEntries("graph", nodes.map((node) => node.id))', graph)
        self.assertIn("if (entering.has(index)) {", graph)

    def test_a_second_theme_change_keeps_its_reveal(self):
        theme = (BOARD / "theme.js").read_text()
        self.assertIn("if (themeReveal === transition) delete root.dataset.themeReveal;", theme)


class WiringTests(unittest.TestCase):
    def test_the_page_and_the_server_carry_the_motion_script(self):
        page = (BOARD / "index.html").read_text()
        self.assertLess(page.index('src="/motion.js"'), page.index('src="/board.js"'))
        server = (ROOT / "scripts" / "improve_board.py").read_text()
        self.assertIn('"/motion.js": ("motion.js", "text/javascript; charset=utf-8")', server)

    def test_the_board_marks_what_changed(self):
        script = (BOARD / "board.js").read_text()
        self.assertIn('markEntrances("board", cards);', script)
        self.assertIn("markMoves(cards, tasks);", script)
        self.assertIn('markEntrances(id, [...$(id).querySelectorAll(".task-row")]);', script)
        self.assertIn('tickText($("working"), countText(data.counts.in_progress));', script)
        self.assertIn("positionTabIndicator();", script)

    def test_no_inline_styles_reach_the_page(self):
        # The board's content security policy allows only its own stylesheet.
        self.assertNotRegex((BOARD / "index.html").read_text(), r"<style|\sstyle=")


@unittest.skipUnless(NODE, "node is not installed; board JavaScript checks skipped")
class MotionLogicTests(unittest.TestCase):
    def test_only_what_changed_is_marked(self):
        result = subprocess.run([NODE, str(HARNESS / "motion_check.mjs"), str(BOARD / "motion.js")],
                                capture_output=True, text=True, timeout=budget(30))
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["checks"]), 5)


if __name__ == "__main__":
    unittest.main()
