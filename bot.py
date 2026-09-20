"""
Telegram Group Manager Bot
Phase 1: admin commands - ban / unban / kick / mute / unmute / warn
"""
import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    ChatPermissions,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

import admin
import antispam
import db
import locks
import notes
import ui
import welcome

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
MAX_WARNS = 3

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("groupbot")

# Warnings are stored in SQLite (see db.py).
warns = db.WarnStore()

ADMIN_COMMANDS = [
    BotCommand("ban", "Ban a user"),
    BotCommand("unban", "Remove a ban"),
    BotCommand("kick", "Remove a user from the group"),
    BotCommand("mute", "Mute a user"),
    BotCommand("unmute", "Remove a mute"),
    BotCommand("warn", "Warn a user"),
    BotCommand("warns", "Show a user's warnings"),
    BotCommand("resetwarns", "Reset a user's warnings"),
]


# ---------------------------------------------------------------- helpers
def parse_duration(text: str) -> timedelta | None:
    """'10m' -> 10 minutes, '2h' -> 2 hours, '1d' -> 1 day."""
    m = re.fullmatch(r"(\d+)([mhd])", text.lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    return timedelta(days=n)


async def _say(update: Update, text: str) -> None:
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def _is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def _fail(update: Update, err: Exception) -> None:
    log.warning("Action failed: %s", err)
    await _say(
        update,
        f"⚠️ Action failed: {html.escape(str(err))}\n"
        "Make sure the bot is an admin and has the 'Ban users' / 'Restrict members' permissions.",
    )


async def _prepare(update: Update, context: ContextTypes.DEFAULT_TYPE, protect_admins: bool = True):
    """Common checks. Returns (target_id, target_mention, remaining_args) or None."""
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await _say(update, "This command only works in groups.")
        return None
    if not await _is_admin(context, chat.id, user.id):
        await _say(update, "This command is for admins only.")
        return None

    args = list(context.args or [])
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user
        target_id, mention = target.id, target.mention_html()
    elif args and args[0].isdigit():
        target_id = int(args.pop(0))
        mention = f"<code>{target_id}</code>"
    else:
        await _say(update, "Reply to a user's message (or pass a user ID).")
        return None

    if target_id == context.bot.id:
        await _say(update, "I can't do that to myself 🙂")
        return None
    if protect_admins and await _is_admin(context, chat.id, target_id):
        await _say(update, "I can't take this action on an admin.")
        return None
    return target_id, mention, args


# --------------------------------------------------------------- commands
async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await _prepare(update, context)
    if not prep:
        return
    uid, mention, args = prep
    reason = " ".join(args)
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid)
    except TelegramError as e:
        await _fail(update, e)
        return
    text = f"🔨 {mention} has been banned."
    if reason:
        text += f"\nReason: {html.escape(reason)}"
    await _say(update, text)


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await _prepare(update, context, protect_admins=False)
    if not prep:
        return
    uid, mention, _ = prep
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, uid, only_if_banned=True)
    except TelegramError as e:
        await _fail(update, e)
        return
    await _say(update, f"✅ {mention} has been unbanned.")


async def kick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await _prepare(update, context)
    if not prep:
        return
    uid, mention, _ = prep
    chat_id = update.effective_chat.id
    try:
        await context.bot.ban_chat_member(chat_id, uid)
        await context.bot.unban_chat_member(chat_id, uid)  # unbanning right away lets them rejoin
    except TelegramError as e:
        await _fail(update, e)
        return
    await _say(update, f"👢 {mention} has been removed from the group.")


async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await _prepare(update, context)
    if not prep:
        return
    uid, mention, args = prep
    until, label = None, "permanent"
    if args:
        delta = parse_duration(args[0])
        if delta:
            until = datetime.now(timezone.utc) + delta
            label = args.pop(0)
    reason = " ".join(args)
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id,
            uid,
            ChatPermissions.no_permissions(),
            until_date=until,
        )
    except TelegramError as e:
        await _fail(update, e)
        return
    text = f"🔇 {mention} has been muted ({label})."
    if reason:
        text += f"\nReason: {html.escape(reason)}"
    await _say(update, text)


async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await _prepare(update, context, protect_admins=False)
    if not prep:
        return
    uid, mention, _ = prep
    chat_id = update.effective_chat.id
    try:
        chat = await context.bot.get_chat(chat_id)
        perms = chat.permissions or ChatPermissions.all_permissions()
        await context.bot.restrict_chat_member(chat_id, uid, perms)
    except TelegramError as e:
        await _fail(update, e)
        return
    await _say(update, f"🔊 {mention} has been unmuted.")


async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await _prepare(update, context)
    if not prep:
        return
    uid, mention, args = prep
    chat_id = update.effective_chat.id
    reason = " ".join(args)

    warns[(chat_id, uid)] += 1
    count = warns[(chat_id, uid)]
    text = f"⚠️ {mention} has been warned ({count}/{MAX_WARNS})."
    if reason:
        text += f"\nReason: {html.escape(reason)}"

    if count >= MAX_WARNS:
        try:
            await context.bot.ban_chat_member(chat_id, uid)
        except TelegramError as e:
            warns[(chat_id, uid)] -= 1
            await _fail(update, e)
            return
        warns.pop((chat_id, uid), None)
        text += f"\n🔨 Reached {MAX_WARNS} warnings, so the user has been banned."
    await _say(update, text)


async def warns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await _prepare(update, context, protect_admins=False)
    if not prep:
        return
    uid, mention, _ = prep
    count = warns.get((update.effective_chat.id, uid), 0)
    await _say(update, f"{mention} has {count}/{MAX_WARNS} warnings.")


async def resetwarns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await _prepare(update, context, protect_admins=False)
    if not prep:
        return
    uid, mention, _ = prep
    warns.pop((update.effective_chat.id, uid), None)
    await _say(update, f"✅ Warnings reset for {mention}.")


# ------------------------------------------------------------------- main
async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [BotCommand("start", "Start the bot"), BotCommand("help", "Show the command list")]
    )
    await app.bot.set_my_commands(
        [BotCommand("help", "Show the command list")] + ADMIN_COMMANDS,
        scope=BotCommandScopeAllChatAdministrators(),
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)


def main() -> None:
    if not TOKEN:
        raise SystemExit("BOT_TOKEN not found. Add BOT_TOKEN=... to your .env file.")

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .connect_timeout(20)
        .read_timeout(20)
        .write_timeout(20)
        .pool_timeout(20)
        .build()
    )
    ui.register(app)
    welcome.register(app)
    antispam.register(app)
    notes.register(app)
    locks.register(app)
    admin.register(app)
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("kick", kick))
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("unmute", unmute))
    app.add_handler(CommandHandler("warn", warn))
    app.add_handler(CommandHandler("warns", warns_cmd))
    app.add_handler(CommandHandler("resetwarns", resetwarns))
    app.add_error_handler(on_error)

    log.info("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
