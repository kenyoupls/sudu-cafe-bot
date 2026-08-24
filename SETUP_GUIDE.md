# ☕ Café Manager Bot — Setup Guide

## Step 1: Create Your Telegram Bot

1. Open Telegram, search for **@BotFather**
2. Send `/newbot` (or use your existing bot token)
3. Choose a name (e.g., "My Café Manager")
4. Choose a username (e.g., `mycafe_manager_bot`)
5. **Copy the bot token** — you'll need it

## Step 2: Get Your Group Chat ID

1. Add the bot to your café's Telegram group
2. Send `/setup` in the group
3. The bot will reply with the Group Chat ID
4. Copy it

## Step 3: Configure the Bot

Edit the `.env` file (or set environment variables):

```
TELEGRAM_BOT_TOKEN=your_token_here
GROUP_CHAT_ID=-100xxxxxxxxxx
CAFE_NAME=Your Café Name
TIMEZONE=Asia/Singapore
ADMIN_USER_IDS=your_telegram_user_id
```

---

## Free Hosting Options (Pick One)

### Option A: Koyeb (Recommended — Easiest Free)

1. Push code to GitHub
2. Go to [koyeb.com](https://www.koyeb.com) → Sign up free
3. Create new App → Connect GitHub repo
4. Set build type: **Dockerfile**
5. Add environment variables (bot token, chat ID, etc.)
6. Deploy — done! Runs 24/7 on free tier

### Option B: Google Cloud Run (Always Free Tier)

1. Install [Google Cloud CLI](https://cloud.google.com/sdk/docs/install)
2. Create a project at [console.cloud.google.com](https://console.cloud.google.com)
3. Deploy:
```bash
gcloud run deploy cafe-bot \
  --source . \
  --region asia-southeast1 \
  --set-env-vars TELEGRAM_BOT_TOKEN=xxx,GROUP_CHAT_ID=xxx \
  --allow-unauthenticated \
  --memory 256Mi \
  --cpu 1 \
  --min-instances 1 \
  --max-instances 1
```
Note: Free tier = 2M requests/month. The bot uses polling, so you may want webhook mode for Cloud Run (requires a small code change).

### Option C: Oracle Cloud Free Tier (Best for Always-On)

1. Sign up at [cloud.oracle.com](https://cloud.oracle.com) (Always Free tier)
2. Create a free ARM VM (4 OCPUs, 24GB RAM — free forever)
3. SSH into the VM:
```bash
sudo apt update && sudo apt install python3-pip python3-venv -y
git clone https://github.com/YOUR_USERNAME/cafe-manager-bot.git
cd cafe-manager-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
4. Create a `.env` file with your settings
5. Run with systemd for auto-restart:
```bash
sudo tee /etc/systemd/system/cafebot.service << 'EOF'
[Unit]
Description=Cafe Manager Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/cafe-manager-bot
EnvironmentFile=/home/ubuntu/cafe-manager-bot/.env
ExecStart=/home/ubuntu/cafe-manager-bot/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable cafebot
sudo systemctl start cafebot
```

### Option D: Railway (Quick & Easy)

1. Go to [railway.app](https://railway.app) → Sign up with GitHub
2. New Project → Deploy from GitHub repo
3. Add environment variables
4. Deploy
5. Note: Free tier has a $5/month credit — enough for a small bot

### Option E: Render (Free with Sleep)

1. Go to [render.com](https://render.com) → Sign up
2. New → Web Service → Connect GitHub repo
3. Environment: Docker
4. Add environment variables
5. Note: Free tier sleeps after 15 min inactivity — bot stops receiving messages during sleep. Add a cron ping to keep alive.

---

## Step 4: First Run

1. Start the bot on your chosen platform
2. In your Telegram group, type `/setup`
3. Register staff: `/addstaff Sarah barista`
4. Set shifts: `/addshift mon Sarah 8:00-16:00`
5. Set hours: `/sethours mon 08:00-22:00`
6. Test: `/today` to see the dashboard

---

## All Commands

| Command | What it does |
|---------|-------------|
| `/start` | Show help |
| `/today` | Today's dashboard |
| `/week` | Weekly overview |
| `/clean` | Start cleaning round |
| `/cleanstatus` | Today's cleaning log |
| `/stock` | View stock levels |
| `/stockcheck` | Interactive stock check |
| `/lowstock` | Low/out items |
| `/open` | Opening checklist |
| `/close` | Closing checklist |
| `/shifts` | View shifts |
| `/addshift` | Add a shift |
| `/removeshift` | Remove a shift |
| `/hours` | View café hours |
| `/sethours` | Change hours |
| `/holiday` | Set holiday hours |
| `/events` | Upcoming events |
| `/addevent` | Plan an event |
| `/buy` | Shopping list |
| `/addbuy` | Add item to buy |
| `/bought` | Mark item bought |
| `/content` | Get content idea |
| `/contentlog` | Recent content |
| `/staff` | Staff list |
| `/addstaff` | Register staff |
| `/setup` | First-time setup |
| `/settings` | Bot settings |

## Natural Language

Just type naturally in the group:
- "cleaned toilets" → logs cleaning
- "running low on milk" → shows stock alert
- "what's left to do" → shows today's dashboard
- "content ideas" → gives you an idea
