#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Svitlo Bot - Simplified Power Outage Notification Bot
Multi-Group Support Version
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
MAX_GROUPS_PER_USER = 6  # Maximum groups a user can subscribe to
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
    [KeyboardButton("📋 Графік"), KeyboardButton("ℹ️ Мої групи")],
    [KeyboardButton("➕ Додати групу"), KeyboardButton("➖ Видалити групу")]
], resize_keyboard=True)

def get_inline_keyboard(has_groups=True):
    """Generate inline keyboard based on user state"""
    if has_groups:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Графік", callback_data="schedule"),
             InlineKeyboardButton("ℹ️ Мої групи", callback_data="mygroups")],
            [InlineKeyboardButton("➕ Додати групу", callback_data="addgroup"),
             InlineKeyboardButton("➖ Видалити групу", callback_data="removegroup")]
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Додати групу", callback_data="addgroup")]
        ])

# =============================================================================
# DATABASE
# =============================================================================

def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Check if old table exists and migrate
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
    if c.fetchone():
        # Check if it's the old single-group schema
        c.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in c.fetchall()]
        
        if 'group_number' in columns or 'group' in columns:
            logger.info("Migrating from single-group to multi-group schema...")
            
            # Rename old table
            c.execute('ALTER TABLE users RENAME TO users_old')
            
            # Create new tables
            c.execute('''CREATE TABLE users (
                chat_id INTEGER PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
            c.execute('''CREATE TABLE user_groups (
                chat_id INTEGER,
                group_number TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, group_number),
                FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
            )''')
            
            # Migrate data
            group_col = 'group_number' if 'group_number' in columns else 'group'
            c.execute(f'SELECT chat_id, {group_col}, created_at FROM users_old')
            old_users = c.fetchall()
            
            for chat_id, group_num, created_at in old_users:
                c.execute('INSERT OR IGNORE INTO users (chat_id, created_at) VALUES (?, ?)', 
                         (chat_id, created_at))
                if group_num:
                    c.execute('INSERT OR IGNORE INTO user_groups (chat_id, group_number) VALUES (?, ?)',
                             (chat_id, group_num))
            
            # Drop old table
            c.execute('DROP TABLE users_old')
            logger.info(f"Migration complete: {len(old_users)} users migrated")
    else:
        # Create new tables from scratch
        c.execute('''CREATE TABLE users (
            chat_id INTEGER PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE user_groups (
            chat_id INTEGER,
            group_number TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, group_number),
            FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
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

def get_user_groups(chat_id):
    """Get all groups for a user"""
    rows = db_execute('SELECT group_number FROM user_groups WHERE chat_id = ? ORDER BY group_number', 
                     (chat_id,), fetch_all=True)
    return [row[0] for row in rows] if rows else []

def add_user_group(chat_id, group):
    """Add a group to user's subscriptions"""
    try:
        # Ensure user exists
        db_execute('INSERT OR IGNORE INTO users (chat_id) VALUES (?)', (chat_id,))
        
        # Check group limit
        current_groups = get_user_groups(chat_id)
        if len(current_groups) >= MAX_GROUPS_PER_USER:
            return False, f"Максимум {MAX_GROUPS_PER_USER} груп"
        
        # Add group
        db_execute('INSERT OR IGNORE INTO user_groups (chat_id, group_number) VALUES (?, ?)', 
                  (chat_id, group))
        return True, None
    except Exception as e:
        logger.error(f"Error adding group: {e}")
        return False, "Помилка при додаванні групи"

def remove_user_group(chat_id, group):
    """Remove a group from user's subscriptions"""
    try:
        db_execute('DELETE FROM user_groups WHERE chat_id = ? AND group_number = ?', 
                  (chat_id, group))
        return True
    except:
        return False

def get_all_users():
    """Get all users with their groups"""
    rows = db_execute('''
        SELECT u.chat_id, GROUP_CONCAT(ug.group_number, ',') as groups
        FROM users u
        LEFT JOIN user_groups ug ON u.chat_id = ug.chat_id
        GROUP BY u.chat_id
    ''', fetch_all=True)
    
    result = []
    for chat_id, groups_str in rows:
        groups = groups_str.split(',') if groups_str else []
        result.append({"chat_id": chat_id, "groups": groups})
    return result

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
            user_changed_groups = [g for g in user['groups'] if g in changed_groups]
            
            for group in user_changed_groups:
                try:
                    result = db_execute(
                        'SELECT today_schedule, tomorrow_schedule, previous_today, previous_tomorrow FROM schedules WHERE group_number = ?',
                        (group,), fetch_one=True
                    )
                    if result:
                        msg = format_notification(group, result[0], result[1], result[2], result[3])
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
    groups = get_user_groups(update.effective_chat.id)
    if groups:
        await update.message.reply_text(
            f"Вітаю! 👋\n\nВи підписані на {len(groups)} груп(у/и).\n\nОберіть дію:",
            reply_markup=REPLY_KEYBOARD
        )
    else:
        await update.message.reply_text(
            "Вітаю! 👋\n\nЯ допоможу відстежувати графік відключень світла.\n\nДодайте групу для початку:",
            reply_markup=REPLY_KEYBOARD
        )

async def show_schedule(update, context):
    """Show schedule for all user's groups"""
    chat_id = update.effective_chat.id
    groups = get_user_groups(chat_id)
    
    if not groups:
        await update.message.reply_text("❌ Спочатку додайте групу", reply_markup=REPLY_KEYBOARD)
        return
    
    for group in groups:
        schedule = get_schedule(group)
        if not schedule:
            await update.message.reply_text(f"ℹ️ Завантаження графіку для групи {group}...", 
                                          reply_markup=REPLY_KEYBOARD)
            continue
        
        msg = f"📋 *Графік відключень*\n\n📍 Група: *{group}*\n\n"
        if schedule['today']:
            msg += "📅 *Сьогодні*\n" + format_schedule_display(schedule['today']) + "\n\n"
        if schedule['tomorrow']:
            msg += "📅 *Завтра*\n" + format_schedule_display(schedule['tomorrow']) + "\n\n"
        if schedule['updated_at']:
            msg += f"🕐 Оновлено: _{schedule['updated_at']}_\n"
        msg += "ℹ️ _Графік може змінюватися протягом дня_"
        
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=REPLY_KEYBOARD)
        if len(groups) > 1:
            await asyncio.sleep(0.3)  # Small delay between multiple messages

