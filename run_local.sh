#!/bin/bash

# Local development script for Power Outage Bot

echo "🔌 Power Outage Bot - Local Setup"
echo "=================================="

# Check if .env file exists
if [ ! -f .env ]; then
    echo "Creating .env file..."
    cat > .env << EOF
# Telegram Bot Token from @BotFather
TELEGRAM_BOT_TOKEN=your_token_here

# Optional: Comma-separated list of allowed user IDs
# Leave empty to allow anyone
# Example: ALLOWED_USER_IDS=123456789,987654321
ALLOWED_USER_IDS=
EOF
    echo "⚠️  Please edit .env file and add your TELEGRAM_BOT_TOKEN"
    exit 1
fi

# Load environment variables
export $(cat .env | grep -v '^#' | xargs)

# Check if token is set
if [ "$TELEGRAM_BOT_TOKEN" = "your_token_here" ]; then
    echo "⚠️  Please set your TELEGRAM_BOT_TOKEN in .env file"
    exit 1
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -q -r requirements.txt

# Create data directory
mkdir -p data

echo ""
echo "✅ Setup complete!"
echo ""
echo "Choose an option:"
echo "1) Run bot (for user interaction)"
echo "2) Test scraper (check schedule)"
echo "3) Run notifier (check and notify)"
echo ""
read -p "Enter option (1-3): " option

case $option in
    1)
        echo "Starting bot..."
        python src/bot.py
        ;;
    2)
        echo "Testing scraper..."
        python src/scraper.py
        ;;
    3)
        echo "Running notifier..."
        python -m asyncio src/notifier.py
        ;;
    *)
        echo "Invalid option"
        exit 1
        ;;
esac