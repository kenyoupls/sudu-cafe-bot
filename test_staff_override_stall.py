#!/usr/bin/env python3
"""Stall-guard tests for cb_staff_override. Run: python3 test_staff_override_stall.py"""

import asyncio
import hashlib
import os
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
_TMP = tempfile.mkdtemp(prefix="staff_override_test_")
os.chdir(_TMP)

import config  # noqa: E402
import bot  # noqa: E402

OWNER_ID = 834454829
STAFF_CHAT = -100555
config.OWNER_USER_IDS = {OWNER_ID}
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


def make_msg_update(text):
    msg = MagicMock()
    msg.text = text
    msg.reply_to_message = None
    msg.reply_text = AsyncMock()
    user = SimpleNamespace(id=42, full_name="Staff Sam", username="sam")
    chat = SimpleNamespace(id=STAFF_CHAT, type="supergroup")
    return SimpleNamespace(message=msg, effective_chat=chat, effective_user=user)


def make_cb_update(user_id, data, name="Boss"):
    q = MagicMock()
    q.data = data
    q.from_user = SimpleNamespace(id=user_id, full_name=name, username=None)
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    q.message = MagicMock()
    q.message.text = "Financial info is only available in the owner group. Check with the boss."
    q.message.chat = SimpleNamespace(id=STAFF_CHAT, type="supergroup")
    q.message.reply_text = AsyncMock()
    chat = SimpleNamespace(id=STAFF_CHAT, type="supergroup")
    return SimpleNamespace(callback_query=q, effective_chat=chat,
                           effective_user=q.from_user, message=None)


def make_ctx():
    b = MagicMock()
    b.id = 777
    b.username = "testbot"
    return SimpleNamespace(chat_data={}, bot=b)


QUERY = "whats the sales for this month so far"
QHASH = hashlib.sha256(QUERY.encode()).hexdigest()[:16]


def owner_tap():
    cb = make_cb_update(OWNER_ID, f"staff_override:{QHASH}")
    ctx = make_ctx()
    ctx.chat_data["pending_staff_override"] = {
        "query": QUERY, "hash": QHASH, "name": "Boss",
        "expires_at": time.time() + 300,
    }
    run(bot.cb_staff_override(cb, ctx))
    return cb.callback_query

bot.store = MagicMock()
bot._execute_actions = AsyncMock(return_value=[])

# Stall then real answer -> second reply sent
bot.process_message = AsyncMock(side_effect=[
    ("Let me pull the expense details.", []),
    ("Milk this month: RM120", []),
])
q = owner_tap()
check("S1a: re-asked once", bot.process_message.await_count == 2)
check("S1b: store refreshed", bot.store.refresh_if_stale.called)
check("S1c: second reply sent, not the stall",
      q.message.reply_text.await_count == 1
      and q.message.reply_text.call_args.args[0] == "Milk this month: RM120")

# Stall twice -> clean fallback
bot.process_message = AsyncMock(return_value=("Let me pull the expense details.", []))
q = owner_tap()
check("S2a: fallback text sent",
      q.message.reply_text.await_count == 1
      and q.message.reply_text.call_args.args[0].startswith("I don't have the data for that right now."))

# read_tab emitted -> no re-ask
bot.process_message = AsyncMock(return_value=("Let me pull the expense details.", [{"action": "read_tab"}]))
q = owner_tap()
check("S3a: read_tab emitted -> no re-ask", bot.process_message.await_count == 1)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
