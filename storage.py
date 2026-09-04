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


def _fmt_ts():
    """Format current timestamp as '29/08/26-1425' style."""
    return _now().strftime("%d/%m/%y-%H%M")

logger = logging.getLogger(__name__)


_BRAND_PREFIXES = [
    "sm", "s&p", "cap", "nestle", "dutch lady", "f&n",
    "ayam brand", "nutrifres", "dasani", "clorox", "ajax",
    "dettol", "mr muscle", "marigold", "yeo's", "yeos",
    "gardenia", "massimo", "sunshine", "goodday", "milo",
    "nescafe", "maggi", "kara", "aroy-d", "aroy d",
    "value", "topvalu", "tesco", "giant", "lotus",
    "spray n wipe", "mr. muscle", "cif", "colgate",
]
_BRAND_PREFIX_RE = _re.compile(
    r'^(?:' + '|'.join(_re.escape(p) for p in _BRAND_PREFIXES) + r')\b\s*',
    _re.IGNORECASE,
)
_SIZE_SUFFIX_RE = _re.compile(
    r'\b\d+(\.\d+)?\s*(kg|g|ml|l|pcs|pack|box|bag|btl|carton)\b',
    _re.IGNORECASE,
)


def normalize_item_name(name: str) -> str:
    """Normalize item name so slight variations match.
    'Coconut (Toasted, 100g)' and 'Coconut - Toasted, 100g' → same key."""
    s = name.strip()
    s = _BRAND_PREFIX_RE.sub('', s)          # Strip known brand prefixes
    s = _SIZE_SUFFIX_RE.sub(' ', s)          # Strip size/weight suffixes
    s = _re.sub(r'\([^)]*\)', ' ', s)       # Strip parenthesized annotations entirely
    s = _re.sub(r'[{}[\]]', ' ', s)         # Remove remaining brackets
    s = _re.sub(r'[-/\\.,;:]+', ' ', s)      # Dashes, slashes, dots → space
    s = _re.sub(r'\s+', ' ', s).strip()      # Collapse whitespace
    return s.lower()


_LEADING_CODE_RE = _re.compile(
    r'''^\s*
        (?:
            [A-Za-z]+-\d+[A-Za-z]*          # E-878, SM-123
            |
            [A-Za-z]{2,}\s*\d+(?:\.\d+)?"   # MAV 14"
            |
            [A-Za-z]{1,4}\d{2,}[A-Za-z0-9]* # SM123, AB99X
        )
        \s+
    ''',
    _re.VERBOSE,
)
_HASHTAG_WORD_RE = _re.compile(r'#\S+')
_SIZE_KEEP_RE = _re.compile(
    r'^\d+(\.\d+)?\s*(kg|g|ml|l|pcs|pack|box|bag|btl|carton)$',
    _re.IGNORECASE,
)
_SIZE_TOKEN_RE = _re.compile(
    r'\b(\d+(?:\.\d+)?)\s*(kg|g|ml|l|pcs|pack|box|bag|btl|carton)\b',
    _re.IGNORECASE,
)


