#!/usr/bin/env python3
"""
☕ Café Manager Bot — Your Digital Café Manager on Telegram
Handles cleaning, stock, checklists, shifts, hours, events,
shopping lists, content ideas, and more.

Usage:
  1. Set TELEGRAM_BOT_TOKEN env var (or edit config.py)
  2. Run: python bot.py
  3. Add the bot to your café Telegram group
  4. Type /start to begin
"""
import json
import logging
import random
import html
from datetime import datetime, timedelta, date, time as dtime
from zoneinfo import ZoneInfo
from textwrap import dedent

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler,
)

import functools

import config
from storage import get_store, normalize_item_name, clean_item_name
from ai_chat import (
    ask_ai, get_content_idea, analyze_stock_and_suggest, suggest_for_event,
    handle_voice, handle_photo, handle_video, remember, process_message,
    classify_photo, classify_video, classify_receipt_reply,
    validate_stock_count, generate_content_suggestions,
    extract_action_items_ai, generate_chaseup_message, text_to_speech,
    search_memory, process_receipt, process_sales_report, analyze_pos_file,
    ask_about_data,
)

# ─── Logging ────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TZ = ZoneInfo(config.TIMEZONE)
store = get_store()

# ─── SOP data: seed Google Sheets on first run, then load AI prompts ───
if store._sheets:
    try:
        store._sheets.seed_sop_to_sheets()
    except Exception as e:
        logger.error(f"seed_sop_to_sheets failed: {e}")
    try:
        from ai_chat import refresh_sop_prompt
        refresh_sop_prompt(store._sheets)
    except Exception as e:
        logger.error(f"refresh_sop_prompt failed: {e}")
else:
    logger.warning("Google Sheets not connected — SOP data (recipes, checklists, "
                    "inspection) will not be available to the AI or stock checks.")

# ─── Conversation states ────────────────────────────────────
(
    STOCK_ITEM, STOCK_QTY,
    SHIFT_DAY, SHIFT_NAME, SHIFT_TIMES,
    HOURS_DAY, HOURS_TIMES,
    EVENT_TITLE, EVENT_DATE, EVENT_DETAILS,
    SHOP_ITEM,
    STAFF_NAME, STAFF_ID,
    AI_CHAT,
) = range(14)


# ═══════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════

def now_sg() -> datetime:
    return datetime.now(TZ)

def today_day() -> str:
    return now_sg().strftime("%a").lower()

def user_name(update: Update) -> str:
    u = update.effective_user
    return u.full_name or u.username or str(u.id)

def _parse_ts(s: str) -> datetime:
    """Parse timestamp in either old ISO or '29/08/26-1425' format."""
    if not s:
        return datetime.min.replace(tzinfo=TZ)
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        pass
    try:
        dt = datetime.strptime(s, "%d/%m/%y-%H%M")
        return dt.replace(tzinfo=TZ)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=TZ)

def is_admin(update: Update) -> bool:
    if not config.ADMIN_USER_IDS:
        return True  # No admins configured = everyone is admin
    return update.effective_user.id in config.ADMIN_USER_IDS


def _is_group_chat(update: Update) -> bool:
    """Check if message is from a group/supergroup."""
    return update.effective_chat.type in ("group", "supergroup")


def _is_allowed_group(update: Update) -> bool:
    """Check if chat is one of the allowed groups. Rejects DMs and unknown groups."""
    if not config.ALLOWED_GROUP_IDS:
        return True  # No groups configured = allow everything (backward compat)
    chat_id = update.effective_chat.id
    return chat_id in config.ALLOWED_GROUP_IDS


def _is_staff_group(update: Update) -> bool:
    """Check if message is from the staff group (restricted permissions)."""
    if not config.STAFF_GROUP_ID:
        return False
    return update.effective_chat.id == config.STAFF_GROUP_ID


def _get_chat_id(update: Update) -> int:
    """Get the chat ID for context isolation."""
    return update.effective_chat.id


async def _auto_add_low_to_shopping(low_items: list, store, bot, chat_id: int):
    """Auto-add items below minimum stock to shopping list."""
    from storage import normalize_item_name
    shopping = store.get_shopping_list()
    existing = {normalize_item_name(s["item"]) for s in shopping}
    added = []
    for li in low_items:
        if normalize_item_name(li["item"]) not in existing:
            store.add_shopping_item(li["item"], "Auto (low stock)", urgency="high")
            added.append(li["item"])
    if added:
        msg = "🛒 *Auto-added to shopping list* (below minimum):\n• " + "\n• ".join(added)
        try:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Shopping auto-add message error: {e}")


