"""Arm, claim and run-ledger files (docs/PLAN.md, decision 9).

``hermes cassette run`` and the plugin's hooks run in different processes: the
command in the user's terminal, the hooks in the long-lived Hermes gateway.
They meet through small JSON files in the plugin data folder:

    armed/<id>.json     written by ``cassette run``: "replay cassette X for the
                        session whose first message is exactly this request"
    claimed/<id>.json   created by the one session that claimed the arm. The
                        hooks then rewrite it as the run's ledger: which
                        session and turn were bound, every tool call answered,
                        the final answer.

Claiming is the exclusive creation of ``claimed/<id>.json`` (``O_EXCL``), so
exactly one caller can win (SEC-8). It is deliberately not a rename: on
Windows, several threads renaming the same file to the same name can all be
told they succeeded, and the claim-race test caught exactly that. An arm
nobody claims expires after ARM_TTL_SECONDS. A claimed arm never expires.

Standard library only; nothing here imports Hermes. Every file is read through
``store.read_json_file`` with a size cap and a field-by-field check (SEC-6).
"""

from __future__ import annotations

import math
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .store import (
    FORMAT,
    MAX_FILE_BYTES,
    CassetteError,
    atomic_write_json,
    read_json_file,
    safe_join,
    validate_name,
)

ARMED_DIR = "armed"
CLAIMED_DIR = "claimed"
LOCK_FILE = "arm.lock"

#: How long an unclaimed arm stays claimable.
ARM_TTL_SECONDS = 600.0
#: An arm created this far in the future (clock change) is treated as expired.
_CLOCK_SKEW_SECONDS = 60.0
#: An arm holds one request string; anything bigger is not an arm.
ARM_MAX_BYTES = 1024 * 1024
MAX_REQUEST_CHARS = 200_000

CLAIMED = "claimed"
DONE = "done"

_ID_RE = re.compile(r"[0-9a-f]{32}")  # always used with fullmatch


def normalize_request(text: Any) -> str:
    """The form in which an arm's request and a turn's user message are
    compared: line endings unified, outer whitespace dropped. Not text -> ""."""
    if not isinstance(text, str):
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def validate_arm_id(arm_id: Any) -> str:
    if not isinstance(arm_id, str) or not _ID_RE.fullmatch(arm_id):
        raise CassetteError("invalid arm id")
    return arm_id


def arm_path(base_dir: os.PathLike | str, arm_id: str) -> Path:
    return safe_join(base_dir, ARMED_DIR, f"{validate_arm_id(arm_id)}.json")


def claimed_path(base_dir: os.PathLike | str, arm_id: str) -> Path:
    return safe_join(base_dir, CLAIMED_DIR, f"{validate_arm_id(arm_id)}.json")


# ---------------------------------------------------------------------------
# Reading (SEC-6)
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check_arm(data: Any, arm_id: Optional[str] = None) -> dict:
    """Return the arm's fields, checked one by one, or raise CassetteError."""
    if not isinstance(data, dict):
        raise CassetteError("arm file is not a JSON object")
    fmt = data.get("format")
    if type(fmt) is not int or fmt != FORMAT:
        raise CassetteError(f"unsupported arm format {fmt!r}")
    found_id = validate_arm_id(data.get("id"))
    if arm_id is not None and found_id != arm_id:
        raise CassetteError("arm file does not match its file name")
    if data.get("mode") != "replay":
        raise CassetteError("arm file has an unknown mode")
    cassette = validate_name(data.get("cassette"))
    request = data.get("request")
    if not isinstance(request, str) or not normalize_request(request):
        raise CassetteError("arm file has no request text")
    if len(request) > MAX_REQUEST_CHARS:
        raise CassetteError("arm request is too long")
    created = data.get("created")
    if not _is_number(created):
        raise CassetteError("arm file has no creation time")
    model = data.get("model", "")
    if not isinstance(model, str):
        raise CassetteError("arm field 'model' must be text")
    return {
        "format": FORMAT,
        "id": found_id,
        "mode": "replay",
        "cassette": cassette,
        "request": request,
        "created": float(created),
        "model": model,
    }


