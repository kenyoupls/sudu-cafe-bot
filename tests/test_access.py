"""Who may see money: owners anywhere, anyone in the owners chat, nobody else."""
from types import SimpleNamespace

import pytest

import bot
import config

OWNER, STAFF = 111, 222
OWNER_CHAT, STAFF_CHAT = -1001, -1002


def upd(user_id, chat_id, chat_type="supergroup"):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id),
                           effective_chat=SimpleNamespace(id=chat_id, type=chat_type))


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_IDS", {OWNER})
    monkeypatch.setattr(config, "OWNER_GROUP_ID", OWNER_CHAT)
    monkeypatch.setattr(config, "STAFF_GROUP_ID", STAFF_CHAT)


def test_staff_in_staff_group_cannot_see_money():
    assert bot.chat_allowed(upd(STAFF, STAFF_CHAT))
    assert not bot.money_allowed(upd(STAFF, STAFF_CHAT))


def test_owner_sees_money_everywhere():
    assert bot.money_allowed(upd(OWNER, STAFF_CHAT))
    assert bot.money_allowed(upd(OWNER, OWNER, "private"))


def test_owners_chat_sees_money():
    assert bot.money_allowed(upd(STAFF, OWNER_CHAT))


def test_single_group_setup_only_owner_ids(monkeypatch):
    monkeypatch.setattr(config, "STAFF_GROUP_ID", OWNER_CHAT)  # one group for everyone
    assert not bot.money_allowed(upd(STAFF, OWNER_CHAT))
    assert bot.money_allowed(upd(OWNER, OWNER_CHAT))


def test_strangers_are_ignored():
    assert not bot.chat_allowed(upd(999, 999, "private"))        # random person DMs the bot
    assert not bot.chat_allowed(upd(OWNER, -100777))               # bot added to another group
    assert bot.chat_allowed(upd(OWNER, OWNER, "private"))


def test_memory_is_per_chat_and_trimmed():
    staff, owners = SimpleNamespace(chat_data={}), SimpleNamespace(chat_data={})
    bot.remember(owners, "Ken", "sales were RM2400 yesterday")
    bot.remember(staff, "Ali", "milk is finished")
    assert "RM2400" not in bot.memory_text(staff) and "milk" in bot.memory_text(staff)
    for i in range(100):
        bot.remember(staff, "Ali", f"msg {i}")
    assert len(staff.chat_data["memory"]) == bot.MEMORY_MAX
    old = {"t": bot.storage.now().timestamp() - 5 * 86400, "who": "x", "text": "old"}
    staff.chat_data["memory"].insert(0, old)
    bot.remember(staff, "Ali", "new")
    assert old not in staff.chat_data["memory"]


def test_staff_context_never_contains_money(monkeypatch):
    """The AI only ever receives money data when money is allowed."""
    import asyncio
    sent = {}

    async def fake_answer(q, context, allowed, history=""):
        sent.update(context=context, allowed=allowed)
        return "ok"

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(bot.sheet, "reference_context", lambda: "RECIPES")
    monkeypatch.setattr(bot.sheet, "money_context", lambda m: "SALES RM9999")
    monkeypatch.setattr(bot.ai, "answer", fake_answer)
    monkeypatch.setattr(bot, "reply", noop)
    u = upd(STAFF, STAFF_CHAT)
    u.effective_chat.send_action = noop
    asyncio.run(bot._answer(u, "how much did we sell?"))
    assert sent == {"context": "RECIPES", "allowed": False}
    u2 = upd(OWNER, STAFF_CHAT)
    u2.effective_chat.send_action = noop
    asyncio.run(bot._answer(u2, "how much did we sell?"))
    assert "SALES RM9999" in sent["context"] and sent["allowed"]
