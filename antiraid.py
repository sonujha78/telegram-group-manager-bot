"""
Anti-raid: while raid mode is on, every new member is banned for a while (they can join again later).
Raid mode can be switched on by an admin or automatically when too many people join in a minute.
"""
import html
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import Application, ApplicationHandlerStop, ChatMemberHandler, CommandHandler, ContextTypes

import db
import logs
import ui
import utils
from utils import bot_rights_warning, is_admin, parse_duration, require_admin, say

log = logging.getLogger("groupbot.antiraid")

DEFAULT_RAID_TIME = 6 * 3600  # how long raid mode stays on
DEFAULT_ACTION_TIME = 3600  # how long raiders are banned
AUTO_WINDOW = 60  # seconds in which N joins trigger the automatic raid mode
MAX_TIME = 7 * 24 * 3600

RAID_HELP = (
    "🚨 <b>Anti-Raid</b>\n\n"
    "While raid mode is on, every new member is banned for a while (they can join again later). "
    "Members added by an admin and approved users are let in.\n\n"
    "/antiraid - show the status\n"
    "/antiraid on - turn raid mode on (for the default raid time)\n"
    "/antiraid [time] - turn it on for a while, e.g. /antiraid 2h\n"
    "/antiraid off - turn it off\n"
    "/raidtime [time] - default raid time (default 6h)\n"
    "/raidactiontime [time] - how long raiders are banned (default 1h)\n"
    "/autoantiraid [N|off] - turn raid mode on automatically when N people join within a minute\n\n"
    "Times: 30m, 6h, 1d, 1w. I need the <b>Ban users</b> admin permission."
)

_joins: dict[int, deque] = {}


# ---------------------------------------------------------------- helpers
def _int(chat_id: int, key: str, default: int) -> int:
    return int(db.get_value(chat_id, key, str(default)))


def raid_left(chat_id: int) -> float:
    """Seconds of raid mode that are left (0 if it is off)."""
    return max(0.0, float(db.get_value(chat_id, "raid_until", "0")) - time.time())


def fmt_seconds(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = [f"{days}d" if days else "", f"{hours}h" if hours else "", f"{minutes}m" if minutes else ""]
    return " ".join(p for p in parts if p) or "under a minute"


def _duration(arg: str, minimum: int = 60):
    """'2h' -> seconds, or None if it is invalid / outside 1 minute .. 1 week."""
    delta = parse_duration(arg) if arg else None
    if delta is None or not minimum <= delta.total_seconds() <= MAX_TIME:
        return None
    return int(delta.total_seconds())


def _start_raid(chat_id: int, seconds: int) -> None:
    db.set_value(chat_id, "raid_until", str(time.time() + seconds))


# --------------------------------------------------------------- watcher
async def on_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmu = update.chat_member
    chat, new = cmu.chat, cmu.new_chat_member
    user = new.user
    if user.is_bot or user.id == context.bot.id:
        return
    joined = cmu.old_chat_member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) and new.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
    )
    if not joined:
        return

    auto = _int(chat.id, "raid_auto", 0)
    if auto:
        now = time.time()
        recent = _joins.setdefault(chat.id, deque())
        recent.append(now)
        while recent and now - recent[0] > AUTO_WINDOW:
            recent.popleft()
        if len(recent) >= auto and raid_left(chat.id) == 0:
            seconds = _int(chat.id, "raid_time", DEFAULT_RAID_TIME)
            _start_raid(chat.id, seconds)
            recent.clear()
            try:
                await context.bot.send_message(
                    chat.id,
                    f"🚨 <b>Anti-raid mode is ON</b> for {fmt_seconds(seconds)}: {auto} people joined within a minute. "
                    "New members are temporarily banned. Admins can stop it with /antiraid off",
                    parse_mode="HTML",
                )
            except TelegramError as e:
                log.warning("Could not announce raid mode in %s: %s", chat.id, e)
            await logs.log_event(context, chat, f"🛡 <b>{html.escape(chat.title or 'group')}</b>\n#ANTIRAID automatically enabled ({auto} joins in a minute)")

    if raid_left(chat.id) == 0:
        return
    if utils.is_approved(chat.id, user.id):
        return
    if cmu.from_user.id != user.id and await is_admin(context, chat.id, cmu.from_user.id):
        return  # an admin added this member on purpose
    until = datetime.now(timezone.utc) + timedelta(seconds=_int(chat.id, "raid_action", DEFAULT_ACTION_TIME))
    try:
        await context.bot.ban_chat_member(chat.id, user.id, until_date=until)
    except TelegramError as e:
        log.warning("Anti-raid: could not ban %s in %s: %s", user.id, chat.id, e)
        return
    raise ApplicationHandlerStop  # no welcome message / captcha for raiders


