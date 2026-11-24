"""Sheet rules, using a fake Google Sheet (no network)."""
import pytest

import google_integration as g
import storage


class FakeWS:
    def __init__(self, title, rows):
        self.title, self.rows, self.id = title, [list(r) for r in rows], 1

    def row_values(self, n):
        return self.rows[n - 1] if n <= len(self.rows) else []

    def col_values(self, n):
        return [r[n - 1] if len(r) >= n else "" for r in self.rows]

    def get_all_records(self, expected_headers=None, default_blank=""):
        head = self.rows[0]
        return [dict(zip(head, r + [default_blank] * (len(head) - len(r)))) for r in self.rows[1:]]

    def append_rows(self, rows, **kw):
        assert kw["value_input_option"] == "USER_ENTERED"
        self.rows += [list(r) for r in rows]

    def delete_rows(self, i):
        self.rows.pop(i - 1)

    def batch_update(self, cells, **kw):
        for c in cells:
            col, row = c["range"][0], int("".join(ch for ch in c["range"].split(":")[0] if ch.isdigit()))
            for k, v in enumerate(c["values"][0]):
                r = self.rows[row - 1]
                idx = ord(col) - 65 + k
                r += [""] * (idx + 1 - len(r))
                r[idx] = v

    def update(self, values, rng, **kw):
        self.batch_update([{"range": rng, "values": values}])

    def acell(self, a1):
        class C:
            pass
        c = C()
        c.value = self.rows[int(a1[1:]) - 1][ord(a1[0]) - 65]
        return c


class FakeSS:
    def __init__(self, tabs):
        self.tabs = {t: FakeWS(t, rows) for t, rows in tabs.items()}

    def worksheet(self, name):
        import gspread
        if name not in self.tabs:
            raise gspread.WorksheetNotFound(name)
        return self.tabs[name]


@pytest.fixture
def ss(monkeypatch):
    fake = FakeSS({t: [h] for t, h in g.TABS.items()})
    monkeypatch.setattr(g, "_ss", fake)
    monkeypatch.setattr(g, "_checked", set())
    return fake


def test_cell_keeps_numbers_and_protects_text():
    assert g._cell(12.5) == 12.5
    assert g._cell("=SUM(A1)") == "'=SUM(A1)"
    assert g._cell("2026-11") == "'2026-11"         # month must stay text, not become a date
    assert g._cell("2026-11-12") == "2026-11-12"    # full dates become real dates
    assert g._cell(None) == ""


def test_wrong_header_is_refused(ss):
    ss.tabs["Stock"].rows[0] = ["Item", "Current Stock", "18/09/26"]   # the old 755-column layout
    with pytest.raises(g.SheetLayoutError):
        g.ws("Stock")


def test_save_receipt_and_duplicate(ss):
    r = storage.normalize_receipt({"supplier": "Giant", "date": "2026-11-12", "total": 13.38,
                                   "items": [{"name": "Bleach", "qty": 2, "unit_price": 6.69, "line_total": 13.38,
                                              "category": "Consumables"}]}, "Ken")
    g.save_receipt("R-1", r, "link", "Ken", "abc123")
    rec = ss.tabs["Receipts"].rows[1]
    assert rec[:6] == ["R-1", "2026-11-12", "'2026-11", "Giant", 13.38, "Ken"]
    item = ss.tabs["Receipt Items"].rows[1]
    assert item[4:9] == ["Bleach", "Consumables", 2.0, 6.69, 13.38]
    assert g.find_duplicate("abc123", "x") == "R-1"                       # same photo
    assert g.find_duplicate("zzz", storage.receipt_key(r)) == "R-1"       # same shop/date/total
    assert g.find_duplicate("zzz", "2026-11-13|giant|13.38") is None


def test_half_saved_receipt_is_rolled_back(ss, monkeypatch):
    r = storage.normalize_receipt({"supplier": "Giant", "date": "2026-11-12", "total": 1,
                                   "items": [{"name": "Ice", "qty": 1, "line_total": 1}]}, "Ken")
    real = g._append

    def boom(tab, rows):
        if tab == "Receipt Items":
            raise RuntimeError("network down")
        real(tab, rows)
    monkeypatch.setattr(g, "_append", boom)
    with pytest.raises(RuntimeError):
        g.save_receipt("R-2", r, "", "Ken", "h")
    assert len(ss.tabs["Receipts"].rows) == 1                              # header only


def test_sales_row_maps_payments():
    row = g.sales_row({"date": "2026-11-12", "total_sales": 1500, "bills": 80, "pax": 120,
                       "payments": [{"method": "Cash", "amount": 300}, {"method": "VISA Card", "amount": 700},
                                    {"method": "DuitNow QR", "amount": 250}, {"method": "Touch n Go", "amount": 150},
                                    {"method": "Boost", "amount": 100}]}, "Ken")
    assert row[:10] == ["2026-11-12", 1500, 80, 120, 300, 700, 250, 150, 0, 100]
    assert len(row) == len(g.TABS["Daily Sales"])


def test_stock_update_and_unknown(ss):
    ss.tabs["Stock"].rows += [["Full Cream Milk", 88, 40, "", "Right bench", "", ""],
                              ["Condensed Milk", 30, 10, "", "", "", ""]]
    done, unknown = g.update_stock([{"item": "full cream milk", "qty": 120},
                                    {"item": "Durian", "qty": 3}], "Ali")
    assert done == ["Full Cream Milk"] and unknown == [{"item": "Durian", "qty": 3}]
    assert ss.tabs["Stock"].rows[1][1] == 120 and ss.tabs["Stock"].rows[1][6] == "Ali"


def test_shopping_add_skips_duplicates_and_done(ss):
    assert g.shopping_add(["Ice", "ice ", "Straws"], "Ken") == ["Ice", "Straws"]
    assert g.shopping_add(["ICE", "Cups"], "Ali") == ["Cups"]
    assert g.shopping_done(2, "Ali") == "Ice"
    assert [r["Item"] for _, r in g.shopping_open()] == ["Straws", "Cups"]
    assert g.shopping_add(["Ice"], "Ken") == ["Ice"]                     # bought before → can be added again
