#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Svitlo Bot - Multi-City Power Outage Notification Bot
"""

import os
import logging
import sqlite3
import re
import hashlib
import hmac
import time
from io import BytesIO
from html import escape as html_escape
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Lock, Thread
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.error import BadRequest, NetworkError
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from flask import Flask, request, jsonify
from waitress import serve

import sys
sys.path.append(os.path.dirname(__file__))
from scraper import ScheduleScraper
from schedule_card import render_schedule_png

# =============================================================================
# CONFIG
# =============================================================================

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
API_SECRET = os.getenv('API_SECRET')
WEBHOOK_SECRET = os.getenv('TELEGRAM_WEBHOOK_SECRET')
PORT = int(os.getenv('PORT', 8080))
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
LOCAL_MODE = os.getenv('LOCAL_MODE', '').lower() in ('1', 'true', 'yes')

MAX_USERS = 40
MAX_GROUPS_PER_USER = 6
DB_PATH = os.getenv('DB_PATH', '/data/users.db')
MAX_WEBHOOK_QUEUE = 256
MAX_BROADCAST_LENGTH = 4096
MIN_BROADCAST_INTERVAL_SECONDS = 10
KYIV_TIMEZONE = ZoneInfo('Europe/Kyiv')

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
update_queue = Queue(maxsize=MAX_WEBHOOK_QUEUE)
broadcast_lock = Lock()
last_broadcast_at = None


def redact_secrets(value):
    message = str(value)
    for secret in (BOT_TOKEN, API_SECRET, WEBHOOK_SECRET):
        if secret:
            message = message.replace(secret, '[REDACTED]')
    return message


def validate_config():
    if not BOT_TOKEN:
        raise RuntimeError('TELEGRAM_BOT_TOKEN must be configured')
    if LOCAL_MODE:
        return

    if not WEBHOOK_URL:
        raise RuntimeError('WEBHOOK_URL must be configured unless LOCAL_MODE=1')
    parsed_webhook_url = urlsplit(WEBHOOK_URL)
    if (parsed_webhook_url.scheme != 'https' or not parsed_webhook_url.netloc
            or parsed_webhook_url.username or parsed_webhook_url.password):
        raise RuntimeError('WEBHOOK_URL must be an https URL')
    if not WEBHOOK_SECRET or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', WEBHOOK_SECRET):
        raise RuntimeError('TELEGRAM_WEBHOOK_SECRET must be 32-256 letters, numbers, underscores, or hyphens')
    if WEBHOOK_SECRET == BOT_TOKEN or (API_SECRET and WEBHOOK_SECRET == API_SECRET):
        raise RuntimeError('Telegram webhook secret must be unique and separate from other secrets')


def api_auth_error():
    if not API_SECRET or len(API_SECRET) < 32:
        return jsonify({'error': 'API authentication is not securely configured'}), 503

    authorization = request.headers.get('Authorization', '')
    scheme, separator, credential = authorization.partition(' ')
    if (not separator or scheme.lower() != 'bearer' or not credential
            or credential != credential.strip() or any(char.isspace() for char in credential)):
        return jsonify({'error': 'Unauthorized'}), 401
    if not hmac.compare_digest(credential.encode('utf-8'), API_SECRET.encode('utf-8')):
        return jsonify({'error': 'Unauthorized'}), 401
    return None


def broadcast_is_rate_limited():
    global last_broadcast_at
    now = time.monotonic()
    with broadcast_lock:
        if (last_broadcast_at is not None
                and now - last_broadcast_at < MIN_BROADCAST_INTERVAL_SECONDS):
            return True
        last_broadcast_at = now
        return False

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
            [InlineKeyboardButton("🏙 Змінити область", callback_data="changecity")]
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🏙 Обрати область", callback_data="selectcity")]
        ])

def get_city_keyboard():
    buttons = []
    for city_id, city_info in CITIES.items():
        buttons.append([InlineKeyboardButton(
            f"{city_info['emoji']} {city_info['name']}", 
            callback_data=f"city_{city_id}"
        )])
    return InlineKeyboardMarkup(buttons)

def get_group_keyboard(city, current_groups=None):
    current_groups = current_groups or []
    available = [group for group in CITIES[city]['groups'] if group not in current_groups]
    buttons = [
        [InlineKeyboardButton(group, callback_data=f"add_{group}") for group in available[i:i + 3]]
        for i in range(0, len(available), 3)
    ]
    return InlineKeyboardMarkup(buttons) if buttons else None

# =============================================================================
# DATABASE
# =============================================================================

def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Users
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            city TEXT DEFAULT 'lviv',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Groups
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_groups (
            chat_id INTEGER,
            city TEXT,
            group_number TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, city, group_number),
            FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
        )
    ''')
    
    # Schedules
    c.execute('''
        CREATE TABLE IF NOT EXISTS schedules (
            city TEXT,
            group_number TEXT,
            today_schedule TEXT,
            tomorrow_schedule TEXT,
            previous_today TEXT,
            previous_tomorrow TEXT,
            schedule_hash TEXT,
            reference_date TEXT,  -- Date when this schedule was recorded (YYYY-MM-DD)
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (city, group_number)
        )
    ''')
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
        logger.error("Error adding group: %s", redact_secrets(e))
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

def save_schedule(city, group_number, today, tomorrow, schedule_hash, reference_date):
    curr = db_execute(
        'SELECT today_schedule, tomorrow_schedule FROM schedules WHERE city = ? AND group_number = ?', 
        (city, group_number), fetch_one=True
    )
    prev_today, prev_tomorrow = (curr[0], curr[1]) if curr else (None, None)
    
    db_execute('''INSERT INTO schedules (city, group_number, today_schedule, tomorrow_schedule, previous_today, previous_tomorrow, schedule_hash, reference_date, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(city, group_number) DO UPDATE SET
            previous_today = schedules.today_schedule,
            previous_tomorrow = schedules.tomorrow_schedule,
            today_schedule = excluded.today_schedule,
            tomorrow_schedule = excluded.tomorrow_schedule,
            schedule_hash = excluded.schedule_hash,
            reference_date = excluded.reference_date,
            updated_at = CURRENT_TIMESTAMP
    ''', (city, group_number, today, tomorrow, prev_today, prev_tomorrow, schedule_hash, reference_date))

def get_schedule_hash(city, group_number):
    result = db_execute(
        'SELECT schedule_hash FROM schedules WHERE city = ? AND group_number = ?', 
        (city, group_number), fetch_one=True
    )
    return result[0] if result else None

def get_schedule_with_date(city, group_number):
    """Get current schedule including reference date for comparison."""
    result = db_execute(
        'SELECT today_schedule, tomorrow_schedule, previous_today, previous_tomorrow, schedule_hash, reference_date FROM schedules WHERE city = ? AND group_number = ?', 
        (city, group_number), fetch_one=True
    )
    if not result:
        return None
    return {
        'today': result[0],
        'tomorrow': result[1],
        'prev_today': result[2],
        'prev_tomorrow': result[3],
        'hash': result[4],
        'reference_date': result[5]
    }

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

def schedule_status(schedule_text, minute_of_day=None):
    """Return the current planned power state and the next scheduled change."""
    if not schedule_text:
        return None, None
    if schedule_text == "Відключень не заплановано":
        return "on", 1440
    now = datetime.now(KYIV_TIMEZONE)
    minute_of_day = minute_of_day if minute_of_day is not None else now.hour * 60 + now.minute
    intervals = extract_intervals(schedule_text)
    for state in ("off", "on"):
        for start, end in intervals[state]:
            if start <= minute_of_day < end:
                return state, end
    return None, None

def format_timeline(schedule_text):
    """Compact visual schedule that renders in every Telegram client."""
    if not schedule_text:
        return "▫️ Графік відсутній"
    if schedule_text == "Відключень не заплановано":
        return "🟩" * 12 + "\n🟩 є світло · 🟥 вимкнення · 00—24"
    intervals = extract_intervals(schedule_text)
    blocks = []
    for block in range(12):
        midpoint = block * 120 + 60
        is_off = any(start <= midpoint < end for start, end in intervals["off"])
        blocks.append("🟥" if is_off else "🟩")
    return "".join(blocks) + "\n🟩 є світло · 🟥 вимкнення · 00—24"

def schedule_caption(city, group, today_text, tomorrow_text=None, updated_at=None,
                    notify=False, test=False, test_label=None):
    """Lead with exact planned outage windows, not the current power state."""
    def ranges_block(schedule_text):
        intervals = extract_intervals(schedule_text)["off"]
        if intervals:
            return "\n".join(f"• <b>{fmt_time(start)} — {fmt_time(end)}</b>" for start, end in intervals)
        if schedule_text == "Відключень не заплановано":
            return "🟢 Відключень за графіком немає"
        return "⚪️ Графік ще не опублікований"

    city_name = html_escape(CITIES.get(city, {}).get("name", city))
    group_label = html_escape(str(group))
    if test:
        label = f" · {html_escape(test_label)}" if test_label else ""
        heading = f"🧪 <b>ТЕСТОВЕ СПОВІЩЕННЯ{label}</b>\n⚡️ <b>Імітація оновлення графіка</b>"
    elif notify:
        heading = "⚡️ <b>ГРАФІК ОНОВИВСЯ</b>"
    else:
        heading = "⚡️ <b>ГРАФІК ВІДКЛЮЧЕНЬ</b>"
    caption = (
        f"{heading}\n"
        f"📍 <b>{city_name}</b> · група <b>{group_label}</b>\n"
        f"\n🔴 <b>КОЛИ НЕ БУДЕ СВІТЛА · СЬОГОДНІ</b>\n"
        f"{ranges_block(today_text)}"
    )
    caption += f"\n\n🗓 <b>ЗАВТРА</b>\n{ranges_block(tomorrow_text)}"
    if updated_at:
        caption += f"\n🕒 <i>Оновлено о {html_escape(updated_at)}</i>"
    return caption


async def send_schedule_card(bot, chat_id, city, group, today_text, tomorrow_text=None,
                             updated_at=None, notify=False, test=False, test_label=None):
    """Render the SVG schedule card to PNG and send it with a readable caption."""
    city_name = CITIES.get(city, {}).get('name', city)
    caption = schedule_caption(city, group, today_text, tomorrow_text, notify=notify,
                               test=test, test_label=test_label)
    try:
        now = datetime.now(KYIV_TIMEZONE)
        updated_label = None
        if updated_at:
            try:
                parsed_update = datetime.fromisoformat(updated_at)
                if parsed_update.tzinfo is None:
                    parsed_update = parsed_update.replace(tzinfo=ZoneInfo("UTC"))
                updated_label = parsed_update.astimezone(KYIV_TIMEZONE).strftime("%H:%M")
            except (TypeError, ValueError):
                updated_label = str(updated_at)[-8:-3]
        updated_label = updated_label or now.strftime("%H:%M")
        caption = schedule_caption(
            city, group, today_text, tomorrow_text, updated_label,
            notify=notify, test=test, test_label=test_label,
        )
        png = render_schedule_png(
            city_name,
            group,
            today_text,
            tomorrow_text,
            updated_label,
            now.hour * 60 + now.minute,
        )
        await bot.send_photo(
            chat_id=chat_id,
            photo=BytesIO(png),
            caption=caption,
            parse_mode="HTML",
            show_caption_above_media=True,
            disable_notification=not notify,
        )
    except Exception as error:
        logger.warning("Schedule card send failed for chat %s; sending a text fallback: %s", chat_id, redact_secrets(error))
        await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML")


async def test_update_notification(update, context):
    """Send two local-only sample updates without changing the database."""
    if not LOCAL_MODE:
        await update.message.reply_text("Ця команда доступна лише в локальному режимі.")
        return

    chat_id = update.effective_chat.id
    city = get_user_city(chat_id) or "lviv"
    groups = get_user_groups(chat_id, city)
    group = groups[0] if groups else "1.1"
    now = datetime.now(KYIV_TIMEZONE)
    samples = [
        (
            "є графік на завтра",
            "Відключення за графіком: з 04:00 до 07:30, з 14:30 до 18:00, з 21:30 до 24:00",
            "Відключення за графіком: з 05:00 до 08:00, з 15:00 до 18:30",
        ),
        (
            "відключень немає",
            "Відключень не заплановано",
            "Відключень не заплановано",
        ),
    ]
    for label, today_text, tomorrow_text in samples:
        await send_schedule_card(
            context.bot, chat_id, city, group, today_text, tomorrow_text,
            updated_at=now.isoformat(), notify=True, test=True, test_label=label,
        )
        await asyncio.sleep(0.5)
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

    if schedule_text == "Відключень не заплановано":
        return f"🟢 *Зараз є світло*\n{format_timeline(schedule_text)}\n✅ Відключень не заплановано"

    iv = extract_intervals(schedule_text)
    state, next_change = schedule_status(schedule_text)
    headline = "🟢 *Зараз є світло*" if state == "on" else "🔴 *Зараз немає світла*" if state == "off" else "ℹ️ *Поточний стан невідомий*"
    lines = [headline, format_timeline(schedule_text)]
    if next_change is not None and next_change < 1440:
        lines.append(f"⏭ Наступна зміна о *{fmt_time(next_change)}*")
    lines.append("\n🟢 *Є світло:*")
    
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
    state, next_change = schedule_status(curr_today)
    status_line = "🟢 *Зараз є світло*" if state == "on" else "🔴 *Зараз немає світла*" if state == "off" else "ℹ️ *Поточний стан невідомий*"
    msg = f"⚡️ *Оновився графік\\!*\n\n{status_line}\n{format_timeline(curr_today)}\n"
    if next_change is not None and next_change < 1440:
        msg += f"⏭ Наступна зміна о *{esc(fmt_time(next_change))}*\n"
    msg += f"\n{esc(city_emoji)} *{esc(city_name)}* · група *{esc(group)}*\n\n"
    
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

    if curr_tomorrow and prev_tomorrow:
        tomorrow_now = extract_intervals(curr_tomorrow)
        tomorrow_before = extract_intervals(prev_tomorrow)
        tomorrow_added = [item for item in tomorrow_now['off'] if item not in tomorrow_before['off']]
        tomorrow_removed = [item for item in tomorrow_before['off'] if item not in tomorrow_now['off']]
        if tomorrow_added or tomorrow_removed:
            msg += "📅 *Зміни на завтра:*\n"
            for start, end in tomorrow_added:
                msg += f"🔴 Додано вимкнення {esc(fmt_time(start))}—{esc(fmt_time(end))}\n"
            for start, end in tomorrow_removed:
                msg += f"🟢 Скасовано вимкнення {esc(fmt_time(start))}—{esc(fmt_time(end))}\n"
            msg += "\n"
    
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
        msg += "  • відключень не заплановано\n"
    
    if curr_tomorrow:
        msg += "\n━━━━━━━━━━━━━━━━━━━━\n\n📅 *ЗАВТРА:*\n\n"
        tm = extract_intervals(curr_tomorrow)
        msg += format_timeline(curr_tomorrow) + "\n\n"
        
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
            msg += "  • відключень не заплановано\n"
    
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
            logger.error("Broadcast failed for user %s: %s", chat_id, redact_secrets(e))
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
            today_date = datetime.now(KYIV_TIMEZONE).strftime('%Y-%m-%d')  # Local schedule date
            for group, data in groups_data.items():
                parsed_today, parsed_tomorrow = parse_schedule_entries(data)
                if not parsed_today:
                    logger.warning(f"No schedule found for {city_id} group {group}")
                    continue
                
                new_hash = hashlib.sha256(f"{parsed_today}|{parsed_tomorrow or ''}|{today_date}".encode()).hexdigest()
                old_data = get_schedule_with_date(city_id, group)
                old_hash = old_data['hash'] if old_data else None
                old_reference_date = old_data['reference_date'] if old_data else None
                
                save_schedule(city_id, group, parsed_today or '', parsed_tomorrow or '', new_hash, today_date)
                saved_count += 1
                logger.info(f"Saved schedule for {city_id} group {group}")
                
                # Only notify if hash changed AND dates match (not midnight boundary issue)
                # Treat a missing/old reference date as a baseline refresh. This
                # avoids notifications caused only by the midnight date rollover.
                date_changed = old_data is None or old_reference_date != today_date
                hash_changed = new_hash != old_hash
                
                if hash_changed and not date_changed:  # Hash changed but same reference date
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
                            'SELECT today_schedule, tomorrow_schedule, previous_today, previous_tomorrow, updated_at FROM schedules WHERE city = ? AND group_number = ?',
                            (city_id, group), fetch_one=True
                        )
                        if result:
                            await send_schedule_card(
                                bot_app.bot,
                                user['chat_id'],
                                city_id,
                                group,
                                result[0],
                                result[1],
                                updated_at=result[4],
                                notify=True,
                            )
                            await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.error("Notify error for chat %s: %s", user['chat_id'], redact_secrets(e))
                        
    except Exception as e:
        logger.error("Checker error: %s", redact_secrets(e))

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
            "⚡️ *СВІТЛО · ГРАФІКИ ТА СПОВІЩЕННЯ*\n\nВітаю! Тут можна стежити за графіком вимкнень і отримувати сповіщення про зміни.\n\n🏙 *Оберіть область, щоб почати:*",
            parse_mode='Markdown',
            reply_markup=get_city_keyboard()
        )
        return
    
    groups = get_user_groups(chat_id, city)
    city_name = CITIES[city]['name']
    
    if groups:
        await update.message.reply_text(
            f"⚡️ *СВІТЛО · ВАША ПАНЕЛЬ*\n\n🏙 *{city_name}*\n🔔 Підписок на групи: *{len(groups)}*\n\nОберіть дію нижче:",
            parse_mode='Markdown',
            reply_markup=REPLY_KEYBOARD
        )
        schedule = get_schedule(city, groups[0])
        if schedule and schedule['today']:
            await send_schedule_card(
                context.bot, chat_id, city, groups[0],
                schedule['today'], schedule['tomorrow'], schedule['updated_at']
            )
    else:
        await update.message.reply_text(
            f"⚡️ *СВІТЛО · ВАША ПАНЕЛЬ*\n\n🏙 *{city_name}*\n\nДодайте групу, щоб бачити графік і отримувати сповіщення:",
            parse_mode='Markdown',
            reply_markup=REPLY_KEYBOARD
        )

async def show_schedule(update, context):
    chat_id = update.effective_chat.id
    city = get_user_city(chat_id)
    
    if not city:
        await update.message.reply_text("❌ Спочатку оберіть область", reply_markup=get_city_keyboard())
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
                f"ℹ️ Актуальний графік для {city_name}, група {group}, поки недоступний. "
                "Джерело ще не опублікувало графік на сьогодні; перевірка оновиться автоматично.",
                reply_markup=REPLY_KEYBOARD
            )
            continue
        
        await send_schedule_card(
            context.bot,
            chat_id,
            city,
            group,
            schedule['today'] or '',
            schedule['tomorrow'],
            schedule['updated_at'],
        )
        if len(groups) > 1:
            await asyncio.sleep(0.3)

async def show_groups(update, context):
    chat_id = update.effective_chat.id
    city = get_user_city(chat_id)
    
    if not city:
        await update.message.reply_text("❌ Спочатку оберіть область", reply_markup=get_city_keyboard())
        return
    
    groups = get_user_groups(chat_id, city)
    city_name = CITIES[city]['name']
    city_emoji = CITIES[city]['emoji']
    
    if groups:
        groups_str = ", ".join(groups)
        text = f"⚡️ *ВАШІ ПІДПИСКИ*\n\n{city_emoji} *{city_name}*\n📍 Групи: *{groups_str}*\n\n🔔 Сповіщення надходитимуть про зміни у вибраних групах.\n_До {MAX_GROUPS_PER_USER} груп_"
    else:
        text = f"⚡️ *ВАШІ ПІДПИСКИ*\n\n{city_emoji} *{city_name}*\n▫️ Групи ще не обрані"
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=REPLY_KEYBOARD)

async def show_cities(update, context):
    chat_id = update.effective_chat.id
    current_city = get_user_city(chat_id)
    
    text = "⚡️ *ОБЛАСТЬ ДЛЯ ГРАФІКА*\n\nОберіть область, щоб бачити відповідний графік і керувати підписками.\n\n"
    if current_city:
        city_name = CITIES[current_city]['name']
        text += f"_Поточна область: {city_name}_\n\n"
        text += "⚠️ _При зміні області ваші поточні підписки залишаться, але графіки будуть показуватися для нової області_"
    
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=get_city_keyboard())

async def add_group(update, context):
    chat_id = update.effective_chat.id
    city = get_user_city(chat_id)
    
    if not city:
        await update.message.reply_text("❌ Спочатку оберіть область", reply_markup=get_city_keyboard())
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
        await update.message.reply_text("❌ Спочатку оберіть область", reply_markup=get_city_keyboard())
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
            await safe_edit(query, "❌ Невідома область", reply_markup=get_city_keyboard())
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
            schedule = get_schedule(city_id, groups[0])
            if schedule and schedule['today']:
                await send_schedule_card(
                    context.bot, chat_id, city_id, groups[0],
                    schedule['today'], schedule['tomorrow'], schedule['updated_at']
                )
        else:
            await safe_edit(
                query,
                f"✅ Область обрано: {city_emoji} *{city_name}*\n\nОберіть свою групу:",
                parse_mode='Markdown',
                reply_markup=get_group_keyboard(city_id)
            )
        return
    
    if data.startswith("add_"):
        city = get_user_city(chat_id)
        if not city:
            await safe_edit(query, "❌ Спочатку оберіть область", reply_markup=get_city_keyboard())
            return
        
        group = data[4:]
        success, error = add_user_group(chat_id, city, group)
        
        if success:
            schedule = get_schedule(city, group)
            groups = get_user_groups(chat_id, city)
            
            if schedule and schedule['today']:
                await safe_edit(
                    query,
                    f"✅ Групу {group} додано. Підписок: {len(groups)}/{MAX_GROUPS_PER_USER}.",
                    reply_markup=get_inline_keyboard(True)
                )
                await send_schedule_card(
                    context.bot, chat_id, city, group,
                    schedule['today'], schedule['tomorrow'], schedule['updated_at']
                )
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
            await safe_edit(query, "❌ Спочатку оберіть область", reply_markup=get_city_keyboard())
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
            await safe_edit(query, "❌ Спочатку оберіть область", reply_markup=get_city_keyboard())
            return
        
        groups = get_user_groups(chat_id, city)
        if not groups:
            await safe_edit(query, "❌ Спочатку додайте групу", reply_markup=get_inline_keyboard(False))
            return
        
        for group in groups:
            schedule = get_schedule(city, group)
            if schedule:
                await send_schedule_card(
                    context.bot, chat_id, city, group,
                    schedule['today'] or '', schedule['tomorrow'], schedule['updated_at']
                )
                await asyncio.sleep(0.3)
    
    elif data == "mygroups":
        city = get_user_city(chat_id)
        if not city:
            await safe_edit(query, "❌ Спочатку оберіть область", reply_markup=get_city_keyboard())
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
            await safe_edit(query, "❌ Спочатку оберіть область", reply_markup=get_city_keyboard())
            return
        
        current_groups = get_user_groups(chat_id, city)
        
        if len(current_groups) >= MAX_GROUPS_PER_USER:
            await safe_edit(
                query, 
                f"❌ Ви вже підписані на максимальну кількість груп ({MAX_GROUPS_PER_USER})",
                reply_markup=get_inline_keyboard(True)
            )
            return
        
        keyboard = get_group_keyboard(city, current_groups)
        if not keyboard:
            await safe_edit(query, "✅ Ви вже підписані на всі доступні групи.",
                            reply_markup=get_inline_keyboard(True))
            return
        
        city_name = CITIES[city]['name']
        await safe_edit(query, f"Оберіть групу для додавання ({city_name}):", reply_markup=keyboard)
    
    elif data == "removegroup":
        city = get_user_city(chat_id)
        if not city:
            await safe_edit(query, "❌ Спочатку оберіть область", reply_markup=get_city_keyboard())
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
        
        text = "🏙 *Оберіть область:*\n\n"
        if current_city:
            city_name = CITIES[current_city]['name']
            text += f"_Поточна область: {city_name}_\n\n"
            text += "⚠️ _При зміні області ваші підписки у старій області залишаться_"
        
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
flask_app.config['MAX_CONTENT_LENGTH'] = 64 * 1024

@flask_app.route('/health')
def health():
    return jsonify({'status': 'healthy'})


@flask_app.after_request
def add_security_headers(response):
    response.headers.setdefault('Cache-Control', 'no-store')
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    return response

@flask_app.route('/api/users')
def api_users():
    auth_error = api_auth_error()
    if auth_error:
        return auth_error
    users = get_all_users()
    return jsonify({'users': users, 'count': len(users)})

@flask_app.route('/webhook', methods=['POST'])
def webhook():
    if LOCAL_MODE:
        return jsonify({'error': 'Webhook is disabled in local polling mode'}), 404
    if not WEBHOOK_SECRET:
        return jsonify({'error': 'Webhook authentication is not configured'}), 503
    supplied_secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not hmac.compare_digest(supplied_secret.encode('utf-8'), WEBHOOK_SECRET.encode('utf-8')):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json(silent=True)
    event_keys = [key for key in ('message', 'callback_query') if key in data] if isinstance(data, dict) else []
    if (not isinstance(data, dict) or not isinstance(data.get('update_id'), int)
            or isinstance(data.get('update_id'), bool) or len(event_keys) != 1
            or not isinstance(data[event_keys[0]], dict)):
        return jsonify({'error': 'Invalid Telegram update'}), 400
    try:
        update_queue.put_nowait(data)
    except Full:
        return jsonify({'error': 'Update queue is full'}), 503
    return 'OK'

@flask_app.route('/api/broadcast', methods=['POST'])
def api_broadcast():
    auth_error = api_auth_error()
    if auth_error:
        return auth_error
    
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or 'message' not in data:
        return jsonify({'error': 'Missing message field'}), 400
    
    message = data['message']
    if not isinstance(message, str) or not message or len(message) > MAX_BROADCAST_LENGTH:
        return jsonify({'error': f'Message must contain 1-{MAX_BROADCAST_LENGTH} characters'}), 400
    parse_mode = data.get('parse_mode', 'Markdown')
    
    if parse_mode not in ['Markdown', 'MarkdownV2', 'HTML', None]:
        return jsonify({'error': 'Invalid parse_mode. Use: Markdown, MarkdownV2, HTML, or null'}), 400
    
    if bot_loop is None:
        return jsonify({'error': 'Bot not ready'}), 503

    if broadcast_is_rate_limited():
        return jsonify({'error': 'Broadcast rate limit exceeded'}), 429
    
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
        try:
            data = update_queue.get_nowait()
        except Empty:
            await asyncio.sleep(0.1)
            continue
        try:
            update = Update.de_json(data, bot_app.bot)
            await bot_app.process_update(update)
        except Exception as e:
            logger.error("Update error: %s", redact_secrets(e))
        finally:
            update_queue.task_done()

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
    if LOCAL_MODE:
        bot_app.add_handler(CommandHandler('testupdate', test_update_notification))
    
    bot_app.add_handler(CallbackQueryHandler(handle_callback))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    startup_attempt = 0
    while True:
        try:
            await bot_app.initialize()
            break
        except NetworkError as error:
            startup_attempt += 1
            retry_delay = min(60, 2 ** min(startup_attempt, 6))
            logger.warning(
                "Telegram API unavailable during startup (%s); retrying in %s seconds",
                redact_secrets(error),
                retry_delay,
            )
            await asyncio.sleep(retry_delay)
    await bot_app.start()
    if LOCAL_MODE:
        await bot_app.updater.start_polling()
    else:
        if not WEBHOOK_URL:
            raise RuntimeError('WEBHOOK_URL must be set unless LOCAL_MODE=1')
        await bot_app.bot.set_webhook(
            url=f"{WEBHOOK_URL.rstrip('/')}/webhook",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=['message', 'callback_query'],
        )

def run_bot():
    global bot_loop
    loop = asyncio.new_event_loop()
    bot_loop = loop
    asyncio.set_event_loop(loop)
    loop.run_until_complete(setup())
    if not LOCAL_MODE:
        loop.create_task(process_updates())
    loop.create_task(checker_loop())
    loop.run_forever()

if __name__ == '__main__':
    validate_config()
    init_db()
    Thread(target=run_bot, daemon=True).start()
    host = '127.0.0.1' if LOCAL_MODE else '0.0.0.0'
    serve(flask_app, host=host, port=PORT, threads=8)
