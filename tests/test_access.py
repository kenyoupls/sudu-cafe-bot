"""Who may see money, the refusal + 🔓 Show anyway override, and per-chat memory."""
import asyncio
from types import SimpleNamespace

import pytest

import bot
import config
import storage

OWNER, STAFF = 111, 222
OWNER_CHAT, STAFF_CHAT = -1001, -1002


def upd(user_id, chat_id, chat_type="supergroup"):
    async def noop(*a, **k):
        return None
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id, first_name="Ken" if user_id == OWNER else "Ali"),
                           effective_chat=SimpleNamespace(id=chat_id, type=chat_type, send_action=noop))


def ctx_():
    return SimpleNamespace(chat_data={}, bot_data={}, args=[])


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_IDS", {OWNER})
    monkeypatch.setattr(config, "OWNER_GROUP_ID", OWNER_CHAT)
    monkeypatch.setattr(config, "STAFF_GROUP_ID", STAFF_CHAT)


def test_staff_group_has_no_money_even_for_owners_until_override():
    c = ctx_()
    assert bot.chat_allowed(upd(STAFF, STAFF_CHAT))
    assert not bot.money_allowed(upd(STAFF, STAFF_CHAT), c)
    assert not bot.money_allowed(upd(OWNER, STAFF_CHAT), c)      # refusal first, like the old bot
    now = storage.now().timestamp()
    c.chat_data["override"] = {"by": "Ken", "hard": now + 900, "idle": now + 300}
    assert bot.money_allowed(upd(STAFF, STAFF_CHAT), c)          # session open for follow-ups
    c.chat_data["override"]["idle"] = now - 1                    # idle timeout
    assert not bot.money_allowed(upd(OWNER, STAFF_CHAT), c) and "override" not in c.chat_data


def test_owners_chat_and_owner_dm_see_money():
    assert bot.money_allowed(upd(STAFF, OWNER_CHAT))
    assert bot.money_allowed(upd(OWNER, OWNER, "private"))
    assert not bot.money_allowed(upd(STAFF, STAFF, "private"))


def test_single_group_setup_only_owner_ids(monkeypatch):
    monkeypatch.setattr(config, "STAFF_GROUP_ID", OWNER_CHAT)
    assert not bot.money_allowed(upd(STAFF, OWNER_CHAT))
    assert bot.money_allowed(upd(OWNER, OWNER_CHAT))


def test_strangers_are_ignored():
    assert not bot.chat_allowed(upd(999, 999, "private"))
    assert not bot.chat_allowed(upd(OWNER, -100777))
    assert bot.chat_allowed(upd(OWNER, OWNER, "private"))


def test_memory_is_per_chat_and_trimmed():
    staff, owners = ctx_(), ctx_()
    bot.remember(owners, "Ken", "sales were RM2400 yesterday")
    bot.remember(staff, "Ali", "milk is finished")
    assert "RM2400" not in bot.memory_text(staff) and "milk" in bot.memory_text(staff)
    for i in range(100):
        bot.remember(staff, "Ali", f"msg {i}")
    assert len(staff.chat_data["memory"]) == bot.MEMORY_MAX
    old = {"t": storage.now().timestamp() - 5 * 86400, "who": "x", "text": "old"}
    staff.chat_data["memory"].insert(0, old)
    bot.remember(staff, "Ali", "new")
    assert old not in staff.chat_data["memory"]


@pytest.fixture
def capture(monkeypatch):
    sent = {"replies": [], "ai": None}

    async def fake_reply(update, text, **kw):
        sent["replies"].append((text, kw.get("reply_markup")))
        return SimpleNamespace(message_id=1, chat_id=update.effective_chat.id)

    async def fake_answer(q, context, allowed, history=""):
        sent["ai"] = {"context": context, "allowed": allowed}
        return "ok"

    monkeypatch.setattr(bot, "reply", fake_reply)
    monkeypatch.setattr(bot.ai, "answer", fake_answer)
    monkeypatch.setattr(bot.sheet, "reference_context", lambda: "RECIPES")
    monkeypatch.setattr(bot.sheet, "money_context", lambda m: "SALES RM9999")
    return sent


def test_staff_money_question_refused_by_code_with_button(capture):
    c = ctx_()
    asyncio.run(bot._answer(upd(STAFF, STAFF_CHAT), "how much did we sell yesterday?", c))
    text, markup = capture["replies"][-1]
    assert text == storage.REFUSAL and capture["ai"] is None            # AI never asked
    assert markup.inline_keyboard[0][0].text == "🔓 Show anyway (owner only)"
    assert "Financial info" not in bot.memory_text(c)                   # refusals not remembered


def test_staff_normal_question_gets_no_money_data(capture):
    asyncio.run(bot._answer(upd(STAFF, STAFF_CHAT), "how much coconut milk for cendol?", ctx_()))
    assert capture["ai"] == {"context": "RECIPES", "allowed": False}


def test_owners_chat_gets_money_data(capture):
    asyncio.run(bot._answer(upd(STAFF, OWNER_CHAT), "how much did we sell?", ctx_()))
    assert "SALES RM9999" in capture["ai"]["context"] and capture["ai"]["allowed"]


@pytest.mark.parametrize("q,money", [
    ("how much did we sell today", True), ("what's our profit", True), ("who paid for the milk", True),
    ("Ali's salary?", True), ("how many hours did Mei work", True), ("working hours this week", True),
    ("berapa gaji", True), ("are we open on wednesday?", False), ("opening hours?", False),
    ("how much coconut milk for cendol?", False), ("how much do we spend on milk", True),
    ("is milk low?", False),
])
def test_money_keywords(q, money):
    assert storage.is_money_question(q) is money
