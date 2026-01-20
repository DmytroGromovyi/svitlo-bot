#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Svitlo Bot - Simplified Power Outage Notification Bot
"""

import os
import logging
import sqlite3
import json
import re
import hashlib
from pathlib import Path
from queue import Queue
from threading import Thread
from datetime import datetime
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from flask import Flask, request, jsonify

import sys
sys.path.append(os.path.dirname(__file__))
from scraper import ScheduleScraper

# =============================================================================
# CONFIG
# =============================================================================

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
API_SECRET = os.getenv('API_SECRET')
PORT = int(os.getenv('PORT', 8080))
WEBHOOK_URL = os.getenv('WEBHOOK_URL')

MAX_USERS = 25
DB_PATH = '/data/users.db'
GROUPS = [f"{i}.{j}" for i in range(1, 7) for j in range(1, 3)]  # Only X.1 and X.2 exist!

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

bot_app = None
update_queue = Queue()

# =============================================================================
# KEYBOARDS
# =============================================================================

REPLY_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("📋 Графік"), KeyboardButton("ℹ️ Моя група")],
    [KeyboardButton("🔄 Змінити групу")]
], resize_keyboard=True)

INLINE_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("📋 Графік", callback_data="schedule"),
     InlineKeyboardButton("ℹ️ Моя група", callback_data="mygroup")],
    [InlineKeyboardButton("🔄 Змінити групу", callback_data="setgroup")]
])

# =============================================================================
# DATABASE
# =============================================================================

def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Migrate old table if needed
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
    if c.fetchone():
        c.execute("PRAGMA table_info(users)")
        if 'group' in [row[1] for row in c.fetchall()]:
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

def db_execute(query, params=(), fetch_one=False, fetch_all=False):
    """Single DB helper to reduce boilerplate"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(query, params)
    
    result = None
    if fetch_one:
        result = c.fetchone()
    elif fetch_all:
        result = c.fetchall()
    
    conn.commit()
    conn.close()
    return result

def get_user_group(chat_id):
    result = db_execute('SELECT group_number FROM users WHERE chat_id = ?', (chat_id,), fetch_one=True)
    return result[0] if result else None

def save_user_group(chat_id, group):
    try:
        db_execute('INSERT OR REPLACE INTO users (chat_id, group_number) VALUES (?, ?)', (chat_id, group))
        return True
    except:
        return False

def get_all_users():
    rows = db_execute('SELECT chat_id, group_number FROM users', fetch_all=True)
    return [{"chat_id": r[0], "group": r[1]} for r in rows]

def get_schedule(group_number):
    result = db_execute('SELECT today_schedule, tomorrow_schedule, updated_at FROM schedules WHERE group_number = ?', 
                       (group_number,), fetch_one=True)
    return {'today': result[0], 'tomorrow': result[1], 'updated_at': result[2]} if result else None

def save_schedule(group_number, today, tomorrow, schedule_hash):
    # Get current to store as previous
    curr = db_execute('SELECT today_schedule, tomorrow_schedule FROM schedules WHERE group_number = ?', 
                     (group_number,), fetch_one=True)
    prev_today, prev_tomorrow = (curr[0], curr[1]) if curr else (None, None)
    
    db_execute('''INSERT INTO schedules (group_number, today_schedule, tomorrow_schedule, previous_today, previous_tomorrow, schedule_hash, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(group_number) DO UPDATE SET
            previous_today = schedules.today_schedule,
            previous_tomorrow = schedules.tomorrow_schedule,
            today_schedule = excluded.today_schedule,
            tomorrow_schedule = excluded.tomorrow_schedule,
            schedule_hash = excluded.schedule_hash,
            updated_at = CURRENT_TIMESTAMP
    ''', (group_number, today, tomorrow, prev_today, prev_tomorrow, schedule_hash))

def get_schedule_hash(group_number):
    result = db_execute('SELECT schedule_hash FROM schedules WHERE group_number = ?', (group_number,), fetch_one=True)
    return result[0] if result else None

# =============================================================================
# SCHEDULE PARSING
# =============================================================================

