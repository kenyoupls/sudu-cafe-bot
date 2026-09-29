#!/usr/bin/env python3
"""Mock tests for the staff-group owner override button. Run: python3 test_staff_override.py"""

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
    return SimpleNamespace(chat_data={}, bot=MagicMock())


QUERY = "whats the sales for this month so far"
QHASH = hashlib.sha256(QUERY.encode()).hexdigest()[:16]

# Fail loudly if the normal AI path is hit when it shouldn't be.
bot.process_message = AsyncMock(return_value=("Sales: RM1000", []))

# ─── Test 1 & 2: refusal + button, pending saved ───
upd = make_msg_update(QUERY)
ctx = make_ctx()
run(bot._handle_message_inner(upd, ctx))
upd.message.reply_text.assert_called_once()
args, kwargs = upd.message.reply_text.call_args
markup = kwargs.get("reply_markup")
btn = markup.inline_keyboard[0][0] if markup else None
check("T1a: refusal text sent", "only available in the owner group" in args[0])
check("T1b: button present with label",
      btn is not None and btn.text == "🔓 Show anyway (owner only)")
check("T1c: callback_data format + <=64 bytes",
      btn is not None and btn.callback_data == f"staff_override:{QHASH}"
      and len(btn.callback_data.encode()) <= 64)
check("T1d: AI not called for refusal", bot.process_message.await_count == 0)
p = ctx.chat_data.get("pending_staff_override")
check("T2a: pending saved with query + hash",
      p is not None and p["query"] == QUERY and p["hash"] == QHASH)
check("T2b: expiry ~5 min", p is not None and 290 < p["expires_at"] - time.time() <= 300)

# ─── Test 3: non-owner tap ───
cb = make_cb_update(999, f"staff_override:{QHASH}")
ctx3 = make_ctx()
ctx3.chat_data["pending_staff_override"] = dict(p)
bot.process_message.reset_mock()
run(bot.cb_staff_override(cb, ctx3))
q = cb.callback_query
check("T3a: non-owner -> answer() called once, no args",
      q.answer.await_count == 1 and q.answer.call_args.args == () and not q.answer.call_args.kwargs)
check("T3b: non-owner -> no processing / edits / replies",
      bot.process_message.await_count == 0 and q.edit_message_text.await_count == 0
      and q.message.reply_text.await_count == 0)
check("T3c: non-owner tap leaves pending intact", "pending_staff_override" in ctx3.chat_data)

# ─── Test 4: owner tap ───
cb = make_cb_update(OWNER_ID, f"staff_override:{QHASH}")
ctx4 = make_ctx()
ctx4.chat_data["pending_staff_override"] = dict(p)
bot.process_message.reset_mock()
bot.store = MagicMock()
run(bot.cb_staff_override(cb, ctx4))
q = cb.callback_query
check("T4a: process_message called once", bot.process_message.await_count == 1)
pm = bot.process_message.call_args
check("T4b: is_staff_group=False", pm.kwargs.get("is_staff_group") is False)
check("T4c: original query passed", pm.args[0] == QUERY)
check("T4d: AI reply posted", q.message.reply_text.await_count == 1
      and q.message.reply_text.call_args.args[0] == "Sales: RM1000")
edit_txt = q.edit_message_text.call_args
check("T4e: refusal edited, button removed, annotated",
      edit_txt is not None and "Overridden by Boss" in edit_txt.args[0]
      and edit_txt.kwargs.get("reply_markup") is None)
check("T4f: pending consumed (single use)", "pending_staff_override" not in ctx4.chat_data)

# ─── Test 4b: owner tap executes actions unfiltered ───
cb = make_cb_update(OWNER_ID, f"staff_override:{QHASH}")
ctx4b = make_ctx()
ctx4b.chat_data["pending_staff_override"] = dict(p)
acts = [{"action": "show_sales"}]
bot.process_message = AsyncMock(return_value=("Here you go", acts))
bot._execute_actions = AsyncMock(return_value=["done"])
run(bot.cb_staff_override(cb, ctx4b))
check("T4g: blocked action (show_sales) passed to executor for owner",
      bot._execute_actions.await_count == 1 and bot._execute_actions.call_args.args[0] == acts)
ov = bot._execute_actions.call_args.args[2]
check("T4h: executor update exposes original text", ov.message.text == QUERY)

# ─── Test 5: hash mismatch ───
cb = make_cb_update(OWNER_ID, "staff_override:deadbeefdeadbeef")
ctx5 = make_ctx()
ctx5.chat_data["pending_staff_override"] = dict(p)
bot.process_message = AsyncMock(return_value=("x", []))
run(bot.cb_staff_override(cb, ctx5))
q = cb.callback_query
check("T5a: stale hash -> no processing", bot.process_message.await_count == 0)
check("T5b: stale hash -> no message edit/reply",
      q.edit_message_text.await_count == 0 and q.message.reply_text.await_count == 0)
check("T5c: stale hash -> pending untouched", "pending_staff_override" in ctx5.chat_data)

# ─── Test 6: expired ───
cb = make_cb_update(OWNER_ID, f"staff_override:{QHASH}")
ctx6 = make_ctx()
exp = dict(p)
exp["expires_at"] = time.time() - 1
ctx6.chat_data["pending_staff_override"] = exp
run(bot.cb_staff_override(cb, ctx6))
check("T6a: expired -> no processing", bot.process_message.await_count == 0)
check("T6b: expired -> no edits/replies",
      cb.callback_query.edit_message_text.await_count == 0
      and cb.callback_query.message.reply_text.await_count == 0)

# No pending at all
cb = make_cb_update(OWNER_ID, f"staff_override:{QHASH}")
run(bot.cb_staff_override(cb, make_ctx()))
check("T6c: no pending -> no processing", bot.process_message.await_count == 0)

# ─── Test 7: non-blocked query in staff group -> no button ───
bot._rehydrate_pending_tasks = MagicMock()
bot.remember = MagicMock()
bot.store = MagicMock()
bot.process_message = AsyncMock(return_value=("Noted milk", []))
bot.pending_store = MagicMock()
bot.pending_store.get_for_chat.return_value = []
upd = make_msg_update("@testbot milk stock is 5 bottles now")
ctx7 = make_ctx()
ctx7.bot.username = "testbot"
run(bot._handle_message_inner(upd, ctx7))
sent_kwargs = [c.kwargs for c in upd.message.reply_text.call_args_list]
check("T7a: normal query -> AI called with is_staff_group=True",
      bot.process_message.await_count >= 1
      and bot.process_message.call_args.kwargs.get("is_staff_group") is True)
check("T7b: normal query -> no inline button on any reply",
      all(k.get("reply_markup") is None for k in sent_kwargs))
check("T7c: normal query -> no pending override saved",
      "pending_staff_override" not in ctx7.chat_data)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
