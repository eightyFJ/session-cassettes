# Research notes

Checked on 2026-11-10 against the installed Hermes Agent 0.23.1 (commit `3b421578588243f920886e6fe79a6faa87ea026e`). All paths below are relative to `%LOCALAPPDATA%\hermes\hermes-agent` unless they start with a drive letter.

## Checked in the source

### Middleware

- `website/docs/developer-guide/plugins/index.md`, around lines 1688–1722, documents middleware.
- There are four kinds: `tool_request`, `llm_request`, `tool_execution`, `llm_execution`. The canonical list is `VALID_MIDDLEWARE` in `hermes_cli/middleware.py:24`.
- `tool_execution` receives the payload plus `next_call`. The doc says: call `next_call(payload)` exactly once to run the real tool, "or skip it to short-circuit", and return the result.
- `next_call` is single-use. Calling it twice raises.
- A middleware callback that raises is logged and skipped, and the chain continues.
- Register with `ctx.register_middleware("<kind>", callback)` inside `register(ctx)`.
- The runner is `run_tool_execution_middleware(tool_name, args, next_call, **context)` at `hermes_cli/middleware.py:144`. The exact fields passed to the callback were not read yet.

### Hooks

- `pre_tool_call` can return `{"action": "block", "message": ...}`, `{"action": "modify", "args": {...}}` or `{"action": "approve", ...}`. It cannot return a tool result. See `website/docs/developer-guide/observer-hooks.md:63` and `index.md:1538`.
- Most other hooks are observers whose return values are ignored. Hook names seen in the source include `post_tool_call`, `pre_llm_call`, `post_llm_call`, `on_session_start`, `on_session_end`, `on_turn_start` and `on_turn_complete`.
- A Python hook that runs longer than `plugins.hook_callback_timeout` (default 30 s) is abandoned. A timed-out `pre_tool_call` blocks the tool. See `website/docs/user-guide/features/hooks.md:396`.
- The full hook reference is `website/docs/user-guide/features/hooks.md`.

### Commands

- `ctx.register_cli_command()` registers a CLI subcommand such as `hermes my-plugin <subcommand>` (`index.md:938`).
- `ctx.register_command()` registers an in-session slash command (`index.md:939`).

### Manifest and catalog entry

- A minimal bundled manifest to copy from is `plugins/disk-cleanup/plugin.yaml` (fields: `name`, `version`, `description`, `author`, `provides_hooks`).
- A full catalog entry to copy from is `plugin-catalog/completion-proof.yaml`. Its fields include `name`, `repo`, `sha`, `subdir`, `description`, `maintainer`, `tier`, `category`, `requires_hermes`, `version` (quoted), `platforms`, `readme`, `known_issues`, and a `capabilities` block with `provides_tools`, `provides_hooks`, `provides_middleware` and `requires_env`.
- Catalog categories are `desktop`, `memory`, `platform`, `web`, `tools`, `voice`, `automation`, `models` and `general`.
- The admission rules are in `plugin-catalog/README.md` and `website/docs/developer-guide/plugins/catalog-submission.md`. The ones that shape this build:
  - The repository must be public, and the entry pins a full 40-character SHA.
  - The owner or a major contributor submits the entry.
  - No self-updating code.
  - Declared capabilities must match what the plugin registers.
  - The install security scan runs at admission. A `dangerous` finding fails the entry.
  - The PR description must disclose what the plugin does and which Hermes surfaces it uses. Rule 13 is summarised in `docs/RULES.md`.
- Validate locally with `hermes plugins validate /path/to/plugin --install-deps`. Catalog CI runs the same check.
- Enable a plugin with `hermes plugins enable <name>`. Plugins are opt-in.

### Tools

- The core tool list is `_HERMES_CORE_TOOLS` at `toolsets.py:11`. It includes `web_search`, `web_extract`, `terminal`, `process_manage`, `read_file`, `write_file`, `patch`, `search_files`, `skills_list`, `skill_view`, `session_search`, `execute_code`, `delegate_task`, `memory`, the browser tools and the kanban tools.
- No per-tool read-only flag was found in the registry. The plugin needs its own allowlist.

### Built-in verification (relevant to the rejected "Shown Work" idea)

- Hermes has a `verify_on_stop` setting (off by default) that refuses a final answer when code was edited without fresh verification evidence. See `website/docs/user-guide/configuration.md:1379`.
- A `pre_verify` hook fires once per coding turn after that guard. See `website/docs/user-guide/features/hooks.md:838`.

## Neighbouring catalog plugins

The catalog has 1,380 entries. None contains the word "cassette" or "speculative". The closest neighbours, from their catalog descriptions:

| Plugin | What it does | Why it is not the same |
|---|---|---|
| `workflow-record` | Records a browser workflow once and turns it into a skill that replays it with the browser tools. | Replays actions for real. Does not stand in for tool results. |
| `afterforge` | Turns agent failures and corrections into reviewed regression checks. | Works from failures, not from replaying a good session. |
| `model-drift-watch` | Runs 34 fixed, exactly graded questions on a schedule to catch a model getting worse. | Fixed questions with no tools. |
| `agent-run-debugger` | Imports one trace and builds a timeline with failure points and hypotheses. | For a person to read. Does not re-run anything. |
| `completion-proof` | Revision-bound receipts for configured checks; Linux only. | About test evidence after edits, not replay. |
| `session-lens`, `agent-station` | Both exist in the catalog. Grok reports they play a trace back for a person to read. | Their entries were not read in this session. Check them. |

## The hackathon

The announcement is the rules. There is no separate rules page. The full write-up, including the eighteen catalog admission rules as they apply here, is `docs/RULES.md`.

Confirmed from the announcement (4 Nov 2026) and the reminder post (10 Nov 2026, 21:45 UTC), which repeats the same deadline:

- Deadline: 11 Nov 2026, 5 PM EST.
- $20,000 in cash prizes, $10,000 for first, plus merch, neon signs and credits. The split is in `docs/RULES.md`.
- Judged on usefulness, reliability, polish and creativity. Plugins that do more than a prompt or a skill could are favored.
- Entry: a catalog PR, a demo under 90 seconds on X tagging @NousResearch, and https://form.typeform.com/to/FwJLvOln.

The catalog submission guide and `plugin-catalog/README.md` (local copy, Hermes 0.23.1) were read on 2026-11-10. The Typeform field list did not render, so those fields are still unknown.

Sources:
- https://x.com/NousResearch/status/2118094256438936062
- https://x.com/NousResearch/status/2120271539006222576
- https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission
- https://hermes-agent.nousresearch.com/docs/developer-guide/plugins

## Ideas considered and dropped

- **Shown Work** (Grok's first idea: prove "tests pass" from a tool ledger). Dropped because Hermes's built-in `verify_on_stop` and the `completion-proof` plugin already cover most of it. The part nobody covers is scanning the diff for weakened tests (a new `skip` or `xfail`, a deleted test, a removed assertion).
- **Precog** (start likely read-only tools early while the model is still writing the call). Dropped for the deadline: it needs partial-JSON parsing and streaming, which is off in this config.
- **Lossless** (measure what compaction forgot), **Multiverse** (fork a stuck session and race the branches) and an **intent tripwire** were ranked lower on build risk or crowding.
