"""Launch audit regressions: data correctness, stale actions and isolation."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet

from metrika_bot.analysis import (
    BreakdownChange,
    Change,
    Period,
    ReportBuilder,
    ReportData,
    compare_breakdowns,
    format_compact_report,
    format_report,
)
from metrika_bot.bot import BotService
from metrika_bot.config import Config
from metrika_bot.crypto import TokenCipher
from metrika_bot.db import Database
from metrika_bot.formatting import fit_html
from metrika_bot.yandex import OAuthTokens, YandexAPIError, YandexClient


@pytest.fixture
def service(tmp_path):
    cfg = Config(
        "telegram",
        "client",
        "secret",
        "https://example.test/oauth/callback",
        Fernet.generate_key().decode(),
        tmp_path / "bot.sqlite3",
    )
    db = Database(cfg.database_path)
    yandex = YandexClient(cfg, db, TokenCipher(cfg.token_encryption_key))
    bot = BotService(cfg, db, Mock(), yandex)
    db.upsert_user(123, "owner")
    yandex.save_tokens(123, OAuthTokens("access", "refresh", None))
    db.select_counter(123, 1, "example.test")
    db.set_goals(123, [11])
    yandex.counters = Mock(return_value=[{"id": i, "name": f"Site {i}"} for i in range(1, 46)])
    yandex.goals = Mock(return_value=[{"id": i, "name": f"purchase {i:02}"} for i in range(1, 51)])
    yield bot
    bot.stop()
    bot.jobs.executor.shutdown(wait=True, cancel_futures=True)
    bot.updates.executor.shutdown(wait=True, cancel_futures=True)


def sample(**kwargs):
    values = dict(
        counter_name="example.test",
        current_period=Period(date(2026, 9, 7), date(2026, 9, 7)),
        previous_period=Period(date(2026, 9, 6), date(2026, 9, 6)),
        visits=Change(100, 100),
        users=Change(90, 90),
        goals=Change(4, 10),
        goal_names=["purchase"],
        goal_details=[BreakdownChange("purchase", 4, 10)],
        sources=[],
        pages=[],
    )
    values.update(kwargs)
    return ReportData(**values)


def callback(data, chat=123):
    return {
        "callback_query": {
            "id": "cb",
            "data": data,
            "from": {"username": "owner"},
            "message": {"message_id": 9, "chat": {"id": chat, "type": "private"}},
        }
    }


def command(text, chat=123):
    return {"message": {"chat": {"id": chat, "type": "private"}, "text": text}}


def buttons(bot):
    return [button for row in bot.telegram.send_message.call_args.args[2] for button in row]


def test_manual_english_goal_is_counted_without_name_heuristic(service):
    client = service.yandex
    client.goals = Mock(return_value=[{"id": 11, "name": "purchase"}])

    def report(*args, **kw):
        metrics = args[4]
        if kw.get("filters"):
            return {"totals": [3]}
        if len(args) > 5:
            return {"data": [], "totals": [0]}
        return {"totals": [12] * len(metrics)}

    client.report = Mock(side_effect=report)
    data = ReportBuilder(client).collect(123, service.db.get_connection(123))
    assert data.goals == Change(3, 3)  # unique visits, not 12 goal reaches
    assert data.goal_names == ["purchase"]
    assert (
        sum(
            "goal11IsReached" in str(c.kwargs.get("filters", ""))
            for c in client.report.call_args_list
        )
        == 2
    )


def page_row(name, visits):
    return {"dimensions": [{"name": name}], "metrics": [visits]}


def test_page_501_is_fetched_and_does_not_turn_into_false_zero():
    class API:
        def report(self, *args, offset=1, **kw):
            rows = [page_row(f"page{i}", 1000 - i) for i in range(500)] + [page_row("tail", 100)]
            return {"total_rows": 501, "data": rows[offset - 1 : offset + 499]}

    payload, complete = ReportBuilder(API())._pages(
        1, 1, Period(date(2026, 9, 1), date(2026, 9, 7))
    )
    assert complete and len(payload["data"]) == 501
    assert payload["data"][-1]["metrics"] == [100]


def test_pages_limit_excludes_unknown_but_keeps_verified_zero():
    class API:
        calls = 0

        def report(self, *args, offset=1, **kw):
            self.calls += 1
            return {
                "total_rows": 6000,
                "data": [page_row(f"page{i}", 3) for i in range(offset, offset + 500)],
            }

    api = API()
    payload, complete = ReportBuilder(api)._pages(1, 1, Period(date(2026, 9, 1), date(2026, 9, 7)))
    assert not complete and len(payload["data"]) == 5000 and api.calls == 10
    result = compare_breakdowns({"both": 10}, {"both": 10, "unknown": 100}, current_complete=False)
    assert [(r.name, r.delta) for r in result] == [("both", 0)]
    assert compare_breakdowns({}, {"truly gone": 100})[0].delta == -100
    assert "не означает ноль" in format_report(sample(pages_partial=True))


def test_goal_drop_stays_visible_in_overview():
    text = format_compact_report(
        sample(
            sources=[BreakdownChange("Переходы из поисковых систем", 30, 80)],
            pages=[BreakdownChange("https://example.test/page", 5, 50)],
        )
    )
    assert "Целевые визиты:" in text
    assert "Проверить формы" in format_report(sample())


def test_deleted_selected_goal_is_reported_as_missing(service):
    service.db.set_goals(123, [11, 999])
    service.yandex.report = Mock(return_value={"totals": [10, 9], "data": []})
    data = service.reports.collect(123, service.db.get_connection(123))
    assert data.missing_goals == [999]
    assert "цели удалены или недоступны" in format_report(data)


def test_robots_and_timezone_apply_to_every_kind_of_request(service):
    client = service.yandex
    client._api = Mock(return_value={})
    for dimensions, filters in [
        (None, None),
        (["ym:s:startURL"], None),
        (None, "ym:s:goal11IsReached=='yes' OR ym:s:goal12IsReached=='yes'"),
    ]:
        client.report(
            123, 1, "2026-09-01", "2026-09-07", ["ym:s:visits"], dimensions, filters=filters
        )
        params = client._api.call_args.args[2]
        assert params["timezone"] == "+03:00"
        assert params["filters"].endswith("ym:s:isRobot=='No'")
        if filters:
            assert params["filters"].startswith("(" + filters + ") AND ")


def test_report_uses_moscow_date_at_almaty_midnight(service, monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, 20, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr("metrika_bot.analysis.datetime", FixedDateTime)
    service.yandex.report = Mock(return_value={"totals": [10, 9], "data": []})
    data = service.reports.collect(123, service.db.get_connection(123), days=1)
    assert data.current_period.end == date(2026, 9, 6)
    assert data.previous_period.end == date(2026, 9, 5)


def test_all_counter_pages_and_goal_pages_are_reachable(service):
    gen = service.db.get_connection(123)["generation"]
    service.send_counters(123, page=2)
    assert any(b["callback_data"] == f"c:{gen}:45" for b in buttons(service))
    service.send_goals(123, page=2)
    last_button = next(b for b in buttons(service) if b["callback_data"].endswith(":50"))
    assert len(last_button["callback_data"].encode()) <= 64
    service.handle_update(callback(last_button["callback_data"]))
    assert 50 in json.loads(service.db.get_connection(123)["goal_ids"])


def test_counter_management_api_follows_offset(service):
    service.yandex._api = Mock(
        side_effect=[{"counters": [{"id": i} for i in range(1000)]}, {"counters": [{"id": 1000}]}]
    )
    counters = YandexClient.counters(service.yandex, 123)
    assert len(counters) == 1001
    assert [c.args[2]["offset"] for c in service.yandex._api.call_args_list] == [1, 1001]


@pytest.mark.parametrize(
    "action", ["g:{gen}:0:11", "ga:{gen}:0:rec", "ga:{gen}:0:clear", "ready:{gen}"]
)
def test_old_goal_keyboard_cannot_change_new_counter(service, action):
    old_gen = service.db.get_connection(123)["generation"]
    service.db.select_counter(123, 2, "second")
    service.db.set_goals(123, [22])
    service.handle_update(callback(action.format(gen=old_gen)))
    assert json.loads(service.db.get_connection(123)["goal_ids"]) == [22]
    service.yandex.goals.assert_not_called()
    service.telegram.send_rich_message.assert_not_called()


@pytest.mark.parametrize("destructive", ["/disconnect", "/delete_me"])
def test_inflight_report_cannot_send_or_recreate_events_after_removal(service, destructive):
    started, release = threading.Event(), threading.Event()

    def collect(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return sample()

    service.reports.collect = collect
    try:
        assert service.send_report(123)
        assert started.wait(3)
        service._dispatch_update(command(destructive))
    finally:
        release.set()
    service.jobs.executor.shutdown(wait=True)
    service.telegram.send_rich_message.assert_not_called()
    assert service.db.get_connection(123) is None
    with service.db.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM events WHERE event='report_manual'").fetchone()[0] == 0
        )
        assert db.execute("SELECT COUNT(*) FROM report_contexts").fetchone()[0] == 0
    if destructive == "/delete_me":
        assert service.db.get_user(123) is None
        service.db.event(123, "late event")
        with service.db.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_repeated_report_requests_are_coalesced_and_other_user_progresses(service):
    started, release, second = threading.Event(), threading.Event(), threading.Event()
    service.db.upsert_user(124, "second")
    service.yandex.save_tokens(124, OAuthTokens("other", None, None))
    service.db.select_counter(124, 2, "second")
    counts = []

    def collect(chat, *args, **kwargs):
        counts.append(chat)
        if chat == 123:
            started.set()
            assert release.wait(5)
        else:
            second.set()
        return sample()

    service.reports.collect = collect
    try:
        assert service.send_report(123)
        assert started.wait(2)
        assert all(not service.send_report(123) for _ in range(8))
        assert service.send_report(124)
        assert second.wait(2)
        service.handle_update(command("/pause"))
        assert service.db.get_user(123)["report_enabled"] == 0
    finally:
        release.set()
    service.jobs.executor.shutdown(wait=True)
    assert counts.count(123) == 1 and counts.count(124) == 1


@pytest.mark.parametrize("remove", ["/disconnect", "/delete_me"])
def test_oauth_consumed_before_removal_cannot_restore_connection(service, remove):
    state = parse_qs(urlparse(service.yandex.authorization_url(123)).query)["state"][0]
    pending = service.db.consume_oauth_state(state)
    service.handle_update(command(remove))
    # Starting again after deletion must not resurrect the old authorization.
    service.db.upsert_user(123, "owner")
    assert not service.complete_oauth(pending, OAuthTokens("late access", None, None))
    assert service.db.get_connection(123) is None


def test_only_latest_oauth_link_can_complete_and_scope_is_read_only(service):
    first = parse_qs(urlparse(service.yandex.authorization_url(123)).query)
    pending = service.db.consume_oauth_state(first["state"][0])
    second = parse_qs(urlparse(service.yandex.authorization_url(123)).query)
    assert second["scope"] == ["metrika:read"]
    assert not service.complete_oauth(pending, OAuthTokens("stale", None, None))
    assert service.complete_oauth(
        service.db.consume_oauth_state(second["state"][0]), OAuthTokens("new", None, None)
    )
    assert service.yandex.token_for(123) == "new"


def test_oauth_state_atomic_under_concurrent_callbacks(service):
    service.db.save_oauth_state("one-state", 123, "pkce")
    barrier = threading.Barrier(8)

    def consume(_):
        barrier.wait(timeout=3)
        return service.db.consume_oauth_state("one-state")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(consume, range(8)))
    assert sum(result is not None for result in results) == 1


def test_refresh_cannot_overwrite_reconnected_account(service):
    old = service.db.get_connection(123)["generation"]
    service.yandex.save_tokens(123, OAuthTokens("new-account", None, None))
    assert not service.db.update_tokens(123, "old-refresh", None, None, expected_generation=old)
    assert service.yandex.token_for(123) == "new-account"


def test_report_stops_api_requests_after_disconnect(service):
    gen = service.db.get_connection(123)["generation"]
    with service.yandex.report_scope(123, gen):
        service.db.disconnect(123)
        with pytest.raises(YandexAPIError, match="Подключение изменилось"):
            service.yandex._api(123, "/stat/v1/data")


def test_daily_details_keep_original_dates_and_goals(service):
    row = dict(service.db.get_connection(123))
    ctx = service.db.save_report_context(
        123,
        row["generation"],
        {
            "today": "2026-09-02",
            "days": 1,
            "connection": {k: row[k] for k in ("counter_id", "counter_name", "goal_ids")},
        },
    )
    service.db.set_goals(123, [22])
    service.reports.collect = Mock(return_value=sample())
    service.handle_update(callback(f"r:{ctx}:full"))
    service.jobs.executor.shutdown(wait=True)
    call = service.reports.collect.call_args
    assert call.kwargs == {"today": date(2026, 9, 2), "days": 1}
    assert call.args[1]["goal_ids"] == "[11]"
    service.telegram.send_rich_message.assert_called_once()


def test_old_report_is_not_reinterpreted_after_counter_switch(service):
    row = service.db.get_connection(123)
    ctx = service.db.save_report_context(123, row["generation"], {})
    service.db.select_counter(123, 2, "new")
    service.reports.collect = Mock()
    assert not service.send_report(123, context_id=ctx)
    service.reports.collect.assert_not_called()
    assert service.db.report_context(999, ctx) is None


def test_revoked_access_pauses_schedule_and_connect_offers_new_link(service):
    service.reports.collect = Mock(side_effect=YandexAPIError("revoked", reconnect=True))
    row = dict(service.db.get_connection(123))
    today = date.today()
    for _ in range(2):
        service._run_report(123, row, today, 1, False, "scheduled", None)
    assert len(service.db.scheduled_users()) == 0
    assert service.telegram.send_message.call_count == 1
    assert "/connect" in service.telegram.send_message.call_args.args[1]
    service.handle_update(command("/connect"))
    assert any("url" in b for b in buttons(service))


def test_temporary_schedule_error_has_backoff_and_one_notice(service):
    service.reports.collect = Mock(side_effect=YandexAPIError("quota", retry_after=600))
    row = dict(service.db.get_connection(123))
    for _ in range(2):
        service._run_report(123, row, date.today(), 1, False, "scheduled", None)
    user = service.db.get_user(123)
    assert datetime.fromisoformat(user["report_retry_at"]) > datetime.now(timezone.utc) + timedelta(
        seconds=590
    )
    assert not service.db.scheduled_users()
    assert not service.db.get_connection(123)["reauth_required"]
    assert service.telegram.send_message.call_count == 1


class HTMLCheck(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.visible = [], ""

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag

    def handle_data(self, text):
        self.visible += text


def test_fallback_preserves_html_entities_links_and_utf16_limit():
    source = (
        '<b>😀 &amp; &lt;tag&gt; <a href="https://example.test/?x=1&amp;y=2">'
        + "😀 &amp;" * 2000
        + "</a></b>"
    )
    result = fit_html(source)
    parser = HTMLCheck()
    parser.feed(result)
    assert not parser.stack
    assert len(parser.visible.encode("utf-16-le")) // 2 <= 4096
    assert parser.visible.endswith("…")
    assert 'href="https://example.test/?x=1&amp;y=2"' in result
    assert fit_html(result) == result


def test_oversize_rich_report_uses_balanced_plain_html_fallback(service):
    service._send_formatted_report(123, sample(counter_name="😀<&" * 9000), detailed=True)
    service.telegram.send_rich_message.assert_not_called()
    text = service.telegram.send_message.call_args.args[1]
    parser = HTMLCheck()
    parser.feed(text)
    assert not parser.stack
    assert len(parser.visible.encode("utf-16-le")) // 2 <= 4096


def test_database_migration_preserves_existing_connection_and_is_idempotent(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE users(chat_id INTEGER PRIMARY KEY, username TEXT, created_at TEXT NOT NULL, report_enabled INTEGER NOT NULL DEFAULT 1, last_report_key TEXT);
        CREATE TABLE connections(chat_id INTEGER PRIMARY KEY REFERENCES users(chat_id) ON DELETE CASCADE, access_token TEXT NOT NULL, refresh_token TEXT, expires_at TEXT, counter_id INTEGER, counter_name TEXT, goal_ids TEXT NOT NULL DEFAULT '[]', connected_at TEXT NOT NULL);
        INSERT INTO users VALUES (123,'owner','2026-08-01',0,'W-2026-32');
        INSERT INTO connections VALUES (123,'encrypted','refresh',NULL,1,'site','[11,22]','2026-08-01');
        """)
    db = Database(path)
    gen = db.get_connection(123)["generation"]
    db = Database(path)
    assert db.get_connection(123)["generation"] == gen
    assert db.get_connection(123)["goal_ids"] == "[11,22]"
    assert db.get_connection(123)["access_token"] == "encrypted"
    assert db.get_user(123)["report_enabled"] == 0
    assert db.get_user(123)["last_report_key"] == "W-2026-32"


