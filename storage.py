"""
Café Manager Bot — Storage Layer (Google Sheets + Local JSON)
Uses local JSON for fast reads, syncs to Google Sheets for visibility & backup.
"""
import json
import os
import logging
import threading
import time as _time
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import re as _re
import config

_TZ = ZoneInfo(config.TIMEZONE)


def _now():
    """Current time in configured timezone (MYT)."""
    return datetime.now(_TZ)

logger = logging.getLogger(__name__)


def normalize_item_name(name: str) -> str:
    """Normalize item name so slight variations match.
    'Coconut (Toasted, 100g)' and 'Coconut - Toasted, 100g' → same key."""
    s = name.strip()
    s = _re.sub(r'[(){}[\]]', ' ', s)       # Remove brackets/parens
    s = _re.sub(r'[-/\\.,;:]+', ' ', s)      # Dashes, slashes, dots → space
    s = _re.sub(r'\s+', ' ', s).strip()      # Collapse whitespace
    return s.lower()

# ─── Try Google Sheets ─────────────────────────────────────
try:
    import gspread
    from google.oauth2.service_account import Credentials
    HAS_GSPREAD = True
except ImportError:
    HAS_GSPREAD = False
    logger.info("gspread not installed — using local JSON storage only")


DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


# ═══════════════════════════════════════════════════════════
#  GOOGLE SHEETS SYNC
# ═══════════════════════════════════════════════════════════

