"""Mock tests for Bug A fix: multi-item new-item queue.

Runs bot._handle_message_inner end-to-end against a fake Telegram update,
verifying that multiple concurrent "<item> <qty>" new-item flows can be
in flight at once, that replies get routed to the right pending item, and
that the AI is never hijacked while a code-side new-item flow is pending.

Based on the FakeUpdate/FakeContext pattern used for bug-repro tracing.
"""
import sys
import os
import asyncio
import tempfile

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_DIR)
os.environ.setdefault('DISABLE_GOOGLE_SHEETS', '1')
os.environ.setdefault('OWNER_GROUP_ID', '123456')

# storage.LocalJsonStore resolves its data file as a *relative* path
# ("data/cafe_data.json"), and `import bot` instantiates the real store at
# module-load time. Run from an isolated scratch directory so this test
# never reads or writes the real cafe's data/cafe_data.json.
_scratch_dir = tempfile.mkdtemp(prefix="new_item_multi_test_")
os.makedirs(os.path.join(_scratch_dir, "data"), exist_ok=True)
os.chdir(_scratch_dir)

import bot
import config


# ─── Fakes ──────────────────────────────────────────────────────────

sent_messages = []
_next_msg_id = [1000]


def _new_msg_id():
    _next_msg_id[0] += 1
    return _next_msg_id[0]


class FakeMessage:
    def __init__(self, text, msg_id=None, from_user=None, chat=None, reply_to=None):
        self.text = text
        self.message_id = msg_id if msg_id is not None else _new_msg_id()
        self.from_user = from_user
        self.chat = chat
        self.reply_to_message = reply_to
        self.photo = None
        self.document = None
        self.voice = None
        self.video = None
        self.caption = None
        self.entities = []
        self.caption_entities = []
        self.date = None

    async def reply_text(self, text, **kwargs):
        sent = FakeMessage(text, from_user=self.from_user, chat=self.chat)
        sent_messages.append(('reply_text', text))
        return sent


class FakeUser:
    def __init__(self, uid=42, name="TestUser", username="testuser"):
        self.id = uid
        self.full_name = name
        self.username = username
        self.first_name = name


class FakeChat:
    def __init__(self, cid=123456, ctype="supergroup"):
        self.id = cid
        self.type = ctype


class FakeBotUser:
    """from_user for a message the bot itself sent (its prompt) — needed so
    reply_to_message.from_user.id == ctx.bot.id is recognized as a tag."""
    def __init__(self, uid=999):
        self.id = uid
        self.full_name = "Sudu Helper"
        self.username = "Sudu_helper_bot"
        self.first_name = "Sudu"


class FakeBot:
    def __init__(self):
        self.username = "Sudu_helper_bot"
        self.id = 999

    async def send_message(self, *a, **k):
        sent_messages.append(('send_message', str(a) + str(k)))

    async def get_file(self, *a, **k):
        return None


class FakeUpdate:
    def __init__(self, text, chat_id=123456, reply_to=None, msg_id=None):
        self.message = FakeMessage(text, msg_id=msg_id, from_user=FakeUser(),
                                    chat=FakeChat(cid=chat_id), reply_to=reply_to)
        self.effective_chat = self.message.chat
        self.effective_user = self.message.from_user
        self.callback_query = None


class FakeContext:
    def __init__(self):
        self.chat_data = {}
        self.user_data = {}
        self.bot = FakeBot()
        self.args = []


# ─── Patches: no network, no real sheets, no real group gate ──────────

async def fake_group_gate(update):
    return False  # allow all groups


bot._group_gate = fake_group_gate


async def fake_process_message(*args, **kwargs):
    sent_messages.append(('AI-CALL', str(args)[:200]))
    return ("Fake AI reply — should not appear during pre-empt/reply tests", [])


bot.process_message = fake_process_message

if hasattr(bot, 'extract_action_items_ai'):
    async def _stub(*a, **k):
        return []
    bot.extract_action_items_ai = _stub

# store.update_stock talks to Sheets normally; stub it so the confirm-yes
# path in the state machine doesn't try to hit the network.
bot.store.update_stock = lambda *a, **k: True
bot.store._find_existing_stock_name = lambda name: name


def setup_config():
    config.OWNER_GROUP_ID = 123456
    config.STAFF_GROUP_IDS = {123456}
    config.KNOWN_CHAT_IDS = {123456}


def reset():
    global sent_messages
    sent_messages = []


def ai_called():
    return any(s[0] == 'AI-CALL' for s in sent_messages)


def prompts_sent():
    """All 'New item detected' prompts sent this run."""
    return [s[1] for s in sent_messages if s[0] == 'reply_text' and 'New item detected' in s[1]]


def replies():
    return [s[1] for s in sent_messages if s[0] == 'reply_text']


BOT_MENTION = "@Sudu_helper_bot"


