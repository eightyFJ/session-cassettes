# Build plan — Session Cassettes v1

Written 2026-11-10 by Claude, after re-reading `SPEC.md`, `RULES.md` and `RESEARCH.md` and checking the open questions against the Hermes 0.23.1 source (commit `3b421578588243f920886e6fe79a6faa87ea026e`). Paths are relative to `%LOCALAPPDATA%\hermes\hermes-agent`.

Deadline: **2026-11-11, 5 PM EST.** That leaves about 23 hours. The plan puts a working, validated plugin first and polish last, and says what gets cut when time runs short.

## 1. Spec questions, answered from the source

| # | Question | Answer | Source |
|---|---|---|---|
| 2 | What does a `tool_execution` callback receive and return? | Keyword arguments `tool_name`, `args`, `original_args`, `next_call`, plus the call IDs (`task_id`, `session_id`, `tool_call_id`, `turn_id`, ...) and two schema-version fields. Hermes passes only the names the signature declares, so take `**kwargs`. Whatever the callback returns becomes the tool result. Tool results are strings (usually JSON). An error is `tool_error(msg)`, which is the string `{"error": msg}`. **Not calling `next_call` skips the real dispatch, including the approval step inside it.** | `hermes_cli/middleware.py:144-260`, `agent/tool_executor.py:770-815`, `tools/registry.py:1072-1198` |
| 3 | How does the plugin get the user request and the final answer? | `post_llm_call` fires once per finished turn with `user_message` and `assistant_response` (plus `session_id`, `model`, `conversation_history`). One hook covers both sides. It does not fire on an interrupted turn, which is the behaviour we want. | `agent/turn_finalizer.py:474-505`, `website/docs/user-guide/features/hooks.md:461` |
| 1 | How does `hermes cassette run` start a session headlessly? | `hermes -z "<request>" -m <model> [--provider P] [-s skill] [--in DIR]`. Its stdout is the final answer only. **Risk:** `-z` launches through `gateway_chat_startup.launch_gateway_chat`, so the agent may run in a gateway process that does not inherit our environment. See spike S1. | `hermes_cli/main.py:2903`, `hermes_cli/_parser.py:165-245` |
| 4 | How does the middleware know which mode it is in? | Two options: an environment variable, or an "armed" state file in `plugin_data_dir("session-cassettes")`. Spike S1 decides between them. `register_session_env` is the wrong tool here: it pushes variables *into* tool subprocesses, not into the plugin. | `hermes_cli/plugins_context.py:752`, `plugins/plugin_storage.py:27` |
| 5 | Does anything leak around the cassette in sub-agents? | Sub-agent tool calls go through the same middleware (`agent/agent_runtime_helpers.py:2547`). In replay mode the cassette answers **every** tool call, not only the allowlisted ones. Anything not recorded, including `delegate_task`, `terminal` and `write_file`, gets a `cassette miss` error and never runs. Nothing leaks. | as cited |
| 6 | Is the name free? | Yes on 2026-11-10 (`RULES.md`, rule 16). Use `session-cassettes` for the plugin, the catalog entry and the GitHub repo. | — |

### New finding that changes the code: the validator probe imports nothing from Hermes

`hermes plugins validate` imports `__init__.py` in a bare child interpreter and calls `register()` against a stub context (`hermes_cli/plugin_validate.py:200-330`). A top-level `from plugins.plugin_storage import plugin_data_dir` would fail there. **All Hermes imports go inside functions**, and `register()` must touch nothing but `ctx`.

The probe diffs only tools, hooks and middleware. CLI commands are recorded but not compared. So the manifest declares:

```yaml
provides_tools: []
provides_hooks: [post_llm_call]          # plus on_session_end if S1 needs it
provides_middleware: [tool_execution]
requires_env: []
```

## 2. Design decisions this plan makes

These are Claude's proposals. Grok should check them and the owner should approve them.

1. **Replay intercepts everything.** In replay mode the middleware never calls `next_call`. A hit returns the recorded string. A miss returns `{"error": "cassette miss: <tool> <args>"}` and marks the run diverged. As a result:
   - the replay is offline by construction,
   - no approval prompt can ever come up, which meets rule 12 without `--yolo`, and
   - the README can say this plainly and back it with a test.
