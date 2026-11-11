# Block G — public README and catalog draft

Written 2026-11-11. Read `README.md`, `plugin.yaml`, `docs/catalog-entry.yaml`, `snapshot.py`, `__init__.py` (`register`, the terminal check, the record path), `cli.py` (`preflight`, `replay`), `verdict.py`, `arms.py` (`create_arm`, `process_yolo`), and the Hermes 0.23.1 catalog schema and neighbours (`plugin-catalog/README.md`, `completion-proof.yaml`, `session-lens.yaml`, `hermes_cli/gateway_runtime_start.py`, `tools/approval.py`). Then `docs/TESTED.md`, `docs/SECURITY.md`, `docs/VALIDATION.txt`, and the progress log in `docs/PLAN.md`. Did not run the tests. No start id was given, so these findings are not numbered as G ids.

The README is mostly the log. The install footnote, the untested list, the unclaimed `rm` / `rmdir` run, the provider sentence, the outside-workspace search results, and the middleware seeing every gateway session are all in the Safety section and match the code or a TESTED row. The two sample boxes match the block G rows (gpt-oss:20b PASS, 11 s, 1 hit and 1 synthesized; llama3.1:8b FAIL, 8 s, the `netstat` miss). The `(7 recorded)` clause is what `path_summary` adds when the hits are a subsequence of the recording, and the `-z` record row is 7 calls. `requires_hermes: ">=0.23.1"` is the same in `plugin.yaml` and the catalog draft. `register()` registers `pre_llm_call`, `post_llm_call`, and `tool_execution` middleware, and the capabilities blocks match. Commands and the CLI command are not capability keys in the schema. The `sha` of forty zeros is called out as a placeholder. It is valid hex, so that comment has to stay out of the PR file.

## Would get the entry marked down or sent back

**1. "The plugin reads no credentials, `.env` files or `auth.json`."**

