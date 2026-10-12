"""Machine-readable results (JSON, JUnit XML) and the human summary."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from acceptance_gate import __version__
from acceptance_gate.runner import GateResult

SCHEMA = "ai-acceptance-gate/result/v1"


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _safe_url(url: str) -> str:
    """Drop any user:password@ so a result file never carries one."""
    parts = urlsplit(url)
    host = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    return urlunsplit(parts._replace(netloc=host))


def to_dict(result: GateResult, suite_path: Path) -> dict[str, Any]:
    cases = result.cases
    target = result.suite.target
    return {
        "schema": SCHEMA,
        "tool_version": __version__,
        "suite": result.suite.name,
        "suite_file": str(suite_path),
        "suite_sha256": hashlib.sha256(suite_path.read_bytes()).hexdigest(),
        # Option keys only: values can hold tokens.
        "target": {
            "adapter": target.adapter,
            "base_url": _safe_url(target.base_url),
            "option_keys": sorted(target.options),
        },
        "started_at": _iso(result.started),
        "finished_at": _iso(result.finished),
        "duration_ms": int((result.finished - result.started) * 1000),
        "verdict": result.verdict,
        "exit_code": result.exit_code,
        "totals": {
            "cases": len(cases),
            "passed": sum(c.verdict == "pass" for c in cases),
            "failed": sum(c.verdict == "fail" for c in cases),
            "errored": sum(c.verdict == "error" for c in cases),
            "attempts": sum(len(c.attempts) for c in cases),
        },
        "flipped": [c.case.id for c in cases if c.flipped],
        "cases": [
            {
                "id": c.case.id,
                "expect": c.case.expect,
                "tags": c.case.tags,
                "required_runs": c.required_runs,
                "verdict": c.verdict,
                "flipped": c.flipped,
                "ask": c.case.ask,
                "attempts": [
                    {
                        "run": a.run,
                        "verdict": a.verdict,
                        "problems": a.problems,
                        "reply": a.reply,
                        "latency_ms": a.latency_ms,
                        "finish_reason": a.finish_reason,
                        "tools": a.tools,
                        "transport_retries": a.transport_retries,
                        "meta": a.meta,
                    }
                    for a in c.attempts
                ],
            }
            for c in cases
        ],
    }


def write_json(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_junit(data: dict[str, Any], path: Path) -> None:
    totals = data["totals"]
    suite = ET.Element(
        "testsuite",
        name=data["suite"],
        tests=str(totals["cases"]),
        failures=str(totals["failed"]),
        errors=str(totals["errored"]),
        time=f"{data['duration_ms'] / 1000:.3f}",
        timestamp=data["started_at"],
    )
    for case in data["cases"]:
        seconds = sum(a["latency_ms"] for a in case["attempts"]) / 1000
        tc = ET.SubElement(
            suite,
            "testcase",
            classname=f"{data['suite']}.{case['expect']}",
            name=case["id"],
            time=f"{seconds:.3f}",
        )
        lines = [
            f"run {a['run']}: {a['verdict']}"
            + (f": {'; '.join(a['problems'])}" if a["problems"] else "")
            for a in case["attempts"]
        ]
        if case["verdict"] in ("fail", "error"):
            tag = "failure" if case["verdict"] == "fail" else "error"
            first = next((a for a in case["attempts"] if a["verdict"] != "pass"), None)
            message = "; ".join(first["problems"]) if first else "fewer runs than required"
            el = ET.SubElement(tc, tag, message=message[:500])
            el.text = "\n".join(lines)
        out = [f"Q: {case['ask']}"] + [f"A{a['run']}: {a['reply']}" for a in case["attempts"]]
        ET.SubElement(tc, "system-out").text = "\n".join(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def summary(data: dict[str, Any]) -> str:
    t = data["totals"]
    lines = []
    for case in data["cases"]:
        marks = " ".join(
            {"pass": "ok", "fail": "FAIL", "error": "ERR"}[a["verdict"]] for a in case["attempts"]
        )
        line = f"  {case['verdict'].upper():5} {case['id']} ({case['expect']}, {marks})"
        bad = next((a for a in case["attempts"] if a["problems"]), None)
        if bad:
            line += f": {'; '.join(bad['problems'])[:160]}"
        lines.append(line)
    lines.append(
        f"{data['verdict'].upper()}: {t['passed']}/{t['cases']} cases clean over "
        f"{t['attempts']} attempts; failed {t['failed']}, errored {t['errored']}; "
        f"flipped: {', '.join(data['flipped']) or 'none'}"
    )
    return "\n".join(lines)
