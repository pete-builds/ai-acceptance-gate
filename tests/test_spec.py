from pathlib import Path

import pytest
import yaml

from acceptance_gate.spec import ConfigError, load_suite

BASE = {
    "version": 1,
    "name": "t",
    "target": {"adapter": "ai-sdk-ui", "base_url": "http://127.0.0.1:1"},
    "defaults": {"handoff_markers": ["hello@example.com"]},
    "cases": [{"id": "a", "ask": "q", "expect": "answer", "must_include_any": ["x"]}],
}


def write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "suite.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def with_case(**case) -> dict:
    return {**BASE, "cases": [{"id": "a", "ask": "q", **case}]}


def test_minimal_suite_loads(tmp_path):
    suite = load_suite(write(tmp_path, BASE))
    assert suite.cases[0].id == "a"
    assert suite.required_runs(suite.cases[0]) == 1
    assert suite.required_runs(suite.cases[0], min_runs=3) == 3


@pytest.mark.parametrize(
    "data, needle",
    [
        ({**BASE, "defualts": {}}, "defualts"),
        (with_case(expect="answer", must_include_any=["x"], prohibted_claims=["y"]), "prohibted"),
        (with_case(expect="answer"), "needs must_include_any"),
        (with_case(expect="refuse", prohibited_patterns=["("]), "bad regex"),
        (
            with_case(expect="refuse", history=[{"role": "assistant", "content": "hi"}]),
            "must alternate",
        ),
        (with_case(expect="refuse", history=[{"role": "user", "content": "hi"}]), "must end"),
        ({**BASE, "cases": BASE["cases"] * 2}, "duplicate case ids"),
        ({**with_case(expect="handoff"), "defaults": {}}, "handoff_markers"),
        ({**BASE, "version": 2}, "version"),
        ({**BASE, "target": {**BASE["target"], "base_url": "localhost:1"}}, "base_url"),
    ],
)
def test_bad_suites_are_config_errors(tmp_path, data, needle):
    with pytest.raises(ConfigError, match=needle):
        load_suite(write(tmp_path, data))


def test_case_runs_override_default(tmp_path):
    data = {**BASE, "defaults": {**BASE["defaults"], "runs": 2}}
    data["cases"] = [{**BASE["cases"][0], "runs": 5}]
    suite = load_suite(write(tmp_path, data))
    assert suite.required_runs(suite.cases[0]) == 5
    # --min-runs raises, never lowers.
    assert suite.required_runs(suite.cases[0], min_runs=3) == 5


def test_env_expansion_in_target_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GATE_URL", "http://10.0.0.5:8080")
    data = {
        **BASE,
        "target": {
            "adapter": "ai-sdk-ui",
            "base_url": "${GATE_URL}",
            "options": {"origin": "${GATE_ORIGIN:-http://example.test}"},
        },
        "cases": [{**BASE["cases"][0], "prohibited_claims": ["${NOT_EXPANDED}"]}],
    }
    suite = load_suite(write(tmp_path, data))
    assert suite.target.base_url == "http://10.0.0.5:8080"
    assert suite.target.options["origin"] == "http://example.test"
    assert suite.cases[0].prohibited_claims == ["${NOT_EXPANDED}"]


def test_unset_env_without_default_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("GATE_MISSING", raising=False)
    data = {**BASE, "target": {**BASE["target"], "base_url": "${GATE_MISSING}"}}
    with pytest.raises(ConfigError, match="GATE_MISSING is not set"):
        load_suite(write(tmp_path, data))


def test_base_url_override(tmp_path):
    suite = load_suite(write(tmp_path, BASE), base_url="http://other:9")
    assert suite.target.base_url == "http://other:9"


def test_examples_are_valid():
    paths = sorted(Path(__file__).parent.parent.joinpath("examples").glob("*.yaml"))
    # This passed on an empty folder once while an example was unparseable.
    assert {p.name for p in paths} >= {"session-sse.yaml", "ai-sdk-ui.yaml"}
    for path in paths:
        load_suite(path, base_url="http://127.0.0.1:1")
