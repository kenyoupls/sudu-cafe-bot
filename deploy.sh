#!/bin/bash
# Sudu Cafe Bot — Deploy Script
# Run this on the VM after uploading cafe_manager_bot.tar.gz
#
# Usage:
#   cd ~/cafe_manager_bot_pkg
#   chmod +x deploy.sh
#   ./deploy.sh
#
set -e

BOT_DIR="$HOME/cafe_bot"
BACKUP_DIR="$HOME/cafe_bot_backup_$(date +%Y%m%d_%H%M%S)"

echo "=== Sudu Cafe Bot Deploy ==="

# 1. Stop the bot
echo "Stopping bot..."
sudo systemctl stop cafebot || true

# 2. Backup current files (just in case — keep only last 2 backups)
echo "Backing up to $BACKUP_DIR..."
cp -r "$BOT_DIR" "$BACKUP_DIR"

# Delete old backups, keep only the 2 newest
echo "Cleaning old backups..."
ls -dt "$HOME"/cafe_bot_backup_* 2>/dev/null | tail -n +3 | xargs rm -rf 2>/dev/null || true

# 3. Clear ALL old source files and cache from bot dir
echo "Clearing old files..."
rm -f "$BOT_DIR"/*.py
rm -f "$BOT_DIR"/*.sh
rm -rf "$BOT_DIR/__pycache__"

# 4. Copy new files from this package
echo "Deploying new files..."
for f in *.py *.sh *.txt; do
    if [ -f "$f" ]; then
        cp -f "$f" "$BOT_DIR/$f"
        echo "  Copied: $f"
    fi
done

# 5. Verify key files exist
echo ""
echo "Verifying..."
for f in bot.py google_integration.py storage.py ai_chat.py config.py; do
    if [ -f "$BOT_DIR/$f" ]; then
        echo "  OK: $f"
    else
        echo "  MISSING: $f — deploy may be incomplete!"
    fi
done

# 6. Quick syntax check
echo ""
echo "Syntax check..."
cd "$BOT_DIR"
source venv/bin/activate 2>/dev/null || true
python -m py_compile bot.py && echo "  bot.py OK"
python -m py_compile ai_chat.py && echo "  ai_chat.py OK"
python -m py_compile google_integration.py && echo "  google_integration.py OK"
python -m py_compile storage.py && echo "  storage.py OK"

# 7. Restart
echo ""
echo "Restarting bot..."
sudo systemctl start cafebot
sleep 2
sudo systemctl status cafebot --no-pager | head -5

# 8. Clean up deploy package and tar from home
echo ""
echo "Cleaning up..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$SCRIPT_DIR" != "$BOT_DIR" ]; then
    rm -rf "$SCRIPT_DIR"
fi
rm -f "$HOME"/cafe_manager_bot.tar*.gz

echo ""
echo "=== Deploy complete ==="
echo "Check logs: sudo journalctl -u cafebot -f"
