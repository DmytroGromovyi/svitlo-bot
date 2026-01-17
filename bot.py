#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Svitlo Bot - Power Outage Notification Bot with Webhook Support
This version uses webhooks instead of polling for better efficiency on Fly.io
"""

import os
import logging
import sqlite3
from typing import Optional
from pathlib import Path
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from flask import Flask, request, jsonify

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

# =============================================================================
# DATABASE FUNCTIONS
# =============================================================================

def init_db():
    """Initialize SQLite database"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            group_number TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    
    # Note: In production, this would fetch actual schedule from API
    # For now, show a placeholder message
    await update.message.reply_text(
        f"📋 Графік для групи {group}\n\n"
        f"🔄 Завантаження актуального графіка...\n\n"
        f"ℹ️ Функція перегляду графіка буде доступна після інтеграції з API.\n"
        f"Наразі ви отримуватимете сповіщення про зміни."
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
    global bot_app
    
    if request.method == 'POST':
        try:
            update_data = request.get_json(force=True)
            update = Update.de_json(update_data, bot_app.bot)
            
            # Process update in async context
            asyncio.run(bot_app.process_update(update))
            
            return 'OK', 200
        except Exception as e:
            logger.error(f"Error processing update: {e}")
            return 'Error', 500
    return 'Invalid request', 400

# =============================================================================
# BOT APPLICATION SETUP
# =============================================================================

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
    
    # Set up bot application
    logger.info("🔧 Initializing bot application...")
    asyncio.run(setup_application())
    
    # Run Flask server
    logger.info("🌐 Starting Flask server...")
    logger.info("=" * 60)
    flask_app.run(host='0.0.0.0', port=PORT, debug=False)