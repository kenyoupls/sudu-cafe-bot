#!/usr/bin/env python3
"""Mock tests for the staff-override SESSION (stays open through follow-ups).
Run: python3 test_staff_override_session.py"""

import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
_TMP = tempfile.mkdtemp(prefix="staff_override_session_test_")
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


class Clock:
    """Controllable replacement for time.time()."""
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


def make_msg_update(text, reply_to_bot=False):
    msg = MagicMock()
    msg.text = text
    if reply_to_bot:
        rep = MagicMock()
        rep.from_user = SimpleNamespace(id=777, full_name="Bot", username="testbot")
        rep.message_id = 5
        rep.text = "Which milk type?"
        rep.caption = None
        rep.photo = rep.document = rep.voice = rep.video = None
        msg.reply_to_message = rep
    else:
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


def sent_texts(upd):
    return [c.args[0] for c in upd.message.reply_text.call_args_list]


QUERY = "whats the sales for this month so far"
QHASH = hashlib.sha256(QUERY.encode()).hexdigest()[:16]

# Shared mocks (same set test_staff_override.py uses for _handle_message_inner)
bot._rehydrate_pending_tasks = MagicMock()
bot.remember = MagicMock()
bot.store = MagicMock()
bot.pending_store = MagicMock()
bot.pending_store.get_for_chat.return_value = []
bot._execute_actions = AsyncMock(return_value=[])


def pending_for(ctx):
    ctx.chat_data["pending_staff_override"] = {
        "query": QUERY, "hash": QHASH, "name": "Sam",
        "expires_at": clock() + 300,
    }


def tap(ctx, ai_reply="Which milk type?"):
    bot.process_message = AsyncMock(return_value=(ai_reply, []))
    pending_for(ctx)
    cb = make_cb_update(OWNER_ID, f"staff_override:{QHASH}")
    run(bot.cb_staff_override(cb, ctx))
    return cb


clock = Clock()

