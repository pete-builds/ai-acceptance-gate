import pytest

from acceptance_gate.adapters import ADAPTERS, TransportError
from acceptance_gate.spec import ConfigError, Turn
from tests.fakes import AiSdkUiHandler, SessionSseHandler, serve, table

SESSION_OPTS = {"site": "demo", "origin": "http://localhost:8765"}
UI_OPTS = {"csrf": {}}


def session_adapter(url, **opts):
    return ADAPTERS["session-sse"](url, {**SESSION_OPTS, **opts}, 5)


def ui_adapter(url, **opts):
    return ADAPTERS["ai-sdk-ui"](url, {**UI_OPTS, **opts}, 5)


# ---- session-sse ----


def test_session_sse_streams_a_reply():
    hello = "Hello there, how can I help today?"
    with serve(SessionSseHandler, table({"hi": hello})) as (url, _, _s):
        reply = session_adapter(url).ask([], "hi")
    assert reply.text == hello
    assert reply.meta["done"] == {"turns_left": 11}


def test_session_sse_replays_history_live():
    history = [
        Turn(role="user", content="first"),
        Turn(role="assistant", content="scripted, never sent"),
        Turn(role="user", content="second"),
        Turn(role="assistant", content="scripted"),
    ]
    with serve(SessionSseHandler, table({}, "ok")) as (url, log, _s):
        reply = session_adapter(url).ask(history, "third")
    assert [e["message"] for e in log] == ["first", "second", "third"]
    assert log[-1]["history"] == ["first", "second"]
    assert reply.meta["history"] == "replayed 2 user turn(s) live"


def test_session_sse_wrong_origin_is_a_setup_error():
    with serve(SessionSseHandler, table({})) as (url, _, _s):
        with pytest.raises(TransportError, match="403") as exc:
            session_adapter(url, origin="http://evil.test").ask([], "hi")
    assert not exc.value.retryable


def test_session_sse_busy_is_retryable_other_codes_are_not():
    with serve(SessionSseHandler, table({}), {"stream_error": "busy"}) as (url, _, _s):
        with pytest.raises(TransportError) as exc:
            session_adapter(url).ask([], "hi")
    assert exc.value.retryable
    with serve(SessionSseHandler, table({}), {"stream_error": "budget"}) as (url, _, _s):
        with pytest.raises(TransportError) as exc:
            session_adapter(url).ask([], "hi")
    assert not exc.value.retryable


def test_session_sse_stream_without_done_is_not_a_reply():
    with serve(SessionSseHandler, table({}, "half an ans"), {"drop_done": True}) as (url, _, _s):
        with pytest.raises(TransportError, match="without a done"):
            session_adapter(url).ask([], "hi")


def test_session_sse_unknown_option_is_a_config_error():
    with pytest.raises(ConfigError, match="sitee"):
        ADAPTERS["session-sse"]("http://x", {**SESSION_OPTS, "sitee": "x"}, 5)


# ---- ai-sdk-ui ----


def test_ai_sdk_ui_csrf_flow_and_reply():
    with serve(AiSdkUiHandler, table({"hi": "Hello from the site assistant."})) as (url, log, _s):
        adapter = ui_adapter(url)
        assert adapter.ask([], "hi").text == "Hello from the site assistant."
        # The cookie is reused, not refetched.
        assert adapter.ask([], "hi").text == "Hello from the site assistant."
    assert log[0]["body"]["trigger"] == "submit-message"


def test_ai_sdk_ui_without_csrf_is_refused():
    # Control: the fake really enforces the double-submit check.
    with serve(AiSdkUiHandler, table({})) as (url, _, _s):
        with pytest.raises(TransportError, match="CSRF"):
            ADAPTERS["ai-sdk-ui"](url, {}, 5).ask([], "hi")


def test_ai_sdk_ui_wrong_origin_is_refused():
    with serve(AiSdkUiHandler, table({})) as (url, _, _s):
        with pytest.raises(TransportError, match="Cross-origin"):
            ui_adapter(url, origin="http://evil.test").ask([], "hi")


def test_ai_sdk_ui_sends_scripted_history():
    history = [Turn(role="user", content="a"), Turn(role="assistant", content="b")]
    with serve(AiSdkUiHandler, table({}, "ok")) as (url, log, _s):
        ui_adapter(url, body={"model": "m"}).ask(history, "c")
    assert log[0]["messages"] == [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    assert log[0]["body"]["model"] == "m"


def test_ai_sdk_ui_records_tools_and_finish_reason():
    script = {"tool": True, "finish_reason": "length"}
    with serve(AiSdkUiHandler, table({}, "ok"), script) as (url, _, _s):
        reply = ui_adapter(url).ask([], "x")
    assert reply.tools == ["search_content"]
    assert reply.finish_reason == "length"


def test_ai_sdk_ui_stream_error_and_truncation():
    with serve(AiSdkUiHandler, table({}), {"stream_error": "gateway down"}) as (url, _, _s):
        with pytest.raises(TransportError, match="gateway down"):
            ui_adapter(url).ask([], "x")
    with serve(AiSdkUiHandler, table({}, "partial"), {"drop_done": True}) as (url, _, _s):
        with pytest.raises(TransportError, match="without finish"):
            ui_adapter(url).ask([], "x")


def test_http_429_is_retryable_400_is_not():
    with serve(AiSdkUiHandler, table({}), {"statuses": [429]}) as (url, _, _s):
        with pytest.raises(TransportError) as exc:
            ui_adapter(url).ask([], "x")
    assert exc.value.retryable
    with serve(AiSdkUiHandler, table({}), {"statuses": [400]}) as (url, _, _s):
        with pytest.raises(TransportError) as exc:
            ui_adapter(url).ask([], "x")
    assert not exc.value.retryable


def test_connection_refused_is_a_transport_error():
    with pytest.raises(TransportError, match="Connect"):
        ui_adapter("http://127.0.0.1:9").ask([], "x")
