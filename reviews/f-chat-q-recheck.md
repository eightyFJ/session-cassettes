# Block F recheck — yolo env, session id, ping order

Written 2026-11-11. Read `cli.py` at `94856bc` (`git diff 0c846be..94856bc -- cli.py tests/`) before `reviews/f-chat-q.md` and before the "Grok's review of the fix" section in `docs/SECURITY.md`. Checked the launch flags and both `session_id:` printers against Hermes 0.23.1 (`hermes_cli/gateway_chat.py`, `hermes_cli/gateway_session_store.py`, `hermes_cli/cli_single_query.py`, `hermes_cli/gateway_agent.py`, `tools/approval.py`, `agent/session_client_env.py`). Did not re-run the tests.

## 1. Does dropping `HERMES_YOLO_MODE` close the auto-approve path the child was opening?

It closes the one the child was forwarding. It is the only name in `_ENV_LAUNCH_FLAGS` that does that.

`spawn` copies the environment, sets `PYTHONIOENCODING`, and deletes every key whose upper-cased name is `HERMES_YOLO_MODE`. The classic `hermes chat -Q` path (this child's argv) builds `session.create` in `gateway_session_store._creation_policy`, which calls `env_launch_flags()`. That map is:

```python
_ENV_LAUNCH_FLAGS = (("yolo", "HERMES_YOLO_MODE"), ("ignore_rules", "HERMES_IGNORE_RULES"),
                     ("safe_mode", "HERMES_SAFE_MODE"), ("ignore_user_config", "HERMES_IGNORE_USER_CONFIG"),
                     ("accept_hooks", "HERMES_ACCEPT_HOOKS"))
```

`yolo` is the flag `run_turn_runner.py` turns into `apply_launch_yolo`. With the variable gone, this child does not send it. `prompt.submit` from `GatewayAgent._submit_params` sends `finite` when the agent is finite, and does not send `unattended` or an environment block.

`client_env` is not a second copy of that flag. `collect_client_env` keeps the behaviour names (`HERMES_EPHEMERAL_SYSTEM_PROMPT`, `HERMES_PREFILL_MESSAGES_FILE`, `HERMES_INFERENCE_PROVIDER`, `HERMES_ENABLE_PROJECT_PLUGINS`) and toolchain names. `_travels` drops every other `HERMES_` name, so `HERMES_YOLO_MODE` is not in the map even if a passthrough list names it.

The other four flags do not skip the approval gate:

- `HERMES_IGNORE_RULES` becomes `ignore_rules`. That skips memory, soul, and context files. It is not read by `tools/approval.py`.
- `HERMES_ACCEPT_HOOKS` consents to the profile's shell hooks. It does not set `yolo` and does not take the early return in `_run_approval_gate`.
- `HERMES_SAFE_MODE` and `HERMES_IGNORE_USER_CONFIG` are sent on `session.create` for both the ping child and the request child, because both go through `spawn`. `safe_mode` implies `ignore_user_config` and `ignore_rules`. It drops plugin toolsets from that session's tool list. It does not set `policy.yolo`. Plugin discovery is skipped only in a process whose own environment has `HERMES_SAFE_MODE` (`plugins.py`, `discover_and_load`). That is the client. The gateway process is where `/cassette` and `pre_llm_call` run, and this strip does not put the variable there. A ping session and a request session created from the same shell both carry the same `safe_mode` bit. I did not find a mode where the ping handler runs and the request session's hooks are left unloaded.

What the strip does not change is a gateway process that already imported `tools.approval` with `HERMES_YOLO_MODE` set. `_YOLO_MODE_FROZEN` is taken once, at that import, and `_yolo_active` returns true before `single_query_mode` is consulted. The child never sends that. The gateway already has it. `spawn` cannot clear it.

## 2. Does a mandatory session id tie the ledger to this child?

For a child that finishes and prints the line these two paths print, a different session's ledger is INVALID. The compression sync the request names does not move that line onto the other session.

The check runs when a ledger was read and the child did not time out. No `session_id:` line, an empty ledger `session_id`, or a mismatch each append a reason. `compute_verdict` turns any reason into INVALID. A timeout still skips this check and still appends the timeout reason, so that run stays INVALID. An unclaimed arm never reaches this check: `armed_left and not claimed` already added the leftover-arm reason.

Both printers use one token after `session_id:`:

- Default `hermes chat -Q` is `cli.main` → `_run_quiet_single_query`, which prints `session_id: {cli.session_id}` (`cli_single_query.py`). `_sync_cli_session_id_from_agent` copies `cli.agent.session_id` into `cli.session_id` first.
- `--safe-mode` / `--ignore-user-config` on the argv is the transport client, which prints `session_id: {self.session_id}` from `_print_exit_ids`. The plugin does not pass those flags. An inherited `HERMES_SAFE_MODE` does not flip `_bypass_chat_launch`; that function reads the argparse flags.

`GatewayAgent.session_id` changes when the client repoints it (`/new`, `/resume`, `/branch`). A compression inside the gateway assigns `agent.session_id` on the gateway's agent (`conversation_compression.py`) and does not call that setter. The printed id stays the id the client created. The ledger stores the id `pre_llm_call` received, which is that same session at claim time. A later rotation in the gateway changes the id the next hook sees. The binding is keyed by the old id, so that later hook does not finish it. The parent then sees a ledger that is not `done`, which is already INVALID. I did not find a path that exits 0, prints this child's id, and grades a ledger whose `session_id` is some other session: the last matching line would have to be that other id, and both printers write their own id last. `exit_single_query` can append a Kanban trailer after that line. The trailer is `[kanban-worker-exit] rc=`, which the session regex does not match.

A finished quiet turn that never reaches the printer (credentials or agent init failed) exits through `_single_query_exit_code(None)`, which is non-zero, so the run is INVALID before the missing line matters. The plugin does not pass `--resume`. A resumed id is not what this child prints.

`_read_back(err, tail=True)` keeps the last 16 MiB. The session line is at the end, so the truncation that used to drop it now keeps it. Stdout is still the start of the file. Nothing in `replay` reads the start of stderr. The failure record's `stderr_tail` is the last lines of whatever was kept, which is the end of the stream. A warning that existed only in the first part of a stderr larger than 16 MiB is no longer in that record.

## 3. Does ping-before-arm still leave a window, and does a missing plugin stay INVALID?

The ping no longer has an arm to steal. The window after a matching pong is still there, and a request that then runs with no plugin is INVALID.

`replay` calls `preflight` and only then `create_arm`. A `CliError` from preflight is raised with no arm file. The pong is unlinked in `preflight`'s `finally`.

After a matching pong the gateway can still restart, or the plugin can be disabled, before this child's `pre_llm_call`. The request is sent. Nothing claims the new arm. The check is unchanged:

```python
if armed_left and not claimed:
    reasons.append("no session claimed this run: the arm file is left over, "
                   "so the turn may have run with live tools")
```

That reason is enough. `compute_verdict` does not also need the session line. If the arm file is gone and no ledger was written, the other branch appends "no run ledger was written", which is also INVALID. A claim that does land during the run is the case the session-id check reports when the ledger id differs from the child's line.

## 4. What from the first review is still open?

The three code gaps named in the 2026-11-11 progress-log entry are closed in this diff: the child no longer inherits `HERMES_YOLO_MODE`, a missing session id is INVALID, and the arm is created after the ping. The docstring changes match the code. The follow-up sentence in `docs/SECURITY.md` that the ping runs before the arm is written is true of `replay` now.

Still open, and not in that file's "Accepted as is" list: a gateway process that was itself started with `HERMES_YOLO_MODE`. `_YOLO_MODE_FROZEN` auto-approves flagged commands on this child's turn, including an unclaimed one. The strip never sees that process. The accepted list covers the user's approval settings, a forged pong, the unchecked ping exit code, and untested `plugins.isolation: host`. It does not name the frozen gateway flag.

The progress log's "offline unless INVALID holds now that the session check is mandatory" matches the foreign-ledger hole from the first review. A plugin removed after the pong does not pass: the leftover arm marks it INVALID, which is the same shape as item (b) under the gateway-check write-up. I did not find a sentence in that 2026-11-11 entry that the current `cli.py` contradicts.
