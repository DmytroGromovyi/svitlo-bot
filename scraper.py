import requests
import hashlib
import json
import os
import logging
import re
from bs4 import BeautifulSoup
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ScheduleScraper:
    def __init__(self, url='https://poweron.loe.lviv.ua/', storage_path='data/schedules.json'):
        self.url = url
        self.api_url = 'https://api.loe.lviv.ua/api/menus?page=1&type=photo-grafic'
        self.storage_path = storage_path
        self.schedules = self._load_schedules()
    
    def _load_schedules(self):
        """Load previous schedules from storage"""
        try:
            with open(self.storage_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
    
    def _save_schedules(self):
        """Save schedules to storage"""
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        with open(self.storage_path, 'w', encoding='utf-8') as f:
            json.dump(self.schedules, f, ensure_ascii=False, indent=2)
    
    def fetch_schedule(self):
        """Fetch the current schedule from the API"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json'
            }
            
            response = requests.get(self.api_url, headers=headers, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            logger.info(f"✓ Fetched API data successfully")
            logger.info(f"  API response type: {data.get('@type')}")
            logger.info(f"  Members found: {len(data.get('hydra:member', []))}")
            
            return json.dumps(data, ensure_ascii=False)
            
        except Exception as e:
            logger.error(f"Error fetching schedule from API: {e}", exc_info=True)
            return None
    
    def parse_schedule(self, json_content):
        """Parse the schedule from API JSON response"""
        if not json_content:
            return None
        
        try:
            data = json.loads(json_content)
            
            schedule_data = {
                'timestamp': datetime.now().isoformat(),
                'groups': {},
                'raw_data': {}
            }
            
            # Extract menu items
            members = data.get('hydra:member', [])
            
            for member in members:
                menu_items = member.get('menuItems', [])
                
                for item in menu_items:
                    # Get the raw HTML content
                    raw_html = item.get('rawHtml', '')
                    raw_mobile_html = item.get('rawMobileHtml', '')
                    name = item.get('name', '')
                    description = item.get('description', '')
                    
                    # Store metadata
                    schedule_data['raw_data'][name] = {
                        'name': name,
                        'description': description,
                        'html': raw_html,
                        'mobile_html': raw_mobile_html
                    }
                    
                    # Parse HTML to extract group schedules
                    soup = BeautifulSoup(raw_html, 'html.parser')
                    text = soup.get_text()
                    
                    # Find all group mentions (Група 1.1, Група 2.2, etc.)
                    import re
                    group_pattern = re.compile(r'Група (\d+\.\d+)\. (.+?)(?=Група \d+\.\d+\.|$)', re.DOTALL)
                    matches = group_pattern.findall(text)
                    
                    for group_num, schedule_text in matches:
                        if group_num not in schedule_data['groups']:
                            schedule_data['groups'][group_num] = []
                        
                        schedule_data['groups'][group_num].append({
                            'date': name,
                            'schedule': schedule_text.strip(),
                            'timestamp': item.get('name', '')
                        })
            
            logger.info(f"✓ Parsed {len(schedule_data['groups'])} groups")
            
            if schedule_data['groups']:
                logger.info(f"  Groups: {', '.join(sorted(schedule_data['groups'].keys()))}")
            
            return schedule_data
            
        except Exception as e:
            logger.error(f"Error parsing schedule: {e}", exc_info=True)
            return None
    
    def calculate_hash(self, data):
        """Calculate hash of schedule data"""
        if not data:
            return None
        json_str = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(json_str.encode('utf-8')).hexdigest()
    
    def check_for_changes(self):
        """Check if schedule has changed"""
        json_content = self.fetch_schedule()
        if not json_content:
            logger.warning("Could not fetch schedule from API")
            return None
        
        # Save a copy of the JSON for debugging
        debug_path = 'data/last_fetch.json'
        os.makedirs(os.path.dirname(debug_path), exist_ok=True)
        with open(debug_path, 'w', encoding='utf-8') as f:
            f.write(json_content)
        logger.info(f"✓ Saved JSON to {debug_path} for debugging")
        
        new_schedule = self.parse_schedule(json_content)
        if not new_schedule:
            logger.warning("Could not parse schedule")
            return None
        
        new_hash = self.calculate_hash(new_schedule)
        old_hash = self.schedules.get('last_hash')
        
        result = {
            'changed': new_hash != old_hash,
            'new_schedule': new_schedule,
            'old_schedule': self.schedules.get('last_schedule'),
            'new_hash': new_hash,
            'old_hash': old_hash,
            'timestamp': datetime.now().isoformat()
        }
        
        if result['changed']:
            logger.info(f"🔔 Schedule has changed! Old hash: {old_hash}, New hash: {new_hash}")
            self.schedules['last_hash'] = new_hash
            self.schedules['last_schedule'] = new_schedule
            self.schedules['last_checked'] = datetime.now().isoformat()
            self._save_schedules()
        else:
            logger.info("✓ No changes detected in schedule")
            self.schedules['last_checked'] = datetime.now().isoformat()
            self._save_schedules()
        
        return result
    
    def get_group_schedule(self, group_id):
        """Get schedule for a specific group"""
        last_schedule = self.schedules.get('last_schedule', {})
        groups = last_schedule.get('groups', {})
        return groups.get(group_id)

def main():
    """Test the scraper"""
    scraper = ScheduleScraper()
    
    print("\n" + "="*60)
    print("Testing Power Schedule Scraper")
    print("="*60 + "\n")
    
    result = scraper.check_for_changes()
    
    if result:
        print(f"✓ Fetch successful")
        print(f"✓ Changed: {result['changed']}")
        print(f"✓ Timestamp: {result['timestamp']}")
        print(f"✓ Hash: {result['new_hash'][:16]}...")
        
        new_schedule = result['new_schedule']
        print(f"\nSchedule Data:")
        print(f"  - Dates found: {len(new_schedule.get('dates', []))}")
        print(f"  - Groups found: {len(new_schedule.get('groups', {}))}")
        
        if new_schedule.get('groups'):
            print(f"\nGroups detected:")
            for group_id, data in new_schedule['groups'].items():
                print(f"  - Group {group_id}: {len(data)} entries")
        
        if new_schedule.get('dates'):
            print(f"\nDates:")
            for date in new_schedule['dates'][:3]:
                print(f"  - {date}")
        
        print(f"\n✓ HTML saved to: data/last_fetch.html")
        print(f"✓ Schedule saved to: data/schedules.json")
        print("\nTip: Check data/last_fetch.html to see what was fetched")
        
        if result['changed']:
            print("\n🔔 Schedule has changed!")
        else:
            print("\n✓ No changes in schedule")
    else:
        print("✗ Failed to fetch or parse schedule")
        print("\nTroubleshooting:")
        print("  1. Check your internet connection")
        print("  2. Visit https://poweron.loe.lviv.ua/ in your browser")
        print("  3. Check data/last_fetch.html to see what was downloaded")

if __name__ == '__main__':
    main()