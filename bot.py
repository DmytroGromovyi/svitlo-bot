#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Svitlo Bot - Power Outage Notification Bot with Webhook Support
Enhanced with Calculated Outage Hours
"""

import os
import logging
import sqlite3
import json
import re
import hashlib
import time as time_module
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
try:
    from scraper import ScheduleScraper
except ImportError:
    # Fallback for local testing if scraper.py isn't present
    class ScheduleScraper:
        def fetch_schedule(self): return None
        def parse_schedule(self, content): return None

# =============================================================================
# CONFIGURATION
# =============================================================================

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
API_SECRET = os.getenv('API_SECRET')
PORT = int(os.getenv('PORT', 8080))
WEBHOOK_URL = os.getenv('WEBHOOK_URL')

MAX_USERS = 15
DB_PATH = '/data/users.db'
GROUPS = [f"{i}.{j}" for i in range(1, 7) for j in range(1, 4)]

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

bot_app = None
update_queue = Queue()

# =============================================================================
# DATABASE FUNCTIONS
# =============================================================================

def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
    if cursor.fetchone():
        cursor.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'group' in columns and 'group_number' not in columns:
            cursor.execute('ALTER TABLE users RENAME COLUMN "group" TO group_number')
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

def get_user_count() -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM users')
    count = cursor.fetchone()[0]
    conn.close()
    return count

def get_user_group(chat_id: int) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT group_number FROM users WHERE chat_id = ?', (chat_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def save_user_group(chat_id: int, group: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT OR REPLACE INTO users (chat_id, group_number) VALUES (?, ?)', (chat_id, group))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error saving user group: {e}")
        return False
    finally:
        conn.close()

def get_all_users() -> list:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT chat_id, group_number FROM users')
    users = [{"chat_id": row[0], "group": row[1]} for row in cursor.fetchall()]
    conn.close()
    return users

def delete_user(chat_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM users WHERE chat_id = ?', (chat_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted

def get_schedule_from_db(group_number: str) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT today_schedule, tomorrow_schedule, updated_at FROM schedules WHERE group_number = ?', (group_number,))
    result = cursor.fetchone()
    conn.close()
    return {'today': result[0], 'tomorrow': result[1], 'updated_at': result[2]} if result else None

def save_schedule_to_db(group_number: str, today: str, tomorrow: str, schedule_hash: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT today_schedule, tomorrow_schedule FROM schedules WHERE group_number = ?', (group_number,))
    current = cursor.fetchone()
    prev_today, prev_tomorrow = (current[0], current[1]) if current else (None, None)
    
    cursor.execute('''
        INSERT INTO schedules (group_number, today_schedule, tomorrow_schedule, previous_today, previous_tomorrow, schedule_hash, updated_at)
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
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT schedule_hash FROM schedules WHERE group_number = ?', (group_number,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

# =============================================================================
# SCHEDULE FORMATTING LOGIC (MODIFIED)
# =============================================================================

def parse_schedule_entries(group_data):
    today_text = None
    tomorrow_text = None
    for entry in group_data:
        date_name = entry.get('date', '').lower()
        schedule_text = entry.get('schedule', '')
        if 'сьогодні' in date_name or 'сього' in date_name:
            today_text = schedule_text
        elif 'завтра' in date_name:
            tomorrow_text = schedule_text
        elif not today_text:
            today_text = schedule_text
        elif not tomorrow_text:
            tomorrow_text = schedule_text
    return today_text, tomorrow_text

def format_schedule_text(schedule_text):
    """Format schedule text with calculated durations and total sum"""
    if not schedule_text:
        return "ℹ️ Інформація відсутня"

    # Parse OFF ranges using regex
    off_ranges = re.findall(r'з (\d{1,2}:\d{2}) до (\d{1,2}:\d{2})', schedule_text)

    def to_minutes(t):
        h, m = map(int, t.split(':'))
        return h * 60 + m

    def fmt(mins):
        # Handle 24:00 wrap around visually
        if mins >= 1440: return "24:00"
        return f"{mins // 60:02d}:{mins % 60:02d}"

    off_intervals = sorted([(to_minutes(s), to_minutes(e)) for s, e in off_ranges])

    # Build ON intervals
    on_intervals = []
    last_end = 0
    for start, end in off_intervals:
        if start > last_end:
            on_intervals.append((last_end, start))
        last_end = end
    if last_end < 1440:
        on_intervals.append((last_end, 1440))

    lines = []

    # 🟢 ON Section
    lines.append("🟢 *Є світло:*")
    if on_intervals:
        for s, e in on_intervals:
            if s != e:
                lines.append(f"  • {fmt(s)} — {fmt(e)}")
    else:
        lines.append("  • немає даних")

    # 🔴 OFF Section (Calculates per-slot and total duration)
    lines.append("\n🔴 *Немає світла:*")
    total_off_minutes = 0
    if off_intervals:
        for s, e in off_intervals:
            duration_mins = e - s
            total_off_minutes += duration_mins
            duration_hours = duration_mins / 60
            lines.append(f"  • {fmt(s)} — {fmt(e)} ({duration_hours:.1f} год)")
        
        # Add Total
        total_hours = total_off_minutes / 60
        lines.append(f"\n⏱ *Загалом відключено:* {total_hours:.1f} годин")
    else:
        lines.append("  • немає даних")
        lines.append(f"\n⏱ *Загалом відключено:* 0.0 годин")

    return "\n".join(lines)

def format_notification_message(group_number, current_today, current_tomorrow, previous_today=None, previous_tomorrow=None):
    message = "⚡️ *Оновлення графіку відключень!*\n\n"
    message += f"📍 Група: *{group_number}*\n\n"
    message += "📅 *Сьогодні*\n"
    if previous_today and previous_today != current_today:
        old_lines = format_schedule_text(previous_today).split("\n")
        message += "\n".join(f"~{line}~" if line.strip() else line for line in old_lines) + "\n\n🔄 *Оновлено:*\n"
    message += format_schedule_text(current_today) + "\n\n"
    if current_tomorrow:
        message += "📅 *Завтра*\n"
        if previous_tomorrow and previous_tomorrow != current_tomorrow:
            old_lines = format_schedule_text(previous_tomorrow).split("\n")
            message += "\n".join(f"~{line}~" if line.strip() else line for line in old_lines) + "\n\n🔄 *Оновлено:*\n"
        message += format_schedule_text(current_tomorrow) + "\n\n"
    message += "ℹ️ _Перекреслено — години, які були змінені_"
    return message

def format_schedule_message(group_number, today, tomorrow, updated_at):
    message = f"📋 *Графік відключень*\n\n"
    message += f"📍 Група: *{group_number}*\n\n"
    if today:
        message += "📅 *Сьогодні*\n" + format_schedule_text(today) + "\n\n"
    if tomorrow:
        message += "📅 *Завтра*\n" + format_schedule_text(tomorrow) + "\n\n"
    if updated_at:
        message += f"🕐 Оновлено: _{updated_at}_\n"
    message += "ℹ️ _Графік може змінюватися протягом дня_"
    return message

# =============================================================================
# BOT LOGIC & BACKGROUND TASKS
# =============================================================================

async def check_schedule_and_notify():
    global bot_app
    logger.info("🔍 Checking for schedule changes...")
    try:
        scraper = ScheduleScraper()
        json_content = scraper.fetch_schedule()
        if not json_content: return
        
        new_schedule = scraper.parse_schedule(json_content)
        if not new_schedule: return
        
        groups = new_schedule.get('groups', {})
        changed_groups = []
        for group_num, group_data in groups.items():
            today_text, tomorrow_text = parse_schedule_entries(group_data)
            if not today_text: continue
            
            group_hash_data = f"{today_text}|{tomorrow_text or ''}"
            new_hash = hashlib.sha256(group_hash_data.encode('utf-8')).hexdigest()
            if new_hash != get_schedule_hash(group_num):
                changed_groups.append(group_num)
                save_schedule_to_db(group_num, today_text or '', tomorrow_text or '', new_hash)
        
        if not changed_groups: return
        
        users = get_all_users()
        for user in users:
            if user['group'] in changed_groups:
                try:
                    sched = get_schedule_from_db(user['group'])
                    msg = format_notification_message(user['group'], sched['today'], sched['tomorrow'])
                    await bot_app.bot.send_message(chat_id=user['chat_id'], text=msg, parse_mode='Markdown')
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error(f"Error notifying {user['chat_id']}: {e}")
    except Exception as e:
        logger.error(f"Error in checker: {e}", exc_info=True)

async def schedule_checker_loop():
    await asyncio.sleep(10)
    while True:
        try:
            await check_schedule_and_notify()
            await asyncio.sleep(300)
        except Exception as e:
            logger.error(f"Error in loop: {e}")

# =============================================================================
# HANDLERS
# =============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Вітаю! 👋\n📍 Оберіть групу: /setgroup\n📋 Графік: /schedule")

async def setgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_user_group(update.effective_chat.id) is None and get_user_count() >= MAX_USERS:
        await update.message.reply_text("❌ Ліміт користувачів вичерпано.")
        return
    keyboard = [[InlineKeyboardButton(g, callback_data=f"group_{g}") for g in GROUPS[i:i+3]] for i in range(0, len(GROUPS), 3)]
    await update.message.reply_text("Оберіть групу:", reply_markup=InlineKeyboardMarkup(keyboard))

async def group_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    group = query.data.replace("group_", "")
    if save_user_group(query.from_user.id, group):
        await query.edit_message_text(f"✅ Групу {group} збережено! /schedule")

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group = get_user_group(update.effective_chat.id)
    if not group:
        await update.message.reply_text("❌ Оберіть групу: /setgroup")
        return
    sched = get_schedule_from_db(group)
    if not sched:
        await update.message.reply_text("ℹ️ Графік завантажується, зачекайте...")
        return
    
    msg = format_schedule_message(group, sched['today'], sched['tomorrow'], sched['updated_at'])
    await update.message.reply_text(msg, parse_mode='Markdown')

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if delete_user(update.effective_chat.id):
        await update.message.reply_text("✅ Відписано.")

# =============================================================================
# WEBHOOK & FLASK SETUP
# =============================================================================

flask_app = Flask(__name__)

@flask_app.route('/webhook', methods=['POST'])
def webhook_handler():
    update_queue.put(request.get_json(force=True))
    return 'OK', 200

async def process_queue_updates():
    while True:
        if not update_queue.empty():
            data = update_queue.get()
            update = Update.de_json(data, bot_app.bot)
            await bot_app.process_update(update)
        await asyncio.sleep(0.1)

async def setup_application():
    global bot_app
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler('start', start_command))
    bot_app.add_handler(CommandHandler('setgroup', setgroup_command))
    bot_app.add_handler(CommandHandler('schedule', schedule_command))
    bot_app.add_handler(CommandHandler('stop', stop_command))
    bot_app.add_handler(CallbackQueryHandler(group_selection, pattern='^group_'))
    
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")

def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(setup_application())
    loop.create_task(process_queue_updates())
    loop.create_task(schedule_checker_loop())
    loop.run_forever()

if __name__ == '__main__':
    init_db()
    Thread(target=run_bot, daemon=True).start()
    flask_app.run(host='0.0.0.0', port=PORT)