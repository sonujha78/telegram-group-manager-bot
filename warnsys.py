"""
Warnings with per-group settings: limit, punishment mode, remove/reset.
The warning counters live in SQLite (db.WarnStore).
"""
import html
import logging

from telegram import ChatPermissions, Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

import db
import logs
from utils import require_admin, resolve_target, say

log = logging.getLogger("groupbot.warns")

DEFAULT_LIMIT = 3
MODES = ("ban", "kick", "mute")
warns = db.WarnStore()


def get_limit(chat_id: int) -> int:
    return int(db.get_value(chat_id, "warn_limit", str(DEFAULT_LIMIT)))


def get_mode(chat_id: int) -> str:
    return db.get_value(chat_id, "warn_mode", "ban")


async def add_warn(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, mention: str, reason: str = ""):
    """Give one warning and apply the punishment when the limit is reached.
    Returns (notice_html, count, limit, punished). Raises TelegramError if the punishment fails."""
    key = (chat_id, user_id)
    limit, mode = get_limit(chat_id), get_mode(chat_id)
    count = warns[key] + 1
    warns[key] = count
    reason_line = f"\nReason: {html.escape(reason)}" if reason else ""
    if count < limit:
        return f"⚠️ {mention} has been warned ({count}/{limit}).{reason_line}", count, limit, False
    if mode == "kick":
        await context.bot.ban_chat_member(chat_id, user_id)
        await context.bot.unban_chat_member(chat_id, user_id)
    elif mode == "mute":
        await context.bot.restrict_chat_member(chat_id, user_id, ChatPermissions.no_permissions())
    else:
        await context.bot.ban_chat_member(chat_id, user_id)
    warns.pop(key, None)
    verb = {"ban": "banned", "kick": "removed from the group", "mute": "muted"}[mode]
    return f"🔨 {mention} reached {limit}/{limit} warnings and was {verb}.{reason_line}", count, limit, True


async def _fail(update: Update, err: Exception) -> None:
    log.warning("Warn action failed: %s", err)
    await say(update, f"⚠️ Action failed: {html.escape(str(err))}\nMake sure I am an admin with the <b>Ban users</b> permission.")


async def warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context)
    if not prep:
        return
    uid, mention, args = prep
    reason = " ".join(args)
    try:
        text, count, limit, punished = await add_warn(context, update.effective_chat.id, uid, mention, reason)
    except TelegramError as e:
        await _fail(update, e)
        return
    await logs.log_action(update, context, "WARN", mention, f"{count}/{limit}" + (" - punished" if punished else "") + (f" - {reason}" if reason else ""))
    await say(update, text)


async def dwarn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context)
    if not prep:
        return
    uid, mention, args = prep
    msg = update.effective_message
    if not msg.reply_to_message:
        await say(update, "Reply to the message: it will be deleted and its sender warned.")
        return
    chat_id = update.effective_chat.id
    reason = " ".join(args)
    try:
        await context.bot.delete_message(chat_id, msg.reply_to_message.message_id)
        text, count, limit, punished = await add_warn(context, chat_id, uid, mention, reason)
    except TelegramError as e:
        await _fail(update, e)
        return
    await logs.log_action(update, context, "DWARN", mention, f"{count}/{limit}" + (" - punished" if punished else "") + (f" - {reason}" if reason else ""))
    await say(update, text)


async def warns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context, protect_admins=False)
    if not prep:
        return
    uid, mention, _ = prep
    chat_id = update.effective_chat.id
    await say(update, f"{mention} has {warns.get((chat_id, uid), 0)}/{get_limit(chat_id)} warnings.")


async def rmwarn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context, protect_admins=False)
    if not prep:
        return
    uid, mention, _ = prep
    chat_id = update.effective_chat.id
    count = warns.get((chat_id, uid), 0)
    if count == 0:
        await say(update, f"{mention} has no warnings.")
        return
    warns[(chat_id, uid)] = count - 1
    await logs.log_action(update, context, "RMWARN", mention, f"{count - 1}/{get_limit(chat_id)}")
    await say(update, f"✅ One warning removed. {mention} now has {count - 1}/{get_limit(chat_id)}.")


async def resetwarns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context, protect_admins=False)
    if not prep:
        return
    uid, mention, _ = prep
    warns.pop((update.effective_chat.id, uid), None)
    await logs.log_action(update, context, "RESETWARNS", mention)
    await say(update, f"✅ Warnings reset for {mention}.")


async def warnlimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    try:
        n = int(context.args[0])
        if not 2 <= n <= 10:
            raise ValueError
    except (IndexError, ValueError):
        await say(update, f"The warn limit is currently <b>{get_limit(chat_id)}</b>.\nChange it with /warnlimit [2-10]")
        return
    db.set_value(chat_id, "warn_limit", str(n))
    await say(update, f"✅ Warn limit set to <b>{n}</b>. Members are punished when they reach {n} warnings.")


async def warnmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    arg = context.args[0].lower() if context.args else ""
    if arg not in MODES:
        await say(update, f"The warn punishment is currently <b>{get_mode(chat_id)}</b>.\nChange it with /warnmode ban|kick|mute")
        return
    db.set_value(chat_id, "warn_mode", arg)
    await say(update, f"✅ Members who reach the warn limit will now be <b>{arg}</b>.")


def register(app: Application) -> None:
    for name, handler in (
        ("warn", warn_cmd), ("dwarn", dwarn_cmd), ("warns", warns_cmd), ("rmwarn", rmwarn_cmd),
        ("resetwarns", resetwarns_cmd), ("warnlimit", warnlimit_cmd), ("warnmode", warnmode_cmd),
    ):
        app.add_handler(CommandHandler(name, handler))
