#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Svitlo Bot - Multi-City Power Outage Notification Bot
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
MAX_GROUPS_PER_USER = 6
DB_PATH = '/data/users.db'

# City configurations
CITIES = {
    'lviv': {
        'name': 'Львівська область',
        'groups': [f"{i}.{j}" for i in range(1, 7) for j in range(1, 3)],  # 1.1, 1.2, 2.1, 2.2, ..., 6.2
        'emoji': '🦁'
    },
    'ivano-frankivsk': {
        'name': 'Івано-Франківська область',
        'groups': [f"{i}.{j}" for i in range(1, 7) for j in range(1, 3)],  # 1.1, 1.2, 2.1, 2.2, ..., 6.2
        'emoji': '🏔'
    }
}

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

bot_app = None
bot_loop = None
update_queue = Queue()

# =============================================================================
# KEYBOARDS
# =============================================================================

REPLY_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("📋 Графік"), KeyboardButton("ℹ️ Мої групи")],
    [KeyboardButton("➕ Додати групу"), KeyboardButton("➖ Видалити групу")],
    [KeyboardButton("🏙 Області")]
], resize_keyboard=True)

def get_inline_keyboard(has_groups=True):
    if has_groups:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Графік", callback_data="schedule"),
             InlineKeyboardButton("ℹ️ Мої групи", callback_data="mygroups")],
            [InlineKeyboardButton("➕ Додати групу", callback_data="addgroup"),
             InlineKeyboardButton("➖ Видалити групу", callback_data="removegroup")],
            [InlineKeyboardButton("🏙 Змінити місто", callback_data="changecity")]
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🏙 Обрати місто", callback_data="selectcity")]
        ])

