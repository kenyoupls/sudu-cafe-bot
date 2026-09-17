"""
Sudu Café — SOP prompt builder.

All SOP data (recipes, minimums, checklists, inspection) lives in Google Sheets
and is the ONLY source of truth. This module contains only the formatter that
turns whatever the sheet returned into a text block for the AI system prompt.
"""

# ═══════════════════════════════════════════════════════════
#  BUILD SOP TEXT FOR AI SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════

def build_sop_prompt(bingsu_recipes=None, foam_recipes=None, topping_recipes=None,
                     drinks_recipes=None, stock_minimums=None, ops_checklists=None,
                     inspection_checklist=None) -> str:
    """Build the full SOP knowledge block for the AI system prompt.

    All SOP data now lives in Google Sheets (private, editable) rather than
    hardcoded here. Callers should fetch the current data via
    SheetsSync.read_sop_from_sheets() and pass it in as keyword arguments.
    If nothing is passed, returns an empty string — no SOP data means no
    SOP block gets added to the system prompt."""
    if not any([bingsu_recipes, foam_recipes, topping_recipes, drinks_recipes,
                stock_minimums, ops_checklists, inspection_checklist]):
        return ""

    lines = []
    lines.append("=" * 50)
    lines.append("SUDU CAFE SOP — YOU KNOW ALL OF THIS")
    lines.append("=" * 50)

    # ─── Bingsu Recipes ───
    if bingsu_recipes:
        lines.append("\nBINGSU BASE RECIPES:")
        for flavor, sizes in bingsu_recipes.items():
            lines.append(f"\n  {flavor} Bingsu Base:")
            for size, ingredients in sizes.items():
                ing_str = ", ".join(f"{k}: {v}" for k, v in ingredients.items())
                lines.append(f"    {size}: {ing_str}")

    # ─── Foam Recipes ───
    if foam_recipes:
        lines.append("\nFOAM RECIPES:")
        for name, data in foam_recipes.items():
            ing_str = ", ".join(f"{k}: {v}" for k, v in data.get("ingredients", {}).items())
            lines.append(f"  {name}: {ing_str}")
            lines.append(f"    Method: {data.get('method', '')}")

    # ─── Topping Prep ───
    if topping_recipes:
        lines.append("\nTOPPING PREP RECIPES:")
        for name, data in topping_recipes.items():
            lines.append(f"\n  {name}:")
            if "ingredients" in data:
                lines.append(f"    Ingredients: {data['ingredients']}")
            for i, step in enumerate(data.get("method", []), 1):
                lines.append(f"    {i}. {step}")

    # ─── Drinks ───
    if drinks_recipes:
        lines.append("\nDRINKS RECIPES:")
        for category, drinks in drinks_recipes.items():
            lines.append(f"\n  [{category}]")
            for drink_name, data in drinks.items():
                ing_str = ", ".join(f"{k}: {v}" for k, v in data.get("ingredients", {}).items())
                lines.append(f"  {drink_name}: {ing_str}")
                lines.append(f"    Method: {data.get('method', '')}")

    # ─── Operations ───
    if ops_checklists:
        lines.append("\n\nOPERATIONS CHECKLISTS:")
        for checklist, items in ops_checklists.items():
            lines.append(f"\n  {checklist.upper()}:")
            for i, item in enumerate(items, 1):
                lines.append(f"    {i}. {item}")

    # ─── Stock Minimums ───
    if stock_minimums:
        lines.append("\n\nSTOCK MINIMUM LEVELS (alert if below):")
        for item, info in stock_minimums.items():
            unit = info.get("unit", "")
            loc = info.get("location", "")
            extra = f" ({unit})" if unit else ""
            loc_str = f" [{loc}]" if loc else ""
            lines.append(f"  {item}: min {info['min']}{extra}{loc_str}")

    # ─── Inspection ───
    if inspection_checklist:
        lines.append("\n\nINSPECTION CHECKLIST:")
        for zone, items in inspection_checklist.items():
            lines.append(f"\n  [{zone}]")
            for item in items:
                lines.append(f"    {item}")

    return "\n".join(lines)
