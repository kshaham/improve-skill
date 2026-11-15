"""The board's "Work in parallel" switch and slider: where it sits, what it sends, and what the
skill is told to do with it."""
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "assets" / "board"
sys.path.insert(0, str(ROOT / "scripts"))
import improve_control as control  # noqa: E402

PAGE = (BOARD / "index.html").read_text()
CSS = (BOARD / "board.css").read_text()
AGENTS = (BOARD / "agents.js").read_text()
FOCUS = (BOARD / "focus.js").read_text()


class MarkupTests(unittest.TestCase):
    def test_the_control_sits_above_the_improvement_categories(self):
        self.assertLess(PAGE.index('id="focus-body"'), PAGE.index('id="agents-control"'))
        self.assertLess(PAGE.index('id="agents-control"'), PAGE.index("<h3>Improvement categories</h3>"))
        self.assertLess(PAGE.index('id="agents-enabled" type="checkbox"'), PAGE.index('id="agents-count"'))

    def test_the_slider_runs_from_one_to_the_server_limit_and_starts_at_five(self):
        slider = re.search(r'<input id="agents-count"[^>]*>', PAGE).group(0)
        self.assertIn('type="range" min="1" max="10" step="1" value="5"', slider)
        self.assertIn("aria-label=", slider)
        self.assertEqual(control.MAX_AGENTS, 10)
        self.assertIn("const AGENTS_MAX = 10;", AGENTS)
        self.assertIn("const AGENTS_DEFAULT = 5;", AGENTS)

    def test_the_script_is_loaded_before_the_focus_panel_and_served(self):
        self.assertLess(PAGE.index('src="/motion.js"'), PAGE.index('src="/agents.js"'))
        self.assertLess(PAGE.index('src="/agents.js"'), PAGE.index('src="/focus.js"'))
        server = (ROOT / "scripts" / "improve_board.py").read_text()
        self.assertIn('"/agents.js": ("agents.js", "text/javascript; charset=utf-8")', server)


class BehaviourPins(unittest.TestCase):
    def test_off_sends_no_field_and_on_sends_the_count(self):
        self.assertIn("if (focusAgents) payload.agents = focusAgents;", FOCUS)
        self.assertIn("focusAgents = event.target.checked ? agentsChoice : 0;", AGENTS)

    def test_the_choice_counts_as_an_unsaved_change_and_shows_in_the_summary(self):
        self.assertIn("focusAgents !== saved.agents;", FOCUS)
        self.assertIn('agents ? "In parallel: " + agentsLabel(agents) : ""', FOCUS)
        self.assertIn("focusSummary(row.areas, row.themes, row.agents)", (BOARD / "controls.js").read_text())

    def test_a_spoken_focus_change_keeps_the_choice(self):
        # A focus request replaces the one before it.
        self.assertIn("if (savedFocus().agents) payload.agents = savedFocus().agents;", (BOARD / "voice.js").read_text())

    def test_the_slider_is_off_limits_while_the_switch_is_off(self):
        self.assertIn("slider.disabled = !allowed || !on;", AGENTS)
        self.assertIn('slider.setAttribute("aria-valuetext", agentsLabel(agentsChoice));', AGENTS)


class StyleTests(unittest.TestCase):
    def block(self):
        return CSS[CSS.index("/* Parallel helpers: a switch and a slider"):]

    def test_the_fill_and_the_thumb_move_with_transforms(self):
        block = self.block()
        self.assertIn("transform: scaleX(var(--ratio, 0.444));", block)
        fill = next(body for body in re.findall(r"\n\.agents-slider::after \{([^}]*)\}", block) if "scaleX" in body)
        self.assertIn("var(--ease-out)", fill, "the fill never overshoots the rail")
        self.assertIn("animation: swarm 2.4s var(--ease-in-out) infinite;", block)
        self.assertIn(".agents-value[data-tick] {", block)

    def test_the_slider_keeps_a_44px_target_and_a_visible_focus_ring(self):
        block = self.block()
        self.assertRegex(block, r"\.agents-slider input \{[^}]*height: 44px;")
        self.assertIn(".agents-slider input:focus-visible::-webkit-slider-thumb {", block)
        self.assertIn(".agents-slider input:focus-visible::-moz-range-thumb {", block)

    def test_reduced_motion_reaches_the_thumb(self):
        # `*::before` and `*::after` do not match a range input's thumb.
        block = self.block()
        still = block[block.index("@media (prefers-reduced-motion: reduce) {"):]
        still = still[:still.index("\n}\n")]
        for thumb in ("::-webkit-slider-thumb", "::-moz-range-thumb"):
            self.assertRegex(still, re.escape(".agents-slider input" + thumb) + r" \{\s*transition: none;")

    def test_the_control_stacks_on_a_phone(self):
        self.assertRegex(self.block(), r"\.mobile \.agents-control \{\s*grid-template-columns: minmax\(0, 1fr\);")


class GuidanceTests(unittest.TestCase):
    def test_the_skill_is_told_what_the_setting_means(self):
        guide = (ROOT / "references" / "board-control.md").read_text()
        section = guide[guide.index("## Parallel agents"):]
        for phrase in ("Never run more than N at once", "Helpers never edit", "one item at a time",
                       "read-only assignment", "No helpers available?", "turns it off"):
            self.assertIn(phrase, section)
        self.assertIn("agents: N", guide[:guide.index("## Parallel agents")], "the focus row records the field")
        self.assertIn("run.json.focus.board_areas.agents", (ROOT / "SKILL.md").read_text())


if __name__ == "__main__":
    unittest.main()
