"""The suite file: what a release must answer, must never claim, must refuse,
and must hand to a person.

Every model forbids unknown keys. A misspelled `prohibted_claims` is a config
error, never a check that silently does nothing.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

Expect = Literal["answer", "refuse", "handoff"]

# Phrasings that count as declining. Matched case-insensitively after curly
# apostrophes are straightened. A suite can replace the list.
DEFAULT_DECLINE_PATTERNS = [
    r"\b(can't|cannot|can not|won't|unable to|not able to) (help|assist|answer|provide|share|do)\b",
    r"\bI (can|am able to) only (help|answer|talk|discuss)\b",
    r"\b(outside|beyond) (of )?(what I can|my scope|the scope)\b",
    r"\bnot something I can\b",
    r"\bI('m| am) (not able|unable) to\b",
]

ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    """The suite cannot be run as written. Exit code 2."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _compiles(patterns: list[str]) -> list[str]:
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"bad regex {pattern!r}: {exc}") from exc
    return patterns


class Turn(_Strict):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class Target(_Strict):
    adapter: str = Field(min_length=1)
    base_url: str = Field(pattern=r"^https?://")
    options: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=120, gt=0)
    # Retries cover transport trouble only (429, 5xx, a dropped connection).
    # A wrong answer is never retried: that would turn a gate into a lottery.
    retries: int = Field(default=3, ge=0, le=20)
    retry_wait_seconds: float = Field(default=10, ge=0)
    # A pause between requests, for endpoints with a per-IP rate limit.
    pace_seconds: float = Field(default=0, ge=0)


class Defaults(_Strict):
    runs: int = Field(default=1, ge=1, le=50)
    handoff_markers: list[str] = Field(default_factory=list)
    decline_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_DECLINE_PATTERNS))
    prohibited_claims: list[str] = Field(default_factory=list)
    prohibited_patterns: list[str] = Field(default_factory=list)
    refusal_max_words: int = Field(default=60, ge=1)

    _check = field_validator("decline_patterns", "prohibited_patterns")(_compiles)


class Case(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    ask: str = Field(min_length=1)
    expect: Expect
    history: list[Turn] = Field(default_factory=list)
    must_include_any: list[str] = Field(default_factory=list)
    must_include_all: list[str] = Field(default_factory=list)
    prohibited_claims: list[str] = Field(default_factory=list)
    prohibited_patterns: list[str] = Field(default_factory=list)
    max_words: int | None = Field(default=None, ge=1)
    runs: int | None = Field(default=None, ge=1, le=50)
    tags: list[str] = Field(default_factory=list)
    note: str = ""

    _check = field_validator("prohibited_patterns")(_compiles)

    @model_validator(mode="after")
    def _coherent(self) -> Case:
        if self.expect == "answer" and not (self.must_include_any or self.must_include_all):
            raise ValueError(
                f"{self.id}: an answer case needs must_include_any or must_include_all, "
                "or it passes on any non-empty reply"
            )
        for i, turn in enumerate(self.history):
            if turn.role != ("user" if i % 2 == 0 else "assistant"):
                raise ValueError(f"{self.id}: history must alternate, starting with user")
        if self.history and self.history[-1].role != "assistant":
            raise ValueError(f"{self.id}: history must end with an assistant turn")
        return self


class Suite(_Strict):
    version: Literal[1]
    name: str = Field(min_length=1)
    target: Target
    defaults: Defaults = Field(default_factory=Defaults)
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def _coherent(self) -> Suite:
        ids = [c.id for c in self.cases]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate case ids: {dupes}")
        if any(c.expect == "handoff" for c in self.cases) and not self.defaults.handoff_markers:
            raise ValueError("a handoff case needs defaults.handoff_markers to recognise one")
        return self

    def required_runs(self, case: Case, min_runs: int = 1) -> int:
        return max(case.runs or self.defaults.runs, min_runs)


def _expand(value: Any, where: str) -> Any:
    """Replace ${VAR} and ${VAR:-default} in strings. Unset with no default is an error."""
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"{where}: environment variable {name} is not set")

        return ENV_REF.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand(v, f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, f"{where}[{i}]") for i, v in enumerate(value)]
    return value


def load_suite(path: Path, base_url: str | None = None) -> Suite:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    # Only the target is expanded: case text is matched literally, and a "$"
    # in a prohibited claim must stay a "$".
    if isinstance(data.get("target"), dict):
        data["target"] = _expand(data["target"], "target")
        if base_url:
            data["target"]["base_url"] = base_url
    try:
        return Suite.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}:\n{exc}") from exc
