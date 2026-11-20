FROM python:3.11-slim

WORKDIR /app

# Include a Unicode font for Ukrainian labels in the generated SVG schedule cards.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY bot.py .
COPY scraper.py .
COPY schedule_card.py .

# Create data directory
RUN mkdir -p data

# Run bot
CMD ["python", "bot.py"]
