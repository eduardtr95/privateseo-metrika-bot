import io
import json
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from metrika_bot.bot import BotService
from metrika_bot.runtime import KeyedQueue, QuotaWait, RequestGate
from metrika_bot.telegram import TelegramAPI, TelegramAPIError
from metrika_bot.web import LimitedHTTPServer, make_handler
from metrika_bot.yandex import YandexAPIError, YandexClient


def test_queue_rejects_overflow_and_frees_capacity():
    queue = KeyedQueue(2, 4, "test")
    release = threading.Event()
    try:
        assert all(queue.submit(i, lambda: release.wait(3)) for i in range(4))
        assert not queue.submit(4, lambda: None)
        assert not queue.submit(0, lambda: None)
        assert len(queue.pending) == 4
    finally:
        release.set()
        queue.executor.shutdown(wait=True)
    assert not queue.pending


def test_request_gate_has_two_concurrent_slots_and_bounded_rate(monkeypatch):
    gate = RequestGate()
    both_started = threading.Event()
    release = threading.Event()
    guard = threading.Lock()
    active, maximum = 0, 0
    starts = []

    def call():
        nonlocal active, maximum
        with gate.enter("account", report=True):
            with guard:
                active += 1
                maximum = max(maximum, active)
                starts.append(time.monotonic())
                if active == 2:
                    both_started.set()
            assert release.wait(4)
            with guard:
                active -= 1

    threads = [threading.Thread(target=call) for _ in range(6)]
    try:
        for thread in threads:
            thread.start()
        assert both_started.wait(2)
        assert maximum == 2
    finally:
        release.set()
        for thread in threads:
            thread.join(3)
    assert maximum == 2 and len(starts) == 6
    assert all(b - a >= 0.05 for a, b in zip(starts, starts[1:]))


def test_five_minute_quota_defers_without_blocking_worker():
    gate = RequestGate()
    gate.history["account"] = [time.monotonic()] * 180
    with pytest.raises(QuotaWait) as caught:
        with gate.enter("account", report=True):
            pytest.fail("over-quota request was allowed")
    assert 299 <= caught.value.retry_after <= 301
    # Management calls and another account still progress.
    with gate.enter("account", report=False):
        pass
    with gate.enter("other", report=True):
        pass


@pytest.mark.parametrize(
    "status,body,auth",
    [
        (401, {}, True),
        (403, {}, True),
        (400, {"error": "invalid_grant"}, True),
        (429, {}, False),
        (503, {}, False),
    ],
)
def test_yandex_failure_classification_omits_raw_secrets(monkeypatch, status, body, auth):
    body["description"] = "SECRET_SHOULD_NOT_LEAK"
    failure = urllib.error.HTTPError(
        "https://example.test",
        status,
        "error",
        {"Retry-After": "600"},
        io.BytesIO(json.dumps(body).encode()),
    )
    monkeypatch.setattr(urllib.request, "urlopen", Mock(side_effect=failure))
    with pytest.raises(YandexAPIError) as caught:
        YandexClient._open_json(urllib.request.Request("https://example.test"))
    assert caught.value.reconnect == auth
    assert caught.value.status == status and caught.value.retry_after == 600
    assert "SECRET_SHOULD_NOT_LEAK" not in str(caught.value)


def test_unchanged_schedule_keyboard_is_not_reported_as_error():
    api = TelegramAPI("test")
    api.call = Mock(side_effect=TelegramAPIError("Bad Request: message is not modified"))
    api.edit_message_text(1, 2, "schedule", [])
    api.call = Mock(side_effect=TelegramAPIError("actual failure"))
    with pytest.raises(TelegramAPIError, match="actual failure"):
        api.edit_message_text(1, 2, "schedule", [])


def test_scheduler_recovers_after_database_error():
    service = object.__new__(BotService)
    service._setup_runtime()

    class Clock:
        cycles = 0

        def is_set(self):
            return self.cycles >= 2

        def wait(self, duration):
            self.cycles += 1

        def set(self):
            self.cycles = 2

    service.stop_event = Clock()
    service.scheduler_tick = Mock(side_effect=[RuntimeError("database temporarily locked"), None])
    try:
        service.run_scheduler()
        assert service.scheduler_tick.call_count == 2
        assert "scheduler" in service.heartbeats
    finally:
        service.stop()


def test_health_http_detects_missing_and_stale_workers_and_escapes_oauth_error():
    service = object.__new__(BotService)
    service._setup_runtime()
    service.db = Mock()
    server = LimitedHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def read(path):
        try:
            response = urllib.request.urlopen(base + path, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.headers, response.read().decode()

    try:
        assert read("/health")[0] == 503
        service.heartbeat("polling")
        service.heartbeat("scheduler")
        assert read("/health")[0] == 200
        service.heartbeats["scheduler"] = time.monotonic() - 181
        assert read("/health")[0] == 503
        code, headers, body = read(
            "/oauth/callback?error=%3Cscript%3Ebad%3C/script%3E&state=denied"
        )
        assert code == 400 and "<script>bad</script>" not in body and "&lt;script&gt;" in body
        assert (
            headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"
        )
        service.db.consume_oauth_state.assert_called_once_with("denied")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        service.stop()


def test_http_cancels_consumed_oauth_after_disconnect():
    service = SimpleNamespace(db=Mock(), yandex=Mock(), complete_oauth=Mock(return_value=False))
    service.db.consume_oauth_state.return_value = {"chat_id": 123, "code_verifier": "verifier"}
    server = LimitedHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/oauth/callback?code=test&state=test",
                timeout=3,
            )
        assert caught.value.code == 400
        assert "Подключение отменено" in caught.value.read().decode()
        service.complete_oauth.assert_called_once()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
