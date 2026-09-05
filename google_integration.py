"""
Café Manager Bot — Google Integration (Sheets + Drive)
Free cloud storage for P&L tracking and receipt archiving.

Setup:
  1. Go to console.cloud.google.com (free)
  2. Create project → Enable "Google Sheets API" + "Google Drive API"
  3. Create Service Account → Download JSON key → save as credentials.json
  4. Share your Google Sheet with the service account email
  5. Share your Google Drive folder with the service account email

All free — no credit card needed.
"""
import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import config

_TZ = ZoneInfo(config.TIMEZONE)


def _now():
    """Current time in configured timezone (MYT)."""
    return datetime.now(_TZ)


def _fmt_ts():
    """Format current timestamp as '29/08/26-1425' style."""
    return _now().strftime("%d/%m/%y-%H%M")

logger = logging.getLogger(__name__)

# ─── Lazy imports (don't crash if not configured) ──────────
_sheets_client = None
_drive_service = None
_spreadsheet = None


def _get_credentials():
    """Load Google service account credentials."""
    creds_file = config.GOOGLE_SHEETS_CREDS_FILE
    if not creds_file or not Path(creds_file).exists():
        logger.info("Google credentials not found — Google integration disabled")
        return None
    try:
        from google.oauth2.service_account import Credentials
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/calendar.readonly",
        ]
        return Credentials.from_service_account_file(creds_file, scopes=scopes)
    except Exception as e:
        logger.error(f"Failed to load Google credentials: {e}")
        return None


# ═══════════════════════════════════════════════════════════
#  📊 GOOGLE SHEETS — P&L + Transaction Tracking
# ═══════════════════════════════════════════════════════════

def _get_spreadsheet():
    """Lazy-init: get or create the spreadsheet."""
    global _spreadsheet
    if _spreadsheet is not None:
        return _spreadsheet

    creds = _get_credentials()
    if creds is None:
        return None

    try:
        import gspread
        gc = gspread.authorize(creds)

        try:
            _spreadsheet = gc.open(config.SPREADSHEET_NAME)
            logger.info(f"Connected to Google Sheet: {config.SPREADSHEET_NAME}")
        except gspread.SpreadsheetNotFound:
            _spreadsheet = gc.create(config.SPREADSHEET_NAME)
            logger.info(f"Created new Google Sheet: {config.SPREADSHEET_NAME}")

        # Ensure required worksheets exist
        _ensure_worksheets(_spreadsheet)
        return _spreadsheet

    except Exception as e:
        logger.error(f"Google Sheets error: {e}")
        return None


def _ensure_worksheets(spreadsheet):
    """Create required worksheets if they don't exist.
    Tab order: Stock, Shopping List, Expenses, Expenses Detail,
               Monthly Summary, Daily Sales, POS Reports, Events
    Stock, Shopping List, Events are managed by storage.py SheetsSync.
    """
    existing = [ws.title for ws in spreadsheet.worksheets()]

    # --- Delete old/removed tabs ---
    OLD_TABS = [
        "Transactions", "Stock Ledger", "Cleaning Log",
        "Shifts", "Custom Instructions", "Action Items", "Staff",
    ]
    for old_tab in OLD_TABS:
        if old_tab in existing:
            try:
                spreadsheet.del_worksheet(spreadsheet.worksheet(old_tab))
                logger.info(f"Deleted old worksheet: {old_tab}")
            except Exception as e:
                logger.warning(f"Could not delete old worksheet {old_tab}: {e}")

    # Refresh existing list after deletions
    existing = [ws.title for ws in spreadsheet.worksheets()]

    # --- Tabs managed by google_integration.py ---

    # Expenses Detail sheet (every individual item from receipts)
    if "Expenses Detail" not in existing:
        ws = spreadsheet.add_worksheet("Expenses Detail", rows=5000, cols=12)
        ws.append_row([
            "Date", "Item", "Qty", "Amount (RM)",
            "Total (RM)", "Category", "Supplier", "Paid By",
            "Created At"
        ])
        logger.info("Created Expenses Detail worksheet")

    # Expenses sheet (monthly aggregation of purchases by item)
    if "Expenses" not in existing:
        ws = spreadsheet.add_worksheet("Expenses", rows=500, cols=6)
        ws.append_row([
            "Month", "Item", "Total Qty", "Total Spent (RM)",
            "Category", "Updated At"
        ])
        logger.info("Created Expenses worksheet")

    # Monthly Summary sheet
    if "Monthly Summary" not in existing:
        ws = spreadsheet.add_worksheet("Monthly Summary", rows=100, cols=10)
        ws.append_row([
            "Month", "Total Expenses (RM)", "Total Revenue (RM)",
            "Gross Profit (RM)", "Margin %", "Transaction Count",
            "Top Supplier", "Top Category", "Notes", "Generated At"
        ])
        logger.info("Created Monthly Summary worksheet")

    # Daily Sales sheet (per-day POS close-up data)
    if "Daily Sales" not in existing:
        ws = spreadsheet.add_worksheet("Daily Sales", rows=1000, cols=15)
        ws.append_row([
            "Date", "Total Sales (RM)", "Bills", "Pax",
            "Cash (RM)", "Card (RM)", "DuitNow (RM)", "TnG (RM)",
            "GrabPay (RM)", "Other (RM)",
            "Discount (RM)", "Void (RM)", "Refund (RM)",
            "Cashier", "Recorded By", "Notes", "Created At"
        ])
        logger.info("Created Daily Sales worksheet")

    # POS Reports sheet (detailed POS data — best sellers, items, toppings, etc.)
    if "POS Reports" not in existing:
        ws = spreadsheet.add_worksheet("POS Reports", rows=500, cols=10)
        ws.append_row([
            "Month", "Total Sales (RM)", "Transaction Count",
            "Avg Transaction (RM)", "Top Items", "Peak Hours",
            "Notes", "Analysis", "File Link", "Uploaded At"
        ])
        logger.info("Created POS Reports worksheet")

    # Remove default Sheet1 if other sheets exist
    if "Sheet1" in existing and len(existing) > 1:
        try:
            spreadsheet.del_worksheet(spreadsheet.worksheet("Sheet1"))
        except Exception:
            pass



