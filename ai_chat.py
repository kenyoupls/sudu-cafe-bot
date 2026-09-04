"""
Café Manager Bot — AI Chat Module (Gemini Flash)
Powers natural language conversations with cafe context.
Includes: conversation memory, voice note transcription, photo understanding.

Free tier: 15 RPM, 1,500 RPD, 1M TPM on Gemini 2.0 Flash.
Audio + image input included in free tier.
"""
import base64
import io
import json
import logging
from collections import deque
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

try:
    import edge_tts
    HAS_EDGE_TTS = True
except ImportError:
    HAS_EDGE_TTS = False

try:
    from groq import Groq
    HAS_GROQ = True
except ImportError:
    HAS_GROQ = False

import requests

import config
from storage import get_store
from sop_data import build_sop_prompt

logger = logging.getLogger(__name__)

# ─── Timezone-aware now ────────────────────────────────────
_TZ = ZoneInfo(config.TIMEZONE)

def _now():
    """Get current time in the configured timezone."""
    return datetime.now(_TZ)

def _today():
    """Get today's date in the configured timezone."""
    return _now().date()


# ─── Google Places API — live café info ────────────────────
_cafe_info_cache = {"data": None, "fetched_at": None}


def get_cafe_google_info() -> dict:
    """Fetch live café info from Google Places API. Cached for 2 hours."""
    # Return cache if fresh (within 2 hours)
    if (_cafe_info_cache["data"]
            and _cafe_info_cache["fetched_at"]
            and (_now() - _cafe_info_cache["fetched_at"]).total_seconds() < 7200):
        return _cafe_info_cache["data"]

    place_id = getattr(config, "GOOGLE_PLACE_ID", "")
    api_key = getattr(config, "GOOGLE_PLACES_API_KEY", "")

    if not place_id or not api_key:
        return {}

    try:
        url = f"https://places.googleapis.com/v1/places/{place_id}"
        headers = {
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": (
                "displayName,formattedAddress,currentOpeningHours,"
                "regularOpeningHours,rating,userRatingCount,"
                "reviews,websiteUri,nationalPhoneNumber,"
                "editorialSummary,dineIn,takeout,delivery,"
                "servesBreakfast,servesLunch,servesDinner,"
                "servesDessert"
            ),
        }
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            info = {
                "name": data.get("displayName", {}).get("text", config.CAFE_NAME),
                "address": data.get("formattedAddress", ""),
                "rating": data.get("rating", ""),
                "total_reviews": data.get("userRatingCount", 0),
                "phone": data.get("nationalPhoneNumber", ""),
                "website": data.get("websiteUri", ""),
            }

            # Opening hours
            hours = data.get("currentOpeningHours") or data.get("regularOpeningHours")
            if hours:
                info["open_now"] = hours.get("openNow", None)
                if "weekdayDescriptions" in hours:
                    info["hours"] = hours["weekdayDescriptions"]
                elif "periods" in hours:
                    # Build readable hours from periods
                    day_names = ["Sunday", "Monday", "Tuesday", "Wednesday",
                                 "Thursday", "Friday", "Saturday"]
                    day_hours = {}
                    for p in hours["periods"]:
                        d = p.get("open", {}).get("day", 0)
                        oh = p.get("open", {}).get("hour", 0)
                        om = p.get("open", {}).get("minute", 0)
                        ch = p.get("close", {}).get("hour", 0)
                        cm = p.get("close", {}).get("minute", 0)
                        day_hours[d] = f"{day_names[d]}: {oh:02d}:{om:02d} – {ch:02d}:{cm:02d}"
                    info["hours"] = [day_hours.get(i, f"{day_names[i]}: Closed") for i in range(7)]

            # Recent reviews
            reviews = data.get("reviews", [])
            info["recent_reviews"] = []
            for r in reviews[:5]:
                info["recent_reviews"].append({
                    "rating": r.get("rating", ""),
                    "text": r.get("text", {}).get("text", "")[:200],
                    "author": r.get("authorAttribution", {}).get("displayName", ""),
                })

            _cafe_info_cache["data"] = info
            _cafe_info_cache["fetched_at"] = _now()
            logger.info("Fetched fresh café info from Google Places")
            return info
        else:
            logger.warning(f"Google Places API error: {resp.status_code} {resp.text[:200]}")
            return _cafe_info_cache.get("data") or {}
    except Exception as e:
        logger.warning(f"Google Places fetch failed: {e}")
        return _cafe_info_cache.get("data") or {}

# ─── Client setup ───────────────────────────────────────────
_client: Optional[genai.Client] = None


def get_client() -> Optional[genai.Client]:
    """Lazy-init Gemini client."""
    global _client
    if _client is None:
        api_key = config.GEMINI_API_KEY
        if not api_key or api_key == "YOUR_GEMINI_API_KEY_HERE":
            logger.warning("GEMINI_API_KEY not set — AI chat disabled")
            return None
        _client = genai.Client(api_key=api_key)
    return _client


# ─── Groq Client (fallback) ─────────────────────────────────
_groq_client: Optional[Groq] = None

GROQ_TEXT_MODEL = "openai/gpt-oss-120b"
GROQ_VISION_MODEL = "meta-llama/llama-4-maverick-17b-128e-instruct"
GROQ_TEXT_MODEL_SMALL = "llama-3.1-8b-instant"


def get_groq_client() -> Optional[Groq]:
    """Lazy-init Groq client (fallback when Gemini fails)."""
    global _groq_client
    if _groq_client is None:
        if not HAS_GROQ:
            logger.warning("groq package not installed — fallback disabled")
            return None
        api_key = getattr(config, "GROQ_API_KEY", "")
        if not api_key:
            logger.warning("GROQ_API_KEY not set — fallback disabled")
            return None
        _groq_client = Groq(api_key=api_key)
    return _groq_client