def parse_schedule_entries(group_data):
    """Extract today and tomorrow schedules from group data"""
    today, tomorrow = None, None
    for entry in group_data:
        date = entry.get('date', '').lower()
        schedule = entry.get('schedule', '')
        if 'сьогодні' in date or 'сього' in date:
            today = schedule
        elif 'завтра' in date:
            tomorrow = schedule
        elif not today:
            today = schedule
        elif not tomorrow:
            tomorrow = schedule
    return today, tomorrow

def extract_intervals(schedule_text):
    """Extract ON/OFF intervals from schedule text"""
    if not schedule_text:
        return {'on': [], 'off': []}
    
    # Find all OFF periods
    off_ranges = re.findall(r'з (\d{1,2}:\d{2}) до (\d{1,2}:\d{2})', schedule_text)
    to_min = lambda t: int(t.split(':')[0]) * 60 + int(t.split(':')[1])
    off_intervals = sorted([(to_min(s), to_min(e)) for s, e in off_ranges])
    
    # Calculate ON periods (gaps between OFF)
    on_intervals = []
    last = 0
    for start, end in off_intervals:
        if start > last:
            on_intervals.append((last, start))
        last = end
    if last < 1440:
        on_intervals.append((last, 1440))
    
    return {'on': on_intervals, 'off': off_intervals}

def fmt_time(mins):
    """Format minutes as HH:MM"""
    return "24:00" if mins >= 1440 else f"{mins // 60:02d}:{mins % 60:02d}"

def fmt_hours(hours):
    """Format hours for display"""
    return f"{hours:.1f}"

def esc(text):
    """Escape MarkdownV2 special chars"""
    for c in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        text = text.replace(c, f'\\{c}')
    return text

# =============================================================================
# MESSAGE FORMATTING
# =============================================================================

def format_schedule_display(schedule_text):
    """Format schedule for regular display"""
    if not schedule_text:
        return "ℹ️ Інформація відсутня"

    iv = extract_intervals(schedule_text)
    lines = ["🟢 *Є світло:*"]
    
    for s, e in iv['on']:
        if s != e:
            lines.append(f"  • {fmt_time(s)} — {fmt_time(e)}")
    if not iv['on']:
        lines.append("  • немає даних")

    lines.append("\n🔴 *Немає світла:*")
    total = 0
    for s, e in iv['off']:
        dur = e - s
        total += dur
        lines.append(f"  • {fmt_time(s)} — {fmt_time(e)} ({fmt_hours(dur/60)} год)")
    if iv['off']:
        lines.append(f"\n⏱ *Загалом відключено:* {fmt_hours(total/60)} годин")
    else:
        lines.append("  • немає даних")

    return "\n".join(lines)

