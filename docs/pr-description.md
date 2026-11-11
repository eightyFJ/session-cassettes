# Pull request text for NousResearch/hermes-agent

**Title:** `plugin-catalog: add session-cassettes`

**File added:** `plugin-catalog/session-cassettes.yaml` (the content of `docs/catalog-entry.yaml` below its comment lines)

---

## What it is

Session Cassettes records the read-only tool results of one Hermes session and lets the user pin the facts a good answer must contain. It then replays the same request against another model, prompt or skill. During a claimed replay, `tool_execution` middleware answers every tool call of the replayed turn itself and never calls the real tool. The answer is the exact recorded result, or a `read_file` or `search_files` answer built from a snapshot of the workspace files the session's results named. Anything else gets a cassette-miss error. The verdict is PASS, FAIL, DIVERGED or INVALID, computed from the pins with no judge model.

## The entry

- Repo: https://github.com/eightyFJ/session-cassettes
- Pin: `3f06ba568f2a0620a8a1d53bae87e80d2dcfe7c3` (tag `v0.1.0`)
- Tier `community`, category `tools`, plugin at the repo root
- Capabilities: hooks `pre_llm_call` and `post_llm_call`, middleware `tool_execution`, no tools, no environment variables
- It also registers the `/cassette` slash command and the `hermes cassette` CLI command

## Disclosures (rule 13)

- **What is stored.** Cassettes are plain JSON in the plugin data folder. They hold the request, the answer and the full results of recorded tools: file contents, search hits, web search results and extracts, skill text, session-search results, and the output of a fixed list of read-only terminal commands. They also hold the redacted text of workspace files those results named.
- **What is read.** At the end of a recorded turn the plugin reads those files from the session's working directory. Hidden paths and secret-looking names (key files, credentials, `auth.json`, `token.json`, `tokens.json`) are skipped by file name. The skip goes by the name the tool result used, so a link with an ordinary name that points at a skipped file inside the folder would be read. Hermes's `redact_sensitive_text` is applied to the rest, which does nothing if the user has switched Hermes's redaction off.
- **Child processes.** `hermes cassette run` starts two `hermes chat -Q` children: argument list, no shell, no `-z`, no `--yolo`. `HERMES_YOLO_MODE` is removed from the child. The run is refused when the gateway's environment has `HERMES_YOLO_MODE` set at ping time. That check is not Hermes's own startup yolo flag. The child's output is held in two temporary files in the system temp folder until the child exits.
- **The model call is real.** The replay's model call goes to the model and provider the user names. A cloud model is sent the request and the recorded results.
- **An unclaimed run is live.** If no session claims the replay, its turn runs with real tools under the user's own approval settings and is graded INVALID afterwards.
- **Scope.** The middleware sees every session's tool calls and passes unbound sessions through, keeping only an in-memory count. The two hooks are called for every turn of every session and keep nothing for a session the plugin has not bound.
- No network calls of its own, no telemetry, no self-update, no third-party dependencies.

## Checked at the pinned commit

- `hermes plugins validate --install-deps` passes with no warnings (`docs/VALIDATION.txt`).
- semgrep `p/python`: 0 findings. bandit: no high findings, one medium in the demo fixture and eight lows, each explained in `docs/SECURITY.md`. gitleaks: no leaks in the history or the tree.
- 254 unit tests: 253 pass and 1 is skipped, on Windows and on Linux.
- Run end to end on native Windows 11 with Hermes 0.23.1 and local Ollama models (`docs/TESTED.md`).

## Known limits

- Run inside Hermes only on native Windows 11. On Linux only the unit tests have run. macOS is untested.
- `plugins.isolation: host` is untested, and by the Hermes source replay would not work in that mode.
- Recording was tested through `hermes chat -Q` only.
- Installing from GitHub had not been tried when this was written.

The README's "Limits" and "What was tested" sections give the full list.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
