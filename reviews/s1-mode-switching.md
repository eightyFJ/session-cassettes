# Grok review — block A / S1 (mode switching)

Written 2026-11-10. Review of the "block A / S1" entry in `docs/PLAN.md`. Checked against Hermes 0.23.1 (`3b421578`), not against later S2 notes.

Two sessions can take the same arm, and a replay that never claimed it can still be graded as valid. Dropping `on_session_end` is right. Releasing on `post_llm_call` is the weak point. Catching exceptions inside the middleware callback stops the raise-and-fall-through leak, and it leaves other live-tool paths open. Items 5b and the 10-minute TTL cut against design decisions 1 and 6.

## 1. Same arm, or a live run that looks claimed

Both can happen. `pre_llm_call` runs in the one shared gateway, and the arm is chosen by exact string equality on a first turn. Nothing in the scheme makes that claim exclusive.

`is_first_turn` is `not bool(conversation_history)` (`agent/turn_context.py`). A new `hermes -z` session is a first turn. So is any other new session whose first user text is the same string: a second `cassette run` of the same cassette, or a new desktop tab, inside the 10-minute window. Both callbacks can read the same `armed/<id>.json` before either removes it. Each then binds its own `session_id`. The runs share one cassette cursor (decision 3 returns results in order), so the two replays interleave.

The parent cannot see which session claimed the arm. `hermes -z` is the unattended path: on success it prints only the final answer. It does not print `Session: …` (`hermes_cli/gateway_chat.py`) and it does not print `session_id` (`hermes_cli/gateway_chat_view.py`, `_print_exit_ids`). After the child exits, "the arm file is gone" or "some binding exists" is true when a different session took it. The child whose text matched nothing then runs every tool live, and the verdict still looks claimed.

These text changes make equality fail, so the arm stays unclaimed. That case is detectable, because the arm file is still there:

- `-z` does not send `api_text`. With `api_text` absent, an `@` reference is expanded into the stored user text (`gateway/session_context_refs.py`, `turn_references`). The hook is given that expanded string (`original_user_message`).
- A leading gateway timestamp prefix is stripped from the persisted text (`gateway/message_timestamps.py`) even though timestamp rendering is off.
- The process argument itself can differ from the cassette string (quoting, a trailing newline) before Hermes sees it.

A resumed session is also unclaimed: history is non-empty, so `is_first_turn` is false and the arm is never considered. The leftover arm file makes that visible.

The claim has to be an atomic rename of one arm id that the parent created, and the parent has to require that this child produced the binding. Message equality on a fresh `-z` is not that check. A random token inside the user text would identify the run, and it would also change the prompt the model sees. `pre_llm_call` can add context or block the turn. It cannot rewrite the message.

## 2. `on_session_end` and the release point

Dropping it from the manifest is right. Hermes documents it as the end of every `run_conversation()`, including failed and interrupted turns, and the desktop session keeps going (`website/docs/user-guide/features/hooks.md`). It is not session close. `on_session_finalize` is the close hook, and a `-z` turn does not shut the gateway down, so finalize does not run either.

`post_llm_call` plus a TTL is a weak release. `post_llm_call` runs only when `final_response` is non-empty and the turn was not interrupted (`agent/turn_finalizer.py`). An interrupt, an empty finish, or a hook timeout (the hook is on the fail-open timeout list, default 30 seconds) leaves the binding in place. The next desktop turn is still record or replay.

`on_session_end` does run at the end of that same function for every outcome, so it is the better "this turn is over" signal. It is still a bad place to drop a session-wide binding. The background review is spawned earlier in the same function, on a fork that reuses the parent's `session_id` (`agent/background_review.py`) and sets `_persist_disabled`, which makes `pre_llm_call` return immediately. This profile has memory on and a skill nudge every 15 tool iterations. A continuing desktop record session can be due for a review. If the binding is still session-wide, the review's `read_file` and `search_files` calls are recorded or they consume replay results. If `post_llm_call` already released it, those calls run live.

Scope the binding to the `turn_id` that `pre_llm_call` and `tool_execution` already receive. The next turn's `pre_llm_call` clears it. Keep the TTL only for a crashed claim, and do not expire a binding while its `turn_id` is in the tool loop. A 10-minute expiry measured from arm time will unbind a slow Arete turn in the middle and the rest of the tools run live.

`/cassette record` does not need `on_session_end`. A slash command is not a model turn, so nothing fires between the command and the next user message.

## 3. The raise fall-through, and the other live paths

Returning a miss string from a `try` that covers the whole callback is what stops trap (a). In `_run_execution_chain`, an exception raised before `next_call` is logged and the real tool runs (`hermes_cli/middleware.py`). `pre_tool_call`, approval, and the registry dispatch all sit on that `next_call` path (`agent/tool_executor.py`, `_dispatch_authorized_once`). They do not run when the callback returns. Raising a "miss" exception does not do this. Hermes treats that raise as a failed plugin and dispatches the tool. If the callback calls `next_call` and then raises, Hermes returns the live result.

The `try` has to be inside the callback. A `TypeError` from a signature that does not accept the kwargs is raised at the call site, outside the callback, and that also falls through. `**kwargs` plus one `except Exception` that returns `{"error": "cassette miss: …"}` covers (a). `except Exception` does not include `BaseException`. That path propagates, and the concurrent worker turns it into a cancelled result rather than a dispatch.

These still run the real tool with that catch in place:

- An unbound session. The middleware is specified to call `next_call` unless the `session_id` is bound. A missed claim, a timed-out `pre_llm_call` (fail-open, same as item 5b), or a TTL that expires mid-turn all take that path. The catch never runs, because the callback thinks it is idle.
- A later fork on the same `session_id` with a different `turn_id`, if only the bound turn is intercepted and every other call falls through. The background-review fork is that case. For a replay session, a non-matching `turn_id` has to return an error and must not call `next_call`. Those calls also must not become cassette misses, or a review marks a good run diverged.
- Memory prefetch. It runs after `pre_llm_call` returns and it does not go through `tool_execution`. `memory_enabled` is true in the local config. That is a live read on the replay turn, beside the tool ban.

## 4. Where this disagrees with decisions 1–8

Decision 1 says replay never calls `next_call`, so the run is offline by construction. Item 5b says a failed claim runs the tools and is noticed afterwards. That is a live run. The 10-minute arm TTL does the same thing if it hits during the tool loop.

Decision 6 has three outcomes: PASS, DIVERGED, and FAIL. Item 5b adds INVALID. A run that executed live tools and missed a pin would be FAIL under decision 6, with the live calls hidden. INVALID has to be an explicit fourth result, and a live run must not be PASS.

Decision 3's ordered cursor assumes one replay consumes each repeated key. Two sessions bound to the same arm break that, and so does counting the background review's calls against the same cursor.

Decision 5's grade-from-`post_llm_call`-or-stdout split still holds, because stdout is the fallback when the hook does not fire. Item 6 does not contradict decisions 1–8. It does replace block D's `hermes cassette record` CLI with an in-session slash command. The outside command remains `hermes cassette run`.

Items 1, 3, and 4 agree with the decisions. The env var is gone because it never reached the gateway. Path normalisation is required. The slash command is what binds a desktop `session_id`, and `is_first_turn` is false there.
