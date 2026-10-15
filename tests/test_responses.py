"""Responses API loop, image generation, spoken replies, citations. No network: every OpenAI
client is a fake; the DB is the temp index_db."""

import base64
from types import SimpleNamespace

import pytest

import agent_tools
import config
import db
import llm
from telegram_format import markdown_to_telegram_html
from tests.conftest import run_async


# ── provider-neutral turns → Responses `input` items ─────────────

def test_to_responses_input_maps_all_turn_kinds_and_keeps_ids():
    turns = [
        {"role": "user", "content": "find the lease"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Looking…"},
            {"type": "tool_use", "id": "call_1", "name": "search_everything", "input": {"query": "lease"}},
            {"type": "tool_use", "id": "call_2", "name": "read_email", "input": {"email_id": "x"}},
        ]},
        {"role": "tool_results", "content": [
            {"tool_use_id": "call_1", "content": "r1"}, {"tool_use_id": "call_2", "content": "r2"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Here it is."}]},
        {"role": "assistant", "content": []},
    ]
    items = llm._to_responses_input(turns)
    assert items[0] == {"role": "user", "content": "find the lease"}
    assert items[1] == {"role": "assistant", "content": "Looking…"}
    assert items[2] == {"type": "function_call", "call_id": "call_1", "name": "search_everything",
                        "arguments": '{"query": "lease"}'}
    assert items[3]["call_id"] == "call_2"
    assert items[4] == {"type": "function_call_output", "call_id": "call_1", "output": "r1"}
    assert items[5] == {"type": "function_call_output", "call_id": "call_2", "output": "r2"}
    assert items[6] == {"role": "assistant", "content": "Here it is."}
    assert len(items) == 7  # the empty assistant turn adds nothing


# ── the streaming loop with a fake responses client ──────────────

def _usage(inp, cached, out):
    return {"input_tokens": inp, "output_tokens": out, "input_tokens_details": {"cached_tokens": cached}}


class FakeResponses:
    """Round 1: narration + a web search + one function call. Round 2: the final answer."""

    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kw):
        self.calls.append(kw)
        n = len(self.calls)

        async def events():
            if n == 1:
                yield SimpleNamespace(type="response.output_text.delta", delta="Checking")
                yield SimpleNamespace(type="response.output_text.delta", delta=" the lease…")
                yield SimpleNamespace(type="response.output_item.done", item=SimpleNamespace(
                    type="web_search_call", action=SimpleNamespace(query="lease law")))
                yield SimpleNamespace(type="response.output_item.added", item=SimpleNamespace(
                    type="function_call", call_id="call_a", name="search_everything", arguments=""))
                yield SimpleNamespace(type="response.function_call_arguments.done",
                                      item_id="fc_a", arguments='{"query": "lease"}')
                yield SimpleNamespace(type="response.completed", response=SimpleNamespace(
                    id="resp_1", usage=_usage(100, 40, 5),
                    output=[
                        SimpleNamespace(type="reasoning"),
                        SimpleNamespace(type="web_search_call"),
                        SimpleNamespace(type="message", content=[SimpleNamespace(
                            type="output_text", text="Checking the lease…", annotations=[])]),
                        SimpleNamespace(type="function_call", call_id="call_a",
                                        name="search_everything", arguments='{"query": "lease"}'),
                    ]))
            else:
                yield SimpleNamespace(type="response.output_text.delta", delta="Found it")
                yield SimpleNamespace(type="response.output_text.annotation.added", annotation={
                    "type": "url_citation", "url": "https://example.com/lease", "title": "Lease",
                    "start_index": 0, "end_index": 8})
                yield SimpleNamespace(type="response.completed", response=SimpleNamespace(
                    id="resp_2", usage=_usage(200, 0, 10), output=[]))

        return events()


def test_responses_loop_streams_chains_and_matches_chat_shape(index_db, monkeypatch):
    monkeypatch.setitem(config.PRICES_PER_MTOK, config.OPENAI_MODEL, {"in": 1.0, "cached": 0.0, "out": 1.0})
    monkeypatch.setattr(config, "OPENAI_API_MODE", "responses")
    monkeypatch.setattr(config, "OPENAI_REASONING_EFFORT", "low")
    monkeypatch.setattr(config, "WEB_SEARCH_ENABLED", True)
    fake = FakeResponses()
    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(responses=fake))

    async def fake_execute(name, args):
        assert (name, args) == ("search_everything", {"query": "lease"})
        return {"result": "lease found", "entities": [{"type": "gmail", "id": "e1", "label": "Lease"}]}

    monkeypatch.setattr(llm, "execute_tool", fake_execute)

    class Progress:
        texts: list[str] = []
        tools: list[tuple] = []

        async def on_text(self, t):
            self.texts.append(t)

        async def on_tool(self, name, args):
            self.tools.append((name, args))

    progress = Progress()
    history = [{"role": "user", "content": "hi"}]
    result, turn, entities, totals = run_async(llm._openai_agent_loop("sys", history, progress))

    # same shape as the chat loop
    assert result["actions"] == [] and result["files"] == []
    assert result["text"] == "Found it ([example.com](https://example.com/lease))"
    assert turn == [
        {"role": "assistant", "content": [
            {"type": "text", "text": "Checking the lease…"},
            {"type": "tool_use", "id": "call_a", "name": "search_everything", "input": {"query": "lease"}}]},
        {"role": "tool_results", "content": [{"tool_use_id": "call_a", "content": "lease found"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Found it"}]},
    ]
    assert entities == [{"type": "gmail", "id": "e1", "label": "Lease"}]
    assert totals == {"prompt": 300, "cached": 40, "completion": 15, "cost": pytest.approx(275 / 1e6)}

    # streaming + progress
    assert progress.texts == ["Checking", "Checking the lease…", "Found it"]
    assert progress.tools == [("web_search", {"query": "lease law"}), ("search_everything", {"query": "lease"})]

    # request shape and previous_response_id chaining
    first, second = fake.calls
    assert first["instructions"] == "sys" and first["stream"] is True and first["store"] is True
    assert first["reasoning"] == {"effort": "low"} and "previous_response_id" not in first
    assert first["input"] == [{"role": "user", "content": "hi"}]
    assert {"type": "web_search"} in first["tools"]
    assert any(t.get("name") == "search_everything" for t in first["tools"])
    assert second["previous_response_id"] == "resp_1"
    assert second["input"] == [{"type": "function_call_output", "call_id": "call_a", "output": "lease found"}]

    rows = run_async(db.usage_summary("2000-01-01T00:00:00+00:00"))
    assert rows["calls"] == 2 and rows["by_purpose"][0]["key"] == "agent"


def test_responses_loop_failed_event_raises(index_db, monkeypatch):
    class Failing:
        async def create(self, **kw):
            async def events():
                yield SimpleNamespace(type="response.failed", response=SimpleNamespace(
                    error=SimpleNamespace(message="server exploded")))
            return events()

    monkeypatch.setattr(config, "OPENAI_API_MODE", "responses")
    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(responses=Failing()))
    with pytest.raises(RuntimeError, match="server exploded"):
        run_async(llm._openai_agent_loop("sys", [{"role": "user", "content": "hi"}], None))


def test_chat_mode_routes_to_old_loop(monkeypatch):
    seen = {}

    async def fake_chat(system, messages, progress):
        seen["args"] = (system, messages)
        return {"text": "old", "actions": [], "files": []}, [], [], {}

    async def fake_responses(system, messages, progress):
        raise AssertionError("responses loop must not run in chat mode")

    monkeypatch.setattr(llm, "_chat_agent_loop", fake_chat)
    monkeypatch.setattr(llm, "_responses_agent_loop", fake_responses)
    monkeypatch.setattr(config, "OPENAI_API_MODE", "chat")
    result, *_ = run_async(llm._openai_agent_loop("sys", [{"role": "user", "content": "x"}], None))
    assert result["text"] == "old" and seen["args"] == ("sys", [{"role": "user", "content": "x"}])

    monkeypatch.setattr(llm, "_responses_agent_loop", fake_chat)
    monkeypatch.setattr(config, "OPENAI_API_MODE", "responses")
    result, *_ = run_async(llm._openai_agent_loop("sys", [], None))
    assert result["text"] == "old"


# ── tool schemas in Responses format ─────────────────────────────

def test_responses_tools_strict_only_when_every_property_is_required():
    tools = {t["name"]: t for t in agent_tools.get_responses_tools()}
    assert all(t["type"] == "function" and "parameters" in t for t in tools.values())
    assert tools["search_everything"]["strict"] is False          # optional filters
    assert tools["get_index_stats"]["strict"] is True             # no properties at all
    strict_ones = [t for t in tools.values() if t["strict"]]
    assert strict_ones
    for t in strict_ones:
        p = t["parameters"]
        assert p.get("additionalProperties") is False
        assert set(p.get("required") or []) == set(p.get("properties") or {})
    assert agent_tools._strict_schema({"type": "object", "properties": {
        "a": {"type": "object", "properties": {"x": {"type": "string"}}, "required": []}},
        "required": ["a"]}) is None                               # nested optional → not strict
    # the originals are untouched
    assert "additionalProperties" not in next(
        t for t in agent_tools.TOOL_SCHEMAS if t["name"] == "get_index_stats")["input_schema"]


# ── image generation ──────────────────────────────────────────────

def test_generate_image_tool_returns_files_and_records_usage(index_db, monkeypatch):
    png = b"\x89PNG\r\n\x1a\nfake"
    captured = {}

    class FakeImages:
        async def generate(self, **kw):
            captured.update(kw)
            return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(png).decode())],
                                   usage={"input_tokens": 20, "output_tokens": 1000})

    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(images=FakeImages()))
    monkeypatch.setitem(config.PRICES_PER_MTOK, config.IMAGE_MODEL, {"per_image": 0.04})

    out = run_async(agent_tools.execute_tool("generate_image", {"prompt": "a cosy cottage", "size": "landscape"}))
    assert out["files"] == [{"kind": "photo", "bytes": png, "filename": "image.png", "caption": "a cosy cottage"}]
    assert "sent to the user as a photo" in out["result"]
    assert captured["prompt"] == "a cosy cottage" and captured["size"] == "1536x1024" and captured["n"] == 1

    rows = run_async(db.usage_summary("2000-01-01T00:00:00+00:00"))
    assert rows["calls"] == 1 and rows["by_purpose"][0]["key"] == "image"
    assert rows["cost"] == pytest.approx(0.04)
    assert llm.usage_cost("gpt-image-1", llm.usage_dict({"images": 3})) == (pytest.approx(0.12), True)
    assert llm.image_size("portrait") == "1024x1536" and llm.image_size("huge") == "1024x1024"


