#!/usr/bin/env python3
"""Mock tests for staff-group polish: refusal history filter, tag gate,
override history bypass. Run: python3 test_staff_extras.py"""

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
_TMP = tempfile.mkdtemp(prefix="staff_extras_test_")
os.chdir(_TMP)

import config  # noqa: E402
import bot  # noqa: E402
import ai_chat  # noqa: E402

STAFF_CHAT = -100555
BOT_ID = 777
config.OWNER_USER_IDS = {834454829}
config.STAFF_GROUP_ID = STAFF_CHAT
config.ALLOWED_GROUP_IDS = [STAFF_CHAT]

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name}")


def run(coro):
    return asyncio.run(coro)


def make_update(text, reply_from_id=None):
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.entities = []
    msg.caption_entities = []
    msg.reply_text = AsyncMock()
    if reply_from_id is None:
        msg.reply_to_message = None
    else:
        msg.reply_to_message = SimpleNamespace(
            from_user=SimpleNamespace(id=reply_from_id), text="earlier msg",
            message_id=1)
    user = SimpleNamespace(id=42, full_name="Staff Sam", username="sam")
    chat = SimpleNamespace(id=STAFF_CHAT, type="supergroup")
    return SimpleNamespace(message=msg, effective_chat=chat, effective_user=user)


def make_ctx():
    b = MagicMock()
    b.id = BOT_ID
    b.username = "Sudu_helper_bot"
    return SimpleNamespace(chat_data={}, bot=b)


def refusal_sent(upd):
    return any("only available in the owner group" in str(c.args[0])
               for c in upd.message.reply_text.call_args_list if c.args)


bot.process_message = AsyncMock(return_value=("ok", []))

# ─── Fix 1: refusal regex ───
check("F1a: ai_chat has _STAFF_REFUSAL_RE", hasattr(ai_chat, "_STAFF_REFUSAL_RE"))
check("F1b: matches refusal message",
      bool(ai_chat._STAFF_REFUSAL_RE.search(
          "Financial info is only available in the owner group. Check with the boss.")))
check("F1c: does not match 'sales were great today'",
      not ai_chat._STAFF_REFUSAL_RE.search("sales were great today"))

# ─── Fix 2: tag gate ───
# Untagged chatter -> no refusal, no pending override
upd = make_update("sales was low")
ctx = make_ctx()
try:
    run(bot._handle_message_inner(upd, ctx))
except Exception as e:  # later paths may need real services; only the gate matters
    print(f"note: downstream path raised {type(e).__name__}: {e}")
check("F2a: untagged chatter -> no refusal", not refusal_sent(upd))
check("F2a2: untagged chatter -> no pending override saved",
      "pending_staff_override" not in ctx.chat_data)

# Tagged
upd = make_update("@Sudu_helper_bot whats the sales")
ctx = make_ctx()
run(bot._handle_message_inner(upd, ctx))
check("F2b: tagged financial query -> refusal sent", refusal_sent(upd))
check("F2b2: tagged -> pending override saved", "pending_staff_override" in ctx.chat_data)
check("F2b3: tagged -> AI not called", bot.process_message.await_count == 0)

# Reply to bot's message
upd = make_update("whats the sales", reply_from_id=BOT_ID)
ctx = make_ctx()
run(bot._handle_message_inner(upd, ctx))
check("F2c: reply-to-bot financial query -> refusal sent", refusal_sent(upd))

# Reply to a human (not bot) -> no refusal
upd = make_update("whats the sales", reply_from_id=999)
ctx = make_ctx()
try:
    run(bot._handle_message_inner(upd, ctx))
except Exception as e:
    print(f"note: downstream path raised {type(e).__name__}: {e}")
check("F2d: reply to a human -> no refusal", not refusal_sent(upd))

