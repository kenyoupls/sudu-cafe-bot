#!/usr/bin/env python3
"""Mock tests for the staff-group bug fixes (E, F, H, cleanup). Run: python3 test_staff_group.py"""

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# Isolate: importing bot.py may touch ./data, so run from a temp dir.
_TMP = tempfile.mkdtemp(prefix="staff_group_test_")
os.chdir(_TMP)

import bot  # noqa: E402
from pending_tasks import PendingTasksStore  # noqa: E402

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


class FakeStore:
    def __init__(self, data=None):
        self.data = data if data is not None else {}

    def _save_local_only(self):
        pass


REFUSAL = "Financial info is only available in the owner group. Check with the boss."
CANCEL = [{"action": "cancel_pending"}]

# ─── Bug E ───
ex = bot._extract_clarification_options
check("E1: greeting 'Hi Ken, how can I help you today?' -> []",
      ex("Hi Ken, how can I help you today?") == [])
check("E2: greeting 'Hi Ken, how can I help?' -> []",
      ex("Hi Ken, how can I help?") == [])
r = ex("Which milk — full cream, low fat, or skim?")
check(f"E3: real clarification still works ({r})", r == ["full cream", "low fat", "skim"])
check("E4: 'Hello, want some coffee?' -> []", ex("Hello, want some coffee?") == [])

# ─── Bug F ───
sup = bot._should_suppress_chat_reply_for_cancel
check("F5: cancel + 'Okay, cancelled' -> suppressed", sup("Okay, cancelled", CANCEL) is True)
check("F6: cancel + refusal -> NOT suppressed", sup(REFUSAL, CANCEL) is False)
check("F7: no cancel action + cancel-like reply -> NOT suppressed",
      sup("Okay, cancelled", [{"action": "update_stock"}]) is False)

# ─── Bug H ───
h = bot._looks_like_staff_blocked_query
check("H8: 'whats the sales for this month so far' -> True",
      h("whats the sales for this month so far") is True)
check("H9: 'hi bot' -> False", h("hi bot") is False)
check("H10: 'milk 5' -> False", h("milk 5") is False)
check("H11: 'how much did we make this week' -> True",
      h("how much did we make this week") is True)
check("H12: 'who paid for the last receipt' -> True",
      h("who paid for the last receipt") is True)

# ─── Cleanup ───
fs = FakeStore({"pending_tasks": [
    {"id": "a", "chat_id": 1, "type": "clarification",
     "summary": "Clarification needed: Hi Ken, how can I help you today?"},
]})
pts = PendingTasksStore(fs)
check("C13: stale greeting clarification purged on init",
      len(fs.data["pending_tasks"]) == 0)

fs = FakeStore({"pending_tasks": [
    {"id": "b", "chat_id": 1, "type": "clarification",
     "summary": "Clarification needed: Which milk — full cream, low fat, or skim?"},
]})
pts = PendingTasksStore(fs)
check("C14: real clarification kept on init",
      len(fs.data["pending_tasks"]) == 1 and fs.data["pending_tasks"][0]["id"] == "b")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
