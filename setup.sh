#!/bin/bash
# ─── Café Manager Bot — GCP e2-micro Setup Script ─────────
# Run this ONCE on your new VM to install everything.

set -e

echo "☕ Setting up Café Manager Bot..."

# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.11+ and pip
sudo apt install -y python3 python3-pip python3-venv git

# Create bot directory
mkdir -p ~/cafe_bot
cd ~/cafe_bot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Create data directory
mkdir -p data/memory

echo ""
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit your .env file:  nano ~/cafe_bot/.env"
echo "  2. Start the bot:        ~/cafe_bot/start.sh"
echo "  3. Set up auto-restart:  ~/cafe_bot/install_service.sh"
echo ""
