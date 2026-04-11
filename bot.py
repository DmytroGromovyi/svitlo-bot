    # Schedules
    c.execute('''
    # Schedules
    c.execute('''
        CREATE TABLE IF NOT EXISTS schedules (
            city TEXT,
            group_number TEXT,
            today_schedule TEXT,
            tomorrow_schedule TEXT,
            previous_today TEXT,
            previous_tomorrow TEXT,
            reference_date TEXT, -- Added for date context tracking
            schedule_hash TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (city, group_number)
        )
    ''')
            city TEXT,
            group_number TEXT,
            today_schedule TEXT,
            tomorrow_schedule TEXT,
            previous_today TEXT,
            previous_tomorrow TEXT,
            reference_date TEXT, -- Added for date context tracking
            schedule_hash TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (city, group_number)
        )
    ''')