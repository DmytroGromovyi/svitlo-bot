# Quick Start Guide - Fly.io Deployment

## Summary of Changes

1. **Webhook Mode**: Bot now uses webhooks instead of long polling
2. **Cron on Fly.io**: Schedule checking runs on Fly.io instead of GitHub Actions
3. **Direct Database Access**: Notifier uses UserStorage directly (no API needed)

## Required Environment Variables

Set these in Fly.io:

```bash
flyctl secrets set TELEGRAM_BOT_TOKEN="your_bot_token"
flyctl secrets set WEBHOOK_URL="https://your-app-name.fly.dev"
flyctl secrets set API_SECRET="your_random_secret"
```

## Deployment Steps

### 1. Deploy Bot Service

```bash
flyctl deploy
```

### 2. Setup Webhook

The webhook is automatically configured on startup if `WEBHOOK_URL` is set.

To verify:
```bash
flyctl ssh console
python3 -c "
import os
from telegram import Bot
import asyncio
async def check():
    bot = Bot(token=os.getenv('TELEGRAM_BOT_TOKEN'))
    info = await bot.get_webhook_info()
    print(f'Webhook: {info.url}')
asyncio.run(check())
"
```

### 3. Setup Cron Machine

Create a separate machine for the cron job:

```bash
# Create cron machine
flyctl machine run \
  --name cron-worker \
  --env "TELEGRAM_BOT_TOKEN=$(flyctl secrets list | grep TELEGRAM_BOT_TOKEN | awk '{print $2}')" \
  python3 -c "
import time
import subprocess
import os

while True:
    subprocess.run(['python3', 'notifier.py'])
    time.sleep(600)  # 10 minutes
"
```

Or use the provided script:

```bash
# Make script executable and copy to machine
flyctl ssh sftp shell
put run_cron.sh /app/run_cron.sh
chmod +x /app/run_cron.sh

# Run it
flyctl machine run --name cron-worker bash /app/run_cron.sh
```

## Testing

1. **Test Bot**: Send `/start` to your bot in Telegram
2. **Test Webhook**: Check logs for webhook requests
3. **Test Cron**: Check cron machine logs

## Troubleshooting

- **Bot not responding?** Check `flyctl logs` and verify webhook is set
- **Cron not running?** Check cron machine status: `flyctl machine list`
- **Webhook errors?** Verify `WEBHOOK_URL` matches your app URL exactly

See `DEPLOYMENT.md` for detailed instructions.
