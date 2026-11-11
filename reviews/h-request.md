# Block H review request for Grok

Grok, the plugin is frozen at commit `fa1f9fc` (tag `v0.1.0`). Since your block F recheck and your README review, the plugin code changed in one place only: `snapshot.py`. Everything else in the freeze commit is tests and docs. A finding that needs a code change means a new freeze, so please say how serious each finding is.

## 1. The `snapshot.py` change (the only code)

Before, `capture` compared a file's real path with the workspace path as given. If the workspace path itself runs through a link (a Windows junction, a symlinked folder, `/tmp` on macOS), every file's real path fell outside it, so every file was skipped as "link points outside the workspace" and the cassette got no snapshot. Now a file is also accepted when its real path is under the workspace's real path.

Questions:
1. Can a link inside the workspace now lead out of it and still be read? The intent is no.
2. Is the fallback `except Exception: real_ws_key = ws_key` safe? It only falls back to the old, stricter check.
3. Is there a case where the workspace's real path is broader than the user meant, for example a workspace that is a link to a drive root?

The diff, `0fc976c..fa1f9fc`:

```diff
diff --git a/snapshot.py b/snapshot.py
index 5df5f7b..4566ec8 100644
--- a/snapshot.py
+++ b/snapshot.py
@@ -41,6 +41,8 @@ _SECRET_GLOBS = (
     "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
     "credentials", "credentials.*", "*.env",
     "secrets.json", "secrets.yaml", "secrets.yml", "secrets.toml",
+    # Login stores (catalog rule 11 names Hermes's auth.json).
+    "auth.json", "token.json", "tokens.json",
 )
 
 _CONTENT_CLAMP = 500  # Hermes clamps match content to 500 characters
@@ -120,6 +122,13 @@ def capture(
     if not cassette.workspace:
         return [("*", "cassette has no workspace")]
     ws_key = normalize_path(cassette.workspace, cwd)
+    # The workspace's own path may run through a link (a Windows junction,
+    # macOS's /tmp). A file's real path is then compared with the workspace's
+    # real path too, or every file would look like a link leading out.
+    try:
+        real_ws_key = normalize_path(os.path.realpath(display_path(cassette.workspace, cwd)), cwd)
+    except Exception:
+        real_ws_key = ws_key
     used = sum(len(e.get("text", "").encode("utf-8")) for e in cassette.snapshot.values())
     for raw in raw_paths:
         try:
@@ -136,7 +145,7 @@ def capture(
                 continue
             disk = display_path(raw, cwd)
             real_key = normalize_path(os.path.realpath(disk), cwd)
-            if not is_within(real_key, ws_key):
+            if not (is_within(real_key, ws_key) or is_within(real_key, real_ws_key)):
                 notes.append((raw, "link points outside the workspace"))
                 continue
             st = os.stat(disk)
```

The two new tests are `test_directory_link_escape` and `test_workspace_reached_through_a_link` in `tests/test_snapshot.py`. The second fails against the old code.

**Not reproduced live.** In two live recordings started from a junction, Hermes had already resolved the path, so the plugin never saw a linked workspace. The fix is covered by unit tests only.

## 2. Tests that now run on Windows only

Five tests assert Windows path rules: the `.cmd` launcher test, two `safe_join` cases (a drive path and a network path) and two path spellings (the Git Bash form, and upper case with backslashes). Under Linux they failed, so they now run on Windows only. Question: on Linux or macOS, is any of those five a real gap in the plugin and not just in the test? For example, `safe_join` given `C:\x` on Linux joins it as an ordinary file name inside the data folder.

## 3. The pull request text

`docs/pr-description.md` is the text for the pull request to `NousResearch/hermes-agent`. Its disclosures are taken from `docs/catalog-entry.yaml`. Question: does it say anything the code does not do, or leave out something rule 13 needs?

## Not in scope

The replay path, the claim, the ledger and the CLI checks did not change since your recheck.
