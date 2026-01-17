# Deployment Instructions

This guide will help you deploy the Svitlo Bot to Fly.io with webhook support and a cron job for schedule checking.

## Prerequisites

- Fly.io account (sign up at https://fly.io)
- Fly CLI installed (`flyctl`)
- Telegram Bot Token from [@BotFather](https://t.me/botfather)

## Step 1: Initial Setup

### 1.1 Install Fly CLI

```bash
# macOS
brew install flyctl

# Linux
curl -L https://fly.io/install.sh | sh

# Windows (PowerShell)
iwr https://fly.io/install.ps1 -useb | iex
```

### 1.2 Login to Fly.io

```bash
flyctl auth login
```

### 1.3 Create App (if not already created)

```bash
flyctl launch --no-deploy
# Answer prompts:
# - App name: svitlo-bot (or your preferred name)
# - Region: ams (Amsterdam - closest to Ukraine)
# - PostgreSQL: No
# - Redis: No
```

### 1.4 Create Persistent Volume

```bash
flyctl volumes create svitlo_data --region ams --size 1
```

## Step 2: Environment Variables

Set the following secrets in Fly.io:

```bash
# Required: Telegram Bot Token
flyctl secrets set TELEGRAM_BOT_TOKEN="your_bot_token_here"

# Required: API Secret for /api/users endpoint
flyctl secrets set API_SECRET="your_random_secret_key_here"

# Required: Webhook URL (your Fly.io app URL)
flyctl secrets set WEBHOOK_URL="https://your-app-name.fly.dev"

# Optional: Port (defaults to 8080)
flyctl secrets set PORT="8080"
```

**Important:** Replace `your-app-name` with your actual Fly.io app name.

## Step 3: Deploy the Bot

### 3.1 Deploy Main Bot Service

```bash
flyctl deploy
```

This will deploy the bot service that handles:
- Telegram webhook endpoint (`/webhook`)
- Flask API endpoints (`/api/users`, `/health`)
- User database management

### 3.2 Verify Deployment

```bash
# Check status
flyctl status

# View logs
flyctl logs

# Test health endpoint
curl https://your-app-name.fly.dev/health
```

## Step 4: Setup Webhook

The webhook is automatically configured when the bot starts (if `WEBHOOK_URL` is set).

To manually set/update the webhook:

```bash
# SSH into the machine
flyctl ssh console

# Run Python to set webhook
python3 -c "
import os
from telegram import Bot
import asyncio

async def set_webhook():
    bot = Bot(token=os.getenv('TELEGRAM_BOT_TOKEN'))
    await bot.set_webhook(url='https://your-app-name.fly.dev/webhook')
    info = await bot.get_webhook_info()
    print(f'Webhook set: {info.url}')

asyncio.run(set_webhook())
"
```

Or verify webhook status:

```bash
flyctl ssh console
python3 -c "
import os
from telegram import Bot
import asyncio

async def check():
    bot = Bot(token=os.getenv('TELEGRAM_BOT_TOKEN'))
    info = await bot.get_webhook_info()
    print(f'Webhook URL: {info.url}')
    print(f'Pending updates: {info.pending_update_count}')

asyncio.run(check())
"
```

## Step 5: Setup Cron Job for Schedule Checking

Fly.io doesn't have built-in cron, so we'll create a separate machine that runs continuously and executes the notifier on a schedule.

### 5.1 Create Cron Machine

Create a new file `fly.cron.toml`:

```toml
app = "your-app-name"  # Same as your main app
primary_region = "ams"

[build]

[env]
  PORT = "8080"

[[vm]]
  memory = "256mb"
  cpu_kind = "shared"
  cpus = 1

[mounts]
  source = "svitlo_data"
  destination = "/data"
```

### 5.2 Deploy Cron Machine

```bash
# Create a machine for the cron job
flyctl machine create --config fly.cron.toml --name cron-worker

# Set the same secrets
flyctl secrets set TELEGRAM_BOT_TOKEN="your_bot_token_here" --app your-app-name

# Run the cron script
flyctl ssh console -a your-app-name -m cron-worker
# Then inside:
python3 run_cron.sh
```

**Alternative: Use a scheduled script**

Create a script that runs the notifier and use Fly.io's scheduled machines:

```bash
# Create a machine that runs the notifier every 10 minutes
flyctl machine run --config fly.cron.toml \
  --name cron-worker \
  --env "CRON_INTERVAL=600" \
  python3 -c "
import time
import subprocess
import os

interval = int(os.getenv('CRON_INTERVAL', 600))  # 10 minutes default

while True:
    subprocess.run(['python3', 'notifier.py'])
    time.sleep(interval)
"
```

### 5.3 Recommended: Use Systemd Timer (Linux)

If you want a proper cron-like setup, you can use a systemd timer inside the machine:

1. SSH into the cron machine
2. Create a systemd service and timer
3. Enable the timer

Or use a simpler approach: Create a machine that runs a loop:

```bash
# Deploy cron machine with run_cron.sh
flyctl machine run --config fly.cron.toml \
  --name cron-worker \
  bash -c "chmod +x run_cron.sh && ./run_cron.sh"
```

## Step 6: Verify Everything Works

### 6.1 Test Bot Commands

1. Open Telegram
2. Find your bot
3. Send `/start`
4. Try `/setgroup` and select a group
5. Try `/schedule` to see the schedule

### 6.2 Test Webhook

The webhook should work automatically. If messages aren't being received:

```bash
# Check webhook status
flyctl ssh console
python3 -c "
import os
from telegram import Bot
import asyncio

async def check():
    bot = Bot(token=os.getenv('TELEGRAM_BOT_TOKEN'))
    info = await bot.get_webhook_info()
    print(f'Webhook: {info.url}')
    print(f'Pending: {info.pending_update_count}')
    if info.last_error_date:
        print(f'Last error: {info.last_error_message}')

asyncio.run(check())
"
```

### 6.3 Test Cron Job

```bash
# Check cron machine logs
flyctl logs --app your-app-name --machine cron-worker

# Or SSH and run manually
flyctl ssh console -a your-app-name -m cron-worker
python3 notifier.py
```

## Step 7: Monitoring

### View Logs

```bash
# Bot service logs
flyctl logs --app your-app-name

# Cron machine logs
flyctl logs --app your-app-name --machine cron-worker

# Follow logs in real-time
flyctl logs --app your-app-name --follow
```

### Check Machine Status

```bash
flyctl status --app your-app-name
flyctl machine list --app your-app-name
```

## Troubleshooting

### Bot Not Responding

1. **Check webhook is set:**
   ```bash
   flyctl ssh console
   # Run webhook check script (see Step 4)
   ```

2. **Check logs:**
   ```bash
   flyctl logs
   ```

3. **Verify secrets:**
   ```bash
   flyctl secrets list
   ```

### Cron Not Running

1. **Check cron machine is running:**
   ```bash
   flyctl machine list
   ```

2. **Check cron machine logs:**
   ```bash
   flyctl logs --machine cron-worker
   ```

3. **Manually test notifier:**
   ```bash
   flyctl ssh console -m cron-worker
   python3 notifier.py
   ```

### Webhook Not Receiving Updates

1. **Verify webhook URL is correct:**
   - Must be HTTPS
   - Must be publicly accessible
   - Must match your Fly.io app URL

2. **Check for errors:**
   ```bash
   flyctl logs | grep webhook
   ```

3. **Reset webhook:**
   ```bash
   # Delete webhook (falls back to polling temporarily)
   flyctl ssh console
   python3 -c "
   import os
   from telegram import Bot
   import asyncio
   async def delete():
       bot = Bot(token=os.getenv('TELEGRAM_BOT_TOKEN'))
       await bot.delete_webhook()
   asyncio.run(delete())
   "
   # Then redeploy to set it again
   ```

## Environment Variables Summary

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from BotFather | `123456:ABC-DEF...` |
| `WEBHOOK_URL` | ✅ | Your Fly.io app URL | `https://svitlo-bot.fly.dev` |
| `API_SECRET` | ✅ | Secret for API auth | `random-secret-key` |
| `PORT` | ⚠️ | Server port (auto-set) | `8080` |

## Cost Estimate

- **Bot Service**: Free tier (256MB RAM, auto-suspend)
- **Cron Machine**: Free tier (256MB RAM, runs continuously)
- **Volume**: Free tier (1GB included)
- **Total**: **$0/month** (within free tier limits)

## Next Steps

1. Monitor logs regularly
2. Set up alerts (optional)
3. Scale if needed (unlikely for small user base)
4. Update schedule checking frequency if needed (edit `run_cron.sh`)

## Support

If you encounter issues:
1. Check logs: `flyctl logs`
2. Verify secrets: `flyctl secrets list`
3. Test endpoints: `curl https://your-app.fly.dev/health`
4. Check webhook: Use the verification scripts above
