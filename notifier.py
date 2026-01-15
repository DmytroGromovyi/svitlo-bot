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
    
    def format_change_message(self, group_id, old_data, new_data):
        """Format a message about schedule changes"""
        message = "<b>Зміна графіку відключень!</b>

"
        message += f"Група: <b>{group_id}</b>

"
        
        if new_data and len(new_data) > 0:
            latest_schedule = new_data[0]
            
            schedule_date = latest_schedule.get('date', '')
            if schedule_date:
                message += f"<b>{schedule_date}</b>

"
            
            schedule_text = latest_schedule.get('schedule', '')
            if schedule_text:
                schedule_text = schedule_text.replace('Електроенергії немає з', 'Немає світла:')
                schedule_text = schedule_text.strip()
                message += f"{schedule_text}
"
            else:
                message += "<b>Опубліковано новий графік</b>
"
                message += "Деталі доступні на сайті: https://poweron.loe.lviv.ua/
"
        else:
            message += "<b>Опубліковано новий графік</b>
"
            message += "Перевірте деталі на сайті: https://poweron.loe.lviv.ua/
"
        
        return message
    
    def _extract_schedule_summary(self, schedule_data):
        """Extract a brief summary from schedule data"""
        if isinstance(schedule_data, dict):
            content = schedule_data.get('content', '')
            if content:
                return content[:200] + "..." if len(content) > 200 else content
        return "Деталі доступні на сайті"
    
    async def check_and_notify(self):
        """Check for schedule changes and notify users"""
        logger.info("Starting schedule check...")
        
        result = self.scraper.check_for_changes()
        
        if not result:
            logger.error("Failed to check schedule")
            return
        
        if not result['changed']:
            logger.info("No changes detected, skipping notifications")
            return
        
        logger.info("Changes detected! Preparing notifications...")
        
        users = self.load_users_from_file('users.json')
        
        if not users:
            logger.info("No users registered, skipping notifications")
            return
        
        new_schedule = result['new_schedule']
        old_schedule = result['old_schedule']
        
        users_by_group = {}
        for user_id, user_data in users.items():
            group = user_data.get('group')
            if group:
                if group not in users_by_group:
                    users_by_group[group] = []
                users_by_group[group].append(user_id)
        
        notification_count = 0
        
        new_groups = new_schedule.get('groups', {})
        old_groups = old_schedule.get('groups', {}) if old_schedule else {}
        
        for group_id, user_ids in users_by_group.items():
            new_group_data = new_groups.get(group_id)
            old_group_data = old_groups.get(group_id)
            
            if new_group_data != old_group_data:
                logger.info(f"Group {group_id} schedule changed, notifying {len(user_ids)} users")
                message = self.format_change_message(group_id, old_group_data, new_group_data)
                
                for user_id in user_ids:
                    success = await self.send_notification(user_id, message)
                    if success:
                        notification_count += 1
                    await asyncio.sleep(0.5)
            else:
                logger.info(f"Group {group_id} schedule unchanged, skipping notifications")
        
        logger.info(f"Notifications sent: {notification_count}")
        
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
