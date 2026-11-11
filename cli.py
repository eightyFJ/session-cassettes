"""``hermes cassette ...``: the terminal side of Session Cassettes.

    hermes cassette record <name>            how to record (it happens inside a session)
    hermes cassette pin <name> <fact>        add a fact the replayed answer must contain
    hermes cassette run <name> [--model M]   replay the recorded request, print the verdict
    hermes cassette list                     cassettes, pins and last verdict
    hermes cassette show <name>              what a cassette holds

This module runs in the user's terminal process, not in the gateway. ``run``
meets the gateway's hooks through the arm and ledger files (arms.py):

1. check that the gateway has this plugin loaded, with the same code;
2. write ``armed/<id>.json`` with the recorded request (one run at a time);
3. start ``hermes chat -Q --query=<request>`` as a child process, with an
   argument list and no shell. The gateway session whose first message is
   that request claims the arm, and every tool call of its turn is answered
   from the cassette;
4. read the ledger the hooks wrote, check that the claim belongs to the child,
   grade the answer with ``verdict.compute_verdict`` and save the result to
   ``runs/<cassette>/<timestamp>.json``;
5. remove the arm files, whatever happened.

Hermes is imported inside functions only, so the package also loads where
Hermes is not importable (``hermes plugins validate``).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from . import arms, store, verdict

PLUGIN_NAME = "session-cassettes"
RUNS_DIR = "runs"
RUN_FORMAT = 1

#: Exit codes of ``hermes cassette run``. Other commands use 0 and EXIT_ERROR.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2
EXIT_DIVERGED = 3
EXIT_INVALID = 4
_EXIT_FOR = {
    verdict.PASS: EXIT_PASS,
    verdict.FAIL: EXIT_FAIL,
    verdict.DIVERGED: EXIT_DIVERGED,
    verdict.INVALID: EXIT_INVALID,
}

DEFAULT_TIMEOUT_SECONDS = 900.0
PING_TIMEOUT_SECONDS = 120.0
#: After the child exits, how long to wait for the hooks' ``done`` ledger.
LEDGER_WAIT_SECONDS = 20.0
_LEDGER_POLL_SECONDS = 0.1
_STDERR_TAIL_LINES = 15
MAX_CHILD_OUTPUT_BYTES = 16 * 1024 * 1024
_ANSWER_PREVIEW_CHARS = 800
_KEY_PREVIEW_CHARS = 110

_RUN_FILE_RE = re.compile(r"\d{8}T\d{6}\d{3}Z-[0-9a-f]{8}")

_EPILOG = """\
exit codes of 'run': 0 PASS, 1 FAIL, 2 error (nothing was graded),
                     3 DIVERGED, 4 INVALID

