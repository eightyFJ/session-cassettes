"""Spike S1 probe. Logs pid, the HERMES_CASSETTE env var and every hook/tool call. Pass-through only."""
import json
import os
import sys
import threading
import time

_LOG = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes", "cassette-probe.log")
_lock = threading.Lock()


def _log(event, **fields):
    rec = {"t": time.strftime("%H:%M:%S"), "event": event, "pid": os.getpid(), "ppid": os.getppid(),
           "env": os.environ.get("HERMES_CASSETTE"), "argv": sys.argv[:6]}
    rec.update(fields)
    try:
        with _lock, open(_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str)[:2000] + "\n")
    except Exception:
        pass


def _short(v, n=200):
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s[:n]


def _on_session_start(session_id="", **kw):
    _log("on_session_start", session_id=session_id, keys=sorted(kw))


def _post_llm_call(session_id="", user_message="", assistant_response="", model="", **kw):
    _log("post_llm_call", session_id=session_id, model=model, user=_short(user_message),
         answer=_short(assistant_response))


def _on_session_end(session_id="", **kw):
    _log("on_session_end", session_id=session_id, keys=sorted(kw))


def _tool_exec(tool_name="", args=None, next_call=None, **kw):
    result = next_call(args)
    _log("tool_execution", tool=tool_name, args=args, session_id=kw.get("session_id"),
         kw_keys=sorted(kw), result_type=type(result).__name__, result=_short(result))
    return result


def _pre_llm_call(session_id="", user_message="", is_first_turn=None, platform="", **kw):
    _log("pre_llm_call", session_id=session_id, first=is_first_turn, platform=platform, user=_short(user_message))


def _probe_cmd(raw_args, **kw):
    _log("slash_probe", raw=raw_args, ctx=kw)
    return f"probe ok pid={os.getpid()} session_id={kw.get('session_id')}"


def register(ctx):
    _log("register")
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("post_llm_call", _post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_command("probe", _probe_cmd, description="cassette spike probe")
    ctx.register_middleware("tool_execution", _tool_exec)
