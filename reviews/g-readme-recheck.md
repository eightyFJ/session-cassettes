# Block G recheck — README wording

Written 2026-11-11. Read `snapshot.py`, `cli.py` (`spawn`, `replay`), `arms.py` (`process_yolo`, `code_version`), `__init__.py` (every `invalid.append`), and `verdict.py` before the new README sentences and before the progress-log entry. Counted the `chat -Q` rows in `docs/TESTED.md`. Checked the yolo freeze against Hermes 0.23.1 `tools/approval.py` (`_YOLO_MODE_FROZEN`) and the SHA check against `hermes_cli/plugin_catalog.py` (`_SHA_RE`). Did not run the tests or gitleaks.

The two send-back findings are closed. The name list matches the skip list, and no README or catalog sentence still says the refusal covers every yolo gateway.

## 1. Does the auth.json change close the false "reads no credentials" sentence?

The false sentence is gone. The names in the README are `_SECRET_GLOBS`, and the other bullets are the other skips in `capture`.

`_skip_reason` returns "hidden path" when any segment starts with `.`, then matches the lower-cased basename against:

```python
_SECRET_GLOBS = (
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.kdbx", "*.keystore",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "credentials", "credentials.*", "*.env",
    "secrets.json", "secrets.yaml", "secrets.yml", "secrets.toml",
    "auth.json", "token.json", "tokens.json",
)
```

The README lists those twenty-two patterns, then hidden paths, binary and non-UTF-8, non-regular files, symlinks that leave the folder, 512 KB, and 8 MB. `test_login_store_names_are_skipped` writes `auth.json`, `Auth.JSON`, `token.json`, `tokens.json`, and `server/auth.json`, and expects an empty snapshot. The lower-casing makes `Auth.JSON` hit `auth.json`. A redaction failure is the next sentence, not a missing bullet.

Rule 11's `auth.json` and a file whose basename is `token.json`, `tokens.json`, `credentials`, or `credentials.*` are skipped. A fixed login name that is not on that list is still opened when a recorded search names it inside the workspace. `application_default_credentials.json` does not match `credentials` or `credentials.*`. `client_secret.json` and `service-account.json` do not match either. The new sentence says the skip is by file name and that a secret in an ordinary-looking file would be stored. That is the residual, and it is stated.

**Worth fixing.** "Apart from its own files and data folder, the plugin reads nothing outside the session's working directory."

`code_version` reads the seven plugin files next to `arms.py`. Cassette, arm, ledger, pong, and run JSON are read under the data directory. Snapshot reads stay inside the workspace, after the realpath check. `spawn` also reads the two `tempfile.TemporaryFile()` handles back, via `_read_back`. Those files are in the system temp folder. They are not the plugin's source and not the data folder. The writes paragraph already says the child's output is held there. The reads sentence does not except them.

**The writes sentence holds.** Plugin writes are under the data directory (cassettes, runs, armed, claimed, pongs, `arm.lock`, and the `mkstemp` file `atomic_write_json` creates beside its target) and those two temporary files. `arm.lock` is inside the data folder, so "nowhere else" still fits. The temporary files are closed when `spawn` returns, which is before the verdict is printed. "Deleted when the run ends" is that close.

## 2. Does the yolo wording match the check?

Yes. Nothing in the README or the catalog entry still says the refusal covers every yolo gateway.

`process_yolo` is the current process environment, truthy when the stripped value is `1`, `true`, `yes`, or `on`. The ping handler passes that into the pong. `preflight` raises `CliError` when `pong["yolo"]` is true, before the arm exists. `tools/approval.py` sets `_YOLO_MODE_FROZEN` once, at import. The Child processes paragraph and the known-issues line both say the check is the environment at ping time, and that a gateway which later lost the variable is not refused. The catalog description says "run refused when the gateway's environment has HERMES_YOLO_MODE set", which is that check.

`docs/SECURITY.md` still has the older line "refuses a gateway in yolo mode", and the next sentence is the same caveat. That file is not the catalog page.

## 3. Is the INVALID list complete?

Yes, against the reasons that are actually appended. One phrase is looser than the code.

`cli.replay` adds: timeout, non-zero exit, arm left and never claimed, arm left after a claim, no ledger, ledger not `done`, answer mismatch, no session id on stderr, empty ledger session id, and a session-id mismatch. It also copies `ledger["invalid_reasons"]`. `compute_verdict` adds "no final answer was captured" when the answer is empty, and any reason forces INVALID. `__init__.py` appends: calls that ran live before the bind, calls still in flight, "the cassette could not be loaded: …", "an internal error interrupted the replay of a tool call", the call log cut short after `MAX_EVENTS` (5,000), and "this replay was retired because many later runs started". Each of those has a bullet. A timeout skips the session-id check and still carries the timeout reason.

