"""
Notes and filters (formatted text, media and buttons).
Notes: saved content that members fetch with /get name or #name.
Filters: keywords the bot answers automatically.
Everything is stored per group in SQLite. See fmt.py for the formatting.
"""
import html
import logging
import re
import time

from telegram import Update
from telegram.constants import ChatType
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import db
import fmt
import ui
from utils import reply_target_gone, require_admin, say

log = logging.getLogger("groupbot.notes")

MAX_ITEMS = 100  # notes per group, and filters per group
MAX_TEXT = 2000
MAX_CAPTION = 1024  # Telegram's limit for media captions
FILTER_COOLDOWN = 10.0  # seconds before the same filter can answer again

SCHEMA = """
CREATE TABLE IF NOT EXISTS saved (
    chat_id INTEGER NOT NULL,
    scope   TEXT    NOT NULL,
    name    TEXT    NOT NULL,
    kind    TEXT    NOT NULL,
    file_id TEXT    NOT NULL,
    text    TEXT    NOT NULL,
    PRIMARY KEY (chat_id, scope, name)
);
"""

NOTES_HELP = (
    "📝 <b>Notes &amp; Filters</b>\n\n"
    "<b>Notes</b> - save text or media and fetch it any time\n"
    "/save [name] [text] - save a note (or reply to a message, photo, sticker... with /save [name])\n"
    "/get [name] - show a note (members can also write #name)\n"
    "/notes - list all notes\n"
    "/clear [name] - delete a note\n\n"
    "<b>Filters</b> - the bot answers automatically when a keyword is written\n"
    "/filter [keyword] [reply] - add a filter (or reply to a message with /filter [keyword])\n"
    'Use quotes for phrases: /filter "good morning" Good morning!\n'
    "/filters - list all filters\n"
    "/stop [keyword] - remove a filter\n\n"
    "Saving and removing is for admins. Everyone can use /get, #name, /notes and /filters.\n"
    "Text can be formatted and can have buttons: see the ✨ Formatting section of /help.\n"
    "Placeholders: {first} {mention} {group}\n"
    "English keywords match as whole words; other languages and phrases match anywhere in the text."
)

_last_answer: dict[tuple[int, str], float] = {}


# ---------------------------------------------------------------- helpers
def _extract(msg):
    """(kind, file_id, template) of a message: text, photo, sticker, animation, video, voice, audio or document."""
    template = fmt.capture(fmt.message_html(msg))
    if msg.photo:
        return "photo", msg.photo[-1].file_id, template
    if msg.sticker:
        return "sticker", msg.sticker.file_id, ""
    if msg.animation:  # must be checked before document (GIFs are documents too)
        return "animation", msg.animation.file_id, template
    if msg.video:
        return "video", msg.video.file_id, template
    if msg.voice:
        return "voice", msg.voice.file_id, template
    if msg.audio:
        return "audio", msg.audio.file_id, template
    if msg.document:
        return "document", msg.document.file_id, template
    return "text", "", template


def _after_command(msg):
    """(plain text, index of the first character after the command word)."""
    plain = msg.text or ""
    m = re.match(r"\S+\s*", plain)
    return plain, (m.end() if m else len(plain))


def _inline_html(msg, plain: str, start: int) -> str:
    return fmt.to_html(plain, msg.entities, start) if plain[start:].strip() else ""


def _parse_name(msg):
    """'/save name some *text*' -> ('name', html of 'some *text*')."""
    plain, start = _after_command(msg)
    m = re.match(r"(\S+)\s*", plain[start:])
    if not m:
        return "", ""
    return m.group(1).lower().lstrip("#"), _inline_html(msg, plain, start + m.end())


def _parse_keyword(msg):
    """'/filter "good morning" hi' -> ('good morning', html of 'hi');  '/filter hello hi there' -> ('hello', html of 'hi there')."""
    plain, start = _after_command(msg)
    raw = plain[start:].replace("“", '"').replace("”", '"')  # same length, so the indexes stay valid
    if raw.startswith('"'):
        end = raw.find('"', 1)
        if end == -1:
            return None, ""
        return (raw[1:end].strip().lower() or None), _inline_html(msg, plain, start + end + 1)
    m = re.match(r"(\S+)\s*", raw)
    if not m:
        return None, ""
    return m.group(1).lower(), _inline_html(msg, plain, start + m.end())


