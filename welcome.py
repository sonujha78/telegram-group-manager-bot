"""
Welcome / goodbye messages, group rules and join captcha.
Everything is stored per group in SQLite (see db.py).
"""
import html
import logging
import time

from telegram import (
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
)

import db
import ui
from utils import require_admin, say

log = logging.getLogger("groupbot.welcome")

DEFAULT_WELCOME = "Welcome {mention} to {group}! 👋\nPlease read the /rules."
DEFAULT_GOODBYE = "Goodbye {first}! 👋"
CAPTCHA_TIMEOUT = 120  # seconds a new member has to press the button
MAX_TEXT = 1000
MAX_RULES = 3000

CAPTCHA_SCHEMA = """
CREATE TABLE IF NOT EXISTS captcha (
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    expires_at REAL    NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
"""

WELCOME_HELP = (
    "👋 <b>Welcome &amp; Rules</b>\n\n"
    "<b>Admins</b>\n"
    "/welcome on|off - turn the welcome message on or off\n"
    "/setwelcome [text] - set the welcome message (or reply to a message with it)\n"
    "/resetwelcome - back to the default message\n"
    "/goodbye on|off - goodbye message (off by default)\n"
    "/setgoodbye [text] - set the goodbye message\n"
    "/resetgoodbye - back to the default message\n"
    "/captcha on|off - new members must tap a button in 2 minutes or they are removed\n"
    "/setrules [text] - set the group rules\n"
    "/clearrules - remove the rules\n\n"
    "<b>Everyone</b>\n"
    "/rules - show the group rules\n\n"
    "<b>Placeholders</b> for welcome/goodbye texts:\n"
    "{first} - first name, {mention} - clickable mention, {group} - group name"
)

_MEMBER_STATUSES = (
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.OWNER,
    ChatMemberStatus.ADMINISTRATOR,
)


# ---------------------------------------------------------------- helpers
def render(template: str, user, chat) -> str:
    """Escape the admin's text, then fill in the placeholders."""
    text = html.escape(template)
    return (
        text.replace("{first}", html.escape(user.first_name or "friend"))
        .replace("{mention}", user.mention_html())
        .replace("{group}", html.escape(chat.title or "this group"))
    )


def _text_arg(update: Update) -> str:
    """Text after the command, or the text of the replied-to message."""
    msg = update.effective_message
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        return parts[1].strip()
    reply = msg.reply_to_message
    if reply and (reply.text or reply.caption):
        return (reply.text or reply.caption).strip()
    return ""


async def _safe_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs) -> None:
    try:
        await context.bot.send_message(chat_id, text, parse_mode="HTML", **kwargs)
    except TelegramError as e:
        log.warning("Could not send message to %s: %s", chat_id, e)


def _toggler(key: str, label: str, cmd: str, default: bool):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await require_admin(update, context):
            return
        chat_id = update.effective_chat.id
        arg = context.args[0].lower() if context.args else ""
        if arg not in ("on", "off"):
            state = "ON" if db.get_bool(chat_id, key, default) else "OFF"
            await say(update, f"{label} is currently <b>{state}</b>.\nUse /{cmd} on or /{cmd} off.")
            return
        db.set_value(chat_id, key, "1" if arg == "on" else "0")
        await say(update, f"✅ {label} turned <b>{arg.upper()}</b>.")

    return handler


