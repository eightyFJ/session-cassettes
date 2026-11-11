# Security checks

Results of the checks in `docs/PLAN.md` section 5a. Block F ran them on 2026-11-10 on native Windows 11 with Python 3.14.7 and Hermes Agent 0.23.1 (commit `3b421578`). The code checked is commit `aad5ca0` plus the uncommitted block F changes listed under SEC-9. Block H reruns SEC-1 to SEC-3 on the frozen commit (SEC-10) and adds those results here.

## Summary

| ID | Check | Tool | Result |
|---|---|---|---|
| SEC-1 | Hermes install scan | `hermes plugins validate . --install-deps` | **Pass.** Every check passes, `security scan — safe`, no warnings. Full output in `docs/VALIDATION.txt`. |
| SEC-2 | Static analysis | bandit 1.9.4, semgrep 1.185.0 (`p/python`) | **Pass.** semgrep: 0 findings from 151 rules. bandit: 0 high. One medium and eight lows, each explained below. |
| SEC-3 | Secrets scan | gitleaks 8.30.1, plus a check by hand | **Pass.** No leaks in the full git history (4 commits) or in the working tree. No personal paths or email in tracked files. |
| SEC-4 | Command injection | unit tests | **Pass** (block D, re-run after the SEC-9 change). |
| SEC-5 | Path traversal | unit tests | **Pass** (block B). |
| SEC-6 | Untrusted file parsing | unit tests | **Pass** (block B). |
| SEC-7 | Fail closed in replay | unit tests, read of the code | **Pass** (block C). |
| SEC-8 | Concurrency and atomic writes | unit tests | **Pass** (blocks B and C). |
| SEC-9 | AI security review | Claude Code `/security-review` | **One real finding, fixed:** the replay child ran in an auto-approving mode. Two smaller items are fixed too. Details below. |
| SEC-10 | Recheck the frozen commit | — | Block H. |

Unit tests: `python -m unittest discover -s tests` gives **249 pass, 1 skipped** (the symlink test, which needs Developer Mode). That includes the gateway-check tests and the fixes from Grok's review, both added after block F.

The scanners were installed with the owner's OK. bandit and semgrep went into a throwaway venv in Claude's scratchpad, not the system Python. gitleaks came from scoop. semgrep installed and ran on native Windows, so SEC-2 did not fall back to bandit alone. semgrep downloaded the `p/python` rules from its registry and ran with `--metrics=off`.

## SEC-1: Hermes install scan

`hermes plugins validate . --install-deps` exits 0. The manifest and fields parse. `requires_hermes` parses. The plugin loads. No Python dependencies are declared, so `--install-deps` installs nothing. The capability probe runs, and the declared tools (none), hooks (`pre_llm_call`, `post_llm_call`) and middleware (`tool_execution`) match what `register()` registers. The install security scan says `safe`. There are no core overrides and `"warnings": []`. Under `plugins.isolation: host` the validator rates the plugin host-ready.

**Found and fixed while doing this:** the first draft of the SEC-9 redaction fix imported a private Hermes helper (`agent.redact._is_secret_file_arg`). The validator flagged it under catalog rule 9 (public surfaces only). It was replaced with a local rule, and the validator is clean again (see SEC-9, item 3).

## SEC-2: static analysis

Command: `bandit -r . -x ./tests,./spikes`. It scanned 2,685 lines.

| Finding | Where | Severity | Why it stays |
|---|---|---|---|
| B310 `urlopen` | `demo/fixture-repo/client/login.py:10` | Medium | **Not plugin code.** It's the source of the fake "tinyshop" app that the demo model reads as text. The plugin never imports or runs it, and Hermes doesn't load it either: it isn't the plugin entry point and has no manifest. It never touches the network. |
| B110 `try/except/pass` ×4, B112 `try/except/continue` ×1 | `__init__.py:300,369,374` (as scanned), `arms.py:158,331` | Low | **On purpose: fail closed.** The `__init__.py` ones sit inside the replay error path, where an exception must never escape the middleware callback (a raise makes Hermes run the real tool, S1 trap 5a). Each one already returns a `cassette miss` or adds an INVALID reason. The `arms.py` ones skip one malformed arm file, and write a ledger whose claim is already won. |
| B404 `import subprocess`, B603 `subprocess` call | `cli.py:32`, `cli.py:194` | Low | The one allowed child process (SEC-4): an argument list, `shell=False`, stdin empty, and `.bat`/`.cmd` refused. A unit test checks that no other plugin file uses `subprocess`. |
| B105 hardcoded password | `verdict.py:25` | Low | False positive: `PASS = "PASS"` is the verdict name. |

