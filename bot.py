import os
import logging
import sqlite3
import threading
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters
)

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states
SELECTING_GROUP = 1

# Maximum number of users allowed
MAX_USERS = 15

# Database path
DB_PATH = '/data/users.db'


class UserStorage:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._init_database()
    
    def _init_database(self):
        """Initialize the database and create tables if they don't exist"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                group_id TEXT NOT NULL,
                username TEXT,
                first_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
        logger.info(f"Database initialized at {self.db_path}")
    
    def _get_connection(self):
        """Get a database connection"""
        return sqlite3.connect(self.db_path)
    
    def get_user(self, user_id):
        """Get user data by user_id"""
        conn = self._get_connection()
        cursor = conn.execute(
            'SELECT user_id, group_id, username, first_name FROM users WHERE user_id = ?',
            (user_id,)
        )
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                'user_id': row[0],
                'group': row[1],
                'username': row[2],
                'first_name': row[3]
            }
        return None
    
    def set_user(self, user_id, data):
        """Insert or update user data"""
        conn = self._get_connection()
        
        # Use INSERT OR REPLACE to handle both insert and update
        conn.execute('''
            INSERT OR REPLACE INTO users (user_id, group_id, username, first_name, updated_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            user_id,
            data.get('group'),
            data.get('username'),
            data.get('first_name'),
            datetime.now()
        ))
        
        conn.commit()
        conn.close()
        logger.info(f"User {user_id} saved with group {data.get('group')}")
    
    def get_all_users(self):
        """Get all users as a dictionary"""
        conn = self._get_connection()
        cursor = conn.execute('SELECT user_id, group_id, username, first_name FROM users')
        
        users = {}
        for row in cursor.fetchall():
            users[str(row[0])] = {
                'user_id': row[0],
                'group': row[1],
                'username': row[2],
                'first_name': row[3]
            }
        
        conn.close()
        return users
    
    def delete_user(self, user_id):
        """Delete a user"""
        conn = self._get_connection()
        conn.execute('DELETE FROM users WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        logger.info(f"User {user_id} deleted")
    
    def get_user_count(self):
        """Get total number of users"""
        conn = self._get_connection()
        cursor = conn.execute('SELECT COUNT(*) FROM users')
        count = cursor.fetchone()[0]
        conn.close()
        return count


# Initialize storage
storage = UserStorage()


# API endpoint to fetch users
@app.route('/api/users', methods=['GET'])
def get_users():
    # Check authorization
    auth_header = request.headers.get('Authorization')
    expected_auth = f"Bearer {os.environ.get('API_SECRET')}"
    
    if auth_header != expected_auth:
        return jsonify({'error': 'Unauthorized'}), 401
    
    # Query SQLite database
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute('SELECT user_id, group_id FROM users')
    users = [{'user_id': row[0], 'group_id': row[1]} for row in cursor.fetchall()]
    conn.close()
    
    return jsonify({'users': users})


# Health check endpoint
@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


def run_flask():
    """Run Flask server"""
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)


def check_user_limit():
    """Check if user limit has been reached"""
    current_users = storage.get_user_count()
    return current_users < MAX_USERS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
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
    
    # Check if user exists or if we can add new users
    user_data = storage.get_user(user_id)
    
    if not user_data and not check_user_limit():
        await update.message.reply_text(
            "Вибачте, бот досяг максимальної кількості користувачів (15).\n"
            "Зверніться до адміністратора для збільшення ліміту."
        )
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
    current_users = storage.get_user_count()
    await update.message.reply_text(
        "📋 Доступні команди:\n\n"
        "/start - Почати роботу з ботом\n"
        "/setgroup - Встановити/змінити групу відключень\n"
        "/mygroup - Показати поточну групу\n"
        "/stop - Відписатися від сповіщень\n"
        "/help - Показати цю допомогу\n\n"
        f"👥 Користувачів: {current_users}/{MAX_USERS}"
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
    
    # Run Flask in a separate thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    main()
