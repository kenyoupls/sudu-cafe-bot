"""
AI: reading photos, understanding stock messages, answering questions.

Every call asks Gemini for JSON in a fixed shape (a schema), so the bot never
has to guess what the AI meant. If a model is busy or out of free quota, the
next model in config.AI_MODELS is tried, then Groq if a key is set.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import logging

import config
import storage

log = logging.getLogger(__name__)

_client = None


def _gemini():
    global _client
    if _client is None and config.GEMINI_API_KEY:
        from google import genai
        from google.genai import types
        _client = genai.Client(api_key=config.GEMINI_API_KEY,
                               http_options=types.HttpOptions(timeout=config.AI_TIMEOUT_SECONDS * 1000))
    return _client


class AIUnavailable(Exception):
    """Every model failed (busy, out of quota, no key)."""


# ─── Schemas (compact form → Gemini Schema) ───────────────

def _schema(spec):
    from google.genai import types
    T = types.Type
    if isinstance(spec, list):  # [item_spec]
        return types.Schema(type=T.ARRAY, items=_schema(spec[0]))
    if isinstance(spec, dict):
        props = {k.rstrip("?"): _schema(v) for k, v in spec.items()}
        required = [k for k in spec if not k.endswith("?")]
        return types.Schema(type=T.OBJECT, properties=props, required=required)
    if isinstance(spec, tuple):  # ("enum", [...])
        return types.Schema(type=T.STRING, enum=spec[1])
    return types.Schema(type={"str": T.STRING, "num": T.NUMBER, "int": T.INTEGER}[spec])


RECEIPT_SPEC = {
    "supplier": "str",
    "date": "str",
    "items": [{"name": "str", "qty": "num", "unit_price?": "num", "line_total": "num",
               "category": ("enum", config.CATEGORIES)}],
    "discount?": "num",
    "rounding?": "num",
    "tax?": "num",
    "total": "num",
    "payment_method?": "str",
    "paid_by?": "str",
}
SALES_SPEC = {
    "date": "str",
    "total_sales": "num",
    "bills?": "int",
    "pax?": "int",
    "payments?": [{"method": "str", "amount": "num"}],
    "discount?": "num",
    "void?": "num",
    "refund?": "num",
    "cashier?": "str",
}
PHOTO_SPEC = {
    "kind": ("enum", ["receipt", "sales_report", "other"]),
    "receipt?": RECEIPT_SPEC,
    "sales?": SALES_SPEC,
}
STOCK_SPEC = {"updates": [{"typed": "str", "item": "str", "qty": "num"}]}

_RECEIPT_RULES = f"""Receipt / invoice rules (Malaysia, amounts in RM):
- Copy what is PRINTED or WRITTEN. Never invent, round or recalculate values.
- One entry per product line. name = product as printed, tidy capitalisation, keep size/brand (e.g. "Dutch Lady Full Cream Milk 1L").
- qty = number of units bought (may be decimal for kg). If no quantity is shown, qty = 1.
- line_total = the amount printed at the right of that line. unit_price = price per unit if printed.
- "12 x 7.29   87.48" means qty 12, unit_price 7.29, line_total 87.48.
- discount = total discounts/vouchers as a positive number. rounding = rounding adjustment (can be negative). tax = SST/service tax if added on top.
- total = the final amount paid.
- date: write YYYY-MM-DD. Printed dates are DAY first (12/11/26 = 12 Nov 2026).
- category: Ingredients (food/drink for the menu), Consumables (cups, lids, straws, tissue, soap, cleaning), Equipment (tools, machines, decor, one-off), Other.
- payment_method: cash, card, ewallet, transfer or unknown.
- paid_by: ONLY if the caption names who paid; otherwise leave it out."""

_SALES_RULES = """POS end-of-day / sales report rules:
- date YYYY-MM-DD (day first if printed as 12/11/26). total_sales = net sales total.
- payments = each payment method with its amount (Cash, Card, DuitNow, TnG, GrabPay...).
- bills = number of bills/transactions; pax = customers. Leave out what isn't shown."""


