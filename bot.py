"""
Telegram Group Manager Bot - entry point.
The moderation commands live here; every other feature is its own module.
"""
import html
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    ChatPermissions,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

import admin
import antiraid
import antispam
import disabling
import locks
import logs
import notes
import ui
import warnsys
import welcome
from utils import parse_duration, resolve_target, say

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("groupbot")

MOD_HELP = (
    "👮 <b>Moderation</b>\n\n"
    "Admin only. Reply to a user's message, or pass a user ID.\n\n"
    "/ban [reason] - ban a user\n"
    "/unban - remove a ban\n"
    "/kick - remove a user (they can rejoin)\n"
    "/mute [10m | 2h | 1d | 1w] - mute a user (permanent if no duration)\n"
    "/unmute - remove a mute\n\n"
    "<b>Warnings</b>\n"
    "/warn [reason] - warn a user\n"
    "/dwarn [reason] - warn and delete the replied message\n"
    "/warns - show a user's warnings\n"
    "/rmwarn - remove one warning\n"
    "/resetwarns - remove all warnings\n"
    "/warnlimit [2-10] - warnings before the punishment (default 3)\n"
    "/warnmode [ban|kick|mute] - the punishment (default ban)"
)

DEFAULT_COMMANDS = [
    BotCommand("start", "Start the bot"),
    BotCommand("help", "Show the command list"),
    BotCommand("rules", "Show the group rules"),
    BotCommand("notes", "List the saved notes"),
    BotCommand("report", "Report a message to the admins"),
    BotCommand("id", "Show IDs"),
]

ADMIN_COMMANDS = DEFAULT_COMMANDS + [
    BotCommand("ban", "Ban a user"),
    BotCommand("unban", "Remove a ban"),
    BotCommand("kick", "Remove a user from the group"),
    BotCommand("mute", "Mute a user"),
    BotCommand("unmute", "Remove a mute"),
    BotCommand("warn", "Warn a user"),
    BotCommand("tban", "Temporary ban"),
    BotCommand("purge", "Delete many messages"),
    BotCommand("pin", "Pin the replied message"),
    BotCommand("lock", "Lock a message type"),
    BotCommand("antiraid", "Anti-raid mode"),
    BotCommand("setwelcome", "Set the welcome message"),
    BotCommand("setrules", "Set the group rules"),
    BotCommand("save", "Save a note"),
    BotCommand("filter", "Add a filter"),
]


async def _fail(update: Update, err: Exception) -> None:
    log.warning("Action failed: %s", err)
    await say(
        update,
        f"⚠️ Action failed: {html.escape(str(err))}\n"
        "Make sure I am an admin with the 'Ban users' / 'Restrict members' permissions.",
    )


# --------------------------------------------------------------- commands
async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context)
    if not prep:
        return
    uid, mention, args = prep
    reason = " ".join(args)
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid)
    except TelegramError as e:
        await _fail(update, e)
        return
    await logs.log_action(update, context, "BAN", mention, reason)
    text = f"🔨 {mention} has been banned."
    if reason:
        text += f"\nReason: {html.escape(reason)}"
    await say(update, text)


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context, protect_admins=False)
    if not prep:
        return
    uid, mention, _ = prep
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, uid, only_if_banned=True)
    except TelegramError as e:
        await _fail(update, e)
        return
    await logs.log_action(update, context, "UNBAN", mention)
    await say(update, f"✅ {mention} has been unbanned.")


async def kick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context)
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
    await logs.log_action(update, context, "KICK", mention)
    await say(update, f"👢 {mention} has been removed from the group.")


async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context)
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
            update.effective_chat.id, uid, ChatPermissions.no_permissions(), until_date=until
        )
    except TelegramError as e:
        await _fail(update, e)
        return
    await logs.log_action(update, context, "MUTE", mention, f"{label} {reason}".strip())
    text = f"🔇 {mention} has been muted ({label})."
    if reason:
        text += f"\nReason: {html.escape(reason)}"
    await say(update, text)


async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prep = await resolve_target(update, context, protect_admins=False)
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
    await logs.log_action(update, context, "UNMUTE", mention)
    await say(update, f"🔊 {mention} has been unmuted.")


# ------------------------------------------------------------------- main
async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(DEFAULT_COMMANDS)
    await app.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeAllChatAdministrators())


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)


def build_app() -> Application:
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
    for name, handler in (("ban", ban), ("unban", unban), ("kick", kick), ("mute", mute), ("unmute", unmute)):
        app.add_handler(CommandHandler(name, handler))

    ui.register(app)
    ui.SECTIONS["mod"] = MOD_HELP
    welcome.register(app)
    antispam.register(app)
    notes.register(app)
    locks.register(app)
    admin.register(app)
    warnsys.register(app)
    antiraid.register(app)
    disabling.register(app)
    logs.register(app)
    app.add_error_handler(on_error)
    return app


def main() -> None:
    if not TOKEN:
        raise SystemExit("BOT_TOKEN not found. Add BOT_TOKEN=... to your .env file.")
    app = build_app()
    log.info("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