def log_expense_detail(
    expense_date: str,
    supplier: str,
    item_name: str,
    qty: int = 1,
    unit_price: float = 0,
    amount: float = None,
    category: str = "ingredients",
    paid_by: str = "",
    receipt_link: str = "",
    recorded_by: str = "",
    notes: str = "",
) -> bool:
    """Log a single expense item to the Expenses Detail sheet.

    amount = actual total paid for this line (after discount proration).
             If not provided, falls back to qty * unit_price.
    """
    ss = _get_spreadsheet()
    if ss is None:
        return False

    from storage import clean_item_name
    item_name = clean_item_name(item_name)

    # Normalise category
    from config import ITEM_CATEGORIES, DEFAULT_CATEGORY
    cat = category.lower().strip() if category else DEFAULT_CATEGORY
    if cat == "useables":          # backward compat rename
        cat = "consumables"
    if cat not in ITEM_CATEGORIES:
        cat = DEFAULT_CATEGORY

    # Use the pre-calculated amount (discount-adjusted) if provided
    total = amount if amount is not None else qty * unit_price

    try:
        ws = ss.worksheet("Expenses Detail")
        row_data = [
            expense_date,
            item_name,
            qty,
            f"{total:.2f}",
            f"{total:.2f}",
            cat.capitalize(),
            supplier,
            paid_by,
            _fmt_ts(),
        ]
        ws.append_row(row_data, value_input_option="USER_ENTERED", table_range="A1")
        return True

    except Exception as e:
        logger.error(f"Failed to log expense detail: {e}")
        return False


def _normalize_item_name(name: str) -> str:
    """Normalize item name for deduplication.
    Strips punctuation variations so 'Coconut (Toasted, 100g)' and
    'Coconut - Toasted, 100g' match as the same item."""
    import re
    s = name.strip()
    # Remove content inside parentheses and the parens themselves
    # "Toasted Coconut (Toasted, 100g)" → "Toasted Coconut Toasted 100g"
    s = re.sub(r'[(){}[\]]', ' ', s)
    # Replace dashes, slashes, dots with space
    s = re.sub(r'[-/\\.,;:]+', ' ', s)
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip().lower()
    return s