def _image_to_data_url(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Convert raw image bytes to a base64 data URL for Groq vision."""
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{b64}"


async def _groq_text(prompt: str, system: str = "", temperature: float = 0.7,
                     max_tokens: int = 500) -> Optional[str]:
    """Send a text-only request to Groq. Returns response text or None."""
    client = get_groq_client()
    if client is None:
        return None

    for model in [GROQ_TEXT_MODEL, GROQ_TEXT_MODEL_SMALL]:
        try:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content.strip() if resp.choices else None
        except Exception as e:
            err_str = str(e)
            if "413" in err_str or "too large" in err_str.lower() or "rate_limit" in err_str.lower():
                logger.warning(f"Groq {model} too large, trying smaller model")
                continue
            logger.error(f"Groq text error ({model}): {e}")
            return None

    logger.error("Groq text: all models failed")
    return None


async def _groq_vision(prompt: str, image_bytes: bytes, mime_type: str = "image/jpeg",
                       system: str = "", temperature: float = 0.5,
                       max_tokens: int = 300) -> Optional[str]:
    """Send an image + text request to Groq vision. Returns response text or None."""
    client = get_groq_client()
    if client is None:
        return None
    try:
        data_url = _image_to_data_url(image_bytes, mime_type)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt},
            ],
        })
        resp = client.chat.completions.create(
            model=GROQ_VISION_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content.strip() if resp.choices else None
    except Exception as e:
        logger.error(f"Groq vision error: {e}")
        return None


# ═══════════════════════════════════════════════════════════
#  🧠 CONVERSATION MEMORY — 1-Month with Smart Compression
# ═══════════════════════════════════════════════════════════
#
# Storage layout:
#   data/memory/2026-08-22.json   — list of messages for that day
#   data/memory/summaries.json    — daily summaries (auto-generated)
#
# Context sent to Gemini:
#   1. Daily summaries for days older than SUMMARY_DAYS_START
#   2. Last RECENT_MESSAGES_FULL messages verbatim (newest)
#
# Cleanup: messages older than MEMORY_RETENTION_DAYS auto-deleted.

MEMORY_BASE_DIR = Path("data") / "memory"
# Legacy non-group dir (backward compat — used when chat_id=0)
MEMORY_DIR = MEMORY_BASE_DIR
SUMMARIES_FILE = MEMORY_DIR / "summaries.json"

# In-memory cache: {group_key: {date_str: [messages]}}
_day_cache: dict = {}
# In-memory cache: {group_key: {date_str: summary}}
_summaries_cache: dict = {}
_cache_loaded: set = set()  # set of group_keys that have been loaded


def _group_memory_dir(chat_id: int = 0) -> Path:
    """Return group-specific memory directory."""
    if not chat_id:
        return MEMORY_BASE_DIR
    return MEMORY_BASE_DIR / str(chat_id)


def _group_summaries_file(chat_id: int = 0) -> Path:
    return _group_memory_dir(chat_id) / "summaries.json"


def _ensure_dir(chat_id: int = 0):
    _group_memory_dir(chat_id).mkdir(parents=True, exist_ok=True)


def _group_key(chat_id: int = 0) -> str:
    return str(chat_id) if chat_id else "_default"


def _day_file(day: str, chat_id: int = 0) -> Path:
    """Return path like data/memory/{chat_id}/2026-08-22.json"""
    return _group_memory_dir(chat_id) / f"{day}.json"


def _load_day(day: str, chat_id: int = 0) -> list:
    """Load messages for a single day in a specific group."""
    gk = _group_key(chat_id)
    if gk not in _day_cache:
        _day_cache[gk] = {}
    if day in _day_cache[gk]:
        return _day_cache[gk][day]
    path = _day_file(day, chat_id)
    msgs = []
    if path.exists():
        try:
            with open(path, "r") as f:
                msgs = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load memory for {day} (group {chat_id}): {e}")
    _day_cache[gk][day] = msgs
    return msgs


def _save_day(day: str, chat_id: int = 0):
    """Persist messages for a single day."""
    gk = _group_key(chat_id)
    _ensure_dir(chat_id)
    try:
        with open(_day_file(day, chat_id), "w") as f:
            json.dump(_day_cache.get(gk, {}).get(day, []), f, indent=1, default=str)
    except Exception as e:
        logger.error(f"Failed to save memory for {day} (group {chat_id}): {e}")


def _load_summaries(chat_id: int = 0) -> dict:
    """Load daily summaries from disk for a specific group."""
    gk = _group_key(chat_id)
    if gk not in _summaries_cache:
        _summaries_cache[gk] = {}
    if _summaries_cache[gk]:
        return _summaries_cache[gk]
    sf = _group_summaries_file(chat_id)
    if sf.exists():
        try:
            with open(sf, "r") as f:
                _summaries_cache[gk] = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load summaries (group {chat_id}): {e}")
            _summaries_cache[gk] = {}
    return _summaries_cache[gk]


def _save_summaries(chat_id: int = 0):
    """Persist summaries to disk."""
    gk = _group_key(chat_id)
    _ensure_dir(chat_id)
    try:
        with open(_group_summaries_file(chat_id), "w") as f:
            json.dump(_summaries_cache.get(gk, {}), f, indent=1, default=str)
    except Exception as e:
        logger.error(f"Failed to save summaries (group {chat_id}): {e}")


def _migrate_old_memory():
    """One-time migration: move old chat_memory.json into daily files (default group)."""
    old_file = Path("data") / "chat_memory.json"
    if not old_file.exists():
        return
    try:
        with open(old_file, "r") as f:
            old_data = json.load(f)
        if not old_data:
            old_file.unlink(missing_ok=True)
            return
        # Group by date — put into default (no group) memory
        by_day: dict = {}
        for msg in old_data:
            day = msg.get("time", "")[:10]
            if not day:
                day = _today().isoformat()
            by_day.setdefault(day, []).append(msg)
        _ensure_dir(0)
        gk = _group_key(0)
        if gk not in _day_cache:
            _day_cache[gk] = {}
        for day, msgs in by_day.items():
            existing = _load_day(day, 0)
            existing.extend(msgs)
            _day_cache[gk][day] = existing
            _save_day(day, 0)
        old_file.rename(old_file.with_suffix(".json.migrated"))
        logger.info(f"Migrated {len(old_data)} messages from old memory format")
    except Exception as e:
        logger.error(f"Migration error: {e}")

    # Also migrate existing daily files from data/memory/*.json to default group
    # (backward compat — old files without group subdir)
    try:
        for path in MEMORY_BASE_DIR.glob("20??-??-??.json"):
            day_str = path.stem
            existing = _load_day(day_str, 0)
            # Already loaded from the same path, just ensure it's in cache
            if gk not in _day_cache:
                _day_cache[gk] = {}
            _day_cache[gk][day_str] = existing
    except Exception:
        pass


def _cleanup_old_days(chat_id: int = 0):
    """Delete memory files older than MEMORY_RETENTION_DAYS. 0 = keep forever."""
    if config.MEMORY_RETENTION_DAYS <= 0:
        return
    cutoff = _today() - __import__("datetime").timedelta(days=config.MEMORY_RETENTION_DAYS)
    gk = _group_key(chat_id)
    mem_dir = _group_memory_dir(chat_id)
    try:
        for path in mem_dir.glob("20??-??-??.json"):
            day_str = path.stem
            try:
                day_date = date.fromisoformat(day_str)
                if day_date < cutoff:
                    path.unlink()
                    if gk in _day_cache:
                        _day_cache[gk].pop(day_str, None)
                    if gk in _summaries_cache:
                        _summaries_cache[gk].pop(day_str, None)
            except ValueError:
                continue
        _save_summaries(chat_id)
    except Exception as e:
        logger.error(f"Cleanup error: {e}")


# ─── Importance filter ─────────────────────────────────────
# Only "important" messages get stored in memory for AI context.
# Everything still gets saved to daily files for search/history.

_IMPORTANT_KEYWORDS = {
    # Stock / supplies
    "stock", "habis", "out of", "low", "restock", "order", "beli", "buy",
    "delivery", "supplier", "milk", "coffee", "beans", "sugar", "cups",
    "kopi", "teh", "susu",
    # Equipment
    "rosak", "broken", "machine", "grinder", "fridge", "fix", "repair",
    "maintenance", "tak jalan", "problem",
    # Cleaning
    "clean", "mop", "wipe", "cuci", "toilet", "dirty", "kotor",
    # Tasks / actions
    "need to", "kena", "must", "please", "tolong", "remind", "todo",
    "do", "done", "siap", "settle", "belum",
    # Shifts / schedule
    "shift", "late", "lambat", "off", "mc", "cuti", "leave", "cover",
    "replacement", "ganti",
    # Events / planning
    "event", "promo", "holiday", "cuti sekolah", "school holiday",
    "decoration", "deco", "menu", "special",
    # Money
    "cash", "duit", "rm", "price", "harga", "cost", "pay", "bayar",
    "sales", "jualan",
    # Issues / complaints
    "complaint", "customer", "issue", "problem", "masalah",
    # Hours
    "hours", "open", "close", "buka", "tutup",
    # Content
    "post", "content", "instagram", "social media", "tiktok", "photo",
}


def _is_important(text: str) -> bool:
    """Check if a message is important enough to store for AI context."""
    if not text:
        return False
    lower = text.lower()
    # Always important if it's a question
    if "?" in text:
        return True
    # Check keywords
    for kw in _IMPORTANT_KEYWORDS:
        if kw in lower:
            return True
    # Short greetings / chit-chat → not important
    if len(text) < 15:
        return False
    # Longer messages are usually worth keeping
    if len(text) > 50:
        return True
    return False


def search_memory(query: str, max_results: int = 20, chat_id: int = 0) -> list:
    """Search through ALL stored messages (not just recent ones)."""
    _init_memory(chat_id)
    query_lower = query.lower()
    results = []
    all_days = _get_all_recent_days(chat_id)

    for day_str in reversed(all_days):  # newest first
        msgs = _load_day(day_str, chat_id)
        for msg in reversed(msgs):
            text = msg.get("text", "").lower()
            who = msg.get("who", "").lower()
            if query_lower in text or query_lower in who:
                results.append(msg)
                if len(results) >= max_results:
                    return results
    return results


def _init_memory(chat_id: int = 0):
    """First-use init: migrate, cleanup."""
    gk = _group_key(chat_id)
    if gk in _cache_loaded:
        return
    _cache_loaded.add(gk)
    if chat_id == 0:
        _migrate_old_memory()
    _cleanup_old_days(chat_id)
    _load_summaries(chat_id)


def remember(user_name: str, message: str, msg_type: str = "text",
             chat_id: int = 0, message_id: int = 0):
    """Store a message in today's memory bucket. Marks importance for AI context.

    chat_id: Telegram group ID for context isolation. Each group gets its own
    memory directory so conversations stay separate.
    """
    _init_memory(chat_id)
    gk = _group_key(chat_id)
    today = _today().isoformat()
    msgs = _load_day(today, chat_id)
    important = _is_important(message) or msg_type in ("voice", "photo", "receipt", "document")
    entry = {
        "time": _now().strftime("%Y-%m-%d %H:%M"),
        "who": user_name,
        "type": msg_type,
        "text": message[:500],
        "important": important,
    }
    # Store Telegram reference for media so we can point staff back to it
    if chat_id and message_id and msg_type in ("voice", "photo", "receipt", "document"):
        entry["tg_ref"] = {"chat_id": chat_id, "message_id": message_id}
    msgs.append(entry)
    if gk not in _day_cache:
        _day_cache[gk] = {}
    _day_cache[gk][today] = msgs
    _save_day(today, chat_id)


def remember_bot_response(response: str, chat_id: int = 0):
    """Store the bot's own response in today's memory bucket."""
    _init_memory(chat_id)
    gk = _group_key(chat_id)
    today = _today().isoformat()
    msgs = _load_day(today, chat_id)
    msgs.append({
        "time": _now().strftime("%Y-%m-%d %H:%M"),
        "who": "Bot",
        "type": "bot_response",
        "text": response[:300],
    })
    if gk not in _day_cache:
        _day_cache[gk] = {}
    _day_cache[gk][today] = msgs
    _save_day(today, chat_id)


def _auto_summarise_day(day_str: str, messages: list) -> str:
    """Create a short text summary of a day's messages (no AI call — rule-based)."""
    if not messages:
        return ""
    people = set()
    topics = []
    voice_count = 0
    photo_count = 0
    for m in messages:
        who = m.get("who", "")
        if who and who != "Bot":
            people.add(who)
        mtype = m.get("type", "text")
        if mtype == "voice":
            voice_count += 1
        elif mtype == "photo":
            photo_count += 1
        text = m.get("text", "").lower()
        # Extract topic keywords
        for keyword in ["stock", "clean", "order", "delivery", "broken", "late",
                        "shift", "event", "holiday", "menu", "promo", "complaint",
                        "machine", "fridge", "milk", "coffee", "close", "open"]:
            if keyword in text and keyword not in topics:
                topics.append(keyword)

    parts = [f"{day_str}: {len(messages)} msgs"]
    if people:
        parts.append(f"from {', '.join(sorted(people))}")
    if topics:
        parts.append(f"topics: {', '.join(topics[:8])}")
    if voice_count:
        parts.append(f"{voice_count} voice notes")
    if photo_count:
        parts.append(f"{photo_count} photos")
    return " | ".join(parts)


def _get_all_recent_days(chat_id: int = 0) -> list:
    """Return sorted list of day strings we have on disk (newest last)."""
    _ensure_dir(chat_id)
    mem_dir = _group_memory_dir(chat_id)
    days = []
    for path in mem_dir.glob("20??-??-??.json"):
        days.append(path.stem)
    days.sort()
    return days


def get_memory_context(chat_id: int = 0) -> str:
    """
    Build memory context for Gemini:
    - Days older than SUMMARY_DAYS_START → one-line summaries
    - Last RECENT_MESSAGES_FULL messages → verbatim
    """
    _init_memory(chat_id)
    today = _today()
    summary_cutoff = today - __import__("datetime").timedelta(days=config.SUMMARY_DAYS_START)

    all_days = _get_all_recent_days(chat_id)
    if not all_days:
        return ""

    lines = []
    recent_messages = []

    # Split days into "old" (summarise) and "recent" (verbatim)
    for day_str in all_days:
        try:
            day_date = date.fromisoformat(day_str)
        except ValueError:
            continue
        msgs = _load_day(day_str, chat_id)
        if not msgs:
            continue

        if day_date < summary_cutoff:
            # Use cached summary or generate one
            summaries = _load_summaries(chat_id)
            if day_str not in summaries:
                summaries[day_str] = _auto_summarise_day(day_str, msgs)
                _save_summaries(chat_id)
            lines.append(summaries[day_str])
        else:
            # Only include important messages in verbatim context
            for msg in msgs:
                if msg.get("important", True) or msg.get("type") in ("bot_response", "voice", "photo"):
                    recent_messages.append(msg)

    # Build output
    output = []

    if lines:
        output.append("--- OLDER CHAT SUMMARIES (past month) ---")
        output.extend(lines)
        output.append("--- END SUMMARIES ---\n")

    # Take last N messages verbatim
    recent_messages = recent_messages[-config.RECENT_MESSAGES_FULL:]

    if recent_messages:
        output.append("--- RECENT GROUP CHAT (newest last) ---")
        for msg in recent_messages:
            time_str = msg.get("time", "")
            who = msg.get("who", "Unknown")
            msg_type = msg.get("type", "text")
            text = msg.get("text", "")

            if msg_type == "voice":
                output.append(f"[{time_str}] {who} (voice note): {text}")
            elif msg_type == "photo":
                output.append(f"[{time_str}] {who} (sent photo): {text}")
            elif msg_type == "bot_response":
                output.append(f"[{time_str}] Bot: {text}")
            else:
                output.append(f"[{time_str}] {who}: {text}")
        output.append("--- END CHAT HISTORY ---")

    return "\n".join(output)


# ═══════════════════════════════════════════════════════════
#  SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════

_SYSTEM_PROMPT_TEMPLATE = """You are the AI MANAGER of CAFE_NAME_HERE, a bingsu café in Melaka, Malaysia. You are not an assistant — you are the MANAGER. You live in the café's Telegram group and you actively run the business alongside the team.

YOUR ROLE — FULL BOSS MODE:
- You THINK like a café owner. Every message you read, you ask: "What does this mean for the business? What should happen next? Who needs to do what?"
- You DO NOT wait to be asked. If you see a problem, flag it. If something needs doing, assign it. If numbers look off, question them.
- You CONNECT THE DOTS across conversations. "Eric said milk was low yesterday, and today's sales show 40 bingsu orders — we're probably out by now. @Eric did you restock?"
- You FOLLOW UP. If someone said they'd do something and hasn't reported back, you ask about it.
- You THINK STRATEGICALLY. "Sales are down 15% this week vs last week — could be the rain. Consider a rainy day promo: buy 1 free 1 on slow items."
- You ASSIGN TASKS directly. Don't say "someone should..." — say "@Eric please do X by [time]."
- You FLAG RISKS before they become problems. Low stock? Say so before it runs out. Big event coming? Start planning early.

Reply rules: Be SHORT and DIRECT. Max 1-2 sentences. No fluff, no motivational add-ons, no unnecessary encouragement. Just answer the question or confirm the action.

Your personality:
- Direct, no-nonsense, but still warm — like a hands-on café owner who works the floor
- Keep responses SHORT and punchy — 2-3 sentences max. This is Telegram, not email. Get to the point.
- Use emoji sparingly, only where natural
- Be specific, never generic. "Order 5 cartons of milk from Giant" not "consider restocking dairy"
- Say "we" not "you" — you're part of the team
- If you spot something wrong, say it straight: "That doesn't look right — [reason]. Fix: [solution]."
- If you don't know something, say so. Never make up data.

MANAGER MODE: You are the manager. When staff reports issues, don't just acknowledge — follow up:
- Ask if tasks were completed ("Did you finish the cleaning?", "Is the fridge restocked?")
- If someone reports a problem, ask for updates later
- Hold staff accountable — if a task was assigned, check if it's done

PROACTIVE MANAGEMENT — what sets you apart:
- When staff reports stock: think about whether it's enough for the week, flag if not
- When you see a receipt: CHECK the context data for previous orders from the same supplier. Compare prices to what we paid before. If something costs more than last time, say so with numbers: "Sugar from Giant was RM2.50/kg last month, now RM3.20 — that's 28% more." Don't ASK if prices went up — CHECK and TELL.
- When someone mentions a problem: think about root cause, suggest a fix AND a prevention
- When sales data comes in: compare to previous days/weeks if data is in context, spot trends, suggest actions
- When someone does great work: acknowledge it briefly — "Nice one, Eric. ✅"
- When a deadline is approaching: remind the team without nagging

You actively manage:
- Operations: stock, cleaning, equipment, checklists, supplier orders
- People: task assignments, follow-ups, accountability, recognition
- Money: expenses, sales trends, P&L awareness, cost control
- Content: social media planning, content calendar, ideas
- Strategy: promos, events, seasonal planning, menu optimization
- Problems: troubleshooting, food safety, customer complaints, equipment issues

TROUBLESHOOTING RULE: When staff reports something broken/not working (bulb, machine, equipment), DO NOT immediately add to shopping list. First suggest troubleshooting steps (check if loose, reset, clean, etc). Only add to shopping list if staff confirms the item is actually broken beyond repair and needs replacement.

LANGUAGE RULES (VERY IMPORTANT — Malaysia context):
- Staff may write in English, Bahasa Melayu, Mandarin Chinese, Tamil, or MIX multiple languages in ONE message. This is completely normal in Malaysia.
- Examples of mixed messages you must understand:
  "Eh the milk habis already lah" (English + Malay)
  "Boss cakap 冰咖啡 tak sedap hari ni" (Malay + Mandarin)
  "Bro the machine rosak again la wei" (English + Malay)
  "Toilet no more tissue, kena beli" (English + Malay)
  "Aiyo the kopi-o ais all sold out dah" (mixed Malaysian style)
  "Tauke kata stock tak cukup, 要买多一点" (Malay + Mandarin)
- ALWAYS reply in ENGLISH by default. Only switch to another language if the message is clearly written entirely in that language (e.g., full Malay or full Mandarin). If the message is mixed (English + Malay, etc.), reply in English.
- For voice notes: staff may speak in any language or mix languages mid-sentence. Transcribe in the ORIGINAL language(s) they used, then respond in the same style.
- For photos with text: read text in ANY language on signs, labels, receipts, menus, delivery orders (may be in BM, Chinese, Tamil, English). Translate if needed but keep your response in the sender's language style.
- Common MY café terms to know: kopi (coffee), teh (tea), ais (iced), kaw (strong/thick), kurang manis (less sugar), kosong (plain/zero), tapau/bungkus (takeaway), makan (eat), habis (finished/out), rosak (broken), sedap (delicious), best/power (great), jialat (trouble), tauke/boss (owner), kena (must/got hit by), tak (not), dah/sudah (already), lah/la/wei/weh/kan/bah (particles).
- Never correct someone's mixed language — it's how we talk here.
- Currency is RM (Ringgit Malaysia). Prices and costs are in RM.

ACTIONS YOU CAN TRIGGER:
When a message implies something actionable, include a JSON block at the END of your reply (after your chat response) wrapped in ```actions``` fences. The bot will parse and execute these silently.

Available actions:

--- DATA ENTRY ---
- update_stock: {"action": "update_stock", "item": "Coffee Beans", "qty": "OK", "note": "just restocked"}
  qty values: "OK", "LOW", "OUT", or a number like "5 bags"
- log_cleaning: {"action": "log_cleaning", "zone": "Toilets"}
  zone must match one of the configured zones
- add_shopping: {"action": "add_shopping", "item": "Oat milk x5", "urgency": "normal"}
  urgency: "normal" or "urgent"
- mark_bought: {"action": "mark_bought", "item": "Oat milk"}
- save_instruction: {"action": "save_instruction", "instruction": "always reply in Malay when staff writes in Malay"}
  Use when an admin says things like "from now on...", "remember that...", "always do...", "never do...", "our rule is...". Save the instruction so you follow it permanently.
- learn_alias: {"action": "learn_alias", "canonical": "Nata de Coco", "alias": "ndc"}
  Use when staff refers to an item by a nickname, abbreviation, or alternate name. This teaches the bot to map the alias to the correct stock item going forward.
- add_event: {"action": "add_event", "title": "Live Music", "date": "2026-09-15", "details": "Jazz band 7-10pm"}
- stock_count: {"action": "stock_count", "item": "Coffee Beans", "count": "3 bags", "note": "counted by Ahmad"}
  Use this when someone reports a single stock count.
  Prefer bulk_stock over stock_count — use stock_count only when staff explicitly reports just ONE item's count.
- bulk_stock: {"action": "bulk_stock", "checked_by": "Edwin", "date": "20/08/26", "items": [{"item": "Full Cream Milk", "qty": "112"}, {"item": "Low Fat Milk", "qty": "73"}, {"item": "Matcha Powder", "qty": "2"}]}
  IMPORTANT: Use this when someone sends a full stock list or multiple items at once. Put EVERY item as a separate entry in the items array. Include ALL items from the message — do not skip any. If qty is empty or unclear, use dash. The "date" field is the date the stock was counted (format dd/mm/yy). Extract the date from the message if mentioned (e.g. "Updated by: 20.08.2026" → "20/08/26"). If no date is mentioned, omit the date field and it defaults to today. This saves each item individually to Google Sheets with history by date.
  When staff sends a list of items with quantities, ALWAYS use bulk_stock — even for just 2-3 items.
  This is a PHYSICAL COUNT: set each item's stock to the number given (overwrite, not add).
  Examples that should trigger bulk_stock:
    "milk 6, sugar 3, cups 200, ice 10 bags"
    "susu 6, gula 3, cawan 200, ais 10 beg"
    "milk - 6\nsugar - 3\ncups - 200"
    "Whipping cream 1L: 5 boxes\nNata de coco: 12 packets"
    "everything ok except milk is 2 and sugar is low"
  → {"action": "bulk_stock", "checked_by": "<name>", "items": [{"item": "Milk", "qty": "6"}, ...]}
- plan_content: {"action": "plan_content", "title": "Latte art video", "type": "reel", "date": "2026-08-25", "assigned_to": "Ahmad", "notes": "Show the rosetta pour"}
  type: photo, video, reel, story, post. Use when someone plans content for the café socials.
- done_content: {"action": "done_content", "title": "Latte art video"}
  Mark a planned content piece as completed.
- suggest_content: {"action": "suggest_content"}
  When someone asks for content ideas. The bot will generate AI suggestions based on today's context.
- correct_stock: {"action": "correct_stock", "item": "Nata de Coco", "qty": 6, "note": "wrong count earlier"}
  Use when staff says a previous stock entry was WRONG and needs to be corrected/overwritten. Keywords: "that's wrong", "salah tu", "bukan", "change to", "actually it's", "correction", "betulkan". This OVERWRITES the current stock, not adds to it.
- undo_receipt: {"action": "undo_receipt", "supplier": "Giant", "date": "2026-08-22", "items": [{"name": "Nata de Coco", "qty": 12}]}
  Use when staff says a receipt was wrong, cancel it, undo it. This reverses the stock additions AND deletes the expense rows. Keywords: "cancel receipt", "undo receipt", "wrong receipt", "batalkan resit", "salah resit".

--- REPORTS (use these when someone asks to SEE data — no slash commands needed) ---
- show_today: {"action": "show_today"}
  Use when someone asks "how's today", "what's happening today", "today's summary", "any updates today", etc.
- show_expenses: {"action": "show_expenses", "month": "2026-08"}
  Use when someone asks about expenses, spending, costs, "how much we spent", "berapa belanja", etc. month is optional — defaults to current month.
- show_whopaid: {"action": "show_whopaid", "month": "2026-08"}
  Use when someone asks "who paid what", "siapa bayar", "repayment", "how much each person paid", etc. month is optional.
- show_sales: {"action": "show_sales", "month": "2026-08"}
  Use when someone asks "how are sales", "sales this month", "daily sales", "revenue", "how much we made", etc. month is optional.
- show_pnl: {"action": "show_pnl", "month": "2026-08"}
  Use when someone asks for "P&L", "profit and loss", "profit report", "monthly report file", "generate P&L", etc. month is optional. This generates and sends an Excel file.
- show_stock: {"action": "show_stock"}
  Use when someone asks "what's our stock", "stock levels", "how much milk left", "inventory", etc.
- show_lowstock: {"action": "show_lowstock"}
  Use when someone asks "what's running low", "apa yang kurang", "need to buy anything", etc.
- show_shopping: {"action": "show_shopping"}
  Use when someone asks "what do we need to buy", "shopping list", "senarai beli", etc.
- show_cleaning: {"action": "show_cleaning"}
  Use when someone asks "cleaning done?", "who cleaned", "any zones left", etc.
- show_shifts: {"action": "show_shifts"}
  Use when someone asks "who's working today", "shift schedule", "jadual kerja", etc.
- show_week: {"action": "show_week"}
  Use when someone asks "what's happening this week", "weekly plan", "any events coming up", etc.
- show_tasks: {"action": "show_tasks"}
  Use when someone asks "any pending tasks", "what needs to be done", "action items", etc.
- show_staff: {"action": "show_staff"}
  Use when someone asks "who's on the team", "staff list", "senarai pekerja", etc.

You can include MULTIPLE actions in one array. Examples:

Staff says: "Eh cleaned the toilet and kitchen already"
Your reply: "Nice, noted! Both done. ✅"
```actions
[{"action": "log_cleaning", "zone": "🚻 Toilets"}, {"action": "log_cleaning", "zone": "🍳 Kitchen"}]
```

Staff says: "We used 2 bags coffee beans and 3 cartons milk today, milk running low already"
Your reply: "Got it, I'll update the stock. Milk low — should I add to shopping list?"
```actions
[{"action": "update_stock", "item": "Coffee Beans", "qty": "OK", "note": "used 2 bags today"}, {"action": "update_stock", "item": "Milk & Dairy", "qty": "LOW", "note": "3 cartons used, running low"}, {"action": "add_shopping", "item": "Milk", "urgency": "urgent"}]
```

Staff says: "Sugar habis, kena beli cepat"
Your reply: "Alamak, sugar habis! Added to shopping list as urgent. Siapa boleh beli hari ni?"
```actions
[{"action": "update_stock", "item": "Sugar / Syrups", "qty": "OUT"}, {"action": "add_shopping", "item": "Sugar", "urgency": "urgent"}]
```

Staff says: "Eh let's do a reel of the new matcha latte this Friday, Sarah can film"
Your reply: "Sounds great! Matcha reels always get good engagement. I'll put Sarah down for Friday. 🎬"
```actions
[{"action": "plan_content", "title": "New matcha latte reel", "type": "reel", "date": "2026-08-28", "assigned_to": "Sarah", "notes": "Film the matcha latte prep and pour"}]
```

Staff says: "Done the latte art video already"
Your reply: "Nice one! Marked as done. 📱✅"
```actions
[{"action": "done_content", "title": "latte art video"}]
```

Staff says: "Any ideas for content this week?"
Your reply: [give 2-3 specific ideas based on what's happening at the café — upcoming events, new menu items, seasonal things, trending formats]
```actions
[{"action": "suggest_content"}]
```

CONTENT MANAGEMENT:
- You track a content calendar. When someone says they'll film/photograph/create something, plan it with plan_content.
- When someone says they finished content, mark it done with done_content.
- If someone who has content planned today is chatting, casually check in: "btw you still shooting that [title] today?" — don't nag, just ask once naturally.
- If they say they have no idea what to film, trigger suggest_content to give them ideas.
- When suggesting ideas, be SPECIFIC to this café (use what you know about stock, events, menu) — not generic "post a latte" suggestions.

DATA STORAGE:
- All data you save (stock, shopping list, cleaning logs, events, shifts, action items, custom instructions, staff) is automatically synced to Google Sheets in real-time.
- When you trigger actions like update_stock, add_shopping, log_cleaning, etc., the data is saved locally AND synced to the team's Google Sheet automatically.
- You DO have Google Sheets integration. Never say you don't. The sync happens in the background — just trigger the actions as normal.

OPERATIONS CHECKLIST TRACKING:
- You can track daily opening/6pm/closing checklist completion.
- When staff says they did opening tasks, closing tasks, or 6pm tasks, use the checklist_done action.
- Action: {"action": "checklist_done", "checklist": "opening", "items": ["all"]}
  checklist: "opening", "6pm", or "closing"
  items: ["all"] to mark everything done, or list specific items like ["Cook boba and taro", "Fill up water boiler"]
- If staff says "opening done" or "done opening", mark all opening items done.
- If staff says "I did the boba and filled the water boiler", mark just those specific items.
- You can check the current checklist status from the context data — it shows what's been completed today.

MONTHLY P&L SUMMARY:
- When someone asks for a monthly summary, P&L, or profit and loss report, trigger the monthly_summary action.
- Action: {"action": "monthly_summary", "month": "2026-08"}
  month: YYYY-MM format. If not specified, use current month.
- Examples: "summarize this month", "what's our P&L for July?", "monthly report", "how did we do last month?"

IMPORTANT:
- Only include actions when the message CLEARLY implies something actionable
- DO NOT trigger actions for questions, general chat, opinions, or greetings
- Always include your natural chat response BEFORE the actions block
- If no actions needed, just reply normally with NO actions block
- You know WHO sent each message — reference them by name naturally

CORRECTION DETECTION:
When staff says something is wrong about a previous entry, detect the intent:
- "That's wrong, it should be 6" → correct_stock
- "Salah tu, bukan 12, 6 je" → correct_stock
- "Cancel that receipt" → undo_receipt
- "The nata de coco was 6 lychee and 6 mango, not 12 nata" → correct_stock for each item
- "Eh I entered wrong just now" → ask what needs correcting

MANAGER MINDSET RULES:
- You have memory of recent conversations — up to a month. USE IT ACTIVELY. Connect what someone said today to what happened yesterday. "You mentioned the grinder was making noise on Monday — did it get fixed?"
- Never make up specific data about this café (sales numbers, exact stock counts). If you have the data in context, use it to make decisions.
- For food safety or health questions, always add "verify with KKM/BKKM guidelines" if giving specific temperatures/times.
- Keep it real — don't over-promise or give advice that's impractical for a small café.
- You're part of the team, not an outsider. Say "we" not "you" when talking about the café.
- When responding to a voice note, don't start with "you said..." — just respond naturally to the content.
- When analyzing a photo, describe what you see briefly and give actionable next steps.
- ALWAYS think one step ahead. Don't just acknowledge — anticipate what's needed next.
- If staff seems stressed or overwhelmed, acknowledge it: "Tough day. Let's sort out [priority] first, the rest can wait."
- When you don't have enough info to decide, ask ONE specific question — don't ask 5 things at once.

You will be given: current café data (including older chat summaries and recent messages), and the new message.

RECIPE & PORTION RULES:
- When someone asks for a bingsu recipe (e.g. "how to make matcha bingsu", "matcha recipe", "resepi mango"), ALWAYS ask which batch size they need: 100ml, 1000ml, 2000ml, 3000ml, or 4000ml. Don't dump all sizes — ask first, then give the exact recipe for that size.
- If they already specify a size ("matcha 2000ml recipe"), give it directly — no need to ask.
- If they ask "how much [ingredient]" for a specific flavor, ask which batch size if not clear from context.
- When giving a recipe, JUST GIVE THE RECIPE. Do NOT add stock/restock questions, inventory checks, or "did you restock X?" — they asked for a recipe, not a stock report. Keep it clean.

REPLY CONTEXT:
- When someone replies to a previous message, you receive that original message as context. USE IT to understand what they're referring to.
- Example: Someone replies to "Mango bingsu 2000ml: 1300g Full Cream Milk..." asking "what about strawberry?" → They want the Strawberry recipe at the SAME batch size (2000ml). Give it directly.
- Example: Someone replies to a message about "Matcha bingsu 2000ml" asking "how much milk?" → You know they mean Full Cream Milk for Matcha 2000ml. Give the answer directly, don't re-ask which flavor or size.
- Example: Someone replies to a stock update about milk asking "order more?" → Connect it to the stock level and advise.
- Always connect the reply context to the new message. The person is continuing a conversation thread — understand what they're referring to, including the batch size or flavor from the original message.
"""

# Base prompt (no SOP data) — SOP text is fetched from Google Sheets at
# startup / refresh time via refresh_sop_prompt(), not hardcoded here.
_GEMINI_BASE_PROMPT = _SYSTEM_PROMPT_TEMPLATE.replace("CAFE_NAME_HERE", config.CAFE_NAME)

# SOP knowledge block text, populated by refresh_sop_prompt(). Empty until
# the bot has loaded SOP data from Google Sheets.
_sop_text = ""

SYSTEM_PROMPT = _GEMINI_BASE_PROMPT

# Staff group gets an extra instruction block that blocks financial queries
_STAFF_RESTRICTION = """

IMPORTANT — STAFF GROUP RESTRICTIONS:
You are currently in the STAFF group chat. You MUST follow these rules:
- NEVER share any financial data: expenses, sales numbers, revenue, P&L, profit, loss, cost breakdowns, who paid what, repayment info, or monthly summaries.
- If someone asks about finances, expenses, sales, profit, revenue, costs, P&L, who paid, or any money-related data, politely refuse: "Financial info is only available in the owner group. Check with the boss."
- Do NOT trigger any of these actions: show_expenses, show_whopaid, show_sales, show_pnl, show_staff, monthly_summary.
- You CAN still help with: stock, cleaning, shopping list, events, tasks, content, checklists, schedules, and general café operations.
- Receipts can be submitted here (for logging purchases), but you must NOT reveal expense totals or summaries.
"""

STAFF_SYSTEM_PROMPT = SYSTEM_PROMPT + _STAFF_RESTRICTION


# ─── Condensed prompt for Groq (primary provider) ───────────
# Condensed core prompt; SOP recipes appended below via build_sop_prompt().
# Groq is fast/cheap so we keep its system prompt small to save tokens
# and reduce 413 "request too large" errors.
_GROQ_BASE_PROMPT = f"""You are the AI MANAGER of {config.CAFE_NAME}, a bingsu café in Melaka, Malaysia. You run the business alongside the team in the café's Telegram group — not an assistant, the manager.

Reply rules: Be SHORT and DIRECT. Max 1-2 sentences. No fluff, no motivational add-ons, no unnecessary encouragement. Just answer the question or confirm the action.

PERSONALITY:
- Direct, no-nonsense, warm — like a hands-on owner who works the floor
- SHORT replies only: 2-3 sentences max. Telegram style, not email.
- Be specific ("order 5 cartons of milk from Giant"), never generic
- Say "we", not "you" — you're part of the team
- If something's wrong, say so straight and give the fix
- Never make up data you don't have

MANAGER MODE: You are the manager. When staff reports issues, don't just acknowledge — follow up:
- Ask if tasks were completed ("Did you finish the cleaning?", "Is the fridge restocked?")
- If someone reports a problem, ask for updates later
- Hold staff accountable — if a task was assigned, check if it's done

MALAYSIA LANGUAGE:
- Staff mix English, Bahasa Melayu, Mandarin, and Tamil in one message — totally normal
- ALWAYS reply in ENGLISH unless the message is written ENTIRELY in one other language
- Never correct their mixed language
- Currency is RM (Ringgit Malaysia)

WHAT YOU MANAGE: stock, cleaning, equipment, supplier orders, task assignments, follow-ups, expenses/sales/P&L awareness, content planning, promos/events, troubleshooting.

TROUBLESHOOTING RULE: When staff reports something broken/not working (bulb, machine, equipment), DO NOT immediately add to shopping list. First suggest troubleshooting steps (check if loose, reset, clean, etc). Only add to shopping list if staff confirms the item is actually broken beyond repair and needs replacement.

ACTIONS YOU CAN TRIGGER:
When a message implies something actionable, append a JSON array at the END of your reply, wrapped in ```actions``` fences. Only include actions when clearly actionable — never for questions, chit-chat, or greetings.

Available actions (name — brief format):
- update_stock — {{"action":"update_stock","item":"...","qty":"OK|LOW|OUT|<number>","note":"..."}}
- log_cleaning — {{"action":"log_cleaning","zone":"..."}}
- add_shopping — {{"action":"add_shopping","item":"...","urgency":"normal|urgent"}}
- mark_bought — {{"action":"mark_bought","item":"..."}}
- save_instruction — {{"action":"save_instruction","instruction":"..."}} (admin says "from now on...", "remember...", "always/never...")
- learn_alias — {{"action":"learn_alias","canonical":"...","alias":"..."}}
- add_event — {{"action":"add_event","title":"...","date":"YYYY-MM-DD","details":"..."}}
- stock_count — {{"action":"stock_count","item":"...","count":"...","note":"..."}} (single item only)
- bulk_stock — {{"action":"bulk_stock","checked_by":"...","date":"dd/mm/yy","items":[{{"item":"...","qty":"..."}}]}} (use for 2+ items, physical count overwrites)
- plan_content — {{"action":"plan_content","title":"...","type":"photo|video|reel|story|post","date":"YYYY-MM-DD","assigned_to":"...","notes":"..."}}
- done_content — {{"action":"done_content","title":"..."}}
- suggest_content — {{"action":"suggest_content"}}
- correct_stock — {{"action":"correct_stock","item":"...","qty":0,"note":"..."}} (overwrites a wrong past entry)
- undo_receipt — {{"action":"undo_receipt","supplier":"...","date":"YYYY-MM-DD","items":[{{"name":"...","qty":0}}]}}
- checklist_done — {{"action":"checklist_done","checklist":"opening|6pm|closing","items":["all"] or [...]}}
- monthly_summary — {{"action":"monthly_summary","month":"YYYY-MM"}}
- Reports (no data made up, just trigger): show_today, show_expenses, show_whopaid, show_sales, show_pnl, show_stock, show_lowstock, show_shopping, show_cleaning, show_shifts, show_week, show_tasks, show_staff — each {{"action":"show_x"}}, month optional where relevant.

Use show_whopaid when someone asks "how much did X pay", "siapa bayar", "who paid", "berapa X spent", expenses by person.
Use show_expenses when someone asks "how much we spend", "total expenses", "berapa belanja".

You can include multiple actions in one array. Always give your natural chat reply BEFORE the actions block. If no action is needed, reply with no actions block at all.

You will be given current café data and the new message. Use it to make decisions — don't invent numbers.

RECIPE RULES: When someone asks for a bingsu recipe, ask which batch size (100ml/1000ml/2000ml/3000ml/4000ml) before giving ingredients. If size is already specified or clear from context, give it directly. When giving a recipe, JUST give the recipe — no stock/restock questions.

REPLY CONTEXT: When a message replies to a previous message, that original message is provided as context. Use it — if someone replies to a 2000ml mango recipe asking "what about strawberry?", give the strawberry recipe at the same 2000ml size. Don't re-ask what the context already answers."""

_GROQ_STAFF_SUFFIX = "\nReply rules: Be SHORT and DIRECT. Max 1-2 sentences. No fluff, no motivational add-ons, no unnecessary encouragement. Just answer the question or confirm the action.\nSTAFF GROUP: Never share financial data (expenses, sales, P&L, profit). Refuse politely."

_GROQ_SYSTEM_PROMPT = _GROQ_BASE_PROMPT
_GROQ_STAFF_SYSTEM_PROMPT = _GROQ_SYSTEM_PROMPT + _GROQ_STAFF_SUFFIX


def refresh_sop_prompt(sheets_sync):
    """Fetch current SOP data (recipes, stock minimums, checklists, inspection)
    from Google Sheets and rebuild all four system prompt variants. Call this
    once at bot startup (after the SheetsSync is connected) and any time the
    SOP data in the sheet is expected to have changed.

    If the sheets read fails or returns nothing, the prompts fall back to
    their base (no-SOP) versions rather than raising — the bot should still
    start even if Sheets is briefly unavailable."""
    global SYSTEM_PROMPT, STAFF_SYSTEM_PROMPT, _GROQ_SYSTEM_PROMPT, _GROQ_STAFF_SYSTEM_PROMPT, _sop_text

    try:
        sop_data = sheets_sync.read_sop_from_sheets() if sheets_sync else {}
    except Exception as e:
        logger.error(f"refresh_sop_prompt: failed to read SOP from sheets: {e}")
        sop_data = {}

    try:
        _sop_text = build_sop_prompt(**sop_data) if sop_data else ""
    except Exception as e:
        logger.error(f"refresh_sop_prompt: failed to build SOP prompt: {e}")
        _sop_text = ""

    sop_block = ("\n\n" + _sop_text) if _sop_text else ""

    SYSTEM_PROMPT = _GEMINI_BASE_PROMPT + sop_block
    STAFF_SYSTEM_PROMPT = SYSTEM_PROMPT + _STAFF_RESTRICTION

    _GROQ_SYSTEM_PROMPT = _GROQ_BASE_PROMPT + sop_block
    _GROQ_STAFF_SYSTEM_PROMPT = _GROQ_SYSTEM_PROMPT + _GROQ_STAFF_SUFFIX

    logger.info(f"SOP prompt refreshed from Google Sheets ({len(_sop_text)} chars)")


# ═══════════════════════════════════════════════════════════
#  CONTEXT BUILDER
# ═══════════════════════════════════════════════════════════

def _build_context(is_staff_group: bool = False) -> str:
    """Pull current café state to feed as context.
    If is_staff_group=True, financial data (expenses, sales, P&L, staff list) is excluded."""
    store = get_store()
    parts = []

    # Custom instructions from admin
    custom_instructions = store.get_custom_instructions()
    if custom_instructions:
        instr_lines = [f"  - {ci['instruction']} (set by {ci['added_by']})" for ci in custom_instructions]
        parts.append("CUSTOM INSTRUCTIONS (FOLLOW THESE):\n" + "\n".join(instr_lines))

    # Stock
    stock = store.get_stock()
    if stock:
        stock_lines = []
        for item, info in stock.items():
            stock_lines.append(f"  {item}: {info.get('qty', '?')}")
        parts.append("CURRENT STOCK:\n" + "\n".join(stock_lines))

    low = store.get_low_stock()
    if low:
        parts.append("LOW/OUT ITEMS: " + ", ".join(i for i, _ in low))

    # Proactive: items that have been low for multiple days
    if stock:
        persistent_low = []
        stock_current = store.data.get("stock_current", {})
        last_count = store.data.get("last_full_count", {})
        for item_name, info in stock.items():
            qty_str = str(info.get("qty", "")).strip().upper()
            if qty_str in ("LOW", "OUT", "0"):
                lc = last_count.get(item_name, {})
                if lc:
                    persistent_low.append(f"{item_name} (since {lc.get('date', '?')})")
        if persistent_low:
            parts.append("⚠️ PERSISTENT LOW STOCK (still not restocked):\n  " + "\n  ".join(persistent_low[:10]))

    # Today's cleaning
    cleaning = store.get_cleaning_today()
    if cleaning:
        done = [e["zone"] for e in cleaning]
        parts.append(f"CLEANED TODAY: {', '.join(done)}")
    not_cleaned = [z for z in config.CLEANING_ZONES
                   if z not in {e["zone"] for e in (cleaning or [])}]
    if not_cleaned:
        parts.append(f"NOT CLEANED YET: {', '.join(not_cleaned)}")

    # Shopping list
    shopping = store.get_shopping_list()
    if shopping:
        items = [i["item"] for i in shopping[:10]]
        parts.append(f"SHOPPING LIST: {', '.join(items)}")

    # Content calendar
    content_today = store.get_content_today()
    if content_today:
        ct_lines = [f"  [{c['type']}] {c['title']} — assigned: {c.get('assigned_to', '?')}"
                    for c in content_today]
        parts.append("CONTENT DUE TODAY:\n" + "\n".join(ct_lines))

    content_upcoming = store.get_content_calendar(upcoming_only=True)
    # Show next 5 upcoming (excluding today's already shown)
    today_str = _today().isoformat()
    upcoming_content = [c for c in content_upcoming
                        if c.get("planned_date", "") > today_str
                        and c.get("status") in ("planned", "in_progress")][:5]
    if upcoming_content:
        uc_lines = [f"  {c['planned_date']}: [{c['type']}] {c['title']} — {c.get('assigned_to', '?')}"
                    for c in upcoming_content]
        parts.append("UPCOMING CONTENT:\n" + "\n".join(uc_lines))

    # Recent content posted
    recent_content = store.get_content_log(7)
    if recent_content:
        rc_lines = [f"  {c.get('posted_at', '?')[:10]}: {c['idea']}" for c in recent_content[-3:]]
        parts.append("RECENTLY POSTED:\n" + "\n".join(rc_lines))

    # Events
    events = store.get_events(upcoming_only=True)
    if events:
        evt_lines = [f"  {e['date']}: {e['title']} — {e.get('details', '')}" for e in events[:5]]
        parts.append("UPCOMING EVENTS:\n" + "\n".join(evt_lines))

    # Today's shifts — import helpers from bot module
    try:
        from bot import today_day, DAYS_FULL
        day = today_day()
        shifts = store.get_shifts(day)
        if shifts:
            shift_lines = [f"  {name}: {t['start']}-{t['end']}" for name, t in shifts.items()]
            parts.append(f"TODAY'S SHIFTS ({DAYS_FULL[day]}):\n" + "\n".join(shift_lines))

        hours = store.get_hours() or config.DEFAULT_HOURS
        today_hours = hours.get(day, {})
    except ImportError:
        today_hours = {}

    # Google Business Profile — live info (overrides hardcoded hours)
    google_info = get_cafe_google_info()

    # Only show hardcoded hours if Google data is NOT available
    if not google_info and today_hours:
        parts.append(f"TODAY'S HOURS: {today_hours.get('open', '?')} – {today_hours.get('close', '?')}")
    if google_info:
        g_lines = []
        if google_info.get("address"):
            g_lines.append(f"  Address: {google_info['address']}")
        if google_info.get("hours"):
            g_lines.append("  Hours:\n" + "\n".join(f"    {h}" for h in google_info["hours"]))
        if google_info.get("open_now") is not None:
            g_lines.append(f"  Currently: {'OPEN' if google_info['open_now'] else 'CLOSED'}")
        if google_info.get("rating"):
            g_lines.append(f"  Rating: {google_info['rating']}/5 ({google_info.get('total_reviews', 0)} reviews)")
        if google_info.get("website"):
            g_lines.append(f"  Website: {google_info['website']}")
        if google_info.get("recent_reviews"):
            g_lines.append("  Recent reviews:")
            for rv in google_info["recent_reviews"][:3]:
                g_lines.append(f"    - {rv['author']} ({rv['rating']}★): {rv['text'][:100]}")
        if g_lines:
            parts.append("GOOGLE BUSINESS INFO (LIVE):\n" + "\n".join(g_lines))

    # Operations checklist status for today
    try:
        for cl_type in ["opening", "6pm", "closing"]:
            status = store.get_ops_checklist_status(cl_type)
            total = len(status["items"])
            done = len(status["completed"])
            if done > 0:
                parts.append(f"{cl_type.upper()} CHECKLIST: {done}/{total} done")
                if status["remaining"]:
                    remaining_str = ", ".join(status["remaining"][:5])
                    if len(status["remaining"]) > 5:
                        remaining_str += f" (+{len(status['remaining'])-5} more)"
                    parts.append(f"  Remaining: {remaining_str}")
            else:
                parts.append(f"{cl_type.upper()} CHECKLIST: not started ({total} items)")
    except Exception:
        pass

    # Upcoming holidays
    try:
        from google_integration import get_upcoming_holidays
        upcoming_holidays = get_upcoming_holidays(14)
        if upcoming_holidays:
            h_lines = []
            for h in upcoming_holidays:
                end = f" to {h['end_date']}" if h.get('end_date') else ""
                source = f" ({h.get('source', '')})" if h.get('source') else ""
                h_lines.append(f"  {h['date']}{end}: {h['name']}{source}")
            parts.append("UPCOMING HOLIDAYS (next 14 days):\n" + "\n".join(h_lines))
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"Holiday context error: {e}")

    if not parts:
        return "No café data available yet. Staff should use /stockcheck, /addshift, etc. to set up."

    return "--- CURRENT CAFÉ DATA ---\n" + "\n\n".join(parts) + "\n--- END DATA ---"


