"""
Sudu Café Bot — settings.

Everything private (keys, chat IDs, user IDs, the Sheet ID) comes from the
server's `.env` file, which is never uploaded to GitHub. This file only holds
names and sensible defaults, so the repo can stay public.
"""
import os

from dotenv import load_dotenv

load_dotenv()


def _ids(name: str) -> set[int]:
    """Read a comma-separated list of Telegram IDs from the environment."""
    out = set()
    for part in os.getenv(name, "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def _int(name: str) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value.lstrip("-").isdigit() else 0


# ─── Telegram ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Owners chat: money questions are answered here (and to owners anywhere).
# GROUP_CHAT_ID is the old name, still accepted.
OWNER_GROUP_ID = _int("OWNER_GROUP_ID") or _int("GROUP_CHAT_ID")
# Staff group: receipts, sales photos, stock, shopping, recipes. No money answers.
STAFF_GROUP_ID = _int("STAFF_GROUP_ID")
# Owners' personal Telegram user IDs (find yours with /whoami).
OWNER_USER_IDS = _ids("OWNER_USER_IDS") | _ids("ADMIN_USER_IDS")

# ─── Google Sheet ─────────────────────────────────────────
GOOGLE_SHEETS_CREDS_FILE = os.getenv("GOOGLE_SHEETS_CREDS_FILE", "credentials.json")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "SuduBot")  # used only if no ID

# ─── AI ───────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Tried in order. If one is busy or out of free quota, the next one is used.
AI_MODELS = [m.strip() for m in os.getenv(
    "AI_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.5-flash"
).split(",") if m.strip()]
# Stronger model used for a second look when a receipt's numbers don't add up.
AI_CHECK_MODEL = os.getenv("AI_CHECK_MODEL", "gemini-3.5-flash")
# Optional free backup (console.groq.com). Leave empty to skip.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
GROQ_TEXT_MODEL = os.getenv("GROQ_TEXT_MODEL", "openai/gpt-oss-120b")
AI_TIMEOUT_SECONDS = int(os.getenv("AI_TIMEOUT_SECONDS", "60"))

# ─── Café ─────────────────────────────────────────────────
CAFE_NAME = os.getenv("CAFE_NAME", "Sudu")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kuala_Lumpur")
CURRENCY = "RM"

# Receipt item categories (same names the Sheet already uses).
#   Ingredients = food/drink that goes into the menu
#   Consumables = used up regularly: cups, lids, tissue, soap, cleaning
#   Equipment   = bought once: tools, machines, decor
CATEGORIES = ["Ingredients", "Consumables", "Equipment", "Other"]

# How far a receipt's items may be from its printed total before we warn.
TOTAL_TOLERANCE = 0.05

# Where the bot keeps small working state (pending receipts) between restarts.
DATA_DIR = os.getenv("DATA_DIR", "data")