def update_monthly_expenses(month: str = None) -> bool:
    """
    Aggregate ALL months from Expenses Detail into the Expenses tab.
    Groups by month + item, sorted newest month first, then alphabetical.
    Rebuilds the entire Expenses tab each time so it's always in sync.
    """
    ss = _get_spreadsheet()
    if ss is None:
        return False

    try:
        ws_detail = ss.worksheet("Expenses Detail")
        all_rows = ws_detail.get_all_records()

        if not all_rows:
            return True

        # Aggregate by (month, item) across ALL months
        # Key: (month_str, norm_name) → {qty, total, category, display_name}
        agg = {}
        for row in all_rows:
            date_str = str(row.get("Date", "")).strip()
            if len(date_str) < 6:
                continue  # skip bad dates
            # Parse month from multiple date formats
            row_month = None
            if "-" in date_str and len(date_str) >= 7:
                # YYYY-MM-DD format → take first 7 chars
                row_month = date_str[:7]  # "2026-08"
            elif "/" in date_str:
                # dd/mm/yy or dd/mm/yyyy format
                parts = date_str.split("/")
                if len(parts) >= 3:
                    dd, mm, yy = parts[0], parts[1], parts[2]
                    if len(yy) == 2:
                        yy = "20" + yy
                    row_month = f"{yy}-{mm.zfill(2)}"  # "2026-09"
            if not row_month:
                continue

            item = row.get("Item", "")
            if not item or not str(item).strip():
                continue  # skip empty/malformed rows
            norm = _normalize_item_name(item)
            try:
                qty = int(row.get("Qty", 0) or 0)
            except (ValueError, TypeError):
                qty = 0
            try:
                total = float(row.get("Total (RM)", 0) or row.get("Amount (RM)", 0) or 0)
            except (ValueError, TypeError):
                total = 0
            cat = row.get("Category", "ingredients")

            key = (row_month, norm)
            if key not in agg:
                agg[key] = {"qty": 0, "total": 0, "category": cat, "display_name": item, "latest_at": ""}
            agg[key]["qty"] += qty
            agg[key]["total"] += total
            # Track the latest Created At for this item
            created_at = str(row.get("Created At", ""))
            if created_at > agg[key]["latest_at"]:
                agg[key]["latest_at"] = created_at

        # Build rows sorted by month (newest first), then item name
        header = [
            "Month", "Item", "Total Qty", "Total Spent (RM)",
            "Category", "Updated At"
        ]
        data_rows = []
        for (row_month, norm_key), data in agg.items():
            data_rows.append([
                row_month,
                data["display_name"],
                data["qty"],
                f"{data['total']:.2f}",
                data["category"].capitalize(),
                data["latest_at"] or _fmt_ts(),  # fallback to now if no Created At
            ])

        # Sort: newest month first, then alphabetical within month
        data_rows.sort(key=lambda r: (-int(r[0].replace("-", "")), r[1].lower()))

        # Insert month separator rows for visual clarity
        final_rows = []
        current_month = None
        separator_row_indices = []  # track which rows are separators (1-indexed, after header)
        for row in data_rows:
            if row[0] != current_month:
                current_month = row[0]
                try:
                    from datetime import datetime as _dt
                    month_label = _dt.strptime(current_month, "%Y-%m").strftime("%B %Y")
                except ValueError:
                    month_label = current_month
                separator_row_indices.append(len(final_rows) + 2)  # +2 for header row + 1-index
                final_rows.append([f"═══ {month_label} ═══", "", "", "", "", ""])
            final_rows.append(row)

        ws_exp = ss.worksheet("Expenses")
        ws_exp.clear()
        ws_exp.update("A1", [header] + final_rows)
        ws_exp.format("A1:F1", {"textFormat": {"bold": True}})

        # Bold the month separator rows
        import time as _time
        for row_idx in separator_row_indices:
            try:
                ws_exp.format(f"A{row_idx}:F{row_idx}", {
                    "textFormat": {"bold": True},
                    "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
                })
                _time.sleep(1)  # Rate limit
            except Exception:
                pass  # Non-critical formatting

        logger.info(f"Updated expenses: {len(data_rows)} rows across {len(set(k[0] for k in agg))} months")
        return True

    except Exception as e:
        logger.error(f"Failed to update monthly expenses: {e}")
        return False


def delete_expense_rows(supplier: str, expense_date: str) -> int:
    """Find and delete matching rows from Expenses Detail sheet.
    Returns count of deleted rows. Re-aggregates monthly after deletion."""
    ss = _get_spreadsheet()
    if ss is None:
        return 0

    try:
        ws = ss.worksheet("Expenses Detail")

        all_rows = ws.get_all_values()
        if not all_rows or len(all_rows) < 2:
            return 0

        header = all_rows[0]
        # Find supplier and date column indices
        supplier_col = None
        date_col = None
        for i, h in enumerate(header):
            h_lower = h.lower().strip()
            if 'supplier' in h_lower:
                supplier_col = i
            if 'date' in h_lower:
                date_col = i

        if supplier_col is None or date_col is None:
            logger.error("Could not find supplier/date columns in Expenses Detail")
            return 0

        # Find rows to delete (from bottom up to preserve indices)
        rows_to_delete = []
        supplier_norm = supplier.lower().strip()
        for row_idx, row in enumerate(all_rows[1:], 2):  # row 2 = first data row
            if len(row) <= max(supplier_col, date_col):
                continue
            row_supplier = row[supplier_col].lower().strip()
            row_date = row[date_col].strip()
            if supplier_norm in row_supplier or row_supplier in supplier_norm:
                if row_date == expense_date:
                    rows_to_delete.append(row_idx)

        # Delete from bottom up
        deleted = 0
        for row_idx in sorted(rows_to_delete, reverse=True):
            try:
                ws.delete_rows(row_idx)
                deleted += 1
            except Exception as e:
                logger.error(f"Error deleting row {row_idx}: {e}")

        # Re-aggregate monthly expenses
        if deleted > 0:
            try:
                update_monthly_expenses()
            except Exception as e:
                logger.error(f"Error re-aggregating after deletion: {e}")

        return deleted
    except Exception as e:
        logger.error(f"delete_expense_rows error: {e}")
        return 0


def get_expenses_detail(month: str = None) -> list:
    """Get all expense detail rows for a month (YYYY-MM format)."""
    ss = _get_spreadsheet()
    if ss is None:
        return []

    if month is None:
        month = _now().date().strftime("%Y-%m")

    try:
        ws = ss.worksheet("Expenses Detail")
        all_rows = ws.get_all_records()
        return [r for r in all_rows if str(r.get("Date", "")).startswith(month)]
    except Exception as e:
        logger.error(f"Failed to get expenses detail: {e}")
        return []


