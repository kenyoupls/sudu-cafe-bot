#!/bin/bash
# Sudu Cafe Bot — One-command update from GitHub
# Usage: cd ~/cafe_bot && ./update.sh
#
set -e

echo "=== Sudu Bot Update ==="

# 1. Pull latest code
echo "Pulling from GitHub..."
git pull origin main

# 2. Clear Python cache
rm -rf __pycache__

# 3. Syntax check
echo ""
echo "Syntax check..."
source venv/bin/activate 2>/dev/null || true
python -m py_compile bot.py && echo "  bot.py OK"
python -m py_compile ai_chat.py && echo "  ai_chat.py OK"
python -m py_compile google_integration.py && echo "  google_integration.py OK"
python -m py_compile storage.py && echo "  storage.py OK"

# 4. Restart
echo ""
echo "Restarting bot..."
sudo systemctl restart cafebot
sleep 2
sudo systemctl status cafebot --no-pager | head -5

echo ""
echo "=== Update complete ==="
echo "Check logs: sudo journalctl -u cafebot -f"
