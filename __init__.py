"""Session Cassettes: record a session's read-only tool results, then replay
them offline against another model, prompt or skill.

Everything in this module runs inside the Hermes gateway process, for every
session that process hosts. So all state is keyed by session id, and a session
this plugin has not bound is never touched: its tool calls go straight to
``next_call``.

A *binding* ties one mode to one turn of one session (docs/PLAN.md, decision 9):

record   ``/cassette record <name>`` arms the session. Its next turn is
         recorded: recordable tools run for real and their results are kept.
replay   ``hermes cassette run`` writes an arm file (arms.py). The first turn
         of the CLI session whose message equals the recorded request claims
         it. Every tool call of that turn is answered from the cassette, or
         refused with a ``cassette miss``. ``next_call`` is never called, so
         no real tool runs and no approval prompt can come up.

Rules the replay path keeps (SEC-7):
- No exception leaves the middleware callback for a replay-bound call. Hermes
  treats a raising middleware as a broken plugin and runs the real tool.
- A call on a replay session from any other turn (the background-review fork
  shares the session id) is refused, never run, and is not a cassette miss.
- A binding lasts until the session's next ``pre_llm_call``. Nothing is
  released on ``post_llm_call``, and nothing expires while a turn runs.

Hermes imports happen inside functions: ``hermes plugins validate`` imports
this module in a bare interpreter, where Hermes is not importable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

from . import arms, cli, snapshot, store

logger = logging.getLogger(__name__)

PLUGIN_NAME = "session-cassettes"

#: Tools whose results are recorded (docs/PLAN.md, decision 2). Any other tool
#: runs live while recording and is noted by name only.
RECORDED_TOOLS = frozenset({
    "read_file", "search_files", "web_search", "web_extract",
    "skill_view", "skills_list", "session_search",
})

#: ``terminal`` is recorded only for these read-only commands, with no shell
#: operators, keyed on the exact command string plus working directory.
_TERMINAL_COMMANDS = frozenset({"cat", "type", "dir", "ls", "rg"})
_TERMINAL_GIT = frozenset({"log", "show", "diff"})
_SHELL_OPERATORS = frozenset(";&|<>`$()\n\r")

NOT_CLEAN = "this cassette may not replay cleanly"

MAX_RESULT_CHARS = 2 * 1024 * 1024      # one result bigger than this is not recorded
MAX_RECORDED_CHARS = 12 * 1024 * 1024   # all recorded results of one cassette
MAX_EVENTS = 5000                       # tool calls logged for one replay turn
MAX_BINDINGS = 64                       # sessions remembered at once
MAX_LOADED_REPLAYS = 16                 # unfinished replay turns that keep their cassette in memory
REPLAY_GRACE_SECONDS = 600.0            # a finished replay session keeps refusing at least this long
_MAX_TRACKED = 512                      # bound on each small bookkeeping table
# Tables whose eviction would weaken fail-closed: the live-call counts (an evicted
# count hides calls that ran before a late claim) and the retired replay turns
# (an evicted entry lets a late call of that turn pass through). One retired
# entry per replay run; a live-call entry per turn that ran tools, a few hundred bytes each.
_MAX_GUARDED = 10_000

_RECORD, _REPLAY = "record", "replay"
# _PASS: passed through and counted against its session; _PASS_UNCOUNTED: passed
# through without a session to count against (no id, or routing failed).
_PASS, _DO_RECORD, _DO_REPLAY, _DO_REFUSE, _PASS_UNCOUNTED = range(5)

_REFUSED = json.dumps({
    "error": "cassette replay: this call is outside the replayed turn, so it was refused. Nothing ran."
})
_STATIC_MISS = json.dumps({"error": "cassette miss"})

# An @ reference as Hermes spells it (agent/context_references.py): @diff,
# @staged, or @<kind>:<value>. Hermes expands these before plugins see the text.
_AT_REFERENCE_RE = re.compile(r"(?<![\w/])@(?:(?:diff|staged)\b|[A-Za-z][A-Za-z0-9_-]*:\S)")
_ATTACHED_CONTEXT_MARK = "--- Attached Context ---"

_USAGE = (
    "Session Cassettes\n"
    "  /cassette record <name> [--overwrite]   record this session's next message\n"
    "  /cassette stop                          cancel a recording that has not been saved\n"
    "  /cassette status                        what this session is doing\n"
    "  /cassette pin <name> <fact>             add a fact the replayed answer must contain\n"
    "  /cassette list                          cassettes on this profile\n"
    "Replay from a terminal: hermes cassette run <name> --model <model>"
)


# ---------------------------------------------------------------------------
# Hermes lookups. Lazy: Hermes is not importable where the validator runs.
# ---------------------------------------------------------------------------


def _hermes_data_dir():
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME)


def _hermes_redact(text: str, path: str) -> str:
    """Hermes's own redaction for file content shown to a model. The snapshot
    reads files from disk, so it gets the same treatment a live read_file gets.

    A live read_file also passes ``secret_file=True`` for the files Hermes
    treats as secret-bearing (``tools/file_tools.py``). Those are ``.env``-style
    and shell rc names, which the snapshot never captures (hidden paths), and a
    ``config.yaml`` under the Hermes home. The helper that tells a Hermes-home
    ``config.yaml`` apart is private, so every ``config.yaml`` (and the backup
    copies Hermes writes of it) gets the stricter mode: a project's own
    ``config.yaml`` may be over-redacted, never under-redacted."""
    from agent.redact import redact_sensitive_text

    base = str(path).replace("\\", "/").rsplit("/", 1)[-1].lower()
    secret_file = base == "config.yaml" or base.startswith(("config.yaml.good.", "config.yaml.corrupt."))
    return redact_sensitive_text(text, file_read=True, secret_file=secret_file)


def _hermes_session_key() -> str:
    from gateway.session_context import get_session_env

    return get_session_env("HERMES_SESSION_KEY", "") or ""


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


def _miss(tool_name: Any, args: Any, reason: str = "") -> str:
    """A ``cassette miss`` tool result. Never raises."""
    try:
        return store.miss_result(tool_name, args, reason)
    except Exception:
        return _STATIC_MISS


def _event_key(cwd: Optional[str], tool_name: Any, args: Any) -> str:
    try:
        return store.make_key(_text(tool_name), args, cwd)
    except Exception:
        return f"{tool_name if isinstance(tool_name, str) else '?'} <unprintable arguments>"


def _json_safe(args: Any) -> Optional[dict]:
    """A plain-JSON copy of a tool's arguments, or None if they have no such form."""
    try:
        clean = json.loads(json.dumps(args if isinstance(args, dict) else {}, default=str))
    except Exception:
        return None
    return clean if isinstance(clean, dict) else None