with patch("time.time", clock):
    # ─── Test 1: START ───
    ctx = make_ctx()
    tap(ctx, "Which milk type?")
    s = ctx.chat_data.get("staff_override_session")
    check("T1a: session created", s is not None)
    check("T1b: owner_user_id / original_query / closed",
          s is not None and s["owner_user_id"] == OWNER_ID
          and s["original_query"] == QUERY and s["closed"] is False)
    check("T1c: started_at = now",
          s is not None and s["started_at"] == clock.t)
    check("T1d: hard TTL from config (300)",
          s is not None and s["hard_expires_at"] == clock.t + config.STAFF_OVERRIDE_SESSION_HARD_TTL == clock.t + 300)
    check("T1e: idle TTL from config (180)",
          s is not None and s["idle_expires_at"] == clock.t + config.STAFF_OVERRIDE_SESSION_IDLE_TTL == clock.t + 180)
    check("T1f: AI asked a question -> session stays open",
          bot._get_active_override_session(ctx) is not None)

    # ─── Test 2 + 3: DURING — no code-side refusal, owner-mode AI call ───
    upd = make_msg_update("@testbot what were the sales for full cream", reply_to_bot=False)
    bot.process_message = AsyncMock(return_value=("Full cream sold 12. Anything else?", []))
    run(bot._handle_message_inner(upd, ctx))
    check("T2a: staff-block regex would match this text",
          bot._looks_like_staff_blocked_query("@testbot what were the sales for full cream"))
    check("T2b: AI called (no code-side refusal)", bot.process_message.await_count == 1)
    check("T2c: no refusal text / no override button sent",
          all("only available in the owner group" not in t for t in sent_texts(upd))
          and all(c.kwargs.get("reply_markup") is None for c in upd.message.reply_text.call_args_list))
    check("T2d: no new pending_staff_override created", "pending_staff_override" not in ctx.chat_data)
    kw = bot.process_message.call_args.kwargs
    check("T3a: process_message is_staff_group=False in staff chat", kw.get("is_staff_group") is False)
    check("T3b: _bypass_chat_history=True", kw.get("_bypass_chat_history") is True)
    check("T3c: reply_context anchors original query",
          QUERY in (bot.process_message.call_args.args[2] or ""))
    check("T3d: AI reply sent", "Full cream sold 12. Anything else?" in sent_texts(upd))

    # Short bare follow-up ("Oat") reaches the AI instead of the Bug D guard
    upd = make_msg_update("Oat", reply_to_bot=True)
    bot.process_message = AsyncMock(return_value=("Oat: 4 cartons. Want more?", []))
    run(bot._handle_message_inner(upd, ctx))
    check("T3e: short follow-up goes to AI during session", bot.process_message.await_count == 1)
    check("T3f: reply-to-bot keeps its own reply_context",
          "Which milk type?" in (bot.process_message.call_args.args[2] or ""))
    # Tagged (not a reply): no reply_context from Telegram -> session anchors it
    upd = make_msg_update("@testbot oat please")
    bot.process_message = AsyncMock(return_value=("Oat: 4 cartons. Want more?", []))
    run(bot._handle_message_inner(upd, ctx))
    rc = bot.process_message.call_args.args[2] or ""
    check("T3g: session anchors original query + bot's last message",
          QUERY in rc and "Oat: 4 cartons" in rc)

    # Control: a normal staff chat WITHOUT a session keeps staff mode
    ctx_n = make_ctx()
    upd = make_msg_update("@testbot milk stock is 5 bottles now")
    bot.process_message = AsyncMock(return_value=("Noted", []))
    run(bot._handle_message_inner(upd, ctx_n))
    kw = bot.process_message.call_args.kwargs
    check("T3h: no session -> is_staff_group=True, no bypass",
          kw.get("is_staff_group") is True and kw.get("_bypass_chat_history") is False)
    upd = make_msg_update("@testbot what were the sales", reply_to_bot=True)
    bot.process_message = AsyncMock(return_value=("x", []))
    run(bot._handle_message_inner(upd, ctx_n))
    check("T3i: no session -> financial query still refused in code",
          bot.process_message.await_count == 0
          and any("only available in the owner group" in t for t in sent_texts(upd)))

    # ─── Test 4: IDLE BUMP ───
    ctx = make_ctx()
    tap(ctx, "Which milk type?")
    clock.advance(100)
    before = ctx.chat_data["staff_override_session"]["idle_expires_at"]
    upd = make_msg_update("Full cream", reply_to_bot=True)
    bot.process_message = AsyncMock(return_value=("Which size?", []))
    run(bot._handle_message_inner(upd, ctx))
    after = ctx.chat_data["staff_override_session"]["idle_expires_at"]
    check("T4a: idle timer bumped by message", after > before)
    check("T4b: idle = now + IDLE_TTL", after == clock.t + config.STAFF_OVERRIDE_SESSION_IDLE_TTL)
    check("T4c: hard expiry NOT extended",
          ctx.chat_data["staff_override_session"]["hard_expires_at"] == clock.t - 100 + 300)

    # ─── Test 5: END via explicit "done" ───
    for phrase in ("done", "@testbot Thanks!", "thank you", "Ok done", "that's all.", "Close"):
        ctx = make_ctx()
        tap(ctx, "Which milk type?")
        upd = make_msg_update(phrase, reply_to_bot=True)
        bot.process_message = AsyncMock(return_value=("should not be called", []))
        run(bot._handle_message_inner(upd, ctx))
        check(f"T5: '{phrase}' -> session closed", "staff_override_session" not in ctx.chat_data
              and bot._get_active_override_session(ctx) is None)
        check(f"T5: '{phrase}' -> 'OK — override session closed.' sent",
              sent_texts(upd) == ["OK — override session closed."])
    check("T5x: explicit end does not call the AI", bot.process_message.await_count == 0)
    # Untagged "thanks" (staff chatter) must not close the session or make the bot reply
    ctx = make_ctx()
    tap(ctx, "Which milk type?")
    upd = make_msg_update("thanks")
    run(bot._handle_message_inner(upd, ctx))
    check("T5z: untagged 'thanks' -> bot silent, session untouched",
          upd.message.reply_text.await_count == 0 and bot._get_active_override_session(ctx) is not None)
    # Non-phrase text containing a phrase must NOT end the session
    ctx = make_ctx()
    tap(ctx, "Which milk type?")
    bot.process_message = AsyncMock(return_value=("Which size?", []))
    run(bot._handle_message_inner(make_msg_update("thanks, also oat please", reply_to_bot=True), ctx))
    check("T5y: 'thanks, also oat please' does not end session",
          bot._get_active_override_session(ctx) is not None)

    # ─── Test 6: session STAYS OPEN on final answers (no "?" heuristic) ───
    ctx = make_ctx()
    tap(ctx, "Which milk type?")
    upd = make_msg_update("Full cream", reply_to_bot=True)
    bot.process_message = AsyncMock(return_value=("Full cream sales this month: RM1,200.", []))
    run(bot._handle_message_inner(upd, ctx))
    check("T6a: final answer -> session STAYS open", bot._get_active_override_session(ctx) is not None)
    check("T6b: only the AI reply sent",
          sent_texts(upd) == ["Full cream sales this month: RM1,200."])
    check("T6b2: last_bot_reply updated to the final answer",
          ctx.chat_data["staff_override_session"].get("last_bot_reply") == "Full cream sales this month: RM1,200.")
    # A follow-up after a final answer is still owner-mode with the topic injected
    upd = make_msg_update("what about low fat", reply_to_bot=True)
    bot.process_message = AsyncMock(return_value=("Low fat: RM80.", []))
    run(bot._handle_message_inner(upd, ctx))
    kw = bot.process_message.call_args.kwargs
    rc = bot.process_message.call_args.args[2] if len(bot.process_message.call_args.args) > 2 else kw.get("reply_context")
    check("T6b3: follow-up after final answer is owner-mode", kw.get("is_staff_group") is False)
    check("T6b4: follow-up gets Original topic in reply_context", rc and f"Original topic: {QUERY}" in rc)
    check("T6b5: follow-up gets bot's previous answer in reply_context",
          rc and "Bot's previous answer: Full cream sales this month: RM1,200." in rc)
    # "let me check" promise keeps session open
    ctx = make_ctx()
    tap(ctx, "Which milk type?")
    bot.process_message = AsyncMock(return_value=("Sure, let me check the sheet.", []))
    with patch.object(bot, "_execute_actions", AsyncMock(return_value=[])):
        # read_tab emitted so the stall-guard doesn't re-ask; promise stays open
        bot.process_message = AsyncMock(return_value=("Sure, let me check.", [{"action": "read_tab"}]))
        run(bot._handle_message_inner(make_msg_update("Full cream", reply_to_bot=True), ctx))
    check("T6c: check-promise does not close session", bot._get_active_override_session(ctx) is not None)
    # Direct helper unit checks
    ctx = make_ctx(); tap(ctx, "Which milk type?")
    check("T6d: helper: reply ending '?' -> stays open",
          bot._maybe_close_override_session(ctx, "Which size?", "big") is None)
    check("T6e: helper: final reply -> stays open (no final_answer close)",
          bot._maybe_close_override_session(ctx, "It is RM5.", "big") is None
          and "staff_override_session" in ctx.chat_data)
    check("T6e2: helper records last_bot_reply",
          ctx.chat_data["staff_override_session"].get("last_bot_reply") == "It is RM5.")
    # Owner tap whose first answer is already final keeps the session open
    ctx = make_ctx(); tap(ctx, "Sales: RM1000")
    check("T6f: tap answered with final reply -> session stays open",
          "staff_override_session" in ctx.chat_data)

    # ─── Test 7: END via idle timeout ───
    ctx = make_ctx()
    tap(ctx, "Which milk type?")
    s = ctx.chat_data["staff_override_session"]
    clock.t = s["idle_expires_at"] - 1
    check("T7a: just before idle expiry -> active", bot._get_active_override_session(ctx) is not None)
    clock.t = s["idle_expires_at"] + 1
    check("T7b: past idle_expires_at -> not active", bot._get_active_override_session(ctx) is None)
    # ... and the next message is back in staff mode
    ctx = make_ctx()
    tap(ctx, "Which milk type?")
    clock.advance(config.STAFF_OVERRIDE_SESSION_IDLE_TTL + 5)
    bot.process_message = AsyncMock(return_value=("Noted", []))
    run(bot._handle_message_inner(make_msg_update("@testbot milk is 5 bottles"), ctx))
    check("T7c: after idle expiry message is is_staff_group=True",
          bot.process_message.call_args.kwargs.get("is_staff_group") is True)

    # ─── Test 8: END via hard expiry regardless of activity ───
    ctx = make_ctx()
    tap(ctx, "Which milk type?")
    bot.process_message = AsyncMock(return_value=("Which size?", []))
    for _ in range(4):  # keep it busy: 4 x 70s = 280s, idle always fresh
        clock.advance(70)
        run(bot._handle_message_inner(make_msg_update("Full cream", reply_to_bot=True), ctx))
    s = ctx.chat_data.get("staff_override_session")
    check("T8a: busy session still alive at 280s (idle kept fresh)", s is not None
          and bot._get_active_override_session(ctx) is not None)
    hard = s["hard_expires_at"]
    check("T8b: idle is fresh (activity)", s["idle_expires_at"] > hard - 300 + 280)
    clock.t = hard + 1
    check("T8c: past hard_expires_at -> not active despite fresh idle",
          bot._get_active_override_session(ctx) is None)
    ctx = make_ctx()
    tap(ctx, "Which milk type?")
    clock.advance(301)
    ctx.chat_data["staff_override_session"]["idle_expires_at"] = clock.t + 999
    check("T8d: helper closes on hard expiry",
          bot._maybe_close_override_session(ctx, "Which size?", "x") == "expired"
          and "staff_override_session" not in ctx.chat_data)

    # ─── Test 10: new tap replaces stale session ───
    ctx = make_ctx()
    tap(ctx, "Which milk type?")
    ctx.chat_data["staff_override_session"].update(
        original_query="OLD QUERY", started_at=1.0, hard_expires_at=2.0, idle_expires_at=2.0)
    check("T10a: stale session inactive", bot._get_active_override_session(ctx) is None)
    ctx.chat_data["staff_override_session"] = {
        "owner_user_id": 1, "started_at": 1.0, "hard_expires_at": 2.0,
        "idle_expires_at": 2.0, "original_query": "OLD QUERY", "closed": False}
    tap(ctx, "Which milk type?")
    s = ctx.chat_data["staff_override_session"]
    check("T10b: replaced with fresh session",
          s["original_query"] == QUERY and s["owner_user_id"] == OWNER_ID
          and s["hard_expires_at"] == clock.t + 300 and s["closed"] is False)
    # Live (non-stale) session is replaced too, with fresh timers
    clock.advance(50)
    tap(ctx, "Which milk type?")
    check("T10c: second tap restarts timers",
          ctx.chat_data["staff_override_session"]["started_at"] == clock.t)

# ─── Config sanity ───
check("C1: end phrases contain the required set",
      {"done", "thanks", "thank you", "close", "no more", "ok done", "that's all", "nothing else"}
      <= config.STAFF_OVERRIDE_END_PHRASES)

# ─── Test 9: regression — existing button-flow suites ───
for suite in ("test_staff_override.py", "test_staff_group.py"):
    r = subprocess.run([sys.executable, os.path.join(_HERE, suite)],
                       capture_output=True, text=True, cwd=_HERE)
    check(f"T9: {suite} still passes (exit {r.returncode})", r.returncode == 0)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