Recording happens inside a Hermes session: type /cassette record <name>,
then send the request. 'hermes cassette record <name>' prints the steps."""


class CliError(Exception):
    """A user-facing error that ends a command with EXIT_ERROR."""


# ---------------------------------------------------------------------------
# Hermes lookups
# ---------------------------------------------------------------------------


def _hermes_data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME)


def find_hermes() -> list:
    """The command that starts Hermes, as an argument-list prefix.

    Only a real executable is accepted. Windows runs a ``.bat`` or ``.cmd``
    wrapper through ``cmd.exe``, which would re-parse the request and expand
    ``%VAR%`` and ``&`` in it. The ``hermes`` running this command is
    preferred, so the child uses the same install.
    """
    windows = os.name == "nt"
    candidates = []
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.stem.lower() == "hermes":
        candidates.append(str(argv0))
    found = shutil.which("hermes")
    if found:
        candidates.append(found)
    for candidate in candidates:
        path = Path(candidate)
        suffix = path.suffix.lower()
        if not path.is_file():
            continue
        if windows and suffix != ".exe":
            continue
        if not windows and suffix in (".bat", ".cmd"):
            continue
        return [str(path.resolve())]
    raise CliError(
        "cannot find the hermes executable on PATH"
        + (" (a .bat or .cmd wrapper is not used: cmd.exe would re-read the request)" if windows else "")
    )


# ---------------------------------------------------------------------------
# The child process
# ---------------------------------------------------------------------------


def build_child_argv(
    hermes: Sequence[str],
    request: str,
    *,
    model: str = "",
    provider: str = "",
    skills: Sequence[str] = (),
    workdir: str = "",
) -> list:
    """Arguments for the replay child, ``hermes chat -Q --query=<request>``.

    Every value is joined to its flag with ``=``, so a request that starts
    with ``-`` or contains a flag stays one unchanged argument.

    The child is never ``hermes -z``, which auto-approves dangerous commands.
    ``chat -Q`` denies them under Hermes's default approval settings, so a
    turn that runs live because its claim failed gets no approval the user
    did not give."""
    if not isinstance(request, str) or not request:
        raise CliError("the cassette has no request to replay")
    argv = list(hermes)
    if model:
        argv.append(f"--model={model}")
    if provider:
        argv.append(f"--provider={provider}")
    for skill in skills:
        argv.append(f"--skills={skill}")
    if workdir:
        argv.append(f"--in={workdir}")
    argv += ["chat", "-Q", f"--query={request}"]
    return argv


def replay_refusal(request: Any) -> str:
    """Why a cassette's request can't be replayed safely, or "" if it can.
    A request that Hermes rewrites (an @ reference) or handles itself (a slash
    command) would never match its arm, so the turn would run live."""
    if not isinstance(request, str) or not arms.normalize_request(request):
        return "the cassette has no request to replay"
    text = arms.normalize_request(request)
    if text.startswith(("/", "!")):
        return "the cassette's request starts with '/' or '!', which Hermes treats as a command"
    why = arms.request_refusal(request)
    return f"the cassette's request can't be replayed: {why}" if why else ""


class ChildResult:
    def __init__(self, returncode: Optional[int], stdout: str, stderr: str, timed_out: bool, seconds: float):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.seconds = seconds


def _decode(data: Any) -> str:
    if isinstance(data, str):
        return data
    if not data:
        return ""
    return bytes(data).decode("utf-8", errors="replace")


def _read_back(fh: Any, tail: bool = False) -> str:
    """Up to MAX_CHILD_OUTPUT_BYTES of the child's output: the start, or with
    ``tail`` the end (stderr: the session id line comes last)."""
    fh.seek(0, os.SEEK_END)
    size = fh.tell()
    fh.seek(max(0, size - MAX_CHILD_OUTPUT_BYTES) if tail else 0)
    return _decode(fh.read(MAX_CHILD_OUTPUT_BYTES))


def spawn(argv: Sequence[str], *, timeout: float, cwd: Optional[str] = None) -> ChildResult:
    """Run the child with an argument list and no shell. Its stdin is empty,
    so nothing can wait on a prompt. On timeout or Ctrl+C it is killed.

    Output goes to temporary files rather than pipes: a pipe read blocks until
    every process holding the pipe exits, and a launcher's child can outlive
    the launcher."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # the answer is compared character for character
    # HERMES_YOLO_MODE would make the child a yolo session; a replay never inherits it.
    for name in [k for k in env if k.upper() == "HERMES_YOLO_MODE"]:
        del env[name]
    started = time.monotonic()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                cwd=cwd or None,
                env=env,
                shell=False,
            )
        except OSError as exc:
            raise CliError(f"cannot start hermes: {exc}") from None
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        except BaseException:  # Ctrl+C: don't leave the child running
            proc.kill()
            raise
        return ChildResult(None if timed_out else proc.returncode, _read_back(out), _read_back(err, tail=True),
                           timed_out, time.monotonic() - started)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _out(text: str = "", stream: Any = None) -> None:
    """Print without dying on a console that can't show a character."""
    stream = stream if stream is not None else sys.stdout
    try:
        print(text, file=stream)
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", None) or "ascii"
        print(text.encode(enc, errors="backslashreplace").decode(enc, errors="replace"), file=stream)


def _err(text: str) -> None:
    _out(text, sys.stderr)


def _clip(text: str, limit: int) -> str:
    text = text.replace("\r\n", "\n")
    return text if len(text) <= limit else text[:limit] + "..."


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.replace("\r\n", "\n").split("\n"))


