import json
import os
import tempfile
import unittest
from pathlib import Path

from _pkg import snapshot, store

AUTH = '''"""Authentication service: issues and checks session tokens."""
import hashlib

AUTH_PORT = 8080


def serve():
    HTTPServer(("0.0.0.0", AUTH_PORT), Handler).serve_forever()
'''
CATALOG = "from config.settings import CATALOG_PORT\n\ndef serve():\n    listen(CATALOG_PORT)\n"
HEALTH = "HEALTH_PORT = 9100\r\nprint('ok')\r\n"


def make_workspace(root: Path) -> None:
    (root / "server").mkdir()
    (root / "client").mkdir()
    # Bytes, not write_text: on Windows text mode would turn "\n" into "\r\n".
    (root / "server" / "auth.py").write_bytes(AUTH.encode())
    (root / "server" / "catalog.py").write_bytes(CATALOG.encode())
    (root / "server" / "health.py").write_bytes(HEALTH.encode())  # CRLF on purpose
    (root / "client" / "login.js").write_bytes(b"fetch('http://localhost:8080/login')\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        make_workspace(self.root)
        self.ws = str(self.root)
        self.cas = store.Cassette(name="t", cwd=self.ws, workspace=self.ws)

    def tearDown(self):
        self.tmp.cleanup()

    def capture_all(self):
        notes = snapshot.capture(self.cas, [
            "./server/auth.py", "server\\catalog.py", str(self.root / "server" / "health.py"),
            "client/login.js",
        ])
        self.assertEqual(notes, [])


class NamedPathsTests(unittest.TestCase):
    def test_dense_matches_text(self):
        # Shape copied from a real search_files result in the S1 probe log.
        result = json.dumps({
            "total_count": 7,
            "matches_format": "path-grouped: ...",
            "matches_text": "./server\\health.py\n  2: HEALTH_PORT = 9100\n./server\\auth.py\n  4: AUTH_PORT = 8080",
        })
        self.assertEqual(snapshot.named_paths("search_files", {}, result),
                         ["./server\\health.py", "./server\\auth.py"])

    def test_matches_files_counts(self):
        r1 = json.dumps({"total_count": 1, "matches": [{"path": "a.py", "line": 1, "content": "x"}]})
        r2 = json.dumps({"total_count": 1, "files": ["/c/repo/b.py"]})
        r3 = json.dumps({"total_count": 3, "counts": {"c.py": 3}})
        self.assertEqual(snapshot.named_paths("search_files", {}, r1), ["a.py"])
        self.assertEqual(snapshot.named_paths("search_files", {}, r2), ["/c/repo/b.py"])
        self.assertEqual(snapshot.named_paths("search_files", {}, r3), ["c.py"])

    def test_read_file_and_errors(self):
        ok = json.dumps({"content": "1|x", "total_lines": 1})
        self.assertEqual(snapshot.named_paths("read_file", {"path": "a.py"}, ok), ["a.py"])
        err = json.dumps({"error": "File not found: a.py"})
        self.assertEqual(snapshot.named_paths("read_file", {"path": "a.py"}, err), [])
        self.assertEqual(snapshot.named_paths("search_files", {}, "not json"), [])
        self.assertEqual(snapshot.named_paths("web_search", {}, ok), [])
        self.assertEqual(snapshot.named_paths("read_file", None, None), [])


class CaptureTests(Base):
    def test_captures_workspace_files_once(self):
        self.capture_all()
        self.assertEqual(len(self.cas.snapshot), 4)
        key = store.normalize_path(self.root / "server" / "auth.py")
        self.assertEqual(self.cas.snapshot[key]["text"], AUTH)
        self.assertEqual(snapshot.capture(self.cas, ["server/auth.py"]), [])
        self.assertEqual(len(self.cas.snapshot), 4)

    def test_skips(self):
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("[core]\n")
        (self.root / ".env").write_text("TOKEN=abc\n")
        (self.root / "id_rsa").write_text("PRIVATE\n")
        (self.root / "server" / "tls.pem").write_text("PEM\n")
        (self.root / "blob.dat").write_bytes(b"ab\x00cd")
        (self.root / "latin1.txt").write_bytes(b"caf\xe9\n")
        (self.root / "big.txt").write_text("x" * 2000)
        outside = Path(self.tmp.name).resolve().parent / "outside-ws.txt"
        notes = dict(snapshot.capture(
            self.cas,
            [".git/config", ".env", "id_rsa", "server/tls.pem", "blob.dat", "latin1.txt", "big.txt",
             str(outside), "..\\elsewhere.py", "missing.py", "server", self.ws],
            max_file_bytes=1000,
        ))
        self.assertEqual(self.cas.snapshot, {})
        self.assertEqual(notes[".git/config"], "hidden path")
        self.assertEqual(notes[".env"], "hidden path")
        self.assertEqual(notes["id_rsa"], "secret-looking file name")
        self.assertEqual(notes["server/tls.pem"], "secret-looking file name")
        self.assertEqual(notes["blob.dat"], "binary file")
        self.assertEqual(notes["latin1.txt"], "not UTF-8 text")
        self.assertEqual(notes["big.txt"], "file too large")
        self.assertEqual(notes[str(outside)], "outside the workspace")
        self.assertEqual(notes["..\\elsewhere.py"], "outside the workspace")
        self.assertTrue(notes["missing.py"].startswith("unreadable"))
        self.assertEqual(notes["server"], "not a regular file")
        self.assertEqual(notes[self.ws], "outside the workspace")

    def test_login_store_names_are_skipped(self):
        # Catalog rule 11: a plugin never reads auth.json or another login store.
        names = ["auth.json", "Auth.JSON", "token.json", "tokens.json", "server/auth.json"]
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"access_token": "x"}')
        notes = dict(snapshot.capture(self.cas, names))
        self.assertEqual(self.cas.snapshot, {})
        for name in names:
            self.assertEqual(notes[name], "secret-looking file name", name)

    def test_total_cap(self):
        notes = snapshot.capture(self.cas, ["server/auth.py", "server/catalog.py"],
                                 max_total_bytes=len(AUTH) + 5)
        self.assertEqual(len(self.cas.snapshot), 1)
        self.assertEqual(notes, [("server/catalog.py", "snapshot size cap reached")])

    def test_redact_applied_and_failure_skips(self):
        snapshot.capture(self.cas, ["server/auth.py"], redact=lambda text, path: text.replace("8080", "***"))
        (entry,) = self.cas.snapshot.values()
        self.assertNotIn("8080", entry["text"])

        def boom(text, path):
            raise RuntimeError("no")

        notes = snapshot.capture(self.cas, ["server/catalog.py"], redact=boom)
        self.assertEqual(notes, [("server/catalog.py", "redaction failed")])

    def test_symlink_escape(self):
        outside = Path(tempfile.mkdtemp())
        try:
            (outside / "secret.txt").write_text("s\n")
            link = self.root / "link.txt"
            try:
                os.symlink(outside / "secret.txt", link)
            except (OSError, NotImplementedError):
                self.skipTest("cannot create symlinks here (Windows needs Developer Mode)")
            notes = snapshot.capture(self.cas, ["link.txt"])
            self.assertEqual(notes, [("link.txt", "link points outside the workspace")])
        finally:
            (outside / "secret.txt").unlink()
            outside.rmdir()

    def _link_dir(self, target: Path, link: Path) -> None:
        """Make ``link`` a directory link to ``target``: a symlink, or on Windows
        a junction, which needs no Developer Mode."""
        try:
            os.symlink(target, link, target_is_directory=True)
            return
        except (OSError, NotImplementedError):
            pass
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        except Exception:
            self.skipTest("cannot create a directory link here")

    @staticmethod
    def _unlink_dir(link: Path) -> None:
        for remove in (os.unlink, os.rmdir):  # a symlink, then a junction
            try:
                remove(link)
                return
            except OSError:
                pass

    def test_directory_link_escape(self):
        outside = Path(tempfile.mkdtemp()).resolve()
        link = self.root / "linked"
        try:
            (outside / "secret.txt").write_text("s\n")
            self._link_dir(outside, link)
            notes = snapshot.capture(self.cas, ["linked/secret.txt"])
            self.assertEqual(notes, [("linked/secret.txt", "link points outside the workspace")])
            self.assertEqual(self.cas.snapshot, {})
        finally:
            self._unlink_dir(link)
            (outside / "secret.txt").unlink()
            outside.rmdir()

    def test_workspace_reached_through_a_link(self):
        # The working directory can itself sit behind a link (a Windows junction,
        # macOS's /tmp). Its files are still inside the workspace; a link inside
        # it that leads out is still refused.
        holder = Path(tempfile.mkdtemp()).resolve()
        outside = Path(tempfile.mkdtemp()).resolve()
        ws = holder / "ws-link"
        escape = self.root / "linked"
        try:
            (outside / "secret.txt").write_text("s\n")
            self._link_dir(self.root, ws)
            self._link_dir(outside, escape)
            cas = store.Cassette(name="t", cwd=str(ws), workspace=str(ws))
            notes = snapshot.capture(cas, ["server/auth.py", str(ws / "client" / "login.js"),
                                           "linked/secret.txt"])
            self.assertEqual(notes, [("linked/secret.txt", "link points outside the workspace")])
            self.assertEqual(len(cas.snapshot), 2)
            key = store.normalize_path(ws / "server" / "auth.py")
            self.assertEqual(cas.snapshot[key]["text"], AUTH)
            read = json.loads(snapshot.synthesize(cas, "read_file", {"path": "server/auth.py"}))
            self.assertIn("AUTH_PORT = 8080", read["content"])
        finally:
            self._unlink_dir(escape)
            self._unlink_dir(ws)
            holder.rmdir()
            (outside / "secret.txt").unlink()
            outside.rmdir()

    def test_no_workspace(self):
        cas = store.Cassette(name="t", cwd=self.ws)
        self.assertEqual(snapshot.capture(cas, ["server/auth.py"]), [("*", "cassette has no workspace")])

    def test_snapshot_survives_round_trip(self):
        self.capture_all()
        again = store.Cassette.from_dict(json.loads(json.dumps(self.cas.to_dict())))
        self.assertEqual(again.snapshot, self.cas.snapshot)


