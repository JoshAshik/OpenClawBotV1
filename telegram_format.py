"""
Markdown → Telegram HTML rendering.

Telegram's HTML parse mode supports: <b> <i> <s> <u> <code> <pre> <a> <blockquote>.
LLM output is standard-ish markdown; this converts the common subset and
escapes everything else so parse errors can't happen. bot.py falls back to
plain text if Telegram still rejects the message.
"""

import html
import re

_PLACEHOLDER = "\x00PH{}\x00"


def markdown_to_telegram_html(text: str) -> str:
    if not text:
        return ""

    protected: list[str] = []

    def _protect(rendered: str) -> str:
        protected.append(rendered)
        return _PLACEHOLDER.format(len(protected) - 1)

    # 1. Fenced code blocks first — their content must not be transformed.
    def _fence(m: re.Match) -> str:
        code = m.group(2).strip("\n")
        return _protect(f"<pre>{html.escape(code)}</pre>")

    text = re.sub(r"```([a-zA-Z0-9_+-]*)\n?(.*?)```", _fence, text, flags=re.DOTALL)

    # 2. Inline code spans.
    def _inline_code(m: re.Match) -> str:
        return _protect(f"<code>{html.escape(m.group(1))}</code>")

    text = re.sub(r"`([^`\n]+)`", _inline_code, text)

    # 3. Links — capture before escaping so URLs stay intact.
    def _link(m: re.Match) -> str:
        label = html.escape(m.group(1))
        url = html.escape(m.group(2), quote=True)
        return _protect(f'<a href="{url}">{label}</a>')

    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", _link, text)

    # 4. Escape everything that's left.
    text = html.escape(text)

    # 5. Bold / italic / strikethrough (conservative patterns).
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.DOTALL)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", text)

    # 6. Line-level: headers → bold, bullets → •, blockquotes.
    lines = text.split("\n")
    out_lines = []
    quote_buf: list[str] = []

    def _flush_quote():
        if quote_buf:
            out_lines.append("<blockquote>" + "\n".join(quote_buf) + "</blockquote>")
            quote_buf.clear()

    for line in lines:
        m = re.match(r"^&gt;\s?(.*)$", line)
        if m:
            quote_buf.append(m.group(1))
            continue
        _flush_quote()

        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            out_lines.append(f"<b>{h.group(2)}</b>")
            continue

        b = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if b:
            out_lines.append(f"{b.group(1)}• {b.group(2)}")
            continue

        out_lines.append(line)
    _flush_quote()
    text = "\n".join(out_lines)

    # 7. Restore protected segments.
    for i, rendered in enumerate(protected):
        text = text.replace(_PLACEHOLDER.format(i), rendered)

    return text
