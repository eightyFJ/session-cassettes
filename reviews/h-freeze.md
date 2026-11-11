# Block H — freeze

Written 2026-11-11. The request did not name a review path. Read `snapshot.py` (`capture`), `store.py` (`safe_join`, `_windows_context`, `_normalise`, `is_within`), `cli.py` (`find_hermes`, the pong check), `arms.py` (`process_yolo`), and the tests named below, before `docs/pr-description.md` and `docs/catalog-entry.yaml`. Checked rule 13 in the Hermes 0.23.1 catalog README and in `docs/RULES.md`. Did not run the tests, the scanners, or a live recording.

The freeze can stand. Nothing below needs a change in plugin code. Two wording fixes belong in the pull-request text before it is opened.

## 1. The snapshot link check

A link inside the workspace whose real path leaves both the workspace path and the workspace's real path is still skipped. The `except Exception` fallback is the old check. The real path is broader only when the workspace path itself is a link to a broader directory.

`capture` keeps two gates. The path as spelled has to sit strictly inside the workspace path (`is_within` and not equal to the workspace). Only then is the real path checked:

```python
real_key = normalize_path(os.path.realpath(disk), cwd)
if not (is_within(real_key, ws_key) or is_within(real_key, real_ws_key)):
    notes.append((raw, "link points outside the workspace"))
```

`ws_key` is the workspace path as given. `real_ws_key` is `realpath` of that same path. The new `or` adds that one prefix.

**Can a link inside lead out and still be read?** No, when that link's real path is outside both prefixes. A normal folder, or a junction whose real path is that same folder, still refuses `linked/secret.txt`. `test_directory_link_escape` expects that note and an empty snapshot. `test_workspace_reached_through_a_link` expects the two files inside the linked workspace and the same note for `linked/secret.txt`. A relative `..` is collapsed by `normalize_path` before this check, so a spelled path that leaves the workspace is "outside the workspace" and is never opened. I did not run the tests.

**Is the fallback safe?** Yes. Any exception while resolving the workspace sets `real_ws_key = ws_key`. The condition is then the old one, `is_within(real_key, ws_key)`. That can skip files the new check would have kept. It cannot accept a real path the old check would have refused. `KeyboardInterrupt` is not an `Exception`, so it is not swallowed.

