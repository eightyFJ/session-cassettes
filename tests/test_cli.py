"""``hermes cassette ...`` (cli.py): block D of docs/PLAN.md.

The replay tests start a real child process, ``fake_hermes.py``, in place of
``hermes --oneshot``. It drives the plugin's real hooks and middleware, so the
whole loop is exercised across a process boundary: arm file, claim, replayed
tool calls, done ledger, answer check, verdict, run file, clean-up.

SEC-4: the request reaches the child as one unchanged argument, through an
argument list with no shell.
"""

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _pkg import PKG, arms, plugin, store, verdict
import importlib

cli = importlib.import_module(f"{PKG}.cli")

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_hermes.py"
REQUEST = "Where is the auth handler and what port does it bind?"
ANSWER = "The auth handler is server/auth.py; it binds port 8080."
HOSTILE = '-p evil "quoted" & echo pwned | more ; %PATH% $HOME `x`\nsecond line --model=bad'


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.root = root
        self.data = root / "data"
        self.data.mkdir()
        self.ws = root / "ws"
        (self.ws / "server").mkdir(parents=True)
        self.auth = self.ws / "server" / "auth.py"
        self.auth.write_text("AUTH_PORT = 8080\n", encoding="utf-8")
        self.live_log = root / "live.log"
        self.argv_log = root / "argv.json"

    # -- helpers ---------------------------------------------------------------

    def make_cassette(self, name="auth-port", pins=("server/auth.py", "8080"), request=REQUEST,
                      warnings=()):
        c = store.Cassette(name=name, request=request, final_answer=ANSWER, model="arete:27b",
                           cwd=str(self.ws), workspace=str(self.ws))
        c.add_call("read_file", {"path": str(self.auth)}, json.dumps({"content": "1|AUTH_PORT = 8080"}))
        for p in pins:
            c.add_pin(p)
        for w in warnings:
            c.add_warning(w)
        store.CassetteStore(self.data).save(c)
        return c

    def plan(self, **kw):
        plan = {"data_dir": str(self.data), "answer": ANSWER, "live_log": str(self.live_log),
                "argv_log": str(self.argv_log),
                "calls": [["read_file", {"path": str(self.auth)}]]}
        plan.update(kw)
        return mock.patch.dict(os.environ, {"FAKE_HERMES_PLAN": json.dumps(plan)})

    def replay(self, name="auth-port", **kw):
        kw.setdefault("hermes", [sys.executable, str(FAKE)])
        kw.setdefault("model", "gpt-oss:20b")
        kw.setdefault("timeout", 60)
        kw.setdefault("log", lambda text: None)
        return cli.replay(self.data, name, **kw)

    def parse(self, *argv):
        parser = argparse.ArgumentParser(prog="hermes cassette")
        cli.setup(parser)
        return parser.parse_args(list(argv))

    def run_cli(self, *argv, hermes=None):
        args = self.parse(*argv)
        out, err = io.StringIO(), io.StringIO()
        patch = mock.patch.object(cli, "find_hermes", return_value=hermes or [sys.executable, str(FAKE)])
        with patch, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.handle(args, data_dir=lambda: self.data)
        return code, out.getvalue(), err.getvalue()

    def assert_no_arm_files(self):
        for folder in (arms.ARMED_DIR, arms.CLAIMED_DIR):
            path = self.data / folder
            self.assertEqual(list(path.iterdir()) if path.is_dir() else [], [], folder)

    def assert_nothing_ran_live(self):
        self.assertFalse(self.live_log.exists(), "a real tool ran during replay")


# ---------------------------------------------------------------------------
# replay: the four results
# ---------------------------------------------------------------------------