def test_run_tools_collects_files_into_sink(monkeypatch):
    async def fake_execute(name, args):
        return {"result": "ok", "files": [{"kind": "photo", "bytes": b"x", "filename": "i.png", "caption": "c"}]}

    monkeypatch.setattr(llm, "execute_tool", fake_execute)
    sink: list = []
    results, actions, entities = run_async(
        llm._run_tools([{"id": "1", "name": "generate_image", "input": {"prompt": "p"}}], None, files=sink))
    assert results == [{"tool_use_id": "1", "content": "ok"}] and actions == [] and entities == []
    assert sink == [{"kind": "photo", "bytes": b"x", "filename": "i.png", "caption": "c"}]
    # without a sink the signature behaves exactly as before
    assert len(run_async(llm._run_tools([{"id": "1", "name": "x", "input": {}}], None))) == 3


# ── spoken replies ────────────────────────────────────────────────

@pytest.mark.parametrize("mode, incoming_voice, say_it, expected", [
    ("auto", False, False, False),
    ("auto", True, False, True),
    ("auto", False, True, True),
    ("on", False, False, True),
    ("on", True, False, True),
    ("off", False, False, False),
    ("off", True, False, False),
    ("off", False, True, True),    # an explicit "say it" always wins
])
def test_voice_reply_decision_table(mode, incoming_voice, say_it, expected):
    assert llm.should_speak(mode, incoming_voice, say_it) is expected


