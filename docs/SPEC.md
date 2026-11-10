# Session Cassettes — v1 spec

Written 2026-11-10 from the design discussion between the owner, Claude and Grok. Items marked **Agreed** were settled in that discussion. Items marked **Proposal** are Claude's and still need Grok's check or the owner's decision.

## The product in one paragraph

Record a session's read-only tool calls and their results into a cassette file. Pin three or four short facts the final answer must contain. Later, replay the same user request against another model, prompt or skill. During replay the cassette answers every tool call, so no real tool runs. The run passes if every pinned fact is in the final answer and every tool call was found in the cassette.

## Decisions

### Replay mechanism — Agreed

- Replay uses `tool_execution` middleware. The callback returns the recorded result and does not call `next_call`, so the real tool never runs.
- A `pre_tool_call` hook cannot do this. It can only block a call, change its arguments, or send it for approval.
- Recording uses the same middleware: call `next_call`, store the result, return it unchanged.
- The build is middleware, a JSON store and a `hermes cassette` command.

### Grading — Agreed

- Do not grade on the tool-call sequence. A better model may read files in a different order, skip a redundant call or batch two reads. Grading on sequence would mark a correct run as failed.
- The user types the pinned facts at record time. No judge model is involved.
- Three outcomes:
  - **Pass:** every pinned fact is in the final answer, and every tool call was a hit.
  - **Fail:** a pinned fact is missing from the final answer.
  - **Diverged:** at least one tool call was not in the cassette. This is its own row, not a failure.
- The sequence difference is shown in a second column labelled "path". It never decides the colour.

### Cassette contents — Agreed

- Each read-only call is stored under a key made of the tool name plus its canonical arguments.
- The cassette also stores the user request and the final answer.
- On replay, a hit returns the stored result. A miss returns a tool error that says `cassette miss`, and the run is marked diverged.

### Scope of v1 — Agreed

- Read-only tools only. Leave out `write_file`, `patch`, `execute_code`, `delegate_task`, the browser tools and `memory`. Stubbing a write would misreport the state of the files, and running one live would break "offline and free".
- Tools to record and replay: `read_file`, `search_files`, `web_search`, `web_extract`, `skill_view`, `skills_list`, `session_search`.
- Cut from v1: speculative prefetch (the "Precog" idea), a reviewer model, embeddings, and a desktop UI.

### Terminal — Proposal (Grok said leave it out; Claude disagrees)

- Local models such as Arete often read files through `terminal` (`cat`, `rg`, `ls`, `git show`) instead of `read_file`. If terminal is excluded, the demo run may diverge on our own model.
- Proposal: allow a short list of read-only commands, matched on the exact command string plus working directory. Never record or replay any other terminal command.
- Settle this by watching what Arete actually calls on the demo task. If Arete uses `read_file` and `search_files` reliably, drop terminal support and keep v1 smaller.

### Argument normalisation on Windows — Proposal

Without this, a path written two ways produces a false miss.

- Convert `\` to `/`.
- Lowercase the drive letter.
- Resolve relative paths against the session's working directory.
- Record `read_file` as the whole file, and cut out the requested line range at replay time. A model that asks for a different range then still gets a hit.

### Pinned-fact matching — Proposal

- Match case-insensitively.
- Treat `\` and `/` as the same character, so `server\auth.py` matches `server/auth.py`.
- Allow an optional regex pin.

## Command surface — Proposal

The names below are a starting point, not a decision.

| Command | Does |
|---|---|
| `hermes cassette record <name>` | Turns on recording for the next session and saves it as `<name>`. |
| `hermes cassette pin <name> "<fact>"` | Adds a pinned fact to a cassette. |
| `hermes cassette run <name> --model <model>` | Replays the recorded user request against the given model and prints the verdict table. |
| `hermes cassette list` | Lists cassettes with their pins and last verdict. |

Store cassettes under the profile's plugin data folder, one JSON file per cassette.

## Demo script (under 60 seconds) — Agreed

1. In a small fixture repo, ask: "Where is the auth handler and what port does it bind?"
2. Record the session with `arete:27b`. Pin `server/auth.py` and `8080`.
3. Replay against a different model. The table shows green.
4. Show red one of two ways:
   - Edit the recorded file content in the cassette so the port reads 9090. The pin `8080` now fails.
   - Or point the run at a skill that names the wrong file.

**Gap to close before recording:** Ollama only has Arete variants. Swapping `arete:27b` for `arete:27b-q8` is the same model and shows nothing. Pull a smaller model from a different family first, and keep the total under about 22 GB of VRAM.

## Settle these before writing code

These were not checked in the source. Each one can change the design.

1. **How does `hermes cassette run` start a session without a person typing?** Find the supported way to run one user request headlessly with a chosen model (look at `batch_runner.py`, `run_agent.py` and the CLI's one-shot query option). This is the biggest unknown.
2. **What exactly does a `tool_execution` callback receive and return?** Read `hermes_cli/middleware.py` from line 144. Confirm the payload fields, the result type (string or dict), and what a tool error looks like so `cassette miss` can be returned in the right form.
3. **How does the plugin get the user request and the final answer?** Candidates are the `on_turn_start`, `on_turn_complete` and `post_llm_call` hooks. Check their arguments.
4. **How does the middleware know whether it is recording, replaying or off?** Candidates are a state file in the plugin data folder or a session environment variable. Check `ctx.register_session_env` and the plugin storage helper in `plugins/plugin_storage.py`.
5. **Does middleware fire for tool calls made inside sub-agents?** `delegate_task` is out of scope, but confirm nothing leaks around the cassette.
6. **Is the plugin name free?** No catalog entry contains "cassette". Decide the final name (`session-cassettes` is the working name).

## Build order

1. Answer the six questions above.
2. Write the store and the recording middleware. Test with fake payloads.
3. Write the replay middleware, including the miss path.
4. Write the `hermes cassette` commands and the verdict table.
5. Build the fixture repo and run the demo task with Arete for real.
6. Run `hermes plugins validate <path> --install-deps` and fix every failure.
7. Write the README, with known issues stated honestly.
8. Write the catalog entry. The owner pushes the repo, opens the PR, records the video and sends the form.

## Planned layout

```
hermes-cassettes\
├─ plugin.yaml          manifest; capabilities must match the code
├─ __init__.py          register(ctx): middleware, hooks, CLI command
├─ store.py             cassette file format, keys, normalisation
├─ verdict.py           pinned-fact matching and the result table
├─ tests\               fake payloads plus the fixture repo
├─ demo\fixture-repo\   the small repo used in the video
└─ docs\
```
