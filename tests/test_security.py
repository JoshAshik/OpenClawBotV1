"""Authorization: multiple Telegram ids, the owner being the first one."""

import pytest

import config
import security


def test_parse_user_ids():
    assert config._parse_user_ids("894000001") == [894000001]
    assert config._parse_user_ids(" 894000001, 930243154 ;930243154 ") == [894000001, 930243154]
    with pytest.raises(RuntimeError):
        config._parse_user_ids("894000001, nope")
    with pytest.raises(RuntimeError):
        config._parse_user_ids(" , ")


def test_is_authorized_uses_list_and_owner_is_first(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_AUTHORIZED_USER_IDS", [894000001, 930243154])
    monkeypatch.setattr(config, "TELEGRAM_AUTHORIZED_USER_ID", 894000001)
    assert security.is_authorized(894000001) and security.is_authorized(930243154)
    assert not security.is_authorized(1) and not security.is_authorized(0)
    assert config.TELEGRAM_AUTHORIZED_USER_ID == config.TELEGRAM_AUTHORIZED_USER_IDS[0]


def test_loaded_config_is_consistent():
    assert isinstance(config.TELEGRAM_AUTHORIZED_USER_IDS, list) and config.TELEGRAM_AUTHORIZED_USER_IDS
    assert config.TELEGRAM_AUTHORIZED_USER_ID == config.TELEGRAM_AUTHORIZED_USER_IDS[0]