async def show_groups(update, context):
    """Show user's current groups"""
    groups = get_user_groups(update.effective_chat.id)
    if groups:
        groups_str = ", ".join(groups)
        text = f"📍 Ваші групи: *{groups_str}*\n\n_Ви можете мати до {MAX_GROUPS_PER_USER} груп_"
    else:
        text = "❌ Групи не обрані"
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=REPLY_KEYBOARD)

async def add_group(update, context):
    """Show group selection menu to add"""
    chat_id = update.effective_chat.id
    user_count = db_execute('SELECT COUNT(*) FROM users', fetch_one=True)[0]
    current_groups = get_user_groups(chat_id)
    
    if not current_groups and user_count >= MAX_USERS:
        await update.message.reply_text("❌ Ліміт користувачів", reply_markup=REPLY_KEYBOARD)
        return
    
    if len(current_groups) >= MAX_GROUPS_PER_USER:
        await update.message.reply_text(
            f"❌ Ви вже підписані на максимальну кількість груп ({MAX_GROUPS_PER_USER})",
            reply_markup=REPLY_KEYBOARD
        )
        return
    
    # Show available groups (exclude already subscribed)
    available = [g for g in GROUPS if g not in current_groups]
    kb = [[InlineKeyboardButton(g, callback_data=f"add_{g}") for g in available[i:i+3]] 
          for i in range(0, len(available), 3)]
    
    await update.message.reply_text("Оберіть групу для додавання:", reply_markup=InlineKeyboardMarkup(kb))