class SheetsSync:
    """Mirrors key data to Google Sheets for visibility & backup."""

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    # Worksheet definitions: name → column headers
    # Only tabs managed by storage.py SheetsSync
    # Other tabs (Expenses Detail, Expenses, Monthly Summary, Daily Sales,
    # POS Reports) are managed by google_integration.py
    WORKSHEETS = {
        "Stock": ["Item"],  # Date columns added dynamically
        "Shopping List": ["Item", "Added By", "Urgency", "Status", "Added At", "Bought At"],
        "Events": ["Title", "Date", "Details", "Added By", "Status"],
    }

    def __init__(self):
        self.client = None
        self.spreadsheet = None
        self._worksheets = {}
        self._connect()

    def _connect(self):
        """Connect to Google Sheets."""
        try:
            creds_file = config.GOOGLE_SHEETS_CREDS_FILE
            if not os.path.exists(creds_file):
                logger.warning(f"Sheets credentials file not found: {creds_file}")
                return

            creds = Credentials.from_service_account_file(creds_file, scopes=self.SCOPES)
            self.client = gspread.authorize(creds)

            # Open by ID if available, otherwise by name
            sheet_id = getattr(config, "SPREADSHEET_ID", "")
            if sheet_id:
                self.spreadsheet = self.client.open_by_key(sheet_id)
            else:
                self.spreadsheet = self.client.open(config.SPREADSHEET_NAME)

            logger.info(f"✅ Connected to Google Sheet: {self.spreadsheet.title}")
            self._ensure_worksheets()

        except Exception as e:
            logger.error(f"Failed to connect to Google Sheets: {e}")
            self.spreadsheet = None

    def _ensure_worksheets(self):
        """Create worksheets if they don't exist."""
        if not self.spreadsheet:
            return
        try:
            existing = {ws.title: ws for ws in self.spreadsheet.worksheets()}

            for name, headers in self.WORKSHEETS.items():
                if name in existing:
                    self._worksheets[name] = existing[name]
                else:
                    ws = self.spreadsheet.add_worksheet(title=name, rows=100, cols=len(headers))
                    ws.update("A1", [headers])
                    # Bold header row
                    ws.format("A1:Z1", {"textFormat": {"bold": True}})
                    self._worksheets[name] = ws
                    logger.info(f"  Created worksheet: {name}")

            # Remove default Sheet1 if it exists and is empty
            if "Sheet1" in existing:
                try:
                    self.spreadsheet.del_worksheet(existing["Sheet1"])
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Error setting up worksheets: {e}")

    def _get_ws(self, name: str):
        """Get worksheet by name, reconnect if needed."""
        if name not in self._worksheets:
            if self.spreadsheet:
                try:
                    self._worksheets[name] = self.spreadsheet.worksheet(name)
                except Exception:
                    return None
        return self._worksheets.get(name)

    def sync_stock(self, stock_data: dict, stock_history: dict = None):
        """Sync stock data to Sheets — history format: items as rows, dates as columns (newest first).
        Uses stock_history {date: {item: qty}} as primary source."""
        ws = self._get_ws("Stock")
        if not ws:
            return
        try:
            from datetime import datetime

            # Build all_items from stock_history
            all_items = {}  # {item_name: {date: qty}}
            dates = set()

            if stock_history:
                for date_str, items in stock_history.items():
                    for item, qty in items.items():
                        if str(qty).upper() == "OK":
                            continue  # Skip legacy "OK" values
                        dates.add(date_str)
                        if item not in all_items:
                            all_items[item] = {}
                        all_items[item][date_str] = qty

            # Also merge existing sheet data (preserve old entries not in history)
            def is_date_col(s):
                try:
                    datetime.strptime(s, "%d/%m/%y")
                    return True
                except (ValueError, TypeError):
                    return False

            existing = ws.get_all_values()
            if existing and existing[0]:
                header = existing[0]
                date_cols = [(i, h) for i, h in enumerate(header[1:], 1) if is_date_col(h)]
                for row in existing[1:]:
                    if row and row[0]:
                        for col_idx, d in date_cols:
                            val = row[col_idx] if col_idx < len(row) else ""
                            if val and str(val).upper() != "OK":
                                dates.add(d)
                                if row[0] not in all_items:
                                    all_items[row[0]] = {}
                                if d not in all_items[row[0]]:
                                    all_items[row[0]][d] = val

            # Sort dates newest first
            def parse_date(d):
                try:
                    return datetime.strptime(d, "%d/%m/%y")
                except ValueError:
                    return datetime.min
            sorted_dates = sorted(dates, key=parse_date, reverse=True)

            if not sorted_dates:
                return

            # Deduplicate items by normalized name
            merged = {}  # {norm_key: {display_name: str, dates: {d: qty}}}
            for item, date_vals in all_items.items():
                norm = normalize_item_name(item)
                if norm not in merged:
                    merged[norm] = {"display_name": item, "dates": {}}
                for d, v in date_vals.items():
                    # Keep the latest value for each date
                    merged[norm]["dates"][d] = v

            # Build final rows
            header = ["Item"] + sorted_dates
            rows = [header]
            for norm_key, info in sorted(merged.items(), key=lambda x: x[1]["display_name"].lower()):
                row = [info["display_name"]]
                for d in sorted_dates:
                    row.append(info["dates"].get(d, ""))
                rows.append(row)

            ws.clear()
            ws.update("A1", rows)
            col_letter = chr(ord('A') + len(header) - 1) if len(header) <= 26 else 'Z'
            ws.format(f"A1:{col_letter}1", {"textFormat": {"bold": True}})
        except Exception as e:
            logger.error(f"Sheets sync error (Stock): {e}")

    def sync_shopping(self, shopping_data: list):
        """Sync shopping list to Sheets — active items on top, archived (bought) at bottom."""
        ws = self._get_ws("Shopping List")
        if not ws:
            return
        try:
            header = ["Item", "Added By", "Urgency", "Status", "Added At", "Bought At"]
            active = [i for i in shopping_data if not i.get("bought")]
            bought = [i for i in shopping_data if i.get("bought")]

            rows = [header]

            # Active items first
            for item in active:
                rows.append([
                    item.get("item", ""),
                    item.get("added_by", ""),
                    item.get("urgency", "normal"),
                    "🔴 Need to buy",
                    item.get("added_at", ""),
                    "",
                ])

            # Separator row
            if bought:
                rows.append(["── ARCHIVED (Bought) ──", "", "", "", "", ""])

            # Bought items at the bottom
            for item in bought:
                rows.append([
                    item.get("item", ""),
                    item.get("added_by", ""),
                    item.get("urgency", "normal"),
                    "✅ Bought",
                    item.get("added_at", ""),
                    item.get("bought_at", ""),
                ])

            ws.clear()
            if rows:
                ws.update("A1", rows)
                ws.format("A1:F1", {"textFormat": {"bold": True}})
        except Exception as e:
            logger.error(f"Sheets sync error (Shopping): {e}")

    def sync_events(self, events_data: list):
        """Sync events to Sheets."""
        ws = self._get_ws("Events")
        if not ws:
            return
        try:
            rows = [["Title", "Date", "Details", "Added By", "Status"]]
            for event in events_data:
                rows.append([
                    event.get("title", ""),
                    event.get("date", ""),
                    event.get("details", ""),
                    event.get("added_by", ""),
                    event.get("status", ""),
                ])
            ws.clear()
            if rows:
                ws.update("A1", rows)
                ws.format("A1:E1", {"textFormat": {"bold": True}})
        except Exception as e:
            logger.error(f"Sheets sync error (Events): {e}")

    # ─── Read from Sheet (reverse-sync: Sheet → JSON) ────────

    def read_stock_from_sheet(self) -> tuple:
        """Read Stock sheet → return (stock_dict, stock_history_dict).
        Sheet is source of truth — this overwrites local JSON data."""
        ws = self._get_ws("Stock")
        if not ws:
            return None, None
        try:
            from datetime import datetime as _dt
            existing = ws.get_all_values()
            if not existing or not existing[0]:
                return {}, {}

            header = existing[0]

            def is_date_col(s):
                try:
                    _dt.strptime(s, "%d/%m/%y")
                    return True
                except (ValueError, TypeError):
                    return False

            date_cols = [(i, h) for i, h in enumerate(header[1:], 1) if is_date_col(h)]

            # Parse dates for sorting (find latest per item)
            def parse_d(d):
                try:
                    return _dt.strptime(d, "%d/%m/%y")
                except ValueError:
                    return _dt.min

            stock = {}
            history = {}

            for row in existing[1:]:
                if not row or not row[0]:
                    continue
                item = row[0].strip()
                if not item:
                    continue

                latest_qty = ""
                latest_date_parsed = _dt.min

                for col_idx, d in date_cols:
                    val = (row[col_idx] if col_idx < len(row) else "").strip()
                    if val and val.upper() != "OK":
                        # Add to history
                        if d not in history:
                            history[d] = {}
                        history[d][item] = val
                        # Track latest
                        pd = parse_d(d)
                        if pd > latest_date_parsed:
                            latest_date_parsed = pd
                            latest_qty = val

                if latest_qty:
                    stock[item] = {
                        "qty": latest_qty,
                        "updated_by": "sheet",
                        "updated_at": _dt.now().isoformat(),
                    }

            logger.info(f"Read {len(stock)} stock items from Sheet")
            return stock, history

        except Exception as e:
            logger.error(f"Error reading stock from Sheet: {e}")
            return None, None

    def read_shopping_from_sheet(self) -> list:
        """Read Shopping List sheet → return shopping_list array."""
        ws = self._get_ws("Shopping List")
        if not ws:
            return None
        try:
            existing = ws.get_all_values()
            if not existing or len(existing) < 2:
                return []

            shopping = []
            in_archive = False

            for row in existing[1:]:  # Skip header
                if not row or not row[0]:
                    continue
                if "ARCHIVED" in row[0]:
                    in_archive = True
                    continue

                item_name = row[0].strip()
                if not item_name:
                    continue

                entry = {
                    "item": item_name,
                    "added_by": row[1] if len(row) > 1 else "",
                    "urgency": row[2] if len(row) > 2 else "normal",
                    "added_at": row[4] if len(row) > 4 else "",
                }

                if in_archive or (len(row) > 3 and "Bought" in str(row[3])):
                    entry["bought"] = True
                    entry["bought_at"] = row[5] if len(row) > 5 else ""
                else:
                    entry["bought"] = False

                shopping.append(entry)

            logger.info(f"Read {len(shopping)} shopping items from Sheet")
            return shopping

        except Exception as e:
            logger.error(f"Error reading shopping from Sheet: {e}")
            return None

    def read_events_from_sheet(self) -> list:
        """Read Events sheet → return events array."""
        ws = self._get_ws("Events")
        if not ws:
            return None
        try:
            existing = ws.get_all_values()
            if not existing or len(existing) < 2:
                return []

            events = []
            for row in existing[1:]:  # Skip header
                if not row or not row[0]:
                    continue
                events.append({
                    "title": row[0] if len(row) > 0 else "",
                    "date": row[1] if len(row) > 1 else "",
                    "details": row[2] if len(row) > 2 else "",
                    "added_by": row[3] if len(row) > 3 else "",
                    "status": row[4] if len(row) > 4 else "",
                })

            logger.info(f"Read {len(events)} events from Sheet")
            return events

        except Exception as e:
            logger.error(f"Error reading events from Sheet: {e}")
            return None

    # ─── Direct writes to Sheet ──────────────────────────────

    def write_stock_item(self, item: str, qty: str, date_str: str):
        """Write a single stock item to the Sheet (update or add row/col)."""
        ws = self._get_ws("Stock")
        if not ws:
            return
        try:
            from datetime import datetime as _dt
            existing = ws.get_all_values()

            if not existing or not existing[0]:
                # Sheet is empty — create header + first row
                ws.update("A1", [["Item", date_str], [item, qty]])
                ws.format("A1:B1", {"textFormat": {"bold": True}})
                return

            header = existing[0]

            # Find or create date column
            if date_str in header:
                col_idx = header.index(date_str)
            else:
                # Insert as column B (newest date first, after "Item")
                col_idx = 1
                # Shift existing date columns right by inserting new col
                new_header = [header[0], date_str] + header[1:]
                new_rows = [new_header]
                for row in existing[1:]:
                    new_rows.append([row[0] if row else "", ""] + row[1:])
                ws.clear()
                ws.update("A1", new_rows)
                existing = new_rows
                header = new_header

            # Find item row (by normalized name)
            norm = normalize_item_name(item)
            row_idx = None
            for i, row in enumerate(existing[1:], 1):
                if row and normalize_item_name(row[0]) == norm:
                    row_idx = i
                    break

            if row_idx is not None:
                # Update existing row
                cell = gspread.utils.rowcol_to_a1(row_idx + 1, col_idx + 1)
                ws.update_acell(cell, qty)
            else:
                # Append new row
                new_row = [""] * len(header)
                new_row[0] = item
                new_row[col_idx] = qty
                ws.append_row(new_row)

            ws.format("A1:Z1", {"textFormat": {"bold": True}})

        except Exception as e:
            logger.error(f"Sheet write error (stock item): {e}")

    def remove_stock_item(self, item: str):
        """Remove a stock item row from the Sheet."""
        ws = self._get_ws("Stock")
        if not ws:
            return
        try:
            existing = ws.get_all_values()
            if not existing:
                return
            norm = normalize_item_name(item)
            for i, row in enumerate(existing[1:], 2):  # Sheet rows are 1-indexed, +1 for header
                if row and normalize_item_name(row[0]) == norm:
                    ws.delete_rows(i)
                    logger.info(f"Removed stock row from Sheet: {row[0]}")
                    return
        except Exception as e:
            logger.error(f"Sheet remove error (stock): {e}")

    def write_shopping_item(self, item_data: dict):
        """Append a shopping item to the Sheet."""
        ws = self._get_ws("Shopping List")
        if not ws:
            return
        try:
            # Find the archive separator — insert before it
            existing = ws.get_all_values()
            insert_row = None
            for i, row in enumerate(existing):
                if row and "ARCHIVED" in str(row[0]):
                    insert_row = i + 1  # 1-indexed
                    break

            new_row = [
                item_data.get("item", ""),
                item_data.get("added_by", ""),
                item_data.get("urgency", "normal"),
                "🔴 Need to buy",
                item_data.get("added_at", ""),
                "",
            ]

            if insert_row:
                ws.insert_row(new_row, insert_row)
            else:
                ws.append_row(new_row)

        except Exception as e:
            logger.error(f"Sheet write error (shopping): {e}")

    def write_event(self, event_data: dict):
        """Append an event to the Sheet."""
        ws = self._get_ws("Events")
        if not ws:
            return
        try:
            ws.append_row([
                event_data.get("title", ""),
                event_data.get("date", ""),
                event_data.get("details", ""),
                event_data.get("added_by", ""),
                event_data.get("status", "upcoming"),
            ])
        except Exception as e:
            logger.error(f"Sheet write error (event): {e}")

    # ─── Full sync (JSON → Sheet) ──────────────────────────

    def sync_all(self, data: dict):
        """Sync all data to Sheets with delays to avoid rate limits."""
        if not self.spreadsheet:
            return
        syncs = [
            ("Stock", lambda: self.sync_stock(data.get("stock", {}), data.get("stock_history", {}))),
            ("Shopping", lambda: self.sync_shopping(data.get("shopping_list", []))),
            ("Events", lambda: self.sync_events(data.get("events", []))),
        ]
        for name, sync_fn in syncs:
            try:
                sync_fn()
                _time.sleep(3)  # 3s delay between worksheets to avoid rate limit
            except Exception as e:
                logger.error(f"Sheets sync error ({name}): {e}")