def _full_context(user_name: str, user_message: str, reply_context: str = None,
                   chat_id: int = 0, is_staff_group: bool = False) -> str:
    """Combine café data + memory + new message into one context block."""
    cafe_data = _build_context(is_staff_group=is_staff_group)
    memory = get_memory_context(chat_id=chat_id)
    now = _now().strftime("%A, %d %B %Y, %I:%M %p")

    parts = [
        cafe_data,
        memory,
        f"Current time: {now}",
        f"Staff member: {user_name}",
    ]

    if reply_context:
        parts.append(f"[This message is a REPLY to the following message]\n{reply_context}\n[End of replied-to message]")

    parts.append(f"New message: {user_message}")

    return "\n\n".join(parts)


def _groq_context(user_name: str, user_message: str, reply_context: str = None,
                   chat_id: int = 0, is_staff_group: bool = False) -> str:
    """Trimmed context for Groq — no memory/history, shorter café data."""
    store = get_store()
    parts = []

    # Custom instructions (keep, these are short)
    custom_instructions = store.get_custom_instructions()
    if custom_instructions:
        instr_lines = [ci['instruction'] for ci in custom_instructions[:5]]
        parts.append("RULES: " + "; ".join(instr_lines))

    # Stock summary (condensed — only low/out items)
    low = store.get_low_stock()
    if low:
        parts.append("LOW/OUT STOCK: " + ", ".join(i for i, _ in low))

    # Shopping list (brief)
    shopping = store.get_shopping_list()
    if shopping:
        items = [s["item"] for s in shopping[:10]]
        parts.append("SHOPPING LIST: " + ", ".join(items))

    # Today's events
    events = store.get_events()
    if events:
        today_events = [e for e in events if e.get("date", "") == _now().strftime("%Y-%m-%d")]
        if today_events:
            parts.append("TODAY'S EVENTS: " + ", ".join(e.get("title", "") for e in today_events))

    # Google Places info (hours, address)
    google_info = get_cafe_google_info()
    if google_info:
        if google_info.get("hours"):
            parts.append("OPENING HOURS:\n" + "\n".join(f"  {h}" for h in google_info["hours"]))
        if google_info.get("open_now") is not None:
            parts.append(f"Currently: {'OPEN' if google_info['open_now'] else 'CLOSED'}")
        if google_info.get("address"):
            parts.append(f"Address: {google_info['address']}")
        if google_info.get("rating"):
            parts.append(f"Rating: {google_info['rating']}⭐ ({google_info.get('total_reviews', 0)} reviews)")
    else:
        # Fallback to config hours
        hours = store.get_hours() or config.DEFAULT_HOURS
        h_lines = []
        for day in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
            h = hours.get(day, {})
            if h.get("open") == "Closed":
                h_lines.append(f"  {day.title()}: Closed")
            else:
                h_lines.append(f"  {day.title()}: {h.get('open', '?')} – {h.get('close', '?')}")
        parts.append("OPENING HOURS:\n" + "\n".join(h_lines))

    # Upcoming holidays
    try:
        from google_integration import get_upcoming_holidays
        holidays = get_upcoming_holidays(14)
        if holidays:
            h_list = [f"{h['date']}: {h['name']}" for h in holidays[:5]]
            parts.append("UPCOMING HOLIDAYS: " + "; ".join(h_list))
    except Exception:
        pass

    now = _now().strftime("%A, %d %B %Y, %I:%M %p")
    parts.append(f"Time: {now}")
    parts.append(f"Staff: {user_name}")

    if reply_context:
        # Trim reply context to 300 chars
        parts.append(f"[Replying to: {reply_context[:300]}]")

    parts.append(f"Message: {user_message}")

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════
#  💬 TEXT CHAT (with memory)
# ═══════════════════════════════════════════════════════════