def _print_warnings(cassette: store.Cassette) -> None:
    for w in cassette.warnings:
        _out(f"warning: {w}")


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------


def _runs_dir(base: Path, name: str) -> Path:
    store.validate_name(name)
    return store.safe_join(base, RUNS_DIR, name)


def save_run(base: Path, name: str, record: dict, now: Optional[float] = None) -> Path:
    store.validate_name(name)
    now = time.time() if now is None else now
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)) + f"{int((now % 1) * 1000):03d}Z"
    path = store.safe_join(base, RUNS_DIR, name, f"{stamp}-{uuid.uuid4().hex[:8]}.json")
    store.atomic_write_json(path, record)
    return path


def load_runs(base: Path, name: str, limit: int = 0) -> list:
    """The cassette's saved runs, newest first. Unreadable files are skipped."""
    try:
        folder = _runs_dir(base, name)
        entries = sorted(
            (p for p in folder.iterdir() if p.suffix == ".json" and _RUN_FILE_RE.fullmatch(p.stem)),
            key=lambda p: p.name,
            reverse=True,
        )
    except (OSError, store.CassetteError):
        return []
    out = []
    for p in entries:
        try:
            data = store.read_json_file(p)
        except store.CassetteError:
            continue
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            out.append(data)
            if limit and len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _load(base: Path, name: str) -> store.Cassette:
    cassettes = store.CassetteStore(base)
    if not cassettes.exists(name):
        raise CliError(f"no cassette named {name!r}. 'hermes cassette list' shows them.")
    return cassettes.load(name)


def cmd_record(args: argparse.Namespace, base: Path) -> int:
    name = store.validate_name(args.name)
    exists = store.CassetteStore(base).exists(name)
    flag = " --overwrite" if exists else ""
    lines = [
        "Recording happens inside a Hermes session, because only the session knows which",
        "tool calls belong to it. In a terminal:",
        "",
        "  1. cd into the folder the request is about (the cassette remembers it), and run hermes.",
        f"  2. Type:  /cassette record {name}{flag}",
        "  3. Send the request as plain text, with no @ references. That one turn is recorded.",
        f"  4. Check it:  /cassette status   or   hermes cassette show {name}",
        f"  5. Pin the facts a good answer must contain:  hermes cassette pin {name} \"<fact>\"",
        f"  6. Replay it:  hermes cassette run {name} --model <model>",
    ]
    if exists:
        lines += ["", f"Note: {name!r} already exists. --overwrite replaces it, pins included."]
    _out("\n".join(lines))
    return 0


def cmd_pin(args: argparse.Namespace, base: Path) -> int:
    name = store.validate_name(args.name)
    fact = " ".join(args.fact).strip()
    cassettes = store.CassetteStore(base)
    cassette = _load(base, name)
    if args.remove:
        wanted = fact
        if wanted not in cassette.pins:
            raise CliError(f"{name!r} has no pin {wanted!r}. Pins: "
                           + (", ".join(repr(p) for p in cassette.pins) or "none"))
        cassette.pins.remove(wanted)
        cassettes.save(cassette)
        _out(f"removed pin from {name!r}: {wanted}   ({len(cassette.pins)} pin(s) left)")
    else:
        pin = cassette.add_pin(fact)
        cassettes.save(cassette)
        _out(f"pinned to {name!r}: {pin}   ({len(cassette.pins)} pin(s) in all)")
    for p in cassette.pins:
        _out(f"  - {p}")
    _print_warnings(cassette)
    return 0


