"""Sheet clean-up: old layout → new layout (no network)."""
import migrate_sheet as m


def test_stock_merges_old_tabs_carefully():
    old_stock = [["Item", "Current Stock", "18/09/26"],
                 ["Full Cream Milk:", "120", "120"],
                 ["Vanilla Ice Cream (big=4 small):", "11", ""],
                 ["Honey Lemon Drink:", "21", ""],
                 ["Plastic Spoon", "4", ""]]
    minimums = [["Item", "Min", "Unit", "Location"],
                ["Full Cream Milk", "40", "", "Right bench"],
                ["Vanilla Ice Cream", "6", "", "Fridge"],
                ["Plastic Spoons", "100", "", ""],
                ["Honey", "1", "", ""]]
    rows = m.build_stock(old_stock, minimums)
    by_name = {r[0]: r for r in rows}
    assert by_name["Full Cream Milk"][:5] == ["Full Cream Milk", 120.0, 40.0, "", "Right bench"]
    assert by_name["Vanilla Ice Cream (big=4 small)"][2] == 6.0
    assert by_name["Plastic Spoon"][2] == 100.0
    assert by_name["Honey Lemon Drink"][2] == ""        # NOT merged with Honey
    assert by_name["Honey"][1:3] == ["", 1.0]
    assert len(rows) == 5


def test_receipts_grouped_and_cleaned():
    expenses = [["Date", "Item", "Qty", "Unit Price (RM)", "Total (RM)", "Category", "Supplier", "Paid By",
                 "Created At", "Receipt ID"],
                ["03/09/2026", "Water Jug", "1", "8.9", "8.9", "Equipment", "SUPER FULL STORE", "Ken",
                 "14/09/26-1658", "R-1"],
                ["03/09/2026", "Spoon", "1", "8.9", "8.9", "Equipment", "SUPER FULL STORE", "Ken",
                 "14/09/26-1658", "R-1"],
                ["bad date", "X", "1", "1", "1", "", "Shop", "Ken", "", "R-2"]]
    receipts, items, warnings = m.build_receipts(expenses)
    assert receipts == [["R-1", "2026-09-03", "2026-09", "Super Full Store", 17.8, "Ken", "unknown", 2, "",
                         "migrated", "2026-09-14 16:58", ""]]
    assert [i[4] for i in items] == ["Water Jug", "Spoon"] and items[0][6:9] == [1.0, 8.9, 8.9]
    assert len(warnings) == 1
