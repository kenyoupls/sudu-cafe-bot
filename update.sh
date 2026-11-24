#!/bin/bash
# Sudu Cafe Bot — update by hand (normally auto_update.sh does this every minute).
# Usage: cd ~/cafe_bot && ./update.sh
set -e
echo "=== Sudu Bot Update ==="
git pull origin main
rm -rf __pycache__
source venv/bin/activate 2>/dev/null || true
pip install -q -r requirements.txt
python -m py_compile *.py && echo "Syntax OK"
sudo systemctl restart cafebot
sleep 2
sudo systemctl status cafebot --no-pager | head -5
echo "=== Done. Live logs: sudo journalctl -u cafebot -f ==="