def _setter(key: str, label: str, cmd: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await require_admin(update, context):
            return
        text = _text_arg(update)
        if not text:
            await say(
                update,
                f"Usage: /{cmd} [text]\nYou can also reply to a message with /{cmd}.\n"
                "Placeholders: {first} {mention} {group}",
            )
            return
        if len(text) > MAX_TEXT:
            await say(update, f"That text is too long (max {MAX_TEXT} characters).")
            return
        chat = update.effective_chat
        db.set_value(chat.id, key, text)
        preview = render(text, update.effective_user, chat)
        await say(update, f"✅ {label} updated. Preview:\n\n{preview}")

    return handler


def _resetter(key: str, label: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await require_admin(update, context):
            return
        db.del_value(update.effective_chat.id, key)
        await say(update, f"✅ {label} reset to the default.")

    return handler


# ------------------------------------------------------------------ rules
async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await say(update, "Use /rules inside a group.")
        return
    rules = db.get_value(chat.id, "rules")
    if not rules:
        await say(update, "No rules have been set for this group yet.")
        return
    await say(update, f"📜 <b>Rules of {html.escape(chat.title or 'this group')}</b>\n\n{html.escape(rules)}")


async def setrules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    text = _text_arg(update)
    if not text:
        await say(update, "Usage: /setrules [text]\nYou can also reply to a message with /setrules.")
        return
    if len(text) > MAX_RULES:
        await say(update, f"The rules are too long (max {MAX_RULES} characters).")
        return
    db.set_value(update.effective_chat.id, "rules", text)
    await say(update, "✅ Rules saved. Members can read them with /rules.")


async def clearrules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    db.del_value(update.effective_chat.id, "rules")
    await say(update, "✅ Rules removed.")


# ---------------------------------------------------------------- captcha
async def _bot_can_restrict(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    try:
        me = await context.bot.get_chat_member(chat_id, context.bot.id)
    except TelegramError:
        return False
    return me.status == ChatMemberStatus.ADMINISTRATOR and bool(getattr(me, "can_restrict_members", False))


async def captcha_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    arg = context.args[0].lower() if context.args else ""
    if arg not in ("on", "off"):
        state = "ON" if db.get_bool(chat_id, "captcha_on", False) else "OFF"
        await say(update, f"Captcha is currently <b>{state}</b>.\nUse /captcha on or /captcha off.")
        return
    if arg == "on" and not await _bot_can_restrict(context, chat_id):
        await say(
            update,
            "⚠️ I need to be an admin with the <b>Ban users</b> permission to run the captcha. "
            "Please update my admin rights and try again.",
        )
        return
    db.set_value(chat_id, "captcha_on", "1" if arg == "on" else "0")
    await say(update, f"✅ Captcha turned <b>{arg.upper()}</b>.")


def _pop_pending(chat_id: int, user_id: int):
    """Remove a pending captcha and return its message id (None if there is none)."""
    rows = db.query("SELECT message_id FROM captcha WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    if not rows:
        return None
    db.execute("DELETE FROM captcha WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    return rows[0][0]


def _schedule_timeout(job_queue, chat_id: int, user_id: int, delay: float) -> None:
    job_queue.run_once(
        _captcha_timeout, max(1, delay), chat_id=chat_id, user_id=user_id, name=f"cap:{chat_id}:{user_id}"
    )


async def _start_captcha(context: ContextTypes.DEFAULT_TYPE, chat, user) -> bool:
    """Mute the new member and ask them to press a button. Returns False if it could not start."""
    if context.job_queue is None:
        log.warning("JobQueue missing - install python-telegram-bot[job-queue]")
        return False
    try:
        await context.bot.restrict_chat_member(chat.id, user.id, ChatPermissions.no_permissions())
    except TelegramError as e:
        log.warning("Captcha: cannot restrict %s in %s: %s", user.id, chat.id, e)
        return False
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ I'm not a robot", callback_data=f"cap:{user.id}")]])
    try:
        msg = await context.bot.send_message(
            chat.id,
            f"👋 Welcome {user.mention_html()}!\n"
            f"Tap the button within {CAPTCHA_TIMEOUT} seconds to prove you're human, "
            "or you will be removed from the group.",
            parse_mode="HTML",
            reply_markup=kb,
        )
    except TelegramError as e:
        log.warning("Captcha: cannot send message in %s: %s", chat.id, e)
        await context.bot.restrict_chat_member(chat.id, user.id, ChatPermissions.all_permissions())
        return False
    db.execute(
        "INSERT OR REPLACE INTO captcha (chat_id, user_id, message_id, expires_at) VALUES (?, ?, ?, ?)",
        (chat.id, user.id, msg.message_id, time.time() + CAPTCHA_TIMEOUT),
    )
    _schedule_timeout(context.job_queue, chat.id, user.id, CAPTCHA_TIMEOUT)
    return True


async def _captcha_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, user_id = context.job.chat_id, context.job.user_id
    rows = db.query("SELECT message_id, expires_at FROM captcha WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    if not rows or rows[0][1] - time.time() > 2:
        return  # already verified / left, or a newer captcha is pending
    message_id = rows[0][0]
    db.execute("DELETE FROM captcha WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await context.bot.unban_chat_member(chat_id, user_id)  # removed, but can rejoin
    except TelegramError as e:
        log.warning("Captcha timeout: cannot remove %s from %s: %s", user_id, chat_id, e)
    try:
        await context.bot.delete_message(chat_id, message_id)
    except TelegramError:
        pass


async def on_captcha_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    target = int(q.data.split(":")[1])
    if q.from_user.id != target:
        await q.answer("This button is not for you 🙂", show_alert=True)
        return
    chat = q.message.chat
    if _pop_pending(chat.id, target) is None:
        await q.answer("This verification has expired.", show_alert=True)
        return
    try:
        full = await context.bot.get_chat(chat.id)
        perms = full.permissions or ChatPermissions.all_permissions()
        await context.bot.restrict_chat_member(chat.id, target, perms)
    except TelegramError as e:
        log.warning("Captcha: cannot unrestrict %s in %s: %s", target, chat.id, e)
        await q.answer("Something went wrong. Please ask an admin to unmute you.", show_alert=True)
        return
    await q.answer("✅ Verified! Welcome.")
    try:
        await q.message.delete()
    except TelegramError:
        pass
    await _send_welcome(context, chat, q.from_user)


async def _restore_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    """After a restart, re-schedule the timeouts of captchas that were still pending."""
    now = time.time()
    for chat_id, user_id, _message_id, expires_at in db.query(
        "SELECT chat_id, user_id, message_id, expires_at FROM captcha"
    ):
        _schedule_timeout(context.job_queue, chat_id, user_id, expires_at - now)


# ------------------------------------------------------- join / leave events
async def _send_welcome(context: ContextTypes.DEFAULT_TYPE, chat, user) -> None:
    if not db.get_bool(chat.id, "welcome_on", True):
        return
    text = render(db.get_value(chat.id, "welcome_text", DEFAULT_WELCOME), user, chat)
    await _safe_send(context, chat.id, text)


async def _send_goodbye(context: ContextTypes.DEFAULT_TYPE, chat, user) -> None:
    if not db.get_bool(chat.id, "goodbye_on", False):
        return
    text = render(db.get_value(chat.id, "goodbye_text", DEFAULT_GOODBYE), user, chat)
    await _safe_send(context, chat.id, text)


def _member_change(cmu: ChatMemberUpdated):
    """Return (was_member, is_member, new_status), or None if membership did not change."""
    diff = cmu.difference()
    status_change = diff.get("status")
    member_change = diff.get("is_member")
    if status_change is None and member_change is None:
        return None
    old_status, new_status = status_change or (cmu.old_chat_member.status, cmu.new_chat_member.status)
    old_is_member, new_is_member = member_change or (None, None)
    was_member = old_status in _MEMBER_STATUSES or (old_status == ChatMemberStatus.RESTRICTED and old_is_member is True)
    is_member = new_status in _MEMBER_STATUSES or (new_status == ChatMemberStatus.RESTRICTED and new_is_member is True)
    return was_member, is_member, new_status


async def on_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmu = update.chat_member
    change = _member_change(cmu)
    if change is None:
        return
    was_member, is_member, new_status = change
    user = cmu.new_chat_member.user
    chat = cmu.chat
    if user.is_bot:
        return
    if not was_member and is_member:
        if db.get_bool(chat.id, "captcha_on", False) and await _start_captcha(context, chat, user):
            return  # the welcome message is sent after the captcha is solved
        await _send_welcome(context, chat, user)
    elif was_member and not is_member:
        message_id = _pop_pending(chat.id, user.id)
        if message_id is not None:
            try:
                await context.bot.delete_message(chat.id, message_id)
            except TelegramError:
                pass
        if new_status == ChatMemberStatus.LEFT:  # they left on their own (not kicked/banned)
            await _send_goodbye(context, chat, user)


# --------------------------------------------------------------- register
def register(app: Application) -> None:
    db.ensure(CAPTCHA_SCHEMA)
    ui.SECTIONS["welcome"] = WELCOME_HELP

    app.add_handler(CommandHandler("welcome", _toggler("welcome_on", "Welcome message", "welcome", True)))
    app.add_handler(CommandHandler("setwelcome", _setter("welcome_text", "Welcome message", "setwelcome")))
    app.add_handler(CommandHandler("resetwelcome", _resetter("welcome_text", "Welcome message")))
    app.add_handler(CommandHandler("goodbye", _toggler("goodbye_on", "Goodbye message", "goodbye", False)))
    app.add_handler(CommandHandler("setgoodbye", _setter("goodbye_text", "Goodbye message", "setgoodbye")))
    app.add_handler(CommandHandler("resetgoodbye", _resetter("goodbye_text", "Goodbye message")))
    app.add_handler(CommandHandler("captcha", captcha_cmd))
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("setrules", setrules_cmd))
    app.add_handler(CommandHandler("clearrules", clearrules_cmd))
    app.add_handler(ChatMemberHandler(on_member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(on_captcha_button, pattern=r"^cap:\d+$"))

    if app.job_queue is not None:
        app.job_queue.run_once(_restore_pending, 3)
    else:
        log.warning("JobQueue missing - captcha timeouts will not work. Install python-telegram-bot[job-queue]")
