"""Data rules: dates, numbers, receipts, corrections, stock, reports."""
import pytest

import storage as s


@pytest.mark.parametrize("text,expected", [
    ("2026-11-12", "2026-11-12"),
    ("12/11/2026", "2026-11-12"),
    ("12/11/26", "2026-11-12"),
    ("03/09/2026", "2026-09-03"),      # day first, like the old sheet rows
    ("12-11-2026", "2026-11-12"),
    ("12.11.26", "2026-11-12"),
    ("12 Nov 2026", "2026-11-12"),
    ("12 November", f"{s.now().year}-11-12"),
    ("12/11", f"{s.now().year}-11-12"),
    ("31/02/2026", None),
    ("", None),
    (None, None),
    ("hello", None),
])
def test_parse_date(text, expected):
    assert s.parse_date(text) == expected


@pytest.mark.parametrize("value,expected", [
    ("RM 1,234.50", 1234.5), ("12.50", 12.5), (7, 7.0), ("-0.02", -0.02), ("(5.00)", -5.0),
    ("", None), (None, None), ("abc", None), (True, None),
])
def test_to_number(value, expected):
    assert s.to_number(value) == expected


def test_clean_and_match_names():
    assert s.clean_name("  Full Cream  Milk: ") == "Full Cream Milk"
    known = ["Full Cream Milk", "Low Fat Milk", "Condensed Milk", "Takeaway Bag"]
    assert s.match_name("full cream milk:", known) == "Full Cream Milk"
    assert s.match_name("takeaway bags", known) == "Takeaway Bag"
    assert s.match_name("milk", known) is None          # ambiguous: 3 milks
    assert s.match_name("condensed", known) == "Condensed Milk"
    assert s.match_name("durian", known) is None


RAW = {
    "supplier": "Lotus's Melaka", "date": "12/11/26",
    "items": [
        {"name": "Dutch Lady Full Cream Milk 1L", "qty": 12, "unit_price": 7.29, "line_total": 87.48,
         "category": "Ingredients"},
        {"name": "Bleach 2L", "qty": 1, "line_total": 6.69, "category": "consumables"},
        {"name": "Gula Melaka 1KG", "qty": 2, "unit_price": 11.5, "category": "Ingredients"},
    ],
    # total = 87.48 + 6.69 + 23.00 − 5 − 0.02
    "discount": -5, "rounding": -0.02, "total": 112.15, "payment_method": "Card", "paid_by": "Staff",
}


def test_normalize_receipt_keeps_printed_values():
    r = s.normalize_receipt(RAW, "Ken")
    assert r["date"] == "2026-11-12"
    assert r["paid_by"] == "Ken"                       # generic "Staff" replaced by the sender
    assert r["discount"] == 5.0                        # always positive
    milk, bleach, gula = r["items"]
    assert (milk["qty"], milk["unit_price"], milk["line_total"]) == (12, 7.29, 87.48)
    assert bleach["unit_price"] == 6.69 and bleach["category"] == "Consumables"
    assert gula["line_total"] == 23.0                  # filled by arithmetic only
    assert r["payment"] == "card"


def test_check_receipt():
    r = s.normalize_receipt(RAW, "Ken")
    c = s.check_receipt(r)
    assert c["ok"], c
    assert c["expected"] == 112.15 and c["diff"] == 0


def test_check_receipt_mismatch_and_missing_total():
    r = s.normalize_receipt({**RAW, "total": 150.00}, "Ken")
    c = s.check_receipt(r)
    assert not c["ok"] and "receipt says" in c["reason"]
    r = s.normalize_receipt({**RAW, "total": None}, "Ken")
    assert not s.check_receipt(r)["ok"]


def test_receipt_key_same_receipt():
    a = s.normalize_receipt(RAW, "Ken")
    b = s.normalize_receipt({**RAW, "supplier": "LOTUS'S  MELAKA", "date": "2026-11-12"}, "Ali")
    assert s.receipt_key(a) == s.receipt_key(b)


