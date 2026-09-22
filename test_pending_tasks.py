#!/usr/bin/env python3
"""Unit tests for pending_tasks.PendingTasksStore. Run: python3 test_pending_tasks.py"""

import sys
from datetime import datetime, timedelta, timezone

from pending_tasks import PendingTasksStore


class FakeStore:
    """Minimal stand-in for LocalJsonStore: exposes .data and _save_local_only."""

    def __init__(self, data=None):
        self.data = data if data is not None else {}
        self.save_calls = 0

    def _save_local_only(self):
        self.save_calls += 1


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


def iso(dt: datetime) -> str:
    return dt.isoformat()


# 1. add/get_for_chat
fs = FakeStore()
pts = PendingTasksStore(fs)
id1 = pts.add(100, "sales", {"a": 1}, "Sales RM100 — confirm?")
id2 = pts.add(100, "receipts", {"b": 2}, "Receipt from X RM50 — confirm?")
id3 = pts.add(100, "new_item", {"c": 3}, "Widget — regular stock?")
id4 = pts.add(200, "sales", {"d": 4}, "Sales RM20 — confirm?")

chat100 = pts.get_for_chat(100)
chat200 = pts.get_for_chat(200)
check("add/get_for_chat: chat100 has 3", len(chat100) == 3)
check("add/get_for_chat: chat200 has 1", len(chat200) == 1)
check("add/get_for_chat: chat100 oldest-first", [t["id"] for t in chat100] == [id1, id2, id3])

# 2. get_by_type
sales_task = pts.get_by_type(100, "sales")
check("get_by_type: returns sales task", sales_task is not None and sales_task["id"] == id1)
check("get_by_type: no match returns None", pts.get_by_type(100, "bulk_zeroes") is None)

# 3. complete
fs2 = FakeStore()
pts2 = PendingTasksStore(fs2)
tid = pts2.add(1, "sales", {}, "test")
ok = pts2.complete(tid)
check("complete: returns True when found", ok is True)
check("complete: get_for_chat now empty", pts2.get_for_chat(1) == [])
check("complete: returns False when not found", pts2.complete("nonexistent") is False)

# 4. cancel_all
fs3 = FakeStore()
pts3 = PendingTasksStore(fs3)
pts3.add(5, "sales", {}, "a")
pts3.add(5, "receipts", {}, "b")
pts3.add(5, "new_item", {}, "c")
pts3.add(6, "sales", {}, "other chat")
count = pts3.cancel_all(5)
check("cancel_all: returns count 3", count == 3)
check("cancel_all: chat 5 now empty", pts3.get_for_chat(5) == [])
check("cancel_all: chat 6 untouched", len(pts3.get_for_chat(6)) == 1)

# 5. persistence
fs4 = FakeStore()
pts4 = PendingTasksStore(fs4)
tid4 = pts4.add(9, "clarification", {"q": "which item?"}, "Clarification needed: which item?")
# Simulate restart: new store instance built from same underlying data dict
pts4b = PendingTasksStore(fs4)
found = pts4b.get_by_id(tid4)
check("persistence: task survives new store instance from same data", found is not None)
check("persistence: task data intact", found is not None and found["data"] == {"q": "which item?"})

# 6. format_reminder_list
fs5 = FakeStore()
pts5 = PendingTasksStore(fs5)
pts5.add(1, "sales", {}, "Sales RM100 on 22/09 — confirm to save?")
pts5.add(1, "bulk_zeroes", {}, "Bulk stock 22/09 — 3 blank items — zero or skip?")
listing = pts5.format_reminder_list(1)
check("format_reminder_list: contains first summary", "Sales RM100 on 22/09" in listing)
check("format_reminder_list: contains second summary", "Bulk stock 22/09" in listing)
check("format_reminder_list: numbered 1.", "1." in listing)
check("format_reminder_list: numbered 2.", "2." in listing)

# 7. format_reminder_list empty
fs6 = FakeStore()
pts6 = PendingTasksStore(fs6)
check("format_reminder_list empty: no tasks -> empty string", pts6.format_reminder_list(999) == "")

# 8. nudge_candidates
fs7 = FakeStore()
pts7 = PendingTasksStore(fs7)
now = datetime.now(timezone.utc).astimezone()


def add_task_at(store: PendingTasksStore, chat_id, hours_ago, nudged_hours_ago=None):
    tid = store.add(chat_id, "sales", {}, "x")
    t = store.get_by_id(tid)
    t["created_at"] = iso(now - timedelta(hours=hours_ago))
    if nudged_hours_ago is not None:
        t["last_nudged_at"] = iso(now - timedelta(hours=nudged_hours_ago))
    return tid

# task created 2h ago -> not a candidate
t_recent = add_task_at(pts7, 1, 2)
# task created 25h ago, never nudged -> IS candidate
t_old_never_nudged = add_task_at(pts7, 1, 25)
# task created 25h ago, nudged 1h ago -> NOT candidate
t_old_recently_nudged = add_task_at(pts7, 1, 25, nudged_hours_ago=1)
# task created 25h ago, nudged 25h ago -> IS candidate
t_old_stale_nudge = add_task_at(pts7, 1, 25, nudged_hours_ago=25)

candidates = pts7.nudge_candidates(min_age_hours=24)
cand_ids = {t["id"] for t in candidates}

check("nudge_candidates: 2h old excluded", t_recent not in cand_ids)
check("nudge_candidates: 25h old never-nudged included", t_old_never_nudged in cand_ids)
check("nudge_candidates: 25h old nudged 1h ago excluded", t_old_recently_nudged not in cand_ids)
check("nudge_candidates: 25h old nudged 25h ago included", t_old_stale_nudge in cand_ids)

# 9. mark_nudged
fs8 = FakeStore()
pts8 = PendingTasksStore(fs8)
tid8 = pts8.add(1, "sales", {}, "x")
before = pts8.get_by_id(tid8)
check("mark_nudged: initially None", before["last_nudged_at"] is None)
pts8.mark_nudged(tid8)
after = pts8.get_by_id(tid8)
check("mark_nudged: last_nudged_at set", after["last_nudged_at"] is not None)

# 10. task IDs are unique
fs9 = FakeStore()
pts9 = PendingTasksStore(fs9)
ids = [pts9.add(1, "sales", {}, f"task {i}") for i in range(5)]
check("task IDs are unique", len(set(ids)) == 5)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