class Replay(CliCase):
    def test_pass(self):
        self.make_cassette()
        with self.plan():
            v, record, path = self.replay()
        self.assertEqual(v.result, verdict.PASS, v.invalid_reasons)
        self.assertEqual((v.hits, v.misses), (1, 0))
        self.assertEqual(record["session_id"], "S-child")
        self.assertEqual(record["model_seen"], "gpt-oss:20b")
        self.assertTrue(path.is_file())
        self.assertTrue(path.is_relative_to(self.data / "runs" / "auth-port"))
        saved = store.read_json_file(path)
        self.assertEqual(saved["result"], verdict.PASS)
        self.assertEqual(saved["answer"], ANSWER)
        self.assert_no_arm_files()
        self.assert_nothing_ran_live()

    def test_diverged_and_dangerous_tools_never_run(self):
        self.make_cassette()
        calls = [["read_file", {"path": str(self.auth)}],
                 ["write_file", {"path": str(self.auth), "content": "x"}],
                 ["terminal", {"command": "del /q *"}],
                 ["delegate_task", {"goal": "anything"}]]
        with self.plan(calls=calls):
            v, _, _ = self.replay()
        self.assertEqual(v.result, verdict.DIVERGED)
        self.assertEqual(v.misses, 3)
        self.assert_nothing_ran_live()
        self.assertEqual(self.auth.read_text(encoding="utf-8"), "AUTH_PORT = 8080\n")

    def test_fail_when_a_pin_is_missing(self):
        self.make_cassette()
        with self.plan(answer="It is in server/auth.py on port 5000."):
            v, _, _ = self.replay()
        self.assertEqual(v.result, verdict.FAIL)
        self.assertEqual([p.matched for p in v.pins], [True, False])

    def test_synthesized_answers_still_pass(self):
        c = self.make_cassette()
        c.snapshot = {store.normalize_path(self.auth): {"path": str(self.auth), "text": "AUTH_PORT = 8080\n",
                                                        "size": 17}}
        store.CassetteStore(self.data).save(c)
        calls = [["search_files", {"pattern": "AUTH_PORT", "path": str(self.ws)}]]
        with self.plan(calls=calls):
            v, _, _ = self.replay()
        self.assertEqual((v.result, v.synthesized, v.misses), (verdict.PASS, 1, 0), v.invalid_reasons)


class ReplayInvalid(CliCase):
    def test_never_claimed(self):
        self.make_cassette()
        with self.plan(message="a different request"):
            v, _, _ = self.replay()
        self.assertEqual(v.result, verdict.INVALID)
        self.assertTrue(any("no session claimed" in r for r in v.invalid_reasons), v.invalid_reasons)
        self.assert_no_arm_files()  # the leftover arm is removed afterwards

    def test_not_claimed_on_another_platform(self):
        self.make_cassette()
        with self.plan(platform="desktop"):
            v, _, _ = self.replay()
        self.assertEqual(v.result, verdict.INVALID)

    def test_printed_answer_differs_from_the_claimed_turn(self):
        self.make_cassette()
        with self.plan(stdout="server/auth.py 8080 (from some other session)"):
            v, _, _ = self.replay()
        self.assertEqual(v.result, verdict.INVALID)
        self.assertTrue(any("not what hermes printed" in r for r in v.invalid_reasons))

    def test_trailing_whitespace_is_not_a_mismatch(self):
        self.make_cassette()
        with self.plan(stdout=ANSWER + "\r\n\r\n"):
            v, _, _ = self.replay()
        self.assertEqual(v.result, verdict.PASS, v.invalid_reasons)

    def test_nonzero_exit(self):
        self.make_cassette()
        with self.plan(exit=1):
            v, record, _ = self.replay()
        self.assertEqual(v.result, verdict.INVALID)
        self.assertEqual(record["exit_code"], 1)

    def test_timeout(self):
        self.make_cassette()
        with self.plan(sleep=5):
            v, record, _ = self.replay(timeout=1)
        self.assertEqual(v.result, verdict.INVALID)
        self.assertTrue(record["timed_out"])
        self.assert_no_arm_files()

    def test_cassette_reasons_from_the_hooks_reach_the_verdict(self):
        self.make_cassette()
        # Corrupt the cassette after the CLI loaded it: the hooks then fail to load it.
        real_create = arms.create_arm

        def create_then_corrupt(*a, **kw):
            arm = real_create(*a, **kw)
            store.CassetteStore(self.data).path_for("auth-port").write_text("{", encoding="utf-8")
            return arm

        with self.plan(), mock.patch.object(cli.arms, "create_arm", side_effect=create_then_corrupt):
            v, _, _ = self.replay()
        self.assertEqual(v.result, verdict.INVALID)
        self.assertTrue(any("could not be loaded" in r for r in v.invalid_reasons), v.invalid_reasons)
        self.assert_nothing_ran_live()

    def test_arm_files_removed_when_the_child_cannot_start(self):
        self.make_cassette()
        with self.assertRaises(cli.CliError):
            self.replay(hermes=[str(self.root / "no-such-hermes.exe")])
        self.assert_no_arm_files()


