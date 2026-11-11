"""The plugin's hooks and middleware (__init__.py), driven the way Hermes
drives them, with a fake ``next_call`` standing in for the real tool.

Covers record, replay, the miss path, the turn-bound binding, replay failing
closed (``next_call`` never runs) and the claim race at the hook level.
"""

import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from _pkg import ROOT, arms, plugin, store, verdict

REQUEST = "Where is the auth handler and what port does it bind?"
ANSWER = "The auth handler is in server/auth.py and it binds port 8080."

# The plugin logs the failures these tests provoke on purpose; keep them off the console.
logging.getLogger(plugin.__name__).addHandler(logging.NullHandler())


def live(result):
    """A fake real tool: returns ``result`` and counts its calls."""
    return mock.Mock(return_value=result)


class LiveToolRan(BaseException):
    """Not an Exception, so the plugin's own ``except Exception`` cannot hide it."""


def forbidden():
    """A ``next_call`` that must never run."""
    return mock.Mock(side_effect=LiveToolRan("a real tool ran during replay"))


class PluginCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.data = root / "data"
        self.data.mkdir()
        self.ws = root / "ws"
        (self.ws / "server").mkdir(parents=True)
        self.auth = self.ws / "server" / "auth.py"
        self.auth.write_text("HOST = '0.0.0.0'\nAUTH_PORT = 8080\n# token SECRET\n", encoding="utf-8")
        (self.ws / "server" / "health.py").write_text("PORT = 9100\n", encoding="utf-8")
        self.now = 1_000_000.0
        self.session_key = ""
        self.rt = plugin.Runtime(
            data_dir=lambda: self.data,
            session_cwd=lambda: str(self.ws),
            redact=lambda text, path: text.replace("SECRET", "[redacted]"),
            session_key=lambda: self.session_key,
            clock=lambda: self.now,
        )

    # -- driving the runtime the way Hermes does ------------------------------

    def tool(self, sid, tid, name, args, next_call):
        return self.rt.tool_execution(
            tool_name=name, args=args, original_args=args, next_call=next_call,
            session_id=sid, turn_id=tid, task_id="task", tool_call_id="call",
            api_request_id="", conversation_id=sid,
            middleware_schema_version="hermes.middleware.v1",
            telemetry_schema_version="hermes.observer.v1",
        )

    def pre(self, sid, tid, message=REQUEST, first=True, platform="cli", model="gpt-oss:20b"):
        return self.rt.pre_llm_call(
            session_id=sid, turn_id=tid, task_id="task", user_message=message,
            conversation_history=[], is_first_turn=first, model=model, platform=platform,
            parent_session_id="", sender_id="", telemetry_schema_version="hermes.observer.v1",
        )

    def post(self, sid, tid, answer=ANSWER, model="gpt-oss:20b", platform="cli"):
        return self.rt.post_llm_call(
            session_id=sid, turn_id=tid, task_id="task", user_message=REQUEST,
            assistant_response=answer, conversation_history=[], model=model,
            platform=platform, side_question_token=None,
        )

    def cmd(self, text, sid="S-rec", key=None):
        return self.rt.command(text, source=None, session_id=sid,
                               session_key=sid if key is None else key, platform="desktop")

    def search_result(self):
        return json.dumps({"total_count": 1, "matches": [
            {"path": str(self.auth), "line": 2, "content": "AUTH_PORT = 8080"}]})

    def read_result(self):
        return json.dumps({"content": "1|HOST = '0.0.0.0'\n2|AUTH_PORT = 8080\n3|# token [redacted]",
                           "total_lines": 3, "file_size": 48, "truncated": False})

    def record_demo(self, name="auth-port", sid="S-rec", tid="T-rec"):
        self.cmd(f"record {name}", sid)
        self.assertIsNone(self.pre(sid, tid, first=False, platform="desktop", model="arete:27b"))
        self.tool(sid, tid, "search_files", {"pattern": "AUTH_PORT", "path": str(self.ws)},
                  live(self.search_result()))
        self.tool(sid, tid, "read_file", {"path": str(self.auth)}, live(self.read_result()))
        self.post(sid, tid, model="arete:27b", platform="desktop")
        return store.CassetteStore(self.data).load(name)

    def start_replay(self, name="auth-port", sid="S-rep", tid="T-rep", request=REQUEST):
        arm = arms.create_arm(self.data, name, request, now=self.now)
        self.assertIsNone(self.pre(sid, tid, message=request))
        return arm

    def ledger(self, arm):
        return arms.read_ledger(self.data, arm["id"])


# ---------------------------------------------------------------------------
# Registration and the manifest
# ---------------------------------------------------------------------------


class StrictContext:
    """Like the validator's recording stub: only the register_* surface exists."""

    def __init__(self):
        self.hooks, self.middleware, self.commands, self.tools = [], [], [], []
        self.cli_commands = []

    def register_hook(self, hook_name, callback, *, hold_stream=False):
        self.hooks.append((hook_name, callback))

    def register_middleware(self, kind, callback):
        self.middleware.append((kind, callback))

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands.append((name, handler))

    def register_tool(self, name, *a, **kw):
        self.tools.append(name)

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli_commands.append((name, setup_fn, handler_fn))


def manifest_list(text, key):
    match = re.search(rf"^{key}:[ \t]*(\[\])?[ \t]*\n((?:[ \t]+-[ \t]*\S+[ \t]*\n)*)", text, re.M)
    assert match, f"{key} missing from plugin.yaml"
    return re.findall(r"-[ \t]*(\S+)", match.group(2))


