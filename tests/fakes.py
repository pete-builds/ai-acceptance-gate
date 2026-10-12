"""In-process servers that speak each adapter's protocol strictly enough to
refuse a client that gets it wrong: a bad Origin, a missing CSRF echo, a
missing bearer token. A `brain` maps the last user message to a reply."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Brain = Callable[[str, list[dict[str, Any]]], str]


def table(answers: dict[str, str], default: str = "I can't help with that.") -> Brain:
    return lambda message, history: answers.get(message, default)


class _Base(BaseHTTPRequestHandler):
    brain: Brain
    log: list[dict[str, Any]]
    script: dict[str, Any]

    def log_message(self, *args: Any) -> None:
        pass

    def _json(self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(n) or b"{}")

    def _sse_start(self, headers: dict[str, str] | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _write(self, chunk: str) -> None:
        self.wfile.write(chunk.encode())
        self.wfile.flush()

    def _queued_status(self) -> int | None:
        queue = self.script.get("statuses", [])
        return queue.pop(0) if queue else None


class SessionSseHandler(_Base):
    """POST /v1/session then POST /v1/chat, server-held history."""

    sessions: dict[str, list[str]]

    def do_POST(self) -> None:
        status = self._queued_status()
        if status is not None:
            self._json(status, {"detail": "scripted"})
            return
        if self.headers.get("Origin") != "http://localhost:8765":
            self._json(403, {"detail": "origin not allowed"})
            return
        body = self._body()
        if self.path == "/v1/session":
            if body.get("site") != "demo" or not body.get("turnstile_token"):
                self._json(404, {"detail": "unknown site"})
                return
            token = uuid.uuid4().hex
            self.sessions[token] = []
            self._json(200, {"token": token, "expires_in": 1800, "max_turns": 12})
            return
        if self.path == "/v1/chat":
            scheme, _, token = self.headers.get("Authorization", "").partition(" ")
            if scheme != "Bearer" or token not in self.sessions:
                self._json(401, {"detail": "session expired or missing"})
                return
            message = body["message"]
            history = self.sessions[token]
            self.log.append({"message": message, "history": list(history)})
            history.append(message)
            self._sse_start()
            if self.script.get("stream_error"):
                code = self.script["stream_error"]
                self._write(f'event: error\ndata: {{"code": "{code}", "message": "x"}}\n\n')
                return
            reply = self.brain(message, [{"role": "user", "content": m} for m in history[:-1]])
            for i in range(0, len(reply), 7):
                self._write(f"event: delta\ndata: {json.dumps({'text': reply[i : i + 7]})}\n\n")
            if not self.script.get("drop_done"):
                self._write('event: done\ndata: {"turns_left": 11}\n\n')
            return
        self._json(404, {"detail": "not found"})


class AiSdkUiHandler(_Base):
    """GET / issues XSRF-TOKEN; POST /api/chat needs it echoed and a same-origin Origin."""

    def do_GET(self) -> None:
        raw = b"<!doctype html><title>t</title>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if "XSRF-TOKEN" not in SimpleCookie(self.headers.get("Cookie", "")):
            self.send_header(
                "Set-Cookie", f"XSRF-TOKEN={uuid.uuid4().hex}; Path=/; SameSite=Strict"
            )
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        status = self._queued_status()
        if status is not None:
            self._json(status, {"error": "scripted"})
            return
        if self.path != "/api/chat":
            self._json(404, {"error": "not found"})
            return
        host = self.headers.get("Host", "")
        if self.headers.get("Origin") != f"http://{host}":
            self._json(403, {"error": "Cross-origin requests are not allowed."})
            return
        cookie = SimpleCookie(self.headers.get("Cookie", "")).get("XSRF-TOKEN")
        header = self.headers.get("X-XSRF-TOKEN")
        if cookie is None or not header or cookie.value != header:
            self._json(403, {"error": "Missing or invalid CSRF token."})
            return
        body = self._body()
        messages = body.get("messages", [])
        if any(not isinstance(m.get("parts"), list) for m in messages):
            self._json(400, {"error": "Messages must use the AI SDK UIMessage shape."})
            return
        texts = [
            {"role": m["role"], "content": "".join(p["text"] for p in m["parts"])} for m in messages
        ]
        self.log.append({"messages": texts, "body": body})
        self._sse_start({"x-vercel-ai-ui-message-stream": "v1"})

        def part(obj: dict[str, Any]) -> None:
            self._write(f"data: {json.dumps(obj)}\n\n")

        part({"type": "start"})
        if self.script.get("tool"):
            part({"type": "tool-input-available", "toolCallId": "c1", "toolName": "search_content"})
        if self.script.get("stream_error"):
            part({"type": "error", "errorText": self.script["stream_error"]})
            return
        reply = self.brain(texts[-1]["content"], texts[:-1])
        part({"type": "text-start", "id": "t1"})
        for i in range(0, len(reply), 5):
            part({"type": "text-delta", "id": "t1", "delta": reply[i : i + 5]})
        part({"type": "text-end", "id": "t1"})
        if self.script.get("drop_done"):
            return
        finish: dict[str, Any] = {"type": "finish"}
        if self.script.get("finish_reason"):
            finish["finishReason"] = self.script["finish_reason"]
        part(finish)
        self._write("data: [DONE]\n\n")


@contextmanager
def serve(
    handler: type[_Base], brain: Brain, script: dict[str, Any] | None = None
) -> Iterator[tuple[str, list[dict[str, Any]], dict[str, Any]]]:
    log: list[dict[str, Any]] = []
    script = script if script is not None else {}
    attrs: dict[str, Any] = {"brain": staticmethod(brain), "log": log, "script": script}
    if handler is SessionSseHandler:
        attrs["sessions"] = {}
    bound = type(handler.__name__, (handler,), attrs)
    server = ThreadingHTTPServer(("127.0.0.1", 0), bound)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", log, script
    finally:
        server.shutdown()
        server.server_close()
