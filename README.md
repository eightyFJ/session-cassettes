# Session Cassettes

A Hermes Agent plugin that turns one good session into a regression test.

Record a session in which the agent answered a question well. Pin the few facts a good answer must contain. Later, replay the same request against a different model, system prompt or skill. During the replay the cassette answers every tool call of that turn from the recording, so no real tool runs (one caveat, under [Safety](#safety-and-disclosures)). The verdict says whether the new answer still lands the pinned facts.

```
e2e-probe with gpt-oss:20b (11s)
  result  PASS
  pins    2/2
          [ok] auth.py
          [ok] 8080
  path    2 calls: 1 hit, 1 synthesized, 0 miss; subset of recording, in order (7 recorded)
```

A replay is offline for its tools, gives the model the same tool results every time, and costs only the model call. You can try a cheaper local model, a new skill or a prompt change against yesterday's good session without touching files, the web or anything else the session used.

**Why it's a plugin.** A prompt or a skill can't do this. Neither one can stand in for tool execution. Session Cassettes sits in Hermes's `tool_execution` middleware, where it can answer a tool call itself and never call the real tool.

## How it works

1. **Record.** In a session, type `/cassette record <name>`, then send the request. That one turn is recorded. Tools run live as normal, with Hermes's normal approvals. The plugin keeps the results of read-only tools: `read_file`, `search_files`, `web_search`, `web_extract`, `skill_view`, `skills_list` and `session_search`. It also keeps `terminal` output, but only for one foreground command from a fixed read-only list (`cat`, `type`, `dir`, `ls`, `rg`, `git log`, `git show`, `git diff`), with no shell operators, no pty, no `rg --pre` and no `git --output`. Once a turn runs a `terminal` command outside that list, none of its later `terminal` output is kept. A single result over about 2 MB, or anything past about 12 MB of results in one turn, isn't kept either. Any tool call that isn't kept is noted by name only, and the cassette gets a warning that it may not replay cleanly. At the end of the turn the plugin also keeps a **snapshot**: the text of the workspace files that the searches named or `read_file` read.
2. **Pin.** `hermes cassette pin <name> "<fact>"` adds a fact the answer must contain. A pin matches as a case-insensitive substring, with `\` and `/` treated as the same character. A pin that starts with `re:` is a regular expression.
3. **Replay.** `hermes cassette run <name> --model <model>` starts a new `hermes chat -Q` session with the recorded request in the recorded folder. The plugin, running in the Hermes gateway, claims that session's first turn and answers each of its tool calls in one of three ways:
   - **hit:** the same call was recorded, so the recorded result comes back;
   - **synthesized:** a `search_files` or `read_file` call that wasn't recorded exactly, answered by searching or slicing the snapshot in plain Python;
   - **miss:** anything else, answered with a `cassette miss` error. The tool never runs.

   The plugin never calls the real tool in a claimed replay, so no approval prompt can come up.
4. **Verdict.**

   | Result | Meaning | `run` exit code |
   |---|---|---|
   | PASS | Every pin is in the answer and there were no misses. | 0 |
   | FAIL | A pin is missing. Any misses are noted too. | 1 |
   | DIVERGED | Every pin matched, but the model asked for something the recording doesn't hold. | 3 |
   | INVALID | The run can't be trusted to have been a replay (see [Safety](#safety-and-disclosures)). Overrides the others. | 4 |

   Exit code 2 means an error: nothing was graded. The `path` line shows hits, synthesized answers and misses, and how the calls compare with the recording. It never decides the result, because a better model may well take a different path. Each run is saved as JSON under the plugin data folder.

## Install

Requires Hermes Agent 0.23.1 or later. Only 0.23.1 has been tested. Standard library only: there are no Python dependencies.

```powershell
hermes plugins install eightyFJ/session-cassettes
hermes plugins enable session-cassettes
hermes gateway restart
```

Restart the gateway after enabling. The plugin's hooks and middleware live in the gateway process, and `hermes cassette run` refuses to start until the gateway has loaded this exact version of the plugin. It checks that with a ping before every run.

*Tested route:* the repository linked into `%LOCALAPPDATA%\hermes\plugins\session-cassettes`, enabled, then a gateway restart. Installing from GitHub or from the catalog has not been tried yet.

## Walkthrough (the demo)

The repository ships a small fake app, `demo/fixture-repo` ("tinyshop"). Its auth handler is `server/auth.py` on port 8080, with decoys on other ports. Copy it into a folder of its own and make that folder its own git repository. In testing, a model searched from the enclosing git root instead of the demo folder, and you want the recording to stay inside the demo.

```powershell
Copy-Item -Recurse demo\fixture-repo C:\work\tinyshop-demo
cd C:\work\tinyshop-demo
git init

# 1. Record with your default model. The first command arms a session and prints
#    "session_id: <id>" on stderr; the second sends the request in that session.
hermes chat -Q "--query=/cassette record tinyshop"
hermes chat -Q --resume=<id> "--query=Where is the auth handler and what port does it bind?"

# 2. Pin the facts a good answer must contain.
hermes cassette pin tinyshop auth.py
hermes cassette pin tinyshop 8080

# 3. Replay against another model.
hermes cassette run tinyshop --model gpt-oss:20b
```

Run these from PowerShell. cmd should work too, but only PowerShell was used. Git Bash rewrites a leading `/` into a file path, even inside `--query=/...`. In an interactive `hermes` session or the desktop app you should be able to type `/cassette record tinyshop` and then the request, but that route is untested. `hermes cassette record tinyshop` prints those steps.

The steps above were run from start to finish three times on 2026-11-11, the first under the name `walkthrough-check`: recorded with arete:27b, replayed with gpt-oss:20b, PASS each time. Both outputs shown here come from an older test cassette, `e2e-probe`. It holds the same request but was recorded through the earlier `hermes -z` route. A red run, from a weaker model that went looking somewhere the recording never went:

```
e2e-probe with llama3.1:8b (8s)
  result  FAIL  (also 1 cassette miss)
  pins    0/2
          [--] auth.py
          [--] 8080
  path    1 call: 0 hit, 0 synthesized, 1 miss; no recorded call reused
          miss: terminal {"command":"netstat -tlnp | grep auth_handler","cwd":"c:/repo/apps/tinyshop-demo"}
```

The `netstat` command did not run. It got a `cassette miss`.

Two more ways to see red:
- **A wrong skill.** `demo/skills/tinyshop-layout` is a skill that deliberately puts auth in `server/catalog.py` on port 5000. Copy it into your Hermes skills folder (it was tested at `%LOCALAPPDATA%\hermes\skills\software-development\tinyshop-layout`) and run `hermes cassette run tinyshop --model gpt-oss:20b --skill tinyshop-layout`. This case failed as expected in two runs through the earlier `hermes -z` child. It has not been run through the current `chat -Q` child. Remove the skill afterwards: it is wrong on purpose, and it will mislead any session that loads it.
- **A tampered cassette.** Change `8080` to `9090` in the cassette file, in both the recorded `read_file` result and the snapshot copy of `server/auth.py`. In testing, the model then answered 9090 and the `8080` pin failed.

## Commands

| Command | Does |
|---|---|
| `/cassette record <name> [--overwrite]` | In a session: record this session's next turn. |
| `/cassette stop` | Cancel a recording that hasn't been saved. |
| `/cassette status` | Show what this session is doing, and the outcome of the last save. |
| `/cassette pin <name> <fact>` | Add a pin from inside a session. |
| `/cassette list` | List cassettes. |
| `hermes cassette record <name>` | Print the recording steps. Only a session can record itself. |
| `hermes cassette pin <name> <fact...> [--remove]` | Add or remove a pin. Put `--` before a fact that starts with `-`. |
| `hermes cassette run <name> [--model M] [--provider P] [--skill S]... [--in DIR] [--timeout S]` | Replay and print the verdict. The cassette needs at least one pin. The default folder is the recorded one. The default timeout is 900 s. |
| `hermes cassette list` | One row per cassette: calls, snapshot files, pins, last result and model, then any warnings. |
| `hermes cassette show <name> [--full]` | Request, recorded answer, pins, calls, snapshot files, warnings and the last 5 runs. |

Colour turns off with `NO_COLOR`.

## Safety and disclosures

What you should know before installing.

**What a cassette stores.** Cassettes are plain, unencrypted JSON files in `%LOCALAPPDATA%\hermes\plugin-data\session-cassettes\` (Hermes's plugin data folder for the active profile). Each one holds:
- the request and the recorded final answer;
- the model name, the full path of the working directory, and the arguments of every recorded call: paths, search patterns, web queries and terminal commands;
- the full result of every recorded tool call, exactly as the tool returned it to the model: file contents, search hits, web search results and page extracts, skill text, session-search results and the output of allowlisted terminal commands. A search that reached outside the workspace is stored as it came back. If the model itself read a file that holds a secret during the recorded turn, that result is stored as Hermes returned it;
- the snapshot: the full text of workspace files that the searches **named**, not only the files that were read.

Anyone who can read the file can read that content. Treat a cassette like the files it came from. Each run's verdict file stores the replayed answer too, along with the arguments of any call that missed. There is no delete command. To remove a cassette, delete its file (`hermes cassette show <name>` prints the path) and its folder under `runs\`.

**What the plugin reads.** At the end of a recorded turn it reads the snapshot files from disk, inside the session's working directory only. It skips:
- hidden paths, which covers `.env` and `.git`;
- files with a secret-looking name: `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `*.kdbx`, `*.keystore`, `id_rsa*`, `id_dsa*`, `id_ecdsa*`, `id_ed25519*`, `credentials`, `credentials.*`, `*.env`, `secrets.json`, `secrets.yaml`, `secrets.yml`, `secrets.toml`, `auth.json`, `token.json` and `tokens.json`;
- binary and non-UTF-8 files, anything that isn't a regular file, and links (symlinks or junctions) that lead out of the folder;
- files over 512 KB, and anything past 8 MB in total.

Each file it does read goes through Hermes's own `redact_sensitive_text` before it is stored, with the stricter secret-file mode for any `config.yaml`. A file is skipped if redaction fails. If you have switched Hermes's redaction off (`security.redact_secrets: false`), that call changes nothing and the snapshot is stored as read, the same as a live `read_file`. That is from the Hermes source and was not tested. The skip list works by file name and redaction works by pattern, so a secret in an ordinary-looking file, in a form Hermes doesn't recognise, would be stored. The name checked is the one the tool result used, so a link with an ordinary name that points at a skipped file inside the folder would be read. Apart from its own files, its data folder and the two temporary files described below, the plugin reads nothing outside the session's working directory.

**What the plugin writes.** The plugin data folder: cassettes, run files, and short-lived arm, claim and ping files. During a replay it also holds the child's output, which includes the replayed answer, in two temporary files in the system temp folder. It reads them back when the child exits, and they are deleted before the verdict is printed. It writes nowhere else. Each run does start two Hermes sessions, the ping and the replay, and Hermes keeps those in its own session history like any other session.

**What runs during a replay.** In a claimed replay no tool runs. The plugin answers every tool call of that turn, including `terminal`, `write_file`, `execute_code` and `delegate_task`, from the cassette or with a `cassette miss`. **The model call is real.** It goes to whichever model and provider you name. With a local Ollama model the model call stays on the machine, and the plugin makes no network call of its own. It can't speak for the rest of Hermes: an external memory provider, for example, would still be called (see Known issues), and no replay has been run with the network off. With a cloud model, the request and the recorded results the model is shown go to that provider, as they would in any session.

**Child processes.** `hermes cassette run` starts two `hermes chat -Q` processes: a ping (`/cassette ping`, no model call), then the request. They take an argument list, never a shell. The plugin uses no `-z`, which auto-approves, and no `--yolo`. It removes `HERMES_YOLO_MODE` from the child's environment. The ping also reports whether that variable is set in the gateway's own environment, and the run is refused if it is. That check reads the gateway's environment at ping time, not the yolo flag Hermes fixes at startup, so it would not catch a gateway whose variable was removed after it started. The plugin changes no approval settings.

**An unclaimed run is live.** If no session claims the run, the request runs as an ordinary session with real tools. That can happen if another session sends the identical first message at the same moment, if the plugin is disabled between the ping and the request, or if Hermes's `pre_llm_call` hook times out. Hermes then denies flagged commands, because `chat -Q` has nobody to ask. But any tool Hermes doesn't flag still runs. In testing, a deliberately unclaimed run had `rm -rf` denied, and the model then deleted the folder with plain `rm` and `rmdir`. Your own approval settings still apply: `approvals.mode: off`, `single_query_mode: approve` or an allowlist entry would approve a flagged command in such a run. The run is graded **INVALID**, but only after it ends. The ping before each run turns the common causes into a refusal before anything runs: a gateway without the plugin, or a gateway running different plugin code. **A replay is offline only when its result is not INVALID.**

**What makes a run INVALID.** The printed verdict always names the reason. An INVALID run is never PASS. The causes:
- Nothing claimed the arm, or the arm was left over.
- Tool calls of that turn ran live before the claim landed, or were still running when it did.
- The ledger is missing or unfinished.
- `chat -Q` printed no session id, the ledger names no session, or the two don't match.
- No final answer was captured, or the claimed turn's answer doesn't match what the child printed. Hermes's redacted copy of the answer is accepted.
- The child exited non-zero or timed out.
- The cassette failed to load, or the plugin hit an internal error while answering a call.
- The turn made more than 5,000 tool calls, so the call log was cut short.
- More than 16 unfinished replays were holding a cassette in the gateway's memory, so this one's copy was cleared. The cassette file on disk is untouched.

**Every session's tool calls pass through it.** The Hermes gateway hosts every session (desktop tabs, CLI runs, sub-agents). The middleware sees every tool call. For a session it hasn't bound, it passes the call straight through and stores nothing. It keeps an in-memory count of those calls, so a late claim can be marked INVALID. The two hooks are also called for every turn of every session, with the user message and the final answer. For a session it hasn't bound, the plugin only compares a first CLI message with a waiting replay request, and keeps nothing.

**Not included.** No network calls of its own, no telemetry, no judge model, no self-update, no third-party dependencies.

**Hermes surfaces used.** The `pre_llm_call` and `post_llm_call` hooks, `tool_execution` middleware, `ctx.register_command` (`/cassette`) and `ctx.register_cli_command` (`hermes cassette`). It also calls `ctx.session_cwd()` and imports three helpers, `plugin_data_dir`, `redact_sensitive_text` and `get_session_env`, each of which is in Hermes's published plugin API list (`hermes_cli/data/plugin_api.json`). Public APIs only, with no patching of Hermes code.

## Known issues and limits

- **Tested only on native Windows 11.** On macOS and Linux the plugin has never run inside Hermes. Only the unit tests have run on Linux. Also untested are the TUI, gateway platforms such as Telegram, cloud providers, and `plugins.isolation: host`. The validator rates the plugin host-ready, but that mode hasn't been run, and replay would not work in it. Hermes's source says CLI commands are not registered in that mode (`hermes_cli/plugin_isolation.py`), so `hermes cassette` would be missing and only `/cassette` would remain.
- **Recording was tested headlessly only**, through `hermes chat -Q` and, earlier, `hermes -z`. Recording from the desktop app's chat box, or from an interactive `hermes` session, uses the same slash command but has not been tested. Don't record through `hermes -z`, because `-z` auto-approves dangerous commands.
- **Replay needs a CLI session.** Only a first turn on platform `cli` with the exact request text can claim a run. A request with an `@` reference, attached context or an image is refused at record time. So is a request that starts with `/` or `!` at replay time. Requests are stored trimmed, with `\r\n` turned into `\n`.
- **One run can wait for its session at a time.** A second `hermes cassette run` on the same profile is refused until the first run's session has claimed its request, which takes a few seconds. After that the two can overlap. Overlapping replays are covered by unit tests only. Each run spends about 5 s on the gateway ping.
- **Through the current child, only the model has been varied.** The `--skill` runs went through the earlier `hermes -z` child. A prompt change has not been run at all. There is no flag for one: you change what Hermes builds its system prompt from (its help text names `AGENTS.md`, `SOUL.md`, `.cursorrules`, memory and preloaded skills), and Hermes reads those from disk at replay. A skill under test has to be preloaded with `--skill`. `skill_view` and `skills_list` are answered from the recording, so the replay sees a skill's text as it was recorded, or gets a miss for a skill the recording never viewed.
- **On Windows the replay needs `hermes.exe`.** `hermes cassette run` starts the `hermes` that is running it, or the one on `PATH`. A `.bat` or `.cmd` wrapper is refused, because `cmd.exe` would re-read the request.
- **Replays are not deterministic.** The model can still wander outside the recording. Through `chat -Q`, gpt-oss:20b replayed the demo cassette 18 times: PASS ×15, DIVERGED ×1, and the 2 early INVALID runs described under "What was tested". Arete replaying its own cassette came up DIVERGED once in 4 runs. Those 4 went through the earlier `hermes -z` child, and Arete has not replayed through `chat -Q`.
- **The synthesized search is an approximation.** Python `re` stands in for ripgrep. Only snapshot files are searched, and `order: "modified"` is ignored. A search root outside the workspace, or a folder with no snapshot files, is a miss. A pathological regex from the model could hang one replayed call, because Python `re` has no time limit. The run would then end INVALID at its timeout, and that gateway thread would stay busy until the search finished or the gateway was restarted. It has not happened in testing.
- **The workspace is the session's working directory.** There is no git-root detection. Files above that folder aren't snapshotted, so record from the folder the question is about.
- **Memory.** With no external memory provider (the default), Hermes's built-in memory is read into the system prompt from disk at record and at replay alike. If it changed in between, the replay sees the new version. An external provider (Honcho and others) would prefetch on replay. That is untested.
- **A replay with the network switched off has not been run.** The offline claim rests on the code path that never calls the real tool, the unit tests, and live runs in which `terminal` and `execute_code` calls were refused.
- **The yolo-gateway refusal is unit-tested only**, and it is narrower than Hermes's own flag. The tests set `HERMES_YOLO_MODE` on the process that answers the ping. The refusal has not been run against a gateway really in yolo mode. It reads the gateway's current environment, so a gateway that started in yolo mode and later lost the variable would not be refused.
- **Rare races** are written up in [`reviews/c-middleware.md`](reviews/c-middleware.md) and [`reviews/c-middleware-recheck.md`](reviews/c-middleware-recheck.md). For example, a tool call that has already finished is remembered for the last 10,000 turns only.
- **Pins are substrings.** The pin `8080` also matches `18080`. Use `re:\b8080\b` when that matters.

## What was tested, on what

Machine: native Windows 11, Hermes Agent 0.23.1 (commit `3b421578`), local Ollama models, Python 3.14.7 for the unit tests. Every live run is listed with its date and result in [`docs/TESTED.md`](docs/TESTED.md).

- **Unit tests:** 254 tests, of which 253 pass and 1 is skipped: `python -m unittest discover -s tests`. The skipped one is the escape test with a file symlink, which needs Windows Developer Mode. The same escape through a directory junction is tested, and it is refused. The suite was also run under Linux (Ubuntu 22.04 in WSL, Python 3.10): 253 pass there too, the file-symlink test among them, and the 1 skipped is a Windows-only launcher test. That is the unit tests only. The plugin has not run inside Hermes on Linux. They cover:
  - path normalisation (Windows, `./`, Git Bash `/c/...` forms);
  - pins and verdict precedence;
  - atomic writes;
  - name checks against path traversal and Windows device names;
  - corrupt, huge and non-UTF-8 files;
  - an exclusive claim under an 8-thread race;
  - replay never calling the real tool for any tool name, including with a corrupt or missing cassette and after an internal error;
  - the child's argument list against shell metacharacters;
  - the ping, yolo and session-id checks.

  A fake `hermes` child process drives the real hooks across a process boundary.
- **Record:** arete:27b, six recordings of the demo request, one through `hermes -z` and five through `hermes chat -Q`. All six answers were correct. Two of the `chat -Q` recordings carry the "may not replay cleanly" warning, because one `terminal` call was not kept, and those two were never replayed.
- **The walkthrough from start to finish** (2026-11-11, three runs): each was recorded through `hermes chat -Q` with arete:27b, pinned `auth.py` and `8080`, then replayed with gpt-oss:20b: PASS ×3. The first kept 5 calls and 5 snapshot files, and its replay made 4 calls, all synthesized. The other two ran after the last code change, one of them started from a folder reached through a junction. Every other replay listed here used `e2e-probe`, the cassette recorded through `hermes -z`.
- **Replay through `hermes chat -Q`** (the current child), `tinyshop` request, pins `auth.py` and `8080`:
  - gpt-oss:20b: PASS ×15 and DIVERGED ×1 (a read of an empty file the recording never named; the answer was still right). Two early runs were INVALID: Hermes had redacted the printed answer (`AUTH_PORT=***`), so it didn't match the ledger. The check now accepts Hermes's redacted copy, and no run since has been INVALID.
  - llama3.1:8b: FAIL ×4. Three times it reached for `terminal`, got a miss and gave up, and nothing ran. Once it answered with no tool calls at all. One of the three was launched with `HERMES_YOLO_MODE=1` set in the shell, to check that the child doesn't inherit it.
  - Tampered cassette, gpt-oss:20b: FAIL ×1.
- **Earlier replays through `hermes -z`** (the first child, since replaced): Arete PASS ×3 and DIVERGED ×1, gpt-oss PASS ×2, llama FAIL ×2, tampered FAIL ×2, wrong skill FAIL ×2. The wrong-skill run (`--skill`) has not been repeated through `chat -Q` yet.
- **Gateway check:** a gateway running older plugin code and a plugin file changed after the gateway loaded were each refused before anything ran. With the plugin disabled, `hermes cassette` doesn't exist, so nothing can run.
- **Argument passing through the real `hermes.exe`:** a `chat -Q` query holding `"`, `'`, `%PATH%`, `&`, `|`, `;`, `^`, `$HOME` and a backtick reached the plugin in the gateway unchanged. That was one slash command that echoes its argument, not a model turn.
- **Never run live, unit tests only:** `--provider`, `--timeout`, `--in` with a folder other than the recorded one, overlapping runs, and the in-session `/cassette stop`, `status`, `pin` and `list`.
- **Security:** `hermes plugins validate . --install-deps` is clean with no warnings ([`docs/VALIDATION.txt`](docs/VALIDATION.txt)). The scans found nothing outstanding. bandit: no high findings, one medium in the demo fixture and eight lows, each explained. semgrep (`p/python`, 151 rules): 0 findings. bandit scanned everything except `tests/`, and semgrep scanned the plugin's six modules. gitleaks found no leaks in the git history or the working tree. All four were rerun on 2026-11-11, after the last code change. `docs/SECURITY.md` records each scan with the code it covered. Claude Code's security review found one real issue, the auto-approving `-z` child, and two smaller ones. All three are fixed. Details are in [`docs/SECURITY.md`](docs/SECURITY.md).

## How it was built

Built for the Nous Research Hermes Agent plugin hackathon (November 2026):
- Claude (Claude Code) wrote the code and docs.
- Grok reviewed the mode-switching design, the middleware, the security fixes and this README. Its reviews are in [`reviews/`](reviews/). The store, the CLI and the live runs had no review of their own.
- Arete 27B was the recording model in every live test.

The design, build log and decisions are in [`docs/PLAN.md`](docs/PLAN.md) and [`docs/SPEC.md`](docs/SPEC.md).

## Licence

MIT. See [`LICENSE`](LICENSE).
