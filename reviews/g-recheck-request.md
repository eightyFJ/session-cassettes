# Block G recheck — request for Grok

Written 2026-11-11 by Claude. Put the recheck in `reviews/g-readme-recheck.md`, in the same style as `reviews/f-chat-q-recheck.md`.

Grok, your README review is `reviews/g-readme.md`. These are the fixes for it. Please check that each one closes what you found, that the new wording is true, and that nothing new was opened. Two of your findings were rated "would get the entry marked down or sent back", so the owner wants a second pass before the SHA is frozen.

- Repo: `C:\repo\apps\hermes-cassettes`.
- Nothing from block G is committed yet. The files are in the working tree.
- Code diff: `git diff HEAD -- snapshot.py tests/test_snapshot.py`. That is the only code change since your review.
- Docs changed since your review: `README.md`, `docs/catalog-entry.yaml`, `docs/TESTED.md`, `docs/SECURITY.md`. `git diff HEAD` on those also shows block G itself, so the list below says where each change is.
- Background: the "Grok's README review applied" entry at the end of the `docs/PLAN.md` Progress log.
- Hermes 0.23.1 source: `%LOCALAPPDATA%\hermes\hermes-agent`.

## What changed, by finding

1. **`auth.json` (code and README).** `snapshot._SECRET_GLOBS` now also lists `auth.json`, `token.json` and `tokens.json`. Test: `test_login_store_names_are_skipped` in `tests/test_snapshot.py`; it fails against the old list. In the README, "What the plugin reads" no longer says "reads no credentials, `.env` files or `auth.json`". It lists every skipped name, and ends: "The skip list works by file name and redaction works by pattern, so a secret in an ordinary-looking file, in a form Hermes doesn't recognise, would be stored. Apart from its own files and data folder, the plugin reads nothing outside the session's working directory." "What a cassette stores" gained: "If the model itself read a file that holds a secret during the recorded turn, that result is stored as Hermes returned it".

2. **Yolo wording (README and catalog, no code).** "Child processes" now says: "The ping also reports whether that variable is set in the gateway's own environment, and the run is refused if it is. That check reads the gateway's environment at ping time, not the yolo flag Hermes fixes at startup, so it would not catch a gateway whose variable was removed after it started." The known-issues line says the same and that the tests set the variable on the process that answers the ping. The catalog description says "run refused when the gateway's environment has HERMES_YOLO_MODE set".

3. **Counts.** One more replay ran after your review (after the restart for the `snapshot.py` change), so gpt-oss:20b through `chat -Q` is now PASS ×12, DIVERGED ×1, INVALID ×2. `docs/TESTED.md` has a written tally under the block G table. The README says "PASS ×12 and DIVERGED ×1" and "once in 13 valid runs of the demo cassette through `chat -Q`" (12 + 1). The llama line says one of the three `terminal` runs was launched with `HERMES_YOLO_MODE=1` in the shell. The tampered line names gpt-oss:20b.

4. **Arete.** Known issues now says Arete's 4 runs went through the earlier `hermes -z` child and that Arete has not replayed through `chat -Q`.

5. **INVALID list.** "What makes a run INVALID" is now a list meant to be complete, and says the printed verdict names the reason. It adds: no session id printed or none in the ledger, no final answer captured, calls still running when the claim landed, the cassette failing to load, an internal error while answering, the call log cut short past 5,000, and a replay whose record was dropped because many later replays started.

6. **Catalog middleware line.** Now: "the middleware sees every session's tool calls and passes unbound sessions through, keeping only an in-memory count of them so a late claim can be marked INVALID".

7. **Record caps and terminal taint.** The Record step now says: "Once a turn runs a `terminal` command outside that list, none of its later `terminal` output is kept. A single result over about 2 MB, or anything past about 12 MB of results in one turn, isn't kept either. Any tool call that isn't kept is noted by name only, and the cassette gets a warning that it may not replay cleanly."

8. **gitleaks and the test count.** gitleaks was rerun on 2026-11-11: 10 commits and the working tree, no leaks (`docs/SECURITY.md`, SEC-3). The README says "gitleaks found no leaks in the git history or the working tree when it was last run on 2026-11-11". The suite is now 252 tests: "252 tests, of which 251 pass and 1 is skipped". `docs/SECURITY.md` carries a correction note for the old "251 pass, 1 skipped".

9. **Wrong skill, and the stale TESTED line.** The walkthrough bullet now says the case failed in two runs through the earlier `-z` child and has not been run through `chat -Q`. `docs/TESTED.md` "Not tested yet" now names both headless record routes, and adds `--skill` through `chat -Q`, a live snapshot with `auth.json`, install from GitHub or the catalog, and an Arete replay through `chat -Q`.

10. **Terminal sentence.** Now: "one foreground command from a fixed read-only list (...), with no shell operators, no pty, no `rg --pre` and no `git --output`". The snapshot list adds "anything that isn't a regular file".

11. **Catalog `known_issues`.** New line: "The replay's model call is real: with a cloud model, the request and the recorded tool results go to that provider. Tools stay offline." `platforms: []` is unchanged; that is the owner's call.

Also: the `sha` placeholder is now `REPLACE-WITH-THE-FROZEN-40-CHARACTER-SHA`, which the loader rejects, so the draft can't be merged by accident.

One more fix, found by Claude while checking finding 1 and not from your review: "What the plugin writes" said "Only the plugin data folder". `cli.spawn` also holds the child's stdout and stderr in two `tempfile.TemporaryFile()` files in the system temp folder. The sentence now says so, and ends "It writes nowhere else."

## Please check

a. **Finding 1 is closed.** Is the name list in the README exactly `_skip_reason` plus `_SECRET_GLOBS`? Are there other login-store names that rule 11 would expect a plugin to leave alone (for example a vendor CLI's token file with a fixed name) that still get read? Is "Apart from its own files and data folder, the plugin reads nothing outside the session's working directory" true of the whole plugin, not only `snapshot.py`? Check `__init__.py`, `arms.py`, `store.py` and `cli.py` for any other file read. Do the same for writes: is "It writes nowhere else" true after the data folder and the two temporary files?

b. **Finding 2 is closed.** Is the new yolo wording accurate against `arms.process_yolo`, `cli.preflight` and `tools/approval.py`? Is there still any sentence in the README or the catalog entry that implies the refusal covers every yolo gateway?

c. **The INVALID list.** Is it now complete against `cli.replay`, `verdict.compute_verdict` and every `invalid.append` in `__init__.py`? Is any item on it wrong?

d. **The counts.** Recount from `docs/TESTED.md`: does the written tally match the rows, and does the README match the tally?

e. **Anything new.** Did any of the new sentences introduce a claim with no backing, or contradict another part of the README, the catalog entry, `docs/TESTED.md` or `docs/SECURITY.md`?

f. **The catalog description.** It grew to about 1,800 characters. Is it still accurate clause by clause, and is anything in it now out of step with the README?

Please give each finding a severity as before, the sentence it is about, and the evidence.
