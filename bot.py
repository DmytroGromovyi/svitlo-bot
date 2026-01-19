#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Svitlo Bot - Power Outage Notification Bot with Webhook Support
Restored full original functionality with added Outage Duration Calculation
"""

import os
import logging
import sqlite3
import json
import re
import hashlib
from typing import Optional
from pathlib import Path
from queue import Queue
from threading import Thread
from datetime import datetime
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
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
# INLINE MENU HELPER FUNCTIONS
# =============================================================================

def get_main_keyboard():
    """Get the main inline keyboard with common actions"""
    keyboard = [
        [
            InlineKeyboardButton("📋 Графік", callback_data="action_schedule"),
            InlineKeyboardButton("🔄 Змінити групу", callback_data="action_setgroup")
        ],
        [
            InlineKeyboardButton("ℹ️ Моя група", callback_data="action_mygroup")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def safe_edit(query, text, parse_mode=None, reply_markup=None):
    try:
        await query.edit_message_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            logger.debug("Telegram edit skipped (no changes)")
        else:
            raise

async def error_handler(update, context):
    err = context.error
    if isinstance(err, BadRequest) and "Message is not modified" in str(err):
        return
    logger.error("Unhandled Telegram error", exc_info=err)

# =============================================================================
# DATABASE FUNCTIONS
# =============================================================================

def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
    table_exists = cursor.fetchone() is not None
    
    if table_exists:
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
# FORMATTING & CALCULATIONS
# =============================================================================

def parse_schedule_entries(group_data):
    today_text, tomorrow_text = None, None
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
    if not schedule_text:
        return "ℹ️ Інформація відсутня"

    off_ranges = re.findall(r'з (\d{1,2}:\d{2}) до (\d{1,2}:\d{2})', schedule_text)

    def to_minutes(t):
        h, m = map(int, t.split(':'))
        return h * 60 + m

    def fmt(mins):
        if mins >= 1440: return "24:00"
        return f"{mins // 60:02d}:{mins % 60:02d}"

    off_intervals = sorted([(to_minutes(s), to_minutes(e)) for s, e in off_ranges])

    on_intervals = []
    last_end = 0
    for start, end in off_intervals:
        if start > last_end:
            on_intervals.append((last_end, start))
        last_end = end
    if last_end < 1440:
        on_intervals.append((last_end, 1440))

    lines = []
    lines.append("🟢 *Є світло:*")
    if on_intervals:
        for s, e in on_intervals:
            if s != e: lines.append(f"  • {fmt(s)} — {fmt(e)}")
    else:
        lines.append("  • немає даних")

    lines.append("\n🔴 *Немає світла:*")
    total_off_minutes = 0
    if off_intervals:
        for s, e in off_intervals:
            dur = e - s
            total_off_minutes += dur
            lines.append(f"  • {fmt(s)} — {fmt(e)} ({dur/60:.1f} год)")
        lines.append(f"\n⏱ *Загалом відключено:* {total_off_minutes/60:.1f} годин")
    else:
        lines.append("  • немає даних")

    return "\n".join(lines)

def format_notification_message(group_number, current_today, current_tomorrow, previous_today=None, previous_tomorrow=None):
    """Format notification with strikethrough ONLY for changed parts - MarkdownV2 compatible"""
    
    # Escape the group number dots
    escaped_group = group_number.replace('.', '\\.')
    
    message = "⚡️ *Оновлення графіку відключень\\!*\n\n"
    message += f"📍 Група: *{escaped_group}*\n\n"
    
    # Helper function to extract time intervals
    def extract_intervals(schedule_text):
        if not schedule_text:
            return {'on': [], 'off': []}
        
        off_ranges = re.findall(r'з (\d{1,2}:\d{2}) до (\d{1,2}:\d{2})', schedule_text)
        
        def to_minutes(t):
            h, m = map(int, t.split(':'))
            return h * 60 + m
        
        off_intervals = sorted([(to_minutes(s), to_minutes(e)) for s, e in off_ranges])
        
        # Calculate ON intervals
        on_intervals = []
        last_end = 0
        for start, end in off_intervals:
            if start > last_end:
                on_intervals.append((last_end, start))
            last_end = end
        if last_end < 1440:
            on_intervals.append((last_end, 1440))
        
        return {'on': on_intervals, 'off': off_intervals}
    
    def fmt(mins):
        if mins >= 1440: return "24:00"
        return f"{mins // 60:02d}:{mins % 60:02d}"
    
    def fmt_hours(hours_float):
        """Format hours with escaped decimal point for MarkdownV2"""
        return str(hours_float).replace('.', '\\.')
    
    def fmt_duration(hours_float):
        """Format duration with all special chars escaped"""
        hours_str = str(hours_float).replace('.', '\\.')
        return f"\\({hours_str} год\\)"
    
    # Compare and format today's schedule
    message += "📅 *Сьогодні*\n\n"
    
    current_intervals = extract_intervals(current_today)
    previous_intervals = extract_intervals(previous_today) if previous_today else {'on': [], 'off': []}
    
    # Power ON times
    message += "🟢 *Є світло:*\n"
    
    # Show removed ON intervals (strikethrough)
    removed_on = [iv for iv in previous_intervals['on'] if iv not in current_intervals['on']]
    for s, e in removed_on:
        if s != e:
            message += f"~  • {fmt(s)} — {fmt(e)}~\n"
    
    # Show current ON intervals
    for s, e in current_intervals['on']:
        if s != e:
            is_new = (s, e) not in previous_intervals['on'] if previous_intervals['on'] else True
            prefix = "✨ " if is_new and previous_intervals['on'] else ""
            message += f"  • {prefix}{fmt(s)} — {fmt(e)}\n"
    
    # Power OFF times
    message += "\n🔴 *Немає світла:*\n"
    
    # Show removed OFF intervals (strikethrough)
    removed_off = [iv for iv in previous_intervals['off'] if iv not in current_intervals['off']]
    for s, e in removed_off:
        dur = e - s
        message += f"~  • {fmt(s)} — {fmt(e)} {fmt_duration(dur/60)}~\n"
    
    # Show current OFF intervals
    total_off = 0
    for s, e in current_intervals['off']:
        dur = e - s
        total_off += dur
        is_new = (s, e) not in previous_intervals['off'] if previous_intervals['off'] else True
        prefix = "⚠️ " if is_new and previous_intervals['off'] else ""
        message += f"  • {prefix}{fmt(s)} — {fmt(e)} {fmt_duration(dur/60)}\n"
    
    if current_intervals['off']:
        message += f"\n⏱ *Загалом відключено:* {fmt_hours(total_off/60)} годин\n"
    
    message += "\n"
    
    # Tomorrow's schedule (if available)
    if current_tomorrow:
        message += "📅 *Завтра*\n\n"
        
        current_tm_intervals = extract_intervals(current_tomorrow)
        previous_tm_intervals = extract_intervals(previous_tomorrow) if previous_tomorrow else {'on': [], 'off': []}
        
        # Power ON times
        message += "🟢 *Є світло:*\n"
        
        # Show removed ON intervals
        removed_on_tm = [iv for iv in previous_tm_intervals['on'] if iv not in current_tm_intervals['on']]
        for s, e in removed_on_tm:
            if s != e:
                message += f"~  • {fmt(s)} — {fmt(e)}~\n"
        
        # Show current ON intervals
        for s, e in current_tm_intervals['on']:
            if s != e:
                is_new = (s, e) not in previous_tm_intervals['on'] if previous_tm_intervals['on'] else True
                prefix = "✨ " if is_new and previous_tm_intervals['on'] else ""
                message += f"  • {prefix}{fmt(s)} — {fmt(e)}\n"
        
        # Power OFF times
        message += "\n🔴 *Немає світла:*\n"
        
        # Show removed OFF intervals
        removed_off_tm = [iv for iv in previous_tm_intervals['off'] if iv not in current_tm_intervals['off']]
        for s, e in removed_off_tm:
            dur = e - s
            message += f"~  • {fmt(s)} — {fmt(e)} {fmt_duration(dur/60)}~\n"
        
        # Show current OFF intervals
        total_off_tm = 0
        for s, e in current_tm_intervals['off']:
            dur = e - s
            total_off_tm += dur
            is_new = (s, e) not in previous_tm_intervals['off'] if previous_tm_intervals['off'] else True
            prefix = "⚠️ " if is_new and previous_tm_intervals['off'] else ""
            message += f"  • {prefix}{fmt(s)} — {fmt(e)} {fmt_duration(dur/60)}\n"
        
        if current_tm_intervals['off']:
            message += f"\n⏱ *Загалом відключено:* {fmt_hours(total_off_tm/60)} годин\n\n"
    
    message += "ℹ️ _~Перекреслено~ — видалено • ✨/⚠️ — додано_"
    return message

def format_schedule_message(group_number, today, tomorrow, updated_at):
    escaped_group = group_number.replace('.', '\\.')
    message = f"📋 *Графік відключень*\n\n📍 Група: *{escaped_group}*\n\n"
    if today: message += "📅 *Сьогодні*\n" + format_schedule_text(today) + "\n\n"
    if tomorrow: message += "📅 *Завтра*\n" + format_schedule_text(tomorrow) + "\n\n"
    if updated_at: message += f"🕐 Оновлено: _{updated_at}_\n"
    message += "ℹ️ _Графік може змінюватися протягом дня_"
    return message

# =============================================================================
# BACKGROUND TASKS
# =============================================================================

async def check_schedule_and_notify():
    global bot_app
    try:
        scraper = ScheduleScraper()
        json_content = scraper.fetch_schedule()
        if not json_content: return
        new_schedule = scraper.parse_schedule(json_content)
        if not new_schedule: return
        
        groups_data = new_schedule.get('groups', {})
        changed_groups = []
        
        for g_num, g_data in groups_data.items():
            t_text, tm_text = parse_schedule_entries(g_data)
            if not t_text: continue
            
            new_hash = hashlib.sha256(f"{t_text}|{tm_text or ''}".encode('utf-8')).hexdigest()
            
            if new_hash != get_schedule_hash(g_num):
                changed_groups.append(g_num)
                save_schedule_to_db(g_num, t_text or '', tm_text or '', new_hash)
        
        if not changed_groups: return
        
        for user in get_all_users():
            if user['group'] in changed_groups:
                try:
                    # Get current AND previous schedules
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute('''
                        SELECT today_schedule, tomorrow_schedule, 
                               previous_today, previous_tomorrow
                        FROM schedules 
                        WHERE group_number = ?
                    ''', (user['group'],))
                    result = cursor.fetchone()
                    conn.close()
                    
                    if result:
                        current_today, current_tomorrow, prev_today, prev_tomorrow = result
                        
                        # Format message with previous schedules for strikethrough
                        msg = format_notification_message(
                            user['group'], 
                            current_today, 
                            current_tomorrow,
                            prev_today,  # Previous today
                            prev_tomorrow  # Previous tomorrow
                        )
                        
                        await bot_app.bot.send_message(
                            chat_id=user['chat_id'], 
                            text=msg, 
                            parse_mode='MarkdownV2'
                        )
                        await asyncio.sleep(0.5)
                        
                except Exception as e: 
                    logger.error(f"Notify error {user['chat_id']}: {e}")
                    
    except Exception as e: 
        logger.error(f"Checker error: {e}", exc_info=True)

async def schedule_checker_loop():
    await asyncio.sleep(10)
    while True:
        await check_schedule_and_notify()
        await asyncio.sleep(300)

# =============================================================================
# TELEGRAM HANDLERS
# =============================================================================

async def handle_inline_actions(update, context):
    """Handle inline keyboard button clicks"""
    query = update.callback_query
    await query.answer()
    
    action = query.data
    
    if action == "action_schedule":
        # Show schedule
        group = get_user_group(query.from_user.id)
        if not group:
            await safe_edit(
                query,
                "❌ Спочатку оберіть групу:",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Обрати групу", callback_data="action_setgroup")
                ]])
            )
            return
        
        s = get_schedule_from_db(group)
        if not s:
            await safe_edit(
                query,
                "ℹ️ Завантаження графіку...",
                reply_markup=get_main_keyboard()
            )
            return
        
        msg = format_schedule_message(group, s['today'], s['tomorrow'], s['updated_at'])
        await safe_edit(
            query,
            msg,
            parse_mode='Markdown',
            reply_markup=get_main_keyboard()
        )
    
    elif action == "action_setgroup":
        # Show group selection
        kb = [
            [InlineKeyboardButton(g, callback_data=f"group_{g}") for g in GROUPS[i:i+3]]
            for i in range(0, len(GROUPS), 3)
        ]
        await safe_edit(
            query,
            "Оберіть вашу групу відключень:",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    
    elif action == "action_mygroup":
        # Show current group
        g = get_user_group(query.from_user.id)
        if g:
            await safe_edit(
                query,
                f"📍 Ваша група: *{g}*\n\nОберіть дію:",
                parse_mode='Markdown',
                reply_markup=get_main_keyboard()
            )
        else:
            await safe_edit(
                query,
                "❌ Група не обрана\n\nОберіть дію:",
                reply_markup=get_main_keyboard()
            )

async def start_command(update, context):
    welcome_text = (
        "Вітаю! 👋\n\n"
        "Я допоможу відстежувати графік відключень світла.\n\n"
        "Оберіть дію:"
    )
    await update.message.reply_text(
        welcome_text,
        reply_markup=get_main_keyboard()
    )

async def setgroup_command(update, context):
    if get_user_group(update.effective_chat.id) is None and get_user_count() >= MAX_USERS:
        await update.message.reply_text("❌ Ліміт користувачів.")
        return
    kb = [[InlineKeyboardButton(g, callback_data=f"group_{g}") for g in GROUPS[i:i+3]] for i in range(0, len(GROUPS), 3)]
    await update.message.reply_text("Оберіть групу:", reply_markup=InlineKeyboardMarkup(kb))

async def group_selection(update, context):
    query = update.callback_query
    await query.answer()
    group = query.data.replace("group_", "")
    
    if save_user_group(query.from_user.id, group):
        await safe_edit(
            query,
            f"✅ Групу {group} збережено!\n\nОберіть дію:",
            reply_markup=get_main_keyboard()
        )

async def schedule_command(update, context):
    group = get_user_group(update.effective_chat.id)
    if not group:
        await update.message.reply_text(
            "❌ Спочатку оберіть групу:",
            reply_markup=get_main_keyboard()
        )
        return
    
    s = get_schedule_from_db(group)
    if not s:
        await update.message.reply_text(
            "ℹ️ Завантаження графіку...",
            reply_markup=get_main_keyboard()
        )
        return
    
    await update.message.reply_text(
        format_schedule_message(group, s['today'], s['tomorrow'], s['updated_at']),
        parse_mode='Markdown',
        reply_markup=get_main_keyboard()
    )

async def mygroup_command(update, context):
    g = get_user_group(update.effective_chat.id)
    text = f"📍 Ваша група: *{g}*" if g else "❌ Група не обрана"
    await update.message.reply_text(
        text,
        parse_mode='Markdown',
        reply_markup=get_main_keyboard()
    )

async def stop_command(update, context):
    """Unsubscribe user from notifications"""
    if delete_user(update.effective_chat.id):
        await update.message.reply_text(
            "✅ Ви відписані від сповіщень.\n\n"
            "Щоб підписатись знову, натисніть /start"
        )
    else:
        await update.message.reply_text(
            "ℹ️ Ви не були підписані на сповіщення."
        )

# =============================================================================
# FLASK API ROUTES (RESTORED)
# =============================================================================

flask_app = Flask(__name__)

@flask_app.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'healthy', 'users': get_user_count()}), 200

@flask_app.route('/api/users', methods=['GET'])
def get_users_api():
    auth = request.headers.get('Authorization')
    if not auth or auth.replace('Bearer ', '') != API_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    users = get_all_users()
    return jsonify({'users': users, 'count': len(users)}), 200

@flask_app.route('/webhook', methods=['POST'])
def webhook_handler():
    update_queue.put(request.get_json(force=True))
    return 'OK', 200

# =============================================================================
# APP RUNNERS
# =============================================================================

async def process_queue_updates():
    while True:
        if not update_queue.empty():
            data = update_queue.get()
            try:
                update = Update.de_json(data, bot_app.bot)
                await bot_app.process_update(update)
            except Exception as e: logger.error(f"Queue error: {e}")
        await asyncio.sleep(0.1)

async def setup_application():
    global bot_app
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler('start', start_command))
    bot_app.add_handler(CommandHandler('setgroup', setgroup_command))
    bot_app.add_handler(CommandHandler('schedule', schedule_command))
    bot_app.add_handler(CommandHandler('mygroup', mygroup_command))
    bot_app.add_handler(CommandHandler('stop', stop_command))

    # Add handlers for inline buttons
    bot_app.add_handler(CallbackQueryHandler(group_selection, pattern='^group_'))
    bot_app.add_handler(CallbackQueryHandler(handle_inline_actions, pattern='^action_'))

    # Add error handler
    bot_app.add_error_handler(error_handler)

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