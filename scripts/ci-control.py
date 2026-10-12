"""Prove in CI that the installed console script can pass, fail and refuse.

Starts the test fakes, runs `acceptance-gate run` as a subprocess three ways,
and exits non-zero unless the exit codes are exactly 0, 1 and 3.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.fakes import AiSdkUiHandler, serve, table  # noqa: E402

GOOD = {"What is this site about?": "This site lists the lab's events and projects."}
BAD = {"What is this site about?": "This site is guaranteed to rank #1."}


def suite(directory: Path, url: str) -> Path:
    path = directory / "suite.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "name": "control",
                "target": {
                    "adapter": "ai-sdk-ui",
                    "base_url": url,
                    "options": {"csrf": {}},
                    "retries": 1,
                    "retry_wait_seconds": 0,
                },
                "defaults": {"runs": 3, "prohibited_claims": ["guaranteed"]},
                "cases": [
                    {
                        "id": "about",
                        "ask": "What is this site about?",
                        "expect": "answer",
                        "must_include_any": ["events"],
                    }
                ],
            }
        )
    )
    return path


def gate(path: Path) -> int:
    proc = subprocess.run(
        ["acceptance-gate", "run", str(path), "-q"], capture_output=True, text=True
    )
    print(f"  exit {proc.returncode}: {proc.stdout.strip() or proc.stderr.strip()}")
    return proc.returncode


def main() -> int:
    got: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        with serve(AiSdkUiHandler, table(GOOD)) as (url, _, _s):
            got["good"] = gate(suite(d, url))
        with serve(AiSdkUiHandler, table(BAD)) as (url, _, _s):
            got["bad"] = gate(suite(d, url))
        got["down"] = gate(suite(d, "http://127.0.0.1:9"))
    want = {"good": 0, "bad": 1, "down": 3}
    if got != want:
        print(f"ci-control: expected {want}, got {got}", file=sys.stderr)
        return 1
    print("ci-control: the gate passes, fails and refuses as documented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
