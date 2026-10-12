"""acceptance-gate run | validate | adapters

Exit codes:
  0  every selected case passed every required run
  1  at least one case gave a wrong reply (a content failure)
  2  the suite or the command line is invalid; nothing was asked
  3  no content failure, but some run got no reply to grade (endpoint down,
     auth refused, stream broken), so the release is untested, not passed
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from acceptance_gate import __version__
from acceptance_gate.adapters import ADAPTERS, build
from acceptance_gate.report import summary, to_dict, write_json, write_junit
from acceptance_gate.runner import EXIT_CONFIG, Attempt, CaseResult, run_gate, select
from acceptance_gate.spec import ConfigError, load_suite


def _csv(value: str) -> set[str]:
    return {v.strip() for v in value.split(",") if v.strip()}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="acceptance-gate", description=__doc__.split("\n")[0])
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a suite against its endpoint")
    run.add_argument("suite", type=Path)
    run.add_argument("--json", type=Path, help="write the result as JSON")
    run.add_argument("--junit", type=Path, help="write the result as JUnit XML")
    run.add_argument("--base-url", help="override target.base_url")
    run.add_argument(
        "--min-runs",
        type=int,
        default=1,
        help="raise every case to at least N clean runs (never lowers a case's own runs)",
    )
    run.add_argument("--only", type=_csv, help="comma-separated case ids")
    run.add_argument("--tag", type=_csv, help="comma-separated tags; a case needs one")
    run.add_argument("-q", "--quiet", action="store_true", help="print only the final line")

    val = sub.add_parser("validate", help="check a suite file without calling anything")
    val.add_argument("suite", type=Path)

    sub.add_parser("adapters", help="list the adapters")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "adapters":
        for name, cls in sorted(ADAPTERS.items()):
            print(f"{name}: {(sys.modules[cls.__module__].__doc__ or '').splitlines()[0]}")
        return 0
    try:
        suite = load_suite(args.suite, getattr(args, "base_url", None))
        if args.command == "validate":
            # Building checks the adapter name and its options; it opens no connection.
            build(suite.target).close()
            print(f"{args.suite}: valid, {len(suite.cases)} cases, adapter {suite.target.adapter}")
            return 0
        if args.min_runs < 1:
            raise ConfigError("--min-runs must be at least 1")
        cases = select(suite, args.only, args.tag)
        adapter = build(suite.target)
    except ConfigError as exc:
        print(f"acceptance-gate: config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    def progress(result: CaseResult, attempt: Attempt) -> None:
        if not args.quiet:
            note = f": {'; '.join(attempt.problems)[:160]}" if attempt.problems else ""
            print(
                f"  run {attempt.run}/{result.required_runs} {result.case.id}: "
                f"{attempt.verdict} {attempt.latency_ms / 1000:.1f}s{note}",
                file=sys.stderr,
                flush=True,
            )

    try:
        result = run_gate(suite, adapter, cases, args.min_runs, progress)
    finally:
        adapter.close()

    data = to_dict(result, args.suite)
    if args.json:
        write_json(data, args.json)
    if args.junit:
        write_junit(data, args.junit)
    text = summary(data)
    print(text.splitlines()[-1] if args.quiet else text)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