def test_parse_and_apply_edits():
    r = s.normalize_receipt(RAW, "Ken")
    edits = s.parse_edits("2 qty 3\n1 price 7.50; paid by Ali\ndelete 3\nadd Ice 2 x 5.00\n"
                          "date 13/11/2026; shop Giant; 2 cat equip; nonsense here")
    problems = s.apply_edits(r, edits)
    assert problems == ["didn't understand “nonsense here”"]
    names = [i["name"] for i in r["items"]]
    assert names == ["Dutch Lady Full Cream Milk 1L", "Bleach 2L", "Ice"]
    assert r["items"][0]["line_total"] == 90.0          # 12 × 7.50
    assert r["items"][1]["qty"] == 3 and r["items"][1]["line_total"] == 20.07
    assert r["items"][1]["category"] == "Equipment"
    assert r["items"][2]["line_total"] == 10.0
    assert (r["paid_by"], r["date"], r["supplier"]) == ("Ali", "2026-11-13", "Giant")


def test_edit_line_total_and_bad_item_number():
    r = s.normalize_receipt(RAW, "Ken")
    problems = s.apply_edits(r, s.parse_edits("1 total 90; 9 qty 2; total 120.00; discount -4"))
    assert r["items"][0]["unit_price"] == 7.5
    assert r["total"] == 120.0 and r["discount"] == 4.0
    assert problems == ["there is no item 9"]


def test_parse_stock_lines():
    assert s.parse_stock_lines("Full cream milk 120\ncondensed milk: 17, cups 2.5kg") == [
        ("Full cream milk", 120.0), ("condensed milk", 17.0), ("cups", 2.5)]


@pytest.mark.parametrize("cur,mn,status", [(0, 5, "out"), (3, 5, "low"), (5, 5, "ok"), (9, "", "ok"), ("", 5, "unknown")])
def test_stock_status(cur, mn, status):
    assert s.stock_status(cur, mn) == status


def test_payback_and_sum_by():
    rows = [
        {"Date": "2026-11-03", "Paid By": "Ken", "Total (RM)": "1,000.50", "Supplier": "Lotus's"},
        {"Date": "2026-11-20", "Paid By": "Ali", "Total (RM)": 40, "Supplier": "Giant"},
        {"Date": "2026-11-21", "Paid By": "Ken", "Total (RM)": 9.5, "Supplier": "Giant"},
        {"Date": "2026-10-30", "Paid By": "Ali", "Total (RM)": 999, "Supplier": "Giant"},
    ]
    assert s.payback(rows, "2026-11") == {"Ken": 1010.0, "Ali": 40.0}
    assert s.sum_by(rows, "2026-11", "Supplier", "Total (RM)") == {"Lotus's": 1000.5, "Giant": 49.5}


def test_parse_month():
    y = s.now().year
    assert s.parse_month("2026-3") == "2026-03"
    assert s.parse_month("10/2026") == "2026-10"
    assert s.parse_month("oct") == f"{y}-10"
    assert s.parse_month("September 2025") == "2025-09"
    assert s.parse_month("") == s.now().strftime("%Y-%m")


def test_tidy_caps_and_negative_money():
    assert s.tidy_caps("DUTCH LADY F/CREAM MILK 1L") == "Dutch Lady F/Cream Milk 1L"
    assert s.tidy_caps("LOTUS'S MELAKA") == "Lotus's Melaka"
    assert s.tidy_caps("iPhone Case") == "iPhone Case"
    assert s.rm(-0.02) == "-RM0.02" and s.rm(1234.5) == "RM1,234.50"


def test_receipt_text_escapes_html():
    r = s.normalize_receipt({**RAW, "supplier": "A&B <Shop>"}, "Ken")
    text = s.receipt_text(r, s.check_receipt(r), "R-1")
    assert "A&amp;B &lt;Shop&gt;" in text
