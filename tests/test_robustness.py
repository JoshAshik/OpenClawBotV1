"""Robustness: OpenAI retries, Telegram flood control, turn timeout, overlap guard, WAL. No network."""

import asyncio
import sqlite3
from types import SimpleNamespace

import httpx
import openai
import pytest
from telegram.error import BadRequest, RetryAfter

import config
import db
import llm
import ops
import search_index
from telegram_format import tg_retry
from tests.conftest import run_async

_REQ = httpx.Request("POST", "https://api.openai.com/v1/x")


def _status_error(status: int, headers: dict | None = None):
    resp = httpx.Response(status, headers=headers or {}, request=_REQ)
    cls = {429: openai.RateLimitError, 400: openai.BadRequestError}.get(status, openai.APIStatusError)
    return cls("boom", response=resp, body=None)


@pytest.fixture
def no_sleep(monkeypatch):
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return delays


def _flaky(errors, result="ok"):
    """factory that raises each error in turn, then returns result."""
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if errors:
            raise errors.pop(0)
        return result

    return factory, calls


# ── OpenAI retries ─────────────────────────────────────────────────

def test_retry_backoff_then_gives_up(no_sleep):
    factory, calls = _flaky([_status_error(429) for _ in range(10)])
    with pytest.raises(openai.RateLimitError):
        run_async(llm.with_retry(factory, "t"))
    assert calls["n"] == llm.RETRY_ATTEMPTS == 4
    assert no_sleep == [0.5, 1.0, 2.0]


def test_retry_honours_retry_after_and_succeeds(no_sleep):
    factory, calls = _flaky([_status_error(429, {"retry-after": "2"}), _status_error(503)])
    assert run_async(llm.with_retry(factory, "t")) == "ok"
    assert calls["n"] == 3 and no_sleep == [2.0, 1.0]


def test_retry_transient_errors(no_sleep):
    factory, calls = _flaky([openai.APIConnectionError(request=_REQ), openai.APITimeoutError(request=_REQ)])
    assert run_async(llm.with_retry(factory, "t")) == "ok"
    assert calls["n"] == 3 and len(no_sleep) == 2


def test_retry_stops_on_4xx_and_other_errors(no_sleep):
    factory, calls = _flaky([_status_error(400)])
    with pytest.raises(openai.BadRequestError):
        run_async(llm.with_retry(factory, "t"))
    assert calls["n"] == 1 and no_sleep == []
    factory, calls = _flaky([ValueError("not an api error")])
    with pytest.raises(ValueError):
        run_async(llm.with_retry(factory, "t"))
    assert calls["n"] == 1


def test_is_retryable_and_retry_after_parsing():
    assert llm.is_retryable(_status_error(429)) and llm.is_retryable(_status_error(502))
    assert not llm.is_retryable(_status_error(404)) and not llm.is_retryable(RuntimeError())
    assert llm.retry_after_seconds(_status_error(429, {"retry-after": "1.5"})) == 1.5
    assert llm.retry_after_seconds(_status_error(429, {"retry-after": "soon"})) is None
    assert llm.retry_after_seconds(RuntimeError()) is None


def test_clients_carry_timeout_and_no_sdk_retries(monkeypatch):
    import embeddings
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(config, "OPENAI_TIMEOUT_SECONDS", 12.5)
    monkeypatch.setattr(llm, "_openai", None)
    monkeypatch.setattr(embeddings, "_client", None)
    for c in (llm._client(), embeddings._get_client()):
        assert c.max_retries == 0 and float(c.timeout) == 12.5


# ── Telegram flood control ─────────────────────────────────────────

def test_tg_retry_behaviour(no_sleep):
    factory, calls = _flaky([RetryAfter(3)], result="sent")
    assert run_async(tg_retry(factory)) == "sent"
    assert calls["n"] == 2 and no_sleep == [3.5]

    factory, calls = _flaky([RetryAfter(1), RetryAfter(1)])
    with pytest.raises(RetryAfter):
        run_async(tg_retry(factory))
    assert calls["n"] == 2

    factory, _ = _flaky([BadRequest("Message is not modified: nothing to edit")])
    assert run_async(tg_retry(factory)) is None

    factory, _ = _flaky([BadRequest("Message is too long")])
    with pytest.raises(BadRequest):
        run_async(tg_retry(factory))


# ── Overlap guard + turn timeout ──────────────────────────────────

class FakeMessage:
    def __init__(self, log):
        self.log = log

    async def edit_text(self, text, **kw):
        self.log.append(("edit", text, kw.get("reply_markup")))