async def remove_group(update, context):
    """Show group selection menu to remove"""
    groups = get_user_groups(update.effective_chat.id)
    
    if not groups:
        await update.message.reply_text("❌ У вас немає груп", reply_markup=REPLY_KEYBOARD)
        return
    
    kb = [[InlineKeyboardButton(g, callback_data=f"rem_{g}") for g in groups[i:i+3]] 
          for i in range(0, len(groups), 3)]
    
    await update.message.reply_text("Оберіть групу для видалення:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_callback(update, context):
    """Handle all callback queries"""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.from_user.id
    
    # Add group
    if data.startswith("add_"):
        group = data[4:]
        success, error = add_user_group(chat_id, group)
        
        if success:
            # Try to load and show schedule immediately
            schedule = get_schedule(group)
            groups = get_user_groups(chat_id)
            
            if schedule and schedule['today']:
                msg = f"✅ Групу {group} додано!\n\n"
                msg += f"📋 *Графік відключень*\n\n📍 Група: *{group}*\n\n"
                if schedule['today']:
                    msg += "📅 *Сьогодні*\n" + format_schedule_display(schedule['today']) + "\n\n"
                if schedule['tomorrow']:
                    msg += "📅 *Завтра*\n" + format_schedule_display(schedule['tomorrow']) + "\n\n"
                if schedule['updated_at']:
                    msg += f"🕐 Оновлено: _{schedule['updated_at']}_\n"
                msg += f"\n_Всього груп: {len(groups)}/{MAX_GROUPS_PER_USER}_"
                await safe_edit(query, msg, parse_mode='Markdown', reply_markup=get_inline_keyboard(True))
            else:
                await safe_edit(query, 
                    f"✅ Групу {group} додано!\n\nℹ️ Графік ще не завантажено.\n\n_Всього груп: {len(groups)}/{MAX_GROUPS_PER_USER}_", 
                    reply_markup=get_inline_keyboard(True))
        else:
            await safe_edit(query, f"❌ {error}", reply_markup=get_inline_keyboard(bool(get_user_groups(chat_id))))
        return
    
    # Remove group
    if data.startswith("rem_"):
        group = data[4:]
        if remove_user_group(chat_id, group):
            groups = get_user_groups(chat_id)
            await safe_edit(query, 
                f"✅ Групу {group} видалено\n\n_Залишилось груп: {len(groups)}/{MAX_GROUPS_PER_USER}_", 
                reply_markup=get_inline_keyboard(bool(groups)))
        return
    
    # Inline menu actions
    if data == "schedule":
        groups = get_user_groups(chat_id)
        if not groups:
            await safe_edit(query, "❌ Спочатку додайте групу", reply_markup=get_inline_keyboard(False))
            return
        
        # Show first group schedule in edit, then send others as new messages
        first_group = groups[0]
        schedule = get_schedule(first_group)
        
        if schedule:
            msg = f"📋 *Графік відключень*\n\n📍 Група: *{first_group}*\n\n"
            if schedule['today']:
                msg += "📅 *Сьогодні*\n" + format_schedule_display(schedule['today']) + "\n\n"
            if schedule['tomorrow']:
                msg += "📅 *Завтра*\n" + format_schedule_display(schedule['tomorrow']) + "\n\n"
            if schedule['updated_at']:
                msg += f"🕐 Оновлено: _{schedule['updated_at']}_\n"
            msg += "ℹ️ _Графік може змінюватися протягом дня_"
            
            await safe_edit(query, msg, parse_mode='Markdown', reply_markup=get_inline_keyboard(True))
        
        # Send remaining groups as new messages
        for group in groups[1:]:
            schedule = get_schedule(group)
            if schedule:
                msg = f"📋 *Графік відключень*\n\n📍 Група: *{group}*\n\n"
                if schedule['today']:
                    msg += "📅 *Сьогодні*\n" + format_schedule_display(schedule['today']) + "\n\n"
                if schedule['tomorrow']:
                    msg += "📅 *Завтра*\n" + format_schedule_display(schedule['tomorrow']) + "\n\n"
                if schedule['updated_at']:
                    msg += f"🕐 Оновлено: _{schedule['updated_at']}_\n"
                msg += "ℹ️ _Графік може змінюватися протягом дня_"
                
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
                await asyncio.sleep(0.3)
    
    elif data == "mygroups":
        groups = get_user_groups(chat_id)
        if groups:
            groups_str = ", ".join(groups)
            text = f"📍 Ваші групи: *{groups_str}*\n\n_Ви можете мати до {MAX_GROUPS_PER_USER} груп_\n\nОберіть дію:"
        else:
            text = "❌ Групи не обрані\n\nОберіть дію:"
        await safe_edit(query, text, parse_mode='Markdown', reply_markup=get_inline_keyboard(bool(groups)))
    
    elif data == "addgroup":
        current_groups = get_user_groups(chat_id)
        
        if len(current_groups) >= MAX_GROUPS_PER_USER:
            await safe_edit(query, 
                f"❌ Ви вже підписані на максимальну кількість груп ({MAX_GROUPS_PER_USER})",
                reply_markup=get_inline_keyboard(True))
            return
        
        available = [g for g in GROUPS if g not in current_groups]
        kb = [[InlineKeyboardButton(g, callback_data=f"add_{g}") for g in available[i:i+3]] 
              for i in range(0, len(available), 3)]
        
        await safe_edit(query, "Оберіть групу для додавання:", reply_markup=InlineKeyboardMarkup(kb))
    
    elif data == "removegroup":
        groups = get_user_groups(chat_id)
        
        if not groups:
            await safe_edit(query, "❌ У вас немає груп", reply_markup=get_inline_keyboard(False))
            return
        
        kb = [[InlineKeyboardButton(g, callback_data=f"rem_{g}") for g in groups[i:i+3]] 
              for i in range(0, len(groups), 3)]
        
        await safe_edit(query, "Оберіть групу для видалення:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_text(update, context):
    """Handle reply keyboard buttons"""
    text = update.message.text
    if text == "📋 Графік":
        await show_schedule(update, context)
    elif text == "ℹ️ Мої групи":
        await show_groups(update, context)
    elif text == "➕ Додати групу":
        await add_group(update, context)
    elif text == "➖ Видалити групу":
        await remove_group(update, context)

async def stop(update, context):
    """Unsubscribe user"""
    deleted = db_execute('DELETE FROM users WHERE chat_id = ?', (update.effective_chat.id,))
    # Cascade delete will handle user_groups
    text = "✅ Ви відписані від сповіщень.\n\nЩоб підписатись знову, натисніть /start"
    await update.message.reply_text(text)

# =============================================================================
# FLASK API
# =============================================================================

flask_app = Flask(__name__)

@flask_app.route('/health')
def health():
    count = db_execute('SELECT COUNT(*) FROM users', fetch_one=True)[0]
    total_groups = db_execute('SELECT COUNT(*) FROM user_groups', fetch_one=True)[0]
    return jsonify({'status': 'healthy', 'users': count, 'total_subscriptions': total_groups})

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
    bot_app.add_handler(CommandHandler('mygroups', show_groups))
    bot_app.add_handler(CommandHandler('addgroup', add_group))
    bot_app.add_handler(CommandHandler('removegroup', remove_group))
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