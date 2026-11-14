import pytest

from bot.actions import apps
from bot.actions.registry import REGISTRY, run_action

FAKE = [apps.App("Brave", r"C:\SM\Brave.lnk", "lnk"),
        apps.App("Brave Browser Beta", r"C:\SM\Brave Beta.lnk", "lnk"),
        apps.App("Google Chrome", r"C:\SM\Google Chrome.lnk", "lnk"),
        apps.App("Notepad", "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App", "uwp"),
        apps.App("Spotify", "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", "uwp"),
        apps.App("Visual Studio Code", r"C:\SM\VS Code.lnk", "lnk"),
        apps.App("WhatsApp", "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App", "uwp")]


@pytest.fixture
def launched(monkeypatch):
    calls = []
    monkeypatch.setattr(apps, "installed_apps", lambda refresh=False: FAKE)
    monkeypatch.setattr(apps, "_launch", lambda app: calls.append(app.name))
    return calls


@pytest.mark.parametrize("query,expected", [
    ("brave", "Brave"), ("Brave", "Brave"), ("chrome", "Google Chrome"), ("notepad", "Notepad"),
    ("spotify", "Spotify"), ("whatsapp", "WhatsApp"), ("visual studio code", "Visual Studio Code"),
    ("spotfy", "Spotify"), ("whats app", "WhatsApp"),
])
def test_find_app(query, expected):
    app, _ = apps.find_app(query, FAKE)
    assert app is not None and app.name == expected


def test_find_app_unknown_returns_none():
    app, _ = apps.find_app("photoshop", FAKE)
    assert app is None


async def test_open_app_launches_best_match(launched):
    res = await run_action("open_app", name="brave")
    assert res.ok and launched == ["Brave"] and "Opened Brave" in res.text


async def test_open_app_unknown_is_clear_failure(launched):
    res = await run_action("open_app", name="photoshop")
    assert not res.ok and launched == [] and "couldn't find" in res.text


async def test_list_apps_filter(launched):
    res = await run_action("list_apps", query="brave")
    assert res.ok and "Brave" in res.text and "Spotify" not in res.text


def test_registered_and_not_dangerous():
    for name in ("open_app", "list_apps"):
        assert name in REGISTRY and not REGISTRY[name].dangerous


def test_uninstallers_are_skipped():
    assert apps._SKIP.search("Uninstall Brave")
    assert not apps._SKIP.search("Brave")
