import json
import tempfile
import unittest
from pathlib import Path

from _pkg import store

CWD = "C:\\Repo"


class NormalisePathTests(unittest.TestCase):
    def test_windows_spellings_give_one_key(self):
        spellings = [
            "C:\\Repo\\x.py",
            "c:/repo/x.py",
            "./x.py",
            "x.py",
            ".\\x.py",
            "/c/repo/x.py",  # Git Bash / MSYS form
            "..\\Repo\\x.py",
            "C:/Repo/sub/../x.py",
            "  C:\\REPO\\X.PY  ",
        ]
        keys = {store.normalize_path(s, CWD) for s in spellings}
        self.assertEqual(keys, {"c:/repo/x.py"})

    def test_drive_root_and_dotdot_above_root(self):
        self.assertEqual(store.normalize_path("C:\\", None), "c:/")
        self.assertEqual(store.normalize_path("C:\\..\\x", None), "c:/x")
        self.assertEqual(store.normalize_path("C:", None), "c:/")

    def test_trailing_slash_dropped(self):
        self.assertEqual(store.normalize_path("C:\\Repo\\server\\", None), "c:/repo/server")

    def test_unc(self):
        self.assertEqual(store.normalize_path("\\\\Server\\Share\\x.py", None), "//server/share/x.py")

    def test_blank_and_dot_mean_cwd(self):
        self.assertEqual(store.normalize_path("", CWD), "c:/repo")
        self.assertEqual(store.normalize_path(".", CWD), "c:/repo")

    def test_posix_context_keeps_case_and_msys_shape(self):
        cwd = "/home/u/Proj"
        self.assertEqual(store.normalize_path("./A.py", cwd), "/home/u/Proj/A.py")
        self.assertEqual(store.normalize_path("/c/x", cwd), "/c/x")

    def test_display_path_keeps_case(self):
        self.assertEqual(store.display_path(".\\Server\\Auth.py", CWD), "C:/Repo/Server/Auth.py")

    def test_is_within(self):
        self.assertTrue(store.is_within("c:/repo/x.py", "c:/repo"))
        self.assertTrue(store.is_within("c:/repo", "c:/repo"))
        self.assertFalse(store.is_within("c:/repository/x.py", "c:/repo"))
        self.assertTrue(store.is_within("c:/x", "c:/"))


class KeyTests(unittest.TestCase):
    def test_read_file_defaults_and_unknown_keys_dropped(self):
        a = store.make_key("read_file", {"path": "x.py"}, CWD)
        b = store.make_key("read_file", {"path": "C:\\Repo\\x.py", "offset": 1, "limit": 2000, "junk": 3}, CWD)
        c = store.make_key("read_file", {"path": "./x.py", "offset": "1"}, CWD)
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertEqual(a, 'read_file {"path":"c:/repo/x.py"}')

    def test_read_file_range_is_part_of_key(self):
        a = store.make_key("read_file", {"path": "x.py", "offset": 5, "limit": 10}, CWD)
        self.assertEqual(a, 'read_file {"limit":10,"offset":5,"path":"c:/repo/x.py"}')

    def test_search_files_blank_path_and_defaults(self):
        a = store.make_key("search_files", {"pattern": "auth"}, CWD)
        b = store.make_key("search_files", {"pattern": "auth", "path": "", "target": "content",
                                            "limit": 50, "max_results": 9, "recursive": True}, CWD)
        c = store.make_key("search_files", {"pattern": "auth", "path": "C:\\Repo"}, CWD)
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_query_is_not_an_alias_of_pattern(self):
        # Hermes reads args.get("pattern", ""), so "query" is ignored.
        key = store.make_key("search_files", {"query": "auth"}, CWD)
        self.assertEqual(key, 'search_files {"pattern":""}')

    def test_search_target_aliases(self):
        self.assertEqual(
            store.make_key("search_files", {"pattern": "a", "target": "grep"}, CWD),
            store.make_key("search_files", {"pattern": "a"}, CWD),
        )
        self.assertIn('"target":"files"', store.make_key("search_files", {"pattern": "a", "target": "find"}, CWD))

    def test_generic_tool_sorted_and_none_dropped(self):
        a = store.make_key("web_search", {"query": "x", "n": None, "b": 1}, CWD)
        b = store.make_key("web_search", {"b": 1, "query": "x"}, CWD)
        self.assertEqual(a, b)

    def test_terminal_key_is_command_plus_cwd(self):
        a = store.make_key("terminal", {"command": "git log -1"}, CWD)
        b = store.make_key("terminal", {"command": "git log -1", "workdir": "C:\\Repo", "timeout": 5}, CWD)
        self.assertEqual(a, b)
        self.assertNotEqual(a, store.make_key("terminal", {"command": "git log  -1"}, CWD))

    def test_non_dict_args(self):
        self.assertEqual(store.make_key("web_search", None, CWD), "web_search {}")