# ─── Fix 3: bypass chat history (real process_message, mocked LLM + storage) ───
MARKER = "ZZ_HISTORY_MARKER_9931"
REFUSAL = "Financial info is only available in the owner group. Check with the boss."
today = ai_chat._today().isoformat()
fake_msgs = [
    {"time": "10:00", "who": "Sam", "type": "text", "text": f"hello {MARKER}", "important": True},
    {"time": "10:01", "who": "Bot", "type": "bot_response", "text": REFUSAL},
]
ai_chat._init_memory = lambda chat_id=0: None
ai_chat._get_all_recent_days = lambda chat_id=0: [today]
ai_chat._load_day = lambda day, chat_id=0: list(fake_msgs)
ai_chat._load_summaries = lambda chat_id=0: {}
ai_chat._build_context = lambda is_staff_group=False: "CAFE DATA"
ai_chat._pending_tasks_context = lambda chat_id: ""
ai_chat.remember_bot_response = lambda *a, **k: None

# Direct memory context: history present by default, refusal filtered, bypass omits.
mem = ai_chat.get_memory_context(chat_id=STAFF_CHAT)
check("F3a: default memory includes history marker", MARKER in mem)
check("F1d: refusal filtered out of history", "only available in the owner group" not in mem)
mem_b = ai_chat.get_memory_context(chat_id=STAFF_CHAT, _bypass_chat_history=True)
check("F3b: bypass memory omits history", MARKER not in mem_b and "RECENT GROUP CHAT" not in mem_b)

# Force the Gemini path (Groq raises) and capture the prompt; also capture Groq's.
prompts = {}


async def fake_groq(prompt, **kw):
    prompts["groq"] = prompt
    raise RuntimeError("force gemini fallback")


ai_chat._groq_text = fake_groq


def fake_generate(model, contents, config):
    prompts["gemini"] = contents
    return SimpleNamespace(text="Here you go")


ai_chat.get_client = lambda: SimpleNamespace(
    models=SimpleNamespace(generate_content=fake_generate))

reply, _ = run(ai_chat.process_message("whats the sales", "Boss", None, chat_id=STAFF_CHAT,
                                       is_staff_group=False))
check("F3c: normal call -> Gemini prompt contains history marker",
      MARKER in prompts.get("gemini", ""))

check("F3c2: normal call -> Groq prompt has history marker, refusal filtered",
      MARKER in prompts.get("groq", "")
      and "only available in the owner group" not in prompts.get("groq", ""))

prompts.clear()
reply, _ = run(ai_chat.process_message("whats the sales", "Boss", None, chat_id=STAFF_CHAT,
                                       is_staff_group=False, _bypass_chat_history=True))
check("F3d: bypass call -> Gemini prompt lacks history marker",
      "gemini" in prompts and MARKER not in prompts["gemini"]
      and "RECENT GROUP CHAT" not in prompts["gemini"])
check("F3e: bypass call -> Groq prompt lacks history marker",
      MARKER not in prompts.get("groq", ""))
check("F3f: bypass call still returns reply", reply == "Here you go")

# cb_staff_override passes the flag
bot.process_message = AsyncMock(return_value=("Sales: RM1", []))
bot.store = MagicMock()
import hashlib, time  # noqa: E402
q_text = "whats the sales"
h = hashlib.sha256(q_text.encode()).hexdigest()[:16]
qq = MagicMock()
qq.data = f"staff_override:{h}"
qq.from_user = SimpleNamespace(id=834454829, full_name="Boss", username=None)
qq.answer = AsyncMock()
qq.edit_message_text = AsyncMock()
qq.message = MagicMock()
qq.message.text = REFUSAL
qq.message.chat = SimpleNamespace(id=STAFF_CHAT, type="supergroup")
qq.message.reply_text = AsyncMock()
cbu = SimpleNamespace(callback_query=qq, effective_chat=qq.message.chat,
                      effective_user=qq.from_user, message=None)
octx = make_ctx()
octx.chat_data["pending_staff_override"] = {
    "query": q_text, "hash": h, "name": "Sam", "expires_at": time.time() + 200}
run(bot.cb_staff_override(cbu, octx))
check("F3g: cb_staff_override passes _bypass_chat_history=True",
      bot.process_message.await_count == 1
      and bot.process_message.call_args.kwargs.get("_bypass_chat_history") is True
      and bot.process_message.call_args.kwargs.get("is_staff_group") is False)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