def is_expired(arm: dict, now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    age = now - arm["created"]
    return age > ARM_TTL_SECONDS or age < -_CLOCK_SKEW_SECONDS


def _arm_files(base_dir: os.PathLike | str) -> list:
    """``(arm id, path)`` for every well-named file in ``armed/``."""
    out = []
    try:
        with os.scandir(Path(base_dir) / ARMED_DIR) as entries:
            for entry in entries:
                stem, ext = os.path.splitext(entry.name)
                if ext == ".json" and _ID_RE.fullmatch(stem):
                    out.append((stem, entry.path))
    except OSError:
        return []
    return out


def waiting_arms(base_dir: os.PathLike | str, now: Optional[float] = None) -> list:
    """Valid, unexpired, unclaimed arms, oldest first. Never raises: an
    unreadable or malformed file is simply not an arm."""
    arms = []
    for arm_id, path in _arm_files(base_dir):
        try:
            arm = check_arm(read_json_file(path, max_bytes=ARM_MAX_BYTES), arm_id)
            if claimed_path(base_dir, arm_id).exists():
                continue  # claimed already; only its arm file was left behind
        except Exception:
            continue
        if not is_expired(arm, now):
            arms.append(arm)
    arms.sort(key=lambda a: (a["created"], a["id"]))
    return arms


def check_ledger(data: Any, arm_id: Optional[str] = None) -> dict:
    """Check a ``claimed/<id>.json`` file: the arm fields plus what the hooks
    added. Returns a clean dict or raises CassetteError."""
    ledger = check_arm(data, arm_id)
    state = data.get("state", "")
    if state not in ("", CLAIMED, DONE):
        raise CassetteError("ledger has an unknown state")
    ledger["state"] = state
    for key in ("session_id", "turn_id", "platform", "model_seen", "cwd", "final_answer"):
        value = data.get(key, "")
        if not isinstance(value, str):
            raise CassetteError(f"ledger field {key!r} must be text")
        ledger[key] = value
    for key in ("claimed_at", "finished_at"):
        value = data.get(key, 0)
        if not _is_number(value):
            raise CassetteError(f"ledger field {key!r} must be a number")
        ledger[key] = float(value)
    for key in ("refused_calls", "gateway_pid"):
        value = data.get(key, 0)
        if type(value) is not int:
            raise CassetteError(f"ledger field {key!r} must be an integer")
        ledger[key] = value
    events = data.get("events", [])
    if not isinstance(events, list):
        raise CassetteError("ledger field 'events' must be a list")
    clean = []
    for i, event in enumerate(events):
        if (
            not isinstance(event, list)
            or len(event) != 2
            or event[0] not in ("hit", "synthesized", "miss")
            or not isinstance(event[1], str)
        ):
            raise CassetteError(f"ledger event {i} is malformed")
        clean.append((event[0], event[1]))
    ledger["events"] = clean
    reasons = data.get("invalid_reasons", [])
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise CassetteError("ledger field 'invalid_reasons' must hold text")
    ledger["invalid_reasons"] = list(reasons)
    return ledger


def read_ledger(base_dir: os.PathLike | str, arm_id: str) -> dict:
    """The ledger of a claimed arm. Raises CassetteError if the arm was never
    claimed or the file is not a ledger."""
    path = claimed_path(base_dir, arm_id)
    return check_ledger(read_json_file(path, max_bytes=MAX_FILE_BYTES), arm_id)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


@contextmanager
def _creation_lock(base: Path, timeout: float = 5.0, stale: float = 30.0) -> Iterator[None]:
    """Serialise "check for a waiting arm, then write mine". Held for a few
    milliseconds; a lock file older than ``stale`` was left by a crash."""
    path = base / LOCK_FILE
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except (FileExistsError, PermissionError):
            try:
                if time.time() - os.stat(path).st_mtime > stale:
                    os.unlink(path)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise CassetteError("another cassette run is starting; try again in a moment") from None
            time.sleep(0.05)
    try:
        os.close(fd)
        yield
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def create_arm(
    base_dir: os.PathLike | str,
    cassette: str,
    request: str,
    *,
    model: str = "",
    now: Optional[float] = None,
) -> dict:
    """Write ``armed/<id>.json`` and return the arm.

    One run at a time: refuses while another unexpired arm is waiting to be
    claimed. Expired and malformed arm files are removed on the way.
    """
    validate_name(cassette)
    if not isinstance(request, str) or not normalize_request(request):
        raise CassetteError("the cassette has no request text to replay")
    if len(request) > MAX_REQUEST_CHARS:
        raise CassetteError("the cassette's request is too long to arm")
    base = Path(base_dir)
    try:
        (base / ARMED_DIR).mkdir(parents=True, exist_ok=True)
        (base / CLAIMED_DIR).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CassetteError(f"cannot create the arm folders: {exc}") from None
    now = time.time() if now is None else now
    with _creation_lock(base):
        waiting = waiting_arms(base, now)
        if waiting:
            age = max(0, int(now - waiting[0]["created"]))
            raise CassetteError(
                f"another cassette run ({waiting[0]['cassette']!r}, armed {age}s ago) is waiting "
                "for its session to start; let it finish or try again in a minute"
            )
        for _arm_id, path in _arm_files(base):  # only expired, malformed or claimed ones are left
            try:
                os.unlink(path)
            except OSError:
                pass
        arm = {
            "format": FORMAT,
            "id": uuid.uuid4().hex,
            "mode": "replay",
            "cassette": cassette,
            "request": request,
            "created": float(now),
            "model": model if isinstance(model, str) else "",
        }
        try:
            atomic_write_json(arm_path(base, arm["id"]), arm)
        except OSError as exc:
            raise CassetteError(f"cannot write the arm file: {exc}") from None
    return arm


def claim(base_dir: os.PathLike | str, arm_id: str) -> bool:
    """Claim an arm. True for exactly one caller, ever. Never raises.

    The claim is the exclusive creation of ``claimed/<id>.json``: the file
    system creates a new name for one caller only, and every other caller gets
    "already exists". The winner copies the arm into that file and removes
    ``armed/<id>.json``.

    If the arm has vanished by then, whoever armed the run has abandoned it,
    and the claim is void.
    """
    try:
        src = arm_path(base_dir, arm_id)
        dst = claimed_path(base_dir, arm_id)
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(src, "rb") as fh:
            payload = fh.read(ARM_MAX_BYTES + 1)
        if len(payload) > ARM_MAX_BYTES:
            return False
        fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
    except Exception:  # FileExistsError: another session holds this arm
        return False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
    except Exception:
        pass  # the claim is ours either way; the ledger write replaces the content
    try:
        os.unlink(src)
    except FileNotFoundError:
        try:
            os.unlink(dst)
        except OSError:
            pass
        return False
    except OSError:
        pass  # the arm file lingers, but nobody can claim it again
    return True


def write_ledger(base_dir: os.PathLike | str, arm_id: str, data: dict) -> bool:
    """Replace ``claimed/<id>.json`` with ``data``. False, writing nothing,
    if the claimed file is gone: the run was abandoned by whoever armed it,
    and a ledger must not come back from the dead."""
    path = claimed_path(base_dir, arm_id)
    if not path.is_file():
        return False
    atomic_write_json(path, data)
    return True


def discard(base_dir: os.PathLike | str, arm_id: str) -> None:
    """Remove an arm's files, claimed or not. For the process that armed it."""
    for path_for in (arm_path, claimed_path):
        try:
            os.unlink(path_for(base_dir, arm_id))
        except (OSError, CassetteError):
            pass