2. **Record only the allowlist.** `read_file`, `search_files`, `web_search`, `web_extract`, `skill_view`, `skills_list`, `session_search`. Other tools run live and are noted in the cassette as `unrecorded` (name only, no args or result), so the verdict can explain a later miss.
3. **The cassette key is the tool name plus canonical JSON of the normalised arguments.** Normalisation: `\` becomes `/`, the drive letter is lowercased, a relative path is resolved against the recorded working directory, keys are sorted, and default-valued arguments are dropped. For a key recorded more than once, replay returns the results in order and then repeats the last one.
4. **Line ranges in `read_file`: v1 matches exact arguments only.** Recording the whole file would mean calling the tool with arguments the model didn't send, which changes what the model saw. Fallback, if Arete and the second model ask for different ranges in S2: on a `read_file` miss, look for a recording of the same path with no range and slice it. That's about 30 lines. Build it only if S2 shows the problem.
5. **`hermes cassette run` works from outside.** It starts `hermes -z` as a child process with the recorded request, the chosen model or skill, and `--in <recorded cwd>`. It grades the child's final answer: from `post_llm_call` if the plugin captured it, otherwise from stdout. The cassette's recorded results are never changed by a run. Each run's verdict is written to `runs/<cassette>/<timestamp>.json`.
6. **Verdict.** PASS means every pin matched and there were zero misses. DIVERGED means one or more misses (shown even if the pins matched). FAIL means a pin was missing. If a run both missed and lost a pin, it shows FAIL plus a note of the miss count. A "path" column shows hits and misses and the call order compared with the recording. It never sets the colour. Pin matching ignores case, treats `\` and `/` as the same, and accepts `re:` for a regex pin.
7. **Storage.** One JSON file per cassette in `plugin_data_dir("session-cassettes")/cassettes/`. Writes go to a temporary file first and are then renamed into place (atomic). The file has a `format: 1` field.
8. **No third-party dependencies.** Standard library only. Output is a plain-text table with ANSI colour, which Windows Terminal supports, and turns off with `NO_COLOR`.

## 3. Spikes (first 2 hours, before real code)

**S1. Mode switching through `-z`** (biggest risk, 45 min). Build a 20-line throwaway plugin that logs `os.getpid()`, one env var and every `tool_execution` call to a file. Install it in `%LOCALAPPDATA%\hermes\plugins`, run `hermes -z "read README.md" -m arete:27b`, and check whether the env var arrives.
- If it arrives, the mode is the env var `HERMES_CASSETTE=record:<name>` or `replay:<name>`.
- If it doesn't, use an armed state file: `hermes cassette record` writes `armed.json`. The next `on_session_start` claims it for that `session_id` and deletes it, and middleware checks the `session_id` before acting. Other sessions are never touched. `on_session_start` is then added to the declared hooks.
- Also check whether interactive `hermes` sessions load the plugin in the same process. Recording happens in the interactive CLI.

**S2. What Arete actually calls** (45 min). Build `demo/fixture-repo/` (with `server/auth.py` binding 8080 plus three or four decoy files), ask the demo question three times with Arete through `-z`, and log the tool calls with the S1 plugin.
- If `terminal` shows up, add the read-only terminal allowlist (`cat`, `type`, `ls`, `dir`, `rg`, `git show`/`log`/`diff`, matched on the exact command string plus cwd). Otherwise drop terminal support. This settles the open dispute in `SPEC.md`.
- Also run the second model on the same question, to see whether its arguments differ (decision 4).

**S3. Second model** (the owner, in parallel, about 10 min of pulling). Ollama only has Arete. Suggestion: `ollama pull qwen3:8b` (about 5 GB, reliable tool calling, different family). Fallback: `llama3.1:8b`. The owner runs it, because it is a network download.

## 4. Build order and time boxes

| Block | Hours | Work | Done when |
|---|---|---|---|
| A | 0–2 | Spikes S1–S3 | Mode mechanism chosen. Terminal in or out. Second model answers the demo question. |
| B | 2–4 | `store.py` (format, keys, normalisation, atomic write) and `verdict.py` (pins, table) | `pytest tests/` passes on fake payloads, including Windows path cases. |
| C | 4–6 | `__init__.py`: `register()`, record and replay middleware, `post_llm_call` capture, miss path | Unit tests call the middleware with a fake `next_call`, and in replay mode `next_call` is asserted never to run. |
| D | 6–8 | `cli.py`: `hermes cassette record / pin / run / list / show` | All five commands work against a real install. |
| E | 8–10 | End-to-end on Windows: record with Arete, pin, replay with Arete, then with qwen, then the red cases | Green, diverged and red each reproduce twice in a row. |
| F | 10–11 | `hermes plugins validate <path> --install-deps` and fix every failure and warning | Clean output, saved to `docs/VALIDATION.txt`. |
| G | 11–13 | README (pitch, install, 60-second walkthrough, disclosures, known issues, what was tested on what), `plugin.yaml` final, catalog YAML draft | Grok reviews the README. Every claim traces to a test or an E run. |
| H | 13–15 | Buffer and Grok's code-review fixes. Freeze, tag the final commit, write the 40-character SHA into the catalog entry | The owner has the SHA. |
| I | The owner | Push, PR, video, form | Before 5 PM EST, with at least 2 hours to spare. |

**What gets cut, in this order, if time runs short:** the `show` command, regex pins, the `read_file` range fallback, terminal replay, the per-run history files. **Never cut:** the miss path, the validator passing, the honest README.

## 5. Tests that back the README

- Unit tests: normalisation (`C:\Repo\x.py`, `c:/repo/x.py` and `./x.py` give one key), pin matching, verdict precedence, atomic write, and that replay never calls `next_call` for any tool name, including `terminal`, `write_file` and `delegate_task`.
- Live tests (logged in `docs/TESTED.md` with date, model and result): record with Arete, replay with Arete, replay with qwen, the tampered-cassette red, and the wrong-skill red. Plus one replay with Wi-Fi off or Ollama's web tools unreachable, to back "offline". Only if quick.
- Not tested, said in the README: macOS, Linux, the TUI and desktop app, gateway platforms (Telegram and others), cloud providers.

## 6. Disclosures for the README and PR (rule 13)

- Cassettes store the full results of recorded tools: file contents, search hits, web extracts and session-search results. They are plain JSON under the plugin data folder. Anyone with the file sees that content.
- In replay mode no tool runs. Every tool call is answered from the cassette or refused with a `cassette miss` error. That includes tools that would normally ask for approval. Nothing is executed, so nothing is approved.
- `hermes cassette run` starts one `hermes -z` child process with the model the user names. It uses no `--yolo` and changes no approval settings.
- No network calls of its own, no telemetry, no judge model, no self-update. (If terminal replay stays: it records the output of a fixed list of read-only commands.)

## 7. GitHub setup (for the owner and Grok, now)

- Repo: `github.com/<owner>/session-cassettes`, **public**, default branch `main`. Create it empty: no auto-generated README, licence or .gitignore, so it doesn't clash with the local files. Then `git remote add origin ...`. **Don't push yet.** There is no plugin code, and the pinned SHA should come from the frozen commit anyway.
- Licence: MIT is the usual choice in the catalog. The owner decides. Add a `LICENSE` file locally, not through the GitHub UI.
- Layout: the plugin sits at the **repo root** (`plugin.yaml`, `__init__.py` at the top). The catalog `subdir` field is then left out. Docs, tests and demo sit beside it. `.gitignore` already excludes `__pycache__` and `.pyc` (rule 17).
- Local folder name vs plugin name: the local folder is `hermes-cassettes`, but `plugin.yaml` `name`, the catalog file and the repo should all be `session-cassettes`. To install locally for testing, copy or link the folder into `%LOCALAPPDATA%\hermes\plugins\session-cassettes`.
- Leave as-is or strip before the freeze: `AGENTS.md`, `CLAUDE.md` and `docs/RESEARCH.md` are harmless to publish. The owner decides whether internal notes stay public.

## 8. Decisions the owner needs to make

1. Approve design decisions 1–8 above, or send back Grok's objections.
2. Pull the second model (S3).
3. Pick the licence and create the GitHub repo (section 7).
4. After the spikes: terminal in or out (Claude will recommend based on S2's evidence).
