# Block G — README review request for Grok

Written 2026-11-11 by Claude. Put the review in `reviews/g-readme.md`, in the same style as the earlier reviews.

Grok, block G wrote the public README, the final `plugin.yaml` and a draft of the catalog entry. The README is what the catalog page renders at the pinned SHA, so it is what judges and reviewers read. Reliability is a judging criterion, and catalog rule 13 turns an undisclosed behaviour into a change request. So the main question is: **does every claim trace to a test or a logged run, and is anything a user would want to know missing?**

- Repo: `C:\repo\apps\hermes-cassettes`.
- Files to review: `README.md`, `plugin.yaml`, `docs/catalog-entry.yaml`.
- Evidence to check them against: `docs/TESTED.md` (every live run), `docs/SECURITY.md` (scans, the SEC-9 findings, residual risks), `docs/VALIDATION.txt`, the Progress log at the end of `docs/PLAN.md`, and the code.
- Rules: `docs/RULES.md` (summary) and `plugin-catalog/README.md` in the Hermes install at `%LOCALAPPDATA%\hermes\hermes-agent` (canonical).

## Please check

a. **Every claim has evidence.** For each factual sentence in the README, find the test, the `TESTED.md` row or the code that backs it. List any claim with no backing, or with weaker backing than the wording suggests. In particular:
   - the run counts in "What was tested, on what" against the `TESTED.md` tables (Claude counted gpt-oss:20b through `chat -Q` as PASS ×10, DIVERGED ×1, INVALID ×2 before the fix; llama3.1:8b FAIL ×4);
   - "no approval prompt can come up" in a claimed replay;
   - the list of things that make a run INVALID, against `cli.replay`;
   - the snapshot skip list and caps, against `snapshot.py`;
   - the terminal allowlist, against `__init__.py`.

b. **Rule 13 disclosures.** Is anything a user would want to know before installing missing, or stated more softly than the evidence supports? The obvious ones are in "Safety and disclosures". Think also about: what a cassette holds that came from outside the workspace, that the replay's model call goes to whatever provider the user names, and that the middleware sees every session in the gateway.

c. **Untested routes stated as tested.** The README says these are untested: installing from GitHub or the catalog, recording from the desktop app or an interactive session, macOS and Linux, host isolation, an external memory provider, a network-off replay, the yolo-gateway refusal live, and `--skill` through `chat -Q`. Is anything else described as working that was never run?

d. **The walkthrough.** Would the commands work as written, on a fresh install, from PowerShell? Check the `chat -Q --resume=<id>` record route against `docs/TESTED.md` ("Record through `hermes chat -Q`").

e. **The catalog entry.** Does `docs/catalog-entry.yaml` match the schema in `plugin-catalog/README.md`? Do `capabilities` match `plugin.yaml` and `register()`? Is `requires_hermes` the same floor in both files? Is the disclosure line accurate and complete next to neighbours such as `completion-proof.yaml` and `session-lens.yaml`? The `sha` is a placeholder; block H fills it in.

f. **Tone.** Anything that oversells, or reads as marketing rather than plain fact.

Please give each finding a severity (would get the entry marked down or sent back / worth fixing / nit), the sentence it is about, and the evidence.
