"""
Admin tools: admin list, promote/demote, pin, purge, extra bans, approval, reports, misc.
"""
import html
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import db
import ui
import utils
from utils import is_admin, parse_duration, require_admin, resolve_target, say

log = logging.getLogger("groupbot.admin")

MAX_PURGE = 200  # messages

ADMIN_HELP = (
    "🛠 <b>Admin Tools</b>\n\n"
    "Admins only, except /admins and /kickme.\n\n"
    "/admins - list the group admins\n"
    "/promote - make a user admin (reply or ID)\n"
    "/demote - remove admin rights\n"
    "/pin - pin the replied message (add <b>loud</b> to notify everyone)\n"
    "/unpin - unpin the replied message (or the latest pin)\n"
    "/unpinall - unpin all messages\n"
    "/purge - delete every message from the replied one down to your command\n"
    "/del - delete the replied message\n"
    "/tban [10m|2h|1d|1w] [reason] - temporary ban\n"
    "/dban - delete the replied message and ban its sender\n"
    "/kickme - remove yourself from the group"
)

APPROVAL_HELP = (
    "✅ <b>Approval &amp; Reports</b>\n\n"
    "<b>Approval</b> - approved users are ignored by anti-spam and locks\n"
    "/approve - approve a user (reply or ID)\n"
    "/unapprove - remove the approval\n"
    "/approved - list approved users\n"
    "/approval - check if a user is approved\n\n"
    "<b>Reports</b> - members can call the admins\n"
    "/report - reply to a message with it (or write @admin)\n"
    "/reports on|off - allow or block reports (default: on)"
)

MISC_HELP = (
    "ℹ️ <b>Misc</b>\n\n"
    "/id - show chat and user IDs (reply to a user to get theirs)\n"
    "/info - show details about a user (reply) or about yourself\n"
    "/privacy - what data this bot stores"
)

PRIVACY_TEXT = (
    "🔐 <b>Privacy</b>\n\n"
    "This bot stores, per group: settings (welcome/goodbye text, rules, locks, banned words, allowed domains), "
    "notes and filters, warning counts, approved users and pending captchas.\n\n"
    "It does <b>not</b> save or log the content of chat messages. It only reads messages while they "
    "arrive, to apply your group's rules (anti-spam, locks, filters). "
    "Data stays on the server that runs the bot. Removing the bot from a group does not delete that group's settings; "
    "ask the bot owner if you want them erased."
)


# ---------------------------------------------------------------- helpers
def _in_group(update: Update) -> bool:
    return update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


async def _fail(update: Update, err: Exception, hint: str = "") -> None:
    log.warning("Admin action failed: %s", err)
    await say(update, f"⚠️ Action failed: {html.escape(str(err))}" + (f"\n{hint}" if hint else ""))


async def _delete_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id, message_id)
    except TelegramError:
        pass


async def _temp_notice(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, ttl: int = 5) -> None:
    try:
        m = await context.bot.send_message(chat_id, text, parse_mode="HTML")
    except TelegramError:
        return
    if context.job_queue is not None:
        context.job_queue.run_once(_delete_job, ttl, data=(chat_id, m.message_id))


# ----------------------------------------------------------------- admins
async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _in_group(update):
        await say(update, "Use /admins inside a group.")
        return
    chat = update.effective_chat
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
    except TelegramError as e:
        await _fail(update, e)
        return
    lines = []
    for a in admins:
        if a.user.is_bot:
            continue
        icon = "👑" if a.status == ChatMemberStatus.OWNER else "⭐"
        lines.append(f"{icon} {html.escape(a.user.full_name)}")
    await say(update, f"👮 <b>Admins of {html.escape(chat.title or 'this group')}</b>\n" + "\n".join(lines))


async def _can_promote(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    return m.status == ChatMemberStatus.OWNER or (
        m.status == ChatMemberStatus.ADMINISTRATOR and bool(getattr(m, "can_promote_members", False))
    )


async def promote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context, protect_admins=False)
    if not prep:
        return
    uid, mention, _ = prep
    chat_id = update.effective_chat.id
    if not await _can_promote(context, chat_id, update.effective_user.id):
        await say(update, "You need the <b>Add new admins</b> permission to use this command.")
        return
    try:
        await context.bot.promote_chat_member(
            chat_id,
            uid,
            can_delete_messages=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_invite_users=True,
            can_manage_video_chats=True,
        )
    except TelegramError as e:
        await _fail(update, e, "I need the <b>Add new admins</b> permission myself.")
        return
    await say(update, f"⭐ {mention} is now an admin.")


async def demote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context, protect_admins=False)
    if not prep:
        return
    uid, mention, _ = prep
    chat_id = update.effective_chat.id
    if not await _can_promote(context, chat_id, update.effective_user.id):
        await say(update, "You need the <b>Add new admins</b> permission to use this command.")
        return
    try:
        await context.bot.promote_chat_member(
            chat_id,
            uid,
            can_change_info=False,
            can_delete_messages=False,
            can_restrict_members=False,
            can_pin_messages=False,
            can_invite_users=False,
            can_promote_members=False,
            can_manage_video_chats=False,
        )
    except TelegramError as e:
        await _fail(update, e, "I can only demote admins that I promoted myself.")
        return
    await say(update, f"⬇️ {mention} is no longer an admin.")