def get_city_keyboard():
    buttons = []
    for city_id, city_info in CITIES.items():
        buttons.append([InlineKeyboardButton(
            f"{city_info['emoji']} {city_info['name']}", 
            callback_data=f"city_{city_id}"
        )])
    return InlineKeyboardMarkup(buttons)

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
        columns = [row[1] for row in c.fetchall()]
        
        if 'group_number' in columns or 'group' in columns:
            logger.info("Migrating from single-group to multi-city schema...")
            c.execute('ALTER TABLE users RENAME TO users_old')
            
            c.execute('''CREATE TABLE users (
                chat_id INTEGER PRIMARY KEY,
                city TEXT DEFAULT 'lviv',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
            c.execute('''CREATE TABLE user_groups (
                chat_id INTEGER,
                city TEXT,
                group_number TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, city, group_number),
                FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
            )''')
            
            group_col = 'group_number' if 'group_number' in columns else 'group'
            c.execute(f'SELECT chat_id, {group_col}, created_at FROM users_old')
            old_users = c.fetchall()
            
            for chat_id, group_num, created_at in old_users:
                c.execute('INSERT OR IGNORE INTO users (chat_id, city, created_at) VALUES (?, ?, ?)', 
                         (chat_id, 'lviv', created_at))
                if group_num:
                    c.execute('INSERT OR IGNORE INTO user_groups (chat_id, city, group_number) VALUES (?, ?, ?)',
                             (chat_id, 'lviv', group_num))
            
            c.execute('DROP TABLE users_old')
            logger.info(f"Migration complete: {len(old_users)} users migrated to Lviv")
        
        elif 'city' not in columns:
            logger.info("Adding city support to multi-group schema...")
            c.execute('ALTER TABLE users ADD COLUMN city TEXT DEFAULT "lviv"')
            
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_groups'")
            if c.fetchone():
                c.execute("PRAGMA table_info(user_groups)")
                ug_columns = [row[1] for row in c.fetchall()]
                
                if 'city' not in ug_columns:
                    c.execute('ALTER TABLE user_groups RENAME TO user_groups_old')
                    c.execute('''CREATE TABLE user_groups (
                        chat_id INTEGER,
                        city TEXT,
                        group_number TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (chat_id, city, group_number),
                        FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
                    )''')
                    
                    c.execute('SELECT chat_id, group_number, created_at FROM user_groups_old')
                    old_groups = c.fetchall()
                    for chat_id, group_num, created_at in old_groups:
                        c.execute('INSERT INTO user_groups (chat_id, city, group_number, created_at) VALUES (?, ?, ?, ?)',
                                 (chat_id, 'lviv', group_num, created_at))
                    
                    c.execute('DROP TABLE user_groups_old')
                    logger.info(f"Migrated {len(old_groups)} group subscriptions")
    else:
        c.execute('''CREATE TABLE users (
            chat_id INTEGER PRIMARY KEY,
            city TEXT DEFAULT 'lviv',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE user_groups (
            chat_id INTEGER,
            city TEXT,
            group_number TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, city, group_number),
            FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
        )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS schedules (
        city TEXT,
        group_number TEXT,
        today_schedule TEXT,
        tomorrow_schedule TEXT,
        previous_today TEXT,
        previous_tomorrow TEXT,
        schedule_hash TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (city, group_number)
    )''')
    
    conn.commit()
    conn.close()

def db_execute(query, params=(), fetch_one=False, fetch_all=False):
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

def get_user_city(chat_id):
    result = db_execute('SELECT city FROM users WHERE chat_id = ?', (chat_id,), fetch_one=True)
    return result[0] if result else None

def set_user_city(chat_id, city):
    db_execute('INSERT OR IGNORE INTO users (chat_id) VALUES (?)', (chat_id,))
    db_execute('UPDATE users SET city = ? WHERE chat_id = ?', (city, chat_id))

def get_user_groups(chat_id, city=None):
    if city is None:
        city = get_user_city(chat_id) or 'lviv'
    
    rows = db_execute(
        'SELECT group_number FROM user_groups WHERE chat_id = ? AND city = ? ORDER BY group_number', 
        (chat_id, city), fetch_all=True
    )
    return [row[0] for row in rows] if rows else []

def add_user_group(chat_id, city, group):
    try:
        db_execute('INSERT OR IGNORE INTO users (chat_id, city) VALUES (?, ?)', (chat_id, city))
        
        current_groups = get_user_groups(chat_id, city)
        if len(current_groups) >= MAX_GROUPS_PER_USER:
            return False, f"Максимум {MAX_GROUPS_PER_USER} груп"
        
        db_execute('INSERT OR IGNORE INTO user_groups (chat_id, city, group_number) VALUES (?, ?, ?)', 
                  (chat_id, city, group))
        return True, None
    except Exception as e:
        logger.error(f"Error adding group: {e}")
        return False, "Помилка при додаванні групи"

def remove_user_group(chat_id, city, group):
    try:
        db_execute('DELETE FROM user_groups WHERE chat_id = ? AND city = ? AND group_number = ?', 
                  (chat_id, city, group))
        return True
    except:
        return False

def get_all_users():
    rows = db_execute('''
        SELECT u.chat_id, u.city, GROUP_CONCAT(ug.group_number, ',') as groups
        FROM users u
        LEFT JOIN user_groups ug ON u.chat_id = ug.chat_id AND u.city = ug.city
        GROUP BY u.chat_id, u.city
    ''', fetch_all=True)
    
    result = []
    for chat_id, city, groups_str in rows:
        groups = groups_str.split(',') if groups_str else []
        result.append({"chat_id": chat_id, "city": city or 'lviv', "groups": groups})
    return result

def get_schedule(city, group_number):
    result = db_execute(
        'SELECT today_schedule, tomorrow_schedule, updated_at FROM schedules WHERE city = ? AND group_number = ?', 
        (city, group_number), fetch_one=True
    )
    return {'today': result[0], 'tomorrow': result[1], 'updated_at': result[2]} if result else None

def save_schedule(city, group_number, today, tomorrow, schedule_hash):
    curr = db_execute(
        'SELECT today_schedule, tomorrow_schedule FROM schedules WHERE city = ? AND group_number = ?', 
        (city, group_number), fetch_one=True
    )
    prev_today, prev_tomorrow = (curr[0], curr[1]) if curr else (None, None)
    
    db_execute('''INSERT INTO schedules (city, group_number, today_schedule, tomorrow_schedule, previous_today, previous_tomorrow, schedule_hash, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(city, group_number) DO UPDATE SET
            previous_today = schedules.today_schedule,
            previous_tomorrow = schedules.tomorrow_schedule,
            today_schedule = excluded.today_schedule,
            tomorrow_schedule = excluded.tomorrow_schedule,
            schedule_hash = excluded.schedule_hash,
            updated_at = CURRENT_TIMESTAMP
    ''', (city, group_number, today, tomorrow, prev_today, prev_tomorrow, schedule_hash))

def get_schedule_hash(city, group_number):
    result = db_execute(
        'SELECT schedule_hash FROM schedules WHERE city = ? AND group_number = ?', 
        (city, group_number), fetch_one=True
    )
    return result[0] if result else None

def column_exists(table, column):
    rows = db_execute(f"PRAGMA table_info({table})", fetch_all=True)
    return any(r[1] == column for r in rows)

def migrate_to_city_support():
    logger.info("Running DB migration: city support")

    # ---- USERS TABLE ----
    if not column_exists("users", "city"):
        logger.info("Adding city column to users")
        db_execute("ALTER TABLE users ADD COLUMN city TEXT DEFAULT 'lviv'")

    # Backfill users.city
    db_execute("""
        UPDATE users
        SET city = 'lviv'
        WHERE city IS NULL OR city = ''
    """)

    # ---- USER_GROUPS TABLE ----
    if column_exists("user_groups", "group_number") and not column_exists("user_groups", "city"):
        logger.info("Migrating user_groups to include city")

        db_execute("ALTER TABLE user_groups RENAME TO user_groups_old")

        db_execute("""
            CREATE TABLE user_groups (
                chat_id INTEGER,
                city TEXT,
                group_number TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, city, group_number),
                FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
            )
        """)

        db_execute("""
            INSERT INTO user_groups (chat_id, city, group_number, created_at)
            SELECT chat_id, 'lviv', group_number, created_at
            FROM user_groups_old
        """)

        db_execute("DROP TABLE user_groups_old")

    # ---- SCHEDULES TABLE ----
    if not column_exists("schedules", "city"):
        logger.info("Adding city column to schedules")
        db_execute("ALTER TABLE schedules ADD COLUMN city TEXT DEFAULT 'lviv'")

    # Backfill schedules.city
    db_execute("""
        UPDATE schedules
        SET city = 'lviv'
        WHERE city IS NULL OR city = ''
    """)

    logger.info("DB migration complete")

# =============================================================================
# SCHEDULE PARSING
# =============================================================================

def parse_schedule_entries(group_data):
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
    if not schedule_text:
        return {'on': [], 'off': []}
    
    off_ranges = re.findall(r'з (\d{1,2}:\d{2}) до (\d{1,2}:\d{2})', schedule_text)
    to_min = lambda t: int(t.split(':')[0]) * 60 + int(t.split(':')[1])
    off_intervals = sorted([(to_min(s), to_min(e)) for s, e in off_ranges])
    
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
    return "24:00" if mins >= 1440 else f"{mins // 60:02d}:{mins % 60:02d}"

def fmt_hours(hours):
    return f"{hours:.1f}"

def esc(text):
    for c in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        text = text.replace(c, f'\\{c}')
    return text

# =============================================================================
# MESSAGE FORMATTING
# =============================================================================

def format_schedule_display(schedule_text):
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
        lines.append(f"\n⏱ *Загалом вимкнено:* {fmt_hours(total/60)} годин")
    else:
        lines.append("  • немає даних")

    return "\n".join(lines)

def format_notification(city, group, curr_today, curr_tomorrow, prev_today=None, prev_tomorrow=None):
    city_name = CITIES.get(city, {}).get('name', city)
    city_emoji = CITIES.get(city, {}).get('emoji', '🏙')
    
    msg = f"⚡️ *Оновлення графіку вимкнень\\!*\n\n{esc(city_emoji)} Область: *{esc(city_name)}*\n📍 Група: *{esc(group)}*\n\n"
    
    curr = extract_intervals(curr_today)
    prev = extract_intervals(prev_today) if prev_today else {'on': [], 'off': []}
    
    off_removed = [iv for iv in prev['off'] if iv not in curr['off']]
    off_added = [iv for iv in curr['off'] if iv not in prev['off']]
    on_removed = [iv for iv in prev['on'] if iv not in curr['on']]
    on_added = [iv for iv in curr['on'] if iv not in prev['on']]
    
    if off_removed or off_added or on_removed or on_added:
        msg += "📊 *ЩО ЗМІНИЛОСЬ:*\n\n"
        
        if off_removed:
            msg += "✅ *Світло з\\'явилось:*\n"
            for s, e in off_removed:
                msg += f"  • {esc(fmt_time(s))} — {esc(fmt_time(e))}\n"
            msg += "\n"
        
        if off_added:
            msg += "⚠️ *Нові вимкнення:*\n"
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
        msg += f"\n⏱ *Загалом вимкнено:* {esc(fmt_hours(total/60))} годин\n"
    else:
        msg += "  • немає даних\n"
    
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
            msg += f"\n⏱ *Загалом вимкнено:* {esc(fmt_hours(total_tm/60))} годин\n"
        else:
            msg += "  • немає даних\n"
    
    msg += "\n_Графік може змінюватися протягом дня_"
    return msg

# =============================================================================
# BACKGROUND CHECKER
# =============================================================================

async def broadcast_message(message, parse_mode=None):
    users_data = db_execute('SELECT DISTINCT chat_id FROM users', fetch_all=True)
    users = [row[0] for row in users_data]
    success_count = 0
    failed_count = 0
    
    logger.info(f"Starting broadcast to {len(users)} users...")
    
    for chat_id in users:
        try:
            await bot_app.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode=parse_mode
            )
            success_count += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Broadcast failed for user {chat_id}: {e}")
            failed_count += 1
    
    logger.info(f"Broadcast complete: {success_count} sent, {failed_count} failed")
    return success_count, failed_count

async def check_and_notify():
    try:
        for city_id in CITIES.keys():
            logger.info(f"Checking schedules for {city_id}...")
            
            scraper = ScheduleScraper(city=city_id)
            json_content = scraper.fetch_schedule()
            if not json_content:
                logger.warning(f"Failed to fetch schedule for {city_id}")
                continue
            
            schedule = scraper.parse_schedule(json_content)
            if not schedule:
                logger.warning(f"Failed to parse schedule for {city_id}")
                continue
            
            groups_data = schedule.get('groups', {})
            if not groups_data:
                logger.warning(f"No groups found for {city_id}")
                continue
            
            logger.info(f"Processing {len(groups_data)} groups from {city_id}")
            
            changed_groups = []
            saved_count = 0
            for group, data in groups_data.items():
                today, tomorrow = parse_schedule_entries(data)
                if not today:
                    logger.warning(f"No schedule found for {city_id} group {group}")
                    continue
                
                new_hash = hashlib.sha256(f"{today}|{tomorrow or ''}".encode()).hexdigest()
                old_hash = get_schedule_hash(city_id, group)
                
                save_schedule(city_id, group, today or '', tomorrow or '', new_hash)
                saved_count += 1
                logger.info(f"Saved schedule for {city_id} group {group}")
                
                if new_hash != old_hash and old_hash is not None:
                    changed_groups.append(group)
            
            logger.info(f"Saved {saved_count} {city_id} groups, {len(changed_groups)} changed")
            
            if not changed_groups:
                continue
            
            for user in get_all_users():
                if user['city'] != city_id:
                    continue
                    
                user_changed_groups = [g for g in user['groups'] if g in changed_groups]
                
                for group in user_changed_groups:
                    try:
                        result = db_execute(
                            'SELECT today_schedule, tomorrow_schedule, previous_today, previous_tomorrow FROM schedules WHERE city = ? AND group_number = ?',
                            (city_id, group), fetch_one=True
                        )
                        if result:
                            msg = format_notification(city_id, group, result[0], result[1], result[2], result[3])
                            await bot_app.bot.send_message(chat_id=user['chat_id'], text=msg, parse_mode='MarkdownV2')
                            await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.error(f"Notify error {user['chat_id']}: {e}")
                        
    except Exception as e:
        logger.error(f"Checker error: {e}", exc_info=True)

async def checker_loop():
    logger.info("Fetching initial schedules...")
    await check_and_notify()
    logger.info("Initial fetch complete")
    
    while True:
        await asyncio.sleep(300)
        await check_and_notify()

# =============================================================================
# TELEGRAM HANDLERS
# =============================================================================

async def safe_edit(query, text, parse_mode=None, reply_markup=None):
    try:
        await query.edit_message_text(text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

async def start(update, context):
    chat_id = update.effective_chat.id
    city = get_user_city(chat_id)
    
    if not city:
        await update.message.reply_text(
            "Вітаю! 👋\n\nЯ допоможу відстежувати графік вимкнень світла.\n\n🏙 Спочатку оберіть ваше місто:",
            reply_markup=get_city_keyboard()
        )
        return
    
    groups = get_user_groups(chat_id, city)
    city_name = CITIES[city]['name']
    
    if groups:
        await update.message.reply_text(
            f"Вітаю! 👋\n\n🏙 Область: *{city_name}*\nВи підписані на {len(groups)} груп(у/и).\n\nОберіть дію:",
            parse_mode='Markdown',
            reply_markup=REPLY_KEYBOARD
        )
    else:
        await update.message.reply_text(
            f"Вітаю! 👋\n\n🏙 Область: *{city_name}*\n\nДодайте групу для початку:",
            parse_mode='Markdown',
            reply_markup=REPLY_KEYBOARD
        )

async def show_schedule(update, context):
    chat_id = update.effective_chat.id
    city = get_user_city(chat_id)
    
    if not city:
        await update.message.reply_text("❌ Спочатку оберіть місто", reply_markup=get_city_keyboard())
        return
    
    groups = get_user_groups(chat_id, city)
    
    if not groups:
        await update.message.reply_text("❌ Спочатку додайте групу", reply_markup=REPLY_KEYBOARD)
        return
    
    city_name = CITIES[city]['name']
    city_emoji = CITIES[city]['emoji']
    
    for group in groups:
        schedule = get_schedule(city, group)
        if not schedule:
            await update.message.reply_text(
                f"ℹ️ Завантаження графіку для {city_name}, група {group}...", 
                reply_markup=REPLY_KEYBOARD
            )
            continue
        
        msg = f"📋 *Графік вимкнень*\n\n{city_emoji} Область: *{city_name}*\n📍 Група: *{group}*\n\n"
        if schedule['today']:
            msg += "📅 *Сьогодні*\n" + format_schedule_display(schedule['today']) + "\n\n"
        if schedule['tomorrow']:
            msg += "📅 *Завтра*\n" + format_schedule_display(schedule['tomorrow']) + "\n\n"
        if schedule['updated_at']:
            msg += f"🕐 Оновлено: _{schedule['updated_at']}_\n"
        msg += "ℹ️ _Графік може змінюватися протягом дня_"
        
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=REPLY_KEYBOARD)
        if len(groups) > 1:
            await asyncio.sleep(0.3)

async def show_groups(update, context):
    chat_id = update.effective_chat.id
    city = get_user_city(chat_id)
    
    if not city:
        await update.message.reply_text("❌ Спочатку оберіть місто", reply_markup=get_city_keyboard())
        return
    
    groups = get_user_groups(chat_id, city)
    city_name = CITIES[city]['name']
    city_emoji = CITIES[city]['emoji']
    
    if groups:
        groups_str = ", ".join(groups)
        text = f"{city_emoji} *Область:* {city_name}\n📍 *Ваші групи:* {groups_str}\n\n_Ви можете мати до {MAX_GROUPS_PER_USER} груп_"
    else:
        text = f"{city_emoji} *Область:* {city_name}\n❌ Групи не обрані"
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=REPLY_KEYBOARD)

async def show_cities(update, context):
    chat_id = update.effective_chat.id
    current_city = get_user_city(chat_id)
    
    text = "🏙 *Оберіть місто:*\n\n"
    if current_city:
        city_name = CITIES[current_city]['name']
        text += f"_Поточне місто: {city_name}_\n\n"
        text += "⚠️ _При зміні міста ваші поточні підписки залишаться, але графіки будуть показуватися для нового міста_"
    
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=get_city_keyboard())

async def add_group(update, context):
    chat_id = update.effective_chat.id
    city = get_user_city(chat_id)
    
    if not city:
        await update.message.reply_text("❌ Спочатку оберіть місто", reply_markup=get_city_keyboard())
        return
    
    user_count = db_execute('SELECT COUNT(*) FROM users', fetch_one=True)[0]
    current_groups = get_user_groups(chat_id, city)
    
    if not current_groups and user_count >= MAX_USERS:
        await update.message.reply_text("❌ Ліміт користувачів", reply_markup=REPLY_KEYBOARD)
        return
    
    if len(current_groups) >= MAX_GROUPS_PER_USER:
        await update.message.reply_text(
            f"❌ Ви вже підписані на максимальну кількість груп ({MAX_GROUPS_PER_USER})",
            reply_markup=REPLY_KEYBOARD
        )
        return
    
    available = [g for g in CITIES[city]['groups'] if g not in current_groups]
    kb = [[InlineKeyboardButton(g, callback_data=f"add_{g}") for g in available[i:i+3]] 
          for i in range(0, len(available), 3)]
    
    city_name = CITIES[city]['name']
    await update.message.reply_text(
        f"Оберіть групу для додавання ({city_name}):", 
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def remove_group(update, context):
    chat_id = update.effective_chat.id
    city = get_user_city(chat_id)
    
    if not city:
        await update.message.reply_text("❌ Спочатку оберіть місто", reply_markup=get_city_keyboard())
        return
    
    groups = get_user_groups(chat_id, city)
    
    if not groups:
        await update.message.reply_text("❌ У вас немає груп", reply_markup=REPLY_KEYBOARD)
        return
    
    kb = [[InlineKeyboardButton(g, callback_data=f"rem_{g}") for g in groups[i:i+3]] 
          for i in range(0, len(groups), 3)]
    
    city_name = CITIES[city]['name']
    await update.message.reply_text(
        f"Оберіть групу для видалення ({city_name}):", 
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def handle_callback(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.from_user.id
    
    if data.startswith("city_"):
        city_id = data[5:]
        if city_id not in CITIES:
            await safe_edit(query, "❌ Невідоме місто", reply_markup=get_city_keyboard())
            return
        
        set_user_city(chat_id, city_id)
        city_name = CITIES[city_id]['name']
        city_emoji = CITIES[city_id]['emoji']
        
        groups = get_user_groups(chat_id, city_id)
        if groups:
            await safe_edit(
                query,
                f"✅ Область змінено на {city_emoji} *{city_name}*\n\nВи підписані на {len(groups)} груп(у/и)\n\nОберіть дію:",
                parse_mode='Markdown',
                reply_markup=get_inline_keyboard(True)
            )
        else:
            await safe_edit(
                query,
                f"✅ Область обрано: {city_emoji} *{city_name}*\n\nТепер додайте групу:",
                parse_mode='Markdown',
                reply_markup=get_inline_keyboard(False)
            )
        return
    
    if data.startswith("add_"):
        city = get_user_city(chat_id)
        if not city:
            await safe_edit(query, "❌ Спочатку оберіть місто", reply_markup=get_city_keyboard())
            return
        
        group = data[4:]
        success, error = add_user_group(chat_id, city, group)
        
        if success:
            schedule = get_schedule(city, group)
            groups = get_user_groups(chat_id, city)
            city_name = CITIES[city]['name']
            city_emoji = CITIES[city]['emoji']
            
            if schedule and schedule['today']:
                msg = f"✅ Групу {group} додано!\n\n"
                msg += f"📋 *Графік вимкнень*\n\n{city_emoji} Область: *{city_name}*\n📍 Група: *{group}*\n\n"
                if schedule['today']:
                    msg += "📅 *Сьогодні*\n" + format_schedule_display(schedule['today']) + "\n\n"
                if schedule['tomorrow']:
                    msg += "📅 *Завтра*\n" + format_schedule_display(schedule['tomorrow']) + "\n\n"
                if schedule['updated_at']:
                    msg += f"🕐 Оновлено: _{schedule['updated_at']}_\n"
                msg += f"\n_Всього груп: {len(groups)}/{MAX_GROUPS_PER_USER}_"
                await safe_edit(query, msg, parse_mode='Markdown', reply_markup=get_inline_keyboard(True))
            else:
                await safe_edit(
                    query, 
                    f"✅ Групу {group} додано!\n\nℹ️ Графік ще не завантажено.\n\n_Всього груп: {len(groups)}/{MAX_GROUPS_PER_USER}_", 
                    reply_markup=get_inline_keyboard(True)
                )
        else:
            await safe_edit(query, f"❌ {error}", reply_markup=get_inline_keyboard(bool(get_user_groups(chat_id, city))))
        return
    
    if data.startswith("rem_"):
        city = get_user_city(chat_id)
        if not city:
            await safe_edit(query, "❌ Спочатку оберіть місто", reply_markup=get_city_keyboard())
            return
        
        group = data[4:]
        if remove_user_group(chat_id, city, group):
            groups = get_user_groups(chat_id, city)
            await safe_edit(
                query, 
                f"✅ Групу {group} видалено\n\n Залишилось груп: {len(groups)}/{MAX_GROUPS_PER_USER} ", 
                reply_markup=get_inline_keyboard(bool(groups))
            )
        return
    
    if data == "schedule":
        city = get_user_city(chat_id)
        if not city:
            await safe_edit(query, "❌ Спочатку оберіть місто", reply_markup=get_city_keyboard())
            return
        
        groups = get_user_groups(chat_id, city)
        if not groups:
            await safe_edit(query, "❌ Спочатку додайте групу", reply_markup=get_inline_keyboard(False))
            return
        
        city_name = CITIES[city]['name']
        city_emoji = CITIES[city]['emoji']
        first_group = groups[0]
        schedule = get_schedule(city, first_group)
        
        if schedule:
            msg = f"📋 *Графік вимкнень*\n\n{city_emoji} Область: *{city_name}*\n📍 Група: *{first_group}*\n\n"
            if schedule['today']:
                msg += "📅 *Сьогодні*\n" + format_schedule_display(schedule['today']) + "\n\n"
            if schedule['tomorrow']:
                msg += "📅 *Завтра*\n" + format_schedule_display(schedule['tomorrow']) + "\n\n"
            if schedule['updated_at']:
                msg += f"🕐 Оновлено: _{schedule['updated_at']}_\n"
            msg += "ℹ️ _Графік може змінюватися протягом дня_"
            
            await safe_edit(query, msg, parse_mode='Markdown', reply_markup=get_inline_keyboard(True))
        
        for group in groups[1:]:
            schedule = get_schedule(city, group)
            if schedule:
                msg = f"📋 *Графік вимкнень*\n\n{city_emoji} Область: *{city_name}*\n📍 Група: *{group}*\n\n"
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
        city = get_user_city(chat_id)
        if not city:
            await safe_edit(query, "❌ Спочатку оберіть місто", reply_markup=get_city_keyboard())
            return
        
        groups = get_user_groups(chat_id, city)
        city_name = CITIES[city]['name']
        city_emoji = CITIES[city]['emoji']
        
        if groups:
            groups_str = ", ".join(groups)
            text = f"{city_emoji} *Область:* {city_name}\n📍 *Ваші групи:* {groups_str}\n\n_Ви можете мати до {MAX_GROUPS_PER_USER} груп_\n\nОберіть дію:"
        else:
            text = f"{city_emoji} *Область:* {city_name}\n❌ Групи не обрані\n\nОберіть дію:"
        await safe_edit(query, text, parse_mode='Markdown', reply_markup=get_inline_keyboard(bool(groups)))
    
    elif data == "addgroup":
        city = get_user_city(chat_id)
        if not city:
            await safe_edit(query, "❌ Спочатку оберіть місто", reply_markup=get_city_keyboard())
            return
        
        current_groups = get_user_groups(chat_id, city)
        
        if len(current_groups) >= MAX_GROUPS_PER_USER:
            await safe_edit(
                query, 
                f"❌ Ви вже підписані на максимальну кількість груп ({MAX_GROUPS_PER_USER})",
                reply_markup=get_inline_keyboard(True)
            )
            return
        
        available = [g for g in CITIES[city]['groups'] if g not in current_groups]
        kb = [[InlineKeyboardButton(g, callback_data=f"add_{g}") for g in available[i:i+3]] 
              for i in range(0, len(available), 3)]
        
        city_name = CITIES[city]['name']
        await safe_edit(query, f"Оберіть групу для додавання ({city_name}):", reply_markup=InlineKeyboardMarkup(kb))
    
    elif data == "removegroup":
        city = get_user_city(chat_id)
        if not city:
            await safe_edit(query, "❌ Спочатку оберіть місто", reply_markup=get_city_keyboard())
            return
        
        groups = get_user_groups(chat_id, city)
        
        if not groups:
            await safe_edit(query, "❌ У вас немає груп", reply_markup=get_inline_keyboard(False))
            return
        
        kb = [[InlineKeyboardButton(g, callback_data=f"rem_{g}") for g in groups[i:i+3]] 
              for i in range(0, len(groups), 3)]
        
        city_name = CITIES[city]['name']
        await safe_edit(query, f"Оберіть групу для видалення ({city_name}):", reply_markup=InlineKeyboardMarkup(kb))
    
    elif data in ["changecity", "selectcity"]:
        current_city = get_user_city(chat_id)
        
        text = "🏙 *Оберіть місто:*\n\n"
        if current_city:
            city_name = CITIES[current_city]['name']
            text += f"_Поточне місто: {city_name}_\n\n"
            text += "⚠️ _При зміні міста ваші підписки у старому місті залишаться_"
        
        await safe_edit(query, text, parse_mode='Markdown', reply_markup=get_city_keyboard())

async def handle_text(update, context):
    text = update.message.text
    if text == "📋 Графік":
        await show_schedule(update, context)
    elif text == "ℹ️ Мої групи":
        await show_groups(update, context)
    elif text == "➕ Додати групу":
        await add_group(update, context)
    elif text == "➖ Видалити групу":
        await remove_group(update, context)
    elif text == "🏙 Області":
        await show_cities(update, context)

async def stop(update, context):
    db_execute('DELETE FROM users WHERE chat_id = ?', (update.effective_chat.id,))
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
    
    city_stats = {}
    for city_id in CITIES.keys():
        city_users = db_execute(
            'SELECT COUNT(DISTINCT chat_id) FROM user_groups WHERE city = ?', 
            (city_id,), fetch_one=True
        )[0]
        city_stats[city_id] = city_users
    
    return jsonify({
        'status': 'healthy', 
        'users': count, 
        'total_subscriptions': total_groups,
        'by_city': city_stats
    })

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

@flask_app.route('/api/broadcast', methods=['POST'])
def api_broadcast():
    auth = request.headers.get('Authorization', '').replace('Bearer ', '')
    if auth != API_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.get_json()
    if not data or 'message' not in data:
        return jsonify({'error': 'Missing message field'}), 400
    
    message = data['message']
    parse_mode = data.get('parse_mode', 'Markdown')
    
    if parse_mode not in ['Markdown', 'MarkdownV2', 'HTML', None]:
        return jsonify({'error': 'Invalid parse_mode. Use: Markdown, MarkdownV2, HTML, or null'}), 400
    
    if bot_loop is None:
        return jsonify({'error': 'Bot not ready'}), 503
    
    asyncio.run_coroutine_threadsafe(
        broadcast_message(message, parse_mode),
        bot_loop
    )
    
    user_count = db_execute('SELECT COUNT(*) FROM users', fetch_one=True)[0]
    
    return jsonify({
        'status': 'queued',
        'message': 'Broadcast queued successfully',
        'target_users': user_count
    })

# =============================================================================
# APP SETUP
# =============================================================================

async def process_updates():
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
    global bot_app
    bot_app = Application.builder().token(BOT_TOKEN).build()
    
    bot_app.add_handler(CommandHandler('start', start))
    bot_app.add_handler(CommandHandler('schedule', show_schedule))
    bot_app.add_handler(CommandHandler('mygroups', show_groups))
    bot_app.add_handler(CommandHandler('addgroup', add_group))
    bot_app.add_handler(CommandHandler('removegroup', remove_group))
    bot_app.add_handler(CommandHandler('cities', show_cities))
    bot_app.add_handler(CommandHandler('stop', stop))
    
    bot_app.add_handler(CallbackQueryHandler(handle_callback))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")

def run_bot():
    global bot_loop
    loop = asyncio.new_event_loop()
    bot_loop = loop
    asyncio.set_event_loop(loop)
    loop.run_until_complete(setup())
    loop.create_task(process_updates())
    loop.create_task(checker_loop())
    loop.run_forever()

if __name__ == '__main__':
    init_db()
    migrate_to_city_support()
    Thread(target=run_bot, daemon=True).start()
    flask_app.run(host='0.0.0.0', port=PORT)