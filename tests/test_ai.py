"""AI layer with the network faked out."""
import asyncio

import pytest

import ai_chat as ai


def test_schemas_build():
    for spec in (ai.PHOTO_SPEC, ai.RECEIPT_SPEC, ai.SALES_SPEC, ai.STOCK_SPEC):
        schema = ai._schema(spec)
        assert schema.type.name == "OBJECT"
    receipt = ai._schema(ai.RECEIPT_SPEC)
    assert "discount" not in receipt.required and "total" in receipt.required


def test_model_chain_falls_through(monkeypatch):
    calls = []

    def fake_call(model, parts, spec):
        calls.append(model)
        if model != "m3":
            raise RuntimeError("503 UNAVAILABLE")
        return {"kind": "other"}

    monkeypatch.setattr(ai, "_gemini", lambda: object())
    monkeypatch.setattr(ai, "_call_gemini", fake_call)
    monkeypatch.setattr(ai.config, "AI_MODELS", ["m1", "m2", "m3"])
    monkeypatch.setattr(ai.config, "GROQ_API_KEY", "")
    pytest.importorskip("google.genai")
    result = asyncio.run(ai.read_photo(b"img", ""))
    assert calls == ["m1", "m2", "m3"] and result["kind"] == "other" and result["model"] == "m3"


def test_all_models_fail(monkeypatch):
    monkeypatch.setattr(ai, "_gemini", lambda: object())
    monkeypatch.setattr(ai, "_call_gemini", lambda *a: (_ for _ in ()).throw(RuntimeError("429")))
    monkeypatch.setattr(ai.config, "AI_MODELS", ["m1"])
    monkeypatch.setattr(ai.config, "GROQ_API_KEY", "")
    with pytest.raises(ai.AIUnavailable):
        asyncio.run(ai.answer("q", "ctx", False))


def test_parse_stock_simple_lines_need_no_ai(monkeypatch):
    async def no_ai(*a, **k):
        raise AssertionError("AI should not be called")
    monkeypatch.setattr(ai, "_ask_json", no_ai)
    out = asyncio.run(ai.parse_stock("full cream milk 120, condensed milk: 17",
                                     ["Full Cream Milk", "Condensed Milk"]))
    assert out == [{"typed": "full cream milk", "item": "Full Cream Milk", "qty": 120.0},
                   {"typed": "condensed milk", "item": "Condensed Milk", "qty": 17.0}]


def test_parse_stock_uses_ai_for_messy_text(monkeypatch):
    async def fake(prompt, spec, **k):
        return {"updates": [{"typed": "FC milk", "item": "Full Cream Milk", "qty": 88},
                            {"typed": "durian", "item": "durian", "qty": 2}]}, "m"
    monkeypatch.setattr(ai, "_ask_json", fake)
    out = asyncio.run(ai.parse_stock("FC milk eighty-eight lol, durian x2", ["Full Cream Milk"]))
    assert out[0]["item"] == "Full Cream Milk" and out[1]["item"] == "durian"
