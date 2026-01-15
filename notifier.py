import os
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError
from scraper import ScheduleScraper
from bot import UserStorage

# Load environment variables from .env file
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ScheduleNotifier:
    def __init__(self, bot_token):
        self.bot = Bot(token=bot_token)
        self.scraper = ScheduleScraper()
        self.user_storage = UserStorage()
    
    async def send_notification(self, user_id, message):
        """Send notification to a specific user"""
        try:
            await self.bot.send_message(chat_id=user_id, text=message, parse_mode='HTML')
            logger.info(f"Notification sent to user {user_id}")
            return True
        except TelegramError as e:
            logger.error(f"Failed to send notification to user {user_id}: {e}")
            return False
    
    def format_change_message(self, group_id, old_data, new_data):
        """Format a message about schedule changes"""
        message = f"⚡️ <b>Зміна графіку відключень!</b>\n\n"
        message += f"Група: <b>{group_id}</b>\n\n"
        
        if new_data and len(new_data) > 0:
            latest_schedule = new_data[0]  # Get the most recent entry
            
            # Add date/timestamp if available
            schedule_date = latest_schedule.get('date', '')
            if schedule_date:
                message += f"📅 <b>{schedule_date}</b>\n\n"
            
            # Add the actual schedule
            schedule_text = latest_schedule.get('schedule', '')
            if schedule_text:
                # Clean up the text
                schedule_text = schedule_text.replace('Електроенергії немає з', '🔴 Немає світла:')
                schedule_text = schedule_text.strip()
                message += f"📋 {schedule_text}\n"
            else:
                message += "📋 <b>Опубліковано новий графік</b>\n"
                message += "Деталі доступні на сайті: https://poweron.loe.lviv.ua/\n"
        else:
            message += "📋 <b>Опубліковано новий графік</b>\n"
            message += "Перевірте деталі на сайті: https://poweron.loe.lviv.ua/\n"
        
        return message
    
    def _extract_schedule_summary(self, schedule_data):
        """Extract a brief summary from schedule data"""
        if isinstance(schedule_data, dict):
            content = schedule_data.get('content', '')
            # Return first 200 characters
            if content:
                return content[:200] + "..." if len(content) > 200 else content
        return "Деталі доступні на сайті"
    
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
        
        # Get all users
        users = self.user_storage.get_all_users()
        
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
            # Optionally send a generic notification
            # Uncommented this to avoid spamming users with generic messages
            # message = (
            #     "⚡️ <b>Оновлення графіку відключень!</b>\n\n"
            #     "Графік відключень було оновлено.\n"
            #     "Перевірте актуальну інформацію на сайті: https://poweron.loe.lviv.ua/"
            # )
            # for user_id in users.keys():
            #     await self.send_notification(user_id, message)
            #     await asyncio.sleep(0.5)

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