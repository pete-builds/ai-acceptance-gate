"""Run every case the number of times it requires, round-robin, and decide.

A case passes only if every one of its runs passes. Runs go round-robin (every
case's run 1, then every case's run 2) so a drifting endpoint shows up as a
flip rather than as one case's bad streak.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from acceptance_gate.adapters import Adapter, Reply, TransportError
from acceptance_gate.grade import grade
from acceptance_gate.spec import Case, ConfigError, Suite

Verdict = Literal["pass", "fail", "error"]

EXIT_PASS, EXIT_FAIL, EXIT_CONFIG, EXIT_UNTESTED = 0, 1, 2, 3


@dataclass
class Attempt:
    run: int
    verdict: Verdict
    problems: list[str]
    reply: str = ""
    latency_ms: int = 0
    finish_reason: str | None = None
    tools: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    transport_retries: int = 0


@dataclass
class CaseResult:
    case: Case
    required_runs: int
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def verdict(self) -> Verdict:
        if len(self.attempts) < self.required_runs:
            return "error"
        if any(a.verdict == "fail" for a in self.attempts):
            return "fail"
        if any(a.verdict == "error" for a in self.attempts):
            return "error"
        return "pass"

    @property
    def flipped(self) -> bool:
        graded = {a.verdict for a in self.attempts if a.verdict != "error"}
        return len(graded) > 1


@dataclass
class GateResult:
    suite: Suite
    cases: list[CaseResult]
    started: float
    finished: float

    @property
    def verdict(self) -> Verdict:
        verdicts = {c.verdict for c in self.cases}
        if "fail" in verdicts:
            return "fail"
        if "error" in verdicts or not self.cases:
            return "error"
        return "pass"

    @property
    def exit_code(self) -> int:
        return {"pass": EXIT_PASS, "fail": EXIT_FAIL, "error": EXIT_UNTESTED}[self.verdict]


def select(suite: Suite, only: set[str] | None, tags: set[str] | None) -> list[Case]:
    chosen = [
        c
        for c in suite.cases
        if (not only or c.id in only) and (not tags or tags.intersection(c.tags))
    ]
    if only:
        unknown = sorted(only - {c.id for c in suite.cases})
        if unknown:
            raise ConfigError(f"--only names unknown case ids: {unknown}")
    if not chosen:
        # Asking nothing proves nothing: an empty selection must not exit 0.
        raise ConfigError("no cases selected")
    return chosen


def _attempt(
    adapter: Adapter,
    suite: Suite,
    case: Case,
    run: int,
    sleep: Callable[[float], None],
) -> Attempt:
    target = suite.target
    retries = 0
    while True:
        started = time.monotonic()
        try:
            reply: Reply = adapter.ask(case.history, case.ask)
        except TransportError as exc:
            if exc.retryable and retries < target.retries:
                retries += 1
                sleep(target.retry_wait_seconds * retries)
                continue
            return Attempt(
                run=run,
                verdict="error",
                problems=[str(exc)],
                latency_ms=int((time.monotonic() - started) * 1000),
                transport_retries=retries,
            )
        problems = grade(case, suite.defaults, reply)
        return Attempt(
            run=run,
            verdict="fail" if problems else "pass",
            problems=problems,
            reply=reply.text,
            latency_ms=int(reply.meta.pop("latency_ms", (time.monotonic() - started) * 1000)),
            finish_reason=reply.finish_reason,
            tools=reply.tools,
            meta=reply.meta,
            transport_retries=retries,
        )


def run_gate(
    suite: Suite,
    adapter: Adapter,
    cases: list[Case],
    min_runs: int = 1,
    on_attempt: Callable[[CaseResult, Attempt], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> GateResult:
    results = [CaseResult(case=c, required_runs=suite.required_runs(c, min_runs)) for c in cases]
    started = time.time()
    first = True
    for run in range(1, max(r.required_runs for r in results) + 1):
        for result in results:
            if run > result.required_runs:
                continue
            if not first and suite.target.pace_seconds:
                sleep(suite.target.pace_seconds)
            first = False
            attempt = _attempt(adapter, suite, result.case, run, sleep)
            result.attempts.append(attempt)
            if on_attempt is not None:
                on_attempt(result, attempt)
    return GateResult(suite=suite, cases=results, started=started, finished=time.time())
