"""
Locks (delete selected message types from regular members) and clean-service.
Admins and approved users are exempt. Settings are stored per group in SQLite.
"""
import logging

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType, MessageEntityType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
import ui
from utils import bot_rights_warning, is_exempt, require_admin, say

log = logging.getLogger("groupbot.locks")


def _has_link(msg) -> bool:
    for entities in (msg.entities, msg.caption_entities):
        for e in entities or ():
            if e.type in (MessageEntityType.URL, MessageEntityType.TEXT_LINK):
                return True
    return False


PREDICATES = {
    "sticker": lambda m: bool(m.sticker),
    "gif": lambda m: bool(m.animation),
    "photo": lambda m: bool(m.photo),
    "video": lambda m: bool(m.video or m.video_note),
    "audio": lambda m: bool(m.audio),
    "voice": lambda m: bool(m.voice),
    "document": lambda m: bool(m.document) and not m.animation,
    "contact": lambda m: bool(m.contact),
    "location": lambda m: bool(m.location or m.venue),
    "poll": lambda m: bool(m.poll),
    "game": lambda m: bool(m.game),
    "forward": lambda m: m.forward_origin is not None,
    "inline": lambda m: bool(m.via_bot),
    "url": _has_link,
}
PREDICATES["media"] = lambda m: any(
    PREDICATES[t](m) for t in ("photo", "video", "audio", "voice", "document", "gif")
)
SPECIAL = ("all", "bots")  # "all" = every message, "bots" = members cannot add bots
LOCK_TYPES = tuple(PREDICATES) + SPECIAL

LOCKS_HELP = (
    "🔒 <b>Locks &amp; Clean Service</b>\n\n"
    "Admins only. Locked content sent by regular members is deleted. "
    "Admins and approved users are never affected.\n\n"
    "/lock [type] - lock a type\n"
    "/unlock [type] - unlock it\n"
    "/locks - show what is locked\n"
    "/locktypes - list all lock types\n"
    "/cleanservice on|off - delete join, leave and pin service messages\n\n"
    "Types: " + ", ".join(LOCK_TYPES) + "\n"
    "<b>all</b> deletes every message from members, <b>media</b> means photo, video, audio, voice, "
    "document and gif, <b>bots</b> removes bots that members try to add."
)


def _get_locks(chat_id: int) -> set[str]:
    raw = db.get_value(chat_id, "locks", "")
    return {t for t in raw.split(",") if t}


def _save_locks(chat_id: int, locks: set[str]) -> None:
    if locks:
        db.set_value(chat_id, "locks", ",".join(sorted(locks)))
    else:
        db.del_value(chat_id, "locks")


# ------------------------------------------------------------ watchers
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if msg is None or user is None or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if msg.sender_chat or user.is_bot:  # anonymous admins, channel posts, bots
        return
    locked = _get_locks(chat.id)
    if not locked:
        return
    hit = "all" if "all" in locked else next((t for t in locked if t in PREDICATES and PREDICATES[t](msg)), None)
    if hit is None:
        return
    if await is_exempt(context, chat.id, user.id):  # checked only when something is locked (saves API calls)
        return
    try:
        await msg.delete()
    except TelegramError as e:
        log.warning("Lock '%s': could not delete message in %s: %s", hit, chat.id, e)
    raise ApplicationHandlerStop


async def on_bot_added(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmu = update.chat_member
    new = cmu.new_chat_member
    if not new.user.is_bot or new.user.id == context.bot.id:
        return
    joined = cmu.old_chat_member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) and new.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
        ChatMemberStatus.ADMINISTRATOR,
    )
    if not joined or "bots" not in _get_locks(cmu.chat.id):
        return
    if await is_exempt(context, cmu.chat.id, cmu.from_user.id):
        return
    try:
        await context.bot.ban_chat_member(cmu.chat.id, new.user.id)
        await context.bot.unban_chat_member(cmu.chat.id, new.user.id)
    except TelegramError as e:
        log.warning("Bots lock: could not remove bot %s from %s: %s", new.user.id, cmu.chat.id, e)


async def on_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not db.get_bool(update.effective_chat.id, "clean_service", False):
        return
    try:
        await update.effective_message.delete()
    except TelegramError:
        pass


# ------------------------------------------------------------- commands
async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    kind = context.args[0].lower() if context.args else ""
    if kind not in LOCK_TYPES:
        await say(update, "Usage: /lock [type]\nTypes: " + ", ".join(LOCK_TYPES))
        return
    chat_id = update.effective_chat.id
    locks = _get_locks(chat_id)
    locks.add(kind)
    _save_locks(chat_id, locks)
    text = f"🔒 <b>{kind}</b> is now locked."
    text += await bot_rights_warning(context, chat_id, "can_delete_messages", *(("can_restrict_members",) if kind == "bots" else ()))
    await say(update, text)


async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    kind = context.args[0].lower() if context.args else ""
    if kind not in LOCK_TYPES:
        await say(update, "Usage: /unlock [type]\nTypes: " + ", ".join(LOCK_TYPES))
        return
    chat_id = update.effective_chat.id
    locks = _get_locks(chat_id)
    if kind not in locks:
        await say(update, f"<b>{kind}</b> is not locked.")
        return
    locks.discard(kind)
    _save_locks(chat_id, locks)
    await say(update, f"🔓 <b>{kind}</b> is now unlocked.")


async def locks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    locks = _get_locks(update.effective_chat.id)
    lines = [f"{'🔒' if t in locks else '🔓'} {t}" for t in LOCK_TYPES]
    await say(update, "<b>Locks in this group</b>\n" + "\n".join(lines))


async def locktypes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await say(update, "<b>Lock types</b>\n" + ", ".join(LOCK_TYPES) + "\n\nUse /lock [type] (admins only).")


async def cleanservice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    arg = context.args[0].lower() if context.args else ""
    if arg not in ("on", "off"):
        state = "ON" if db.get_bool(chat_id, "clean_service", False) else "OFF"
        await say(update, f"Clean service is currently <b>{state}</b>.\nUse /cleanservice on or /cleanservice off.")
        return
    db.set_value(chat_id, "clean_service", "1" if arg == "on" else "0")
    text = f"✅ Clean service turned <b>{arg.upper()}</b>."
    if arg == "on":
        text += await bot_rights_warning(context, chat_id, "can_delete_messages")
    await say(update, text)


# ------------------------------------------------------------- register
def register(app: Application) -> None:
    ui.SECTIONS["locks"] = LOCKS_HELP

    # lower group numbers run first: locks (-2) before anti-spam (-1) before the commands (0)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, on_message), group=-2)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.StatusUpdate.ALL, on_service), group=3)
    app.add_handler(ChatMemberHandler(on_bot_added, ChatMemberHandler.CHAT_MEMBER), group=2)

    app.add_handler(CommandHandler("lock", lock_cmd))
    app.add_handler(CommandHandler("unlock", unlock_cmd))
    app.add_handler(CommandHandler("locks", locks_cmd))
    app.add_handler(CommandHandler("locktypes", locktypes_cmd))
    app.add_handler(CommandHandler("cleanservice", cleanservice_cmd))
