from __future__ import annotations

import ipaddress

import pytest

from autonomous.cli import lan_address, main


def test_lan_address_is_a_real_address():
    address = lan_address()
    ipaddress.ip_address(address)  # raises if it is not one
    assert address != "0.0.0.0"


def test_lan_refuses_to_expose_the_panel_without_a_token(monkeypatch, capsys, tmp_path):
    """--lan without AUTH_TOKEN must not start: it would expose the panel."""
    from autonomous.config import Settings, get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(
        "autonomous.cli.get_settings",
        lambda: Settings(_env_file=None, data_dir=tmp_path, auth_token=None),
    )
    started = False

    def fake_run(*args, **kwargs):
        nonlocal started
        started = True

    monkeypatch.setattr("uvicorn.run", fake_run)

    assert main(["serve", "--lan"]) == 2
    assert started is False
    assert "AUTH_TOKEN" in capsys.readouterr().err


def test_lan_binds_to_every_interface_once_a_token_is_set(monkeypatch, tmp_path):
    from autonomous.config import Settings, get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(
        "autonomous.cli.get_settings",
        lambda: Settings(_env_file=None, data_dir=tmp_path, auth_token="secret", port=8123),
    )
    captured = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)

    assert main(["serve", "--lan"]) == 0
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 8123


def test_plain_serve_stays_on_localhost(monkeypatch, tmp_path):
    from autonomous.config import Settings, get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(
        "autonomous.cli.get_settings",
        lambda: Settings(_env_file=None, data_dir=tmp_path),
    )
    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(kw))

    assert main(["serve"]) == 0
    assert captured["host"] == "127.0.0.1"


def test_unknown_watcher_is_reported(monkeypatch, tmp_path, capsys):
    from autonomous.config import Settings, get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(
        "autonomous.cli.get_settings",
        lambda: Settings(_env_file=None, data_dir=tmp_path),
    )
    assert main(["poll", "nope"]) == 2
    assert "no watcher" in capsys.readouterr().err


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from autonomous.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
