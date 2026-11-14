"""The board's palette: green is the accent, no purple remains anywhere in the front end, the
two dark blocks agree, and the text colours that carry meaning keep WCAG AA contrast."""
import colorsys
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "assets/board"
CSS = (BOARD / "board.css").read_text()

HEX = re.compile(r"#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b")
RGB = re.compile(r"rgba?\(\s*(\d{1,3})[\s,]+(\d{1,3})[\s,]+(\d{1,3})")
HSL = re.compile(r"hsla?\(\s*(\d{1,3})(?:deg)?[\s,]+(\d{1,3})%[\s,]+(\d{1,3})%")
HUE_DEFAULT = re.compile(r"var\(--hue,\s*(\d{1,3})\)")
TOKEN = re.compile(r"^\s*(--[a-z0-9-]+)\s*:\s*([^;]+);", re.M)
# Violet through magenta. A colour this saturated in that band reads as purple.
PURPLE_BAND = (225, 335)
MIN_SATURATION = 0.15


def hex_rgb(value):
    digits = value.lstrip("#")
    if len(digits) in (3, 4):
        digits = "".join(character * 2 for character in digits[:3])
    return tuple(int(digits[index:index + 2], 16) for index in (0, 2, 4))


def hue_saturation_lightness(rgb):
    hue, lightness, saturation = colorsys.rgb_to_hls(*(channel / 255 for channel in rgb))
    return hue * 360, saturation, lightness


def reads_purple(hue, saturation, lightness):
    return (PURPLE_BAND[0] <= hue <= PURPLE_BAND[1] and saturation >= MIN_SATURATION
            and 0.06 < lightness < 0.97)


def luminance(rgb):
    channels = [channel / 255 for channel in rgb]
    linear = [value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first, second):
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def token_blocks():
    """The light block, the system-dark block, and the explicit dark block, as name -> value maps."""
    light = re.search(r"^:root\s*\{(.*?)^\}", CSS, re.S | re.M).group(1)
    system_dark = re.search(r":root:not\(\[data-theme=\"light\"\]\)\s*\{(.*?)\}", CSS, re.S).group(1)
    forced_dark = re.search(r"^:root\[data-theme=\"dark\"\]\s*\{(.*?)^\}", CSS, re.S | re.M).group(1)
    return tuple(dict(TOKEN.findall(block)) for block in (light, system_dark, forced_dark))


class PaletteTests(unittest.TestCase):
    def colour_literals(self):
        for path in sorted(BOARD.iterdir()):
            if path.suffix not in (".css", ".js", ".html"):
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                where = f"{path.name}:{number}"
                for match in HEX.finditer(line):
                    yield where, match.group(0), hue_saturation_lightness(hex_rgb(match.group(0)))
                for match in RGB.finditer(line):
                    rgb = tuple(int(part) for part in match.groups())
                    yield where, match.group(0), hue_saturation_lightness(rgb)
                for match in HSL.finditer(line):
                    hue, saturation, lightness = (int(part) for part in match.groups())
                    yield where, match.group(0), (hue, saturation / 100, lightness / 100)

    def test_the_scan_sees_the_palette(self):
        literals = list(self.colour_literals())
        self.assertGreater(len(literals), 40, "the colour scan found too little to prove anything")

    def test_no_purple_colour_literal_remains(self):
        purple = [f"{where} {text}" for where, text, colour in self.colour_literals() if reads_purple(*colour)]
        self.assertEqual(purple, [], "purple colours left in the board front end")

    def test_no_purple_area_hue_remains(self):
        hues = [(f"board.css default {match.group(0)}", int(match.group(1))) for match in HUE_DEFAULT.finditer(CSS)]
        focus = (BOARD / "focus.js").read_text()
        table = re.search(r"const FOCUS_AREAS = \[(.*?)\n\];", focus, re.S).group(1)
        rows = re.findall(r"\[\"([a-z-]+)\",.*?,\s*(\d{1,3})\],", table)
        self.assertGreaterEqual(len(rows), 14, "every focus area has a hue")
        hues += [(f"focus.js {name}", int(hue)) for name, hue in rows]
        fallback = re.search(r"\?\?\s*(\d{1,3});", focus)
        hues.append(("focus.js fallback hue", int(fallback.group(1))))
        purple = [f"{where}: {hue}" for where, hue in hues if PURPLE_BAND[0] <= hue <= PURPLE_BAND[1]]
        self.assertEqual(purple, [], "area hues in the purple band")

    def test_no_token_or_class_is_named_purple(self):
        for path in sorted(BOARD.iterdir()):
            if path.suffix in (".css", ".js", ".html"):
                self.assertNotRegex(path.read_text().lower(), r"purple|violet|indigo|lilac", path.name)

    def test_the_accent_is_green(self):
        for name, tokens in zip(("light", "system dark", "forced dark"), token_blocks()):
            hue, saturation, _ = hue_saturation_lightness(hex_rgb(tokens["--accent"]))
            self.assertTrue(120 <= hue <= 170, f"{name} accent hue {hue:.0f} is not green")
            self.assertGreaterEqual(saturation, 0.35, f"{name} accent is too grey to read as green")


