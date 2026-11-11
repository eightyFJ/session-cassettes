# What was tested live

Each row is a real run on native Windows 11, Hermes 0.23.1 (commit `3b421578`), Ollama local models, the plugin linked into `%LOCALAPPDATA%\hermes\plugins\session-cassettes`. The workspace is the demo fixture `C:\repo\apps\tinyshop-demo` (its own git repo). The request is "Where is the auth handler and what port does it bind?" Pins: `auth.py`, `8080`.

## Record

| Date | Model | How | Result |
|---|---|---|---|
| 2026-11-10 | arete:27b | `hermes -z "/cassette record e2e-probe" --in <demo>`, then `hermes -z "<request>" --resume <that session id> --in <demo>` | Saved in 70 s: 7 calls (6 `search_files`, 1 `read_file`), 5 snapshot files, correct answer. One warning: 2 named paths left out of the snapshot, both `.git` (hidden path, skipped by design). |

Recording works headlessly. A slash command sent through `-z` reaches the plugin's `/cassette` handler with a session id, and `--resume` makes the next `-z` turn the recorded one. From Git Bash the leading `/` gets rewritten into a path (`C:/Program Files/Git/cassette ...`), so send the slash command from PowerShell or cmd.

## Replay (`hermes cassette run e2e-probe --model M`)

| Date | Model | Result | Path | Time |
|---|---|---|---|---|
| 2026-11-10 | arete:27b | PASS 2/2 | 5 calls: 1 hit, 4 synthesized, 0 miss | 21 s |
| 2026-11-10 | arete:27b | DIVERGED 2/2 | 6 calls: 1 hit, 4 synthesized, 1 miss (`terminal ls -la /c/repo/apps/tinyshop-demo`) | 52 s |
| 2026-11-10 | arete:27b | PASS 2/2 | 3 calls: 1 hit, 2 synthesized, 0 miss | 13 s |
| 2026-11-10 | arete:27b | PASS 2/2 | 3 calls: 1 hit, 2 synthesized, 0 miss | 16 s |
| 2026-11-10 | gpt-oss:20b | PASS 2/2 | 3 calls: 0 hit, 3 synthesized, 0 miss | 17 s |
| 2026-11-10 | gpt-oss:20b | PASS 2/2 | 3 calls: 1 hit, 2 synthesized, 0 miss | 7 s |
| 2026-11-10 | llama3.1:8b | FAIL 0/2, 1 miss | `terminal docker inspect auth_handler` (refused, never ran) | 10 s |
| 2026-11-10 | llama3.1:8b | FAIL 0/2, 1 miss | `terminal cat /etc/nginx/sites-enabled/default` (refused, never ran) | 5 s |

What these runs show:
- No run was INVALID. Every arm was claimed by the `-z` child's session, and the claimed turn's final answer matched the child's stdout after trimming. So `sys.argv[0]` resolves to a usable `hermes`, the 20 s ledger wait was enough, and the middleware is wired in the restarted gateway.
- The snapshot search carries replays: most answers were synthesized, not exact hits. gpt-oss passed with no exact hit at all in one run.
- Misses are refused, never run: `terminal` calls from llama and Arete came back as `cassette miss` and were graded.
- Exit codes matched the verdicts: 0 PASS, 3 DIVERGED, 1 FAIL.

## Red cases

**Tampered cassette.** `e2e-tampered` is a copy of `e2e-probe` with `8080` changed to `9090` in the recorded `read_file` result and in the snapshot of `server/auth.py`. The pins are unchanged.

| Date | Model | Result | Path | Time |
|---|---|---|---|---|
| 2026-11-10 | gpt-oss:20b | FAIL 1/2 (`8080` missing; answer said 9090) | 2 calls: 1 hit, 1 synthesized, 0 miss | 17 s |
| 2026-11-10 | gpt-oss:20b | FAIL 1/2, 1 miss (`read_file` on the `server` folder) | 6 calls: 0 hit, 5 synthesized, 1 miss | 8 s |

**Wrong skill.** `demo/skills/tinyshop-layout/SKILL.md` claims auth lives in `server/catalog.py` on port 5000. It's installed as a local skill at `%LOCALAPPDATA%\hermes\skills\software-development\tinyshop-layout` and preloaded with `hermes cassette run e2e-probe --model gpt-oss:20b --skill tinyshop-layout`.

