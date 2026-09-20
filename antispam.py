"""
Anti-spam: flood control, anti-link (with allowed domains) and banned words.
Settings are stored per group in SQLite. Admins are never punished.
"""
import html
import logging
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from telegram import ChatPermissions, Update
from telegram.constants import ChatMemberStatus, ChatType, MessageEntityType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
import ui
from utils import is_exempt, require_admin, say

log = logging.getLogger("groupbot.antispam")

ACTIONS = ("delete", "warn", "mute", "kick", "ban")
MAX_WARNS = 3  # keep in sync with bot.py
MUTE_MINUTES = 30
NOTICE_TTL = 15  # seconds before the bot deletes its own notices
FLOOD_WINDOW = 10.0  # seconds
DEFAULT_FLOOD_LIMIT = 5
MAX_WORDS = 200
MAX_DOMAINS = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS blacklist (
    chat_id INTEGER NOT NULL,
    word    TEXT    NOT NULL,
    PRIMARY KEY (chat_id, word)
);
CREATE TABLE IF NOT EXISTS allowed_domains (
    chat_id INTEGER NOT NULL,
    domain  TEXT    NOT NULL,
    PRIMARY KEY (chat_id, domain)
);
"""

FEATURES = {
    "flood": {"on_key": "flood_on", "action_key": "flood_action", "action": "mute"},
    "link": {"on_key": "link_on", "action_key": "link_action", "action": "delete"},
    "blacklist": {"action_key": "blacklist_action", "action": "delete"},
}

ANTISPAM_HELP = (
    "🛡 <b>Anti-Spam</b>\n\n"
    "Admins are never punished. Actions: delete, warn, mute (30 min), kick, ban.\n\n"
    "<b>Anti-flood</b>\n"
    "/antiflood on|off - punish members who send too many messages\n"
    "/setflood [3-20] - max messages per 10 seconds (default 5)\n"
    "/floodaction [action] - what to do (default: mute)\n\n"
    "<b>Anti-link</b>\n"
    "/antilink on|off - remove messages that contain links\n"
    "/linkaction [action] - what to do (default: delete)\n"
    "/allowlink [domain] - allow a domain, e.g. /allowlink youtube.com\n"
    "/unallowlink [domain] - remove an allowed domain\n"
    "/allowedlinks - list allowed domains\n\n"
    "<b>Banned words</b>\n"
    "/addblacklist [word or phrase] - ban a word\n"
    "/rmblacklist [word or phrase] - unban a word\n"
    "/blacklist - list banned words\n"
    "/blacklistaction [action] - what to do (default: delete)\n"
    "English words match as whole words; other languages and phrases match anywhere in the text.\n\n"
    "I need the <b>Delete messages</b> and <b>Ban users</b> admin permissions."
)

warns = db.WarnStore()
_flood: dict[tuple[int, int], deque] = {}


# ---------------------------------------------------------------- helpers
def _host(url: str) -> str:
    """'https://www.Example.com:8080/a?b' -> 'example.com'"""
    url = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url.strip().lower())
    authority = re.split(r"[/?#]", url, maxsplit=1)[0]
    host = authority.rsplit("@", 1)[-1].split(":")[0]
    return host.removeprefix("www.")


def _is_allowed(host: str, allowed: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in allowed)


def _hosts(msg) -> list[str]:
    """Domains of all links (plain URLs and text links) in a message or its caption."""
    found = []
    for entities, parse in ((msg.entities, msg.parse_entity), (msg.caption_entities, msg.parse_caption_entity)):
        for e in entities or ():
            if e.type == MessageEntityType.URL:
                found.append(_host(parse(e)))
            elif e.type == MessageEntityType.TEXT_LINK and e.url:
                found.append(_host(e.url))
    return [h for h in found if h]


def _blacklisted(text: str, words: list[str]):
    low = text.lower()
    for w in words:
        if re.fullmatch(r"[a-z0-9_]+", w):
            if re.search(rf"(?<![a-z0-9_]){re.escape(w)}(?![a-z0-9_])", low):
                return w
        elif w in low:
            return w
    return None


def _flood_hit(chat_id: int, user_id: int, message_id: int, limit: int):
    """Record a message. Returns the message ids of the burst if the user exceeded the limit."""
    now = time.monotonic()
    if len(_flood) > 5000:  # forget users who have been quiet for a while
        for key in [k for k, dq in _flood.items() if not dq or now - dq[-1][0] > FLOOD_WINDOW]:
            del _flood[key]
    dq = _flood.setdefault((chat_id, user_id), deque())
    dq.append((now, message_id))
    while dq and now - dq[0][0] > FLOOD_WINDOW:
        dq.popleft()
    if len(dq) > limit:
        ids = [m for _, m in dq]
        dq.clear()
        return ids
    return None


async def _rights_warning(context: ContextTypes.DEFAULT_TYPE, chat_id: int, action: str) -> str:
    try:
        me = await context.bot.get_chat_member(chat_id, context.bot.id)
    except TelegramError:
        return ""
    if me.status != ChatMemberStatus.ADMINISTRATOR:
        return (
            "\n\n⚠️ I'm not an admin in this group. Please make me admin with the "
            "<b>Delete messages</b> and <b>Ban users</b> permissions."
        )
    missing = []
    if not getattr(me, "can_delete_messages", False):
        missing.append("Delete messages")
    if action in ("warn", "mute", "kick", "ban") and not getattr(me, "can_restrict_members", False):
        missing.append("Ban users")
    if missing:
        return f"\n\n⚠️ I still need these admin permissions: <b>{', '.join(missing)}</b>."
    return ""


async def _delete_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id, message_id)
    except TelegramError:
        pass


async def _notice(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    """Send a short-lived message that deletes itself."""
    try:
        m = await context.bot.send_message(chat_id, text, parse_mode="HTML")
    except TelegramError as e:
        log.warning("Could not send notice to %s: %s", chat_id, e)
        return
    if context.job_queue is not None:
        context.job_queue.run_once(_delete_job, NOTICE_TTL, data=(chat_id, m.message_id))


async def _punish(context: ContextTypes.DEFAULT_TYPE, chat, user, action: str, reason: str, message_ids: list[int]) -> None:
    try:
        if len(message_ids) == 1:
            await context.bot.delete_message(chat.id, message_ids[0])
        else:
            await context.bot.delete_messages(chat.id, message_ids)
    except TelegramError as e:
        log.warning("Could not delete message(s) in %s: %s", chat.id, e)
    if action == "delete":
        return
    mention = user.mention_html()
    reason = html.escape(reason)
    try:
        if action == "warn":
            key = (chat.id, user.id)
            warns[key] += 1
            count = warns[key]
            if count >= MAX_WARNS:
                await context.bot.ban_chat_member(chat.id, user.id)
                warns.pop(key, None)
                text = f"🔨 {mention} was banned after {MAX_WARNS} warnings ({reason})."
            else:
                text = f"⚠️ {mention} was warned ({count}/{MAX_WARNS}): {reason}."
        elif action == "mute":
            until = datetime.now(timezone.utc) + timedelta(minutes=MUTE_MINUTES)
            await context.bot.restrict_chat_member(chat.id, user.id, ChatPermissions.no_permissions(), until_date=until)
            text = f"🔇 {mention} was muted for {MUTE_MINUTES} minutes: {reason}."
        elif action == "kick":
            await context.bot.ban_chat_member(chat.id, user.id)
            await context.bot.unban_chat_member(chat.id, user.id)
            text = f"👢 {mention} was removed from the group: {reason}."
        else:  # ban
            await context.bot.ban_chat_member(chat.id, user.id)
            text = f"🔨 {mention} was banned: {reason}."
    except TelegramError as e:
        log.warning("Could not apply '%s' to %s in %s: %s", action, user.id, chat.id, e)
        return
    await _notice(context, chat.id, text)


# ------------------------------------------------------- message watcher
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if msg is None or user is None or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if msg.sender_chat or user.is_bot:  # anonymous admins, channel posts, bots
        return

    keys = ("flood_on", "flood_limit", "flood_action", "link_on", "link_action", "blacklist_action")
    marks = ",".join("?" * len(keys))
    cfg = dict(db.query(f"SELECT key, value FROM settings WHERE chat_id=? AND key IN ({marks})", (chat.id, *keys)))
    text = msg.text or msg.caption or ""
    words = [r[0] for r in db.query("SELECT word FROM blacklist WHERE chat_id=?", (chat.id,))] if text else []
    flood_on = cfg.get("flood_on") == "1"
    link_on = cfg.get("link_on") == "1"
    if not (flood_on or link_on or words):
        return

    violation = None  # (action, reason, message_ids)
    if flood_on and update.edited_message is None:
        limit = int(cfg.get("flood_limit", DEFAULT_FLOOD_LIMIT))
        ids = _flood_hit(chat.id, user.id, msg.message_id, limit)
        if ids:
            violation = (cfg.get("flood_action", "mute"), "flooding", ids)
    if violation is None and words and _blacklisted(text, words):
        violation = (cfg.get("blacklist_action", "delete"), "banned word", [msg.message_id])
    if violation is None and link_on:
        hosts = _hosts(msg)
        if hosts:
            allowed = [r[0] for r in db.query("SELECT domain FROM allowed_domains WHERE chat_id=?", (chat.id,))]
            if any(not _is_allowed(h, allowed) for h in hosts):
                violation = (cfg.get("link_action", "delete"), "links are not allowed", [msg.message_id])
    if violation is None:
        return
    if await is_exempt(context, chat.id, user.id):  # only checked on a violation (saves API calls)
        return
    action, reason, ids = violation
    await _punish(context, chat, user, action, reason, ids)
    raise ApplicationHandlerStop  # do not run other handlers for a removed message


# --------------------------------------------------------------- commands
def _toggler(feature: str, label: str, cmd: str):
    meta = FEATURES[feature]

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await require_admin(update, context):
            return
        chat_id = update.effective_chat.id
        arg = context.args[0].lower() if context.args else ""
        action = db.get_value(chat_id, meta["action_key"], meta["action"])
        if arg not in ("on", "off"):
            state = "ON" if db.get_bool(chat_id, meta["on_key"], False) else "OFF"
            await say(update, f"{label} is currently <b>{state}</b> (action: <b>{action}</b>).\nUse /{cmd} on or /{cmd} off.")
            return
        db.set_value(chat_id, meta["on_key"], "1" if arg == "on" else "0")
        text = f"✅ {label} turned <b>{arg.upper()}</b> (action: <b>{action}</b>)."
        if arg == "on":
            text += await _rights_warning(context, chat_id, action)
        await say(update, text)

    return handler


def _action_setter(feature: str, label: str, cmd: str):
    meta = FEATURES[feature]

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await require_admin(update, context):
            return
        chat_id = update.effective_chat.id
        arg = context.args[0].lower() if context.args else ""
        if arg not in ACTIONS:
            current = db.get_value(chat_id, meta["action_key"], meta["action"])
            await say(update, f"{label} action is currently <b>{current}</b>.\nUse /{cmd} delete|warn|mute|kick|ban")
            return
        db.set_value(chat_id, meta["action_key"], arg)
        await say(update, f"✅ {label} action set to <b>{arg}</b>." + await _rights_warning(context, chat_id, arg))

    return handler


def _arg_text(update: Update) -> str:
    parts = (update.effective_message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


async def setflood_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    try:
        n = int(context.args[0])
        if not 3 <= n <= 20:
            raise ValueError
    except (IndexError, ValueError):
        await say(update, "Usage: /setflood [3-20]\nExample: /setflood 5")
        return
    db.set_value(update.effective_chat.id, "flood_limit", str(n))
    await say(update, f"✅ Flood limit set: more than <b>{n}</b> messages in {int(FLOOD_WINDOW)} seconds is flooding.")


async def allowlink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    domain = _host(context.args[0]) if context.args else ""
    if "." not in domain or " " in domain:
        await say(update, "Usage: /allowlink example.com")
        return
    chat_id = update.effective_chat.id
    if db.query("SELECT COUNT(*) FROM allowed_domains WHERE chat_id=?", (chat_id,))[0][0] >= MAX_DOMAINS:
        await say(update, f"You can allow at most {MAX_DOMAINS} domains.")
        return
    db.execute("INSERT OR IGNORE INTO allowed_domains (chat_id, domain) VALUES (?, ?)", (chat_id, domain))
    await say(update, f"✅ Links to <b>{html.escape(domain)}</b> (and its subdomains) are now allowed.")


async def unallowlink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    domain = _host(context.args[0]) if context.args else ""
    if not domain:
        await say(update, "Usage: /unallowlink example.com")
        return
    chat_id = update.effective_chat.id
    if not db.query("SELECT 1 FROM allowed_domains WHERE chat_id=? AND domain=?", (chat_id, domain)):
        await say(update, "That domain is not in the allowed list.")
        return
    db.execute("DELETE FROM allowed_domains WHERE chat_id=? AND domain=?", (chat_id, domain))
    await say(update, f"✅ <b>{html.escape(domain)}</b> removed from the allowed list.")


async def allowedlinks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    rows = db.query("SELECT domain FROM allowed_domains WHERE chat_id=? ORDER BY domain", (update.effective_chat.id,))
    if not rows:
        await say(update, "No domains are allowed yet. Use /allowlink example.com")
        return
    await say(update, "✅ <b>Allowed domains</b>\n" + "\n".join(html.escape(r[0]) for r in rows))


async def addblacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    word = _arg_text(update).lower()
    if not word or len(word) > 64:
        await say(update, "Usage: /addblacklist [word or phrase] (max 64 characters)")
        return
    chat_id = update.effective_chat.id
    if db.query("SELECT COUNT(*) FROM blacklist WHERE chat_id=?", (chat_id,))[0][0] >= MAX_WORDS:
        await say(update, f"You can ban at most {MAX_WORDS} words.")
        return
    db.execute("INSERT OR IGNORE INTO blacklist (chat_id, word) VALUES (?, ?)", (chat_id, word))
    action = db.get_value(chat_id, "blacklist_action", "delete")
    await say(update, f"✅ <b>{html.escape(word)}</b> added to the banned words." + await _rights_warning(context, chat_id, action))


async def rmblacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    word = _arg_text(update).lower()
    if not word:
        await say(update, "Usage: /rmblacklist [word or phrase]")
        return
    chat_id = update.effective_chat.id
    if not db.query("SELECT 1 FROM blacklist WHERE chat_id=? AND word=?", (chat_id, word)):
        await say(update, "That word is not in the banned list.")
        return
    db.execute("DELETE FROM blacklist WHERE chat_id=? AND word=?", (chat_id, word))
    await say(update, f"✅ <b>{html.escape(word)}</b> removed from the banned words.")


async def blacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    rows = db.query("SELECT word FROM blacklist WHERE chat_id=? ORDER BY word", (chat_id,))
    if not rows:
        await say(update, "No banned words yet. Use /addblacklist [word]")
        return
    action = db.get_value(chat_id, "blacklist_action", "delete")
    await say(update, f"🚫 <b>Banned words</b> (action: {action})\n" + ", ".join(html.escape(r[0]) for r in rows))


# --------------------------------------------------------------- register
def register(app: Application) -> None:
    db.ensure(SCHEMA)
    ui.SECTIONS["antispam"] = ANTISPAM_HELP

    # group -1 runs before the command handlers, so spam is removed first
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, on_message), group=-1)

    app.add_handler(CommandHandler("antiflood", _toggler("flood", "Anti-flood", "antiflood")))
    app.add_handler(CommandHandler("setflood", setflood_cmd))
    app.add_handler(CommandHandler("floodaction", _action_setter("flood", "Anti-flood", "floodaction")))
    app.add_handler(CommandHandler("antilink", _toggler("link", "Anti-link", "antilink")))
    app.add_handler(CommandHandler("linkaction", _action_setter("link", "Anti-link", "linkaction")))
    app.add_handler(CommandHandler("allowlink", allowlink_cmd))
    app.add_handler(CommandHandler("unallowlink", unallowlink_cmd))
    app.add_handler(CommandHandler("allowedlinks", allowedlinks_cmd))
    app.add_handler(CommandHandler("addblacklist", addblacklist_cmd))
    app.add_handler(CommandHandler("rmblacklist", rmblacklist_cmd))
    app.add_handler(CommandHandler("blacklist", blacklist_cmd))
    app.add_handler(CommandHandler("blacklistaction", _action_setter("blacklist", "Banned words", "blacklistaction")))
