# Block F — chat -Q replay

Written 2026-11-10. Read `__init__.py`, `arms.py`, `cli.py`, and `verdict.py` in the working tree (those files match `223644c`; `0c846be` only adds this request and a progress-log line). Checked the child, the approval gate, the slash path, and the pong against Hermes 0.23.1 (`hermes_cli/gateway_chat.py`, `hermes_cli/gateway_chat_view.py`, `hermes_cli/gateway_agent.py`, `gateway/session_finite.py`, `tools/approval.py`, `gateway/platforms/event.py`, `gateway/session_results.py`, `gateway/session_settlement_recovery.py`, `hermes_cli/plugins_ledger.py`). Did not run the tests. `reviews/TEMPLATE.md` is not in the tree; this file follows the block C write-up. `README.md` is still the stub and does not contain the two sentences in question (f); those sentences are judged as the request states them.

## Can a `chat -Q` turn still be unattended or auto-approved?

Unattended, no. Auto-approved, yes, and that approval is decided before `single_query_mode` is consulted.

`build_child_argv` appends `chat`, `-Q`, and `--query=<request>`. It never passes `-z`. In `gateway_chat.py`, `view.unattended` is set only when the oneshot prompt is a string, which is the `-z` path. `GatewayChatView.submit` sends `unattended: True` only when `self.finite and self.unattended`. `finite_turn_scope` sets the unattended context var to `bool(finite and unattended)`, so a submit that omits the flag leaves `unattended_turn()` false. `GatewayAgent._submit_params` can set `finite: True` and never sets `unattended`. That method is the in-process agent path, not the argv this plugin builds. The `-z` auto-approve inside `_yolo_active` (`return unattended_turn()`) does not fire for this child.

The gate in `_run_approval_gate` returns approved earlier than the single-query branch:

```python
if _yolo_active() or approval_context._get_approval_mode() == "off":
    return _approved()
...
if is_approved(session_key, pattern_key):
    return _approved()
```

`_yolo_active` is true when `_YOLO_MODE_FROZEN` is set (the gateway process imported with `HERMES_YOLO_MODE`) or when the session is in the yolo set. After those, a context whose mode is `approve` also returns approved. The default in `config_defaults.py` is `mode: smart`, `single_query_mode: deny`, `unattended_mode: deny`. Under those defaults a flagged command on this child is denied. The docstring on `build_child_argv` says a failed claim "never gets an approval that nobody gave." That holds for the default deny. It does not hold when any of the earlier returns already approved.

`spawn` copies `os.environ` and adds `PYTHONIOENCODING`. It does not drop `HERMES_YOLO_MODE`. `env_launch_flags` maps that variable to `yolo: True` on `session.create`, and `run_turn_runner.py` then calls `apply_launch_yolo` for that session. A replay started from a shell that already has `HERMES_YOLO_MODE` set creates a yolo session without `--yolo` on the argv and without the plugin writing config. A permanent allowlist entry and a session approval already cached for the pattern take the same early return.

`single_query_mode: deny` blocks the flagged command. It does not stop an unflagged tool. On a turn that never claimed, `tool_execution` still calls `next_call`.

## Can the redacted answer, or a `session_id:` line, accept another session?

The redaction clause alone does not. The session check does, when the line is present and the ledger id is non-empty. Both checks are skipped in cases that can still grade.

`_same_answer` accepts stdout when it matches the ledger answer or `redact_sensitive_text` of that answer, after `normalize_request`. When the ledger is `done`, `replay` grades `ledger["final_answer"]`, not stdout. A foreign `done` ledger whose text, or whose redaction, equals this child's stdout therefore supplies the answer the pins see. Redaction is many answers to one string, so a child that printed the redacted form matches a ledger that still holds the secret.

The session check is the bound on that:

```python
child_session = _child_session_id(child.stderr)
if child_session and ledger["session_id"] and child_session != ledger["session_id"]:
    reasons.append(...)
```

`_child_session_id` keeps the last `^session_id:\s*(\S+)\s*$` match. On a finished quiet oneshot, `_print_exit_ids` prints `session_id: {self.session_id}` to stderr because `unattended` is false. Quiet mode suppresses the tool preview and the notice lines, so tool text is not a second `session_id:` line. I did not find a writer after that print on the oneshot return path. A line that is present and differs from a non-empty ledger id makes the run `INVALID`. `compute_verdict` turns any reason into `INVALID`, including the timeout reason appended whenever `child.timed_out` is set. A timeout also skips the answer comparison and waits `0` for the ledger; the timeout reason still blocks `PASS`.

The comparison does not run when either id is empty. `check_ledger` accepts `session_id` defaulting to `""`. `_read_back` keeps the first `MAX_CHILD_OUTPUT_BYTES` (16 MiB) of each stream, so a stderr that long drops the trailing id line and the check is skipped. A completed child with no id line, a `done` ledger, and stdout equal to the ledger answer or its redaction, is graded from that ledger. That is the hole in accepting the redacted form.

The citation for what is printed is off. `session_results._redacted` redacts the stored result, and its docstring says the live viewer prints the completion event, not that copy. `completion_payload` sets `text` from the gateway `response`, and `GatewayChatView.run` prints that text. `_gateway_redact` calls `redact_sensitive_text` with no `file_read` or `force` flag. A print path that redacts with different flags can disagree with the ledger and add the mismatch reason. That is a false `INVALID`.

## Is a slash command under `chat -Q` handled in the gateway?