class CassetteTests(unittest.TestCase):
    def make(self):
        c = store.Cassette(name="auth-port", request="Where is the auth handler?", cwd=CWD, workspace=CWD)
        c.add_call("read_file", {"path": "x.py"}, '{"content": "1|a"}')
        c.add_unrecorded("write_file")
        c.add_pin("server/auth.py")
        c.add_pin("8080")
        c.add_pin("8080")
        c.add_warning("w")
        return c

    def test_round_trip(self):
        c = self.make()
        d = c.to_dict()
        self.assertEqual(d["format"], 1)
        self.assertEqual(d["pins"], ["server/auth.py", "8080"])
        self.assertEqual([u["seq"] for u in d["unrecorded"]], [2])
        again = store.Cassette.from_dict(json.loads(json.dumps(d)))
        self.assertEqual(again.to_dict(), d)

    def test_bad_pin_is_a_cassette_error(self):
        c = self.make()
        with self.assertRaises(store.CassetteError):
            c.add_pin("   ")
        with self.assertRaises(store.CassetteError):
            c.add_pin("re:(")

    def test_store_save_load_list(self):
        with tempfile.TemporaryDirectory() as d:
            s = store.CassetteStore(d)
            self.assertEqual(s.names(), [])
            c = self.make()
            path = s.save(c)
            self.assertTrue(path.is_file())
            self.assertEqual(path.parent, Path(d).resolve() / "cassettes")
            (Path(d) / "cassettes" / "not a name.json").write_text("{}")
            self.assertEqual(s.names(), ["auth-port"])
            self.assertEqual(s.load("auth-port").to_dict(), c.to_dict())

    def test_load_refuses_renamed_file(self):
        with tempfile.TemporaryDirectory() as d:
            s = store.CassetteStore(d)
            s.save(self.make())
            src = Path(d) / "cassettes" / "auth-port.json"
            src.rename(src.with_name("other.json"))
            with self.assertRaises(store.CassetteError):
                s.load("other")


class ReplayIndexTests(unittest.TestCase):
    def cassette(self):
        c = store.Cassette(name="r", cwd=CWD, workspace=CWD)
        c.add_call("read_file", {"path": "a.py"}, "first")
        c.add_call("read_file", {"path": "C:\\Repo\\a.py"}, "second")
        c.add_call("web_search", {"query": "q"}, "web")
        return c

    def test_repeated_key_in_order_then_last_repeats(self):
        idx = store.ReplayIndex(self.cassette(), synthesize=lambda *a: None)
        got = [idx.answer("read_file", {"path": "./a.py"}) for _ in range(4)]
        self.assertEqual(got, [("hit", "first"), ("hit", "second"), ("hit", "second"), ("hit", "second")])

    def test_cursor_belongs_to_the_index_not_the_cassette(self):
        c = self.cassette()
        one = store.ReplayIndex(c, synthesize=lambda *a: None)
        two = store.ReplayIndex(c, synthesize=lambda *a: None)
        self.assertEqual(one.answer("read_file", {"path": "a.py"})[1], "first")
        self.assertEqual(two.answer("read_file", {"path": "a.py"})[1], "first")
        self.assertEqual(one.answer("read_file", {"path": "a.py"})[1], "second")

    def test_miss_is_an_error_string(self):
        idx = store.ReplayIndex(self.cassette(), synthesize=lambda *a: None)
        kind, result = idx.answer("write_file", {"path": "a.py", "content": "x"})
        self.assertEqual(kind, "miss")
        self.assertTrue(json.loads(result)["error"].startswith("cassette miss: write_file"))

    def test_synthesizer_is_used_before_miss(self):
        idx = store.ReplayIndex(self.cassette(), synthesize=lambda c, t, a: "synth!" if t == "read_file" else None)
        self.assertEqual(idx.answer("read_file", {"path": "b.py"}), ("synthesized", "synth!"))

    def test_never_raises(self):
        def boom(*a):
            raise RuntimeError("x")

        idx = store.ReplayIndex(self.cassette(), synthesize=boom)
        for tool, args in [("read_file", {"path": "zzz"}), (123, None), ("tool_call", {"calls": object()})]:
            kind, result = idx.answer(tool, args)
            self.assertEqual(kind, "miss")
            self.assertIn("cassette miss", json.loads(result)["error"])

    def test_miss_message_is_capped(self):
        result = store.miss_result("web_search", {"q": "x" * 5000})
        self.assertLess(len(result), 500)


if __name__ == "__main__":
    unittest.main()
