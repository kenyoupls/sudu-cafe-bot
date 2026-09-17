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
    """Build a slim SOP block. Only stock minimums are injected. Recipes,
    checklists, and inspection items are pointed to via a read_tab directory
    so the AI fetches them live when asked (see the RECIPES / CHECKLISTS
    rule in the system prompt)."""
    # Ignore the recipe / checklist / inspection kwargs — kept in signature
    # so the caller (refresh_sop_prompt) doesn't need changing.
    _ = (bingsu_recipes, foam_recipes, topping_recipes, drinks_recipes,
         ops_checklists, inspection_checklist)

    lines = []
    lines.append("=" * 50)
    lines.append("SUDU CAFE — LIVE DATA (from Google Sheet)")
    lines.append("=" * 50)

    # ─── Stock Minimums (only section injected every message) ───
    if stock_minimums:
        lines.append("\nSTOCK MINIMUMS (alert if below):")
        for item, info in stock_minimums.items():
            unit = info.get("unit", "")
            loc = info.get("location", "")
            extra = f" ({unit})" if unit else ""
            loc_str = f" [{loc}]" if loc else ""
            lines.append(f"  {item}: min {info['min']}{extra}{loc_str}")

    # ─── read_tab directory (everything else lives here) ───
    lines.append("")
    lines.append("FETCH LIVE via read_tab (do NOT answer these from memory):")
    lines.append("  Bingsu bases (scaled by batch: 100ml/1L/2L/3L/4L)  →  read_tab \"Bingsu Recipes\"")
    lines.append("  Drinks (per cup, don't scale) / Foam / Topping     →  read_tab \"Other Recipes\"")
    lines.append("  Opening / 6pm / Closing checklists                 →  read_tab \"Checklists\"")
    lines.append("  Inspection items                                    →  read_tab \"Inspection\"")

    return "\n".join(lines)