def test_calendar_snapshot_preserves_sources_goals_and_anchor_on_period_switch(service):
    row = dict(service.db.get_connection(123))
    service.db.set_display(123, row["generation"], sources=["organic"], chart=False)
    row = dict(service.db.get_connection(123))
    ctx = service.db.save_report_context(
        123,
        row["generation"],
        {
            "today": "2026-09-23",
            "days": 7,
            "view": "week",
            "connection": {
                k: row[k]
                for k in (
                    "counter_id",
                    "counter_name",
                    "goal_ids",
                    "visible_sources",
                    "chart_enabled",
                )
            },
        },
    )
    service.db.set_display(123, row["generation"], sources=["social"])
    service.db.set_goals(123, [22])
    service.reports.collect = Mock(return_value=sample(source_ids={}))
    assert service.send_report(123, context_id=ctx, view="month", edit_message_id=77)
    service.jobs.executor.shutdown(wait=True)
    call = service.reports.collect.call_args
    assert call.args[1]["goal_ids"] == "[11]"
    assert json.loads(call.args[1]["visible_sources"]) == ["organic"]
    assert call.kwargs["today"] == date(2026, 9, 23)
    assert call.kwargs["periods"] == (
        Period(date(2026, 9, 1), date(2026, 9, 22)),
        Period(date(2026, 8, 1), date(2026, 8, 22)),
    )
    assert service.db.get_connection(123)["report_view"] == "month"
    assert service.db.get_connection(123)["visible_sources"] == '["social"]'
    assert service.telegram.edit_message_text.call_args.args[1] == 77
    service.telegram.send_message.assert_not_called()


