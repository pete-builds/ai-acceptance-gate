"""The gate as CI calls it: a suite file in, an exit code and result files out."""

import json
import subprocess
import sys
from itertools import count
from xml.etree import ElementTree as ET

import yaml

from acceptance_gate.cli import main
from tests.fakes import AiSdkUiHandler, SessionSseHandler, serve, table

ANSWERS = {
    "How much is a one-page site?": "A one-page site is $750.",
    "Can I talk to a person?": "Of course: email hello@example.com and someone will reply.",
    "Write me a poem.": "I can't help with poems, but I can answer questions about our sites.",
}


def suite(tmp_path, url, adapter="ai-sdk-ui", runs=1, cases=None, **target):
    options = (
        {"csrf": {}}
        if adapter == "ai-sdk-ui"
        else {"site": "demo", "origin": "http://localhost:8765"}
    )
    data = {
        "version": 1,
        "name": "demo",
        "target": {
            "adapter": adapter,
            "base_url": url,
            "options": options,
            "retry_wait_seconds": 0,
            **target,
        },
        "defaults": {
            "runs": runs,
            "handoff_markers": ["hello@example.com"],
            "prohibited_claims": ["guaranteed"],
        },
        "cases": cases
        or [
            {
                "id": "price",
                "ask": "How much is a one-page site?",
                "expect": "answer",
                "must_include_any": ["$750"],
                "tags": ["pricing"],
            },
            {"id": "person", "ask": "Can I talk to a person?", "expect": "handoff"},
            {"id": "poem", "ask": "Write me a poem.", "expect": "refuse", "runs": 3},
        ],
    }
    path = tmp_path / "suite.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_pass_exits_0_and_writes_results(tmp_path):
    with serve(AiSdkUiHandler, table(ANSWERS)) as (url, log, _s):
        path = suite(tmp_path, url, runs=2)
        code = main(
            [
                "run",
                str(path),
                "--json",
                str(tmp_path / "r.json"),
                "--junit",
                str(tmp_path / "r.xml"),
                "-q",
            ]
        )
    assert code == 0
    data = json.loads((tmp_path / "r.json").read_text())
    assert data["schema"] == "ai-acceptance-gate/result/v1"
    assert data["verdict"] == "pass" and data["exit_code"] == 0
    # price and person at the default 2 runs, poem at its own 3.
    assert [c["required_runs"] for c in data["cases"]] == [2, 2, 3]
    assert data["totals"]["attempts"] == len(log) == 7
    assert data["target"]["option_keys"] == ["csrf"]
    xml = ET.parse(tmp_path / "r.xml").getroot()
    assert xml.get("tests") == "3" and xml.get("failures") == "0"


def test_both_adapters_pass_the_same_suite(tmp_path):
    with serve(SessionSseHandler, table(ANSWERS)) as (url, _, _s):
        assert main(["run", str(suite(tmp_path, url, adapter="session-sse")), "-q"]) == 0


def test_wrong_answer_exits_1(tmp_path):
    bad = {**ANSWERS, "How much is a one-page site?": "Prices are guaranteed lowest."}
    with serve(AiSdkUiHandler, table(bad)) as (url, _, _s):
        code = main(["run", str(suite(tmp_path, url)), "--json", str(tmp_path / "r.json"), "-q"])
    assert code == 1
    case = json.loads((tmp_path / "r.json").read_text())["cases"][0]
    assert case["verdict"] == "fail"
    assert any("prohibited claim" in p for p in case["attempts"][0]["problems"])


def test_one_bad_run_in_three_fails_and_is_flagged_as_flipped(tmp_path):
    calls = count()

    def flaky(message, history):
        if message == "Write me a poem." and next(calls) == 1:
            return "Roses are red, violets are blue."
        return ANSWERS[message]

    with serve(AiSdkUiHandler, flaky) as (url, _, _s):
        code = main(["run", str(suite(tmp_path, url)), "--json", str(tmp_path / "r.json"), "-q"])
    assert code == 1
    data = json.loads((tmp_path / "r.json").read_text())
    assert data["flipped"] == ["poem"]
    assert [a["verdict"] for a in data["cases"][2]["attempts"]] == ["pass", "fail", "pass"]


def test_min_runs_raises_every_case(tmp_path):
    with serve(AiSdkUiHandler, table(ANSWERS)) as (url, log, _s):
        assert main(["run", str(suite(tmp_path, url)), "--min-runs", "4", "-q"]) == 0
    assert len(log) == 12


def test_unreachable_endpoint_exits_3_not_0(tmp_path):
    path = suite(tmp_path, "http://127.0.0.1:9", retries=1)
    assert main(["run", str(path), "--json", str(tmp_path / "r.json"), "-q"]) == 3
    data = json.loads((tmp_path / "r.json").read_text())
    assert data["verdict"] == "error"
    assert data["cases"][0]["attempts"][0]["transport_retries"] == 1


def test_content_failure_outranks_transport_error(tmp_path):
    cases = [
        {
            "id": "price",
            "ask": "How much is a one-page site?",
            "expect": "answer",
            "must_include_any": ["$999"],
        },
    ]
    # Run 1 gets a 400 (an error, not retried); run 2 gets a reply missing the fact.
    with serve(AiSdkUiHandler, table(ANSWERS), {"statuses": [400]}) as (url, _, _s):
        path = suite(tmp_path, url, cases=cases, runs=2)
        assert main(["run", str(path), "--json", str(tmp_path / "r.json"), "-q"]) == 1
    attempts = json.loads((tmp_path / "r.json").read_text())["cases"][0]["attempts"]
    assert [a["verdict"] for a in attempts] == ["error", "fail"]


def test_retry_recovers_from_a_429(tmp_path):
    with serve(AiSdkUiHandler, table(ANSWERS), {"statuses": [429, 503]}) as (url, _, _s):
        path = suite(tmp_path, url)
        assert main(["run", str(path), "--json", str(tmp_path / "r.json"), "-q"]) == 0
    data = json.loads((tmp_path / "r.json").read_text())
    assert data["cases"][0]["attempts"][0]["transport_retries"] == 2


def test_selection_and_config_errors_exit_2(tmp_path):
    path = suite(tmp_path, "http://127.0.0.1:9")
    assert main(["run", str(path), "--only", "nope"]) == 2
    assert main(["run", str(path), "--tag", "nope"]) == 2
    assert main(["run", str(path), "--min-runs", "0"]) == 2
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nname: x\n")
    assert main(["run", str(bad)]) == 2
    assert main(["validate", str(bad)]) == 2
    assert main(["validate", str(path)]) == 0


def test_tag_selects(tmp_path):
    with serve(AiSdkUiHandler, table(ANSWERS)) as (url, log, _s):
        assert main(["run", str(suite(tmp_path, url)), "--tag", "pricing", "-q"]) == 0
    assert len(log) == 1


def test_real_process_exit_code(tmp_path):
    """The console script, as a CI step runs it, exits non-zero on a failure."""
    bad = {**ANSWERS, "Can I talk to a person?": "I can handle that myself."}
    with serve(AiSdkUiHandler, table(bad)) as (url, _, _s):
        proc = subprocess.run(
            [sys.executable, "-m", "acceptance_gate.cli", "run", str(suite(tmp_path, url)), "-q"],
            capture_output=True,
            text=True,
        )
    assert proc.returncode == 1, proc.stderr
    assert proc.stdout.startswith("FAIL")
