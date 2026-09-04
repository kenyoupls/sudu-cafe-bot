"""
Café Manager Bot — Configuration
All settings in one place. Override via environment variables or .env file.
"""
import os
from datetime import time
from dotenv import load_dotenv

load_dotenv()  # Load .env file if present

# ─── Telegram ───────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))  # Legacy — kept for backward compat
ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x]

# ─── Multi-Group Setup ────────────────────────────────────
# Owner group: full access to everything (financials, settings, staff mgmt)
# Staff group: daily operations only (receipts, stock, cleaning, tasks, AI chat)
OWNER_GROUP_ID = int(os.getenv("OWNER_GROUP_ID", "0")) or GROUP_CHAT_ID
STAFF_GROUP_ID = int(os.getenv("STAFF_GROUP_ID", "0"))
ALLOWED_GROUP_IDS = [gid for gid in [OWNER_GROUP_ID, STAFF_GROUP_ID] if gid]

# Commands blocked in staff group — anything financial, admin, or settings
STAFF_BLOCKED_COMMANDS = {
    "staff", "addstaff", "removestaff",
    "shifts", "addshift", "removeshift",
    "hours", "sethours", "holiday", "holidays",
    "analyze", "pl", "pnl", "data",
    "setup", "settings",
    "receipts", "expenses", "whopaid", "sales",
}

# AI actions blocked in staff group — financial reports
STAFF_BLOCKED_ACTIONS = {
    "show_expenses", "show_whopaid", "show_sales", "show_pnl",
    "show_staff", "monthly_summary",
}

# ─── Google Sheets (free database) ──────────────────────────
GOOGLE_SHEETS_CREDS_FILE = os.getenv("GOOGLE_SHEETS_CREDS_FILE", "credentials.json")
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "SuduBot")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")

# ─── Gemini AI (free tier — primary) ─────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY_HERE")

# ─── Groq AI (free tier — fallback) ─────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# ─── Timezone ───────────────────────────────────────────────
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kuala_Lumpur")

# ─── Café Info ──────────────────────────────────────────────
CAFE_NAME = os.getenv("CAFE_NAME", "My Café")

# ─── Google Places API (for live café info from Google) ─────
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
GOOGLE_PLACE_ID = os.getenv("GOOGLE_PLACE_ID", "")

# ─── Operating Hours (default — editable via bot) ───────────
DEFAULT_HOURS = {
    "mon": {"open": "08:00", "close": "22:00"},
    "tue": {"open": "08:00", "close": "22:00"},
    "wed": {"open": "08:00", "close": "22:00"},
    "thu": {"open": "08:00", "close": "22:00"},
    "fri": {"open": "08:00", "close": "23:00"},
    "sat": {"open": "09:00", "close": "23:00"},
    "sun": {"open": "09:00", "close": "21:00"},
}

# ─── Scheduled Task Times (24h format, SGT) ─────────────────
OPENING_CHECKLIST_TIME = time(7, 30)   # 7:30 AM
CLOSING_CHECKLIST_TIME = time(21, 0)   # 9:00 PM
MORNING_STOCK_CHECK_TIME = time(8, 0)  # 8:00 AM
CLEANING_REMINDER_TIMES = [
    time(10, 0),   # 10 AM
    time(13, 0),   # 1 PM
    time(16, 0),   # 4 PM
    time(19, 0),   # 7 PM
]
SHIFT_REMINDER_MINUTES_BEFORE = 30     # Remind 30 min before shift
CONTENT_REMINDER_TIME = time(9, 0)     # 9 AM daily content nudge
WEEKLY_REVIEW_DAY = 0                  # Monday
WEEKLY_REVIEW_TIME = time(9, 0)

# ─── Memory ────────────────────────────────────────────────
MEMORY_RETENTION_DAYS = 0              # 0 = keep forever (no auto-delete)
RECENT_MESSAGES_FULL = 200             # Send last 200 important messages verbatim to AI
SUMMARY_DAYS_START = 3                 # Summarise messages older than 3 days

# ─── Chase-up / Action Items ──────────────────────────────
CHASEUP_REMINDER_TIMES = [
    time(10, 30),  # 10:30 AM
    time(16, 0),   # 4:00 PM
]
CHASEUP_STALE_HOURS = 8               # Chase up if no update after 8 hours

# ─── Cleaning Zones ─────────────────────────────────────────
CLEANING_ZONES = [
    "🚻 Toilets",
    "🪑 Dining Area / Tables",
    "☕ Bar / Counter",
    "🍳 Kitchen",
    "🗑️ Bins / Trash Area",
    "🚪 Entrance / Outdoor",
    "🧊 Fridge / Display",
]