def test_wants_spoken_reply_phrases():
    assert llm.wants_spoken_reply("What's the weather? Say it out loud")
    assert llm.wants_spoken_reply("read it to me")
    assert llm.wants_spoken_reply("Reply by voice please")
    assert not llm.wants_spoken_reply("what did Mike say about the boiler")
    assert not llm.wants_spoken_reply("")


def test_voice_mode_setting_roundtrip(index_db):
    async def go():
        assert await llm.voice_mode() == "auto"
        await db.set_setting(llm.VOICE_SETTING, "on")
        assert await llm.voice_mode() == "on"
        await db.set_setting(llm.VOICE_SETTING, "bogus")
        return await llm.voice_mode()

    assert run_async(go()) == "auto"


def test_strip_markdown_and_speech_text(monkeypatch):
    md = ("## Rent update\n\n**Mike** paid *late*: `$1,200`.\n- see [the thread](https://mail.example.com/x)\n"
          "- raw https://example.com/y link\n> quoted\n```\ncode here\n```\n~~gone~~ done_now_ok")
    plain = llm.strip_markdown(md)
    assert plain == ("Rent update\n\nMike paid late: $1,200.\nsee the thread\nraw link\nquoted\ncode here\n"
                     "gone done_now_ok")
    assert "http" not in plain and "*" not in plain and "`" not in plain

    monkeypatch.setattr(config, "VOICE_OUT_MAX_CHARS", 1500)
    long = ("This is a sentence that keeps going on. " * 60).strip()
    spoken = llm.speech_text(long)
    assert len(spoken) <= 1500 and spoken.endswith(".") and len(spoken) > 700
    assert llm.speech_text("short **answer**") == "short answer"
    assert llm.speech_text("x" * 50, max_chars=20) == "x" * 20


