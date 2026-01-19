# ⚡ Svitlo Bot

A Telegram bot that monitors power outage schedules in Lviv, Ukraine and sends instant notifications when schedules change. Because staying in the dark about the power schedule is worse than the actual blackouts.

## What it does

Svitlo Bot scrapes power outage schedules from [poweron.loe.lviv.ua](https://poweron.loe.lviv.ua/), monitors for changes, and notifies subscribed users immediately. It shows detailed schedules with power ON/OFF times, duration calculations, and highlights what changed.

## Features

- 🔔 Real-time notifications when schedules change
- ⚡ Smart diff view showing added/removed time slots
- ⏱ Automatic calculation of total outage duration
- 📊 Support for 18 different power groups (1.1-6.3)
- 🗄️ SQLite database for user management
- 🔄 Automatic schedule checking every 5 minutes
- 🌐 Webhook-based Telegram integration

## Quick Start

### Prerequisites
- Python 3.8+
- Telegram Bot Token (get it from [@BotFather](https://t.me/botfather))
- A server or hosting platform with webhook support

### Environment Variables

```bash
TELEGRAM_BOT_TOKEN=your_bot_token_here
API_SECRET=your_secret_for_api_auth
WEBHOOK_URL=https://your-domain.com
PORT=8080
```

### Local Development

1. Clone the repository:
```bash
git clone https://github.com/DmytroGromovyi/svitlo-bot.git
cd svitlo-bot
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Set up environment variables (create `.env` file):
```bash
TELEGRAM_BOT_TOKEN=your_token
API_SECRET=any_random_string
WEBHOOK_URL=http://localhost:8080
PORT=8080
```

4. Run locally:
```bash
./run_local.sh
```

### Docker Deployment

```bash
docker build -t svitlo-bot .
docker run -d \
  -e TELEGRAM_BOT_TOKEN=your_token \
  -e API_SECRET=your_secret \
  -e WEBHOOK_URL=https://your-domain.com \
  -e PORT=8080 \
  -v ./data:/data \
  -p 8080:8080 \
  svitlo-bot
```

## Bot Commands

- `/start` - Start the bot
- `/setgroup` - Choose your power group
- `/schedule` - View current schedule
- `/mygroup` - Check your selected group
- `/stop` - Unsubscribe from notifications

## API Endpoints

- `GET /health` - Health check
- `GET /api/users` - List all users (requires `Authorization: Bearer API_SECRET`)
- `POST /webhook` - Telegram webhook endpoint

## Architecture

- **bot.py** - Main bot logic, Telegram handlers, notification system
- **scraper.py** - Schedule scraper for poweron.loe.lviv.ua
- **SQLite DB** - User subscriptions and schedule cache
- **Flask** - Webhook receiver and API endpoints

## Built with

- Python 3
- python-telegram-bot
- Flask
- BeautifulSoup4
- SQLite3

## Contributing

Found a bug or have a feature idea? Open an issue or submit a PR!

---

Made with 💙💛 for Lviv, Ukraine