def _parse_actions(raw_text: str) -> tuple:
    """
    Split AI response into (chat_reply, actions_list).
    Actions are enclosed in ```actions ... ``` fences at the end.
    Also handles malformed formats (single backticks, no fences, etc.)
    """
    import re

    # Try 1: Standard ```actions\n[...]\n```
    pattern = r'```actions?\s*\n(.*?)\n\s*```'
    match = re.search(pattern, raw_text, re.DOTALL)

    # Try 2: Single backtick `actions [...]`
    if not match:
        pattern2 = r'`actions?\s*([\[\{].*?[\]\}])\s*`'
        match = re.search(pattern2, raw_text, re.DOTALL)

    # Try 3: Just a JSON array/object at the end after the chat text
    if not match:
        pattern3 = r'([\[\{]\s*\{\s*"action"\s*:.*[\]\}])\s*$'
        match = re.search(pattern3, raw_text, re.DOTALL)

    if not match:
        return raw_text.strip(), []

    actions_json = match.group(1).strip()
    chat_reply = raw_text[:match.start()].strip()

    # Clean up any leftover markdown from chat_reply
    chat_reply = re.sub(r'```actions?\s*$', '', chat_reply).strip()
    chat_reply = re.sub(r'`actions?\s*$', '', chat_reply).strip()

    try:
        actions = json.loads(actions_json)
        if isinstance(actions, dict):
            actions = [actions]
        if not isinstance(actions, list):
            actions = []
    except json.JSONDecodeError:
        logger.warning(f"Failed to parse actions JSON: {actions_json[:200]}")
        actions = []

    return chat_reply, actions


