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
| 2026-11-10 | arete:27b | PASS 2/2 | 3 calls: 1 hit, 2 synthesized, 0 miss | 15 s |
| 2026-11-10 | arete:27b | PASS 2/2 | 3 calls: 1 hit, 2 synthesized, 0 miss | 19 s |
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
| 2026-11-10 | arete:27b | `hermes chat -Q "--query=/cassette record f-record-q"` in the demo folder, then `hermes chat -Q --resume=<the session_id it printed on stderr> "--query=<request>"` | Saved in 79 s: 4 recorded calls, correct answer. |

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

## Memory prefetch (decision 9 disclosure)

Checked in the Hermes source and this machine's config, not in a live trace. Memory prefetch (`agent/turn_context.py:1078-1081`) runs only through a memory manager. Hermes creates one only when `memory.provider` names an external provider (`agent/agent_init.py:1211-1225`). This config sets no `memory.provider`, so **no prefetch runs on a replay turn here**. The built-in memory (`memories/MEMORY.md` and the user profile) is loaded from disk into the system prompt instead. That's a local read, with no tool call and no network, done the same way at record and at replay. If it changes between the two, the replay sees the new version. A user with an external memory provider (Honcho, RetainDB, ...) would get that provider's prefetch on replay. That was not tested.

## Not tested yet

- A replay with the network off. The "offline" claim rests on the miss path, the unit tests, and the refused `terminal` and `execute_code` calls above.
- Recording from the desktop app's chat box (only the `-z` + `--resume` route was used).
- An external memory provider.
