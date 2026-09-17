"""
Sudu Café — SOP prompt builder (slim edition).

All SOP data (recipes, minimums, checklists, inspection) lives in Google Sheets
and is the ONLY source of truth.

This module produces a SLIM SOP block for the AI system prompt: only stock
minimums are injected (small, needed on every message for low-stock checks).
Everything else — recipes, checklists, inspection — is fetched on demand by
the AI via the `read_tab` action, so answers come from a fresh live read
instead of a stale/confusing injected copy.
"""

# ═══════════════════════════════════════════════════════════
#  BUILD SOP TEXT FOR AI SYSTEM PROMPT (SLIM)
# ═══════════════════════════════════════════════════════════

def build_sop_prompt(bingsu_recipes=None, foam_recipes=None, topping_recipes=None,
                     drinks_recipes=None, stock_minimums=None, ops_checklists=None,
                     inspection_checklist=None) -> str:
    """Build a slim SOP block. Injects: stock minimums + a NAME directory
    (which recipes are bingsu bases, which are drinks, etc.) so the AI can
    resolve casual references like "gula melaka matcha" (drink) or "matcha
    2L" (bingsu). Full recipe DETAILS are fetched via read_tab on demand
    (see the RECIPES / CHECKLISTS rule in the system prompt)."""
    lines = []
    lines.append("=" * 50)
    lines.append("SUDU CAFE — LIVE DATA (from Google Sheet)")
    lines.append("=" * 50)

    # ─── Stock Minimums (only stock data section injected every message) ───
    if stock_minimums:
        lines.append("\nSTOCK MINIMUMS (alert if below):")
        for item, info in stock_minimums.items():
            unit = info.get("unit", "")
            loc = info.get("location", "")
            extra = f" ({unit})" if unit else ""
            loc_str = f" [{loc}]" if loc else ""
            lines.append(f"  {item}: min {info['min']}{extra}{loc_str}")

    # ─── RECIPE NAME DIRECTORY (names only, no ingredients) ───
    # Cheap ~500 chars. Lets the AI know which name maps to which type BEFORE
    # asking clarifying questions. Solves "gula melaka matcha 2L bingsu" style
    # confusion without dumping full recipes.
    if bingsu_recipes or drinks_recipes or foam_recipes or topping_recipes:
        lines.append("")
        lines.append("RECIPE DIRECTORY (names only — use read_tab for full details):")
        if bingsu_recipes:
            names = ", ".join(bingsu_recipes.keys())
            lines.append(f"  Bingsu Bases (scaled by batch, read_tab \"Bingsu Recipes\"): {names}")
        if drinks_recipes:
            all_drinks = []
            for category, drinks in drinks_recipes.items():
                all_drinks.extend(drinks.keys())
            if all_drinks:
                lines.append(f"  Drinks (per cup, read_tab \"Other Recipes\"): {', '.join(all_drinks)}")
        if foam_recipes:
            lines.append(f"  Foams (read_tab \"Other Recipes\"): {', '.join(foam_recipes.keys())}")
        if topping_recipes:
            lines.append(f"  Toppings (read_tab \"Other Recipes\"): {', '.join(topping_recipes.keys())}")
        lines.append("")
        lines.append("If a user's request matches names across multiple categories (e.g. \"matcha\" → bingsu Matcha + drinks Matcha Latte / Strawberry Matcha / Gula Melaka Matcha), ASK which one before read_tab.")

    # ─── Fallback read_tab pointers for non-recipe SOP ───
    lines.append("")
    lines.append("Other SOP (fetch via read_tab):")
    lines.append("  Opening / 6pm / Closing checklists  →  read_tab \"Checklists\"")
    lines.append("  Inspection items                    →  read_tab \"Inspection\"")

    return "\n".join(lines)
