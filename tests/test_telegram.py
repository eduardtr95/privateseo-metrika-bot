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
