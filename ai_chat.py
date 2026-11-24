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


async def parse_stock(text: str, known_items: list[str]) -> list[dict]:
    """'full cream 120, low fat 139, cond milk 17' → [{'typed','item','qty'}].
    item is one of known_items when it clearly matches, else the typed name."""
    quick = storage.parse_stock_lines(text)
    matched = [(t, storage.match_name(t, known_items), q) for t, q in quick]
    if quick and all(m for _, m, _ in matched):
        return [{"typed": t, "item": m, "qty": q} for t, m, q in matched]
    prompt = (
        "Staff at a café typed a stock count. Extract each item and its counted quantity.\n"
        "For 'item', use the EXACT name from this list when it clearly means that item; "
        "otherwise repeat what they typed.\n"
        f"Known items: {json.dumps(known_items, ensure_ascii=False)}\n\nMessage: {text}"
    )
    try:
        data, _ = await _ask_json(prompt, STOCK_SPEC, groq_shape='{"updates":[{"typed","item","qty"}]}')
    except AIUnavailable:
        return [{"typed": t, "item": m or t, "qty": q} for t, m, q in matched]
    out = []
    for u in data.get("updates", []):
        qty = storage.to_number(u.get("qty"))
        if qty is None:
            continue
        item = storage.match_name(u.get("item", ""), known_items) or storage.clean_name(u.get("typed") or u.get("item"))
        out.append({"typed": storage.clean_name(u.get("typed")), "item": item, "qty": qty})
    return out


async def answer(question: str, context: str, money_allowed: bool) -> str:
    """Answer a question using only the Sheet data we pass in."""
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
        f"=== DATA ===\n{context}\n\n=== QUESTION ===\n{question}"
    )
    data, _ = await _ask_json(prompt, {"answer": "str"}, groq_shape='{"answer": "..."}')
    return str(data.get("answer", "")).strip() or "Sorry, I couldn't work that out."
