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

# read_tab emitted -> no stall re-ask; read_tab safety net re-invokes once
# with the stashed tab data as extra_context and sends the fresh answer.
def owner_tap_ctx():
    cb = make_cb_update(OWNER_ID, f"staff_override:{QHASH}")
    ctx = make_ctx()
    ctx.chat_data["pending_staff_override"] = {
        "query": QUERY, "hash": QHASH, "name": "Boss",
        "expires_at": time.time() + 300,
    }
    run(bot.cb_staff_override(cb, ctx))
    return cb.callback_query, ctx


async def _fake_exec(actions, name, upd, ctx):
    ctx.chat_data.setdefault("_read_tab_results", {})["Sales"] = {
        "headers": ["Month", "Total"],
        "rows": [{"Month": "September", "Total": "RM 9,999"}, {"Month": "May", "Total": "RM 1"}],
    }
    return []

bot._execute_actions = AsyncMock(side_effect=_fake_exec)
bot.process_message = AsyncMock(side_effect=[
    ("Let me pull the expense details.", [{"action": "read_tab", "tab": "Sales"}]),
    ("Sales so far: RM 9,999", []),
])
q, ctx = owner_tap_ctx()
check("S3a: read_tab emitted -> stall guard skipped, one follow-up (2 calls)",
      bot.process_message.await_count == 2)
check("S3b: follow-up got extra_context with tab data",
      "RM 9,999" in (bot.process_message.await_args.kwargs.get("extra_context") or ""))
check("S3c: follow-up keeps owner mode + bypass history",
      bot.process_message.await_args.kwargs.get("is_staff_group") is False
      and bot.process_message.await_args.kwargs.get("_bypass_chat_history") is True)
check("S3d: stall reply then fresh answer sent",
      [c.args[0] for c in q.message.reply_text.call_args_list]
      == ["Let me pull the expense details.", "Sales so far: RM 9,999"])
check("S3e: stash consumed", "_read_tab_results" not in ctx.chat_data)

# read_tab follow-up still stalls -> clean fallback
bot.process_message = AsyncMock(side_effect=[
    ("Let me pull the expense details.", [{"action": "read_tab", "tab": "Sales"}]),
    ("Let me check again.", []),
])
q, _ = owner_tap_ctx()
check("S4a: follow-up stall -> fallback sent",
      q.message.reply_text.call_args.args[0]
      == "I couldn't retrieve the data. Please check the Google Sheet directly.")

# read_tab follow-up raises -> fallback
bot.process_message = AsyncMock(side_effect=[
    ("Let me pull the expense details.", [{"action": "read_tab", "tab": "Sales"}]),
    RuntimeError("boom"),
])
q, _ = owner_tap_ctx()
check("S4b: follow-up error -> fallback sent",
      q.message.reply_text.call_args.args[0].startswith("I couldn't retrieve the data."))

# read_tab + write action -> no follow-up
bot.process_message = AsyncMock(return_value=(
    "Let me pull the expense details.",
    [{"action": "read_tab", "tab": "Sales"}, {"action": "append_row", "tab": "Sales", "data": {}}]))
q, _ = owner_tap_ctx()
check("S5a: read_tab + write -> no follow-up", bot.process_message.await_count == 1)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