async def send(ctx, text, chat_id=123456, reply_to=None, tag=True):
    """Send a message through the real handler. In a group chat the bot
    only responds when @mentioned or replying to one of its own messages
    (see bot._bot_is_tagged) — mirror that here like real staff usage,
    unless a Telegram reply_to already implies the tag, or tag=False."""
    reset()
    if tag and reply_to is None and BOT_MENTION not in text:
        text = f"{BOT_MENTION} {text}"
    update = FakeUpdate(text, chat_id=chat_id, reply_to=reply_to)
    try:
        await bot._handle_message_inner(update, ctx)
    except Exception as e:
        sent_messages.append(('EXCEPTION', f'{type(e).__name__}: {e}'))
    return update


results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"{status}: {name}" + (f" — {detail}" if detail and not cond else ""))
    results.append(cond)
    return cond


# ─── Scenarios ──────────────────────────────────────────────────────

async def scenario_1_bug_a_repro(ctx):
    print("\n--- Scenario 1: Bug A repro (two concurrent new-item flows) ---")
    await send(ctx, "phoenix feather 6")
    check("phoenix prompt sent", len(prompts_sent()) == 1, str(sent_messages))
    check("AI not called for phoenix", not ai_called(), str(sent_messages))

    await send(ctx, "hydra scale 3")
    check("hydra prompt sent (NOT hijacked by AI)", len(prompts_sent()) == 1, str(sent_messages))
    check("AI not called for hydra (Bug A fix)", not ai_called(), str(sent_messages))

    active = ctx.chat_data.get("pending_new_items_active", [])
    check("pending_new_items_active has 2 entries", len(active) == 2,
          f"got {len(active)}: {[a.get('item_name') for a in active]}")
    names = {a.get("item_name") for a in active}
    check("both phoenix feather and hydra scale pending",
          "phoenix feather" in names and "hydra scale" in names, str(names))


async def scenario_2_reply_to_specific(ctx):
    print("\n--- Scenario 2: Telegram reply-to-message match ---")
    active = ctx.chat_data.get("pending_new_items_active", [])
    phoenix = next(a for a in active if a["item_name"] == "phoenix feather")
    phoenix_prompt_msg_id = phoenix.get("prompt_msg_id")
    check("phoenix has a prompt_msg_id saved", phoenix_prompt_msg_id is not None)

    fake_prompt_msg = FakeMessage("🆕 New item detected: phoenix feather (6 units)",
                                   msg_id=phoenix_prompt_msg_id, from_user=FakeBotUser())
    await send(ctx, "yes", reply_to=fake_prompt_msg)
    check("AI not called", not ai_called(), str(sent_messages))
    check("phoenix advanced (added to stock message)",
          any("Added phoenix feather" in r or "phoenix feather" in r for r in replies()),
          str(replies()))

    active = ctx.chat_data.get("pending_new_items_active", [])
    names = {a.get("item_name") for a in active}
    check("phoenix now in expense-confirm stage (not removed, qty was known)",
          "phoenix feather" in names, str(names))
    phoenix2 = next((a for a in active if a["item_name"] == "phoenix feather"), None)
    check("phoenix stage advanced past awaiting_regular_confirm",
          phoenix2 is not None and phoenix2.get("stage") == "awaiting_expense_confirm",
          str(phoenix2))

    # Clean up: answer "no" to expense confirm so phoenix is fully resolved
    # before the next scenarios (which assume a fresh awaiting_regular_confirm).
    # Name-qualified since hydra scale is also still pending (ambiguous bare "no").
    await send(ctx, "no phoenix")


async def scenario_3_name_in_text(ctx):
    print("\n--- Scenario 3: name-in-text match ---")
    # Re-seed: phoenix feather resolved in scenario 2 (removed after "no").
    # hydra scale should still be pending from scenario 1.
    active = ctx.chat_data.get("pending_new_items_active", [])
    check("phoenix feather resolved out of queue", not any(a["item_name"] == "phoenix feather" for a in active),
          str([a.get("item_name") for a in active]))

    # Add phoenix feather back for a clean two-item ambiguity test.
    await send(ctx, "phoenix feather 6")
    active = ctx.chat_data.get("pending_new_items_active", [])
    check("two items pending again", len(active) == 2, str([a.get("item_name") for a in active]))

    await send(ctx, "yes phoenix")
    check("AI not called (yes phoenix)", not ai_called(), str(sent_messages))
    active = ctx.chat_data.get("pending_new_items_active", [])
    phoenix = next((a for a in active if a["item_name"] == "phoenix feather"), None)
    check("phoenix advanced via name match", phoenix is not None and phoenix.get("stage") == "awaiting_expense_confirm",
          str(phoenix))

    await send(ctx, "no hydra")
    check("AI not called (no hydra)", not ai_called(), str(sent_messages))
    active = ctx.chat_data.get("pending_new_items_active", [])
    check("hydra removed via name match (declined)",
          not any(a["item_name"] == "hydra scale" for a in active),
          str([a.get("item_name") for a in active]))

    # Clean up phoenix (only item left pending) for next scenario.
    active = ctx.chat_data.get("pending_new_items_active", [])
    if len(active) == 1:
        await send(ctx, "no")
    elif active:
        await send(ctx, "no phoenix")


