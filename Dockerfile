FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Git
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Copy bot code
COPY bot.py .
COPY scraper.py .

# Create data directory
RUN mkdir -p data

# Run bot
CMD ["python", "bot.py"]