def format_notification(group, curr_today, curr_tomorrow, prev_today=None, prev_tomorrow=None):
    """Format notification with diff-first approach"""
    msg = f"⚡️ *Оновлення графіку відключень\\!*\n\n📍 Група: *{esc(group)}*\n\n"
    
    # Calculate diff
    curr = extract_intervals(curr_today)
    prev = extract_intervals(prev_today) if prev_today else {'on': [], 'off': []}
    
    off_removed = [iv for iv in prev['off'] if iv not in curr['off']]
    off_added = [iv for iv in curr['off'] if iv not in prev['off']]
    on_removed = [iv for iv in prev['on'] if iv not in curr['on']]
    on_added = [iv for iv in curr['on'] if iv not in prev['on']]
    
    # Show changes
    if off_removed or off_added or on_removed or on_added:
        msg += "📊 *ЩО ЗМІНИЛОСЬ:*\n\n"
        
        if off_removed:
            msg += "✅ *Світло з\\'явилось:*\n"
            for s, e in off_removed:
                msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))}\n"
            msg += "\n"
        
        if off_added:
            msg += "⚠️ *Нові відключення:*\n"
            for s, e in off_added:
                msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))} \\({esc(fmt_hours((e-s)/60))} год\\)\n"
            msg += "\n"
        
        if on_removed:
            msg += "🔻 *Прибрано періоди зі світлом:*\n"
            for s, e in on_removed:
                msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))}\n"
            msg += "\n"
        
        if on_added:
            msg += "🔺 *Додано періоди зі світлом:*\n"
            for s, e in on_added:
                msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))}\n"
            msg += "\n"
        
        msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
    
    # Full schedule today
    msg += "📅 *ПОВНИЙ ГРАФІК НА СЬОГОДНІ:*\n\n🟢 *Є світло:*\n"
    for s, e in curr['on']:
        if s != e:
            msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))}\n"
    if not curr['on']:
        msg += "  • немає даних\n"
    
    msg += "\n🔴 *Немає світла:*\n"
    total = 0
    for s, e in curr['off']:
        dur = e - s
        total += dur
        msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))} \\({esc(fmt_hours(dur/60))} год\\)\n"
    if curr['off']:
        msg += f"\n⏱ *Загалом відключено:* {esc(fmt_hours(total/60))} годин\n"
    else:
        msg += "  • немає даних\n"
    
    # Tomorrow if available
    if curr_tomorrow:
        msg += "\n━━━━━━━━━━━━━━━━━━━━\n\n📅 *ЗАВТРА:*\n\n"
        tm = extract_intervals(curr_tomorrow)
        
        msg += "🟢 *Є світло:*\n"
        for s, e in tm['on']:
            if s != e:
                msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))}\n"
        if not tm['on']:
            msg += "  • немає даних\n"
        
        msg += "\n🔴 *Немає світла:*\n"
        total_tm = 0
        for s, e in tm['off']:
            dur = e - s
            total_tm += dur
            msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))} \\({esc(fmt_hours(dur/60))} год\\)\n"
        if tm['off']:
            msg += f"\n⏱ *Загалом відключено:* {esc(fmt_hours(total_tm/60))} годин\n"
        else:
            msg += "  • немає даних\n"
    
    msg += "\n_Графік може змінюватися протягом дня_"
    return msg

# =============================================================================
# BACKGROUND CHECKER
# =============================================================================

async def check_and_notify():
    """Check for schedule updates and notify users"""
    try:
        scraper = ScheduleScraper()
        json_content = scraper.fetch_schedule()
        if not json_content:
            logger.warning("Failed to fetch schedule from API")
            return
        
        schedule = scraper.parse_schedule(json_content)
        if not schedule:
            logger.warning("Failed to parse schedule")
            return
        
        groups_data = schedule.get('groups', {})
        if not groups_data:
            logger.warning("No groups found in schedule")
            return
        
        logger.info(f"Processing {len(groups_data)} groups from schedule")
        
        # Process ALL groups from the fetched schedule
        changed_groups = []
        saved_count = 0
        for group, data in groups_data.items():
            today, tomorrow = parse_schedule_entries(data)
            if not today:
                logger.warning(f"No schedule found for group {group}")
                continue
            
            new_hash = hashlib.sha256(f"{today}|{tomorrow or ''}".encode()).hexdigest()
            old_hash = get_schedule_hash(group)
            
            # Save schedule for ALL groups (not just changed ones)
            save_schedule(group, today or '', tomorrow or '', new_hash)
            saved_count += 1
            logger.info(f"Saved schedule for group {group}")
            
            # Track which groups changed for notifications
            if new_hash != old_hash and old_hash is not None:
                changed_groups.append(group)
        
        logger.info(f"Saved schedules for {saved_count} groups, {len(changed_groups)} changed")
        
        if not changed_groups:
            return
        
        # Notify users only for changed groups
        for user in get_all_users():
            if user['group'] in changed_groups:
                try:
                    result = db_execute(
                        'SELECT today_schedule, tomorrow_schedule, previous_today, previous_tomorrow FROM schedules WHERE group_number = ?',
                        (user['group'],), fetch_one=True
                    )
                    if result:
                        msg = format_notification(user['group'], result[0], result[1], result[2], result[3])
                        await bot_app.bot.send_message(chat_id=user['chat_id'], text=msg, parse_mode='MarkdownV2')
                        await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error(f"Notify error {user['chat_id']}: {e}")
                    
    except Exception as e:
        logger.error(f"Checker error: {e}", exc_info=True)