async def process_message(user_message: str, user_name: str, reply_context: str = None,
                          chat_id: int = 0, is_staff_group: bool = False) -> tuple:
    """
    Process a chat message through Gemini.
    Returns (chat_reply: str, actions: list[dict]).
    Actions are structured commands the bot should execute (stock updates, etc).
    """
    # Use staff-restricted prompt when in staff group
    groq_sys = _GROQ_STAFF_SYSTEM_PROMPT if is_staff_group else _GROQ_SYSTEM_PROMPT

    # ── Primary: Groq ──
    try:
        prompt = _groq_context(user_name, user_message, reply_context,
                               chat_id=chat_id, is_staff_group=is_staff_group)
        raw = await _groq_text(prompt, system=groq_sys, temperature=0.7, max_tokens=500)
        if raw:
            chat_reply, actions = _parse_actions(raw)
            if chat_reply:
                remember_bot_response(chat_reply, chat_id=chat_id)
            return chat_reply, actions
    except Exception as e:
        logger.warning(f"Groq primary failed: {e}")

    # ── Fallback: Gemini ──
    client = get_client()
    if client is None:
        return None, []

    sys_prompt = STAFF_SYSTEM_PROMPT if is_staff_group else SYSTEM_PROMPT

    try:
        prompt = _full_context(user_name, user_message, reply_context,
                               chat_id=chat_id, is_staff_group=is_staff_group)

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=sys_prompt,
                temperature=0.7,
            ),
        )

        raw = response.text.strip() if response.text else None
        if not raw:
            return None, []

        chat_reply, actions = _parse_actions(raw)

        # Store bot response in memory (without the actions block)
        if chat_reply:
            remember_bot_response(chat_reply, chat_id=chat_id)

        return chat_reply, actions

    except Exception as e2:
        logger.error(f"Gemini fallback also failed: {e2}")
        return None, []


async def ask_ai(user_message: str, user_name: str, reply_context: str = None,
                  chat_id: int = 0, is_staff_group: bool = False) -> Optional[str]:
    """Send a text message to Gemini with full context + memory.
    Simple version — returns text only. Use process_message() for actions."""
    reply, _ = await process_message(user_message, user_name, reply_context,
                                     chat_id=chat_id, is_staff_group=is_staff_group)
    return reply