def get_repayment_summary(month: str = None) -> dict:
    """
    Calculate how much each person paid vs the total, so you know
    who to reimburse and how much.

    Returns:
    {
        "month": "2026-08",
        "total_spent": 1234.56,
        "by_person": {
            "Ali": {"paid": 500.00, "items": 12, "categories": {"Ingredients": 400, ...}},
            "Kendrick": {"paid": 734.56, "items": 18, ...},
        }
    }
    """
    expenses = get_expenses_detail(month)
    if not expenses:
        return {}

    total_spent = 0
    by_person = {}

    for row in expenses:
        try:
            amount = float(row.get("Total (RM)", 0) or 0)
        except (ValueError, TypeError):
            amount = 0

        total_spent += amount
        person = row.get("Paid By", "Unknown") or "Unknown"
        cat = row.get("Category", "Other") or "Other"

        if person not in by_person:
            by_person[person] = {"paid": 0, "items": 0, "categories": {}}

        by_person[person]["paid"] += amount
        by_person[person]["items"] += 1
        by_person[person]["categories"][cat] = (
            by_person[person]["categories"].get(cat, 0) + amount
        )

    return {
        "month": month or _now().date().strftime("%Y-%m"),
        "total_spent": total_spent,
        "by_person": by_person,
    }


def log_daily_sales(
    report_date: str,
    total_sales: float,
    bill_count: int,
    total_pax: int,
    payment_breakdown: list,
    total_discount: float = 0,
    total_void: float = 0,
    total_refund: float = 0,
    other_charge: float = 0,
    cashier: str = "",
    recorded_by: str = "",
    notes: str = "",
) -> bool:
    """Log a daily POS sales report to the Daily Sales worksheet."""
    ss = _get_spreadsheet()
    if ss is None:
        return False

    try:
        ws = ss.worksheet("Daily Sales")

        # Extract payment amounts by method
        pay_map = {}
        for p in payment_breakdown:
            method = (p.get("method", "") or "").lower()
            amount = float(p.get("amount", 0) or 0)
            pay_map[method] = pay_map.get(method, 0) + amount

        cash = pay_map.get("cash", 0)
        card = sum(v for k, v in pay_map.items()
                   if any(w in k for w in ("card", "credit", "debit", "visa", "master")))
        duitnow = sum(v for k, v in pay_map.items()
                      if "duitnow" in k or "duit now" in k)
        tng = sum(v for k, v in pay_map.items()
                  if "tng" in k or "touch" in k or "t&g" in k)
        grabpay = sum(v for k, v in pay_map.items()
                      if "grab" in k)
        # Everything else
        known = cash + card + duitnow + tng + grabpay
        other_pay = max(0, total_sales - known) if total_sales > known else 0

        ws.append_row([
            report_date,
            f"{total_sales:.2f}",
            bill_count,
            total_pax,
            f"{cash:.2f}",
            f"{card:.2f}",
            f"{duitnow:.2f}",
            f"{tng:.2f}",
            f"{grabpay:.2f}",
            f"{other_pay:.2f}",
            f"{total_discount:.2f}",
            f"{total_void:.2f}",
            f"{total_refund:.2f}",
            cashier,
            recorded_by,
            notes,
            _fmt_ts(),
        ])
        logger.info(f"Logged daily sales: {report_date} RM{total_sales:.2f}")
        return True

    except Exception as e:
        logger.error(f"Failed to log daily sales: {e}")
        return False


def log_pos_report(
    month: str,
    total_sales: float,
    transaction_count: int,
    avg_transaction: float,
    top_items: str,
    peak_hours: str,
    notes: str = "",
    analysis: str = "",
    file_link: str = "",
) -> bool:
    """Log monthly POS report data."""
    ss = _get_spreadsheet()
    if ss is None:
        return False

    try:
        ws = ss.worksheet("POS Reports")
        ws.append_row([
            month,
            f"{total_sales:.2f}",
            transaction_count,
            f"{avg_transaction:.2f}",
            top_items,
            peak_hours,
            notes,
            analysis[:500],
            file_link,
            _fmt_ts(),
        ])
        return True

    except Exception as e:
        logger.error(f"Failed to log POS report: {e}")
        return False


def get_pl_summary(month: str = None) -> dict:
    """Calculate P&L for a month using Expenses Detail + Daily Sales."""
    if month is None:
        month = _now().date().strftime("%Y-%m")

    expenses = get_expenses_detail(month)
    total_expenses = 0
    suppliers = {}

    for row in expenses:
        try:
            amount = float(row.get("Total (RM)", 0) or 0)
        except (ValueError, TypeError):
            amount = 0
        total_expenses += amount
        supplier = row.get("Supplier", "Unknown")
        suppliers[supplier] = suppliers.get(supplier, 0) + amount

    # Revenue from Daily Sales
    daily_sales = get_daily_sales_for_month(month)
    total_revenue = 0
    for ds in daily_sales:
        try:
            total_revenue += float(ds.get("Total Sales (RM)", 0) or 0)
        except (ValueError, TypeError):
            pass

    top_supplier = max(suppliers, key=suppliers.get) if suppliers else "N/A"

    return {
        "month": month,
        "total_expenses": total_expenses,
        "total_revenue": total_revenue,
        "gross_profit": total_revenue - total_expenses,
        "margin": ((total_revenue - total_expenses) / total_revenue * 100)
                  if total_revenue > 0 else 0,
        "transaction_count": len(expenses),
        "top_supplier": top_supplier,
    }


