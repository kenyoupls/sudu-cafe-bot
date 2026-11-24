"""
Google Sheet: the one place all café records live.

Rules this file follows, so the Sheet never gets messy:
- Every tab has FIXED columns (TABS below). The bot refuses to write to a tab
  whose header row is different, and tells you to run migrate_sheet.py.
- The bot only ADDS rows or updates single cells. It never clears a tab,
  never adds columns, never deletes or renames tabs.
- Numbers go in as numbers, dates as YYYY-MM-DD.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import config
import storage

log = logging.getLogger(__name__)

TABS: dict[str, list[str]] = {
    "Receipts": ["Receipt ID", "Date", "Month", "Supplier", "Total (RM)", "Paid By", "Payment",
                 "Items", "Photo", "Submitted By", "Saved At", "Photo Hash"],
    "Receipt Items": ["Receipt ID", "Date", "Month", "Supplier", "Item", "Category", "Qty",
                      "Unit Price (RM)", "Line Total (RM)", "Paid By"],
    "Daily Sales": ["Date", "Total Sales (RM)", "Bills", "Pax", "Cash (RM)", "Card (RM)",
                    "DuitNow (RM)", "TnG (RM)", "GrabPay (RM)", "Other (RM)", "Discount (RM)",
                    "Void (RM)", "Refund (RM)", "Cashier", "Recorded By", "Notes", "Created At"],
    "Stock": ["Item", "Current", "Min", "Unit", "Location", "Updated At", "Updated By"],
    "Shopping List": ["Item", "Note", "Added By", "Added At", "Status", "Done By", "Done At"],
}
# How each column is displayed (applied when the bot or migration creates a tab).
FORMATS = {
    "Receipts": {"B": "DATE", "E": "MONEY"},
    "Receipt Items": {"B": "DATE", "G": "QTY", "H": "MONEY", "I": "MONEY"},
    "Daily Sales": {"A": "DATE", **{c: "MONEY" for c in "BEFGHIJKLM"}},
    "Stock": {"B": "QTY", "C": "QTY"},
}
# Read-only reference tabs staff can ask about.
REFERENCE_TABS = ["Bingsu Recipes", "Other Recipes", "Checklists", "Inspection"]

NEED, DONE = "Need to buy", "Bought"


class SheetLayoutError(Exception):
    """A tab's columns are not what the bot expects."""


class SheetNotConfigured(Exception):
    pass


_lock = threading.Lock()
_ss = None
_checked: set[str] = set()


def _spreadsheet():
    global _ss
    if _ss is None:
        if not Path(config.GOOGLE_SHEETS_CREDS_FILE).exists():
            raise SheetNotConfigured("credentials.json not found")
        import gspread
        gc = gspread.service_account(filename=config.GOOGLE_SHEETS_CREDS_FILE)
        _ss = gc.open_by_key(config.SPREADSHEET_ID) if config.SPREADSHEET_ID else gc.open(config.SPREADSHEET_NAME)
    return _ss


def format_requests(sheet_id: int, tab: str) -> list[dict]:
    """Bold frozen header + number formats for a tab (Sheets API batchUpdate requests)."""
    patterns = {"DATE": ("DATE", "yyyy-mm-dd"), "MONEY": ("NUMBER", "#,##0.00"), "QTY": ("NUMBER", "0.##")}
    reqs = [
        {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
    ]
    for col, kind in FORMATS.get(tab, {}).items():
        idx = ord(col) - ord("A")
        ftype, pattern = patterns[kind]
        reqs.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": idx, "endColumnIndex": idx + 1},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": ftype, "pattern": pattern}}},
            "fields": "userEnteredFormat.numberFormat"}})
    return reqs


def ws(tab: str):
    """Get a tab, creating it (with header + formats) if missing; check its header."""
    import gspread
    ss = _spreadsheet()
    headers = TABS[tab]
    try:
        sheet = ss.worksheet(tab)
    except gspread.WorksheetNotFound:
        with _lock:
            sheet = ss.add_worksheet(title=tab, rows=1000, cols=len(headers))
            sheet.update([headers], "A1", value_input_option="RAW")
            ss.batch_update({"requests": format_requests(sheet.id, tab)})
            log.info("Created tab %s", tab)
        _checked.add(tab)
        return sheet
    if tab not in _checked:
        first = [h.strip() for h in sheet.row_values(1)]
        if first[:len(headers)] != headers:
            raise SheetLayoutError(
                f"The '{tab}' tab has different columns than expected. "
                f"Run migrate_sheet.py to update the Sheet layout.")
        _checked.add(tab)
    return sheet


