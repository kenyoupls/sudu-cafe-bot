"""
One-off clean-up of the SuduBot Google Sheet into the new fixed layout.

    python migrate_sheet.py            # DRY RUN: only shows what would change
    python migrate_sheet.py --apply    # does it

What --apply does, in order:
 1. Saves every tab's contents to backups/SuduBot-<time>.json on this computer.
 2. Renames old-layout tabs to "OLD <name>" (nothing is deleted).
 3. Builds the new tabs:
      Stock          ← old Stock ("Current Stock" column) + Stock Minimums
      Shopping List  ← old Shopping List items
      Receipts + Receipt Items ← old Expenses Detail (grouped by Receipt ID)
      Summary        ← formulas: spending, sales and payback per month
 4. Applies number/date formats to Daily Sales.
Recipes, Checklists, Inspection and Daily Sales keep their data as they are.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
from collections import OrderedDict

import config
import google_integration as g
import storage

APPLY = "--apply" in sys.argv
TODAY = storage.today()
RETIRE = ["Expenses", "Monthly Summary", "POS Reports", "Events", "Stock Minimums", "Expenses Detail"]


def say(msg=""):
    print(msg, flush=True)


def tab_values(ss) -> dict[str, list[list[str]]]:
    return {ws.title: ws.get_all_values() for ws in ss.worksheets()}


def header_ok(values, tab) -> bool:
    return bool(values) and [h.strip() for h in values[0][:len(g.TABS[tab])]] == g.TABS[tab]


def records(values) -> list[dict]:
    if not values:
        return []
    head = [h.strip() for h in values[0]]
    return [dict(zip(head, row + [""] * (len(head) - len(row)))) for row in values[1:]
            if any(str(c).strip() for c in row)]


def _core_key(name: str) -> str:
    """'Vanilla Ice Cream (big=4 small):' → 'vanilla ice cream' (notes in brackets ignored)."""
    return storage.name_key(re.sub(r"\(.*?\)", " ", name or ""))


def _same_item(a: str, b: str) -> bool:
    """Strict match for merging the two old tabs: same core name, or a plural/typo away."""
    ka, kb = _core_key(a), _core_key(b)
    return ka == kb or difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.9


def build_stock(old_stock, minimums) -> list[list]:
    rows: "OrderedDict[str, list]" = OrderedDict()
    for r in records(old_stock):
        name = storage.clean_name(r.get("Item"))
        if not name:
            continue
        cur = storage.to_number(r.get("Current Stock", r.get("Current")))
        rows[storage.name_key(name)] = [name, cur if cur is not None else "", "", "", "", "", ""]
    for r in records(minimums):
        name = storage.clean_name(r.get("Item"))
        if not name:
            continue
        key = next((k for k, row in rows.items() if _same_item(row[0], name)), storage.name_key(name))
        row = rows.setdefault(key, [name, "", "", "", "", "", ""])
        mn = storage.to_number(r.get("Min"))
        row[2] = mn if mn is not None else ""
        row[3] = r.get("Unit", "")
        row[4] = r.get("Location", "")
    return list(rows.values())


def old_stamp(text: str) -> str:
    """Old bot's '14/09/26-1658' → '2026-09-14 16:58' (anything else is kept as is)."""
    m = re.match(r"^(\d{1,2}/\d{1,2}/\d{2,4})-(\d{2})(\d{2})$", (text or "").strip())
    if m:
        return f"{storage.parse_date(m.group(1))} {m.group(2)}:{m.group(3)}"
    return text or ""


def build_receipts(expenses) -> tuple[list[list], list[list], list[str]]:
    groups: "OrderedDict[str, list[dict]]" = OrderedDict()
    warnings = []
    for r in records(expenses):
        day = storage.parse_date(r.get("Date"))
        if not day:
            warnings.append(f"skipped row with unreadable date: {r}")
            continue
        rid = r.get("Receipt ID") or f"OLD-{day}-{storage.name_key(r.get('Supplier'))[:12]}"
        groups.setdefault(rid, []).append({**r, "_date": day})
    receipts, items = [], []
    for rid, rows in groups.items():
        first = rows[0]
        total = round(sum(storage.to_number(x.get("Total (RM)")) or 0 for x in rows), 2)
        day, month = first["_date"], storage.month_of(first["_date"])
        supplier = storage.tidy_caps(storage.clean_name(first.get("Supplier")))
        paid_by = storage.clean_name(first.get("Paid By"))
        receipts.append([rid, day, month, supplier, total, paid_by, "unknown", len(rows), "",
                         "migrated", old_stamp(first.get("Created At", "")), ""])
        for x in rows:
            cat = storage.clean_name(x.get("Category")).capitalize()
            items.append([rid, day, month, supplier, storage.clean_name(x.get("Item")),
                          cat if cat in config.CATEGORIES else "Other",
                          storage.to_number(x.get("Qty")) or 1, storage.to_number(x.get("Unit Price (RM)")) or 0,
                          storage.to_number(x.get("Total (RM)")) or 0, paid_by])
    return receipts, items, warnings


