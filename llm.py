"""
LLM abstraction: switch between Claude and ChatGPT with a single command.
"""

import anthropic
import openai

import config
import db

_active_llm: str = config.DEFAULT_LLM

SYSTEM_PROMPT = (
    "You are Clawdbot, a personal assistant for a property manager. "
    "You help read and draft emails, monitor property reviews, manage messages, "
    "and fetch information. Be concise, professional, and proactive. "
    "When drafting any outbound content (emails, messages), always present it "
    "as a draft for approval — never imply it has been sent."
)


def get_active_llm() -> str:
    return _active_llm


def switch_llm(to: str) -> str:
    global _active_llm
    to = to.lower().strip()
    if to in ("claude", "anthropic"):
        if not config.ANTHROPIC_API_KEY:
            return "No Anthropic API key configured."
        _active_llm = "claude"
        return "Switched to Claude."
    elif to in ("chatgpt", "openai", "gpt"):
        if not config.OPENAI_API_KEY:
            return "No OpenAI API key configured."
        _active_llm = "chatgpt"
        return "Switched to ChatGPT."
    return f"Unknown LLM '{to}'. Use 'claude' or 'chatgpt'."


async def chat(user_message: str, conversation_history: list[dict] | None = None) -> str:
    await db.save_conversation("user", user_message)

    messages = []
    if conversation_history:
        for msg in conversation_history[-20:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})

    if _active_llm == "claude":
        response = await _call_claude(messages)
    else:
        response = await _call_chatgpt(messages)

    await db.save_conversation("assistant", response, llm_used=_active_llm)
    return response


async def _call_claude(messages: list[dict]) -> str:
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    return response.content[0].text


async def _call_chatgpt(messages: list[dict]) -> str:
    client = openai.AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    all_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    response = await client.chat.completions.create(
        model="gpt-4o",
        max_tokens=1024,
        messages=all_messages,
    )
    return response.choices[0].message.content
