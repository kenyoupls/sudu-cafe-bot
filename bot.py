"""
Sudu Café Bot: the café's back-office helper on Telegram.

Staff group:   receipt photos, POS sales photos, stock counts, shopping list,
               recipes / checklists / opening hours. Money questions are refused
               ("Check with the boss"), with a 🔓 Show anyway button only owners can use.
Owners chat:   all of the above + spending, sales, /payback.
Everything is saved to the Google Sheet (see google_integration.py).
The agreed rules are listed in RULES.md.

Run:  python bot.py
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import string
from datetime import datetime, time as dtime
from functools import wraps

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, PicklePersistence, filters)

import ai_chat as ai
import config
import google_integration as sheet
import storage
from storage import esc, rm

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("sudu")

HTML = ParseMode.HTML
OWNERS_ONLY = "🔒 That's for the owners. Ask in the owners chat."
YES = {"yes", "y", "ya", "yep", "ok", "okay", "regular", "stock", "add", "sure", "betul", "boleh", "save", "confirm"}
NO = {"no", "n", "nope", "skip", "not", "tak", "tidak", "dont", "don't", "nah"}
CANCEL = {"cancel", "batal", "never mind", "nevermind", "forget it", "delete this", "wrong photo"}
END_OVERRIDE = {"done", "thanks", "thank you", "close", "no more", "ok done", "that's all", "nothing else", "end"}
OVERRIDE_HARD, OVERRIDE_IDLE = 15 * 60, 5 * 60
MEMORY_DAYS, MEMORY_MAX = 3, 60


# ─── Who may do what ──────────────────────────────────────

def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in config.OWNER_USER_IDS)


def chat_allowed(update: Update) -> bool:
    chat = update.effective_chat
    if not chat:
        return False
    if chat.type == ChatType.PRIVATE:
        return is_owner(update)
    return chat.id in (config.OWNER_GROUP_ID, config.STAFF_GROUP_ID) and chat.id != 0


def override_active(ctx) -> dict | None:
    """An owner tapped 🔓 Show anyway in this chat in the last few minutes."""
    o = (getattr(ctx, "chat_data", None) or {}).get("override")
    now = storage.now().timestamp()
    if o and now < o["hard"] and now < o["idle"]:
        return o
    if o:
        ctx.chat_data.pop("override", None)
    return None


def money_allowed(update: Update, ctx=None) -> bool:
    """Owners chat, owners' private chat, or an owner-unlocked session in the staff group."""
    chat = update.effective_chat
    if not chat:
        return False
    if chat.type == ChatType.PRIVATE:
        return is_owner(update)
    if config.OWNER_GROUP_ID and chat.id == config.OWNER_GROUP_ID != config.STAFF_GROUP_ID:
        return True
    if config.OWNER_GROUP_ID == config.STAFF_GROUP_ID and chat.id == config.OWNER_GROUP_ID:
        return is_owner(update)  # one shared group: owners only
    return bool(ctx is not None and override_active(ctx))


def who(update: Update) -> str:
    u = update.effective_user
    return (u.first_name or u.full_name or u.username or str(u.id)) if u else "Someone"


def guarded(handler):
    """Ignore chats the bot doesn't belong to; turn common failures into plain messages."""
    @wraps(handler)
    async def run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not chat_allowed(update):
            if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE and update.message:
                await update.message.reply_text(f"Hi! I'm the private helper of {config.CAFE_NAME} café.")
            return
        try:
            await handler(update, ctx)
        except sheet.SheetLayoutError as e:
            await reply(update, f"⚠️ {esc(e)}")
        except sheet.SheetNotConfigured:
            await reply(update, "⚠️ The Google Sheet isn't connected (credentials.json missing).")
        except ai.AIUnavailable:
            await reply(update, "⏳ The AI is busy or out of free requests right now. Try again in a minute.")
        except Exception:
            log.exception("Handler %s failed", handler.__name__)
            await reply(update, "😕 Something went wrong. Please try again; if it keeps happening, tell the owners.")
    return run


async def reply(update: Update, text: str, **kw):
    """Reply in HTML (split if long). Returns the last message sent."""
    msg, sent = update.effective_message, None
    if msg:
        for chunk in _chunks(text):
            sent = await msg.reply_text(chunk, parse_mode=HTML, disable_web_page_preview=True, **kw)
            kw.pop("reply_markup", None)
    return sent


def _chunks(text: str, size: int = 3900) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    return out + [cur] if cur.strip() else out


def run_sync(fn, *args, **kw):
    return asyncio.to_thread(fn, *args, **kw)


def new_id(prefix: str) -> str:
    return f"{prefix}-{storage.now():%y%m%d-%H%M}-" + "".join(random.choices(string.ascii_uppercase, k=3))


def pending(ctx, kind: str) -> dict:
    """Things waiting for an answer. Kept in data/state.pickle: they survive restarts and never expire."""
    return ctx.bot_data.setdefault(kind, {})


def _trim(store: dict, keep: int = 60):
    for key in list(store)[:-keep]:
        store.pop(key, None)


def msg_link(chat_id: int, message_id: int) -> str:
    s = str(chat_id)
    return f"https://t.me/c/{s[4:]}/{message_id}" if s.startswith("-100") else ""


def words(text: str) -> str:
    return re.sub(r"[^a-z' ]+", " ", (text or "").lower()).strip()


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows if r])


# ─── Short memory per chat (so follow-up questions make sense) ──

def remember(ctx, speaker: str, text: str):
    """Each chat keeps its own last 3 days, so owners-chat talk never reaches the staff group.
    Refusals are never stored, so the AI doesn't start repeating them."""
    if ctx is None or getattr(ctx, "chat_data", None) is None or not text:
        return
    mem = ctx.chat_data.setdefault("memory", [])
    mem.append({"t": storage.now().timestamp(), "who": speaker, "text": text[:500]})
    cutoff = storage.now().timestamp() - MEMORY_DAYS * 86400
    ctx.chat_data["memory"] = [m for m in mem if m["t"] >= cutoff][-MEMORY_MAX:]


def memory_text(ctx, last: int = 30) -> str:
    mem = (ctx.chat_data or {}).get("memory", [])[-last:]
    return "\n".join(f"[{datetime.fromtimestamp(m['t'], storage.TZ):%a %H:%M}] {m['who']}: {m['text']}" for m in mem)