class ReplayRefusals(CliCase):
    def test_no_pins(self):
        self.make_cassette(pins=())
        with self.assertRaisesRegex(cli.CliError, "no pins"):
            self.replay()
        self.assert_no_arm_files()

    def test_missing_cassette(self):
        with self.assertRaisesRegex(cli.CliError, "no cassette named"):
            self.replay("nope")

    def test_bad_name(self):
        with self.assertRaises(store.CassetteError):
            self.replay("../x")

    def test_one_run_at_a_time(self):
        self.make_cassette()
        waiting = arms.create_arm(self.data, "auth-port", REQUEST)
        with self.plan(), self.assertRaisesRegex(store.CassetteError, "another cassette run"):
            self.replay()
        self.assertTrue(arms.arm_path(self.data, waiting["id"]).exists())  # someone else's arm is untouched
        self.assertFalse(self.argv_log.exists())  # no child was started

    def test_missing_workdir(self):
        self.make_cassette()
        with self.assertRaisesRegex(cli.CliError, "does not exist"):
            self.replay(workdir=str(self.root / "gone"))


# ---------------------------------------------------------------------------
# SEC-4: the child gets an argument list, never a shell
# ---------------------------------------------------------------------------


class CommandInjection(CliCase):
    def test_hostile_request_reaches_the_child_unchanged(self):
        argv = cli.build_child_argv([sys.executable, str(FAKE)], HOSTILE, model="m", workdir=str(self.ws))
        self.assertIsInstance(argv, list)
        with self.plan():
            child = cli.spawn(argv, timeout=60, cwd=str(self.ws))
        self.assertEqual(child.returncode, 0, child.stderr)
        received = json.loads(self.argv_log.read_text(encoding="utf-8"))
        self.assertEqual(received, argv[2:])
        self.assertIn(f"--oneshot={HOSTILE}", received)
        # Hermes's own parser shape: -z/--oneshot PROMPT next to -m/--model and -p/--profile.
        parser = argparse.ArgumentParser()
        parser.add_argument("-z", "--oneshot")
        parser.add_argument("-m", "--model")
        parser.add_argument("-p", "--profile")
        parser.add_argument("--in", dest="in_dir")
        ns = parser.parse_args(received)
        self.assertEqual((ns.oneshot, ns.model, ns.profile), (HOSTILE, "m", None))
        self.assertNotIn("-p", received)  # Hermes scans argv for a bare -p before argparse

    def test_request_starting_with_a_dash(self):
        for request in ("-z", "--help", "-", "--oneshot=x"):
            with self.subTest(request=request):
                received = cli.build_child_argv(["hermes"], request)[1:]
                parser = argparse.ArgumentParser()
                parser.add_argument("-z", "--oneshot")
                self.assertEqual(parser.parse_args(received).oneshot, request)

    def test_full_replay_with_a_hostile_request(self):
        self.make_cassette(request=HOSTILE)
        with self.plan():
            v, _, _ = self.replay()
        self.assertEqual(v.result, verdict.PASS, v.invalid_reasons)
        received = json.loads(self.argv_log.read_text(encoding="utf-8"))
        self.assertEqual([a for a in received if a.startswith("--oneshot=")], [f"--oneshot={HOSTILE}"])

    def test_subprocess_is_called_without_a_shell(self):
        proc = mock.Mock(returncode=0)
        with mock.patch.object(cli.subprocess, "Popen", return_value=proc) as popen:
            cli.spawn(["hermes.exe", "--oneshot=a & b"], timeout=5)
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["hermes.exe", "--oneshot=a & b"])
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        proc.wait.assert_called_once_with(timeout=5)

    def test_timeout_returns_even_if_a_grandchild_keeps_the_output_open(self):
        # A launcher that starts a long-lived child sharing its stdout, then waits.
        script = ("import subprocess, sys, time\n"
                  "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                  "print('partial', flush=True)\n"
                  "time.sleep(30)\n")
        started = time.monotonic()
        child = cli.spawn([sys.executable, "-c", script], timeout=2)
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(child.timed_out)
        self.assertIsNone(child.returncode)
        self.assertIn("partial", child.stdout)

    def test_no_shell_anywhere_in_the_plugin(self):
        for path in HERE.parent.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertNotIn("shell=True", text)
                self.assertNotIn("os.system", text)
                self.assertNotIn("os.popen", text)
                if path.name != "cli.py":
                    self.assertNotIn("subprocess", text)

    def test_batch_wrappers_are_refused_on_windows(self):
        bat = self.root / "hermes.cmd"
        bat.write_text("@echo off\n", encoding="utf-8")
        exe = self.root / "hermes.exe"
        exe.write_bytes(b"MZ")
        with mock.patch.object(cli.os, "name", "nt"), mock.patch.object(cli.sys, "argv", ["x"]):
            with mock.patch.object(cli.shutil, "which", return_value=str(bat)):
                with self.assertRaisesRegex(cli.CliError, "cmd.exe"):
                    cli.find_hermes()
            with mock.patch.object(cli.shutil, "which", return_value=str(exe)):
                self.assertEqual(cli.find_hermes(), [str(exe.resolve())])
        with mock.patch.object(cli.os, "name", "nt"), mock.patch.object(cli.sys, "argv", [str(exe)]), \
                mock.patch.object(cli.shutil, "which", return_value=None):
            self.assertEqual(cli.find_hermes(), [str(exe.resolve())])

    def test_every_value_is_joined_to_its_flag(self):
        argv = cli.build_child_argv(["h"], "q", model="-p", provider="x y", skills=["a", "-b"], workdir="C:\\w s")
        self.assertEqual(argv, ["h", "--model=-p", "--provider=x y", "--skills=a", "--skills=-b",
                                "--in=C:\\w s", "--oneshot=q"])