def _cell(value):
    """Make a value safe for USER_ENTERED: text that looks like a formula or a
    date stays text; numbers stay numbers."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return round(value, 4)
    s = str(value)
    if s[:1] in ("=", "+", "-", "@") or (len(s) == 7 and s[4:5] == "-" and s[:4].isdigit()):
        return "'" + s
    return s


def _append(tab: str, rows: list[list]) -> None:
    ws(tab).append_rows([[_cell(v) for v in row] for row in rows],
                        value_input_option="USER_ENTERED", insert_data_option="INSERT_ROWS",
                        table_range="A1")


def records(tab: str) -> list[dict]:
    # Formatted values: dates come back as YYYY-MM-DD text (not serial numbers);
    # storage.to_number copes with "1,234.50".
    return ws(tab).get_all_records(expected_headers=TABS[tab], default_blank="")


# ─── Receipts ─────────────────────────────────────────────

def find_duplicate(photo_hash: str, key: str) -> str | None:
    """Receipt ID of an already-saved copy of this receipt, if any."""
    for row in records("Receipts"):
        same_photo = photo_hash and row.get("Photo Hash") == photo_hash
        row_key = storage.receipt_key({"date": storage.parse_date(row.get("Date")) or "",
                                       "supplier": row.get("Supplier", ""),
                                       "total": row.get("Total (RM)")})
        if same_photo or row_key == key:
            return str(row.get("Receipt ID") or "?")
    return None


def save_receipt(rid: str, r: dict, photo_link: str, submitted_by: str, photo_hash: str) -> None:
    month = storage.month_of(r["date"])
    _append("Receipts", [[rid, r["date"], month, r["supplier"], r["total"], r["paid_by"], r["payment"],
                          len(r["items"]), photo_link, submitted_by, storage.stamp(), photo_hash]])
    try:
        _append("Receipt Items", [[rid, r["date"], month, r["supplier"], it["name"], it["category"],
                                   it["qty"], it["unit_price"], it["line_total"], r["paid_by"]]
                                  for it in r["items"]])
    except Exception:
        _delete_rows_where("Receipts", "Receipt ID", rid)  # don't leave half a receipt
        raise


def _delete_rows_where(tab: str, column: str, value: str) -> None:
    sheet = ws(tab)
    col = TABS[tab].index(column) + 1
    rows = [i for i, v in enumerate(sheet.col_values(col), 1) if i > 1 and v == value]
    for i in reversed(rows):
        sheet.delete_rows(i)


# ─── Daily sales ──────────────────────────────────────────

def sales_row_for(day: str) -> int | None:
    sheet = ws("Daily Sales")
    for i, v in enumerate(sheet.col_values(1), 1):
        if i > 1 and storage.parse_date(v) == day:
            return i
    return None


def sales_row(s: dict, recorded_by: str) -> list:
    pay = {"cash": 0.0, "card": 0.0, "duitnow": 0.0, "tng": 0.0, "grab": 0.0}
    other = 0.0
    for p in s.get("payments") or []:
        method = storage.name_key(p.get("method"))
        amount = storage.to_number(p.get("amount")) or 0.0
        key = ("cash" if "cash" in method else
               "card" if any(w in method for w in ("card", "credit", "debit", "visa", "master", "amex")) else
               "duitnow" if "duit" in method or "qr" in method else
               "tng" if "tng" in method or "touch" in method or "t g" in method else
               "grab" if "grab" in method else None)
        if key:
            pay[key] += amount
        else:
            other += amount
    num = storage.to_number
    return [s["date"], num(s.get("total_sales")) or 0.0, int(num(s.get("bills")) or 0), int(num(s.get("pax")) or 0),
            pay["cash"], pay["card"], pay["duitnow"], pay["tng"], pay["grab"], other,
            num(s.get("discount")) or 0.0, num(s.get("void")) or 0.0, num(s.get("refund")) or 0.0,
            storage.clean_name(s.get("cashier")), recorded_by, "", storage.stamp()]


def save_sales(s: dict, recorded_by: str, replace_row: int | None = None) -> None:
    row = sales_row(s, recorded_by)
    if replace_row:
        ws("Daily Sales").update([[_cell(v) for v in row]], f"A{replace_row}", value_input_option="USER_ENTERED")
    else:
        _append("Daily Sales", [row])


# ─── Stock ────────────────────────────────────────────────

def stock() -> list[dict]:
    return [r for r in records("Stock") if storage.clean_name(r.get("Item"))]


def update_stock(updates: list[dict], by: str) -> tuple[list[str], list[dict]]:
    """Set counts for known items. Returns (updated names, unknown updates)."""
    sheet = ws("Stock")
    names = sheet.col_values(1)
    index = {storage.name_key(n): i for i, n in enumerate(names, 1) if i > 1}
    cells, done, unknown = [], [], []
    for u in updates:
        row = index.get(storage.name_key(u["item"]))
        if not row:
            unknown.append(u)
            continue
        cells += [{"range": f"B{row}", "values": [[u["qty"]]]},
                  {"range": f"F{row}:G{row}", "values": [[storage.stamp(), by]]}]
        done.append(names[row - 1])
    if cells:
        sheet.batch_update(cells, value_input_option="USER_ENTERED")
    return done, unknown


def add_stock_item(name: str, qty: float, by: str, minimum: float | None = None) -> None:
    _append("Stock", [[storage.clean_name(name), qty, minimum if minimum is not None else "", "", "",
                       storage.stamp(), by]])


# ─── Shopping list ────────────────────────────────────────

def shopping_open() -> list[tuple[int, dict]]:
    """Open items as (sheet row number, record)."""
    rows = records("Shopping List")
    return [(i, r) for i, r in enumerate(rows, 2) if r.get("Status") != DONE and storage.clean_name(r.get("Item"))]


def shopping_add(items: list[str], by: str, note: str = "") -> list[str]:
    """Add items not already on the open list. Returns the names actually added."""
    have = {storage.name_key(r.get("Item")) for _, r in shopping_open()}
    new, seen = [], set()
    for name in items:
        key = storage.name_key(name)
        if key and key not in have and key not in seen:
            seen.add(key)
            new.append(storage.clean_name(name))
    if new:
        _append("Shopping List", [[n, note, by, storage.stamp(), NEED, "", ""] for n in new])
    return new


def shopping_done(row: int, by: str) -> str:
    sheet = ws("Shopping List")
    item = sheet.acell(f"A{row}").value or ""
    sheet.update([[DONE, by, storage.stamp()]], f"E{row}:G{row}", value_input_option="USER_ENTERED")
    return item


# ─── Reading for questions ────────────────────────────────

def read_tab(name: str) -> list[list[str]]:
    try:
        return _spreadsheet().worksheet(name).get_all_values()
    except Exception:
        return []


def table_text(rows: list[list], limit: int = 400) -> str:
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    return "\n".join(" | ".join(str(c) for c in r) for r in rows[:limit])


def reference_context() -> str:
    """Opening hours, recipes, checklists, inspection and stock: safe for everyone."""
    import places
    parts = ["## Café info\n" + places.as_text(places.cafe_info())]
    parts += [f"## {tab}\n{table_text(read_tab(tab))}" for tab in REFERENCE_TABS]
    stock_rows = [[r.get("Item"), r.get("Current"), r.get("Min"), r.get("Unit"), r.get("Location")] for r in stock()]
    parts.append("## Stock (Item | Current | Min | Unit | Location)\n" + table_text(stock_rows))
    parts.append("## Shopping list (open)\n" + "\n".join(r["Item"] for _, r in shopping_open()))
    return "\n\n".join(parts)


def money_context(month: str) -> str:
    """Receipts and sales for a month and the one before. Owners only."""
    y, m = int(month[:4]), int(month[5:7])
    prev = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
    keep = (month, prev)
    rec = [r for r in records("Receipts") if storage.month_of(storage.parse_date(r.get("Date")) or "") in keep]
    items = [r for r in records("Receipt Items") if storage.month_of(storage.parse_date(r.get("Date")) or "") in keep]
    sales = [r for r in records("Daily Sales") if storage.month_of(storage.parse_date(r.get("Date")) or "") in keep]

    def rows(data, cols):
        return table_text([cols] + [[r.get(c, "") for c in cols] for r in data], limit=1500)

    return "\n\n".join([
        f"## Receipts ({prev} and {month})\n" + rows(rec, ["Date", "Supplier", "Total (RM)", "Paid By", "Payment"]),
        "## Receipt items\n" + rows(items, ["Date", "Supplier", "Item", "Category", "Qty", "Line Total (RM)"]),
        "## Daily sales\n" + rows(sales, TABS["Daily Sales"][:13]),
    ])