def test_source_toggle_and_goal_selection_preserve_each_other(service):
    generation = service.db.get_connection(123)["generation"]
    service.handle_update(callback(f"src:{generation}:social"))
    row = service.db.get_connection(123)
    assert "social" not in json.loads(row["visible_sources"])
    assert "organic" in json.loads(row["visible_sources"])
    assert row["goal_ids"] == "[11]"
    service.handle_update(callback(f"src:{generation}:none"))
    assert service.db.get_connection(123)["visible_sources"] == "[]"
    service.handle_update(callback(f"src:{generation}:all"))
    assert service.db.get_connection(123)["visible_sources"] is None


def test_chart_reuses_summary_and_details_fetch_only_pages(service):
    from metrika_bot.dashboard import collect_dashboard
    from unittest.mock import patch

    calls = []

    def response(chat_id, counter_id, start, end, metrics, dimensions=None, **kwargs):
        calls.append((tuple(metrics), tuple(dimensions or [])))
        if dimensions == ["ym:s:date", "ym:s:trafficSource"]:
            return {"data": [{"dimensions": [{"id": end}, {"id": "organic"}], "metrics": [10]}]}
        if dimensions == ["ym:s:date"]:
            return {"data": [{"dimensions": [{"id": end}], "metrics": [2]}]}
        if dimensions:
            return {"data": [{"dimensions": [{"id": "organic", "name": "Поиск"}], "metrics": [10]}]}
        return {"totals": [10] * len(metrics)}

    service.yandex.report = Mock(side_effect=response)
    service.yandex.goals = Mock(return_value=[{"id": 11, "name": "Форма"}])
    row = dict(service.db.get_connection(123))
    row["chart_enabled"] = 0
    today = date(2026, 9, 23)
    sent = []
    service._send_formatted_report = lambda chat, data, **kw: sent.append(data)
    service._run_report(123, row, today, 7, False, None, None, view="month")
    assert not any("ym:s:startURL" in dims for _, dims in calls)
    assert len(calls) == 8
    row["chart_enabled"] = 1
    service._run_report(123, row, today, 7, False, None, None, view="month")
    assert len(calls) == 10  # Only the two history queries were added.
    service._run_report(123, row, today, 7, False, None, None, view="month")
    assert len(calls) == 10
    assert sent[-1].dashboard.dates and sent[-1].visits.current == 10
    service._run_report(123, row, today, 7, True, None, None, view="month")
    assert len(calls) == 12 and sent[-1].pages_loaded
    assert all(dims == ("ym:s:startURL",) for _, dims in calls[-2:])
    assert service.yandex.goals.call_count == 1
    # Connection removal during an in-flight fetch must not repopulate the cache.
    service.report_cache.drop(123)

    def disconnect(*args, **kwargs):
        result = collect_dashboard(*args, **kwargs)
        service.handle_update(command("/disconnect"))
        return result

    with patch("metrika_bot.bot.collect_dashboard", side_effect=disconnect):
        service._run_report(123, row, today, 7, False, None, None, view="month")
    assert not service.report_cache.entries
    assert len(sent) == 4
