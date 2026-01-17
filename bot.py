#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Svitlo Bot - Power Outage Notification Bot with Webhook Support
This version uses webhooks and runs schedule checking internally
"""

import os
import logging
import sqlite3
import json
from typing import Optional
from pathlib import Path
from queue import Queue
from threading import Thread
from datetime import datetime
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from flask import Flask, request, jsonify

# Import scraper
import sys
sys.path.append(os.path.dirname(__file__))
from scraper import ScheduleScraper

# =============================================================================
# CONFIGURATION
# =============================================================================

# Load environment variables
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
API_SECRET = os.getenv('API_SECRET')
PORT = int(os.getenv('PORT', 8080))
WEBHOOK_URL = os.getenv('WEBHOOK_URL')  # e.g., https://your-app.fly.dev

# Constants
MAX_USERS = 15
DB_PATH = '/data/users.db'
GROUPS = [f"{i}.{j}" for i in range(1, 7) for j in range(1, 4)]

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global bot application instance
bot_app = None
update_queue = Queue()

# =============================================================================
# DATABASE FUNCTIONS
# =============================================================================

def init_db():
    """Initialize SQLite database"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Users table
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
    table_exists = cursor.fetchone() is not None
    
    if table_exists:
        cursor.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cursor.fetchall()]
        logger.info(f"Existing table columns: {columns}")
        
        if 'group' in columns and 'group_number' not in columns:
            logger.info("Migrating database: renaming 'group' to 'group_number'")
            cursor.execute('ALTER TABLE users RENAME COLUMN "group" TO group_number')
            conn.commit()
        elif 'group_number' not in columns:
            logger.warning("Table exists but missing group_number column, recreating table")
            cursor.execute('DROP TABLE users')
            cursor.execute('''
                CREATE TABLE users (
                    chat_id INTEGER PRIMARY KEY,
                    group_number TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
    else:
        cursor.execute('''
            CREATE TABLE users (
                chat_id INTEGER PRIMARY KEY,
                group_number TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
    
    # Schedules table - stores current and previous state for each group
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schedules (
            group_number TEXT PRIMARY KEY,
            today_schedule TEXT,
            tomorrow_schedule TEXT,
            previous_today TEXT,
            previous_tomorrow TEXT,
            schedule_hash TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    
    conn.close()
    logger.info("Database initialized")

def get_user_count() -> int:
    """Get total number of registered users"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM users')
    count = cursor.fetchone()[0]
    conn.close()
    return count

def get_user_group(chat_id: int) -> Optional[str]:
    """Get user's selected group"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT group_number FROM users WHERE chat_id = ?', (chat_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def save_user_group(chat_id: int, group: str) -> bool:
    """Save or update user's group selection"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            INSERT OR REPLACE INTO users (chat_id, group_number)
            VALUES (?, ?)
        ''', (chat_id, group))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error saving user group: {e}")
        conn.close()
        return False

def get_all_users() -> list:
    """Get all registered users"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT chat_id, group_number FROM users')
    users = [{"chat_id": row[0], "group": row[1]} for row in cursor.fetchall()]
    conn.close()
    return users

def delete_user(chat_id: int) -> bool:
    """Delete user from database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM users WHERE chat_id = ?', (chat_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted

def get_schedule_from_db(group_number: str) -> Optional[dict]:
    """Get schedule for a specific group from database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT today_schedule, tomorrow_schedule, updated_at 
        FROM schedules 
        WHERE group_number = ?
    ''', (group_number,))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        return {
            'today': result[0],
            'tomorrow': result[1],
            'updated_at': result[2]
        }
    return None

def save_schedule_to_db(group_number: str, today: str, tomorrow: str, schedule_hash: str):
    """Save schedule to database, keeping previous state"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Get current state to store as previous
    cursor.execute('''
        SELECT today_schedule, tomorrow_schedule 
        FROM schedules 
        WHERE group_number = ?
    ''', (group_number,))
    current = cursor.fetchone()
    
    prev_today = current[0] if current else None
    prev_tomorrow = current[1] if current else None
    
    # Insert or update
    cursor.execute('''
        INSERT INTO schedules 
        (group_number, today_schedule, tomorrow_schedule, previous_today, previous_tomorrow, schedule_hash, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(group_number) DO UPDATE SET
            previous_today = schedules.today_schedule,
            previous_tomorrow = schedules.tomorrow_schedule,
            today_schedule = excluded.today_schedule,
            tomorrow_schedule = excluded.tomorrow_schedule,
            schedule_hash = excluded.schedule_hash,
            updated_at = CURRENT_TIMESTAMP
    ''', (group_number, today, tomorrow, prev_today, prev_tomorrow, schedule_hash))
    
    conn.commit()
    conn.close()

def get_schedule_hash(group_number: str) -> Optional[str]:
    """Get stored hash for a group"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT schedule_hash FROM schedules WHERE group_number = ?', (group_number,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

# =============================================================================
# SCHEDULE CHECKER & NOTIFIER
# =============================================================================

def parse_schedule_entries(group_data):
    """Parse schedule entries to separate today and tomorrow"""
    today_text = None
    tomorrow_text = None
    
    for entry in group_data:
        date_name = entry.get('date', '').lower()
        schedule_text = entry.get('schedule', '')
        
        # Identify today vs tomorrow
        if 'сьогодні' in date_name or 'сього' in date_name:
            today_text = schedule_text
        elif 'завтра' in date_name:
            tomorrow_text = schedule_text
        elif not today_text:
            # If no label, first entry is today
            today_text = schedule_text
        elif not tomorrow_text:
            # Second entry is tomorrow
            tomorrow_text = schedule_text
    
    return today_text, tomorrow_text

def format_schedule_text(schedule_text):
    """Format schedule text showing derived ON/OFF periods correctly"""

    if not schedule_text:
        return "ℹ️ Інформація відсутня"

    import re
    from datetime import time

    # Parse OFF ranges
    off_ranges = re.findall(
        r'з (\d{1,2}:\d{2}) до (\d{1,2}:\d{2})',
        schedule_text
    )

    def to_minutes(t):
        h, m = map(int, t.split(':'))
        return h * 60 + m

    off_intervals = sorted(
        [(to_minutes(s), to_minutes(e)) for s, e in off_ranges]
    )

    # Build ON intervals as complement of OFF
    on_intervals = []
    last_end = 0

    for start, end in off_intervals:
        if start > last_end:
            on_intervals.append((last_end, start))
        last_end = end

    if last_end < 24 * 60:
        on_intervals.append((last_end, 24 * 60))

    def fmt(mins):
        return f"{mins // 60:02d}:{mins % 60:02d}"

    lines = []

    # 🟢 ON
    lines.append("🟢 *Є світло:*")
    if on_intervals:
        for s, e in on_intervals:
            if s != e:
                lines.append(f"  • {fmt(s)} — {fmt(e)}")
    else:
        lines.append("  • немає даних")

    # 🔴 OFF
    lines.append("\n🔴 *Немає світла:*")
    if off_intervals:
        for s, e in off_intervals:
            lines.append(f"  • {fmt(s)} — {fmt(e)}")
    else:
        lines.append("  • немає даних")

    return "\n".join(lines)


def format_notification_message(
    group_number,
    current_today,
    current_tomorrow,
    previous_today=None,
    previous_tomorrow=None
):
    """Format notification message with changed hours crossed out"""

    message = "⚡️ *Оновлення графіку відключень!*\n\n"
    message += f"📍 Група: *{group_number}*\n\n"

    # ===== TODAY =====
    message += "📅 *Сьогодні*\n"

    if previous_today and previous_today != current_today:
        old_block = format_schedule_text(previous_today)
        old_lines = old_block.split("\n")
        message += "\n".join(
            f"~{line}~" if line.strip() else line for line in old_lines
        )
        message += "\n\n🔄 *Оновлено:*\n"

    message += format_schedule_text(current_today) + "\n\n"

    # ===== TOMORROW =====
    if current_tomorrow:
        message += "📅 *Завтра*\n"

        if previous_tomorrow and previous_tomorrow != current_tomorrow:
            old_block = format_schedule_text(previous_tomorrow)
            old_lines = old_block.split("\n")
            message += "\n".join(
                f"~{line}~" if line.strip() else line for line in old_lines
            )
            message += "\n\n🔄 *Оновлено:*\n"

        message += format_schedule_text(current_tomorrow) + "\n\n"

    message += "ℹ️ _Перекреслено — години, які були змінені_"

    return message

def format_schedule_message(group_number, today, tomorrow, updated_at):
    """Format regular schedule display message"""
    message = f"📋 *Графік відключень*\n\n"
    message += f"📍 Група: *{group_number}*\n\n"
    
    if today:
        message += "📅 *Сьогодні*\n"
        message += format_schedule_text(today) + "\n\n"
    
    if tomorrow:
        message += "📅 *Завтра*\n"
        message += format_schedule_text(tomorrow) + "\n\n"
    
    if updated_at:
        message += f"🕐 Оновлено: _{updated_at}_\n"
    
    message += "ℹ️ _Графік може змінюватися протягом дня_"
    
    return message


async def check_schedule_and_notify():
    """Check for schedule changes and notify users"""
    global bot_app
    
    logger.info("🔍 Checking for schedule changes...")
    
    try:
        # Initialize scraper
        scraper = ScheduleScraper()
        
        # Fetch latest schedule
        json_content = scraper.fetch_schedule()
        if not json_content:
            logger.warning("⚠️ Could not fetch schedule")
            return
        
        new_schedule = scraper.parse_schedule(json_content)
        if not new_schedule:
            logger.warning("⚠️ Could not parse schedule")
            return
        
        groups = new_schedule.get('groups', {})
        changed_groups = []
        
        # Check each group for changes
        for group_number, group_data in groups.items():
            # Parse today and tomorrow
            today_text, tomorrow_text = parse_schedule_entries(group_data)
            
            if not today_text:
                continue
            
            # Calculate hash for this group
            import hashlib
            group_hash_data = f"{today_text}|{tomorrow_text or ''}"
            new_hash = hashlib.sha256(group_hash_data.encode('utf-8')).hexdigest()
            
            # Compare with stored hash
            old_hash = get_schedule_hash(group_number)
            
            if new_hash != old_hash:
                logger.info(f"🔔 Group {group_number} changed!")
                changed_groups.append(group_number)
                
                # Save to database
                save_schedule_to_db(
                    group_number=group_number,
                    today=today_text or '',
                    tomorrow=tomorrow_text or '',
                    schedule_hash=new_hash
                )
        
        if not changed_groups:
            logger.info("✅ No changes in schedule")
            return
        
        # Notify users
        logger.info(f"🔔 Schedule changed for groups: {', '.join(changed_groups)}")
        
        users = get_all_users()
        if not users:
            logger.info("ℹ️ No users to notify")
            return
        
        notification_count = 0
        
        for user in users:
            chat_id = user['chat_id']
            group = user['group']
            
            if group not in changed_groups:
                continue
            
            try:
                # Get schedule from DB
                schedule = get_schedule_from_db(group)
                if not schedule:
                    continue
                
                # Format message
                message = f"⚡️ Оновлення графіку відключень!\n\n"
                message += f"Група: {group}\n\n"
                
                if schedule['today']:
                    message += f"📅 Сьогодні\n"
                    message += f"{schedule['today']}\n\n"
                
                if schedule['tomorrow']:
                    message += f"📅 Завтра\n"
                    message += f"{schedule['tomorrow']}\n\n"
                
                message += f"ℹ️ Графік може змінюватися протягом дня."
                
                # Send notification
                await bot_app.bot.send_message(
                    chat_id=chat_id,
                    text=message
                )
                
                notification_count += 1
                logger.info(f"📤 Sent notification to user {chat_id} (group {group})")
                
                # Rate limiting
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.error(f"❌ Error sending notification to {chat_id}: {e}")
        
        logger.info(f"✅ Sent {notification_count} notifications")
        
    except Exception as e:
        logger.error(f"❌ Error in schedule checker: {e}", exc_info=True)
        

async def schedule_checker_loop():
    """Background task that checks schedule every 5 minutes"""
    logger.info("⏰ Schedule checker started (runs every 5 minutes)")
    
    # Wait for bot to be fully initialized
    await asyncio.sleep(10)
    
    # Run first check immediately
    logger.info("🔍 Running initial schedule check...")
    try:
        await check_schedule_and_notify()
    except Exception as e:
        logger.error(f"❌ Error in initial check: {e}", exc_info=True)
    
    while True:
        try:
            # Wait 5 minutes
            logger.info("⏳ Waiting 5 minutes until next check...")
            await asyncio.sleep(300)  # 300 seconds = 5 minutes
            
            await check_schedule_and_notify()
        except Exception as e:
            logger.error(f"❌ Error in checker loop: {e}", exc_info=True)

# =============================================================================
# BOT HANDLERS
# =============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    chat_id = update.effective_chat.id
    
    welcome_msg = (
        "Вітаю! 👋\n\n"
        "Я бот для сповіщень про зміни в графіку відключень світла у Львові.\n\n"
        "📍 Оберіть вашу групу відключень командою /setgroup\n"
        "📋 Переглянути графік: /schedule\n"
        "ℹ️ Допомога: /help"
    )
    
    await update.message.reply_text(welcome_msg)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_msg = (
        "📱 Доступні команди:\n\n"
        "/start - Почати роботу з ботом\n"
        "/setgroup - Обрати групу відключень\n"
        "/schedule - Переглянути поточний графік\n"
        "/mygroup - Показати вашу групу\n"
        "/stop - Відписатися від сповіщень\n"
        "/help - Показати цю довідку\n\n"
        "ℹ️ Бот моніторить зміни кожні 10 хвилин і надсилає сповіщення, "
        "якщо графік змінюється."
    )
    
    await update.message.reply_text(help_msg)

async def setgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setgroup command - show group selection keyboard"""
    # Check user limit
    current_group = get_user_group(update.effective_chat.id)
    if current_group is None and get_user_count() >= MAX_USERS:
        await update.message.reply_text(
            "❌ На жаль, досягнуто максимальну кількість користувачів.\n"
            "Спробуйте пізніше."
        )
        return
    
    # Create inline keyboard with groups
    keyboard = []
    for i in range(0, len(GROUPS), 3):
        row = [
            InlineKeyboardButton(group, callback_data=f"group_{group}")
            for group in GROUPS[i:i+3]
        ]
        keyboard.append(row)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "Оберіть вашу групу відключень:",
        reply_markup=reply_markup
    )

async def group_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle group selection from inline keyboard"""
    query = update.callback_query
    await query.answer()
    
    group = query.data.replace("group_", "")
    chat_id = query.from_user.id
    
    if save_user_group(chat_id, group):
        await query.edit_message_text(
            f"✅ Групу {group} збережено!\n\n"
            f"Ви будете отримувати сповіщення про зміни в графіку відключень.\n\n"
            f"Переглянути графік: /schedule"
        )
        logger.info(f"User {chat_id} selected group {group}")
    else:
        await query.edit_message_text(
            "❌ Помилка при збереженні групи. Спробуйте ще раз: /setgroup"
        )

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /schedule command"""
    chat_id = update.effective_chat.id
    group = get_user_group(chat_id)
    
    if not group:
        await update.message.reply_text(
            "❌ Спочатку оберіть вашу групу: /setgroup"
        )
        return
    
    # Get schedule from database
    try:
        schedule = get_schedule_from_db(group)
        
        if not schedule:
            await update.message.reply_text(
                f"📋 Графік для групи {group}\n\n"
                f"ℹ️ Графік ще не завантажено.\n"
                f"Зачекайте кілька хвилин - бот автоматично перевіряє оновлення кожні 5 хвилин."
            )
            return
        
        # Format timestamp
        updated_at = schedule.get('updated_at', 'Невідомо')
        if updated_at and updated_at != 'Невідомо':
            try:
                from datetime import datetime
                updated_dt = datetime.strptime(updated_at, '%Y-%m-%d %H:%M:%S')
                updated_at = updated_dt.strftime('%d.%m.%Y %H:%M')
            except:
                pass
        
        # Format message
        message = format_schedule_message(
            group_number=group,
            today=schedule['today'],
            tomorrow=schedule['tomorrow'],
            updated_at=updated_at
        )
        
        await update.message.reply_text(
            message,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error fetching schedule: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Помилка при завантаженні графіка.\n"
            f"Спробуйте пізніше або зверніться до адміністратора."
        )

async def mygroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /mygroup command"""
    chat_id = update.effective_chat.id
    group = get_user_group(chat_id)
    
    if group:
        await update.message.reply_text(
            f"📍 Ваша група: {group}\n\n"
            f"Змінити групу: /setgroup"
        )
    else:
        await update.message.reply_text(
            "❌ Група не обрана.\n"
            "Оберіть групу: /setgroup"
        )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stop command"""
    chat_id = update.effective_chat.id
    
    if delete_user(chat_id):
        await update.message.reply_text(
            "✅ Ви відписалися від сповіщень.\n\n"
            "Щоб підписатися знову, використайте /start"
        )
        logger.info(f"User {chat_id} unsubscribed")
    else:
        await update.message.reply_text(
            "ℹ️ Ви не були підписані на сповіщення."
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}", exc_info=context.error)

# =============================================================================
# FLASK API
# =============================================================================

flask_app = Flask(__name__)

@flask_app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'users': get_user_count()
    }), 200

@flask_app.route('/api/users', methods=['GET'])
def get_users():
    """API endpoint to get all users - protected by API secret"""
    auth_header = request.headers.get('Authorization')
    
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Missing or invalid authorization'}), 401
    
    token = auth_header.replace('Bearer ', '')
    if token != API_SECRET:
        return jsonify({'error': 'Invalid API secret'}), 403
    
    try:
        users = get_all_users()
        return jsonify({
            'users': users,
            'count': len(users)
        }), 200
    except Exception as e:
        logger.error(f"Error getting users: {e}")
        return jsonify({'error': str(e)}), 500

@flask_app.route('/webhook', methods=['POST'])
def webhook_handler():
    """Handle incoming webhook updates from Telegram"""
    global update_queue
    
    if request.method == 'POST':
        try:
            update_data = request.get_json(force=True)
            # Add update to queue for processing by bot thread
            update_queue.put(update_data)
            return 'OK', 200
        except Exception as e:
            logger.error(f"Error receiving update: {e}", exc_info=True)
            return 'Error', 500
    return 'Invalid request', 400

# =============================================================================
# BOT APPLICATION SETUP
# =============================================================================

async def process_queue_updates():
    """Process updates from the queue"""
    global bot_app, update_queue
    
    import asyncio
    
    logger.info("🔄 Queue processor started")
    
    while True:
        try:
            # Check queue with timeout
            if not update_queue.empty():
                update_data = update_queue.get(timeout=1)
                logger.info(f"📨 Processing update from queue")
                
                update = Update.de_json(update_data, bot_app.bot)
                await bot_app.process_update(update)
                
                update_queue.task_done()
                logger.info(f"✅ Update processed successfully")
            else:
                # Small delay when queue is empty
                await asyncio.sleep(0.1)
                
        except Exception as e:
            logger.error(f"❌ Error processing queued update: {e}", exc_info=True)
            await asyncio.sleep(0.1)

async def setup_application():
    """Initialize and set up the bot application"""
    global bot_app
    
    # Create bot application
    bot_app = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    bot_app.add_handler(CommandHandler('start', start_command))
    bot_app.add_handler(CommandHandler('help', help_command))
    bot_app.add_handler(CommandHandler('setgroup', setgroup_command))
    bot_app.add_handler(CommandHandler('schedule', schedule_command))
    bot_app.add_handler(CommandHandler('mygroup', mygroup_command))
    bot_app.add_handler(CommandHandler('stop', stop_command))
    bot_app.add_handler(CallbackQueryHandler(group_selection, pattern='^group_'))
    
    # Add error handler
    bot_app.add_error_handler(error_handler)
    
    # Initialize the application
    await bot_app.initialize()
    await bot_app.start()
    
    # Set up webhook
    webhook_url = f"{WEBHOOK_URL}/webhook"
    
    try:
        # Delete any existing webhook
        await bot_app.bot.delete_webhook(drop_pending_updates=True)
        
        # Set new webhook
        await bot_app.bot.set_webhook(
            url=webhook_url,
            allowed_updates=["message", "callback_query"]
        )
        
        # Get webhook info
        webhook_info = await bot_app.bot.get_webhook_info()
        logger.info(f"✅ Webhook set successfully!")
        logger.info(f"📍 Webhook URL: {webhook_info.url}")
        logger.info(f"📊 Pending updates: {webhook_info.pending_update_count}")
        
    except Exception as e:
        logger.error(f"❌ Error setting webhook: {e}")
        raise
    
    logger.info("🤖 Bot application initialized and ready!")

def run_bot():
    """Run the bot in a separate thread"""
    import asyncio
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Set up and start the bot
    loop.run_until_complete(setup_application())
    
    # Start queue processor
    logger.info("🔄 Starting queue processor...")
    loop.create_task(process_queue_updates())
    
    # Start schedule checker (runs every 10 minutes)
    logger.info("⏰ Starting schedule checker...")
    loop.create_task(schedule_checker_loop())
    
    # Keep the loop running
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    finally:
        loop.close()

# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("🚀 Starting Svitlo Bot with webhook support...")
    logger.info("=" * 60)
    logger.info(f"📍 Webhook URL: {WEBHOOK_URL}")
    logger.info(f"🔌 Port: {PORT}")
    
    # Validate environment variables
    if not BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN not set!")
        exit(1)
    if not API_SECRET:
        logger.error("❌ API_SECRET not set!")
        exit(1)
    if not WEBHOOK_URL:
        logger.error("❌ WEBHOOK_URL not set!")
        exit(1)
    
    # Initialize database
    init_db()
    
    # Start bot in separate thread
    logger.info("🔧 Starting bot thread...")
    bot_thread = Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Give bot time to initialize
    import time
    time.sleep(3)
    
    # Run Flask server in main thread
    logger.info("🌐 Starting Flask server...")
    logger.info("=" * 60)
    flask_app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)