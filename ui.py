"""
UI layer: /start welcome screen, /help menu and inline buttons (GroupHelp-style).
Optional .env values: GROUP_URL, CHANNEL_URL, SUPPORT_URL
"""
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

# Admin rights pre-selected when someone taps "Add me to a Group".
ADD_RIGHTS = "change_info+delete_messages+restrict_members+invite_users+pin_messages"

HELP_MENU_TEXT = (
    "📚 <b>Help menu</b>\n\n"
    "Choose a category below to see its commands and how they work."
)

MOD_TEXT = (
    "👮 <b>Moderation</b>\n\n"
    "Admin only. Reply to a user's message, or pass a user ID.\n\n"
    "/ban [reason] - ban a user\n"
    "/unban - remove a ban\n"
    "/kick - remove a user (they can rejoin)\n"
    "/mute [10m | 2h | 1d] - mute a user (permanent if no duration)\n"
    "/unmute - remove a mute\n"
    "/warn [reason] - warn a user (auto-ban at the warn limit)\n"
    "/warns - show a user's warnings\n"
    "/resetwarns - reset a user's warnings"
)


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _welcome_text(bot_name: str) -> str:
    return (
        "👋 Hello!\n"
        f"<b>{bot_name}</b> is the most complete bot to help you manage your groups easily and safely!\n\n"
        "👉 Add me in a <b>Supergroup</b> and promote me as <b>Admin</b> to let me get in action!\n\n"
        "❓ <b>WHICH ARE THE COMMANDS?</b> ❓\n"
        "Press /help to see all the commands and how they work!"
    )


def _info_text(bot_name: str) -> str:
    return (
        "ℹ️ <b>Information</b>\n\n"
        f"<b>{bot_name}</b> helps you keep your group clean and safe.\n\n"
        "• Moderation: ban, kick, mute, warn\n"
        "• Welcome, goodbye, rules and join captcha\n"
        "• Anti-spam: flood, links, banned words\n"
        "• Notes and filters\n\n"
        "Add me to a supergroup, make me admin, and use /help for the full command list."
    )


def _home_keyboard(username: str) -> InlineKeyboardMarkup:
    add_url = f"https://t.me/{username}?startgroup=true&admin={ADD_RIGHTS}"
    rows = [
        [InlineKeyboardButton("➕ Add me to a Group ➕", url=add_url)],
        [InlineKeyboardButton("📚 Commands", callback_data="ui:help")],
    ]
    links = []
    if _env("GROUP_URL"):
        links.append(InlineKeyboardButton("👥 Group", url=_env("GROUP_URL")))
    if _env("CHANNEL_URL"):
        links.append(InlineKeyboardButton("📢 Channel", url=_env("CHANNEL_URL")))
    if links:
        rows.append(links)
    row = []
    if _env("SUPPORT_URL"):
        row.append(InlineKeyboardButton("🆘 Support", url=_env("SUPPORT_URL")))
    row.append(InlineKeyboardButton("💬 Information", callback_data="ui:info"))
    rows.append(row)
    rows.append([InlineKeyboardButton("🌐 Languages 🌐", callback_data="ui:lang")])
    rows.append([InlineKeyboardButton("❌ Close", callback_data="ui:close")])
    return InlineKeyboardMarkup(rows)


# Help sections. A feature module adds its text here (SECTIONS["key"] = "...")
# and its button in the help menu switches from "coming soon" to a real button.
SECTIONS = {"mod": MOD_TEXT}

HELP_BUTTONS = [
    ("👮 Moderation", "mod"),
    ("👋 Welcome & Rules", "welcome"),
    ("🛡 Anti-Spam", "antispam"),
    ("📝 Notes & Filters", "notes"),
    ("🔒 Locks", "locks"),
    ("🛠 Admin Tools", "admin"),
    ("✅ Approval & Reports", "approval"),
    ("ℹ️ Misc", "misc"),
]


def add_section(key: str, label: str, text: str) -> None:
    """Let a feature module add its own help section and menu button."""
    SECTIONS[key] = text
    if all(k != key for _, k in HELP_BUTTONS):
        HELP_BUTTONS.append((label, key))


def _help_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for label, key in HELP_BUTTONS:
        if key in SECTIONS:
            buttons.append(InlineKeyboardButton(label, callback_data=f"ui:{key}"))
        else:
            buttons.append(InlineKeyboardButton(f"{label} 🚧", callback_data="ui:soon"))
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data="ui:home"),
            InlineKeyboardButton("❌ Close", callback_data="ui:close"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _back_keyboard(target: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"ui:{target}")]])


def _screen(key: str, bot):
    """Return (text, keyboard) for a screen, or None if the key is unknown."""
    name = bot.first_name
    if key == "home":
        return _welcome_text(name), _home_keyboard(bot.username)
    if key == "help":
        return HELP_MENU_TEXT, _help_keyboard()
    if key in SECTIONS:
        return SECTIONS[key], _back_keyboard("help")
    if key == "info":
        return _info_text(name), _back_keyboard("home")
    return None


async def _group_pointer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = f"https://t.me/{context.bot.username}?start=help"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📚 Open commands in private", url=url)]])
    await update.effective_message.reply_text(
        "👋 I'm up and running! Tap the button to see all my commands.", reply_markup=kb
    )


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        await _group_pointer(update, context)
        return
    key = "help" if context.args and context.args[0] == "help" else "home"
    text, markup = _screen(key, context.bot)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        await _group_pointer(update, context)
        return
    text, markup = _screen("help", context.bot)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    key = (q.data or "").removeprefix("ui:")
    if key == "lang":
        await q.answer("English is the only language for now 🇬🇧", show_alert=True)
        return
    if key == "soon":
        await q.answer("Coming soon 🚧", show_alert=True)
        return
    if key == "close":
        await q.answer()
        try:
            await q.message.delete()
        except BadRequest:
            pass
        return
    screen = _screen(key, context.bot)
    if screen is None:
        await q.answer()
        return
    text, markup = screen
    await q.answer()
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^ui:"))
