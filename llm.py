"""
LLM agent — routes natural language to tool calls automatically.

Supports Claude (tool_use) and OpenAI (function calling).
Read operations execute immediately; write operations return pending actions
for the approval flow in bot.py.
"""

import json
import logging

import anthropic
import openai

import config
import db
from agent_tools import TOOL_SCHEMAS, execute_tool, get_openai_tools

logger = logging.getLogger(__name__)

_active_llm: str = config.DEFAULT_LLM

SYSTEM_PROMPT = (
    "You are Clawdbot, a personal AI assistant for a property manager.\n\n"
    "You have tools to search emails, read documents, check reviews, and more. "
    "When the user asks a question, USE your tools — don't say you can't access something.\n\n"
    "CRITICAL RULES:\n"
    "1. ALWAYS start with search_everything to find documents, emails, PDFs, or messages. "
    "NEVER guess or make up an email ID — you don't know any IDs until you search.\n"
    "2. search_everything returns full content for top results. Read that content to answer "
    "the user's question. If you need more, use read_indexed_document with the exact source "
    "and source_id from the search results.\n"
    "3. Use list_recent_emails only when the user wants to see their inbox, not to find specific content.\n"
    "4. For sending emails: use draft_email. It will be shown for approval — never say it was sent.\n"
    "5. Be concise. Summarize results in plain language.\n"
    "6. For property reviews: use check_reviews or list_properties.\n"
)

MAX_TOOL_ROUNDS = 6


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


async def agent_chat(user_message: str, conversation_history: list[dict] | None = None) -> dict:
    """
    Run the agent loop. Returns:
      {"text": "...", "actions": [...]}
    where actions is a list of write-actions needing user approval (may be empty).
    """
    await db.save_conversation("user", user_message)

    messages = []
    if conversation_history:
        for msg in conversation_history[-20:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})

    if _active_llm == "claude":
        result = await _claude_agent_loop(messages)
    else:
        result = await _openai_agent_loop(messages)

    # Save response + tool context so follow-up turns have real IDs
    save_text = result["text"]
    if result.get("tool_context"):
        save_text += "\n\n[Tool data: " + result["tool_context"] + "]"
    await db.save_conversation("assistant", save_text, llm_used=_active_llm)
    return result


# ── Claude Agent Loop ─────────────────────────────────────────────

async def _claude_agent_loop(messages: list[dict]) -> dict:
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    pending_actions = []
    tool_context_parts = []

    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )

        text_parts = []
        tool_uses = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        if not tool_uses:
            return {"text": "\n".join(text_parts), "actions": pending_actions,
                    "tool_context": "; ".join(tool_context_parts) if tool_context_parts else ""}

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tu in tool_uses:
            logger.info(f"Agent calling tool: {tu.name}({json.dumps(tu.input)[:200]})")
            result = await execute_tool(tu.name, tu.input)

            if result.get("action"):
                pending_actions.append(result["action"])

            tool_context_parts.append(f"{tu.name}: {result['result'][:300]}")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result["result"],
            })

        messages.append({"role": "user", "content": tool_results})

    return {"text": "\n".join(text_parts) if text_parts else "I couldn't complete that request.",
            "actions": pending_actions,
            "tool_context": "; ".join(tool_context_parts) if tool_context_parts else ""}


# ── OpenAI Agent Loop ─────────────────────────────────────────────

async def _openai_agent_loop(messages: list[dict]) -> dict:
    client = openai.AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    oai_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    oai_tools = get_openai_tools()
    pending_actions = []
    tool_context_parts = []

    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.chat.completions.create(
            model="gpt-4o",
            max_tokens=2048,
            messages=oai_messages,
            tools=oai_tools,
        )

        choice = response.choices[0]

        if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
            return {"text": choice.message.content or "", "actions": pending_actions,
                    "tool_context": "; ".join(tool_context_parts) if tool_context_parts else ""}

        oai_messages.append(choice.message)

        for tc in choice.message.tool_calls:
            args = json.loads(tc.function.arguments)
            logger.info(f"Agent calling tool: {tc.function.name}({json.dumps(args)[:200]})")
            result = await execute_tool(tc.function.name, args)

            if result.get("action"):
                pending_actions.append(result["action"])

            tool_context_parts.append(f"{tc.function.name}: {result['result'][:300]}")
            oai_messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result["result"],
            })

    return {"text": "I couldn't complete that request.", "actions": pending_actions,
            "tool_context": "; ".join(tool_context_parts) if tool_context_parts else ""}


# ── Legacy simple chat (kept for backward compat) ────────────────

async def chat(user_message: str, conversation_history: list[dict] | None = None) -> str:
    result = await agent_chat(user_message, conversation_history)
    return result["text"]