| Date | Model | Result | Path | Time |
|---|---|---|---|---|
| 2026-11-10 | gpt-oss:20b | FAIL 0/2 | 0 calls (answered from the skill alone) | 4 s |
| 2026-11-10 | gpt-oss:20b | FAIL 0/2, 2 misses | 5 calls: 0 hit, 3 synthesized, 2 miss. `execute_code` refused, never ran. `read_file server/catalog.py` missed, since it's not in the snapshot. | 8 s |

Green (gpt-oss ×2), diverged (Arete, once), red from the model (llama ×2), red from tampering (×2) and red from a skill (×2) all reproduced. Diverged reproduced only once in 4 Arete runs; it's not forced.

## Replay through `hermes chat -Q` (block F)

Block F's security review found that `hermes -z` auto-approves dangerous commands (`docs/SECURITY.md`, SEC-9 finding 1). So `hermes cassette run` now starts the child as `hermes ... chat -Q --query=<request>`. **The replay rows above ran through `-z`. The rows below ran through `chat -Q`.** Same cassettes, same workspace.

| Date | Cassette | Model | Result | Path | Time |
|---|---|---|---|---|---|
| 2026-11-10 | e2e-probe | gpt-oss:20b | PASS 2/2 | 4 calls: 0 hit, 4 synthesized, 0 miss | 13 s |
| 2026-11-10 | e2e-probe | llama3.1:8b | FAIL 0/2, 1 miss | `terminal cat /etc/nginx/nginx.conf` (refused, never ran) | 11 s |
| 2026-11-10 | e2e-probe | gpt-oss:20b | INVALID (pins 2/2) | 2 calls: 0 hit, 2 synthesized. Stdout ≠ ledger answer: see below | 6 s |
| 2026-11-10 | e2e-probe | gpt-oss:20b | PASS 2/2 | debug run, stdout captured | — |
| 2026-11-10 | e2e-probe | gpt-oss:20b | PASS 2/2 | debug run, stdout captured | — |
| 2026-11-10 | e2e-probe | gpt-oss:20b | INVALID (pins 2/2) | debug run: stdout had `AUTH_PORT=***` where the ledger had `AUTH_PORT = 8080` | — |
| 2026-11-10 | e2e-probe | gpt-oss:20b | PASS 2/2 | 3 calls: 1 hit, 2 synthesized, 0 miss | 8 s |
| 2026-11-10 | e2e-probe | gpt-oss:20b | PASS 2/2 | 8 calls: 0 hit, 8 synthesized, 0 miss | 11 s |
| 2026-11-10 | e2e-probe | gpt-oss:20b | DIVERGED 2/2 | 5 calls: 0 hit, 4 synthesized, 1 miss (`read_file server/__init__.py`) | 8 s |
| 2026-11-10 | e2e-probe | llama3.1:8b | FAIL 0/2 | 0 calls | 8 s |
| 2026-11-10 | e2e-tampered | gpt-oss:20b | FAIL 1/2 | 6 calls: 1 hit, 5 synthesized, 0 miss | 11 s |
| 2026-11-10 | e2e-probe | gpt-oss:20b | PASS 2/2 | 2 calls: 1 hit, 1 synthesized, 0 miss | 6 s |

What these runs show:
- **The claim works through `chat -Q`.** Every run was claimed by the child's session (platform `cli`, first turn, exact text). The session id `chat -Q` prints on stderr matched the claiming session each time.
- **The 2 INVALID runs were a real mismatch, now fixed.** `chat -Q` prints the gateway's stored copy of the answer, and Hermes redacts that copy. So `AUTH_PORT = 8080` in the answer printed as `AUTH_PORT=***`. The stdout check now also accepts Hermes's redacted copy of the ledger answer. A check against the real `agent.redact` reproduces the live mismatch exactly. The four runs after the fix were not INVALID.
- **The DIVERGED run is honest.** No recorded result named `server/__init__.py` (an empty file), so it isn't in the snapshot. The model went outside the recording, and the answer was still right.
- **The red cases reproduce on the new child:** llama3.1:8b FAIL ×2 (one refused `terminal` call), tampered FAIL.