def _match(text: str, keywords: list[str]):
    low = text.lower()
    for k in keywords:
        if re.fullmatch(r"[a-z0-9_]+", k):
            if re.search(rf"(?<![a-z0-9_]){re.escape(k)}(?![a-z0-9_])", low):
                return k
        elif k in low:
            return k
    return None


def _get(chat_id: int, scope: str, name: str):
    rows = db.query("SELECT kind, file_id, text FROM saved WHERE chat_id=? AND scope=? AND name=?", (chat_id, scope, name))
    return rows[0] if rows else None


def _count(chat_id: int, scope: str) -> int:
    return db.query("SELECT COUNT(*) FROM saved WHERE chat_id=? AND scope=?", (chat_id, scope))[0][0]


def _names(chat_id: int, scope: str) -> list[str]:
    return [r[0] for r in db.query("SELECT name FROM saved WHERE chat_id=? AND scope=? ORDER BY name", (chat_id, scope))]


async def _send(msg, chat, user, kind: str, file_id: str, template: str) -> None:
    """Reply to `msg` with a saved note/filter (sent to the chat if `msg` was deleted meanwhile)."""
    body, markup = fmt.render(template, user, chat) if template else ("", None)
    if kind == "text":
        args, kwargs = (body,), {"parse_mode": "HTML", "reply_markup": markup}
        reply_fn, send_fn = msg.reply_text, msg.chat.send_message
    else:
        args = (file_id,)
        kwargs = {"reply_markup": markup}
        if kind != "sticker":
            kwargs.update(caption=body or None, parse_mode="HTML")
        reply_fn, send_fn = getattr(msg, f"reply_{kind}"), getattr(msg.chat, f"send_{kind}")
    try:
        try:
            await reply_fn(*args, **kwargs)
        except BadRequest as e:
            if not reply_target_gone(e):
                raise
            await send_fn(*args, **kwargs)
    except TelegramError as e:
        log.warning("Could not send saved %s in %s: %s", kind, chat.id, e)


async def _store(update: Update, scope: str, name: str, inline: str):
    """Validate and save. Returns (kind, template), or None if there was nothing to save (reply with usage),
    or "error" after replying with an error."""
    msg = update.effective_message
    chat_id = update.effective_chat.id
    if inline:
        kind, file_id, template = "text", "", fmt.capture(inline)
    elif msg.reply_to_message:
        kind, file_id, template = _extract(msg.reply_to_message)
    else:
        return None
    plain = fmt.plain_text(template) if template else ""
    if kind == "text" and not plain.strip():
        await say(update, "That message has nothing to save.")
        return "error"
    if len(plain) > (MAX_TEXT if kind == "text" else MAX_CAPTION):
        await say(update, "That text is too long.")
        return "error"
    if _get(chat_id, scope, name) is None and _count(chat_id, scope) >= MAX_ITEMS:
        await say(update, f"You can save at most {MAX_ITEMS} items of this kind.")
        return "error"
    db.execute(
        "INSERT OR REPLACE INTO saved (chat_id, scope, name, kind, file_id, text) VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, scope, name, kind, file_id, template),
    )
    return kind, template


def _in_group(update: Update) -> bool:
    return update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


# ------------------------------------------------------------------ notes
async def save_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    name, inline = _parse_name(update.effective_message)
    usage = "Usage: /save [name] [text]\nOr reply to a message (text, photo, sticker...) with /save [name]"
    if not re.fullmatch(r"[^\s#]{1,32}", name):
        await say(update, usage)
        return
    result = await _store(update, "note", name, inline)
    if result is None:
        await say(update, usage)
    elif result != "error":
        await say(update, f"✅ Note <b>{html.escape(name)}</b> saved. Get it with /get {html.escape(name)} or by writing #{html.escape(name)}")


async def get_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _in_group(update):
        await say(update, "Use /get inside a group.")
        return
    name = context.args[0].lower().lstrip("#") if context.args else ""
    if not name:
        await say(update, "Usage: /get [name]\nSee all notes with /notes")
        return
    item = _get(update.effective_chat.id, "note", name)
    if item is None:
        await say(update, f"There is no note called <b>{html.escape(name)}</b>. See /notes")
        return
    await _send(update.effective_message, update.effective_chat, update.effective_user, *item)