def _readonly_terminal(args: Any) -> bool:
    """True for a terminal call whose output is safe to record and replay: one
    command from the read-only list, in the foreground, with no shell operator
    that could chain, redirect or substitute another command."""
    if not isinstance(args, dict) or args.get("background") or args.get("pty"):
        return False
    command = args.get("command")
    if not isinstance(command, str) or any(ch in _SHELL_OPERATORS for ch in command):
        return False
    words = command.split()
    if not words:
        return False
    head = words[0].lower()
    if head in _TERMINAL_COMMANDS:
        return not (head == "rg" and any(w.startswith("--pre") for w in words[1:]))
    if head == "git" and len(words) > 1 and words[1].lower() in _TERMINAL_GIT:
        return not any(w.startswith("--output") for w in words[2:])
    return False


def _record_refusal(user_message: Any) -> str:
    """Why this message cannot be recorded, or "" if it can."""
    if not isinstance(user_message, str):
        return "the message is not plain text, and a replay can only send text."
    if not arms.normalize_request(user_message):
        return "the message is empty."
    if len(user_message) > arms.MAX_REQUEST_CHARS:
        return "the message is too long to replay."
    if _AT_REFERENCE_RE.search(user_message) or _ATTACHED_CONTEXT_MARK in user_message:
        return ("the message uses an @ reference. Hermes expands those before a plugin sees "
                "the text, so the request could not be replayed word for word.")
    return ""


def _trim(table: OrderedDict, limit: Optional[int] = None) -> None:
    limit = _MAX_TRACKED if limit is None else limit
    while len(table) > limit:
        table.popitem(last=False)


# ---------------------------------------------------------------------------
# Bindings
# ---------------------------------------------------------------------------


class _Binding:
    """One mode bound to one turn of one session."""

    def __init__(self, mode: str, session_id: str, turn_id: str, base_dir: Any, now: float):
        self.mode = mode
        self.session_id = session_id
        self.turn_id = turn_id
        self.base_dir = base_dir
        self.started = now
        self.lock = threading.Lock()
        self.finished = False
        self.finished_at = 0.0
        self.model = ""
        self.platform = ""
        self.cwd = ""
        # replay
        self.arm: Optional[dict] = None
        self.index: Any = None          # store.ReplayIndex once the cassette has loaded
        self.load_error = ""
        self.events: list = []          # (kind, key) per answered call, in order
        self.events_dropped = 0
        self.refused = 0                # calls from other turns of this session
        self.invalid: list = []         # reasons this run cannot be trusted
        self.final_answer = ""
        # The done ledger needs both halves: the claim's setup (its INVALID
        # reasons, the cassette load) and the end of the turn. They run on
        # different threads, so whichever finishes second writes it, once.
        self.setup_done = False
        self.ledger_final = False
        self.write_lock = threading.Lock()  # one ledger write at a time, never done -> claimed
        # record
        self.cassette: Any = None
        self.overwrite = False
        self.pending: Optional[dict] = None
        self.paths: list = []           # files the recorded results named
        self.live: dict = {}            # tool name -> calls that ran live, unrecorded
        self.terminal_tainted = False
        self.recorded_chars = 0
        self.oversize = 0


