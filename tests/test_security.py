"""SEC-5 (path traversal), SEC-6 (untrusted file parsing) and SEC-8 (atomic
writes) from docs/PLAN.md section 5a. The SEC-8 arm-claim race is block C."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from _pkg import store


class Sec5PathTraversal(unittest.TestCase):
    BAD_NAMES = [
        "../x", "..\\x", "C:\\x", "C:x", "\\server\\x", "\\\\server\\share\\x", "/etc/passwd",
        "con", "CON", "Nul", "prn", "aux", "com1", "COM9", "lpt1", "LPT0",
        "", " ", None, 7, "a" * 65, ".hidden", "-dash", "_under", "a.b", "a b",
        "a/b", "a\\b", "x\x00y", "caf\u00e9", "name\n",
    ]

    def test_bad_names_rejected(self):
        for name in self.BAD_NAMES:
            with self.subTest(name=name):
                with self.assertRaises(store.CassetteError):
                    store.validate_name(name)

    def test_good_names_accepted(self):
        for name in ["auth-port", "a", "A1_b-2", "x" * 64, "console", "nul1", "com10"]:
            with self.subTest(name=name):
                self.assertEqual(store.validate_name(name), name)

    def test_store_paths_reject_bad_names(self):
        with tempfile.TemporaryDirectory() as d:
            s = store.CassetteStore(d)
            for name in ["../x", "..\\x", "C:\\x", "\\server\\x", "con", ""]:
                with self.subTest(name=name):
                    with self.assertRaises(store.CassetteError):
                        s.path_for(name)
                    with self.assertRaises(store.CassetteError):
                        s.load(name)

    def test_safe_join_refuses_escape(self):
        with tempfile.TemporaryDirectory() as d:
            for parts in [("..", "x"), ("cassettes", "..", "..", "x"), ("C:\\x",), ("\\\\server\\share",), ("",)]:
                with self.subTest(parts=parts):
                    with self.assertRaises(store.CassetteError):
                        store.safe_join(d, *parts)
            inside = store.safe_join(d, "cassettes", "ok.json")
            self.assertTrue(inside.is_relative_to(Path(d).resolve()))


class Sec6UntrustedFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = store.CassetteStore(self.dir)
        (self.dir / "cassettes").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data: bytes, name="c1"):
        (self.dir / "cassettes" / f"{name}.json").write_bytes(data)

    def good(self):
        return {"format": 1, "name": "c1", "pins": ["8080"],
                "calls": [{"seq": 1, "tool": "read_file", "key": "k", "args": {}, "result": "r"}],
                "snapshot": {"c:/w/a.py": {"path": "C:/w/a.py", "text": "x", "size": 1}}}

    def assertClean(self, data: bytes):
        self.write(data)
        with self.assertRaises(store.CassetteError) as ctx:
            self.store.load("c1")
        self.assertTrue(str(ctx.exception))

    def test_good_file_loads(self):
        self.write(json.dumps(self.good()).encode())
        self.assertEqual(self.store.load("c1").pins, ["8080"])

    def test_bom_is_accepted(self):
        self.write(b"\xef\xbb\xbf" + json.dumps(self.good()).encode())
        self.assertEqual(self.store.load("c1").name, "c1")

    def test_truncated(self):
        self.assertClean(json.dumps(self.good()).encode()[:40])

    def test_empty(self):
        self.assertClean(b"")

    def test_not_utf8(self):
        self.assertClean(b'{"format": 1, "name": "c1", "request": "\xff\xfe"}')

    def test_deeply_nested(self):
        self.assertClean(b"[" * 200000 + b"]" * 200000)

    def test_huge(self):
        with mock.patch.object(store, "MAX_FILE_BYTES", 1024):
            self.assertClean(b'{"format": 1, "name": "c1", "request": "' + b"x" * 2000 + b'"}')

    def test_missing_file(self):
        with self.assertRaises(store.CassetteError):
            self.store.load("nothere")

    def test_wrong_types(self):
        def variant(**changes):
            d = self.good()
            for k, v in changes.items():
                d[k] = v
            return d

        bad_call = dict(self.good()["calls"][0])
        cases = {
            "array": [1, 2],
            "format str": variant(format="1"),
            "format bool": variant(format=True),
            "format 2": variant(format=2),
            "no format": {k: v for k, v in self.good().items() if k != "format"},
            "no name": {k: v for k, v in self.good().items() if k != "name"},
            "name int": variant(name=5),
            "bad name": variant(name="../x"),
            "request list": variant(request=["x"]),
            "pins str": variant(pins="8080"),
            "pin int": variant(pins=[8080]),
            "calls dict": variant(calls={}),
            "call not obj": variant(calls=["x"]),
            "call result int": variant(calls=[dict(bad_call, result=5)]),
            "call args list": variant(calls=[dict(bad_call, args=[])]),
            "call seq str": variant(calls=[dict(bad_call, seq="1")]),
            "call no tool": variant(calls=[{k: v for k, v in bad_call.items() if k != "tool"}]),
            "snapshot list": variant(snapshot=[]),
            "snapshot text int": variant(snapshot={"k": {"path": "p", "text": 1}}),
            "snapshot entry str": variant(snapshot={"k": "x"}),
            "unrecorded bad": variant(unrecorded=[{"seq": 1}]),
            "warnings int": variant(warnings=[1]),
        }
        for label, data in cases.items():
            with self.subTest(case=label):
                self.assertClean(json.dumps(data).encode())

    def test_read_json_file_cap_argument(self):
        p = self.dir / "arm.json"
        p.write_bytes(b'{"a": "' + b"x" * 100 + b'"}')
        with self.assertRaises(store.CassetteError):
            store.read_json_file(p, max_bytes=50)
        self.assertEqual(len(store.read_json_file(p, max_bytes=500)["a"]), 100)


class Sec8AtomicWrites(unittest.TestCase):
    def test_interrupted_write_keeps_old_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "c.json"
            store.atomic_write_json(target, {"v": "old"})
            with mock.patch.object(store.os, "replace", side_effect=OSError("power cut")):
                with self.assertRaises(OSError):
                    store.atomic_write_json(target, {"v": "new"})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"v": "old"})
            self.assertEqual(sorted(os.listdir(d)), ["c.json"])

    def test_interrupted_during_write_keeps_old_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "c.json"
            store.atomic_write_json(target, {"v": "old"})
            with mock.patch.object(store.os, "fsync", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    store.atomic_write_json(target, {"v": "new"})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"v": "old"})
            self.assertEqual(sorted(os.listdir(d)), ["c.json"])

    def test_concurrent_writers_leave_one_whole_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "c.json"
            errors = []

            def writer(i):
                try:
                    for _ in range(20):
                        store.atomic_write_json(target, {"writer": i, "pad": "x" * 5000})
                except Exception as exc:  # pragma: no cover - reported below
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn(data["writer"], range(8))
            self.assertEqual(sorted(os.listdir(d)), ["c.json"])

    def test_store_save_wraps_os_errors(self):
        with tempfile.TemporaryDirectory() as d:
            s = store.CassetteStore(d)
            c = store.Cassette(name="c1")
            with mock.patch.object(store.os, "replace", side_effect=PermissionError("locked")):
                with self.assertRaises(store.CassetteError):
                    s.save(c)


if __name__ == "__main__":
    unittest.main()