def get_daily_sales_for_month(month: str = None) -> list:
    """Get all daily sales entries for a month (YYYY-MM format)."""
    ss = _get_spreadsheet()
    if ss is None:
        return []

    if month is None:
        month = _now().date().strftime("%Y-%m")

    try:
        ws = ss.worksheet("Daily Sales")
        all_rows = ws.get_all_records()
        return [r for r in all_rows if str(r.get("Date", "")).startswith(month)]
    except Exception as e:
        logger.error(f"Failed to get daily sales: {e}")
        return []


def generate_monthly_summary(month: str = None) -> dict:
    """
    Calculate and write a monthly P&L summary to the Monthly Summary tab.
    Combines expenses (Expenses Detail) + revenue (Daily Sales).
    Returns the summary dict.
    """
    if month is None:
        month = _now().date().strftime("%Y-%m")

    # Get expense data from Expenses Detail
    pl = get_pl_summary(month)
    total_expenses = pl.get("total_expenses", 0) if pl else 0
    top_supplier = pl.get("top_supplier", "N/A") if pl else "N/A"
    expense_count = pl.get("transaction_count", 0) if pl else 0

    # Get revenue data from Daily Sales
    daily_sales = get_daily_sales_for_month(month)
    total_revenue = 0
    total_bills = 0
    total_pax = 0
    payment_totals = {}
    sales_days = 0

    for row in daily_sales:
        try:
            sales = float(row.get("Total Sales (RM)", 0) or 0)
            total_revenue += sales
            total_bills += int(row.get("Bills", 0) or 0)
            total_pax += int(row.get("Pax", 0) or 0)
            sales_days += 1

            # Aggregate payment methods
            for col in ["Cash (RM)", "Card (RM)", "DuitNow (RM)",
                        "TnG (RM)", "GrabPay (RM)", "Other (RM)"]:
                method = col.replace(" (RM)", "")
                val = float(row.get(col, 0) or 0)
                payment_totals[method] = payment_totals.get(method, 0) + val
        except (ValueError, TypeError):
            continue

    gross_profit = total_revenue - total_expenses
    margin = (gross_profit / total_revenue * 100) if total_revenue > 0 else 0
    avg_daily = total_revenue / sales_days if sales_days > 0 else 0
    avg_bill = total_revenue / total_bills if total_bills > 0 else 0

    # Top payment method
    top_payment = max(payment_totals, key=payment_totals.get) if payment_totals else "N/A"

    summary = {
        "month": month,
        "total_expenses": total_expenses,
        "total_revenue": total_revenue,
        "gross_profit": gross_profit,
        "margin": margin,
        "expense_count": expense_count,
        "sales_days": sales_days,
        "total_bills": total_bills,
        "total_pax": total_pax,
        "avg_daily_sales": avg_daily,
        "avg_bill": avg_bill,
        "top_supplier": top_supplier,
        "top_payment": top_payment,
        "payment_totals": payment_totals,
    }

    # Write to Monthly Summary sheet
    ss = _get_spreadsheet()
    if ss:
        try:
            ws = ss.worksheet("Monthly Summary")

            # Check if this month already has an entry — update or append
            existing = ws.get_all_records()
            row_idx = None
            for i, r in enumerate(existing):
                if r.get("Month", "") == month:
                    row_idx = i + 2  # +1 for header, +1 for 1-indexed
                    break

            notes = (
                f"{sales_days} trading days, "
                f"{total_bills} bills, "
                f"{total_pax} pax, "
                f"Avg bill RM{avg_bill:.2f}, "
                f"Top payment: {top_payment}"
            )

            row_data = [
                month,
                f"{total_expenses:.2f}",
                f"{total_revenue:.2f}",
                f"{gross_profit:.2f}",
                f"{margin:.1f}",
                expense_count,
                top_supplier,
                top_payment,
                notes,
                _fmt_ts(),
            ]

            if row_idx:
                # Update existing row
                ws.update(f"A{row_idx}:J{row_idx}", [row_data])
                logger.info(f"Updated Monthly Summary for {month}")
            else:
                ws.append_row(row_data)
                logger.info(f"Added Monthly Summary for {month}")

        except Exception as e:
            logger.error(f"Failed to write monthly summary: {e}")

    return summary