# ═══════════════════════════════════════════════════════════
#  LOCAL JSON STORE (with Sheets sync)
# ═══════════════════════════════════════════════════════════

class LocalJsonStore:
    """JSON file storage — Google Sheets is the source of truth.
    JSON is a fast local cache. On startup, Sheet data overwrites JSON.
    Bot writes go to JSON first, then sync to Sheet."""

    def __init__(self):
        self.file = DATA_DIR / "cafe_data.json"
        self.data = self._load()
        self._sheets = None
        self._pending_syncs = set()   # Collect changed categories
        self._sync_timer = None       # Debounce timer
        self._sync_lock = threading.Lock()
        self._refresh_timer = None    # Periodic Sheet → JSON refresh

        # Initialize Sheets sync
        if HAS_GSPREAD:
            try:
                self._sheets = SheetsSync()
                if self._sheets.spreadsheet:
                    logger.info("Google Sheets sync enabled")
                    # Read from Sheet (Sheet is source of truth)
                    self._refresh_from_sheet()
                    # Start periodic refresh (every 10 min)
                    self._start_periodic_refresh()
                else:
                    self._sheets = None
            except Exception as e:
                logger.error(f"Sheets sync init failed: {e}")
                self._sheets = None

    def _refresh_from_sheet(self):
        """Read Sheet → update local JSON cache. Sheet wins all conflicts."""
        if not self._sheets:
            return
        try:
            # Stock
            stock, history = self._sheets.read_stock_from_sheet()
            if stock is not None:
                self.data["stock"] = stock
            if history is not None:
                self.data["stock_history"] = history

            _time.sleep(2)  # Rate limit gap

            # Shopping List
            shopping = self._sheets.read_shopping_from_sheet()
            if shopping is not None:
                self.data["shopping_list"] = shopping

            _time.sleep(2)

            # Events
            events = self._sheets.read_events_from_sheet()
            if events is not None:
                self.data["events"] = events

            # Save updated cache to JSON (don't trigger sync back)
            self._save_local_only()
            logger.info("Sheet → JSON refresh complete (Sheet is source of truth)")

        except Exception as e:
            logger.error(f"Error refreshing from Sheet: {e}")

    def _save_local_only(self):
        """Save to JSON file WITHOUT triggering sync to Sheets."""
        with open(self.file, "w") as f:
            json.dump(self.data, f, indent=2, default=str)

    def _start_periodic_refresh(self):
        """Refresh from Sheet every 10 minutes to pick up manual edits."""
        def _do_refresh():
            try:
                self._refresh_from_sheet()
            except Exception as e:
                logger.error(f"Periodic sheet refresh error: {e}")
            finally:
                # Schedule next refresh
                self._refresh_timer = threading.Timer(600, _do_refresh)
                self._refresh_timer.daemon = True
                self._refresh_timer.start()

        self._refresh_timer = threading.Timer(600, _do_refresh)
        self._refresh_timer.daemon = True
        self._refresh_timer.start()
        logger.info("Periodic Sheet refresh scheduled (every 10 min)")

    def _load(self) -> dict:
        if self.file.exists():
            with open(self.file, "r") as f:
                data = json.load(f)
            self._cleanup_ok_values(data)
            return data
        return self._default_data()

    @staticmethod
    def _cleanup_ok_values(data: dict):
        """Remove any legacy 'OK' qty values from stock and stock_history."""
        # Clean stock dict
        stock = data.get("stock", {})
        bad_keys = [k for k, v in stock.items()
                    if str(v.get("qty", "")).upper() == "OK"]
        for k in bad_keys:
            del stock[k]
            logger.info(f"Cleaned 'OK' stock entry: {k}")

        # Clean stock_history
        history = data.get("stock_history", {})
        for date_str in list(history.keys()):
            items = history[date_str]
            bad = [k for k, v in items.items() if str(v).upper() == "OK"]
            for k in bad:
                del items[k]
                logger.info(f"Cleaned 'OK' history entry: {date_str}/{k}")
            if not items:
                del history[date_str]

    def _save(self, changed: str = "all"):
        with open(self.file, "w") as f:
            json.dump(self.data, f, indent=2, default=str)
        # Sync to Sheets in background thread (non-blocking)
        if self._sheets:
            self._sync_to_sheets_bg(changed)

    def _sync_to_sheets_bg(self, changed: str):
        """Debounced sync — waits 5s for more changes, then syncs all at once."""
        if changed == "all":
            # Full sync runs immediately in background (with delays between worksheets)
            def _do_full():
                try:
                    self._sheets.sync_all(self.data)
                    logger.info("Sheets sync: initial full sync complete")
                except Exception as e:
                    logger.error(f"Background sheets full sync error: {e}")
            thread = threading.Thread(target=_do_full, daemon=True)
            thread.start()
            return

        # Debounced sync: collect changes, wait 5s, then sync batch
        with self._sync_lock:
            self._pending_syncs.add(changed)
            # Cancel existing timer
            if self._sync_timer:
                self._sync_timer.cancel()
            # Start new 5s timer
            self._sync_timer = threading.Timer(5.0, self._flush_syncs)
            self._sync_timer.daemon = True
            self._sync_timer.start()

    def _flush_syncs(self):
        """Actually run the pending syncs after debounce delay."""
        with self._sync_lock:
            pending = self._pending_syncs.copy()
            self._pending_syncs.clear()
            self._sync_timer = None

        if not pending:
            return

        sync_map = {
            "stock": lambda: self._sheets.sync_stock(self.data.get("stock", {}), self.data.get("stock_history", {})),
            "shopping": lambda: self._sheets.sync_shopping(self.data.get("shopping_list", [])),
            "events": lambda: self._sheets.sync_events(self.data.get("events", [])),
        }

        def _do_batch():
            for key in pending:
                fn = sync_map.get(key)
                if fn:
                    try:
                        fn()
                        logger.info(f"Sheets synced: {key}")
                        _time.sleep(2)  # 2s gap between worksheets
                    except Exception as e:
                        logger.error(f"Sheets sync error ({key}): {e}")

        thread = threading.Thread(target=_do_batch, daemon=True)
        thread.start()

    def _default_data(self) -> dict:
        return {
            "stock": {},
            "cleaning_log": [],
            "checklist_log": [],
            "shifts": {},
            "hours": {},
            "holiday_hours": {},
            "events": [],
            "shopping_list": [],
            "content_log": [],
            "content_calendar": [],
            "staff": {},
            "daily_reports": [],
            "settings": {},
            "action_items": [],
            "custom_instructions": [],
            "stock_history": {},  # {date_str: {item: qty, ...}}
        }

    # ─── Stock ──────────────────────────────────────────────
    def get_stock(self) -> dict:
        return self.data.get("stock", {})

    def _find_existing_stock_name(self, new_name: str) -> str:
        """Find an existing stock item that matches by normalized name.
        Returns the existing key if found, otherwise the new_name."""
        new_norm = normalize_item_name(new_name)
        for existing in self.data.get("stock", {}):
            if normalize_item_name(existing) == new_norm:
                return existing
        return new_name

    def update_stock(self, item: str, qty: str, updated_by: str):
        # Never store "OK" — default to "1" if somehow passed
        if str(qty).upper() == "OK":
            qty = "1"
        # Deduplicate: use existing name if it matches
        item = self._find_existing_stock_name(item)
        today = _now().strftime("%d/%m/%y")

        # 1. Write to Sheet FIRST (source of truth)
        if self._sheets:
            try:
                self._sheets.write_stock_item(item, qty, today)
            except Exception as e:
                logger.error(f"Direct sheet write failed (stock): {e}")

        # 2. Update local JSON cache
        self.data["stock"][item] = {
            "qty": qty,
            "updated_by": updated_by,
            "updated_at": _now().isoformat(),
        }
        if "stock_history" not in self.data:
            self.data["stock_history"] = {}
        if today not in self.data["stock_history"]:
            self.data["stock_history"][today] = {}
        self.data["stock_history"][today][item] = qty
        self._save_local_only()

    def remove_stock(self, item: str) -> bool:
        """Remove a stock item from Sheet + JSON cache. Returns True if found."""
        norm = normalize_item_name(item)
        removed = False

        # 1. Remove from Sheet FIRST
        if self._sheets:
            try:
                self._sheets.remove_stock_item(item)
            except Exception as e:
                logger.error(f"Sheet remove failed (stock): {e}")

        # 2. Remove from JSON cache
        to_del = [k for k in self.data.get("stock", {})
                  if normalize_item_name(k) == norm]
        for k in to_del:
            del self.data["stock"][k]
            removed = True

        for date_str in list(self.data.get("stock_history", {}).keys()):
            items_on_date = self.data["stock_history"][date_str]
            to_del = [k for k in items_on_date if normalize_item_name(k) == norm]
            for k in to_del:
                del items_on_date[k]
                removed = True
            if not items_on_date:
                del self.data["stock_history"][date_str]

        if removed:
            self._save_local_only()
            logger.info(f"Removed stock item: {item}")
        return removed

    def update_stock_bulk(self, items: list, stock_date: str = None):
        """Update multiple stock items under a specific date.
        stock_date should be dd/mm/yy format. Defaults to today."""
        if not stock_date:
            stock_date = _now().strftime("%d/%m/%y")

        if "stock_history" not in self.data:
            self.data["stock_history"] = {}
        if stock_date not in self.data["stock_history"]:
            self.data["stock_history"][stock_date] = {}

        for entry in items:
            item_name = entry.get("item", "")
            qty = entry.get("qty", "—")
            if item_name:
                # 1. Write to Sheet FIRST
                if self._sheets:
                    try:
                        self._sheets.write_stock_item(item_name, qty, stock_date)
                    except Exception as e:
                        logger.error(f"Direct sheet write failed (stock bulk): {e}")

                # 2. Update JSON cache
                self.data["stock"][item_name] = {
                    "qty": qty,
                    "updated_by": entry.get("checked_by", ""),
                    "updated_at": _now().isoformat(),
                }
                self.data["stock_history"][stock_date][item_name] = qty

        self._save_local_only()

    def check_low_stock(self, items_updated: list = None) -> list:
        """Check stock against SOP minimums. Returns list of {item, qty, min} for low items.
        If items_updated is provided, only checks those items."""
        from sop_data import STOCK_MINIMUMS
        import re

        low_items = []
        stock = self.data.get("stock", {})

        items_to_check = items_updated if items_updated else stock.keys()

        for item_name in items_to_check:
            info = stock.get(item_name, {})
            qty_str = str(info.get("qty", "")).strip()

            # Find matching minimum (fuzzy match)
            min_info = None
            for min_name, min_data in STOCK_MINIMUMS.items():
                if min_name.lower() == item_name.lower():
                    min_info = min_data
                    break
            if not min_info:
                # Try partial match
                for min_name, min_data in STOCK_MINIMUMS.items():
                    if min_name.lower() in item_name.lower() or item_name.lower() in min_name.lower():
                        min_info = min_data
                        break

            if not min_info:
                continue

            # Extract numeric value from qty
            nums = re.findall(r'[\d.]+', qty_str)
            if not nums:
                # Non-numeric qty like "OK", "LOW", "OUT"
                if qty_str.upper() in ("LOW", "OUT", "0"):
                    low_items.append({
                        "item": item_name,
                        "qty": qty_str,
                        "min": min_info["min"],
                        "unit": min_info.get("unit", ""),
                    })
                continue

            qty_num = float(nums[0])
            if qty_num < min_info["min"]:
                low_items.append({
                    "item": item_name,
                    "qty": qty_str,
                    "min": min_info["min"],
                    "unit": min_info.get("unit", ""),
                })

        return low_items

    def get_low_stock(self) -> list:
        return [
            (item, info)
            for item, info in self.data.get("stock", {}).items()
            if info.get("qty", "").upper() in ("LOW", "OUT", "0")
        ]

    # ─── Operations Checklists ─────────────────────────────
    def get_ops_checklist_status(self, checklist_type: str, date_str: str = None) -> dict:
        """Get completion status for a checklist today (or specified date).
        Returns {items: [...], completed: [...], remaining: [...]}"""
        from sop_data import OPS_CHECKLISTS

        if not date_str:
            date_str = _now().strftime("%Y-%m-%d")

        all_items = OPS_CHECKLISTS.get(checklist_type, [])
        completed = self.data.get("ops_checklists", {}).get(date_str, {}).get(checklist_type, [])

        return {
            "items": all_items,
            "completed": completed,
            "remaining": [item for item in all_items if item not in completed],
        }

    def mark_checklist_done(self, checklist_type: str, items: list, done_by: str) -> dict:
        """Mark checklist items as done. items=["all"] marks everything.
        Returns {completed_count, total, newly_done}"""
        from sop_data import OPS_CHECKLISTS

        date_str = _now().strftime("%Y-%m-%d")
        all_items = OPS_CHECKLISTS.get(checklist_type, [])

        if "ops_checklists" not in self.data:
            self.data["ops_checklists"] = {}
        if date_str not in self.data["ops_checklists"]:
            self.data["ops_checklists"][date_str] = {}
        if checklist_type not in self.data["ops_checklists"][date_str]:
            self.data["ops_checklists"][date_str][checklist_type] = []

        completed = self.data["ops_checklists"][date_str][checklist_type]
        newly_done = []

        if items == ["all"]:
            newly_done = [item for item in all_items if item not in completed]
            completed.extend(newly_done)
        else:
            for item in items:
                # Fuzzy match against checklist items
                matched = None
                for ci in all_items:
                    if item.lower() in ci.lower() or ci.lower() in item.lower():
                        matched = ci
                        break
                if matched and matched not in completed:
                    completed.append(matched)
                    newly_done.append(matched)

        self.data["ops_checklists"][date_str][checklist_type] = completed
        self._save("ops_checklists")

        return {
            "completed_count": len(completed),
            "total": len(all_items),
            "newly_done": newly_done,
        }

    # ─── Cleaning ───────────────────────────────────────────
    def log_cleaning(self, zone: str, done_by: str):
        self.data["cleaning_log"].append({
            "zone": zone,
            "done_by": done_by,
            "done_at": _now().isoformat(),
        })
        self._save("cleaning")

    def get_cleaning_today(self) -> list:
        today = date.today().isoformat()
        return [
            e for e in self.data.get("cleaning_log", [])
            if e.get("done_at", "").startswith(today)
        ]

    # ─── Checklists ─────────────────────────────────────────
    def log_checklist(self, checklist_type: str, items_done: list, done_by: str):
        self.data["checklist_log"].append({
            "type": checklist_type,
            "items_done": items_done,
            "done_by": done_by,
            "done_at": _now().isoformat(),
        })
        self._save()

    def get_checklist_today(self, checklist_type: str) -> Optional[dict]:
        today = date.today().isoformat()
        for entry in reversed(self.data.get("checklist_log", [])):
            if (entry.get("type") == checklist_type and
                entry.get("done_at", "").startswith(today)):
                return entry
        return None

    # ─── Shifts ─────────────────────────────────────────────
    def get_shifts(self, day: str = None) -> dict:
        if day:
            return self.data.get("shifts", {}).get(day, {})
        return self.data.get("shifts", {})

    def set_shift(self, day: str, staff_name: str, start: str, end: str):
        if day not in self.data["shifts"]:
            self.data["shifts"][day] = {}
        self.data["shifts"][day][staff_name] = {"start": start, "end": end}
        self._save("shifts")

    def remove_shift(self, day: str, staff_name: str):
        if day in self.data.get("shifts", {}) and staff_name in self.data["shifts"][day]:
            del self.data["shifts"][day][staff_name]
            self._save("shifts")

    # ─── Operating Hours ────────────────────────────────────
    def get_hours(self) -> dict:
        return self.data.get("hours", {})

    def set_hours(self, day: str, open_time: str, close_time: str):
        self.data["hours"][day] = {"open": open_time, "close": close_time}
        self._save()

    def set_holiday_hours(self, date_str: str, open_time: str, close_time: str, label: str = ""):
        self.data["holiday_hours"][date_str] = {
            "open": open_time, "close": close_time, "label": label
        }
        self._save()

    def get_holiday_hours(self) -> dict:
        return self.data.get("holiday_hours", {})

    # ─── Events ─────────────────────────────────────────────
    def add_event(self, title: str, event_date: str, details: str, added_by: str):
        entry = {
            "title": title,
            "date": event_date,
            "details": details,
            "added_by": added_by,
            "added_at": _now().isoformat(),
            "status": "upcoming",
        }
        # 1. Write to Sheet FIRST
        if self._sheets:
            try:
                self._sheets.write_event(entry)
            except Exception as e:
                logger.error(f"Direct sheet write failed (event): {e}")

        # 2. Update JSON cache
        self.data["events"].append(entry)
        self._save_local_only()

    def get_events(self, upcoming_only: bool = True) -> list:
        events = self.data.get("events", [])
        if upcoming_only:
            today = date.today().isoformat()
            events = [e for e in events if e.get("date", "") >= today]
        return sorted(events, key=lambda e: e.get("date", ""))

    def complete_event(self, index: int):
        if 0 <= index < len(self.data["events"]):
            self.data["events"][index]["status"] = "done"
            # Full re-sync events to Sheet
            if self._sheets:
                try:
                    self._sheets.sync_events(self.data.get("events", []))
                except Exception as e:
                    logger.error(f"Sheet sync failed (complete_event): {e}")
            self._save_local_only()

    # ─── Shopping / To-Buy List ─────────────────────────────
    def add_shopping_item(self, item: str, added_by: str, urgency: str = "normal"):
        entry = {
            "item": item,
            "added_by": added_by,
            "urgency": urgency,
            "added_at": _now().isoformat(),
            "bought": False,
        }
        # 1. Write to Sheet FIRST
        if self._sheets:
            try:
                self._sheets.write_shopping_item(entry)
            except Exception as e:
                logger.error(f"Direct sheet write failed (shopping): {e}")

        # 2. Update JSON cache
        self.data["shopping_list"].append(entry)
        self._save_local_only()

    def get_shopping_list(self, include_bought: bool = False) -> list:
        items = self.data.get("shopping_list", [])
        if not include_bought:
            items = [i for i in items if not i.get("bought")]
        return items

    def mark_bought(self, index: int):
        pending = [i for i in self.data["shopping_list"] if not i.get("bought")]
        if 0 <= index < len(pending):
            count = 0
            for idx, item in enumerate(self.data["shopping_list"]):
                if not item.get("bought"):
                    if count == index:
                        self.data["shopping_list"][idx]["bought"] = True
                        self.data["shopping_list"][idx]["bought_at"] = _now().isoformat()
                        break
                    count += 1
            # Full re-sync shopping to Sheet (active/archive layout)
            if self._sheets:
                try:
                    self._sheets.sync_shopping(self.data.get("shopping_list", []))
                except Exception as e:
                    logger.error(f"Sheet sync failed (mark_bought): {e}")
            self._save_local_only()

    def clear_bought(self):
        self.data["shopping_list"] = [
            i for i in self.data["shopping_list"] if not i.get("bought")
        ]
        # Full re-sync shopping to Sheet
        if self._sheets:
            try:
                self._sheets.sync_shopping(self.data.get("shopping_list", []))
            except Exception as e:
                logger.error(f"Sheet sync failed (clear_bought): {e}")
        self._save_local_only()

    # ─── Staff Registry ─────────────────────────────────────
    def add_staff(self, name: str, telegram_id: int, role: str = "staff"):
        self.data["staff"][name] = {
            "telegram_id": telegram_id,
            "role": role,
            "added_at": _now().isoformat(),
        }
        self._save("staff")

    def get_staff(self) -> dict:
        return self.data.get("staff", {})

    def remove_staff(self, name: str):
        if name in self.data.get("staff", {}):
            del self.data["staff"][name]
            self._save("staff")

    # ─── Content Log ────────────────────────────────────────
    def log_content(self, idea: str, posted_by: str):
        self.data["content_log"].append({
            "idea": idea,
            "posted_by": posted_by,
            "posted_at": _now().isoformat(),
        })
        self._save()

    def get_content_log(self, days: int = 30) -> list:
        return self.data.get("content_log", [])[-days:]

    # ─── Content Calendar ──────────────────────────────────
    def add_content_plan(self, title: str, content_type: str, planned_date: str,
                         assigned_to: str, added_by: str, notes: str = ""):
        if "content_calendar" not in self.data:
            self.data["content_calendar"] = []
        self.data["content_calendar"].append({
            "title": title,
            "type": content_type,
            "planned_date": planned_date,
            "assigned_to": assigned_to,
            "added_by": added_by,
            "notes": notes,
            "status": "planned",
            "created_at": _now().isoformat(),
            "completed_at": None,
        })
        self._save()

    def get_content_calendar(self, upcoming_only: bool = True) -> list:
        items = self.data.get("content_calendar", [])
        if upcoming_only:
            today = date.today().isoformat()
            items = [c for c in items
                     if c.get("status") in ("planned", "in_progress")
                     or c.get("planned_date", "") >= today]
        return sorted(items, key=lambda c: c.get("planned_date", ""))

    def get_content_today(self) -> list:
        today = date.today().isoformat()
        return [c for c in self.data.get("content_calendar", [])
                if c.get("planned_date") == today
                and c.get("status") in ("planned", "in_progress")]

    def update_content_status(self, index: int, status: str, completed_by: str = ""):
        cal = self.data.get("content_calendar", [])
        if 0 <= index < len(cal):
            cal[index]["status"] = status
            if status == "done":
                cal[index]["completed_at"] = _now().isoformat()
                cal[index]["completed_by"] = completed_by
            self._save()

    def complete_content_by_title(self, title_search: str, completed_by: str = "") -> bool:
        search = title_search.lower()
        for item in self.data.get("content_calendar", []):
            if (search in item.get("title", "").lower()
                    and item.get("status") in ("planned", "in_progress")):
                item["status"] = "done"
                item["completed_at"] = _now().isoformat()
                item["completed_by"] = completed_by
                self._save()
                return True
        return False

    # ─── Daily Reports ──────────────────────────────────────
    def save_daily_report(self, report: dict):
        self.data["daily_reports"].append(report)
        self._save()

    def get_daily_reports(self, days: int = 7) -> list:
        return self.data.get("daily_reports", [])[-days:]

    # ─── Action Items (Chase-up System) ──────────────────────
    def add_action_item(self, task: str, assigned_to: str, mentioned_by: str,
                        source_msg: str = "", urgency: str = "normal"):
        if "action_items" not in self.data:
            self.data["action_items"] = []
        self.data["action_items"].append({
            "task": task,
            "assigned_to": assigned_to,
            "mentioned_by": mentioned_by,
            "source_msg": source_msg[:200],
            "urgency": urgency,
            "status": "pending",
            "created_at": _now().isoformat(),
            "last_chased": None,
            "chase_count": 0,
        })
        self._save("actions")

    def get_action_items(self, status: str = "pending") -> list:
        items = self.data.get("action_items", [])
        if status:
            items = [i for i in items if i.get("status") == status]
        return items

    def complete_action_item(self, index: int, completed_by: str = ""):
        pending = [i for i in self.data.get("action_items", []) if i.get("status") == "pending"]
        if 0 <= index < len(pending):
            count = 0
            for idx, item in enumerate(self.data["action_items"]):
                if item.get("status") == "pending":
                    if count == index:
                        self.data["action_items"][idx]["status"] = "done"
                        self.data["action_items"][idx]["completed_at"] = _now().isoformat()
                        self.data["action_items"][idx]["completed_by"] = completed_by
                        break
                    count += 1
            self._save("actions")

    def mark_action_chased(self, index: int):
        pending = [i for i in self.data.get("action_items", []) if i.get("status") == "pending"]
        if 0 <= index < len(pending):
            count = 0
            for idx, item in enumerate(self.data["action_items"]):
                if item.get("status") == "pending":
                    if count == index:
                        self.data["action_items"][idx]["last_chased"] = _now().isoformat()
                        self.data["action_items"][idx]["chase_count"] = item.get("chase_count", 0) + 1
                        break
                    count += 1
            self._save("actions")

    def dismiss_action_item(self, index: int):
        pending = [i for i in self.data.get("action_items", []) if i.get("status") == "pending"]
        if 0 <= index < len(pending):
            count = 0
            for idx, item in enumerate(self.data["action_items"]):
                if item.get("status") == "pending":
                    if count == index:
                        self.data["action_items"][idx]["status"] = "dismissed"
                        break
                    count += 1
            self._save("actions")

    # ─── Custom Instructions ──────────────────────────────────
    def add_custom_instruction(self, instruction: str, added_by: str):
        if "custom_instructions" not in self.data:
            self.data["custom_instructions"] = []
        self.data["custom_instructions"].append({
            "instruction": instruction,
            "added_by": added_by,
            "date": _now().strftime("%Y-%m-%d %H:%M"),
        })
        self._save("instructions")

    def get_custom_instructions(self) -> list:
        return self.data.get("custom_instructions", [])

    def remove_custom_instruction(self, index: int):
        instructions = self.data.get("custom_instructions", [])
        if 0 <= index < len(instructions):
            instructions.pop(index)
            self._save("instructions")

    # ─── Settings ───────────────────────────────────────────
    def get_setting(self, key: str, default=None):
        return self.data.get("settings", {}).get(key, default)

    def set_setting(self, key: str, value):
        self.data["settings"][key] = value
        self._save()


# ─── Factory (singleton) ─────────────────────────────────────
_store_instance: Optional[LocalJsonStore] = None

def get_store() -> LocalJsonStore:
    """Returns the singleton storage backend with Google Sheets sync."""
    global _store_instance
    if _store_instance is None:
        _store_instance = LocalJsonStore()
    return _store_instance
