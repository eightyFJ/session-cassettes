"""Pinned-fact matching, the verdict, and the plain-text result table.

- INVALID overrides everything: the run can't be trusted to have been a replay
  (arm never claimed, arm left over, answer doesn't match the child's output,
  no final answer). An INVALID run is never PASS.
- FAIL: a pin is missing from the final answer.
- DIVERGED: every pin matched, but one or more tool calls missed the cassette.
- PASS: every pin matched and there were no misses. Answers synthesized from
  the snapshot do not count as misses.

The "path" line describes the calls. It never sets the result.

Standard library only. No ANSI colour when NO_COLOR is set or the stream is
not a terminal.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional, Sequence

PASS = "PASS"
DIVERGED = "DIVERGED"
FAIL = "FAIL"
INVALID = "INVALID"

MAX_PIN_CHARS = 200
_HIT, _SYNTH, _MISS = "hit", "synthesized", "miss"


class VerdictError(ValueError):
    """Bad pin text, or a verdict asked for with no pins."""


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


def validate_pin(pin: Any) -> str:
    """Return the pin, stripped, or raise VerdictError.

    A pin starting with ``re:`` is a regular expression (matched without case);
    anything else is a plain substring.
    """
    if not isinstance(pin, str):
        raise VerdictError("a pin must be text")
    text = pin.strip()
    if not text or text == "re:":
        raise VerdictError("a pin cannot be empty")
    if len(text) > MAX_PIN_CHARS:
        raise VerdictError(f"a pin can be at most {MAX_PIN_CHARS} characters")
    if text.startswith("re:"):
        try:
            re.compile(text[3:], re.IGNORECASE)
        except re.error as exc:
            raise VerdictError(f"invalid regex pin {text!r}: {exc}") from None
    return text


def _fold(text: str) -> str:
    return text.replace("\\", "/").casefold()


def match_pin(pin: str, answer: str) -> bool:
    """Case-insensitive; ``\\`` and ``/`` count as the same character in the
    answer (and in plain pins). Regex pins see the answer with ``\\`` turned
    into ``/``; the pattern itself is used as typed."""
    if not isinstance(answer, str) or not isinstance(pin, str):
        return False
    if pin.startswith("re:"):
        try:
            return re.search(pin[3:], answer.replace("\\", "/"), re.IGNORECASE) is not None
        except re.error:
            return False
    return _fold(pin) in _fold(answer)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


@dataclass
class PinResult:
    pin: str
    matched: bool


@dataclass
class Verdict:
    result: str
    pins: list = field(default_factory=list)
    hits: int = 0
    synthesized: int = 0
    misses: int = 0
    miss_details: list = field(default_factory=list)
    invalid_reasons: list = field(default_factory=list)
    path: str = ""
    notes: list = field(default_factory=list)

    @property
    def pins_matched(self) -> int:
        return sum(1 for p in self.pins if p.matched)

    def to_dict(self) -> dict:
        return asdict(self)


def _is_subsequence(needle: Sequence, haystack: Sequence) -> bool:
    it = iter(haystack)
    return all(any(x == y for y in it) for x in needle)


def path_summary(recorded_keys: Sequence[str], events: Sequence[tuple]) -> str:
    """One line on the calls: counts, plus how the hit order compares with the
    recording. ``events`` are ``(kind, key)`` pairs in call order."""
    hits = [k for kind, k in events if kind == _HIT]
    n_hit = len(hits)
    n_syn = sum(1 for kind, _ in events if kind == _SYNTH)
    n_miss = sum(1 for kind, _ in events if kind == _MISS)
    total = len(events)
    head = (f"{total} call{'s' if total != 1 else ''}: {n_hit} hit, "
            f"{n_syn} synthesized, {n_miss} miss")
    recorded = list(recorded_keys)
    if not total:
        order = "no tool calls"
    elif not hits:
        order = "no recorded call reused"
    elif hits == recorded:
        order = "same order as recording"
    elif _is_subsequence(hits, recorded):
        order = f"subset of recording, in order ({len(recorded)} recorded)"
    else:
        order = f"order differs from recording ({len(recorded)} recorded)"
    return f"{head}; {order}"


def compute_verdict(
    pins: Iterable[str],
    answer: Optional[str],
    events: Sequence[tuple] = (),
    recorded_keys: Sequence[str] = (),
    invalid_reasons: Iterable[str] = (),
) -> Verdict:
    """Grade one replay run.

    ``events``: ``(kind, key)`` per tool call answered for the bound turn, kind
    being ``"hit"``, ``"synthesized"`` or ``"miss"``.
    ``invalid_reasons``: anything that means the run may not have been a
    replay. Any reason makes the result INVALID.
    """
    pin_list = list(pins)
    if not pin_list:
        raise VerdictError("the cassette has no pins; add one with 'hermes cassette pin'")
    reasons = [str(r) for r in invalid_reasons if r]
    if answer is None or (isinstance(answer, str) and not answer.strip()):
        reasons.append("no final answer was captured")
        answer = ""
    pin_results = [PinResult(p, match_pin(p, answer)) for p in pin_list]
    events = list(events)
    misses = [k for kind, k in events if kind == _MISS]
    v = Verdict(
        result=PASS,
        pins=pin_results,
        hits=sum(1 for kind, _ in events if kind == _HIT),
        synthesized=sum(1 for kind, _ in events if kind == _SYNTH),
        misses=len(misses),
        miss_details=misses,
        invalid_reasons=reasons,
        path=path_summary(recorded_keys, events),
    )
    if reasons:
        v.result = INVALID
    elif not all(p.matched for p in pin_results):
        v.result = FAIL
        if misses:
            v.notes.append(f"also {len(misses)} cassette miss{'es' if len(misses) != 1 else ''}")
    elif misses:
        v.result = DIVERGED
    return v


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_COLOURS = {PASS: "32", DIVERGED: "33", FAIL: "31", INVALID: "35"}


def use_colour(stream: Any = None) -> bool:
    if "NO_COLOR" in os.environ:
        return False
    stream = stream if stream is not None else sys.stdout
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def paint(text: str, result: str, colour: bool) -> str:
    if not colour or result not in _COLOURS:
        return text
    return f"\x1b[1;{_COLOURS[result]}m{text}\x1b[0m"


def render_verdict(v: Verdict, title: str = "", colour: Optional[bool] = None) -> str:
    """Multi-line block for one run. ASCII only, so it prints on any console."""
    colour = use_colour() if colour is None else colour
    lines = []
    if title:
        lines.append(title)
    lines.append(f"  result  {paint(v.result, v.result, colour)}"
                 + (f"  ({'; '.join(v.notes)})" if v.notes else ""))
    lines.append(f"  pins    {v.pins_matched}/{len(v.pins)}")
    for p in v.pins:
        lines.append(f"          [{'ok' if p.matched else '--'}] {p.pin}")
    lines.append(f"  path    {v.path}")
    for k in v.miss_details[:5]:
        lines.append(f"          miss: {k[:120]}")
    if len(v.miss_details) > 5:
        lines.append(f"          ... and {len(v.miss_details) - 5} more")
    for r in v.invalid_reasons:
        lines.append(f"  invalid {r}")
    return "\n".join(lines)


def render_table(rows: Sequence[dict], colour: Optional[bool] = None) -> str:
    """Summary table. Each row: ``name``, ``model``, ``result``, ``pins``
    (e.g. "2/2"), ``path``. Missing values print as "-"."""
    colour = use_colour() if colour is None else colour
    headers = ["CASSETTE", "MODEL", "RESULT", "PINS", "PATH"]
    keys = ["name", "model", "result", "pins", "path"]
    cells = [[str(r.get(k) or "-") for k in keys] for r in rows]
    widths = [max([len(h)] + [len(c[i]) for c in cells]) for i, h in enumerate(headers)]
    widths[-1] = len(headers[-1])  # last column is not padded

    def fmt(values: list, result: str = "") -> str:
        out = []
        for i, val in enumerate(values):
            padded = val if i == len(values) - 1 else val.ljust(widths[i])
            out.append(paint(padded, result, colour) if i == 2 and result else padded)
        return "  ".join(out).rstrip()

    lines = [fmt(headers)]
    lines += [fmt(c, c[2]) for c in cells]
    return "\n".join(lines)