def cmd_list(args: argparse.Namespace, base: Path) -> int:
    cassettes = store.CassetteStore(base)
    names = cassettes.names()
    if not names:
        _out("no cassettes yet. 'hermes cassette record <name>' shows how to make one.")
        return 0
    colour = verdict.use_colour()
    headers = ["CASSETTE", "CALLS", "FILES", "PINS", "LAST RUN", "MODEL"]
    rows, results, notes = [], [], []
    for name in names:
        try:
            c = cassettes.load(name)
        except store.CassetteError as exc:
            rows.append([name, "-", "-", "-", "unreadable", "-"])
            results.append("")
            notes.append(f"{name}: {exc}")
            continue
        last = load_runs(base, name, limit=1)
        if last:
            run = last[0]
            v = run.get("verdict") if isinstance(run.get("verdict"), dict) else {}
            pins = v.get("pins") if isinstance(v.get("pins"), list) else []
            matched = sum(1 for p in pins if isinstance(p, dict) and p.get("matched"))
            last_run = f"{run['result']} {matched}/{len(pins)}"
            model = str(run.get("model_seen") or run.get("model") or "-")
            results.append(run["result"])
        else:
            last_run, model = "never run", "-"
            results.append("")
        rows.append([name, str(len(c.calls)), str(len(c.snapshot)), str(len(c.pins)), last_run, model])
        notes += [f"{name}: warning: {w}" for w in c.warnings]
        if not c.pins:
            notes.append(f"{name}: no pins yet, so it can't be run")
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def fmt(cells: list, result: str = "") -> str:
        out = []
        for i, cell in enumerate(cells):
            padded = cell.ljust(widths[i]) if i < len(cells) - 1 else cell
            out.append(verdict.paint(padded, result, colour) if i == 4 and result else padded)
        return "  ".join(out).rstrip()

    _out(fmt(headers))
    for row, result in zip(rows, results):
        _out(fmt(row, result))
    if notes:
        _out()
        for n in notes:
            _out(n)
    return 0


def cmd_show(args: argparse.Namespace, base: Path) -> int:
    name = store.validate_name(args.name)
    c = _load(base, name)
    full = args.full
    clip = (lambda s, n: s.replace("\r\n", "\n")) if full else _clip
    lines = [
        f"cassette  {c.name}",
        f"file      {store.CassetteStore(base).path_for(name)}",
        f"created   {c.created or '-'}",
        f"model     {c.model or '-'}",
        f"cwd       {c.cwd or '-'}",
        f"workspace {c.workspace or '-'}",
        "request",
        _indent(clip(c.request, _ANSWER_PREVIEW_CHARS)),
        "recorded answer",
        _indent(clip(c.final_answer, _ANSWER_PREVIEW_CHARS) or "(none)"),
        f"pins ({len(c.pins)})",
    ]
    lines += [f"    - {p}" for p in c.pins] or ["    (none: 'hermes cassette pin' adds one)"]
    lines.append(f"recorded calls ({len(c.calls)})")
    for call in c.calls:
        lines.append(f"    {call.seq:>3}. {clip(call.key, _KEY_PREVIEW_CHARS)}  -> {len(call.result)} chars")
    if c.unrecorded:
        lines.append(f"ran live, not recorded ({len(c.unrecorded)})")
        for u in c.unrecorded:
            lines.append(f"    {u['seq']:>3}. {u['tool']}")
    lines.append(f"snapshot files ({len(c.snapshot)})")
    for entry in sorted(c.snapshot.values(), key=lambda e: e["path"].lower()):
        lines.append(f"    {entry['path']}  ({entry['size']} bytes)")
    if c.warnings:
        lines.append(f"warnings ({len(c.warnings)})")
        lines += [f"    {w}" for w in c.warnings]
    runs = load_runs(base, name, limit=5)
    if runs:
        lines.append("last runs")
        for run in runs:
            lines.append(f"    {run.get('started', '?')}  {run['result']:<8}  "
                         f"{run.get('model_seen') or run.get('model') or '-'}")
    _out("\n".join(lines))
    if not full and (len(c.request) > _ANSWER_PREVIEW_CHARS or len(c.final_answer) > _ANSWER_PREVIEW_CHARS):
        _out("(cut short; --full shows everything)")
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _wait_for_ledger(base: Path, arm_id: str, wait: float, sleep: Callable[[float], None]) -> tuple:
    """``(ledger or None, error text)``. Waits up to ``wait`` seconds for
    ``state == done``; returns the last readable ledger if it never gets there."""
    deadline = time.monotonic() + wait
    last, error = None, ""
    while True:
        try:
            last = arms.read_ledger(base, arm_id)
            error = ""
            if last["state"] == arms.DONE:
                return last, ""
        except store.CassetteError as exc:
            error = str(exc)
        if time.monotonic() >= deadline:
            return last, error
        sleep(_LEDGER_POLL_SECONDS)


