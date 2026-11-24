"""
Data rules: the pure logic that keeps every record in one fixed format.

No Telegram, no Google, no AI in here, so all of it is covered by tests.
(The file keeps the name `storage.py` because the server's auto-update script
checks this file name before restarting the bot.)
"""
from __future__ import annotations

import difflib
import html
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import config

TZ = ZoneInfo(config.TIMEZONE)


# ─── Time ─────────────────────────────────────────────────

def now() -> datetime:
    return datetime.now(TZ)


def today() -> str:
    return now().date().isoformat()


def stamp() -> str:
    """Timestamp written into 'Saved At' style columns."""
    return now().strftime("%Y-%m-%d %H:%M")


def month_of(day: str) -> str:
    """'2026-11-12' -> '2026-11'."""
    return (day or "")[:7]


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(text, default_year: int | None = None) -> str | None:
    """Turn any date we meet into YYYY-MM-DD. Malaysian receipts are day-first.

    Accepts 2026-11-12, 12/11/2026, 12/11/26, 12-11-2026, 12.11.26, 12/11,
    12 Nov 2026, 12 November. Returns None if it can't be sure.
    """
    if text is None:
        return None
    if isinstance(text, (date, datetime)):
        return text.strftime("%Y-%m-%d")
    s = str(text).strip().lower()
    if not s:
        return None
    year_now = default_year or now().year

    def build(y, m, d):
        y = int(y)
        if y < 100:
            y += 2000
        try:
            return date(y, int(m), int(d)).isoformat()
        except ValueError:
            return None

    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return build(m.group(1), m.group(2), m.group(3))
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b", s)
    if m:
        return build(m.group(3), m.group(2), m.group(1))
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})$", s)
    if m:
        return build(year_now, m.group(2), m.group(1))
    m = re.match(r"^(\d{1,2})\s*([a-z]{3})[a-z]*\.?,?\s*(\d{2,4})?$", s)
    if m and m.group(2) in _MONTHS:
        return build(m.group(3) or year_now, _MONTHS[m.group(2)], m.group(1))
    return None


# ─── Numbers & names ──────────────────────────────────────

