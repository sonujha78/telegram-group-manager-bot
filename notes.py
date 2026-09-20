"""
Notes and filters.
Notes: saved text or media that members fetch with /get name or #name.
Filters: keywords the bot answers automatically.
Everything is stored per group in SQLite.
"""
import html
import logging
import re
import time

from telegram import Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import db
import ui
from utils import require_admin, say
from welcome import render

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
    "Placeholders: {first} {mention} {group}\n"
    "English keywords match as whole words; other languages and phrases match anywhere in the text."
)

_last_answer: dict[tuple[int, str], float] = {}


# ---------------------------------------------------------------- helpers
def _extract(msg):
    """(kind, file_id, text) of a message: text, photo, sticker, animation, video, voice, audio or document."""
    caption = msg.caption or ""
    if msg.photo:
        return "photo", msg.photo[-1].file_id, caption
    if msg.sticker:
        return "sticker", msg.sticker.file_id, ""
    if msg.animation:  # must be checked before document (GIFs are documents too)
        return "animation", msg.animation.file_id, caption
    if msg.video:
        return "video", msg.video.file_id, caption
    if msg.voice:
        return "voice", msg.voice.file_id, caption
    if msg.audio:
        return "audio", msg.audio.file_id, caption
    if msg.document:
        return "document", msg.document.file_id, caption
    return "text", "", msg.text or ""


def _rest(msg) -> str:
    """Everything after the command word (newlines are kept)."""
    parts = (msg.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def _parse_keyword(raw: str):
    """'"good morning" hi' -> ('good morning', 'hi');  'hello hi there' -> ('hello', 'hi there')."""
    raw = raw.replace("“", '"').replace("”", '"').strip()
    if raw.startswith('"'):
        end = raw.find('"', 1)
        if end == -1:
            return None, ""
        return raw[1:end].strip().lower() or None, raw[end + 1 :].strip()
    parts = raw.split(maxsplit=1)
    if not parts:
        return None, ""
    return parts[0].lower(), (parts[1].strip() if len(parts) == 2 else "")


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


async def _send(msg, chat, user, kind: str, file_id: str, text: str) -> None:
    """Reply to `msg` with a saved note/filter."""
    body = render(text, user, chat) if text else None
    try:
        if kind == "text":
            await msg.reply_text(body, parse_mode="HTML")
        elif kind == "sticker":
            await msg.reply_sticker(file_id)
        else:
            await getattr(msg, f"reply_{kind}")(file_id, caption=body, parse_mode="HTML")
    except TelegramError as e:
        log.warning("Could not send saved %s in %s: %s", kind, chat.id, e)


async def _store(update: Update, scope: str, name: str, inline: str):
    """Validate and save. Returns the saved (kind, text) or None after replying with an error."""
    msg = update.effective_message
    chat_id = update.effective_chat.id
    if inline:
        kind, file_id, text = "text", "", inline
    elif msg.reply_to_message:
        kind, file_id, text = _extract(msg.reply_to_message)
    else:
        return None
    if kind == "text" and not text.strip():
        await say(update, "That message has nothing to save.")
        return "error"
    if len(text) > (MAX_TEXT if kind == "text" else MAX_CAPTION):
        await say(update, "That text is too long.")
        return "error"
    if _get(chat_id, scope, name) is None and _count(chat_id, scope) >= MAX_ITEMS:
        await say(update, f"You can save at most {MAX_ITEMS} items of this kind.")
        return "error"
    db.execute(
        "INSERT OR REPLACE INTO saved (chat_id, scope, name, kind, file_id, text) VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, scope, name, kind, file_id, text),
    )
    return kind, text


def _in_group(update: Update) -> bool:
    return update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


# ------------------------------------------------------------------ notes
async def save_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    parts = _rest(update.effective_message).split(maxsplit=1)
    name = parts[0].lower().lstrip("#") if parts else ""
    usage = "Usage: /save [name] [text]\nOr reply to a message (text, photo, sticker...) with /save [name]"
    if not re.fullmatch(r"[^\s#]{1,32}", name):
        await say(update, usage)
        return
    result = await _store(update, "note", name, parts[1].strip() if len(parts) == 2 else "")
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
    keyword, inline = _parse_keyword(_rest(update.effective_message))
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
    keyword, _ = _parse_keyword(_rest(update.effective_message))
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