def preflight(base: Path, hermes: Sequence[str], workdir: str, *,
              run_child: Callable[..., ChildResult] = None, timeout: float = 120.0) -> dict:
    """Check, before the request is sent, that the gateway that will run it has
    this plugin loaded, with the same code as this folder, and does not
    auto-approve commands.

    Sends ``/cassette ping <nonce>`` through ``hermes chat -Q``. The plugin's
    handler in the gateway answers by writing ``pongs/<nonce>.json``; no model
    is called. Without a matching pong the hooks could not claim the run and
    its tools would run live, so nothing is replayed. Returns the pong or
    raises CliError."""
    run_child = run_child or spawn
    nonce = uuid.uuid4().hex
    argv = list(hermes) + ([f"--in={workdir}"] if workdir else []) + [
        "chat", "-Q", f"--query=/cassette ping {nonce}"]
    restart = ("Run 'hermes gateway restart' (and check 'hermes plugins list' shows "
               "session-cassettes enabled), then try again. Nothing was replayed.")
    try:
        child = run_child(argv, timeout=timeout, cwd=workdir or None)
        pong = arms.read_pong(base, nonce)
    finally:
        arms.discard_pong(base, nonce)
    if pong is None:
        why = "it timed out" if child.timed_out else "no answer came back"
        raise CliError(f"the Hermes gateway did not answer for session-cassettes ({why}), so it "
                       f"may not have the plugin loaded and a replay could run tools live. {restart}")
    if pong["version"] != arms.CODE_VERSION:
        raise CliError(f"the Hermes gateway runs a different version of session-cassettes "
                       f"({pong['version']}) than this folder ({arms.CODE_VERSION}). {restart}")
    if pong["yolo"]:
        # The gateway's yolo mode covers every session it hosts; the child's
        # environment cannot undo it.
        raise CliError("the Hermes gateway was started with HERMES_YOLO_MODE set, so it approves "
                       "dangerous commands for every session, and a replay whose claim failed would "
                       "run them. Restart the gateway from a shell without HERMES_YOLO_MODE "
                       "('hermes gateway restart'), then try again. Nothing was replayed.")
    return pong


_SESSION_LINE_RE = re.compile(r"^session_id:\s*(\S+)\s*$", re.MULTILINE)


def _child_session_id(stderr: str) -> str:
    """The session id ``hermes chat -Q`` prints on stderr, or "" if none."""
    found = _SESSION_LINE_RE.findall(stderr or "")
    return found[-1] if found else ""


def _gateway_redact(text: str) -> str:
    """The answer as Hermes's redaction prints it. ``hermes chat -Q`` can print
    the gateway's stored copy of the answer, which is redacted, while the
    hooks see the original. Returns the text unchanged if Hermes isn't
    importable."""
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text)
    except Exception:
        return text


def _same_answer(ledger_answer: str, stdout: str, redact: Callable[[str], str] = _gateway_redact) -> bool:
    printed = arms.normalize_request(stdout)
    if arms.normalize_request(ledger_answer) == printed:
        return True
    return arms.normalize_request(redact(ledger_answer)) == printed


