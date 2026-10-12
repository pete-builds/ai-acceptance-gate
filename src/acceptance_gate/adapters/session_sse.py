"""`session-sse`: a widget backend that opens a session, then streams named SSE events.

    POST {session_path}  {"site": ..., "turnstile_token": ...}   with Origin
      -> {"token": "..."}
    POST {chat_path}     {"message": "..."}   Authorization: Bearer <token>
      -> event: delta  data: {"text": "..."}     (repeated)
         event: done   data: {...}
         event: error  data: {"code": "...", "message": "..."}

The server holds the conversation, so a case's scripted history cannot be
injected. Its user turns are replayed live on the same session instead, and
the reply's meta says so. Every ask opens a fresh session.
"""

from __future__ import annotations

import time
from typing import ClassVar

import httpx
from pydantic import Field

from acceptance_gate.adapters.base import (
    Options,
    Reply,
    TransportError,
    check_status,
    json_data,
    network_error,
    parse_options,
    sse_events,
)
from acceptance_gate.spec import Turn

# Cloudflare's published dummy response. Only a tenant on Turnstile's
# always-pass test keys accepts it; a real tenant needs a real token.
TURNSTILE_DUMMY = "XXXX.DUMMY.TOKEN.XXXX"


class SessionSseOptions(Options):
    site: str = Field(min_length=1)
    origin: str = Field(pattern=r"^https?://")
    session_path: str = "/v1/session"
    chat_path: str = "/v1/chat"
    turnstile_token: str = Field(default=TURNSTILE_DUMMY, min_length=1)
    # Stream error codes that mean "busy, ask again", not "broken".
    retry_codes: list[str] = Field(default_factory=lambda: ["busy"])
    headers: dict[str, str] = Field(default_factory=dict)


class SessionSseAdapter:
    name: ClassVar[str] = "session-sse"

    def __init__(self, base_url: str, options: dict, timeout: float) -> None:
        self.opts: SessionSseOptions = parse_options(SessionSseOptions, options, self.name)
        self.client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=10),
            headers={"Origin": self.opts.origin, **self.opts.headers},
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()

    def _session(self) -> str:
        try:
            r = self.client.post(
                self.opts.session_path,
                json={"site": self.opts.site, "turnstile_token": self.opts.turnstile_token},
            )
        except httpx.HTTPError as exc:
            raise network_error("open session", exc) from exc
        check_status(r, "open session")
        try:
            token = r.json().get("token")
        except (ValueError, AttributeError):
            token = None
        if not isinstance(token, str) or not token:
            raise TransportError("open session: response has no token")
        return token

    def _turn(self, token: str, message: str) -> Reply:
        parts: list[str] = []
        done: dict | None = None
        started = time.monotonic()
        try:
            with self.client.stream(
                "POST",
                self.opts.chat_path,
                json={"message": message},
                headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
            ) as r:
                check_status(r, "chat")
                for event in sse_events(r.iter_lines()):
                    if event.event == "delta":
                        text = json_data(event).get("text", "")
                        parts.append(text if isinstance(text, str) else "")
                    elif event.event == "done":
                        done = json_data(event)
                    elif event.event == "error":
                        data = json_data(event)
                        code = str(data.get("code", ""))
                        raise TransportError(
                            f"chat: stream error {code}: {data.get('message', '')}",
                            retryable=code in self.opts.retry_codes,
                        )
        except httpx.HTTPError as exc:
            raise network_error("chat", exc) from exc
        if done is None:
            # A stream that stops without `done` is a dropped reply, not a short one.
            raise TransportError("chat: stream ended without a done event", retryable=True)
        return Reply(
            text="".join(parts),
            meta={"latency_ms": int((time.monotonic() - started) * 1000), "done": done},
        )

    def ask(self, history: list[Turn], message: str) -> Reply:
        token = self._session()
        replayed = 0
        for turn in history:
            if turn.role == "user":
                self._turn(token, turn.content)
                replayed += 1
        reply = self._turn(token, message)
        if history:
            reply.meta["history"] = f"replayed {replayed} user turn(s) live"
        return reply
