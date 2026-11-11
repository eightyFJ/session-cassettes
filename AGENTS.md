# Session Cassettes — a Hermes Agent plugin (hackathon entry)

**Status on 2026-11-11:** blocks A to H of `docs/PLAN.md` are done. The plugin is built, with 254 unit tests. It's installed and has run end to end in a live Hermes (`docs/TESTED.md`). The validator and security checks are in `docs/VALIDATION.txt` and `docs/SECURITY.md`. Block G wrote the public `README.md` and the catalog entry draft `docs/catalog-entry.yaml`, which still has a placeholder SHA. Grok's README review (`reviews/g-readme.md`) and recheck (`reviews/g-readme-recheck.md`) are both applied, and the recheck closed both serious findings. The Fable 5.2 xhigh README audit is done and its findings are applied, docs only. It also ran the README walkthrough once from start to finish, and that passed. Block H: the pre-freeze code read found one bug in `snapshot.py`, now fixed (a workspace behind a link got no snapshot), and the validator, semgrep, bandit and gitleaks are all clean on the changed code. **The plugin is frozen.** The frozen commit is the one `docs/catalog-entry.yaml` names. The commit right after it fills that SHA in and records the scans rerun on the frozen commit. The freeze was redone on 2026-11-11 after a comment cleanup with no change in behaviour (commit `3f06ba5`), so the pin moved; the Progress log has the entry. The repo is pushed and public, and the pull request is open (NousResearch/hermes-agent#153854). What is left of block I is the owner's: video, form. The Progress log at the end of `docs/PLAN.md` says what is done and what is not yet tested.

## What this is

Session Cassettes records the tool calls and results of a good Hermes session. Later it runs the same user request against a different model, prompt or skill, and serves the recorded results back instead of running the real tools. The replay is offline, free and repeatable. The verdict is whether the new run still lands a few facts the user pinned at record time.

A prompt or a skill cannot do this, because neither can stand in for tool execution. That is the pitch.

## Deadline and submission

- **Deadline: 2026-11-11, 5 PM EST.** This is from the official announcement and the 24-hour reminder posted the same day. Details, prizes and links are in `docs/RULES.md`.
- What an entry needs:
  1. A public GitHub repository holding the plugin.
  2. A pull request to `NousResearch/hermes-agent` adding one file, `plugin-catalog/<name>.yaml`, pinned to a full 40-character commit SHA.
  3. A demo video under 90 seconds on X, tagging @NousResearch, showing the plugin working.
  4. Both links sent through https://form.typeform.com/to/FwJLvOln. The form can go out as soon as the pull request is open.
- Judging is on usefulness, reliability, polish and creativity. Judges favor plugins that do more than a prompt or a skill could.

## Who does what

- **Claude (Claude Code)** builds the plugin.
- **Grok** reviews the design and the code.
- **Arete 27B** (through Hermes on local Ollama) runs the plugin for real and checks it. It is also the record-side model in the demo.
- **The owner** decides, records the video, and submits. Pushing to GitHub, opening the PR, posting on X and sending the form all wait for the owner's say-so.

## Read next

1. `docs/SPEC.md` — the v1 design, the demo script, the build order, and the open questions to settle first.
2. `docs/RULES.md` — the hackathon rules and the catalog admission rules that bind the entry.
3. `docs/RESEARCH.md` — what was checked in the installed Hermes source, with file paths, plus the neighbouring catalog plugins.

## Rules for working here

- Use public plugin surfaces only: hooks, middleware and the `ctx.register_*` APIs. Never patch Hermes code at runtime.
- The `capabilities` declared in the manifest and catalog entry must exactly match what the plugin registers. A mismatch fails catalog validation.
- No network calls, no telemetry, no self-updating code, and no second model acting as a judge.
- Treat the Hermes install at `%LOCALAPPDATA%\hermes\hermes-agent` as read-only reference. Do not edit it.
- Build and test on native Windows first. Running on Windows is one of the entry's edges.
- Never delete the owner's files. Anything unwanted goes to `C:\repo\_archive\`.
- Say plainly what was tested and what was not. The README claims nothing that was not tested.

## Environment

- Hermes Agent 0.23.1 (commit `3b421578`), installed at `%LOCALAPPDATA%\hermes\hermes-agent`. Config is `%LOCALAPPDATA%\hermes\config.yaml`. User plugins install to `%LOCALAPPDATA%\hermes\plugins` (currently empty).
- The default model is `arete:27b` through Ollama at `http://127.0.0.1:11434/v1`, with a 64k context window. Enabled toolsets are `hermes-cli` and `web`. Streaming is off.
- Ollama only has Arete variants installed. The demo needs a second model from a different family (see `docs/SPEC.md`).