semgrep `p/python` over the six plugin modules (`tests`, `spikes` and `demo` excluded) gives 0 findings.

**No network, telemetry or self-update.** A grep of the six plugin modules for `urllib`, `http.`, `httpx`, `aiohttp`, `socket`, `requests`, `subprocess`, `importlib`, `reload`, `os.system`, `os.popen`, `shell=True`, `eval(`, `exec(`, `pickle`, `yaml.load`, `marshal`, `__import__` and `ctypes` finds only `cli.py`'s `subprocess` (SEC-4) and a docstring in `store.py` saying it never uses pickle, eval or YAML.

## SEC-3: secrets

- `gitleaks git .`: 4 commits, about 400 KB, no leaks.
- `gitleaks dir .`: about 900 KB, no leaks.
- By hand: `git grep` for the owner's email, the Windows user folder and its Git Bash and forward-slash forms finds nothing in tracked files. No cassette, run file, ledger or log is tracked. They all live under `%LOCALAPPDATA%\hermes\plugin-data\session-cassettes`, outside the repo. The demo fixture holds no real secrets, only a fake app.
- **Not a leak, but the owner's call before the push:** `LICENSE` and the commit author carry the owner's name, and the commit metadata carries his email. `AGENTS.md` and `docs/PLAN.md` name the owner, Grok and Arete as the team (see PLAN section 7).

## SEC-9: AI security review

Claude Code's `/security-review` ran over the whole plugin. The diff was taken against the docs-only first commit, so it covered all the plugin code. This needed a temporary local `origin/HEAD`, which was removed afterwards. A second, independent pass checked each claim against the Hermes 0.23.1 source. Then the fix was tested live.

### Finding 1 (fixed): the replay child ran in an auto-approving mode

**What was wrong.** `hermes cassette run` started the child as `hermes -z <request>`. In Hermes 0.23.1, `-z` is an unattended turn that **auto-approves dangerous commands**. Hermes's own help says "approvals are auto-bypassed" (`hermes_cli/_parser.py:148-153`). The chain is: `hermes_cli/gateway_chat.py:236` sets `view.unattended`, the gateway runs the turn inside `finite_turn_scope(finite, unattended)`, and `tools/approval.py:388-394` (`_yolo_active()`) then returns true. Normally every tool call of a replay is answered by the cassette, so nothing runs. But if the claim fails, the turn runs live with real tools. Under `-z`, a flagged command such as `rm -rf` would have been approved with nobody asked. The claim can fail if the gateway hasn't loaded the plugin's hooks, if another session with the same first message wins the claim, or if a `pre_llm_call` times out. That also broke catalog rule 12 ("spawn a Hermes child in YOLO or non-interactive mode to dodge an approval"). And it made PLAN section 6's disclosure ("changes no approval settings") false.

**Fix.** The child is now `hermes [--model=M] [--provider=P] [--skills=S] [--in=DIR] chat -Q --query=<request>` (`cli.py`, `build_child_argv`). `chat -Q` is a finite single query that is **not** unattended. With no one to answer, a flagged command is denied by `approvals.single_query_mode`, which defaults to `deny` (`hermes_cli/config_defaults.py:1661`, `tools/approval.py:1302-1308`). Every value is still joined to its flag with `=` (SEC-4).

