"""Small shared helpers for the feature modules."""
from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes


async def say(update: Update, text: str, **kwargs) -> None:
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, **kwargs)


async def is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


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
