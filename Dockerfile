FROM python:3.11-slim

WORKDIR /app

# Include a Unicode font for Ukrainian labels in the generated SVG schedule cards.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core gosu \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY bot.py .
COPY scraper.py .
COPY schedule_card.py .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint

# Prepare writable paths. The entrypoint fixes ownership of the mounted Fly
# volume at runtime, then drops privileges before starting the application.
RUN groupadd --system --gid 10001 bot \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin bot \
    && mkdir -p data /data \
    && chown -R 10001:10001 /app /data \
    && chmod 755 /usr/local/bin/docker-entrypoint

ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]

CMD ["python", "bot.py"]
