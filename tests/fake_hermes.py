"""A stand-in for ``hermes ... chat -Q --query=...`` used by test_cli.py.

It runs in its own process, like the real child, and drives the plugin's real
hooks and middleware the way the gateway would: ``pre_llm_call`` (which claims
the arm), the scripted tool calls through ``tool_execution``, then
``post_llm_call`` (which writes the done ledger). Then it prints the answer.

The test's plan comes in the FAKE_HERMES_PLAN environment variable (JSON):

    data_dir   the plugin data folder (required)
    calls      [[tool, args], ...] made during the turn
    answer     the final answer the hooks see
    stdout     what to print instead of the answer (default: the answer)
    message    the first message the session sees (default: the --query value)
    session    the session id printed on stderr, as -Q does (default "S-child";
               "" prints none)
    platform   default "cli"
    exit       exit code (default 0)
    sleep      seconds to sleep before answering
    live_log   file to append to if a real tool (next_call) ever runs
    argv_log   file to write this process's argv to, as JSON (not for a ping)
    ping       "ok" (default), "none" (no plugin: no pong), "old" (other code)
               or "yolo" (a gateway started with HERMES_YOLO_MODE)
    ping_log   file to append each ping request to
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pkg import arms, plugin  # noqa: E402


def main() -> int:
    plan = json.loads(os.environ["FAKE_HERMES_PLAN"])
    argv = sys.argv[1:]
    opts = {}
    for token in argv:
        if token.startswith("--") and "=" in token:
            key, _, value = token[2:].partition("=")
            opts.setdefault(key, value)
    request = opts.get("query", "")
    if request.startswith("/cassette ping "):
        return ping(plan, request)
    if plan.get("argv_log"):
        Path(plan["argv_log"]).write_text(json.dumps(argv), encoding="utf-8")
    workdir = opts.get("in", os.getcwd())
    model = opts.get("model", "default-model")
    data = Path(plan["data_dir"])

    def next_call(args):
        if plan.get("live_log"):
            with open(plan["live_log"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps(args) + "\n")
        return "REAL TOOL RESULT"

    rt = plugin.Runtime(
        data_dir=lambda: data,
        session_cwd=lambda: workdir,
        redact=lambda text, path: text,
        session_key=lambda: "",
    )
    rt.pre_llm_call(session_id="S-child", turn_id="T-child", user_message=plan.get("message", request),
                    is_first_turn=True, platform=plan.get("platform", "cli"), model=model)
    for tool, args in plan.get("calls", []):
        rt.tool_execution(tool_name=tool, args=args, next_call=next_call,
                          session_id="S-child", turn_id="T-child")
    if plan.get("sleep"):
        time.sleep(plan["sleep"])
    answer = plan.get("answer", "")
    rt.post_llm_call(session_id="S-child", turn_id="T-child", assistant_response=answer, model=model)
    sys.stdout.write(plan.get("stdout", answer) + "\n")
    sys.stdout.flush()
    session = plan.get("session", "S-child")
    if session:
        sys.stderr.write(f"\nsession_id: {session}\n")  # as hermes chat -Q does
    return int(plan.get("exit", 0))


def ping(plan: dict, request: str) -> int:
    """The gateway's side of ``/cassette ping``: the plugin's real handler.
    ``ping: "none"`` acts as a gateway without the plugin (no pong);
    ``ping: "old"`` as one that loaded other code."""
    if plan.get("ping_log"):
        with open(plan["ping_log"], "a", encoding="utf-8") as fh:
            fh.write(request + "\n")
    mode = plan.get("ping", "ok")
    if mode == "none":
        sys.stdout.write("Unknown command: /cassette\n")
        return 0
    data = Path(plan["data_dir"])
    if mode == "yolo":  # a gateway started with HERMES_YOLO_MODE
        nonce = request.split()[-1]
        arms.write_pong(data, nonce, arms.CODE_VERSION, os.getpid(), time.time(), yolo=True)
        sys.stdout.write("pong\n")
        return 0
    if mode == "old":
        nonce = request.split()[-1]
        arms.write_pong(data, nonce, "0123456789abcdef", os.getpid(), time.time())
        sys.stdout.write("pong 0123456789abcdef\n")
        return 0
    rt = plugin.Runtime(data_dir=lambda: data, redact=lambda text, path: text, session_key=lambda: "")
    sys.stdout.write(rt.command(raw_args=request[len("/cassette "):], session_id="S-ping") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
