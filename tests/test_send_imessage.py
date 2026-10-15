"""send_imessage: tool → pending action → approval card → executor. No network, no osascript."""

import pytest

import agent_tools
import imessage_send
from tests.conftest import run_async

MIKE = {"name": "Mike Plumber", "emails": ["mike@plumbing.example"], "phones": ["+14155550101"],
        "channels": ["contacts", "imessage"], "count": 40, "last_seen": "2026-10-10"}
MIKE2 = {"name": "Mike Painter", "emails": [], "phones": ["+14155550177"],
         "channels": ["imessage"], "count": 3, "last_seen": "2026-01-01"}
SARA = {"name": "Sara Lee", "emails": ["sara@example.com"],
        "phones": ["+14155550102", "+14155550199"], "channels": ["contacts"], "count": 0}
PAT = {"name": "Pat Tenant", "emails": ["tenant@example.com"], "phones": [],
       "channels": ["gmail", "imessage"], "count": 9, "last_seen": "2026-09-01"}
MAILONLY = {"name": "Mail Only", "emails": ["mail@example.com"], "phones": [],
            "channels": ["gmail"], "count": 2}


@pytest.fixture
def contacts(index_db, monkeypatch):
    """Seed the contacts table in the temp DB and make any real send an error."""
    import db

    def _boom(*a, **k):
        raise AssertionError("send_message must never run from a tool")

    monkeypatch.setattr(imessage_send, "send_message", _boom)
    run_async(db.replace_contacts([MIKE, MIKE2, SARA, PAT, MAILONLY]))
    return index_db


def _tool(args: dict) -> dict:
    return run_async(agent_tools.execute_tool("send_imessage", args))


# ── Tool returns an action, never sends ───────────────────────────

def test_name_resolves_to_single_phone_and_returns_action(contacts):
    out = _tool({"to": "Sara", "body": "Plumber is coming Tuesday at 9."})
    action = out["action"]
    assert action["type"] == "send_imessage"
    assert action["payload"] == {"to": "+14155550102", "to_name": "Sara Lee",
                                 "body": "Plumber is coming Tuesday at 9.", "service": "iMessage"}
    assert action["summary"] == "Text to Sara Lee"
    assert "Awaiting user approval" in out["result"]
    assert "+14155550199" in out["result"]          # the alternate number is mentioned
    assert "sent" not in out["result"].lower().replace("not sent", "")


def test_handle_given_directly_is_normalized_and_named(contacts):
    out = _tool({"to": "(415) 555-0101", "body": "hi", "service": "SMS"})
    p = out["action"]["payload"]
    assert p["to"] == "+14155550101" and p["to_name"] == "Mike Plumber" and p["service"] == "SMS"
    unknown = _tool({"to": "+14155559999", "body": "hi"})
    assert unknown["action"]["payload"] == {"to": "+14155559999", "to_name": "", "body": "hi",
                                            "service": "iMessage"}


def test_imessage_email_handle_used_when_no_phone(contacts):
    out = _tool({"to": "Pat Tenant", "body": "Got your note"})
    assert out["action"]["payload"]["to"] == "tenant@example.com"


def test_ambiguous_name_asks_instead_of_acting(contacts):
    out = _tool({"to": "Mike", "body": "hi"})
    assert "action" not in out
    assert "Mike Plumber" in out["result"] and "Mike Painter" in out["result"]
    assert "Ask the user which" in out["result"]


def test_contact_without_handle_or_unknown_asks_for_number(contacts):
    out = _tool({"to": "Mail Only", "body": "hi"})
    assert "action" not in out and "Ask the user for the number" in out["result"]
    out = _tool({"to": "Zed Nobody", "body": "hi"})
    assert "action" not in out and "No contact matching" in out["result"]


def test_missing_fields(contacts):
    assert "action" not in _tool({"to": "", "body": "hi"})
    assert "action" not in _tool({"to": "Sara", "body": "  "})


# ── Card, edit lock, schemas ──────────────────────────────────────

