"""Arm, claim and ledger files (arms.py), with their file checks and the
claim race."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from _pkg import arms, store

REQUEST = "Where is the auth handler and what port does it bind?"


class ArmsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def arm(self, **kw):
        kw.setdefault("now", 1000.0)
        return arms.create_arm(self.base, kw.pop("cassette", "auth-port"), kw.pop("request", REQUEST), **kw)


class CreateAndList(ArmsCase):
    def test_create_writes_a_checked_arm(self):
        arm = self.arm(model="gpt-oss:20b")
        on_disk = json.loads(arms.arm_path(self.base, arm["id"]).read_text(encoding="utf-8"))
        self.assertEqual(on_disk, arm)
        self.assertEqual(arm["mode"], "replay")
        self.assertEqual(arm["cassette"], "auth-port")
        self.assertEqual(arm["request"], REQUEST)
        self.assertEqual(arm["model"], "gpt-oss:20b")
        self.assertEqual(arms.waiting_arms(self.base, now=1001.0), [arm])

    def test_one_run_at_a_time(self):
        self.arm()
        with self.assertRaises(store.CassetteError) as ctx:
            self.arm(cassette="other", now=1030.0)
        self.assertIn("another cassette run", str(ctx.exception))
        self.assertEqual(len(arms.waiting_arms(self.base, now=1030.0)), 1)

    def test_a_claimed_arm_no_longer_blocks_the_next_run(self):
        first = self.arm()
        self.assertTrue(arms.claim(self.base, first["id"]))
        second = self.arm(now=1005.0)
        self.assertNotEqual(first["id"], second["id"])

    def test_expired_arm_is_not_waiting_and_is_cleaned_up(self):
        old = self.arm(now=1000.0)
        late = 1000.0 + arms.ARM_TTL_SECONDS + 1
        self.assertEqual(arms.waiting_arms(self.base, now=late), [])
        new = self.arm(now=late)
        self.assertFalse(arms.arm_path(self.base, old["id"]).exists())
        self.assertEqual(arms.waiting_arms(self.base, now=late), [new])

    def test_arm_from_the_future_is_treated_as_expired(self):
        self.arm(now=10_000.0)
        self.assertEqual(arms.waiting_arms(self.base, now=1000.0), [])

    def test_bad_inputs_are_refused(self):
        for cassette, request in [("../x", REQUEST), ("con", REQUEST), ("ok", ""), ("ok", "   \n"),
                                  ("ok", None), ("ok", "x" * (arms.MAX_REQUEST_CHARS + 1))]:
            with self.subTest(cassette=cassette, request=str(request)[:10]):
                with self.assertRaises(store.CassetteError):
                    arms.create_arm(self.base, cassette, request, now=1000.0)
        self.assertEqual(arms.waiting_arms(self.base, now=1000.0), [])

    def test_stale_creation_lock_is_taken_over(self):
        lock = self.base / arms.LOCK_FILE
        lock.write_text("", encoding="utf-8")
        os.utime(lock, (1, 1))
        self.assertEqual(self.arm()["cassette"], "auth-port")
        self.assertFalse(lock.exists())

    def test_fresh_creation_lock_refuses_cleanly(self):
        (self.base / arms.LOCK_FILE).write_text("", encoding="utf-8")
        with mock.patch.object(arms.time, "sleep"), \
                mock.patch.object(arms.time, "monotonic", side_effect=[0.0, 100.0, 200.0]):
            with self.assertRaises(store.CassetteError):
                self.arm()

    def test_normalize_request(self):
        self.assertEqual(arms.normalize_request("  a\r\nb \r\n"), "a\nb")
        self.assertEqual(arms.normalize_request(None), "")
        self.assertEqual(arms.normalize_request(["a"]), "")


class Sec6ArmFiles(ArmsCase):
    """Untrusted arm and ledger files give a clean error or are ignored. They never crash."""

    ID = "0123456789abcdef0123456789abcdef"

    def good(self):
        return {"format": 1, "id": self.ID, "mode": "replay", "cassette": "auth-port",
                "request": REQUEST, "created": 1000.0, "model": ""}

    def bad_arms(self):
        g = self.good()
        return {
            "truncated": json.dumps(g).encode()[:30],
            "empty": b"",
            "not utf-8": b'{"format": 1, "request": "\xff\xfe"}',
            "array": b"[1, 2]",
            "nested": b"[" * 100000 + b"]" * 100000,
            "format str": json.dumps(dict(g, format="1")).encode(),
            "format bool": json.dumps(dict(g, format=True)).encode(),
            "other id": json.dumps(dict(g, id="f" * 32)).encode(),
            "bad id": json.dumps(dict(g, id="../x")).encode(),
            "mode record": json.dumps(dict(g, mode="record")).encode(),
            "bad cassette": json.dumps(dict(g, cassette="..\\x")).encode(),
            "cassette int": json.dumps(dict(g, cassette=5)).encode(),
            "request list": json.dumps(dict(g, request=["x"])).encode(),
            "request blank": json.dumps(dict(g, request="  ")).encode(),
            "created str": json.dumps(dict(g, created="now")).encode(),
            "created bool": json.dumps(dict(g, created=True)).encode(),
            "created nan": json.dumps(dict(g, created=float("nan"))).encode(),
            "model int": json.dumps(dict(g, model=3)).encode(),
            "huge": b'{"request": "' + b"x" * (arms.ARM_MAX_BYTES + 10) + b'"}',
        }

    def test_malformed_arms_are_not_arms(self):
        (self.base / arms.ARMED_DIR).mkdir()
        path = self.base / arms.ARMED_DIR / f"{self.ID}.json"
        for label, raw in self.bad_arms().items():
            with self.subTest(case=label):
                path.write_bytes(raw)
                self.assertEqual(arms.waiting_arms(self.base, now=1000.0), [])
        path.write_bytes(json.dumps(self.good()).encode())
        self.assertEqual(len(arms.waiting_arms(self.base, now=1000.0)), 1)

    def test_oddly_named_files_are_ignored(self):
        armed = self.base / arms.ARMED_DIR
        armed.mkdir()
        for name in ["notes.txt", "ABCDEF.json", "x.json", f".{self.ID}.json.tmp", f"{self.ID}.JSON.bak"]:
            (armed / name).write_bytes(json.dumps(self.good()).encode())
        self.assertEqual(arms.waiting_arms(self.base, now=1000.0), [])

    def test_missing_folder_is_no_arms(self):
        self.assertEqual(arms.waiting_arms(self.base / "nope", now=1000.0), [])

    def test_malformed_ledgers_raise_clean_errors(self):
        (self.base / arms.CLAIMED_DIR).mkdir()
        path = self.base / arms.CLAIMED_DIR / f"{self.ID}.json"
        g = dict(self.good(), state="done", session_id="s", turn_id="t", final_answer="a",
                 events=[["hit", "k"]], invalid_reasons=[])
        cases = dict(self.bad_arms())
        cases.pop("huge")
        cases.update({
            "state odd": json.dumps(dict(g, state="finished")).encode(),
            "session int": json.dumps(dict(g, session_id=5)).encode(),
            "answer list": json.dumps(dict(g, final_answer=["a"])).encode(),
            "events dict": json.dumps(dict(g, events={})).encode(),
            "event kind": json.dumps(dict(g, events=[["won", "k"]])).encode(),
            "event shape": json.dumps(dict(g, events=[["hit"]])).encode(),
            "event key int": json.dumps(dict(g, events=[["hit", 5]])).encode(),
            "reasons str": json.dumps(dict(g, invalid_reasons="x")).encode(),
            "reason int": json.dumps(dict(g, invalid_reasons=[1])).encode(),
            "refused str": json.dumps(dict(g, refused_calls="2")).encode(),
            "finished str": json.dumps(dict(g, finished_at="noon")).encode(),
        })
        for label, raw in cases.items():
            with self.subTest(case=label):
                path.write_bytes(raw)
                with self.assertRaises(store.CassetteError) as ctx:
                    arms.read_ledger(self.base, self.ID)
                self.assertTrue(str(ctx.exception))
        path.write_bytes(json.dumps(g).encode())
        ledger = arms.read_ledger(self.base, self.ID)
        self.assertEqual(ledger["events"], [("hit", "k")])
        self.assertEqual(ledger["state"], "done")

    def test_ledger_of_an_unclaimed_arm_is_an_error(self):
        arm = self.arm()
        with self.assertRaises(store.CassetteError):
            arms.read_ledger(self.base, arm["id"])

    def test_paths_reject_bad_ids(self):
        for bad in ["../x", "..\\x", "C:\\x", "", None, "A" * 32, "g" * 32, "0" * 31, "0" * 33, "0" * 32 + "\n"]:
            with self.subTest(arm_id=bad):
                with self.assertRaises(store.CassetteError):
                    arms.arm_path(self.base, bad)
                with self.assertRaises(store.CassetteError):
                    arms.claimed_path(self.base, bad)
                self.assertFalse(arms.claim(self.base, bad))


class Sec8Claim(ArmsCase):
    def test_claim_moves_the_arm(self):
        arm = self.arm()
        self.assertTrue(arms.claim(self.base, arm["id"]))
        self.assertFalse(arms.arm_path(self.base, arm["id"]).exists())
        self.assertTrue(arms.claimed_path(self.base, arm["id"]).is_file())
        self.assertEqual(arms.waiting_arms(self.base, now=1000.0), [])
        self.assertFalse(arms.claim(self.base, arm["id"]))  # already taken
        self.assertFalse(arms.claim(self.base, "f" * 32))   # never existed

    def test_a_discarded_arm_cannot_be_claimed(self):
        arm = self.arm()
        arms.discard(self.base, arm["id"])
        self.assertFalse(arms.claim(self.base, arm["id"]))
        self.assertFalse(arms.claimed_path(self.base, arm["id"]).exists())

    def test_claim_is_void_if_the_arm_vanishes_midway(self):
        arm = self.arm()
        real_unlink = os.unlink

        def vanish_first(path, *a, **kw):  # the armer discards the run just before our unlink
            if Path(path).parent.name == arms.ARMED_DIR and os.path.exists(path):
                real_unlink(path)
            return real_unlink(path, *a, **kw)

        with mock.patch.object(arms.os, "unlink", side_effect=vanish_first):
            self.assertFalse(arms.claim(self.base, arm["id"]))
        self.assertFalse(arms.claimed_path(self.base, arm["id"]).exists())

    def test_leftover_arm_file_of_a_claimed_arm_is_not_waiting(self):
        arm = self.arm()
        with mock.patch.object(arms.os, "unlink", side_effect=PermissionError("busy")):
            self.assertTrue(arms.claim(self.base, arm["id"]))
        self.assertTrue(arms.arm_path(self.base, arm["id"]).exists())  # could not be removed
        self.assertEqual(arms.waiting_arms(self.base, now=1000.0), [])
        self.assertFalse(arms.claim(self.base, arm["id"]))
        self.arm(now=1001.0)  # and it does not block the next run
        self.assertFalse(arms.arm_path(self.base, arm["id"]).exists())

    def test_many_threads_claim_one_arm_and_exactly_one_wins(self):
        for round_no in range(150):
            arm = self.arm(now=1000.0 + round_no)
            barrier = threading.Barrier(8)
            wins = []

            def racer():
                barrier.wait()
                wins.append(arms.claim(self.base, arm["id"]))

            threads = [threading.Thread(target=racer) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sorted(wins), [False] * 7 + [True], f"round {round_no}")

    def test_ledger_is_written_only_while_the_claim_exists(self):
        arm = self.arm()
        data = dict(arm, state="done", final_answer="8080")
        self.assertFalse(arms.write_ledger(self.base, arm["id"], data))  # not claimed yet
        self.assertFalse(arms.claimed_path(self.base, arm["id"]).exists())
        arms.claim(self.base, arm["id"])
        self.assertTrue(arms.write_ledger(self.base, arm["id"], data))
        self.assertEqual(arms.read_ledger(self.base, arm["id"])["final_answer"], "8080")
        arms.discard(self.base, arm["id"])
        self.assertFalse(arms.write_ledger(self.base, arm["id"], data))  # abandoned: stays gone
        self.assertFalse(arms.claimed_path(self.base, arm["id"]).exists())

    def test_discard_removes_both_files_and_tolerates_nothing(self):
        arm = self.arm()
        arms.discard(self.base, arm["id"])
        self.assertFalse(arms.arm_path(self.base, arm["id"]).exists())
        arms.discard(self.base, arm["id"])
        arms.discard(self.base, "not-an-id")


if __name__ == "__main__":
    unittest.main()