class Runtime:
    """The plugin's state and callbacks. One instance serves the whole gateway
    process. Tests build their own with fake lookups."""

    def __init__(
        self,
        data_dir: Optional[Callable[[], Any]] = None,
        session_cwd: Optional[Callable[[], Any]] = None,
        redact: Optional[Callable[[str, str], str]] = None,
        session_key: Optional[Callable[[], str]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self._data_dir = data_dir or _hermes_data_dir
        self._session_cwd = session_cwd
        self._redact = redact or _hermes_redact
        self._session_key = session_key or _hermes_session_key
        self._clock = clock
        self._lock = threading.Lock()
        self._bindings: dict = {}                 # session id -> _Binding
        self._replay_active = 0                   # replay bindings in _bindings
        self._pending: dict = {}                  # "id:<session id>" / "key:<session key>" -> record request
        self._status: OrderedDict = OrderedDict()  # session id -> last outcome, for /cassette status
        self._live_calls: OrderedDict = OrderedDict()  # (session id, turn id) -> tool calls passed through
        self._retired: OrderedDict = OrderedDict()     # (session id, turn id) of replaced replay bindings
        self._inflight: dict = {}                 # session id -> passed-through calls still running

    def attach(self, ctx: Any) -> None:
        """Take the session-cwd lookup from the plugin context. ``ctx.session_cwd()``
        is the working directory of the session a hook runs for, never the gateway's."""
        self._session_cwd = getattr(ctx, "session_cwd", None)

    # -- tool_execution middleware -------------------------------------------

    def tool_execution(self, tool_name: Any = "", args: Any = None, next_call: Any = None, **kwargs: Any) -> Any:
        try:
            binding, action = self._route(kwargs.get("session_id"), kwargs.get("turn_id"))
        except Exception:
            # Cannot tell whose call this is. While a replay is bound, refuse
            # rather than risk running a real tool for it.
            if self._replay_active:
                return _REFUSED
            binding, action = None, _PASS_UNCOUNTED
        if action == _DO_REPLAY or action == _DO_REFUSE:
            # Nothing below may raise or reach next_call: a raise here would
            # make Hermes run the real tool.
            try:
                if action == _DO_REPLAY:
                    return self._replay(binding, tool_name, args)
                return self._refuse(binding)
            except Exception:
                try:
                    binding.invalid.append("an internal error interrupted the replay of a tool call")
                except Exception:
                    pass
                return _STATIC_MISS
        if action == _DO_RECORD:
            return self._record(binding, tool_name, args, next_call)
        if action == _PASS:
            return self._pass_through(kwargs.get("session_id"), next_call, args)
        return next_call(args)

    def _pass_through(self, session_id: Any, next_call: Any, args: Any) -> Any:
        """Run a call that belongs to no binding. While it runs it is counted as
        in flight for its session, so a replay claim that lands meanwhile (a
        pre_llm_call that outlived Hermes's hook timeout) sees it and marks the
        run INVALID. A tool already running cannot be stopped; it can be reported."""
        sid = _text(session_id)
        try:
            return next_call(args)
        finally:
            with self._lock:
                left = self._inflight.get(sid, 0) - 1
                if left > 0:
                    self._inflight[sid] = left
                else:
                    self._inflight.pop(sid, None)

    def _route(self, session_id: Any, turn_id: Any) -> tuple:
        sid, tid = _text(session_id), _text(turn_id)
        with self._lock:
            b = self._bindings.get(sid) if sid else None
            if b is not None and b.mode == _REPLAY:
                if b.finished or tid != b.turn_id:
                    return b, _DO_REFUSE
                return b, _DO_REPLAY
            if (sid, tid) in self._retired:
                return None, _DO_REFUSE
            if b is not None and not b.finished and tid == b.turn_id:
                return b, _DO_RECORD
            if not sid:
                return None, _PASS_UNCOUNTED
            key = (sid, tid)
            self._live_calls[key] = self._live_calls.get(key, 0) + 1
            self._live_calls.move_to_end(key)
            _trim(self._live_calls, _MAX_GUARDED)
            self._inflight[sid] = self._inflight.get(sid, 0) + 1
            return None, _PASS

    def _replay(self, b: _Binding, tool_name: Any, args: Any) -> str:
        """Answer one call of the replayed turn. Never calls a tool, never raises."""
        try:
            with b.lock:
                if b.finished:
                    b.refused += 1
                    return _REFUSED
                index = b.index
                if index is None:
                    kind = store.MISS
                    result = _miss(tool_name, args, b.load_error or "the cassette is not loaded")
                    cwd = None
                else:
                    kind, result = index.answer(tool_name, args)
                    cwd = index.cassette.cwd or None
                    if not isinstance(result, str):
                        kind, result = store.MISS, _miss(tool_name, args, "the recorded result is not text")
                self._log_event(b, kind, _event_key(cwd, tool_name, args))
            return result
        except Exception as exc:
            try:
                b.invalid.append(f"an internal error interrupted the replay of a tool call: "
                                 f"{type(exc).__name__}")
            except Exception:
                pass
            try:
                with b.lock:
                    self._log_event(b, store.MISS, _event_key(None, tool_name, None))
            except Exception:
                pass
            return _miss(tool_name, args, f"internal error: {type(exc).__name__}")

    @staticmethod
    def _log_event(b: _Binding, kind: str, key: str) -> None:
        if len(b.events) < MAX_EVENTS:
            b.events.append((kind, key))
        else:
            b.events_dropped += 1

    @staticmethod
    def _refuse(b: Optional[_Binding]) -> str:
        if b is not None:
            with b.lock:
                b.refused += 1
        return _REFUSED

    def _record(self, b: _Binding, tool_name: Any, args: Any, next_call: Any) -> Any:
        """Run the real tool and keep its result if the tool is recordable."""
        try:
            keep = self._should_record(b, tool_name, args)
        except Exception:
            keep = False
        result = next_call(args)  # the real tool; its errors are Hermes's to handle
        try:
            self._note_call(b, tool_name, args, result, keep)
        except Exception:
            logger.debug("session-cassettes: could not note a %s call", tool_name, exc_info=True)
        return result

    @staticmethod
    def _should_record(b: _Binding, tool_name: Any, args: Any) -> bool:
        if tool_name in RECORDED_TOOLS:
            return True
        if tool_name == "terminal":
            with b.lock:
                # After one command outside the read-only list, later terminal
                # output may depend on what it changed, so none of it is kept.
                if b.terminal_tainted:
                    return False
                if _readonly_terminal(args):
                    return True
                b.terminal_tainted = True
        return False

    @staticmethod
    def _note_call(b: _Binding, tool_name: Any, args: Any, result: Any, keep: bool) -> None:
        name = _text(tool_name)
        with b.lock:
            if b.finished or b.cassette is None:
                return
            if keep and isinstance(result, str):
                clean = _json_safe(args)
                if len(result) > MAX_RESULT_CHARS or b.recorded_chars + len(result) > MAX_RECORDED_CHARS:
                    b.oversize += 1
                elif clean is not None:
                    b.cassette.add_call(name, clean, result)
                    b.recorded_chars += len(result)
                    b.paths.extend(snapshot.named_paths(name, clean, result))
                    return
            b.cassette.add_unrecorded(name)
            b.live[name] = b.live.get(name, 0) + 1

    # -- pre_llm_call ---------------------------------------------------------

    def pre_llm_call(self, session_id: Any = "", turn_id: Any = "", user_message: Any = None,
                     is_first_turn: Any = None, platform: Any = "", model: Any = "", **_: Any) -> Any:
        try:
            return self._pre(session_id, turn_id, user_message, is_first_turn, platform, model)
        except Exception:
            logger.warning("session-cassettes: pre_llm_call failed", exc_info=True)
            return None

    def _pre(self, session_id: Any, turn_id: Any, user_message: Any, is_first_turn: Any,
             platform: Any, model: Any) -> Any:
        sid, tid = _text(session_id), _text(turn_id)
        if not sid:
            return None
        with self._lock:
            old = self._bindings.get(sid)
            if old is not None:
                if old.turn_id == tid and not old.finished:
                    return None  # the same turn again: its binding stands
                self._drop_locked(old)  # a new turn replaces the session's binding
            pending = self._pending.get("id:" + sid)
            keyed = pending is None and any(k.startswith("key:") for k in self._pending)
        if keyed:
            pending = self._pending_by_key()
        if pending is not None:
            return self._begin_record(sid, tid, pending, user_message, model, platform)
        if is_first_turn is True and platform == "cli":
            self._try_claim(sid, tid, user_message, model, platform)
        return None

    def _pending_by_key(self) -> Optional[dict]:
        try:
            key = self._session_key()
        except Exception:
            return None
        if not isinstance(key, str) or not key:
            return None
        with self._lock:
            return self._pending.get("key:" + key)

    def _begin_record(self, sid: str, tid: str, pending: dict, user_message: Any,
                      model: Any, platform: Any) -> Any:
        name = pending["name"]
        problem = _record_refusal(user_message)
        if problem:
            self._clear_pending(pending)
            self._set_status(sid, f"recording {name!r} was cancelled: {problem}")
            return {
                "action": "block",
                "message": (f"Session Cassettes: {problem} Recording {name!r} was cancelled and "
                            "nothing was saved. Run /cassette record again, then send the request "
                            "as plain text."),
            }
        base = self._data_dir()
        cwd = self._cwd()
        cassette = store.Cassette(
            name=name, request=arms.normalize_request(user_message), model=_text(model),
            cwd=cwd, workspace=cwd,
        )
        b = _Binding(_RECORD, sid, tid, base, self._clock())
        b.cassette = cassette
        b.overwrite = bool(pending.get("overwrite"))
        b.pending = pending
        b.model, b.platform, b.cwd = _text(model), _text(platform), cwd
        with self._lock:
            pending["keys"].add("id:" + sid)
            self._pending["id:" + sid] = pending
            self._bindings[sid] = b
            self._trim_locked()
        return None

    def _try_claim(self, sid: str, tid: str, user_message: Any, model: Any, platform: Any) -> None:
        wanted = arms.normalize_request(user_message)
        if not wanted:
            return
        base = self._data_dir()
        for arm in arms.waiting_arms(base, self._clock()):
            if arms.normalize_request(arm["request"]) != wanted:
                continue
            if arms.claim(base, arm["id"]):  # atomic: at most one session gets here per arm
                self._bind_replay(sid, tid, base, arm, model, platform)
                return

    def _bind_replay(self, sid: str, tid: str, base: Any, arm: dict, model: Any, platform: Any) -> None:
        b = _Binding(_REPLAY, sid, tid, base, self._clock())
        b.arm = arm
        with self._lock:
            live = self._live_calls.get((sid, tid), 0)
            running = self._inflight.get(sid, 0)
            # The reasons go on the binding before anyone else can see it.
            if live:
                b.invalid.append(f"{live} tool call(s) of this turn ran live before the cassette was bound")
            if running:
                b.invalid.append(f"{running} tool call(s) of this session were still running live "
                                 "when the cassette was bound")
            # Bound before the cassette loads: from here every call of this turn
            # is answered from the cassette or refused. With no cassette loaded
            # yet (or ever), each answer is a miss.
            self._bindings[sid] = b
            self._replay_active += 1
            self._trim_locked()
        b.model, b.platform, b.cwd = _text(model), _text(platform), self._cwd()
        try:
            cassette = store.CassetteStore(base).load(arm["cassette"])
            index = store.ReplayIndex(cassette)
        except Exception as exc:
            reason = f"the cassette could not be loaded: {exc}"[:300]
            with b.lock:
                b.load_error = reason
                b.invalid.append(reason)
        else:
            with b.lock:
                if not b.finished:  # a turn that already ended keeps refusing
                    b.index = index
        with b.lock:
            b.setup_done = True
        self._write_ledger(b)

    # -- post_llm_call --------------------------------------------------------

    def post_llm_call(self, session_id: Any = "", turn_id: Any = "", assistant_response: Any = None,
                      model: Any = "", **_: Any) -> None:
        try:
            sid, tid = _text(session_id), _text(turn_id)
            with self._lock:
                b = self._bindings.get(sid) if sid else None
            if b is None or tid != b.turn_id:
                return
            if b.mode == _REPLAY:
                self._finish_replay(b, _text(assistant_response))
            else:
                self._finish_record(b, _text(assistant_response), model)
        except Exception:
            logger.warning("session-cassettes: post_llm_call failed", exc_info=True)

    def _finish_replay(self, b: _Binding, answer: str) -> None:
        with b.lock:
            if b.finished:
                return
            b.finished = True
            b.finished_at = self._clock()
            b.final_answer = answer
            b.index = None  # the cassette is no longer needed; later calls are refused
        self._write_ledger(b)

    def _write_ledger(self, b: _Binding) -> bool:
        """Write what this binding knows to ``claimed/<id>.json`` for the
        process that armed the run. The state is ``done`` only once the claim's
        setup and the turn have both finished; whichever comes second writes it,
        and nothing is written after it. A failed write leaves the run unproven,
        which the verdict reports as INVALID."""
        try:
            with b.write_lock:
                return self._write_ledger_locked(b)
        except Exception:
            logger.warning("session-cassettes: could not write the run ledger", exc_info=True)
            return False

    @staticmethod
    def _write_ledger_locked(b: _Binding) -> bool:
        with b.lock:
            if b.ledger_final:
                return True
            state = arms.DONE if (b.finished and b.setup_done) else arms.CLAIMED
            invalid = list(b.invalid)
            if b.events_dropped:
                invalid.append(f"the call log is cut short: {b.events_dropped} tool call(s) "
                               f"after the first {MAX_EVENTS} were not logged")
            data = dict(b.arm or {})
            data.update({
                "state": state,
                "session_id": b.session_id,
                "turn_id": b.turn_id,
                "platform": b.platform,
                "model_seen": b.model,
                "cwd": b.cwd,
                "claimed_at": b.started,
                "finished_at": b.finished_at,
                "gateway_pid": os.getpid(),
                "events": [[kind, key] for kind, key in b.events],
                "refused_calls": b.refused,
                "invalid_reasons": invalid,
                "final_answer": b.final_answer,
            })
        written = arms.write_ledger(b.base_dir, data["id"], data)
        if written and state == arms.DONE:
            with b.lock:
                b.ledger_final = True
                b.events = []  # the ledger has them; the binding stays only to refuse
        return written

    def _finish_record(self, b: _Binding, answer: str, model: Any) -> None:
        with b.lock:
            if b.finished:
                return
            b.finished = True
            cassette, paths = b.cassette, list(b.paths)
            live, oversize = dict(b.live), b.oversize
            b.cassette, b.paths = None, []
        try:
            status = self._save_recording(b, cassette, paths, live, oversize, answer, model)
        except store.CassetteError as exc:
            status = f"cassette {cassette.name!r} was NOT saved: {exc}"
        except Exception as exc:
            logger.warning("session-cassettes: saving a recording failed", exc_info=True)
            status = f"cassette {cassette.name!r} was NOT saved: internal error ({type(exc).__name__})"
        self._clear_pending(b.pending)  # a recording is one completed turn
        self._set_status(b.session_id, status)

    def _save_recording(self, b: _Binding, cassette: Any, paths: list, live: dict, oversize: int,
                        answer: str, model: Any) -> str:
        cassette.final_answer = answer
        if _text(model):
            cassette.model = _text(model)
        notes = snapshot.capture(cassette, paths, redact=self._redact)
        if live:
            parts = ", ".join(f"{name} x{count}" for name, count in sorted(live.items()))
            cassette.add_warning(
                f"{NOT_CLEAN}: {sum(live.values())} tool call(s) ran live and were not recorded "
                f"({parts}); a replay answers each of them with a cassette miss"
            )
        if oversize:
            cassette.add_warning(f"{NOT_CLEAN}: {oversize} tool result(s) were too large to record")
        if not cassette.calls:
            cassette.add_warning(f"{NOT_CLEAN}: no recordable tool call was made in this turn")
        if not cassette.workspace:
            cassette.add_warning(
                "the session had no working directory, so no file snapshot was taken; "
                "a replay can only reuse calls with the same arguments"
            )
        elif notes:
            reasons: dict = {}
            for _path, reason in notes:
                reasons[reason] = reasons.get(reason, 0) + 1
            detail = ", ".join(f"{reason}: {count}" for reason, count in sorted(reasons.items()))
            cassette.add_warning(f"the snapshot left out {len(notes)} file(s) the results named ({detail})")
        cassettes = store.CassetteStore(b.base_dir)
        if not b.overwrite and cassettes.exists(cassette.name):
            raise store.CassetteError("a cassette with that name appeared while recording")
        cassettes.save(cassette)
        lines = [
            f"saved {cassette.name!r}: {len(cassette.calls)} tool call(s) recorded, "
            f"{len(cassette.snapshot)} file(s) in the snapshot.",
        ]
        lines += [f"warning: {w}" for w in cassette.warnings]
        lines.append(f"Next: /cassette pin {cassette.name} <fact the answer must contain>")
        return "\n".join(lines)

    # -- housekeeping ---------------------------------------------------------

    def _cwd(self) -> str:
        if self._session_cwd is None:
            return ""
        try:
            value = self._session_cwd()
        except Exception:
            return ""
        return value.strip() if isinstance(value, str) else ""

    def _set_status(self, sid: str, text: str) -> None:
        with self._lock:
            self._status[sid] = text
            self._status.move_to_end(sid)
            _trim(self._status)

    def _clear_pending(self, pending: Optional[dict]) -> None:
        if not pending:
            return
        with self._lock:
            for key in list(pending.get("keys", ())):
                if self._pending.get(key) is pending:
                    del self._pending[key]

    def _drop_locked(self, b: _Binding) -> None:
        if self._bindings.get(b.session_id) is not b:
            return
        del self._bindings[b.session_id]
        if b.mode == _REPLAY:
            self._replay_active -= 1
            # Late calls of the replayed turn must still never run live.
            self._retired[(b.session_id, b.turn_id)] = True
            _trim(self._retired, _MAX_GUARDED)

    def _trim_locked(self) -> None:
        """Keep the tables small in a gateway that runs for weeks. Dropped, in
        this order: finished recordings; replays that finished more than
        REPLAY_GRACE_SECONDS ago (until then the session keeps refusing, which
        covers the background-review fork that follows a turn); unfinished
        recordings (their tools run live either way). An unfinished replay
        binding is never dropped. Old ones only give up their cassette."""
        by_age = sorted(self._bindings.values(), key=lambda b: b.started)
        over = len(by_age) - MAX_BINDINGS
        settled = self._clock() - REPLAY_GRACE_SECONDS
        tiers = (
            lambda b: b.finished and b.mode == _RECORD,
            lambda b: b.finished and b.finished_at <= settled,
            lambda b: b.mode == _RECORD,
        )
        for wanted in tiers:
            for b in by_age:
                if over <= 0:
                    break
                if wanted(b) and self._bindings.get(b.session_id) is b:
                    self._drop_locked(b)
                    over -= 1
        # Runs as a binding is added, before its own cassette loads: it takes the last slot.
        loaded = [b for b in by_age if b.mode == _REPLAY and not b.finished and b.index is not None]
        for b in loaded[:max(0, len(loaded) - (MAX_LOADED_REPLAYS - 1))]:
            b.index = None
            b.load_error = "this replay was retired because many later runs started"
            b.invalid.append(b.load_error)

    # -- /cassette ------------------------------------------------------------

    def command(self, raw_args: Any = "", session_id: Any = "", session_key: Any = "", **_: Any) -> str:
        try:
            return self._command(_text(raw_args), _text(session_id), _text(session_key))
        except store.CassetteError as exc:
            return f"cassette: {exc}"
        except Exception as exc:
            logger.warning("session-cassettes: /cassette failed", exc_info=True)
            return f"cassette: internal error ({type(exc).__name__})"

    def _command(self, raw: str, sid: str, skey: str) -> str:
        words = raw.split()
        verb = words[0].lower() if words else "help"
        if verb == "record":
            return self._cmd_record(words[1:], sid, skey)
        if verb == "stop":
            return self._cmd_stop(sid, skey)
        if verb == "status":
            return self._cmd_status(sid, skey)
        if verb == "pin":
            return self._cmd_pin(raw)
        if verb == "ping":
            return self._cmd_ping(words[1:])
        if verb == "list":
            names = store.CassetteStore(self._data_dir()).names()
            return "cassettes: " + ", ".join(names) if names else "no cassettes yet. /cassette record <name> starts one."
        return _USAGE

    @staticmethod
    def _keys(sid: str, skey: str) -> set:
        """Where a record request is filed. The session id when there is one.
        A brand-new session may not have an id yet; then its route key stands
        in until the first turn supplies the id."""
        if sid:
            return {"id:" + sid}
        return {"key:" + skey} if skey else set()

    def _find_pending(self, sid: str, skey: str) -> Optional[dict]:
        with self._lock:
            for key in ("id:" + sid if sid else "", "key:" + skey if skey else ""):
                if key and key in self._pending:
                    return self._pending[key]
        return None

    def _cmd_ping(self, words: list) -> str:
        """``hermes cassette run`` sends ``/cassette ping <nonce>`` before it arms
        a run. Writing the pong proves this process (the gateway) has the plugin
        loaded, and says which code it loaded. Not listed in the usage text."""
        if len(words) != 1:
            return "cassette: ping takes one nonce"
        arms.write_pong(self._data_dir(), words[0].lower(), arms.CODE_VERSION, os.getpid(), self._clock())
        return f"pong {arms.CODE_VERSION}"

    def _cmd_record(self, words: list, sid: str, skey: str) -> str:
        flags = [w for w in words if w.startswith("--")]
        names = [w for w in words if not w.startswith("--")]
        if len(names) != 1 or any(f != "--overwrite" for f in flags):
            return "usage: /cassette record <name> [--overwrite]"
        name = store.validate_name(names[0])
        keys = self._keys(sid, skey)
        if not keys:
            return "cassette: this session has no id yet. Send one message first, then run /cassette record."
        overwrite = "--overwrite" in flags
        cassettes = store.CassetteStore(self._data_dir())
        if cassettes.exists(name) and not overwrite:
            return (f"cassette {name!r} already exists. /cassette record {name} --overwrite replaces it, "
                    "pins included.")
        self._clear_pending(self._find_pending(sid, skey))
        pending = {"name": name, "overwrite": overwrite, "keys": keys}
        with self._lock:
            for key in keys:
                self._pending[key] = pending
            while len(self._pending) > _MAX_TRACKED:
                self._pending.pop(next(iter(self._pending)))
        return (
            f"Recording is armed: this session's next message is recorded as cassette {name!r}.\n"
            "Saved to disk in plain JSON: the request, the final answer, the results of read-only "
            "tools (file reads, searches, web and skill lookups), and the text of the workspace "
            f"files those results name.\nFile: {cassettes.path_for(name)}\n"
            "/cassette stop cancels."
        )

    def _cmd_stop(self, sid: str, skey: str) -> str:
        pending = self._find_pending(sid, skey)
        self._clear_pending(pending)
        dropped = None
        with self._lock:
            b = self._bindings.get(sid) if sid else None
            if b is not None and b.mode == _RECORD and not b.finished:
                with b.lock:
                    b.finished = True
                    dropped, b.cassette, b.paths = b.cassette, None, []
                self._drop_locked(b)
        if dropped is not None:
            return f"recording {dropped.name!r} stopped. Nothing was saved."
        if pending is not None:
            return f"recording {pending['name']!r} cancelled. Nothing was saved."
        return "nothing is being recorded in this session."

    def _cmd_status(self, sid: str, skey: str) -> str:
        with self._lock:
            b = self._bindings.get(sid) if sid else None
            last = self._status.get(sid) if sid else None
        if b is not None and b.mode == _RECORD and not b.finished:
            with b.lock:
                count = len(b.cassette.calls) if b.cassette is not None else 0
                name = b.cassette.name if b.cassette is not None else "?"
            return f"recording {name!r} now: {count} tool call(s) kept so far."
        pending = self._find_pending(sid, skey)
        if pending is not None:
            return f"armed: this session's next message is recorded as {pending['name']!r}."
        return last or "nothing is armed in this session. /cassette record <name> starts a recording."

    def _cmd_pin(self, raw: str) -> str:
        parts = raw.split(None, 2)
        if len(parts) < 3:
            return "usage: /cassette pin <name> <fact>   (re:<regex> pins a pattern)"
        name = store.validate_name(parts[1])
        fact = parts[2].strip()
        if len(fact) >= 2 and fact[0] == fact[-1] and fact[0] in "\"'":
            fact = fact[1:-1]
        cassettes = store.CassetteStore(self._data_dir())
        cassette = cassettes.load(name)
        pin = cassette.add_pin(fact)
        cassettes.save(cassette)
        return f"pinned to {name!r}: {pin}   ({len(cassette.pins)} pin(s) in all)"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_RUNTIME = Runtime()


def _tool_execution(tool_name: Any = "", args: Any = None, next_call: Any = None, **kwargs: Any) -> Any:
    return _RUNTIME.tool_execution(tool_name, args, next_call, **kwargs)


def _pre_llm_call(**kwargs: Any) -> Any:
    return _RUNTIME.pre_llm_call(**kwargs)


def _post_llm_call(**kwargs: Any) -> None:
    return _RUNTIME.post_llm_call(**kwargs)


def _cassette_command(raw_args: str = "", **kwargs: Any) -> str:
    return _RUNTIME.command(raw_args, **kwargs)


def register(ctx: Any) -> None:
    """Hermes entry point. What is registered here must match plugin.yaml."""
    _RUNTIME.attach(ctx)
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("post_llm_call", _post_llm_call)
    ctx.register_middleware("tool_execution", _tool_execution)
    ctx.register_command(
        "cassette",
        _cassette_command,
        description="Record this session's tool results as a replayable cassette",
        args_hint="record <name> | stop | status | pin <name> <fact> | list",
    )
    # `hermes cassette ...` in a terminal: run, pin, list, show (cli.py). Hermes
    # skips this one under plugins.isolation: host; the slash command still works.
    register_cli = getattr(ctx, "register_cli_command", None)
    if register_cli is not None:
        register_cli(
            "cassette",
            help="Replay a recorded cassette offline and grade the answer",
            setup_fn=cli.setup,
            handler_fn=cli.handle,
            description="Session Cassettes: replay a recorded request against another model, "
                        "prompt or skill, with every tool call answered from the cassette.",
        )