SUMMARY = [
    ["Spending by month & category", "", "", "", "Sales by month", "", "", "Who paid (month, person)"],
    ["=QUERY('Receipt Items'!C:I, \"select C, F, sum(I) where C is not null group by C, F "
     "order by C desc label sum(I) 'Spent (RM)'\", 1)", "", "", "",
     "=QUERY({ARRAYFORMULA(TEXT('Daily Sales'!A2:A, \"yyyy-mm\")), 'Daily Sales'!B2:B}, "
     "\"select Col1, sum(Col2) where Col2 is not null group by Col1 order by Col1 desc "
     "label Col1 'Month', sum(Col2) 'Sales (RM)'\", 0)", "", "",
     "=QUERY(Receipts!C:F, \"select C, F, sum(E) where C is not null group by C, F "
     "order by C desc label sum(E) 'Paid (RM)'\", 1)"],
]


def main():
    ss = g._spreadsheet()
    say(f"Sheet: {ss.title}   ({'APPLY' if APPLY else 'DRY RUN, nothing will change'})\n")
    values = tab_values(ss)
    existing = set(values)

    stock_rows = build_stock(values.get("Stock", []), values.get("Stock Minimums", []))
    shop_rows = [[storage.clean_name(r.get("Item")), "", "migrated", TODAY, g.NEED, "", ""]
                 for r in records(values.get("Shopping List", [])) if storage.clean_name(r.get("Item"))
                 and r.get("Status") != g.DONE]
    receipts, items, warnings = build_receipts(values.get("Expenses Detail", []))

    plan = []
    for tab, rows in (("Stock", stock_rows), ("Shopping List", shop_rows),
                      ("Receipts", receipts), ("Receipt Items", items)):
        if tab in existing and header_ok(values[tab], tab):
            plan.append((tab, None, "already in the new layout, left as is"))
        else:
            plan.append((tab, rows, f"{'replace old tab' if tab in existing else 'create'} with {len(rows)} rows"))
    retire = [t for t in RETIRE if t in existing]

    say("Plan:")
    for tab, rows, what in plan:
        say(f"  • {tab}: {what}")
        for row in (rows or [])[:3]:
            say(f"      e.g. {row}")
    for t in retire:
        say(f"  • {t}: rename to 'OLD {t}' (kept, not deleted)")
    say("  • Summary: monthly spending / sales / payback formulas" + (" (exists, left as is)" if "Summary" in existing else ""))
    say(f"  • Daily Sales: {'formats only' if header_ok(values.get('Daily Sales', []), 'Daily Sales') else 'create'}")
    for w in warnings:
        say(f"  ⚠️ {w}")

    if not APPLY:
        say("\nDry run finished. Run again with --apply to make these changes.")
        return

    os.makedirs("backups", exist_ok=True)
    path = os.path.join("backups", f"{ss.title}-{storage.now():%Y%m%d-%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(values, f, ensure_ascii=False)
    say(f"\n💾 Backup saved: {path}")

    def retire_tab(name):
        old = f"OLD {name}"
        if old in existing:
            old = f"OLD {name} {storage.now():%Y%m%d-%H%M}"
        ss.worksheet(name).update_title(old)
        existing.add(old)
        say(f"  renamed {name} → {old}")

    for tab, rows, _ in plan:
        if rows is None:
            continue
        if tab in existing:
            retire_tab(tab)
            existing.discard(tab)
        ws = ss.add_worksheet(title=tab, rows=max(1000, len(rows) + 200), cols=len(g.TABS[tab]))
        ws.update([g.TABS[tab]] + [[g._cell(v) for v in r] for r in rows], "A1", value_input_option="USER_ENTERED")
        ss.batch_update({"requests": g.format_requests(ws.id, tab)})
        say(f"  created {tab} ({len(rows)} rows)")
    for t in retire:
        retire_tab(t)
    if "Daily Sales" in existing and header_ok(values["Daily Sales"], "Daily Sales"):
        ss.batch_update({"requests": g.format_requests(ss.worksheet("Daily Sales").id, "Daily Sales")})
    else:
        g.ws("Daily Sales")
    if "Summary" not in existing:
        ws = ss.add_worksheet(title="Summary", rows=200, cols=12)
        ws.update(SUMMARY, "A1", value_input_option="USER_ENTERED")
        ws.format("A1:H1", {"textFormat": {"bold": True}})
        say("  created Summary")
    say("\n✅ Done. Open the Sheet to check. Old tabs are kept as 'OLD …'; delete them yourself when happy.")


if __name__ == "__main__":
    main()