class SynthReadTests(Base):
    def test_full_read_format(self):
        self.capture_all()
        out = json.loads(snapshot.synthesize(self.cas, "read_file", {"path": "server/auth.py"}))
        lines = AUTH.rstrip("\n").split("\n")
        self.assertEqual(out["total_lines"], len(lines))
        self.assertEqual(out["content"].split("\n")[3], "4|AUTH_PORT = 8080")
        self.assertFalse(out["truncated"])
        self.assertEqual(out["file_size"], len(AUTH.encode()))

    def test_path_spellings_hit_the_same_entry(self):
        self.capture_all()
        spellings = ["./server/auth.py", str(self.root / "server" / "auth.py")]
        if os.name == "nt":
            # Windows spellings: the Git Bash form of the path, and another
            # case with backslashes. Elsewhere those name a different file.
            msys = "/" + str(self.root)[0].lower() + str(self.root)[2:].replace("\\", "/") + "/server/auth.py"
            spellings += [msys, "SERVER\\AUTH.PY"]
        for p in spellings:
            with self.subTest(path=p):
                self.assertIsNotNone(snapshot.synthesize(self.cas, "read_file", {"path": p}))

    def test_range_and_hint(self):
        self.capture_all()
        out = json.loads(snapshot.synthesize(self.cas, "read_file", {"path": "server/auth.py", "offset": 2, "limit": 2}))
        self.assertEqual(out["content"], "2|import hashlib\n3|")
        self.assertTrue(out["truncated"])
        self.assertIn("offset=4", out["hint"])

    def test_crlf_lines(self):
        self.capture_all()
        out = json.loads(snapshot.synthesize(self.cas, "read_file", {"path": "server/health.py"}))
        self.assertEqual(out["content"], "1|HEALTH_PORT = 9100\n2|print('ok')")

    def test_unknown_file_is_none(self):
        self.capture_all()
        self.assertIsNone(snapshot.synthesize(self.cas, "read_file", {"path": "server/other.py"}))
        self.assertIsNone(snapshot.synthesize(self.cas, "read_file", {}))
        self.assertIsNone(snapshot.synthesize(self.cas, "web_search", {"query": "x"}))