def clean_item_name(raw: str) -> str:
    """Clean up a raw/ugly receipt item name into a simple, readable name.

    - Strips leading product/model codes (e.g. "E-878", "MAV 14\"", "SM-123")
    - Removes hashtag words (e.g. "#SOBBAR")
    - Title-cases an ALL CAPS name
    - Strips known brand prefixes (see _BRAND_PREFIXES)
    - Keeps size info like "1.5L", "500ml", "250g"
    - Collapses whitespace and removes duplicate trailing size-only words
    """
    if not raw:
        return raw

    s = raw.strip()

    # 1. Strip leading product/model code
    s = _LEADING_CODE_RE.sub('', s, count=1)

    # 2. Remove hashtag words
    s = _HASHTAG_WORD_RE.sub('', s)

    # Collapse whitespace before further processing
    s = _re.sub(r'\s+', ' ', s).strip()

    # 3. If entire name is ALL CAPS, convert to Title Case
    letters = [c for c in s if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        # Preserve size tokens' original casing (e.g. "1.5L" stays "1.5L",
        # "400ML" becomes "400ml") — title-casing would otherwise mangle them.
        sizes = {}
        def _stash(m):
            key = f'\x00SIZE{len(sizes)}\x00'
            unit = m.group(2)
            sizes[key] = m.group(1) + (unit if unit == 'L' else unit.lower())
            return key
        s = _SIZE_TOKEN_RE.sub(_stash, s)
        s = s.title()
        for key, val in sizes.items():
            s = s.replace(key.title(), val)

    # 4. Strip known brand prefixes
    s = _BRAND_PREFIX_RE.sub('', s)

    # Collapse whitespace again
    s = _re.sub(r'\s+', ' ', s).strip()

    # 7. Remove duplicate trailing size/unit-only words (e.g. "... Spoon Spoon")
    words = s.split(' ')
    while len(words) >= 2 and words[-1].lower() == words[-2].lower():
        words.pop()
    s = ' '.join(words)

    return s.strip()


def _words_prefix_match(words_a: list, words_b: list) -> bool:
    """Check if two word lists match with allowance for truncated words.
    E.g. ['pistachio', 'cru', 'pandan'] matches ['pistachio', 'crunch', 'pandan']
    because 'cru' is a prefix of 'crunch'. At least 70% of words must match."""
    shorter, longer = (words_a, words_b) if len(words_a) <= len(words_b) else (words_b, words_a)
    if not shorter:
        return False
    matches = 0
    used = set()
    for sw in shorter:
        for i, lw in enumerate(longer):
            if i in used:
                continue
            if sw == lw or (len(sw) >= 3 and (lw.startswith(sw) or sw.startswith(lw))):
                matches += 1
                used.add(i)
                break
    ratio = matches / max(len(shorter), len(longer))
    return ratio >= 0.7


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
        "Shopping List": ["Item"],
        "Events": ["Title", "Date", "Details", "Added By", "Status"],
        "Bingsu Recipes": ["Flavor", "Batch Size", "Ingredient", "Quantity"],
        "Other Recipes": ["Type", "Name", "Category", "Ingredient", "Quantity", "Method", "Step Number", "Step Text"],
        "Stock Minimums": ["Item", "Min", "Unit", "Location"],
        "Checklists": ["Checklist", "Step Number", "Task"],
        "Inspection": ["Section", "Item"],
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

    def sync_stock(self, stock_data: dict, stock_history: dict = None, stock_current: dict = None):
        """Sync stock data to Sheets — MERGE only, never delete rows.
        Sheet is source of truth. Updates existing rows in place, appends new items.
        Uses stock_history {date: {item: qty}} and stock_current {item: qty}.
        Column A = Item, Column B = Current Stock, columns C+ = date columns."""
        ws = self._get_ws("Stock")
        if not ws:
            return
        try:
            stock_history = stock_history or {}
            stock_current = stock_current or {}

            if not stock_history and not stock_current:
                return

            existing = ws.get_all_values()

            # If sheet is completely empty, create header
            if not existing or not existing[0]:
                ws.update("A1", [["Item", "Current Stock"]])
                ws.format("A1:B1", {"textFormat": {"bold": True}})
                existing = [["Item", "Current Stock"]]

            header = list(existing[0])

            # Ensure "Current Stock" occupies column B
            has_current_col = len(header) > 1 and header[1].strip() == "Current Stock"
            if not has_current_col:
                new_header = [header[0], "Current Stock"] + header[1:]
                new_rows = [new_header]
                for row in existing[1:]:
                    new_rows.append([row[0] if row else "", ""] + (row[1:] if row else []))
                ws.clear()
                ws.update("A1", new_rows)
                existing = new_rows
                header = list(new_header)

            # Build lookup: normalized item name → sheet row index (1-based, skipping header)
            row_lookup = {}  # {norm_name: row_index}
            for i, row in enumerate(existing[1:], 1):
                if row and row[0].strip():
                    norm = normalize_item_name(row[0])
                    if norm not in row_lookup:  # first match wins
                        row_lookup[norm] = i

            # Helper: find row by normalized name, with substring fallback
            def _find_row(item_name):
                norm = normalize_item_name(item_name)
                if norm in row_lookup:
                    return row_lookup[norm]
                # Substring fallback
                if len(norm) >= 4:
                    for existing_norm, idx in row_lookup.items():
                        if len(existing_norm) >= 4 and (norm in existing_norm or existing_norm in norm):
                            return idx
                return None

            # Track next available row for appending
            next_row = len(existing) + 1

            # Collect all cell updates: {(row, col): value}
            cell_updates = {}

            # Process stock_history: update date columns
            for date_str, items in stock_history.items():
                # Find or create date column
                if date_str in header:
                    col_idx = header.index(date_str)
                else:
                    # Add new date column at end of header
                    header.append(date_str)
                    col_idx = len(header) - 1
                    cell_updates[(1, col_idx + 1)] = date_str

                for item, qty in items.items():
                    if str(qty).upper() == "OK":
                        continue
                    row_idx = _find_row(item)
                    if row_idx is not None:
                        cell_updates[(row_idx + 1, col_idx + 1)] = qty
                    else:
                        # Append new row
                        norm = normalize_item_name(item)
                        row_lookup[norm] = next_row - 1  # store 1-based index for existing[1:]
                        cell_updates[(next_row, 1)] = item
                        cell_updates[(next_row, col_idx + 1)] = qty
                        next_row += 1

            # Process stock_current: update column B
            for item, val in stock_current.items():
                row_idx = _find_row(item)
                if row_idx is not None:
                    cell_updates[(row_idx + 1, 2)] = val
                else:
                    # Append new row
                    norm = normalize_item_name(item)
                    row_lookup[norm] = next_row - 1
                    cell_updates[(next_row, 1)] = item
                    cell_updates[(next_row, 2)] = val
                    next_row += 1

            # Apply all updates in batch
            if cell_updates:
                # gspread batch_update for efficiency
                batch = []
                for (row, col), val in cell_updates.items():
                    cell_label = gspread.utils.rowcol_to_a1(row, col)
                    batch.append({"range": cell_label, "values": [[val]]})
                # batch_update accepts up to ~50k cells
                if batch:
                    ws.batch_update(batch, value_input_option="RAW")

            ws.format("A1:Z1", {"textFormat": {"bold": True}})
        except Exception as e:
            logger.error(f"Sheets sync error (Stock): {e}")

    def sync_shopping(self, shopping_data: list):
        """Sync shopping list to Sheets — merge JSON items with existing sheet.
        Keeps manually added items, adds JSON items not already present."""
        ws = self._get_ws("Shopping List")
        if not ws:
            return
        try:
            # Read existing sheet items
            existing = ws.get_all_values()
            sheet_items = []
            if existing and len(existing) > 1:
                for row in existing[1:]:
                    if row and row[0].strip():
                        sheet_items.append(row[0].strip())

            sheet_norms = {normalize_item_name(i) for i in sheet_items}

            # Add JSON items not already on sheet
            json_items = [item.get("item", "") for item in shopping_data if item.get("item")]
            new_items = []
            for item in json_items:
                if normalize_item_name(item) not in sheet_norms:
                    new_items.append(item)

            # Append new items (don't clear existing)
            for item in new_items:
                ws.append_row([item])

            # Ensure header exists
            if not existing or not existing[0]:
                ws.update("A1", [["Item"]])
            ws.format("A1:A1", {"textFormat": {"bold": True}})
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
        """Read Stock sheet → return (stock_dict, stock_history_dict, stock_current_dict).
        Sheet is source of truth — this overwrites local JSON data.
        Handles the "Item | Current Stock | date1 | date2 ..." layout — the
        Current Stock column (B) is read separately and skipped when scanning
        for date columns."""
        ws = self._get_ws("Stock")
        if not ws:
            return None, None, None
        try:
            from datetime import datetime as _dt
            existing = ws.get_all_values()
            if not existing or not existing[0]:
                return {}, {}, {}

            header = existing[0]

            # "Current Stock" occupies column B (index 1) when present
            has_current_col = len(header) > 1 and header[1].strip() == "Current Stock"
            date_start = 2 if has_current_col else 1

            def is_date_col(s):
                try:
                    _dt.strptime(s, "%d/%m/%y")
                    return True
                except (ValueError, TypeError):
                    return False

            date_cols = [(i, h) for i, h in enumerate(header[date_start:], date_start) if is_date_col(h)]

            # Parse dates for sorting (find latest per item)
            def parse_d(d):
                try:
                    return _dt.strptime(d, "%d/%m/%y")
                except ValueError:
                    return _dt.min

            stock = {}
            history = {}
            stock_current = {}

            for row in existing[1:]:
                if not row or not row[0]:
                    continue
                item = row[0].strip()
                if not item:
                    continue

                if has_current_col:
                    cur_val = (row[1] if len(row) > 1 else "").strip()
                    if cur_val:
                        try:
                            stock_current[item] = int(float(cur_val))
                        except (ValueError, TypeError):
                            pass

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

                # Backfill stock_current from most recent date column if column B was empty
                if item not in stock_current and latest_qty:
                    try:
                        stock_current[item] = int(float(latest_qty))
                    except (ValueError, TypeError):
                        pass

                if latest_qty:
                    stock[item] = {
                        "qty": latest_qty,
                        "updated_by": "sheet",
                        "updated_at": _now().strftime("%d/%m/%y-%H%M"),
                    }

            logger.info(f"Read {len(stock)} stock items from Sheet")
            return stock, history, stock_current

        except Exception as e:
            logger.error(f"Error reading stock from Sheet: {e}")
            return None, None, None

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
            for row in existing[1:]:  # Skip header
                if not row or not row[0]:
                    continue
                item_name = row[0].strip()
                if not item_name:
                    continue
                shopping.append({"item": item_name})

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
                # Layout: Item | Current Stock | date_str
                ws.update("A1", [["Item", "Current Stock", date_str], [item, "", qty]])
                ws.format("A1:C1", {"textFormat": {"bold": True}})
                return

            header = existing[0]

            # Ensure "Current Stock" occupies column B — insert if missing
            has_current_col = len(header) > 1 and header[1].strip() == "Current Stock"
            if not has_current_col:
                new_header = [header[0], "Current Stock"] + header[1:]
                new_rows = [new_header]
                for row in existing[1:]:
                    new_rows.append([row[0] if row else "", ""] + (row[1:] if row else []))
                ws.clear()
                ws.update("A1", new_rows)
                existing = new_rows
                header = new_header

            # Find or create date column (date columns start at column C / index 2)
            if date_str in header[2:]:
                col_idx = header.index(date_str, 2)
            else:
                # Insert as column C (newest date first, after "Item" and "Current Stock")
                col_idx = 2
                # Shift existing date columns right by inserting new col
                new_header = header[:2] + [date_str] + header[2:]
                new_rows = [new_header]
                for row in existing[1:]:
                    new_rows.append((row[:2] if row else ["", ""]) + [""] + (row[2:] if row else []))
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

            # Substring fallback (handles AI sending shorter/longer name variants)
            if row_idx is None and len(norm) >= 4:
                for i, row in enumerate(existing[1:], 1):
                    if row:
                        row_norm = normalize_item_name(row[0])
                        if len(row_norm) >= 4 and (norm in row_norm or row_norm in norm):
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
        """Append a shopping item to the Sheet — single 'Item' column."""
        ws = self._get_ws("Shopping List")
        if not ws:
            return
        try:
            ws.append_row([item_data.get("item", "")])
        except Exception as e:
            logger.error(f"Sheet write error (shopping): {e}")

    def clear_shopping_list(self):
        """Clear all rows from the Shopping List sheet, keeping only the header."""
        ws = self._get_ws("Shopping List")
        if not ws:
            return
        try:
            ws.clear()
            ws.update("A1", [["Item"]])
            ws.format("A1:A1", {"textFormat": {"bold": True}})
        except Exception as e:
            logger.error(f"Sheet clear error (shopping): {e}")

    def remove_shopping_item(self, item_name: str):
        """Remove a single item from the Shopping List sheet by name."""
        ws = self._get_ws("Shopping List")
        if not ws:
            return
        try:
            existing = ws.get_all_values()
            if not existing:
                return
            norm = normalize_item_name(item_name)
            for i, row in enumerate(existing[1:], 2):  # 2 = sheet row (1-indexed, skip header)
                if row and normalize_item_name(row[0]) == norm:
                    ws.delete_rows(i)
                    return
            # Substring fallback
            if len(norm) >= 4:
                for i, row in enumerate(existing[1:], 2):
                    if row:
                        row_norm = normalize_item_name(row[0])
                        if len(row_norm) >= 4 and (norm in row_norm or row_norm in norm):
                            ws.delete_rows(i)
                            return
        except Exception as e:
            logger.error(f"Sheet remove error (shopping): {e}")

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
            ("Stock", lambda: self.sync_stock(data.get("stock", {}), data.get("stock_history", {}), data.get("stock_current", {}))),
            ("Shopping", lambda: self.sync_shopping(data.get("shopping_list", []))),
            ("Events", lambda: self.sync_events(data.get("events", []))),
        ]
        for name, sync_fn in syncs:
            try:
                sync_fn()
                _time.sleep(3)  # 3s delay between worksheets to avoid rate limit
            except Exception as e:
                logger.error(f"Sheets sync error ({name}): {e}")

    # ─── SOP data (recipes, minimums, checklists, inspection) ───

    def _ws_row_count(self, name: str) -> int:
        """Return number of data rows (excluding header) currently in a worksheet."""
        ws = self._get_ws(name)
        if not ws:
            return 0
        try:
            values = ws.get_all_values()
            return max(0, len(values) - 1)
        except Exception as e:
            logger.error(f"Error reading row count for {name}: {e}")
            return 0

    def seed_sop_to_sheets(self):
        """One-time seed: write the hardcoded SOP data (from sop_data.py) into the
        SOP worksheets, but only for tabs that are currently empty (no data rows).
        Safe to call on every startup — it's a no-op once sheets are populated."""
        if not self.spreadsheet:
            return
        try:
            from sop_data import (
                BINGSU_RECIPES, FOAM_RECIPES, TOPPING_RECIPES, DRINKS_RECIPES,
                STOCK_MINIMUMS, OPS_CHECKLISTS, INSPECTION_CHECKLIST,
            )
        except Exception as e:
            logger.info(f"seed_sop_to_sheets: sop_data hardcoded dicts not available ({e}) — skipping seed")
            return

        # ── Bingsu Recipes ──
        try:
            if self._ws_row_count("Bingsu Recipes") == 0:
                ws = self._get_ws("Bingsu Recipes")
                if ws:
                    rows = []
                    for flavor, sizes in BINGSU_RECIPES.items():
                        for batch_size, ingredients in sizes.items():
                            for ingredient, qty in ingredients.items():
                                rows.append([flavor, batch_size, ingredient, qty])
                    if rows:
                        ws.update("A2", rows)
                        logger.info(f"Seeded {len(rows)} rows into Bingsu Recipes")
        except Exception as e:
            logger.error(f"seed_sop_to_sheets (Bingsu Recipes): {e}")

        _time.sleep(1)

        # ── Other Recipes (Foam, Topping, Drink) ──
        try:
            if self._ws_row_count("Other Recipes") == 0:
                ws = self._get_ws("Other Recipes")
                if ws:
                    rows = []
                    # Foam recipes
                    for name, data in FOAM_RECIPES.items():
                        method = data.get("method", "")
                        ingredients = data.get("ingredients", {})
                        if ingredients:
                            for ingredient, qty in ingredients.items():
                                rows.append(["Foam", name, "", ingredient, qty, method, "", ""])
                        else:
                            rows.append(["Foam", name, "", "", "", method, "", ""])

                    # Topping recipes
                    for name, data in TOPPING_RECIPES.items():
                        ingredients_str = data.get("ingredients", "")
                        method_steps = data.get("method", [])
                        if method_steps:
                            for i, step in enumerate(method_steps, 1):
                                rows.append(["Topping", name, "", ingredients_str if i == 1 else "", "", "", i, step])
                        else:
                            rows.append(["Topping", name, "", ingredients_str, "", "", "", ""])

                    # Drinks recipes
                    for category, drinks in DRINKS_RECIPES.items():
                        for drink_name, data in drinks.items():
                            method = data.get("method", "")
                            ingredients = data.get("ingredients", {})
                            if ingredients:
                                for ingredient, qty in ingredients.items():
                                    rows.append(["Drink", drink_name, category, ingredient, qty, method, "", ""])
                            else:
                                rows.append(["Drink", drink_name, category, "", "", method, "", ""])

                    if rows:
                        ws.update("A2", rows)
                        logger.info(f"Seeded {len(rows)} rows into Other Recipes")
        except Exception as e:
            logger.error(f"seed_sop_to_sheets (Other Recipes): {e}")

        _time.sleep(1)

        # ── Stock Minimums ──
        try:
            if self._ws_row_count("Stock Minimums") == 0:
                ws = self._get_ws("Stock Minimums")
                if ws:
                    rows = []
                    for item, info in STOCK_MINIMUMS.items():
                        rows.append([item, info.get("min", ""), info.get("unit", ""), info.get("location", "")])
                    if rows:
                        ws.update("A2", rows)
                        logger.info(f"Seeded {len(rows)} rows into Stock Minimums")
        except Exception as e:
            logger.error(f"seed_sop_to_sheets (Stock Minimums): {e}")

        _time.sleep(1)

        # ── Checklists ──
        try:
            if self._ws_row_count("Checklists") == 0:
                ws = self._get_ws("Checklists")
                if ws:
                    rows = []
                    for checklist, items in OPS_CHECKLISTS.items():
                        for i, task in enumerate(items, 1):
                            rows.append([checklist, i, task])
                    if rows:
                        ws.update("A2", rows)
                        logger.info(f"Seeded {len(rows)} rows into Checklists")
        except Exception as e:
            logger.error(f"seed_sop_to_sheets (Checklists): {e}")

        _time.sleep(1)

        # ── Inspection ──
        try:
            if self._ws_row_count("Inspection") == 0:
                ws = self._get_ws("Inspection")
                if ws:
                    rows = []
                    for section, items in INSPECTION_CHECKLIST.items():
                        for item in items:
                            rows.append([section, item])
                    if rows:
                        ws.update("A2", rows)
                        logger.info(f"Seeded {len(rows)} rows into Inspection")
        except Exception as e:
            logger.error(f"seed_sop_to_sheets (Inspection): {e}")

    def read_sop_from_sheets(self) -> dict:
        """Read all SOP data from the SOP worksheets and reconstruct it into the
        same nested dict structures the hardcoded sop_data.py dicts used to have.
        Returns a dict with keys: bingsu_recipes, foam_recipes, topping_recipes,
        drinks_recipes, stock_minimums, ops_checklists, inspection_checklist.
        Any tab that can't be read is returned as an empty dict for that key."""
        result = {
            "bingsu_recipes": {},
            "foam_recipes": {},
            "topping_recipes": {},
            "drinks_recipes": {},
            "stock_minimums": {},
            "ops_checklists": {},
            "inspection_checklist": {},
        }

        # ── Bingsu Recipes ──
        try:
            ws = self._get_ws("Bingsu Recipes")
            if ws:
                values = ws.get_all_values()
                for row in values[1:]:
                    if not row or not row[0]:
                        continue
                    flavor = row[0].strip()
                    batch_size = (row[1] if len(row) > 1 else "").strip()
                    ingredient = (row[2] if len(row) > 2 else "").strip()
                    qty = (row[3] if len(row) > 3 else "").strip()
                    if not flavor or not batch_size or not ingredient:
                        continue
                    result["bingsu_recipes"].setdefault(flavor, {}).setdefault(batch_size, {})[ingredient] = qty
        except Exception as e:
            logger.error(f"read_sop_from_sheets (Bingsu Recipes): {e}")

        # ── Other Recipes (Foam / Topping / Drink) ──
        try:
            ws = self._get_ws("Other Recipes")
            if ws:
                values = ws.get_all_values()
                # Preserve method for topping steps grouped by name (ordered)
                topping_methods = {}  # name -> list of (step_num, text)
                topping_ingredients = {}  # name -> ingredients str

                for row in values[1:]:
                    if not row or not row[0]:
                        continue
                    rtype = row[0].strip()
                    name = (row[1] if len(row) > 1 else "").strip()
                    category = (row[2] if len(row) > 2 else "").strip()
                    ingredient = (row[3] if len(row) > 3 else "").strip()
                    qty = (row[4] if len(row) > 4 else "").strip()
                    method = (row[5] if len(row) > 5 else "").strip()
                    step_num = (row[6] if len(row) > 6 else "").strip()
                    step_text = (row[7] if len(row) > 7 else "").strip()

                    if not name:
                        continue

                    if rtype == "Foam":
                        entry = result["foam_recipes"].setdefault(name, {"ingredients": {}, "method": method})
                        if ingredient:
                            entry["ingredients"][ingredient] = qty
                        if method:
                            entry["method"] = method

                    elif rtype == "Topping":
                        if ingredient:
                            topping_ingredients[name] = ingredient
                        if step_text:
                            try:
                                sn = int(step_num) if step_num else len(topping_methods.get(name, [])) + 1
                            except ValueError:
                                sn = len(topping_methods.get(name, [])) + 1
                            topping_methods.setdefault(name, []).append((sn, step_text))

                    elif rtype == "Drink":
                        cat_dict = result["drinks_recipes"].setdefault(category, {})
                        entry = cat_dict.setdefault(name, {"ingredients": {}, "method": method})
                        if ingredient:
                            entry["ingredients"][ingredient] = qty
                        if method:
                            entry["method"] = method

                # Build topping_recipes from accumulated steps/ingredients,
                # preserving the order names were first seen in the sheet.
                all_topping_names = list(dict.fromkeys(
                    list(topping_ingredients.keys()) + list(topping_methods.keys())
                ))
                for name in all_topping_names:
                    entry = {}
                    if name in topping_ingredients:
                        entry["ingredients"] = topping_ingredients[name]
                    steps = sorted(topping_methods.get(name, []), key=lambda x: x[0])
                    entry["method"] = [text for _, text in steps]
                    result["topping_recipes"][name] = entry
        except Exception as e:
            logger.error(f"read_sop_from_sheets (Other Recipes): {e}")

        # ── Stock Minimums ──
        try:
            result["stock_minimums"] = self.read_stock_minimums_from_sheet()
        except Exception as e:
            logger.error(f"read_sop_from_sheets (Stock Minimums): {e}")

        # ── Checklists ──
        try:
            ws = self._get_ws("Checklists")
            if ws:
                values = ws.get_all_values()
                grouped = {}  # checklist -> list of (step_num, task)
                for row in values[1:]:
                    if not row or not row[0]:
                        continue
                    checklist = row[0].strip()
                    step_num = (row[1] if len(row) > 1 else "").strip()
                    task = (row[2] if len(row) > 2 else "").strip()
                    if not task:
                        continue
                    try:
                        sn = int(step_num) if step_num else len(grouped.get(checklist, [])) + 1
                    except ValueError:
                        sn = len(grouped.get(checklist, [])) + 1
                    grouped.setdefault(checklist, []).append((sn, task))
                for checklist, items in grouped.items():
                    items.sort(key=lambda x: x[0])
                    result["ops_checklists"][checklist] = [t for _, t in items]
        except Exception as e:
            logger.error(f"read_sop_from_sheets (Checklists): {e}")

        # ── Inspection ──
        try:
            ws = self._get_ws("Inspection")
            if ws:
                values = ws.get_all_values()
                for row in values[1:]:
                    if not row or not row[0]:
                        continue
                    section = row[0].strip()
                    item = (row[1] if len(row) > 1 else "").strip()
                    if not item:
                        continue
                    result["inspection_checklist"].setdefault(section, []).append(item)
        except Exception as e:
            logger.error(f"read_sop_from_sheets (Inspection): {e}")

        return result

    def read_stock_minimums_from_sheet(self) -> dict:
        """Convenience method: read just the Stock Minimums tab and return
        {item: {"min": int, "unit": str, "location": str}}."""
        stock_minimums = {}
        try:
            ws = self._get_ws("Stock Minimums")
            if not ws:
                return stock_minimums
            values = ws.get_all_values()
            for row in values[1:]:
                if not row or not row[0]:
                    continue
                item = row[0].strip()
                min_str = (row[1] if len(row) > 1 else "").strip()
                unit = (row[2] if len(row) > 2 else "").strip()
                location = (row[3] if len(row) > 3 else "").strip()
                if not item:
                    continue
                try:
                    min_val = int(float(min_str)) if min_str else 0
                except (ValueError, TypeError):
                    min_val = 0
                info = {"min": min_val, "location": location}
                if unit:
                    info["unit"] = unit
                stock_minimums[item] = info
        except Exception as e:
            logger.error(f"read_stock_minimums_from_sheet: {e}")
        return stock_minimums


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
                    # Backfill current stock to sheet for items missing it
                    self._backfill_current_stock_to_sheet()
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
            stock, history, stock_current = self._sheets.read_stock_from_sheet()
            if stock is not None:
                self.data["stock"] = stock
            if history is not None:
                self.data["stock_history"] = history
            if stock_current is not None and stock_current:
                if "stock_current" not in self.data:
                    self.data["stock_current"] = {}
                self.data["stock_current"].update(stock_current)

            # Backfill stock_current from latest historical qty for items missing it
            for item_name, info in self.data.get("stock", {}).items():
                if item_name not in self.data.get("stock_current", {}):
                    # Try to parse a number from the latest qty
                    qty_str = str(info.get("qty", "")).strip()
                    try:
                        import re as _re_local
                        nums = _re_local.findall(r'[\d.]+', qty_str)
                        if nums:
                            self.data["stock_current"][item_name] = int(float(nums[0]))
                    except (ValueError, TypeError):
                        pass

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
            "stock": lambda: self._sheets.sync_stock(self.data.get("stock", {}), self.data.get("stock_history", {}), self.data.get("stock_current", {})),
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
            "stock_current": {},        # {item_name: int} — running current stock count
            "last_full_count": {},      # {item_name: {"qty": int, "date": "YYYY-MM-DD"}}
            "oneoff_items": {},         # For Phase 7 later
            "receipt_hashes": {},       # {hash_key: {supplier, date, total, item_count, recorded_by, recorded_at}}
        }

    # ─── Stock ──────────────────────────────────────────────
    def get_stock(self) -> dict:
        return self.data.get("stock", {})

    def _find_existing_stock_name(self, new_name: str) -> str:
        """Find an existing stock item that matches by normalized name or alias.
        Returns the existing key if found, otherwise the new_name."""
        new_norm = normalize_item_name(new_name)
        # Check direct normalized match first
        for existing in self.data.get("stock", {}):
            if normalize_item_name(existing) == new_norm:
                return existing
        # Also check stock_current keys
        for existing in self.data.get("stock_current", {}):
            if normalize_item_name(existing) == new_norm:
                return existing
        # Check alias store
        alias_store = get_alias_store()
        canonical = alias_store.resolve(new_name)
        if canonical != new_name:
            # Found an alias — check if canonical exists in stock
            for existing in self.data.get("stock", {}):
                if normalize_item_name(existing) == normalize_item_name(canonical):
                    return existing
        # Fuzzy fallback: word-level prefix match (handles AI truncation like "CRU" → "CRUNCH")
        new_words = new_norm.split()
        if len(new_words) >= 2:
            for existing in self.data.get("stock", {}):
                existing_norm = normalize_item_name(existing)
                existing_words = existing_norm.split()
                if len(existing_words) >= 2 and _words_prefix_match(new_words, existing_words):
                    return existing
        # Substring fallback: "coconut milk" matches "coconut milk 400ml" and vice versa
        if len(new_norm) >= 4:  # avoid matching very short strings
            for existing in self.data.get("stock", {}):
                existing_norm = normalize_item_name(existing)
                if len(existing_norm) >= 4 and (new_norm in existing_norm or existing_norm in new_norm):
                    return existing
            for existing in self.data.get("stock_current", {}):
                existing_norm = normalize_item_name(existing)
                if len(existing_norm) >= 4 and (new_norm in existing_norm or existing_norm in new_norm):
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
            "updated_at": _fmt_ts(),
        }
        if "stock_history" not in self.data:
            self.data["stock_history"] = {}
        if today not in self.data["stock_history"]:
            self.data["stock_history"][today] = {}
        self.data["stock_history"][today][item] = qty

        # Keep stock_current in sync
        if "stock_current" not in self.data:
            self.data["stock_current"] = {}
        try:
            self.data["stock_current"][item] = int(qty)
        except (ValueError, TypeError):
            pass

        self._save_local_only()
        self._rebuild_shopping_list()

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

        # Also clear from stock_current and last_full_count
        to_del_current = [k for k in self.data.get("stock_current", {})
                          if normalize_item_name(k) == norm]
        for k in to_del_current:
            del self.data["stock_current"][k]
            removed = True

        to_del_lfc = [k for k in self.data.get("last_full_count", {})
                      if normalize_item_name(k) == norm]
        for k in to_del_lfc:
            del self.data["last_full_count"][k]

        if removed:
            self._save_local_only()
            self._rebuild_shopping_list()
            logger.info(f"Removed stock item: {item}")
        return removed

    def check_duplicate_receipt(self, supplier: str, receipt_date: str, total: float, items: list) -> dict | None:
        """Check if a receipt with same supplier+date+total+items was already logged.
        Returns the existing receipt info dict if duplicate found, None otherwise."""
        key = self._receipt_hash_key(supplier, receipt_date, total, items)
        hashes = self.data.get("receipt_hashes", {})
        return hashes.get(key)

    def record_receipt_hash(self, supplier: str, receipt_date: str, total: float, items: list, recorded_by: str = ""):
        """Record a receipt's hash after successful save."""
        key = self._receipt_hash_key(supplier, receipt_date, total, items)
        if "receipt_hashes" not in self.data:
            self.data["receipt_hashes"] = {}
        self.data["receipt_hashes"][key] = {
            "supplier": supplier,
            "date": receipt_date,
            "total": total,
            "item_count": len(items),
            "recorded_by": recorded_by,
            "recorded_at": _now().strftime("%d/%m/%y-%H%M"),
        }
        self._save_local_only()

    def _receipt_hash_key(self, supplier: str, receipt_date: str, total: float, items: list) -> str:
        """Generate a hash key for duplicate detection.
        Uses only supplier + date + total (not item names — OCR is too noisy)."""
        import hashlib
        norm_supplier = normalize_item_name(supplier)
        rounded_total = f"{float(total):.2f}"
        raw = f"{norm_supplier}|{receipt_date}|{rounded_total}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def record_oneoff_item(self, item: str):
        """Record an item as one-off purchase (not tracked in regular stock)."""
        if "oneoff_items" not in self.data:
            self.data["oneoff_items"] = {}
        norm = normalize_item_name(item)
        now_str = _fmt_ts()
        if norm in self.data["oneoff_items"]:
            self.data["oneoff_items"][norm]["count"] = self.data["oneoff_items"][norm].get("count", 0) + 1
            self.data["oneoff_items"][norm]["last_purchased"] = now_str
            self.data["oneoff_items"][norm]["display_name"] = item
        else:
            self.data["oneoff_items"][norm] = {
                "display_name": item,
                "count": 1,
                "first_purchased": now_str,
                "last_purchased": now_str,
            }
        self._save_local_only()

    def is_known_oneoff(self, item: str) -> bool:
        """Check if item was previously marked as one-off."""
        norm = normalize_item_name(item)
        return norm in self.data.get("oneoff_items", {})

    def get_frequent_oneoffs(self, threshold: int = 3) -> list:
        """Get one-off items bought >= threshold times — candidates for promotion to regular."""
        result = []
        for norm, info in self.data.get("oneoff_items", {}).items():
            if info.get("count", 0) >= threshold:
                result.append({
                    "item": info.get("display_name", norm),
                    "count": info["count"],
                    "first": info.get("first_purchased", ""),
                    "last": info.get("last_purchased", ""),
                })
        return result

    def promote_oneoff_to_regular(self, item: str) -> bool:
        """Remove item from oneoff_items (it stays in stock as regular)."""
        norm = normalize_item_name(item)
        if norm in self.data.get("oneoff_items", {}):
            del self.data["oneoff_items"][norm]
            self._save_local_only()
            return True
        return False

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
                    "updated_at": _fmt_ts(),
                }
                self.data["stock_history"][stock_date][item_name] = qty

                # Keep stock_current in sync
                if "stock_current" not in self.data:
                    self.data["stock_current"] = {}
                try:
                    self.data["stock_current"][item_name] = int(qty)
                except (ValueError, TypeError):
                    pass

        self._save_local_only()
        self._rebuild_shopping_list()

    def add_receipt_to_stock(self, item: str, qty: int, receipt_date: str = None):
        """Add received quantity to running current stock (from a receipt),
        and also record the new total in stock_history under a date column
        on the Stock sheet."""
        item = self._find_existing_stock_name(clean_item_name(item))

        if "stock_current" not in self.data:
            self.data["stock_current"] = {}
        try:
            qty = int(qty)
        except (ValueError, TypeError):
            qty = 0

        current = self.data["stock_current"].get(item, 0)
        try:
            current = int(current)
        except (ValueError, TypeError):
            current = 0
        new_total = current + qty
        self.data["stock_current"][item] = new_total

        # Backward compat: update the stock dict with current qty
        self.data["stock"][item] = {
            "qty": str(new_total),
            "updated_by": self.data.get("stock", {}).get(item, {}).get("updated_by", "Receipt"),
            "updated_at": _fmt_ts(),
        }

        # Also record in stock_history + sheet date column
        date_col = receipt_date or _now().strftime("%d/%m/%y")
        if "stock_history" not in self.data:
            self.data["stock_history"] = {}
        if date_col not in self.data["stock_history"]:
            self.data["stock_history"][date_col] = {}
        self.data["stock_history"][date_col][item] = new_total

        # Write to sheet date column
        if self._sheets:
            try:
                self._sheets.write_stock_item(item, str(new_total), date_col)
            except Exception as e:
                logger.error(f"Direct sheet write failed (receipt_to_stock): {e}")

        self._sync_current_stock_to_sheet(item)
        self._save_local_only()
        self._rebuild_shopping_list()

    def full_stock_count(self, items: dict, counted_by: str):
        """Record a full physical stock count.
        items: {item_name: qty_int} from physical count.
        Items not present carry forward their existing stock_current value."""
        if "stock_current" not in self.data:
            self.data["stock_current"] = {}
        if "last_full_count" not in self.data:
            self.data["last_full_count"] = {}
        if "stock_history" not in self.data:
            self.data["stock_history"] = {}

        today_iso = _now().strftime("%Y-%m-%d")
        today_sheet = _now().strftime("%d/%m/%y")

        if today_sheet not in self.data["stock_history"]:
            self.data["stock_history"][today_sheet] = {}

        for item, qty in items.items():
            item = self._find_existing_stock_name(item)
            try:
                qty = int(qty)
            except (ValueError, TypeError):
                continue

            self.data["stock_current"][item] = qty
            self.data["last_full_count"][item] = {"qty": qty, "date": today_iso}
            self.data["stock_history"][today_sheet][item] = qty

            # Backward compat
            self.data["stock"][item] = {
                "qty": str(qty),
                "updated_by": counted_by,
                "updated_at": _fmt_ts(),
            }

            # Write to Sheet
            if self._sheets:
                try:
                    self._sheets.write_stock_item(item, str(qty), today_sheet)
                except Exception as e:
                    logger.error(f"Direct sheet write failed (full_stock_count): {e}")
            self._sync_current_stock_to_sheet(item)

        self._save_local_only()
        self._rebuild_shopping_list()

    def _backfill_current_stock_to_sheet(self):
        """On startup, ensure all stock items have a Current Stock value on the sheet."""
        if not self._sheets:
            return
        try:
            stock_current = self.data.get("stock_current", {})
            if not stock_current:
                return
            self._sync_current_stock_to_sheet()
            logger.info(f"Backfilled Current Stock for {len(stock_current)} items")
        except Exception as e:
            logger.error(f"Current stock backfill error: {e}")

    def _sync_current_stock_to_sheet(self, item: str = None):
        """Update the Current Stock (column B) values on the Stock sheet.
        If item is given, only that item's row is updated; otherwise all items."""
        if not self._sheets:
            return
        ws = self._sheets._get_ws("Stock")
        if not ws:
            return
        try:
            existing = ws.get_all_values()
            if not existing or not existing[0]:
                return

            header = existing[0]

            # Ensure "Current Stock" is column B; insert it if missing
            if len(header) < 2 or header[1] != "Current Stock":
                new_header = [header[0], "Current Stock"] + header[1:]
                new_rows = [new_header]
                for row in existing[1:]:
                    new_rows.append([row[0] if row else "", ""] + (row[1:] if row else []))
                ws.clear()
                ws.update("A1", new_rows)
                existing = new_rows
                header = new_header

            stock_current = self.data.get("stock_current", {})

            if item is not None:
                items_to_sync = {item: stock_current.get(item, 0)}
            else:
                items_to_sync = stock_current

            for it, qty in items_to_sync.items():
                norm = normalize_item_name(it)
                row_idx = None
                for i, row in enumerate(existing[1:], 1):
                    if row and normalize_item_name(row[0]) == norm:
                        row_idx = i
                        break

                if row_idx is not None:
                    cell = gspread.utils.rowcol_to_a1(row_idx + 1, 2)
                    ws.update_acell(cell, qty)
                else:
                    new_row = [""] * len(header)
                    new_row[0] = it
                    new_row[1] = qty
                    ws.append_row(new_row)
                    existing.append(new_row)

            ws.format("A1:Z1", {"textFormat": {"bold": True}})

        except Exception as e:
            logger.error(f"Sheet sync error (current stock): {e}")

    def correct_stock_entry(self, item: str, new_qty: int, corrected_by: str):
        """Overwrite an item's current stock and today's history entry."""
        item = self._find_existing_stock_name(item)
        try:
            new_qty = int(new_qty)
        except (ValueError, TypeError):
            new_qty = 0

        today_sheet = _now().strftime("%d/%m/%y")

        if "stock_current" not in self.data:
            self.data["stock_current"] = {}
        self.data["stock_current"][item] = new_qty

        if "stock_history" not in self.data:
            self.data["stock_history"] = {}
        if today_sheet not in self.data["stock_history"]:
            self.data["stock_history"][today_sheet] = {}
        self.data["stock_history"][today_sheet][item] = new_qty

        self.data["stock"][item] = {
            "qty": str(new_qty),
            "updated_by": corrected_by,
            "updated_at": _fmt_ts(),
        }

        if self._sheets:
            try:
                self._sheets.write_stock_item(item, str(new_qty), today_sheet)
            except Exception as e:
                logger.error(f"Direct sheet write failed (correct_stock_entry): {e}")
        self._sync_current_stock_to_sheet(item)

        self._save_local_only()
        self._rebuild_shopping_list()

    def undo_last_stock_update(self, item: str) -> bool:
        """Undo the most recent stock_history entry for an item.
        Restores stock_current to the previous date's value (or removes if none).
        Returns True if an undo was performed, False if there was nothing to undo."""
        item = self._find_existing_stock_name(item)
        history = self.data.get("stock_history", {})

        # Collect all (date, qty) entries for this item, sorted by date desc
        norm = normalize_item_name(item)

        def parse_d(d):
            try:
                return datetime.strptime(d, "%d/%m/%y")
            except (ValueError, TypeError):
                return datetime.min

        entries = []
        for date_str, items_on_date in history.items():
            for k, v in items_on_date.items():
                if normalize_item_name(k) == norm:
                    entries.append((date_str, k, v))

        if not entries:
            return False

        entries.sort(key=lambda e: parse_d(e[0]), reverse=True)
        latest_date, latest_key, _latest_qty = entries[0]

        # Remove the latest entry
        del history[latest_date][latest_key]
        if not history[latest_date]:
            del history[latest_date]

        # Find previous entry (next most recent) to restore stock_current
        if len(entries) > 1:
            _, _, prev_qty = entries[1]
            try:
                prev_qty_val = int(prev_qty)
            except (ValueError, TypeError):
                prev_qty_val = prev_qty
            if "stock_current" not in self.data:
                self.data["stock_current"] = {}
            self.data["stock_current"][item] = prev_qty_val
            self.data["stock"][item] = {
                "qty": str(prev_qty_val),
                "updated_by": self.data.get("stock", {}).get(item, {}).get("updated_by", ""),
                "updated_at": _fmt_ts(),
            }
        else:
            # No previous entry — remove current stock tracking for this item
            self.data.get("stock_current", {}).pop(item, None)
            self.data.get("stock", {}).pop(item, None)

        self._sync_current_stock_to_sheet(item)
        self._save_local_only()
        self._rebuild_shopping_list()
        return True

    def _get_stock_minimums(self) -> dict:
        """Read stock minimums from Google Sheets (source of truth) with a
        fallback to the hardcoded sop_data.py dict for backward compatibility
        (e.g. before the sheet has been seeded, or sheets unavailable)."""
        if self._sheets:
            try:
                minimums = self._sheets.read_stock_minimums_from_sheet()
                if minimums:
                    return minimums
            except Exception as e:
                logger.error(f"_get_stock_minimums: sheet read failed: {e}")
        try:
            from sop_data import STOCK_MINIMUMS
            return STOCK_MINIMUMS
        except Exception as e:
            logger.error(f"_get_stock_minimums: fallback import failed: {e}")
            return {}

    def _get_ops_checklists(self) -> dict:
        """Read ops checklists from Google Sheets (source of truth) with a
        fallback to the hardcoded sop_data.py dict for backward compatibility."""
        if self._sheets:
            try:
                sop = self._sheets.read_sop_from_sheets()
                checklists = sop.get("ops_checklists") or {}
                if checklists:
                    return checklists
            except Exception as e:
                logger.error(f"_get_ops_checklists: sheet read failed: {e}")
        try:
            from sop_data import OPS_CHECKLISTS
            return OPS_CHECKLISTS
        except Exception as e:
            logger.error(f"_get_ops_checklists: fallback import failed: {e}")
            return {}

    def check_low_stock(self, items_updated: list = None) -> list:
        """Check stock against SOP minimums. Returns list of {item, qty, min} for low items.
        If items_updated is provided, only checks those items."""
        STOCK_MINIMUMS = self._get_stock_minimums()
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
        OPS_CHECKLISTS = self._get_ops_checklists()

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
        OPS_CHECKLISTS = self._get_ops_checklists()

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
            "done_at": _fmt_ts(),
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
            "done_at": _fmt_ts(),
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
            "added_at": _fmt_ts(),
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

    # ─── Shopping / To-Buy List ──────────────────────────────
    # Auto-generated from low stock — see _rebuild_shopping_list().
    def add_shopping_item(self, item: str, added_by: str = "", urgency: str = "normal"):
        item = clean_item_name(item)
        # Skip if already on the list
        existing = {normalize_item_name(s["item"]) for s in self.data.get("shopping_list", [])}
        if normalize_item_name(item) in existing:
            return
        entry = {"item": item}
        # 1. Write to Sheet FIRST
        if self._sheets:
            try:
                self._sheets.write_shopping_item(entry)
            except Exception as e:
                logger.error(f"Direct sheet write failed (shopping): {e}")

        # 2. Update JSON cache
        self.data["shopping_list"].append(entry)
        self._save_local_only()

    def get_shopping_list(self) -> list:
        return self.data.get("shopping_list", [])

    def mark_bought(self, index: int):
        """Remove an item from the shopping list (JSON + Sheet), then rebuild.
        Works for both auto-added (low stock) and manually added items."""
        items = self.data.get("shopping_list", [])
        if 0 <= index < len(items):
            removed_item = items[index].get("item", "")
            del items[index]
            self._save_local_only()
            # Also remove from sheet so _rebuild_shopping_list doesn't re-add it
            if self._sheets and removed_item:
                try:
                    self._sheets.remove_shopping_item(removed_item)
                except Exception as e:
                    logger.error(f"Sheet remove shopping item failed: {e}")
        self._rebuild_shopping_list()

    def clear_bought(self):
        """No-op kept for backward compat — list is always rebuilt from stock."""
        self._rebuild_shopping_list()

    def _rebuild_shopping_list(self):
        """Smart-merge the Shopping List based on stock levels.
        - ADD items that are below stock minimum and not already listed
        - REMOVE items that are tracked (in Stock Minimums) and now above minimum
        - KEEP items manually added by humans (not in Stock Minimums)
        Sheet is respected — never wipe manually added items."""
        STOCK_MINIMUMS = self._get_stock_minimums()
        if not STOCK_MINIMUMS:
            logger.error("_rebuild_shopping_list: no stock minimums available")
            return

        stock_current = self.data.get("stock_current", {})

        # Classify each tracked item as low or OK
        low_norms = set()   # normalized names of items below minimum
        ok_norms = set()    # normalized names of items at/above minimum

        for min_name, min_data in STOCK_MINIMUMS.items():
            min_qty = min_data.get("min", 0)

            # Find matching current-stock entry (exact, then fuzzy normalized match)
            current_qty = None
            for item_name, qty in stock_current.items():
                if item_name.lower() == min_name.lower():
                    current_qty = qty
                    break
            if current_qty is None:
                min_norm = normalize_item_name(min_name)
                for item_name, qty in stock_current.items():
                    if normalize_item_name(item_name) == min_norm:
                        current_qty = qty
                        break

            if current_qty is None:
                continue  # No stock data — can't judge

            try:
                current_qty = int(current_qty)
            except (ValueError, TypeError):
                continue

            norm = normalize_item_name(min_name)
            if current_qty < min_qty:
                low_norms.add(norm)
            else:
                ok_norms.add(norm)

        # Build {norm: display_name} for low items (use the minimums display name)
        low_display = {}
        for min_name in STOCK_MINIMUMS:
            norm = normalize_item_name(min_name)
            if norm in low_norms:
                low_display[norm] = min_name

        # Read existing items from sheet (or JSON cache if no sheets)
        existing_items = []
        if self._sheets:
            try:
                sheet_items = self._sheets.read_shopping_from_sheet()
                if sheet_items is not None:
                    existing_items = [s["item"] for s in sheet_items]
            except Exception as e:
                logger.error(f"Shopping list read failed: {e}")
                existing_items = [s["item"] for s in self.data.get("shopping_list", [])]
        else:
            existing_items = [s["item"] for s in self.data.get("shopping_list", [])]

        # Filter existing: remove restocked trackable items, keep everything else
        final_items = []
        for item in existing_items:
            norm = normalize_item_name(item)
            if norm in ok_norms:
                continue  # restocked above minimum — remove
            final_items.append(item)

        # Add low-stock items not already present
        final_norms = {normalize_item_name(i) for i in final_items}
        for norm, display in low_display.items():
            if norm not in final_norms:
                final_items.append(display)

        # Update JSON cache
        self.data["shopping_list"] = [{"item": name} for name in final_items]

        # Write merged list to sheet
        if self._sheets:
            try:
                self._sheets.clear_shopping_list()
                for name in final_items:
                    self._sheets.write_shopping_item({"item": name})
            except Exception as e:
                logger.error(f"Sheet rebuild failed (shopping list): {e}")

        self._save_local_only()

    # ─── Staff Registry ─────────────────────────────────────
    def add_staff(self, name: str, telegram_id: int, role: str = "staff"):
        self.data["staff"][name] = {
            "telegram_id": telegram_id,
            "role": role,
            "added_at": _fmt_ts(),
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
            "posted_at": _fmt_ts(),
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
            "created_at": _fmt_ts(),
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
                cal[index]["completed_at"] = _fmt_ts()
                cal[index]["completed_by"] = completed_by
            self._save()

    def complete_content_by_title(self, title_search: str, completed_by: str = "") -> bool:
        search = title_search.lower()
        for item in self.data.get("content_calendar", []):
            if (search in item.get("title", "").lower()
                    and item.get("status") in ("planned", "in_progress")):
                item["status"] = "done"
                item["completed_at"] = _fmt_ts()
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
            "created_at": _fmt_ts(),
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
                        self.data["action_items"][idx]["completed_at"] = _fmt_ts()
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
                        self.data["action_items"][idx]["last_chased"] = _fmt_ts()
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


class AliasStore:
    """Persistent alias mappings for stock item names.
    Maps alternative names → canonical stock names."""

    _instance = None
    FILE = DATA_DIR / "aliases.json"

    def __init__(self):
        self._data = {}  # {alias_normalized: canonical_name}
        self._load()

    def _load(self):
        if self.FILE.exists():
            try:
                with open(self.FILE) as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}

    def _save(self):
        with open(self.FILE, "w") as f:
            json.dump(self._data, f, indent=2)

    def add_alias(self, canonical: str, alias: str):
        """Map alias → canonical name."""
        norm = normalize_item_name(alias)
        self._data[norm] = canonical
        self._save()

    def resolve(self, name: str) -> str:
        """Return canonical name if alias exists, else the input name."""
        norm = normalize_item_name(name)
        return self._data.get(norm, name)

    def find_match(self, name: str) -> str:
        """Try alias store first, then return input."""
        return self.resolve(name)

    def get_all(self) -> dict:
        return dict(self._data)


def get_alias_store() -> AliasStore:
    if AliasStore._instance is None:
        AliasStore._instance = AliasStore()
    return AliasStore._instance


# ─── Factory (singleton) ─────────────────────────────────────
_store_instance: Optional[LocalJsonStore] = None

def get_store() -> LocalJsonStore:
    """Returns the singleton storage backend with Google Sheets sync."""
    global _store_instance
    if _store_instance is None:
        _store_instance = LocalJsonStore()
    return _store_instance