**Can the real path be broader than the path the user typed?** Yes, when the workspace path itself is a link to a broader directory. A junction at `C:\repo\app` whose target is `C:\` makes `real_ws_key` `c:/`. A name spelled under that junction, such as `Windows\win.ini`, is lexically inside `c:/repo/app` and its real path is under `c:/`, so it is read. The same thing happens if the junction target is the parent: a sibling folder addressed through the junction is under `real_ws_key` and is read. A path that is not spelled under the workspace is still "outside the workspace". A link from inside that tree to another drive fails both prefixes and is skipped.

That is the directory the workspace path actually is. Refusing it would also refuse a workspace that is a junction to the project, which is the bug this change fixes (`/tmp` on macOS, a Windows junction Hermes has not already resolved). It does not need a code change. The request's live note stands: two recordings started from a junction never showed the plugin a linked workspace, because Hermes had already resolved it. Coverage of the positive case is the unit test.

## 2. The five Windows-only cases

None of the five is a gap in the plugin on Linux or macOS. One whole test is skipped there. The other four are extra assertions inside tests that still run.

**`.cmd` launcher.** `test_batch_wrappers_are_refused_on_windows` is `skipUnless(os.name == "nt")` because it forces `os.name` to `nt` and builds Windows paths. On Linux `find_hermes` still skips a `.bat` or `.cmd` suffix (`if not windows and suffix in (".bat", ".cmd")`). The `cmd.exe` re-parse this test is about does not exist on Linux. A native `hermes` executable is what that platform should start.

**`safe_join` drive and UNC.** `test_safe_join_refuses_escape` always refuses `..` and an empty part. It adds `("C:\\x",)` and `("\\\\server\\share",)` only when `os.name == "nt"`. On Linux those strings are single file names: `\` is not a separator, and neither string is absolute, so `Path.joinpath` stays inside the data folder. `..` is still refused on every platform. Cassette names, arm ids, and ping nonces are checked before `safe_join`, and `C:\x` is already an illegal cassette name.

**Git Bash form, and upper case with backslashes.** `test_path_spellings_hit_the_same_entry` always checks `./server/auth.py` and the absolute path. It adds the `/c/...` spelling and `SERVER\AUTH.PY` only on Windows, because with a POSIX temp directory those two strings are different files. `synthesize` looks up `normalize_path`. `_windows_context` follows the working directory: a POSIX cwd keeps case and leaves `/c/x` as `/c/x` (`test_posix_context_keeps_case_and_msys_shape`, which runs on every platform). A Windows cwd still folds case and rewrites `/c/repo` to `c:/repo` on Linux too (`test_windows_spellings_give_one_key`, cwd `C:\Repo`). Git Bash paths are a Windows recording. Leaving them unfolder on a Linux cwd is the right behaviour.

## 3. The pull request text

The catalog description is the accurate disclosure. The PR body shortens two clauses a reviewer would use. Both are docs. The pinned plugin commit can stay.

Rule 13 in the Hermes catalog README: what a user would want to know before installing goes in the PR description and the plugin README (network calls, reads outside the plugin's own data, shell commands, long-running processes, stored credentials). Undisclosed behaviour is a request for changes. `docs/RULES.md` says the same for this plugin's stored tool results.

**Worth fixing.** The body's "What it is" says a claimed replay answers every tool call of the replayed turn from the recording, so no real tool runs. The code answers an exact recorded result, or a `read_file` / `search_files` slice of the snapshot, or a `cassette miss`. The description field in `docs/catalog-entry.yaml`, which this PR adds as the catalog file, already says that. The body should use that sentence. The unclaimed-run bullet is right: a run nobody claims uses real tools and is graded INVALID afterwards.

**Worth fixing.** The body says the run is refused when the gateway's environment has `HERMES_YOLO_MODE` set. The catalog description says the check is at ping time and is not Hermes's own startup flag. `process_yolo` reads the gateway process environment when the ping is handled (`1`, `true`, `yes`, `on`). `cli.preflight` raises when `pong["yolo"]` is true, before the arm exists. A gateway that no longer has the variable at ping time is not refused. The README says that. The body should keep the catalog's clause so the PR description does not read as Hermes's frozen yolo flag.

**Nit.** The body says the child's output is held in two temporary files. The catalog description says they are in the system temp folder. The README says they are read back and deleted before the verdict is printed. The entry file already has the location.

**Nit.** The README says the skip checks the name the tool result used, so a link with an ordinary name that points at a skipped file inside the folder is read. The PR and the catalog description stop at "skipped by file name" and list `auth.json` and `token.json` as examples (`tokens.json` is on the skip list too). That residual is already public in the README. One clause in the PR's "What is read" would match it. It is not a new hole in `capture`.

The rest of the body matches the code and the entry. Claimed replay does not run the real tool. There is no shell, no `-z`, and no `--yolo` on the child. `HERMES_YOLO_MODE` is removed from the child. Redaction is Hermes's, and it does nothing when the user has switched it off. An unclaimed run is live. Capabilities match `plugin.yaml`: the two hooks, `tool_execution`, no tools, no required environment variables, plus `/cassette` and `hermes cassette`. The pin is 40 hex characters. `platforms: []` is the entry, and the body does not claim a Linux or macOS Hermes run. The known-limits paragraph is the careful one: inside Hermes, Windows 11 only; on Linux, unit tests only; macOS untested. The entry's first `known_issues` line says Linux is untested, which is stricter than that paragraph and stricter than the README. It does not claim a run that did not happen.

There are 254 `test_` methods under `tests/`. The body's "253 pass and 1 is skipped, on Windows and on Linux" matches the README's logged runs (on Windows the skipped method is the file-symlink test; on Linux it is the `.cmd` launcher test). I did not re-run the suite or the scanners. The semgrep, bandit, and gitleaks sentences are the logged scan, not a scan I repeated.