def test_action_detail_and_recipient_label():
    payload = {"to": "+14155550101", "to_name": "Mike Plumber", "body": "On my way", "service": "iMessage"}
    text = agent_tools.action_detail("send_imessage", payload)
    assert text.startswith("*To:* Mike Plumber (+14155550101) via iMessage")
    assert text.endswith("On my way")
    assert agent_tools.message_recipient_label({"to": "+1", "to_name": ""}) == "+1"


def test_edit_loop_cannot_change_recipient():
    import llm
    assert llm._EDITABLE_FIELDS["send_imessage"] == ("body",)


def test_schemas_offer_imessage_but_not_whatsapp():
    names = {t["name"] for t in agent_tools.TOOL_SCHEMAS}
    assert "send_imessage" in names and "send_whatsapp" not in names
    desc = next(t for t in agent_tools.TOOL_SCHEMAS if t["name"] == "send_imessage")["description"]
    assert "NEVER say it was sent" in desc


# ── AppleScript command ───────────────────────────────────────────

def test_build_command_passes_body_as_argv_not_script():
    body = 'say "hi"\n-- end tell\n$(rm -rf /)'
    cmd = imessage_send.build_command("+14155550101", body, "sms")
    assert cmd[:2] == ["osascript", "-e"]
    assert cmd[2] == imessage_send.APPLESCRIPT and body not in cmd[2]
    assert cmd[3:] == ["+14155550101", body, "SMS"]
    with pytest.raises(ValueError):
        imessage_send.build_command("", "hi")
    with pytest.raises(ValueError):
        imessage_send.build_command("+1", "   ")
    assert imessage_send.normalize_service(None) == "iMessage"


def test_send_message_refuses_off_mac(monkeypatch):
    monkeypatch.setattr(imessage_send, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="needs the Mac"):
        imessage_send.send_message("+1", "hi")


# ── Executor (only path that may send) ───────────────────────────

def _approved(payload: dict) -> dict:
    return {"id": 1, "action_type": "send_imessage", "payload": payload}


def test_execute_approved_action_without_mac_reports_and_audits(index_db, monkeypatch):
    import bot
    import db

    monkeypatch.setattr(imessage_send, "is_available", lambda: False)
    sent = []
    monkeypatch.setattr(imessage_send, "send_message", lambda *a, **k: sent.append(a))

    async def go():
        text = await bot.execute_approved_action(_approved(
            {"to": "+14155550101", "to_name": "Mike Plumber", "body": "hi", "service": "iMessage"}))
        conn = await db.get_db()
        cur = await conn.execute("SELECT summary, approved FROM audit_log WHERE action_type = 'send_imessage'")
        return text, await cur.fetchall()

    text, rows = run_async(go())
    assert "needs the Mac" in text and "Nothing was sent" in text
    assert sent == []
    assert len(rows) == 1 and rows[0]["approved"] == 0


def test_execute_approved_action_sends_when_available(index_db, monkeypatch):
    import bot
    import db

    monkeypatch.setattr(imessage_send, "is_available", lambda: True)
    calls = []
    monkeypatch.setattr(imessage_send, "send_message", lambda h, b, s: calls.append((h, b, s)) or "")

    async def go():
        ok = await bot.execute_approved_action(_approved(
            {"to": "+14155550101", "to_name": "Mike Plumber", "body": "hi", "service": "SMS"}))
        monkeypatch.setattr(imessage_send, "send_message",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("not allowed (-1743)")))
        failed = await bot.execute_approved_action(_approved(
            {"to": "+14155550101", "to_name": "", "body": "hi", "service": "iMessage"}))
        conn = await db.get_db()
        cur = await conn.execute("SELECT approved FROM audit_log WHERE action_type = 'send_imessage' ORDER BY id")
        return ok, failed, [r["approved"] for r in await cur.fetchall()]

    ok, failed, audited = run_async(go())
    assert ok == "SMS sent to Mike Plumber (+14155550101)."
    assert calls == [("+14155550101", "hi", "SMS")]
    assert failed.startswith("iMessage send failed: not allowed")
    assert audited == [1, 0]


def test_whatsapp_send_is_gone():
    import bot
    out = run_async(bot.execute_approved_action({"id": 2, "action_type": "send_whatsapp", "payload": {}}))
    assert out.startswith("Unknown action type")
