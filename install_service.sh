#!/bin/bash
# ─── Install as systemd service (auto-start on boot + auto-restart) ───
set -e

SERVICE_FILE=/etc/systemd/system/cafebot.service

sudo tee $SERVICE_FILE > /dev/null << 'EOF'
[Unit]
Description=Cafe Manager Telegram Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=/home/$USER/cafe_bot
ExecStart=/home/$USER/cafe_bot/venv/bin/python3 /home/$USER/cafe_bot/bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# Replace $USER with actual username
sudo sed -i "s/\$USER/$USER/g" $SERVICE_FILE

sudo systemctl daemon-reload
sudo systemctl enable cafebot
sudo systemctl start cafebot

echo ""
echo "✅ Bot installed as system service!"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status cafebot    — check if running"
echo "  sudo systemctl restart cafebot   — restart bot"
echo "  sudo systemctl stop cafebot      — stop bot"
echo "  sudo journalctl -u cafebot -f    — view live logs"
echo ""