class TokenTests(unittest.TestCase):
    def test_both_dark_blocks_are_identical(self):
        _, system_dark, forced_dark = token_blocks()
        self.assertEqual(system_dark, forced_dark, "the system-dark and data-theme dark palettes drifted apart")

    def test_light_and_dark_define_the_same_tokens(self):
        light, system_dark, _ = token_blocks()
        self.assertEqual(sorted(light), sorted(system_dark))

    def test_area_chips_keep_aa_contrast_for_every_area_in_both_themes(self):
        """Area tags tint by hue; their lightness and saturation come from theme tokens."""
        focus = (BOARD / "focus.js").read_text()
        table = re.search(r"const FOCUS_AREAS = \[(.*?)\n\];", focus, re.S).group(1)
        hues = [int(hue) for _, hue in re.findall(r"\[\"([a-z-]+)\",.*?,\s*(\d{1,3})\],", table)]
        self.assertIn("hsl(var(--hue, 196) var(--chip-ink-s) var(--chip-ink-l))", CSS)
        self.assertIn("hsl(var(--hue, 196) var(--chip-s) var(--chip-l))", CSS)
        percent = lambda tokens, name: int(tokens[name].rstrip("%")) / 100
        light, dark, _ = token_blocks()
        for theme, tokens in (("light", light), ("dark", dark)):
            for hue in hues:
                ink = colorsys.hls_to_rgb(hue / 360, percent(tokens, "--chip-ink-l"), percent(tokens, "--chip-ink-s"))
                chip = colorsys.hls_to_rgb(hue / 360, percent(tokens, "--chip-l"), percent(tokens, "--chip-s"))
                ratio = contrast(tuple(round(value * 255) for value in ink), tuple(round(value * 255) for value in chip))
                with self.subTest(theme=theme, hue=hue):
                    self.assertGreaterEqual(ratio, 4.5, f"{ratio:.2f}:1")

    def test_text_colours_outside_the_token_blocks_use_tokens(self):
        """A hard-coded text colour ignores the theme: a dark-red error on the dark panel failed AA."""
        body = CSS[CSS.index(':root[data-theme="dark"]'):]
        body = body[body.index("\n}") + 2:]
        literal = re.compile(r"(?<![-\w])color:\s*(#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(\s*\d)")
        offenders = [line.strip() for line in body.splitlines() if literal.search(line)]
        self.assertEqual(offenders, [], "text colours that bypass the theme tokens")

    def test_text_tokens_keep_aa_contrast_in_both_themes(self):
        pairs = [("--ink", "--surface", 7), ("--ink-2", "--surface", 4.5), ("--muted", "--surface", 4.5),
                 ("--muted", "--page", 4.5), ("--muted", "--surface-2", 4.5), ("--accent-ink", "--surface", 4.5),
                 ("--accent-ink", "--page", 4.5), ("--accent-ink", "--accent-soft", 4.5),
                 ("--on-accent", "--accent", 4.5), ("--on-accent", "--accent-strong", 4.5),
                 ("--success-ink", "--success-soft", 4.5), ("--warn-fg", "--warn-bg", 4.5),
                 ("--danger-ink", "--danger-soft", 4.5), ("--info-ink", "--info-soft", 4.5),
                 ("--neutral-ink", "--neutral-soft", 4.5), ("--sidebar-ink", "--sidebar", 7),
                 ("--sidebar-muted", "--sidebar", 4.5)]
        light, dark, _ = token_blocks()
        for theme, tokens in (("light", light), ("dark", dark)):
            for foreground, background, minimum in pairs:
                with self.subTest(theme=theme, pair=f"{foreground} on {background}"):
                    ratio = contrast(hex_rgb(tokens[foreground]), hex_rgb(tokens[background]))
                    self.assertGreaterEqual(ratio, minimum, f"{ratio:.2f}:1")


if __name__ == "__main__":
    unittest.main()