# ---------------------------------------------------------------------------
# The commands, through argparse the way Hermes calls them
# ---------------------------------------------------------------------------

WARNING = "this cassette may not replay cleanly: 1 tool call(s) ran live and were not recorded (terminal x1)"


class Commands(CliCase):
    def test_run_prints_the_verdict_and_returns_its_exit_code(self):
        self.make_cassette(warnings=[WARNING])
        with self.plan():
            code, out, _ = self.run_cli("run", "auth-port", "--model", "gpt-oss:20b")
        self.assertEqual(code, cli.EXIT_PASS, out)
        self.assertIn("PASS", out)
        self.assertIn("2/2", out)
        self.assertIn(WARNING, out)
        self.assertIn("saved", out)
        with self.plan(answer="nothing useful"):
            code, out, _ = self.run_cli("run", "auth-port")
        self.assertEqual(code, cli.EXIT_FAIL)
        with self.plan(calls=[["write_file", {"path": "x"}]]):
            code, out, _ = self.run_cli("run", "auth-port")
        self.assertEqual(code, cli.EXIT_DIVERGED)
        self.assert_nothing_ran_live()
        # Unclaimed: the session is not bound, so its tools run live. That is
        # exactly why the run is INVALID and never PASS.
        with self.plan(message="other"):
            code, out, _ = self.run_cli("run", "auth-port")
        self.assertEqual(code, cli.EXIT_INVALID)
        self.assertIn("INVALID", out)
        self.assertTrue(self.live_log.exists())

    def test_run_errors_exit_2(self):
        code, _, err = self.run_cli("run", "missing")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("no cassette named", err)

    def test_pin_and_unpin(self):
        self.make_cassette(pins=(), warnings=[WARNING])
        code, out, _ = self.run_cli("pin", "auth-port", "binds", "8080")
        self.assertEqual(code, 0)
        self.assertIn("binds 8080", out)
        self.assertIn(WARNING, out)
        code, out, _ = self.run_cli("pin", "auth-port", "--", "-8080")
        self.assertEqual(code, 0)
        self.assertEqual(store.CassetteStore(self.data).load("auth-port").pins, ["binds 8080", "-8080"])
        code, out, _ = self.run_cli("pin", "auth-port", "--remove", "binds 8080")
        self.assertEqual(code, 0)
        self.assertEqual(store.CassetteStore(self.data).load("auth-port").pins, ["-8080"])
        code, _, err = self.run_cli("pin", "auth-port", "--remove", "nope")
        self.assertEqual(code, cli.EXIT_ERROR)
        code, _, err = self.run_cli("pin", "auth-port", "re:([")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("regex", err)

    def test_list(self):
        code, out, _ = self.run_cli("list")
        self.assertEqual((code, "no cassettes yet" in out), (0, True))
        self.make_cassette(warnings=[WARNING])
        self.make_cassette(name="unpinned", pins=())
        (self.data / "cassettes" / "broken.json").write_text("{", encoding="utf-8")
        with self.plan():
            self.replay()
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("CASSETTE"))
        row = next(line for line in lines if line.startswith("auth-port"))
        self.assertIn("PASS 2/2", row)
        self.assertIn("gpt-oss:20b", row)
        self.assertIn("never run", next(line for line in lines if line.startswith("unpinned")))
        self.assertIn("unreadable", next(line for line in lines if line.startswith("broken")))
        self.assertIn(f"auth-port: warning: {WARNING}", out)
        self.assertIn("unpinned: no pins yet", out)

    def test_show(self):
        self.make_cassette(warnings=[WARNING])
        with self.plan():
            self.replay()
        code, out, _ = self.run_cli("show", "auth-port")
        self.assertEqual(code, 0)
        for text in (REQUEST, "server/auth.py", "read_file", WARNING, "last runs", "PASS"):
            self.assertIn(text, out)
        code, _, err = self.run_cli("show", "nope")
        self.assertEqual(code, cli.EXIT_ERROR)

    def test_show_cuts_long_text_unless_full(self):
        c = self.make_cassette()
        c.final_answer = "x" * 5000
        store.CassetteStore(self.data).save(c)
        _, out, _ = self.run_cli("show", "auth-port")
        self.assertIn("--full", out)
        self.assertNotIn("x" * 5000, out)
        _, out, _ = self.run_cli("show", "auth-port", "--full")
        self.assertIn("x" * 5000, out)

    def test_record_prints_the_in_session_steps(self):
        code, out, _ = self.run_cli("record", "auth-port")
        self.assertEqual(code, 0)
        self.assertIn("/cassette record auth-port", out)
        self.assertNotIn("--overwrite", out)
        self.make_cassette()
        _, out, _ = self.run_cli("record", "auth-port")
        self.assertIn("/cassette record auth-port --overwrite", out)
        code, _, err = self.run_cli("record", "con")
        self.assertEqual(code, cli.EXIT_ERROR)

    def test_no_subcommand_prints_help(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("run", out)

    def test_flags_do_not_collide_with_hermes_top_level_flags(self):
        # Hermes's top-level parser has -m/--model, --provider, -s/--skills and --in.
        top = argparse.ArgumentParser(prog="hermes")
        top.add_argument("-m", "--model", default=None)
        top.add_argument("--provider", default=None)
        top.add_argument("-s", "--skills", action="append", default=None)
        top.add_argument("--in", dest="in_dir", default=None)
        sub = top.add_subparsers(dest="command")
        cli.setup(sub.add_parser("cassette"))
        ns = top.parse_args(["cassette", "run", "x", "-m", "gpt-oss:20b", "--provider", "ollama",
                             "-s", "a", "--skill", "b", "--in", "C:\\w", "--timeout", "30"])
        self.assertEqual((ns.cc_model, ns.cc_provider, ns.cc_skills, ns.cc_in, ns.timeout),
                         ("gpt-oss:20b", "ollama", ["a", "b"], "C:\\w", 30.0))
        self.assertEqual((ns.model, ns.provider, ns.skills, ns.in_dir), (None, None, None, None))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            top.parse_args(["cassette", "run", "x", "--timeout", "0"])

    def test_registered_with_hermes(self):
        seen = {}

        class Ctx:
            def register_hook(self, *a, **kw): pass
            def register_middleware(self, *a, **kw): pass
            def register_command(self, *a, **kw): pass

            def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
                seen.update(name=name, setup=setup_fn, handler=handler_fn)

        plugin.register(Ctx())
        self.assertEqual(seen["name"], "cassette")
        self.assertIs(seen["setup"], cli.setup)
        self.assertIs(seen["handler"], cli.handle)


class RunHistory(CliCase):
    def test_load_runs_skips_junk_and_sorts_newest_first(self):
        for i, result in enumerate(["FAIL", "PASS"]):
            cli.save_run(self.data, "auth-port", {"result": result}, now=1_700_000_000 + i)
        folder = self.data / "runs" / "auth-port"
        (folder / "notes.json").write_text("{}", encoding="utf-8")
        (folder / "20990101T0000000000Z-00000000.json").write_text("{", encoding="utf-8")
        self.assertEqual([r["result"] for r in cli.load_runs(self.data, "auth-port")], ["PASS", "FAIL"])
        self.assertEqual(len(cli.load_runs(self.data, "auth-port", limit=1)), 1)
        self.assertEqual(cli.load_runs(self.data, "never"), [])

    def test_run_paths_stay_inside_the_data_folder(self):
        for bad in ("../x", "..\\x", "C:\\x", "con", ""):
            with self.subTest(name=bad), self.assertRaises(store.CassetteError):
                cli.save_run(self.data, bad, {"result": "PASS"})


if __name__ == "__main__":
    unittest.main()