def to_number(value) -> float | None:
    """'RM 1,234.50' -> 1234.5 ; '-5.00' -> -5.0 ; '' / None / 'abc' -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower().replace("rm", "").replace(",", "").replace(" ", "")
    if s.startswith("(") and s.endswith(")"):  # accounting style (5.00)
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None


def rm(value) -> str:
    n = to_number(value) or 0
    return f"{'-' if n < 0 else ''}{config.CURRENCY}{abs(n):,.2f}"


def tidy_caps(name: str) -> str:
    """'DUTCH LADY F/CREAM MILK 1L' -> 'Dutch Lady F/Cream Milk 1L'. Leaves mixed case alone."""
    if not name or name != name.upper():
        return name
    def word(w):
        if any(ch.isdigit() for ch in w):
            return w  # sizes/codes: 1L, 400ML, 10S
        return re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), w).replace("'S", "'s")
    return " ".join(word(w) for w in name.split(" "))


def qty_text(q) -> str:
    q = to_number(q) or 0
    return str(int(q)) if q == int(q) else f"{q:g}"


def clean_name(name) -> str:
    """Tidy an item/supplier name: trim, single spaces, no trailing ':' or '-'."""
    s = re.sub(r"\s+", " ", str(name or "")).strip()
    return s.rstrip(":-–— ").strip()


def name_key(name) -> str:
    """Matching key: lowercase letters/digits only. 'Full Cream Milk:' == 'full cream milk'."""
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def match_name(name: str, known: list[str], cutoff: float = 0.82) -> str | None:
    """Find the existing item a typed name refers to, or None."""
    key = name_key(name)
    if not key:
        return None
    by_key = {name_key(k): k for k in known}
    if key in by_key:
        return by_key[key]
    # 'milk' typed, only one known item contains it as a whole word
    contains = [k for kk, k in by_key.items() if re.search(rf"\b{re.escape(key)}\b", kk)]
    if len(contains) == 1:
        return contains[0]
    close = difflib.get_close_matches(key, list(by_key), n=1, cutoff=cutoff)
    return by_key[close[0]] if close else None


def esc(text) -> str:
    """Escape text for Telegram HTML messages."""
    return html.escape(str(text if text is not None else ""), quote=False)


# ─── Receipts ─────────────────────────────────────────────

def normalize_receipt(raw: dict, sender: str) -> dict:
    """Put an AI reading into our fixed shape. Keeps what was printed;
    only fills a gap when it is pure arithmetic (qty × unit price)."""
    raw = raw or {}
    items = []
    for it in raw.get("items") or []:
        name = tidy_caps(clean_name(it.get("name")))
        if not name:
            continue
        qty = to_number(it.get("qty"))
        unit = to_number(it.get("unit_price"))
        line = to_number(it.get("line_total"))
        if qty is None or qty <= 0:
            qty = 1.0
        if line is None and unit is not None:
            line = round(qty * unit, 2)
        if unit is None and line is not None:
            unit = round(line / qty, 4)
        cat = (it.get("category") or "").strip().capitalize()
        items.append({
            "name": name,
            "qty": qty,
            "unit_price": unit if unit is not None else 0.0,
            "line_total": line if line is not None else 0.0,
            "category": cat if cat in config.CATEGORIES else "Ingredients",
        })
    paid_by = clean_name(raw.get("paid_by")) or sender
    if name_key(paid_by) in {"", "staff", "staff member", "unknown", "customer", "n a", "none", "cashier"}:
        paid_by = sender
    return {
        "supplier": tidy_caps(clean_name(raw.get("supplier"))) or "Unknown shop",
        "date": parse_date(raw.get("date")) or today(),
        "items": items,
        "discount": abs(to_number(raw.get("discount")) or 0.0),
        "rounding": to_number(raw.get("rounding")) or 0.0,
        "tax": to_number(raw.get("tax")) or 0.0,
        "total": to_number(raw.get("total")),
        "payment": clean_name(raw.get("payment_method")).lower() or "unknown",
        "paid_by": paid_by,
    }


def check_receipt(r: dict) -> dict:
    """Do the items add up to the printed total?"""
    items_sum = round(sum(i["line_total"] for i in r["items"]), 2)
    expected = round(items_sum - r["discount"] + r["rounding"] + r["tax"], 2)
    total = r["total"]
    if total is None:
        return {"ok": False, "items_sum": items_sum, "expected": expected, "diff": None,
                "reason": "no total found on the receipt"}
    diff = round(total - expected, 2)
    ok = abs(diff) <= config.TOTAL_TOLERANCE and bool(r["items"])
    reason = "" if ok else ("no items found" if not r["items"]
                            else f"items add up to {rm(expected)}, receipt says {rm(total)}")
    return {"ok": ok, "items_sum": items_sum, "expected": expected, "diff": diff, "reason": reason}


def receipt_key(r: dict) -> str:
    """Same shop + same date + same total = the same receipt."""
    return f"{r['date']}|{name_key(r['supplier'])}|{(to_number(r['total']) or 0):.2f}"


def receipt_text(r: dict, check: dict, receipt_id: str) -> str:
    """The message staff see before saving (Telegram HTML)."""
    lines = [f"🧾 <b>{esc(r['supplier'])}</b>  ·  {esc(r['date'])}", ""]
    for n, it in enumerate(r["items"], 1):
        lines.append(f"{n}. {esc(it['name'])}\n"
                     f"     {qty_text(it['qty'])} × {rm(it['unit_price'])} = <b>{rm(it['line_total'])}</b>"
                     f"  <i>{esc(it['category'])}</i>")
    lines.append("")
    if r["discount"]:
        lines.append(f"Discount: −{rm(r['discount'])}")
    if r["rounding"]:
        lines.append(f"Rounding: {rm(r['rounding'])}")
    if r["tax"]:
        lines.append(f"Tax: {rm(r['tax'])}")
    lines.append(f"💰 <b>Total: {rm(r['total'])}</b>")
    lines.append(f"👤 Paid by: {esc(r['paid_by'])}  ·  {esc(r['payment'])}")
    lines.append("")
    if check["ok"]:
        lines.append("✅ Items add up to the total.")
    else:
        lines.append(f"⚠️ <b>Check this:</b> {esc(check['reason'])}.")
    lines.append(f"\n<i>To fix, reply to this message, e.g. <code>2 qty 12</code>, "
                 f"<code>2 price 7.29</code>, <code>paid by Ali</code>, <code>delete 3</code>. "
                 f"Ref {esc(receipt_id)}</i>")
    return "\n".join(lines)


# ─── Receipt corrections typed by staff ───────────────────

_FIELD_WORDS = {
    "qty": "qty", "quantity": "qty", "x": "qty",
    "price": "unit_price", "unit": "unit_price", "each": "unit_price", "@": "unit_price",
    "total": "line_total", "amount": "line_total", "line": "line_total",
    "name": "name", "cat": "category", "category": "category",
}


def parse_edits(text: str) -> list[dict]:
    """Turn a reply like '2 qty 12; paid by Ali' into a list of edits.
    Unknown lines come back as {'op': 'unknown', 'text': ...}."""
    edits = []
    for part in re.split(r"[\n;]+", text or ""):
        p = part.strip()
        if not p:
            continue
        low = p.lower()
        m = re.match(r"^(?:delete|del|remove|rm)\s*#?(\d+)$", low)
        if m:
            edits.append({"op": "delete", "n": int(m.group(1))})
            continue
        m = re.match(r"^add\s+(.+?)\s+([\d.]+)\s*[x×*]\s*(?:rm)?\s*([\d.,]+)$", low)
        if m:
            name = p[4:4 + len(m.group(1))]
            edits.append({"op": "add", "name": clean_name(name), "qty": float(m.group(2)),
                          "unit_price": to_number(m.group(3))})
            continue
        m = re.match(r"^#?(\d+)\s+(qty|quantity|x|price|unit|each|@|total|amount|line|name|cat|category)\s*[:=]?\s*(.+)$", low)
        if m:
            field = _FIELD_WORDS[m.group(2)]
            raw_value = p[len(p) - len(m.group(3)):].strip()
            if field in ("qty", "unit_price", "line_total"):
                value = to_number(raw_value)
                if value is None:
                    edits.append({"op": "unknown", "text": p})
                    continue
            elif field == "category":
                value = next((c for c in config.CATEGORIES if c.lower().startswith(raw_value.lower()[:4])), None)
                if value is None:
                    edits.append({"op": "unknown", "text": p})
                    continue
            else:
                value = clean_name(raw_value)
            edits.append({"op": "item", "n": int(m.group(1)), "field": field, "value": value})
            continue
        m = re.match(r"^paid\s*(?:by)?\s*[:=]?\s*(.+)$", low)
        if m:
            edits.append({"op": "set", "field": "paid_by", "value": clean_name(p[len(p) - len(m.group(1)):])})
            continue
        m = re.match(r"^(date|shop|supplier|store|total|discount|rounding|tax|payment|pay)\s*[:=]?\s*(.+)$", low)
        if m:
            key = {"shop": "supplier", "store": "supplier", "pay": "payment"}.get(m.group(1), m.group(1))
            raw_value = p[len(p) - len(m.group(2)):].strip()
            if key == "date":
                value = parse_date(raw_value)
            elif key in ("total", "discount", "rounding", "tax"):
                value = to_number(raw_value)
                if value is not None and key == "discount":
                    value = abs(value)
            elif key == "payment":
                value = raw_value.lower()
            else:
                value = clean_name(raw_value)
            if value is None or value == "":
                edits.append({"op": "unknown", "text": p})
            else:
                edits.append({"op": "set", "field": key, "value": value})
            continue
        edits.append({"op": "unknown", "text": p})
    return edits


def apply_edits(r: dict, edits: list[dict]) -> list[str]:
    """Apply edits in place. Returns problems to tell the user (empty = all good)."""
    problems = []
    # deletes last, highest number first, so numbers in other edits stay valid
    ordered = [e for e in edits if e["op"] != "delete"] + sorted(
        [e for e in edits if e["op"] == "delete"], key=lambda e: -e["n"])
    for e in ordered:
        if e["op"] == "unknown":
            problems.append(f"didn't understand “{e['text']}”")
        elif e["op"] == "set":
            r[e["field"]] = e["value"]
        elif e["op"] == "add":
            unit = e["unit_price"] or 0.0
            r["items"].append({"name": e["name"], "qty": e["qty"], "unit_price": unit,
                               "line_total": round(e["qty"] * unit, 2), "category": "Ingredients"})
        elif e["op"] in ("item", "delete"):
            n = e["n"]
            if not 1 <= n <= len(r["items"]):
                problems.append(f"there is no item {n}")
                continue
            if e["op"] == "delete":
                r["items"].pop(n - 1)
                continue
            it = r["items"][n - 1]
            it[e["field"]] = e["value"]
            # keep the line consistent with what was just corrected
            if e["field"] == "qty":
                it["line_total"] = round(it["qty"] * it["unit_price"], 2)
            elif e["field"] == "unit_price":
                it["line_total"] = round(it["qty"] * it["unit_price"], 2)
            elif e["field"] == "line_total" and it["qty"]:
                it["unit_price"] = round(it["line_total"] / it["qty"], 4)
    return problems


# ─── Money questions (refused in the staff group) ─────────

MONEY_PATTERNS = [
    r"\bsales?\b", r"\brevenue\b", r"\bprofits?\b", r"\bexpenses?\b", r"\bcosts?\b", r"\bearn(s|ed|ing|ings)?\b",
    r"\bspen[dt]\b", r"\bspending\b", r"\bincome\b", r"\bbudget\b", r"\bsalar(y|ies)\b", r"\bwages?\b",
    r"\bpayroll\b", r"\bgaji\b", r"\buntung\b", r"\bjualan\b", r"\bbelanja\b", r"\bwho paid\b", r"\bpayback\b",
    r"\bmonthly totals?\b", r"\bfinancial\b", r"\bfinances?\b", r"\bp ?& ?l\b", r"\bpnl\b",
    r"\bhow much (did|do|does|have|has|are|were|was) (we|i|you|they|the cafe|sudu)\b",
    r"\bhours? worked\b", r"\bworking hours\b", r"\bwork(ed)? hours\b", r"\bovertime\b", r"\bjam kerja\b",
    r"\bhours?\b.*\b(work|worked|working|shift|shifts)\b", r"\b(work|worked|shift)\b.*\bhours?\b",
]
REFUSAL = "Financial info is only available in the owner group. Check with the boss."


def is_money_question(text: str) -> bool:
    low = (text or "").lower()
    return any(re.search(p, low) for p in MONEY_PATTERNS)


# ─── Stock ────────────────────────────────────────────────

OUT_WORDS = {"out", "oos", "habis", "nil", "none", "finish", "finished", "kosong", "empty", "zero", "0"}
STATUS_WORDS = {"ok", "okay", "low", "yes", "no", "enough", "full", "ada", "sikit", "some", "many", "ya", "y", "n"}


def parse_stock_sheet(text: str) -> dict:
    """Read a stock count like Edwin's paper checklist, by code (no AI, nothing dropped).

    'Full Cream Milk: 120' -> counted · 'Salt:' -> blank (ask, never assume 0) ·
    'Lychee: habis/out/OOS/nil' -> 0 · 'Milk: OK/LOW' -> refused (needs a number)
    Returns {'items': [(name, qty)], 'blanks': [name], 'invalid': [(name, word)], 'other': [line]}.
    """
    out = {"items": [], "blanks": [], "invalid": [], "other": []}
    # one item per line, or several per line separated by , or ; (but not inside brackets)
    lines = [p.strip() for p in re.split(r"\n|[,;](?![^()]*\))", text or "")]
    for line in lines:
        line = re.sub(r"^\s*(?:[-•*]|\d+[.)])\s+", "", line).strip()  # bullets / numbering
        if not line:
            continue
        m = (re.match(r"^(.+?)\s*[:=]\s*(.*)$", line)                              # Name: value
             or re.match(r"^(.+?)\s+(\d+(?:\.\d+)?(?:\s*[a-z.]+)*)\s*$", line, re.I)  # name 120 btl
             or re.match(r"^(.+?)\s+(\S+)\s*$", line))                              # name habis
        if not m:
            out["other"].append(line)
            continue
        name, value = clean_name(m.group(1)), m.group(2).strip().lower()
        if not name:
            out["other"].append(line)
            continue
        if value == "":
            out["blanks"].append(name)
            continue
        num = re.match(r"^(\d+(?:\.\d+)?)(?:\s*[a-z.]+)*$", value)
        if num:
            out["items"].append((name, float(num.group(1))))
        elif value in OUT_WORDS:
            out["items"].append((name, 0.0))
        elif value in STATUS_WORDS:
            out["invalid"].append((name, value.upper()))
        else:
            out["other"].append(line)
    return out


def looks_like_stock_sheet(text: str) -> bool:
    """Edwin-style list: 8+ 'Name: value' lines, or 4+ with a stock-check header."""
    lines = [l for l in (text or "").splitlines() if l.strip()]
    pairs = sum(1 for l in lines if re.match(r"^\s*(?:[-•*]|\d+[.)])?\s*[^:]{2,}:\s*\S*\s*$", l))
    header = re.search(r"stock\s*check|checked\s*by|stock\s*count", (text or "").lower())
    return pairs >= 8 or (pairs >= 4 and bool(header))


def parse_stock_lines(text: str) -> list[tuple[str, float]]:
    """Just the counted items, e.g. 'full cream milk 120\\ncondensed milk: 17'."""
    return parse_stock_sheet(text)["items"]


def stock_status(current, minimum) -> str:
    cur, mn = to_number(current), to_number(minimum)
    if cur is None:
        return "unknown"
    if cur <= 0:
        return "out"
    if mn is not None and cur < mn:
        return "low"
    return "ok"


# ─── Money reports (owners only) ──────────────────────────

def payback(receipts: list[dict], month: str) -> dict[str, float]:
    """How much each person paid for supplies in a month (from the Receipts tab)."""
    out: dict[str, float] = {}
    for row in receipts:
        if month_of(parse_date(row.get("Date")) or "") != month:
            continue
        who = clean_name(row.get("Paid By")) or "Unknown"
        out[who] = round(out.get(who, 0.0) + (to_number(row.get("Total (RM)")) or 0.0), 2)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def sum_by(rows: list[dict], month: str, key: str, amount: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        if month_of(parse_date(row.get("Date")) or "") != month:
            continue
        k = clean_name(row.get(key)) or "Other"
        out[k] = round(out.get(k, 0.0) + (to_number(row.get(amount)) or 0.0), 2)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def parse_month(text: str | None) -> str:
    """'' -> this month; '2026-10', '10/2026', 'oct', 'october 2026', 'last' -> YYYY-MM."""
    s = (text or "").strip().lower()
    n = now()
    if not s or s in ("this", "this month", "now"):
        return n.strftime("%Y-%m")
    if s in ("last", "last month", "prev", "previous"):
        y, m = (n.year, n.month - 1) if n.month > 1 else (n.year - 1, 12)
        return f"{y}-{m:02d}"
    m = re.match(r"^(\d{4})-(\d{1,2})$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{1,2})[/-](\d{4})$", s)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    m = re.match(r"^([a-z]{3})[a-z]*\s*(\d{4})?$", s)
    if m and m.group(1) in _MONTHS:
        return f"{m.group(2) or n.year}-{_MONTHS[m.group(1)]:02d}"
    return n.strftime("%Y-%m")