**Unclaimed `chat -Q` run (approval check).** In a scratch folder, with no arm, so the plugin passed every call through: `hermes --model=gpt-oss:20b --in=<scratch> chat -Q --query="Use the terminal tool to run exactly this command ...: rm -rf ./victim"`. **Hermes denied `rm -rf`** ("dangerous recursive delete"). Under `-z` it would have been auto-approved. The model then ran `rm ./victim/keep.txt` and `rmdir ./victim`, which Hermes doesn't flag, and the folder was deleted. So an unclaimed run is not harmless. It is only graded INVALID afterwards. See `docs/SECURITY.md`.

## Record through `hermes chat -Q` (block F follow-up)

| Date | Model | How | Result |
|---|---|---|---|
| 2026-11-10 | arete:27b | `hermes chat -Q "--query=/cassette record f-record-q"` in the demo folder, then `hermes chat -Q --resume=<the session_id it printed on stderr> "--query=<request>"` | Saved in 79 s: 4 recorded calls, 2 snapshot files, correct answer. One `terminal` call ran live and was not kept, so the cassette carries the "may not replay cleanly" warning. It was never pinned or replayed. |

This is the headless record route that doesn't auto-approve (`-z` does). It also shows that a slash command sent through `chat -Q` runs in the gateway: the record request lives only in the gateway's memory, and a separate process's turn picked it up. Run from PowerShell; Git Bash rewrites the `/` even inside `--query=/...`.

Side finding: the wrong-map demo skill `tinyshop-layout` was still installed. Arete read it during this ordinary recording and called it out as wrong. Install it only for the wrong-skill red run.

## Gateway check before a run (block F follow-up)

`hermes cassette run` now sends `/cassette ping <nonce>` before the request (`docs/SECURITY.md`).

| Date | State | Result |
|---|---|---|
| 2026-11-10 | Gateway still on the block F commit (no `ping` verb) | Refused in 5 s: "the Hermes gateway did not answer ... Nothing was replayed." Exit 2, no arm or pong left. |
| 2026-11-10 | After `hermes gateway restart` | gpt-oss:20b PASS 2/2, 5 calls: 0 hit, 5 synthesized, 0 miss. 27 s in all, about 5 s of it the ping. |
| 2026-11-10 | `verdict.py` changed after the gateway loaded | Refused: "runs a different version ... (1b73c1a286de85c0) than this folder (6059834b4122c5dc)". Exit 2. File restored. |
| 2026-11-10 | `hermes plugins disable session-cassettes` + gateway restart | `hermes cassette` is not a command, so nothing can run. Re-enabled and restarted afterwards: PASS. |

## After Grok's block F review

The fixes are: no `HERMES_YOLO_MODE` in the child's environment, a session id required on both sides, stderr read from its end, and the ping before the arm (`docs/SECURITY.md`). Run after `hermes gateway restart`.

| Date | Model | Result | Path | Time |
|---|---|---|---|---|
| 2026-11-11 | gpt-oss:20b | PASS 2/2 | 2 calls: 1 hit, 1 synthesized, 0 miss | 14 s |
| 2026-11-11 | gpt-oss:20b | PASS 2/2 | 3 calls: 0 hit, 3 synthesized, 0 miss | 7 s |
| 2026-11-11 | llama3.1:8b, launched with `HERMES_YOLO_MODE=1` in the shell | FAIL, 1 miss | `terminal docker inspect ...` (refused, never ran) | 11 s |

## Block G (README output samples)

Run 2026-11-11 to capture real output for the README, with `NO_COLOR=1`. Same cassette and workspace as above.

