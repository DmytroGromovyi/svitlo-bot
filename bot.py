import os
import logging
import sqlite3
import threading
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
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                group_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            'SELECT user_id, group_id FROM users WHERE user_id = ?',
            (user_id,)
        )
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                'user_id': row[0],
                'group': row[1]
            }
        return None
    
    def set_user(self, user_id, data):
        """Insert or update user data"""
        conn = self._get_connection()
        
        # Use INSERT OR REPLACE to handle both insert and update
        conn.execute('''
            INSERT OR REPLACE INTO users (user_id, group_id)
            VALUES (?, ?)
        ''', (
            user_id,
            data.get('group')
        ))
        
        conn.commit()
        conn.close()
        logger.info(f"User {user_id} saved with group {data.get('group')}")
    
    def get_all_users(self):
        """Get all users as a dictionary"""
        conn = self._get_connection()
        cursor = conn.execute('SELECT user_id, group_id FROM users')
        
        users = {}
        for row in cursor.fetchall():
            users[str(row[0])] = {
                'user_id': row[0],
                'group': row[1]
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
    app.run(host='0.0.0.0', port=port, use_reloader=False)


def check_user_limit():
    """Check if user limit has been reached"""
    current_users = storage.get_user_count()
    return current_users < MAX_USERS

def format_schedule_message(group_id, schedule_entries):
    """Format schedule for today and tomorrow"""
    import re
    
    def calculate_power_times(schedule_text):
        """Calculate ON and OFF times"""
        pattern = re.compile(r'з (\d{1,2}):(\d{2}) до (\d{1,2}):(\d{2})')
        off_ranges = []
        
        for match in pattern.finditer(schedule_text):
            start_h, start_m, end_h, end_m = map(int, match.groups())
            start_min = start_h * 60 + start_m
            end_min = (end_h * 60 + end_m) if end_h != 24 else 1440
            off_ranges.append((start_min, end_min))
        
        off_ranges.sort()
        merged = []
        for start, end in off_ranges:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        
        on_ranges = []
        current = 0
        for off_start, off_end in merged:
            if current < off_start:
                on_ranges.append((current, off_start))
            current = max(current, off_end)
        
        if current < 1440:
            on_ranges.append((current, 1440))
        
        def fmt(minutes):
            h, m = divmod(minutes, 60)
            return f"{h:02d}:{m:02d}"
        
        off_text = ", ".join(f"з {fmt(s)} до {fmt(e) if e < 1440 else '24:00'}" for s, e in merged)
        on_text = ", ".join(f"з {fmt(s)} до {fmt(e) if e < 1440 else '24:00'}" for s, e in on_ranges)
        
        return on_text or "немає", off_text or "немає"
    
    message = f"📋 <b>Графік для групи {group_id}</b>\n\n"
    
    for idx, entry in enumerate(schedule_entries[:2]):  # Today and tomorrow
        date = entry.get('date', '')
        schedule = entry.get('schedule', '')
        
        if not schedule:
            continue
        
        label = "Сьогодні" if idx == 0 else "Завтра"
        if date and date != "Today":
            label = date
        
        on_time, off_time = calculate_power_times(schedule)
        
        message += f"📅 <b>{label}</b>\n\n"
        message += f"🟢 <b>Є світло:</b> {on_time}\n"
        message += f"🔴 <b>Немає світла:</b> {off_time}\n\n"
    
    if len(schedule_entries) == 0:
        message += "ℹ️ Графік наразі недоступний."
    else:
        message += "ℹ️ Графік може змінюватися протягом дня."
    
    return message


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
        'user_id': user_id
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
        "/schedule - Показати графік\n"
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

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /schedule command - show current schedule for user's group"""
    user_id = update.effective_user.id
    user_data = storage.get_user(user_id)
    
    if not user_data:
        await update.message.reply_text(
            "Спочатку оберіть групу: /setgroup"
        )
        return
    
    group = user_data.get('group')
    
    # Fetch current schedule
    try:
        scraper = ScheduleScraper()
        result = scraper.check_for_changes()
        
        if not result or not result.get('new_schedule'):
            await update.message.reply_text(
                "❌ Не вдалося отримати графік. Спробуйте пізніше."
            )
            return
        
        schedule = result['new_schedule']
        groups_data = schedule.get('groups', {})
        user_schedule = groups_data.get(group)
        
        if not user_schedule or len(user_schedule) == 0:
            await update.message.reply_text(
                f"📋 Графік для групи <b>{group}</b> наразі недоступний.",
                parse_mode='HTML'
            )
            return
        
        # Format message with today and tomorrow
        message = format_schedule_message(group, user_schedule)
        
        await update.message.reply_text(
            message,
            parse_mode='HTML'
        )
        
    except Exception as e:
        logger.error(f"Error fetching schedule: {e}")
        await update.message.reply_text(
            "❌ Помилка при отриманні графіку."
        )

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
    application.add_handler(CommandHandler('schedule', schedule_command))
    
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