def replay(
    base: Path,
    name: str,
    *,
    model: str = "",
    provider: str = "",
    skills: Sequence[str] = (),
    workdir: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    hermes: Optional[Sequence[str]] = None,
    run_child: Callable[..., ChildResult] = spawn,
    ledger_wait: float = LEDGER_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = _out,
) -> tuple:
    """Replay one cassette. Returns ``(Verdict, run record, run file path)``.
    Raises CliError (or CassetteError) when nothing could be graded."""
    name = store.validate_name(name)
    cassette = _load(base, name)
    if not cassette.pins:
        raise CliError(f"{name!r} has no pins, so there is nothing to grade. "
                       f"Add one: hermes cassette pin {name} \"<fact>\"")
    for w in cassette.warnings:
        log(f"warning: {w}")
    if not workdir:
        workdir = cassette.cwd
        if not workdir:
            raise CliError(f"{name!r} has no recorded working directory; pass --in <folder>")
    if not Path(workdir).is_dir():
        raise CliError(f"the working directory \"{workdir}\" does not exist; pass --in <folder>")
    refusal = replay_refusal(cassette.request)
    if refusal:
        raise CliError(refusal)
    hermes = list(hermes) if hermes else find_hermes()
    argv = build_child_argv(hermes, cassette.request, model=model, provider=provider,
                            skills=skills, workdir=workdir)

    # Ping before arming, so no other session can claim the arm during the wait.
    preflight(base, hermes, workdir, run_child=run_child, timeout=min(timeout, PING_TIMEOUT_SECONDS))
    arm = arms.create_arm(base, name, cassette.request, model=model)
    try:
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        log(f"replaying {name!r} with {model or 'the default model'} in {workdir}")
        log("every tool call of the replayed turn is answered from the cassette; no tool runs. "
            f"waiting for hermes (timeout {int(timeout)}s)...")
        child = run_child(argv, timeout=timeout, cwd=workdir)
        reasons: list = []
        if child.timed_out:
            reasons.append(f"hermes did not finish within {int(timeout)}s and was stopped")
        elif child.returncode != 0:
            reasons.append(f"hermes exited with code {child.returncode}")
        ledger, ledger_error = None, ""
        armed_left = arms.arm_path(base, arm["id"]).exists()
        claimed = arms.claimed_path(base, arm["id"]).exists()
        if armed_left and not claimed:
            reasons.append("no session claimed this run: the arm file is left over, "
                           "so the turn may have run with live tools")
        else:
            if armed_left:
                reasons.append("the arm file was left behind after its claim")
            ledger, ledger_error = _wait_for_ledger(base, arm["id"], 0 if child.timed_out else ledger_wait,
                                                    sleep)
            if ledger is None:
                reasons.append(f"no run ledger was written ({ledger_error or 'unknown error'})")
            elif ledger["state"] != arms.DONE:
                reasons.append(f"the replayed turn did not finish (ledger state {ledger['state'] or 'empty'!r})")
        events: list = []
        answer = child.stdout
        if ledger is not None:
            reasons += ledger["invalid_reasons"]
            events = ledger["events"]
            if ledger["state"] == arms.DONE:
                answer = ledger["final_answer"]
                if not child.timed_out and not _same_answer(ledger["final_answer"], child.stdout):
                    reasons.append("the claiming session's final answer is not what hermes printed, "
                                   "so the claim may belong to another session")
            # The session id ties the claim to this child; the answer check
            # alone cannot, because it also accepts a redacted copy.
            child_session = _child_session_id(child.stderr)
            if not child.timed_out:
                if not child_session:
                    reasons.append("hermes printed no session id, so the claim can't be tied to this run")
                elif not ledger["session_id"]:
                    reasons.append("the ledger names no session, so the claim can't be tied to this run")
                elif child_session != ledger["session_id"]:
                    reasons.append(f"hermes ran session {child_session}, but session "
                                   f"{ledger['session_id']} claimed this run")
        v = verdict.compute_verdict(cassette.pins, answer, events, [c.key for c in cassette.calls], reasons)
        model_seen = ledger["model_seen"] if ledger is not None else ""
        if model and model_seen and model_seen != model and not model_seen.endswith(model):
            v.notes.append(f"asked for model {model!r}, the session reported {model_seen!r}")
        record = {
            "format": RUN_FORMAT,
            "cassette": name,
            "started": started_at,
            "seconds": round(child.seconds, 2),
            "model": model,
            "provider": provider,
            "skills": list(skills),
            "workdir": workdir,
            "exit_code": child.returncode,
            "timed_out": child.timed_out,
            "result": v.result,
            "verdict": v.to_dict(),
            "answer": answer,
            "session_id": ledger["session_id"] if ledger is not None else "",
            "turn_id": ledger["turn_id"] if ledger is not None else "",
            "model_seen": model_seen,
            "refused_calls": ledger["refused_calls"] if ledger is not None else 0,
        }
        if (child.timed_out or child.returncode != 0) and child.stderr.strip():
            tail = child.stderr.strip().splitlines()[-_STDERR_TAIL_LINES:]
            record["stderr_tail"] = tail
        path = save_run(base, name, record)
        return v, record, path
    finally:
        arms.discard(base, arm["id"])