async def checker_loop():
    """Background loop to check schedules"""
    # Fetch schedules immediately on startup
    logger.info("Fetching initial schedules...")
    await check_and_notify()
    logger.info("Initial fetch complete")
    
    # Then continue with regular interval
    while True:
        await asyncio.sleep(300)
        await check_and_notify()

# =============================================================================
# TELEGRAM HANDLERS
# =============================================================================

async def safe_edit(query, text, parse_mode=None, reply_markup=None):
    """Safe message edit that ignores 'not modified' errors"""
    try:
        await query.edit_message_text(text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

async def start(update, context):
    await update.message.reply_text(
        "Вітаю! 👋\n\nЯ допоможу відстежувати графік відключень світла.\n\nОберіть дію:",
        reply_markup=REPLY_KEYBOARD
    )

async def show_schedule(update, context):
    """Show schedule for user's group"""
    chat_id = update.effective_chat.id
    group = get_user_group(chat_id)
    
    if not group:
        await update.message.reply_text("❌ Спочатку оберіть групу", reply_markup=REPLY_KEYBOARD)
        return
    
    schedule = get_schedule(group)
    if not schedule:
        await update.message.reply_text("ℹ️ Завантаження графіку...", reply_markup=REPLY_KEYBOARD)
        return
    
    msg = f"📋 *Графік відключень*\n\n📍 Група: *{group}*\n\n"
    if schedule['today']:
        msg += "📅 *Сьогодні*\n" + format_schedule_display(schedule['today']) + "\n\n"
    if schedule['tomorrow']:
        msg += "📅 *Завтра*\n" + format_schedule_display(schedule['tomorrow']) + "\n\n"
    if schedule['updated_at']:
        msg += f"🕐 Оновлено: _{schedule['updated_at']}_\n"
    msg += "ℹ️ _Графік може змінюватися протягом дня_"
    
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=REPLY_KEYBOARD)

async def show_group(update, context):
    """Show user's current group"""
    group = get_user_group(update.effective_chat.id)
    text = f"📍 Ваша група: *{group}*" if group else "❌ Група не обрана"
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=REPLY_KEYBOARD)

