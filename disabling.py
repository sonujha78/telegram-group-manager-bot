"""
Disabling: admins can switch member commands off in their group (admins can still use them).
"""
import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, ApplicationHandlerStop, CommandHandler, ContextTypes, MessageHandler, filters

import db
import ui
from utils import is_admin, require_admin, say

log = logging.getLogger("groupbot.disabling")

DISABLEABLE = (
    "rules", "get", "notes", "filters", "admins", "id", "info",
    "report", "kickme", "privacy", "locktypes", "help",
)

DISABLING_HELP = (
    "🚫 <b>Disabling</b>\n\n"
    "Switch member commands off in your group. Admins can still use them.\n\n"
    "/disable [command] - disable a command, e.g. /disable kickme\n"
    "/enable [command] - enable it again\n"
    "/disabled - list the disabled commands\n"
    "/disableable - list the commands that can be disabled"
)


def _get_disabled(chat_id: int) -> set[str]:
    return {c for c in db.get_value(chat_id, "disabled_cmds", "").split(",") if c}


def _save_disabled(chat_id: int, cmds: set[str]) -> None:
    if cmds:
        db.set_value(chat_id, "disabled_cmds", ",".join(sorted(cmds)))
    else:
        db.del_value(chat_id, "disabled_cmds")


def _clean(arg: str) -> str:
    return arg.lower().lstrip("/").split("@")[0]


async def on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every command handler and stops disabled commands from non-admins."""
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if msg is None or not msg.text or user is None or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    name, _, addressed_to = msg.text.split()[0][1:].partition("@")
    if addressed_to and addressed_to.lower() != (context.bot.username or "").lower():
        return  # the command is meant for another bot
    if name.lower() not in _get_disabled(chat.id):
        return
    if await is_admin(context, chat.id, user.id):  # only checked for disabled commands (saves API calls)
        return
    raise ApplicationHandlerStop


async def disable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    name = _clean(context.args[0]) if context.args else ""
    if name not in DISABLEABLE:
        await say(update, "Usage: /disable [command]\nCommands you can disable: " + ", ".join(DISABLEABLE))
        return
    chat_id = update.effective_chat.id
    cmds = _get_disabled(chat_id)
    cmds.add(name)
    _save_disabled(chat_id, cmds)
    await say(update, f"🚫 /{name} is now disabled for members.")


async def enable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    name = _clean(context.args[0]) if context.args else ""
    chat_id = update.effective_chat.id
    cmds = _get_disabled(chat_id)
    if not name:
        await say(update, "Usage: /enable [command]")
        return
    if name not in cmds:
        await say(update, f"/{name} is not disabled.")
        return
    cmds.discard(name)
    _save_disabled(chat_id, cmds)
    await say(update, f"✅ /{name} is enabled again.")


async def disabled_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    cmds = sorted(_get_disabled(update.effective_chat.id))
    if not cmds:
        await say(update, "No commands are disabled. Use /disable [command]")
        return
    await say(update, "🚫 <b>Disabled commands</b>\n" + "\n".join(f"/{c}" for c in cmds))


async def disableable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await say(update, "<b>Commands that can be disabled</b>\n" + "\n".join(f"/{c}" for c in DISABLEABLE))


def register(app: Application) -> None:
    ui.add_section("disabling", "🚫 Disabling", DISABLING_HELP)
    # group -3 runs before everything else, so a disabled command never reaches its handler
    app.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.GROUPS, on_command), group=-3)
    app.add_handler(CommandHandler("disable", disable_cmd))
    app.add_handler(CommandHandler("enable", enable_cmd))
    app.add_handler(CommandHandler("disabled", disabled_cmd))
    app.add_handler(CommandHandler("disableable", disableable_cmd))