def cmd_run(args: argparse.Namespace, base: Path) -> int:
    v, record, path = replay(
        base,
        args.name,
        model=args.cc_model or "",
        provider=args.cc_provider or "",
        skills=args.cc_skills or [],
        workdir=args.cc_in or "",
        timeout=args.timeout,
    )
    model = record["model_seen"] or record["model"] or "default model"
    _out()
    _out(verdict.render_verdict(v, title=f"{record['cassette']} with {model} ({record['seconds']:.0f}s)"))
    if record.get("refused_calls"):
        _out(f"  note    {record['refused_calls']} call(s) from other turns of the session were refused")
    for line in record.get("stderr_tail", []):
        _out(f"  stderr  {line}")
    _out(f"  saved   {path}")
    return _EXIT_FOR.get(v.result, EXIT_ERROR)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number of seconds") from None
    if not (0 < value <= 24 * 3600):
        raise argparse.ArgumentTypeError("must be between 0 and 86400 seconds")
    return value


def setup(parser: argparse.ArgumentParser) -> None:
    """Build ``hermes cassette``'s argument tree. Destinations carry a ``cc_``
    prefix where Hermes's top-level parser has the same flag (``--model``,
    ``--provider``, ``--skills``, ``--in``), so the two never overwrite each other."""
    parser.epilog = _EPILOG
    sub = parser.add_subparsers(dest="cassette_command", metavar="<command>")

    p = sub.add_parser("record", help="how to record a cassette (it happens inside a session)")
    p.add_argument("name")

    p = sub.add_parser("pin", help="add a fact the replayed answer must contain",
                       description="Add a pinned fact. Matching ignores case and treats \\ and / as "
                                   "the same. A fact starting with re: is a regular expression. "
                                   "Put -- before a fact that starts with '-'.")
    p.add_argument("name")
    p.add_argument("fact", nargs="+")
    p.add_argument("--remove", action="store_true", help="remove this pin instead")

    p = sub.add_parser("run", help="replay the recorded request and print the verdict",
                       description="Replay a cassette's request in a new 'hermes chat -Q' session. Every "
                                   "tool call of that turn is answered from the cassette, so no tool "
                                   "runs. The verdict checks the answer against the pins.",
                       epilog=_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name")
    p.add_argument("--model", "-m", dest="cc_model", default=None, help="model for the replay")
    p.add_argument("--provider", dest="cc_provider", default=None, help="provider for the replay")
    p.add_argument("--skill", "--skills", "-s", dest="cc_skills", action="append", default=None,
                   help="preload a skill for the replay (repeatable)")
    p.add_argument("--in", dest="cc_in", default=None, metavar="DIR",
                   help="working directory for the replay (default: the recorded one)")
    p.add_argument("--timeout", type=_positive_seconds, default=DEFAULT_TIMEOUT_SECONDS,
                   help=f"seconds before the replay is stopped (default {int(DEFAULT_TIMEOUT_SECONDS)})")

    sub.add_parser("list", help="cassettes, pins and last verdict")

    p = sub.add_parser("show", help="what a cassette holds")
    p.add_argument("name")
    p.add_argument("--full", action="store_true", help="don't cut long text short")

    parser.set_defaults(_cassette_parser=parser)


_COMMANDS = {"record": cmd_record, "pin": cmd_pin, "run": cmd_run, "list": cmd_list, "show": cmd_show}


def handle(args: argparse.Namespace, data_dir: Optional[Callable[[], Any]] = None) -> int:
    """Entry point for ``hermes cassette``. Returns the exit code."""
    command = _COMMANDS.get(getattr(args, "cassette_command", None) or "")
    if command is None:
        parser = getattr(args, "_cassette_parser", None)
        if parser is not None:
            parser.print_help()
        else:
            _out("usage: hermes cassette {record,pin,run,list,show} ...")
        return EXIT_ERROR
    try:
        base = Path((data_dir or _hermes_data_dir)())
        return command(args, base)
    except (CliError, store.CassetteError, verdict.VerdictError) as exc:
        _err(f"cassette: {exc}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        _err("cassette: interrupted")
        return 130
