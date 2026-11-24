"""
Sudu Café Bot: the café's back-office helper on Telegram.

Staff group:   receipt photos, POS sales photos, stock counts, shopping list,
               recipes/checklists questions. No money answers.
Owners chat:   all of the above + spending, sales, /payback.
Everything is saved to the Google Sheet (see google_integration.py).

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


def money_allowed(update: Update) -> bool:
    """Owners anywhere, or anyone in the owners chat (if it's a separate chat)."""
    if is_owner(update):
        return True
    chat_id = update.effective_chat.id if update.effective_chat else 0
    return bool(config.OWNER_GROUP_ID) and chat_id == config.OWNER_GROUP_ID != config.STAFF_GROUP_ID


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
            await reply(update, f"⚠️ {e}")
        except sheet.SheetNotConfigured:
            await reply(update, "⚠️ The Google Sheet isn't connected (credentials.json missing).")
        except ai.AIUnavailable:
            await reply(update, "⏳ The AI is busy or out of free requests right now. Try again in a minute.")
        except Exception:
            log.exception("Handler %s failed", handler.__name__)
            await reply(update, "😕 Something went wrong. Please try again; if it keeps happening, tell the owners.")
    return run


async def reply(update: Update, text: str, **kw):
    msg = update.effective_message
    if msg:
        for chunk in _chunks(text):
            await msg.reply_text(chunk, parse_mode=HTML, disable_web_page_preview=True, **kw)
            kw.pop("reply_markup", None)


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
    return ctx.bot_data.setdefault(kind, {})


def photo_link(chat_id: int, message_id: int) -> str:
    s = str(chat_id)
    return f"https://t.me/c/{s[4:]}/{message_id}" if s.startswith("-100") else "Telegram photo"


# ─── Help ─────────────────────────────────────────────────

HELP_STAFF = """👋 <b>Sudu helper</b>

🧾 <b>Receipt</b>: send a photo. Check it, then tap ✅ Save.
   Fix mistakes by replying to my message: <code>2 qty 12</code> · <code>2 price 7.29</code> · <code>paid by Ali</code> · <code>delete 3</code>
📊 <b>Daily sales</b>: send the POS closing report photo.
📦 <b>Stock</b>: /stock shows what's low · /count shows everything
   Update: <code>stock full cream milk 120, condensed 17</code>
🛒 <b>Shopping</b>: /buy shows the list · <code>buy ice, straws</code> adds items
🕐 <b>Hours</b>: /hours (live from Google)
📖 <b>Ask</b>: /recipe cendol · /checklist opening · or /ask anything about recipes, hours and procedures"""

HELP_OWNER = """

🔒 <b>Owners</b>
/spend: this month's spending by category and shop
/sales: this month's sales
/payback: who paid for supplies and how much
(add a month: <code>/spend oct</code>, <code>/payback last</code>)
/ask works with money questions here too."""


@guarded
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, HELP_STAFF + (HELP_OWNER if money_allowed(update) else ""))


