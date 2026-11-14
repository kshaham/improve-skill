"""TOON encoder and decoder: the official conformance fixtures, round trips, randomized
values, host-type normalization, the view helpers, the CLI, and the token economics the
format exists for."""
import datetime
import decimal
import json
from pathlib import Path
import random
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import support  # noqa: E402,F401
from support import budget  # noqa: E402
import improve_toon as toon  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "toon"


def fixture_suites(category):
    for path in sorted((FIXTURES / category).glob("*.json")):
        yield path.name, json.loads(path.read_text())


class ConformanceTests(unittest.TestCase):
    """Every encode fixture must match byte for byte; every decode fixture must produce the
    expected value or raise, honoring strict and indentSize options."""

    def test_fixtures_are_present_and_licensed(self):
        self.assertTrue((FIXTURES / "LICENSE").read_text().startswith("MIT License"))
        self.assertGreaterEqual(sum(len(suite["tests"]) for _, suite in fixture_suites("encode")), 150)
        self.assertGreaterEqual(sum(len(suite["tests"]) for _, suite in fixture_suites("decode")), 400)

    def test_encode_fixtures(self):
        failures = []
        for name, suite in fixture_suites("encode"):
            for test in suite["tests"]:
                options = test.get("options", {})
                got = toon.dumps(test["input"], indent=options.get("indentSize", 2), delimiter=options.get("delimiter", ","))
                if got != test["expected"]:
                    failures.append(f"{name}: {test['name']}: expected {test['expected']!r}, got {got!r}")
        self.assertEqual(failures, [])

    def test_decode_fixtures(self):
        failures = []
        for name, suite in fixture_suites("decode"):
            for test in suite["tests"]:
                options = test.get("options", {})
                kwargs = dict(indent=options.get("indentSize", 2), strict=options.get("strict", True))
                try:
                    got = toon.loads(test["input"], **kwargs)
                except toon.ToonError:
                    if not test.get("shouldError"):
                        failures.append(f"{name}: {test['name']}: unexpected error for {test['input']!r}")
                    continue
                if test.get("shouldError"):
                    failures.append(f"{name}: {test['name']}: expected an error, got {got!r}")
                elif got != test["expected"]:
                    failures.append(f"{name}: {test['name']}: expected {test['expected']!r}, got {got!r}")
        self.assertEqual(failures, [])

    def test_every_encode_fixture_round_trips(self):
        for name, suite in fixture_suites("encode"):
            for test in suite["tests"]:
                options = test.get("options", {})
                encoded = toon.dumps(test["input"], indent=options.get("indentSize", 2), delimiter=options.get("delimiter", ","))
                decoded = toon.loads(encoded, indent=options.get("indentSize", 2))
                self.assertEqual(decoded, test["input"], f"{name}: {test['name']}")

    def test_decode_errors_carry_line_numbers(self):
        with self.assertRaises(toon.ToonDecodeError) as caught:
            toon.loads("a: 1\nitems[2]{id}:\n  1\n  2,3")
        self.assertEqual(caught.exception.line, 4)


