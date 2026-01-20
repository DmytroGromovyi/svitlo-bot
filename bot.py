#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Svitlo Bot - Power Outage Notification Bot
Refactored: Clear diff-first notifications for better UX
"""

import os
import logging
import sqlite3
import json
import re
import hashlib
from typing import Optional, Dict, List, Tuple
from pathlib import Path
from queue import Queue
from threading import Thread
from datetime import datetime
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from flask import Flask, request, jsonify

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

MAX_USERS = 25
DB_PATH = '/data/users.db'
GROUPS = [f"{i}.{j}" for i in range(1, 7) for j in range(1, 4)]

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

bot_app = None
update_queue = Queue()

# =============================================================================
# HELPERS
# =============================================================================

def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Графік", callback_data="action_schedule"),
         InlineKeyboardButton("🔄 Змінити групу", callback_data="action_setgroup")],
        [InlineKeyboardButton("ℹ️ Моя група", callback_data="action_mygroup")]
    ])

async def safe_edit(query, text, parse_mode=None, reply_markup=None):
    try:
        await query.edit_message_text(text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

async def error_handler(update, context):
    if not isinstance(context.error, BadRequest) or "Message is not modified" not in str(context.error):
        logger.error("Telegram error", exc_info=context.error)

# =============================================================================
# DATABASE
# =============================================================================

def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
    if c.fetchone():
        c.execute("PRAGMA table_info(users)")
        cols = [row[1] for row in c.fetchall()]
        if 'group' in cols and 'group_number' not in cols:
            c.execute('ALTER TABLE users RENAME COLUMN "group" TO group_number')
    else:
        c.execute('''CREATE TABLE users (
            chat_id INTEGER PRIMARY KEY,
            group_number TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS schedules (
        group_number TEXT PRIMARY KEY,
        today_schedule TEXT,
        tomorrow_schedule TEXT,
        previous_today TEXT,
        previous_tomorrow TEXT,
        schedule_hash TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

def get_user_count() -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM users')
    count = c.fetchone()[0]
    conn.close()
    return count

def get_user_group(chat_id: int) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT group_number FROM users WHERE chat_id = ?', (chat_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def save_user_group(chat_id: int, group: str) -> bool:
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO users (chat_id, group_number) VALUES (?, ?)', (chat_id, group))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error saving user: {e}")
        return False

def get_all_users() -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT chat_id, group_number FROM users')
    users = [{"chat_id": r[0], "group": r[1]} for r in c.fetchall()]
    conn.close()
    return users

def delete_user(chat_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM users WHERE chat_id = ?', (chat_id,))
    conn.commit()
    deleted = c.rowcount > 0
    conn.close()
    return deleted

def get_schedule_from_db(group_number: str) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT today_schedule, tomorrow_schedule, updated_at FROM schedules WHERE group_number = ?', (group_number,))
    r = c.fetchone()
    conn.close()
    return {'today': r[0], 'tomorrow': r[1], 'updated_at': r[2]} if r else None

def save_schedule_to_db(group_number: str, today: str, tomorrow: str, schedule_hash: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT today_schedule, tomorrow_schedule FROM schedules WHERE group_number = ?', (group_number,))
    curr = c.fetchone()
    prev_today, prev_tomorrow = (curr[0], curr[1]) if curr else (None, None)
    
    c.execute('''INSERT INTO schedules (group_number, today_schedule, tomorrow_schedule, previous_today, previous_tomorrow, schedule_hash, updated_at)
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
    c = conn.cursor()
    c.execute('SELECT schedule_hash FROM schedules WHERE group_number = ?', (group_number,))
    r = c.fetchone()
    conn.close()
    return r[0] if r else None

# =============================================================================
# SCHEDULE PARSING
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

def to_minutes(time_str: str) -> int:
    h, m = map(int, time_str.split(':'))
    return h * 60 + m

def fmt_time(mins: int) -> str:
    return "24:00" if mins >= 1440 else f"{mins // 60:02d}:{mins % 60:02d}"

def fmt_hours(hours_float: float) -> str:
    return f"{hours_float:.1f}"

def extract_intervals(schedule_text: str) -> Dict[str, List[Tuple[int, int]]]:
    if not schedule_text:
        return {'on': [], 'off': []}
    
    off_ranges = re.findall(r'з (\d{1,2}:\d{2}) до (\d{1,2}:\d{2})', schedule_text)
    off_intervals = sorted([(to_minutes(s), to_minutes(e)) for s, e in off_ranges])
    
    on_intervals = []
    last_end = 0
    for start, end in off_intervals:
        if start > last_end:
            on_intervals.append((last_end, start))
        last_end = end
    if last_end < 1440:
        on_intervals.append((last_end, 1440))
    
    return {'on': on_intervals, 'off': off_intervals}

def calculate_diff(current: Dict, previous: Dict) -> Dict:
    return {
        'on_added': [iv for iv in current['on'] if iv not in previous['on']],
        'on_removed': [iv for iv in previous['on'] if iv not in current['on']],
        'off_added': [iv for iv in current['off'] if iv not in previous['off']],
        'off_removed': [iv for iv in previous['off'] if iv not in current['off']]
    }

# =============================================================================
# MESSAGE FORMATTING
# =============================================================================

def esc(text: str) -> str:
    """Escape MarkdownV2 special chars"""
    for char in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        text = text.replace(char, f'\\{char}')
    return text

def format_schedule_text(schedule_text: str) -> str:
    if not schedule_text:
        return "ℹ️ Інформація відсутня"

    intervals = extract_intervals(schedule_text)
    lines = ["🟢 *Є світло:*"]
    
    if intervals['on']:
        for s, e in intervals['on']:
            if s != e:
                lines.append(f"  • {fmt_time(s)} — {fmt_time(e)}")
    else:
        lines.append("  • немає даних")

    lines.append("\n🔴 *Немає світла:*")
    total_off = 0
    if intervals['off']:
        for s, e in intervals['off']:
            dur = e - s
            total_off += dur
            lines.append(f"  • {fmt_time(s)} — {fmt_time(e)} ({fmt_hours(dur/60)} год)")
        lines.append(f"\n⏱ *Загалом відключено:* {fmt_hours(total_off/60)} годин")
    else:
        lines.append("  • немає даних")

    return "\n".join(lines)

def format_notification_message(group_number: str, current_today: str, current_tomorrow: str, 
                                previous_today: str = None, previous_tomorrow: str = None) -> str:
    """Diff-first notification format"""
    
    msg = "⚡️ *Оновлення графіку відключень\\!*\n\n"
    msg += f"📍 Група: *{esc(group_number)}*\n\n"
    
    # Calculate diff
    curr_iv = extract_intervals(current_today)
    prev_iv = extract_intervals(previous_today) if previous_today else {'on': [], 'off': []}
    diff = calculate_diff(curr_iv, prev_iv)
    
    has_changes = any([diff['on_added'], diff['on_removed'], diff['off_added'], diff['off_removed']])
    
    # SECTION 1: WHAT CHANGED
    if has_changes:
        msg += "📊 *ЩО ЗМІНИЛОСЬ:*\n\n"
        
        if diff['off_removed']:
            msg += "✅ *Світло з\\'явилось:*\n"
            for s, e in diff['off_removed']:
                msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))}\n"
            msg += "\n"
        
        if diff['off_added']:
            msg += "⚠️ *Нові відключення:*\n"
            for s, e in diff['off_added']:
                dur = e - s
                msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))} \\({esc(fmt_hours(dur/60))} год\\)\n"
            msg += "\n"
        
        if diff['on_removed']:
            msg += "🔻 *Прибрано періоди зі світлом:*\n"
            for s, e in diff['on_removed']:
                msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))}\n"
            msg += "\n"
        
        if diff['on_added']:
            msg += "🔺 *Додано періоди зі світлом:*\n"
            for s, e in diff['on_added']:
                msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))}\n"
            msg += "\n"
        
        msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
    
    # SECTION 2: FULL SCHEDULE TODAY
    msg += "📅 *ПОВНИЙ ГРАФІК НА СЬОГОДНІ:*\n\n"
    msg += "🟢 *Є світло:*\n"
    
    if curr_iv['on']:
        for s, e in curr_iv['on']:
            if s != e:
                msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))}\n"
    else:
        msg += "  • немає даних\n"
    
    msg += "\n🔴 *Немає світла:*\n"
    total_off = 0
    if curr_iv['off']:
        for s, e in curr_iv['off']:
            dur = e - s
            total_off += dur
            msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))} \\({esc(fmt_hours(dur/60))} год\\)\n"
        msg += f"\n⏱ *Загалом відключено:* {esc(fmt_hours(total_off/60))} годин\n"
    else:
        msg += "  • немає даних\n"
    
    # SECTION 3: TOMORROW
    if current_tomorrow:
        msg += "\n━━━━━━━━━━━━━━━━━━━━\n\n"
        msg += "📅 *ЗАВТРА:*\n\n"
        
        tm_iv = extract_intervals(current_tomorrow)
        msg += "🟢 *Є світло:*\n"
        
        if tm_iv['on']:
            for s, e in tm_iv['on']:
                if s != e:
                    msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))}\n"
        else:
            msg += "  • немає даних\n"
        
        msg += "\n🔴 *Немає світла:*\n"
        total_off_tm = 0
        if tm_iv['off']:
            for s, e in tm_iv['off']:
                dur = e - s
                total_off_tm += dur
                msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))} \\({esc(fmt_hours(dur/60))} год\\)\n"
            msg += f"\n⏱ *Загалом відключено:* {esc(fmt_hours(total_off_tm/60))} годин\n"
        else:
            msg += "  • немає даних\n"
    
    msg += "\n_Графік може змінюватися протягом дня_"
    return msg

def format_schedule_message(group_number: str, today: str, tomorrow: str, updated_at: str) -> str:
    msg = f"📋 *Графік відключень*\n\n📍 Група: *{group_number}*\n\n"
    if today:
        msg += "📅 *Сьогодні*\n" + format_schedule_text(today) + "\n\n"
    if tomorrow:
        msg += "📅 *Завтра*\n" + format_schedule_text(tomorrow) + "\n\n"
    if updated_at:
        msg += f"🕐 Оновлено: _{updated_at}_\n"
    msg += "ℹ️ _Графік може змінюватися протягом дня_"
    return msg

# =============================================================================
# BACKGROUND TASKS
# =============================================================================

async def check_schedule_and_notify():
    global bot_app
    try:
        scraper = ScheduleScraper()
        json_content = scraper.fetch_schedule()
        if not json_content:
            return
        
        new_schedule = scraper.parse_schedule(json_content)
        if not new_schedule:
            return
        
        groups_data = new_schedule.get('groups', {})
        changed_groups = []
        
        for g_num, g_data in groups_data.items():
            t_text, tm_text = parse_schedule_entries(g_data)
            if not t_text:
                continue
            
            new_hash = hashlib.sha256(f"{t_text}|{tm_text or ''}".encode('utf-8')).hexdigest()
            
            if new_hash != get_schedule_hash(g_num):
                changed_groups.append(g_num)
                save_schedule_to_db(g_num, t_text or '', tm_text or '', new_hash)
        
        if not changed_groups:
            return
        
        for user in get_all_users():
            if user['group'] in changed_groups:
                try:
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute('SELECT today_schedule, tomorrow_schedule, previous_today, previous_tomorrow FROM schedules WHERE group_number = ?', (user['group'],))
                    result = c.fetchone()
                    conn.close()
                    
                    if result:
                        curr_today, curr_tomorrow, prev_today, prev_tomorrow = result
                        msg = format_notification_message(user['group'], curr_today, curr_tomorrow, prev_today, prev_tomorrow)
                        await bot_app.bot.send_message(chat_id=user['chat_id'], text=msg, parse_mode='MarkdownV2')
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
    query = update.callback_query
    await query.answer()
    
    if query.data == "action_schedule":
        group = get_user_group(query.from_user.id)
        if not group:
            await safe_edit(query, "❌ Спочатку оберіть групу:", parse_mode='Markdown',
                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Обрати групу", callback_data="action_setgroup")]]))
            return
        
        s = get_schedule_from_db(group)
        if not s:
            await safe_edit(query, "ℹ️ Завантаження графіку...", reply_markup=get_main_keyboard())
            return
        
        msg = format_schedule_message(group, s['today'], s['tomorrow'], s['updated_at'])
        await safe_edit(query, msg, parse_mode='Markdown', reply_markup=get_main_keyboard())
    
    elif query.data == "action_setgroup":
        kb = [[InlineKeyboardButton(g, callback_data=f"group_{g}") for g in GROUPS[i:i+3]] for i in range(0, len(GROUPS), 3)]
        await safe_edit(query, "Оберіть вашу групу відключень:", reply_markup=InlineKeyboardMarkup(kb))
    
    elif query.data == "action_mygroup":
        g = get_user_group(query.from_user.id)
        text = f"📍 Ваша група: *{g}*\n\nОберіть дію:" if g else "❌ Група не обрана\n\nОберіть дію:"
        await safe_edit(query, text, parse_mode='Markdown', reply_markup=get_main_keyboard())

async def start_command(update, context):
    await update.message.reply_text("Вітаю! 👋\n\nЯ допоможу відстежувати графік відключень світла.\n\nОберіть дію:", reply_markup=get_main_keyboard())

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
        await safe_edit(query, f"✅ Групу {group} збережено!\n\nОберіть дію:", reply_markup=get_main_keyboard())

async def schedule_command(update, context):
    group = get_user_group(update.effective_chat.id)
    if not group:
        await update.message.reply_text("❌ Спочатку оберіть групу:", reply_markup=get_main_keyboard())
        return
    s = get_schedule_from_db(group)
    if not s:
        await update.message.reply_text("ℹ️ Завантаження графіку...", reply_markup=get_main_keyboard())
        return
    await update.message.reply_text(format_schedule_message(group, s['today'], s['tomorrow'], s['updated_at']), 
                                   parse_mode='Markdown', reply_markup=get_main_keyboard())

async def mygroup_command(update, context):
    g = get_user_group(update.effective_chat.id)
    text = f"📍 Ваша група: *{g}*" if g else "❌ Група не обрана"
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=get_main_keyboard())

async def stop_command(update, context):
    if delete_user(update.effective_chat.id):
        await update.message.reply_text("✅ Ви відписані від сповіщень.\n\nЩоб підписатись знову, натисніть /start")
    else:
        await update.message.reply_text("ℹ️ Ви не були підписані на сповіщення.")

# =============================================================================
# FLASK API
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
            except Exception as e:
                logger.error(f"Queue error: {e}")
        await asyncio.sleep(0.1)

async def setup_application():
    global bot_app
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler('start', start_command))
    bot_app.add_handler(CommandHandler('setgroup', setgroup_command))
    bot_app.add_handler(CommandHandler('schedule', schedule_command))
    bot_app.add_handler(CommandHandler('mygroup', mygroup_command))
    bot_app.add_handler(CommandHandler('stop', stop_command))
    bot_app.add_handler(CallbackQueryHandler(group_selection, pattern='^group_'))
    bot_app.add_handler(CallbackQueryHandler(handle_inline_actions, pattern='^action_'))
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