async def cmd_chatid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Setup helper: works anywhere so owners can find IDs for .env."""
    await update.effective_message.reply_text(f"This chat's ID: {update.effective_chat.id}")


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Your Telegram user ID: {update.effective_user.id}")


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
    photo = msg.photo[-1]
    image = bytes(await (await photo.get_file()).download_as_bytearray())
    caption = msg.caption or ""
    in_group = update.effective_chat.type != ChatType.PRIVATE

    status = await msg.reply_text("👀 Reading…")
    try:
        result = await ai.read_photo(image, caption)
    except ai.AIUnavailable:
        await status.edit_text("⏳ The AI is busy or out of free requests right now. Please resend the photo in a minute.")
        return

    if result["kind"] == "receipt" and result["receipt"]:
        await _show_receipt(update, ctx, status, image, caption, result)
    elif result["kind"] == "sales_report" and result["sales"]:
        await _show_sales(update, ctx, status, result["sales"])
    elif in_group and not _bot_mentioned(update, ctx):
        await status.delete()  # just a normal photo in the group: stay quiet
    else:
        await status.edit_text("🤔 That doesn't look like a receipt or a sales report. "
                               "Try a clearer, straight-on photo with the whole receipt in view.")


async def _show_receipt(update, ctx, status, image: bytes, caption: str, result: dict):
    msg = update.effective_message
    r = storage.normalize_receipt(result["receipt"], who(update))
    check = storage.check_receipt(r)
    if not check["ok"]:  # second, careful read with the stronger model
        await status.edit_text("🔎 Numbers don't add up yet, taking a closer look…")
        second = await ai.reread_receipt(image, caption, check["reason"])
        if second:
            r2 = storage.normalize_receipt(second, who(update))
            c2 = storage.check_receipt(r2)
            if c2["ok"] or (c2["diff"] is not None and (check["diff"] is None or abs(c2["diff"]) < abs(check["diff"]))):
                r, check = r2, c2

    rid = new_id("R")
    photo_hash = hashlib.sha256(image).hexdigest()[:16]
    dup = await run_sync(sheet.find_duplicate, photo_hash, storage.receipt_key(r))
    text = storage.receipt_text(r, check, rid)
    buttons = [Btn("✅ Save", callback_data=f"rs:{rid}"), Btn("❌ Cancel", callback_data=f"rx:{rid}")]
    if dup:
        text = f"⚠️ <b>Looks already saved</b> as {esc(dup)}.\n\n" + text
        buttons[0] = Btn("Save anyway", callback_data=f"rs:{rid}")
    await status.edit_text(text, parse_mode=HTML, reply_markup=InlineKeyboardMarkup([buttons]))
    pending(ctx, "receipts")[rid] = {
        "r": r, "msg_id": status.message_id, "chat_id": status.chat_id, "by": who(update),
        "photo": photo_link(msg.chat_id, msg.message_id), "hash": photo_hash,
        "same_photo": bool(dup), "created": storage.stamp(),
    }
    _trim(pending(ctx, "receipts"))


def _trim(store: dict, keep: int = 40):
    for key in list(store)[:-keep]:
        store.pop(key, None)


def _find_by_msg(store: dict, chat_id: int, msg_id: int):
    for key, p in store.items():
        if p.get("msg_id") == msg_id and p.get("chat_id") == chat_id:
            return key, p
    return None, None


@guarded
async def on_receipt_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action, rid = q.data.split(":", 1)
    p = pending(ctx, "receipts").get(rid)
    if not p:
        await q.answer("This receipt was already handled.", show_alert=True)
        await q.edit_message_reply_markup(None)
        return
    if action == "rx":
        pending(ctx, "receipts").pop(rid, None)
        await q.answer("Cancelled")
        await q.edit_message_text("❌ Cancelled, nothing was saved.")
        return
    await q.answer("Saving…")
    r = p["r"]
    try:
        await run_sync(sheet.save_receipt, rid, r, p["photo"], p["by"], p["hash"])
    except Exception as e:
        log.exception("save receipt")
        await q.message.reply_text(f"⚠️ Couldn't save to the Sheet: {str(e)[:150]}\nTap ✅ Save again in a minute.")
        return
    pending(ctx, "receipts").pop(rid, None)
    note = await _tick_shopping(r, who(update))
    await q.edit_message_text(
        f"✅ <b>Saved</b> {esc(rid)}\n{esc(r['supplier'])} · {esc(r['date'])} · <b>{rm(r['total'])}</b>\n"
        f"{len(r['items'])} items · paid by {esc(r['paid_by'])} · saved by {esc(who(update))}{note}",
        parse_mode=HTML)


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
    r = p["r"]
    problems = storage.apply_edits(r, storage.parse_edits(update.effective_message.text))
    check = storage.check_receipt(r)
    try:
        await ctx.bot.edit_message_text(
            storage.receipt_text(r, check, rid), chat_id=p["chat_id"], message_id=p["msg_id"], parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([[Btn("✅ Save", callback_data=f"rs:{rid}"),
                                                Btn("❌ Cancel", callback_data=f"rx:{rid}")]]))
    except Exception as e:  # "message is not modified" when nothing changed
        log.info("edit receipt message: %s", e)
    if problems:
        await reply(update, "⚠️ " + "; ".join(esc(x) for x in problems) +
                    "\nExamples: <code>2 qty 12</code> · <code>2 price 7.29</code> · <code>2 total 87.48</code> · "
                    "<code>2 name Low Fat Milk 1L</code> · <code>2 cat consumables</code> · <code>delete 3</code> · "
                    "<code>add Ice 2 x 5.00</code> · <code>date 12/11</code> · <code>shop Lotus's</code> · "
                    "<code>total 152.95</code> · <code>paid by Ali</code>")
    else:
        await reply(update, "✏️ Updated. Check the receipt above and tap ✅ Save.")


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


async def _show_sales(update, ctx, status, raw: dict):
    s = dict(raw)
    s["date"] = storage.parse_date(s.get("date")) or storage.today()
    sid = new_id("S")
    existing = await run_sync(sheet.sales_row_for, s["date"])
    text = sales_text(s, sid)
    save_label = "✅ Save"
    if existing:
        text = f"⚠️ Sales for {esc(s['date'])} are already saved. Saving will <b>replace</b> them.\n\n" + text
        save_label = "♻️ Replace"
    await status.edit_text(text, parse_mode=HTML, reply_markup=InlineKeyboardMarkup(
        [[Btn(save_label, callback_data=f"ss:{sid}"), Btn("❌ Cancel", callback_data=f"sx:{sid}")]]))
    pending(ctx, "sales")[sid] = {"s": s, "msg_id": status.message_id, "chat_id": status.chat_id, "by": who(update)}
    _trim(pending(ctx, "sales"))


@guarded
async def on_sales_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action, sid = q.data.split(":", 1)
    p = pending(ctx, "sales").pop(sid, None)
    if not p:
        await q.answer("Already handled.", show_alert=True)
        await q.edit_message_reply_markup(None)
        return
    if action == "sx":
        await q.answer("Cancelled")
        await q.edit_message_text("❌ Cancelled, nothing was saved.")
        return
    await q.answer("Saving…")
    s = p["s"]
    try:
        row = await run_sync(sheet.sales_row_for, s["date"])
        await run_sync(sheet.save_sales, s, p["by"], row)
    except Exception as e:
        pending(ctx, "sales")[sid] = p
        await q.message.reply_text(f"⚠️ Couldn't save to the Sheet: {str(e)[:150]}")
        return
    await q.edit_message_text(f"✅ <b>Daily sales saved</b> · {esc(s['date'])} · {rm(s.get('total_sales'))}"
                              f"{' (replaced)' if row else ''}", parse_mode=HTML)


async def _edit_sales(update: Update, ctx, sid: str, p: dict):
    s, bad = p["s"], []
    for part in re.split(r"[\n;,]+", update.effective_message.text or ""):
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
                                    parse_mode=HTML, reply_markup=InlineKeyboardMarkup(
                                        [[Btn("✅ Save", callback_data=f"ss:{sid}"),
                                          Btn("❌ Cancel", callback_data=f"sx:{sid}")]]))
    await reply(update, ("⚠️ Didn't understand: " + esc("; ".join(bad))) if bad else "✏️ Updated. Tap ✅ Save.")


# ─── Stock ────────────────────────────────────────────────

def _stock_line(r: dict) -> str:
    unit = f" {r['Unit']}" if r.get("Unit") else ""
    mn = f" (min {storage.qty_text(r['Min'])})" if storage.to_number(r.get("Min")) is not None else ""
    return f"• {esc(r['Item'])}: <b>{storage.qty_text(r.get('Current')) if r.get('Current') != '' else '?'}</b>{esc(unit)}{mn}"


@guarded
async def cmd_stock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args or []).strip()
    if text:
        await _stock_update(update, ctx, text)
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


async def _stock_update(update: Update, ctx, text: str):
    rows = await run_sync(sheet.stock)
    known = [r["Item"] for r in rows]
    current = {r["Item"]: r.get("Current") for r in rows}
    updates = await ai.parse_stock(text, known)
    if not updates:
        await reply(update, "🤔 I couldn't find any item + number. Example: <code>stock full cream milk 120</code>")
        return
    sid = new_id("K")
    lines = ["📦 <b>Update stock?</b>", ""]
    for u in updates:
        if u["item"] in current:
            old = current[u["item"]]
            lines.append(f"• {esc(u['item'])}: {storage.qty_text(old) if old != '' else '?'} → <b>{storage.qty_text(u['qty'])}</b>")
        else:
            lines.append(f"• 🆕 {esc(u['item'])}: <b>{storage.qty_text(u['qty'])}</b> <i>(new item)</i>")
    pending(ctx, "stock")[sid] = {"updates": updates, "by": who(update)}
    _trim(pending(ctx, "stock"))
    await reply(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(
        [[Btn("✅ Update", callback_data=f"ks:{sid}"), Btn("❌ Cancel", callback_data=f"kx:{sid}")]]))


@guarded
async def on_stock_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action, sid = q.data.split(":", 1)
    p = pending(ctx, "stock").pop(sid, None)
    if not p:
        await q.answer("Already handled.", show_alert=True)
        await q.edit_message_reply_markup(None)
        return
    if action == "kx":
        await q.answer("Cancelled")
        await q.edit_message_text("❌ Cancelled, stock not changed.")
        return
    await q.answer("Updating…")
    done, unknown = await run_sync(sheet.update_stock, p["updates"], who(update))
    for u in unknown:
        await run_sync(sheet.add_stock_item, u["item"], u["qty"], who(update))
    rows = await run_sync(sheet.stock)
    changed = {storage.name_key(u["item"]) for u in p["updates"]}
    need = [r["Item"] for r in rows if storage.name_key(r["Item"]) in changed
            and storage.stock_status(r.get("Current"), r.get("Min")) in ("low", "out")]
    added = await run_sync(sheet.shopping_add, need, "Stock check", "low stock") if need else []
    text = f"✅ Stock updated: {len(done)} item(s)" + (f", {len(unknown)} new" if unknown else "")
    if added:
        text += f"\n🛒 Added to shopping list: {esc(', '.join(added))}"
    await q.edit_message_text(text, parse_mode=HTML)


# ─── Shopping list ────────────────────────────────────────

@guarded
async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        await _shopping_add(update, " ".join(ctx.args))
        return
    items = await run_sync(sheet.shopping_open)
    if not items:
        await reply(update, "🛒 The shopping list is empty. Add with <code>buy ice, straws</code>")
        return
    rows = [[Btn(f"✅ {r['Item'][:40]}" + (f" ({r['Note'][:20]})" if r.get("Note") else ""),
                 callback_data=f"bd:{i}")] for i, r in items[:40]]
    await reply(update, f"🛒 <b>Shopping list</b> ({len(items)})\nTap an item when it's bought.",
                reply_markup=InlineKeyboardMarkup(rows))


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
    row = int(q.data.split(":", 1)[1])
    item = await run_sync(sheet.shopping_done, row, who(update))
    await q.answer(f"✅ {item} bought")
    items = await run_sync(sheet.shopping_open)
    rows = [[Btn(f"✅ {r['Item'][:40]}", callback_data=f"bd:{i}")] for i, r in items[:40]]
    await q.edit_message_text(
        f"🛒 <b>Shopping list</b> ({len(items)})\n✔️ {esc(item)} bought by {esc(who(update))}",
        parse_mode=HTML, reply_markup=InlineKeyboardMarkup(rows) if rows else None)


# ─── Recipes, checklists, questions ───────────────────────

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
    lines = [f"📖 <b>{esc(hit)}</b>"]
    batch = None
    for r in bingsu[1:]:
        if r and r[0] == hit:
            if r[1] != batch:
                batch = r[1]
                lines.append(f"\n<b>{esc(batch)}</b>")
            lines.append(f"• {esc(r[2])}: {esc(r[3])}")
    method = []
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
    await update.effective_chat.send_action("typing")
    allowed = money_allowed(update)
    context = await run_sync(sheet.reference_context)
    if allowed:
        context += "\n\n" + await run_sync(sheet.money_context, storage.parse_month(""))
    history = memory_text(ctx) if ctx else ""
    text = await ai.answer(question, context, allowed, history)
    if ctx:
        remember(ctx, config.CAFE_NAME + " bot", text)
    await reply(update, esc(text))


# ─── Short memory per chat (so follow-up questions make sense) ──
MEMORY_DAYS = 3
MEMORY_MAX = 60


def remember(ctx, speaker: str, text: str):
    """Keep this chat's recent messages. Each chat has its own memory, so owners-chat
    talk is never visible to the staff group."""
    if ctx is None or ctx.chat_data is None or not text:
        return
    mem = ctx.chat_data.setdefault("memory", [])
    mem.append({"t": storage.now().timestamp(), "who": speaker, "text": text[:500]})
    cutoff = storage.now().timestamp() - MEMORY_DAYS * 86400
    ctx.chat_data["memory"] = [m for m in mem if m["t"] >= cutoff][-MEMORY_MAX:]


def memory_text(ctx, last: int = 30) -> str:
    mem = (ctx.chat_data or {}).get("memory", [])[-last:]
    from datetime import datetime
    return "\n".join(f"[{datetime.fromtimestamp(m['t'], storage.TZ):%a %H:%M}] {m['who']}: {m['text']}" for m in mem)


# ─── Owner reports ────────────────────────────────────────

def owners_only(handler):
    @wraps(handler)
    async def run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not money_allowed(update):
            await reply(update, OWNERS_ONLY)
            return
        await handler(update, ctx)
    return run


@guarded
@owners_only
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
@owners_only
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
@owners_only
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


# ─── Plain text: corrections, stock, shopping, questions ──

@guarded
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = (msg.text or "").strip()
    remember(ctx, who(update), text)  # everything said in this chat, for follow-up questions
    if msg.reply_to_message:
        replied = msg.reply_to_message.message_id
        rid, p = _find_by_msg(pending(ctx, "receipts"), msg.chat_id, replied)
        if p:
            await _edit_receipt(update, ctx, rid, p)
            return
        sid, p = _find_by_msg(pending(ctx, "sales"), msg.chat_id, replied)
        if p:
            await _edit_sales(update, ctx, sid, p)
            return
    low = text.lower()
    if bot_name := (ctx.bot.username or ""):
        low = low.replace(f"@{bot_name.lower()}", "").strip()
        text = re.sub(rf"@{re.escape(bot_name)}", "", text, flags=re.I).strip()
    m = re.match(r"^(stock|count)\b[:\s]*(.*)$", low, re.S)
    if m and m.group(2):
        await _stock_update(update, ctx, text[len(text) - len(m.group(2)):])
        return
    m = re.match(r"^(buy|need)\b[:\s]+(.+)$", low, re.S)
    if m:
        await _shopping_add(update, text[len(text) - len(m.group(2)):])
        return
    is_private = update.effective_chat.type == ChatType.PRIVATE
    if is_private or _bot_mentioned(update, ctx):
        await _answer(update, text, ctx)


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
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(CommandHandler("count", cmd_count))
    app.add_handler(CommandHandler(["buy", "shopping"], cmd_buy))
    app.add_handler(CommandHandler("recipe", cmd_recipe))
    app.add_handler(CommandHandler("checklist", cmd_checklist))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler(["hours", "open"], cmd_hours))
    app.add_handler(CommandHandler("payback", cmd_payback))
    app.add_handler(CommandHandler(["spend", "expenses"], cmd_spend))
    app.add_handler(CommandHandler("sales", cmd_sales))
    app.add_handler(CallbackQueryHandler(on_receipt_button, pattern=r"^r[sx]:"))
    app.add_handler(CallbackQueryHandler(on_sales_button, pattern=r"^s[sx]:"))
    app.add_handler(CallbackQueryHandler(on_stock_button, pattern=r"^k[sx]:"))
    app.add_handler(CallbackQueryHandler(on_buy_button, pattern=r"^bd:"))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Sudu bot starting (owners chat %s, staff group %s, %d owners)",
             config.OWNER_GROUP_ID, config.STAFF_GROUP_ID, len(config.OWNER_USER_IDS))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
