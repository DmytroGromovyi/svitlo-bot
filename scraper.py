import requests
import hashlib
import json
import os
import logging
import re
from bs4 import BeautifulSoup
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# City configurations
CITY_CONFIGS = {
    'lviv': {
        'name': 'Львівська область',
        'url': 'https://poweron.loe.lviv.ua/',
        'api_url': 'https://api.loe.lviv.ua/api/menus?page=1&type=photo-grafic',
        'source_type': 'api',  # Uses API endpoint
    },
    'ivano-frankivsk': {
        'name': 'Франківська область',
        'url': 'https://github.com/yaroslav2901/OE_OUTAGE_DATA/blob/main/data/Prykarpattiaoblenerho.json',
        'api_url': 'https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Prykarpattiaoblenerho.json',
        'source_type': 'github_json',  # Direct JSON from GitHub
    }
}

class ScheduleScraper:
    def __init__(self, city='lviv', storage_path='data/schedules.json'):
        if city not in CITY_CONFIGS:
            raise ValueError(f"Unknown city: {city}. Available cities: {', '.join(CITY_CONFIGS.keys())}")
        
        self.city = city
        self.config = CITY_CONFIGS[city]
        self.storage_path = storage_path
        self.schedules = self._load_schedules()
    
    def _load_schedules(self):
        """Load previous schedules from storage"""
        try:
            with open(self.storage_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Ensure city-based structure
                if self.city not in data:
                    data[self.city] = {}
                return data
        except FileNotFoundError:
            return {self.city: {}}
    
    def _save_schedules(self):
        """Save schedules to storage"""
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        with open(self.storage_path, 'w', encoding='utf-8') as f:
            json.dump(self.schedules, f, ensure_ascii=False, indent=2)
    
    def fetch_schedule(self):
        """Fetch the current schedule based on city configuration"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json'
            }
            
            response = requests.get(self.config['api_url'], headers=headers, timeout=30)
            response.raise_for_status()
            
            if self.config['source_type'] == 'github_json':
                # GitHub returns JSON directly
                data = response.json()
            else:
                # API returns structured data
                data = response.json()
            
            logger.info(f"✓ Fetched {self.config['name']} schedule successfully")
            return json.dumps(data, ensure_ascii=False)
            
        except Exception as e:
            logger.error(f"Error fetching schedule from {self.config['name']}: {e}", exc_info=True)
            return None
    
    def parse_schedule(self, json_content):
        """Parse schedule based on city source type"""
        if not json_content:
            return None
        
        try:
            data = json.loads(json_content)
            
            if self.config['source_type'] == 'github_json':
                return self._parse_github_json(data)
            else:
                return self._parse_api_data(data)
                
        except Exception as e:
            logger.error(f"Error parsing {self.config['name']} schedule: {e}", exc_info=True)
            return None
    
    def _parse_api_data(self, data):
        """Parse Lviv API data format"""
        schedule_data = {
            'timestamp': datetime.now().isoformat(),
            'groups': {}
        }
        
        members = data.get('hydra:member', [])
        
        for member in members:
            menu_items = member.get('menuItems', [])
            
            for item in menu_items:
                raw_html = item.get('rawHtml', '')
                
                # Parse HTML
                soup = BeautifulSoup(raw_html, 'html.parser')
                text = soup.get_text()
                
                # Extract groups
                group_pattern = re.compile(r'Група (\d+\.\d+)\. (.+?)(?=Група \d+\.\d+\.|$)', re.DOTALL)
                matches = group_pattern.findall(text)
                
                for group_num, schedule_text in matches:
                    if group_num not in schedule_data['groups']:
                        schedule_data['groups'][group_num] = []
                    
                    schedule_data['groups'][group_num].append({
                        'date': item.get('name', ''),
                        'schedule': schedule_text.strip()
                    })
        
        return schedule_data
    
    def _parse_github_json(self, data):
        """Parse Ivano-Frankivsk GitHub JSON format with multiple timestamps"""
        schedule_data = {
            'timestamp': datetime.now().isoformat(),
            'groups': {}
        }
        
        fact = data.get('fact', {})
        fact_data = fact.get('data', {})
        preset = data.get('preset', {})
        time_zone = preset.get('time_zone', {})
        today_timestamp = fact.get('today')
        
        if not fact_data:
            logger.warning("No fact data found in Ivano-Frankivsk JSON")
            return schedule_data
        
        # Sort timestamps to get today and tomorrow
        timestamps = sorted([int(ts) for ts in fact_data.keys()])
        
        logger.info(f"Found {len(timestamps)} timestamp(s) in data: {timestamps}")
        
        # Process each timestamp
        for idx, timestamp in enumerate(timestamps):
            timestamp_str = str(timestamp)
            groups_data = fact_data[timestamp_str]
            
            # Determine if this is today or tomorrow
            if timestamp == today_timestamp or idx == 0:
                date_label = f'Сьогодні ({fact.get("update", "")})'
            else:
                date_label = 'Завтра'
            
            logger.info(f"Processing timestamp {timestamp} as '{date_label}'")
            
            # Process each group
            for group_key, hours in groups_data.items():
                if not group_key.startswith('GPV'):
                    continue
                
                group_num = group_key.replace('GPV', '')
                
                # Build schedule text from hourly data
                schedule_text = self._build_schedule_from_hours(hours, time_zone)
                
                if group_num not in schedule_data['groups']:
                    schedule_data['groups'][group_num] = []
                
                schedule_data['groups'][group_num].append({
                    'date': date_label,
                    'schedule': schedule_text
                })
        
        return schedule_data
    
    def _build_schedule_from_hours(self, hours, time_zone):
        """Convert hourly yes/no data to schedule text format"""
        # Find all OFF periods (where value is "no", "first", "second", or "maybe")
        off_periods = []
        start_hour = None
        
        for hour_num in range(1, 25):
            hour_str = str(hour_num)
            status = hours.get(hour_str, 'yes')
            
            # Treat "no", "first", "second", and "maybe" as outages
            if status in ['no', 'first', 'second', 'maybe']:
                if start_hour is None:
                    start_hour = hour_num
            else:
                if start_hour is not None:
                    # End of OFF period
                    off_periods.append((start_hour, hour_num))
                    start_hour = None
        
        # If still in OFF period at end of day
        if start_hour is not None:
            off_periods.append((start_hour, 25))  # 25 represents end of day (24:00)
        
        # Build schedule text
        if not off_periods:
            return "Відключень не заплановано"
        
        parts = []
        for start, end in off_periods:
            # Get time strings from time_zone
            start_time = time_zone.get(str(start), [None, "00:00", "01:00"])[1]
            end_time = time_zone.get(str(end - 1), [None, "23:00", "24:00"])[2]
            
            # Handle end of day
            if end == 25:
                end_time = "24:00"
            
            parts.append(f"з {start_time} до {end_time}")
        
        return "Відключення електроенергії: " + ", ".join(parts)
    
    def calculate_hash(self, data):
        """Calculate hash only from schedule content, ignore timestamps"""
        if not data:
            return None
        
        relevant_data = {
            'groups': data.get('groups', {})
        }
        
        for group_id, entries in relevant_data['groups'].items():
            cleaned_entries = []
            for entry in entries:
                cleaned_entry = {
                    'schedule': entry.get('schedule', '')
                }
                cleaned_entries.append(cleaned_entry)
            relevant_data['groups'][group_id] = cleaned_entries
        
        json_str = json.dumps(relevant_data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(json_str.encode('utf-8')).hexdigest()
    
    def check_for_changes(self):
        """Check if schedule has changed for this city"""
        json_content = self.fetch_schedule()
        if not json_content:
            logger.warning(f"Could not fetch schedule from {self.config['name']} API")
            return None
        
        # Save a copy of the JSON for debugging
        debug_path = f'data/last_fetch_{self.city}.json'
        os.makedirs(os.path.dirname(debug_path), exist_ok=True)
        with open(debug_path, 'w', encoding='utf-8') as f:
            f.write(json_content)
        logger.info(f"✓ Saved {self.config['name']} JSON to {debug_path} for debugging")
        
        new_schedule = self.parse_schedule(json_content)
        if not new_schedule:
            logger.warning(f"Could not parse {self.config['name']} schedule")
            return None
        
        new_hash = self.calculate_hash(new_schedule)
        
        # Get old hash for this city
        city_data = self.schedules.get(self.city, {})
        old_hash = city_data.get('last_hash')
        
        result = {
            'changed': new_hash != old_hash,
            'new_schedule': new_schedule,
            'old_schedule': city_data.get('last_schedule'),
            'new_hash': new_hash,
            'old_hash': old_hash,
            'timestamp': datetime.now().isoformat(),
            'city': self.city
        }
        
        if result['changed']:
            logger.info(f"🔔 {self.config['name']} schedule has changed! Old hash: {old_hash}, New hash: {new_hash}")
            self.schedules[self.city]['last_hash'] = new_hash
            self.schedules[self.city]['last_schedule'] = new_schedule
            self.schedules[self.city]['last_checked'] = datetime.now().isoformat()
            self._save_schedules()
        else:
            logger.info(f"✓ No changes detected in {self.config['name']} schedule")
            self.schedules[self.city]['last_checked'] = datetime.now().isoformat()
            self._save_schedules()
        
        return result
    
    def get_group_schedule(self, group_id):
        """Get schedule for a specific group in this city"""
        city_data = self.schedules.get(self.city, {})
        last_schedule = city_data.get('last_schedule', {})
        groups = last_schedule.get('groups', {})
        return groups.get(group_id)

def main():
    """Test the scraper for all cities"""
    print("\n" + "="*60)
    print("Testing Multi-City Power Schedule Scraper")
    print("="*60 + "\n")
    
    for city_id, city_config in CITY_CONFIGS.items():
        print(f"\n{'='*60}")
        print(f"Testing {city_config['name']} ({city_id})")
        print(f"{'='*60}\n")
        
        scraper = ScheduleScraper(city=city_id)
        
        result = scraper.check_for_changes()
        
        if result:
            print(f"✓ Fetch successful for {city_config['name']}")
            print(f"✓ Changed: {result['changed']}")
            print(f"✓ Timestamp: {result['timestamp']}")
            print(f"✓ Hash: {result['new_hash'][:16]}...")
            
            new_schedule = result['new_schedule']
            print(f"\nSchedule Data:")
            print(f"  - Groups found: {len(new_schedule.get('groups', {}))}")
            
            if new_schedule.get('groups'):
                print(f"\nGroups detected:")
                for group_id, data in list(new_schedule['groups'].items())[:5]:
                    print(f"  - Group {group_id}: {len(data)} entries")
                    for entry in data:
                        print(f"    * {entry['date']}: {entry['schedule'][:80]}...")
                
                if len(new_schedule['groups']) > 5:
                    print(f"  ... and {len(new_schedule['groups']) - 5} more groups")
            
            print(f"\n✓ JSON saved to: data/last_fetch_{city_id}.json")
            
            if result['changed']:
                print(f"\n🔔 {city_config['name']} schedule has changed!")
            else:
                print(f"\n✓ No changes in {city_config['name']} schedule")
        else:
            print(f"✗ Failed to fetch or parse {city_config['name']} schedule")
            print("\nTroubleshooting:")
            print(f"  1. Check your internet connection")
            print(f"  2. Visit {city_config['url']} in your browser")
            print(f"  3. Check data/last_fetch_{city_id}.json to see what was downloaded")

if __name__ == '__main__':
    main()