async def select_group(update, context):
    """Show group selection menu"""
    chat_id = update.effective_chat.id
    user_count = db_execute('SELECT COUNT(*) FROM users', fetch_one=True)[0]
    
    if not get_user_group(chat_id) and user_count >= MAX_USERS:
        await update.message.reply_text("❌ Ліміт користувачів", reply_markup=REPLY_KEYBOARD)
        return
    
    kb = [[InlineKeyboardButton(g, callback_data=f"g_{g}") for g in GROUPS[i:i+3]] for i in range(0, len(GROUPS), 3)]
    await update.message.reply_text("Оберіть групу:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_callback(update, context):
    """Handle all callback queries"""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    # Group selection
    if data.startswith("g_"):
        group = data[2:]
        if save_user_group(query.from_user.id, group):
            # Try to load and show schedule immediately after group selection
            schedule = get_schedule(group)
            if schedule and schedule['today']:
                msg = f"✅ Групу {group} збережено!\n\n"
                msg += f"📋 *Графік відключень*\n\n📍 Група: *{group}*\n\n"
                if schedule['today']:
                    msg += "📅 *Сьогодні*\n" + format_schedule_display(schedule['today']) + "\n\n"
                if schedule['tomorrow']:
                    msg += "📅 *Завтра*\n" + format_schedule_display(schedule['tomorrow']) + "\n\n"
                if schedule['updated_at']:
                    msg += f"🕐 Оновлено: _{schedule['updated_at']}_\n"
                msg += "ℹ️ _Графік може змінюватися протягом дня_"
                await safe_edit(query, msg, parse_mode='Markdown', reply_markup=INLINE_KEYBOARD)
            else:
                await safe_edit(query, f"✅ Групу {group} збережено!\n\nℹ️ Графік ще не завантажено. Спробуйте пізніше або натисніть 📋 Графік.", reply_markup=INLINE_KEYBOARD)
        return
    
    # Inline menu actions
    if data == "schedule":
        group = get_user_group(query.from_user.id)
        if not group:
            await safe_edit(query, "❌ Спочатку оберіть групу", reply_markup=INLINE_KEYBOARD)
            return
        
        schedule = get_schedule(group)
        if not schedule:
            await safe_edit(query, "ℹ️ Завантаження графіку...", reply_markup=INLINE_KEYBOARD)
            return
        
        msg = f"📋 *Графік відключень*\n\n📍 Група: *{group}*\n\n"
        if schedule['today']:
            msg += "📅 *Сьогодні*\n" + format_schedule_display(schedule['today']) + "\n\n"
        if schedule['tomorrow']:
            msg += "📅 *Завтра*\n" + format_schedule_display(schedule['tomorrow']) + "\n\n"
        if schedule['updated_at']:
            msg += f"🕐 Оновлено: _{schedule['updated_at']}_\n"
        msg += "ℹ️ _Графік може змінюватися протягом дня_"
        
        await safe_edit(query, msg, parse_mode='Markdown', reply_markup=INLINE_KEYBOARD)
    
    elif data == "mygroup":
        group = get_user_group(query.from_user.id)
        text = f"📍 Ваша група: *{group}*\n\nОберіть дію:" if group else "❌ Група не обрана\n\nОберіть дію:"
        await safe_edit(query, text, parse_mode='Markdown', reply_markup=INLINE_KEYBOARD)
    
    elif data == "setgroup":
        kb = [[InlineKeyboardButton(g, callback_data=f"g_{g}") for g in GROUPS[i:i+3]] for i in range(0, len(GROUPS), 3)]
        await safe_edit(query, "Оберіть вашу групу відключень:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_text(update, context):
    """Handle reply keyboard buttons"""
    text = update.message.text
    if text == "📋 Графік":
        await show_schedule(update, context)
    elif text == "ℹ️ Моя група":
        await show_group(update, context)
    elif text == "🔄 Змінити групу":
        await select_group(update, context)

async def stop(update, context):
    """Unsubscribe user"""
    deleted = db_execute('DELETE FROM users WHERE chat_id = ?', (update.effective_chat.id,)).rowcount > 0
    text = "✅ Ви відписані від сповіщень.\n\nЩоб підписатись знову, натисніть /start" if deleted else "ℹ️ Ви не були підписані"
    await update.message.reply_text(text)

# =============================================================================
# FLASK API
# =============================================================================

flask_app = Flask(__name__)

@flask_app.route('/health')
def health():
    count = db_execute('SELECT COUNT(*) FROM users', fetch_one=True)[0]
    return jsonify({'status': 'healthy', 'users': count})

@flask_app.route('/api/users')
def api_users():
    auth = request.headers.get('Authorization', '').replace('Bearer ', '')
    if auth != API_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    users = get_all_users()
    return jsonify({'users': users, 'count': len(users)})

@flask_app.route('/webhook', methods=['POST'])
def webhook():
    update_queue.put(request.get_json(force=True))
    return 'OK'

# =============================================================================
# APP SETUP
# =============================================================================

async def process_updates():
    """Process updates from queue"""
    while True:
        if not update_queue.empty():
            try:
                data = update_queue.get()
                update = Update.de_json(data, bot_app.bot)
                await bot_app.process_update(update)
            except Exception as e:
                logger.error(f"Update error: {e}")
        await asyncio.sleep(0.1)

async def setup():
    """Setup Telegram bot"""
    global bot_app
    bot_app = Application.builder().token(BOT_TOKEN).build()
    
    # Commands
    bot_app.add_handler(CommandHandler('start', start))
    bot_app.add_handler(CommandHandler('schedule', show_schedule))
    bot_app.add_handler(CommandHandler('mygroup', show_group))
    bot_app.add_handler(CommandHandler('setgroup', select_group))
    bot_app.add_handler(CommandHandler('stop', stop))
    
    # Callbacks and text
    bot_app.add_handler(CallbackQueryHandler(handle_callback))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")

def run_bot():
    """Run bot in separate thread"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(setup())
    loop.create_task(process_updates())
    loop.create_task(checker_loop())
    loop.run_forever()

if __name__ == '__main__':
    init_db()
    Thread(target=run_bot, daemon=True).start()
    flask_app.run(host='0.0.0.0', port=PORT)