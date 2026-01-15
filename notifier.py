import os
import json
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError
from scraper import ScheduleScraper


load_dotenv()


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ScheduleNotifier:
    def __init__(self, bot_token):
        self.bot = Bot(token=bot_token)
        self.scraper = ScheduleScraper()
    
    def load_users_from_file(self, filepath='users.json'):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                users_list = data.get('users', [])
                
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
        try:
            await self.bot.send_message(chat_id=user_id, text=message, parse_mode='HTML')
            logger.info(f"Notification sent to user {user_id}")
            return True
        except TelegramError as e:
            logger.error(f"Failed to send notification to user {user_id}: {e}")
            return False
    
    def format_change_message(self, group_id, old_data, new_data):
        message = "Schedule change for group " + str(group_id) + "

"
        
        if new_data and len(new_data) > 0:
            latest_schedule = new_data[0]
            
            schedule_date = latest_schedule.get('date', '')
            if schedule_date:
                message += "Date: " + schedule_date + "

"
            
            schedule_text = latest_schedule.get('schedule', '')
            if schedule_text:
                schedule_text = schedule_text.strip()
                message += schedule_text + "
"
            else:
                message += "New schedule published
"
                message += "Details: https://poweron.loe.lviv.ua/
"
        else:
            message += "New schedule published
"
            message += "Details: https://poweron.loe.lviv.ua/
"
        
        return message
    
    async def check_and_notify(self):
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


async def main():
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is required")
        return
    
    notifier = ScheduleNotifier(bot_token)
    await notifier.check_and_notify()
    logger.info("Schedule check completed")


if __name__ == '__main__':
    asyncio.run(main())
