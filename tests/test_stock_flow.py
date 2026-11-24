"""Stock counts: Edwin's paper checklist, blanks, OUT/OK words, new items, 'actually 92', expenses."""
import asyncio
from types import SimpleNamespace

import ai_chat as ai
import bot
import storage

EDWIN = """Stock check 24/11 - checked by Edwin
Full Cream Milk: 120
Low Fat Milk: 139
Condensed Milk: 17
Salt:
Taro Balls:
Lychee Popping Boba: habis
Coffee Jelly: OOS
Whipping Cream: LOW
Brown Boba (Cook): 3
Brown Boba (Jelly): 5"""
KNOWN = ["Full Cream Milk", "Low Fat Milk", "Condensed Milk", "Salt", "Taro Balls", "Lychee Popping Boba",
         "Coffee Jelly", "Whipping Cream", "Brown Boba (Cook)", "Brown Boba (Jelly)"]


def test_paper_checklist_read_by_code():
    s = storage.parse_stock_sheet(EDWIN)
    items = dict(s["items"])
    assert items["Full Cream Milk"] == 120 and items["Lychee Popping Boba"] == 0 and items["Coffee Jelly"] == 0
    assert items["Brown Boba (Cook)"] == 3 and items["Brown Boba (Jelly)"] == 5     # no collision
    assert s["blanks"] == ["Salt", "Taro Balls"]                                     # never silently 0
    assert s["invalid"] == [("Whipping Cream", "LOW")]                               # needs a number
    assert storage.looks_like_stock_sheet(EDWIN)
    assert not storage.looks_like_stock_sheet("milk: 3\ncups: 4")


def test_inline_counts_and_units():
    assert storage.parse_stock_sheet("milk 120 btl, cups 2.5kg, ice habis")["items"] == [
        ("milk", 120.0), ("cups", 2.5), ("ice", 0.0)]


def test_read_stock_matches_without_ai_and_flags_new(monkeypatch):
    async def no_ai(*a, **k):
        raise ai.AIUnavailable("offline")
    monkeypatch.setattr(ai, "_ask_json", no_ai)
    r = asyncio.run(ai.read_stock(EDWIN + "\nUnicorn Tears: 3", KNOWN))
    new = [u for u in r["updates"] if u["new"]]
    assert [u["item"] for u in new] == ["Unicorn Tears"] and new[0]["qty"] == 3     # qty kept, not re-asked
    assert len(r["updates"]) == 8 and r["blanks"] == ["Salt", "Taro Balls"]


def test_stock_steps_blanks_then_new_then_confirm():
    p = {"id": "K1", "updates": [{"typed": "milk", "item": "Full Cream Milk", "qty": 120.0, "new": False},
                                 {"typed": "unicorn", "item": "Unicorn Tears", "qty": 3.0, "new": True}],
         "blanks": ["Salt"], "invalid": [], "add_new": None}
    text, buttons = bot.stock_step(p, {"Full Cream Milk": 88})
    assert "left blank" in text and buttons.inline_keyboard[0][0].callback_data == "kz:K1"
    p["blanks"] = []
    text, buttons = bot.stock_step(p, {"Full Cream Milk": 88})
    assert "regular stock item" in text and buttons.inline_keyboard[0][0].callback_data == "ky:K1"
    p["add_new"] = True
    text, buttons = bot.stock_step(p, {"Full Cream Milk": 88})
    assert "88 → <b>120</b>" in text and "will be added" in text
    assert buttons.inline_keyboard[0][0].callback_data == "ks:K1"


def test_actually_92_updates_that_item(monkeypatch):
    seen = {}

    async def fake_update(update, ctx, text):
        seen["text"] = text
    monkeypatch.setattr(bot, "stock_update", fake_update)
    msg = SimpleNamespace(text="actually 92")
    upd = SimpleNamespace(effective_message=msg)
    asyncio.run(bot._reply_to_stock_done(upd, None, {"chat_id": 1, "items": ["Full Cream Milk"]}))
    assert seen["text"] == "Full Cream Milk: 92"


def test_expense_reply_becomes_receipt(monkeypatch):
    async def fake(prompt, spec, **k):
        return {"bought": "yes", "supplier": "FairPrice", "paid_by": "me", "total": 15}, "m"
    monkeypatch.setattr(ai, "_ask_json", fake)
    raw = asyncio.run(ai.read_expense("yes RM15 FairPrice paid by me", [{"name": "Unicorn Tears", "qty": 3}], "Ken"))
    r = storage.normalize_receipt(raw, "Ken")
    assert r["supplier"] == "FairPrice" and r["paid_by"] == "Ken" and r["total"] == 15
    assert r["items"][0]["qty"] == 3 and r["items"][0]["line_total"] == 15 and storage.check_receipt(r)["ok"]


def test_expense_no(monkeypatch):
    async def fake(prompt, spec, **k):
        return {"bought": "no"}, "m"
    monkeypatch.setattr(ai, "_ask_json", fake)
    assert asyncio.run(ai.read_expense("no la", [{"name": "X", "qty": 1}], "Ken")) is None


def test_human_reply_to_receipt_is_turned_into_edits():
    action, edits = ai.reply_to_edits({"action": "edit", "edits": [
        {"op": "item", "n": 2, "field": "qty", "value": "3"},
        {"op": "set", "field": "paid_by", "value": "Ali"},
        {"op": "set", "field": "date", "value": "12/11/2026"},
        {"op": "delete", "n": 4},
        {"op": "item", "n": 1, "field": "qty", "value": "lots"}]})   # not a number → ignored
    assert action == "edit"
    assert edits == [{"op": "item", "n": 2, "field": "qty", "value": 3.0},
                     {"op": "set", "field": "paid_by", "value": "Ali"},
                     {"op": "set", "field": "date", "value": "2026-11-12"},
                     {"op": "delete", "n": 4}]
    assert ai.reply_to_edits({"action": "cancel"}) == ("cancel", [])
