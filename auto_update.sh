#!/bin/bash
# Sudu Cafe Bot — Auto-update from GitHub
# Runs via cron every minute. Pulls only if there are new commits.
# Logs to /var/log/cafebot-update.log
#
BOT_DIR="$HOME/cafe_bot"
LOG="/var/log/cafebot-update.log"

cd "$BOT_DIR" || exit 1

# Fetch latest from GitHub (quiet)
git fetch origin main --quiet 2>/dev/null

# Check if local is behind remote
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    # Already up to date — do nothing
    exit 0
fi

# New code available — update
echo "$(date '+%Y-%m-%d %H:%M:%S') — New code detected, updating..." >> "$LOG"

git pull origin main --quiet 2>>"$LOG"
rm -rf __pycache__

# Syntax check before restarting
source venv/bin/activate 2>/dev/null || true
if python -m py_compile bot.py && python -m py_compile ai_chat.py && python -m py_compile google_integration.py && python -m py_compile storage.py; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') — Syntax OK, restarting bot..." >> "$LOG"
    sudo systemctl restart cafebot
    echo "$(date '+%Y-%m-%d %H:%M:%S') — Bot restarted successfully" >> "$LOG"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') — SYNTAX ERROR! Bot NOT restarted. Fix the code." >> "$LOG"
fi