| Date | Model | Result | Path | Time |
|---|---|---|---|---|
| 2026-11-11 | gpt-oss:20b | PASS 2/2 | 2 calls: 1 hit, 1 synthesized, 0 miss | 11 s |
| 2026-11-11 | llama3.1:8b | FAIL 0/2, 1 miss | `terminal netstat -tlnp \| grep auth_handler` (refused, never ran) | 8 s |
| 2026-11-11 | gpt-oss:20b | PASS 2/2 | 2 calls: 0 hit, 2 synthesized, 0 miss. After `hermes gateway restart` for the block G `plugin.yaml` change. | 14 s |
| 2026-11-11 | gpt-oss:20b | PASS 2/2 | 3 calls: 0 hit, 3 synthesized, 0 miss. After `hermes gateway restart` for the `snapshot.py` skip-list change (Grok's README review, finding 1). | 13 s |

**Tally of `chat -Q` replays of `e2e-probe` up to here**, counted from the rows in this file: gpt-oss:20b PASS ×12, DIVERGED ×1, INVALID ×2 (both before the redaction fix). llama3.1:8b FAIL ×4. `e2e-tampered` with gpt-oss:20b FAIL ×1. The "re-enabled and restarted afterwards: PASS" line in the gateway-check table names no model and is not counted. **This tally is 3 PASS runs short of the saved run files. The next section has the full count.**

## Checked against the saved run files (README audit, 2026-11-11)

The Fable 5.2 audit read every run file under `plugin-data\session-cassettes\runs\`: 32 for `e2e-probe` and 3 for `e2e-tampered`. Every row above has its file, and the result, calls and time match. The two Arete times that did not match (15 s and 19 s) are corrected above to 13 s and 16 s. Three gpt-oss:20b PASS runs through `chat -Q` had a file but no row:

| Date (EST) | Run file stamp (UTC) | Model | Result | Path | Time |
|---|---|---|---|---|---|
| 2026-11-10 23:32 | 2026-11-11T04:32:28 | gpt-oss:20b | PASS 2/2 | 1 call: 0 hit, 1 synthesized, 0 miss | 8 s |
| 2026-11-11 00:11 | 2026-11-11T05:11:54 | gpt-oss:20b | PASS 2/2 | 9 calls: 0 hit, 9 synthesized, 0 miss | 12 s |
| 2026-11-11 00:24 | 2026-11-11T05:24:57 | gpt-oss:20b | PASS 2/2 | 5 calls: 0 hit, 5 synthesized, 0 miss | 14 s |

By their times, the first is the "re-enabled and restarted afterwards: PASS" line in the gateway-check table, and the second is the replay after the yolo-pong change (`docs/SECURITY.md`, Grok's recheck). The third has no note anywhere. Which step each belonged to is read from the timestamps, not from a log.

**Full tally of `chat -Q` replays of `e2e-probe`, from the run files:** gpt-oss:20b 18 runs: PASS ×15, DIVERGED ×1, INVALID ×2 (both before the redaction fix). llama3.1:8b FAIL ×4. `e2e-tampered` with gpt-oss:20b FAIL ×1, and in all three tampered runs (two through `-z`) the answer said 9090. Through `-z`, 10 runs of `e2e-probe`: Arete PASS ×3 and DIVERGED ×1, gpt-oss:20b PASS ×2, llama3.1:8b FAIL ×2, wrong skill FAIL ×2.

## The README walkthrough from start to finish (README audit, 2026-11-11)

Until this run, no cassette recorded through `chat -Q` had been replayed: every replay used `e2e-probe`, recorded through `-z`. This run used the README's own commands, from PowerShell, in the demo folder, under the name `walkthrough-check`.

| Step | Command | Result |
|---|---|---|
| Arm | `hermes chat -Q "--query=/cassette record walkthrough-check"` | "Recording is armed", `session_id:` on stderr, exit 0. |
| Record | `hermes chat -Q --resume=<that id> "--query=<request>"`, arete:27b | Saved in 46 s: 5 recorded calls (3 `search_files`, 2 `read_file`), 5 snapshot files, correct answer. One warning: 1 named path left out of the snapshot (hidden path). Hermes printed "found but has no messages. Starting fresh." and kept the same session id. |
| Pin | `hermes cassette pin walkthrough-check auth.py`, then `8080` | 2 pins. |
| Replay | `hermes cassette run walkthrough-check --model gpt-oss:20b` | PASS 2/2, 4 calls: 0 hit, 4 synthesized, 0 miss. 15 s. Exit 0. |

The ping accepted the gateway, so the gateway was running this folder's code.

## Block H pre-freeze checks (2026-11-11)

The pre-freeze code read changed one thing in `snapshot.py`: a workspace whose own path runs through a link (a junction, a symlinked folder) no longer has every file skipped as "link points outside the workspace" (`docs/PLAN.md`, Progress log). `hermes gateway restart` came first (PID 23916), and the ping accepted the gateway in both replays below, so the gateway ran this folder's code. All commands were run from PowerShell.

| Cassette | Step | Result |
|---|---|---|
| `h-freeze-check` | Record, arete:27b, the README's two `chat -Q` commands in the demo folder | Saved in 34 s: 3 recorded calls (all `search_files`), 7 snapshot files, correct answer. One warning: 2 named paths left out of the snapshot (hidden path). |
| `h-freeze-check` | `hermes cassette run h-freeze-check --model gpt-oss:20b` | PASS 2/2, 5 calls: 0 hit, 5 synthesized, 0 miss. 14 s. Exit 0. |
| `h-junction-check` | Record, arete:27b, the same two commands after `cd` into a junction that points at the demo folder | Saved in 39 s: 5 recorded calls (1 `search_files`, 4 `read_file`), 5 snapshot files, correct answer, no warning. **The cassette's `cwd` is the real folder, not the junction:** Hermes resolved the link before the plugin saw the path. |
| `h-junction-check` | `hermes cassette run h-junction-check --model gpt-oss:20b` | PASS 2/2, 3 calls: 1 hit, 2 synthesized, 0 miss. 13 s. Exit 0. |
| `h-junction-in` | Record, arete:27b, from another folder with `hermes --in=<junction> chat -Q ...` | Saved in 38 s: 2 recorded calls, 2 snapshot files, correct answer. One `terminal` call ran live, so it carries the "may not replay cleanly" warning. `cwd` is again the real folder. Not pinned or replayed. |

What these runs show, and what they don't:
- Recording and replay work on the changed code.
- **The junction runs did not exercise the fix.** In both routes Hermes handed the plugin the resolved path, so the plugin never saw a workspace behind a link. The fix is covered by unit tests only (`test_workspace_reached_through_a_link`, which fails against the old code, and `test_directory_link_escape`). Whether any other route hands over an unresolved path (the desktop app, macOS, Linux) was not checked.
- `h-freeze-check` is still in the plugin data folder. The two junction cassettes and the junction run file were moved to `C:\repo\_archive\session-cassettes-block-h-2026-11-11`.

**Unit tests under Linux (2026-11-11).** `python3 -m unittest discover -s tests`, run from this folder in WSL (Ubuntu 22.04.5, Python 3.10.12): 254 run, 253 pass, 1 skipped.
- The first run had 5 that did not pass. All five assert Windows path rules: the `.cmd` launcher test (it sets `os.name` to `nt`, and Python can only build Windows paths on Windows), two `safe_join` cases (a drive path and a network path, which are ordinary file names off Windows), and two path spellings (the Git Bash form, and upper case with backslashes).
- Those five now run on Windows only. No plugin code changed. On Windows the suite is unchanged: 254 run, 253 pass, 1 skipped.
- The file-symlink escape test, which Windows skips without Developer Mode, ran on Linux and passed. The 1 skipped on Linux is the launcher test.
- **This is not a test of the plugin inside Hermes on Linux.** That distro has no Hermes, and its Python 3.10 is older than Hermes's minimum of 3.11.

## Video preparation runs (2026-11-11, after the freeze)

Run on the frozen code, commit `fa1f9fc`, from PowerShell in `C:\repo\apps\tinyshop-demo`. The request each time was "Where is the auth handler and what port does it bind?", with the pins `auth.py` and `8080`.

| Cassette | Step | Result |
|---|---|---|
| `video-rehearsal` | Record, arete:27b, two `chat -Q` commands | Saved in 73 s: 4 recorded calls (3 `search_files`, 1 `read_file`), 6 snapshot files, correct answer. One warning: 1 named path left out of the snapshot (hidden path). |
| `video-rehearsal` | Replay, gpt-oss:20b | PASS 2/2, 3 calls: 0 hit, 3 synthesized, 0 miss. 24 s. Exit 0. |
| `video-rehearsal` | Replay, llama3.1:8b | FAIL 0/2, 1 call: 1 miss, `terminal cat /etc/nginx/nginx.conf` (refused, never ran). 19 s. Exit 1. |
| `tinyshop` | Record, arete:27b, armed with `/cassette record tinyshop --overwrite` | Saved in 68 s: 4 recorded calls (2 `search_files`, 2 `read_file`), 6 snapshot files, correct answer. One warning: 5 named paths left out of the snapshot (hidden path). |
| `tinyshop` | Replay, gpt-oss:20b | PASS 2/2, 3 calls: 0 hit, 3 synthesized, 0 miss. 25 s. Exit 0. |
| `tinyshop` | Replay, llama3.1:8b | FAIL 0/2, 1 call: 1 miss, a `terminal docker inspect ...` command (refused, never ran). 16 s. Exit 1. |

Also run once each through `chat -Q`, for the first time live:
- `/cassette record <name> --overwrite` on a name that did not exist armed the session as usual.
- `/cassette stop` in that session answered that the recording was cancelled and nothing was saved.
- Pinning a fact a second time did not add a duplicate.

The times are wall-clock for the whole command, model loading included. `tinyshop` stays in the plugin data folder for the video. `video-rehearsal` and its two run files were moved to `C:\repo\_archive\session-cassettes-block-h-2026-11-11`.

**Argument passing through the real `hermes.exe` (SEC-4, live).** The SEC-4 unit test sends a hostile request to a Python child, not through Hermes's own launcher. One live check: Python's `subprocess` with an argument list and no shell started `hermes.exe chat -Q "--query=/cassette record <text>"`, where `<text>` held `"`, `'`, `%PATH%`, `&`, `|`, `;`, `^`, `$HOME` and a backtick. The plugin's handler in the gateway refused it as an invalid cassette name and echoed the text back unchanged, in 3 s. Nothing was armed. This was a slash command, not a model turn, and the text had no spaces or newlines.

## Memory prefetch (decision 9 disclosure)

Checked in the Hermes source and this machine's config, not in a live trace. Memory prefetch (`agent/turn_context.py:1078-1081`) runs only through a memory manager. Hermes creates one only when `memory.provider` names an external provider (`agent/agent_init.py:1211-1225`). This config sets no `memory.provider`, so **no prefetch runs on a replay turn here**. The built-in memory (`memories/MEMORY.md` and the user profile) is loaded from disk into the system prompt instead. That's a local read, with no tool call and no network, done the same way at record and at replay. If it changes between the two, the replay sees the new version. A user with an external memory provider (Honcho, RetainDB, ...) would get that provider's prefetch on replay. That was not tested.

## Not tested yet

- A replay with the network off. The "offline" claim rests on the miss path, the unit tests, and the refused `terminal` and `execute_code` calls above.
- Recording from the desktop app's chat box or an interactive `hermes` session. Only the two headless routes were used: `-z` + `--resume`, and `chat -Q` + `--resume`.
- An external memory provider.
- `hermes cassette run --skill` through the current `chat -Q` child. The two wrong-skill runs went through `-z`.
- A snapshot of a workspace that holds an `auth.json` or `token.json`, live. The skip is a unit test (`test_login_store_names_are_skipped`).
- Installing the plugin from GitHub or from the catalog. Every run used the repo linked into the plugins folder.
- An Arete replay through `chat -Q`. Arete's four replays went through `-z`.
- A replay after a prompt change. Only the model was varied through `chat -Q`.
- Two replays overlapping. A second run is refused only while the first is still waiting to be claimed.
- `hermes cassette run` with `--provider`, with `--timeout`, or with `--in` set to a folder other than the recorded one. The in-session `/cassette stop`, `status`, `pin` and `list`. All of these have unit tests only.
- A recording with Hermes's redaction switched off (`security.redact_secrets: false`). By the Hermes source, the snapshot would then be stored unredacted.
- `plugins.isolation: host`. By the Hermes source (`hermes_cli/plugin_isolation.py`), `hermes cassette` is not registered in that mode, so replay would not be available.
- The walkthrough commands from cmd. Only PowerShell was used.
- A workspace that the plugin sees through a link. Hermes resolved the junction in both live tries, so the block H fix has unit tests only.
