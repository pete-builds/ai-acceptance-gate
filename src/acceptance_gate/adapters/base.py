"""What every adapter provides: one question in, one finished reply out."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from acceptance_gate.spec import ConfigError, Turn


@dataclass
class Reply:
    text: str
    finish_reason: str | None = None
    tools: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


class TransportError(Exception):
    """The endpoint did not produce a reply to grade. Never a content verdict."""

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Adapter(Protocol):
    name: ClassVar[str]

    def ask(self, history: list[Turn], message: str) -> Reply: ...

    def close(self) -> None: ...


def parse_options(cls: type[Options], raw: dict[str, Any], adapter: str) -> Any:
    try:
        return cls.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"target.options for adapter {adapter!r}:\n{exc}") from exc


def check_status(response: httpx.Response, step: str) -> None:
    """Raise for a non-2xx. 429 and 5xx are worth retrying; other 4xx are setup errors."""
    if response.is_success:
        return
    try:
        body = response.read().decode("utf-8", "replace")[:200]
    except httpx.HTTPError:
        body = ""
    retryable = response.status_code == 429 or response.status_code >= 500
    raise TransportError(f"{step}: HTTP {response.status_code} {body}".strip(), retryable)


@dataclass
class Event:
    event: str
    data: str


def sse_events(lines: Iterator[str]) -> Iterator[Event]:
    """Server-Sent Events per the WHATWG spec: data lines join with newlines,
    a blank line dispatches, a line starting with ':' is a comment."""
    name, data = "message", []
    for line in lines:
        if line == "":
            if data:
                yield Event(name, "\n".join(data))
            name, data = "message", []
            continue
        if line.startswith(":"):
            continue
        key, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if key == "event":
            name = value
        elif key == "data":
            data.append(value)
    if data:
        yield Event(name, "\n".join(data))


def json_data(event: Event) -> dict[str, Any]:
    try:
        parsed = json.loads(event.data)
    except json.JSONDecodeError as exc:
        raise TransportError(
            f"stream event {event.event!r} is not JSON: {event.data[:120]}"
        ) from exc
    if not isinstance(parsed, dict):
        raise TransportError(f"stream event {event.event!r} is not an object")
    return parsed


def network_error(step: str, exc: httpx.HTTPError) -> TransportError:
    return TransportError(f"{step}: {type(exc).__name__}: {exc}", retryable=True)
