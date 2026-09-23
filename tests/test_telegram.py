from metrika_bot.telegram import TelegramAPI


def test_bot_module_imports():
    from metrika_bot.bot import BotService

    assert BotService is not None


def test_profile_texts_update_name_short_and_full_description(monkeypatch):
    telegram = TelegramAPI("token")
    calls = []

    def fake_call(method, payload=None, timeout=30):
        calls.append((method, payload, timeout))
        return True

    monkeypatch.setattr(telegram, "call", fake_call)
    telegram.set_profile_texts()

    assert [method for method, _, _ in calls] == [
        "setMyName",
        "setMyShortDescription",
        "setMyDescription",
    ]
    description = calls[-1][1]["description"]
    assert "utm_source=telegram" in description
    assert "@eduardtr95" in description
    assert "github.com/eduardtr95/privateseo-metrika-bot" in description


def test_polling_subscribes_to_private_chat_block_events(monkeypatch):
    telegram = TelegramAPI("token")
    captured = {}

    def fake_call(method, payload=None, timeout=30):
        captured.update(method=method, payload=payload, timeout=timeout)
        return []

    monkeypatch.setattr(telegram, "call", fake_call)
    telegram.get_updates(None)

    assert captured["method"] == "getUpdates"
    assert "my_chat_member" in captured["payload"]["allowed_updates"]


def test_chat_action_uses_typing_by_default(monkeypatch):
    telegram = TelegramAPI("token")
    captured = {}

    def fake_call(method, payload=None, timeout=30):
        captured.update(method=method, payload=payload, timeout=timeout)

    monkeypatch.setattr(telegram, "call", fake_call)
    telegram.send_chat_action(123)

    assert captured["method"] == "sendChatAction"
    assert captured["payload"] == {"chat_id": 123, "action": "typing"}


def test_chart_upload_and_period_edit_use_the_same_message(monkeypatch):
    import io
    import json
    import urllib.request

    requests = []

    def open_request(request, timeout):
        requests.append(request)
        return io.BytesIO(json.dumps({"ok": True, "result": {"message_id": 77}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    api = TelegramAPI("test-token")
    api.send_chart(
        123, b"png-data", "<b>Сентябрь</b>", [[{"text": "День", "callback_data": "v:ctx:day"}]]
    )
    api.send_chart(123, b"new-png", "<b>Вчера</b>", [], message_id=77)
    assert requests[0].full_url.endswith("sendPhoto")
    assert requests[1].full_url.endswith("editMessageMedia")
    body = requests[1].data.decode()
    assert 'name="message_id"\r\n\r\n77' in body
    assert '"media": "attach://chart"' in body
    assert "new-png" in body and "Вчера" in body
