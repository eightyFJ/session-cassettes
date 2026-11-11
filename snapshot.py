"""Workspace snapshot: captured at record time, searched at replay time.

At record time the cassette also stores the text of every workspace file that a
``search_files`` result named or a ``read_file`` call read (docs/PLAN.md,
decision of 2026-11-10, "snapshot + search for replay misses"). At replay time
a ``read_file`` or ``search_files`` call with no exact recording is answered by
a pure-Python slice or search over that snapshot, in the JSON shape Hermes's
own tools return (``tools/file_operations_common.py``: ``ReadResult``,
``SearchResult.to_dict(densify=True)``). No real tool runs.

A call that reaches outside the snapshot returns ``None``; the caller turns
that into a ``cassette miss``.

Limits of the imitation, stated plainly:
- Python ``re`` stands in for ripgrep's regex engine. Simple patterns behave
  the same; exotic syntax can differ.
- Only snapshot files are searched, so a synthesized search can report fewer
  matches than a live search of the whole folder would.
- ``order: "modified"`` is ignored (the snapshot has no modification times).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import stat
from typing import Any, Callable, Iterable, Optional

from .store import Cassette, display_path, is_within, normalize_path

#: Per-file and per-cassette caps on snapshot text.
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024

#: Names never captured, even inside the workspace. Hidden files and folders
#: (any path segment starting with ".") are skipped too, as ripgrep does.
_SECRET_GLOBS = (
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.kdbx", "*.keystore",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "credentials", "credentials.*", "*.env",
    "secrets.json", "secrets.yaml", "secrets.yml", "secrets.toml",
)

_CONTENT_CLAMP = 500  # Hermes clamps match content to 500 characters
_DENSIFY_MIN_MATCHES = 5
_MATCHES_FORMAT = (
    "path-grouped: each file path on its own line, followed by "
    "indented '<line>: <content>' rows for matches in that file"
)


# ---------------------------------------------------------------------------
# Record side
# ---------------------------------------------------------------------------


def named_paths(tool: str, args: Any, result: Any) -> list:
    """Raw path strings a tool call names: the ``read_file`` path, or every
    file in a ``search_files`` result. Never raises."""
    try:
        if tool not in ("read_file", "search_files") or not isinstance(result, str):
            return []
        try:
            parsed = json.loads(result)
        except ValueError:
            return []
        if not isinstance(parsed, dict) or parsed.get("error"):
            return []
        if tool == "read_file":
            path = args.get("path") if isinstance(args, dict) else None
            return [path] if isinstance(path, str) and path.strip() else []
        out: list = []
        for m in parsed.get("matches") or []:
            if isinstance(m, dict) and isinstance(m.get("path"), str):
                out.append(m["path"])
        text = parsed.get("matches_text")
        if isinstance(text, str):
            out.extend(ln for ln in text.split("\n") if ln and not ln.startswith(" "))
        out.extend(f for f in parsed.get("files") or [] if isinstance(f, str))
        counts = parsed.get("counts")
        if isinstance(counts, dict):
            out.extend(k for k in counts if isinstance(k, str))
        seen: set = set()
        return [p for p in out if not (p in seen or seen.add(p))]
    except Exception:
        return []


def _skip_reason(rel: str) -> Optional[str]:
    parts = [p for p in rel.split("/") if p]
    if any(p.startswith(".") for p in parts):
        return "hidden path"
    base = parts[-1] if parts else ""
    if any(fnmatch.fnmatchcase(base.lower(), g) for g in _SECRET_GLOBS):
        return "secret-looking file name"
    return None


def capture(
    cassette: Cassette,
    raw_paths: Iterable[str],
    *,
    redact: Optional[Callable[[str, str], str]] = None,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> list:
    """Add workspace files to ``cassette.snapshot``. Returns ``(path, reason)``
    notes for files it skipped. Never raises.

    ``redact(text, path)`` should be Hermes's own secret redaction: a live
    ``read_file`` result is redacted by Hermes, and the snapshot is read
    straight from disk, so without it the cassette could hold secrets the
    live session never showed the model. If ``redact`` raises, the file is
    skipped rather than stored unredacted.
    """
    notes: list = []
    cwd = cassette.cwd or None
    if not cassette.workspace:
        return [("*", "cassette has no workspace")]
    ws_key = normalize_path(cassette.workspace, cwd)
    used = sum(len(e.get("text", "").encode("utf-8")) for e in cassette.snapshot.values())
    for raw in raw_paths:
        try:
            key = normalize_path(raw, cwd)
            if key in cassette.snapshot:
                continue
            if not is_within(key, ws_key) or key == ws_key:
                notes.append((raw, "outside the workspace"))
                continue
            rel = key[len(ws_key):].lstrip("/")
            reason = _skip_reason(rel)
            if reason:
                notes.append((raw, reason))
                continue
            disk = display_path(raw, cwd)
            real_key = normalize_path(os.path.realpath(disk), cwd)
            if not is_within(real_key, ws_key):
                notes.append((raw, "link points outside the workspace"))
                continue
            st = os.stat(disk)
            if not stat.S_ISREG(st.st_mode):
                notes.append((raw, "not a regular file"))
                continue
            if st.st_size > max_file_bytes:
                notes.append((raw, "file too large"))
                continue
            with open(disk, "rb") as fh:
                data = fh.read(max_file_bytes + 1)
            if len(data) > max_file_bytes:
                notes.append((raw, "file too large"))
                continue
            if b"\x00" in data[:8192]:
                notes.append((raw, "binary file"))
                continue
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                notes.append((raw, "not UTF-8 text"))
                continue
            if redact is not None:
                try:
                    text = redact(text, disk)
                except Exception:
                    notes.append((raw, "redaction failed"))
                    continue
                if not isinstance(text, str):
                    notes.append((raw, "redaction failed"))
                    continue
            size = len(text.encode("utf-8"))
            if used + size > max_total_bytes:
                notes.append((raw, "snapshot size cap reached"))
                continue
            used += size
            cassette.snapshot[key] = {"path": disk, "text": text, "size": len(data)}
        except Exception as exc:
            notes.append((str(raw), f"unreadable: {type(exc).__name__}"))
    return notes


# ---------------------------------------------------------------------------
# Replay side
# ---------------------------------------------------------------------------


def synthesize(cassette: Cassette, tool: str, args: Any) -> Optional[str]:
    """Answer ``read_file`` / ``search_files`` from the snapshot, or None."""
    if not cassette.snapshot or not cassette.workspace:
        return None
    if not isinstance(args, dict):
        args = {}
    if tool == "read_file":
        return _synth_read(cassette, args)
    if tool == "search_files":
        return _synth_search(cassette, args)
    return None


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _lines(text: str) -> list:
    if not text:
        return []
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return [p[:-1] if p.endswith("\r") else p for p in parts]


def _synth_read(cassette: Cassette, args: dict) -> Optional[str]:
    path = args.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    entry = cassette.snapshot.get(normalize_path(path, cassette.cwd or None))
    if entry is None:
        return None
    offset = max(1, _int(args.get("offset", 1), 1))
    limit = max(1, min(_int(args.get("limit", 2000), 2000), 2000))
    lines = _lines(entry["text"])
    total = len(lines)
    page = lines[offset - 1: offset - 1 + limit]
    end = offset - 1 + len(page)
    result: dict = {
        "content": "\n".join(f"{offset + i}|{ln}" for i, ln in enumerate(page)),
        "total_lines": total,
        "file_size": entry.get("size", 0),
        "truncated": end < total,
    }
    if end < total:
        result["hint"] = (
            f"Use offset={end + 1} to continue reading (showing {offset}-{end} of {total} lines)"
        )
    elif offset > total and total:
        result["hint"] = f"offset {offset} is past the end of the file ({total} lines)"
    return json.dumps(result, ensure_ascii=False)


def _expand_braces(pattern: str) -> list:
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    head, tail = pattern[: m.start()], pattern[m.end():]
    out: list = []
    for alt in m.group(1).split(","):
        out.extend(_expand_braces(head + alt + tail))
    return out


def _glob_match(glob: str, rel: str) -> bool:
    """ripgrep-style ``--glob``: without a ``/`` it matches the file name at
    any depth; with one, the path relative to the search root."""
    base = rel.rsplit("/", 1)[-1]
    for g in _expand_braces(glob):
        g = g[3:] if g.startswith("**/") else g
        target = rel if "/" in g else base
        if fnmatch.fnmatchcase(target, g):
            return True
    return False


def _scope(cassette: Cassette, raw_root: Any) -> Optional[list]:
    """Snapshot entries under the search root, as ``(shown path, key, entry,
    path relative to the root)``, sorted. None when the root is outside the workspace or the snapshot holds
    nothing under it (the cassette knows nothing there: a real miss)."""
    cwd = cassette.cwd or None
    if raw_root is None or (isinstance(raw_root, str) and not raw_root.strip()):
        raw_root = "."
    raw_root = str(raw_root)
    root_key = normalize_path(raw_root, cwd)
    ws_key = normalize_path(cassette.workspace, cwd)
    if not is_within(root_key, ws_key):
        return None
    shown = raw_root.strip().replace("\\", "/")
    if re.match(r"^[A-Za-z]:", shown) or shown.startswith("/"):
        shown_root = display_path(raw_root, cwd)
    else:
        shown_root = shown.rstrip("/") or "."
    if root_key in cassette.snapshot:  # the root is a single file
        return [(shown_root, root_key, cassette.snapshot[root_key], "")]
    out = []
    for key in sorted(cassette.snapshot):
        if not is_within(key, root_key) or key == root_key:
            continue
        rel_key = key[len(root_key):].lstrip("/")
        n = rel_key.count("/") + 1
        entry = cassette.snapshot[key]
        rel_shown = "/".join(entry["path"].split("/")[-n:])
        sep = "" if shown_root.endswith("/") else "/"
        out.append((shown_root + sep + rel_shown, key, entry, rel_shown))
    return out or None


def _synth_search(cassette: Cassette, args: dict) -> Optional[str]:
    raw_target = args.get("target", "content")
    target = {"grep": "content", "find": "files"}.get(raw_target, raw_target)
    pattern = args.get("pattern", "")  # Hermes ignores "query" and other unknown keys
    pattern = pattern if isinstance(pattern, str) else str(pattern)
    limit = max(1, _int(args.get("limit", 50), 50))
    offset = max(0, _int(args.get("offset", 0), 0))
    output_mode = args.get("output_mode", "content")
    context = max(0, _int(args.get("context", 0), 0))
    file_glob = args.get("file_glob") or None
    scope = _scope(cassette, args.get("path", "."))
    if scope is None:
        return None
    if target == "files":
        name_glob = pattern.split("/")[-1] if (pattern.startswith("**/") or "/" in pattern) else pattern
        if not name_glob.startswith("*"):
            name_glob = "*" + name_glob  # Hermes wraps bare names this way for rg
        files = [shown for shown, _k, _e, rel in scope
                 if fnmatch.fnmatchcase((rel or shown).rsplit("/", 1)[-1], name_glob)]
        return _dump({"total_count": len(files), "files": files[offset:offset + limit],
                      "truncated": len(files) > offset + limit})
    if target != "content":
        return _dump({"total_count": 0, "error": f"Invalid target {raw_target!r}"})
    flags = re.MULTILINE if "\\n" in pattern else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        return _dump({"total_count": 0, "error": f"Search failed: regex parse error: {exc}"})
    matches: list = []
    files: list = []
    counts: dict = {}
    for shown, _key, entry, rel in scope:
        if file_glob and not _glob_match(str(file_glob), rel or shown.rsplit("/", 1)[-1]):
            continue
        lines = _lines(entry["text"])
        hit_lines = [i for i, ln in enumerate(lines) if rx.search(ln)]
        if not hit_lines:
            continue
        files.append(shown)
        counts[shown] = len(hit_lines)
        wanted = sorted({j for i in hit_lines
                         for j in range(max(0, i - context), min(len(lines), i + context + 1))})
        matches.extend((shown, j + 1, lines[j][:_CONTENT_CLAMP]) for j in wanted)
    if output_mode == "files_only":
        return _dump({"total_count": len(files), "files": files[offset:offset + limit],
                      "truncated": False})
    if output_mode == "count":
        return _dump({"total_count": sum(counts.values()), "counts": counts, "truncated": False})
    total = len(matches)
    page = matches[offset:offset + limit]
    result: dict = {"total_count": total}
    if len(page) >= _DENSIFY_MIN_MATCHES:
        rows: list = []
        current = None
        for path, line_no, content in page:
            if path != current:
                rows.append(path)
                current = path
            rows.append(f"  {line_no}: {content.rstrip()}")
        result["matches_format"] = _MATCHES_FORMAT
        result["matches_text"] = "\n".join(rows)
    elif page:
        result["matches"] = [{"path": p, "line": n, "content": c} for p, n, c in page]
    if total > offset + limit:
        result["truncated"] = True
        result["total_count_is_lower_bound"] = True
        result["_hint"] = (f"Results truncated. Use offset={offset + limit} to see more, "
                           "or narrow with a more specific pattern or file_glob.")
    return json.dumps(result, ensure_ascii=False)


def _dump(result: dict) -> str:
    """Drop empty fields the way ``SearchResult.to_dict`` does."""
    out = {"total_count": result.get("total_count", 0)}
    for key in ("files", "counts"):
        if result.get(key):
            out[key] = result[key]
    if result.get("truncated"):
        out["truncated"] = True
        out["total_count_is_lower_bound"] = True
    if result.get("error"):
        out["error"] = result["error"]
    return json.dumps(out, ensure_ascii=False)