# ─── Stock Categories ───────────────────────────────────────
STOCK_CATEGORIES = [
    "🍧 Bingsu Toppings",
    "🥛 Milk & Dairy",
    "🍓 Fruits & Fresh Items",
    "🍵 Tea & Beverages",
    "🧁 Pastries & Food",
    "🥤 Cups & Packaging",
    "🧴 Cleaning Supplies",
    "🍬 Sugar / Syrups",
    "🧊 Ice & Frozen Items",
    "📦 Other Supplies",
]

# ─── Item Categories (for receipt items) ──────────────────
ITEM_CATEGORIES = {
    "ingredients":  "🧂 Ingredients",
    "consumables":  "🧻 Consumables",
    "one-off":      "🧪 One-off",
    "equipment":    "🔧 Equipment",
    "marketing":    "📢 Marketing",
}
DEFAULT_CATEGORY = "ingredients"

# ─── Opening Checklist ──────────────────────────────────────
OPENING_CHECKLIST = [
    "Turn on lights & AC",
    "Turn on coffee machine & grinder",
    "Check fridge temperatures",
    "Set up POS / register",
    "Stock pastry display",
    "Wipe down all tables & counters",
    "Check toilet supplies (soap, tissue, towels)",
    "Put out outdoor signage / menu board",
    "Unlock front door",
    "Post 'We're Open' on social media",
]

# ─── Closing Checklist ──────────────────────────────────────
CLOSING_CHECKLIST = [
    "Last call for orders",
    "Clean & shut down coffee machine",
    "Wipe all tables & counters",
    "Mop floors",
    "Take out trash / recycling",
    "Restock cups, lids, straws for tomorrow",
    "Count cash register / close POS",
    "Lock fridge & check temperatures",
    "Turn off AC, lights, signage",
    "Lock all doors",
    "Send closing report to group",
]

# ─── Holiday System ───────────────────────────────────────
CALENDARIFIC_API_KEY = os.getenv("CALENDARIFIC_API_KEY", "")

# Google Calendar public holiday calendar IDs
HOLIDAY_CALENDAR_IDS = {
    "MY": "en.malaysia#holiday@group.v.calendar.google.com",
    "SG": "en.singapore#holiday@group.v.calendar.google.com",
}

# Calendarific country/state codes for state-level holidays
CALENDARIFIC_LOCATIONS = [
    {"country": "MY", "state": "MY-01"},  # Johor
    {"country": "MY", "state": "MY-10"},  # Selangor (Klang Valley)
    {"country": "MY", "state": "MY-04"},  # Melaka
]

# School holidays — hardcoded from MOE (Malaysia + Singapore)
# Format: list of (start_date, end_date, label) tuples
SCHOOL_HOLIDAYS_2026 = [
    ("2026-03-14", "2026-03-22", "MY Term 1 Break"),
    ("2026-05-23", "2026-06-07", "MY Mid-Year Break"),
    ("2026-08-15", "2026-08-23", "MY Term 3 Break"),
    ("2026-11-21", "2026-12-31", "MY Year-End Break"),
    ("2026-03-14", "2026-03-22", "SG March School Holiday"),
    ("2026-05-30", "2026-06-28", "SG June School Holiday"),
    ("2026-09-05", "2026-09-13", "SG September School Holiday"),
    ("2026-11-21", "2026-12-31", "SG Year-End Holiday"),
]

SCHOOL_HOLIDAYS_2027 = [
    ("2027-03-13", "2027-03-21", "MY Term 1 Break"),
    ("2027-05-22", "2027-06-06", "MY Mid-Year Break"),
    ("2027-08-14", "2027-08-22", "MY Term 3 Break"),
    ("2027-11-20", "2027-12-31", "MY Year-End Break"),
    ("2027-03-13", "2027-03-21", "SG March School Holiday"),
    ("2027-05-29", "2027-06-27", "SG June School Holiday"),
    ("2027-09-04", "2027-09-12", "SG September School Holiday"),
    ("2027-11-20", "2027-12-31", "SG Year-End Holiday"),
]

SCHOOL_HOLIDAY_REMINDER_MONTH = 10  # October — remind about upcoming year-end holidays

# ─── Content Ideas Pool ─────────────────────────────────────
CONTENT_IDEAS = [
    "📸 Behind-the-scenes: barista making latte art",
    "🎥 Time-lapse of opening the café",
    "📝 'Did you know?' post about coffee origin",
    "🗳️ Poll: what new drink should we add?",
    "🌅 Cozy morning ambience reel",
    "🎂 Feature a customer birthday celebration",
    "👨‍🍳 Spotlight: meet our team member",
    "☕ Recipe share: our signature drink",
    "📊 Fun fact Friday: how many cups we served this week",
    "🎉 Announce upcoming event or promo",
    "🍰 New item reveal / teaser",
    "💬 Customer review / testimonial share",
    "🌿 Sustainability post: our eco-friendly practices",
    "📅 Weekly special announcement",
    "🎵 Playlist of the week share",
]
