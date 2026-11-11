import json
import os
import unittest
from unittest import mock

from _pkg import verdict

ANSWER = "The auth handler is in `server\\auth.py`; it binds port **8080**."


class PinTests(unittest.TestCase):
    def test_plain_pins(self):
        self.assertTrue(verdict.match_pin("server/auth.py", ANSWER))
        self.assertTrue(verdict.match_pin("SERVER\\AUTH.PY", ANSWER))
        self.assertTrue(verdict.match_pin("8080", ANSWER))
        self.assertFalse(verdict.match_pin("9090", ANSWER))
        self.assertFalse(verdict.match_pin("8080", None))

    def test_regex_pins(self):
        self.assertTrue(verdict.match_pin(r"re:port\W+80\d0", ANSWER))
        self.assertTrue(verdict.match_pin("re:server/auth\\.py", ANSWER))
        self.assertFalse(verdict.match_pin("re:\\b9090\\b", ANSWER))
        self.assertFalse(verdict.match_pin("re:(", ANSWER))  # invalid regex never matches

    def test_validate_pin(self):
        self.assertEqual(verdict.validate_pin("  8080 "), "8080")
        for bad in ["", "   ", "re:", "re:(", None, 5, "x" * 201]:
            with self.subTest(pin=bad):
                with self.assertRaises(verdict.VerdictError):
                    verdict.validate_pin(bad)


class VerdictTests(unittest.TestCase):
    PINS = ["server/auth.py", "8080"]
    REC = ["k1", "k2", "k3"]

    def test_pass(self):
        v = verdict.compute_verdict(self.PINS, ANSWER, [("hit", "k1"), ("hit", "k2")], self.REC)
        self.assertEqual(v.result, verdict.PASS)
        self.assertEqual(v.pins_matched, 2)

    def test_synthesized_alone_is_still_pass(self):
        v = verdict.compute_verdict(self.PINS, ANSWER, [("synthesized", "s1"), ("hit", "k1")], self.REC)
        self.assertEqual(v.result, verdict.PASS)
        self.assertEqual(v.synthesized, 1)

    def test_diverged(self):
        v = verdict.compute_verdict(self.PINS, ANSWER, [("hit", "k1"), ("miss", "terminal {}")], self.REC)
        self.assertEqual(v.result, verdict.DIVERGED)
        self.assertEqual(v.miss_details, ["terminal {}"])

    def test_fail_beats_diverged_and_notes_misses(self):
        v = verdict.compute_verdict(["9090"], ANSWER, [("miss", "a"), ("miss", "b")], self.REC)
        self.assertEqual(v.result, verdict.FAIL)
        self.assertEqual(v.notes, ["also 2 cassette misses"])

    def test_invalid_beats_everything(self):
        for pins, events in [(self.PINS, []), (["9090"], [("miss", "a")]), (self.PINS, [("miss", "a")])]:
            v = verdict.compute_verdict(pins, ANSWER, events, self.REC, invalid_reasons=["arm never claimed"])
            self.assertEqual(v.result, verdict.INVALID)
            self.assertEqual(v.invalid_reasons, ["arm never claimed"])

    def test_no_answer_is_invalid(self):
        for answer in (None, "", "   "):
            v = verdict.compute_verdict(self.PINS, answer)
            self.assertEqual(v.result, verdict.INVALID)

    def test_no_pins_is_an_error(self):
        with self.assertRaises(verdict.VerdictError):
            verdict.compute_verdict([], ANSWER)

    def test_to_dict_is_json(self):
        v = verdict.compute_verdict(self.PINS, ANSWER, [("hit", "k1")], self.REC)
        self.assertEqual(json.loads(json.dumps(v.to_dict()))["result"], "PASS")


class PathSummaryTests(unittest.TestCase):
    REC = ["a", "b", "c"]

    def test_variants(self):
        s = verdict.path_summary
        self.assertIn("same order", s(self.REC, [("hit", "a"), ("hit", "b"), ("hit", "c")]))
        self.assertIn("subset of recording", s(self.REC, [("hit", "a"), ("hit", "c")]))
        self.assertIn("order differs", s(self.REC, [("hit", "c"), ("hit", "a")]))
        self.assertIn("no recorded call reused", s(self.REC, [("synthesized", "x")]))
        self.assertIn("no tool calls", s(self.REC, []))
        self.assertTrue(s(self.REC, [("hit", "a"), ("miss", "z")]).startswith("2 calls: 1 hit, 0 synthesized, 1 miss"))


class RenderTests(unittest.TestCase):
    def v(self):
        return verdict.compute_verdict(["8080", "9090"], ANSWER, [("hit", "k"), ("miss", "m")], ["k"])

    def test_plain_text_is_ascii(self):
        out = verdict.render_verdict(self.v(), title="auth-port  replay with gpt-oss:20b", colour=False)
        out.encode("ascii")
        self.assertNotIn("\x1b", out)
        self.assertIn("FAIL", out)
        self.assertIn("[ok] 8080", out)
        self.assertIn("[--] 9090", out)
        self.assertIn("miss: m", out)

    def test_colour(self):
        self.assertIn("\x1b[1;31mFAIL", verdict.render_verdict(self.v(), colour=True))

    def test_no_color_env(self):
        class Tty:
            def isatty(self):
                return True

        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertFalse(verdict.use_colour(Tty()))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(verdict.use_colour(Tty()))

    def test_table(self):
        rows = [
            {"name": "auth-port", "model": "gpt-oss:20b", "result": "PASS", "pins": "2/2", "path": "3 calls"},
            {"name": "auth-port", "model": "llama3.1:8b", "result": "FAIL", "pins": "0/2", "path": "1 call"},
            {"name": "other", "result": None},
        ]
        out = verdict.render_table(rows, colour=False).splitlines()
        self.assertEqual(out[0].split(), ["CASSETTE", "MODEL", "RESULT", "PINS", "PATH"])
        self.assertEqual(out[1].index("PASS"), out[0].index("RESULT"))
        self.assertEqual(out[3].split(), ["other", "-", "-", "-", "-"])
        coloured = verdict.render_table(rows, colour=True).splitlines()
        self.assertIn("\x1b[1;32mPASS", coloured[1])


if __name__ == "__main__":
    unittest.main()
