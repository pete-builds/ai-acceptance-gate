"""`ai-sdk-ui`: an endpoint that speaks the Vercel AI SDK UI message stream.

    POST {chat_path}  {"id": ..., "messages": [UIMessage...], "trigger": "submit-message"}
      -> data: {"type": "text-delta", "id": ..., "delta": "..."}   (repeated)
         data: {"type": "tool-input-available", "toolName": ...}
         data: {"type": "error", "errorText": "..."}
         data: {"type": "finish", ...}
         data: [DONE]

Optional double-submit CSRF: GET {csrf.page_path} first to receive the cookie,
then echo its value in the header on every POST, with a same-origin Origin.
Scripted history is sent as earlier messages, exactly as a browser would.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from acceptance_gate.adapters.base import (
    Options,
    Reply,
    TransportError,
    check_status,
    network_error,
    parse_options,
    sse_events,
)
from acceptance_gate.spec import Turn


class Csrf(Options):
    page_path: str = "/"
    cookie: str = "XSRF-TOKEN"
    header: str = "X-XSRF-TOKEN"


class AiSdkUiOptions(Options):
    chat_path: str = "/api/chat"
    # Defaults to the base URL's own origin, which is what a same-origin check wants.
    origin: str | None = Field(default=None, pattern=r"^https?://")
    csrf: Csrf | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    # Merged into the request body, e.g. {"model": "..."}.
    body: dict[str, Any] = Field(default_factory=dict)


def _ui_message(role: str, text: str) -> dict[str, Any]:
    return {"id": uuid.uuid4().hex, "role": role, "parts": [{"type": "text", "text": text}]}


class AiSdkUiAdapter:
    name: ClassVar[str] = "ai-sdk-ui"

    def __init__(self, base_url: str, options: dict, timeout: float) -> None:
        self.opts: AiSdkUiOptions = parse_options(AiSdkUiOptions, options, self.name)
        parts = urlsplit(base_url)
        origin = self.opts.origin or f"{parts.scheme}://{parts.netloc}"
        self.client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=10),
            headers={"Origin": origin, **self.opts.headers},
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()

    def _csrf_token(self) -> str | None:
        csrf = self.opts.csrf
        if csrf is None:
            return None
        token = self.client.cookies.get(csrf.cookie)
        if token:
            return token
        try:
            r = self.client.get(csrf.page_path, headers={"Accept": "text/html"})
        except httpx.HTTPError as exc:
            raise network_error("fetch csrf page", exc) from exc
        check_status(r, "fetch csrf page")
        token = self.client.cookies.get(csrf.cookie)
        if not token:
            raise TransportError(
                f"fetch csrf page: GET {csrf.page_path} set no {csrf.cookie} cookie"
            )
        return token

    def ask(self, history: list[Turn], message: str) -> Reply:
        token = self._csrf_token()
        headers = {"Accept": "text/event-stream"}
        if token is not None and self.opts.csrf is not None:
            headers[self.opts.csrf.header] = token
        body = {
            **self.opts.body,
            "id": uuid.uuid4().hex,
            "messages": [_ui_message(t.role, t.content) for t in history]
            + [_ui_message("user", message)],
            "trigger": "submit-message",
        }
        text: list[str] = []
        tools: list[str] = []
        finish: str | None = None
        finished = False
        started = time.monotonic()
        try:
            with self.client.stream("POST", self.opts.chat_path, json=body, headers=headers) as r:
                check_status(r, "chat")
                if "text/event-stream" not in r.headers.get("content-type", ""):
                    raise TransportError(
                        f"chat: expected text/event-stream, got {r.headers.get('content-type')!r}"
                    )
                for event in sse_events(r.iter_lines()):
                    if event.data == "[DONE]":
                        finished = True
                        break
                    try:
                        part = json.loads(event.data)
                    except json.JSONDecodeError as exc:
                        raise TransportError(
                            f"chat: stream part is not JSON: {event.data[:120]}"
                        ) from exc
                    kind = part.get("type") if isinstance(part, dict) else None
                    if kind == "text-delta":
                        delta = part.get("delta", "")
                        text.append(delta if isinstance(delta, str) else "")
                    elif kind in ("tool-input-available", "tool-input-start"):
                        name = part.get("toolName")
                        if isinstance(name, str) and name not in tools:
                            tools.append(name)
                    elif kind == "error":
                        raise TransportError(f"chat: stream error: {part.get('errorText', '')}")
                    elif kind == "finish":
                        finished = True
                        reason = part.get("finishReason")
                        finish = reason if isinstance(reason, str) else finish
        except httpx.HTTPError as exc:
            raise network_error("chat", exc) from exc
        if not finished:
            raise TransportError("chat: stream ended without finish or [DONE]", retryable=True)
        return Reply(
            text="".join(text),
            finish_reason=finish,
            tools=tools,
            meta={"latency_ms": int((time.monotonic() - started) * 1000)},
        )