For this child's submit, yes. The oneshot `run` always does `submit(query)`. It never enters `command()`, which is the interactive loop's local handling of `/quit`, `/stop`, `/approve`, `/model`, and the rest. `submit` sends the text as `prompt.submit`. `MessageEvent.is_command` is `allow_gateway_control and` the text, after a leading attachment-ref strip, starts with `/`. The default of `allow_gateway_control` is true. The gateway dispatches that command, including a plugin handler, before the model. `/cassette` is not one of the client-local commands. A record request filed by one `chat -Q` process is visible to a later process's turn because the handler writes the pending record in the gateway process. That matches the preflight comment: the ping is a slash command with no model call.

Two bounds. An event with `allow_gateway_control` false is not a command; local CLI submit does not clear that flag. `!` is not a gateway slash command. `bang_shell.py` describes a local composer, and this submit does not go through it. `replay_refusal` still rejects a request that starts with `/` or `!` after `normalize_request`, and `_record_refusal` rejects the `@` pattern and the attached-context marker, before `create_arm`.

`plugins.isolation` defaults to `in_process`. `register` skips `register_cli_command` when that attribute is missing, which the plugin comment says is the host-isolation case. The slash handler still registers. I did not trace a host-process split far enough to say the ping and `pre_llm_call` are always the same interpreter when isolation is `host`.

## Does `CODE_VERSION` show that the hooks and middleware are wired?

It shows that the process which wrote the pong had loaded this hash, and that its ping handler ran. It does not invoke the hooks.

`CODE_VERSION` is the first 16 hex chars of a sha256 over `__init__.py`, `arms.py`, `cli.py`, `snapshot.py`, `store.py`, `verdict.py`, and `plugin.yaml`, taken when `arms.py` is imported. A missing file is hashed as the bytes `<missing>`. `register` attaches `pre_llm_call`, `post_llm_call`, and `tool_execution` before `register_command("cassette", ...)`. A ping handler that came from that `register` call was registered after those three calls returned.

A force reload does not leave the old callbacks in the map. `unload` of every plugin clears `_hooks`, `_middleware`, and `_plugin_commands` (`plugins_ledger.py`, `_reset_after_unload_all`), then discovery runs `register` again. `invoke_hook` reads `self._hooks` at call time. An old hook does not stay registered across that reload. A callback whose worker was already abandoned on the hook timeout can still finish; that is one in-flight call, not an entry in the new list.

The pong is not that registration. `preflight` compares `pong["version"]` to `arms.CODE_VERSION` and ignores the pid and the ping child's return code. `write_pong` is an ordinary JSON replace, not `O_EXCL`. The nonce is on the child command line. A process that can write the plugin data dir can place `pongs/<nonce>.json` with this folder's version, and preflight will send the request. The same-process reload above is not that writer.

`turn_isolation` defaults to false. A second process that still has the old module is outside what this comparison sees. The ping does not call `pre_llm_call` or `tool_execution`, so a handler that runs while those callbacks were removed afterward would still produce a matching version. I did not find that removal inside the normal `register` path.

## Do preflight failures leave an arm waiting, or send the request anyway?

A raised preflight does not send the request. The arm is waiting for the whole ping, including a ping that succeeds.

`replay` calls `create_arm` and then `preflight`. The ping query is `/cassette ping <nonce>`, which does not equal the cassette request, so the ping turn does not take the arm. Any other first CLI turn whose message does equal it can claim during the ping. The ping timeout is `min(timeout, PING_TIMEOUT_SECONDS)` and `PING_TIMEOUT_SECONDS` is 120. After a matching pong, `replay` starts the request child with the arm still in place. If the arm was taken, this child runs with live tools. The session-id check above is what turns that into `INVALID` when the child prints a line and the ledger id is non-empty.

On the way out, `replay`'s `finally` calls `arms.discard`, and `preflight`'s `finally` calls `discard_pong` after the pong has been read. `discard` swallows `OSError`. A failed unlink leaves the file, and the request is still not started when preflight raised. Killing the parent so `finally` does not run leaves the arm too.

Preflight treats a readable, version-matching pong as success. The ping child's return code is not checked. `_cmd_ping`'s docstring says the ping is sent before the run is armed. `replay` arms first.

## The two README claims

"No `-z`, no approval settings changed" matches the argv and the absence of any config write. The child is `chat -Q`. The plugin does not pass `--yolo` and does not edit `approvals.*`. The same child still inherits `HERMES_YOLO_MODE` and turns it into session yolo, and the gate still honors `approvals.mode: off`, `single_query_mode: approve`, and an existing allow. Those are settings the plugin did not change. They are also approvals this turn will use. The docstring's "never gets an approval that nobody gave" is the sentence that overshoots.

"Offline unless INVALID" holds for the unclaimed arm: the leftover arm adds a reason, `compute_verdict` forces `INVALID`, and the live `next_call` calls happened on a run that is not `PASS`. A denied flagged command can still be followed by an unflagged tool on that live turn; the verdict stays `INVALID`. It does not hold for a `PASS` built from a foreign `done` ledger when the session line was missing and stdout matched the ledger or its redaction. The preflight window is how that ledger gets claimed by someone other than this child.

The `config.yaml` change does what it says. `_hermes_redact` sets `secret_file=True` for a basename of `config.yaml`, `config.yaml.good.*`, or `config.yaml.corrupt.*`, then calls `redact_sensitive_text(..., file_read=True, secret_file=secret_file)`. Hermes's own secret-file rule is narrower (a `config.yaml` under the Hermes home). This rule redacts a project `config.yaml` as well. Hidden paths stay out of the snapshot, which is where `.env` and shell rc files live.
