"""
Universal persistent pending-tasks system.

Replaces the old per-flow ``ctx.chat_data["pending_X"]`` in-memory state
(sales confirm, receipt confirm, new-item classification, disambiguation,
bulk-stock zero confirmation, AI clarification, ...) with a single
persistent store. Tasks survive bot restarts and never silently expire —
they live until explicitly completed (finished) or cancelled.

Persists under the top-level ``pending_tasks`` key of the LocalJsonStore's
``self.data`` dict (i.e. ``data/cafe_data.json``).
"""

import uuid
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _relative_time(ts: str) -> str:
    """Human-readable "how long ago" string for a stored ISO timestamp."""
    try:
        then = _parse_iso(ts)
        now = datetime.now(then.tzinfo) if then.tzinfo else datetime.now()
        delta = now - then
        secs = delta.total_seconds()
        if secs < 0:
            secs = 0
        if secs < 60:
            return "just now"
        mins = int(secs // 60)
        if mins < 60:
            return f"{mins}m ago"
        hours = int(mins // 60)
        if hours < 24:
            return f"{hours}h ago"
        days = int(hours // 24)
        return f"{days}d ago"
    except Exception:
        return ""


class PendingTasksStore:
    """Single persistent store for all pending/unfinished bot flows.

    Backed by a LocalJsonStore-like object exposing a ``.data`` dict.
    Tasks have NO expiry — they persist until explicitly completed or
    cancelled (by the user, via AI-detected cancel intent, or by a
    handler once the flow resolves).
    """

    def __init__(self, store):
        self.store = store
        if "pending_tasks" not in self.store.data:
            self.store.data["pending_tasks"] = []

    # ─── internal helpers ──────────────────────────────────────
    @property
    def _tasks(self) -> list:
        return self.store.data.setdefault("pending_tasks", [])

    def _persist(self):
        """Write through to disk without triggering a Sheets sync."""
        save = getattr(self.store, "_save_local_only", None)
        if callable(save):
            save()

    # ─── CRUD ───────────────────────────────────────────────────
    def add(self, chat_id: int, task_type: str, data: dict, summary: str) -> str:
        """Add a pending task. Returns the new task's id (uuid4 hex str).

        No expiry: the task persists until cancel/complete.
        """
        task_id = uuid.uuid4().hex
        task = {
            "id": task_id,
            "chat_id": chat_id,
            "type": task_type,
            "summary": summary,
            "data": data if data is not None else {},
            "created_at": _now_iso(),
            "last_nudged_at": None,
        }
        self._tasks.append(task)
        self._persist()
        return task_id

    def get_for_chat(self, chat_id: int) -> list:
        """All pending tasks for this chat, oldest first."""
        return [t for t in self._tasks if t.get("chat_id") == chat_id]

    def get_all_chat_ids(self) -> list:
        """Unique chat_ids that have at least one pending task."""
        return list({t["chat_id"] for t in self._tasks})

    def get_by_id(self, task_id: str):
        for t in self._tasks:
            if t.get("id") == task_id:
                return t
        return None

    def get_by_type(self, chat_id: int, task_type: str):
        """Latest pending task of a given type for this chat, or None."""
        matches = [
            t for t in self._tasks
            if t.get("chat_id") == chat_id and t.get("type") == task_type
        ]
        if not matches:
            return None
        return matches[-1]

    def complete(self, task_id: str) -> bool:
        """Remove a task (finished or cancelled). Returns True if found."""
        tasks = self._tasks
        for i, t in enumerate(tasks):
            if t.get("id") == task_id:
                del tasks[i]
                self._persist()
                return True
        return False

    def cancel_all(self, chat_id: int) -> int:
        """Cancel all pending tasks for a chat. Returns count cancelled."""
        tasks = self._tasks
        remaining = [t for t in tasks if t.get("chat_id") != chat_id]
        cancelled = len(tasks) - len(remaining)
        if cancelled:
            tasks[:] = remaining
            self._persist()
        return cancelled

    def mark_nudged(self, task_id: str):
        """Update last_nudged_at timestamp for a task."""
        t = self.get_by_id(task_id)
        if t is not None:
            t["last_nudged_at"] = _now_iso()
            self._persist()

    # ─── reminders ──────────────────────────────────────────────
    def format_reminder_list(self, chat_id: int) -> str:
        """Numbered markdown list of all pending tasks for a chat.

        Empty string if none.
        """
        tasks = self.get_for_chat(chat_id)
        if not tasks:
            return ""
        lines = ["⏳ Unfinished tasks — please respond or cancel:"]
        for i, t in enumerate(tasks, start=1):
            when = _relative_time(t.get("created_at", ""))
            suffix = f" (created {when})" if when else ""
            lines.append(f"{i}. {t.get('summary', '')}{suffix}")
        return "\n".join(lines)

    def nudge_candidates(self, min_age_hours: int = 24) -> list:
        """Tasks older than min_age_hours AND (never nudged OR nudged
        more than min_age_hours - 1 hours ago).

        Concretely: never nudged, or last nudged >23h ago (per spec).
        """
        now = datetime.now(timezone.utc).astimezone()
        candidates = []
        for t in self._tasks:
            created = t.get("created_at")
            if not created:
                continue
            try:
                created_dt = _parse_iso(created)
            except Exception:
                continue
            age_hours = (now - created_dt).total_seconds() / 3600.0
            if age_hours < min_age_hours:
                continue
            last_nudged = t.get("last_nudged_at")
            if not last_nudged:
                candidates.append(t)
                continue
            try:
                nudged_dt = _parse_iso(last_nudged)
            except Exception:
                candidates.append(t)
                continue
            nudged_age_hours = (now - nudged_dt).total_seconds() / 3600.0
            if nudged_age_hours > (min_age_hours - 1):
                candidates.append(t)
        return candidates