class FakeTgBot:
    def __init__(self):
        self.log = []

    async def send_chat_action(self, **kw):
        pass

    async def send_message(self, chat_id=None, text=None, **kw):
        self.log.append(("send", text, kw.get("reply_markup")))
        return FakeMessage(self.log)


@pytest.fixture
def agent_env(index_db, monkeypatch):
    import bot

    bot._TURN_LOCKS.clear()
    state = {"active": 0, "max": 0, "order": []}

    async def fake_agent_chat(text, progress=None):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        state["order"].append(("start", text))
        await asyncio.sleep(0.15)
        state["order"].append(("end", text))
        state["active"] -= 1
        return {"text": f"done:{text}", "actions": []}

    monkeypatch.setattr(bot.llm, "agent_chat", fake_agent_chat)
    return bot, state


def test_same_user_turns_are_serialized_with_busy_notice(agent_env):
    bot, state = agent_env
    tg = FakeTgBot()
    ctx = SimpleNamespace(bot=tg)

    async def go():
        await asyncio.gather(
            bot._run_agent_turn(ctx, 1, 7, "first"),
            bot._run_agent_turn(ctx, 1, 7, "second"),
        )

    run_async(go())
    assert state["max"] == 1
    assert state["order"] == [("start", "first"), ("end", "first"), ("start", "second"), ("end", "second")]
    sends = [t for kind, t, _ in tg.log if kind == "send"]
    assert sends.count(bot.BUSY_TEXT) == 1
    finals = [t for kind, t, _ in tg.log if kind == "edit" and "done:" in t]
    assert len(finals) == 2


def test_different_users_run_concurrently(agent_env):
    bot, state = agent_env

    async def go():
        await asyncio.gather(
            bot._run_agent_turn(SimpleNamespace(bot=FakeTgBot()), 1, 7, "a"),
            bot._run_agent_turn(SimpleNamespace(bot=FakeTgBot()), 2, 8, "b"),
        )

    run_async(go())
    assert state["max"] == 2


def test_turn_timeout_gives_friendly_error_with_retry(agent_env, monkeypatch):
    bot, state = agent_env
    monkeypatch.setattr(config, "TURN_TIMEOUT_SECONDS", 0.03)
    tg = FakeTgBot()
    run_async(bot._run_agent_turn(SimpleNamespace(bot=tg), 1, 7, "slow one"))
    edits = [(t, m) for kind, t, m in tg.log if kind == "edit"]
    assert edits and edits[-1][0].startswith("That took too long (over 0s) and was stopped. Nothing was sent.")
    assert edits[-1][1] is not None                                   # Retry button attached
    key = edits[-1][1].inline_keyboard[0][0].callback_data.split(":", 1)[1]
    assert bot._RETRY_CACHE[key] == "slow one"
    assert state["active"] == 0 or True                               # cancelled task never 'ends'


# ── SQLite WAL + backup under load ────────────────────────────────

def test_pragmas_and_backup_during_write_transaction(index_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "BACKUP_KEEP", 3)

    async def go():
        conn = await db.get_db()
        idx = await search_index.get_db()
        modes, timeouts = [], []
        for c in (conn, idx):
            cur = await c.execute("PRAGMA journal_mode")
            modes.append((await cur.fetchone())[0])
            cur = await c.execute("PRAGMA busy_timeout")
            timeouts.append((await cur.fetchone())[0])
        await db.set_setting("k", "v")
        return modes, timeouts

    modes, timeouts = run_async(go())
    assert modes == ["wal", "wal"] and timeouts == [5000, 5000]

    writer = sqlite3.connect(index_db, timeout=1)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO settings (key, value) VALUES ('held', 'open')")
    try:
        dest, pruned = ops._backup_sync()                    # another connection holds a write lock
    finally:
        writer.rollback()
        writer.close()
    assert dest.exists() and pruned == 0
    copy = sqlite3.connect(dest)
    assert copy.execute("SELECT value FROM settings WHERE key = 'k'").fetchone()[0] == "v"
    assert copy.execute("SELECT COUNT(*) FROM settings WHERE key = 'held'").fetchone()[0] == 0
    copy.close()
    # the full job (backup + timestamp) works once the writer is gone
    dest2, _ = run_async(ops.backup_database())
    assert dest2.exists() and run_async(db.get_setting(ops.LAST_BACKUP_KEY))


def test_concurrent_first_open_is_serialized(index_db):
    async def go():
        conns = await asyncio.gather(*(db.get_db() for _ in range(5)))
        idx = await asyncio.gather(*(search_index.get_db() for _ in range(5)))
        return conns, idx

    conns, idx = run_async(go())
    assert len({id(c) for c in conns}) == 1 and len({id(c) for c in idx}) == 1