class SynthSearchTests(Base):
    def search(self, **args):
        out = snapshot.synthesize(self.cas, "search_files", args)
        return None if out is None else json.loads(out)

    def test_content_search_in_a_file_only_read(self):
        # gpt-oss greps for AUTH_PORT in a file Arete's run only read.
        snapshot.capture(self.cas, ["server/auth.py"])
        out = self.search(pattern="AUTH_PORT", path="")
        self.assertEqual(out["total_count"], 2)
        self.assertEqual(out["matches"][0], {"path": "./server/auth.py", "line": 4, "content": "AUTH_PORT = 8080"})

    def test_absolute_root_gives_absolute_paths(self):
        self.capture_all()
        out = self.search(pattern="8080", path=str(self.root))
        paths = [m["path"] for m in out["matches"]]
        self.assertTrue(all(p.startswith(str(self.root).replace("\\", "/")) for p in paths), paths)
        self.assertEqual(len(paths), 2)

    def test_query_key_means_empty_pattern(self):
        self.capture_all()
        out = self.search(query="AUTH_PORT", path="", max_results=5, recursive=True)
        self.assertEqual(out["total_count"], 8 + 4 + 2 + 1)  # every line of every snapshot file
        self.assertIn("matches_text", out)  # densified at 5 or more matches

    def test_file_glob_with_braces(self):
        self.capture_all()
        out = self.search(pattern="8080", file_glob="*.{js,ts}")
        self.assertEqual([m["path"] for m in out["matches"]], ["./client/login.js"])

    def test_subdir_root(self):
        self.capture_all()
        out = self.search(pattern="PORT", path="./server", output_mode="files_only")
        self.assertEqual(out["files"], ["./server/auth.py", "./server/catalog.py", "./server/health.py"])

    def test_count_mode(self):
        self.capture_all()
        out = self.search(pattern="PORT", output_mode="count")
        self.assertEqual(out["counts"]["./server/auth.py"], 2)
        self.assertEqual(out["total_count"], 5)

    def test_context(self):
        self.capture_all()
        out = self.search(pattern="HEALTH_PORT", context=1)
        self.assertEqual([m["line"] for m in out["matches"]], [1, 2])

    def test_limit_offset_truncation(self):
        self.capture_all()
        out = self.search(pattern="", limit=3, offset=1)
        self.assertEqual(len(out["matches"]), 3)
        self.assertTrue(out["truncated"])
        self.assertIn("offset=4", out["_hint"])

    def test_files_target(self):
        self.capture_all()
        self.assertEqual(self.search(pattern="*.js", target="files")["files"], ["./client/login.js"])
        self.assertEqual(self.search(pattern="auth.py", target="find")["files"], ["./server/auth.py"])
        # Hermes wraps a bare name as "*name", so "auth" alone matches nothing.
        self.assertEqual(self.search(pattern="auth", target="files"), {"total_count": 0})

    def test_bad_regex_is_a_tool_error(self):
        self.capture_all()
        out = self.search(pattern="listen(")
        self.assertIn("Search failed", out["error"])

    def test_outside_workspace_or_unknown_dir_is_none(self):
        self.capture_all()
        self.assertIsNone(self.search(pattern="x", path=".."))
        self.assertIsNone(self.search(pattern="x", path="C:\\Windows"))
        (self.root / "docs").mkdir()
        self.assertIsNone(self.search(pattern="x", path="docs"))

    def test_single_file_root(self):
        self.capture_all()
        out = self.search(pattern="8080", path="server/auth.py")
        self.assertEqual(out["matches"][0]["path"], "server/auth.py")

    def test_replay_index_uses_snapshot(self):
        self.capture_all()
        idx = store.ReplayIndex(self.cas)
        kind, result = idx.answer("search_files", {"pattern": "AUTH_PORT"})
        self.assertEqual(kind, "synthesized")
        kind, _ = idx.answer("search_files", {"pattern": "x", "path": "C:\\Windows"})
        self.assertEqual(kind, "miss")


if __name__ == "__main__":
    unittest.main()
