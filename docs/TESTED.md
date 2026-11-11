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

## Memory prefetch (decision 9 disclosure)

Checked in the Hermes source and this machine's config, not in a live trace. Memory prefetch (`agent/turn_context.py:1078-1081`) runs only through a memory manager. Hermes creates one only when `memory.provider` names an external provider (`agent/agent_init.py:1211-1225`). This config sets no `memory.provider`, so **no prefetch runs on a replay turn here**. The built-in memory (`memories/MEMORY.md` and the user profile) is loaded from disk into the system prompt instead. That's a local read, with no tool call and no network, done the same way at record and at replay. If it changes between the two, the replay sees the new version. A user with an external memory provider (Honcho, RetainDB, ...) would get that provider's prefetch on replay. That was not tested.

## Not tested yet

- A replay with the network off. The "offline" claim rests on the miss path, the unit tests, and the refused `terminal` and `execute_code` calls above.
- Recording from the desktop app's chat box (only the `-z` + `--resume` route was used).
- An external memory provider.
