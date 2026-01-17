#!/bin/bash
# Cron job script for schedule checking
# This runs the notifier every 10 minutes

while true; do
    python notifier.py
    sleep 600  # Wait 10 minutes (600 seconds)
done