# ─── Help ─────────────────────────────────────────────────

HELP_STAFF = """👋 <b>Sudu helper</b>

🧾 <b>Receipt</b>: send a photo. Check it, then tap ✅ Save.
   Wrong? Reply to my message in your own words, or e.g. <code>2 qty 12</code> · <code>paid by Ali</code> · <code>delete 3</code>
📊 <b>Daily sales</b>: send the POS closing report photo.
📦 <b>Stock</b>: /stock shows what's low · /count shows everything
   Update: <code>stock full cream milk 120, condensed 17</code> or paste the whole checklist
🛒 <b>Shopping</b>: /buy shows the list · <code>buy ice, straws</code> adds items
🕐 <b>Hours</b>: /hours (opening hours, live from Google)
📖 <b>Ask</b>: /recipe cendol · /checklist opening · or tag me with any question"""

HELP_OWNER = """

🔒 <b>Owners</b>
/spend: this month's spending by category and shop
/sales: this month's sales
/payback: who paid for supplies and how much
(add a month: <code>/spend oct</code>, <code>/payback last</code>)"""


@guarded
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, HELP_STAFF + (HELP_OWNER if money_allowed(update, ctx) or is_owner(update) else ""))


async def cmd_chatid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Setup helper: works anywhere so owners can find IDs for .env."""
    await update.effective_message.reply_text(f"This chat's ID: {update.effective_chat.id}")


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Your Telegram user ID: {update.effective_user.id}")


# ─── Money refusal + owner override ───────────────────────

async def refuse(update: Update, ctx, question: str = "", cmd: str = "", args=None):
    """'Check with the boss' with a 🔓 Show anyway button that only owners can use."""
    oid = new_id("O")
    pending(ctx, "override_q")[oid] = {"chat_id": update.effective_chat.id, "text": question, "cmd": cmd,
                                       "args": list(args or []), "asker": who(update)}
    _trim(pending(ctx, "override_q"), 20)
    await reply(update, esc(storage.REFUSAL),
                reply_markup=kb([Btn("🔓 Show anyway (owner only)", callback_data=f"ov:{oid}")]))


@guarded
async def on_override_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(update):
        await q.answer()  # non-owner tap: silently nothing
        return
    p = pending(ctx, "override_q").pop(q.data.split(":", 1)[1], None)
    if not p:
        await q.answer("Already shown.")
        return
    now = storage.now().timestamp()
    ctx.chat_data["override"] = {"by": who(update), "hard": now + OVERRIDE_HARD, "idle": now + OVERRIDE_IDLE}
    await q.answer("Owner mode on for 15 min")
    await q.edit_message_text(f"{esc(storage.REFUSAL)}\n✅ Overridden by {esc(who(update))}", parse_mode=HTML)
    if p["cmd"] in OWNER_COMMANDS:
        ctx.args = p["args"]
        await OWNER_COMMANDS[p["cmd"]](update, ctx)
    elif p["text"]:
        await _answer(update, p["text"], ctx)
    await reply(update, "<i>🔓 Owner mode is on here for up to 15 minutes. Say <code>done</code> to close it.</i>")


def owners_only(name: str):
    def deco(handler):
        @wraps(handler)
        async def run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if money_allowed(update, ctx):
                await handler(update, ctx)
            elif update.effective_chat.type != ChatType.PRIVATE:
                await refuse(update, ctx, cmd=name, args=ctx.args)
            else:
                await reply(update, OWNERS_ONLY)
        return run
    return deco


# ─── Photos: receipts and sales reports ───────────────────

def _bot_mentioned(update: Update, ctx) -> bool:
    msg = update.effective_message
    text = (msg.caption or msg.text or "").lower()
    if ctx.bot.username and f"@{ctx.bot.username.lower()}" in text:
        return True
    return bool(msg.reply_to_message and msg.reply_to_message.from_user
                and msg.reply_to_message.from_user.id == ctx.bot.id)


@guarded
async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    image = bytes(await (await msg.photo[-1].get_file()).download_as_bytearray())
    caption = msg.caption or ""
    in_group = update.effective_chat.type != ChatType.PRIVATE

    status = await msg.reply_text("👀 Reading…")
    try:
        result = await ai.read_photo(image, caption)
    except ai.AIUnavailable:
        await status.edit_text("⏳ The AI is busy or out of free requests right now. Please resend the photo in a minute.")
        return

    if result["kind"] == "receipt" and result["receipt"]:
        r = storage.normalize_receipt(result["receipt"], who(update))
        check = storage.check_receipt(r)
        if not check["ok"]:  # second, careful read with the stronger model
            await status.edit_text("🔎 Numbers don't add up yet, taking a closer look…")
            second = await ai.reread_receipt(image, caption, check["reason"])
            if second:
                r2 = storage.normalize_receipt(second, who(update))
                c2 = storage.check_receipt(r2)
                if c2["ok"] or (c2["diff"] is not None and (check["diff"] is None or abs(c2["diff"]) < abs(check["diff"]))):
                    r = r2
        link = msg_link(msg.chat_id, msg.message_id) or "Telegram photo"
        await present_receipt(update, ctx, r, link, hashlib.sha256(image).hexdigest()[:16], status)
    elif result["kind"] == "sales_report" and result["sales"]:
        await _show_sales(update, ctx, status, result["sales"])
    elif in_group and not _bot_mentioned(update, ctx):
        await status.delete()  # just a normal photo in the group: stay quiet
    else:
        await status.edit_text("🤔 That doesn't look like a receipt or a sales report. "
                               "Try a clearer, straight-on photo with the whole receipt in view.")


def receipt_buttons(rid: str, dup: bool = False):
    return kb([Btn("Save anyway" if dup else "✅ Save", callback_data=f"rs:{rid}"),
               Btn("❌ Cancel", callback_data=f"rx:{rid}")])


async def present_receipt(update: Update, ctx, r: dict, photo: str, photo_hash: str, status=None):
    """Show a receipt (from a photo or typed in) with ✅ Save / ❌ Cancel."""
    rid = new_id("R")
    check = storage.check_receipt(r)
    dup = await run_sync(sheet.find_duplicate, photo_hash, storage.receipt_key(r))
    text = storage.receipt_text(r, check, rid)
    if dup:
        text = f"⚠️ <b>Looks already saved</b> as {esc(dup)}.\n\n" + text
    if status:
        await status.edit_text(text, parse_mode=HTML, reply_markup=receipt_buttons(rid, bool(dup)))
        sent = status
    else:
        sent = await reply(update, text, reply_markup=receipt_buttons(rid, bool(dup)))
    pending(ctx, "receipts")[rid] = {"r": r, "msg_id": sent.message_id, "chat_id": sent.chat_id, "by": who(update),
                                     "photo": photo, "hash": photo_hash, "created": storage.stamp(),
                                     "dup": bool(dup)}
    _trim(pending(ctx, "receipts"))


async def save_receipt(ctx, rid: str, by: str) -> str:
    """The one place a receipt gets saved (used by the button and by a typed 'save')."""
    p = pending(ctx, "receipts").get(rid)
    if not p:
        return "This receipt was already handled."
    r = p["r"]
    await run_sync(sheet.save_receipt, rid, r, p["photo"], p["by"], p["hash"])
    pending(ctx, "receipts").pop(rid, None)
    note = await _tick_shopping(r, by)
    return (f"✅ <b>Saved</b> {esc(rid)}\n{esc(r['supplier'])} · {esc(r['date'])} · <b>{rm(r['total'])}</b>\n"
            f"{len(r['items'])} items · paid by {esc(r['paid_by'])} · saved by {esc(by)}{note}")


@guarded
async def on_receipt_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action, rid = q.data.split(":", 1)
    if rid not in pending(ctx, "receipts"):
        await q.answer("This receipt was already handled.", show_alert=True)
        await q.edit_message_reply_markup(None)
        return
    if action == "rx":
        pending(ctx, "receipts").pop(rid, None)
        await q.answer("Cancelled")
        await q.edit_message_text("❌ Cancelled, nothing was saved.")
        return
    await q.answer("Saving…")
    try:
        await q.edit_message_text(await save_receipt(ctx, rid, who(update)), parse_mode=HTML)
    except Exception as e:
        log.exception("save receipt")
        await q.message.reply_text(f"⚠️ Couldn't save to the Sheet: {str(e)[:150]}\nTap ✅ Save again in a minute.")


async def _tick_shopping(r: dict, by: str) -> str:
    """Mark shopping-list items as bought when they appear on a saved receipt."""
    try:
        open_items = await run_sync(sheet.shopping_open)
        names = [row["Item"] for _, row in open_items]
        ticked = []
        for it in r["items"]:
            hit = storage.match_name(it["name"], names, cutoff=0.75)
            if hit and hit not in ticked:
                row = next(i for i, rec in open_items if rec["Item"] == hit)
                await run_sync(sheet.shopping_done, row, by)
                ticked.append(hit)
        return f"\n🛒 Ticked off the shopping list: {esc(', '.join(ticked))}" if ticked else ""
    except Exception:
        log.exception("tick shopping")
        return ""


async def _edit_receipt(update: Update, ctx, rid: str, p: dict):
    text = update.effective_message.text or ""
    r = p["r"]
    edits = storage.parse_edits(text)
    action = "edit"
    if words(text) in CANCEL:
        action = "cancel"
    elif words(text) in YES:
        action = "save"
    elif not edits or any(e["op"] == "unknown" for e in edits):
        action, edits = await ai.understand_receipt_reply(r, text)  # read it like a human
    if action == "cancel":
        pending(ctx, "receipts").pop(rid, None)
        await ctx.bot.edit_message_text("❌ Cancelled, nothing was saved.", chat_id=p["chat_id"], message_id=p["msg_id"])
        await reply(update, "OK, cancelled. Nothing was saved.")
        return
    if action == "save":
        done = await save_receipt(ctx, rid, who(update))
        await ctx.bot.edit_message_text(done, chat_id=p["chat_id"], message_id=p["msg_id"], parse_mode=HTML)
        return
    if action == "unclear" or not edits:
        await reply(update, "🤔 I'm not sure what to change. Try e.g. <code>2 qty 12</code> · <code>2 price 7.29</code> · "
                            "<code>delete 3</code> · <code>date 12/11</code> · <code>paid by Ali</code>, or say <code>cancel</code>.")
        return
    problems = storage.apply_edits(r, edits)
    try:
        await ctx.bot.edit_message_text(storage.receipt_text(r, storage.check_receipt(r), rid), chat_id=p["chat_id"],
                                        message_id=p["msg_id"], parse_mode=HTML, reply_markup=receipt_buttons(rid))
    except Exception as e:  # "message is not modified" when nothing changed
        log.info("edit receipt message: %s", e)
    await reply(update, ("⚠️ " + esc("; ".join(problems))) if problems else "✏️ Updated. Check it above and tap ✅ Save.")


# ─── Daily sales ──────────────────────────────────────────

SALES_FIELDS = {"date": "date", "total": "total_sales", "sales": "total_sales", "bills": "bills", "pax": "pax",
                "discount": "discount", "void": "void", "refund": "refund", "cashier": "cashier"}
PAY_WORDS = ["cash", "card", "duitnow", "tng", "grab"]


def sales_text(s: dict, sid: str) -> str:
    lines = [f"📊 <b>Daily sales</b> · {esc(s['date'])}", "",
             f"💰 <b>Total: {rm(s.get('total_sales'))}</b>",
             f"🧾 Bills: {s.get('bills') or '-'}   👥 Pax: {s.get('pax') or '-'}"]
    for p in s.get("payments") or []:
        lines.append(f"   • {esc(p.get('method'))}: {rm(p.get('amount'))}")
    for k in ("discount", "void", "refund"):
        if storage.to_number(s.get(k)):
            lines.append(f"{k.capitalize()}: {rm(s.get(k))}")
    if s.get("cashier"):
        lines.append(f"Cashier: {esc(s['cashier'])}")
    lines.append(f"\n<i>To fix, reply e.g. <code>total 1234.50</code>, <code>date 12/11</code>, "
                 f"<code>cash 300</code>. Ref {esc(sid)}</i>")
    return "\n".join(lines)


def sales_buttons(sid: str, replace: bool = False):
    return kb([Btn("♻️ Replace" if replace else "✅ Save", callback_data=f"ss:{sid}"),
               Btn("❌ Cancel", callback_data=f"sx:{sid}")])


async def _show_sales(update, ctx, status, raw: dict):
    s = dict(raw)
    s["date"] = storage.parse_date(s.get("date")) or storage.today()
    sid = new_id("S")
    existing = await run_sync(sheet.sales_row_for, s["date"])
    text = sales_text(s, sid)
    if existing:
        text = f"⚠️ Sales for {esc(s['date'])} are already saved. Saving will <b>replace</b> them.\n\n" + text
    await status.edit_text(text, parse_mode=HTML, reply_markup=sales_buttons(sid, bool(existing)))
    pending(ctx, "sales")[sid] = {"s": s, "msg_id": status.message_id, "chat_id": status.chat_id, "by": who(update),
                                  "created": storage.stamp()}
    _trim(pending(ctx, "sales"))


async def save_sales(ctx, sid: str) -> str:
    p = pending(ctx, "sales").get(sid)
    if not p:
        return "Already handled."
    s = p["s"]
    row = await run_sync(sheet.sales_row_for, s["date"])
    await run_sync(sheet.save_sales, s, p["by"], row)
    pending(ctx, "sales").pop(sid, None)
    return f"✅ <b>Daily sales saved</b> · {esc(s['date'])} · {rm(s.get('total_sales'))}{' (replaced)' if row else ''}"


@guarded
async def on_sales_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action, sid = q.data.split(":", 1)
    if sid not in pending(ctx, "sales"):
        await q.answer("Already handled.", show_alert=True)
        await q.edit_message_reply_markup(None)
        return
    if action == "sx":
        pending(ctx, "sales").pop(sid, None)
        await q.answer("Cancelled")
        await q.edit_message_text("❌ Cancelled, nothing was saved.")
        return
    await q.answer("Saving…")
    try:
        await q.edit_message_text(await save_sales(ctx, sid), parse_mode=HTML)
    except Exception as e:
        await q.message.reply_text(f"⚠️ Couldn't save to the Sheet: {str(e)[:150]}")


async def _edit_sales(update: Update, ctx, sid: str, p: dict):
    text = update.effective_message.text or ""
    if words(text) in CANCEL:
        pending(ctx, "sales").pop(sid, None)
        await ctx.bot.edit_message_text("❌ Cancelled, nothing was saved.", chat_id=p["chat_id"], message_id=p["msg_id"])
        return
    if words(text) in YES:
        await ctx.bot.edit_message_text(await save_sales(ctx, sid), chat_id=p["chat_id"], message_id=p["msg_id"],
                                        parse_mode=HTML)
        return
    s, bad = p["s"], []
    for part in re.split(r"[\n;,]+", text):
        m = re.match(r"^\s*([a-z ]+?)\s*[:=]?\s*(.+?)\s*$", part.lower())
        if not m:
            continue
        word, value = m.group(1).strip(), m.group(2)
        if word in PAY_WORDS and storage.to_number(value) is not None:
            pays = [x for x in s.get("payments") or [] if storage.name_key(x.get("method")) != word]
            s["payments"] = pays + [{"method": word.capitalize(), "amount": storage.to_number(value)}]
        elif word == "date" and storage.parse_date(value):
            s["date"] = storage.parse_date(value)
        elif word in SALES_FIELDS and word not in ("date", "cashier") and storage.to_number(value) is not None:
            s[SALES_FIELDS[word]] = storage.to_number(value)
        elif word == "cashier":
            s["cashier"] = storage.clean_name(value)
        else:
            bad.append(part.strip())
    await ctx.bot.edit_message_text(sales_text(s, sid), chat_id=p["chat_id"], message_id=p["msg_id"],
                                    parse_mode=HTML, reply_markup=sales_buttons(sid))
    await reply(update, ("⚠️ Didn't understand: " + esc("; ".join(bad))) if bad else "✏️ Updated. Tap ✅ Save.")


# ─── Stock ────────────────────────────────────────────────

def _stock_line(r: dict) -> str:
    unit = f" {r['Unit']}" if r.get("Unit") else ""
    mn = f" (min {storage.qty_text(r['Min'])})" if storage.to_number(r.get("Min")) is not None else ""
    cur = storage.qty_text(r.get("Current")) if r.get("Current") != "" else "?"
    return f"• {esc(r['Item'])}: <b>{cur}</b>{esc(unit)}{mn}"


@guarded
async def cmd_stock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args or []).strip()
    if text:
        await stock_update(update, ctx, text)
        return
    rows = await run_sync(sheet.stock)
    out = [r for r in rows if storage.stock_status(r.get("Current"), r.get("Min")) == "out"]
    low = [r for r in rows if storage.stock_status(r.get("Current"), r.get("Min")) == "low"]
    lines = ["📦 <b>Stock</b>"]
    if out:
        lines += ["", "🔴 <b>Out</b>"] + [_stock_line(r) for r in out]
    if low:
        lines += ["", "🟡 <b>Low</b>"] + [_stock_line(r) for r in low]
    if not out and not low:
        lines.append("\n✅ Nothing is low.")
    lines.append("\n/count shows every item. Update with e.g. <code>stock full cream milk 120, condensed 17</code>")
    await reply(update, "\n".join(lines))


@guarded
async def cmd_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = await run_sync(sheet.stock)
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r.get("Location") or "Other", []).append(r)
    lines = ["📋 <b>Stock count</b>: reply with new numbers, e.g. <code>stock milk 120, cups 400</code>"]
    for loc, items in groups.items():
        lines += ["", f"<b>{esc(loc)}</b>"] + [_stock_line(r) for r in items]
    await reply(update, "\n".join(lines))


def stock_step(p: dict, current: dict) -> tuple[str, InlineKeyboardMarkup]:
    """The stock preview, one question at a time: blanks → new items → confirm."""
    sid = p["id"]
    known = [u for u in p["updates"] if not u["new"]]
    new = [u for u in p["updates"] if u["new"]]
    lines = ["📦 <b>Stock update</b>", ""]
    for u in known:
        old = current.get(u["item"], "")
        lines.append(f"• {esc(u['item'])}: {storage.qty_text(old) if old != '' else '?'} → <b>{storage.qty_text(u['qty'])}</b>")
    if new and p.get("add_new") is not None:
        verb = "➕ will be added" if p["add_new"] else "⏭ skipped"
        lines += [f"• 🆕 {esc(u['item'])}: <b>{storage.qty_text(u['qty'])}</b> ({verb})" for u in new]
    if p["invalid"]:
        lines.append("\n⚠️ Ignored (needs a number or OUT): " + esc(", ".join(f"{n}: {w}" for n, w in p["invalid"])))
    if p["blanks"]:
        lines += ["", "❓ <b>These items were left blank:</b> " + esc(", ".join(p["blanks"])),
                  "Reply <code>zero</code>, <code>skip</code>, or the counts (e.g. <code>salt 3</code>)."]
        return "\n".join(lines), kb([Btn("0️⃣ They're 0", callback_data=f"kz:{sid}"),
                                     Btn("⏭ Skip them", callback_data=f"kb:{sid}")],
                                    [Btn("❌ Cancel", callback_data=f"kx:{sid}")])
    if new and p.get("add_new") is None:
        lines += ["", "🆕 <b>Not on the stock list yet:</b> " +
                  esc(", ".join(f"{u['item']} ({storage.qty_text(u['qty'])})" for u in new)),
                  "Is this a regular stock item?"]
        return "\n".join(lines), kb([Btn("✅ Yes, add", callback_data=f"ky:{sid}"),
                                     Btn("⏭ No, skip", callback_data=f"kn:{sid}")],
                                    [Btn("❌ Cancel", callback_data=f"kx:{sid}")])
    if not known and not (new and p.get("add_new")):
        lines.append("Nothing left to update.")
        return "\n".join(lines), kb([Btn("❌ Close", callback_data=f"kx:{sid}")])
    return "\n".join(lines), kb([Btn("✅ Update", callback_data=f"ks:{sid}"), Btn("❌ Cancel", callback_data=f"kx:{sid}")])


async def stock_update(update: Update, ctx, text: str):
    rows = await run_sync(sheet.stock)
    known = [r["Item"] for r in rows]
    reading = await ai.read_stock(text, known)
    if not reading["updates"] and not reading["blanks"] and not reading["invalid"]:
        await reply(update, "🤔 I couldn't find any item + number. Example: <code>stock full cream milk 120</code>")
        return
    sid = new_id("K")
    p = {"id": sid, **reading, "by": who(update), "chat_id": update.effective_chat.id, "add_new": None,
         "created": storage.stamp()}
    text_, buttons = stock_step(p, {r["Item"]: r.get("Current") for r in rows})
    sent = await reply(update, text_, reply_markup=buttons)
    p["msg_id"] = sent.message_id
    pending(ctx, "stock")[sid] = p
    _trim(pending(ctx, "stock"))


async def _refresh_stock_msg(ctx, p: dict):
    rows = await run_sync(sheet.stock)
    text, buttons = stock_step(p, {r["Item"]: r.get("Current") for r in rows})
    try:
        await ctx.bot.edit_message_text(text, chat_id=p["chat_id"], message_id=p["msg_id"], parse_mode=HTML,
                                        reply_markup=buttons)
    except Exception as e:
        log.info("refresh stock msg: %s", e)


async def apply_stock(update: Update, ctx, sid: str) -> str:
    p = pending(ctx, "stock").pop(sid)
    by = who(update)
    known = [u for u in p["updates"] if not u["new"]]
    new = [u for u in p["updates"] if u["new"]] if p.get("add_new") else []
    done, unknown = await run_sync(sheet.update_stock, known, by)
    for u in unknown + new:
        await run_sync(sheet.add_stock_item, u["item"], u["qty"], by)
    rows = await run_sync(sheet.stock)
    changed = {storage.name_key(u["item"]) for u in known + new}
    need = [r["Item"] for r in rows if storage.name_key(r["Item"]) in changed
            and storage.stock_status(r.get("Current"), r.get("Min")) in ("low", "out")]
    added = await run_sync(sheet.shopping_add, need, "Stock check", "low stock") if need else []
    text = f"✅ <b>Stock updated</b>: {len(done)} item(s)" + (f", {len(unknown) + len(new)} new" if unknown or new else "")
    text += "\n" + "\n".join(f"• {esc(u['item'])}: {storage.qty_text(u['qty'])}" for u in known + new)
    if added:
        text += f"\n🛒 Added to shopping list: {esc(', '.join(added))}"
    text += "\n<i>Wrong number? Reply to this message, e.g. <code>actually 92</code>.</i>"
    # remember what this message updated, so 'actually 92' can fix it later
    pending(ctx, "stock_done")[str(p["msg_id"])] = {"chat_id": p["chat_id"], "items": [u["item"] for u in known + new]}
    _trim(pending(ctx, "stock_done"), 30)
    if new:  # newly added items: were they bought?
        eid = new_id("E")
        names = ", ".join(f"{u['item']} ({storage.qty_text(u['qty'])})" for u in new)
        sent = await reply(update, f"🧾 Was <b>{esc(names)}</b> just bought? Add to expenses?\n"
                                   f"Reply to this message with the details, e.g. <code>yes RM15 Lotus paid by me</code>.",
                           reply_markup=kb([Btn("No, not bought", callback_data=f"ex:{eid}")]))
        pending(ctx, "expense")[eid] = {"items": [{"name": u["item"], "qty": u["qty"]} for u in new], "by": by,
                                        "chat_id": sent.chat_id, "msg_id": sent.message_id, "created": storage.stamp()}
        _trim(pending(ctx, "expense"))
    return text


@guarded
async def on_stock_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action, sid = q.data.split(":", 1)
    p = pending(ctx, "stock").get(sid)
    if not p:
        await q.answer("Already handled.", show_alert=True)
        await q.edit_message_reply_markup(None)
        return
    if action == "kx":
        pending(ctx, "stock").pop(sid, None)
        await q.answer("Cancelled")
        await q.edit_message_text("❌ Cancelled, stock not changed.")
        return
    if action == "ks":
        await q.answer("Updating…")
        await q.edit_message_text(await apply_stock(update, ctx, sid), parse_mode=HTML)
        return
    if action == "kz":
        p["updates"] += [{"typed": b, "item": b, "qty": 0.0, "new": False} for b in p["blanks"]]
        p["blanks"] = []
    elif action == "kb":
        p["blanks"] = []
    elif action in ("ky", "kn"):
        p["add_new"] = action == "ky"
    await q.answer()
    await _refresh_stock_msg(ctx, p)


async def _reply_to_stock(update: Update, ctx, sid: str, p: dict):
    """Typed answers to a stock preview: zero / skip / yes / no / cancel / more counts."""
    text = update.effective_message.text or ""
    w = words(text)
    if w in CANCEL:
        pending(ctx, "stock").pop(sid, None)
        await ctx.bot.edit_message_text("❌ Cancelled, stock not changed.", chat_id=p["chat_id"], message_id=p["msg_id"])
        return
    if p["blanks"]:
        if w in ("zero", "0", "all zero", "all 0", "kosong"):
            p["updates"] += [{"typed": b, "item": b, "qty": 0.0, "new": False} for b in p["blanks"]]
            p["blanks"] = []
        elif w in NO or w == "skip":
            p["blanks"] = []
        else:
            counts = storage.parse_stock_sheet(text)["items"]
            for name, qty in counts:
                hit = storage.match_name(name, p["blanks"])
                if hit:
                    p["updates"].append({"typed": name, "item": hit, "qty": qty, "new": False})
                    p["blanks"].remove(hit)
    elif any(u["new"] for u in p["updates"]) and p.get("add_new") is None and (w in YES or w in NO):
        p["add_new"] = w in YES
    elif w in YES:
        await ctx.bot.edit_message_text(await apply_stock(update, ctx, sid), chat_id=p["chat_id"],
                                        message_id=p["msg_id"], parse_mode=HTML)
        return
    else:  # corrections like 'milk 92'
        for name, qty in storage.parse_stock_sheet(text)["items"]:
            hit = storage.match_name(name, [u["item"] for u in p["updates"]])
            if hit:
                for u in p["updates"]:
                    if u["item"] == hit:
                        u["qty"] = qty
    await _refresh_stock_msg(ctx, p)


async def _reply_to_stock_done(update: Update, ctx, done: dict):
    """'actually 92' in reply to a 'Stock updated' message."""
    text = update.effective_message.text or ""
    counts = storage.parse_stock_sheet(text)["items"]
    if counts and not (len(counts) == 1 and storage.name_key(counts[0][0]) in ("actually", "sorry", "no", "it s", "its")):
        await stock_update(update, ctx, text)
        return
    num = re.search(r"\d+(?:\.\d+)?", text)
    if num and len(done["items"]) == 1:
        await stock_update(update, ctx, f"{done['items'][0]}: {num.group(0)}")
    elif num:
        await reply(update, "Which item? e.g. <code>" + esc(done["items"][0]) + f" {num.group(0)}</code>")


# ─── Expenses for newly added items ───────────────────────

@guarded
async def on_expense_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    pending(ctx, "expense").pop(q.data.split(":", 1)[1], None)
    await q.answer("OK")
    await q.edit_message_text("👍 OK, not added to expenses.")


async def _reply_to_expense(update: Update, ctx, eid: str, p: dict):
    raw = await ai.read_expense(update.effective_message.text or "", p["items"], who(update))
    if raw is None:
        pending(ctx, "expense").pop(eid, None)
        await ctx.bot.edit_message_text("👍 OK, not added to expenses.", chat_id=p["chat_id"], message_id=p["msg_id"])
        return
    if raw.get("unclear"):
        await reply(update, "Was it bought? Reply e.g. <code>yes RM15 Lotus paid by me</code>, or <code>no</code>.")
        return
    pending(ctx, "expense").pop(eid, None)
    await ctx.bot.edit_message_text("🧾 Got it, check the expense below and tap ✅ Save.",
                                    chat_id=p["chat_id"], message_id=p["msg_id"])
    r = storage.normalize_receipt(raw, who(update))
    await present_receipt(update, ctx, r, f"typed by {who(update)}", "")


# ─── Shopping list ────────────────────────────────────────

def shopping_buttons(items):
    return kb(*[[Btn(f"✅ {r['Item'][:40]}" + (f" ({r['Note'][:20]})" if r.get("Note") else ""),
                     callback_data=f"bd:{i}")] for i, r in items[:40]])


@guarded
async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        await _shopping_add(update, " ".join(ctx.args))
        return
    items = await run_sync(sheet.shopping_open)
    if not items:
        await reply(update, "🛒 The shopping list is empty. Add with <code>buy ice, straws</code>")
        return
    await reply(update, f"🛒 <b>Shopping list</b> ({len(items)})\nTap an item when it's bought.",
                reply_markup=shopping_buttons(items))


async def _shopping_add(update: Update, text: str):
    names = [storage.clean_name(x) for x in re.split(r"[\n,;]+", text) if storage.clean_name(x)]
    added = await run_sync(sheet.shopping_add, names, who(update))
    skipped = len(names) - len(added)
    msg = f"🛒 Added: {esc(', '.join(added))}" if added else "🛒 Nothing new to add."
    if skipped:
        msg += f"\n({skipped} already on the list)"
    await reply(update, msg)


@guarded
async def on_buy_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    item = await run_sync(sheet.shopping_done, int(q.data.split(":", 1)[1]), who(update))
    await q.answer(f"✅ {item} bought")
    items = await run_sync(sheet.shopping_open)
    await q.edit_message_text(f"🛒 <b>Shopping list</b> ({len(items)})\n✔️ {esc(item)} bought by {esc(who(update))}",
                              parse_mode=HTML, reply_markup=shopping_buttons(items) if items else None)


# ─── Recipes, checklists, hours, questions ────────────────

@guarded
async def cmd_recipe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    want = storage.name_key(" ".join(ctx.args or []))
    bingsu = await run_sync(sheet.read_tab, "Bingsu Recipes")
    other = await run_sync(sheet.read_tab, "Other Recipes")
    names = sorted({r[0] for r in bingsu[1:] if r} | {r[1] for r in other[1:] if len(r) > 1})
    if not want:
        await reply(update, "📖 Recipes: " + esc(", ".join(names)) + "\nExample: <code>/recipe cendol</code>")
        return
    hit = storage.match_name(want, names, cutoff=0.6)
    if not hit:
        await reply(update, f"🤔 No recipe called “{esc(want)}”. Try: " + esc(", ".join(names[:30])))
        return
    lines, batch, method = [f"📖 <b>{esc(hit)}</b>"], None, []
    for r in bingsu[1:]:
        if r and r[0] == hit:
            if r[1] != batch:
                batch = r[1]
                lines.append(f"\n<b>{esc(batch)}</b>")
            lines.append(f"• {esc(r[2])}: {esc(r[3])}")
    for r in other[1:]:
        if len(r) > 4 and r[1] == hit:
            lines.append(f"• {esc(r[3])}: {esc(r[4])}")
            if len(r) > 5 and r[5] and r[5] not in method:
                method.append(r[5])
    if method:
        lines += ["", "<b>Method</b>"] + [esc(m) for m in method]
    await reply(update, "\n".join(lines))


@guarded
async def cmd_checklist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = await run_sync(sheet.read_tab, "Checklists")
    kinds = sorted({r[0] for r in rows[1:] if r and r[0]})
    want = storage.name_key(" ".join(ctx.args or []))
    hit = storage.match_name(want, kinds, cutoff=0.6) if want else None
    if not hit:
        await reply(update, "✅ Checklists: " + esc(", ".join(kinds)) + "\nExample: <code>/checklist opening</code>")
        return
    steps = [r for r in rows[1:] if r and r[0] == hit]
    await reply(update, f"✅ <b>{esc(hit.capitalize())} checklist</b>\n" +
                "\n".join(f"{esc(r[1])}. {esc(r[2])}" for r in steps))


@guarded
async def cmd_hours(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Opening hours (public, everyone). Staff working hours / salary are money → owners only."""
    import places
    info = await run_sync(places.cafe_info)
    if not info:
        await reply(update, "🕐 Opening hours aren't connected (Google Places key or place ID missing in .env).")
        return
    lines = ["🕐 <b>Opening hours</b> <i>(live from Google)</i>"]
    if info["open_now"] is not None:
        lines.append("🟢 Open now" if info["open_now"] else "🔴 Closed now")
    lines += [""] + [esc(h) for h in info["hours"]]
    if info["regular"] and info["regular"] != info["hours"]:
        lines += ["", "<i>Usual hours:</i>"] + [esc(h) for h in info["regular"]]
    await reply(update, "\n".join(lines))


