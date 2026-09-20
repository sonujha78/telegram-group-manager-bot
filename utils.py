"""Small shared helpers for the feature modules."""
import re
from datetime import timedelta

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db

_APPROVED_SCHEMA = """
CREATE TABLE IF NOT EXISTS approved (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name    TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (chat_id, user_id)
);
"""
_approved_ready = False

_RIGHT_LABELS = {
    "can_delete_messages": "Delete messages",
    "can_restrict_members": "Ban users",
    "can_pin_messages": "Pin messages",
    "can_promote_members": "Add new admins",
}


async def say(update: Update, text: str, **kwargs) -> None:
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, **kwargs)


async def is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def ensure_approved_table() -> None:
    global _approved_ready
    if not _approved_ready:
        db.ensure(_APPROVED_SCHEMA)
        _approved_ready = True


def is_approved(chat_id: int, user_id: int) -> bool:
    ensure_approved_table()
    return bool(db.query("SELECT 1 FROM approved WHERE chat_id=? AND user_id=?", (chat_id, user_id)))


async def is_exempt(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """Approved users and admins are never touched by anti-spam or locks."""
    return is_approved(chat_id, user_id) or await is_admin(context, chat_id, user_id)


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if the command came from an admin in a group; otherwise replies and returns False."""
    chat = update.effective_chat
    msg = update.effective_message
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await say(update, "This command only works in groups.")
        return False
    if msg.sender_chat and msg.sender_chat.id == chat.id:
        return True  # anonymous admin
    if not await is_admin(context, chat.id, update.effective_user.id):
        await say(update, "This command is for admins only.")
        return False
    return True


def parse_duration(text: str) -> timedelta | None:
    """'10m' -> 10 minutes, '2h' -> 2 hours, '1d' -> 1 day, '1w' -> 1 week."""
    m = re.fullmatch(r"(\d+)([mhdw])", text.lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]


async def resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE, protect_admins: bool = True):
    """Admin-only. The target is the replied-to user or a numeric user ID.
    Returns (user_id, mention_html, remaining_args) or None after replying with an error."""
    if not await require_admin(update, context):
        return None
    chat = update.effective_chat
    msg = update.effective_message
    args = list(context.args or [])
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user
        uid, mention = target.id, target.mention_html()
    elif args and args[0].isdigit():
        uid = int(args.pop(0))
        mention = f"<code>{uid}</code>"
    else:
        await say(update, "Reply to a user's message (or pass a user ID).")
        return None
    if uid == context.bot.id:
        await say(update, "I can't do that to myself 🙂")
        return None
    if protect_admins and await is_admin(context, chat.id, uid):
        await say(update, "I can't take this action on an admin.")
        return None
    return uid, mention, args


async def bot_rights_warning(context: ContextTypes.DEFAULT_TYPE, chat_id: int, *needs: str) -> str:
    """A warning line if the bot is missing admin rights (e.g. "can_delete_messages"), else ''."""
    try:
        me = await context.bot.get_chat_member(chat_id, context.bot.id)
    except TelegramError:
        return ""
    if me.status != ChatMemberStatus.ADMINISTRATOR:
        return "\n\n⚠️ I'm not an admin in this group. Please make me admin so I can do my job."
    missing = [_RIGHT_LABELS[n] for n in needs if not getattr(me, n, False)]
    if missing:
        return f"\n\n⚠️ I still need these admin permissions: <b>{', '.join(missing)}</b>."
    return ""
