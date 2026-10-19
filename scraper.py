"""Fetch and normalize outage schedules from the OE_OUTAGE_DATA repository."""

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
KYIV_TIMEZONE = ZoneInfo("Europe/Kyiv")


class StaleScheduleError(ValueError):
    """Raised when the repository does not contain today's schedule."""

DATA_REPOSITORY = "https://github.com/yaroslav2901/OE_OUTAGE_DATA"
POWER_GROUPS = [f"{group}.{subgroup}" for group in range(1, 7) for subgroup in (1, 2)]
CITY_CONFIGS = {
    "lviv": {
        "name": "Львівська область",
        "api_url": "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Lvivoblenerho.json",
    },
    "ivano-frankivsk": {
        "name": "Франківська область",
        "api_url": "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Prykarpattiaoblenerho.json",
    },
}


class ScheduleScraper:
    def __init__(self, city="lviv", storage_path="data/schedules.json"):
        if city not in CITY_CONFIGS:
            raise ValueError(f"Unknown city: {city}. Available cities: {', '.join(CITY_CONFIGS)}")

        self.city = city
        self.config = CITY_CONFIGS[city]
        self.storage_path = storage_path
        self.stale_source = False
        self.source_date = None
        self.source_updated = None
        self.schedules = self._load_schedules()

    def _load_schedules(self):
        """Load previous schedules from storage."""
        try:
            with open(self.storage_path, "r", encoding="utf-8") as schedule_file:
                data = json.load(schedule_file)
                data.setdefault(self.city, {})
                return data
        except FileNotFoundError:
            return {self.city: {}}

    def _save_schedules(self):
        """Save schedules to storage."""
        directory = os.path.dirname(self.storage_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.storage_path, "w", encoding="utf-8") as schedule_file:
            json.dump(self.schedules, schedule_file, ensure_ascii=False, indent=2)

    def fetch_schedule(self):
        """Fetch the city's pre-structured JSON schedule."""
        try:
            response = requests.get(
                self.config["api_url"],
                headers={"Accept": "application/json", "User-Agent": "svitlo-bot"},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object from outage data repository")

            logger.info("Fetched %s schedule successfully", self.config["name"])
            return json.dumps(data, ensure_ascii=False)
        except (requests.RequestException, ValueError) as error:
            logger.error("Error fetching schedule from %s: %s", self.config["name"], error)
            return None

    def parse_schedule(self, json_content):
        """Normalize repository JSON to the schedule structure consumed by the bot."""
        if not json_content:
            return None

        self.stale_source = False
        self.source_date = None
        self.source_updated = None
        try:
            data = json.loads(json_content)
            return self._parse_repository_json(data)
        except StaleScheduleError as error:
            self.stale_source = True
            logger.warning(
                "Using no-outage default for stale %s schedule: %s",
                self.config["name"], error,
            )
            return self._build_no_outage_schedule()
        except (json.JSONDecodeError, TypeError, ValueError, KeyError, AttributeError) as error:
            logger.error("Error parsing %s schedule: %s", self.config["name"], error, exc_info=True)
            return None

    @staticmethod
    def _build_no_outage_schedule():
        """Create today's all-electricity schedule when the source is stale."""
        return {
            "timestamp": datetime.now(KYIV_TIMEZONE).isoformat(),
            "groups": {
                group: [{
                    "date": "Сьогодні (джерело застаріле)",
                    "schedule": "Відключень не заплановано",
                }]
                for group in POWER_GROUPS
            },
        }

    def _parse_repository_json(self, data):
        fact = data.get("fact")
        if not isinstance(fact, dict):
            raise ValueError("Missing fact object")

        fact_data = fact.get("data")
        if not isinstance(fact_data, dict) or not fact_data:
            raise ValueError("Missing fact.data schedules")

        preset = data.get("preset") or {}
        time_zone = preset.get("time_zone") or {}
        today_timestamp = fact.get("today")
        self.source_updated = fact.get("update") or data.get("lastUpdated")
        if today_timestamp is None:
            raise ValueError("Missing fact.today schedule date")

        today_key = str(today_timestamp)
        if today_key not in fact_data:
            raise ValueError("fact.today does not match any fact.data timestamp")

        try:
            source_date = datetime.fromtimestamp(int(today_timestamp), KYIV_TIMEZONE).date()
        except (OverflowError, OSError) as error:
            raise ValueError("Invalid fact.today timestamp") from error
        self.source_date = source_date.isoformat()
        current_date = datetime.now(KYIV_TIMEZONE).date()
        if source_date < current_date:
            raise StaleScheduleError(
                f"source date is {source_date.isoformat()}, current date is {current_date.isoformat()}"
            )
        if source_date > current_date:
            raise ValueError(
                f"source date {source_date.isoformat()} is ahead of current date {current_date.isoformat()}"
            )

        timestamps = sorted(fact_data, key=lambda value: int(value))
        timestamps.remove(today_key)
        timestamps.insert(0, today_key)

        schedule_data = {"timestamp": datetime.now().isoformat(), "groups": {}}
        for timestamp in timestamps:
            groups = fact_data[timestamp]
            if not isinstance(groups, dict):
                raise ValueError(f"Invalid schedule data for timestamp {timestamp}")
            timestamp_date = datetime.fromtimestamp(int(timestamp), KYIV_TIMEZONE).date()
            if timestamp_date == current_date:
                date_label = f'Сьогодні ({fact.get("update", "")})'
            elif timestamp_date == current_date + timedelta(days=1):
                date_label = "Завтра"
            else:
                logger.info("Skipping schedule timestamp %s outside today/tomorrow", timestamp)
                continue

            for group_key, hours in groups.items():
                if not group_key.startswith("GPV") or not isinstance(hours, dict):
                    continue
                group_number = group_key[3:]
                if not group_number:
                    continue
                schedule = self._build_schedule_from_hours(hours, time_zone)
                schedule_data["groups"].setdefault(group_number, []).append(
                    {"date": date_label, "schedule": schedule}
                )

        if not schedule_data["groups"]:
            raise ValueError("No GPV groups found in outage data")
        return schedule_data

    @staticmethod
    def _build_schedule_from_hours(hours, time_zone):
        """Convert hourly status values into merged outage periods."""
        outage_segments = []
        valid_statuses = {"yes", "no", "maybe", "first", "second"}

        for hour_number in range(1, 25):
            hour_key = str(hour_number)
            status = hours.get(hour_key, "yes")
            if status not in valid_statuses:
                raise ValueError(f"Unknown status {status!r} for hour {hour_key}")
            if status == "yes":
                continue

            hour_data = time_zone.get(hour_key, [None, f"{hour_number - 1:02d}:00", f"{hour_number:02d}:00"])
            if not isinstance(hour_data, (list, tuple)) or len(hour_data) < 3:
                raise ValueError(f"Invalid time zone entry for hour {hour_key}")
            start, end = hour_data[1], hour_data[2]
            start_hour = int(start.split(":")[0])

            if status in {"no", "maybe"}:
                outage_segments.append((start, end))
            elif status == "first":
                outage_segments.append((start, f"{start_hour:02d}:30"))
            else:  # second half of the hour
                outage_segments.append((f"{start_hour:02d}:30", end))

        if not outage_segments:
            return "Відключень не заплановано"

        merged_periods = []
        current_start, current_end = outage_segments[0]
        for segment_start, segment_end in outage_segments[1:]:
            if segment_start == current_end:
                current_end = segment_end
            else:
                merged_periods.append((current_start, current_end))
                current_start, current_end = segment_start, segment_end
        merged_periods.append((current_start, current_end))

        periods = [f"з {start} до {end}" for start, end in merged_periods]
        return "Відключення електроенергії: " + ", ".join(periods)

    def calculate_hash(self, data):
        """Calculate a stable hash from schedule content, ignoring timestamps."""
        if not data:
            return None
        relevant_data = {
            "groups": {
                group_id: [{"schedule": entry.get("schedule", "")} for entry in entries]
                for group_id, entries in data.get("groups", {}).items()
            }
        }
        json_str = json.dumps(relevant_data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(json_str.encode("utf-8")).hexdigest()

    def check_for_changes(self):
        """Fetch, parse, and persist this city's latest schedule."""
        json_content = self.fetch_schedule()
        if not json_content:
            logger.warning("Could not fetch schedule from %s repository", self.config["name"])
            return None

        debug_path = f"data/last_fetch_{self.city}.json"
        os.makedirs(os.path.dirname(debug_path), exist_ok=True)
        with open(debug_path, "w", encoding="utf-8") as debug_file:
            debug_file.write(json_content)

        new_schedule = self.parse_schedule(json_content)
        if not new_schedule:
            logger.warning("Could not parse %s schedule", self.config["name"])
            return None

        new_hash = self.calculate_hash(new_schedule)
        city_data = self.schedules.setdefault(self.city, {})
        old_hash = city_data.get("last_hash")
        result = {
            "changed": new_hash != old_hash,
            "new_schedule": new_schedule,
            "old_schedule": city_data.get("last_schedule"),
            "new_hash": new_hash,
            "old_hash": old_hash,
            "timestamp": datetime.now().isoformat(),
            "city": self.city,
        }

        if result["changed"]:
            logger.info("%s schedule has changed", self.config["name"])
            city_data["last_hash"] = new_hash
            city_data["last_schedule"] = new_schedule
        else:
            logger.info("No changes detected in %s schedule", self.config["name"])
        city_data["last_checked"] = datetime.now().isoformat()
        self._save_schedules()
        return result

    def get_group_schedule(self, group_id):
        """Get the cached schedule for a group."""
        city_data = self.schedules.get(self.city, {})
        return city_data.get("last_schedule", {}).get("groups", {}).get(group_id)


def main():
    """Fetch and report schedules for configured cities."""
    for city_id, city_config in CITY_CONFIGS.items():
        scraper = ScheduleScraper(city=city_id)
        result = scraper.check_for_changes()
        if not result:
            print(f"Failed to fetch or parse {city_config['name']}")
            continue
        print(f"{city_config['name']}: {len(result['new_schedule']['groups'])} groups")


if __name__ == "__main__":
    main()
