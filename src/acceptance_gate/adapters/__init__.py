"""Adapters, by the name a suite's `target.adapter` uses."""

from __future__ import annotations

from acceptance_gate.adapters.ai_sdk_ui import AiSdkUiAdapter
from acceptance_gate.adapters.base import Adapter, Reply, TransportError
from acceptance_gate.adapters.session_sse import SessionSseAdapter
from acceptance_gate.spec import ConfigError, Target

ADAPTERS = {cls.name: cls for cls in (SessionSseAdapter, AiSdkUiAdapter)}

__all__ = ["ADAPTERS", "Adapter", "Reply", "TransportError", "build"]


def build(target: Target) -> Adapter:
    cls = ADAPTERS.get(target.adapter)
    if cls is None:
        raise ConfigError(
            f"unknown adapter {target.adapter!r}; known: {', '.join(sorted(ADAPTERS))}"
        )
    return cls(target.base_url, target.options, target.timeout_seconds)