async def scenario_4_number_reference(ctx):
    print("\n--- Scenario 4: number reference ---")
    # Queue should be empty entering this scenario (drained by scenario 3).
    pre_existing = ctx.chat_data.get("pending_new_items_active", [])
    check("queue empty at start of scenario 4", len(pre_existing) == 0, str(pre_existing))

    await send(ctx, "phoenix feather 6")
    await send(ctx, "hydra scale 3")
    active = ctx.chat_data.get("pending_new_items_active", [])
    check("two items pending", len(active) == 2, str([a.get("item_name") for a in active]))
    first_name = active[0]["item_name"]

    await send(ctx, "1 yes")
    check("AI not called (1 yes)", not ai_called(), str(sent_messages))
    active = ctx.chat_data.get("pending_new_items_active", [])
    first_item = next((a for a in active if a["item_name"] == first_name), None)
    check(f"item #1 ({first_name}) advanced via number ref",
          first_item is not None and first_item.get("stage") == "awaiting_expense_confirm",
          str(first_item))

    # Clean up both — unambiguous each time (name-qualified while 2 pending,
    # bare "no" once only 1 remains).
    second_name = next(a["item_name"] for a in active if a["item_name"] != first_name)
    await send(ctx, f"no {first_name.split()[0]}")
    remaining = ctx.chat_data.get("pending_new_items_active", [])
    if remaining:
        await send(ctx, "no")
    check("queue drained for next scenario", len(ctx.chat_data.get("pending_new_items_active", [])) == 0,
          str(ctx.chat_data.get("pending_new_items_active")))


async def scenario_5_single_item_fallback(ctx):
    print("\n--- Scenario 5: single-item fallback (no disambiguation needed) ---")
    await send(ctx, "phoenix feather 6")
    active = ctx.chat_data.get("pending_new_items_active", [])
    check("only phoenix pending", len(active) == 1 and active[0]["item_name"] == "phoenix feather",
          str([a.get("item_name") for a in active]))

    await send(ctx, "yes")
    check("AI not called (bare yes, single item)", not ai_called(), str(sent_messages))
    check("no disambiguation prompt sent", not any("Which one" in r for r in replies()), str(replies()))
    active = ctx.chat_data.get("pending_new_items_active", [])
    phoenix = next((a for a in active if a["item_name"] == "phoenix feather"), None)
    check("phoenix matched unambiguously and advanced",
          phoenix is not None and phoenix.get("stage") == "awaiting_expense_confirm", str(phoenix))

    await send(ctx, "no")  # clean up


async def scenario_6_ambiguous(ctx):
    print("\n--- Scenario 6: ambiguous bare reply -> disambiguation prompt ---")
    await send(ctx, "phoenix feather 6")
    await send(ctx, "hydra scale 3")
    active = ctx.chat_data.get("pending_new_items_active", [])
    check("two items pending", len(active) == 2, str([a.get("item_name") for a in active]))

    await send(ctx, "yes")
    check("AI not called (bare yes, ambiguous)", not ai_called(), str(sent_messages))
    check("disambiguation prompt sent", any("Which one" in r for r in replies()), str(replies()))
    check("disambiguation state saved",
          ctx.chat_data.get("pending_new_item_disambiguation") is not None,
          str(ctx.chat_data.get("pending_new_item_disambiguation")))
    active = ctx.chat_data.get("pending_new_items_active", [])
    check("both items still pending (neither consumed)", len(active) == 2,
          str([a.get("item_name") for a in active]))

    # Resolve the disambiguation with a follow-up naming one item.
    await send(ctx, "phoenix")
    check("AI not called (disambiguation follow-up)", not ai_called(), str(sent_messages))
    active = ctx.chat_data.get("pending_new_items_active", [])
    phoenix = next((a for a in active if a["item_name"] == "phoenix feather"), None)
    check("phoenix resolved via disambiguation follow-up",
          phoenix is not None and phoenix.get("stage") == "awaiting_expense_confirm", str(phoenix))
    check("disambiguation state cleared",
          ctx.chat_data.get("pending_new_item_disambiguation") is None)

    # Clean up.
    await send(ctx, "no")  # resolves phoenix's expense confirm
    await send(ctx, "no hydra")  # resolves hydra


async def main():
    setup_config()
    ctx = FakeContext()

    await scenario_1_bug_a_repro(ctx)
    await scenario_2_reply_to_specific(ctx)
    await scenario_3_name_in_text(ctx)
    await scenario_4_number_reference(ctx)
    await scenario_5_single_item_fallback(ctx)
    await scenario_6_ambiguous(ctx)

    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{passed}/{total} checks passed")
    return passed == total


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