async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _in_group(update):
        await say(update, "Use /notes inside a group.")
        return
    names = _names(update.effective_chat.id, "note")
    if not names:
        await say(update, "No notes yet. Admins can add one with /save [name] [text]")
        return
    await say(update, "📒 <b>Notes</b>\n" + "\n".join(f"#{html.escape(n)}" for n in names) + "\n\nGet one with /get [name] or by writing #name")


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    name = context.args[0].lower().lstrip("#") if context.args else ""
    chat_id = update.effective_chat.id
    if not name:
        await say(update, "Usage: /clear [name]")
        return
    if _get(chat_id, "note", name) is None:
        await say(update, "There is no note with that name.")
        return
    db.execute("DELETE FROM saved WHERE chat_id=? AND scope='note' AND name=?", (chat_id, name))
    await say(update, f"✅ Note <b>{html.escape(name)}</b> deleted.")


# ---------------------------------------------------------------- filters
async def filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    keyword, inline = _parse_keyword(update.effective_message)
    usage = 'Usage: /filter [keyword] [reply]\nFor a phrase use quotes: /filter "good morning" Hello!\nOr reply to a message with /filter [keyword]'
    if not keyword or len(keyword) > 64:
        await say(update, usage)
        return
    result = await _store(update, "filter", keyword, inline)
    if result is None:
        await say(update, usage)
    elif result != "error":
        await say(update, f"✅ Filter added: I will answer when someone writes <b>{html.escape(keyword)}</b>.")


async def filters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _in_group(update):
        await say(update, "Use /filters inside a group.")
        return
    names = _names(update.effective_chat.id, "filter")
    if not names:
        await say(update, "No filters yet. Admins can add one with /filter [keyword] [reply]")
        return
    await say(update, "🔎 <b>Filters</b>\n" + "\n".join(html.escape(n) for n in names))


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    keyword, _ = _parse_keyword(update.effective_message)
    chat_id = update.effective_chat.id
    if not keyword:
        await say(update, 'Usage: /stop [keyword]  (use quotes for phrases: /stop "good morning")')
        return
    if _get(chat_id, "filter", keyword) is None:
        await say(update, "There is no filter with that keyword.")
        return
    db.execute("DELETE FROM saved WHERE chat_id=? AND scope='filter' AND name=?", (chat_id, keyword))
    await say(update, f"✅ Filter <b>{html.escape(keyword)}</b> removed.")


# ------------------------------------------------- #note and filter triggers
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if msg is None or user is None or user.is_bot or update.edited_message is not None:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    text = msg.text or msg.caption or ""
    if not text:
        return

    m = re.match(r"#(\S+)", text)  # "#rules" at the start of a message fetches the note "rules"
    if m:
        item = _get(chat.id, "note", m.group(1).rstrip(".,!?;:").lower())
        if item is not None:
            await _send(msg, chat, user, *item)
            return

    rows = db.query("SELECT name, kind, file_id, text FROM saved WHERE chat_id=? AND scope='filter'", (chat.id,))
    if not rows:
        return
    hit = _match(text, [r[0] for r in rows])
    if hit is None:
        return
    now = time.monotonic()
    if len(_last_answer) > 5000:
        _last_answer.clear()
    if now - _last_answer.get((chat.id, hit), -FILTER_COOLDOWN) < FILTER_COOLDOWN:
        return  # the same filter just answered, do not spam the group
    _last_answer[(chat.id, hit)] = now
    row = next(r for r in rows if r[0] == hit)
    await _send(msg, chat, user, *row[1:])


# --------------------------------------------------------------- register
def register(app: Application) -> None:
    db.ensure(SCHEMA)
    ui.SECTIONS["notes"] = NOTES_HELP
    ui.add_section("formatting", "✨ Formatting", fmt.FORMAT_HELP)

    app.add_handler(CommandHandler("save", save_cmd))
    app.add_handler(CommandHandler("get", get_cmd))
    app.add_handler(CommandHandler("notes", notes_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("filter", filter_cmd))
    app.add_handler(CommandHandler("filters", filters_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    # group 1: runs after the command handlers and never blocks them
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_text),
        group=1,
    )
