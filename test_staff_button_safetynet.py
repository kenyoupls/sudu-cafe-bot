#!/usr/bin/env python3
"""Mock tests: broader staff-blocked regex + post-hoc override-button safety net.
Run: python3 test_staff_button_safetynet.py"""

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
os.chdir(tempfile.mkdtemp(prefix="staff_safetynet_test_"))

import config  # noqa: E402
import bot  # noqa: E402

STAFF_CHAT = -100555
OWNER_CHAT = -100777
config.OWNER_USER_IDS = {834454829}
config.STAFF_GROUP_ID = STAFF_CHAT
config.ALLOWED_GROUP_IDS = [STAFF_CHAT, OWNER_CHAT]

PASS = FAIL = 0


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


# ─── Regex A ───
CASES = [
    ("how much did we spend on milk", True),
    ("how much did we spent on milk", True),
    ("how much do we spend on milk", True),
    ("how much are we spending", True),
    ("cost of milk this month", True),
    ("total expenses", True),
    ("our income", True),
    ("milk cost", True),
    ("sales was low", True),
    ("who paid", True),
    ("milk 5", False),
    ("hi bot", False),
    # extra tenses / phrases
    ("how much have we earned", True),
    ("how much did we make today", True),
    ("how much did we pay the supplier", True),
    ("how much for oat milk", True),
    ("how much money is left", True),
    ("what's our budget", True),
    ("total spent this week", True),
    ("the money we owe", True),
    # non-financial
    ("how much milk do we have", False),
    ("how much stock is left", False),
    ("cups 20 straws 5", False),
]
for phrase, want in CASES:
    got = bot._looks_like_staff_blocked_query(phrase)
    check(f"regex {phrase!r} -> {want}", got is want)

# ─── Refusal echo regex ───
check("echo: owner-group refusal",
      bool(bot._REFUSAL_ECHO_RE.search("Financial info is only available in the owner group.")))
check("echo: 'only share ... in the owner group'",
      bool(bot._REFUSAL_ECHO_RE.search(
          "We only share sales figures in the owner group – please check with the boss.")))
check("echo: normal reply not matched",
      not bot._REFUSAL_ECHO_RE.search("The sales for this month were good"))

# ─── E2E mocks ───
bot._rehydrate_pending_tasks = MagicMock()
bot.remember = MagicMock()
bot.store = MagicMock()
bot.pending_store = MagicMock()
bot.pending_store.get_for_chat.return_value = []

REFUSAL = "Financial info is only available in the owner group. Check with the boss."


def make_update(text, chat_id):
    msg = MagicMock()
    msg.text = "@testbot " + text  # @mention => bot addressed
    msg.caption = None
    msg.entities = []
    msg.reply_to_message = None
    msg.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=1))
    user = SimpleNamespace(id=42, full_name="Sam", username="sam")
    chat = SimpleNamespace(id=chat_id, type="supergroup")
    return SimpleNamespace(message=msg, effective_chat=chat, effective_user=user)


def make_ctx():
    b = MagicMock()
    b.id = 777
    b.username = "testbot"
    return SimpleNamespace(chat_data={}, bot=b)


def button_of(upd):
    for c in upd.message.reply_text.call_args_list:
        m = c.kwargs.get("reply_markup")
        if m is not None:
            return m.inline_keyboard[0][0]
    return None


# E2E 1: regex MISSES, AI refuses -> post-hoc button
Q = "how much moolah went out on milk"
check("E2E1 precondition: regex misses phrase", not bot._looks_like_staff_blocked_query(Q))
bot.process_message = AsyncMock(return_value=(REFUSAL, []))
upd, ctx = make_update(Q, STAFF_CHAT), make_ctx()
run(bot._handle_message_inner(upd, ctx))
b = button_of(upd)
check("E2E1: AI was called (regex missed)", bot.process_message.await_count == 1)
check("E2E1: post-hoc button attached", b is not None and b.text == "🔓 Show anyway (owner only)")
check("E2E1: callback_data format",
      b is not None and b.callback_data.startswith("staff_override:") and len(b.callback_data.encode()) <= 64)
p = ctx.chat_data.get("pending_staff_override")
check("E2E1: pending override saved", p is not None and p["query"] == Q
      and b is not None and b.callback_data == f"staff_override:{p['hash']}")

# E2E 2: regex catches -> code-side refusal + button, AI not called
bot.process_message = AsyncMock(return_value=("should not be used", []))
upd, ctx = make_update("how much did we spend on milk", STAFF_CHAT), make_ctx()
run(bot._handle_message_inner(upd, ctx))
check("E2E2: AI not called", bot.process_message.await_count == 0)
check("E2E2: exactly one reply", upd.message.reply_text.await_count == 1)
check("E2E2: button present", button_of(upd) is not None)
check("E2E2: pending saved", "pending_staff_override" in ctx.chat_data)

# E2E 3: staff group, normal AI reply -> no button
bot.process_message = AsyncMock(return_value=("Noted milk 5", []))
upd, ctx = make_update("milk 5", STAFF_CHAT), make_ctx()
run(bot._handle_message_inner(upd, ctx))
check("E2E3: normal reply no button", button_of(upd) is None
      and "pending_staff_override" not in ctx.chat_data)

# E2E 4: owner group, financial question -> no button, normal response
bot.process_message = AsyncMock(return_value=("You spent RM120 on milk.", []))
upd, ctx = make_update("how much did we spend on milk", OWNER_CHAT), make_ctx()
run(bot._handle_message_inner(upd, ctx))
check("E2E4: owner group AI called", bot.process_message.await_count == 1)
check("E2E4: owner group no button", button_of(upd) is None)
check("E2E4: owner group normal reply sent",
      upd.message.reply_text.call_args_list[0].args[0] == "You spent RM120 on milk.")

# E2E 5: owner group even if reply echoes refusal wording -> no button
bot.process_message = AsyncMock(return_value=(REFUSAL, []))
upd, ctx = make_update("some odd question", OWNER_CHAT), make_ctx()
run(bot._handle_message_inner(upd, ctx))
check("E2E5: non-staff group never gets button", button_of(upd) is None)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