def test_synthesize_speech_records_characters(index_db, monkeypatch):
    captured = {}

    class FakeSpeech:
        async def create(self, **kw):
            captured.update(kw)
            return SimpleNamespace(content=b"OggS-audio")

    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(audio=SimpleNamespace(speech=FakeSpeech())))
    monkeypatch.setitem(config.PRICES_PER_MTOK, config.VOICE_OUT_MODEL, {"in": 15.0, "cached": 15.0, "out": 0.0})
    audio = run_async(llm.synthesize_speech("**Hello** there"))
    assert audio == b"OggS-audio"
    assert captured["input"] == "Hello there" and captured["response_format"] == "opus"
    assert captured["model"] == config.VOICE_OUT_MODEL and captured["voice"] == config.VOICE_OUT_VOICE
    rows = run_async(db.usage_summary("2000-01-01T00:00:00+00:00"))
    assert rows["by_purpose"][0]["key"] == "tts" and rows["cost"] == pytest.approx(11 * 15 / 1e6)
    assert run_async(llm.synthesize_speech("   ")) == b""


# ── citations ─────────────────────────────────────────────────────

def test_render_citations_inline_footer_dedupe_and_html():
    text = "Rates rose again this week. Markets expect a pause."
    anns = [
        {"type": "url_citation", "url": "https://www.reuters.com/a", "title": "Reuters", "start_index": 0, "end_index": 10},
        {"type": "url_citation", "url": "https://www.reuters.com/a", "title": "Reuters", "start_index": 0, "end_index": 10},
        {"type": "url_citation", "url": "https://ft.com/b", "title": "FT", "start_index": None, "end_index": None},
        {"type": "file_citation", "file_id": "f1"},
    ]
    out = llm.render_citations(text, anns)
    assert out == ("Rates rose ([reuters.com](https://www.reuters.com/a)) again this week. Markets expect a pause."
                   "\n\nSources: [ft.com](https://ft.com/b)")
    html = markdown_to_telegram_html(out)
    assert '<a href="https://www.reuters.com/a">reuters.com</a>' in html
    assert '<a href="https://ft.com/b">ft.com</a>' in html

    # a url the model already wrote is not added twice; no annotations → unchanged
    already = "See [Reuters](https://www.reuters.com/a)."
    assert llm.render_citations(already, anns[:1]) == already
    assert llm.render_citations(text, []) == text

    # SDK-style objects work too
    obj = SimpleNamespace(type="url_citation", url="https://x.org/p", title="X", start_index=0, end_index=5)
    assert llm.render_citations("Hello world", [obj]) == "Hello ([x.org](https://x.org/p)) world"