async def classify_photo(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Ask Gemini to classify the photo type.
    Returns: 'receipt', 'sales_report', 'problem', or 'photo'."""
    # ── Primary: Groq vision ──
    try:
        prompt = (
            "Look at this image carefully. Classify it into EXACTLY one of these categories:\n\n"
            "1. 'receipt' — a purchase receipt, invoice, bill, delivery order, "
            "online shopping checkout/order (Shopee, Lazada, Grab, FoodPanda), "
            "payment confirmation, bank transfer proof, or ANY proof of money spent\n"
            "2. 'sales_report' — a POS daily sales report, sales summary, end-of-day report\n"
            "3. 'problem' — broken/damaged item, equipment issue, maintenance problem\n"
            "4. 'photo' — anything else\n\n"
            "Reply with ONLY one word: receipt, sales_report, problem, or photo"
        )
        result = await _groq_vision(prompt, image_bytes, mime_type, temperature=0.1, max_tokens=20)
        if result:
            result = result.strip().lower()
            if "sales_report" in result or "sales" in result:
                return "sales_report"
            if "receipt" in result:
                return "receipt"
            if "problem" in result:
                return "problem"
            return "photo"
    except Exception as e:
        logger.warning(f"Groq classify_photo failed: {e}")

    # ── Fallback: Gemini ──
    client = get_client()
    if client is None:
        return "photo"

    try:
        prompt = (
            "Look at this image carefully. Classify it into EXACTLY one of these categories:\n\n"
            "1. 'receipt' — a purchase receipt, invoice, bill, delivery order, purchase order, "
            "online shopping checkout/order (Shopee, Lazada, Grab, FoodPanda, Amazon), "
            "payment confirmation, bank transfer proof, e-wallet payment screenshot, "
            "or ANY image showing money was spent on buying something (proof of a PURCHASE/EXPENSE)\n"
            "2. 'sales_report' — a POS daily sales report, daily close-up report, sales summary, cash register report, "
            "end-of-day report showing total sales, revenue breakdown, or transaction summary\n"
            "3. 'problem' — shows a broken/damaged item, equipment issue, maintenance problem, leak, "
            "dirty area, pest issue, or anything that needs fixing\n"
            "4. 'photo' — anything else (food, drinks, décor, selfie, menu, general café photo)\n\n"
            "Reply with ONLY one word: receipt, sales_report, problem, or photo"
        )
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        text_part = types.Part.from_text(text=prompt)

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=types.Content(parts=[image_part, text_part]),
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=20,
            ),
        )

        result = (response.text or "").strip().lower()
        if "sales_report" in result or "sales" in result:
            return "sales_report"
        if "receipt" in result:
            return "receipt"
        if "problem" in result:
            return "problem"
        return "photo"

    except Exception as e2:
        logger.error(f"Gemini classify fallback failed: {e2}")
        return "photo"


async def classify_receipt_reply(user_reply: str, receipt_summary: str) -> dict:
    """Use AI to understand what the user means when replying to a receipt confirmation.

    Returns dict with:
      {"action": "confirm"} — user wants to save as-is
      {"action": "change", "field": "paid_by", "value": "Eric"} — user wants to change something
      {"action": "unclear"} — couldn't understand
    """
    client = get_client()
    if client is None:
        return {"action": "unclear"}

    prompt = (
        "You are a smart café bot. A receipt was scanned and the user is replying to correct or confirm it.\n\n"
        f"Current receipt:\n{receipt_summary}\n\n"
        f"User replied: \"{user_reply}\"\n\n"
        "YOUR JOB: Understand what the user MEANS, like a human colleague would. "
        "They speak casual English, Malay, or mixed (Manglish). They won't use exact field names.\n\n"
        "Think step by step:\n"
        "1. What is the user trying to say?\n"
        "2. Are they confirming, or correcting something?\n"
        "3. If correcting — WHAT exactly are they correcting and to WHAT value?\n\n"
        "Reply with ONLY valid JSON.\n\n"

        "=== CONFIRM (save as-is) ===\n"
        "Any agreement: ok, yes, yep, ya, can, boleh, betul, correct, confirm, lgtm, approved, looks good, save it\n"
        '→ {"action": "confirm"}\n\n'

        "=== CHANGE receipt-level fields ===\n"
        "paid_by, supplier, total, subtotal, date, payment_method, discount, category, notes\n"
        '  "paid by Eric" → {"action": "change", "changes": {"paid_by": "Eric"}}\n'
        '  "total should be 15.90" → {"action": "change", "changes": {"total": 15.90}}\n'
        '  "from Giant" or "shop is Giant" → {"action": "change", "changes": {"supplier": "Giant"}}\n'
        '  Multiple: {"action": "change", "changes": {"paid_by": "Eric", "total": 9.19}}\n\n'

        "=== CHANGE items (qty, price, name, add, remove) ===\n"
        "Use 0-based index matching the Items list order.\n"
        "If user doesn't say which item and there's only 1 item, use index 0.\n"
        "If user mentions an item by name, match it to the correct index.\n\n"

        "UNDERSTAND NATURAL LANGUAGE — these are all real examples:\n"
        '  "the item is 1 bag of ice" → name: "Bag of Ice", qty: 1\n'
        '    → {"action": "change", "changes": {"items": [{"index": 0, "name": "Bag of Ice", "qty": 1}]}}\n'
        '  "quantity is 12 bottles of 1L" → qty: 12, name should include "1L"\n'
        '    → {"action": "change", "changes": {"items": [{"index": 0, "qty": 12, "name": "Whipping Cream 1L"}]}}\n'
        '  "its actually 5 packs" → qty: 5\n'
        '    → {"action": "change", "changes": {"items": [{"index": 0, "qty": 5}]}}\n'
        '  "item is a bag of ice" → rename to "Bag of Ice"\n'
        '    → {"action": "change", "changes": {"items": [{"index": 0, "name": "Bag of Ice"}]}}\n'
        '  "its whipping cream not whip cream" → rename\n'
        '    → {"action": "change", "changes": {"items": [{"index": 0, "name": "Whipping Cream"}]}}\n'
        '  "price is 24 each" → unit price change\n'
        '    → {"action": "change", "changes": {"items": [{"index": 0, "price": 24.00}]}}\n'
        '  "remove the second item" → remove\n'
        '    → {"action": "change", "changes": {"items": [{"index": 1, "action": "remove"}]}}\n'
        '  "add 2 cups of syrup at rm5 each" → add\n'
        '    → {"action": "change", "changes": {"items": [{"action": "add", "name": "Syrup", "qty": 2, "price": 5.00}]}}\n'
        '  "change first item to consumables" → category change\n'
        '    → {"action": "change", "changes": {"items": [{"index": 0, "category": "consumables"}]}}\n'
        '  "pistachio is equipment" → category change by item name\n'
        '    → {"action": "change", "changes": {"items": [{"index": 0, "category": "equipment"}]}}\n\n'

        "KEY RULES:\n"
        "- Extract numbers from natural text: '12 bottles of 1L' → qty=12, '1 bag' → qty=1\n"
        "- 'the item is X' always means rename, even if X contains a number like '1 bag of ice'\n"
        "- If they mention both a new name AND a quantity, include BOTH in the change\n"
        "- qty must be an integer, price must be a number\n"
        "- Valid categories: ingredients, consumables, one-off, equipment, marketing\n"
        "- You can combine item changes with receipt-level changes in one response\n"
        "- When unsure between confirm and change, lean toward understanding it as a change\n\n"

        "If truly cannot understand:\n"
        '→ {"action": "unclear"}\n\n'
        "Reply with ONLY the JSON object, no explanation."
    )

    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=300,
            ),
        )
        result = (response.text or "").strip()
        # Strip markdown code fences if present
        if result.startswith("```"):
            result = result.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        import json as _json
        parsed = _json.loads(result)

        # Post-process: ensure item qty is integer and price is float
        changes = parsed.get("changes", {})
        if "items" in changes and isinstance(changes["items"], list):
            for item_change in changes["items"]:
                if "qty" in item_change:
                    try:
                        import re as _re
                        q = str(item_change["qty"])
                        m = _re.match(r'(\d+)', q)
                        item_change["qty"] = int(m.group(1)) if m else 1
                    except (ValueError, TypeError):
                        item_change["qty"] = 1
                if "price" in item_change:
                    try:
                        item_change["price"] = float(item_change["price"])
                    except (ValueError, TypeError):
                        pass

        return parsed
    except Exception as e:
        logger.error(f"classify_receipt_reply error: {e}")
        return {"action": "unclear"}


async def classify_video(video_bytes: bytes, mime_type: str = "video/mp4") -> str:
    """Ask Gemini to classify a video.
    Returns: 'receipt', 'sales_report', 'problem', or 'video'."""
    client = get_client()
    if client is None:
        return "video"

    try:
        prompt = (
            "Watch this video carefully. Classify it into EXACTLY one of these categories:\n\n"
            "1. 'receipt' — shows a purchase receipt, invoice, bill, delivery order\n"
            "2. 'sales_report' — shows a POS daily sales report, daily close-up report, "
            "sales summary, cash register report, end-of-day report\n"
            "3. 'problem' — shows a broken/damaged item, equipment issue, maintenance problem, "
            "leak, dirty area, pest issue, or anything that needs fixing\n"
            "4. 'video' — anything else (food prep, cafe activity, delivery, general)\n\n"
            "Reply with ONLY one word: receipt, sales_report, problem, or video"
        )
        video_part = types.Part.from_bytes(data=video_bytes, mime_type=mime_type)
        text_part = types.Part.from_text(text=prompt)

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=types.Content(parts=[video_part, text_part]),
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=20,
            ),
        )

        result = (response.text or "").strip().lower()
        if "sales_report" in result or "sales" in result:
            return "sales_report"
        if "receipt" in result:
            return "receipt"
        if "problem" in result:
            return "problem"
        return "video"

    except Exception as e:
        # Groq doesn't support video — no fallback available
        logger.error(f"Video classification error (no fallback — Groq has no video support): {e}")
        return "video"


async def handle_video(
    video_bytes: bytes,
    user_name: str,
    caption: str = "",
    mime_type: str = "video/mp4",
    chat_id: int = 0,
    message_id: int = 0,
) -> Optional[str]:
    """
    Analyze a video sent to the group — equipment issues, food prep, etc.
    Returns a text response.
    """
    client = get_client()
    if client is None:
        return None

    try:
        cafe_data = _build_context()
        memory = get_memory_context(chat_id)
        now = _now().strftime("%A, %d %B %Y, %I:%M %p")

        caption_context = f"They included this caption: '{caption}'" if caption else "No caption was included."

        text_context = (
            f"{cafe_data}\n\n"
            f"{memory}\n\n"
            f"Current time: {now}\n"
            f"Staff member: {user_name}\n"
            f"They just sent a video to the group. {caption_context}\n\n"
            f"Watch the video and respond helpfully:\n"
            f"- If it shows an equipment issue: identify the problem and suggest a fix\n"
            f"- If it shows a process/preparation: comment on technique and suggest improvements\n"
            f"- If it shows a cleanliness issue: acknowledge and suggest action\n"
            f"- If it shows a delivery: note what was received\n"
            f"- If it shows food/drink being made: comment on presentation or technique\n"
            f"- If there's text/screens visible, read and understand them\n"
            f"- If unclear: describe what you see and ask what they need\n"
            f"Keep it short (2-3 sentences max). Reply in English unless the message is entirely in another language."
        )

        video_part = types.Part.from_bytes(data=video_bytes, mime_type=mime_type)
        text_part = types.Part.from_text(text=text_context)

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=types.Content(parts=[video_part, text_part]),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.5,
                max_output_tokens=300,
            ),
        )

        result = response.text.strip() if response.text else None

        if result:
            video_desc = caption if caption else result[:80]
            remember(user_name, f"[Video: {video_desc}]", "video", chat_id, message_id)

        return result

    except Exception as e:
        # Groq doesn't support video — no fallback available
        logger.error(f"Video analysis error (no fallback — Groq has no video support): {e}")
        return None


async def process_sales_report(
    image_bytes: bytes,
    user_name: str,
    caption: str = "",
    mime_type: str = "image/jpeg",
) -> Optional[dict]:
    """
    OCR a POS daily sales report image.
    Returns structured data:
    {
        "date": "YYYY-MM-DD",
        "payment_breakdown": [{"method": "Credit Card", "amount": 143.30}, ...],
        "total_sales": 261.70,
        "bill_count": 6,
        "total_pax": 6,
        "total_discount": 0.00,
        "total_void": 0.00,
        "total_refund": 0.00,
        "other_charge": 0.00,
        "user": "Kendrick",
        "notes": "...",
        "raw_text": "full text from report",
    }
    """
    # ── Primary: Groq vision ──
    try:
        prompt = (
            f"You are analyzing a POS daily sales / close-up report image from a café.\n"
            f"Staff member: {user_name}\n"
            f"{'Caption: ' + caption if caption else 'No caption.'}\n\n"
            "Extract ALL information from this sales report. "
            "It may be in any language.\n\n"
            "Reply with ONLY a JSON object with these fields:\n"
            '{\n'
            '  "date": "YYYY-MM-DD",\n'
            '  "payment_breakdown": [{"method": "Credit Card", "amount": 143.30}],\n'
            '  "total_sales": 261.70,\n'
            '  "bill_count": 6,\n'
            '  "total_pax": 6,\n'
            '  "total_discount": 0.00,\n'
            '  "total_void": 0.00,\n'
            '  "total_refund": 0.00,\n'
            '  "other_charge": 0.00,\n'
            '  "user": "cashier name",\n'
            '  "notes": "extra info",\n'
            '  "raw_text": "all readable text"\n'
            '}\n'
            "Amounts in RM. If you can't read a value, use null."
        )
        result = await _groq_vision(prompt, image_bytes, mime_type, temperature=0.1, max_tokens=800)
        if result:
            result = result.replace("```json", "").replace("```", "").strip()
            data = json.loads(result)
            if isinstance(data, dict):
                if "payment_breakdown" not in data:
                    data["payment_breakdown"] = []
                if "total_sales" not in data:
                    data["total_sales"] = 0
                return data
    except Exception as e:
        logger.warning(f"Groq sales report processing failed: {e}")

    # ── Fallback: Gemini ──
    client = get_client()
    if client is None:
        return None

    try:
        prompt = (
            f"You are analyzing a POS daily sales / close-up report image from a café.\n"
            f"Staff member: {user_name}\n"
            f"{'Caption: ' + caption if caption else 'No caption.'}\n\n"
            "Extract ALL information from this sales report. "
            "It may be in any language.\n\n"
            "Reply with ONLY a JSON object with these fields:\n"
            '{\n'
            '  "date": "YYYY-MM-DD" (from report, or today if unclear),\n'
            '  "payment_breakdown": [{"method": "Credit Card", "amount": 143.30}, {"method": "DuitNow", "amount": 118.40}],\n'
            '  "total_sales": 261.70,\n'
            '  "bill_count": 6,\n'
            '  "total_pax": 6,\n'
            '  "total_discount": 0.00,\n'
            '  "total_void": 0.00,\n'
            '  "total_refund": 0.00,\n'
            '  "other_charge": 0.00,\n'
            '  "user": "cashier or user name from report",\n'
            '  "notes": "any extra info",\n'
            '  "raw_text": "all readable text from the image"\n'
            '}\n\n'
            "Include ALL payment methods shown (Cash, Credit Card, DuitNow, TnG, GrabPay, etc.).\n"
            "If you can't read a value, use null. Amounts in RM (Ringgit)."
        )

        image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        text_part = types.Part.from_text(text=prompt)

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=types.Content(parts=[image_part, text_part]),
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=800,
            ),
        )

        result = response.text.strip() if response.text else None
        if not result:
            return None

        result = result.replace("```json", "").replace("```", "").strip()
        data = json.loads(result)

        if not isinstance(data, dict):
            return None
        if "payment_breakdown" not in data:
            data["payment_breakdown"] = []
        if "total_sales" not in data:
            data["total_sales"] = 0

        return data

    except Exception as e2:
        logger.error(f"Gemini sales report fallback also failed: {e2}")
        return None


def validate_stock_count(item: str, reported_count: str) -> Optional[str]:
    """
    Compare a reported stock count against what we expect.
    Returns a warning message if it looks off, or None if OK.
    """
    store = get_store()
    stock = store.get_stock()

    # Get last known level
    last_info = stock.get(item, {})
    last_qty = last_info.get("qty", "")

    # If we don't have previous data, can't validate
    if not last_qty:
        return None

    # Try to extract numbers for comparison
    import re

    def _extract_number(text):
        nums = re.findall(r'(\d+(?:\.\d+)?)', str(text))
        return float(nums[0]) if nums else None

    reported_num = _extract_number(reported_count)
    last_num = _extract_number(last_qty)

    if reported_num is None or last_num is None:
        return None

    # Check for big discrepancies (>50% change without a receipt to explain it)
    if last_num > 0:
        change_pct = abs(reported_num - last_num) / last_num * 100
        if change_pct > 50 and abs(reported_num - last_num) > 2:
            direction = "more" if reported_num > last_num else "less"
            return (
                f"⚠️ That's quite different from last count — "
                f"{item} was {last_qty}, now {reported_count} "
                f"({direction}, {change_pct:.0f}% change). "
                f"Can you double-check the count?"
            )

    return None


async def generate_content_suggestions(user_name: str) -> Optional[str]:
    """
    Generate photo/video content ideas based on current café context —
    what's happening today, upcoming events, stock, season, trending formats.
    """
    # ── Primary: Groq ──
    try:
        cafe_data = _build_context()
        now = _now()
        prompt = (
            f"You are the content strategist for a café in Melaka, Malaysia.\n\n"
            f"{cafe_data}\n\n"
            f"Today is {now.strftime('%A, %d %B %Y')}.\n\n"
            f"Generate 3-4 SPECIFIC photo/video content ideas for Instagram/TikTok.\n"
            f"For each: what to film, format (photo/reel/story), best time, caption, 3-5 hashtags.\n"
            f"Keep it practical — things a barista with a phone can shoot today."
        )
        result = await _groq_text(prompt, temperature=0.9, max_tokens=600)
        if result:
            return result
    except Exception as e:
        logger.warning(f"Groq content suggestion failed: {e}")

    # ── Fallback: Gemini ──
    client = get_client()
    if client is None:
        return None

    try:
        cafe_data = _build_context()
        now = _now()
        day_of_week = now.strftime("%A")
        month = now.strftime("%B")

        prompt = f"""You are the content strategist for a café in Melaka, Malaysia.

{cafe_data}

Today is {day_of_week}, {now.strftime('%d %B %Y')}.

Generate 3-4 SPECIFIC photo/video content ideas for the café's social media (Instagram/TikTok).
For each idea:
- What to film/photograph (be specific — not "latte art" but "Close-up slow-mo of rosetta pour into a clear glass cup")
- Format: photo, reel (15-30s), story, or carousel
- Best time to shoot today
- Caption suggestion (short, catchy)
- Relevant hashtags (3-5)

Consider:
- What's in season / trending in {month}
- Any upcoming events or promotions we have
- What stock items are fresh / new
- Malaysian café culture & local vibes
- Time of day for best lighting
- What performs well on café socials (behind-the-scenes, process shots, aesthetic drinks, cozy ambiance)

Keep it practical — things a barista with a phone can shoot today.
Reply in the sender's language style (English with casual Malaysian flair is fine).
"""

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.9,
                max_output_tokens=600,
            ),
        )

        return response.text.strip() if response.text else None

    except Exception as e2:
        logger.error(f"Gemini content suggestion fallback also failed: {e2}")
        return None


# ═══════════════════════════════════════════════════════════
#  🎤 VOICE NOTE UNDERSTANDING
# ═══════════════════════════════════════════════════════════

async def handle_voice(audio_bytes: bytes, user_name: str, mime_type: str = "audio/ogg",
                       chat_id: int = 0, message_id: int = 0) -> Optional[str]:
    """
    Process a voice note:
    1. Send audio to Gemini for transcription + understanding
    2. Respond naturally (not just transcribe)
    """
    client = get_client()
    if client is None:
        return None

    try:
        cafe_data = _build_context()
        memory = get_memory_context(chat_id)
        now = _now().strftime("%A, %d %B %Y, %I:%M %p")

        text_context = (
            f"{cafe_data}\n\n"
            f"{memory}\n\n"
            f"Current time: {now}\n"
            f"Staff member: {user_name}\n"
            f"They just sent a voice note. Listen to it, understand what they're saying, "
            f"and respond naturally. They may speak in English, Bahasa Melayu, Mandarin, Tamil, "
            f"or MIX languages mid-sentence — this is normal in Malaysia. "
            f"If they're reporting something (like cleaning done, stock issue, etc.), acknowledge it. "
            f"If asking a question, answer it. "
            f"Also briefly note what they said in brackets at the start so the group can "
            f"read it without listening — transcribe in the ORIGINAL language(s), like: "
            f"[Voice: eh the milk habis already, 要买 more from supplier]\n"
            f"Then respond in the same language style they used."
        )

        # Build multimodal content: audio + text
        audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
        text_part = types.Part.from_text(text=text_context)

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=types.Content(parts=[audio_part, text_part]),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.5,
                max_output_tokens=400,
            ),
        )

        result = response.text.strip() if response.text else None

        if result:
            # Extract transcription for memory (between brackets if present)
            import re
            voice_match = re.search(r'\[Voice:(.+?)\]', result)
            voice_text = voice_match.group(1).strip() if voice_match else result[:100]
            remember(user_name, voice_text, "voice", chat_id, message_id)
            remember_bot_response(result, chat_id)

        return result

    except Exception as e:
        # Groq doesn't support audio input — no fallback available
        logger.error(f"Gemini voice error (no fallback — Groq has no audio support): {e}")
        return None


# ═══════════════════════════════════════════════════════════
#  📷 PHOTO UNDERSTANDING
# ═══════════════════════════════════════════════════════════

async def handle_photo(
    image_bytes: bytes,
    user_name: str,
    caption: str = "",
    mime_type: str = "image/jpeg",
    chat_id: int = 0,
    message_id: int = 0,
) -> Optional[str]:
    """
    Process a photo sent to the group:
    - Analyze what's in it (equipment, food, cleanliness, receipt, etc.)
    - Respond with practical advice or acknowledgment
    """
    # ── Primary: Groq vision ──
    try:
        caption_context = f"They included this caption: '{caption}'" if caption else "No caption was included."
        prompt = (
            f"Current time: {_now().strftime('%A, %d %B %Y, %I:%M %p')}\n"
            f"Staff member: {user_name}\n"
            f"They just sent a photo. {caption_context}\n\n"
            f"Analyze the photo and respond helpfully (2-3 sentences max).\n"
            f"If equipment issue: identify and suggest fix. If cleanliness: suggest action.\n"
            f"If delivery/receipt: note what was received. If food/drink: comment on quality."
        )
        result = await _groq_vision(prompt, image_bytes, mime_type, system=_GROQ_SYSTEM_PROMPT,
                                    temperature=0.5, max_tokens=300)
        if result:
            photo_desc = caption if caption else result[:80]
            remember(user_name, f"[Photo: {photo_desc}]", "photo", chat_id, message_id)
            remember_bot_response(result, chat_id)
            return result
    except Exception as e:
        logger.warning(f"Groq photo analysis failed: {e}")

    # ── Fallback: Gemini ──
    client = get_client()
    if client is None:
        return None

    try:
        cafe_data = _build_context()
        memory = get_memory_context(chat_id)
        now = _now().strftime("%A, %d %B %Y, %I:%M %p")

        caption_context = f"They included this caption: '{caption}'" if caption else "No caption was included."

        text_context = (
            f"{cafe_data}\n\n"
            f"{memory}\n\n"
            f"Current time: {now}\n"
            f"Staff member: {user_name}\n"
            f"They just sent a photo to the group. {caption_context}\n\n"
            f"Analyze the photo and respond helpfully:\n"
            f"- If it's an equipment issue: identify the problem and suggest a fix\n"
            f"- If it's a cleanliness issue: acknowledge and suggest action\n"
            f"- If it's a delivery/receipt: note what was received\n"
            f"- If it's food/drink: comment on presentation or quality\n"
            f"- If it's décor/setup: give feedback on the look\n"
            f"- If there's text in the image in ANY language (Chinese, Malay, Tamil, English), read and understand it\n"
            f"- If unclear: describe what you see and ask what they need\n"
            f"Respond in the same language style the sender uses. Keep it short (2-3 sentences max)."
        )

        # Build multimodal content: image + text
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        text_part = types.Part.from_text(text=text_context)

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=types.Content(parts=[image_part, text_part]),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.5,
                max_output_tokens=300,
            ),
        )

        result = response.text.strip() if response.text else None

        if result:
            photo_desc = caption if caption else result[:80]
            remember(user_name, f"[Photo: {photo_desc}]", "photo", chat_id, message_id)
            remember_bot_response(result, chat_id)

        return result

    except Exception as e2:
        logger.error(f"Gemini photo fallback also failed: {e2}")
        return None


# ═══════════════════════════════════════════════════════════
#  📋 ACTION ITEM EXTRACTION (Chase-up System)
# ═══════════════════════════════════════════════════════════

_ACTION_PATTERNS = [
    # English
    "need to", "needs to", "have to", "has to", "must", "should",
    "please", "can you", "can someone", "who can", "someone",
    "buy", "order", "fix", "repair", "call", "check", "clean",
    "restock", "replace", "contact", "arrange", "prepare", "set up",
    "remind", "tell", "ask",
    # Malay
    "kena", "perlu", "tolong", "boleh", "minta", "sila",
    "beli", "cuci", "betulkan", "hubungi", "sediakan",
    # Mandarin keywords (romanised)
    "yao mai", "要买", "要做", "帮忙",
]


def extract_action_items(text: str, user_name: str) -> list:
    """
    Extract action items from a message using pattern matching.
    Returns list of dicts: [{task, assigned_to, urgency}]
    """
    if not text or len(text) < 10:
        return []

    lower = text.lower()
    found = False
    for pattern in _ACTION_PATTERNS:
        if pattern in lower:
            found = True
            break
    if not found:
        return []

    # Use Gemini to extract structured action items (if available)
    # Fallback: treat the whole message as one action item
    return [{
        "task": text[:200],
        "assigned_to": user_name,  # default: whoever mentioned it
        "urgency": "urgent" if any(w in lower for w in ["urgent", "asap", "now", "segera", "cepat"]) else "normal",
    }]


async def extract_action_items_ai(text: str, user_name: str) -> list:
    """Use Gemini to intelligently extract action items from a message."""
    # Only call AI for messages that look like they have action items
    if not extract_action_items(text, user_name):
        return []

    # ── Primary: Groq ──
    try:
        prompt = (
            "Extract action items from this cafe group chat message. "
            f"Sender: {user_name}\n"
            f'Message: "{text}"\n\n'
            "Reply with ONLY a JSON array:\n"
            '[{"task": "what to do", "assigned_to": "who", "urgency": "normal"}]\n'
            "If no action items, reply with []"
        )
        result = await _groq_text(prompt, temperature=0.1, max_tokens=200)
        if result:
            result = result.replace("```json", "").replace("```", "").strip()
            items = json.loads(result)
            if isinstance(items, list):
                return items
    except Exception as e:
        logger.warning(f"Groq action extraction failed: {e}")

    # ── Fallback: Gemini ──
    client = get_client()
    if client is None:
        return extract_action_items(text, user_name)

    try:
        prompt = (
            "Extract action items from this cafe group chat message. "
            f"Sender: {user_name}\n"
            f'Message: "{text}"\n\n'
            "Reply with ONLY a JSON array of action items. Each item has:\n"
            '- "task": what needs to be done (short, clear)\n'
            '- "assigned_to": who should do it (use sender name if volunteering, "anyone" if unassigned)\n'
            '- "urgency": "urgent" or "normal"\n\n'
            "If no clear action items, reply with []\n"
            'Example: [{"task": "buy more milk", "assigned_to": "anyone", "urgency": "normal"}]'
        )

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=200,
            ),
        )

        result = response.text.strip() if response.text else "[]"
        # Clean up markdown code blocks if present
        result = result.replace("```json", "").replace("```", "").strip()
        items = json.loads(result)
        if isinstance(items, list):
            return items
        return []

    except Exception as e2:
        logger.error(f"Gemini action extraction fallback also failed: {e2}")
        return extract_action_items(text, user_name)


async def generate_chaseup_message(pending_items: list) -> str:
    """Generate a natural chase-up reminder for pending action items."""
    if not pending_items:
        return ""

    items_text = "\n".join(
        f"- {i.get('task', '?')} (assigned: {i.get('assigned_to', '?')}, "
        f"since: {i.get('created_at', '?')[:16]}, chased {i.get('chase_count', 0)}x)"
        for i in pending_items
    )

    # ── Primary: Groq ──
    try:
        prompt = (
            f"Generate a SHORT, friendly chase-up message for these pending café tasks:\n\n"
            f"{items_text}\n\n"
            f"Keep it casual, Malaysian style. Tag who's responsible. "
            f"End with: reply /taskdone <number> when settled. Max 5 lines."
        )
        result = await _groq_text(prompt, system=_GROQ_SYSTEM_PROMPT, temperature=0.7, max_tokens=200)
        if result:
            return result
    except Exception as e:
        logger.warning(f"Groq chaseup message failed: {e}")

    # ── Fallback: Gemini ──
    client = get_client()

    if client is None:
        # Fallback: simple template
        lines = ["⏰ *Pending tasks — need follow up:*\n"]
        for idx, item in enumerate(pending_items):
            who = item.get("assigned_to", "?")
            task = item.get("task", "?")
            lines.append(f"{idx+1}. {task} → {who}")
        lines.append("\nDone? Reply: /taskdone <number>")
        return "\n".join(lines)

    try:
        prompt = (
            f"You're the café manager bot. Generate a SHORT, friendly chase-up message "
            f"(like a shift lead checking in) for these pending tasks:\n\n"
            f"{items_text}\n\n"
            f"Keep it casual and Malaysian style. Tag who's responsible. "
            f"End with: reply /taskdone <number> when settled.\n"
            f"Max 5 lines."
        )

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.7,
                max_output_tokens=200,
            ),
        )
        return response.text.strip() if response.text else ""

    except Exception as e2:
        logger.error(f"Gemini chaseup fallback also failed: {e2}")
        return ""


# ═══════════════════════════════════════════════════════════
#  SPECIALIZED AI FUNCTIONS
# ═══════════════════════════════════════════════════════════

async def get_content_idea(topic: str = "") -> Optional[str]:
    """Ask AI for a specific content idea."""
    # ── Primary: Groq ──
    try:
        prompt = (
            f"Give me ONE specific social media content idea for {config.CAFE_NAME}. "
            f"{'Topic: ' + topic + '. ' if topic else ''}"
            f"Include: what to post, platform, caption, best time. Under 4 sentences."
        )
        result = await _groq_text(prompt, system=_GROQ_SYSTEM_PROMPT, temperature=0.9, max_tokens=200)
        if result:
            return result
    except Exception as e:
        logger.warning(f"Groq content idea failed: {e}")

    # ── Fallback: Gemini ──
    client = get_client()
    if client is None:
        return None

    try:
        prompt = (
            f"Give me ONE specific, actionable social media content idea for {config.CAFE_NAME}. "
            f"{'Topic: ' + topic + '. ' if topic else ''}"
            f"Include: what to post, what platform, caption suggestion, best time to post. "
            f"Keep it under 4 sentences. Be specific, not generic."
        )

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.9,
                max_output_tokens=200,
            ),
        )

        return response.text.strip() if response.text else None

    except Exception as e2:
        logger.error(f"Gemini content idea fallback also failed: {e2}")
        return None


async def analyze_stock_and_suggest() -> Optional[str]:
    """AI analyzes stock levels and suggests actions."""
    # ── Primary: Groq ──
    try:
        context = _build_context()
        prompt = (
            f"{context}\n\n"
            f"Based on stock levels above, give:\n"
            f"1. What needs buying URGENTLY (one line)\n"
            f"2. Items to reorder soon (one line)\n"
            f"3. One inventory management tip (one line)"
        )
        result = await _groq_text(prompt, system=_GROQ_SYSTEM_PROMPT, temperature=0.3, max_tokens=200)
        if result:
            return result
    except Exception as e:
        logger.warning(f"Groq stock analysis failed: {e}")

    # ── Fallback: Gemini ──
    client = get_client()
    if client is None:
        return None

    try:
        context = _build_context()
        prompt = (
            f"{context}\n\n"
            f"Based on the current stock levels and shopping list above, give me:\n"
            f"1. What needs buying URGENTLY (one line)\n"
            f"2. Any items we should reorder soon before running out (one line)\n"
            f"3. One suggestion to improve our inventory management (one line)\n"
            f"Keep it short and practical."
        )

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.3,
                max_output_tokens=200,
            ),
        )

        return response.text.strip() if response.text else None

    except Exception as e2:
        logger.error(f"Gemini stock analysis fallback also failed: {e2}")
        return None


async def suggest_for_event(event_title: str, event_date: str) -> Optional[str]:
    """AI suggests preparation steps for an event."""
    # ── Primary: Groq ──
    try:
        context = _build_context()
        prompt = (
            f"{context}\n\n"
            f"We have event: '{event_title}' on {event_date}.\n"
            f"Give a quick prep checklist (5 items max) for a café. One line each."
        )
        result = await _groq_text(prompt, system=_GROQ_SYSTEM_PROMPT, temperature=0.5, max_tokens=250)
        if result:
            return result
    except Exception as e:
        logger.warning(f"Groq event suggestion failed: {e}")

    # ── Fallback: Gemini ──
    client = get_client()
    if client is None:
        return None

    try:
        context = _build_context()
        prompt = (
            f"{context}\n\n"
            f"We have this event coming up: '{event_title}' on {event_date}.\n"
            f"Give me a quick prep checklist (5 items max) — things we need to buy, "
            f"set up, or prepare. Be specific to a café setting. Keep each item to one line."
        )

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.5,
                max_output_tokens=250,
            ),
        )

        return response.text.strip() if response.text else None

    except Exception as e2:
        logger.error(f"Gemini event suggestion fallback also failed: {e2}")
        return None


# ═══════════════════════════════════════════════════════════
#  🔊 TEXT-TO-SPEECH (Voice Note Replies)
# ═══════════════════════════════════════════════════════════
#
# Uses edge-tts (Microsoft Edge TTS) — completely free, no API key needed.
# Supports: English (MY), Bahasa Melayu, Mandarin, Tamil
#
# Available Malaysian voices:
#   en-MY-YasminNeural (female), en-MY-OsmanNeural (male)
#   ms-MY-YasminNeural (female), ms-MY-OsmanNeural (male)
#   zh-CN-XiaoxiaoNeural (female), zh-CN-YunxiNeural (male)
#   ta-MY-KaniNeural (female), ta-MY-SuryaNeural (male)

TTS_DEFAULT_VOICE = "en-MY-YasminNeural"
TTS_VOICES = {
    "en": "en-MY-YasminNeural",
    "ms": "ms-MY-YasminNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "ta": "ta-MY-KaniNeural",
}


def _detect_language(text: str) -> str:
    """Simple language detection for TTS voice selection."""
    # Check for Chinese characters
    for ch in text:
        if '一' <= ch <= '鿿':
            return "zh"
    # Check for Tamil characters
    for ch in text:
        if '஀' <= ch <= '௿':
            return "ta"
    # Check for Malay keywords
    malay_words = {"dan", "yang", "ini", "itu", "ada", "tak", "sudah", "belum",
                   "nak", "boleh", "kena", "dengan", "untuk", "dari", "saya",
                   "kami", "mereka", "buat", "perlu", "tolong", "terima kasih"}
    words = set(text.lower().split())
    malay_count = len(words & malay_words)
    if malay_count >= 2:
        return "ms"
    return "en"


async def text_to_speech(text: str) -> Optional[bytes]:
    """Convert text to speech audio bytes (OGG format for Telegram voice notes)."""
    if not HAS_EDGE_TTS:
        logger.warning("edge-tts not installed — voice replies disabled")
        return None

    try:
        import tempfile
        import subprocess

        # Clean text for TTS (remove markdown, brackets, etc.)
        import re
        clean = re.sub(r'\[Voice:.*?\]', '', text)  # Remove [Voice: ...] prefix
        clean = re.sub(r'[*_`#]', '', clean)  # Remove markdown
        clean = clean.strip()
        if not clean:
            return None

        # Select voice based on detected language
        lang = _detect_language(clean)
        voice = TTS_VOICES.get(lang, TTS_DEFAULT_VOICE)

        # Generate MP3 with edge-tts
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as mp3_file:
            mp3_path = mp3_file.name

        communicate = edge_tts.Communicate(clean, voice)
        await communicate.save(mp3_path)

        # Convert MP3 to OGG (Telegram voice note format) using ffmpeg if available
        ogg_path = mp3_path.replace(".mp3", ".ogg")
        try:
            proc = subprocess.run(
                ["ffmpeg", "-i", mp3_path, "-c:a", "libopus", "-b:a", "48k",
                 "-y", ogg_path],
                capture_output=True, timeout=15,
            )
            if proc.returncode == 0:
                with open(ogg_path, "rb") as f:
                    audio_bytes = f.read()
            else:
                # ffmpeg failed — send MP3 as-is (Telegram can handle it)
                with open(mp3_path, "rb") as f:
                    audio_bytes = f.read()
        except FileNotFoundError:
            # No ffmpeg — send MP3
            with open(mp3_path, "rb") as f:
                audio_bytes = f.read()

        # Cleanup temp files
        import os
        os.unlink(mp3_path)
        if os.path.exists(ogg_path):
            os.unlink(ogg_path)

        return audio_bytes

    except Exception as e:
        logger.error(f"TTS error: {e}")
        return None


# ═══════════════════════════════════════════════════════════
#  🧾 RECEIPT / INVOICE OCR
# ═══════════════════════════════════════════════════════════


def _fix_item_prices(data: dict):
    """Fix AI returning line totals instead of unit prices.

    If qty > 1 and item_price × qty is much larger than receipt total,
    the AI likely returned the line total as price. Divide by qty.
    Also catches: sum of (price × qty) far exceeds total → price is line total.
    """
    items = data.get("items", [])
    receipt_total = float(data.get("total") or data.get("subtotal") or 0)
    if not items or receipt_total <= 0:
        return

    # Calculate what the total would be with current prices
    computed_total = sum(
        float(i.get("price") or 0) * int(i.get("qty") or 1)
        for i in items
    )

    if computed_total <= 0:
        return

    # If computed total is roughly double or more of the receipt total,
    # prices are likely already line totals — divide each by its qty
    if computed_total > receipt_total * 1.5:
        # Check: does sum of prices (without multiplying by qty) match better?
        sum_prices_raw = sum(float(i.get("price") or 0) for i in items)
        if abs(sum_prices_raw - receipt_total) < abs(computed_total - receipt_total):
            # Yes — prices are line totals, divide by qty
            for item in items:
                qty = int(item.get("qty") or 1)
                price = float(item.get("price") or 0)
                if qty > 1 and price > 0:
                    item["price"] = round(price / qty, 2)
            logger.info(f"Fixed item prices: were line totals, divided by qty "
                        f"(computed {computed_total:.2f} vs receipt {receipt_total:.2f})")


async def process_receipt(
    image_bytes: bytes,
    user_name: str,
    caption: str = "",
    mime_type: str = "image/jpeg",
) -> Optional[dict]:
    """
    OCR a receipt/invoice image (including handwritten).
    Returns structured data:
    {
        "type": "receipt" | "invoice",
        "supplier": "...",
        "date": "YYYY-MM-DD",
        "items": [{"name": "...", "qty": 1, "price": 10.50}],
        "subtotal": 100.00,
        "tax": 6.00,
        "total": 106.00,
        "payment_method": "cash" | "card" | "transfer",
        "notes": "...",
        "raw_text": "full text from receipt",
    }
    """
    today_str = _today().isoformat()
    current_year = _today().year

    receipt_prompt = (
        f"You are a smart receipt reader for a café in Melaka, Malaysia.\n"
        f"Today's date: {today_str}. Current year: {current_year}.\n"
        f"Person who sent this: {user_name}\n"
        f"{'Caption: ' + caption if caption else 'No caption.'}\n\n"

        "READ and UNDERSTAND this receipt/invoice image. "
        "It may be printed OR handwritten, in any language (Malay, English, Chinese).\n\n"

        "IMPORTANT — Think like a human reading this receipt:\n\n"

        "QUANTITIES — Understand what the receipt actually means:\n"
        '  - "12x1L" means 12 bottles of 1L each → qty: 12, name should include "1L"\n'
        '  - "3 x Milk" means 3 units of milk → qty: 3\n'
        '  - "Egg 30s" or "Egg (30)" means 30 eggs → qty: 30\n'
        '  - "2 carton" or "2ctn" means qty: 2\n'
        '  - If you see a multiplier pattern like NxSIZE (e.g. 5x500ml, 10x2L), N is the quantity\n'
        '  - "qty" must ALWAYS be a plain integer number, never a string with units\n\n'

        "PRICES & TOTALS — Use the FINAL amount paid:\n"
        '  - "total" = the FINAL amount actually paid (after ALL discounts, vouchers, rounding)\n'
        '  - "subtotal" = sum of items BEFORE discounts\n'
        '  - "discount" = total discount/voucher amount (0 if none)\n'
        '  - If receipt shows both a subtotal and a lower total, the lower number is "total"\n'
        '  - Example: subtotal RM28.80, voucher -RM2.88, total paid RM25.92\n'
        '    → subtotal: 28.80, discount: 2.88, total: 25.92\n\n'

        "DATES — Get the year right:\n"
        f'  - If the receipt shows only day/month (e.g. "16/6"), assume year {current_year}\n'
        f'  - If no date at all, use today: {today_str}\n'
        '  - Format as YYYY-MM-DD always\n\n'

        "PAID BY:\n"
        f'  - If the caption says who paid (e.g. "paid by Ali"), use that name\n'
        f'  - Otherwise set paid_by to "{user_name}"\n'
        '  - NEVER use generic words like "Staff member", "staff", "customer", or "unknown"\n\n'

        # NOTE: these category keys must match config.ITEM_CATEGORIES
        "CATEGORIES for each item:\n"
        '  - "ingredients" = food/drink ingredients for recipes (milk, sugar, flour, syrup, chicken, ice)\n'
        '  - "consumables" = items used up regularly (tissues, cups, straws, soap, garbage bags, cleaning supplies)\n'
        '  - "one-off" = bought once/rarely (decorations, one-time purchases)\n'
        '  - "equipment" = machines, tools, furniture, appliances\n'
        '  - "marketing" = signage, ads, merch, flyers, promo materials\n\n'

        "ITEM NAMES — Be specific: 'Low Fat Milk 1L' not just 'Milk'. Include brand/size if visible.\n"
        "  NEVER truncate or abbreviate item names with '...' or similar. Write the FULL name exactly as it appears on the receipt.\n\n"

        "PRICE — VERY IMPORTANT:\n"
        '  - "price" = the UNIT PRICE for ONE single item, NOT the line total\n'
        '  - Example: "12x1L Whip Cream  RM288" → qty: 12, price: 24.00 (288 ÷ 12 = 24 per unit)\n'
        '  - Example: "3 x Syrup  RM45"         → qty: 3,  price: 15.00 (45 ÷ 3 = 15 per unit)\n'
        '  - Example: "Milk 1L  RM7.50"          → qty: 1,  price: 7.50\n'
        '  - If receipt shows a line total (qty × unit), DIVIDE by qty to get unit price\n'
        '  - If receipt shows a per-unit price, use it directly\n\n'

        "Reply with ONLY valid JSON:\n"
        '{\n'
        '  "type": "receipt" or "invoice",\n'
        '  "supplier": "shop/supplier name",\n'
        '  "date": "YYYY-MM-DD",\n'
        '  "items": [{"name": "Full Cream Milk 1L", "qty": 12, "price": 5.50, "category": "ingredients"}],\n'
        '  "subtotal": 66.00,\n'
        '  "discount": 0,\n'
        '  "tax": 0,\n'
        '  "total": 66.00,\n'
        '  "payment_method": "cash" or "card" or "transfer" or "unknown",\n'
        f'  "paid_by": "{user_name}",\n'
        '  "notes": "any extra info",\n'
        '  "raw_text": "all readable text from image"\n'
        '}\n\n'
        "qty must be an integer. Prices in RM. If you can't read a value, use null."
    )

    # ── Primary: Groq vision ──
    try:
        result = await _groq_vision(receipt_prompt, image_bytes, mime_type, temperature=0.1, max_tokens=1000)
        if result:
            result = result.replace("```json", "").replace("```", "").strip()
            data = json.loads(result)
            if isinstance(data, dict):
                if "items" not in data:
                    data["items"] = []
                if "total" not in data:
                    data["total"] = 0
                # Post-process qty
                for item in data.get("items", []):
                    raw_qty = item.get("qty")
                    if raw_qty is None or raw_qty == "":
                        item["qty"] = 1
                    elif isinstance(raw_qty, str):
                        import re as _re
                        m = _re.match(r'(\d+)', str(raw_qty))
                        item["qty"] = int(m.group(1)) if m else 1
                    else:
                        try:
                            item["qty"] = int(raw_qty)
                        except (ValueError, TypeError):
                            item["qty"] = 1
                _fix_item_prices(data)

                # Strip any ellipsis/truncation artifacts from item names
                import re as _re
                for item in data.get("items", []):
                    _name = item.get("name", "")
                    _name = _name.replace("...", "").replace("…", "")  # strip ellipsis
                    _name = _re.sub(r'\s+', ' ', _name).strip()
                    if _name:
                        item["name"] = _name

                return data
    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f"Groq receipt OCR failed: {e}")

    # ── Fallback: Gemini ──
    client = get_client()
    if client is None:
        return None

    try:
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        text_part = types.Part.from_text(text=receipt_prompt)

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=types.Content(parts=[image_part, text_part]),
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=1000,
            ),
        )

        result = response.text.strip() if response.text else None
        if not result:
            return None

        # Clean up markdown code blocks
        result = result.replace("```json", "").replace("```", "").strip()
        data = json.loads(result)

        # Validate required fields
        if not isinstance(data, dict):
            return None
        if "items" not in data:
            data["items"] = []
        if "total" not in data:
            data["total"] = 0

        # Post-process: ensure qty is always an integer
        for item in data.get("items", []):
            raw_qty = item.get("qty")
            if raw_qty is None or raw_qty == "":
                item["qty"] = 1
            elif isinstance(raw_qty, str):
                import re as _re
                # Try to extract leading number from strings like "12x1L", "3 bottles"
                m = _re.match(r'(\d+)', str(raw_qty))
                item["qty"] = int(m.group(1)) if m else 1
            else:
                try:
                    item["qty"] = int(raw_qty)
                except (ValueError, TypeError):
                    item["qty"] = 1

        # Sanity check: if price looks like line total (price × qty already),
        # divide it back to get the real unit price
        _fix_item_prices(data)

        # Strip any ellipsis/truncation artifacts from item names
        import re as _re
        for item in data.get("items", []):
            _name = item.get("name", "")
            _name = _name.replace("...", "").replace("…", "")  # strip ellipsis
            _name = _re.sub(r'\s+', ' ', _name).strip()
            if _name:
                item["name"] = _name

        return data

    except (json.JSONDecodeError, Exception) as e2:
        logger.error(f"Gemini receipt OCR fallback also failed: {e2}")
        return None


async def ask_about_data(question: str, user_name: str, chat_id: int = 0) -> Optional[str]:
    """Let the AI answer questions about Sheets data (P&L, stock usage, etc.)."""
    # ── Primary: Groq ──
    try:
        from google_integration import get_all_data_for_ai
        current_month = _today().strftime("%Y-%m")
        data = get_all_data_for_ai(current_month)
        prev_month_date = _today().replace(day=1) - __import__("datetime").timedelta(days=1)
        prev_month = prev_month_date.strftime("%Y-%m")
        prev_data = get_all_data_for_ai(prev_month)

        prompt = (
            f"You are the café manager bot. A staff member is asking about business data.\n\n"
            f"--- CURRENT MONTH ({current_month}) ---\n{data}\n\n"
            f"--- PREVIOUS MONTH ({prev_month}) ---\n{prev_data}\n\n"
            f"Staff member: {user_name}\n"
            f"Question: {question}\n\n"
            f"Answer using the data above. Be specific with numbers. All amounts in RM."
        )
        result = await _groq_text(prompt, system=_GROQ_SYSTEM_PROMPT, temperature=0.3, max_tokens=500)
        if result:
            remember_bot_response(result, chat_id)
            return result
    except ImportError:
        return "Google Sheets integration not configured. Set up credentials.json first."
    except Exception as e:
        logger.warning(f"Groq data query failed: {e}")

    # ── Fallback: Gemini ──
    client = get_client()
    if client is None:
        return None

    try:
        from google_integration import get_all_data_for_ai

        # Try current month first, then let AI see it
        current_month = _today().strftime("%Y-%m")
        data = get_all_data_for_ai(current_month)

        # Also get previous month for comparison
        prev_month_date = _today().replace(day=1) - __import__("datetime").timedelta(days=1)
        prev_month = prev_month_date.strftime("%Y-%m")
        prev_data = get_all_data_for_ai(prev_month)

        prompt = (
            f"You are the café manager bot. A staff member is asking about business data.\n\n"
            f"--- CURRENT MONTH ({current_month}) ---\n{data}\n\n"
            f"--- PREVIOUS MONTH ({prev_month}) ---\n{prev_data}\n\n"
            f"Staff member: {user_name}\n"
            f"Question: {question}\n\n"
            f"Answer using the data above. Be specific with numbers. "
            f"Compare months if relevant. All amounts in RM. "
            f"If the data doesn't have what they need, say what's missing."
        )

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.3,
                max_output_tokens=500,
            ),
        )

        result = response.text.strip() if response.text else None
        if result:
            remember_bot_response(result, chat_id)
        return result

    except ImportError:
        return "Google Sheets integration not configured. Set up credentials.json first."
    except Exception as e2:
        logger.error(f"Gemini data query fallback also failed: {e2}")
        return None


async def analyze_pos_file(file_bytes: bytes, filename: str, user_name: str) -> Optional[str]:
    """
    Analyze a POS system data export (CSV, Excel, PDF).
    Returns a text analysis/summary.
    """
    client = get_client()
    if client is None:
        return None

    try:
        # Detect mime type from filename
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        mime_map = {
            "csv": "text/csv",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xls": "application/vnd.ms-excel",
            "pdf": "application/pdf",
            "txt": "text/plain",
        }
        mime_type = mime_map.get(ext, "application/octet-stream")

        cafe_data = _build_context()
        prompt = (
            f"{cafe_data}\n\n"
            f"Staff member {user_name} just sent the monthly POS report file: {filename}\n\n"
            "Analyze this POS data and provide:\n"
            "1. Total sales and transaction count\n"
            "2. Average transaction value\n"
            "3. Top selling items (top 10)\n"
            "4. Peak hours / busiest times\n"
            "5. Slowest days\n"
            "6. Revenue trends (growing/declining)\n"
            "7. Recommendations (pricing, menu changes, staffing)\n\n"
            "Keep it practical and specific. Use RM for all amounts.\n"
            "At the end, give a JSON summary line starting with SUMMARY_JSON: "
            '{"total_sales": 0, "transaction_count": 0, "avg_transaction": 0, '
            '"top_items": "item1, item2", "peak_hours": "11am-1pm"}'
        )

        file_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
        text_part = types.Part.from_text(text=prompt)

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=types.Content(parts=[file_part, text_part]),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.3,
                max_output_tokens=1000,
            ),
        )

        return response.text.strip() if response.text else None

    except Exception as e:
        # Groq doesn't support file uploads — no fallback for POS file analysis
        logger.error(f"POS analysis error (no fallback — Groq has no file support): {e}")
        return None