@guarded
async def cmd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    question = " ".join(ctx.args or []).strip()
    if not question:
        await reply(update, "Ask me something, e.g. <code>/ask how many scoops in a cendol bingsu?</code>")
        return
    remember(ctx, who(update), question)
    await _answer(update, question, ctx)


async def _answer(update: Update, question: str, ctx=None):
    allowed = money_allowed(update, ctx)
    if not allowed and storage.is_money_question(question):
        await refuse(update, ctx, question=question)  # decided by code, not left to the AI
        return
    await update.effective_chat.send_action("typing")
    context = await run_sync(sheet.reference_context)
    if allowed:
        context += "\n\n" + await run_sync(sheet.money_context, storage.parse_month(""))
    history = memory_text(ctx) if ctx else ""
    text = await ai.answer(question, context, allowed, history)
    if ctx and text.strip() != storage.REFUSAL:
        remember(ctx, config.CAFE_NAME + " bot", text)
    await reply(update, esc(text))


# ─── Owner reports ────────────────────────────────────────

@guarded
@owners_only("payback")
async def cmd_payback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    month = storage.parse_month(" ".join(ctx.args or []))
    totals = storage.payback(await run_sync(sheet.records, "Receipts"), month)
    if not totals:
        await reply(update, f"💸 No receipts saved for {month}.")
        return
    lines = [f"💸 <b>Who paid for supplies · {month}</b>", ""]
    lines += [f"• {esc(k)}: <b>{rm(v)}</b>" for k, v in totals.items()]
    lines.append(f"\nTotal: <b>{rm(sum(totals.values()))}</b>")
    await reply(update, "\n".join(lines))


