# Rules that bind this entry

Read on 2026-11-10. The hackathon rules are the Nous Research posts below. There is no separate rules page. Catalog admission is a second set of rules: a plugin that wins still has to pass them to be listed.

## Hackathon

Source: [the announcement](https://x.com/NousResearch/status/2118094256438936062) (4 Nov 2026, 21:33 UTC). [The 24-hour reminder](https://x.com/NousResearch/status/2120271539006222576) (10 Nov 2026, 21:45 UTC) repeats the same deadline, prizes and entry steps. The catalog checks have not been started by a maintainer until someone opens the PR; the reminder says hundreds of entries are already in.

**Deadline: 5 PM EST on 11 Nov 2026.**

To enter, all three:

1. Open a pull request that adds the plugin to the Hermes plugin catalog. Guide: https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission
2. Post a demo video on X, under 90 seconds, tagging @NousResearch, showing the plugin working.
3. Send both links through https://form.typeform.com/to/FwJLvOln

The form can be sent as soon as the pull request is open. Catalog CI runs once a maintainer starts it. Entries are reviewed every few days. After close, each entry gets a thread in the Nous Research Discord (https://discord.gg/nousresearch).

The entry is the plugin. Judges score usefulness, reliability, polish and creativity. They favor plugins that do more than a prompt or a skill could.

Prizes: 1st $10,000, 2nd $5,000, 3rd $2,500, 4th $1,500, 5th $1,000, plus five runners-up. Places 1–3 also get a neon sign. Places 1–3 get $1,000 in credits, 4–5 get $500, runners-up get $250. Every placed entry and runner-up gets merch. Accepted entries are listed in the marketplace at https://hermes-agent.nousresearch.com/marketplace.

How to build and test: https://hermes-agent.nousresearch.com/docs/developer-guide/plugins

The form page is a Typeform shell. Its field list did not render from the public URL, so the fields themselves were not read.

## Catalog admission

Canonical text: `plugin-catalog/README.md` in the Hermes install, mirrored at the submission guide above. A test fails the Hermes build if the two copies drift. What follows is the constraint each rule puts on this plugin, not a substitute for the canonical text.

Before the PR:

- The repo is public. `repo` is an `https://` URL.
- The pinned commit has `plugin.yaml` and a loadable entrypoint (`__init__.py` here).
- `hermes plugins validate <path> --install-deps` is clean. It checks the manifest, `requires_hermes`, that the plugin loads, that declared `capabilities` match what it registers, `config_schema`, `requires_env`, Python deps against Hermes's pins, the install security scan, the desktop-surface rule and the no-core-override rule. Read the warnings. A reviewer does.

The PR adds one file, `plugin-catalog/<name>.yaml`, to `NousResearch/hermes-agent`. Pin `sha` to 40 hex characters. Quote `version`. The description says what the plugin does, which Hermes surfaces it uses, and the rule 13 disclosures. The owner submits it. He is the owner. A drive-by PR of someone else's repo is declined (rule 5).

The eighteen rules, as they apply here:

1. A person merges the entry. There is no self-serve publish.
2. Full 40-character SHA only. No branch, tag or short SHA.
3. No self-updater, no remote code fetch that replaces the plugin.
4. A later SHA bump is a new reviewed PR.
5. The repo owner or a major contributor opens the PR.
6. `capabilities` (tools, hooks, middleware, env) must match `register(ctx)`. A mismatch fails validation and is treated as a security issue.
7. The install scanner runs. `dangerous` fails the entry. `caution` is a warning the reviewer reads.
8. No desktop UI in v1, so the desktop-surface bans do not apply. Do not add a `desktop/plugin.js` that patches the app.
9. Public surfaces only: hooks, middleware, `ctx.register_*`. No `setattr` on Hermes modules, no writing core tables, no private-method calls. Undocumented core imports warn by file and line.
10. Keep Python deps inside Hermes's pins. Prefer no third-party deps. A bare `>=` floor draws a review comment.
11. No secrets in v1. Do not read `.env`, `auth.json`, or another tool's login. If a secret is ever needed, it goes through `requires_env` and `agent.secret_scope.get_secret`.
12. Do not auto-approve tools, disable guards, or spawn a Hermes or shell child in YOLO or non-interactive mode to dodge an approval. A headless `hermes cassette run` must fail cleanly when it would otherwise wait on a person.
13. Disclose in the README and the PR: anything a user would want to know before installing. For this plugin that is reading and replaying tool results, including file contents and web extracts that were in the recorded session, and any shell use if terminal replay is kept. No telemetry. Undisclosed behaviour is a change request.
14. `requires_hermes` is a SemVer floor, quoted, not newer than the current release (0.23.1 here), and the same floor is in `plugin.yaml` and the catalog entry. `version` matches the pinned code.
15. Do not reskin a bundled plugin.
16. One listing per lineage. This is a new plugin. The name `session-cassettes` was free in the catalog on 2026-11-10.
17. Source only at the pinned commit. No binaries, no committed `.pyc`, no archive that hides one.
18. Do not register a `browser_vault` login backend.

`tier` stays `community`. Only Nous maintainers set `official` or `verified`.

## Entry shape

From the schema in `plugin-catalog/README.md`:

- `name`: `[a-z0-9_-]{1,64}`
- `category`: one of `desktop`, `memory`, `platform`, `web`, `tools`, `voice`, `automation`, `models`, `general`. This plugin is `tools`.
- `platforms`: empty means all. Set it only if a platform is actually unsupported. Windows is a claimed edge, so leave this empty once it runs there.
- `known_issues`: shown at the install prompt. Use it for anything the README already admits.
- `readme: true` (the default). The page at `/docs/plugins/<name>` renders the README at the pinned SHA. What the README says is what reviewers and users see.
- Images, if any, must be on a GitHub host and pinned to that same SHA.

## Build rules that sit next to admission

From the developer guide's compatibility contract:

- Accept `**kwargs` on hook and middleware callbacks. Hermes adds hook fields by keyword and only passes the ones the signature names.
- Unknown `plugin.yaml` keys are ignored. Old Hermes builds load a newer manifest as long as the code stays on documented behaviour.
- Do not import Hermes internal modules. The Sep 2026 compatibility layer for old import paths is gone. An `ImportError` shows up in `hermes plugins list`.
- Store cassettes with the plugin data helper (`plugin_data_dir()`), not a path under the Hermes install. That directory follows the active profile and survives `hermes plugins update`.

Plugins are opt-in. `hermes plugins enable <name>` after install.