def get_all_data_for_ai(month: str = None) -> str:
    """Pull all Sheets data into a text block for AI to analyze."""
    parts = []

    # P&L summary (from Expenses Detail + Daily Sales)
    pl = get_pl_summary(month)
    if pl:
        parts.append(
            f"P&L for {pl['month']}:\n"
            f"  Expenses: RM{pl['total_expenses']:.2f}\n"
            f"  Revenue: RM{pl['total_revenue']:.2f}\n"
            f"  Gross Profit: RM{pl['gross_profit']:.2f}\n"
            f"  Margin: {pl['margin']:.1f}%\n"
            f"  Expense Items: {pl['transaction_count']}\n"
            f"  Top Supplier: {pl['top_supplier']}"
        )

    # Daily Sales (per-day revenue breakdown)
    daily_sales = get_daily_sales_for_month(month)
    if daily_sales:
        lines = [f"Daily Sales ({len(daily_sales)} days recorded):"]
        total_rev = 0
        for ds in daily_sales[-15:]:  # last 15 days
            day_total = 0
            try:
                day_total = float(ds.get("Total Sales (RM)", 0) or 0)
                total_rev += day_total
            except (ValueError, TypeError):
                pass
            lines.append(
                f"  {ds.get('Date', '?')}: RM{day_total:.2f}, "
                f"{ds.get('Bills', '?')} bills, "
                f"{ds.get('Pax', '?')} pax, "
                f"Cash RM{ds.get('Cash (RM)', 0)}, "
                f"Card RM{ds.get('Card (RM)', 0)}, "
                f"DuitNow RM{ds.get('DuitNow (RM)', 0)}"
            )
        lines.append(f"  → Month total revenue from Daily Sales: RM{total_rev:.2f}")
        parts.append("\n".join(lines))

    # Expenses Detail (itemised purchases with categories & who paid)
    expenses_detail = get_expenses_detail(month)
    if expenses_detail:
        lines = [f"Expenses Detail ({len(expenses_detail)} items this month):"]
        by_cat = {}
        by_person = {}
        by_item = {}
        for ed in expenses_detail[-30:]:
            cat = ed.get("Category", "?")
            person = ed.get("Paid By", "?")
            item_name = ed.get("Item", "?")
            try:
                amt = float(ed.get("Total (RM)", 0) or 0)
                qty = int(ed.get("Qty", 0) or 0)
            except (ValueError, TypeError):
                amt = 0
                qty = 0
            by_cat[cat] = by_cat.get(cat, 0) + amt
            by_person[person] = by_person.get(person, 0) + amt
            if item_name not in by_item:
                by_item[item_name] = {"qty": 0, "total": 0}
            by_item[item_name]["qty"] += qty
            by_item[item_name]["total"] += amt
            lines.append(
                f"  {ed.get('Date', '?')} | {item_name} x{qty} "
                f"| RM{amt:.2f} | {cat} | {ed.get('Supplier', '?')} | Paid: {person}"
            )
        lines.append("  By category: " + ", ".join(
            f"{k}: RM{v:.2f}" for k, v in sorted(by_cat.items())))
        lines.append("  By person: " + ", ".join(
            f"{k}: RM{v:.2f}" for k, v in sorted(by_person.items())))
        # Monthly totals per item (for price comparison)
        lines.append("  Monthly totals by item:")
        for item, data in sorted(by_item.items()):
            lines.append(f"    {item}: {data['qty']} units, RM{data['total']:.2f}")
        parts.append("\n".join(lines))

    # Repayment summary
    repay = get_repayment_summary(month)
    if repay and repay.get("by_person"):
        lines = [f"Repayment Summary ({repay['month']}):"]
        lines.append(f"  Total spent: RM{repay['total_spent']:.2f}")
        for person, info in repay["by_person"].items():
            lines.append(f"  {person}: paid RM{info['paid']:.2f} ({info['items']} items)")
        parts.append("\n".join(lines))

    # POS reports
    ss = _get_spreadsheet()
    if ss:
        try:
            ws = ss.worksheet("POS Reports")
            pos_rows = ws.get_all_records()
            if pos_rows:
                lines = ["POS Reports:"]
                for r in pos_rows[-6:]:
                    lines.append(
                        f"  {r.get('Month', '?')}: Sales RM{r.get('Total Sales (RM)', '?')}, "
                        f"{r.get('Transaction Count', '?')} txns, "
                        f"Top: {r.get('Top Items', '?')[:40]}"
                    )
                parts.append("\n".join(lines))
        except Exception:
            pass

    if not parts:
        return "No Google Sheets data available yet."

    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════
#  📁 GOOGLE DRIVE — Receipt Image Storage
# ═══════════════════════════════════════════════════════════

RECEIPTS_FOLDER_NAME = "CafeManager_Receipts"


def _get_drive_service():
    """Lazy-init Google Drive API service."""
    global _drive_service
    if _drive_service is not None:
        return _drive_service

    creds = _get_credentials()
    if creds is None:
        return None

    try:
        from googleapiclient.discovery import build
        _drive_service = build("drive", "v3", credentials=creds)
        logger.info("Connected to Google Drive")
        return _drive_service
    except ImportError:
        logger.warning(
            "google-api-python-client not installed — Drive uploads disabled. "
            "Install with: pip install google-api-python-client"
        )
        return None
    except Exception as e:
        logger.error(f"Google Drive error: {e}")
        return None


def _get_or_create_receipts_folder() -> Optional[str]:
    """Get or create the receipts folder on Google Drive. Returns folder ID."""
    service = _get_drive_service()
    if service is None:
        return None

    try:
        # Check if folder exists
        query = (
            f"name='{RECEIPTS_FOLDER_NAME}' and "
            f"mimeType='application/vnd.google-apps.folder' and "
            f"trashed=false"
        )
        results = service.files().list(q=query, spaces="drive", fields="files(id, name)").execute()
        folders = results.get("files", [])

        if folders:
            return folders[0]["id"]

        # Create it
        folder_meta = {
            "name": RECEIPTS_FOLDER_NAME,
            "mimeType": "application/vnd.google-apps.folder",
        }
        folder = service.files().create(body=folder_meta, fields="id").execute()
        folder_id = folder.get("id")
        logger.info(f"Created receipts folder on Drive: {folder_id}")
        return folder_id

    except Exception as e:
        logger.error(f"Drive folder error: {e}")
        return None


