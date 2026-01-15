import os
import json
import logging
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove

# Load environment variables from .env file
load_dotenv()
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states
SELECTING_GROUP = 1

# Whitelist of allowed user IDs (optional)
WHITELIST = os.getenv('ALLOWED_USER_IDS', '').split(',')
WHITELIST = [int(uid.strip()) for uid in WHITELIST if uid.strip().isdigit()]

class UserStorage:
    def __init__(self, filepath='data/users.json'):
        self.filepath = filepath
        self.users = self._load()
    
    def _load(self):
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
    
    def save(self):
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        with open(self.filepath, 'w', encoding='utf-8') as f:
            json.dump(self.users, f, ensure_ascii=False, indent=2)
    
    def get_user(self, user_id):
        return self.users.get(str(user_id))
    
    def set_user(self, user_id, data):
        self.users[str(user_id)] = data
        self.save()
    
    def get_all_users(self):
        return self.users
    
    def delete_user(self, user_id):
        if str(user_id) in self.users:
            del self.users[str(user_id)]
            self.save()

storage = UserStorage()

def check_whitelist(user_id):
    """Check if user is whitelisted (if whitelist is enabled)"""
    if not WHITELIST:
        return True
    return user_id in WHITELIST

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not check_whitelist(user_id):
        await update.message.reply_text(
            "Вибачте, цей бот доступний лише для авторизованих користувачів."
        )
        return ConversationHandler.END
    
    user_data = storage.get_user(user_id)
    
    if user_data:
        group = user_data.get('group', 'не встановлено')
        await update.message.reply_text(
            f"Вітаю! 👋\n\n"
            f"Ваша поточна група: {group}\n\n"
            f"Команди:\n"
            f"/setgroup - Змінити групу відключень\n"
            f"/mygroup - Показати поточну групу\n"
            f"/stop - Відписатися від сповіщень\n"
            f"/help - Допомога"
        )
    else:
        await update.message.reply_text(
            f"Вітаю! 👋\n\n"
            f"Я допоможу вам отримувати сповіщення про зміни в графіку відключень електроенергії.\n\n"
            f"Для початку, оберіть вашу групу відключень командою /setgroup"
        )
    
    return ConversationHandler.END

async def set_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not check_whitelist(user_id):
        await update.message.reply_text("Доступ заборонено.")
        return ConversationHandler.END
    
    # Available groups
    groups = [
        ['1.1', '1.2', '1.3'],
        ['2.1', '2.2', '2.3'],
        ['3.1', '3.2', '3.3'],
        ['4.1', '4.2', '4.3'],
        ['5.1', '5.2', '5.3'],
        ['6.1', '6.2', '6.3'],
        ['Скасувати']
    ]
    
    reply_markup = ReplyKeyboardMarkup(groups, one_time_keyboard=True, resize_keyboard=True)
    
    await update.message.reply_text(
        "Оберіть вашу групу відключень:",
        reply_markup=reply_markup
    )
    
    return SELECTING_GROUP

async def group_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    group = update.message.text
    
    if group == 'Скасувати':
        await update.message.reply_text(
            "Скасовано.",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
    
    # Validate group format
    valid_groups = [f"{i}.{j}" for i in range(1, 7) for j in range(1, 4)]
    
    if group not in valid_groups:
        await update.message.reply_text(
            "Невірний формат групи. Спробуйте ще раз або натисніть /cancel",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
    
    user_data = {
        'group': group,
        'user_id': user_id,
        'username': update.effective_user.username,
        'first_name': update.effective_user.first_name
    }
    
    storage.set_user(user_id, user_data)
    
    await update.message.reply_text(
        f"✅ Групу {group} збережено!\n\n"
        f"Ви будете отримувати сповіщення про зміни в графіку відключень.",
        reply_markup=ReplyKeyboardRemove()
    )
    
    return ConversationHandler.END

async def my_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = storage.get_user(user_id)
    
    if user_data:
        group = user_data.get('group', 'не встановлено')
        await update.message.reply_text(f"Ваша група: {group}")
    else:
        await update.message.reply_text(
            "Група не встановлена. Використайте /setgroup для налаштування."
        )

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    storage.delete_user(user_id)
    
    await update.message.reply_text(
        "Ви відписалися від сповіщень. Для повторної підписки використайте /start"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 Доступні команди:\n\n"
        "/start - Почати роботу зботом\n"
        "/setgroup - Встановити/змінити групу відключень\n"
        "/mygroup - Показати поточну групу\n"
        "/stop - Відписатися від сповіщень\n"
        "/help - Показати цю допомогу"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Скасовано.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

def main():
    import asyncio
    
    # Fix for Python 3.14 - ensure event loop exists BEFORE building application
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")
    
    application = Application.builder().token(token).build()
    
    # Conversation handler for setting group
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('setgroup', set_group)],
        states={
            SELECTING_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_selected)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler('mygroup', my_group))
    application.add_handler(CommandHandler('stop', stop))
    application.add_handler(CommandHandler('help', help_command))
    
    logger.info("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    import asyncio
    # Ensure event loop exists for Python 3.14+
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    
    main()