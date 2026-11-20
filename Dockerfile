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

# Create writable data paths and drop root privileges for runtime.
RUN mkdir -p data /data && chown -R 10001:10001 /app /data

# Fly.io assigns the mounted volume to the image's configured user.
USER 10001:10001

# Run bot
CMD ["python", "bot.py"]