**Two changes that came with it.**
- *The printed answer can be redacted.* `AUTH_PORT = 8080` in the ledger printed as `AUTH_PORT=***`, and the stdout check marked 2 of 6 live runs INVALID. `redact_sensitive_text` of the ledger answer reproduces the printed text exactly. The check now accepts either the raw answer or that redacted copy (`cli.py`, `_same_answer`). Grading still uses the unredacted ledger answer. *(Corrected after Grok's review: this file first cited `gateway/session_results.py` as the place the printed copy is redacted. That module redacts only the stored copy, and `finish_result` returns the response unredacted. Where the printed text gets redacted was not pinned down. A print path that redacted differently would make a run INVALID, which fails safe.)*
- *A stronger claim check.* `chat -Q` prints `session_id: <id>` on stderr. If that id differs from the session that claimed the arm, the run is INVALID. That catches "another session won the claim" directly, not just through the answer text.

**Tested live** on 2026-11-10 (rows in `docs/TESTED.md`):
- 12 replays through `chat -Q`. `e2e-probe` with gpt-oss:20b: PASS ×6, DIVERGED ×1, and the 2 INVALID runs above (before the redaction fix). `e2e-probe` with llama3.1:8b: FAIL ×2. `e2e-tampered` with gpt-oss:20b: FAIL ×1. Every claim worked: no run was INVALID for a missing claim or a session mismatch.
- A deliberately unclaimed `chat -Q` run in a scratch folder, asked to run `rm -rf ./victim`. **Hermes denied it** as a "dangerous recursive delete". Under `-z` it would have been auto-approved.

**What the fix does not cover, said plainly.** In that same unclaimed run, the model worked around the denial with `rm ./victim/keep.txt` and `rmdir ./victim`. Hermes doesn't flag those, so they ran, and the folder was deleted. So `chat -Q` stops automatic approval of flagged commands. It does **not** make an unclaimed run harmless: any tool Hermes doesn't gate still runs live. The run is then graded INVALID ("no session claimed this run ... may have run with live tools"), but only after the child exits. The README must say this: a replay is offline only when its run is not INVALID.

**Follow-up, built the same night: a gateway check before the run.** Before writing the arm (after Grok's review; it first ran between the arm and the request), `cassette run` sends `/cassette ping <nonce>` through `hermes chat -Q` (`cli.preflight`). That makes no model call. Hermes hands the slash command to the plugin's handler in the gateway, and the handler writes `pongs/<nonce>.json` with `arms.CODE_VERSION`, a hash of the plugin files taken when the module loaded. The run goes ahead only if the pong arrives and its version equals the CLI's own. Otherwise the CLI stops with "Nothing was replayed", and the arm and the pong are removed. This turns the most likely claim failures into refusals before anything runs: a gateway that hasn't loaded the plugin, or one still running older code.

How we know the slash command runs in the gateway, not the CLI process: a recording started with `chat -Q --query="/cassette record f-record-q"` was saved by the next, separate `chat -Q --resume` process. The record request lives only in the gateway's memory.

Tested live: the gateway still on the block F commit, with no `ping` verb, was refused in 5 s. After `hermes gateway restart` the run passed. One plugin file changed after the gateway loaded was refused, with both versions shown. With the plugin disabled, `hermes cassette` doesn't exist. Unit tests: no pong, an old version, a malformed pong, the ping's argv (`chat -Q`, no `--model`), and the pong file removed.

What it doesn't cover: (a) a second process that still holds older code; Grok found that a forced reload in the same process clears the old hooks, middleware and commands before `register` runs again (`hermes_cli/plugins_ledger.py`, `_reset_after_unload_all`). (b) Another session with the identical first message winning the claim during the run, which the stderr session-id check reports afterwards. (c) A `pre_llm_call` that times out. All three are rare and are graded INVALID.

### Grok's review of the fix (`reviews/f-chat-q.md`), and what changed

Grok confirmed that `chat -Q` is never unattended, that a slash command under `chat -Q` is dispatched in the gateway (`prompt.submit`, `MessageEvent.is_command`), and that a forced reload clears the old hooks before `register` runs again. It found three real gaps, all fixed and tested (249 tests):

1. **`HERMES_YOLO_MODE` reached the child.** `spawn` copied the whole environment, and Hermes turns that variable into a yolo session (`hermes_cli/gateway_chat.py`, `_ENV_LAUNCH_FLAGS`) with no `--yolo` on the command line. `spawn` now drops it. Test: `test_the_child_never_inherits_yolo_mode`. Live: a replay started with `HERMES_YOLO_MODE=1` in the shell was claimed and graded as normal.
2. **The session check could be skipped.** It ran only when the child printed a `session_id:` line and the ledger named a session. Because the answer check also accepts the redacted copy, a foreign `done` ledger could then supply the graded answer. Now a missing id on either side makes the run INVALID. stderr is also read from its **end** (the id line comes last), so 16 MiB of earlier stderr can't push the line out. Tests: `test_no_session_id_on_stderr_is_invalid`, `test_stderr_keeps_its_end`.
3. **The arm waited during the ping.** `replay` armed first, then pinged (about 5 s), so another session with the same first message could claim the arm in that window. The ping now runs before the arm is created. Test: `test_the_ping_happens_before_the_arm_exists`.

All four new tests fail against the previous `cli.py`. Live after `hermes gateway restart`: gpt-oss:20b PASS ×2, llama3.1:8b FAIL (refused `terminal`).

**Accepted as is, said plainly:**
- **The user's own approval settings still apply.** `approvals.mode: off`, `single_query_mode: approve`, a permanent allowlist entry, or an approval already cached for the session would approve a flagged command on an unclaimed run. The plugin changes none of them. The `build_child_argv` docstring now says "under Hermes's default approval settings" rather than "never".
- **A forged pong.** Anyone who can write the plugin data folder can write a matching `pongs/<nonce>.json`. That's the same trust boundary as a tampered cassette: that writer could already edit Hermes's config.
- The ping child's exit code is not checked. The pong file is the proof.
- Under `plugins.isolation: host`, Grok did not trace whether the ping and `pre_llm_call` always run in the same interpreter. That mode is opt-in and untested.

### Item 2 (fixed): a cassette's request was not re-checked before a replay

Cassette files are untrusted (SEC-6), but `cli.replay` sent `cassette.request` to the child unchecked. An edited request that begins with `/` or `!` (Hermes treats those as commands) or holds an `@` reference (Hermes rewrites it before the plugin sees it) could never match its arm. The claim would fail every time. `cli.replay_refusal` now refuses those before any arm is written or any child starts. It applies the record-time rule (`_record_refusal`) plus the `/` and `!` prefixes. Tests: `test_requests_that_could_never_be_claimed_are_refused`, `test_tampered_request_is_refused_before_any_arm_or_child`.

Rated below the review's bar: whoever can write the plugin data folder can already edit Hermes's `config.yaml`. The plugin has no import or share feature. It's fixed because it's cheap and it closes a guaranteed claim failure.

### Item 3 (fixed): snapshot redaction was weaker than a live `read_file` for `config.yaml`

A live `read_file` redacts with `secret_file=True` for files Hermes treats as secret-bearing (`tools/file_tools.py:709-710`). Those are the `.env`-style names, the shell rc names, and a `config.yaml` under the Hermes home. The snapshot passed only `file_read=True`. The snapshot already skips every `.env` and rc name, since they are all hidden paths. That left a Hermes-home `config.yaml` inside a recorded workspace. The helper that recognises one is private, so the plugin now uses the stricter mode for every `config.yaml` and for Hermes's `config.yaml.good.*` and `config.yaml.corrupt.*` copies (`__init__.py`, `_hermes_redact`). A project's own `config.yaml` may be over-redacted. It's never under-redacted.

Checked against the real `agent.redact`: for a Hermes-home `config.yaml`, the plugin's output equals a live `read_file`'s. For a project `config.yaml`, it's at least as strict. For other files it's identical. Before the fix, a `DB_PASSWORD=` line in a secret-mode file was kept in clear. **Not a unit test:** the suite can't import Hermes, so this was a one-off script against the 0.23.1 source. **Not yet live in the gateway:** the gateway loaded `__init__.py` before this change, so recordings use the old redaction until `hermes gateway restart`.

### Areas the review checked and found sound

- **SEC-5:** `store.validate_name` (a `fullmatch` on `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`, with Windows device names rejected), `safe_join` (`resolve()` plus `is_relative_to`), arm ids that `fullmatch` 32 hex characters, and run-file names that are validated and filtered.
- **SEC-6:** every file goes through `read_json_file` (byte cap, strict UTF-8, `json.loads` only), and `from_dict`, `check_arm` and `check_ledger` check field types.
- **SEC-4:** argument list, `shell=False`, `.bat`/`.cmd` refused, an absolute resolved executable.
- **Fail closed:** the replay and refuse branches never reach `next_call`. Every Hermes dispatch path (inline tools, `delegate_task`, context-engine and memory-provider tools, registry tools, desktop terminal approval batches) goes through the same `tool_execution` middleware chain.
- **Isolation:** bindings are keyed by session and turn. A claim needs a first turn, platform `cli`, an exact text match and an `O_EXCL` create.
- **Terminal allowlist:** a replay never runs a command. A bypass of the record-time allowlist can only make a cassette stale. It can't cause a side effect.
- **Snapshot:** paths must sit inside the workspace (re-checked with `realpath`). Hidden and secret-looking names are skipped. Only regular UTF-8 files up to 512 KB are kept. A file is skipped if redaction raises. Paths come only from successful tool results, which Hermes's own read denylist has already filtered.

## Known residual risks (for the README)

- **An unclaimed run runs live** (see Finding 1). The gateway check stops the common causes before the request is sent. In the rare cases left, flagged commands are denied, everything else runs, and the run is INVALID.
- The residuals of block C, written up in `reviews/c-middleware.md` and `reviews/c-middleware-recheck.md` and in PLAN's Progress log, items (a) to (f). For example, a `BaseException` in a narrow window, and caps of 10,000 retired turns.
- Memory prefetch with an external memory provider is untested (`docs/TESTED.md`).
- The snapshot search uses Python `re`, so a pathological pattern from the model could make a replayed call slow. That's denial of service only, and out of scope here.