# -------------------------------------------------------------------- pin
async def pin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message:
        await say(update, "Reply to the message you want to pin.")
        return
    loud = bool(context.args) and context.args[0].lower() in ("loud", "notify")
    try:
        await context.bot.pin_chat_message(update.effective_chat.id, msg.reply_to_message.message_id, disable_notification=not loud)
    except TelegramError as e:
        await _fail(update, e, "I need the <b>Pin messages</b> permission.")
        return
    await say(update, "📌 Message pinned.")


async def unpin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    msg = update.effective_message
    target = msg.reply_to_message.message_id if msg.reply_to_message else None
    try:
        await context.bot.unpin_chat_message(update.effective_chat.id, message_id=target)
    except TelegramError as e:
        await _fail(update, e, "I need the <b>Pin messages</b> permission.")
        return
    await say(update, "📌 Message unpinned.")


async def unpinall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    try:
        await context.bot.unpin_all_chat_messages(update.effective_chat.id)
    except TelegramError as e:
        await _fail(update, e, "I need the <b>Pin messages</b> permission.")
        return
    await say(update, "📌 All messages unpinned.")


# ------------------------------------------------------------------ purge
async def purge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message:
        await say(update, "Reply to the first message you want to delete. I will delete everything from there down to your command.")
        return
    first, last = msg.reply_to_message.message_id, msg.message_id
    if last - first > MAX_PURGE:
        await say(update, f"That is too many messages (max {MAX_PURGE}).")
        return
    chat_id = update.effective_chat.id
    ids = list(range(first, last + 1))
    try:
        for i in range(0, len(ids), 100):
            await context.bot.delete_messages(chat_id, ids[i : i + 100])
    except TelegramError as e:
        await _fail(update, e, "I need the <b>Delete messages</b> permission. Telegram only lets bots delete messages newer than 48 hours.")
        return
    await _temp_notice(context, chat_id, "🧹 Purge complete.")


async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message:
        await say(update, "Reply to the message you want to delete.")
        return
    try:
        await context.bot.delete_messages(update.effective_chat.id, [msg.reply_to_message.message_id, msg.message_id])
    except TelegramError as e:
        await _fail(update, e, "I need the <b>Delete messages</b> permission.")


# -------------------------------------------------------------- extra bans
async def tban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context)
    if not prep:
        return
    uid, mention, args = prep
    delta = parse_duration(args[0]) if args else None
    if delta is None:
        await say(update, "Usage: /tban [10m|2h|1d|1w] [reason]\nReply to the user's message (or pass a user ID first).")
        return
    label, reason = args[0], " ".join(args[1:])
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid, until_date=datetime.now(timezone.utc) + delta)
    except TelegramError as e:
        await _fail(update, e, "I need the <b>Ban users</b> permission.")
        return
    await say(update, f"⏳ {mention} is banned for {html.escape(label)}." + (f"\nReason: {html.escape(reason)}" if reason else ""))


async def dban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context)
    if not prep:
        return
    uid, mention, _ = prep
    msg = update.effective_message
    if not msg.reply_to_message:
        await say(update, "Reply to the user's message: it will be deleted and the user banned.")
        return
    chat_id = update.effective_chat.id
    try:
        await context.bot.delete_message(chat_id, msg.reply_to_message.message_id)
        await context.bot.ban_chat_member(chat_id, uid)
    except TelegramError as e:
        await _fail(update, e, "I need the <b>Delete messages</b> and <b>Ban users</b> permissions.")
        return
    await say(update, f"🔨 {mention} was banned and their message deleted.")


async def kickme_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _in_group(update):
        await say(update, "Use /kickme inside a group.")
        return
    chat_id, user = update.effective_chat.id, update.effective_user
    if await is_admin(context, chat_id, user.id):
        await say(update, "I can't remove admins.")
        return
    try:
        await context.bot.ban_chat_member(chat_id, user.id)
        await context.bot.unban_chat_member(chat_id, user.id)
    except TelegramError as e:
        await _fail(update, e, "I need the <b>Ban users</b> permission.")
        return
    await say(update, f"👋 Bye {html.escape(user.first_name)}!")


# ---------------------------------------------------------------- approval
def _target_name(update: Update, uid: int) -> str:
    reply = update.effective_message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == uid:
        return reply.from_user.full_name
    return str(uid)


async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context, protect_admins=False)
    if not prep:
        return
    uid, mention, _ = prep
    utils.ensure_approved_table()
    db.execute(
        "INSERT OR REPLACE INTO approved (chat_id, user_id, name) VALUES (?, ?, ?)",
        (update.effective_chat.id, uid, _target_name(update, uid)),
    )
    await say(update, f"✅ {mention} is approved. Anti-spam and locks will ignore them.")