# ─── Calling the models ───────────────────────────────────

def _call_gemini(model: str, parts: list, spec) -> dict:
    from google.genai import types
    response = _gemini().models.generate_content(
        model=model,
        contents=parts,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=_schema(spec),
        ),
    )
    text = (response.text or "").strip()
    if not text:
        raise ValueError("empty answer")
    return json.loads(text)


def _call_groq(prompt: str, image: bytes | None) -> dict:
    from groq import Groq
    content: list = [{"type": "text", "text": prompt + "\nReply with JSON only."}]
    if image:
        b64 = base64.b64encode(image).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    resp = Groq(api_key=config.GROQ_API_KEY, timeout=config.AI_TIMEOUT_SECONDS).chat.completions.create(
        model=config.GROQ_VISION_MODEL if image else config.GROQ_TEXT_MODEL,
        messages=[{"role": "user", "content": content}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


async def _ask_json(prompt: str, spec, image: bytes | None = None,
                    models: list[str] | None = None, groq_shape: str = "") -> tuple[dict, str]:
    """Ask for JSON, trying each model in turn. Returns (data, model_used)."""
    errors = []
    if _gemini():
        from google.genai import types
        parts = ([types.Part.from_bytes(data=image, mime_type="image/jpeg")] if image else []) + [prompt]
        for attempt in range(2):  # Gemini "busy" spikes are short: one more round after a pause
            if attempt:
                if not any("503" in e or "UNAVAILABLE" in e or "timeout" in e.lower() for e in errors):
                    break
                await asyncio.sleep(4)
            for model in models or config.AI_MODELS:
                try:
                    return await asyncio.to_thread(_call_gemini, model, parts, spec), model
                except Exception as e:  # busy / quota / timeout / bad JSON → next model
                    errors.append(f"{model}: {str(e)[:120]}")
                    log.warning("AI model %s failed: %s", model, str(e)[:200])
    if config.GROQ_API_KEY and models is None:
        try:
            return await asyncio.to_thread(_call_groq, prompt + "\n" + groq_shape, image), "groq"
        except Exception as e:
            errors.append(f"groq: {str(e)[:120]}")
            log.warning("Groq failed: %s", str(e)[:200])
    raise AIUnavailable("; ".join(errors) or "no AI key configured")


# ─── Public functions ─────────────────────────────────────

async def read_photo(image: bytes, caption: str = "") -> dict:
    """What is this photo? Returns {'kind', 'receipt', 'sales', 'model'}."""
    prompt = (
        "You are the record keeper for a café in Melaka, Malaysia. Look at this photo.\n"
        "kind = 'receipt' if it is a shop receipt, supplier invoice or handwritten bill for things the café BOUGHT; "
        "'sales_report' if it is the café's own POS end-of-day / daily sales summary; otherwise 'other'.\n"
        "Fill 'receipt' only for receipts, 'sales' only for sales reports.\n\n"
        f"{_RECEIPT_RULES}\n\n{_SALES_RULES}\n\n"
        f"Today is {storage.today()}. Caption from staff: {caption or '(none)'}"
    )
    shape = ('{"kind": "receipt|sales_report|other", "receipt": {"supplier","date","items":[{"name","qty",'
             '"unit_price","line_total","category"}],"discount","rounding","tax","total","payment_method","paid_by"}, '
             '"sales": {"date","total_sales","bills","pax","payments":[{"method","amount"}],"discount","void","refund","cashier"}}')
    data, model = await _ask_json(prompt, PHOTO_SPEC, image=image, groq_shape=shape)
    return {"kind": data.get("kind", "other"), "receipt": data.get("receipt"),
            "sales": data.get("sales"), "model": model}


async def reread_receipt(image: bytes, caption: str, problem: str) -> dict | None:
    """Second, more careful read with the stronger model when numbers don't add up."""
    prompt = (
        "Read this café purchase receipt very carefully, line by line.\n"
        f"A first reading had a problem: {problem}. Check every quantity and amount against the photo.\n\n"
        f"{_RECEIPT_RULES}\n\nToday is {storage.today()}. Caption: {caption or '(none)'}"
    )
    try:
        data, _ = await _ask_json(prompt, RECEIPT_SPEC, image=image, models=[config.AI_CHECK_MODEL])
        return data
    except AIUnavailable:
        return None


MATCH_SPEC = {"matches": [{"typed": "str", "item": "str"}]}


async def match_items(names: list[str], known_items: list[str]) -> dict[str, str]:
    """AI only for names code couldn't match: 'FC milk' → 'Full Cream Milk'. Unknown stays ''."""
    if not names:
        return {}
    prompt = (
        "Match each name typed by café staff to the EXACT item name from the stock list, "
        "only when it clearly means that item (abbreviations, typos, Malay/English). "
        "If it is not on the list, return an empty string for item.\n"
        f"Stock list: {json.dumps(known_items, ensure_ascii=False)}\n"
        f"Typed names: {json.dumps(names, ensure_ascii=False)}"
    )
    try:
        data, _ = await _ask_json(prompt, MATCH_SPEC, groq_shape='{"matches":[{"typed","item"}]}')
    except AIUnavailable:
        return {}
    out = {}
    for m in data.get("matches", []):
        hit = storage.match_name(m.get("item", ""), known_items, cutoff=0.95) if m.get("item") else None
        if hit:
            out[storage.clean_name(m.get("typed"))] = hit
    return out


async def read_stock(text: str, known_items: list[str]) -> dict:
    """A stock count message → {'updates': [{'typed','item','qty','new'}], 'blanks', 'invalid', 'other'}.
    Code reads the numbers (nothing dropped); AI only helps match unfamiliar names."""
    sheet = storage.parse_stock_sheet(text)
    rows = [(t, None, q) for t, q in sheet["items"]]  # (typed, AI's guess, qty)
    other = [l for l in sheet["other"] if re.search(r"[a-z]", l, re.I)]
    if other:  # lines code can't read ('FC milk eighty-eight lol') → AI reads only those
        prompt = (
            "Café staff typed stock counts. Extract each item and its counted quantity "
            "(numbers may be in words; 'habis', 'out', 'OOS', 'nil' mean 0). Skip lines that are not counts "
            "(headers, dates, names of who checked). For 'item', use the EXACT name from this list when it "
            f"clearly means that item; otherwise repeat what they typed.\nKnown items: {json.dumps(known_items, ensure_ascii=False)}\n\n"
            "Lines:\n" + "\n".join(other)
        )
        try:
            data, _ = await _ask_json(prompt, STOCK_SPEC, groq_shape='{"updates":[{"typed","item","qty"}]}')
            for u in data.get("updates", []):
                qty = storage.to_number(u.get("qty"))
                if qty is not None and (u.get("typed") or u.get("item")):
                    rows.append((storage.clean_name(u.get("typed") or u.get("item")), u.get("item"), qty))
        except AIUnavailable:
            pass
    found = [storage.match_name(t, known_items) or
             (storage.match_name(guess, known_items, cutoff=0.95) if guess else None) for t, guess, _ in rows]
    ai_matches = await match_items([t for (t, _, _), m in zip(rows, found) if not m], known_items)
    updates = []
    for (typed, _, qty), hit in zip(rows, found):
        item = hit or ai_matches.get(typed)
        updates.append({"typed": typed, "item": item or typed, "qty": qty, "new": not item})
    blanks = [storage.match_name(b, known_items) or ai_matches.get(b) or b for b in sheet["blanks"]]
    return {"updates": updates, "blanks": blanks, "invalid": sheet["invalid"], "other": sheet["other"]}


async def parse_stock(text: str, known_items: list[str]) -> list[dict]:
    """Compatibility: just the updates."""
    return [{k: u[k] for k in ("typed", "item", "qty")} for u in (await read_stock(text, known_items))["updates"]]


# ─── Understanding replies like a human ───────────────────

REPLY_SPEC = {
    "action": ("enum", ["edit", "save", "cancel", "unclear"]),
    "edits?": [{"op": ("enum", ["item", "set", "delete", "add"]), "n?": "int",
                "field?": ("enum", ["qty", "unit_price", "line_total", "name", "category", "date", "supplier",
                                   "total", "discount", "rounding", "tax", "payment", "paid_by"]),
                "value?": "str", "name?": "str", "qty?": "num", "unit_price?": "num"}],
}
_NUMERIC = {"qty", "unit_price", "line_total", "total", "discount", "rounding", "tax"}


def reply_to_edits(data: dict) -> tuple[str, list[dict]]:
    """Turn the AI's reading of a reply into storage.apply_edits() operations."""
    action = data.get("action", "unclear")
    edits = []
    for e in data.get("edits") or []:
        op, field, value = e.get("op"), e.get("field"), e.get("value")
        if op == "delete" and e.get("n"):
            edits.append({"op": "delete", "n": int(e["n"])})
        elif op == "add" and e.get("name"):
            edits.append({"op": "add", "name": storage.clean_name(e["name"]), "qty": float(e.get("qty") or 1),
                          "unit_price": storage.to_number(e.get("unit_price")) or 0.0})
        elif op in ("item", "set") and field and value not in (None, ""):
            if field in _NUMERIC:
                value = storage.to_number(value)
                if value is None:
                    continue
                if field == "discount":
                    value = abs(value)
            elif field == "date":
                value = storage.parse_date(value)
                if not value:
                    continue
            elif field == "category":
                value = next((c for c in config.CATEGORIES if c.lower() == str(value).lower()), "Other")
            else:
                value = storage.clean_name(value)
            if op == "item" and e.get("n"):
                edits.append({"op": "item", "n": int(e["n"]), "field": field, "value": value})
            elif op == "set" and field not in ("qty", "unit_price", "line_total", "name", "category"):
                edits.append({"op": "set", "field": field, "value": value})
    return action, edits


async def understand_receipt_reply(r: dict, text: str) -> tuple[str, list[dict]]:
    """A free-form reply to a receipt summary → ('edit'|'save'|'cancel'|'unclear', edits)."""
    numbered = [{"n": i, **{k: it[k] for k in ("name", "qty", "unit_price", "line_total", "category")}}
                for i, it in enumerate(r["items"], 1)]
    prompt = (
        "A café staff member replied to a receipt the bot read. Work out what they want.\n"
        "action: 'edit' (change something), 'save' (they confirm it's right), 'cancel' (don't save it / wrong photo), "
        "or 'unclear'.\nFor edits: op 'item' changes field of item number n; 'set' changes a receipt field "
        "(date YYYY-MM-DD, supplier, total, discount, payment, paid_by); 'delete' removes item n; 'add' adds an item.\n"
        f"Receipt: {json.dumps({**{k: r[k] for k in ('supplier', 'date', 'total', 'discount', 'paid_by', 'payment')}, 'items': numbered}, ensure_ascii=False)}\n"
        f"Reply: {text}"
    )
    try:
        data, _ = await _ask_json(prompt, REPLY_SPEC, groq_shape='{"action":"edit|save|cancel|unclear","edits":[...]}')
    except AIUnavailable:
        return "unclear", []
    return reply_to_edits(data)


EXPENSE_SPEC = {
    "bought": ("enum", ["yes", "no", "unclear"]),
    "supplier?": "str", "date?": "str", "paid_by?": "str", "payment_method?": "str",
    "items?": [{"name": "str", "qty?": "num", "line_total?": "num", "unit_price?": "num"}],
    "total?": "num",
}


async def read_expense(text: str, items: list[dict], sender: str) -> dict | None:
    """'yes RM15 FairPrice paid by me' for new items → a receipt dict ready to show, or None if 'no'."""
    prompt = (
        "Café staff just added new stock items. The bot asked whether they were bought and should be added to "
        "expenses. Read their reply.\n"
        "bought: 'no' if they say no/skip/not bought; 'yes' if they give purchase details or say yes; else 'unclear'.\n"
        "Fill what they said: supplier (shop), date (YYYY-MM-DD, day-first if written like 12/11), paid_by "
        f"('me'/'I' = {sender}), items with qty and price (line_total = price paid for that line), total.\n"
        f"Today is {storage.today()}. New items: {json.dumps(items, ensure_ascii=False)}\nReply: {text}"
    )
    data, _ = await _ask_json(prompt, EXPENSE_SPEC, groq_shape='{"bought","supplier","date","paid_by","items":[...],"total"}')
    if data.get("bought") == "no":
        return None
    if data.get("bought") != "yes":
        return {"unclear": True}
    lines = data.get("items") or []
    if not lines:  # only a total given: put it on the item(s)
        lines = [{"name": it["name"], "qty": it.get("qty") or 1} for it in items]
        if len(lines) == 1 and data.get("total") is not None:
            lines[0]["line_total"] = data["total"]
    for line in lines:
        line.setdefault("category", "Ingredients")
        if line.get("qty") in (None, 0):
            match = next((it for it in items if storage.name_key(it["name"]) == storage.name_key(line["name"])), None)
            line["qty"] = (match or {}).get("qty") or 1
    paid = data.get("paid_by") or sender
    if storage.name_key(paid) in ("me", "i", "myself", "saya"):
        paid = sender
    total = data.get("total")
    if total is None:
        total = sum(storage.to_number(l.get("line_total")) or 0 for l in lines)
    return {"supplier": data.get("supplier") or "Unknown shop", "date": data.get("date") or storage.today(),
            "items": lines, "total": total, "paid_by": paid, "payment_method": data.get("payment_method") or "unknown"}


async def answer(question: str, context: str, money_allowed: bool, history: str = "") -> str:
    """Answer a question using only the Sheet data we pass in, plus this chat's recent messages."""
    rules = ("You may discuss sales, spending and money." if money_allowed else
             "You must NOT reveal or estimate any money figures (sales, spending, costs, profit). "
             "If asked, say politely that money questions are for the owners.")
    prompt = (
        f"You are the helpful assistant of {config.CAFE_NAME}, a bingsu café in Melaka. "
        "Answer staff/owner questions in a short, friendly reply (one to a few sentences or a short list), "
        "using ONLY the data below. "
        "If the data doesn't contain the answer, say so in one line. "
        f"Amounts are in RM. Today is {storage.today()}. {rules}\n"
        "Reply in the same language as the question (English, Malay or Chinese).\n\n"
        "Use the recent conversation to understand follow-ups (e.g. 'what about Friday?', 'and him?'), "
        "but trust the DATA over anything said in chat (old stock numbers in chat may be out of date).\n"
        "HONESTY RULES: You cannot save, change or record anything yourself. Never say something was saved, "
        "updated or added. If someone wants to record something, tell them how: send the receipt photo, "
        "type 'stock milk 120', or 'buy ice'. Never invent where something is stored; if unsure say "
        "'check the Google Sheet'. If the message is just 'ok', 'yes', 'no' or similar with nothing in the "
        "recent conversation it could answer, ask them to say a bit more instead of guessing.\n\n"
        f"=== DATA ===\n{context}\n\n"
        f"=== RECENT CONVERSATION IN THIS CHAT (oldest first) ===\n{history or '(none)'}\n\n"
        f"=== QUESTION ===\n{question}"
    )
    data, _ = await _ask_json(prompt, {"answer": "str"}, groq_shape='{"answer": "..."}')
    return str(data.get("answer", "")).strip() or "Sorry, I couldn't work that out."