@guarded
@owners_only("spend")
async def cmd_spend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    month = storage.parse_month(" ".join(ctx.args or []))
    items = await run_sync(sheet.records, "Receipt Items")
    receipts = await run_sync(sheet.records, "Receipts")
    by_cat = storage.sum_by(items, month, "Category", "Line Total (RM)")
    by_shop = storage.sum_by(receipts, month, "Supplier", "Total (RM)")
    total = sum(by_shop.values())
    if not total:
        await reply(update, f"🧾 No spending saved for {month}.")
        return
    lines = [f"🧾 <b>Spending · {month}</b>: <b>{rm(total)}</b>", "", "<b>By category</b>"]
    lines += [f"• {esc(k)}: {rm(v)}" for k, v in by_cat.items()]
    lines += ["", "<b>Top shops</b>"] + [f"• {esc(k)}: {rm(v)}" for k, v in list(by_shop.items())[:8]]
    await reply(update, "\n".join(lines))


@guarded
@owners_only("sales")
async def cmd_sales(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    month = storage.parse_month(" ".join(ctx.args or []))
    rows = [r for r in await run_sync(sheet.records, "Daily Sales")
            if storage.month_of(storage.parse_date(r.get("Date")) or "") == month]
    if not rows:
        await reply(update, f"📊 No daily sales saved for {month}.")
        return
    totals = [(storage.parse_date(r["Date"]), storage.to_number(r.get("Total Sales (RM)")) or 0) for r in rows]
    total = sum(t for _, t in totals)
    best = max(totals, key=lambda x: x[1])
    spend = sum(storage.sum_by(await run_sync(sheet.records, "Receipts"), month, "Supplier", "Total (RM)").values())
    await reply(update, f"📊 <b>Sales · {month}</b>\n\n"
                        f"Total: <b>{rm(total)}</b> over {len(totals)} days\n"
                        f"Average day: {rm(total / len(totals))}\n"
                        f"Best day: {best[0]} ({rm(best[1])})\n"
                        f"Supplies bought: {rm(spend)}\n"
                        f"<i>Sales minus supplies: {rm(total - spend)} (before rent, wages, etc.)</i>")


OWNER_COMMANDS = {"payback": cmd_payback, "spend": cmd_spend, "sales": cmd_sales}


# ─── Plain text: replies, stock, shopping, questions ──────

def _find_by_msg(store: dict, chat_id: int, msg_id: int):
    for key, p in store.items():
        if p.get("msg_id") == msg_id and p.get("chat_id") == chat_id:
            return key, p
    return None, None


def _only_pending(store: dict, chat_id: int):
    """The single pending entry in this chat, if there is exactly one."""
    mine = [(k, p) for k, p in store.items() if p.get("chat_id") == chat_id]
    return mine[0] if len(mine) == 1 else (None, None)


@guarded
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = (msg.text or "").strip()
    chat_id = msg.chat_id
    remember(ctx, who(update), text)  # everything said in this chat, for follow-up questions

    # owner override session: close on 'done', otherwise keep it alive
    if override_active(ctx):
        if words(text) in END_OVERRIDE:
            ctx.chat_data.pop("override", None)
            await reply(update, "🔒 Owner mode closed.")
            return
        ctx.chat_data["override"]["idle"] = storage.now().timestamp() + OVERRIDE_IDLE

    # 1. replies to something the bot is waiting on
    if msg.reply_to_message:
        replied = msg.reply_to_message.message_id
        for kind, handler in (("receipts", _edit_receipt), ("sales", _edit_sales),
                              ("stock", _reply_to_stock), ("expense", _reply_to_expense)):
            key, p = _find_by_msg(pending(ctx, kind), chat_id, replied)
            if p:
                await handler(update, ctx, key, p)
                return
        done = pending(ctx, "stock_done").get(str(replied))
        if done and done.get("chat_id") == chat_id:
            await _reply_to_stock_done(update, ctx, done)
            return

    is_private = update.effective_chat.type == ChatType.PRIVATE
    mentioned = is_private or _bot_mentioned(update, ctx)
    bot_name = ctx.bot.username or ""
    clean = re.sub(rf"@{re.escape(bot_name)}", "", text, flags=re.I).strip() if bot_name else text
    low = clean.lower()

    # 2. stock counts: 'stock ...' or a pasted paper checklist (Edwin's format)
    m = re.match(r"^(stock|count)\b[:\s]*(.*)$", low, re.S)
    if m and m.group(2):
        body = clean[len(clean) - len(m.group(2)):]
        sheet_ = storage.parse_stock_sheet(body)
        if mentioned or sheet_["items"] or sheet_["blanks"]:  # 'stock is low lah' is just chat
            await stock_update(update, ctx, body)
            return
    if storage.looks_like_stock_sheet(clean):
        await stock_update(update, ctx, clean)
        return

    # 3. shopping list (only when talking to the bot, so normal chat about buying isn't added)
    m = re.match(r"^buy\b[:\s]+(.+)$", low, re.S)
    if m and mentioned:
        await _shopping_add(update, clean[len(clean) - len(m.group(1)):])
        return

    if not mentioned:
        return  # never butt in on untagged chat

    # 4. tagged but not a reply: one open question in this chat → treat as its answer
    for kind, handler in (("expense", _reply_to_expense), ("stock", _reply_to_stock)):
        key, p = _only_pending(pending(ctx, kind), chat_id)
        if p and (kind == "expense" or words(clean) in YES | NO | {"zero", "0"}):
            await handler(update, ctx, key, p)
            return

    # 5. a question
    await _answer(update, clean, ctx)


# ─── 12 PM daily nudge: everything still waiting ──────────

async def daily_nudge(ctx: ContextTypes.DEFAULT_TYPE):
    by_chat: dict[int, list[str]] = {}

    def add(p, text):
        link = msg_link(p.get("chat_id", 0), p.get("msg_id", 0))
        by_chat.setdefault(p.get("chat_id"), []).append(f"• {text}" + (f" (<a href=\"{link}\">open</a>)" if link else ""))

    for p in pending(ctx, "receipts").values():
        add(p, f"Receipt from {esc(p['r']['supplier'])} {rm(p['r']['total'])}, sent by {esc(p['by'])}: tap ✅ Save or ❌ Cancel")
    for p in pending(ctx, "sales").values():
        add(p, f"Daily sales for {esc(p['s']['date'])}: tap ✅ Save or ❌ Cancel")
    for p in pending(ctx, "stock").values():
        add(p, f"Stock update from {esc(p['by'])}: needs an answer")
    for p in pending(ctx, "expense").values():
        add(p, "Were these bought? " + esc(", ".join(i["name"] for i in p["items"])))
    for chat_id, lines in by_chat.items():
        if not chat_id:
            continue
        try:
            await ctx.bot.send_message(chat_id, "📌 <b>Still waiting for an answer</b>\n" + "\n".join(lines),
                                       parse_mode=HTML, disable_web_page_preview=True)
        except Exception as e:
            log.warning("nudge to %s failed: %s", chat_id, e)


# ─── Start ────────────────────────────────────────────────

def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing from .env")
    os.makedirs(config.DATA_DIR, exist_ok=True)
    persistence = PicklePersistence(filepath=os.path.join(config.DATA_DIR, "state.pickle"))
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).persistence(persistence).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler(["stock", "stockcheck"], cmd_stock))
    app.add_handler(CommandHandler("count", cmd_count))
    app.add_handler(CommandHandler(["buy", "shopping"], cmd_buy))
    app.add_handler(CommandHandler("recipe", cmd_recipe))
    app.add_handler(CommandHandler("checklist", cmd_checklist))
    app.add_handler(CommandHandler(["hours", "open"], cmd_hours))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("payback", cmd_payback))
    app.add_handler(CommandHandler(["spend", "expenses", "pl"], cmd_spend))
    app.add_handler(CommandHandler("sales", cmd_sales))
    app.add_handler(CallbackQueryHandler(on_receipt_button, pattern=r"^r[sx]:"))
    app.add_handler(CallbackQueryHandler(on_sales_button, pattern=r"^s[sx]:"))
    app.add_handler(CallbackQueryHandler(on_stock_button, pattern=r"^k[sxzbyn]:"))
    app.add_handler(CallbackQueryHandler(on_expense_button, pattern=r"^ex:"))
    app.add_handler(CallbackQueryHandler(on_buy_button, pattern=r"^bd:"))
    app.add_handler(CallbackQueryHandler(on_override_button, pattern=r"^ov:"))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if app.job_queue:
        app.job_queue.run_daily(daily_nudge, time=dtime(12, 0, tzinfo=storage.TZ), name="daily_nudge")
    else:
        log.warning("job-queue not installed: no 12 PM nudge (pip install 'python-telegram-bot[job-queue]')")

    log.info("Sudu bot starting (owners chat %s, staff group %s, %d owners)",
             config.OWNER_GROUP_ID, config.STAFF_GROUP_ID, len(config.OWNER_USER_IDS))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