def _get_or_create_month_subfolder(month: str) -> Optional[str]:
    """Get or create a monthly subfolder (e.g. '2026-08') inside receipts folder."""
    service = _get_drive_service()
    parent_id = _get_or_create_receipts_folder()
    if service is None or parent_id is None:
        return None

    try:
        query = (
            f"name='{month}' and "
            f"mimeType='application/vnd.google-apps.folder' and "
            f"'{parent_id}' in parents and "
            f"trashed=false"
        )
        results = service.files().list(q=query, spaces="drive", fields="files(id)").execute()
        folders = results.get("files", [])

        if folders:
            return folders[0]["id"]

        folder_meta = {
            "name": month,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = service.files().create(body=folder_meta, fields="id").execute()
        return folder.get("id")

    except Exception as e:
        logger.error(f"Drive subfolder error: {e}")
        return None


def upload_receipt_to_drive(
    image_bytes: bytes,
    filename: str,
    mime_type: str = "image/jpeg",
    description: str = "",
) -> Optional[str]:
    """
    Upload a receipt image to Google Drive.
    Returns the web view link (shareable URL).
    Files are organized: CafeManager_Receipts/2026-08/filename
    """
    service = _get_drive_service()
    if service is None:
        return None

    month = _now().date().strftime("%Y-%m")
    folder_id = _get_or_create_month_subfolder(month)
    if folder_id is None:
        return None

    try:
        from googleapiclient.http import MediaInMemoryUpload

        # Build filename: 2026-08-22_receipt_supplier.jpg
        file_meta = {
            "name": filename,
            "parents": [folder_id],
            "description": description[:500],
        }
        media = MediaInMemoryUpload(image_bytes, mimetype=mime_type)

        uploaded = service.files().create(
            body=file_meta,
            media_body=media,
            fields="id, webViewLink",
        ).execute()

        link = uploaded.get("webViewLink", "")
        file_id = uploaded.get("id", "")

        # Make viewable by anyone with link
        try:
            service.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
            ).execute()
        except Exception:
            pass  # Still works, just not shareable

        logger.info(f"Receipt uploaded to Drive: {filename}")
        return link

    except Exception as e:
        logger.error(f"Drive upload error: {e}")
        return None


def upload_file_to_drive(
    file_bytes: bytes,
    filename: str,
    mime_type: str = "application/octet-stream",
    subfolder: str = "POS_Reports",
) -> Optional[str]:
    """Upload any file to Drive (for POS reports etc). Returns web view link."""
    service = _get_drive_service()
    parent_id = _get_or_create_receipts_folder()
    if service is None or parent_id is None:
        return None

    try:
        from googleapiclient.http import MediaInMemoryUpload

        # Get or create subfolder
        query = (
            f"name='{subfolder}' and "
            f"mimeType='application/vnd.google-apps.folder' and "
            f"'{parent_id}' in parents and "
            f"trashed=false"
        )
        results = service.files().list(q=query, spaces="drive", fields="files(id)").execute()
        folders = results.get("files", [])
        if folders:
            folder_id = folders[0]["id"]
        else:
            folder_meta = {
                "name": subfolder,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }
            folder = service.files().create(body=folder_meta, fields="id").execute()
            folder_id = folder.get("id")

        file_meta = {"name": filename, "parents": [folder_id]}
        media = MediaInMemoryUpload(file_bytes, mimetype=mime_type)

        uploaded = service.files().create(
            body=file_meta, media_body=media, fields="id, webViewLink"
        ).execute()

        link = uploaded.get("webViewLink", "")

        # Make viewable
        try:
            service.permissions().create(
                fileId=uploaded["id"],
                body={"type": "anyone", "role": "reader"},
            ).execute()
        except Exception:
            pass

        return link

    except Exception as e:
        logger.error(f"Drive file upload error: {e}")
        return None


# ═══════════════════════════════════════════════════════════
#  🎉 HOLIDAY SYSTEM
# ═══════════════════════════════════════════════════════════

def fetch_google_calendar_holidays(year: int) -> list:
    """Fetch public holidays from Google Calendar API for MY + SG.
    Uses the existing service account credentials.
    Returns list of {"date": "YYYY-MM-DD", "name": str, "country": str}."""
    holidays = []
    try:
        creds = _get_credentials()
        if not creds:
            return holidays

        from googleapiclient.discovery import build
        service = build('calendar', 'v3', credentials=creds)

        time_min = f"{year}-01-01T00:00:00Z"
        time_max = f"{year}-12-31T23:59:59Z"

        for country, cal_id in config.HOLIDAY_CALENDAR_IDS.items():
            try:
                events = service.events().list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy='startTime',
                ).execute()

                for event in events.get('items', []):
                    start = event.get('start', {})
                    date_str = start.get('date', start.get('dateTime', '')[:10])
                    if date_str:
                        holidays.append({
                            "date": date_str,
                            "name": event.get('summary', 'Public Holiday'),
                            "country": country,
                            "source": "google_calendar",
                        })
            except Exception as e:
                logger.error(f"Google Calendar error for {country}: {e}")
    except Exception as e:
        logger.error(f"fetch_google_calendar_holidays error: {e}")

    return holidays