# ------------------------------------------------------------- commands
async def antiraid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    arg = context.args[0].lower() if context.args else ""
    if not arg:
        left = raid_left(chat_id)
        state = f"<b>ON</b> ({fmt_seconds(left)} left)" if left else "<b>OFF</b>"
        auto = _int(chat_id, "raid_auto", 0)
        await say(
            update,
            f"🚨 Anti-raid mode is {state}\n"
            f"Raid time: {fmt_seconds(_int(chat_id, 'raid_time', DEFAULT_RAID_TIME))}\n"
            f"Raiders are banned for: {fmt_seconds(_int(chat_id, 'raid_action', DEFAULT_ACTION_TIME))}\n"
            f"Automatic mode: {f'on ({auto} joins per minute)' if auto else 'off'}\n\n"
            "Use /antiraid on, /antiraid [time] or /antiraid off",
        )
        return
    if arg == "off":
        db.set_value(chat_id, "raid_until", "0")
        await say(update, "✅ Anti-raid mode turned <b>OFF</b>.")
        await logs.log_action(update, context, "ANTIRAID", None, "turned off")
        return
    seconds = _int(chat_id, "raid_time", DEFAULT_RAID_TIME) if arg == "on" else _duration(arg)
    if seconds is None:
        await say(update, "Usage: /antiraid on | off | [time]\nTimes: 30m, 6h, 1d (between 1 minute and 1 week)")
        return
    _start_raid(chat_id, seconds)
    await logs.log_action(update, context, "ANTIRAID", None, f"turned on for {fmt_seconds(seconds)}")
    await say(
        update,
        f"🚨 Anti-raid mode is <b>ON</b> for {fmt_seconds(seconds)}. New members will be temporarily banned."
        + await bot_rights_warning(context, chat_id, "can_restrict_members"),
    )


async def raidtime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    seconds = _duration(context.args[0]) if context.args else None
    if seconds is None:
        await say(update, f"Raid mode lasts <b>{fmt_seconds(_int(chat_id, 'raid_time', DEFAULT_RAID_TIME))}</b> by default.\nChange it with /raidtime [time], e.g. /raidtime 12h")
        return
    db.set_value(chat_id, "raid_time", str(seconds))
    await say(update, f"✅ Raid mode will now last <b>{fmt_seconds(seconds)}</b> by default.")


async def raidactiontime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    seconds = _duration(context.args[0]) if context.args else None
    if seconds is None:
        await say(update, f"Raiders are banned for <b>{fmt_seconds(_int(chat_id, 'raid_action', DEFAULT_ACTION_TIME))}</b>.\nChange it with /raidactiontime [time], e.g. /raidactiontime 1d")
        return
    db.set_value(chat_id, "raid_action", str(seconds))
    await say(update, f"✅ Raiders will now be banned for <b>{fmt_seconds(seconds)}</b>.")


async def autoantiraid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    arg = context.args[0].lower() if context.args else ""
    if arg == "off":
        db.set_value(chat_id, "raid_auto", "0")
        await say(update, "✅ Automatic anti-raid turned <b>OFF</b>.")
        return
    try:
        n = int(arg)
        if not 3 <= n <= 100:
            raise ValueError
    except ValueError:
        auto = _int(chat_id, "raid_auto", 0)
        await say(update, f"Automatic anti-raid is {f'<b>on</b> ({auto} joins per minute)' if auto else '<b>off</b>'}.\nUse /autoantiraid [3-100] or /autoantiraid off")
        return
    db.set_value(chat_id, "raid_auto", str(n))
    await say(update, f"✅ Raid mode will start automatically when <b>{n}</b> people join within a minute." + await bot_rights_warning(context, chat_id, "can_restrict_members"))


# --------------------------------------------------------------- register
def register(app: Application) -> None:
    ui.add_section("antiraid", "🚨 Anti-Raid", RAID_HELP)
    # group -1: runs before the welcome/captcha handler (group 0) and can stop it
    app.add_handler(ChatMemberHandler(on_join, ChatMemberHandler.CHAT_MEMBER), group=-1)
    app.add_handler(CommandHandler("antiraid", antiraid_cmd))
    app.add_handler(CommandHandler("raidtime", raidtime_cmd))
    app.add_handler(CommandHandler("raidactiontime", raidactiontime_cmd))
    app.add_handler(CommandHandler("autoantiraid", autoantiraid_cmd))
