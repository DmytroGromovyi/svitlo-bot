"""
Schedule Notifier - Reads users from SQLite and sends notifications
"""
import os
import sqlite3
import logging
import asyncio
from pathlib import Path
from telegram import Bot
from telegram.error import TelegramError
from scraper import ScheduleScraper

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = Path('data/users.db')


def get_all_users():
    """Fetch all users from SQLite database"""
    if not DB_PATH.exists():
        logger.warning(f"Database not found: {DB_PATH}")
        return {}
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, group_id, username, first_name FROM users')
    rows = cursor.fetchall()
    conn.close()
    
    users = {
        str(row[0]): {
            'user_id': row[0],
            'group': row[1],
            'username': row[2],
            'first_name': row[3]
        }
        for row in rows
    }
    
    logger.info(f"Loaded {len(users)} users from database")
    return users


def calculate_power_times(schedule_text):
    """Calculate power ON and OFF times"""
    import re
    from datetime import time
    
    # Extract OFF times: "з 03:00 до 06:30"
    pattern = re.compile(r'з (\d{1,2}):(\d{2}) до (\d{1,2}):(\d{2})')
    off_ranges = []
    
    for match in pattern.finditer(schedule_text):
        start_h, start_m, end_h, end_m = map(int, match.groups())
        start_min = start_h * 60 + start_m
        end_min = (end_h * 60 + end_m) if end_h != 24 else 1440
        off_ranges.append((start_min, end_min))
    
    # Sort and merge overlapping ranges
    off_ranges.sort()
    merged = []
    for start, end in off_ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    
    # Calculate ON times (gaps)
    on_ranges = []
    current = 0
    for off_start, off_end in merged:
        if current < off_start:
            on_ranges.append((current, off_start))
        current = max(current, off_end)
    
    if current < 1440:
        on_ranges.append((current, 1440))
    
    # Format times
    def fmt(minutes):
        h, m = divmod(minutes, 60)
        return f"{h:02d}:{m:02d}"
    
    off_text = ", ".join(f"з {fmt(s)} до {fmt(e) if e < 1440 else '24:00'}" for s, e in merged)
    on_text = ", ".join(f"з {fmt(s)} до {fmt(e) if e < 1440 else '24:00'}" for s, e in on_ranges)
    
    return on_text or "немає", off_text or "немає"


def format_notification(group_id, schedule_entries):
    """Format notification message"""
    message = f"⚡️ <b>Оновлення графіку!</b>\n\nГрупа: <b>{group_id}</b>\n\n"
    
    for idx, entry in enumerate(schedule_entries[:2]):  # Max 2 days
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
    
    message += "ℹ️ Графік може змінюватися."
    return message


async def send_notification(bot, user_id, message):
    """Send notification to user"""
    try:
        await bot.send_message(chat_id=user_id, text=message, parse_mode='HTML')
        logger.info(f"✓ Sent to {user_id}")
        return True
    except TelegramError as e:
        logger.error(f"✗ Failed to send to {user_id}: {e}")
        return False


async def main():
    """Main function"""
    logger.info("="*60)
    logger.info("🔍 Starting schedule check")
    logger.info("="*60)
    
    # Get bot token
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return
    
    bot = Bot(token)
    
    # Fetch users
    users = get_all_users()
    if not users:
        logger.info("No users found")
        return
    
    # Check schedule
    scraper = ScheduleScraper()
    result = scraper.check_for_changes()
    
    if not result:
        logger.error("Failed to check schedule")
        return
    
    if not result['changed']:
        logger.info("No changes detected")
        return
    
    logger.info("📢 Changes detected! Sending notifications...")
    
    # Group users by their selected group
    users_by_group = {}
    for user_id, user_data in users.items():
        group = user_data.get('group')
        if group:
            users_by_group.setdefault(group, []).append(user_id)
    
    # Get new schedule
    new_schedule = result['new_schedule']
    old_schedule = result.get('old_schedule', {})
    
    new_groups = new_schedule.get('groups', {})
    old_groups = old_schedule.get('groups', {})
    
    # Send notifications
    sent = 0
    for group_id, user_ids in users_by_group.items():
        new_data = new_groups.get(group_id)
        old_data = old_groups.get(group_id)
        
        # Check if this group changed
        if new_data != old_data and new_data:
            message = format_notification(group_id, new_data)
            
            for user_id in user_ids:
                success = await send_notification(bot, user_id, message)
                if success:
                    sent += 1
                await asyncio.sleep(0.5)  # Rate limiting
    
    logger.info(f"✅ Sent {sent} notifications")


if __name__ == '__main__':
    asyncio.run(main())
