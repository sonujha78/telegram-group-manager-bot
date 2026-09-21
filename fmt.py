"""
Rich text for welcome/goodbye messages, rules, notes and filters.

Admins can format text in two ways, and both can be mixed:
  1. Telegram's own formatting (select the text, then Bold / Italic / Link ...).
  2. Typing the markup: *bold* _italic_ __underline__ ~strike~ `code` [text](https://link)
Buttons under the message: [Label](buttonurl://https://example.com)  (add :same for the same row)

A saved template is a small JSON document (marked with MARK) that holds safe HTML and the buttons.
Older plain-text templates (without MARK) are still shown correctly.
"""
import html
import json
import re
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

MARK = "\x01"  # first character of a stored template
MAX_BUTTONS = 12

FORMAT_HELP = (
    "✨ <b>Formatting</b>\n\n"
    "Welcome and goodbye messages, rules, notes and filters can be formatted.\n\n"
    "<b>Two ways</b>\n"
    "1. Use Telegram's own formatting (select the text, then Bold, Italic, Link...) when you write the command.\n"
    "2. Or type the markup yourself:\n"
    "*bold* - bold text\n"
    "_italic_ - italic text\n"
    "__underline__ - underlined text\n"
    "~strike~ - crossed out text\n"
    "`code` - monospace text\n"
    "[link text](https://example.com) - a link\n\n"
    "<b>Buttons</b> under the message:\n"
    "[Our site](buttonurl://https://example.com)\n"
    "[Channel](buttonurl://https://t.me/yourchannel)\n"
    "Add :same to put a button on the same row as the one before it:\n"
    "[Docs](buttonurl://https://example.com/docs:same)\n\n"
    "<b>Placeholders</b>: {first} first name, {mention} clickable mention, {group} group name\n\n"
    "Works with /setwelcome, /setgoodbye, /setrules, /save and /filter. "
    "You can also reply to a formatted message (or a photo with a formatted caption) with these commands."
)

_ENTITY_TAGS = {
    "bold": ("<b>", "</b>"),
    "italic": ("<i>", "</i>"),
    "underline": ("<u>", "</u>"),
    "strikethrough": ("<s>", "</s>"),
    "spoiler": ("<tg-spoiler>", "</tg-spoiler>"),
    "code": ("<code>", "</code>"),
    "pre": ("<pre>", "</pre>"),
    "blockquote": ("<blockquote>", "</blockquote>"),
    "expandable_blockquote": ("<blockquote expandable>", "</blockquote>"),
}


# ------------------------------------------------- Telegram entities -> HTML
def _tags_for(entity):
    kind = getattr(entity.type, "value", entity.type)
    if kind == "text_link" and entity.url:
        return f'<a href="{html.escape(entity.url, quote=True)}">', "</a>"
    return _ENTITY_TAGS.get(kind)


def to_html(text: str, entities, start: int = 0) -> str:
    """HTML for text[start:], built from Telegram's formatting entities (their offsets are UTF-16 units)."""
    units = text.encode("utf-16-le")
    first = len(text[:start].encode("utf-16-le")) // 2
    total = len(units) // 2
    spans = []
    for order, e in enumerate(entities or ()):
        tags = _tags_for(e)
        s, en = max(e.offset, first), min(e.offset + e.length, total)
        if tags is not None and s < en:
            spans.append((s, -en, order, en, tags))
    spans.sort()
    opens, closes = {}, {}
    for idx, (s, _neg, _order, en, tags) in enumerate(spans):
        opens.setdefault(s, []).append((idx, tags[0]))
        closes.setdefault(en, []).append((idx, tags[1]))
    points = sorted({first, total, *opens, *closes})
    out = []
    for i, pos in enumerate(points):
        for _idx, tag in sorted(closes.get(pos, ()), reverse=True):  # inner tags close first
            out.append(tag)
        for _idx, tag in sorted(opens.get(pos, ())):  # outer tags open first
            out.append(tag)
        if i + 1 < len(points):
            chunk = units[pos * 2 : points[i + 1] * 2].decode("utf-16-le")
            out.append(html.escape(chunk, quote=False))
    return "".join(out).strip()


def message_html(msg, start: int = 0) -> str:
    """HTML of a message's text (or caption) from character `start` on."""
    if msg.text is not None:
        return to_html(msg.text, msg.entities, start)
    if msg.caption is not None:
        return to_html(msg.caption, msg.caption_entities, start)
    return ""