class Registration(unittest.TestCase):
    def test_register_matches_the_manifest(self):
        ctx = StrictContext()
        plugin.register(ctx)
        manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertEqual(sorted(n for n, _ in ctx.hooks), sorted(manifest_list(manifest, "provides_hooks")))
        self.assertEqual(sorted(k for k, _ in ctx.middleware), sorted(manifest_list(manifest, "provides_middleware")))
        self.assertEqual(ctx.tools, manifest_list(manifest, "provides_tools"))
        self.assertEqual([n for n, _ in ctx.commands], ["cassette"])
        self.assertEqual([n for n, _, _ in ctx.cli_commands], ["cassette"])
        self.assertRegex(manifest, r"(?m)^name: session-cassettes$")

    def test_callbacks_take_any_keywords(self):
        ctx = StrictContext()
        plugin.register(ctx)
        hooks = dict(ctx.hooks)
        self.assertIsNone(hooks["pre_llm_call"](session_id="", some_future_field=1))
        self.assertIsNone(hooks["post_llm_call"](session_id="", some_future_field=1))
        next_call = live("real")
        result = ctx.middleware[0][1](tool_name="read_file", args={"path": "x"}, next_call=next_call,
                                      session_id="nobody", turn_id="t", some_future_field=1)
        self.assertEqual(result, "real")
        next_call.assert_called_once_with({"path": "x"})
        self.assertIn("/cassette record", ctx.commands[0][1]("", session_id="s", session_key="s",
                                                              platform="cli", source=None))

    def test_imports_and_registers_in_a_bare_interpreter(self):
        # What `hermes plugins validate` does: no Hermes on the path, register()
        # against a stub that has only the registration methods.
        script = (
            "import importlib.util, json, sys\n"
            "d = sys.argv[1]\n"
            "spec = importlib.util.spec_from_file_location('probe_plugin', d + '/__init__.py',"
            " submodule_search_locations=[d])\n"
            "m = importlib.util.module_from_spec(spec); m.__path__ = [d]\n"
            "sys.modules[spec.name] = m; spec.loader.exec_module(m)\n"
            "seen = []\n"
            "class Ctx:\n"
            "    def register_hook(self, n, cb, *, hold_stream=False): seen.append(['hook', n])\n"
            "    def register_middleware(self, k, cb): seen.append(['middleware', k])\n"
            "    def register_command(self, n, *a, **kw): seen.append(['command', n])\n"
            "    def register_cli_command(self, n, *a, **kw): seen.append(['cli_command', n])\n"
            "m.register(Ctx())\n"
            "bad = [n for n in sys.modules if n.split('.')[0] in"
            " ('agent', 'gateway', 'hermes_cli', 'plugins', 'tools', 'hermes_constants')]\n"
            "print(json.dumps({'seen': seen, 'hermes': bad}))\n"
        )
        out = subprocess.run([sys.executable, "-I", "-c", script, str(ROOT)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        report = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(report["hermes"], [])
        self.assertEqual(sorted(report["seen"]), [["cli_command", "cassette"], ["command", "cassette"],
                                                  ["hook", "post_llm_call"],
                                                  ["hook", "pre_llm_call"], ["middleware", "tool_execution"]])


# ---------------------------------------------------------------------------
# Sessions the plugin has not bound
# ---------------------------------------------------------------------------


class Unbound(PluginCase):
    def test_tools_pass_straight_through(self):
        for name in ["read_file", "terminal", "write_file", "delegate_task", "tool_call"]:
            with self.subTest(tool=name):
                next_call = live("real result")
                args = {"path": "x"}
                self.assertEqual(self.tool("S-other", "T1", name, args, next_call), "real result")
                next_call.assert_called_once_with(args)

    def test_tool_errors_propagate_untouched(self):
        with self.assertRaises(ZeroDivisionError):
            self.tool("S-other", "T1", "read_file", {}, mock.Mock(side_effect=ZeroDivisionError))

    def test_hooks_ignore_ordinary_turns(self):
        self.assertIsNone(self.pre("S-other", "T1"))
        self.post("S-other", "T1")
        self.assertEqual(store.CassetteStore(self.data).names(), [])
        self.assertFalse((self.data / arms.CLAIMED_DIR).exists())

    def test_an_ordinary_session_is_untouched_while_another_replays(self):
        self.record_demo()
        self.start_replay()
        next_call = live("real")
        self.assertEqual(self.tool("S-other", "T9", "read_file", {"path": str(self.auth)}, next_call), "real")
        next_call.assert_called_once()


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


class Record(PluginCase):
    def test_record_saves_request_answer_calls_and_snapshot(self):
        cassette = self.record_demo()
        self.assertEqual(cassette.request, REQUEST)
        self.assertEqual(cassette.final_answer, ANSWER)
        self.assertEqual(cassette.model, "arete:27b")
        self.assertEqual(cassette.cwd, str(self.ws))
        self.assertEqual(cassette.workspace, str(self.ws))
        self.assertEqual([c.tool for c in cassette.calls], ["search_files", "read_file"])
        self.assertEqual(cassette.calls[0].result, self.search_result())
        self.assertEqual(cassette.calls[1].result, self.read_result())
        self.assertEqual(cassette.unrecorded, [])
        self.assertEqual(cassette.warnings, [])
        texts = [entry["text"] for entry in cassette.snapshot.values()]
        self.assertEqual(len(texts), 1)  # auth.py only: health.py was never named
        self.assertIn("AUTH_PORT = 8080", texts[0])
        self.assertIn("[redacted]", texts[0])  # Hermes's redaction is applied to the snapshot
        self.assertNotIn("SECRET", json.dumps(cassette.to_dict()))
        self.assertIn("saved 'auth-port': 2 tool call(s) recorded, 1 file(s)", self.cmd("status"))

    def test_the_real_tool_runs_once_and_its_result_is_returned_unchanged(self):
        self.cmd("record c1")
        self.pre("S-rec", "T1", first=False, platform="desktop")
        next_call = live(self.read_result())
        args = {"path": str(self.auth)}
        self.assertEqual(self.tool("S-rec", "T1", "read_file", args, next_call), self.read_result())
        next_call.assert_called_once_with(args)

    def test_only_the_allowlist_is_recorded_and_the_rest_warns(self):
        self.cmd("record c1")
        self.pre("S-rec", "T1", first=False, platform="desktop")
        for name in sorted(plugin.RECORDED_TOOLS):
            self.tool("S-rec", "T1", name, {"q": name}, live(json.dumps({"ok": name})))
        for name in ["write_file", "patch", "execute_code", "delegate_task", "memory", "browser_navigate"]:
            next_call = live("done")
            self.assertEqual(self.tool("S-rec", "T1", name, {"x": 1}, next_call), "done")
            next_call.assert_called_once()
        self.tool("S-rec", "T1", "write_file", {"x": 2}, live("done"))
        self.post("S-rec", "T1")
        cassette = store.CassetteStore(self.data).load("c1")
        self.assertEqual(sorted(c.tool for c in cassette.calls), sorted(plugin.RECORDED_TOOLS))
        self.assertEqual(sorted(u["tool"] for u in cassette.unrecorded),
                         sorted(["write_file", "write_file", "patch", "execute_code", "delegate_task",
                                 "memory", "browser_navigate"]))
        self.assertNotIn('"x": 2', json.dumps(cassette.to_dict()))  # name only, no arguments
        self.assertEqual(len(cassette.warnings), 1)
        self.assertIn("this cassette may not replay cleanly", cassette.warnings[0])
        self.assertIn("write_file x2", cassette.warnings[0])
        self.assertIn("this cassette may not replay cleanly", self.cmd("status"))

    def test_calls_from_another_turn_run_live_and_are_not_saved(self):
        self.cmd("record c1")
        self.pre("S-rec", "T1", first=False, platform="desktop")
        fork = live("fork result")
        self.assertEqual(self.tool("S-rec", "T-fork", "read_file", {"path": "memo.md"}, fork), "fork result")
        fork.assert_called_once()
        self.tool("S-rec", "T1", "read_file", {"path": str(self.auth)}, live(self.read_result()))
        self.post("S-rec", "T1")
        cassette = store.CassetteStore(self.data).load("c1")
        self.assertEqual(len(cassette.calls), 1)
        self.assertEqual(cassette.unrecorded, [])

    def test_post_llm_call_of_another_turn_does_not_end_the_recording(self):
        self.cmd("record c1")
        self.pre("S-rec", "T1", first=False, platform="desktop")
        self.post("S-rec", "T-fork")
        self.assertEqual(store.CassetteStore(self.data).names(), [])
        self.post("S-rec", "T1")
        self.assertEqual(store.CassetteStore(self.data).names(), ["c1"])

    def test_recording_is_one_completed_turn(self):
        self.record_demo("c1")
        next_call = live("real")
        self.pre("S-rec", "T2", first=False, platform="desktop")
        self.tool("S-rec", "T2", "read_file", {"path": "later.py"}, next_call)
        self.post("S-rec", "T2", answer="something else")
        cassette = store.CassetteStore(self.data).load("c1")
        self.assertEqual(len(cassette.calls), 2)
        self.assertEqual(cassette.final_answer, ANSWER)

    def test_an_interrupted_turn_keeps_the_recording_armed(self):
        self.cmd("record c1")
        self.pre("S-rec", "T1", message="first try", first=False, platform="desktop")
        self.tool("S-rec", "T1", "read_file", {"path": "junk.py"}, live("junk"))
        # no post_llm_call: the turn was interrupted. The user asks again.
        self.pre("S-rec", "T2", first=False, platform="desktop")
        self.tool("S-rec", "T2", "read_file", {"path": str(self.auth)}, live(self.read_result()))
        self.post("S-rec", "T2")
        cassette = store.CassetteStore(self.data).load("c1")
        self.assertEqual(cassette.request, REQUEST)
        self.assertEqual([c.args for c in cassette.calls], [{"path": str(self.auth)}])

    def test_at_references_are_refused_and_the_turn_is_blocked(self):
        for message in ["explain @file:server/auth.py", "what changed? @diff", "see @url:https://x.test/a",
                        "see @folder:server", "notes\n\n--- Attached Context ---\n\nfile body"]:
            with self.subTest(message=message[:20]):
                self.cmd("record c1")
                out = self.pre("S-rec", "T1", message=message, first=False, platform="desktop")
                self.assertEqual(out["action"], "block")
                self.assertIn("@ reference", out["message"])
                next_call = live("real")
                self.tool("S-rec", "T1", "read_file", {"path": "x"}, next_call)
                next_call.assert_called_once()
                self.post("S-rec", "T1")
                self.assertEqual(store.CassetteStore(self.data).names(), [])
                self.assertIn("cancelled", self.cmd("status"))

    def test_plain_at_signs_are_fine(self):
        for message in ["mail bob@example.com about it", "what does the @property decorator do?", "a @ b"]:
            self.assertEqual(arms.request_refusal(message), "", message)

    def test_non_text_and_empty_messages_are_refused(self):
        for message in [[{"type": "image"}], None, "", "   \n"]:
            with self.subTest(message=str(message)[:20]):
                self.cmd("record c1")
                out = self.pre("S-rec", "T1", message=message, first=False, platform="desktop")
                self.assertEqual(out["action"], "block")
        self.assertEqual(store.CassetteStore(self.data).names(), [])

    def test_request_is_stored_in_its_comparable_form(self):
        self.cmd("record c1")
        self.pre("S-rec", "T1", message="  line one\r\nline two \n", first=False, platform="desktop")
        self.post("S-rec", "T1")
        self.assertEqual(store.CassetteStore(self.data).load("c1").request, "line one\nline two")

    def test_no_working_directory_means_no_snapshot_and_a_warning(self):
        self.rt = plugin.Runtime(data_dir=lambda: self.data, session_cwd=lambda: "",
                                 redact=lambda t, p: t, session_key=lambda: "", clock=lambda: self.now)
        cassette = self.record_demo()
        self.assertEqual(cassette.snapshot, {})
        self.assertEqual(len(cassette.calls), 2)
        self.assertTrue(any("no working directory" in w for w in cassette.warnings))

    def test_failed_redaction_keeps_the_file_out(self):
        def broken(text, path):
            raise RuntimeError("no redactor")

        self.rt = plugin.Runtime(data_dir=lambda: self.data, session_cwd=lambda: str(self.ws),
                                 redact=broken, session_key=lambda: "", clock=lambda: self.now)
        cassette = self.record_demo()
        self.assertEqual(cassette.snapshot, {})
        self.assertTrue(any("redaction failed" in w for w in cassette.warnings))

    def test_oversize_results_are_not_recorded(self):
        self.cmd("record c1")
        self.pre("S-rec", "T1", first=False, platform="desktop")
        with mock.patch.object(plugin, "MAX_RESULT_CHARS", 10):
            self.assertEqual(self.tool("S-rec", "T1", "web_extract", {"url": "u"}, live("x" * 11)), "x" * 11)
        self.post("S-rec", "T1")
        cassette = store.CassetteStore(self.data).load("c1")
        self.assertEqual(cassette.calls, [])
        self.assertTrue(any("too large" in w for w in cassette.warnings))

    def test_non_text_results_and_odd_arguments_do_not_break_the_session(self):
        self.cmd("record c1")
        self.pre("S-rec", "T1", first=False, platform="desktop")
        self.assertEqual(self.tool("S-rec", "T1", "read_file", {"path": "x"}, live({"not": "text"})), {"not": "text"})
        self.assertEqual(self.tool("S-rec", "T1", "read_file", None, live("r1")), "r1")
        self.assertEqual(self.tool("S-rec", "T1", "read_file", {"path": object()}, live("r2")), "r2")
        self.assertEqual(self.tool("S-rec", "T1", 42, {"path": "x"}, live("r3")), "r3")
        self.post("S-rec", "T1")
        self.assertEqual(store.CassetteStore(self.data).names(), ["c1"])

    def test_a_bookkeeping_failure_never_costs_the_user_the_result(self):
        self.cmd("record c1")
        self.pre("S-rec", "T1", first=False, platform="desktop")
        with mock.patch.object(store.Cassette, "add_call", side_effect=RuntimeError("boom")):
            self.assertEqual(self.tool("S-rec", "T1", "read_file", {"path": "x"}, live("real")), "real")

    def test_tool_errors_propagate_while_recording(self):
        self.cmd("record c1")
        self.pre("S-rec", "T1", first=False, platform="desktop")
        with self.assertRaises(ZeroDivisionError):
            self.tool("S-rec", "T1", "read_file", {}, mock.Mock(side_effect=ZeroDivisionError))

    def test_concurrent_tool_calls_are_all_recorded(self):
        self.cmd("record c1")
        self.pre("S-rec", "T1", first=False, platform="desktop")
        barrier = threading.Barrier(8)

        def worker(i):
            barrier.wait()
            for j in range(10):
                self.tool("S-rec", "T1", "web_search", {"query": f"{i}-{j}"}, live(f"r{i}-{j}"))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.post("S-rec", "T1")
        cassette = store.CassetteStore(self.data).load("c1")
        self.assertEqual(len(cassette.calls), 80)
        self.assertEqual(sorted(c.seq for c in cassette.calls), list(range(1, 81)))


class RecordTerminal(PluginCase):
    def test_read_only_command_check(self):
        good = ["cat server/auth.py", "type server\\auth.py", "dir", "ls -la server", "rg -n AUTH_PORT .",
                "git log --oneline -5", "git show HEAD:server/auth.py", "git diff HEAD~1", "  LS  ", "Git Log"]
        bad = ["rm -rf x", "cat a > b", "cat a >> b", "ls; rm x", "ls && rm x", "ls | xargs rm", "cat `id`",
               "cat $(id)", "cat $HOME/x", "ls\nrm x", "ls (rm x)", "cat < x", "git push", "git", "git -C x log",
               "git diff --output=x", "git log --output x", "rg --pre ./run.sh x", "python x.py", "catx a",
               "/bin/cat a", "", "   ", "echo hi", "cd server", "type nul > x"]
        for command in good:
            self.assertTrue(plugin._readonly_terminal({"command": command}), command)
        for command in bad:
            self.assertFalse(plugin._readonly_terminal({"command": command}), command)
        self.assertFalse(plugin._readonly_terminal({"command": "ls", "background": True}))
        self.assertFalse(plugin._readonly_terminal({"command": "ls", "pty": True}))
        self.assertFalse(plugin._readonly_terminal({"command": ["ls"]}))
        self.assertFalse(plugin._readonly_terminal(None))

    def test_read_only_commands_are_recorded_until_one_is_not(self):
        self.cmd("record c1")
        self.pre("S-rec", "T1", first=False, platform="desktop")
        self.tool("S-rec", "T1", "terminal", {"command": "cat server/auth.py"}, live('{"output": "AUTH_PORT = 8080"}'))
        mutating = live('{"output": ""}')
        self.tool("S-rec", "T1", "terminal", {"command": "sed -i s/8080/9090/ server/auth.py"}, mutating)
        mutating.assert_called_once()
        self.tool("S-rec", "T1", "terminal", {"command": "cat server/auth.py"}, live('{"output": "AUTH_PORT = 9090"}'))
        self.post("S-rec", "T1")
        cassette = store.CassetteStore(self.data).load("c1")
        self.assertEqual([c.result for c in cassette.calls], ['{"output": "AUTH_PORT = 8080"}'])
        self.assertEqual([u["tool"] for u in cassette.unrecorded], ["terminal", "terminal"])
        self.assertNotIn("sed -i", json.dumps(cassette.to_dict()))
        self.assertIn("this cassette may not replay cleanly", cassette.warnings[0])
        self.assertIn("terminal x2", cassette.warnings[0])

    def test_recorded_terminal_output_replays_on_an_exact_match_only(self):
        self.cmd("record c1")
        self.pre("S-rec", "T1", first=False, platform="desktop")
        self.tool("S-rec", "T1", "terminal", {"command": "cat server/auth.py"}, live('{"output": "8080"}'))
        self.post("S-rec", "T1")
        arm = self.start_replay("c1")
        next_call = forbidden()
        self.assertEqual(self.tool("S-rep", "T-rep", "terminal", {"command": "cat server/auth.py"}, next_call),
                         '{"output": "8080"}')
        for command in ["cat  server/auth.py", "cat server/auth.py ", "cat server/health.py", "rm -rf ."]:
            out = self.tool("S-rep", "T-rep", "terminal", {"command": command}, next_call)
            self.assertIn("cassette miss", out, command)
        out = self.tool("S-rep", "T-rep", "terminal", {"command": "cat server/auth.py", "workdir": "server"}, next_call)
        self.assertIn("cassette miss", out)
        next_call.assert_not_called()
        self.post("S-rep", "T-rep")
        self.assertEqual([kind for kind, _ in self.ledger(arm)["events"]],
                         ["hit", "miss", "miss", "miss", "miss", "miss"])


# ---------------------------------------------------------------------------
# /cassette
# ---------------------------------------------------------------------------


class Command(PluginCase):
    def test_usage(self):
        for text in ["", "help", "nonsense", "   "]:
            self.assertIn("/cassette record <name>", self.cmd(text))
        self.assertIn("usage", self.cmd("record"))
        self.assertIn("usage", self.cmd("record a b"))
        self.assertIn("usage", self.cmd("record a --force"))

    def test_record_refuses_bad_names(self):
        for name in ["../x", "..\\x", "con", "a.b", "C:\\x"]:
            out = self.cmd(f"record {name}")
            self.assertTrue(out.startswith("cassette: "), out)
        self.assertEqual(self.rt._pending, {})

    def test_record_says_what_will_be_stored(self):
        out = self.cmd("record auth-port")
        self.assertIn("next message is recorded as cassette 'auth-port'", out)
        self.assertIn("plain JSON", out)
        self.assertIn(str(self.data), out)
        self.assertIn("armed", self.cmd("status"))

    def test_existing_cassette_needs_overwrite(self):
        first = self.record_demo("c1")
        store.CassetteStore(self.data).load("c1")
        self.assertIn("already exists", self.cmd("record c1"))
        self.assertIn("nothing is armed", self.rt.command("status", session_id="S-new", session_key="S-new"))
        self.assertIn("armed", self.cmd("record c1 --overwrite"))
        self.pre("S-rec", "T5", message="another request", first=False, platform="desktop")
        self.post("S-rec", "T5", answer="another answer")
        second = store.CassetteStore(self.data).load("c1")
        self.assertEqual(second.request, "another request")
        self.assertNotEqual(first.request, second.request)

    def test_a_cassette_that_appears_while_recording_is_not_clobbered(self):
        self.cmd("record c1")
        self.pre("S-rec", "T1", first=False, platform="desktop")
        other = store.Cassette(name="c1", request="someone else's")
        store.CassetteStore(self.data).save(other)
        self.post("S-rec", "T1")
        self.assertEqual(store.CassetteStore(self.data).load("c1").request, "someone else's")
        self.assertIn("NOT saved", self.cmd("status"))

    def test_stop_cancels_before_and_during_a_turn(self):
        self.cmd("record c1")
        self.assertIn("cancelled", self.cmd("stop"))
        self.pre("S-rec", "T1", first=False, platform="desktop")
        self.post("S-rec", "T1")
        self.assertEqual(store.CassetteStore(self.data).names(), [])

        self.cmd("record c2")
        self.pre("S-rec", "T2", first=False, platform="desktop")
        self.tool("S-rec", "T2", "read_file", {"path": "x"}, live("r"))
        self.assertIn("recording 'c2' now: 1 tool call(s)", self.cmd("status"))
        self.assertIn("stopped", self.cmd("stop"))
        next_call = live("real")
        self.assertEqual(self.tool("S-rec", "T2", "read_file", {"path": "y"}, next_call), "real")
        self.post("S-rec", "T2")
        self.assertEqual(store.CassetteStore(self.data).names(), [])
        self.assertIn("nothing is being recorded", self.cmd("stop"))

    def test_recording_binds_the_session_that_asked(self):
        self.cmd("record c1", sid="S-a")
        self.pre("S-b", "T1", first=False, platform="desktop")
        self.post("S-b", "T1")
        self.assertEqual(store.CassetteStore(self.data).names(), [])
        self.pre("S-a", "T1", first=False, platform="desktop")
        self.post("S-a", "T1")
        self.assertEqual(store.CassetteStore(self.data).names(), ["c1"])

    def test_a_session_with_no_id_yet_is_found_by_its_key(self):
        self.assertIn("armed", self.rt.command("record c1", session_id="", session_key="route-7"))
        self.session_key = "route-other"
        self.pre("S-x", "T1", first=True, platform="desktop")
        self.post("S-x", "T1")
        self.assertEqual(store.CassetteStore(self.data).names(), [])
        self.session_key = "route-7"
        self.pre("S-new", "T1", first=True, platform="desktop")
        self.post("S-new", "T1")
        self.assertEqual(store.CassetteStore(self.data).names(), ["c1"])
        self.assertEqual(self.rt._pending, {})

    def test_no_id_and_no_key_is_refused(self):
        self.assertIn("no id yet", self.rt.command("record c1", session_id="", session_key=""))

    def test_pin_and_list(self):
        self.assertIn("no cassettes yet", self.cmd("list"))
        self.record_demo("auth-port")
        self.assertIn("pinned to 'auth-port': server/auth.py", self.cmd("pin auth-port server/auth.py"))
        self.assertIn("(2 pin(s) in all)", self.cmd('pin auth-port "8080"'))
        self.assertEqual(store.CassetteStore(self.data).load("auth-port").pins, ["server/auth.py", "8080"])
        self.assertIn("usage", self.cmd("pin auth-port"))
        self.assertTrue(self.cmd("pin nothere 8080").startswith("cassette: "))
        self.assertTrue(self.cmd("pin auth-port re:(").startswith("cassette: "))
        self.assertTrue(self.cmd("pin ../x 8080").startswith("cassette: "))
        self.assertEqual(self.cmd("list"), "cassettes: auth-port")

    def test_command_never_raises(self):
        self.rt = plugin.Runtime(data_dir=mock.Mock(side_effect=RuntimeError("no hermes")),
                                 session_cwd=lambda: "", redact=lambda t, p: t)
        for text in ["record c1", "list", "pin c1 x", "status", "stop"]:
            self.assertIsInstance(self.rt.command(text, session_id="s", session_key="s"), str)


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


class Replay(PluginCase):
    def setUp(self):
        super().setUp()
        self.cassette = self.record_demo()
        self.cmd("pin auth-port server/auth.py")
        self.cmd("pin auth-port 8080")

    def test_replay_answers_from_the_cassette_and_never_runs_a_tool(self):
        arm = self.start_replay()
        next_call = forbidden()
        # the recorded call, with the path written another way
        out = self.tool("S-rep", "T-rep", "search_files", {"pattern": "AUTH_PORT", "path": "."}, next_call)
        self.assertEqual(out, self.search_result())
        out = self.tool("S-rep", "T-rep", "read_file", {"path": "./server/auth.py"}, next_call)
        self.assertEqual(out, self.read_result())
        # not recorded, but answerable from the snapshot
        out = json.loads(self.tool("S-rep", "T-rep", "search_files", {"pattern": "HOST"}, next_call))
        self.assertEqual(out["total_count"], 1)
        self.assertIn("HOST = '0.0.0.0'", json.dumps(out))
        out = json.loads(self.tool("S-rep", "T-rep", "read_file", {"path": "server/auth.py", "limit": 1}, next_call))
        self.assertEqual(out["content"], "1|HOST = '0.0.0.0'")
        # outside what the cassette knows
        out = json.loads(self.tool("S-rep", "T-rep", "read_file", {"path": "server/health.py"}, next_call))
        self.assertIn("cassette miss: read_file", out["error"])
        next_call.assert_not_called()

        self.post("S-rep", "T-rep")
        ledger = self.ledger(arm)
        self.assertEqual(ledger["state"], arms.DONE)
        self.assertEqual(ledger["session_id"], "S-rep")
        self.assertEqual(ledger["turn_id"], "T-rep")
        self.assertEqual(ledger["model_seen"], "gpt-oss:20b")
        self.assertEqual(ledger["platform"], "cli")
        self.assertEqual(ledger["final_answer"], ANSWER)
        self.assertEqual(ledger["invalid_reasons"], [])
        self.assertEqual([kind for kind, _ in ledger["events"]],
                         ["hit", "hit", "synthesized", "synthesized", "miss"])
        self.assertEqual(ledger["events"][0][1], self.cassette.calls[0].key)

        cassette = store.CassetteStore(self.data).load("auth-port")
        v = verdict.compute_verdict(cassette.pins, ledger["final_answer"], ledger["events"],
                                    [c.key for c in cassette.calls], ledger["invalid_reasons"])
        self.assertEqual(v.result, verdict.DIVERGED)
        self.assertEqual((v.hits, v.synthesized, v.misses), (2, 2, 1))

    def test_a_clean_replay_grades_pass(self):
        arm = self.start_replay()
        next_call = forbidden()
        self.tool("S-rep", "T-rep", "search_files", {"pattern": "AUTH_PORT"}, next_call)
        self.tool("S-rep", "T-rep", "search_files", {"pattern": "8080", "output_mode": "files_only"}, next_call)
        self.post("S-rep", "T-rep")
        ledger = self.ledger(arm)
        cassette = store.CassetteStore(self.data).load("auth-port")
        v = verdict.compute_verdict(cassette.pins, ledger["final_answer"], ledger["events"],
                                    [c.key for c in cassette.calls], ledger["invalid_reasons"])
        self.assertEqual(v.result, verdict.PASS)
        next_call.assert_not_called()

    def test_every_tool_name_is_intercepted(self):
        self.start_replay()
        names = ["terminal", "write_file", "patch", "execute_code", "delegate_task", "memory", "process_manage",
                 "browser_navigate", "web_search", "web_extract", "skill_view", "skills_list", "session_search",
                 "tool_call", "manage_connections", "", "no_such_tool", "read_file", "search_files"]
        for name in names:
            with self.subTest(tool=name):
                next_call = forbidden()
                out = self.tool("S-rep", "T-rep", name, {"command": "rm -rf .", "path": "C:/nowhere/x"}, next_call)
                self.assertIn("cassette miss", json.loads(out)["error"])
                next_call.assert_not_called()

    def test_the_replay_changes_nothing_in_the_cassette(self):
        before = store.CassetteStore(self.data).path_for("auth-port").read_bytes()
        arm = self.start_replay()
        self.tool("S-rep", "T-rep", "read_file", {"path": "server/health.py"}, forbidden())
        self.post("S-rep", "T-rep")
        self.assertEqual(store.CassetteStore(self.data).path_for("auth-port").read_bytes(), before)
        self.assertEqual(self.ledger(arm)["state"], arms.DONE)

    def test_pre_llm_call_adds_nothing_to_the_replayed_prompt(self):
        arms.create_arm(self.data, "auth-port", REQUEST, now=self.now)
        self.assertIsNone(self.pre("S-rep", "T-rep"))

    def test_calls_from_another_turn_are_refused_and_are_not_misses(self):
        arm = self.start_replay()
        next_call = forbidden()
        self.tool("S-rep", "T-rep", "search_files", {"pattern": "AUTH_PORT"}, next_call)
        for turn in ["T-fork", "", "T-rep2"]:
            out = json.loads(self.tool("S-rep", turn, "read_file", {"path": str(self.auth)}, next_call))
            self.assertIn("refused", out["error"])
            self.assertNotIn("cassette miss", out["error"])
        next_call.assert_not_called()
        self.post("S-rep", "T-fork")  # a stray post from another turn changes nothing
        self.assertEqual(self.ledger(arm)["state"], arms.CLAIMED)
        self.post("S-rep", "T-rep")
        ledger = self.ledger(arm)
        self.assertEqual(ledger["events"], [("hit", self.cassette.calls[0].key)])
        self.assertEqual(ledger["refused_calls"], 3)
        self.assertEqual(ledger["invalid_reasons"], [])

    def test_after_the_turn_ends_the_session_keeps_refusing(self):
        arm = self.start_replay()
        self.post("S-rep", "T-rep")
        next_call = forbidden()
        for turn in ["T-rep", "T-fork"]:
            out = json.loads(self.tool("S-rep", turn, "read_file", {"path": str(self.auth)}, next_call))
            self.assertIn("refused", out["error"])
        next_call.assert_not_called()
        self.assertEqual(self.ledger(arm)["events"], [])
        self.post("S-rep", "T-rep", answer="a second answer")  # cannot overwrite the first
        self.assertEqual(self.ledger(arm)["final_answer"], ANSWER)

    def test_the_next_turn_of_the_session_is_live_again_but_the_old_turn_is_not(self):
        self.start_replay()
        self.assertIsNone(self.pre("S-rep", "T2", message="thanks, now fix it", first=False))
        live_call = live("real")
        self.assertEqual(self.tool("S-rep", "T2", "write_file", {"path": "x"}, live_call), "real")
        live_call.assert_called_once()
        late = forbidden()
        out = json.loads(self.tool("S-rep", "T-rep", "write_file", {"path": "x"}, late))
        self.assertIn("refused", out["error"])
        late.assert_not_called()

    def test_a_repeated_pre_llm_call_for_the_same_turn_keeps_the_binding(self):
        arm = self.start_replay()
        self.assertIsNone(self.pre("S-rep", "T-rep"))
        next_call = forbidden()
        self.assertEqual(self.tool("S-rep", "T-rep", "search_files", {"pattern": "AUTH_PORT"}, next_call),
                         self.search_result())
        self.post("S-rep", "T-rep")
        self.assertEqual(len(self.ledger(arm)["events"]), 1)

    def test_repeated_keys_use_a_cursor_per_run(self):
        cassette = store.Cassette(name="twice", request=REQUEST, cwd=str(self.ws), workspace=str(self.ws))
        cassette.add_call("web_search", {"query": "q"}, "first")
        cassette.add_call("web_search", {"query": "q"}, "second")
        store.CassetteStore(self.data).save(cassette)
        for sid in ["S-1", "S-2"]:  # two runs, one after the other: each starts at "first"
            arm = arms.create_arm(self.data, "twice", REQUEST, now=self.now)
            self.pre(sid, "T")
            got = [self.tool(sid, "T", "web_search", {"query": "q"}, forbidden()) for _ in range(3)]
            self.assertEqual(got, ["first", "second", "second"])
            self.post(sid, "T")
            self.assertEqual(len(self.ledger(arm)["events"]), 3)

    def test_concurrent_replay_calls_are_all_answered_and_logged(self):
        arm = self.start_replay()
        next_call = forbidden()
        barrier = threading.Barrier(8)
        outs = []

        def worker(i):
            barrier.wait()
            for j in range(25):
                args = {"pattern": "AUTH_PORT"} if j % 2 else {"path": f"missing-{i}-{j}.py"}
                outs.append(self.tool("S-rep", "T-rep", "search_files" if j % 2 else "read_file", args, next_call))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.post("S-rep", "T-rep")
        next_call.assert_not_called()
        self.assertEqual(len(outs), 200)
        kinds = [kind for kind, _ in self.ledger(arm)["events"]]
        self.assertEqual((kinds.count("hit"), kinds.count("miss")), (96, 104))

    def test_a_runaway_turn_is_cut_off_in_the_log_and_marked(self):
        arm = self.start_replay()
        with mock.patch.object(plugin, "MAX_EVENTS", 3):
            for i in range(5):
                self.tool("S-rep", "T-rep", "read_file", {"path": f"m{i}.py"}, forbidden())
            self.post("S-rep", "T-rep")
        ledger = self.ledger(arm)
        self.assertEqual(len(ledger["events"]), 3)
        self.assertTrue(any("cut short" in r for r in ledger["invalid_reasons"]))


class Claim(PluginCase):
    def setUp(self):
        super().setUp()
        self.record_demo()

    def assert_unclaimed(self, arm, sid, tid="T"):
        self.assertTrue(arms.arm_path(self.data, arm["id"]).exists())
        self.assertFalse(arms.claimed_path(self.data, arm["id"]).exists())
        next_call = live("real")
        self.assertEqual(self.tool(sid, tid, "read_file", {"path": "x"}, next_call), "real")
        next_call.assert_called_once()

    def test_only_a_first_turn_claims(self):
        arm = arms.create_arm(self.data, "auth-port", REQUEST, now=self.now)
        for first in [False, None, 1, "true"]:
            self.pre("S-x", "T", first=first)
            self.assert_unclaimed(arm, "S-x")

    def test_only_the_cli_platform_claims(self):
        arm = arms.create_arm(self.data, "auth-port", REQUEST, now=self.now)
        for platform_name in ["desktop", "telegram", "tui", "", None]:
            self.pre("S-x", "T", platform=platform_name)
            self.assert_unclaimed(arm, "S-x")

    def test_only_the_recorded_request_claims(self):
        arm = arms.create_arm(self.data, "auth-port", REQUEST, now=self.now)
        for message in [REQUEST + " please", REQUEST.lower(), "", None, [REQUEST],
                        "[10:02] " + REQUEST, REQUEST + "\n\n--- Attached Context ---\n\nx"]:
            self.pre("S-x", "T", message=message)
            self.assert_unclaimed(arm, "S-x")

    def test_outer_whitespace_and_line_endings_do_not_block_a_claim(self):
        arm = arms.create_arm(self.data, "auth-port", REQUEST, now=self.now)
        self.pre("S-rep", "T", message="  " + REQUEST + "\r\n")
        self.assertEqual(self.ledger(arm)["session_id"], "S-rep")

    def test_an_expired_arm_is_not_claimed(self):
        arm = arms.create_arm(self.data, "auth-port", REQUEST, now=self.now)
        self.now += arms.ARM_TTL_SECONDS + 5
        self.pre("S-x", "T")
        self.assert_unclaimed(arm, "S-x")

    def test_a_claimed_binding_does_not_expire_mid_turn(self):
        arm = self.start_replay()
        self.now += 10 * arms.ARM_TTL_SECONDS  # a very slow turn
        next_call = forbidden()
        self.assertEqual(self.tool("S-rep", "T-rep", "search_files", {"pattern": "AUTH_PORT"}, next_call),
                         self.search_result())
        self.pre("S-other", "T")  # other sessions come and go meanwhile
        self.assertIn("cassette miss", self.tool("S-rep", "T-rep", "terminal", {"command": "ls"}, next_call))
        next_call.assert_not_called()
        self.post("S-rep", "T-rep")
        self.assertEqual(self.ledger(arm)["state"], arms.DONE)

    def test_a_session_without_an_id_cannot_claim(self):
        arm = arms.create_arm(self.data, "auth-port", REQUEST, now=self.now)
        self.pre("", "T")
        self.assertTrue(arms.arm_path(self.data, arm["id"]).exists())

    def test_two_sessions_with_the_same_first_message_one_claims(self):
        # Equal text is not an exclusive claim; the exclusive file create is.
        for round_no in range(25):
            arm = arms.create_arm(self.data, "auth-port", REQUEST, now=self.now)
            barrier = threading.Barrier(6)

            def session(i):
                barrier.wait()
                self.pre(f"S{round_no}-{i}", "T")

            threads = [threading.Thread(target=session, args=(i,)) for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            winner = self.ledger(arm)["session_id"]
            self.assertTrue(winner.startswith(f"S{round_no}-"))
            for i in range(6):
                sid = f"S{round_no}-{i}"
                next_call = live("real")
                out = self.tool(sid, "T", "search_files", {"pattern": "AUTH_PORT"}, next_call)
                if sid == winner:
                    self.assertEqual(out, self.search_result())
                    next_call.assert_not_called()
                else:  # the losers are ordinary sessions: their tools run
                    self.assertEqual(out, "real")
                    next_call.assert_called_once()

    def test_live_calls_before_a_late_bind_make_the_run_invalid(self):
        # pre_llm_call is time-bounded and fails open: if Hermes gave up on it
        # and the turn began, tools ran live before the claim landed.
        live_call = live("real")
        self.tool("S-rep", "T-rep", "read_file", {"path": "x"}, live_call)
        self.tool("S-rep", "T-rep", "read_file", {"path": "y"}, live_call)
        arm = self.start_replay()
        self.assertIn("cassette miss", self.tool("S-rep", "T-rep", "read_file", {"path": "z"}, forbidden()))
        self.post("S-rep", "T-rep")
        ledger = self.ledger(arm)
        self.assertTrue(any("2 tool call(s) of this turn ran live" in r for r in ledger["invalid_reasons"]))
        v = verdict.compute_verdict(["8080"], ledger["final_answer"], ledger["events"], [], ledger["invalid_reasons"])
        self.assertEqual(v.result, verdict.INVALID)

    def test_a_claim_landing_while_a_live_call_runs_makes_the_run_invalid(self):
        # An abandoned pre_llm_call worker can bind the replay after a call
        # passed routing but before its tool returned.
        # Even with the turn's live count evicted, the in-flight count catches it.
        arms.create_arm(self.data, "auth-port", REQUEST, now=self.now)
        claimed = {}

        def real_tool_during_which_the_claim_lands(args):
            self.rt._live_calls.clear()  # as if evicted: only the in-flight count is left
            self.assertIsNone(self.pre("S-rep", "T-rep"))
            claimed["arm"] = next(iter(arms.waiting_arms(self.data, self.now)), None)
            return "real"

        self.assertEqual(
            self.tool("S-rep", "T-rep", "read_file", {"path": "x"},
                      mock.Mock(side_effect=real_tool_during_which_the_claim_lands)),
            "real")
        self.assertIsNone(claimed["arm"])  # the arm was claimed inside the live call
        self.assertEqual(self.rt._inflight, {})
        self.post("S-rep", "T-rep")
        ledger_files = list((self.data / "claimed").glob("*.json"))
        self.assertEqual(len(ledger_files), 1)
        ledger = json.loads(ledger_files[0].read_text(encoding="utf-8"))
        self.assertTrue(any("still running live" in r for r in ledger["invalid_reasons"]))

    def test_the_turn_ending_during_the_claim_setup_still_gets_its_reasons(self):
        # post_llm_call can run on the agent thread while an abandoned hook
        # worker is still inside _bind_replay.
        # The done ledger must wait for the setup and carry its reasons, and a
        # later "claimed" write must not replace it.
        arm = arms.create_arm(self.data, "auth-port", REQUEST, now=self.now)
        real_load = store.CassetteStore.load
        seen = {}

        def load_while_the_turn_ends(store_self, name):
            self.post("S-rep", "T-rep")
            seen["state_after_post"] = self.ledger(arm)["state"]
            return real_load(store_self, name)

        def real_tool(args):
            with mock.patch.object(store.CassetteStore, "load", autospec=True,
                                   side_effect=load_while_the_turn_ends):
                self.assertIsNone(self.pre("S-rep", "T-rep"))
            return "real"

        self.tool("S-rep", "T-rep", "read_file", {"path": "x"}, mock.Mock(side_effect=real_tool))
        self.assertEqual(seen["state_after_post"], arms.CLAIMED)  # not done before the setup ends
        ledger = self.ledger(arm)
        self.assertEqual(ledger["state"], arms.DONE)
        self.assertEqual(ledger["final_answer"], ANSWER)
        self.assertTrue(any("still running live" in r for r in ledger["invalid_reasons"]))
        self.assertIsNone(self.rt._bindings["S-rep"].index)  # the ended turn keeps refusing

    def test_the_done_ledger_is_never_replaced(self):
        arm = self.start_replay()
        self.post("S-rep", "T-rep")
        self.assertEqual(self.ledger(arm)["state"], arms.DONE)
        b = self.rt._bindings["S-rep"]
        b.invalid.append("late")
        self.rt._write_ledger(b)
        self.assertNotIn("late", self.ledger(arm)["invalid_reasons"])
        self.assertEqual(self.ledger(arm)["state"], arms.DONE)

    def test_threads_racing_a_claim(self):
        # Real threads: 8 tool calls are inside next_call when
        # the claim lands on another thread, then 8 more start after it.
        arm = arms.create_arm(self.data, "auth-port", REQUEST, now=self.now)
        entered, release = threading.Semaphore(0), threading.Event()

        def slow_tool(args):
            entered.release()
            release.wait(10)
            return "real"

        early_calls = [mock.Mock(side_effect=slow_tool) for _ in range(8)]
        early = [threading.Thread(target=self.tool, args=("S-rep", "T-rep", "read_file", {"path": f"e{i}"}, c))
                 for i, c in enumerate(early_calls)]
        for t in early:
            t.start()
        for _ in early:
            self.assertTrue(entered.acquire(timeout=10))
        claimer = threading.Thread(target=self.pre, args=("S-rep", "T-rep"))
        claimer.start()
        claimer.join(10)
        late_calls = [mock.Mock(return_value="real") for _ in range(8)]
        late = [threading.Thread(target=self.tool, args=("S-rep", "T-rep", "read_file", {"path": f"l{i}"}, c))
                for i, c in enumerate(late_calls)]
        for t in late:
            t.start()
        for t in late:
            t.join(10)
        release.set()
        for t in early:
            t.join(10)
        for c in late_calls:
            c.assert_not_called()
        self.assertEqual(self.rt._inflight, {})
        self.post("S-rep", "T-rep")
        ledger = self.ledger(arm)
        self.assertEqual(ledger["state"], arms.DONE)
        self.assertTrue(any(r.startswith("8 tool call(s) of this session were still running live")
                            for r in ledger["invalid_reasons"]), ledger["invalid_reasons"])
        self.assertEqual(len(ledger["events"]), 8)  # the late calls were answered from the cassette

    def test_in_flight_count_drops_when_the_live_tool_raises(self):
        with self.assertRaises(ValueError):
            self.tool("S-x", "T", "read_file", {"path": "x"}, mock.Mock(side_effect=ValueError("tool broke")))
        self.assertEqual(self.rt._inflight, {})
        with self.assertRaises(LiveToolRan):  # a BaseException
            self.tool("S-x", "T", "read_file", {"path": "x"}, forbidden())
        self.assertEqual(self.rt._inflight, {})

    def test_retired_replay_turns_outlive_the_small_tables(self):
        # A retired turn must not be trimmed with the small bookkeeping tables,
        # or its late calls would run live.
        with mock.patch.object(plugin, "_MAX_TRACKED", 2):
            for sid in ["S-old", "S1", "S2", "S3", "S4"]:
                self.now += 1
                self.start_replay(sid=sid, tid="T-old")
                self.post(sid, "T-old")
                self.pre(sid, "T-next", first=False)  # a new turn retires the replayed one
            self.assertEqual(len(self.rt._retired), 5)
            next_call = forbidden()
            self.assertIn("refused", self.tool("S-old", "T-old", "write_file", {"path": "x"}, next_call))
            next_call.assert_not_called()

    def test_a_recording_session_does_not_also_claim(self):
        arm = arms.create_arm(self.data, "auth-port", REQUEST, now=self.now)
        self.cmd("record again", sid="S-both")
        self.pre("S-both", "T")
        self.assertTrue(arms.arm_path(self.data, arm["id"]).exists())


# ---------------------------------------------------------------------------
# SEC-7: replay fails closed
# ---------------------------------------------------------------------------


class Sec7FailClosed(PluginCase):
    TOOLS = ["read_file", "search_files", "terminal", "write_file", "delegate_task", "web_search"]

    def setUp(self):
        super().setUp()
        self.record_demo()

    def assert_all_miss(self, sid="S-rep", tid="T-rep"):
        for name in self.TOOLS:
            with self.subTest(tool=name):
                next_call = forbidden()
                out = self.tool(sid, tid, name, {"path": str(self.auth), "pattern": "AUTH_PORT"}, next_call)
                self.assertIn("cassette miss", json.loads(out)["error"])
                next_call.assert_not_called()

    def test_corrupt_cassette(self):
        store.CassetteStore(self.data).path_for("auth-port").write_bytes(b'{"format": 1, "name": "auth-po')
        arm = self.start_replay()
        self.assert_all_miss()
        self.post("S-rep", "T-rep")
        ledger = self.ledger(arm)
        self.assertEqual([kind for kind, _ in ledger["events"]], ["miss"] * len(self.TOOLS))
        self.assertTrue(any("could not be loaded" in r for r in ledger["invalid_reasons"]))

    def test_wrong_typed_cassette(self):
        store.CassetteStore(self.data).path_for("auth-port").write_text(
            json.dumps({"format": 1, "name": "auth-port", "calls": "none"}), encoding="utf-8")
        self.start_replay()
        self.assert_all_miss()

    def test_missing_cassette(self):
        store.CassetteStore(self.data).path_for("auth-port").unlink()
        arm = self.start_replay()
        self.assert_all_miss()
        self.post("S-rep", "T-rep")
        self.assertTrue(any("could not be loaded" in r for r in self.ledger(arm)["invalid_reasons"]))

    def test_cassette_named_by_another_file(self):
        other = store.Cassette(name="other", request=REQUEST)
        other.add_call("read_file", {"path": str(self.auth)}, "from the wrong cassette")
        path = store.CassetteStore(self.data).path_for("auth-port")
        path.write_text(json.dumps(other.to_dict()), encoding="utf-8")
        self.start_replay()
        self.assert_all_miss()

    def test_ledger_file_deleted_mid_run(self):
        arm = self.start_replay()
        next_call = forbidden()
        self.assertEqual(self.tool("S-rep", "T-rep", "search_files", {"pattern": "AUTH_PORT"}, next_call),
                         self.search_result())
        arms.claimed_path(self.data, arm["id"]).unlink()
        # The binding lives in the gateway's memory, so the turn is still held.
        self.assertEqual(self.tool("S-rep", "T-rep", "read_file", {"path": str(self.auth)}, next_call),
                         self.read_result())
        self.assertIn("cassette miss", self.tool("S-rep", "T-rep", "write_file", {"path": "x"}, next_call))
        self.assertIn("refused", self.tool("S-rep", "T-fork", "write_file", {"path": "x"}, next_call))
        next_call.assert_not_called()
        self.post("S-rep", "T-rep")
        # The ledger does not come back, so the run cannot be reported as valid.
        self.assertFalse(arms.claimed_path(self.data, arm["id"]).exists())
        with self.assertRaises(store.CassetteError):
            self.ledger(arm)

    def test_whole_data_folder_removed_mid_run(self):
        self.start_replay()
        shutil.rmtree(self.data)
        self.assertEqual(self.tool("S-rep", "T-rep", "search_files", {"pattern": "AUTH_PORT"}, forbidden()),
                         self.search_result())
        for name in ["terminal", "write_file", "delegate_task"]:
            next_call = forbidden()
            self.assertIn("cassette miss", self.tool("S-rep", "T-rep", name, {"x": 1}, next_call))
            next_call.assert_not_called()
        self.post("S-rep", "T-rep")  # must not raise

    def test_exception_inside_key_normalisation(self):
        arm = self.start_replay()
        with mock.patch.object(store, "normalize_args", side_effect=RuntimeError("boom")):
            self.assert_all_miss()
        self.post("S-rep", "T-rep")
        self.assertEqual([kind for kind, _ in self.ledger(arm)["events"]], ["miss"] * len(self.TOOLS))

    def test_exception_inside_the_snapshot_search(self):
        self.start_replay()
        binding = self.rt._bindings["S-rep"]
        binding.index._synthesize = mock.Mock(side_effect=RuntimeError("boom"))
        next_call = forbidden()
        out = self.tool("S-rep", "T-rep", "search_files", {"pattern": "HOST"}, next_call)
        self.assertIn("cassette miss", out)
        next_call.assert_not_called()

    def test_exception_in_the_answer_path_itself(self):
        arm = self.start_replay()
        binding = self.rt._bindings["S-rep"]
        binding.index.answer = mock.Mock(side_effect=RuntimeError("boom"))
        self.assert_all_miss()
        self.post("S-rep", "T-rep")
        self.assertEqual([kind for kind, _ in self.ledger(arm)["events"]], ["miss"] * len(self.TOOLS))

    def test_exception_in_the_event_log(self):
        arm = self.start_replay()
        with mock.patch.object(plugin.Runtime, "_log_event", side_effect=RuntimeError("boom")):
            self.assert_all_miss()

    def test_exception_in_the_replay_handler_itself(self):
        arm = self.start_replay()
        with mock.patch.object(plugin.Runtime, "_replay", side_effect=RuntimeError("boom")):
            self.assert_all_miss()
        self.post("S-rep", "T-rep")
        self.assertTrue(any("internal error" in r for r in self.ledger(arm)["invalid_reasons"]))

    def test_exception_inside_the_answer_is_a_miss_and_makes_the_run_invalid(self):
        arm = self.start_replay()
        with mock.patch.object(store.ReplayIndex, "answer", side_effect=RuntimeError("boom")):
            next_call = forbidden()
            self.assertIn("cassette miss", self.tool("S-rep", "T-rep", "read_file", {"path": "x"}, next_call))
            next_call.assert_not_called()
        self.post("S-rep", "T-rep")
        self.assertTrue(any("internal error" in r and "RuntimeError" in r
                            for r in self.ledger(arm)["invalid_reasons"]))

    def test_exception_while_routing_refuses_during_a_replay(self):
        self.start_replay()
        with mock.patch.object(plugin.Runtime, "_route", side_effect=RuntimeError("boom")):
            for sid in ["S-rep", "S-other"]:
                next_call = forbidden()
                out = self.tool(sid, "T-rep", "write_file", {"path": "x"}, next_call)
                self.assertIn("refused", json.loads(out)["error"])
                next_call.assert_not_called()

    def test_exception_while_routing_passes_through_when_nothing_replays(self):
        with mock.patch.object(plugin.Runtime, "_route", side_effect=RuntimeError("boom")):
            next_call = live("real")
            self.assertEqual(self.tool("S-other", "T", "read_file", {"path": "x"}, next_call), "real")

    def test_hostile_arguments_and_names(self):
        self.start_replay()

        class Unprintable:
            def __str__(self):
                raise RuntimeError("no str")

            __repr__ = __str__

        cases = [
            ("read_file", None), ("read_file", "a string"), ("read_file", ["list"]), ("read_file", {"path": None}),
            ("read_file", {"path": 5, "offset": "x", "limit": []}), ("read_file", {"path": "\x00"}),
            ("read_file", {"path": "x" * 100000}), ("search_files", {"pattern": "(unclosed", "path": "."}),
            ("search_files", {"pattern": ["a"], "limit": "many"}), ("read_file", {"path": Unprintable()}),
            ("read_file", {Unprintable(): 1}), (Unprintable(), {"path": "x"}), (None, None), (5, {"a": {"b": [1, {}]}}),
        ]
        for name, args in cases:
            with self.subTest(tool=type(name).__name__, args=type(args).__name__):
                next_call = forbidden()
                out = self.tool("S-rep", "T-rep", name, args, next_call)  # must not raise
                self.assertIsInstance(out, str)
                self.assertIsInstance(json.loads(out), dict)  # a miss, or an answer from the snapshot
                next_call.assert_not_called()

    def test_tampered_recorded_result_type_is_a_miss(self):
        self.start_replay()
        binding = self.rt._bindings["S-rep"]
        for results in binding.index._by_key.values():
            results[:] = [{"not": "text"}]
        for name, args in [("search_files", {"pattern": "AUTH_PORT", "path": str(self.ws)}),
                           ("read_file", {"path": str(self.auth)})]:  # the two recorded calls
            next_call = forbidden()
            out = self.tool("S-rep", "T-rep", name, args, next_call)
            self.assertIn("cassette miss", json.loads(out)["error"])
            next_call.assert_not_called()

    def test_hooks_never_raise(self):
        broken = plugin.Runtime(data_dir=mock.Mock(side_effect=RuntimeError("no hermes")),
                                session_cwd=mock.Mock(side_effect=RuntimeError("no ctx")),
                                redact=lambda t, p: t, session_key=mock.Mock(side_effect=RuntimeError("x")))
        self.assertIsNone(broken.pre_llm_call(session_id="s", turn_id="t", user_message=REQUEST,
                                              is_first_turn=True, platform="cli"))
        self.assertIsNone(broken.post_llm_call(session_id="s", turn_id="t", assistant_response="a"))
        self.assertIsNone(broken.pre_llm_call(session_id=object(), turn_id=None, user_message=object()))


# ---------------------------------------------------------------------------
# A gateway that stays up for weeks
# ---------------------------------------------------------------------------


class Housekeeping(PluginCase):
    def setUp(self):
        super().setUp()
        self.record_demo()

    def test_finished_bindings_are_dropped_but_live_replays_never_are(self):
        with mock.patch.object(plugin, "MAX_BINDINGS", 4):
            held = self.start_replay(sid="S-held", tid="T-held")
            for i in range(12):
                self.now += 1
                arm = arms.create_arm(self.data, "auth-port", REQUEST, now=self.now)
                self.pre(f"S{i}", "T")
                self.post(f"S{i}", "T")
                self.assertEqual(self.ledger(arm)["state"], arms.DONE)
            # inside the grace period nothing finished is dropped
            self.assertEqual(len(self.rt._bindings), 13)
            self.now += plugin.REPLAY_GRACE_SECONDS + 1
            arms.create_arm(self.data, "auth-port", REQUEST, now=self.now)
            self.pre("S-last", "T")
            self.assertLessEqual(len(self.rt._bindings), 4)
            self.assertIn("S-held", self.rt._bindings)
            self.assertIn("S-last", self.rt._bindings)
            next_call = forbidden()
            self.assertEqual(self.tool("S-held", "T-held", "search_files", {"pattern": "AUTH_PORT"}, next_call),
                             self.search_result())
            # a dropped session's replayed turn is still refused
            self.assertIn("refused", self.tool("S0", "T", "write_file", {"path": "x"}, next_call))
            next_call.assert_not_called()
            self.post("S-held", "T-held")
            self.assertEqual(self.ledger(held)["state"], arms.DONE)
            self.assertEqual(self.rt._replay_active, len([b for b in self.rt._bindings.values()
                                                          if b.mode == "replay"]))

    def test_old_unfinished_replays_give_up_their_cassette_but_keep_refusing(self):
        with mock.patch.object(plugin, "MAX_LOADED_REPLAYS", 2):
            for i in range(5):
                self.now += 1
                arms.create_arm(self.data, "auth-port", REQUEST, now=self.now)
                self.pre(f"S{i}", "T")  # never finishes: the run was interrupted
            loaded = [sid for sid, b in self.rt._bindings.items() if b.index is not None]
            self.assertEqual(sorted(loaded), ["S3", "S4"])
            next_call = forbidden()
            self.assertIn("cassette miss", self.tool("S0", "T", "search_files", {"pattern": "AUTH_PORT"}, next_call))
            self.assertIn("refused", self.tool("S0", "T-fork", "write_file", {"path": "x"}, next_call))
            next_call.assert_not_called()

    def test_bookkeeping_tables_stay_bounded(self):
        with mock.patch.object(plugin, "_MAX_GUARDED", 8):
            for i in range(50):
                self.tool(f"S{i}", "T", "read_file", {"path": "x"}, live("r"))
            self.assertLessEqual(len(self.rt._live_calls), 8)
            self.assertEqual(self.rt._inflight, {})


if __name__ == "__main__":
    unittest.main()