`snapshot.capture` opens any regular file a recorded `read_file` or `search_files` result named, when the path is inside the session working directory. `_skip_reason` drops hidden segments and `_SECRET_GLOBS` (`*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `*.kdbx`, `*.keystore`, `id_rsa*`, `id_dsa*`, `id_ecdsa*`, `id_ed25519*`, `credentials`, `credentials.*`, `*.env`, `secrets.json`, `secrets.yaml`, `secrets.yml`, `secrets.toml`). `auth.json` is none of those. A workspace file with that name is read from disk, then passed through `redact_sensitive_text`. `.env` is skipped, because the name is hidden and because `*.env` is in the list. The same sentence's "no credentials" is wider than that list (`token.json` is read too). Catalog rule 11 names `auth.json` as a file a plugin does not read. The catalog description says secret-looking names are skipped and does not repeat this sentence, but the catalog page renders this README. The live demo never named an `auth.json`; the sentence is still about what the plugin does.

**2. "It refuses to run against a gateway that was itself started in yolo mode."**

`preflight` refuses when `pong["yolo"]` is true. The ping handler sets that with `arms.process_yolo()`, which is the gateway process's current `HERMES_YOLO_MODE` (`1`, `true`, `yes`, `on`). `tools/approval.py` freezes the same variable into `_YOLO_MODE_FROZEN` at import and then ignores later changes to the environment. `docs/SECURITY.md` already says the refusal does not cover a gateway that removed the variable after that import. The README's known-issues line says only that the check has not been run against a gateway really started with the variable set. A normal `hermes gateway restart` is a different case again: `spawn_unmanaged_gateway` deletes `HERMES_YOLO_MODE` in `DAEMON_STRIPPED_ENV` before the daemon process exists, so that daemon is not frozen yolo and the ping does not refuse it. The unit tests set the variable on the process that writes the pong. They do not read `_YOLO_MODE_FROZEN`.

## Worth fixing

**3. "gpt-oss:20b: PASS ×10 and DIVERGED ×1" and "once in 13 graded runs."**

Named `chat -Q` gpt-oss:20b rows in the current `docs/TESTED.md`: PASS at the block F table (6: the 13 s run, two debug runs, the 8 s run, the 11 s run, the 6 s run), the gateway-restart row (1), the post-review table (2), the block G sample (1), and the row after the `plugin.yaml` restart (1). That is 11 PASS. DIVERGED ×1 and INVALID ×2 match. llama3.1:8b FAIL ×4 matches (three `terminal` misses, one with no tool calls). One of those three is the run launched with `HERMES_YOLO_MODE=1` in the shell; the README's FAIL sentence does not say so. The tampered cassette is one more gpt-oss FAIL. Graded, leaving out the two INVALID runs, is 11 + 1 + 1 = 13, so "once in 13 graded runs" matches the file and "PASS ×10" does not. The two sentences cannot both be a tally of the same rows.

**4. "Arete replaying its own cassette came up DIVERGED once in 4 runs."**

That is the replaced `hermes -z` table: PASS, DIVERGED, PASS, PASS. No Arete replay through `chat -Q` is in `docs/TESTED.md`. The tested section labels those rows as the earlier child. This sentence is in Known issues, with no child named.

**5. "What else makes a run INVALID."**

The paragraph covers a leftover arm, live calls before the claim, a missing or unfinished ledger, a session id that does not match, an answer that does not match (redacted copy accepted), a non-zero exit, and a timeout. `cli.replay` also adds a reason when `chat -Q` prints no session id, and when the ledger names no session. Those two are how a foreign ledger stays INVALID. `verdict.compute_verdict` adds "no final answer was captured" when the answer is empty. The ledger's own reasons, which `replay` appends, include calls still running live when the cassette was bound, the call log cut short past 5,000, the cassette failing to load, an internal error on the answer path, and a replay retired because many later runs started. The printed verdict shows the reason. The README presents the list as the list.

**6. "the middleware sees every session's tool calls but passes unbound sessions through untouched."**

That is the catalog description. For a session with an id, the pass-through increments an in-memory count so a late claim can be marked INVALID. The README says this. The catalog line says untouched. The count is dropped past 10,000 turns, which the README does say, pointing at the block C reviews. `completion-proof.yaml` and `session-lens.yaml` put this kind of behaviour in the disclosure sentence itself.

**7. "The plugin keeps the results of read-only tools" and the terminal sentence.**

A result longer than 2 MiB, or recorded text past 12 MiB for the turn (`MAX_RESULT_CHARS`, `MAX_RECORDED_CHARS`), is not stored. The tool is counted as live and the cassette gets the "may not replay cleanly" warning. After one `terminal` command outside the read-only list, `terminal_tainted` stops every later `terminal` result of that turn from being kept, including a later `cat` or `git diff`. The README says the output is kept for a command on the list.

**8. "gitleaks clean over the full history" and "251 pass and 1 is skipped."**

`docs/SECURITY.md` records the gitleaks run over 4 commits. `git log --oneline` is 10 commits now, and the README, `plugin.yaml`, and the catalog draft are still uncommitted. The README points at that file and still says the full history. SEC-10 is the rerun on the frozen commit. There are 251 `test_*` methods under `tests/`, and `test_symlink_escape` is one of them (`skipTest` when a symlink cannot be created). "251 pass and 1 is skipped" is one more result than that set. The suite was not run for this review.

**9. The wrong-skill command in the walkthrough.**

`hermes cassette run tinyshop --model gpt-oss:20b --skill tinyshop-layout` is offered as a way to see a red result. The only logged runs of `--skill` are the two `hermes -z` rows. The tested section says the route has not been repeated through `chat -Q`. The walkthrough does not. The record commands above it do match the logged PowerShell route: `hermes chat -Q "--query=/cassette record …"` and then `hermes chat -Q --resume=<id>`. `docs/TESTED.md` still says, under "Not tested yet", that only the `-z` + `--resume` route was used for recording. The later record table contradicts that line. The README's own wording matches the later table.

## Nit

**10. The terminal allowlist is stricter than the sentence.**

The commands match: `cat`, `type`, `dir`, `ls`, `rg`, and `git log` / `show` / `diff`, with no character from `;&|<>`$()` or a newline. `_readonly_terminal` also returns false for `background`, `pty`, `rg` with `--pre`, and `git` with `--output`. A replay of those is a miss. The snapshot caps in the README match the code: 512 KB per file, 8 MB total, hidden paths, binary (a NUL in the first 8192 bytes), non-UTF-8, a symlink whose real path leaves the workspace, and a skip when redaction raises. Non-regular and unreadable files are skipped as well.

**11. Tone.**

The opening calls a cassette a regression test and says a replay costs only the model call. The same paragraph points at the Safety caveat, and the provider sentence later says a cloud model is sent the request and the recorded results. That is enough. I would not send the page back for tone. The install prompt's `known_issues` list is shorter than the README: it has Windows-only, the live unclaimed run, the recording route, non-determinism, and the search approximation. Egress to a named cloud provider is in the description, not in that list. `platforms: []` matches the schema's "empty means all" and this repo's `docs/RULES.md`. `completion-proof.yaml` instead sets `platforms: [linux]` where other platforms are untested. Empty is not a schema error. The known-issues line is what tells an installer the other platforms were not run.