def fetch_calendarific_holidays(year: int) -> list:
    """Fetch state-level holidays from Calendarific API (free tier).
    Returns list of {"date": "YYYY-MM-DD", "name": str, "state": str}."""
    holidays = []
    api_key = config.CALENDARIFIC_API_KEY
    if not api_key:
        logger.info("No CALENDARIFIC_API_KEY — skipping state holidays")
        return holidays

    import urllib.request
    import json as _json

    for loc in config.CALENDARIFIC_LOCATIONS:
        try:
            country = loc["country"]
            state = loc.get("state", "")
            url = (
                f"https://calendarific.com/api/v2/holidays"
                f"?api_key={api_key}&country={country}&year={year}"
            )
            if state:
                url += f"&location={state}"

            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read().decode())

            for h in data.get("response", {}).get("holidays", []):
                date_info = h.get("date", {})
                iso_date = date_info.get("iso", "")[:10]
                if iso_date:
                    holidays.append({
                        "date": iso_date,
                        "name": h.get("name", "Holiday"),
                        "state": state or country,
                        "source": "calendarific",
                    })
        except Exception as e:
            logger.error(f"Calendarific error for {loc}: {e}")

    return holidays


def refresh_holiday_cache() -> dict:
    """Combine all holiday sources, deduplicate, save to data/holidays.json."""
    import json as _json
    from pathlib import Path

    cache_file = Path(__file__).parent / "data" / "holidays.json"
    cache_file.parent.mkdir(exist_ok=True)
    current_year = _now().year
    next_year = current_year + 1

    all_holidays = []

    # Google Calendar holidays
    for year in (current_year, next_year):
        try:
            all_holidays.extend(fetch_google_calendar_holidays(year))
        except Exception as e:
            logger.error(f"Google Calendar fetch failed for {year}: {e}")

    # Calendarific state holidays
    for year in (current_year, next_year):
        try:
            all_holidays.extend(fetch_calendarific_holidays(year))
        except Exception as e:
            logger.error(f"Calendarific fetch failed for {year}: {e}")

    # School holidays
    school_lists = {
        current_year: getattr(config, f"SCHOOL_HOLIDAYS_{current_year}", []),
        next_year: getattr(config, f"SCHOOL_HOLIDAYS_{next_year}", []),
    }
    for year, school_list in school_lists.items():
        for start, end, label in school_list:
            all_holidays.append({
                "date": start,
                "end_date": end,
                "name": label,
                "source": "school_holidays",
            })

    # Deduplicate by (date, name)
    seen = set()
    unique = []
    for h in all_holidays:
        key = (h["date"], h["name"])
        if key not in seen:
            seen.add(key)
            unique.append(h)

    # Sort by date
    unique.sort(key=lambda x: x["date"])

    cache_data = {
        "last_updated": _fmt_ts(),
        "holidays": unique,
    }

    cache_file.parent.mkdir(exist_ok=True)
    with open(cache_file, "w") as f:
        _json.dump(cache_data, f, indent=2)

    logger.info(f"Holiday cache refreshed: {len(unique)} holidays")
    return cache_data


def get_upcoming_holidays(days_ahead: int = 14) -> list:
    """Read holiday cache + school holidays. Return holidays in the next N days."""
    import json as _json
    from pathlib import Path
    from datetime import timedelta

    cache_file = Path(__file__).parent / "data" / "holidays.json"
    today = _now().date()
    cutoff = today + timedelta(days=days_ahead)

    upcoming = []

    # Read cache
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                cache = _json.load(f)
            for h in cache.get("holidays", []):
                try:
                    h_date = datetime.strptime(h["date"], "%Y-%m-%d").date()
                    h_end = None
                    if h.get("end_date"):
                        h_end = datetime.strptime(h["end_date"], "%Y-%m-%d").date()

                    # Include if: single-day holiday in range, OR multi-day holiday overlaps range
                    if h_end:
                        if h_date <= cutoff and h_end >= today:
                            upcoming.append(h)
                    else:
                        if today <= h_date <= cutoff:
                            upcoming.append(h)
                except (ValueError, KeyError):
                    continue
        except Exception as e:
            logger.error(f"Error reading holiday cache: {e}")

    # Always check school holidays from config as a fallback, regardless of
    # whether the cache file exists or is populated. Check both the current
    # year and the next year so holidays near a year boundary aren't missed.
    current_year = today.year
    next_year = current_year + 1
    school_lists = {
        current_year: getattr(config, f"SCHOOL_HOLIDAYS_{current_year}", []),
        next_year: getattr(config, f"SCHOOL_HOLIDAYS_{next_year}", []),
    }
    for _year, school_list in school_lists.items():
        for start, end, label in school_list:
            try:
                s_date = datetime.strptime(start, "%Y-%m-%d").date()
                e_date = datetime.strptime(end, "%Y-%m-%d").date()
                if s_date <= cutoff and e_date >= today:
                    entry = {"date": start, "end_date": end, "name": label, "source": "school_holidays"}
                    # Avoid duplicates
                    if not any(h["date"] == start and h["name"] == label for h in upcoming):
                        upcoming.append(entry)
            except ValueError:
                continue

    upcoming.sort(key=lambda x: x["date"])
    return upcoming