An exception inside `_route` while a replay is bound returns the refused string and does not append a reason. That run is not INVALID for that reason, so it does not belong on the list.

**Nit.** "The replay's record was dropped because many later replays started before it finished." The code clears that run's in-memory cassette (`index = None`) when more than the loaded cap (`MAX_LOADED_REPLAYS` is 16) of unfinished replays are holding one, and appends the retired reason. The cassette file on disk stays.

## 4. Do the counts match the rows?

The written tally and the tested-section counts match the rows. "Once in 13 valid runs" is the soft sentence.

`e2e-probe` gpt-oss:20b through `chat -Q`: PASS at the block F table (6), the named gateway-restart row (1), the post-review table (2), the block G sample (1), the `plugin.yaml` restart (1), and the `snapshot.py` restart (1). That is 12. DIVERGED is the one `server/__init__.py` row. INVALID is the two redaction rows. llama3.1:8b FAIL is four rows, and one of them is the shell `HERMES_YOLO_MODE=1` run. `e2e-tampered` gpt-oss FAIL is one row. The "re-enabled and restarted afterwards: PASS" line names no model, and the tally excludes it. The README's "PASS ×12 and DIVERGED ×1", the llama line, and the tampered line match that.

**Worth fixing.** "gpt-oss:20b came up DIVERGED once in 13 valid runs of the demo cassette through `chat -Q`." The 13 is 12 PASS + 1 DIVERGED, so "valid" here means gpt-oss runs of `e2e-probe` that were not INVALID. The same cassette has four llama FAIL rows that are also graded and not INVALID. Counted with those, a DIVERGED result showed up once in 17 non-INVALID `chat -Q` runs of `e2e-probe`. The tested section does not use "valid", and its ×12 / ×1 split is the clear one.

Arete's four runs are still the `-z` table only, and the known-issues line says Arete has not replayed through `chat -Q`. The walkthrough says the wrong-skill case was two `-z` runs and has not been run through `chat -Q`. `docs/TESTED.md` "Not tested yet" says the same, and it no longer says recording was only `-z`.

## 5. What the new sentences add

**Worth fixing.** Catalog `known_issues`: "The replay's model call is real: with a cloud model, the request and the recorded tool results go to that provider. Tools stay offline."

The first half matches the README. "Tools stay offline" does not. The line above it says an unclaimed run uses real tools and is graded INVALID afterwards. The README's limit is "A replay is offline only when its result is not INVALID."

The record caps match the code: `MAX_RESULT_CHARS` and `MAX_RECORDED_CHARS` are `2 * 1024 * 1024` and `12 * 1024 * 1024` characters, and one `terminal` command outside the list sets `terminal_tainted` for the rest of the turn. "About 2 MB" is that character cap. The terminal sentence now includes foreground, no pty, no `rg --pre`, and no `git --output`, which is `_readonly_terminal`.

"252 tests, of which 251 pass and 1 is skipped" matches the method count: the previous 251 `test_*` methods plus `test_login_store_names_are_skipped`, with `test_symlink_escape` still the `skipTest`. The suite was not run here. The gitleaks sentence matches the 2026-11-11 rerun note in `docs/SECURITY.md` (10 commits and the working tree). Gitleaks was not run here.

The SHA placeholder `REPLACE-WITH-THE-FROZEN-40-CHARACTER-SHA` fails `_SHA_RE` (`^[0-9a-f]{40}$`). The comment that the loader rejects it is true.

## 6. Catalog description, clause by clause

The description matches the README on the recording, the three answer kinds, the four verdicts, plain JSON, stored tool results, a snapshot read inside the working directory, name-based skips including `auth.json` and `token.json`, redaction, two `chat -Q` children, no `-z`, no `--yolo`, the child env strip, refusal when the gateway environment has `HERMES_YOLO_MODE`, the model call going to the named provider, an unclaimed run graded INVALID, the in-memory count on unbound sessions, and no network, telemetry, self-update, or third-party dependencies.

It is behind the README on two points that the README now states and this description does not: the yolo check is not Hermes's frozen flag, and the child's output is held in two temporary files. Neither clause in the description says the opposite. The "Tools stay offline" known-issues line does, and that is the one to change.
