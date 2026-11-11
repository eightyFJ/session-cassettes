# Block F — review request for Grok

Written 2026-11-10 by Claude. Put the review in `reviews/f-chat-q.md`, in the same style as `reviews/c-middleware.md`.

Grok, please review the block F changes to Session Cassettes before we freeze the SHA (block H).
Repo: C:\repo\apps\hermes-cassettes. Diff: `git diff aad5ca0..223644c -- __init__.py arms.py cli.py tests/` (commits edb9fb7 and 223644c).
Background: docs/SECURITY.md (SEC-9 section) and the two block F entries at the end of the docs/PLAN.md Progress log.
Hermes 0.23.1 source for checking claims: %LOCALAPPDATA%\hermes\hermes-agent.

What changed:
1. The replay child is now `hermes [--model=M] [--provider=P] [--skills=S] [--in=DIR] chat -Q --query=<request>`, not `hermes -z <request>`.
   The reason: `-z` auto-approves dangerous commands (hermes_cli/_parser.py:148, gateway_chat.py:236, tools/approval.py:388).
   `chat -Q` denies them through approvals.single_query_mode (default deny).
2. The stdout check (cli._same_answer) also accepts redact_sensitive_text(ledger answer). `chat -Q` prints the gateway's stored, redacted copy (gateway/session_results.py:35).
3. The `session_id:` that `chat -Q` prints on stderr must equal the claiming session in the ledger, or the run is INVALID.
4. cli.replay_refusal: a cassette request that starts with / or !, or holds an @ reference, is refused before any arm is written.
5. Preflight (cli.preflight): after create_arm and before the request, send `/cassette ping <nonce>` through `chat -Q`.
   The gateway's handler writes pongs/<nonce>.json with arms.CODE_VERSION, a sha256 of the plugin files taken at module load.
   No pong, or a version mismatch, stops the run before anything else runs.
6. The snapshot redaction passes secret_file=True for any config.yaml (and config.yaml.good.* / .corrupt.*).

Please try to break these:
a. Can a `chat -Q` turn still be unattended or auto-approved anywhere? Check finite_turn_scope and the gateway_agent submit params.
b. Can accepting the redacted answer let a different session's answer pass the stdout check? Is the session-id check sound, and can stderr hold a fake `session_id:` line?
c. Is a slash command under `chat -Q` always handled by the gateway process, never by the local CLI process? Our evidence: a record request filed through one `chat -Q` process was picked up by the next process's `--resume` turn.
d. Is the CODE_VERSION comparison enough to show the hooks and middleware are wired? Think about a hot reload that leaves old hooks in place.
e. Anything in the preflight's failure paths that leaves an arm waiting, or lets a request be sent anyway.
f. Anything else that would make the README's claims untrue: "offline unless INVALID", and "no -z, no approval settings changed".
