# Block C recheck — inflight count, retired cap, swallowed error

Written 2026-11-10. Read `__init__.py` and `tests/test_plugin.py` before the "Grok review of block C, fixes applied" entry in `docs/PLAN.md`. Did not re-run the tests.

## 1. Can a call reach `next_call` after its session is bound and still not be INVALID?

Not by slipping past the count. It can still be missing from the ledger the parent reads.

`_route` and `_bind_replay` share `_lock`. A call with a session id that does not see a replay binding increments `_inflight[sid]` and only then returns `_PASS`. `_bind_replay` reads that number under the same lock, **before** it publishes the binding. A worker still waiting on the lock observes the binding and returns `_DO_REPLAY` or `_DO_REFUSE`. Eight concurrent workers are eight increments. Each one that has already passed `_route` is inside the snapshot. Each one that has not is refused or replayed and does not call `next_call`.

The snapshot is a local `running`. The `finally` in `_pass_through` can drop `_inflight` to zero after the lock is released and before the append. That does not clear `running`. The append still happens.

What the append does not do is publish the reason under that lock. After the lock, `_bind_replay` calls `_cwd()` and only then appends the live-count reason and the in-flight reason. `post_llm_call` can run on the agent thread as soon as the last tool returns. `_finish_replay` writes `state: done` from whatever `b.invalid` holds at that moment. If the last live tool returns during `_cwd()`, or in the gap before those appends, the done file has neither reason. The appends then land on the binding and match leftover (b) in the progress log: they never reach that write. `_finish_replay` will not write again.

That is the timeout case this fix is for. The abandoned `pre_llm_call` worker is the thread inside `_bind_replay`. The tools Hermes already started are the other threads. A fast tool and a slow `session_cwd()` are enough. The later `_write_ledger(..., "claimed")` on the hook thread can overwrite that done file with a claimed file that does contain the reason. A parent that already accepted `state: done` has the clean copy. `compute_verdict` with empty `invalid_reasons`, no miss events, and pins that match the answer is PASS.

A call that has not yet entered `_route` cannot call `next_call` after the binding is visible. The hole is the call that was already counted and already inside `next_call`, whose reason loses the race to the done write.

## 2. Can `_inflight` leak, go negative, or stick?

It does not go negative. The `finally` stores a new value only when `left > 0`. Otherwise it pops the key.

For a session id Hermes actually sends, a string, `_text` in `_route` and `_text` in `_pass_through` return the same value. The decrement hits the key the increment wrote. An object whose `__str__` changes between the two calls would leave the route key stuck and pop or shrink the other key. The tool path coerces `session_id` to `str` before the callback.

The routing-raised path does not touch the counter. It sets `_PASS_UNCOUNTED` and calls `next_call` directly. No increment, so no leak and no negative. The same is true of an empty session id, which `_route` returns as `_PASS_UNCOUNTED` before the increment.

`BaseException` inside `next_call` still runs the `finally` and drops the count. `ValueError` is what `test_in_flight_count_drops_when_the_live_tool_raises` covers. The same `finally` covers `BaseException`.

The count sticks if `BaseException` is raised after the increment and before `_pass_through`'s `try`. That stretch is the `return _PASS`, the lock release, and the two `if`s in `tool_execution`. Nothing there catches `BaseException`, and `_pass_through` has not started, so nothing decrements. The session's count stays at least 1. Later claims on that session keep adding an in-flight INVALID reason. `except Exception` around `_route` does not create this leak: the increment is the last operation inside the lock, so an `Exception` from `_route` happens before it.

## 3. Is the log's residual the only one left?

"(a) `_route` raises while no replay is bound, then a bind lands before `next_call`" is a real residual, and it is stated fairly. That call is `_PASS_UNCOUNTED`. It is not in `_inflight` or `_live_calls`. Nothing marks the run.

It is not the only one.

- **The done write can beat the new reason.** Section 1. The log's (b) describes an INVALID reason that arrives after `state: done`. The in-flight append is in that position on purpose: it is outside the lock, after `_cwd()`. (b) is not only a later replay error. It is this fix's own mark.
- **A call that has already returned is not in `_inflight`.** Only `_live_calls` remembers it, and that map still drops the oldest entry at `_MAX_GUARDED` (10,000). The log says the in-flight count works even when the live count was evicted. That is true while the tool is still inside `next_call`. After it returns, eviction still hides it, and a later bind adds no reason.
- **(c) and (d) still match the code.** A `BaseException` from the replay callback propagates. Hermes's middleware falls through to the real tool only on `Exception`. Memory prefetch still does not go through `tool_execution`. (c) does not mention the stuck `_inflight` count in section 2, which is a different consequence of `BaseException`.

`_PASS_UNCOUNTED` for an empty session id also calls `next_call` with no count while some other session is replaying. That call is not the bound session's. `_pre` refuses to bind an empty session id, so it is not a second way for the replay's own call to escape.

## 4. Do the new tests run the race?

`test_a_claim_landing_while_a_live_call_runs` is a single-thread imitation. `next_call`'s side effect clears `_live_calls` and calls `pre`, which runs `_bind_replay` to the end, including both appends, before the tool returns. The `finally` cannot beat the append, and `post_llm_call` cannot write `done` in that gap. It does show that a claim overlapping `next_call` reads `_inflight` and that wiping `_live_calls` does not hide that one ordering. It does not start 8 workers, does not interleave the two lock holders, and does not finish the call between the release and the append.

`test_in_flight_count_drops_when_the_live_tool_raises` checks the `finally` for `ValueError` on one call. It does not raise `BaseException`, and it does not raise from `_route`.

`test_retired_replay_turns_outlive_the_small_tables` patches `_MAX_TRACKED` to 2, retires five turns, and checks the oldest is still refused. That fails if `_retired` were still trimmed with the 512-cap tables. It never fills `_MAX_GUARDED`, so the new cliff is untested.

`test_exception_inside_the_answer_is_a_miss_and_makes_the_run_invalid` is a real test of the rule 4 fix. `ReplayIndex.answer` raises inside `_replay`'s `with b.lock`, the lock is released, the new `except` appends the reason, and `post` writes it. One thread, one call. That is the right shape for this bug.

`test_bookkeeping_tables_stay_bounded` patches `_MAX_GUARDED` to 8, runs 50 pass-throughs, and expects `_live_calls` to stay within 8 and `_inflight` to be empty. It covers the cap and the decrement. It does not cover a bind.

## 5. Did this break `_route`, record mode, or `_trim_locked`?

No. Replay is still matched first, then the retired pair, then an unfinished record turn with the same `turn_id`. Record still returns `_DO_RECORD` before any in-flight increment, and `_record` still calls `next_call` itself. A session with a pending record takes `_begin_record` and does not claim, so a record tool in flight is not something `_bind_replay` is supposed to see.

An empty session id now returns `_PASS_UNCOUNTED` instead of falling through to `_PASS`. It still skips both counters and still calls `next_call`. The retired check is above that return. Real bindings have a non-empty session id, so an empty id is not a retired key.

`_trim_locked` is unchanged: same three drop tiers, and an unfinished replay binding is still kept. `_drop_locked` now trims `_retired` at 10,000 rather than 512. `_trim(self._status)` and the pending-record cap stay at 512. `_live_calls` is trimmed at 10,000, and the current key is moved to the newest end first, so a turn does not evict its own count by logging another call.
