"""
Log channel: admin actions are copied to a channel that the group's admins choose.

Setup (proves that a channel admin agrees):
  1. Add the bot to the channel as an admin (it must be allowed to post).
  2. Post /setlog in the channel. The bot answers with a message that contains a code.
  3. Forward that message to the group and reply to it with /setlog.
"""
import html
import logging
import re
import secrets
import time

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import db
import ui
from utils import require_admin, say

log = logging.getLogger("groupbot.logs")

SETUP_TTL = 600  # seconds the setup code stays valid (and the setup messages stay visible)

LOGS_HELP = (
    "📋 <b>Log Channel</b>\n\n"
    "Copies admin actions (bans, mutes, warnings, purges, anti-spam, anti-raid...) to a channel.\n\n"
    "<b>Setup</b>\n"
    "1. Add me to your channel as an admin (I must be allowed to post)\n"
    "2. Post /setlog in the channel\n"
    "3. Forward my answer to the group and reply to it with /setlog\n\n"
    "/setlog - connect the log channel (see above)\n"
    "/unsetlog - stop logging\n"
    "/logchannel - show the current log channel"
)

_pending: dict[str, tuple[int, float]] = {}  # setup code -> (channel_id, expires_at)


# ---------------------------------------------------------------- sending
async def log_event(context: ContextTypes.DEFAULT_TYPE, chat, text: str) -> None:
    """Send a ready-made HTML message to the group's log channel (if one is set)."""
    channel_id = db.get_value(chat.id, "log_channel")
    if not channel_id:
        return
    try:
        await context.bot.send_message(int(channel_id), text, parse_mode="HTML")
    except TelegramError as e:
        log.warning("Could not write to log channel %s (group %s): %s", channel_id, chat.id, e)


async def log_action(update: Update, context: ContextTypes.DEFAULT_TYPE, tag: str, target: str | None = None, note: str = "") -> None:
    """Log something an admin did. `target` is an HTML mention/name of the affected user."""
    chat, actor = update.effective_chat, update.effective_user
    if not db.get_value(chat.id, "log_channel"):
        return
    admin_name = "Anonymous admin" if update.effective_message.sender_chat else html.escape(actor.full_name)
    lines = [f"🛡 <b>{html.escape(chat.title or 'group')}</b>", f"#{tag}", f"<b>Admin:</b> {admin_name}"]
    if target:
        lines.append(f"<b>User:</b> {target}")
    if note and note.strip():
        lines.append(f"<b>Note:</b> {html.escape(note.strip())}")
    await log_event(context, chat, "\n".join(lines))


# ----------------------------------------------------------------- setup
async def _delete_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id, message_id)
    except TelegramError:
        pass


async def on_channel_setlog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setlog posted inside a channel."""
    post = update.channel_post
    if post is None:
        return
    for code, (_, expires) in list(_pending.items()):  # forget old codes
        if expires < time.time():
            del _pending[code]
    code = secrets.token_hex(4)
    _pending[code] = (post.chat_id, time.time() + SETUP_TTL)
    try:
        answer = await context.bot.send_message(
            post.chat_id,
            "🔧 <b>Log channel setup</b>\n"
            "Forward this message to your group and reply to it with /setlog\n"
            f"Code: <code>{code}</code> (valid for {SETUP_TTL // 60} minutes)",
            parse_mode="HTML",
        )
    except TelegramError as e:
        log.warning("Could not answer /setlog in channel %s: %s", post.chat_id, e)
        return
    if context.job_queue is not None:  # remove the setup message and the command afterwards
        context.job_queue.run_once(_delete_job, SETUP_TTL, data=(post.chat_id, answer.message_id))
        context.job_queue.run_once(_delete_job, SETUP_TTL, data=(post.chat_id, post.message_id))


async def setlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setlog in the group, as a reply to the forwarded setup message."""
    if not await require_admin(update, context):
        return
    chat = update.effective_chat
    reply = update.effective_message.reply_to_message
    origin = getattr(reply, "forward_origin", None) if reply else None
    channel = getattr(origin, "chat", None)
    code_match = re.search(r"Code:\s*([0-9a-f]{8})", (reply.text or "") if reply else "")
    usage = (
        "To connect a log channel:\n"
        "1. Add me to the channel as an admin\n"
        "2. Post /setlog in the channel\n"
        "3. Forward my answer here and reply to it with /setlog"
    )
    if channel is None or code_match is None:
        await say(update, usage)
        return
    entry = _pending.get(code_match.group(1))
    if entry is None or entry[1] < time.time() or entry[0] != channel.id:
        await say(update, "That setup message has expired. Post /setlog in the channel again.\n\n" + usage)
        return
    try:
        await context.bot.send_message(
            channel.id,
            f"✅ This channel is now the log channel of <b>{html.escape(chat.title or 'a group')}</b>.",
            parse_mode="HTML",
        )
    except TelegramError as e:
        await say(update, f"⚠️ I can't post in that channel: {html.escape(str(e))}\nMake me a channel admin with permission to post.")
        return
    del _pending[code_match.group(1)]
    db.set_value(chat.id, "log_channel", str(channel.id))
    await say(update, f"✅ Log channel set: <b>{html.escape(channel.title or str(channel.id))}</b>")


async def unsetlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    if not db.get_value(chat_id, "log_channel"):
        await say(update, "No log channel is set.")
        return
    db.del_value(chat_id, "log_channel")
    await say(update, "✅ Logging stopped. The log channel is disconnected.")


async def logchannel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    channel_id = db.get_value(update.effective_chat.id, "log_channel")
    if not channel_id:
        await say(update, "No log channel is set. See /setlog")
        return
    try:
        title = (await context.bot.get_chat(int(channel_id))).title or channel_id
    except TelegramError:
        title = channel_id
    await say(update, f"📋 Log channel: <b>{html.escape(str(title))}</b>")


# --------------------------------------------------------------- register
def register(app: Application) -> None:
    ui.add_section("logs", "📋 Log Channel", LOGS_HELP)
    app.add_handler(CommandHandler("setlog", setlog_cmd))
    app.add_handler(CommandHandler("unsetlog", unsetlog_cmd))
    app.add_handler(CommandHandler("logchannel", logchannel_cmd))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & filters.Regex(r"^/setlog\b"), on_channel_setlog))
