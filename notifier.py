import os
import json
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError
from scraper import ScheduleScraper


# Load environment variables from .env file
load_dotenv()


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ScheduleNotifier:
    def __init__(self, bot_token):
        self.bot = Bot(token=bot_token)
        self.scraper = ScheduleScraper()
    
    def load_users_from_file(self, filepath='users.json'):
        """Load users from the JSON file fetched by GitHub Actions"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                users_list = data.get('users', [])
                
                # Convert to dict format: {user_id: {'group': group_id}}
                users_dict = {}
                for user in users_list:
                    user_id = str(user.get('user_id'))
                    group_id = user.get('group_id')
                    users_dict[user_id] = {'group': group_id}
                
                logger.info(f"Loaded {len(users_dict)} users from {filepath}")
                return users_dict
        except FileNotFoundError:
            logger.error(f"File {filepath} not found")
            return {}
        except Exception as e:
            logger.error(f"Error loading users: {e}")
            return {}
    
    async def send_notification(self, user_id, message):
        """Send notification to a specific user"""
        try:
            await self.bot.send_message(chat_id=user_id, text=message, parse_mode='HTML')
            logger.info(f"Notification sent to user {user_id}")
            return True
        except TelegramError as e:
            logger.error(f"Failed to send notification to user {user_id}: {e}")
            return False
    
    def _calculate_power_times(self, schedule_text):
        """Calculate power ON and OFF times from schedule text"""
        import re
        
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
        
        # Calculate ON times (gaps between OFF times)
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
    
    def format_change_message(self, group_id, old_data, new_data):
        """Format notification message for schedule changes - show today and tomorrow"""
        message = f"⚡️ <b>Оновлення графіку відключень!</b>\n\n"
        message += f"Група: <b>{group_id}</b>\n\n"
        
        if new_data and len(new_data) > 0:
            # Show up to 2 days (today and tomorrow)
            for idx, schedule_entry in enumerate(new_data[:2]):
                schedule_date = schedule_entry.get('date', '')
                schedule_text = schedule_entry.get('schedule', '')
                
                if not schedule_text:
                    continue
                
                # Determine label
                label = "Сьогодні" if idx == 0 else "Завтра"
                if schedule_date and schedule_date != "Today":
                    label = schedule_date
                
                message += f"📅 <b>{label}</b>\n\n"
                
                # Calculate power ON and OFF times
                on_time, off_time = self._calculate_power_times(schedule_text)
                
                message += f"🟢 <b>Є світло:</b> {on_time}\n"
                message += f"🔴 <b>Немає світла:</b> {off_time}\n\n"
            
            message += "ℹ️ Графік може змінюватися протягом дня."
        else:
            message += "📋 <b>Опубліковано новий графік</b>\n"
            message += "Використайте /schedule для перегляду."
        
        return message
    
    async def check_and_notify(self):
        """Check for schedule changes and notify users"""
        logger.info("Starting schedule check...")
        
        # Check for changes
        result = self.scraper.check_for_changes()
        
        if not result:
            logger.error("Failed to check schedule")
            return
        
        if not result['changed']:
            logger.info("No changes detected, skipping notifications")
            return
        
        logger.info("Changes detected! Preparing notifications...")
        
        # Load users from file (fetched by GitHub Actions)
        users = self.load_users_from_file('users.json')
        
        if not users:
            logger.info("No users registered, skipping notifications")
            return
        
        # Get new schedule
        new_schedule = result['new_schedule']
        old_schedule = result['old_schedule']
        
        # Group users by their selected group
        users_by_group = {}
        for user_id, user_data in users.items():
            group = user_data.get('group')
            if group:
                if group not in users_by_group:
                    users_by_group[group] = []
                users_by_group[group].append(user_id)
        
        # Send notifications
        notification_count = 0
        
        # Check which groups have changed
        new_groups = new_schedule.get('groups', {})
        old_groups = old_schedule.get('groups', {}) if old_schedule else {}
        
        for group_id, user_ids in users_by_group.items():
            # Get schedule for this group
            new_group_data = new_groups.get(group_id)
            old_group_data = old_groups.get(group_id)
            
            # Check if this specific group's schedule changed
            if new_group_data != old_group_data:
                logger.info(f"Group {group_id} schedule changed, notifying {len(user_ids)} users")
                message = self.format_change_message(group_id, old_group_data, new_group_data)
                
                for user_id in user_ids:
                    success = await self.send_notification(user_id, message)
                    if success:
                        notification_count += 1
                    # Small delay to avoid rate limiting
                    await asyncio.sleep(0.5)
            else:
                logger.info(f"Group {group_id} schedule unchanged, skipping notifications")
        
        logger.info(f"Notifications sent: {notification_count}")
        
        # If overall schedule changed but no specific groups matched, notify all users
        if notification_count == 0 and result['changed'] and users:
            logger.info("Overall schedule changed but no group-specific changes detected")
            logger.info("This might be a new schedule format or date change")


async def main():
    """Main function for cron job"""
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is required")
        return
    
    notifier = ScheduleNotifier(bot_token)
    await notifier.check_and_notify()
    logger.info("Schedule check completed")


if __name__ == '__main__':
    asyncio.run(main())