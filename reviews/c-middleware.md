# Block C — claim and replay middleware

Written 2026-11-10. Read `arms.py`, `__init__.py`, and the block C entry in `docs/PLAN.md` before `reviews/s1-mode-switching.md`. Checked the timeout and the middleware fall-through against Hermes 0.23.1 (`hermes_cli/plugins_dispatch.py`, `hermes_cli/middleware.py`, `agent/tool_executor.py`). Did not re-run the tests.

## Does the O_EXCL claim hold?

Yes. For one arm id, `arms.claim` gives `True` to at most one caller.

The winning line is the exclusive create of `claimed/<id>.json`:

```python
fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
```

On Windows that is `CREATE_NEW`. The second opener gets `FileExistsError`, and the `except Exception` around the open returns `False`. That is the property the rename did not have: the race test in `tests/test_arms.py` (`test_many_threads_claim_one_arm_and_exactly_one_wins`, 150 rounds of 8 threads) exists because up to three of eight `os.rename` callers were told they had won. The same shape, through `pre_llm_call`, is `test_two_sessions_with_the_same_first_message_one_claims`. Both callers build the same path via `safe_join`, so two spellings of one file are not in play. A leftover `claimed/<id>.json` also makes a later `claim` return `False`, including when the winner could not delete `armed/<id>.json`.

Two limits sit beside that, and neither lets a second caller win:

- "True for exactly one caller, ever" is stronger than the code. If `armed/<id>.json` is already gone when the winner unlinks it, the winner deletes `claimed/<id>.json` and returns `False` (`test_claim_is_void_if_the_arm_vanishes_midway`). That interleaving has zero winners. `create_arm` can cause it: under its lock it deletes every armed file that is no longer waiting, and a file whose `claimed/` name already exists is no longer waiting.
- If the write of the copied bytes raises, `claim` still returns `True` and keeps the name. The claimed file can be empty until `write_ledger` replaces it. A second `claim` still loses.

The progress log's "450 rounds" is the log text. The test in the tree runs 150 rounds.

## Can any of the five rules reach `next_call` while a replay is bound?

Yes. Rule 2 can. Rules 1, 4 and 5 do not, for a session whose replay binding is still installed. Rule 3 does not on the path it describes; it has the same gap as rule 2 if `_route` raises in the window below.

A call that enters `_route` while that session's replay binding is already in `_bindings` gets `_DO_REPLAY` or `_DO_REFUSE` and returns a string. `ReplayIndex.answer` has no tool callback. Unbound sessions still call `next_call` while some other session is replaying. That is the default under these rules: only a routing failure refuses every session.

1. **Retired turns.** While the binding is installed, `_route` matches `mode == "replay"` first and refuses every other `turn_id`, a finished turn, and the background-review fork. The retired list is consulted only after that binding is gone. Replacing the binding does put `(session_id, turn_id)` on `_retired`, and a late call is refused. `_trim` drops that map at 512 entries, oldest first. After the entry is gone, and the session has no current replay binding, the late call takes the pass-through below and calls `next_call`. That breaks "still refused" for a turn that has already been dropped. It is not a call against a binding that is still installed.

2. **Live calls before the bind.** This one does call `next_call` after the replay is bound. `_route` counts the call and returns `_PASS` only when it does not see a replay binding. `tool_execution` then calls `next_call(args)` with no second look. Those two steps are not one critical section. `pre_llm_call` runs on Hermes's hook worker. On the 30 second timeout Hermes abandons that worker and starts the turn (`_run_hook_callback_bounded`: the thread is not joined and the callback is not cancelled). The worker can still reach `_bind_replay`. Any tool call that already passed `_route` then enters `next_call` with the binding in place. Concurrent tool batches (`tool_executor.py`, up to 8 workers) can have several such calls. A call that has not yet taken `_lock` inside `_route` sees the binding and does not call `next_call`. The binding is published before the cassette loads, so the slow load is not part of this window. The live-count does record the overlap: the increment happens before the lock is released, and `_bind_replay` appends an INVALID reason when the count is non-zero. The reason does not stop the call. The count itself is capped at 512 keys. A turn whose entry was evicted before the claim gets no INVALID reason. The test (`test_live_calls_before_a_late_bind_make_the_run_invalid`) finishes both live calls before `pre`, so it does not cover the overlap.

3. **Routing raises.** If `_route` raises and `_replay_active` is non-zero, the callback returns the refused string and does not call `next_call`. If the flag is still 0, it falls through to the same `return next_call(args)`. A bind between that read and the call is the rule 2 gap again. `_route` as written does not raise on ordinary ids. The flag is an `int` updated under `_lock` and read outside it.

4. **The two tries on the answer path.** Neither calls `next_call`. The outer `try` returns `_replay` or `_refuse`, and on `Exception` returns a cassette-miss string. `_replay` has its own `try` and returns a miss string. The "unlogged internal error adds an INVALID reason" half is narrower than the sentence. An exception inside `_replay` is swallowed there and becomes a miss event, with no line added to `invalid`. If that event log also raises, the miss is returned and `invalid` is still unchanged. `invalid` is appended only when `_replay` or `_refuse` themselves raise, and it reaches the parent only on the next `_write_ledger` (`_bind_replay`, or `_finish_replay` before it clears the events).

5. **The 5,000-event cap.** The call is still answered from the cassette or as a miss. Past the cap, `_log_event` increments `events_dropped` and the ledger write adds an INVALID reason. No branch calls `next_call`.

## S1 fixes, as they stand in this code

The earlier review's binding fixes are in this tree. The claim is the exclusive create above, not message equality by itself. Two first CLI turns with the same text still produce one binding. The binding is `(session_id, turn_id)`. `post_llm_call` sets `finished` and keeps refusing; the next `pre_llm_call` is what replaces it. `ARM_TTL_SECONDS` applies in `waiting_arms` only. A claimed binding has no time check on the tool path. Other turns of a replay session get the refused string, and that string is not a cassette miss. The registered middleware takes `**kwargs`. The replay and refuse branches catch `Exception` inside the callback. `BaseException` still propagates. Hermes runs the real tool when middleware raises `Exception` before `next_call`; a `BaseException` leaves `_run_execution_chain` instead of taking that fall-through.

Memory prefetch is unchanged. Decision 9 still records it as a live read on the replay turn that does not go through `tool_execution`. The ledger fields the parent needs (`session_id`, `turn_id`, `final_answer`, `invalid_reasons`) are written here. The comparison with the child's stdout is block D, which is not in this tree.