async def unapprove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context, protect_admins=False)
    if not prep:
        return
    uid, mention, _ = prep
    chat_id = update.effective_chat.id
    if not utils.is_approved(chat_id, uid):
        await say(update, f"{mention} is not approved.")
        return
    db.execute("DELETE FROM approved WHERE chat_id=? AND user_id=?", (chat_id, uid))
    await say(update, f"✅ {mention} is no longer approved.")


async def approved_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    utils.ensure_approved_table()
    rows = db.query("SELECT user_id, name FROM approved WHERE chat_id=? ORDER BY name", (update.effective_chat.id,))
    if not rows:
        await say(update, "Nobody is approved yet. Use /approve (reply to a user).")
        return
    await say(update, "✅ <b>Approved users</b>\n" + "\n".join(f"{html.escape(n)} (<code>{u}</code>)" for u, n in rows))


async def approval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context, protect_admins=False)
    if not prep:
        return
    uid, mention, _ = prep
    ok = utils.is_approved(update.effective_chat.id, uid)
    await say(update, f"{mention} is {'✅ approved' if ok else '❌ not approved'}.")


# ---------------------------------------------------------------- reports
async def _admin_pings(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    """Invisible mentions (zero-width links) that notify every human admin."""
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
    except TelegramError:
        return ""
    return "".join(f'<a href="tg://user?id={a.user.id}">\u200b</a>' for a in admins if not a.user.is_bot)


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _in_group(update):
        await say(update, "Use /report inside a group.")
        return
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if not db.get_bool(chat.id, "reports_on", True):
        return
    reported = msg.reply_to_message
    if reported is None or reported.from_user is None:
        await say(update, "Reply to the message you want to report.")
        return
    target = reported.from_user
    if target.id == user.id:
        await say(update, "You can't report yourself.")
        return
    if await is_admin(context, chat.id, target.id):
        await say(update, "You can't report an admin 🙂")
        return
    pings = await _admin_pings(context, chat.id)
    text = f"🚨 <b>Report</b> by {html.escape(user.full_name)}\nReported user: {html.escape(target.full_name)}{pings}"
    try:
        await reported.reply_text(text, parse_mode="HTML")
    except TelegramError as e:
        log.warning("Could not send report in %s: %s", chat.id, e)


async def reports_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    arg = context.args[0].lower() if context.args else ""
    if arg not in ("on", "off"):
        state = "ON" if db.get_bool(chat_id, "reports_on", True) else "OFF"
        await say(update, f"Reports are currently <b>{state}</b>.\nUse /reports on or /reports off.")
        return
    db.set_value(chat_id, "reports_on", "1" if arg == "on" else "0")
    await say(update, f"✅ Reports turned <b>{arg.upper()}</b>.")


# ------------------------------------------------------------------- misc
async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    lines = [f"👤 Your ID: <code>{user.id}</code>"]
    if chat.type != ChatType.PRIVATE:
        lines.insert(0, f"💬 Chat ID: <code>{chat.id}</code>")
    if msg.reply_to_message and msg.reply_to_message.from_user:
        lines.append(f"↩️ Replied user's ID: <code>{msg.reply_to_message.from_user.id}</code>")
    await say(update, "\n".join(lines))


async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, msg = update.effective_chat, update.effective_message
    target = msg.reply_to_message.from_user if msg.reply_to_message and msg.reply_to_message.from_user else update.effective_user
    lines = [
        "ℹ️ <b>User info</b>",
        f"ID: <code>{target.id}</code>",
        f"Name: {html.escape(target.full_name)}",
    ]
    if target.username:
        lines.append(f"Username: @{html.escape(target.username)}")
    if target.is_bot:
        lines.append("This account is a bot 🤖")
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        try:
            member = await context.bot.get_chat_member(chat.id, target.id)
            lines.append(f"Status in this group: {member.status}")
        except TelegramError:
            pass
    await say(update, "\n".join(lines))


async def privacy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await say(update, PRIVACY_TEXT)


# --------------------------------------------------------------- register
def register(app: Application) -> None:
    utils.ensure_approved_table()
    ui.SECTIONS["admin"] = ADMIN_HELP
    ui.SECTIONS["approval"] = APPROVAL_HELP
    ui.SECTIONS["misc"] = MISC_HELP

    for name, handler in (
        ("admins", admins_cmd), ("promote", promote_cmd), ("demote", demote_cmd),
        ("pin", pin_cmd), ("unpin", unpin_cmd), ("unpinall", unpinall_cmd),
        ("purge", purge_cmd), ("del", del_cmd),
        ("tban", tban_cmd), ("dban", dban_cmd), ("kickme", kickme_cmd),
        ("approve", approve_cmd), ("unapprove", unapprove_cmd), ("approved", approved_cmd), ("approval", approval_cmd),
        ("report", report_cmd), ("reports", reports_cmd),
        ("id", id_cmd), ("info", info_cmd), ("privacy", privacy_cmd),
    ):
        app.add_handler(CommandHandler(name, handler))
    # "@admin ..." works like /report
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.Regex(r"(?i)^@admins?\b"), report_cmd), group=2)
