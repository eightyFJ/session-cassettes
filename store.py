"""Cassette file format, call keys, path normalisation and the on-disk store.

Standard library only.

- Names: a cassette name must match NAME_RE and not be a Windows reserved
  device name, and every file path must resolve inside the store's base
  directory.
- Reads: files are parsed with ``json.loads`` only, after a size cap, a strict
  UTF-8 decode and a check of ``format`` and every field type.
- Writes: a temporary file in the same folder, moved into place with
  ``os.replace``.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .verdict import VerdictError, validate_pin

FORMAT = 1

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(10)]
    + [f"LPT{i}" for i in range(10)]
)

#: Hard cap on any JSON file this plugin reads.
MAX_FILE_BYTES = 32 * 1024 * 1024

#: Longest argument text quoted back in a ``cassette miss`` error.
_MISS_ARGS_CHARS = 300

# Hermes's defaults for the file tools. An argument equal to its default is
# dropped from the key, so ``offset: 1`` and no offset give the same key.
_READ_DEFAULTS = {"offset": 1, "limit": 2000}
_SEARCH_DEFAULTS = {
    "target": "content",
    "path": ".",
    "file_glob": None,
    "limit": 50,
    "offset": 0,
    "output_mode": "content",
    "context": 0,
    "order": "discovery",
}
_SEARCH_TARGET_ALIASES = {"grep": "content", "find": "files"}


class CassetteError(Exception):
    """A clean, user-facing error: bad name, bad file, failed write."""


# ---------------------------------------------------------------------------
# Names and paths inside the store
# ---------------------------------------------------------------------------


def validate_name(name: Any) -> str:
    """Return ``name`` if it is a safe cassette name, else raise CassetteError."""
    if not isinstance(name, str) or not name:
        raise CassetteError("cassette name must be a non-empty string")
    if not NAME_RE.fullmatch(name):  # fullmatch: "$" would let a trailing "\n" through
        raise CassetteError(
            f"invalid cassette name {name!r}: use 1-64 letters, digits, '-' or '_', "
            "starting with a letter or digit"
        )
    if name.upper() in _RESERVED_NAMES:
        raise CassetteError(f"invalid cassette name {name!r}: reserved on Windows")
    return name


def safe_join(base_dir: os.PathLike | str, *parts: str) -> Path:
    """Join ``parts`` onto ``base_dir`` and refuse anything that resolves outside it."""
    base = Path(base_dir).resolve()
    candidate = base.joinpath(*parts).resolve()
    if candidate == base or not candidate.is_relative_to(base):
        raise CassetteError(f"path escapes the plugin data folder: {'/'.join(parts)!r}")
    return candidate


# ---------------------------------------------------------------------------
# Argument and path normalisation
# ---------------------------------------------------------------------------

_DRIVE_RE = re.compile(r"^([A-Za-z]):(.*)$")
_MSYS_RE = re.compile(r"^/([A-Za-z])(/.*)?$")


def _is_windows_style(path: str) -> bool:
    return bool(_DRIVE_RE.match(path)) or path.startswith("//")


def _windows_context(path: str, cwd: Optional[str]) -> bool:
    """Whether to treat paths as Windows paths: decided by the working
    directory when there is one, else by the path, else by the host."""
    if cwd:
        return _is_windows_style(str(cwd).strip().replace("\\", "/"))
    if _is_windows_style(path) or (path.startswith("/") and not _MSYS_RE.match(path)):
        return _is_windows_style(path)
    return os.name == "nt"


def _normalise(path: Any, cwd: Optional[str], fold: bool) -> str:
    s = str(path).strip().replace("\\", "/")
    windows = _windows_context(s, cwd)
    cwd_s = None if not cwd else _normalise(cwd, None, fold)
    if windows:
        m = _MSYS_RE.match(s)  # Git Bash style: /c/repo -> c:/repo
        if m:
            s = f"{m.group(1)}:{m.group(2) or '/'}"
    # A "~" path stays literal: home expansion depends on the machine.
    anchored = _is_windows_style(s) or s.startswith(("/", "~"))
    if cwd_s and not anchored:
        s = cwd_s.rstrip("/") + "/" + s if s not in ("", ".") else cwd_s
    m = _DRIVE_RE.match(s)
    if m:
        rest = m.group(2) or "/"
        if not rest.startswith("/"):
            rest = "/" + rest  # "C:foo" (drive-relative) is treated as "C:/foo"
        drive = m.group(1).lower() if fold else m.group(1)
        s = drive + ":" + posixpath.normpath(rest)
    elif s:  # POSIX normpath keeps exactly two leading slashes, so UNC survives
        s = posixpath.normpath(s)
    if len(s) > 1 and s.endswith("/") and not re.match(r"^[A-Za-z]:/$", s):
        s = s.rstrip("/")
    if fold and (windows or _is_windows_style(s)):
        s = s.lower()
    return s


def normalize_path(path: Any, cwd: Optional[str] = None) -> str:
    """Key form of a path: ``/`` separators, lower-case drive, relative paths
    resolved against ``cwd``, ``..`` collapsed, and the whole path lower-cased
    for Windows paths (the file system there ignores case)."""
    return _normalise(path, cwd, fold=True)


def display_path(path: Any, cwd: Optional[str] = None) -> str:
    """Like :func:`normalize_path` but keeps the original letter case."""
    return _normalise(path, cwd, fold=False)


def is_within(path_key: str, root_key: str) -> bool:
    """True if normalised ``path_key`` is ``root_key`` or below it."""
    if path_key == root_key:
        return True
    prefix = root_key if root_key.endswith("/") else root_key + "/"
    return path_key.startswith(prefix)


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_args(tool: str, args: Any, cwd: Optional[str]) -> dict:
    """The arguments a tool's result depends on, in canonical form.

    For ``read_file`` and ``search_files`` this mirrors the Hermes handlers:
    unknown keys are dropped (``query`` is not an alias of ``pattern``), a
    blank search path means ``"."``, and default-valued arguments are dropped.
    """
    if not isinstance(args, dict):
        args = {}
    if tool == "read_file":
        out = {"path": normalize_path(args.get("path", ""), cwd)}
        offset = max(1, _as_int(args.get("offset", 1), 1))
        limit = max(1, min(_as_int(args.get("limit", 2000), 2000), 2000))
        if offset != _READ_DEFAULTS["offset"]:
            out["offset"] = offset
        if limit != _READ_DEFAULTS["limit"]:
            out["limit"] = limit
        return out
    if tool == "search_files":
        raw_target = args.get("target", "content")
        target = _SEARCH_TARGET_ALIASES.get(raw_target, raw_target)
        path = args.get("path", ".")
        if path is None or (isinstance(path, str) and not path.strip()):
            path = "."
        pattern = args.get("pattern", "")
        out = {
            "pattern": pattern if isinstance(pattern, str) else str(pattern),
            "target": target,
            "path": normalize_path(path, cwd),
            "file_glob": args.get("file_glob") or None,
            "limit": max(1, _as_int(args.get("limit", 50), 50)),
            "offset": max(0, _as_int(args.get("offset", 0), 0)),
            "output_mode": args.get("output_mode", "content"),
            "context": max(0, _as_int(args.get("context", 0), 0)),
            "order": args.get("order", "discovery"),
        }
        defaults = dict(_SEARCH_DEFAULTS, path=normalize_path(".", cwd))
        return {k: v for k, v in out.items() if k == "pattern" or v != defaults.get(k)}
    if tool == "terminal":
        workdir = args.get("workdir") or args.get("cwd") or "."
        return {
            "command": args.get("command", ""),
            "cwd": normalize_path(workdir, cwd),
        }
    return {k: v for k, v in args.items() if v is not None}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def make_key(tool: str, args: Any, cwd: Optional[str]) -> str:
    """Cassette key: the tool name plus canonical JSON of the normalised args."""
    return f"{tool} {canonical_json(normalize_args(tool, args, cwd))}"


def miss_result(tool: Any, args: Any, reason: str = "") -> str:
    """The tool result returned on a replay miss. Never raises."""
    try:
        shown = canonical_json(args)
    except Exception:
        shown = "<unprintable args>"
    if len(shown) > _MISS_ARGS_CHARS:
        shown = shown[:_MISS_ARGS_CHARS] + "..."
    msg = f"cassette miss: {tool} {shown}"
    if reason:
        msg += f" ({reason})"
    return json.dumps({"error": msg}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# The cassette
# ---------------------------------------------------------------------------


@dataclass
class Call:
    seq: int
    tool: str
    key: str
    args: dict
    result: str


@dataclass
class Cassette:
    name: str
    request: str = ""
    final_answer: str = ""
    model: str = ""
    cwd: str = ""
    workspace: str = ""
    created: str = ""
    pins: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    unrecorded: list = field(default_factory=list)
    #: normalised absolute path -> {"path": display path, "text": str, "size": int}
    snapshot: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def __post_init__(self) -> None:
        validate_name(self.name)
        if not self.created:
            self.created = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # -- recording ---------------------------------------------------------

    def _next_seq(self) -> int:
        return len(self.calls) + len(self.unrecorded) + 1

    def add_call(self, tool: str, args: Any, result: str) -> Call:
        call = Call(
            seq=self._next_seq(),
            tool=tool,
            key=make_key(tool, args, self.cwd or None),
            args=dict(args) if isinstance(args, dict) else {},
            result=result,
        )
        self.calls.append(call)
        return call

    def add_unrecorded(self, tool: str) -> None:
        """Note a tool that ran live during recording (name only, no args or result)."""
        self.unrecorded.append({"seq": self._next_seq(), "tool": str(tool)})

    def add_warning(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)

    def add_pin(self, pin: str) -> str:
        try:
            text = validate_pin(pin)
        except VerdictError as exc:
            raise CassetteError(str(exc)) from None
        if text not in self.pins:
            self.pins.append(text)
        return text

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "format": FORMAT,
            "name": self.name,
            "created": self.created,
            "request": self.request,
            "final_answer": self.final_answer,
            "model": self.model,
            "cwd": self.cwd,
            "workspace": self.workspace,
            "pins": list(self.pins),
            "calls": [vars(c).copy() for c in self.calls],
            "unrecorded": [dict(u) for u in self.unrecorded],
            "snapshot": {k: dict(v) for k, v in self.snapshot.items()},
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Cassette":
        """Build a cassette from parsed JSON, checking every field."""
        if not isinstance(data, dict):
            raise CassetteError("cassette file is not a JSON object")
        fmt = data.get("format")
        if type(fmt) is not int or fmt != FORMAT:
            raise CassetteError(f"unsupported cassette format {fmt!r} (expected {FORMAT})")
        _check(data, "name", str, required=True)
        for key in ("request", "final_answer", "model", "cwd", "workspace", "created"):
            _check(data, key, str)
        for key in ("pins", "calls", "unrecorded", "warnings"):
            _check(data, key, list)
        _check(data, "snapshot", dict)
        pins = data.get("pins", [])
        warnings = data.get("warnings", [])
        if not all(isinstance(p, str) for p in pins):
            raise CassetteError("cassette field 'pins' must hold strings")
        if not all(isinstance(w, str) for w in warnings):
            raise CassetteError("cassette field 'warnings' must hold strings")
        calls = []
        for i, c in enumerate(data.get("calls", [])):
            if not isinstance(c, dict):
                raise CassetteError(f"cassette call {i} is not an object")
            for key, typ in (("tool", str), ("key", str), ("result", str), ("args", dict)):
                if not isinstance(c.get(key), typ):
                    raise CassetteError(f"cassette call {i}: field {key!r} must be {typ.__name__}")
            seq = c.get("seq", i + 1)
            if type(seq) is not int:
                raise CassetteError(f"cassette call {i}: field 'seq' must be int")
            calls.append(Call(seq=seq, tool=c["tool"], key=c["key"], args=c["args"], result=c["result"]))
        unrecorded = []
        for i, u in enumerate(data.get("unrecorded", [])):
            if not isinstance(u, dict) or not isinstance(u.get("tool"), str):
                raise CassetteError(f"cassette unrecorded entry {i} is malformed")
            seq = u.get("seq", 0)
            if type(seq) is not int:
                raise CassetteError(f"cassette unrecorded entry {i}: 'seq' must be int")
            unrecorded.append({"seq": seq, "tool": u["tool"]})
        snapshot = {}
        for k, v in data.get("snapshot", {}).items():
            if (
                not isinstance(v, dict)
                or not isinstance(v.get("path"), str)
                or not isinstance(v.get("text"), str)
                or type(v.get("size", 0)) is not int
            ):
                raise CassetteError(f"cassette snapshot entry {k!r} is malformed")
            snapshot[k] = {"path": v["path"], "text": v["text"], "size": v.get("size", 0)}
        try:
            return cls(
                name=data["name"],
                request=data.get("request", ""),
                final_answer=data.get("final_answer", ""),
                model=data.get("model", ""),
                cwd=data.get("cwd", ""),
                workspace=data.get("workspace", ""),
                created=data.get("created", ""),
                pins=list(pins),
                calls=calls,
                unrecorded=unrecorded,
                snapshot=snapshot,
                warnings=list(warnings),
            )
        except CassetteError as exc:
            raise CassetteError(f"bad cassette: {exc}") from None


def _check(data: dict, key: str, typ: type, required: bool = False) -> None:
    if key not in data:
        if required:
            raise CassetteError(f"cassette is missing field {key!r}")
        return
    if not isinstance(data[key], typ) or (typ is int and isinstance(data[key], bool)):
        raise CassetteError(f"cassette field {key!r} must be {typ.__name__}")


# ---------------------------------------------------------------------------
# Safe file I/O
# ---------------------------------------------------------------------------


def read_json_file(path: os.PathLike | str, max_bytes: Optional[int] = None) -> Any:
    """Read one JSON file with a size cap and a strict UTF-8 decode.

    Raises CassetteError for every failure: missing, too big, not UTF-8, not
    JSON.
    """
    max_bytes = MAX_FILE_BYTES if max_bytes is None else max_bytes
    p = Path(path)
    try:
        with open(p, "rb") as fh:
            raw = fh.read(max_bytes + 1)
    except FileNotFoundError:
        raise CassetteError(f"file not found: {p.name}") from None
    except OSError as exc:
        raise CassetteError(f"cannot read {p.name}: {exc.strerror or exc}") from None
    if len(raw) > max_bytes:
        raise CassetteError(f"{p.name} is larger than {max_bytes} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CassetteError(f"{p.name} is not valid UTF-8") from None
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise CassetteError(f"{p.name} is not valid JSON: {exc}") from None


def atomic_write_json(path: os.PathLike | str, data: Any) -> None:
    """Write JSON to a temp file in the same folder, then ``os.replace`` it in.

    An interruption before the replace leaves any old file untouched and
    removes the temp file.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, str(p))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _replace_with_retry(src: str, dst: str, attempts: int = 10) -> None:
    """``os.replace``, retried: on Windows it raises PermissionError while
    another process holds the target open."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.02 * (i + 1))


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class CassetteStore:
    """One JSON file per cassette under ``<base_dir>/cassettes/``."""

    def __init__(self, base_dir: os.PathLike | str):
        self.base_dir = Path(base_dir)

    @property
    def cassettes_dir(self) -> Path:
        return self.base_dir / "cassettes"

    def path_for(self, name: str) -> Path:
        validate_name(name)
        return safe_join(self.base_dir, "cassettes", f"{name}.json")

    def exists(self, name: str) -> bool:
        return self.path_for(name).is_file()

    def save(self, cassette: Cassette) -> Path:
        path = self.path_for(cassette.name)
        try:
            atomic_write_json(path, cassette.to_dict())
        except OSError as exc:
            raise CassetteError(f"cannot save cassette {cassette.name!r}: {exc}") from None
        return path

    def load(self, name: str) -> Cassette:
        data = read_json_file(self.path_for(name))
        cassette = Cassette.from_dict(data)
        if cassette.name != name:
            raise CassetteError(
                f"cassette file {name}.json names itself {cassette.name!r}; refusing to load it"
            )
        return cassette

    def names(self) -> list:
        """Names of the cassette files present, sorted. Invalid names are skipped."""
        if not self.cassettes_dir.is_dir():
            return []
        out = []
        for entry in self.cassettes_dir.iterdir():
            if entry.suffix != ".json" or not entry.is_file():
                continue
            try:
                out.append(validate_name(entry.stem))
            except CassetteError:
                continue
        return sorted(out)


# ---------------------------------------------------------------------------
# Replay lookup
# ---------------------------------------------------------------------------

HIT = "hit"
SYNTH = "synthesized"
MISS = "miss"


class ReplayIndex:
    """Answers tool calls for one replay binding.

    A key recorded several times is answered in recorded order, then the last
    result repeats. The cursor lives here, not in the cassette, so two runs
    never share it.

    ``synthesize(cassette, tool, args)`` answers a call that was not recorded,
    or returns None (``snapshot.synthesize``).

    ``answer`` returns ``(kind, result)`` where kind is HIT, SYNTH or MISS.
    It never raises and never runs a tool.
    """

    def __init__(self, cassette: Cassette, synthesize: Callable[[Cassette, str, Any], Optional[str]]):
        self.cassette = cassette
        self._by_key: dict = {}
        for call in cassette.calls:
            self._by_key.setdefault(call.key, []).append(call.result)
        self._cursor: dict = {}
        self._synthesize = synthesize

    def answer(self, tool: Any, args: Any) -> tuple:
        try:
            tool_name = tool if isinstance(tool, str) else str(tool)
            cwd = self.cassette.cwd or None
            key = make_key(tool_name, args, cwd)
            results = self._by_key.get(key)
            if results:
                i = self._cursor.get(key, 0)
                self._cursor[key] = i + 1
                return HIT, results[min(i, len(results) - 1)]
            synth = self._synthesize(self.cassette, tool_name, args)
            if synth is not None:
                return SYNTH, synth
            return MISS, miss_result(tool_name, args)
        except Exception as exc:  # fail closed
            return MISS, miss_result(tool, args, f"internal error: {type(exc).__name__}")