def random_value(rng, depth=0):
    """A random JSON-shaped value with the shapes the skill emits: uniform rows, nested
    objects, primitive arrays, awkward strings."""
    kind = rng.random()
    if depth >= 3 or kind < 0.35:
        return rng.choice([
            None, True, False, 0, -1, 42, 3.5, -0.25, 1e-7, 123456789012345678, 2.5e21,
            "", "plain", "with, comma", "colon: here", "  padded  ", "true", "42", "-dash", "#hash",
            "quote \" inside", "back\\slash", "tab\tand\nnewline", "unicode 世界 👋", "[bracket]", "{brace}",
            "pipe|pipe", "a" * 300,
        ])
    if kind < 0.55:
        return [random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    if kind < 0.7:
        keys = ["id", "name", "status", "weight"]
        return [{key: random_value(rng, 3) for key in keys} for _ in range(rng.randint(1, 5))]
    return {rng.choice(["id", "my-key", "a.b", "", "k e y", "ünï", "42", "x"]) + str(index): random_value(rng, depth + 1)
            for index in range(rng.randint(0, 4))}


class RoundTripTests(unittest.TestCase):
    def test_random_values_round_trip(self):
        rng = random.Random(20261108)
        for _ in range(400):
            value = random_value(rng)
            for delimiter in (",", "\t", "|"):
                text = toon.dumps(value, delimiter=delimiter)
                self.assertEqual(toon.loads(text), value, f"{value!r} via {text!r}")
                self.assertFalse(text.endswith("\n"))
                self.assertNotIn(" \n", text)
                self.assertTrue(all(not line.endswith(" ") for line in text.split("\n")))

    def test_numbers_are_canonical(self):
        cases = {0: "0", -0.0: "0", 1.0: "1", 1.5000: "1.5", 1e6: "1000000", 1e-6: "0.000001", 1e-7: "1e-7",
                 1e21: "1e+21", 123456789: "123456789", -2.5: "-2.5", 0.1 + 0.2: "0.30000000000000004",
                 9007199254740993: "9007199254740993"}
        for number, expected in cases.items():
            self.assertEqual(toon.dumps(number), expected, number)
        self.assertEqual(toon.loads("1E+03"), 1000)
        self.assertEqual(toon.loads("-0"), 0)
        self.assertEqual(toon.loads("05"), "05", "a token outside the JSON number grammar is a string")
        self.assertEqual(toon.loads("1e999"), "1e999", "a number too large for a float decodes losslessly as text")

    def test_strings_quote_exactly_when_required(self):
        for text in ("", " x", "x ", "true", "null", "42", "1e-6", "+1", "a:b", 'q"q', "a\\b", "[x]", "{x}", "-x", "#x", "a,b", "a\tb"):
            self.assertTrue(toon.dumps(text).startswith('"'), text)
        for text in ("hello", "Ada_99", "two words", "世界", "a|b", "x-y", "3 apples"):
            self.assertEqual(toon.dumps(text), text)
        self.assertEqual(toon.dumps("a|b", delimiter="|"), '"a|b"')
        self.assertEqual(toon.dumps({"k": "a,b"}, delimiter="\t"), "k: a,b")


class NormalizationTests(unittest.TestCase):
    def test_host_types(self):
        value = {"nan": float("nan"), "inf": float("inf"), "when": datetime.datetime(2026, 11, 8, 12, 30, tzinfo=datetime.timezone.utc),
                 "day": datetime.date(2026, 11, 8), "set": {3, 1, 2}, "tuple": (1, 2), "exact": decimal.Decimal("1.50"),
                 "big": decimal.Decimal("0.1234567890123456789012345"), "whole": decimal.Decimal("7"), "bytes": b"hi",
                 "object": object(), 5: "int key"}
        decoded = toon.loads(toon.dumps(value))
        self.assertIsNone(decoded["nan"])
        self.assertIsNone(decoded["inf"])
        self.assertEqual(decoded["when"], "2026-11-08T12:30:00+00:00")
        self.assertEqual(decoded["day"], "2026-11-08")
        self.assertEqual(decoded["set"], [1, 2, 3])
        self.assertEqual(decoded["tuple"], [1, 2])
        self.assertEqual(decoded["exact"], 1.5)
        self.assertEqual(decoded["big"], "0.1234567890123456789012345", "a Decimal a float cannot hold is kept as text")
        self.assertEqual(decoded["whole"], 7)
        self.assertEqual(decoded["bytes"], "hi")
        self.assertIsNone(decoded["object"])
        self.assertEqual(decoded["5"], "int key")

    def test_unpaired_surrogates_are_refused(self):
        with self.assertRaises(toon.ToonError):
            toon.dumps({"bad": "\ud800"})
        with self.assertRaises(toon.ToonDecodeError):
            toon.loads(b"\xff\xfe")

    def test_options_are_validated(self):
        with self.assertRaises(toon.ToonError):
            toon.dumps({}, delimiter=";")
        with self.assertRaises(toon.ToonError):
            toon.dumps({}, indent=0)
        with self.assertRaises(toon.ToonError):
            toon.loads("", indent=True)

    def test_deep_nesting_is_bounded(self):
        value = {}
        for _ in range(toon.MAX_DEPTH + 5):
            value = {"n": value}
        with self.assertRaises(toon.ToonError):
            toon.dumps(value)
        nearly = {}
        for _ in range(toon.MAX_DEPTH - 1):
            nearly = {"n": nearly}
        try:
            toon.dumps(nearly)
        except toon.ToonError:
            pass  # A small stack may refuse it, but only with the documented error, never RecursionError.


class ViewHelperTests(unittest.TestCase):
    def test_uniform_makes_ragged_rows_tabular(self):
        rows = [{"id": "a", "status": "ready", "note": "x"}, {"id": "b", "status": "done"}, {"id": "c", "commit": "abc"}]
        text = toon.dumps(toon.uniform(rows))
        self.assertTrue(text.startswith("[3]{id,status,note,commit}:"), text)
        self.assertEqual(toon.loads(text)[1], {"id": "b", "status": "done", "note": None, "commit": None})
        self.assertEqual(toon.dumps(toon.uniform(rows, keys=["id", "status"])), "[3]{id,status}:\n  a,ready\n  b,done\n  c,null")
        self.assertEqual(toon.uniform([]), [])
        self.assertEqual(toon.uniform([1, {"a": 1}]), [1, {"a": 1}], "mixed arrays are left alone")

    def test_truncate_bounds_text_everywhere(self):
        value = {"short": "ok", "long": "x" * 50, "rows": [{"t": "y" * 50}], "n": 7}
        cut = toon.truncate(value, 10)
        self.assertEqual(cut["short"], "ok")
        self.assertEqual(cut["long"], "x" * 4 + "\u2026[+46]", "the marker says how much was cut")
        self.assertEqual(cut["rows"][0]["t"], "y" * 4 + "\u2026[+46]")
        self.assertEqual(len(cut["long"]), 10)
        self.assertEqual(toon.truncate("z" * 50, 10, marker="...")[-3:], "...", "an explicit marker is kept")
        self.assertEqual(toon.truncate("text that ends in...", 100), "text that ends in...", "real text is untouched")
        self.assertEqual(cut["n"], 7)
        self.assertIs(toon.truncate(value, 0), value)


class CliTests(unittest.TestCase):
    def run_cli(self, *args, stdin):
        return subprocess.run([sys.executable, str(ROOT / "scripts/improve_toon.py"), *args], input=stdin,
                              capture_output=True, text=True, timeout=budget(30))

    def test_encode_and_decode_through_stdin(self):
        encoded = self.run_cli("encode", stdin=json.dumps({"items": [{"id": 1, "n": "a"}, {"id": 2, "n": "b"}]}))
        self.assertEqual(encoded.returncode, 0, encoded.stderr)
        self.assertEqual(encoded.stdout, "items[2]{id,n}:\n  1,a\n  2,b\n")
        decoded = self.run_cli("decode", stdin=encoded.stdout)
        self.assertEqual(json.loads(decoded.stdout), {"items": [{"id": 1, "n": "a"}, {"id": 2, "n": "b"}]})
        bad = self.run_cli("decode", stdin="items[2]{id}:\n  1")
        self.assertEqual(bad.returncode, 2)
        lenient = self.run_cli("decode", "--lenient", stdin="items[2]{id}:\n  1")
        self.assertEqual(json.loads(lenient.stdout), {"items": [{"id": 1}]})


def sample_backlog(count=40):
    rng = random.Random(7)
    areas = ["quality", "security", "ui", "performance", "docs", "coverage"]
    statuses = ["ready"] * 5 + ["done"] * 3 + ["rejected", "blocked", "proposed"]
    rows = []
    for index in range(count):
        status = rng.choice(statuses)
        rows.append({"id": f"q-{index:04d}", "area": rng.choice(areas), "tier": rng.randint(1, 4), "severity": rng.randint(1, 5),
                     "confidence": round(rng.uniform(0.5, 1.0), 2), "blast": rng.randint(1, 5),
                     "file": f"src/module{index % 9}/handler{index % 4}.py", "line": rng.randint(10, 900),
                     "title": f"Fix the retry path in handler{index % 4}",
                     "claim": "error from the repository is assigned and never checked, so a failed update returns success",
                     "evidence": "err is assigned on line 214 and the function returns nil to its caller without inspecting it",
                     "failure": "a transient database error silently marks the row as updated and the caller keeps stale state",
                     "status": status, "commit": "a1b2c3d" if status == "done" else None, "note": None, "counter": "none" if status == "done" else None,
                     "created_at": "2026-11-08T10:00:00Z", "updated_at": "2026-11-08T11:00:00Z"})
    return rows


class TokenEconomicsTests(unittest.TestCase):
    """TOON must be materially shorter than the JSON it replaces on the shapes the agent
    reads. Characters are the dependency-free proxy; the plan records tiktoken counts."""

    def test_backlog_rows_are_much_smaller_than_jsonl(self):
        rows = sample_backlog()
        jsonl = "\n".join(json.dumps(row) for row in rows)
        text = toon.dumps(toon.uniform(rows))
        self.assertLess(len(text), len(jsonl) * 0.75, f"TOON {len(text)} chars vs JSONL {len(jsonl)}")
        self.assertEqual(toon.loads(text), rows)

    def test_pretty_json_results_shrink_by_at_least_half(self):
        result = [{"id": f"insight:{index:012x}", "type": "insight", "label": f"Note {index} about caches", "kind": "pitfall",
                   "score": 0.81, "match": 0.5, "reinforced": 2, "source": "agent"} for index in range(15)]
        pretty = json.dumps(result, indent=2)
        text = toon.dumps(toon.uniform(result))
        self.assertLess(len(text), len(pretty) * 0.5, f"TOON {len(text)} chars vs pretty JSON {len(pretty)}")

    def test_small_objects_do_not_get_worse(self):
        value = {"run_id": "abc123", "pause_requested": False, "stop_requested": False, "requests": []}
        self.assertLessEqual(len(toon.dumps(value)), len(json.dumps(value)))


if __name__ == "__main__":
    unittest.main()
