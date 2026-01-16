# ⚡ Svitlo Bot - Power Outage Notification System

<div align="center">

![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![Telegram](https://img.shields.io/badge/telegram-bot-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Status](https://img.shields.io/badge/status-active-success.svg)

**Telegram bot that keeps Ukrainians informed about power outage schedules in Lviv region**

[Features](#-features) • [Architecture](#-architecture) • [Quick Start](#-quick-start) • [Deployment](#-deployment) • [Usage](#-usage)

</div>

---

## 📋 Table of Contents

- [About](#-about)
- [Features](#-features)
- [Architecture](#-architecture)
- [Tech Stack](#-tech-stack)
- [Quick Start](#-quick-start)
- [Deployment Guide](#-deployment-guide)
- [Configuration](#-configuration)
- [Usage](#-usage)
- [Development](#-development)
- [Contributing](#-contributing)
- [License](#-license)

---

## 🌟 About

**Svitlo Bot** (Світло Бот) is a Telegram bot designed to help residents of Lviv, Ukraine stay informed about planned power outages. The bot monitors the official schedule from Lviv Oblast Energy (Львівобленерго) and sends real-time notifications when schedules change.

### Why This Bot?

During power infrastructure challenges, staying informed about outage schedules is crucial. This bot:
- ⚡ **Monitors changes** in real-time (every 10 minutes)
- 📱 **Sends instant notifications** when your group's schedule updates
- 📊 **Shows clear schedules** with power ON and OFF times
- 🔔 **Provides today AND tomorrow** schedules
- 🆓 **Completely free** to use

---

## ✨ Features

### For Users

- 🤖 **Self-service registration** - Choose your power outage group (1.1 - 6.3)
- 📅 **Schedule viewing** - `/schedule` command shows current and next-day schedules
- 🔔 **Smart notifications** - Only receive updates when YOUR group's schedule changes
- 🟢🔴 **Clear formatting** - See exactly when power is ON and OFF
- 🇺🇦 **Ukrainian language** - Native language interface

### For Administrators

- 🎯 **User limit control** - Maximum 15 users (configurable)
- 📊 **SQLite storage** - Lightweight, persistent user database
- 🔒 **API authentication** - Secure endpoint with Bearer token
- 🔍 **Smart change detection** - Ignores timestamp changes, only alerts on real schedule updates
- 📈 **Comprehensive logging** - Track all operations

---

## 🏗️ Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         Telegram Users                           │
└────────────────┬────────────────────────────────────────────────┘
                 │
                 │ Commands: /start, /setgroup, /schedule
                 ↓
┌─────────────────────────────────────────────────────────────────┐
│                      Fly.io Application                          │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Bot Service (bot.py)                                     │  │
│  │  • Handles user commands                                 │  │
│  │  • Manages group preferences                             │  │
│  │  • SQLite database (/data/users.db)                      │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Flask API (bot.py)                                       │  │
│  │  • GET /api/users (protected)                            │  │
│  │  • GET /health (public)                                  │  │
│  └──────────────────────────────────────────────────────────┘  │
└────────────────┬────────────────────────────────────────────────┘
                 │
                 │ API call (every 10 min)
                 ↓
┌─────────────────────────────────────────────────────────────────┐
│                     GitHub Actions Cron                          │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  1. Fetch users via API → users.json                     │  │
│  │  2. Check schedule (scraper.py)                          │  │
│  │  3. Detect changes (smart hash)                          │  │
│  │  4. Send notifications (notifier.py)                     │  │
│  └──────────────────────────────────────────────────────────┘  │
└────────────────┬────────────────────────────────────────────────┘
                 │
                 │ Scrape schedule
                 ↓
┌─────────────────────────────────────────────────────────────────┐
│           Lviv Oblast Energy API                                 │
│           https://api.loe.lviv.ua/api/menus                     │
└─────────────────────────────────────────────────────────────────┘
```

### Component Breakdown

#### 1. **Bot Service** (`bot.py`)
- **Technology**: python-telegram-bot + Flask
- **Runs on**: Fly.io (24/7)
- **Responsibilities**:
  - Handle Telegram commands (`/start`, `/setgroup`, `/schedule`)
  - Store user preferences in SQLite
  - Expose API endpoint for GitHub Actions
  
#### 2. **Scheduler** (GitHub Actions)
- **Frequency**: Every 10 minutes
- **Responsibilities**:
  - Fetch users from Fly.io API
  - Check schedule from Lviv Oblast Energy
  - Compare with previous schedule
  - Send notifications on changes

#### 3. **Scraper** (`scraper.py`)
- **Data Source**: Lviv Oblast Energy Hydra API
- **Responsibilities**:
  - Fetch current schedule
  - Parse HTML content
  - Extract group schedules
  - Calculate smart hash (ignores timestamps)

#### 4. **Notifier** (`notifier.py`)
- **Responsibilities**:
  - Load users from JSON
  - Format messages with ON/OFF times
  - Send targeted notifications
  - Rate limiting (0.5s between messages)

---

## 🛠️ Tech Stack

### Backend
- **Python 3.11+** - Core language
- **python-telegram-bot 21.7** - Telegram Bot API wrapper
- **Flask** - API server
- **SQLite** - User database
- **BeautifulSoup4** - HTML parsing
- **aiohttp** - Async HTTP client

### Infrastructure
- **Fly.io** - Bot hosting (free tier)
- **GitHub Actions** - Cron scheduler (free tier)
- **Telegram Bot API** - Messaging platform

### Tools
- **dotenv** - Environment configuration
- **logging** - Application logging

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Telegram account
- GitHub account (for deployment)
- Fly.io account (for hosting)

### Local Development

1. **Clone the repository**
   ```bash
   git clone https://github.com/DmytroGromovyi/svitlo-bot.git
   cd svitlo-bot
   ```

2. **Create virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables**
   
   Create `.env` file:
   ```env
   TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
   API_SECRET=your_random_secret_key
   PORT=8080
   ```

5. **Get your Telegram Bot Token**
   - Open Telegram
   - Message [@BotFather](https://t.me/botfather)
   - Send `/newbot`
   - Follow instructions
   - Copy the token

6. **Run the bot locally**
   ```bash
   python bot.py
   ```

7. **Test in Telegram**
   - Find your bot
   - Send `/start`
   - Test `/setgroup` and `/schedule`

---

## 📦 Deployment Guide

### Step 1: Deploy Bot to Fly.io

1. **Install Fly CLI**
   ```bash
   # macOS
   brew install flyctl
   
   # Linux
   curl -L https://fly.io/install.sh | sh
   
   # Windows
   iwr https://fly.io/install.ps1 -useb | iex
   ```

2. **Sign up and login**
   ```bash
   flyctl auth signup
   flyctl auth login
   ```

3. **Create app**
   ```bash
   flyctl launch --no-deploy
   
   # Answer prompts:
   # App name: svitlo-bot
   # Region: Amsterdam (ams) - closest to Ukraine
   # PostgreSQL: No
   # Redis: No
   ```

4. **Create persistent volume**
   ```bash
   flyctl volumes create svitlo_data --region ams --size 1
   ```

5. **Set secrets**
   ```bash
   flyctl secrets set TELEGRAM_BOT_TOKEN="your_token"
   flyctl secrets set API_SECRET="your_random_secret"
   ```

6. **Deploy**
   ```bash
   flyctl deploy
   ```

7. **Verify deployment**
   ```bash
   flyctl status
   flyctl logs
   
   # Test API
   curl https://your-app.fly.dev/health
   ```

### Step 2: Configure GitHub Actions

1. **Fork this repository**

2. **Set GitHub Secrets**
   
   Go to: Repository → Settings → Secrets and variables → Actions
   
   Add secrets:
   - `TELEGRAM_BOT_TOKEN` - Your bot token
   - `API_URL` - Your Fly.io app URL (e.g., `https://svitlo-bot.fly.dev`)
   - `API_SECRET` - Same secret as in Fly.io

3. **Enable GitHub Actions**
   
   Go to: Actions tab → Enable workflows

4. **Verify workflow runs**
   
   Go to: Actions tab → "Check Power Schedule"
   
   Click "Run workflow" to test manually

### Step 3: Verify End-to-End

1. **Register with bot**
   ```
   /start
   /setgroup
   [Select your group]
   ```

2. **Check schedule**
   ```
   /schedule
   ```

3. **Wait for notification**
   - GitHub Actions runs every 10 minutes
   - You'll receive notification when schedule changes

---

## ⚙️ Configuration

### Environment Variables

#### Bot Service (Fly.io)

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from BotFather | `123456:ABC-DEF...` |
| `API_SECRET` | ✅ | Secret for API authentication | `random-secret-key` |
| `PORT` | ⚠️ | Server port (auto-set by Fly.io) | `8080` |

#### GitHub Actions

| Secret | Required | Description |
|--------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token |
| `API_URL` | ✅ | Fly.io app URL |
| `API_SECRET` | ✅ | API authentication secret |

### Application Settings

Edit `bot.py` to customize:

```python
# Maximum users allowed
MAX_USERS = 15

# Database path
DB_PATH = '/data/users.db'

# Available groups
# 1.1 - 6.3 (Lviv Oblast Energy groups)
```

Edit `.github/workflows/check_schedule.yml` to change frequency:

```yaml
schedule:
  - cron: '*/10 * * * *'  # Every 10 minutes
```

---

## 📱 Usage

### User Commands

| Command | Description | Example |
|---------|-------------|---------|
| `/start` | Start bot, see welcome message | `/start` |
| `/setgroup` | Choose your outage group | `/setgroup` → Select 1.1 |
| `/schedule` | View current schedule | `/schedule` |
| `/mygroup` | Show your selected group | `/mygroup` |
| `/stop` | Unsubscribe from notifications | `/stop` |
| `/help` | Show available commands | `/help` |

### Example Interaction

```
User: /start
Bot: Вітаю! 👋
     Я допоможу вам отримувати сповіщення про зміни в графіку відключень.
     Оберіть вашу групу: /setgroup

User: /setgroup
Bot: Оберіть вашу групу відключень:
     [Keyboard with 1.1, 1.2, 1.3, etc.]

User: [Selects 1.1]
Bot: ✅ Групу 1.1 збережено!
     Ви будете отримувати сповіщення про зміни в графіку.

User: /schedule
Bot: 📋 Графік для групи 1.1
     
     📅 Сьогодні
     🟢 Є світло: з 00:00 до 03:00, з 06:30 до 09:00
     🔴 Немає світла: з 03:00 до 06:30, з 09:00 до 14:00
     
     📅 Завтра
     🟢 Є світло: з 00:00 до 02:00, з 08:00 до 12:00
     🔴 Немає світла: з 02:00 до 08:00, з 12:00 до 24:00
     
     ℹ️ Графік може змінюватися протягом дня.
```

### Notification Example

When schedule changes, users receive:

```
⚡️ Оновлення графіку відключень!

Група: 1.1

📅 Сьогодні
🟢 Є світло: з 00:00 до 03:00, з 06:30 до 09:00, з 14:00 до 17:00
🔴 Немає світла: з 03:00 до 06:30, з 09:00 до 14:00, з 17:00 до 22:30

📅 Завтра
🟢 Є світло: з 00:00 до 02:00, з 08:00 до 12:00
🔴 Немає світла: з 02:00 до 08:00, з 12:00 до 24:00

ℹ️ Графік може змінюватися протягом дня.
```

---

## 🔧 Development

### Project Structure

```
svitlo-bot/
├── .github/
│   └── workflows/
│       └── check_schedule.yml    # GitHub Actions cron job
├── data/
│   ├── last_fetch.json           # Debug: last API response
│   ├── schedules.json            # Schedule history
│   └── users.db                  # SQLite database (Fly.io)
├── bot.py                        # Main bot + API server
├── scraper.py                    # Schedule scraper
├── notifier.py                   # Notification sender
├── requirements.txt              # Python dependencies
├── Dockerfile                    # Container definition
├── fly.toml                      # Fly.io configuration
├── .env.example                  # Environment template
├── .gitignore                    # Git ignore rules
└── README.md                     # This file
```

### Running Tests

```bash
# Test scraper
python scraper.py

# Test notifier (requires users.json)
python notifier.py

# Test bot locally
python bot.py
```

### Debugging

**View Fly.io logs:**
```bash
flyctl logs
flyctl logs --follow
```

**View GitHub Actions logs:**
- Go to Actions tab
- Click on workflow run
- Expand steps to see details

**Check database:**
```bash
flyctl ssh console
sqlite3 /data/users.db
SELECT * FROM users;
.exit
```

**Test API endpoint:**
```bash
# Health check (public)
curl https://your-app.fly.dev/health

# Users endpoint (protected)
curl -H "Authorization: Bearer YOUR_SECRET" \
     https://your-app.fly.dev/api/users
```

### Common Issues

**Bot not responding?**
- Check Fly.io status: `flyctl status`
- View logs: `flyctl logs`
- Verify secrets: `flyctl secrets list`

**No notifications?**
- Check GitHub Actions is running
- Verify API_URL and API_SECRET in GitHub
- Check workflow logs for errors

**Hash keeps changing?**
- Verify `calculate_hash` in `scraper.py` ignores timestamps
- Check `data/schedules.json` for changes

---

## 💰 Cost Breakdown

### Free Tier (Perfect for this project!)

| Service | Plan | Cost | Usage |
|---------|------|------|-------|
| **Fly.io** | Free Tier | $0/month | Bot hosting, 256MB RAM |
| **GitHub Actions** | Free Tier | $0/month | 2,000 min/month (~300 used) |
| **Telegram Bot API** | Free | $0/month | Unlimited messages |
| **SQLite** | Local | $0/month | Included |
| **TOTAL** | | **$0/month** | ✅ |

### Estimated Usage

- **Fly.io**: ~10 hours/day active (auto-suspend)
- **GitHub Actions**: ~720 minutes/month (10-minute cron)
- **API calls**: ~4,320/month (144/day)
- **Telegram messages**: Variable (depends on schedule changes)

---

## 🤝 Contributing

Contributions are welcome! Here's how you can help:

### Ways to Contribute

- 🐛 **Report bugs** - Open an issue
- 💡 **Suggest features** - Share your ideas
- 📝 **Improve docs** - Fix typos, add examples
- 🔧 **Submit PRs** - Fix bugs, add features

### Development Process

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Make your changes
4. Test thoroughly
5. Commit: `git commit -m 'Add amazing feature'`
6. Push: `git push origin feature/amazing-feature`
7. Open a Pull Request

### Code Style

- Follow PEP 8
- Use meaningful variable names
- Add comments for complex logic
- Update README if adding features

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- **Lviv Oblast Energy** - For providing the public API
- **Telegram** - For the excellent Bot API
- **Fly.io** - For generous free tier
- **GitHub** - For Actions and hosting
- **Ukrainian people** - For resilience and inspiration

---
<div align="center">

**Made with ❤️ for Ukraine 🇺🇦**

[⬆ Back to Top](#-svitlo-bot---power-outage-notification-system)

</div>