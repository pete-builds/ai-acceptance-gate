"""Plain string and regex checks on one reply.

Deterministic on purpose: the same reply gets the same verdict every time, so
a flip between runs is the model's, never the grader's. A pass is a floor, not
proof. The checks catch a missing fact and the wrong claims someone thought to
name; they cannot catch an invented claim nobody listed.
"""

from __future__ import annotations

import re

from acceptance_gate.adapters.base import Reply
from acceptance_gate.spec import Case, Defaults

# Curly quotes would make "can't" miss "can’t". Straighten both sides.
_STRAIGHTEN = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


def normalize(text: str) -> str:
    return text.translate(_STRAIGHTEN)


def grade(case: Case, defaults: Defaults, reply: Reply) -> list[str]:
    """Return why this reply fails. An empty list is a pass."""
    text = normalize(reply.text).strip()
    if not text:
        return ["empty reply"]
    lowered = text.lower()
    problems: list[str] = []

    wanted = case.must_include_any
    if wanted and not any(normalize(s).lower() in lowered for s in wanted):
        problems.append(f"missing any of {case.must_include_any}")
    for s in case.must_include_all:
        if normalize(s).lower() not in lowered:
            problems.append(f"missing {s!r}")

    for s in defaults.prohibited_claims + case.prohibited_claims:
        if normalize(s).lower() in lowered:
            problems.append(f"prohibited claim {s!r}")
    for pattern in defaults.prohibited_patterns + case.prohibited_patterns:
        found = re.search(pattern, text, re.IGNORECASE)
        if found:
            problems.append(f"prohibited pattern /{pattern}/ matched {found.group(0)!r}")

    words = len(text.split())
    cap = case.max_words or (defaults.refusal_max_words if case.expect == "refuse" else None)
    if cap is not None and words > cap:
        problems.append(f"{words} words, over {cap}")

    # Half an answer can still contain the expected fact.
    if reply.finish_reason in ("length", "max_tokens"):
        problems.append(f"reply was cut off (finish={reply.finish_reason})")

    handed_off = any(normalize(m).lower() in lowered for m in defaults.handoff_markers)
    declined = any(re.search(p, text, re.IGNORECASE) for p in defaults.decline_patterns)
    if case.expect == "handoff" and not handed_off:
        problems.append("did not hand off to a person")
    if case.expect == "refuse" and not (declined or handed_off):
        problems.append("did not decline or hand off")
    return problems
