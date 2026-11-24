#!/bin/bash
# Sudu Cafe Bot — auto-update from GitHub.
# Runs from cron every minute. Does nothing unless GitHub's main branch changed.
# Logs to /var/log/cafebot-update.log
#
BOT_DIR="$HOME/cafe_bot"
LOG="/var/log/cafebot-update.log"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') — $*" >> "$LOG"; }

cd "$BOT_DIR" || exit 1

git fetch origin main --quiet 2>/dev/null
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
[ "$LOCAL" = "$REMOTE" ] && exit 0

log "New code detected, updating..."
OLD_REQS=$(md5sum requirements.txt 2>/dev/null)
git pull origin main --quiet 2>>"$LOG"
rm -rf __pycache__
source venv/bin/activate 2>/dev/null || true

# Install new libraries only if requirements.txt changed
if [ "$OLD_REQS" != "$(md5sum requirements.txt 2>/dev/null)" ]; then
    log "requirements.txt changed, installing..."
    pip install -q -r requirements.txt >>"$LOG" 2>&1
fi

# Check every Python file compiles before restarting
if python -m py_compile *.py; then
    log "Syntax OK, restarting bot..."
    sudo systemctl restart cafebot
    log "Bot restarted"
else
    log "SYNTAX ERROR! Bot NOT restarted. Fix the code."
fi