def _bot_is_tagged(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if bot was @tagged or if the message is a reply to the bot."""
    msg = update.message
    if not msg:
        return False

    # Check if replying to one of the bot's messages
    if msg.reply_to_message and msg.reply_to_message.from_user:
        if msg.reply_to_message.from_user.id == ctx.bot.id:
            return True

    # Check if bot is @mentioned in the text
    text = msg.text or msg.caption or ""
    bot_username = ctx.bot.username  # e.g. "Sudu_helper_bot"
    if bot_username and f"@{bot_username}" in text:
        return True

    # Check entities for bot mention
    entities = msg.entities or msg.caption_entities or []
    for ent in entities:
        if ent.type == "mention":
            mention = text[ent.offset:ent.offset + ent.length]
            if bot_username and mention.lower() == f"@{bot_username}".lower():
                return True
        elif ent.type == "text_mention" and ent.user:
            if ent.user.id == ctx.bot.id:
                return True

    return False


# ═══════════════════════════════════════════════════════════
#  GROUP RESTRICTION + PERMISSION CHECKS
# ═══════════════════════════════════════════════════════════

async def _group_gate(update: Update) -> bool:
    """Returns True if message should be BLOCKED (wrong group / DM).
    Silent reject — bot just ignores the message."""
    if not config.ALLOWED_GROUP_IDS:
        return False  # No groups configured = allow all
    if not _is_group_chat(update):
        return True  # Block DMs
    if not _is_allowed_group(update):
        return True  # Block unknown groups
    return False


async def _staff_cmd_gate(update: Update, cmd_name: str) -> bool:
    """Returns True if command should be BLOCKED in staff group.
    Sends a polite rejection message."""
    if _is_staff_group(update) and cmd_name in config.STAFF_BLOCKED_COMMANDS:
        await update.message.reply_text(
            "🔒 This command is only available in the owner group."
        )
        return True
    return False


def group_only(func):
    """Decorator: silently ignore messages from DMs / unknown groups."""
    @functools.wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if await _group_gate(update):
            return
        return await func(update, ctx, *args, **kwargs)
    return wrapper


def owner_only(cmd_name: str):
    """Decorator: block command in staff group (financial / admin commands)."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            if await _group_gate(update):
                return
            if await _staff_cmd_gate(update, cmd_name):
                return
            return await func(update, ctx, *args, **kwargs)
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════
#  /start & /help
# ═══════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = f"""
☕ *{config.CAFE_NAME} Manager Bot*

I'm your digital café manager\\! Here's what I can do:

🧹 *Cleaning*
  /clean — Start cleaning round
  /cleanstatus — Today's cleaning log

📦 *Stock*
  /stock — View current stock
  /stockcheck — Run a stock check
  /lowstock — Show low/out items

✅ *Checklists*
  /open — Opening checklist
  /close — Closing checklist

👥 *Staff & Shifts*
  /shifts — View shift schedule
  /addshift — Add a shift
  /staff — View staff list
  /addstaff — Register staff member

🕐 *Operating Hours*
  /hours — View café hours
  /sethours — Change hours
  /holidays — View holiday hours

📅 *Events*
  /events — View upcoming events
  /addevent — Plan a new event

🛒 *Shopping*
  /buy — View shopping list
  /addbuy — Add item to buy
  /bought — Mark item as bought

📱 *Content*
  /content — Get a content idea
  /contentlog — Recent content posted

📊 *Reports*
  /today — Today's summary
  /week — Weekly overview

📋 *Tasks \\& Follow\\-up*
  /tasks — View pending action items
  /taskdone — Mark task as done
  /taskdismiss — Dismiss a task
  /search — Search chat history

🧾 *Receipts \\& P\\&L*
  /receipts — Recent receipts
  /expenses — Expense breakdown by supplier
  /whopaid — Who paid what \\& repayment
  /sales — Daily sales summary
  /pl — Profit \\& Loss summary
  /pnl — Download P\\&L report \\(XLSX\\)
  /stockusage — Monthly stock usage
  /data — Ask about business data

🤖 *AI Manager*
  /ask — Ask me anything about the café
  /analyze — AI stock \\& operations analysis
  /aicontent — AI\\-generated content ideas

⚙️ *Admin*
  /settings — Bot settings
  /setup — First\\-time setup

💬 Or just chat with me naturally — I understand\\!
🎤 Send voice notes — I'll reply with voice too\\!
"""
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


# ═══════════════════════════════════════════════════════════
#  🧹 CLEANING MODULE
# ═══════════════════════════════════════════════════════════

async def cmd_clean(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Start a cleaning round — shows zones as buttons."""
    buttons = []
    for zone in config.CLEANING_ZONES:
        buttons.append([InlineKeyboardButton(zone, callback_data=f"clean:{zone}")])
    buttons.append([InlineKeyboardButton("✅ All Done", callback_data="clean:ALL_DONE")])

    await update.message.reply_text(
        "🧹 *Cleaning Round*\nTap each zone when done:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def cb_clean(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    zone = query.data.split(":", 1)[1]
    name = user_name(update)

    if zone == "ALL_DONE":
        await query.edit_message_text(
            f"✅ Cleaning round completed by {name}!\n"
            f"Time: {now_sg().strftime('%I:%M %p')}"
        )
        return

    store.log_cleaning(zone, name)
    await query.edit_message_text(
        f"✅ {zone} cleaned by {name}\n"
        f"Time: {now_sg().strftime('%I:%M %p')}\n\n"
        f"Use /clean to log another zone."
    )


async def cmd_cleanstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show today's cleaning log."""
    logs = store.get_cleaning_today()
    if not logs:
        await update.message.reply_text("🧹 No cleaning logged today yet.\nUse /clean to start!")
        return

    lines = ["🧹 *Today's Cleaning Log*\n"]
    for entry in logs:
        t = _parse_ts(entry["done_at"]).strftime("%I:%M %p")
        lines.append(f"  ✅ {entry['zone']} — {entry['done_by']} at {t}")

    # Check what's NOT cleaned yet
    cleaned_zones = {e["zone"] for e in logs}
    uncleaned = [z for z in config.CLEANING_ZONES if z not in cleaned_zones]
    if uncleaned:
        lines.append("\n⚠️ *Still needs cleaning:*")
        for z in uncleaned:
            lines.append(f"  ❌ {z}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════
#  📦 STOCK MODULE
# ═══════════════════════════════════════════════════════════

async def cmd_stock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """View current stock levels."""
    stock = store.get_stock()
    if not stock:
        await update.message.reply_text(
            "📦 No stock data yet.\nUse /stockcheck to run your first stock check!"
        )
        return

    lines = ["📦 *Current Stock Levels*\n"]
    for item, info in sorted(stock.items()):
        qty = info.get("qty", "?")
        emoji = "🔴" if qty.upper() in ("LOW", "OUT", "0") else "🟢"
        lines.append(f"  {emoji} *{item}*: {qty}")

    last_check = max(
        (info.get("updated_at", "") for info in stock.values()), default="Never"
    )
    if last_check != "Never":
        last_check = _parse_ts(last_check).strftime("%d %b, %I:%M %p")
    lines.append(f"\n🕐 Last updated: {last_check}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_removestock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Remove a stock item. Usage: /removestock item name"""
    text = (update.message.text or "").split(None, 1)
    if len(text) < 2:
        await update.message.reply_text(
            "Usage: /removestock <item name>\nExample: /removestock Glade Air Freshener"
        )
        return

    item_name = text[1].strip()
    removed = store.remove_stock(item_name)
    if removed:
        await update.message.reply_text(f"✅ Removed *{item_name}* from stock tracking.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Item *{item_name}* not found in stock.", parse_mode="Markdown")


async def cmd_stockcheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show instructions for bulk stock check."""
    await update.message.reply_text(
        "📋 *Stock Check*\n\n"
        "Just send a list of items with their counts in the chat. For example:\n\n"
        "_milk 6\n"
        "sugar 3\n"
        "cups 200\n"
        "ice 10 bags_\n\n"
        "I'll update everything and tell you what's missing!",
        parse_mode="Markdown",
    )


async def cmd_lowstock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show items that are LOW or OUT."""
    low = store.get_low_stock()
    if not low:
        await update.message.reply_text("✅ All stock levels are OK!")
        return

    lines = ["🚨 *Low / Out of Stock*\n"]
    for item, info in low:
        lines.append(f"  🔴 {item}: {info['qty']}")
    lines.append("\nUse /addbuy <item> to add to shopping list.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════
#  ✅ CHECKLISTS MODULE
# ═══════════════════════════════════════════════════════════

async def _send_checklist(update, checklist_items, checklist_type, title_emoji, title):
    """Generic checklist sender with interactive buttons."""
    existing = store.get_checklist_today(checklist_type)
    if existing:
        done_count = len(existing.get("items_done", []))
        total = len(checklist_items)
        await update.message.reply_text(
            f"{title_emoji} {title} already done today by {existing['done_by']}!\n"
            f"({done_count}/{total} items completed)\n\n"
            f"Send /{'open' if checklist_type == 'opening' else 'close'} again to redo."
        )

    buttons = []
    for i, item in enumerate(checklist_items):
        buttons.append([InlineKeyboardButton(
            f"⬜ {item}", callback_data=f"chk:{checklist_type}:{i}"
        )])
    buttons.append([InlineKeyboardButton(
        "✅ All Done!", callback_data=f"chk:{checklist_type}:DONE"
    )])

    ctx_key = f"{checklist_type}_done"
    # We'll track done items in callback

    await update.message.reply_text(
        f"{title_emoji} *{title}*\nTap each item when done:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def cmd_open(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["opening_done"] = set()
    await _send_checklist(
        update, config.OPENING_CHECKLIST, "opening", "🌅", "Opening Checklist"
    )


async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["closing_done"] = set()
    await _send_checklist(
        update, config.CLOSING_CHECKLIST, "closing", "🌙", "Closing Checklist"
    )


async def cb_checklist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":", 2)
    checklist_type = parts[1]
    action = parts[2]
    name = user_name(update)

    items = config.OPENING_CHECKLIST if checklist_type == "opening" else config.CLOSING_CHECKLIST
    done_key = f"{checklist_type}_done"

    if done_key not in ctx.user_data:
        ctx.user_data[done_key] = set()

    if action == "DONE":
        done_items = list(ctx.user_data[done_key])
        store.log_checklist(checklist_type, done_items, name)
        emoji = "🌅" if checklist_type == "opening" else "🌙"
        await query.edit_message_text(
            f"{emoji} *{checklist_type.title()} Checklist Complete!*\n"
            f"Done by: {name}\n"
            f"Items: {len(done_items)}/{len(items)}\n"
            f"Time: {now_sg().strftime('%I:%M %p')}",
            parse_mode="Markdown",
        )
        return

    idx = int(action)
    ctx.user_data[done_key].add(idx)

    # Rebuild buttons with done items checked
    buttons = []
    for i, item in enumerate(items):
        check = "✅" if i in ctx.user_data[done_key] else "⬜"
        buttons.append([InlineKeyboardButton(
            f"{check} {item}", callback_data=f"chk:{checklist_type}:{i}"
        )])
    buttons.append([InlineKeyboardButton(
        "✅ All Done!", callback_data=f"chk:{checklist_type}:DONE"
    )])

    done_count = len(ctx.user_data[done_key])
    emoji = "🌅" if checklist_type == "opening" else "🌙"
    await query.edit_message_text(
        f"{emoji} *{checklist_type.title()} Checklist* ({done_count}/{len(items)})\n"
        f"Tap each item when done:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


# ═══════════════════════════════════════════════════════════
#  👥 SHIFTS MODULE
# ═══════════════════════════════════════════════════════════

DAYS_OF_WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAYS_FULL = {
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
    "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"
}

async def cmd_shifts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """View shift schedule for the week."""
    all_shifts = store.get_shifts()
    if not all_shifts:
        await update.message.reply_text(
            "👥 No shifts scheduled yet.\nUse /addshift to add one!"
        )
        return

    lines = ["👥 *Shift Schedule*\n"]
    for day in DAYS_OF_WEEK:
        day_shifts = all_shifts.get(day, {})
        today_marker = " 👈" if day == today_day() else ""
        lines.append(f"*{DAYS_FULL[day]}*{today_marker}")
        if not day_shifts:
            lines.append("  — No shifts —")
        else:
            for staff, times in day_shifts.items():
                lines.append(f"  • {staff}: {times['start']} – {times['end']}")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_addshift(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Add a shift: /addshift <day> <name> <start>-<end>"""
    args = ctx.args
    if not args or len(args) < 3:
        await update.message.reply_text(
            "Usage: /addshift <day> <name> <start-end>\n"
            "Example: /addshift mon Sarah 8:00-16:00"
        )
        return

    day = args[0].lower()[:3]
    if day not in DAYS_OF_WEEK:
        await update.message.reply_text(f"❌ Invalid day. Use: {', '.join(DAYS_OF_WEEK)}")
        return

    name = args[1]
    times = args[2] if len(args) > 2 else "TBD"

    if "-" in times:
        start, end = times.split("-", 1)
    else:
        start, end = times, "TBD"

    store.set_shift(day, name, start, end)
    await update.message.reply_text(
        f"✅ Shift added:\n"
        f"  {DAYS_FULL[day]}: {name} → {start} – {end}"
    )


async def cmd_removeshift(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Remove a shift: /removeshift <day> <name>"""
    args = ctx.args
    if not args or len(args) < 2:
        await update.message.reply_text("Usage: /removeshift <day> <name>\nExample: /removeshift mon Sarah")
        return

    day = args[0].lower()[:3]
    name = args[1]
    store.remove_shift(day, name)
    await update.message.reply_text(f"✅ Removed {name}'s shift on {DAYS_FULL.get(day, day)}.")


# ═══════════════════════════════════════════════════════════
#  🕐 HOURS MODULE
# ═══════════════════════════════════════════════════════════

async def cmd_hours(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show café operating hours."""
    hours = store.get_hours() or config.DEFAULT_HOURS
    lines = [f"🕐 *{config.CAFE_NAME} Operating Hours*\n"]

    for day in DAYS_OF_WEEK:
        h = hours.get(day, {"open": "Closed", "close": ""})
        today_marker = " 👈" if day == today_day() else ""
        if h.get("open") == "Closed":
            lines.append(f"  {DAYS_FULL[day]}: Closed{today_marker}")
        else:
            lines.append(f"  {DAYS_FULL[day]}: {h['open']} – {h['close']}{today_marker}")

    # Show holiday hours if any
    holidays = store.get_holiday_hours()
    upcoming = {d: h for d, h in holidays.items() if d >= now_sg().date().isoformat()}
    if upcoming:
        lines.append("\n📅 *Special / Holiday Hours:*")
        for d, h in sorted(upcoming.items()):
            label = f" ({h['label']})" if h.get("label") else ""
            lines.append(f"  {d}: {h['open']} – {h['close']}{label}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_sethours(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Set hours: /sethours <day> <open>-<close>"""
    args = ctx.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Usage: /sethours <day> <open-close>\n"
            "Example: /sethours sat 10:00-23:00\n"
            "Example: /sethours sun closed"
        )
        return

    day = args[0].lower()[:3]
    if day not in DAYS_OF_WEEK:
        await update.message.reply_text(f"❌ Invalid day. Use: {', '.join(DAYS_OF_WEEK)}")
        return

    time_str = args[1].lower()
    if time_str == "closed":
        store.set_hours(day, "Closed", "")
    elif "-" in time_str:
        open_t, close_t = time_str.split("-", 1)
        store.set_hours(day, open_t, close_t)
    else:
        await update.message.reply_text("❌ Format: open-close (e.g., 08:00-22:00) or 'closed'")
        return

    await update.message.reply_text(f"✅ {DAYS_FULL[day]} hours updated to {time_str}")


async def cmd_setholiday(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Set holiday hours: /holiday <date> <open-close> [label]"""
    args = ctx.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Usage: /holiday <YYYY-MM-DD> <open-close> [label]\n"
            "Example: /holiday 2026-12-25 10:00-15:00 Christmas\n"
            "Example: /holiday 2026-01-01 closed New Year"
        )
        return

    date_str = args[0]
    time_str = args[1].lower()
    label = " ".join(args[2:]) if len(args) > 2 else ""

    if time_str == "closed":
        store.set_holiday_hours(date_str, "Closed", "", label)
    elif "-" in time_str:
        open_t, close_t = time_str.split("-", 1)
        store.set_holiday_hours(date_str, open_t, close_t, label)
    else:
        await update.message.reply_text("❌ Format: open-close or 'closed'")
        return

    await update.message.reply_text(f"✅ Holiday hours set for {date_str}: {time_str} {label}")


# ═══════════════════════════════════════════════════════════
#  📅 EVENTS MODULE
# ═══════════════════════════════════════════════════════════

async def cmd_events(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """View upcoming events."""
    events = store.get_events(upcoming_only=True)
    if not events:
        await update.message.reply_text("📅 No upcoming events.\nUse /addevent to plan one!")
        return

    lines = ["📅 *Upcoming Events*\n"]
    for i, evt in enumerate(events):
        lines.append(
            f"  {i+1}. *{evt['title']}*\n"
            f"     📆 {evt['date']}\n"
            f"     📝 {evt['details']}\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_addevent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Add event: /addevent <YYYY-MM-DD> <title> | <details>"""
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await update.message.reply_text(
            "Usage: /addevent <YYYY-MM-DD> <title> | <details>\n"
            "Example: /addevent 2026-09-15 Live Music Night | Jazz band from 7-10pm, need extra chairs"
        )
        return

    parts = text.split(" ", 1)
    event_date = parts[0]
    rest = parts[1] if len(parts) > 1 else "Untitled Event"

    if "|" in rest:
        title, details = rest.split("|", 1)
    else:
        title, details = rest, ""

    store.add_event(title.strip(), event_date, details.strip(), user_name(update))
    await update.message.reply_text(
        f"✅ Event added!\n"
        f"  📅 {event_date}\n"
        f"  📌 {title.strip()}\n"
        f"  📝 {details.strip() or 'No details'}"
    )


# ═══════════════════════════════════════════════════════════
#  🛒 SHOPPING / TO-BUY MODULE
# ═══════════════════════════════════════════════════════════

async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """View shopping list."""
    items = store.get_shopping_list()
    if not items:
        await update.message.reply_text("🛒 Shopping list is empty! ✅")
        return

    lines = ["🛒 *Shopping List*\n"]
    for i, item in enumerate(items):
        urgency = "🔴" if item.get("urgency") == "urgent" else "⚪"
        lines.append(f"  {i+1}. {urgency} {item['item']} (by {item['added_by']})")

    lines.append("\nUse /bought <number> to mark as bought.")
    lines.append("Use /addbuy <item> to add more.")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_addbuy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Add to shopping list: /addbuy <item>"""
    item = " ".join(ctx.args) if ctx.args else ""
    if not item:
        await update.message.reply_text("Usage: /addbuy <item>\nExample: /addbuy Oat milk x5")
        return

    urgency = "urgent" if "urgent" in item.lower() or "!" in item else "normal"
    item_clean = item.replace("urgent", "").replace("!", "").strip()

    store.add_shopping_item(item_clean, user_name(update), urgency)
    emoji = "🔴 URGENT" if urgency == "urgent" else "➕"
    await update.message.reply_text(f"{emoji} Added to shopping list: {item_clean}")


async def cmd_bought(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Mark item as bought: /bought <number>"""
    if not ctx.args:
        await update.message.reply_text("Usage: /bought <number>\nSee /buy for the list.")
        return

    try:
        idx = int(ctx.args[0]) - 1
        items = store.get_shopping_list()
        if 0 <= idx < len(items):
            item_name = items[idx]["item"]
            store.mark_bought(idx)
            await update.message.reply_text(f"✅ Bought: {item_name}")
        else:
            await update.message.reply_text("❌ Invalid number. Check /buy for the list.")
    except ValueError:
        await update.message.reply_text("❌ Please enter a number. Check /buy for the list.")


# ═══════════════════════════════════════════════════════════
#  📱 CONTENT MODULE
# ═══════════════════════════════════════════════════════════

async def cmd_content(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Get a random content idea."""
    recent = store.get_content_log(30)
    recent_ideas = {e["idea"] for e in recent}

    # Filter out recently used ideas
    available = [i for i in config.CONTENT_IDEAS if i not in recent_ideas]
    if not available:
        available = config.CONTENT_IDEAS  # Reset if all used

    idea = random.choice(available)

    buttons = [
        [
            InlineKeyboardButton("✅ I'll do this!", callback_data=f"content:use:{idea[:50]}"),
            InlineKeyboardButton("🔄 Another idea", callback_data="content:reroll"),
        ]
    ]

    await update.message.reply_text(
        f"📱 *Content Idea for Today*\n\n{idea}\n\n"
        f"💡 *Tips:*\n"
        f"• Best time to post: 9-11 AM or 7-9 PM\n"
        f"• Use 3-5 relevant hashtags\n"
        f"• Tag your location",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def cb_content(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "reroll":
        idea = random.choice(config.CONTENT_IDEAS)
        buttons = [
            [
                InlineKeyboardButton("✅ I'll do this!", callback_data=f"content:use:{idea[:50]}"),
                InlineKeyboardButton("🔄 Another idea", callback_data="content:reroll"),
            ]
        ]
        await query.edit_message_text(
            f"📱 *Content Idea*\n\n{idea}",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
    elif action.startswith("use:"):
        idea = action[4:]
        store.log_content(idea, user_name(update))
        await query.edit_message_text(
            f"📱 Great! {user_name(update)} is working on:\n{idea}\n\n"
            f"Don't forget to post it! 🎉"
        )


async def cmd_contentlog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """View recent content posted."""
    log = store.get_content_log(14)
    if not log:
        await update.message.reply_text("📱 No content logged yet.\nUse /content to get ideas!")
        return

    lines = ["📱 *Recent Content*\n"]
    for entry in reversed(log[-10:]):
        dt = _parse_ts(entry["posted_at"]).strftime("%d %b")
        lines.append(f"  • {dt}: {entry['idea'][:60]} ({entry['posted_by']})")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════
#  👥 STAFF MODULE
# ═══════════════════════════════════════════════════════════

async def cmd_staff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """View registered staff."""
    staff = store.get_staff()
    if not staff:
        await update.message.reply_text("👥 No staff registered.\nUse /addstaff <name> to add.")
        return

    lines = ["👥 *Staff List*\n"]
    for name, info in staff.items():
        role = info.get("role", "staff")
        lines.append(f"  • {name} ({role})")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_addstaff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Register staff: /addstaff <name> [role]"""
    args = ctx.args
    if not args:
        await update.message.reply_text(
            "Usage: /addstaff <name> [role]\n"
            "Example: /addstaff Sarah barista\n"
            "Example: /addstaff Ahmad manager"
        )
        return

    name = args[0]
    role = args[1] if len(args) > 1 else "staff"
    # Use the command sender's telegram ID as a placeholder
    store.add_staff(name, update.effective_user.id, role)
    await update.message.reply_text(f"✅ Staff added: {name} ({role})")


async def cmd_removestaff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Remove staff: /removestaff <name>"""
    if not ctx.args:
        await update.message.reply_text("Usage: /removestaff <name>")
        return
    name = ctx.args[0]
    store.remove_staff(name)
    await update.message.reply_text(f"✅ Removed {name} from staff list.")


# ═══════════════════════════════════════════════════════════
#  📊 REPORTS MODULE
# ═══════════════════════════════════════════════════════════

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Today's summary dashboard."""
    now = now_sg()
    cleaning = store.get_cleaning_today()
    open_check = store.get_checklist_today("opening")
    close_check = store.get_checklist_today("closing")
    low = store.get_low_stock()
    shopping = store.get_shopping_list()
    events = store.get_events(upcoming_only=True)
    upcoming_events = [e for e in events if e["date"] == now_sg().date().isoformat()]

    lines = [
        f"📊 *{config.CAFE_NAME} — Today's Dashboard*",
        f"📅 {now.strftime('%A, %d %B %Y')}\n",

        f"🌅 Opening checklist: {'✅ Done' if open_check else '❌ Not done'}",
        f"🌙 Closing checklist: {'✅ Done' if close_check else '⏳ Pending'}",
        f"🧹 Cleaning zones done: {len(cleaning)}/{len(config.CLEANING_ZONES)}",
    ]

    if low:
        lines.append(f"🔴 Low stock items: {len(low)}")
    else:
        lines.append("📦 Stock: All OK")

    if shopping:
        lines.append(f"🛒 Shopping list: {len(shopping)} items pending")

    if upcoming_events:
        lines.append(f"\n📅 *Today's Events:*")
        for evt in upcoming_events:
            lines.append(f"  • {evt['title']}: {evt['details']}")

    # Today's shifts
    day = today_day()
    shifts = store.get_shifts(day)
    if shifts:
        lines.append(f"\n👥 *Today's Shifts:*")
        for staff, times in shifts.items():
            lines.append(f"  • {staff}: {times['start']} – {times['end']}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Weekly overview."""
    events = store.get_events(upcoming_only=True)
    week_events = [
        e for e in events
        if e["date"] <= (now_sg().date() + timedelta(days=7)).isoformat()
    ]

    lines = [
        f"📊 *{config.CAFE_NAME} — Weekly Overview*\n",
    ]

    # This week's events
    if week_events:
        lines.append("📅 *Events This Week:*")
        for evt in week_events:
            lines.append(f"  • {evt['date']}: {evt['title']}")
    else:
        lines.append("📅 No events this week.")

    # Shifts overview
    all_shifts = store.get_shifts()
    if all_shifts:
        lines.append("\n👥 *Shift Schedule:*")
        for day in DAYS_OF_WEEK:
            shifts = all_shifts.get(day, {})
            if shifts:
                staff_list = ", ".join(f"{s} ({t['start']}-{t['end']})" for s, t in shifts.items())
                lines.append(f"  {DAYS_FULL[day]}: {staff_list}")

    # Shopping
    shopping = store.get_shopping_list()
    if shopping:
        lines.append(f"\n🛒 *Shopping List ({len(shopping)} items):*")
        for item in shopping[:5]:
            lines.append(f"  • {item['item']}")
        if len(shopping) > 5:
            lines.append(f"  ... and {len(shopping)-5} more")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════
#  ⚙️ SETUP & SETTINGS
# ═══════════════════════════════════════════════════════════

async def cmd_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """First-time setup guide."""
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"⚙️ *First-Time Setup*\n\n"
        f"1️⃣ Your Group Chat ID is: `{chat_id}`\n"
        f"   Set this as GROUP\\_CHAT\\_ID in your config\\.\n\n"
        f"2️⃣ Your User ID is: `{update.effective_user.id}`\n"
        f"   Add this to ADMIN\\_USER\\_IDS to be admin\\.\n\n"
        f"3️⃣ Use /addstaff to register your team\\.\n"
        f"4️⃣ Use /addshift to set up the schedule\\.\n"
        f"5️⃣ Use /sethours to set operating hours\\.\n\n"
        f"The bot will auto\\-send reminders once configured\\!",
        parse_mode="MarkdownV2",
    )


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """View/change bot settings."""
    lines = [
        "⚙️ *Bot Settings*\n",
        f"  ☕ Café: {config.CAFE_NAME}",
        f"  🌏 Timezone: {config.TIMEZONE}",
        f"  💬 Group Chat ID: {config.OWNER_GROUP_ID}",
        f"  👑 Admins: {len(config.ADMIN_USER_IDS)} configured",
        f"\n*Scheduled Reminders:*",
        f"  🌅 Opening checklist: {config.OPENING_CHECKLIST_TIME}",
        f"  🌙 Closing checklist: {config.CLOSING_CHECKLIST_TIME}",
        f"  📦 Stock check: {config.MORNING_STOCK_CHECK_TIME}",
        f"  🧹 Cleaning: {', '.join(str(t) for t in config.CLEANING_REMINDER_TIMES)}",
        f"  📱 Content reminder: {config.CONTENT_REMINDER_TIME}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════
#  🎤 VOICE NOTE HANDLER
# ═══════════════════════════════════════════════════════════

async def handle_voice_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle voice notes — download, send to Gemini, respond."""
    if not update.message or not update.message.voice:
        return

    # In groups: only process if tagged or replied-to
    if _is_group_chat(update) and not _bot_is_tagged(update, ctx):
        name = user_name(update)
        remember(name, "[Sent a voice note]", "voice", update.effective_chat.id)
        return

    voice = update.message.voice
    name = user_name(update)

    # Check duration — skip very long voice notes (>120s) to save tokens
    if voice.duration and voice.duration > 120:
        await update.message.reply_text(
            f"🎤 That voice note is {voice.duration}s — a bit long for me to process. "
            f"Could you keep it under 2 minutes, {name}?"
        )
        return

    try:
        # Download voice note
        voice_file = await ctx.bot.get_file(voice.file_id)
        audio_data = await voice_file.download_as_bytearray()

        # Determine mime type
        mime_type = voice.mime_type or "audio/ogg"

        # Send to Gemini (pass chat_id/message_id for media reference)
        chat_id = update.effective_chat.id
        msg_id = update.message.message_id
        response = await handle_voice(bytes(audio_data), name, mime_type, chat_id, msg_id)

        if response:
            # Send text reply first (so group can read it)
            await update.message.reply_text(f"🎤 {response}")

            # Also reply with voice note
            try:
                voice_bytes = await text_to_speech(response)
                if voice_bytes:
                    import io as _io
                    await update.message.reply_voice(
                        voice=_io.BytesIO(voice_bytes),
                    )
            except Exception as tts_err:
                logger.warning(f"TTS reply failed (non-critical): {tts_err}")
        else:
            await update.message.reply_text(
                f"🎤 I heard your voice note, {name}, but couldn't process it. "
                f"Try typing your message instead, or check that GEMINI_API_KEY is set."
            )
    except Exception as e:
        logger.error(f"Voice note error: {e}")
        await update.message.reply_text(
            f"🎤 Couldn't process that voice note, {name}. Try typing it out?"
        )


# ═══════════════════════════════════════════════════════════
#  📷 PHOTO HANDLER
# ═══════════════════════════════════════════════════════════

async def handle_photo_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle photos — detect receipts vs regular photos, route accordingly."""
    if not update.message or not update.message.photo:
        return

    name = user_name(update)
    caption = update.message.caption or ""
    chat_id = update.effective_chat.id
    msg_id = update.message.message_id

    in_group = _is_group_chat(update)
    tagged = _bot_is_tagged(update, ctx)

    try:
        # Get the largest photo (last in the array)
        photo = update.message.photo[-1]

        # ── Store file_id so reply-to-photo can find it later ──
        stored = ctx.chat_data.setdefault("photo_file_ids", {})
        stored[str(msg_id)] = photo.file_id
        # Keep only last 50 to avoid memory bloat
        if len(stored) > 50:
            oldest = list(stored.keys())[:-50]
            for k in oldest:
                stored.pop(k, None)

        photo_file = await ctx.bot.get_file(photo.file_id)
        image_data = await photo_file.download_as_bytearray()
        image_bytes = bytes(image_data)

        # ─── Classify the photo (AI-powered) ─────────────────
        classification = "photo"
        try:
            classification = await classify_photo(image_bytes, "image/jpeg")
        except Exception as e:
            logger.warning(f"Photo classification failed: {e}")

        # In groups: if not tagged and it's just a regular photo, stay quiet
        if in_group and not tagged and classification == "photo":
            remember(name, f"[Sent a photo: {caption}]" if caption else "[Sent a photo]", "photo", chat_id)
            return

        # ─── RECEIPT ───────────────────────────────────────
        if classification == "receipt":
            processing_msg = await update.message.reply_text("🧾 Processing receipt/invoice...")
            # Reset tracking for new receipt conversation
            ctx.chat_data["pending_receipt_msg_ids"] = []
            _track_receipt_msg(ctx, processing_msg.message_id)
            receipt_data = await process_receipt(image_bytes, name, caption)

            if receipt_data:
                _fix_receipt_total(receipt_data)
                _fix_receipt_paid_by(receipt_data, name)
                _r_total = float(receipt_data.get('total') or 0)
                _r_subtotal = float(receipt_data.get('subtotal') or 0)
                _r_tax = float(receipt_data.get('tax') or 0)

                remember(name, f"[Receipt: {receipt_data.get('supplier', '?')} "
                         f"RM{_r_total:.2f}]",
                         "receipt", chat_id, msg_id)

                confirm_msg = _build_receipt_confirm_msg(receipt_data, name)

                ctx.chat_data["pending_receipt"] = {
                    "data": receipt_data,
                    "image_bytes": image_bytes,
                    "user": name,
                    "caption": caption,
                }

                sent_msg = await update.message.reply_text(
                    confirm_msg,
                    reply_markup=_receipt_confirm_buttons(),
                    parse_mode="Markdown",
                )
                ctx.chat_data["pending_receipt_msg_id"] = sent_msg.message_id
                _track_receipt_msg(ctx, sent_msg.message_id)
            else:
                await update.message.reply_text(
                    f"🧾 Couldn't read that receipt, {name}. "
                    f"Try a clearer photo or different angle."
                )
            return

        # ─── SALES REPORT ─────────────────────────────────
        if classification == "sales_report":
            await update.message.reply_text("📊 Processing daily sales report...")
            sales_data = await process_sales_report(image_bytes, name, caption)

            if sales_data:
                _s_total = float(sales_data.get('total_sales') or 0)
                remember(name, f"[Sales Report: {sales_data.get('date', 'unknown')} "
                         f"RM{_s_total:.2f}]",
                         "sales_report", chat_id, msg_id)

                # Build payment breakdown lines
                payment_lines = ""
                for p in sales_data.get("payment_breakdown", []):
                    method = p.get("method", "Unknown")
                    amount = float(p.get("amount", 0) or 0)
                    payment_lines += f"\n  • {method}: RM{amount:.2f}"

                total_sales = float(sales_data.get("total_sales", 0) or 0)
                bill_count = sales_data.get("bill_count", 0) or 0
                total_pax = sales_data.get("total_pax", 0) or 0
                discount = float(sales_data.get("total_discount", 0) or 0)
                void = float(sales_data.get("total_void", 0) or 0)
                refund = float(sales_data.get("total_refund", 0) or 0)
                report_date = sales_data.get("date", "Unknown")
                report_user = sales_data.get("user", name)

                confirm_msg = (
                    f"📊 *Daily Sales Report*\n\n"
                    f"📅 Date: {report_date}\n"
                    f"👤 Submitted by: {name}\n"
                    f"🧑‍💼 Cashier: {report_user}\n\n"
                    f"💰 *Total Sales: RM{total_sales:.2f}*\n"
                    f"🧾 Bills: {bill_count}\n"
                    f"👥 Pax: {total_pax}\n\n"
                    f"💳 *Payment Breakdown:*{payment_lines}\n\n"
                )

                # Only show if non-zero
                extras = []
                if discount > 0:
                    extras.append(f"🏷️ Discount: RM{discount:.2f}")
                if void > 0:
                    extras.append(f"🚫 Void: RM{void:.2f}")
                if refund > 0:
                    extras.append(f"↩️ Refund: RM{refund:.2f}")
                if extras:
                    confirm_msg += "\n".join(extras) + "\n\n"

                if sales_data.get("notes"):
                    confirm_msg += f"📝 Notes: {sales_data['notes']}\n\n"

                confirm_msg += "_Reply 'yes' to confirm, or tell me what to change._"

                ctx.chat_data["pending_sales"] = {
                    "data": sales_data,
                    "user": name,
                }

                buttons = [
                    [
                        InlineKeyboardButton(
                            "✅ Confirm & Save",
                            callback_data="sales:confirm",
                        ),
                        InlineKeyboardButton(
                            "✏️ Change",
                            callback_data="sales:change",
                        ),
                    ]
                ]

                sent_msg = await update.message.reply_text(
                    confirm_msg,
                    reply_markup=InlineKeyboardMarkup(buttons),
                    parse_mode="Markdown",
                )
                ctx.chat_data["pending_sales_msg_id"] = sent_msg.message_id
            else:
                await update.message.reply_text(
                    f"📊 Couldn't read that sales report, {name}. "
                    f"Try a clearer photo or different angle."
                )
            return

        # ─── PROBLEM PHOTO ─────────────────────────────────
        if classification == "problem":
            remember(name, f"[Problem photo: {caption}]" if caption else "[Problem photo]",
                     "problem", chat_id, msg_id)
            response = await handle_photo(
                image_bytes, name, caption, "image/jpeg", chat_id, msg_id
            )
            if response:
                await update.message.reply_text(f"⚠️ *Issue Detected*\n\n{response}",
                                                parse_mode="Markdown")
            else:
                await update.message.reply_text(
                    f"⚠️ I see a potential issue in the photo, {name}, "
                    f"but couldn't analyze it in detail. Can you describe the problem?"
                )
            return

        # ─── GENERAL PHOTO ─────────────────────────────────
        response = await handle_photo(
            image_bytes, name, caption, "image/jpeg", chat_id, msg_id
        )

        if response:
            await update.message.reply_text(f"📷 {response}")
        else:
            await update.message.reply_text(
                f"📷 I see the photo, {name}, but couldn't analyze it. "
                f"Check that GEMINI_API_KEY is set."
            )
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await update.message.reply_text(
            f"📷 Couldn't process that photo, {name}. What did you want to show?"
        )


# ─── Video Message Handler ────────────────────────────────

# Max video size for Gemini free tier (20MB to be safe)
_MAX_VIDEO_BYTES = 20 * 1024 * 1024


async def handle_video_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle videos — classify and analyze with Gemini (free tier)."""
    msg = update.message
    if not msg:
        return

    # Support both regular videos and video notes (round videos)
    video = msg.video or msg.video_note
    if not video:
        return

    name = user_name(update)
    caption = msg.caption or ""
    chat_id = update.effective_chat.id
    msg_id = msg.message_id

    # In groups: only process if tagged
    if _is_group_chat(update) and not _bot_is_tagged(update, ctx):
        remember(name, f"[Sent a video: {caption}]" if caption else "[Sent a video]", "video", chat_id)
        return

    # Check file size — Gemini free tier has limits
    file_size = video.file_size or 0
    if file_size > _MAX_VIDEO_BYTES:
        await msg.reply_text(
            f"🎥 That video is too large ({file_size // (1024*1024)}MB). "
            f"Please send a shorter clip (under 20MB) so I can analyze it for free."
        )
        return

    try:
        video_file = await ctx.bot.get_file(video.file_id)
        video_data = await video_file.download_as_bytearray()
        video_bytes = bytes(video_data)

        # Determine mime type
        mime_type = video.mime_type or "video/mp4"

        await msg.reply_text("🎥 Analyzing video...")

        # Classify the video
        classification = await classify_video(video_bytes, mime_type)

        # ─── SALES REPORT VIDEO ────────────────────────
        if classification == "sales_report":
            await msg.reply_text("📊 Detected a sales report — extracting data...")
            sales_data = await process_sales_report(video_bytes, name, caption, mime_type)

            if sales_data:
                remember(name, f"[Sales Report Video: {sales_data.get('date', 'unknown')} "
                         f"RM{sales_data.get('total_sales', 0):.2f}]",
                         "sales_report", chat_id, msg_id)

                payment_lines = ""
                for p in sales_data.get("payment_breakdown", []):
                    method = p.get("method", "Unknown")
                    amount = float(p.get("amount", 0) or 0)
                    payment_lines += f"\n  • {method}: RM{amount:.2f}"

                total_sales = float(sales_data.get("total_sales", 0) or 0)
                bill_count = sales_data.get("bill_count", 0) or 0
                total_pax = sales_data.get("total_pax", 0) or 0
                discount = float(sales_data.get("total_discount", 0) or 0)
                void = float(sales_data.get("total_void", 0) or 0)
                refund = float(sales_data.get("total_refund", 0) or 0)
                report_date = sales_data.get("date", "Unknown")
                report_user = sales_data.get("user", name)

                confirm_msg = (
                    f"📊 *Daily Sales Report (from video)*\n\n"
                    f"📅 Date: {report_date}\n"
                    f"👤 Submitted by: {name}\n"
                    f"🧑‍💼 Cashier: {report_user}\n\n"
                    f"💰 *Total Sales: RM{total_sales:.2f}*\n"
                    f"🧾 Bills: {bill_count}\n"
                    f"👥 Pax: {total_pax}\n\n"
                    f"💳 *Payment Breakdown:*{payment_lines}\n\n"
                )

                extras = []
                if discount > 0:
                    extras.append(f"🏷️ Discount: RM{discount:.2f}")
                if void > 0:
                    extras.append(f"🚫 Void: RM{void:.2f}")
                if refund > 0:
                    extras.append(f"↩️ Refund: RM{refund:.2f}")
                if extras:
                    confirm_msg += "\n".join(extras) + "\n\n"

                if sales_data.get("notes"):
                    confirm_msg += f"📝 Notes: {sales_data['notes']}\n\n"

                confirm_msg += "_Reply 'yes' to confirm, or tell me what to change._"

                ctx.chat_data["pending_sales"] = {
                    "data": sales_data,
                    "user": name,
                }

                buttons = [
                    [
                        InlineKeyboardButton("✅ Confirm & Save", callback_data="sales:confirm"),
                        InlineKeyboardButton("✏️ Change", callback_data="sales:change"),
                    ]
                ]

                sent_msg = await msg.reply_text(
                    confirm_msg,
                    reply_markup=InlineKeyboardMarkup(buttons),
                    parse_mode="Markdown",
                )
                ctx.chat_data["pending_sales_msg_id"] = sent_msg.message_id
            else:
                await msg.reply_text(
                    f"📊 Couldn't read the sales report from that video, {name}. "
                    f"Try a clearer recording or a screenshot instead."
                )
            return

        # ─── RECEIPT VIDEO ─────────────────────────────
        if classification == "receipt":
            processing_msg = await msg.reply_text("🧾 Detected a receipt — extracting data...")
            ctx.chat_data["pending_receipt_msg_ids"] = []
            _track_receipt_msg(ctx, processing_msg.message_id)
            receipt_data = await process_receipt(video_bytes, name, caption, mime_type)

            if receipt_data:
                _fix_receipt_total(receipt_data)
                _fix_receipt_paid_by(receipt_data, name)
                remember(name, f"[Receipt Video: {receipt_data.get('supplier', '?')} "
                         f"RM{receipt_data.get('total', 0):.2f}]",
                         "receipt", chat_id, msg_id)

                confirm_msg = _build_receipt_confirm_msg(receipt_data, name)

                ctx.chat_data["pending_receipt"] = {
                    "data": receipt_data,
                    "image_bytes": video_bytes,
                    "user": name,
                    "caption": caption,
                }

                sent_msg = await msg.reply_text(
                    confirm_msg,
                    reply_markup=_receipt_confirm_buttons(),
                    parse_mode="Markdown",
                )
                ctx.chat_data["pending_receipt_msg_id"] = sent_msg.message_id
                _track_receipt_msg(ctx, sent_msg.message_id)
            else:
                await msg.reply_text(
                    f"🧾 Couldn't read that receipt from the video, {name}. "
                    f"Try a photo instead."
                )
            return

        # ─── PROBLEM VIDEO ─────────────────────────────
        if classification == "problem":
            remember(name, f"[Problem video: {caption}]" if caption else "[Problem video]",
                     "problem", chat_id, msg_id)
            response = await handle_video(
                video_bytes, name, caption, mime_type, chat_id, msg_id
            )
            if response:
                await msg.reply_text(f"⚠️ *Issue Detected*\n\n{response}",
                                     parse_mode="Markdown")
            else:
                await msg.reply_text(
                    f"⚠️ I see a potential issue in the video, {name}, "
                    f"but couldn't analyze it in detail. Can you describe the problem?"
                )
            return

        # ─── GENERAL VIDEO ─────────────────────────────
        response = await handle_video(
            video_bytes, name, caption, mime_type, chat_id, msg_id
        )

        if response:
            await msg.reply_text(f"🎥 {response}")
        else:
            await msg.reply_text(
                f"🎥 I see the video, {name}, but couldn't analyze it. "
                f"Try sending a shorter clip or a photo instead."
            )

    except Exception as e:
        logger.error(f"Video error: {e}")
        await msg.reply_text(
            f"🎥 Couldn't process that video, {name}. "
            f"Try a shorter clip or send a photo instead."
        )


def _fix_receipt_total(receipt_data: dict):
    """If total is 0 but subtotal or item prices exist, fix it."""
    total = float(receipt_data.get('total') or 0)
    subtotal = float(receipt_data.get('subtotal') or 0)
    tax = float(receipt_data.get('tax') or 0)
    discount = float(receipt_data.get('discount') or 0)
    # Prefer total (final amount paid). Only fall back to subtotal if total is 0.
    if total == 0 and subtotal > 0:
        receipt_data['total'] = subtotal + tax - discount
    elif total == 0 and subtotal == 0:
        # Sum up item prices
        item_total = sum(
            float(i.get('price') or 0) * float(i.get('qty') or 1)
            for i in receipt_data.get('items', [])
        )
        if item_total > 0:
            receipt_data['total'] = item_total - discount
            receipt_data['subtotal'] = item_total
    # If subtotal is 0 but total exists, set subtotal = total (for display)
    if subtotal == 0 and total > 0:
        receipt_data['subtotal'] = total


def _track_receipt_msg(ctx, msg_id: int):
    """Track a bot message ID as part of the receipt conversation.
    Users can reply to ANY of these and it counts as a receipt reply."""
    ids = ctx.chat_data.setdefault("pending_receipt_msg_ids", [])
    if msg_id not in ids:
        ids.append(msg_id)
    # Keep list bounded
    if len(ids) > 20:
        ctx.chat_data["pending_receipt_msg_ids"] = ids[-20:]


def _clear_receipt_tracking(ctx):
    """Clear all receipt tracking state after confirmation or cancellation."""
    ctx.chat_data.pop("pending_receipt", None)
    ctx.chat_data.pop("pending_receipt_msg_id", None)
    ctx.chat_data.pop("pending_receipt_msg_ids", None)


def _fix_receipt_total_from_items(receipt_data: dict):
    """Recalculate total & subtotal from item prices after item-level edits."""
    discount = float(receipt_data.get('discount') or 0)
    tax = float(receipt_data.get('tax') or 0)
    item_total = sum(
        float(i.get('price') or 0) * float(i.get('qty') or 1)
        for i in receipt_data.get('items', [])
    )
    receipt_data['subtotal'] = item_total
    receipt_data['total'] = item_total + tax - discount


def _fix_receipt_paid_by(receipt_data: dict, sender_name: str):
    """Replace generic/placeholder paid_by with actual sender name."""
    paid_by = (receipt_data.get("paid_by") or "").strip()
    # AI sometimes returns these generic strings instead of the real name
    generic = {
        "staff member", "staff", "unknown", "n/a", "na", "none",
        "not specified", "customer", "buyer", "user", "sender",
    }
    if not paid_by or paid_by.lower() in generic:
        receipt_data["paid_by"] = sender_name


def _build_receipt_confirm_msg(receipt_data: dict, name: str) -> str:
    """Build the receipt confirmation message text from receipt_data."""
    _r_total = float(receipt_data.get('total') or receipt_data.get('subtotal') or 0)
    _r_subtotal = float(receipt_data.get('subtotal') or 0)
    _r_discount = float(receipt_data.get('discount') or 0)

    from config import ITEM_CATEGORIES, DEFAULT_CATEGORY
    items_text = ""
    for item in receipt_data.get("items", []):
        _item_price = float(item.get('price') or 0)
        _item_qty = item.get('qty', '?')
        _item_cat = item.get('category', DEFAULT_CATEGORY)
        _cat_label = ITEM_CATEGORIES.get(_item_cat, ITEM_CATEGORIES.get(DEFAULT_CATEGORY, ""))
        items_text += (
            f"\n  \U0001f4e6 {item.get('name', '?')} "
            f"x{_item_qty} "
            f"@ RM{_item_price:.2f} [{_cat_label}]"
        )

    paid_by = receipt_data.get("paid_by", "") or name

    confirm_msg = (
        f"\U0001f9fe *Receipt Scanned*\n\n"
        f"\U0001f4c5 Date: {receipt_data.get('date', 'Unknown')}\n"
        f"\U0001f3ea Shop: {receipt_data.get('supplier', 'Unknown')}\n"
        f"\U0001f4b3 Paid by: {paid_by}\n"
        f"\U0001f4cb Items:{items_text}\n\n"
    )
    if _r_discount > 0:
        confirm_msg += f"\U0001f3f7️ Discount: -RM{_r_discount:.2f}\n"
    confirm_msg += f"\U0001f4b0 *Total: RM{_r_total:.2f}*"
    confirm_msg += "\n\n_Reply 'yes' to confirm, or tell me what to change (e.g. 'paid by Eric')._"
    return confirm_msg


def _receipt_confirm_buttons():
    """Return the standard confirm/amend button rows."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm & Save", callback_data="receipt:confirm"),
            InlineKeyboardButton("✏️ Change", callback_data="receipt:change"),
        ],
        [
            InlineKeyboardButton("🏷️ Change Category", callback_data="receipt:chgcat"),
        ],
    ])


async def _detect_new_items(items: list, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """After a receipt is confirmed, ask about items that look genuinely new
    (never seen before this receipt) so the user can mark them Regular vs One-off."""
    try:
        from storage import get_alias_store
        alias_store = get_alias_store()
        today_str = now_sg().strftime("%d/%m/%y")
        new_items = []
        for receipt_item in items:
            r_name = receipt_item.get("name", "")
            if not r_name:
                continue
            norm = normalize_item_name(r_name)

            # Check if it was in stock BEFORE this receipt (any date other than today)
            was_known = False
            for date_str, date_items in store.data.get("stock_history", {}).items():
                if date_str == today_str:
                    continue  # skip today (just added by this receipt)
                for hist_item in date_items:
                    if normalize_item_name(hist_item) == norm:
                        was_known = True
                        break
                if was_known:
                    break

            # Also check aliases and one-off purchase history
            if not was_known:
                was_known = alias_store.resolve(r_name) != r_name  # known alias

            if not was_known and store.is_known_oneoff(r_name):
                # It's a known one-off — record another purchase but don't ask again
                was_known = True
                store.record_oneoff_item(r_name)
                store.remove_stock(r_name)  # one-off — don't keep tracking it in stock

            if not was_known:
                new_items.append({"name": r_name, "qty": receipt_item.get("qty", 1)})

        if new_items:
            ctx.chat_data["new_receipt_items"] = new_items
            ctx.chat_data["new_receipt_items_idx"] = 0
            item = new_items[0]["name"]
            buttons = [
                [
                    InlineKeyboardButton("🔄 Regular (track stock)", callback_data="newitem:regular"),
                    InlineKeyboardButton("🧪 One-off (don't track)", callback_data="newitem:oneoff"),
                ]
            ]
            await update.effective_message.reply_text(
                f"🆕 *New item detected:* {item}\n\n"
                f"Is this a regular stock item or a one-off purchase?",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error(f"New item detection error: {e}")


async def cb_newitem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle Regular/One-off selection for new receipt items."""
    query = update.callback_query
    await query.answer()

    choice = query.data.split(":", 1)[1]  # "regular" or "oneoff"
    items = ctx.chat_data.get("new_receipt_items", [])
    idx = ctx.chat_data.get("new_receipt_items_idx", 0)

    if idx >= len(items):
        return

    item = items[idx]  # {"name": ..., "qty": ...}
    item_name = item["name"]
    item_qty = item.get("qty", 1)

    if choice == "oneoff":
        store.record_oneoff_item(item_name)
        # No need to remove_stock since we never added it
        await query.edit_message_text(
            f"🧪 Got it — *{item_name}* marked as one-off (won't track in stock).",
            parse_mode="Markdown",
        )
    else:
        store.add_receipt_to_stock(item_name, item_qty)
        await query.edit_message_text(
            f"🔄 Got it — *{item_name}* will be tracked as regular stock.",
            parse_mode="Markdown",
        )

    # Move to next new item
    idx += 1
    ctx.chat_data["new_receipt_items_idx"] = idx

    if idx < len(items):
        next_item = items[idx]["name"]
        buttons = [
            [
                InlineKeyboardButton("🔄 Regular (track stock)", callback_data="newitem:regular"),
                InlineKeyboardButton("🧪 One-off (don't track)", callback_data="newitem:oneoff"),
            ]
        ]
        await query.message.reply_text(
            f"🆕 *New item detected:* {next_item}\n\n"
            f"Is this a regular stock item or a one-off purchase?",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )


async def cb_duplicate_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle duplicate receipt confirmation — user says Yes (save anyway) or No (skip)."""
    query = update.callback_query
    await query.answer()

    choice = query.data.split(":", 1)[1]  # "save" or "skip"
    pending = ctx.chat_data.get("pending_receipt")

    if not pending:
        await query.edit_message_text("⚠️ No pending receipt to process.")
        return

    if choice == "skip":
        _clear_receipt_tracking(ctx)
        await query.edit_message_text("🚫 Receipt skipped — duplicate not saved.")
        return

    # choice == "save" — proceed with normal confirmation
    name = user_name(update)
    # Route to the shared confirmation logic
    await query.edit_message_text("⏳ Saving receipt (confirmed not a duplicate)...")
    await _confirm_receipt(pending, name, update, ctx, skip_duplicate_check=True)


def _merge_receipt_items(items: list) -> list:
    """Merge duplicate items on same receipt into one entry with summed qty."""
    merged = {}
    for item in items:
        name = item.get("name", "").strip()
        if not name:
            continue
        key = normalize_item_name(name)
        if key in merged:
            try:
                merged[key]["qty"] = int(merged[key].get("qty", 1)) + int(item.get("qty", 1))
            except (ValueError, TypeError):
                merged[key]["qty"] = int(merged[key].get("qty", 1)) + 1
            # Keep the higher unit price (they should be same)
            try:
                existing_price = float(merged[key].get("price", 0) or 0)
                new_price = float(item.get("price", 0) or 0)
                if new_price > existing_price:
                    merged[key]["price"] = new_price
            except (ValueError, TypeError):
                pass
        else:
            merged[key] = dict(item)  # copy
    return list(merged.values())


async def _confirm_receipt(pending: dict, confirmed_by: str,
                           update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                           skip_duplicate_check: bool = False):
    """Shared receipt confirmation logic — used by both button and reply."""
    receipt_data = pending["data"]
    image_bytes = pending["image_bytes"]
    receipt_user = pending["user"]

    # Check for duplicate receipt
    if not skip_duplicate_check:
        supplier_check = receipt_data.get("supplier", "Unknown")
        receipt_total_check = float(receipt_data.get("total") or 0)
        receipt_date_check = receipt_data.get("date", now_sg().date().isoformat())
        items_check = receipt_data.get("items", [])

        existing = store.check_duplicate_receipt(
            supplier_check, receipt_date_check, receipt_total_check, items_check
        )
        if existing:
            buttons = [
                [
                    InlineKeyboardButton("✅ Yes, save it", callback_data="dupcheck:save"),
                    InlineKeyboardButton("❌ No, skip", callback_data="dupcheck:skip"),
                ]
            ]
            await update.effective_message.reply_text(
                f"⚠️ *Possible duplicate receipt detected!*\n\n"
                f"A receipt from *{existing.get('supplier', supplier_check)}* on {existing.get('date', receipt_date_check)} "
                f"for RM{existing.get('total', receipt_total_check):.2f} ({existing.get('item_count', len(items_check))} items) "
                f"was already saved by {existing.get('recorded_by', '?')}.\n\n"
                f"Is this a *new* purchase or the same one?",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown",
            )
            return  # Wait for user to click button

    status_msg = await update.effective_message.reply_text("⏳ Saving receipt, updating stock & records...")

    results = []

    # 1. Save receipt to Google Sheets + Drive
    try:
        from google_integration import (
            log_expense_detail, upload_receipt_to_drive,
            update_monthly_expenses,
        )

        # Upload image to Google Drive
        supplier = receipt_data.get("supplier", "Unknown")
        receipt_date = receipt_data.get("date", now_sg().date().isoformat())
        filename = f"{receipt_date}_receipt_{supplier.replace(' ', '_')}.jpg"

        drive_link = upload_receipt_to_drive(
            image_bytes, filename, "image/jpeg",
            description=f"Receipt from {supplier}, RM{receipt_data.get('total', 0):.2f}",
        )
        if drive_link:
            results.append(f"📁 Saved to Drive")

        # 2. Update stock + log expense details from receipt items
        items = receipt_data.get("items", [])
        items = _merge_receipt_items(items)
        # Always default paid_by to the person who sent the receipt
        paid_by = receipt_data.get("paid_by", "") or receipt_user
        receipt_data["paid_by"] = paid_by  # ensure it's saved in data too
        expense_date = receipt_data.get("date", now_sg().strftime("%d/%m/%y"))
        # Convert ISO date to dd/mm/yy if needed
        if expense_date and "-" in expense_date and len(expense_date) == 10:
            try:
                from datetime import datetime
                expense_date = datetime.strptime(expense_date, "%Y-%m-%d").strftime("%d/%m/%y")
            except ValueError:
                pass
        detail_count = 0

        # Use receipt's final total — the amount actually paid
        receipt_total = float(receipt_data.get("total") or 0)

        import re as _re
        for item in items:
            item_name = clean_item_name(item.get("name", ""))
            qty = item.get("qty", 0)
            price = item.get("price", 0)
            category = item.get("category", "ingredients")
            try:
                qty_nums = _re.findall(r'[\d.]+', str(qty))
                qty_int = int(float(qty_nums[0])) if qty_nums else 1
            except (ValueError, IndexError):
                qty_int = 1

            if item_name:
                # For single-item receipts: log the receipt total directly
                # For multi-item: log qty * unit_price per item
                if len(items) == 1 and receipt_total > 0:
                    item_amount = receipt_total
                else:
                    item_amount = qty_int * (float(price) if price else 0)

                try:
                    logged_detail = log_expense_detail(
                        expense_date=expense_date,
                        supplier=supplier,
                        item_name=item_name,
                        qty=qty_int,
                        amount=item_amount,
                        category=category or "ingredients",
                        paid_by=paid_by,
                        receipt_link=drive_link or "",
                        recorded_by=receipt_user,
                    )
                    if logged_detail:
                        detail_count += 1
                    else:
                        logger.error(f"log_expense_detail returned False for {item_name}")
                except Exception as e:
                    logger.error(f"Expense detail error for {item_name}: {e}")

                # Only update stock for items already being tracked.
                # New items are handled by _detect_new_items (Regular vs One-off).
                existing_stock = store.data.get("stock_current", {})
                is_known = any(
                    normalize_item_name(k) == normalize_item_name(item_name)
                    for k in existing_stock
                )
                if is_known:
                    store.add_receipt_to_stock(item_name, qty_int)

        if items:
            results.append(f"📦 {len(items)} items updated in stock")

        # Auto-add low stock items to shopping list
        try:
            low_items = store.check_low_stock([item.get("name", "") for item in items if item.get("name")])
            if low_items:
                await _auto_add_low_to_shopping(low_items, store, ctx.bot, update.effective_chat.id)
        except Exception as e:
            logger.error(f"Low stock auto-add error: {e}")

        # Auto-clear matching shopping list items
        try:
            shopping = store.get_shopping_list()
            cleared_items = []
            for receipt_item in items:
                r_name = receipt_item.get("name", "").lower()
                if not r_name:
                    continue
                r_norm = normalize_item_name(r_name)
                for idx, shop_item in enumerate(shopping):
                    if shop_item.get("bought"):
                        continue
                    s_norm = normalize_item_name(shop_item.get("item", ""))
                    # Match if either contains the other, or normalized names match
                    if (r_norm in s_norm or s_norm in r_norm or r_norm == s_norm):
                        store.mark_bought(idx)
                        cleared_items.append(shop_item["item"])
                        break
            if cleared_items:
                results.append(f"🛒 Auto-cleared from shopping: {', '.join(cleared_items)}")
        except Exception as e:
            logger.error(f"Auto-clear shopping error: {e}")

        if detail_count:
            results.append(f"📋 {detail_count} items logged to Expenses (paid by {paid_by})")

        # Update monthly expense aggregation + monthly summary
        try:
            update_monthly_expenses()
        except Exception as e:
            logger.error(f"Monthly expense aggregation error: {e}")
        try:
            from google_integration import generate_monthly_summary
            generate_monthly_summary()
        except Exception as e:
            logger.error(f"Monthly summary update error: {e}")

    except ImportError:
        results.append("⚠️ Google integration not configured")
    except Exception as e:
        logger.error(f"Receipt save error: {e}")
        results.append(f"⚠️ Partial save: {e}")

    await _detect_new_items(items, update, ctx)

    _clear_receipt_tracking(ctx)

    # Record receipt hash for duplicate detection
    try:
        store.record_receipt_hash(
            supplier=receipt_data.get("supplier", "Unknown"),
            receipt_date=receipt_data.get("date", now_sg().date().isoformat()),
            total=float(receipt_data.get("total") or 0),
            items=receipt_data.get("items", []),
            recorded_by=confirmed_by,
        )
    except Exception as e:
        logger.error(f"Receipt hash recording error: {e}")

    summary = "\n".join(f"  {r}" for r in results) if results else "  Saved locally"
    await status_msg.edit_text(
        f"✅ *Receipt Confirmed*\n\n{summary}\n\n"
        f"Submitted by {receipt_user}\n"
        f"Confirmed by {confirmed_by}",
        parse_mode="Markdown",
    )


async def cb_receipt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle receipt confirm/change callbacks (button press)."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    name = user_name(update)

    pending = ctx.chat_data.get("pending_receipt")
    if not pending:
        await query.edit_message_text("⚠️ No pending receipt to process.")
        return

    if action == "change":
        await query.edit_message_text(
            "✏️ What needs to be changed?\n\n"
            "Just reply with what's wrong, e.g.:\n"
            "• `paid by Eric`\n"
            "• `supplier Giant`\n"
            "• `total 15.90`\n"
            "• `date 2026-08-20`\n"
            "• `category useables`\n\n"
            "_I'll update and show you again._",
            parse_mode="Markdown",
        )
        # Track this message so reply to it is also caught
        ctx.chat_data["pending_receipt_msg_id"] = query.message.message_id
        _track_receipt_msg(ctx, query.message.message_id)
        ctx.chat_data["pending_receipt_changing"] = True
        return

    if action == "chgcat":
        await cb_receipt_chgcat(update, ctx)
        return

    if action == "back":
        confirm_msg = _build_receipt_confirm_msg(pending["data"], pending["user"])
        await query.edit_message_text(
            confirm_msg,
            reply_markup=_receipt_confirm_buttons(),
            parse_mode="Markdown",
        )
        return

    # action == "confirm" — use shared logic
    receipt_data = pending["data"]
    image_bytes = pending["image_bytes"]
    receipt_user = pending["user"]

    # Check for duplicate receipt
    supplier_check = receipt_data.get("supplier", "Unknown")
    receipt_total_check = float(receipt_data.get("total") or 0)
    receipt_date_check = receipt_data.get("date", now_sg().date().isoformat())
    items_check = receipt_data.get("items", [])

    existing = store.check_duplicate_receipt(
        supplier_check, receipt_date_check, receipt_total_check, items_check
    )
    if existing:
        buttons = [
            [
                InlineKeyboardButton("✅ Yes, save it", callback_data="dupcheck:save"),
                InlineKeyboardButton("❌ No, skip", callback_data="dupcheck:skip"),
            ]
        ]
        await query.edit_message_text(
            f"⚠️ *Possible duplicate receipt detected!*\n\n"
            f"A receipt from *{existing.get('supplier', supplier_check)}* on {existing.get('date', receipt_date_check)} "
            f"for RM{existing.get('total', receipt_total_check):.2f} ({existing.get('item_count', len(items_check))} items) "
            f"was already saved by {existing.get('recorded_by', '?')}.\n\n"
            f"Is this a *new* purchase or the same one?",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
        return  # Wait for user to click button

    await query.edit_message_text("⏳ Saving receipt, updating stock & records...")

    results = []

    try:
        from google_integration import (
            log_expense_detail, upload_receipt_to_drive,
            update_monthly_expenses,
        )

        supplier = receipt_data.get("supplier", "Unknown")
        receipt_date = receipt_data.get("date", now_sg().date().isoformat())
        filename = f"{receipt_date}_receipt_{supplier.replace(' ', '_')}.jpg"

        drive_link = upload_receipt_to_drive(
            image_bytes, filename, "image/jpeg",
            description=f"Receipt from {supplier}, RM{receipt_data.get('total', 0):.2f}",
        )
        if drive_link:
            results.append(f"📁 Saved to Drive")

        # Update stock + log expense details from receipt items
        items = receipt_data.get("items", [])
        items = _merge_receipt_items(items)
        paid_by = receipt_data.get("paid_by", "") or receipt_user
        receipt_data["paid_by"] = paid_by
        expense_date = receipt_data.get("date", now_sg().strftime("%d/%m/%y"))
        # Convert ISO date to dd/mm/yy if needed
        if expense_date and "-" in expense_date and len(expense_date) == 10:
            try:
                from datetime import datetime
                expense_date = datetime.strptime(expense_date, "%Y-%m-%d").strftime("%d/%m/%y")
            except ValueError:
                pass
        detail_count = 0

        # Use receipt's final total — the amount actually paid
        receipt_total = float(receipt_data.get("total") or 0)

        import re as _re
        for item in items:
            item_name = clean_item_name(item.get("name", ""))
            qty = item.get("qty", 0)
            price = item.get("price", 0)
            category = item.get("category", "ingredients")
            try:
                qty_nums = _re.findall(r'[\d.]+', str(qty))
                qty_int = int(float(qty_nums[0])) if qty_nums else 1
            except (ValueError, IndexError):
                qty_int = 1

            if item_name:
                # For single-item receipts: log the receipt total directly
                # For multi-item: log qty * unit_price per item
                if len(items) == 1 and receipt_total > 0:
                    item_amount = receipt_total
                else:
                    item_amount = qty_int * (float(price) if price else 0)

                try:
                    logged_detail = log_expense_detail(
                        expense_date=expense_date,
                        supplier=supplier,
                        item_name=item_name,
                        qty=qty_int,
                        amount=item_amount,
                        category=category or "ingredients",
                        paid_by=paid_by,
                        receipt_link=drive_link or "",
                        recorded_by=receipt_user,
                    )
                    if logged_detail:
                        detail_count += 1
                    else:
                        logger.error(f"log_expense_detail returned False for {item_name}")
                except Exception as e:
                    logger.error(f"Expense detail error for {item_name}: {e}")

                # Only update stock for items already being tracked.
                # New items are handled by _detect_new_items (Regular vs One-off).
                existing_stock = store.data.get("stock_current", {})
                is_known = any(
                    normalize_item_name(k) == normalize_item_name(item_name)
                    for k in existing_stock
                )
                if is_known:
                    store.add_receipt_to_stock(item_name, qty_int)

        if items:
            results.append(f"📦 {len(items)} items updated in stock")

        # Auto-add low stock items to shopping list
        try:
            low_items = store.check_low_stock([item.get("name", "") for item in items if item.get("name")])
            if low_items:
                await _auto_add_low_to_shopping(low_items, store, ctx.bot, update.effective_chat.id)
        except Exception as e:
            logger.error(f"Low stock auto-add error: {e}")

        # Auto-clear matching shopping list items
        try:
            shopping = store.get_shopping_list()
            cleared_items = []
            for receipt_item in items:
                r_name = receipt_item.get("name", "").lower()
                if not r_name:
                    continue
                r_norm = normalize_item_name(r_name)
                for idx, shop_item in enumerate(shopping):
                    if shop_item.get("bought"):
                        continue
                    s_norm = normalize_item_name(shop_item.get("item", ""))
                    # Match if either contains the other, or normalized names match
                    if (r_norm in s_norm or s_norm in r_norm or r_norm == s_norm):
                        store.mark_bought(idx)
                        cleared_items.append(shop_item["item"])
                        break
            if cleared_items:
                results.append(f"🛒 Auto-cleared from shopping: {', '.join(cleared_items)}")
        except Exception as e:
            logger.error(f"Auto-clear shopping error: {e}")

        if detail_count:
            results.append(f"📋 {detail_count} items logged to Expenses (paid by {paid_by})")

        # Update monthly expense aggregation + monthly summary
        try:
            update_monthly_expenses()
        except Exception as e:
            logger.error(f"Monthly expense aggregation error: {e}")
        try:
            from google_integration import generate_monthly_summary
            generate_monthly_summary()
        except Exception as e:
            logger.error(f"Monthly summary update error: {e}")

    except ImportError:
        results.append("⚠️ Google integration not configured")
    except Exception as e:
        logger.error(f"Receipt save error: {e}")
        results.append(f"⚠️ Partial save: {e}")

    await _detect_new_items(items, update, ctx)

    _clear_receipt_tracking(ctx)

    # Record receipt hash for duplicate detection
    try:
        store.record_receipt_hash(
            supplier=receipt_data.get("supplier", "Unknown"),
            receipt_date=receipt_data.get("date", now_sg().date().isoformat()),
            total=float(receipt_data.get("total") or 0),
            items=receipt_data.get("items", []),
            recorded_by=name,
        )
    except Exception as e:
        logger.error(f"Receipt hash recording error: {e}")

    summary = "\n".join(f"  {r}" for r in results) if results else "  Saved locally"
    await query.edit_message_text(
        f"✅ *Receipt Confirmed*\n\n{summary}\n\n"
        f"Submitted by {receipt_user}\n"
        f"Confirmed by {name}",
        parse_mode="Markdown",
    )


async def cb_receipt_chgcat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show item picker for category change."""
    query = update.callback_query
    await query.answer()
    pending = ctx.chat_data.get("pending_receipt")
    if not pending:
        await query.edit_message_text("⚠️ No pending receipt.")
        return
    items = pending["data"].get("items", [])
    if not items:
        await query.edit_message_text("⚠️ No items in receipt.")
        return
    from config import ITEM_CATEGORIES, DEFAULT_CATEGORY
    buttons = []
    for i, item in enumerate(items):
        cat = item.get("category", DEFAULT_CATEGORY)
        cat_label = ITEM_CATEGORIES.get(cat, ITEM_CATEGORIES.get(DEFAULT_CATEGORY, ""))
        buttons.append([InlineKeyboardButton(
            f"{i+1}. {item.get('name', '?')} [{cat_label}]",
            callback_data=f"catitem:{i}",
        )])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="receipt:back")])
    await query.edit_message_text(
        "🏷️ *Which item's category do you want to change?*",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def cb_catitem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show category picker for a specific item."""
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":", 1)[1])
    pending = ctx.chat_data.get("pending_receipt")
    if not pending:
        await query.edit_message_text("⚠️ No pending receipt.")
        return
    items = pending["data"].get("items", [])
    if idx >= len(items):
        await query.edit_message_text("⚠️ Item not found.")
        return
    item_name = items[idx].get("name", "?")
    from config import ITEM_CATEGORIES
    buttons = []
    row = []
    for key, label in ITEM_CATEGORIES.items():
        row.append(InlineKeyboardButton(label, callback_data=f"setcat:{idx}:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="receipt:chgcat")])
    await query.edit_message_text(
        f"🏷️ Pick category for *{item_name}*:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def cb_setcat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Apply selected category to an item and re-show confirmation."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":", 2)  # setcat:idx:category
    idx = int(parts[1])
    new_cat = parts[2]
    pending = ctx.chat_data.get("pending_receipt")
    if not pending:
        await query.edit_message_text("⚠️ No pending receipt.")
        return
    items = pending["data"].get("items", [])
    if idx >= len(items):
        await query.edit_message_text("⚠️ Item not found.")
        return
    from config import ITEM_CATEGORIES
    items[idx]["category"] = new_cat
    cat_label = ITEM_CATEGORIES.get(new_cat, new_cat)
    item_name = items[idx].get("name", "?")
    confirm_msg = _build_receipt_confirm_msg(pending["data"], pending["user"])
    await query.edit_message_text(
        f"✅ {item_name} → {cat_label}\n\n{confirm_msg}",
        reply_markup=_receipt_confirm_buttons(),
        parse_mode="Markdown",
    )


# ─── Sales Report Confirmation ────────────────────────────

async def _confirm_sales(pending: dict, confirmed_by: str,
                          update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Shared sales confirmation logic — used by both button and reply."""
    sales_data = pending["data"]
    sales_user = pending["user"]

    status_msg = await update.message.reply_text("⏳ Saving sales report to Google Sheets...")

    results = []

    try:
        from google_integration import log_daily_sales

        logged = log_daily_sales(
            report_date=sales_data.get("date", now_sg().date().isoformat()),
            total_sales=float(sales_data.get("total_sales", 0) or 0),
            bill_count=int(sales_data.get("bill_count", 0) or 0),
            total_pax=int(sales_data.get("total_pax", 0) or 0),
            payment_breakdown=sales_data.get("payment_breakdown", []),
            total_discount=float(sales_data.get("total_discount", 0) or 0),
            total_void=float(sales_data.get("total_void", 0) or 0),
            total_refund=float(sales_data.get("total_refund", 0) or 0),
            other_charge=float(sales_data.get("other_charge", 0) or 0),
            cashier=sales_data.get("user", sales_user),
            recorded_by=sales_user,
            notes=sales_data.get("notes", ""),
        )
        if logged:
            results.append("📊 Logged to Daily Sales")

        # Auto-generate previous month's summary if this is a new month
        try:
            from google_integration import generate_monthly_summary, get_daily_sales_for_month
            report_date = sales_data.get("date", now_sg().date().isoformat())
            report_month = report_date[:7]  # YYYY-MM
            current_month = now_sg().date().strftime("%Y-%m")

            if report_month == current_month:
                # Check if previous month has sales but no summary yet
                prev_year = int(current_month[:4])
                prev_mon = int(current_month[5:7]) - 1
                if prev_mon == 0:
                    prev_mon = 12
                    prev_year -= 1
                prev_month = f"{prev_year}-{prev_mon:02d}"

                prev_sales = get_daily_sales_for_month(prev_month)
                if prev_sales:
                    summary_data = generate_monthly_summary(prev_month)
                    if summary_data and summary_data.get("total_revenue", 0) > 0:
                        results.append(
                            f"📈 Auto-generated {prev_month} summary: "
                            f"Revenue RM{summary_data['total_revenue']:.2f}, "
                            f"Expenses RM{summary_data['total_expenses']:.2f}, "
                            f"Profit RM{summary_data['gross_profit']:.2f}"
                        )
        except Exception as e:
            logger.warning(f"Auto monthly summary failed: {e}")

    except ImportError:
        results.append("⚠️ Google integration not configured")
    except Exception as e:
        logger.error(f"Sales save error: {e}")
        results.append(f"⚠️ Partial save: {e}")

    ctx.chat_data.pop("pending_sales", None)
    ctx.chat_data.pop("pending_sales_msg_id", None)

    summary = "\n".join(f"  {r}" for r in results) if results else "  Saved locally"
    await status_msg.edit_text(
        f"✅ *Sales Report Confirmed*\n\n{summary}\n\n"
        f"Submitted by {sales_user}\n"
        f"Confirmed by {confirmed_by}",
        parse_mode="Markdown",
    )


async def cb_sales(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle sales report confirm/change callbacks (button press)."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    name = user_name(update)

    pending = ctx.chat_data.get("pending_sales")
    if not pending:
        await query.edit_message_text("⚠️ No pending sales report to process.")
        return

    if action == "change":
        await query.edit_message_text(
            "✏️ What needs to be changed?\n\n"
            "Just reply with what's wrong, e.g.:\n"
            "• `total 500`\n"
            "• `date 2026-08-20`\n"
            "• `bills 45`\n\n"
            "_I'll update and show you again._",
            parse_mode="Markdown",
        )
        ctx.chat_data["pending_sales_msg_id"] = query.message.message_id
        return

    # action == "confirm"
    sales_data = pending["data"]
    sales_user = pending["user"]

    await query.edit_message_text("⏳ Saving sales report to Google Sheets...")

    results = []

    try:
        from google_integration import log_daily_sales

        logged = log_daily_sales(
            report_date=sales_data.get("date", now_sg().date().isoformat()),
            total_sales=float(sales_data.get("total_sales", 0) or 0),
            bill_count=int(sales_data.get("bill_count", 0) or 0),
            total_pax=int(sales_data.get("total_pax", 0) or 0),
            payment_breakdown=sales_data.get("payment_breakdown", []),
            total_discount=float(sales_data.get("total_discount", 0) or 0),
            total_void=float(sales_data.get("total_void", 0) or 0),
            total_refund=float(sales_data.get("total_refund", 0) or 0),
            other_charge=float(sales_data.get("other_charge", 0) or 0),
            cashier=sales_data.get("user", sales_user),
            recorded_by=sales_user,
            notes=sales_data.get("notes", ""),
        )
        if logged:
            results.append("📊 Logged to Daily Sales")

    except ImportError:
        results.append("⚠️ Google integration not configured")
    except Exception as e:
        logger.error(f"Sales save error: {e}")
        results.append(f"⚠️ Partial save: {e}")

    ctx.chat_data.pop("pending_sales", None)
    ctx.chat_data.pop("pending_sales_msg_id", None)

    summary = "\n".join(f"  {r}" for r in results) if results else "  Saved locally"
    await query.edit_message_text(
        f"✅ *Sales Report Confirmed*\n\n{summary}\n\n"
        f"Submitted by {sales_user}\n"
        f"Confirmed by {name}",
        parse_mode="Markdown",
    )


# ═══════════════════════════════════════════════════════════
#  💬 NATURAL LANGUAGE / AI CHAT
# ═══════════════════════════════════════════════════════════

# Action type → keywords that map to task descriptions
_ACTION_TASK_KEYWORDS = {
    "update_stock": lambda a: ["stock", a.get("item", "").lower()],
    "log_cleaning": lambda a: ["clean", a.get("zone", "").lower()],
    "add_shopping": lambda a: ["buy", "shopping", a.get("item", "").lower()],
    "mark_bought": lambda a: ["buy", "bought", a.get("item", "").lower()],
    "checklist_done": lambda a: ["checklist", a.get("checklist", "").lower()],
    "bulk_stock": lambda a: ["stock", "count", "check"],
    "correct_stock": lambda a: ["stock", a.get("item", "").lower()],
}


def _auto_clear_matching_tasks(actions: list):
    """After actions execute, auto-clear pending tasks that match what was just done.
    Uses fuzzy keyword overlap: if 2+ significant words from the action match a task
    description, mark it done."""
    pending = store.get_action_items(status="pending")
    if not pending:
        return

    cleared = []
    for act in actions:
        if not isinstance(act, dict):
            continue
        action_type = act.get("action", "")
        kw_func = _ACTION_TASK_KEYWORDS.get(action_type)
        if not kw_func:
            continue

        keywords = [w for w in kw_func(act) if w and len(w) > 2]
        if not keywords:
            continue

        for idx, task in enumerate(pending):
            if task.get("status") != "pending":
                continue
            desc = task.get("task", "").lower()
            # Count how many keywords appear in the task description
            matches = sum(1 for kw in keywords if kw in desc)
            if matches >= 2:
                store.complete_action_item(idx, "Auto (bot did it)")
                cleared.append(task.get("task", "?"))
                break  # one action clears one task max

    if cleared:
        logger.info(f"Auto-cleared {len(cleared)} tasks: {cleared}")


async def _execute_actions(actions: list, name: str, update: Update):
    """Execute structured actions returned by the AI."""
    feedback = []

    for act in actions:
        if not isinstance(act, dict):
            continue
        action_type = act.get("action", "")

        try:
            if action_type == "update_stock":
                item = act.get("item", "")
                qty = act.get("qty", "1")
                note = act.get("note", "")
                if item:
                    store.update_stock(item, qty, f"{name}: {note}" if note else name)
                    feedback.append(f"📦 {item} → {qty}")
                    # Check low stock
                    low_items = store.check_low_stock([item])
                    if low_items:
                        li = low_items[0]
                        unit = f" {li['unit']}" if li.get('unit') else ""
                        feedback.append(f"⚠️ LOW STOCK: {li['item']} is at {li['qty']} (min: {li['min']}{unit})")
                        await _auto_add_low_to_shopping(low_items, store, update.get_bot(), update.effective_chat.id)

            elif action_type == "log_cleaning":
                zone = act.get("zone", "")
                if zone:
                    store.log_cleaning(zone, name)
                    feedback.append(f"🧹 {zone} ✓")

            elif action_type == "add_shopping":
                item = act.get("item", "")
                urgency = act.get("urgency", "normal")
                if item:
                    store.add_shopping_item(item, name, urgency)
                    emoji = "🔴" if urgency == "urgent" else "🛒"
                    feedback.append(f"{emoji} Added: {item}")

            elif action_type == "mark_bought":
                item_name = act.get("item", "").lower()
                shopping = store.get_shopping_list()
                for i, si in enumerate(shopping):
                    if item_name in si["item"].lower():
                        store.mark_bought(i)
                        feedback.append(f"✅ Bought: {si['item']}")
                        break

            elif action_type == "add_event":
                title = act.get("title", "")
                evt_date = act.get("date", "")
                details = act.get("details", "")
                if title and evt_date:
                    store.add_event(title, evt_date, details, name)
                    feedback.append(f"📅 Event: {title} on {evt_date}")

            elif action_type == "plan_content":
                title = act.get("title", "")
                content_type = act.get("type", "photo")
                planned_date = act.get("date", now_sg().date().isoformat())
                assigned_to = act.get("assigned_to", name)
                notes = act.get("notes", "")
                if title:
                    store.add_content_plan(
                        title=title, content_type=content_type,
                        planned_date=planned_date, assigned_to=assigned_to,
                        added_by=name, notes=notes,
                    )
                    feedback.append(f"📱 Content planned: {title} [{content_type}] on {planned_date}")

            elif action_type == "done_content":
                title = act.get("title", "")
                if title:
                    found = store.complete_content_by_title(title, completed_by=name)
                    if found:
                        store.log_content(title, name)
                        feedback.append(f"📱 Content done: {title} ✅")
                    else:
                        feedback.append(f"📱 Couldn't find planned content matching '{title}'")

            elif action_type == "suggest_content":
                try:
                    suggestions = await generate_content_suggestions(name)
                    if suggestions:
                        await update.message.reply_text(f"📱 *Content Ideas*\n\n{suggestions}",
                                                         parse_mode="Markdown")
                except Exception as e:
                    logger.warning(f"Content suggestions failed: {e}")

            elif action_type == "bulk_stock":
                items = act.get("items", [])
                checked_by = act.get("checked_by", name)
                stock_date = act.get("date", None)  # dd/mm/yy format from AI
                # Add checked_by to each item for the bulk method
                for entry in items:
                    entry["checked_by"] = f"Count by {checked_by}"
                store.update_stock_bulk(items, stock_date)
                count = len([e for e in items if e.get("item")])
                date_label = stock_date if stock_date else "today"
                if count:
                    feedback.append(f"📦 Updated {count} stock items ({date_label}) to Google Sheet!")

                # Check for low stock against SOP minimums
                item_names = [e.get("item", "") for e in items if e.get("item")]
                low_items = store.check_low_stock(item_names)
                if low_items:
                    alert_lines = ["⚠️ LOW STOCK ALERT:"]
                    for li in low_items:
                        unit = f" {li['unit']}" if li.get('unit') else ""
                        alert_lines.append(f"  • {li['item']}: {li['qty']} (min: {li['min']}{unit})")
                    alert_lines.append("\nPlease restock these items!")
                    await update.message.reply_text("\n".join(alert_lines))
                    await _auto_add_low_to_shopping(low_items, store, update.get_bot(), update.effective_chat.id)

                # Follow up: list items not mentioned in this stock count
                try:
                    all_stock = store.get_stock()
                    updated_norms = set()
                    for si in items:
                        updated_norms.add(normalize_item_name(si.get("item", "")))

                    missing = []
                    for item_name in all_stock:
                        if normalize_item_name(item_name) not in updated_norms:
                            # Skip one-off items
                            if not store.is_known_oneoff(item_name):
                                missing.append(item_name)

                    if missing:
                        missing_list = "\n".join(f"  • {m}" for m in sorted(missing)[:30])
                        await update.effective_message.reply_text(
                            f"📋 *Items not counted yet:*\n\n{missing_list}\n\n"
                            f"_Send their counts when ready, or say 'skip' to keep current values._",
                            parse_mode="Markdown",
                        )
                except Exception as e:
                    logger.error(f"Stock follow-up error: {e}")

            elif action_type == "correct_stock":
                item = act.get("item", "")
                new_qty = act.get("qty", 0)
                note = act.get("note", "")
                if item:
                    try:
                        new_qty = int(new_qty)
                    except (ValueError, TypeError):
                        new_qty = 0
                    store.correct_stock_entry(item, new_qty, f"{name}: {note}" if note else name)
                    feedback.append(f"✏️ Corrected: {item} → {new_qty}")

            elif action_type == "undo_receipt":
                supplier = act.get("supplier", "")
                expense_date = act.get("date", "")
                undo_items = act.get("items", [])
                if supplier:
                    # Reverse stock additions
                    for ui in undo_items:
                        item_name = ui.get("name", "")
                        qty = ui.get("qty", 0)
                        if item_name:
                            try:
                                qty = int(qty)
                            except (ValueError, TypeError):
                                qty = 0
                            # Subtract the qty that was added
                            stock_current = store.data.get("stock_current", {})
                            current_val = stock_current.get(item_name, 0)
                            try:
                                current_val = int(current_val)
                            except (ValueError, TypeError):
                                current_val = 0
                            new_val = max(0, current_val - qty)
                            store.correct_stock_entry(item_name, new_val, f"Undo receipt ({name})")
                    # Delete expense rows
                    if expense_date:
                        try:
                            from google_integration import delete_expense_rows
                            deleted = delete_expense_rows(supplier, expense_date)
                            feedback.append(f"↩️ Undone: {supplier} receipt ({deleted} expense rows removed)")
                        except ImportError:
                            feedback.append(f"↩️ Stock reversed for {supplier} (no Sheets integration)")
                        except Exception as e:
                            feedback.append(f"↩️ Stock reversed for {supplier} (expense cleanup error: {e})")
                    else:
                        feedback.append(f"↩️ Stock reversed for {supplier}")

            elif action_type == "checklist_done":
                checklist = act.get("checklist", "").lower()
                items = act.get("items", ["all"])
                if checklist in ("opening", "6pm", "closing"):
                    result = store.mark_checklist_done(checklist, items, name)
                    done = result["completed_count"]
                    total = result["total"]
                    if done == total:
                        feedback.append(f"✅ {checklist.capitalize()} checklist: ALL DONE! ({done}/{total})")
                    else:
                        feedback.append(f"✅ {checklist.capitalize()} checklist: {done}/{total} done")

            elif action_type == "monthly_summary":
                month = act.get("month", now_sg().date().strftime("%Y-%m"))
                try:
                    from google_integration import generate_monthly_summary
                    summary_data = generate_monthly_summary(month)
                    if summary_data:
                        revenue = summary_data.get("total_revenue", 0)
                        expenses = summary_data.get("total_expenses", 0)
                        profit = summary_data.get("gross_profit", 0)
                        margin = summary_data.get("margin", 0)
                        days = summary_data.get("sales_days", 0)
                        bills = summary_data.get("total_bills", 0)
                        pax = summary_data.get("total_pax", 0)
                        avg_daily = summary_data.get("avg_daily_sales", 0)
                        avg_bill = summary_data.get("avg_bill", 0)
                        top_pay = summary_data.get("top_payment", "N/A")
                        top_sup = summary_data.get("top_supplier", "N/A")

                        pay_lines = ""
                        for method, amount in summary_data.get("payment_totals", {}).items():
                            if amount > 0:
                                pay_lines += f"\n  • {method}: RM{amount:.2f}"

                        profit_emoji = "📈" if profit >= 0 else "📉"

                        summary_msg = (
                            f"📊 *Monthly Summary — {month}*\n\n"
                            f"💰 Revenue: RM{revenue:.2f}\n"
                            f"💸 Expenses: RM{expenses:.2f}\n"
                            f"{profit_emoji} *Profit: RM{profit:.2f}* ({margin:.1f}%)\n\n"
                            f"📅 Trading days: {days}\n"
                            f"🧾 Total bills: {bills}\n"
                            f"👥 Total pax: {pax}\n"
                            f"📊 Avg daily sales: RM{avg_daily:.2f}\n"
                            f"🧾 Avg bill: RM{avg_bill:.2f}\n\n"
                            f"💳 *Payment Methods:*{pay_lines}\n\n"
                            f"🏪 Top supplier: {top_sup}\n"
                            f"💳 Top payment: {top_pay}\n\n"
                            f"_Written to Monthly Summary sheet._"
                        )
                        await update.message.reply_text(summary_msg, parse_mode="Markdown")
                    else:
                        feedback.append(f"📊 No data found for {month}")
                except ImportError:
                    feedback.append("⚠️ Google integration not configured")
                except Exception as e:
                    logger.error(f"Monthly summary error: {e}")
                    feedback.append(f"⚠️ Couldn't generate summary: {e}")

            elif action_type == "save_instruction":
                instruction = act.get("instruction", "")
                if instruction:
                    store.add_custom_instruction(instruction, name)
                    feedback.append(f"📝 Noted! I'll remember: {instruction}")

            elif action_type == "learn_alias":
                canonical = act.get("canonical", "")
                alias = act.get("alias", "")
                if canonical and alias:
                    from storage import get_alias_store
                    alias_store = get_alias_store()
                    alias_store.add_alias(canonical, alias)
                    feedback.append(f"🧠 Learned: '{alias}' = '{canonical}'")

            # ── REPORT ACTIONS (natural language → same output as slash commands) ──

            elif action_type == "show_today":
                now = now_sg()
                cleaning = store.get_cleaning_today()
                open_check = store.get_checklist_today("opening")
                close_check = store.get_checklist_today("closing")
                low = store.get_low_stock()
                shopping = store.get_shopping_list()
                events_list = store.get_events(upcoming_only=True)
                upcoming_events = [e for e in events_list if e["date"] == now_sg().date().isoformat()]

                lines = [
                    f"📊 *{config.CAFE_NAME} — Today's Dashboard*",
                    f"📅 {now.strftime('%A, %d %B %Y')}\n",
                    f"🌅 Opening checklist: {'✅ Done' if open_check else '❌ Not done'}",
                    f"🌙 Closing checklist: {'✅ Done' if close_check else '⏳ Pending'}",
                    f"🧹 Cleaning zones done: {len(cleaning)}/{len(config.CLEANING_ZONES)}",
                ]
                if low:
                    lines.append(f"🔴 Low stock items: {len(low)}")
                else:
                    lines.append("📦 Stock: All OK")
                if shopping:
                    lines.append(f"🛒 Shopping list: {len(shopping)} items pending")
                if upcoming_events:
                    lines.append(f"\n📅 *Today's Events:*")
                    for evt in upcoming_events:
                        lines.append(f"  • {evt['title']}: {evt['details']}")
                day = today_day()
                shifts = store.get_shifts(day)
                if shifts:
                    lines.append(f"\n👥 *Today's Shifts:*")
                    for staff, times in shifts.items():
                        lines.append(f"  • {staff}: {times['start']} – {times['end']}")
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

            elif action_type == "show_expenses":
                month = act.get("month")
                try:
                    from google_integration import get_expenses_detail
                    expenses = get_expenses_detail(month)
                    if not expenses:
                        await update.message.reply_text(
                            "💸 No expenses recorded this month.\nSend a receipt photo to start tracking!"
                        )
                    else:
                        month_str = month or now_sg().date().strftime("%Y-%m")
                        total = 0
                        by_supplier = {}
                        for t in expenses:
                            try:
                                amount = float(t.get("Total (RM)", 0) or 0)
                            except (ValueError, TypeError):
                                amount = 0
                            total += amount
                            supplier = t.get("Supplier", "Unknown")
                            by_supplier[supplier] = by_supplier.get(supplier, 0) + amount

                        rpt = [f"💸 *Expenses — {month_str}* ({len(expenses)} entries)\n"]
                        rpt.append("*By Supplier:*")
                        for supplier, amt in sorted(by_supplier.items(), key=lambda x: -x[1]):
                            pct = (amt / total * 100) if total > 0 else 0
                            rpt.append(f"  {supplier}: RM{amt:.2f} ({pct:.0f}%)")
                        rpt.append(f"\n💰 *Total Expenses: RM{total:.2f}*")
                        await update.message.reply_text("\n".join(rpt), parse_mode="Markdown")
                except ImportError:
                    feedback.append("⚠️ Google Sheets not configured.")

            elif action_type == "show_whopaid":
                month = act.get("month")
                try:
                    from google_integration import get_repayment_summary
                    summary = get_repayment_summary(month)
                    if not summary or not summary.get("by_person"):
                        await update.message.reply_text(
                            "💳 No expense records yet this month.\nSend receipt photos to start tracking who paid!"
                        )
                    else:
                        month_str = summary["month"]
                        total = summary["total_spent"]
                        people = summary["by_person"]
                        rpt = [f"💳 *Expenses — {month_str}*\n"]
                        rpt.append(f"Total expenses: RM{total:.2f}\n")
                        rpt.append("*Paid by:*")
                        for person, info in sorted(people.items(), key=lambda x: -x[1]["paid"]):
                            rpt.append(f"  {person}: RM{info['paid']:.2f}")
                        rpt.append(f"  *Total: RM{total:.2f}*")
                        await update.message.reply_text("\n".join(rpt), parse_mode="Markdown")
                except ImportError:
                    feedback.append("⚠️ Google Sheets not configured.")

            elif action_type == "show_sales":
                month = act.get("month")
                try:
                    from google_integration import get_daily_sales_for_month
                    sales = get_daily_sales_for_month(month)
                    if not sales:
                        await update.message.reply_text(
                            "💰 No daily sales recorded this month.\nSend a daily POS closeout photo to start tracking!"
                        )
                    else:
                        month_str = month or now_sg().date().strftime("%Y-%m")
                        total_revenue = 0
                        total_bills = 0
                        best_day = {"date": "", "amount": 0}
                        for ds in sales:
                            try:
                                day_total = float(ds.get("Total Sales (RM)", 0) or 0)
                                bills = int(ds.get("Bills", 0) or 0)
                            except (ValueError, TypeError):
                                day_total, bills = 0, 0
                            total_revenue += day_total
                            total_bills += bills
                            if day_total > best_day["amount"]:
                                best_day = {"date": ds.get("Date", "?"), "amount": day_total}

                        avg_daily = total_revenue / len(sales) if sales else 0
                        rpt = [f"💰 *Sales — {month_str}* ({len(sales)} days)\n"]
                        for ds in sales[-10:]:
                            day_total = float(ds.get("Total Sales (RM)", 0) or 0)
                            bills = ds.get("Bills", "?")
                            pax = ds.get("Pax", "?")
                            rpt.append(f"  {ds.get('Date', '?')}: RM{day_total:.2f} ({bills} bills, {pax} pax)")
                        rpt.append(f"\n📊 Total: RM{total_revenue:.2f}")
                        rpt.append(f"📊 Avg daily: RM{avg_daily:.2f}")
                        rpt.append(f"🏆 Best day: {best_day['date']} (RM{best_day['amount']:.2f})")
                        await update.message.reply_text("\n".join(rpt), parse_mode="Markdown")
                except ImportError:
                    feedback.append("⚠️ Google Sheets not configured.")

            elif action_type == "show_pnl":
                month = act.get("month")
                await update.message.reply_text("📊 Generating P&L report... This may take a moment.")
                try:
                    from pnl_generator import generate_pnl_xlsx
                    month_str = month or now_sg().date().strftime("%Y-%m")
                    output_path = f"/tmp/SUDU_PNL_{month_str}.xlsx"
                    result = generate_pnl_xlsx(month=month, output_path=output_path)
                    if result:
                        with open(result, "rb") as f:
                            await update.message.reply_document(
                                document=f,
                                filename=f"SUDU_PNL_{month_str}.xlsx",
                                caption=(
                                    f"📊 P&L Report — {month_str}\n\n"
                                    "✅ Auto-filled: Sales, Purchases, Discounts\n"
                                    "🟡 Yellow cells: Manual entry needed"
                                ),
                            )
                    else:
                        feedback.append("❌ Couldn't generate P&L — check Google Sheets setup.")
                except ImportError:
                    feedback.append("⚠️ P&L generator not available. Make sure openpyxl is installed.")
                except Exception as e:
                    logger.error(f"P&L report error: {e}")
                    feedback.append(f"❌ Error generating report: {e}")

            elif action_type == "show_stock":
                stock = store.get_all_stock()
                if not stock:
                    await update.message.reply_text("📦 No stock items tracked yet.")
                else:
                    rpt = ["📦 *Stock Levels*\n"]
                    for item, info in sorted(stock.items()):
                        qty = info.get("qty", "?")
                        emoji = "🔴" if str(qty).upper() in ("LOW", "OUT") else "✅"
                        rpt.append(f"  {emoji} {item}: {qty}")
                    await update.message.reply_text("\n".join(rpt), parse_mode="Markdown")

            elif action_type == "show_lowstock":
                low = store.get_low_stock()
                if not low:
                    await update.message.reply_text("✅ All stock levels are OK!")
                else:
                    rpt = ["⚠️ *Low Stock Items*\n"]
                    for li in low:
                        unit = f" {li['unit']}" if li.get('unit') else ""
                        rpt.append(f"  🔴 {li['item']}: {li['qty']} (min: {li['min']}{unit})")
                    rpt.append("\nPlease restock these items!")
                    await update.message.reply_text("\n".join(rpt), parse_mode="Markdown")

            elif action_type == "show_shopping":
                shopping = store.get_shopping_list()
                if not shopping:
                    await update.message.reply_text("🛒 Shopping list is empty! All stocked up.")
                else:
                    rpt = ["🛒 *Shopping List*\n"]
                    for i, si in enumerate(shopping, 1):
                        emoji = "🔴" if si.get("urgency") == "urgent" else "⬜"
                        rpt.append(f"  {emoji} {si['item']} (added by {si.get('added_by', '?')})")
                    await update.message.reply_text("\n".join(rpt), parse_mode="Markdown")

            elif action_type == "show_cleaning":
                cleaning = store.get_cleaning_today()
                zones = config.CLEANING_ZONES
                rpt = [f"🧹 *Cleaning Status* ({len(cleaning)}/{len(zones)} done)\n"]
                for zone in zones:
                    if zone in cleaning:
                        rpt.append(f"  ✅ {zone} — {cleaning[zone]}")
                    else:
                        rpt.append(f"  ❌ {zone}")
                await update.message.reply_text("\n".join(rpt), parse_mode="Markdown")

            elif action_type == "show_shifts":
                day = today_day()
                shifts = store.get_shifts(day)
                if not shifts:
                    await update.message.reply_text("👥 No shifts scheduled for today.")
                else:
                    rpt = ["👥 *Today's Shifts*\n"]
                    for staff, times in shifts.items():
                        rpt.append(f"  • {staff}: {times['start']} – {times['end']}")
                    await update.message.reply_text("\n".join(rpt), parse_mode="Markdown")

            elif action_type == "show_week":
                events_list = store.get_events(upcoming_only=True)
                week_events = [
                    e for e in events_list
                    if e["date"] <= (now_sg().date() + timedelta(days=7)).isoformat()
                ]
                if not week_events:
                    await update.message.reply_text("📅 Nothing special planned this week.")
                else:
                    rpt = ["📅 *This Week*\n"]
                    for evt in week_events:
                        rpt.append(f"  • {evt['date']}: {evt['title']} — {evt['details']}")
                    await update.message.reply_text("\n".join(rpt), parse_mode="Markdown")

            elif action_type == "show_tasks":
                pending = store.get_action_items("pending")
                if not pending:
                    await update.message.reply_text("✅ No pending tasks! Everything's settled.")
                else:
                    rpt = [f"📋 *Pending Tasks* ({len(pending)})\n"]
                    for i, task in enumerate(pending[:15], 1):
                        rpt.append(f"  {i}. {task.get('task', '?')} (→ {task.get('assigned_to', '?')})")
                    await update.message.reply_text("\n".join(rpt), parse_mode="Markdown")

            elif action_type == "show_staff":
                staff_list = store.get_staff()
                if not staff_list:
                    await update.message.reply_text("👥 No staff registered yet.")
                else:
                    rpt = ["👥 *Team*\n"]
                    for s in staff_list:
                        rpt.append(f"  • {s.get('name', '?')}")
                    await update.message.reply_text("\n".join(rpt), parse_mode="Markdown")

            elif action_type == "stock_count":
                item = act.get("item", "")
                count = act.get("count", "")
                if item and count:
                    # Validate against expected levels
                    warning = validate_stock_count(item, count)
                    store.update_stock(item, count, f"Count by {name}")
                    feedback.append(f"📦 Counted: {item} = {count}")
                    if warning:
                        feedback.append(warning)

            elif action_type == "read_tab":
                tab = act.get("tab", "")
                if tab:
                    data = store.read_tab(tab)
                    if data and data.get("rows"):
                        header_line = " | ".join(data["headers"][:8])
                        lines = [f"📋 {tab} ({data['total_rows']} rows):"]
                        lines.append(header_line)
                        for row in data["rows"][:20]:
                            lines.append(" | ".join(str(row.get(h, ""))[:30] for h in data["headers"][:8]))
                        feedback.append("\n".join(lines))
                    else:
                        feedback.append(f"❌ Could not read tab '{tab}'")

            elif action_type == "append_row":
                tab = act.get("tab", "")
                row_data = act.get("data", {})
                if tab and row_data:
                    result = store.append_to_tab(tab, row_data)
                    feedback.append(f"✅ Added row to {tab}" if result else f"❌ Failed to add to {tab}")

        except Exception as e:
            logger.error(f"Action execution error ({action_type}): {e}")

    return feedback


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle all natural language messages — AI-driven, fully conversational."""
    if not update.message or not update.message.text:
        return

    # Group restriction — ignore DMs and unknown groups
    if await _group_gate(update):
        return

    text = update.message.text.strip()
    name = user_name(update)
    chat_id = _get_chat_id(update)

    # Skip commands
    if text.startswith("/"):
        return

    # Store in memory — ALWAYS, even if bot won't reply
    remember(name, text, "text", chat_id=chat_id)

    # ─── Check for receipt confirmation / amendment ──
    # Catches: (a) reply to ANY bot message in the receipt conversation,
    #          OR (b) tagged bot while receipt is pending
    _pending_rcpt_id = ctx.chat_data.get("pending_receipt_msg_id")
    _rcpt_msg_ids = set(ctx.chat_data.get("pending_receipt_msg_ids", []))
    if _pending_rcpt_id:
        _rcpt_msg_ids.add(_pending_rcpt_id)
    _reply_target = (
        update.message.reply_to_message.message_id
        if update.message.reply_to_message else None
    )
    _is_reply_to_rcpt = (
        _reply_target is not None
        and bool(_rcpt_msg_ids)
        and _reply_target in _rcpt_msg_ids
    )
    _is_tagged_with_pending = (
        _pending_rcpt_id
        and _bot_is_tagged(update, ctx)
        and ctx.chat_data.get("pending_receipt")
    )
    if _is_reply_to_rcpt or _is_tagged_with_pending:
        pending = ctx.chat_data.get("pending_receipt")
        if not pending:
            await update.message.reply_text("⚠️ No pending receipt to process.")
            return

        # Strip bot tag so AI only sees the user's intent
        rcpt_text = text
        if ctx.bot.username:
            rcpt_text = rcpt_text.replace(f"@{ctx.bot.username}", "").strip()

        # Build a short summary for the AI to understand context
        rd = pending["data"]
        receipt_summary = (
            f"Supplier: {rd.get('supplier', '?')}, "
            f"Total: RM{float(rd.get('total') or 0):.2f}, "
            f"Paid by: {rd.get('paid_by', '?')}, "
            f"Date: {rd.get('date', '?')}, "
            f"Items: {', '.join(i.get('name', '?') for i in rd.get('items', []))}"
        )

        # Ask AI what the user means
        ai_result = await classify_receipt_reply(rcpt_text, receipt_summary)
        action = ai_result.get("action", "unclear")

        if action == "confirm":
            await _confirm_receipt(pending, name, update, ctx)
            return

        elif action == "change":
            changes_dict = ai_result.get("changes", {})
            if changes_dict:
                change_descriptions = []
                for field, value in changes_dict.items():
                    if field in ("paid_by", "supplier", "date", "payment_method",
                                 "total", "subtotal", "tax", "discount",
                                 "category", "notes"):
                        if field == "category":
                            for item in rd.get("items", []):
                                item["category"] = str(value)
                        elif field in ("total", "subtotal", "tax", "discount"):
                            rd[field] = float(value)
                            # Keep total & subtotal in sync
                            if field == "total":
                                rd["subtotal"] = float(value)
                            elif field == "subtotal":
                                rd["total"] = float(value)
                        else:
                            rd[field] = str(value)
                        change_descriptions.append(f"{field.replace('_', ' ').title()} → {value}")

                    # ── Item-level changes (qty, price, name, add, remove) ──
                    elif field == "items" and isinstance(value, list):
                        for item_change in value:
                            idx = item_change.get("index")
                            items = rd.get("items", [])
                            if item_change.get("action") == "add":
                                new_item = {
                                    "name": item_change.get("name", "New item"),
                                    "qty": int(item_change.get("qty", 1)),
                                    "price": float(item_change.get("price", 0)),
                                    "category": item_change.get("category", "ingredients"),
                                }
                                items.append(new_item)
                                change_descriptions.append(f"Added: {new_item['name']}")
                            elif item_change.get("action") == "remove" and idx is not None and 0 <= idx < len(items):
                                removed = items.pop(idx)
                                change_descriptions.append(f"Removed: {removed.get('name', '?')}")
                            elif idx is not None and 0 <= idx < len(items):
                                if "qty" in item_change:
                                    items[idx]["qty"] = int(item_change["qty"])
                                    change_descriptions.append(
                                        f"{items[idx].get('name', '?')} qty → {item_change['qty']}")
                                if "price" in item_change:
                                    items[idx]["price"] = float(item_change["price"])
                                    change_descriptions.append(
                                        f"{items[idx].get('name', '?')} price → RM{float(item_change['price']):.2f}")
                                if "name" in item_change:
                                    old_name = items[idx].get("name", "?")
                                    items[idx]["name"] = str(item_change["name"])
                                    change_descriptions.append(f"{old_name} → {item_change['name']}")
                                if "category" in item_change:
                                    from config import ITEM_CATEGORIES, DEFAULT_CATEGORY
                                    new_cat = str(item_change["category"]).lower().strip()
                                    if new_cat == "useables":
                                        new_cat = "consumables"
                                    if new_cat in ITEM_CATEGORIES:
                                        items[idx]["category"] = new_cat
                                        cat_label = ITEM_CATEGORIES[new_cat]
                                        change_descriptions.append(
                                            f"{items[idx].get('name', '?')} category → {cat_label}")
                        # Recalculate total from items after item-level changes
                        _fix_receipt_total_from_items(rd)

                if change_descriptions:
                    confirm_msg = _build_receipt_confirm_msg(rd, pending["user"])
                    sent_msg = await update.message.reply_text(
                        f"✅ Updated: {', '.join(change_descriptions)}\n\n{confirm_msg}",
                        reply_markup=_receipt_confirm_buttons(),
                        parse_mode="Markdown",
                    )
                    ctx.chat_data["pending_receipt_msg_id"] = sent_msg.message_id
                    _track_receipt_msg(ctx, sent_msg.message_id)
                    return

        # action == "unclear" or no valid changes
        help_msg = await update.message.reply_text(
            "🤔 I didn't catch that. You can:\n"
            "• Tell me what to change (e.g. 'paid by Eric', 'total 15.90')\n"
            "• Say 'ok' or 'confirm' to save\n"
            "• Tap the buttons below the receipt",
        )
        _track_receipt_msg(ctx, help_msg.message_id)
        return

    # ─── Check for sales confirmation / amendment (AI-powered) ──
    _pending_sales_id = ctx.chat_data.get("pending_sales_msg_id")
    _is_reply_to_sales = (
        update.message.reply_to_message
        and _pending_sales_id
        and update.message.reply_to_message.message_id == _pending_sales_id
    )
    _is_tagged_with_pending_sales = (
        _pending_sales_id
        and _bot_is_tagged(update, ctx)
        and ctx.chat_data.get("pending_sales")
    )
    if _is_reply_to_sales or _is_tagged_with_pending_sales:
        pending = ctx.chat_data.get("pending_sales")
        if not pending:
            await update.message.reply_text("⚠️ No pending sales report to process.")
            return

        # Strip bot tag
        sales_text = text
        if ctx.bot.username:
            sales_text = sales_text.replace(f"@{ctx.bot.username}", "").strip()

        sd = pending["data"]
        sales_summary = (
            f"Date: {sd.get('date', '?')}, "
            f"Total Sales: RM{float(sd.get('total_sales') or 0):.2f}, "
            f"Bills: {sd.get('bill_count', '?')}, Pax: {sd.get('total_pax', '?')}"
        )
        ai_result = await classify_receipt_reply(sales_text, sales_summary)
        action = ai_result.get("action", "unclear")

        if action == "confirm":
            await _confirm_sales(pending, name, update, ctx)
            return
        elif action == "change":
            changes_dict = ai_result.get("changes", {})
            if changes_dict:
                change_descriptions = []
                for field, value in changes_dict.items():
                    if field in ("total_sales", "bill_count", "total_pax",
                                 "total_discount", "date"):
                        if field in ("total_sales", "total_discount"):
                            sd[field] = float(value)
                        elif field in ("bill_count", "total_pax"):
                            sd[field] = int(value)
                        else:
                            sd[field] = str(value)
                        change_descriptions.append(f"{field.replace('_', ' ').title()} → {value}")
                if change_descriptions:
                    _s_total = float(sd.get('total_sales') or 0)
                    await update.message.reply_text(
                        f"✅ Updated: {', '.join(change_descriptions)}\n\n"
                        f"📊 Sales report: RM{_s_total:.2f} on {sd.get('date', '?')}\n"
                        f"Reply to confirm or tell me what else to change.",
                    )
                    return
        # unclear
        await update.message.reply_text(
            "🤔 I didn't catch that. You can:\n"
            "• Say 'ok' or 'confirm' to save\n"
            "• Tell me what to change (e.g. 'total should be 500')\n"
            "• Tap the buttons below the report",
        )
        return

    # ─── Reply to a photo + tag bot = process that photo ────
    if update.message.reply_to_message and _bot_is_tagged(update, ctx):
        replied = update.message.reply_to_message
        replied_msg_id = str(replied.message_id)

        # Try to get photo file_id: from reply_to_message first, then stored cache
        photo_file_id = None
        if replied.photo:
            photo_file_id = replied.photo[-1].file_id
        else:
            stored = ctx.chat_data.get("photo_file_ids", {})
            photo_file_id = stored.get(replied_msg_id)
            if photo_file_id:
                logger.info(f"Found stored photo file_id for msg {replied_msg_id}")

        if photo_file_id:
            try:
                photo_file = await ctx.bot.get_file(photo_file_id)
                image_data = await photo_file.download_as_bytearray()
                image_bytes = bytes(image_data)
                reply_caption = replied.caption or ""

                # Strip bot tag from the reply text to get extra info (e.g. "by eric")
                extra_info = text
                if ctx.bot.username:
                    extra_info = extra_info.replace(f"@{ctx.bot.username}", "").strip()

                # Combine original caption with the reply text
                combined_caption = f"{reply_caption} {extra_info}".strip() if extra_info else reply_caption

                # Classify the photo (AI-powered)
                classification = "photo"
                try:
                    classification = await classify_photo(image_bytes, "image/jpeg")
                except Exception as e:
                    logger.warning(f"Photo classification failed: {e}")

                if classification == "receipt":
                    processing_msg = await update.message.reply_text("🧾 Processing receipt/invoice...")
                    ctx.chat_data["pending_receipt_msg_ids"] = []
                    _track_receipt_msg(ctx, processing_msg.message_id)
                    receipt_data = await process_receipt(image_bytes, name, combined_caption)

                    if receipt_data:
                        _fix_receipt_total(receipt_data)
                        _fix_receipt_paid_by(receipt_data, name)

                        # If user tagged with extra info like "by Eric", set paid_by
                        if extra_info and not receipt_data.get("paid_by"):
                            receipt_data["paid_by"] = extra_info

                        confirm_msg = _build_receipt_confirm_msg(receipt_data, name)

                        ctx.chat_data["pending_receipt"] = {
                            "data": receipt_data,
                            "image_bytes": image_bytes,
                            "user": name,
                            "caption": combined_caption,
                        }

                        sent_msg = await update.message.reply_text(
                            confirm_msg,
                            reply_markup=_receipt_confirm_buttons(),
                            parse_mode="Markdown",
                        )
                        ctx.chat_data["pending_receipt_msg_id"] = sent_msg.message_id
                        _track_receipt_msg(ctx, sent_msg.message_id)
                    else:
                        await update.message.reply_text(
                            f"🧾 Couldn't read that receipt, {name}. Try a clearer photo."
                        )
                    return
                elif classification == "sales_report":
                    await update.message.reply_text("📊 Processing daily sales report...")
                    sales_data = await process_sales_report(image_bytes, name, combined_caption)
                    if sales_data:
                        _s_total = float(sales_data.get('total_sales') or 0)
                        ctx.chat_data["pending_sales"] = {"data": sales_data, "user": name}
                        await update.message.reply_text(
                            f"📊 Sales report detected: RM{_s_total:.2f}. Reply 'yes' to confirm."
                        )
                    return
                else:
                    # It's a regular photo — let AI handle the text with photo context
                    logger.info(f"Reply-to-photo classified as '{classification}', passing to AI")
            except Exception as e:
                logger.error(f"Reply-to-photo processing error: {e}")
                await update.message.reply_text(f"⚠️ Couldn't process that photo: {e}")
                return

    # ─── Group chat: only respond if tagged or replied-to ──
    in_group = _is_group_chat(update)
    tagged = _bot_is_tagged(update, ctx)

    if in_group and not tagged:
        # Silently read & remember, extract action items, but don't reply
        try:
            action_items = await extract_action_items_ai(text, name)
            for item in action_items:
                store.add_action_item(
                    task=item.get("task", text[:200]),
                    assigned_to=item.get("assigned_to", name),
                    mentioned_by=name,
                    source_msg=text,
                    urgency=item.get("urgency", "normal"),
                )
        except Exception as e:
            logger.debug(f"Action item extraction skipped: {e}")
        return  # Stay quiet — not tagged

    # ─── Extract action items for chase-up system ──────────
    try:
        action_items = await extract_action_items_ai(text, name)
        for item in action_items:
            store.add_action_item(
                task=item.get("task", text[:200]),
                assigned_to=item.get("assigned_to", name),
                mentioned_by=name,
                source_msg=text,
                urgency=item.get("urgency", "normal"),
            )
    except Exception as e:
        logger.debug(f"Action item extraction skipped: {e}")

    # Strip the @tag from the message before sending to AI
    if tagged and ctx.bot.username:
        text = text.replace(f"@{ctx.bot.username}", "").strip()

    # ─── Extract reply context if replying to a message ────
    reply_context = None
    if update.message.reply_to_message:
        replied = update.message.reply_to_message
        replied_name = ""
        if replied.from_user:
            replied_name = replied.from_user.full_name or replied.from_user.username or str(replied.from_user.id)
        replied_text = replied.text or replied.caption or ""

        # If replying to a photo/document with no text, describe what it was
        if not replied_text:
            if replied.photo:
                replied_text = "[a photo was sent]"
            elif replied.document:
                doc_name = replied.document.file_name or "a file"
                replied_text = f"[sent a document: {doc_name}]"
            elif replied.voice:
                replied_text = "[sent a voice note]"
            elif replied.video:
                replied_text = "[sent a video]"

        # Add any pending receipt/sales context if replying to bot's confirmation
        if replied.from_user and replied.from_user.id == ctx.bot.id:
            pending_receipt = ctx.chat_data.get("pending_receipt")
            if pending_receipt and pending_receipt.get("data"):
                rd = pending_receipt["data"]
                items_desc = ", ".join(
                    f"{i.get('name', '?')} x{i.get('qty', '?')}"
                    for i in rd.get("items", [])
                )
                replied_text += (
                    f" [Receipt from {rd.get('supplier', '?')}: "
                    f"{items_desc}, total RM{float(rd.get('total') or 0):.2f}]"
                )

        if replied_text:
            reply_context = f"{replied_name}: {replied_text}"

    # ─── Send to AI — get reply + actions ──────────────────
    is_staff = _is_staff_group(update)
    chat_reply, actions = await process_message(
        text, name, reply_context, chat_id=chat_id, is_staff_group=is_staff,
    )

    if chat_reply:
        await update.message.reply_text(chat_reply)

        # Execute any actions the AI triggered
        if actions:
            # Filter out blocked actions in staff group
            if is_staff:
                actions = [
                    a for a in actions
                    if a.get("action", "") not in config.STAFF_BLOCKED_ACTIONS
                ]
            feedback = await _execute_actions(actions, name, update)
            # Auto-clear pending tasks that match executed actions
            try:
                _auto_clear_matching_tasks(actions)
            except Exception as e:
                logger.error(f"Auto-clear tasks error: {e}")
            if feedback:
                await update.message.reply_text(
                    "\n".join(feedback),
                )
    else:
        # AI unavailable — simple fallback
        await update.message.reply_text(
            f"Sorry {name}, I'm having trouble processing that. "
            f"Try again in a moment or use /help to see what I can do."
        )


# ═══════════════════════════════════════════════════════════
#  📋 ACTION ITEMS / CHASE-UP COMMANDS
# ═══════════════════════════════════════════════════════════

async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """View pending action items."""
    pending = store.get_action_items("pending")
    if not pending:
        await update.message.reply_text("✅ No pending tasks! Everything's settled.")
        return

    lines = ["📋 *Pending Tasks*\n"]
    for i, item in enumerate(pending):
        urgency = "🔴" if item.get("urgency") == "urgent" else "⚪"
        who = item.get("assigned_to", "?")
        task = item.get("task", "?")[:80]
        created = item.get("created_at", "")[:10]
        chased = item.get("chase_count", 0)
        lines.append(f"  {i+1}. {urgency} {task}")
        lines.append(f"     → {who} (since {created}, chased {chased}x)")

    lines.append("\n/taskdone <number> — mark as done")
    lines.append("/taskdismiss <number> — dismiss/cancel")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_taskdone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Mark action item as done: /taskdone <number>"""
    if not ctx.args:
        await update.message.reply_text("Usage: /taskdone <number>\nSee /tasks for the list.")
        return
    try:
        idx = int(ctx.args[0]) - 1
        pending = store.get_action_items("pending")
        if 0 <= idx < len(pending):
            task_name = pending[idx].get("task", "?")[:60]
            store.complete_action_item(idx, user_name(update))
            await update.message.reply_text(f"✅ Done: {task_name}")
        else:
            await update.message.reply_text("❌ Invalid number. Check /tasks for the list.")
    except ValueError:
        await update.message.reply_text("❌ Please enter a number.")


async def cmd_taskdismiss(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Dismiss an action item: /taskdismiss <number>"""
    if not ctx.args:
        await update.message.reply_text("Usage: /taskdismiss <number>\nSee /tasks for the list.")
        return
    try:
        idx = int(ctx.args[0]) - 1
        pending = store.get_action_items("pending")
        if 0 <= idx < len(pending):
            task_name = pending[idx].get("task", "?")[:60]
            store.dismiss_action_item(idx)
            await update.message.reply_text(f"🗑️ Dismissed: {task_name}")
        else:
            await update.message.reply_text("❌ Invalid number. Check /tasks for the list.")
    except ValueError:
        await update.message.reply_text("❌ Please enter a number.")


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Search chat history: /search <query>"""
    query = " ".join(ctx.args) if ctx.args else ""
    if not query:
        await update.message.reply_text("Usage: /search <keyword>\nExample: /search milk delivery")
        return

    results = search_memory(query, max_results=10)
    if not results:
        await update.message.reply_text(f"🔍 No messages found for '{query}'.")
        return

    lines = [f"🔍 *Search: '{query}'* ({len(results)} results)\n"]
    for msg in results:
        time_str = msg.get("time", "?")
        who = msg.get("who", "?")
        text = msg.get("text", "")[:100]
        lines.append(f"  [{time_str}] {who}: {text}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════
#  📄 DOCUMENT / FILE HANDLER (POS Reports)
# ═══════════════════════════════════════════════════════════

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle document uploads — detect POS reports, invoices, etc."""
    if not update.message or not update.message.document:
        return

    doc = update.message.document
    name = user_name(update)
    caption = update.message.caption or ""
    filename = doc.file_name or "unknown"
    chat_id = update.effective_chat.id
    msg_id = update.message.message_id

    # Check file extension
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    supported = {"csv", "xlsx", "xls", "pdf", "txt"}

    if ext not in supported:
        # Store reference but don't process
        remember(name, f"[Document: {filename}]", "document", chat_id, msg_id)
        return

    # Check size (max 10MB for Gemini)
    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await update.message.reply_text(
            f"📄 {filename} is too large ({doc.file_size // (1024*1024)}MB). "
            f"Max 10MB for analysis."
        )
        return

    try:
        # Download file
        file_obj = await ctx.bot.get_file(doc.file_id)
        file_data = await file_obj.download_as_bytearray()

        remember(name, f"[Document: {filename} — POS/data file for analysis]",
                 "document", chat_id, msg_id)

        await update.message.reply_text(f"📄 Analyzing {filename}...")

        # Send to Gemini for analysis
        analysis = await analyze_pos_file(bytes(file_data), filename, name)

        if analysis:
            # Try to extract summary JSON and log to Sheets
            try:
                import re
                from google_integration import log_pos_report, upload_file_to_drive

                # Upload file to Drive
                mime_map = {
                    "csv": "text/csv",
                    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "xls": "application/vnd.ms-excel",
                    "pdf": "application/pdf",
                    "txt": "text/plain",
                }
                file_link = upload_file_to_drive(
                    bytes(file_data), filename,
                    mime_type=mime_map.get(ext, "application/octet-stream"),
                )

                # Extract SUMMARY_JSON if present
                json_match = re.search(r'SUMMARY_JSON:\s*(\{.*\})', analysis)
                if json_match:
                    try:
                        summary = json.loads(json_match.group(1))
                        current_month = now_sg().date().strftime("%Y-%m")
                        log_pos_report(
                            month=summary.get("month", current_month),
                            total_sales=float(summary.get("total_sales", 0)),
                            transaction_count=int(summary.get("transaction_count", 0)),
                            avg_transaction=float(summary.get("avg_transaction", 0)),
                            top_items=summary.get("top_items", ""),
                            peak_hours=summary.get("peak_hours", ""),
                            notes=f"Uploaded by {name}",
                            analysis=analysis[:500],
                            file_link=file_link or "",
                        )
                    except (json.JSONDecodeError, ValueError):
                        pass

            except ImportError:
                pass

            # Send analysis (may be long, split if needed)
            if len(analysis) > 4000:
                # Split into chunks
                for i in range(0, len(analysis), 4000):
                    chunk = analysis[i:i+4000]
                    await update.message.reply_text(chunk)
            else:
                await update.message.reply_text(f"📊 *POS Analysis*\n\n{analysis}",
                                                 parse_mode="Markdown")
        else:
            await update.message.reply_text(
                f"📄 Couldn't analyze {filename}. "
                f"Check that GEMINI_API_KEY is set."
            )

    except Exception as e:
        logger.error(f"Document handler error: {e}")
        await update.message.reply_text(
            f"📄 Error processing {filename}: {str(e)[:100]}"
        )


# ═══════════════════════════════════════════════════════════
#  📊 P&L, RECEIPTS, DATA COMMANDS
# ═══════════════════════════════════════════════════════════

async def cmd_pl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show P&L summary: /pl [YYYY-MM]"""
    month = ctx.args[0] if ctx.args else None

    try:
        from google_integration import get_pl_summary
        pl = get_pl_summary(month)
        if not pl:
            await update.message.reply_text(
                "📊 No P&L data yet. Send receipts/invoices to start tracking!"
            )
            return

        await update.message.reply_text(
            f"📊 *P&L Summary — {pl['month']}*\n\n"
            f"💸 Expenses: RM{pl['total_expenses']:.2f}\n"
            f"💰 Revenue: RM{pl['total_revenue']:.2f}\n"
            f"📈 Gross Profit: RM{pl['gross_profit']:.2f}\n"
            f"📊 Margin: {pl['margin']:.1f}%\n"
            f"🧾 Transactions: {pl['transaction_count']}\n"
            f"🏪 Top Supplier: {pl['top_supplier']}",
            parse_mode="Markdown",
        )
    except ImportError:
        await update.message.reply_text(
            "⚠️ Google Sheets not configured. Set up credentials.json first."
        )


async def cmd_receipts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """View recent receipts: /receipts [YYYY-MM]"""
    month = ctx.args[0] if ctx.args else None

    try:
        from google_integration import get_expenses_detail
        receipts = get_expenses_detail(month)

        if not receipts:
            await update.message.reply_text(
                "🧾 No receipts this month. Send a photo with caption 'receipt' to log one!"
            )
            return

        month_str = month or now_sg().date().strftime("%Y-%m")
        lines = [f"🧾 *Receipts — {month_str}* ({len(receipts)} total)\n"]
        total = 0
        for t in receipts[-15:]:
            amount = float(t.get("Total (RM)", 0) or 0)
            total += amount
            link = t.get("Receipt Link", "")
            link_text = " 📎" if link else ""
            lines.append(
                f"  {t.get('Date', '?')} | {t.get('Supplier', '?')} | "
                f"RM{amount:.2f}{link_text}"
            )
        lines.append(f"\n💰 Total: RM{total:.2f}")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except ImportError:
        await update.message.reply_text(
            "⚠️ Google Sheets not configured. Set up credentials.json first."
        )


async def cmd_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ask AI about business data: /data <question>"""
    question = " ".join(ctx.args) if ctx.args else ""
    if not question:
        await update.message.reply_text(
            "Usage: /data <question>\n"
            "Examples:\n"
            "  /data how much did we spend on milk this month?\n"
            "  /data which supplier costs the most?\n"
            "  /data compare this month vs last month\n"
            "  /data what's our profit margin?"
        )
        return

    await update.message.reply_text("📊 Pulling data and analyzing...")
    name = user_name(update)
    chat_id = update.effective_chat.id
    response = await ask_about_data(question, name, chat_id)

    if response:
        await update.message.reply_text(f"📊 {response}")
    else:
        await update.message.reply_text(
            "❌ Couldn't get the data. Check Google Sheets setup or try /pl for quick P&L."
        )


async def cmd_stockusage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show stock usage for a month: /stockusage [YYYY-MM]"""
    month = ctx.args[0] if ctx.args else None

    try:
        from google_integration import get_stock_summary
        summary = get_stock_summary(month)

        if not summary or not summary.get("items"):
            await update.message.reply_text(
                "📦 No stock usage data yet. "
                "Send receipts to start tracking what comes in!"
            )
            return

        lines = [f"📦 *Stock Usage — {summary['month']}*\n"]
        total_cost = 0
        for item, data in sorted(summary["items"].items()):
            total_cost += data["cost"]
            lines.append(
                f"  {item}: +{data['qty_in']} in, "
                f"-{data['qty_out']} out, "
                f"RM{data['cost']:.2f}"
            )
        lines.append(f"\n💰 Total stock cost: RM{total_cost:.2f}")
        lines.append(f"📊 Total movements: {summary['total_movements']}")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except ImportError:
        await update.message.reply_text(
            "⚠️ Google Sheets not configured."
        )


async def cmd_pnl_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Generate a P&L XLSX report: /pnl [YYYY-MM]"""
    month = ctx.args[0] if ctx.args else None

    await update.message.reply_text(
        "📊 Generating P&L report... This may take a moment."
    )

    try:
        from pnl_generator import generate_pnl_xlsx
        month_str = month or now_sg().date().strftime("%Y-%m")
        output_path = f"/tmp/SUDU_PNL_{month_str}.xlsx"
        result = generate_pnl_xlsx(month=month, output_path=output_path)

        if result:
            with open(result, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=f"SUDU_PNL_{month_str}.xlsx",
                    caption=(
                        f"📊 P&L Report — {month_str}\n\n"
                        "✅ Auto-filled: Sales, Purchases, Discounts\n"
                        "🟡 Yellow cells: Manual entry needed\n"
                        "(Rent, TNB, Salaries, etc.)"
                    ),
                )
        else:
            await update.message.reply_text(
                "❌ Failed to generate P&L report. "
                "Check Google Sheets setup."
            )
    except ImportError:
        await update.message.reply_text(
            "⚠️ P&L generator not available. "
            "Make sure openpyxl is installed: pip install openpyxl"
        )
    except Exception as e:
        logger.error(f"P&L report error: {e}")
        await update.message.reply_text(f"❌ Error generating report: {e}")


async def cmd_expenses(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show expenses for a month: /expenses [YYYY-MM]"""
    month = ctx.args[0] if ctx.args else None

    try:
        from google_integration import get_expenses_detail
        expenses = get_expenses_detail(month)

        if not expenses:
            await update.message.reply_text(
                "💸 No expenses recorded this month.\n"
                "Send a receipt photo to start tracking!"
            )
            return

        month_str = month or now_sg().date().strftime("%Y-%m")
        total = 0
        by_supplier = {}

        for t in expenses:
            try:
                amount = float(t.get("Total (RM)", 0) or 0)
            except (ValueError, TypeError):
                amount = 0
            total += amount
            supplier = t.get("Supplier", "Unknown")
            by_supplier[supplier] = by_supplier.get(supplier, 0) + amount

        lines = [f"💸 *Expenses — {month_str}* ({len(expenses)} entries)\n"]

        # Top suppliers breakdown
        lines.append("*By Supplier:*")
        for supplier, amt in sorted(by_supplier.items(), key=lambda x: -x[1]):
            pct = (amt / total * 100) if total > 0 else 0
            lines.append(f"  {supplier}: RM{amt:.2f} ({pct:.0f}%)")

        # Recent entries
        lines.append(f"\n*Recent ({min(10, len(expenses))}):*")
        for t in expenses[-10:]:
            amount = float(t.get("Total (RM)", 0) or 0)
            items_str = t.get("Item", "")[:40]
            lines.append(
                f"  {t.get('Date', '?')} | {t.get('Supplier', '?')} | "
                f"RM{amount:.2f}"
            )
            if items_str:
                lines.append(f"    ↳ {items_str}")

        lines.append(f"\n💰 *Total Expenses: RM{total:.2f}*")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except ImportError:
        await update.message.reply_text(
            "⚠️ Google Sheets not configured."
        )


async def cmd_whopaid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show who paid for what: /whopaid [YYYY-MM]"""
    month = ctx.args[0] if ctx.args else None

    try:
        from google_integration import get_repayment_summary
        summary = get_repayment_summary(month)

        if not summary or not summary.get("by_person"):
            await update.message.reply_text(
                "💳 No expense records yet this month.\n"
                "Send receipt photos to start tracking who paid!"
            )
            return

        month_str = summary["month"]
        total = summary["total_spent"]
        people = summary["by_person"]

        lines = [f"💳 *Expenses — {month_str}*\n"]
        lines.append(f"Total expenses: RM{total:.2f}\n")
        lines.append("*Paid by:*")

        for person, info in sorted(people.items(), key=lambda x: -x[1]["paid"]):
            lines.append(f"  {person}: RM{info['paid']:.2f}")

        lines.append(f"  *Total: RM{total:.2f}*")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except ImportError:
        await update.message.reply_text(
            "⚠️ Google Sheets not configured."
        )


async def cmd_sales(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show daily sales for a month: /sales [YYYY-MM]"""
    month = ctx.args[0] if ctx.args else None

    try:
        from google_integration import get_daily_sales_for_month
        sales = get_daily_sales_for_month(month)

        if not sales:
            await update.message.reply_text(
                "💰 No daily sales recorded this month.\n"
                "Send a daily POS closeout photo to start tracking!"
            )
            return

        month_str = month or now_sg().date().strftime("%Y-%m")
        total_revenue = 0
        total_bills = 0
        total_pax = 0
        best_day = {"date": "", "amount": 0}
        worst_day = {"date": "", "amount": float("inf")}

        lines = [f"💰 *Daily Sales — {month_str}* ({len(sales)} days)\n"]

        for ds in sales:
            try:
                day_total = float(ds.get("Total Sales (RM)", 0) or 0)
                bills = int(ds.get("Bills", 0) or 0)
                pax = int(ds.get("Pax", 0) or 0)
            except (ValueError, TypeError):
                day_total, bills, pax = 0, 0, 0

            total_revenue += day_total
            total_bills += bills
            total_pax += pax

            if day_total > best_day["amount"]:
                best_day = {"date": ds.get("Date", "?"), "amount": day_total}
            if day_total < worst_day["amount"]:
                worst_day = {"date": ds.get("Date", "?"), "amount": day_total}

        # Show last 10 days
        lines.append("*Recent days:*")
        for ds in sales[-10:]:
            day_total = float(ds.get("Total Sales (RM)", 0) or 0)
            bills = ds.get("Bills", "?")
            pax = ds.get("Pax", "?")
            lines.append(
                f"  {ds.get('Date', '?')}: RM{day_total:.2f} "
                f"({bills} bills, {pax} pax)"
            )

        avg_daily = total_revenue / len(sales) if sales else 0
        avg_bill = total_revenue / total_bills if total_bills > 0 else 0

        lines.append(f"\n📊 *Summary:*")
        lines.append(f"  Total Revenue: RM{total_revenue:.2f}")
        lines.append(f"  Avg Daily: RM{avg_daily:.2f}")
        lines.append(f"  Avg Bill: RM{avg_bill:.2f}")
        lines.append(f"  Total Bills: {total_bills}")
        lines.append(f"  Total Pax: {total_pax}")
        lines.append(f"  🏆 Best Day: {best_day['date']} (RM{best_day['amount']:.2f})")
        if worst_day["amount"] != float("inf"):
            lines.append(f"  📉 Slowest Day: {worst_day['date']} (RM{worst_day['amount']:.2f})")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except ImportError:
        await update.message.reply_text(
            "⚠️ Google Sheets not configured."
        )


# ═══════════════════════════════════════════════════════════
#  ⏰ SCHEDULED REMINDERS (JobQueue)
# ═══════════════════════════════════════════════════════════

async def scheduled_sop_refresh(context):
    """Re-read SOP data from Google Sheets every few minutes."""
    if not store._sheets:
        return
    try:
        from ai_chat import refresh_sop_prompt
        refresh_sop_prompt(store._sheets)
        logger.debug("SOP prompt refreshed from Google Sheets")
    except Exception as e:
        logger.error(f"Scheduled SOP refresh failed: {e}")


async def scheduled_cleaning_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    """Auto-sent cleaning reminder."""
    if not config.OWNER_GROUP_ID:
        return
    buttons = []
    for zone in config.CLEANING_ZONES:
        buttons.append([InlineKeyboardButton(zone, callback_data=f"clean:{zone}")])
    buttons.append([InlineKeyboardButton("✅ All Done", callback_data="clean:ALL_DONE")])

    await ctx.bot.send_message(
        config.OWNER_GROUP_ID,
        "🧹 *Cleaning Reminder!*\nTime for a cleaning round. Tap when done:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def scheduled_opening_checklist(ctx: ContextTypes.DEFAULT_TYPE):
    """Auto-sent opening checklist."""
    if not config.OWNER_GROUP_ID:
        return
    existing = store.get_checklist_today("opening")
    if existing:
        return  # Already done

    buttons = []
    for i, item in enumerate(config.OPENING_CHECKLIST):
        buttons.append([InlineKeyboardButton(f"⬜ {item}", callback_data=f"chk:opening:{i}")])
    buttons.append([InlineKeyboardButton("✅ All Done!", callback_data="chk:opening:DONE")])

    await ctx.bot.send_message(
        config.OWNER_GROUP_ID,
        "🌅 *Good Morning! Opening Checklist*\nTap each item when done:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def scheduled_closing_checklist(ctx: ContextTypes.DEFAULT_TYPE):
    """Auto-sent closing checklist."""
    if not config.OWNER_GROUP_ID:
        return
    buttons = []
    for i, item in enumerate(config.CLOSING_CHECKLIST):
        buttons.append([InlineKeyboardButton(f"⬜ {item}", callback_data=f"chk:closing:{i}")])
    buttons.append([InlineKeyboardButton("✅ All Done!", callback_data="chk:closing:DONE")])

    await ctx.bot.send_message(
        config.OWNER_GROUP_ID,
        "🌙 *Closing Time! Checklist*\nTap each item when done:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def scheduled_stock_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    """Morning stock check reminder."""
    if not config.OWNER_GROUP_ID:
        return
    low = store.get_low_stock()
    msg = "📦 *Morning Stock Check*\nTime to check inventory! Use /stockcheck\n"
    if low:
        items = "\n".join(f"  🔴 {i}" for i, _ in low)
        msg += f"\n⚠️ *Items already flagged low:*\n{items}"
    await ctx.bot.send_message(config.OWNER_GROUP_ID, msg, parse_mode="Markdown")


async def scheduled_content_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    """Daily content reminder — ask staff what they're filming, suggest if no plan."""
    if not config.OWNER_GROUP_ID:
        return

    today_content = store.get_content_today()

    if today_content:
        # Someone has content planned — ask them about it directly
        for c in today_content:
            assigned = c.get("assigned_to", "team")
            title = c.get("title", "content")
            content_type = c.get("type", "content")
            notes = c.get("notes", "")

            msg = (
                f"📱 Hey {assigned}! You've got a {content_type} planned today: "
                f"*{title}*"
            )
            if notes:
                msg += f"\n📝 Notes: {notes}"
            msg += (
                f"\n\nWhat time are you thinking of shooting? "
                f"Need any help with angles or ideas? 🎬"
            )
            await ctx.bot.send_message(
                config.OWNER_GROUP_ID, msg, parse_mode="Markdown",
            )
            # Store this as a bot message in memory so the AI remembers it asked
            remember("Bot", f"Asked {assigned} about today's {content_type}: {title}", "text", config.OWNER_GROUP_ID)
    else:
        # No content planned — ask who's on shift what they want to film
        # and offer an AI suggestion
        try:
            suggestions = await generate_content_suggestions("Bot")
            if suggestions:
                first_idea = suggestions.split("\n\n")[0] if "\n\n" in suggestions else suggestions[:300]
                msg = (
                    f"📱 *Content Time!*\n\n"
                    f"No content planned for today yet. "
                    f"Anyone feel like shooting something?\n\n"
                    f"💡 Here's an idea:\n{first_idea}\n\n"
                    f"Just tell me what you want to film and I'll schedule it! 🎥"
                )
            else:
                idea = random.choice(config.CONTENT_IDEAS)
                msg = (
                    f"📱 *Content Time!*\n\n"
                    f"No content planned today. Anyone want to create something?\n\n"
                    f"💡 Quick idea: {idea}\n\n"
                    f"Just tell me what you're thinking! 🎥"
                )
        except Exception:
            idea = random.choice(config.CONTENT_IDEAS)
            msg = (
                f"📱 *Content Time!*\n\n"
                f"No content planned today. Anyone want to shoot something?\n\n"
                f"💡 Quick idea: {idea}\n\n"
                f"Just tell me! 🎥"
            )

        await ctx.bot.send_message(
            config.OWNER_GROUP_ID, msg, parse_mode="Markdown",
        )
        remember("Bot", "Asked team about content for today — no content planned", "text", config.OWNER_GROUP_ID)


async def scheduled_shift_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    """Check if anyone's shift is starting soon."""
    if not config.OWNER_GROUP_ID:
        return
    day = today_day()
    shifts = store.get_shifts(day)
    if not shifts:
        return

    now = now_sg()
    current_time = now.strftime("%H:%M")

    for staff, times in shifts.items():
        # Simple time comparison
        shift_start = times.get("start", "")
        if shift_start and shift_start > current_time:
            # Calculate minutes until shift
            try:
                sh, sm = map(int, shift_start.split(":"))
                shift_dt = now.replace(hour=sh, minute=sm, second=0)
                diff = (shift_dt - now).total_seconds() / 60
                if 25 <= diff <= 35:  # ~30 min before
                    await ctx.bot.send_message(
                        config.OWNER_GROUP_ID,
                        f"⏰ Shift reminder: *{staff}* starts at {shift_start} (~30 min)",
                        parse_mode="Markdown",
                    )
            except (ValueError, AttributeError):
                pass


async def scheduled_chaseup(ctx: ContextTypes.DEFAULT_TYPE):
    """Chase up on pending action items that are stale."""
    if not config.OWNER_GROUP_ID:
        return
    pending = store.get_action_items("pending")
    if not pending:
        return

    # Filter to stale items (not chased recently)
    stale = []
    now = now_sg()
    for i, item in enumerate(pending):
        last_chased = item.get("last_chased")
        created = item.get("created_at", "")
        ref_time = last_chased or created
        if ref_time:
            try:
                ref_dt = _parse_ts(ref_time)
                hours_old = (now - ref_dt).total_seconds() / 3600
                if hours_old >= config.CHASEUP_STALE_HOURS:
                    stale.append((i, item))
            except (ValueError, TypeError):
                stale.append((i, item))

    if not stale:
        return

    # Generate and send chase-up message
    items_only = [item for _, item in stale]
    message = await generate_chaseup_message(items_only)
    if message:
        await ctx.bot.send_message(
            config.OWNER_GROUP_ID,
            message,
            parse_mode="Markdown",
        )
        # Mark as chased
        for idx, _ in stale:
            store.mark_action_chased(idx)


async def scheduled_event_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    """Remind about events happening today or tomorrow."""
    if not config.OWNER_GROUP_ID:
        return
    events = store.get_events(upcoming_only=True)
    today_str = now_sg().date().isoformat()
    tomorrow_str = (now_sg().date() + timedelta(days=1)).isoformat()

    for evt in events:
        if evt["date"] == today_str:
            await ctx.bot.send_message(
                config.OWNER_GROUP_ID,
                f"📅 *TODAY's Event:* {evt['title']}\n📝 {evt['details']}",
                parse_mode="Markdown",
            )
        elif evt["date"] == tomorrow_str:
            await ctx.bot.send_message(
                config.OWNER_GROUP_ID,
                f"📅 *TOMORROW:* {evt['title']}\n📝 {evt['details']}\nMake sure we're prepared!",
                parse_mode="Markdown",
            )


async def scheduled_oneoff_check(ctx: ContextTypes.DEFAULT_TYPE):
    """Daily: check if any one-off items should be promoted to regular stock."""
    if not config.OWNER_GROUP_ID:
        return
    frequent = store.get_frequent_oneoffs(threshold=3)
    if not frequent:
        return

    lines = ["🔄 *One-off items bought frequently:*\n"]
    for f in frequent:
        lines.append(f"  • {f['item']} — bought {f['count']} times")
    lines.append("\nConsider making these regular stock items!")

    await ctx.bot.send_message(
        config.OWNER_GROUP_ID, "\n".join(lines), parse_mode="Markdown",
    )


async def scheduled_holiday_refresh(ctx: ContextTypes.DEFAULT_TYPE):
    """Weekly: refresh holiday cache from Google Calendar + Calendarific."""
    try:
        from google_integration import refresh_holiday_cache
        cache = refresh_holiday_cache()
        count = len(cache.get("holidays", []))
        logger.info(f"Holiday cache refreshed: {count} holidays")
    except Exception as e:
        logger.error(f"Holiday refresh error: {e}")


async def scheduled_holiday_alert(context: ContextTypes.DEFAULT_TYPE):
    """Daily check for upcoming holidays — alert at 14, 7, and 1 day(s) before."""
    try:
        from google_integration import get_upcoming_holidays
        today = now_sg().date()
        upcoming = get_upcoming_holidays(days_ahead=15)
        if not upcoming:
            return

        alerts = []
        for h in upcoming:
            try:
                h_date = datetime.strptime(h["date"], "%Y-%m-%d").date()
                days_until = (h_date - today).days
                if days_until in (14, 7, 1):
                    label = f"{days_until} day{'s' if days_until > 1 else ''}"
                    alerts.append(f"📅 *{h.get('name', 'Holiday')}* — {h['date']} ({label} away)")
            except (ValueError, KeyError):
                continue

        if alerts:
            msg = "🗓️ *Upcoming Holiday Alert*\n\n" + "\n".join(alerts)
            await context.bot.send_message(
                chat_id=config.OWNER_GROUP_ID,
                text=msg,
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error(f"Holiday alert error: {e}")


async def scheduled_school_holiday_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    """October: remind about upcoming school year-end holidays."""
    if not config.OWNER_GROUP_ID:
        return

    now = now_sg()
    if now.month != config.SCHOOL_HOLIDAY_REMINDER_MONTH:
        return
    if now.day != 1:  # Only on the 1st of October
        return

    try:
        from google_integration import get_upcoming_holidays
        upcoming = get_upcoming_holidays(days_ahead=90)  # Look 3 months ahead
        school = [h for h in upcoming if h.get("source") == "school_holidays"]

        if school:
            lines = ["🏫 *School Holiday Reminder*\n"]
            lines.append("Year-end school holidays are coming up! Plan for busier periods:\n")
            for h in school:
                end = f" to {h['end_date']}" if h.get('end_date') else ""
                lines.append(f"  📅 {h['name']}: {h['date']}{end}")
            lines.append("\n💡 Consider: extra stock, additional staff, holiday specials!")

            await ctx.bot.send_message(
                config.OWNER_GROUP_ID, "\n".join(lines), parse_mode="Markdown",
            )
    except Exception as e:
        logger.error(f"School holiday reminder error: {e}")


# ═══════════════════════════════════════════════════════════
#  🚀 MAIN — Wire everything up
# ═══════════════════════════════════════════════════════════

def main():
    """Start the bot."""
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # ─── AI-powered commands ───────────────────────────────
    async def cmd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Direct AI question: /ask <question>"""
        question = " ".join(ctx.args) if ctx.args else ""
        if not question:
            await update.message.reply_text("Usage: /ask <your question>\nExample: /ask how do I clean the grinder?")
            return
        await update.message.reply_text("🤔 Thinking...")
        chat_id = _get_chat_id(update)
        is_staff = _is_staff_group(update)
        response = await ask_ai(question, user_name(update),
                                chat_id=chat_id, is_staff_group=is_staff)
        if response:
            await update.message.reply_text(f"🤖 {response}")
        else:
            await update.message.reply_text("❌ AI is not available. Check that GEMINI_API_KEY is set.")

    async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """AI analyzes stock and suggests actions."""
        await update.message.reply_text("📊 Analyzing stock and operations...")
        response = await analyze_stock_and_suggest()
        if response:
            await update.message.reply_text(f"📊 *Stock Analysis*\n\n{response}", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ AI is not available. Check that GEMINI_API_KEY is set.")

    async def cmd_aicontent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """AI-generated content idea: /aicontent [topic]"""
        topic = " ".join(ctx.args) if ctx.args else ""
        await update.message.reply_text("📱 Generating content idea...")
        response = await get_content_idea(topic)
        if response:
            await update.message.reply_text(f"📱 *AI Content Idea*\n\n{response}", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ AI is not available. Try /content for pre-built ideas.")

    # ─── Command handlers ───────────────────────────────────
    # g = group_only (all groups), o = owner_only (blocked in staff group)
    g = group_only
    o = owner_only

    app.add_handler(CommandHandler("start", g(cmd_start)))
    app.add_handler(CommandHandler("help", g(cmd_help)))

    # Cleaning (allowed everywhere)
    app.add_handler(CommandHandler("clean", g(cmd_clean)))
    app.add_handler(CommandHandler("cleanstatus", g(cmd_cleanstatus)))
    app.add_handler(CallbackQueryHandler(g(cb_clean), pattern=r"^clean:"))

    # Stock (allowed everywhere)
    app.add_handler(CommandHandler("stock", g(cmd_stock)))
    app.add_handler(CommandHandler("stockcheck", g(cmd_stockcheck)))
    app.add_handler(CommandHandler("removestock", g(cmd_removestock)))
    app.add_handler(CommandHandler("lowstock", g(cmd_lowstock)))

    # Checklists (allowed everywhere)
    app.add_handler(CommandHandler("open", g(cmd_open)))
    app.add_handler(CommandHandler("close", g(cmd_close)))
    app.add_handler(CallbackQueryHandler(g(cb_checklist), pattern=r"^chk:"))

    # Shifts (owner only)
    app.add_handler(CommandHandler("shifts", o("shifts")(cmd_shifts)))
    app.add_handler(CommandHandler("addshift", o("addshift")(cmd_addshift)))
    app.add_handler(CommandHandler("removeshift", o("removeshift")(cmd_removeshift)))

    # Hours (owner only)
    app.add_handler(CommandHandler("hours", o("hours")(cmd_hours)))
    app.add_handler(CommandHandler("sethours", o("sethours")(cmd_sethours)))
    app.add_handler(CommandHandler("holiday", o("holiday")(cmd_setholiday)))
    app.add_handler(CommandHandler("holidays", o("holidays")(cmd_hours)))

    # Events (allowed everywhere)
    app.add_handler(CommandHandler("events", g(cmd_events)))
    app.add_handler(CommandHandler("addevent", g(cmd_addevent)))

    # Shopping (allowed everywhere)
    app.add_handler(CommandHandler("buy", g(cmd_buy)))
    app.add_handler(CommandHandler("addbuy", g(cmd_addbuy)))
    app.add_handler(CommandHandler("bought", g(cmd_bought)))

    # Content (allowed everywhere)
    app.add_handler(CommandHandler("content", g(cmd_content)))
    app.add_handler(CommandHandler("contentlog", g(cmd_contentlog)))
    app.add_handler(CallbackQueryHandler(g(cb_content), pattern=r"^content:"))

    # Staff (owner only)
    app.add_handler(CommandHandler("staff", o("staff")(cmd_staff)))
    app.add_handler(CommandHandler("addstaff", o("addstaff")(cmd_addstaff)))
    app.add_handler(CommandHandler("removestaff", o("removestaff")(cmd_removestaff)))

    # Reports (allowed everywhere)
    app.add_handler(CommandHandler("today", g(cmd_today)))
    app.add_handler(CommandHandler("week", g(cmd_week)))

    # Settings (owner only)
    app.add_handler(CommandHandler("setup", o("setup")(cmd_setup)))
    app.add_handler(CommandHandler("settings", o("settings")(cmd_settings)))

    # AI-powered
    app.add_handler(CommandHandler("ask", g(cmd_ask)))
    app.add_handler(CommandHandler("analyze", o("analyze")(cmd_analyze)))
    app.add_handler(CommandHandler("aicontent", g(cmd_aicontent)))

    # Action items / chase-up (allowed everywhere)
    app.add_handler(CommandHandler("tasks", g(cmd_tasks)))
    app.add_handler(CommandHandler("taskdone", g(cmd_taskdone)))
    app.add_handler(CommandHandler("taskdismiss", g(cmd_taskdismiss)))

    # Search history (allowed everywhere)
    app.add_handler(CommandHandler("search", g(cmd_search)))

    # P&L, receipts, data commands (owner only)
    app.add_handler(CommandHandler("pl", o("pl")(cmd_pl)))
    app.add_handler(CommandHandler("receipts", o("receipts")(cmd_receipts)))
    app.add_handler(CommandHandler("data", o("data")(cmd_data)))
    app.add_handler(CommandHandler("stockusage", g(cmd_stockusage)))
    app.add_handler(CommandHandler("expenses", o("expenses")(cmd_expenses)))
    app.add_handler(CommandHandler("pnl", o("pnl")(cmd_pnl_report)))
    app.add_handler(CommandHandler("whopaid", o("whopaid")(cmd_whopaid)))
    app.add_handler(CommandHandler("sales", o("sales")(cmd_sales)))

    # Receipt confirm/change callback (allowed everywhere — staff can submit receipts)
    app.add_handler(CallbackQueryHandler(g(cb_receipt), pattern=r"^receipt:"))
    app.add_handler(CallbackQueryHandler(g(cb_receipt_chgcat), pattern=r"^receipt:chgcat$"))
    app.add_handler(CallbackQueryHandler(g(cb_catitem), pattern=r"^catitem:\d+$"))
    app.add_handler(CallbackQueryHandler(g(cb_setcat), pattern=r"^setcat:\d+:"))

    # Sales report confirm/change callback (allowed everywhere — staff can submit POS)
    app.add_handler(CallbackQueryHandler(g(cb_sales), pattern=r"^sales:"))

    # New receipt item — regular vs one-off callback
    app.add_handler(CallbackQueryHandler(g(cb_newitem), pattern=r"^newitem:"))
    app.add_handler(CallbackQueryHandler(g(cb_duplicate_check), pattern=r"^dupcheck:"))

    # Voice note handler
    app.add_handler(MessageHandler(filters.VOICE, g(handle_voice_note)))

    # Photo handler (receipts + general photos)
    app.add_handler(MessageHandler(filters.PHOTO, g(handle_photo_message)))

    # Video handler (equipment issues, sales reports, general)
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.VIDEO_NOTE, g(handle_video_message)
    ))

    # Document handler (POS reports, CSV, Excel, PDF)
    app.add_handler(MessageHandler(filters.Document.ALL, g(handle_document)))

    # Natural language handler (must be last — has its own group gate)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_message
    ))

    # ─── Scheduled jobs ─────────────────────────────────────
    jq = app.job_queue
    if jq and config.OWNER_GROUP_ID:
        # ── Disabled for now ──────────────────────────────────
        # Opening checklist
        # jq.run_daily(scheduled_opening_checklist, time=config.OPENING_CHECKLIST_TIME,
        #     days=(0,1,2,3,4,5,6), name="opening_checklist")

        # Closing checklist
        # jq.run_daily(scheduled_closing_checklist, time=config.CLOSING_CHECKLIST_TIME,
        #     days=(0,1,2,3,4,5,6), name="closing_checklist")

        # Stock check reminder
        # jq.run_daily(scheduled_stock_reminder, time=config.MORNING_STOCK_CHECK_TIME,
        #     days=(0,1,2,3,4,5,6), name="stock_reminder")

        # Cleaning reminders
        # for i, t in enumerate(config.CLEANING_REMINDER_TIMES):
        #     jq.run_daily(scheduled_cleaning_reminder, time=t,
        #         days=(0,1,2,3,4,5,6), name=f"cleaning_{i}")

        # Content reminder
        # jq.run_daily(scheduled_content_reminder, time=config.CONTENT_REMINDER_TIME,
        #     days=(0,1,2,3,4,5,6), name="content_reminder")

        # Shift reminders
        # jq.run_repeating(scheduled_shift_reminder, interval=300, name="shift_reminder")

        # ── Still active ─────────────────────────────────────
        # Event reminders — once in the morning
        jq.run_daily(
            scheduled_event_reminder,
            time=dtime(8, 0),
            days=(0, 1, 2, 3, 4, 5, 6),
            name="event_reminder",
        )

        # Chase-up reminders for pending action items
        for i, t in enumerate(config.CHASEUP_REMINDER_TIMES):
            jq.run_daily(
                scheduled_chaseup,
                time=t,
                days=(0, 1, 2, 3, 4, 5, 6),
                name=f"chaseup_{i}",
            )

        # One-off items bought frequently — suggest promoting to regular stock
        jq.run_daily(
            scheduled_oneoff_check,
            time=dtime(3, 30, tzinfo=TZ),
            days=(0, 1, 2, 3, 4, 5, 6),
            name="oneoff_check",
        )

        # Holiday refresh — every Sunday at 3:00 AM MYT
        jq.run_daily(
            scheduled_holiday_refresh,
            time=dtime(3, 0, tzinfo=TZ),
            days=(6,),
            name="holiday_refresh",
        )

        # Holiday alert — daily check, fires at 14/7/1 days before a holiday
        jq.run_daily(
            scheduled_holiday_alert,
            time=dtime(9, 0, tzinfo=TZ),
            days=(0, 1, 2, 3, 4, 5, 6),
            name="holiday_alert",
        )

        # School holiday reminder — daily check (only fires on Oct 1)
        jq.run_daily(
            scheduled_school_holiday_reminder,
            time=dtime(9, 0, tzinfo=TZ),
            days=(0, 1, 2, 3, 4, 5, 6),
            name="school_holiday_reminder",
        )

        logger.info("✅ Scheduled jobs registered")
    elif not config.OWNER_GROUP_ID:
        logger.warning("⚠️ GROUP_CHAT_ID not set — scheduled reminders disabled. Use /setup to get your chat ID.")

    # ── SOP auto-refresh (always, not just owner group) ──
    if jq and store._sheets:
        jq.run_repeating(scheduled_sop_refresh, interval=60, first=60, name="sop_refresh")

    # ─── Set bot commands menu ──────────────────────────────
    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("start", "Show help & commands"),
            BotCommand("today", "Today's dashboard"),
            BotCommand("clean", "Start cleaning round"),
            BotCommand("stockcheck", "Run stock check"),
            BotCommand("open", "Opening checklist"),
            BotCommand("close", "Closing checklist"),
            BotCommand("shifts", "View shift schedule"),
            BotCommand("hours", "Café operating hours"),
            BotCommand("events", "Upcoming events"),
            BotCommand("buy", "Shopping list"),
            BotCommand("tasks", "Pending action items"),
            BotCommand("search", "Search chat history"),
            BotCommand("pl", "P&L summary"),
            BotCommand("receipts", "Recent receipts"),
            BotCommand("data", "Ask about business data"),
            BotCommand("stockusage", "Monthly stock usage"),
            BotCommand("content", "Get content idea"),
            BotCommand("staff", "Staff list"),
            BotCommand("ask", "Ask AI a question"),
            BotCommand("analyze", "AI stock analysis"),
            BotCommand("setup", "First-time setup"),
        ])

    app.post_init = post_init

    # Force refresh holiday cache on startup
    try:
        from google_integration import refresh_holiday_cache
        refresh_holiday_cache()
        logger.info("Holiday cache refreshed on startup")
    except Exception as e:
        logger.warning(f"Holiday cache refresh failed on startup (non-fatal): {e}")

    # ─── Start polling ──────────────────────────────────────
    logger.info(f"☕ {config.CAFE_NAME} Manager Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