# ------------------------------------------------------ typed markup -> HTML
_BUTTON = re.compile(r"\[([^\[\]\n]{1,60}?)\]\(buttonurl://([^\s()<>\"']+?)(:same)?\)")
_LINK = re.compile(r"\[([^\[\]\n]+)\]\(((?:https?|tg)://[^\s()<>\"']+)\)")
_EMPHASIS = (
    (re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])"), r"<b>\1</b>"),
    (re.compile(r"(?<![\w_])__(?!\s)([^_\n]+?)(?<!\s)__(?![\w_])"), r"<u>\1</u>"),
    (re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])"), r"<i>\1</i>"),
    (re.compile(r"(?<![\w~])~(?!\s)([^~\n]+?)(?<!\s)~(?![\w~])"), r"<s>\1</s>"),
)


def _emphasis(text: str) -> str:
    for pattern, repl in _EMPHASIS:
        text = pattern.sub(repl, text)
    return text


def _markup_segment(text: str) -> str:
    """Convert typed markup in a piece of text that contains no HTML tags."""
    pieces = []
    for part in re.split(r"(`[^`\n]+`)", text):
        if len(part) > 2 and part[0] == "`" and part[-1] == "`":
            pieces.append(f"<code>{part[1:-1]}</code>")
            continue
        links = []

        def stash(m):
            links.append((m.group(1), m.group(2)))
            return f"\x02{len(links) - 1}\x03"

        part = _emphasis(_LINK.sub(stash, part))
        part = re.sub(
            r"\x02(\d+)\x03",
            lambda m: f'<a href="{links[int(m.group(1))][1]}">{_emphasis(links[int(m.group(1))][0])}</a>',
            part,
        )
        pieces.append(part)
    return "".join(pieces)


def _typed_markup(html_text: str) -> str:
    """Typed markup -> HTML, leaving the tags that Telegram's entities already produced alone."""
    return "".join(
        token if token.startswith("<") and token.endswith(">") else _markup_segment(token)
        for token in re.split(r"(<[^>]*>)", html_text)
    )


def _extract_buttons(html_text: str):
    """Remove [Label](buttonurl://...) buttons from the text. Returns (text, rows)."""
    rows, count = [], 0

    def take(m):
        nonlocal count
        label = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        url = html.unescape(m.group(2))
        parsed = urlparse(url)
        if not label or parsed.scheme not in ("http", "https", "tg") or not parsed.netloc or count >= MAX_BUTTONS:
            return m.group(0)  # not a valid button: keep it as ordinary text
        count += 1
        if m.group(3) and rows:
            rows[-1].append([label, url])
        else:
            rows.append([[label, url]])
        return ""

    text = _BUTTON.sub(take, html_text)
    return re.sub(r"\n{3,}", "\n\n", text).strip(), rows


# ----------------------------------------------------- stored templates
def capture(html_text: str) -> str:
    """Turn what an admin wrote (as HTML) into a stored template. Returns '' if there is nothing at all."""
    text, rows = _extract_buttons(html_text)
    text = _typed_markup(text).strip()
    if not text and not rows:
        return ""
    return MARK + json.dumps({"h": text, "b": rows}, ensure_ascii=False)


def _load(template: str) -> dict:
    if template.startswith(MARK):
        try:
            data = json.loads(template[1:])
            return {"h": str(data.get("h", "")), "b": data.get("b", [])}
        except (ValueError, AttributeError):
            return {"h": html.escape(template[1:]), "b": []}
    return {"h": html.escape(template), "b": []}  # older plain-text template


def plain_text(template: str) -> str:
    """The visible text of a template (no tags), used for length checks."""
    return html.unescape(re.sub(r"<[^>]+>", "", _load(template)["h"]))


def render(template: str, user, chat):
    """Fill in the placeholders. Returns (html_text, reply_markup or None)."""
    data = _load(template)
    text = data["h"]
    text = text.replace("{first}", html.escape(user.first_name or "friend"))
    if "{mention}" in text:
        text = text.replace("{mention}", user.mention_html())
    text = text.replace("{group}", html.escape(chat.title or "this group"))
    rows = [[InlineKeyboardButton(label, url=url) for label, url in row] for row in data["b"] if row]
    return text, (InlineKeyboardMarkup(rows) if rows else None)
