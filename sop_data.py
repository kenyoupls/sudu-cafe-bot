"""
Sudu Café — SOP Data
All recipes, stock minimums, operations checklists, and inspection items.
Extracted from the official Sudu SOP document.
"""

# ═══════════════════════════════════════════════════════════
#  BINGSU BASE RECIPES (scaled by batch size)
# ═══════════════════════════════════════════════════════════

BINGSU_RECIPES = {
    "Cendol": {
        "100ml":  {"Full Cream Milk": "18.75g", "Low Fat Milk": "47.5g", "Coconut Milk": "25g", "Gula Melaka": "8.75g", "Condensed Milk": "6.25g"},
        "1000ml": {"Full Cream Milk": "187.5g", "Low Fat Milk": "475g", "Coconut Milk": "250g", "Gula Melaka": "87.5g", "Condensed Milk": "62.5g"},
        "2000ml": {"Full Cream Milk": "375g", "Low Fat Milk": "950g", "Coconut Milk": "500g", "Gula Melaka": "175g", "Condensed Milk": "125g"},
        "3000ml": {"Full Cream Milk": "562.5g", "Low Fat Milk": "1425g", "Coconut Milk": "750g", "Gula Melaka": "262.5g", "Condensed Milk": "187.5g"},
        "4000ml": {"Full Cream Milk": "750g", "Low Fat Milk": "1900g", "Coconut Milk": "1000g", "Gula Melaka": "350g", "Condensed Milk": "250g"},
    },
    "Mango": {
        "100ml":  {"Full Cream Milk": "65g", "Whipping Cream": "14g", "Mango Cordial": "12g", "Condensed Milk": "3g", "Lemon Juice": "0.5g"},
        "1000ml": {"Full Cream Milk": "650g", "Whipping Cream": "140g", "Mango Cordial": "120g", "Condensed Milk": "30g", "Lemon Juice": "5g"},
        "2000ml": {"Full Cream Milk": "1300g", "Whipping Cream": "280g", "Mango Cordial": "240g", "Condensed Milk": "60g", "Lemon Juice": "10g"},
        "3000ml": {"Full Cream Milk": "1950g", "Whipping Cream": "420g", "Mango Cordial": "360g", "Condensed Milk": "90g", "Lemon Juice": "15g"},
        "4000ml": {"Full Cream Milk": "2600g", "Whipping Cream": "560g", "Mango Cordial": "480g", "Condensed Milk": "120g", "Lemon Juice": "20g"},
    },
    "Milo": {
        "100ml":  {"Full Cream Milk": "65g", "Low Fat Milk": "20g", "Whipping Cream": "5g", "Milo Powder": "12g", "Condensed Milk": "5g"},
        "1000ml": {"Full Cream Milk": "650g", "Low Fat Milk": "200g", "Whipping Cream": "50g", "Milo Powder": "120g", "Condensed Milk": "50g"},
        "2000ml": {"Full Cream Milk": "1300g", "Low Fat Milk": "400g", "Whipping Cream": "100g", "Milo Powder": "240g", "Condensed Milk": "100g"},
        "3000ml": {"Full Cream Milk": "1950g", "Low Fat Milk": "600g", "Whipping Cream": "150g", "Milo Powder": "360g", "Condensed Milk": "150g"},
        "4000ml": {"Full Cream Milk": "2600g", "Low Fat Milk": "800g", "Whipping Cream": "200g", "Milo Powder": "480g", "Condensed Milk": "200g"},
    },
    "Yakult": {
        "100ml":  {"Full Cream Milk": "15g", "Whipping Cream": "5g", "Yakult": "65.5g", "Condensed Milk": "3g", "Lemon Juice": "2g"},
        "1000ml": {"Full Cream Milk": "150g", "Whipping Cream": "50g", "Yakult": "655g", "Condensed Milk": "30g", "Lemon Juice": "20g"},
        "2000ml": {"Full Cream Milk": "300g", "Whipping Cream": "100g", "Yakult": "131g", "Condensed Milk": "60g", "Lemon Juice": "40g"},
        "3000ml": {"Full Cream Milk": "450g", "Whipping Cream": "150g", "Yakult": "1965g", "Condensed Milk": "90g", "Lemon Juice": "60g"},
        "4000ml": {"Full Cream Milk": "600g", "Whipping Cream": "200g", "Yakult": "2620g", "Condensed Milk": "120g", "Lemon Juice": "80g"},
    },
    "Oolong": {
        "100ml":  {"Low Fat Milk": "75g", "Whipping Cream": "12g", "Tea Powder": "3.5g", "Condensed Milk": "8g"},
        "1000ml": {"Low Fat Milk": "750g", "Whipping Cream": "120g", "Tea Powder": "35g", "Condensed Milk": "80g"},
        "2000ml": {"Low Fat Milk": "1500g", "Whipping Cream": "240g", "Tea Powder": "70g", "Condensed Milk": "160g"},
        "3000ml": {"Low Fat Milk": "2250g", "Whipping Cream": "360g", "Tea Powder": "105g", "Condensed Milk": "240g"},
        "4000ml": {"Low Fat Milk": "3000g", "Whipping Cream": "480g", "Tea Powder": "140g", "Condensed Milk": "320g"},
    },
    "Matcha": {
        "100ml":  {"Low Fat Milk": "75g", "Whipping Cream": "12g", "Matcha Powder": "3g", "Condensed Milk": "8g"},
        "1000ml": {"Low Fat Milk": "750g", "Whipping Cream": "120g", "Matcha Powder": "30g", "Condensed Milk": "80g"},
        "2000ml": {"Low Fat Milk": "1500g", "Whipping Cream": "240g", "Matcha Powder": "60g", "Condensed Milk": "160g"},
        "3000ml": {"Low Fat Milk": "2250g", "Whipping Cream": "360g", "Matcha Powder": "90g", "Condensed Milk": "240g"},
        "4000ml": {"Low Fat Milk": "3000g", "Whipping Cream": "480g", "Matcha Powder": "120g", "Condensed Milk": "320g"},
    },
}

# Note: Oolong also uses dried flower as garnish (no specific weight)

# ═══════════════════════════════════════════════════════════
#  FOAM RECIPES
# ═══════════════════════════════════════════════════════════

FOAM_RECIPES = {
    "Cheese Foam": {
        "ingredients": {"Whipping Cream": "300g", "Milk": "75g", "Condensed Milk": "10g", "Salt": "4.5g"},
        "method": "Whip all together until thick and creamy."
    },
    "Milo Foam": {
        "ingredients": {"Whipping Cream": "200g", "Low Fat Milk": "150g", "Milo Powder": "130g"},
        "method": "Whip cream till thick. Add low fat milk and Milo powder. Then whip to mix."
    },
    "Yoghurt Drizzle": {
        "ingredients": {"Yoghurt": "mix with low fat milk"},
        "method": "Whisk yoghurt with low fat milk till flowy."
    },
}

# ═══════════════════════════════════════════════════════════
#  TOPPING PREP RECIPES
# ═══════════════════════════════════════════════════════════

TOPPING_RECIPES = {
    "Osmanthus Goji Konnyaku Jelly": {
        "ingredients": "1 pack konnyaku jelly powder, 1250ml water, 40g sugar, 3g dried osmanthus flowers, 3-4 tsp goji berries (soaked)",
        "method": [
            "Soak goji berries in warm water for 5-10 minutes, then drain",
            "Pour 1250ml water into pot, add jelly powder slowly while stirring",
            "Heat and bring to boil, keep stirring to avoid lumps",
            "Lower heat and continue stirring for about 3 minutes",
            "Turn off heat. Add sugar (40g), osmanthus (3g), and goji berries",
            "Stir until dissolved and evenly mixed",
            "Pour into moulds. Let cool, then refrigerate at least 4 hours (best overnight)",
        ]
    },
    "Brown Boba": {
        "method": [
            "Boil water till boiling (2000)",
            "Put half cup of boba in",
            "Stir straight away",
            "Once all floats, change to medium heat 1400",
            "Wait for 20 minutes and stir from time to time",
            "Filter it and rinse with cold water",
            "Fill up container with 3 scoops of sugar and hot water, stir till melt",
            "Add boba into sugar water container",
        ]
    },
    "Taro Balls": {
        "method": [
            "Boil water till boiling",
            "Put 4 sets of taro balls into it (1 set is 2 of each colour)",
            "Stir to prevent sticking",
            "Boil till float",
            "After float, continue boiling for 8 minutes",
            "Fill up container with 3 scoops of sugar and hot water, stir till melt",
            "Add cooked taro balls into sugar water container",
        ]
    },
}

# ═══════════════════════════════════════════════════════════
#  DRINKS RECIPES
# ═══════════════════════════════════════════════════════════

DRINKS_RECIPES = {
    "Fizz": {
        "Honey Lemon": {
            "ingredients": {"Honey Lemon": "2.5 scoops (30g)", "Soda + Ice": "100%"},
            "method": "Swirl honey lemon around cup. Ice and soda."
        },
        "Orange Lychee": {
            "ingredients": {"Orange Syrup": "20g", "Whipping Cream": "30g", "Milk": "20g", "Lychee Syrup": "2 pumps (20g)", "Water + Ice": "80% fill"},
            "method": "(Separate) Whip everything together. Mix to make a drink. Pour orange foam on top."
        },
        "Blackcurrant Lychee": {
            "ingredients": {"Blackcurrant Syrup": "1 pump (10g)", "Lychee Syrup": "1 pump (10g)", "Soda + Ice": "100%"},
            "method": "Mix syrup and swirl around cup. Ice and soda."
        },
        "Passion Fizz": {
            "ingredients": {"Passion": "2.5 scoops (30g)", "Passion Juice": "15g", "Soda + Ice": "100%"},
            "method": "Swirl passion around cup. Ice and soda."
        },
    },
    "Caffeine": {
        "Long Black": {
            "ingredients": {"Nescafe Classic": "6g", "Water + Ice": "100%"},
            "method": "Dissolve coffee in little hot water. Add water and ice."
        },
        "Ice Coffee Latte": {
            "ingredients": {"Moconna": "6g", "Milk": "40g", "Ice": "50%", "Sugar Syrup": "1 pump (10g)"},
            "method": "Dissolve coffee in little hot water with sugar syrup. Add milk and ice."
        },
        "Matcha Latte": {
            "ingredients": {"Matcha Powder": "6g", "Sugar Syrup": "1 pump (10g)", "Milk": "50%", "Ice": "50%"},
            "method": "(Separate) Whisk matcha with little hot water and sugar syrup till nice and silky. Milk and ice into cup. Add matcha on top."
        },
    },
    "Specialty": {
        "Strawberry Matcha Latte": {
            "ingredients": {"Matcha Powder": "6g", "Strawberry Syrup": "20g", "Milk": "50%", "Ice": "50%"},
            "method": "(Separate) Whisk matcha with little hot water till nice and silky. Swirl strawberry syrup around cup. Milk + ice. Matcha on top."
        },
        "Himalayan Lime": {
            "ingredients": {"Lemon": "3 slices", "Salt": "3g", "Ice": "50%", "Soda": "100%"},
            "method": "Add lemon, salt and a little water into shaker and press them till juices come out. Pour into cup and add soda."
        },
        "Gula Melaka Matcha": {
            "ingredients": {"Matcha": "4g", "Gula Melaka": "20g", "Milk": "60g", "Ice": "50%"},
            "method": "(Separate) Whisk matcha with little hot water. Gula melaka in cup and swirl around. Add milk and ice. Matcha on top."
        },
    },
    "Cake Drink": {
        "Ice Lemon Drink": {
            "ingredients": {"Lemon": "1 slice", "Sugar Syrup": "5 pumps (50g)", "Lime Juice": "20ml (20g)", "Ice and Water": "400ml"},
            "method": "Put lemon, sugar water, lime juice in shaker and press it. Add ice and water in shaker. Shake it."
        },
    },
}


# ═══════════════════════════════════════════════════════════
#  STOCK MINIMUMS (triggers low stock alert)
# ═══════════════════════════════════════════════════════════

# Format: item_name -> {"min": minimum_qty, "unit": unit_description, "location": where}
# The "min" is the number below which the alert triggers.
# When staff reports a number, we compare against this.

STOCK_MINIMUMS = {
    # ─── Ingredients (Outside Counter minimums) ───
    "Full Cream Milk":        {"min": 20, "location": "Right bench"},
    "Low Fat Milk":           {"min": 20, "location": "Right bench"},
    "Condensed Milk":         {"min": 4, "location": ""},
    "Coconut Milk":           {"min": 5, "location": ""},
    "Gula Melaka Liquid":     {"min": 4, "location": ""},
    "Red Bean":               {"min": 6, "location": ""},
    "Matcha Powder":          {"min": 2, "unit": "500g packs", "location": ""},
    "Osmanthus Oolong Powder": {"min": 3, "unit": "1kg packs", "location": ""},
    "Osmanthus Flower":       {"min": 4, "location": ""},
    "Konnyaku Powder":        {"min": 4, "location": ""},
    "Goji Berry":             {"min": 4, "location": ""},
    "Puffed Rice":            {"min": 1, "location": ""},
    "Milo Powder":            {"min": 3, "location": ""},
    "Milo Krunch":            {"min": 10, "location": ""},
    "Mini Marshmallow":       {"min": 3, "location": ""},
    "Hup Seng Biscuits":      {"min": 5, "location": ""},
    "Lotus Biscoff Crumbs":   {"min": 5, "location": ""},
    "Freeze Dried Strawberries": {"min": 1, "location": ""},
    "Strawberry Sauce":       {"min": 1, "location": ""},
    "Lychee Nata De Coco":    {"min": 6, "location": ""},
    "Lychee Popping Boba":    {"min": 3, "unit": "3kg big tub", "location": ""},
    "Mango Cordial":          {"min": 5, "location": ""},
    "Mango Nata De Coco":     {"min": 6, "location": ""},
    "Brown Boba (Jelly)":     {"min": 4, "location": ""},
    "Blackcurrant Cordial":   {"min": 2, "location": ""},
    "Jasmine Tea Bag":        {"min": 15, "unit": "sachets each", "location": ""},
    "Honey":                  {"min": 1, "location": ""},
    "Moccona Coffee":         {"min": 1, "location": ""},
    "Sin Sing Coffee":        {"min": 1, "location": ""},
    "Salt":                   {"min": 1, "location": ""},
    "Lychee":                 {"min": 6, "location": ""},
    "Almond Flakes":          {"min": 2, "location": ""},
    "Earl Grey Powder":       {"min": 2, "location": ""},
    "Sparkling Gas":          {"min": 4, "location": "Left bench"},
    "Passion Fruit Drink":    {"min": 8, "location": ""},
    "Honey Lemon Drink":      {"min": 8, "location": ""},
    "Brown Boba (Cook)":      {"min": 6, "location": ""},
    "Sugar":                  {"min": 6, "location": ""},
    "Lime Juice":             {"min": 4, "location": ""},
    "Whipping Cream":         {"min": 8, "location": "Fridge"},
    "Yakult":                 {"min": 100, "unit": "pieces (20 rows)", "location": "Fridge"},
    "Mango Fruit":            {"min": 10, "location": "Fridge"},
    "Yoghurt":                {"min": 2, "location": "Fridge"},
    "Vanilla Ice Cream":      {"min": 6, "location": "Fridge"},
    "Cendol":                 {"min": 2, "location": "Fridge"},
    "Taro Balls":             {"min": 1, "unit": "half pack", "location": "Fridge"},
    # ─── Supplies (Outside Counter minimums) ───
    "Customer Square Tissues": {"min": 15, "unit": "small packs + 2 big packs", "location": "Left bench / In counter"},
    "Staff Tissues Paper":    {"min": 1, "location": ""},
    "Sink Hand Tissue":       {"min": 20, "unit": "small packs", "location": ""},
    "Takeaway Plastic Cup":   {"min": 4, "unit": "packs", "location": ""},
    "Takeaway Plastic Cup Cover": {"min": 2, "unit": "packs", "location": ""},
    "Takeaway Hot Tea Cup":   {"min": 1, "location": ""},
    "Takeaway Hot Tea Cup Cover": {"min": 1, "location": ""},
    "Thin Straw":             {"min": 6, "unit": "packs", "location": ""},
    "Thick Straw":            {"min": 2, "unit": "packs", "location": ""},
    "Drinks Takeaway Plastic Bag": {"min": 2, "unit": "packs", "location": ""},
    "Kitchen Bin Bags":       {"min": 5, "location": ""},
    "Washing Liquid":         {"min": 3, "location": ""},
    "Dish Sponge":            {"min": 4, "location": ""},
    "Cloths":                 {"min": 5, "location": ""},
    "Glove":                  {"min": 2, "unit": "boxes", "location": ""},
    "Face Mask":              {"min": 8, "location": ""},
    "Hand Wash":              {"min": 4, "location": ""},
    "Receipt Printer Roll":   {"min": 15, "location": ""},
    "Card Machine Roll":      {"min": 1, "location": ""},
    "Takeaway Bowl":          {"min": 50, "location": ""},
    "Takeaway Bowl Cover":    {"min": 50, "location": ""},
    "Plastic Spoons":         {"min": 100, "location": ""},
    "Takeaway Plastic Bag":   {"min": 50, "location": ""},
    "Toilet Bin Bags":        {"min": 2, "location": "Back of shop"},
    "Toilet Roll (big)":      {"min": 4, "location": ""},
    "Toilet Roll (small)":   {"min": 1, "location": ""},
    "Toilet/Floor Cleaner":   {"min": 2, "location": ""},
    "Bleach":                 {"min": 2, "location": ""},
}


# ═══════════════════════════════════════════════════════════
#  OPERATIONS CHECKLISTS
# ═══════════════════════════════════════════════════════════

OPS_CHECKLISTS = {
    "opening": [
        "Cook boba and taro",
        "Fill up water boiler",
        "Turn on perfume diffuser",
        "Self service section tissues",
        "Turn on music",
        "Taste all bingsu base, toppings and drizzle",
        "Wipe down the long bench",
        "Turn on bench LED lights",
        "Turn on outside TVs",
        "Turn on menu TVs",
        "Change lighting",
        "Unlock toilet",
        "Toilet light is on and clean",
        "Put camping chairs and tables out",
        "Wipe down door and glass",
    ],
    "6pm": [
        "Turn on outside light",
        "Turn on signage light",
        "Put camping chairs and tables out",
    ],
    "closing": [
        "Wash drinks section",
        "Clear ice box",
        "Keep leftover boba and taro in fridge",
        "Wash toppings cutleries",
        "Turn off bench LED lights",
        "Lock toilet",
        "Keep camping chairs in",
        "Text if need any ingredients buying",
        "Turn off perfume diffuser",
        "Throw rubbish",
        "Lock cash and iPad",
        "Wash Toilet (make sure toilet floor clean, no tissues or other things)",
        "Clear Toilet Bins",
        "Wash toilet Sink",
        "Vacuum and Mop floor",
        "Check toilet roll and toilet sink tissue",
        "Sales close up",
        "Post daily sales into groupchat",
        "Separate RM200 and the rest keep in envelope to pass to owner next day",
    ],
    "weekly": [
        "Clean and wipe down TV",
        "Mop and vacuum outside",
        "Wash outside and inside filters",
        "Stock Check",
    ],
    "monthly": [
        "Clean grease trap",
    ],
}


# ═══════════════════════════════════════════════════════════
#  INSPECTION CHECKLIST
# ═══════════════════════════════════════════════════════════

INSPECTION_CHECKLIST = {
    "Front Outside": [
        "1.1 TVs wipe clean",
        "1.2 Plants wipe clean",
        "1.3 All TV working",
        "1.4 Floor clean",
        "1.5 Floor rail is clean",
        "1.6 Big Water Filter clean regularly",
        "1.7 Glass door and window clean",
    ],
    "Seating Area": [
        "2.1 Floor Clean (no bingsu or drinks stains, etc)",
        "2.2 Table Clean (no wet marks or fingerprint, side clean)",
        "2.3 Table leg (clean, no marks)",
        "2.4 Chairs clean and working (no screw poking out)",
        "2.5 Bench wipe clean (no dust or fingerprint)",
        "2.6 Counter carpet clean (no dirt)",
        "2.7 Menu TV no dust",
        "2.8 Bag in frame tidy and straight (clean)",
        "2.9 Wall maintain (no dust falling)",
        "2.10 Tables and chairs (straight and tidy)",
        "2.11 Storage under bench tidy and clean",
    ],
    "Kitchen Bar": [
        "3.1 Counter table (no dust, watermarks, wet)",
        "3.2 Snow machine (no watermarks, no dirt stuck)",
        "3.3 Snow machine accessories (clean inside out, not oily)",
        "3.4 Drinks section (not sticky, no ants)",
        "3.5 Sink (clean and tidy, no water marks)",
        "3.6 Sink hand wash sufficient",
        "3.7 Drinking Water Filter (clean)",
        "3.8 Grease Trap (clean)",
        "3.9 Sink tap (tighten)",
        "3.10 Microwave inside and out",
        "3.11 Table working top clean",
        "3.12 Toppings section (tidy and clean)",
        "3.13 Bowls, plates, cups, cutleries (clean, no watermarks, no dust, no rust)",
        "3.14 Racks on top (tidy and clean, no dust)",
        "3.15 Cleaning sponge and basket",
        "3.16 Cupboard inside tidy and clean",
        "3.17 Counter below tidy and clean",
        "3.18 Fridge (tidy and clean)",
        "3.19 Freezer (tidy, clean, no ice build-up)",
        "3.20 Rubbish bin inside and out (clean)",
        "3.21 Freezer and fridge temperature",
    ],
    "Back Area": [
        "4.1 Toilet Bowl (clean inside and out)",
        "4.2 Toilet floor (no tissues, clean)",
        "4.3 Toilet roll",
        "4.4 Toilet dustbin clear",
        "4.5 Toilet Wall clean",
        "4.6 Toilet drainage clear",
        "4.7 Sink Clean and tidy",
        "4.8 Sink filter fitted nicely",
        "4.9 Handsoap sufficient",
        "4.10 Under Sink storage (clean and tidy)",
        "4.11 Wall clean and tidy",
        "4.12 Back Area Floor Clean",
        "4.13 Cleaning Equipment area (tidy and clean)",
        "4.14 Above toilet storage (clean and tidy)",
        "4.15 Fire Extinguisher (clean, functional and unobstructed)",
        "4.16 Clear Sink rubbish bin",
        "4.17 Toilet bin clean outside and inside",
        "4.18 Sink bin clean outside and inside",
    ],
    "Back Outside": [
        "5.1 Table and chairs (clean and tidy)",
        "5.2 Floors (clean and tidy)",
        "5.3 Back TVs (clean and tidy)",
        "5.4 Sidewalk Board (clean)",
        "5.5 Menu Stand (clean)",
        "5.6 Outside plant tidy",
    ],
    "Staff Attire": [
        "6.1 Cap",
        "6.2 Apron",
        "6.3 Long Pants",
        "6.4 Nails",
        "6.5 Face Masks",
        "6.6 No Watches",
        "6.7 Covered up Shoes",
    ],
    "Targets": [
        "7.1 Punctuality",
        "7.2 Complaints",
        "7.3 Bingsu Base",
        "7.4 Toppings",
        "7.5 Stock Balance and expiry check",
        "7.6 Pest Control",
        "7.7 Music",
        "7.8 Lighting",
        "7.9 Content (20/month)",
        "7.10 KOL post (4/month)",
        "7.11 Reviews (40 a month)",
    ],
}


# ═══════════════════════════════════════════════════════════
#  BUILD SOP TEXT FOR AI SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════

def build_sop_prompt() -> str:
    """Build the full SOP knowledge block for the AI system prompt."""
    lines = []
    lines.append("=" * 50)
    lines.append("SUDU CAFE SOP — YOU KNOW ALL OF THIS")
    lines.append("=" * 50)

    # ─── Bingsu Recipes ───
    lines.append("\nBINGSU BASE RECIPES:")
    for flavor, sizes in BINGSU_RECIPES.items():
        lines.append(f"\n  {flavor} Bingsu Base:")
        for size, ingredients in sizes.items():
            ing_str = ", ".join(f"{k}: {v}" for k, v in ingredients.items())
            lines.append(f"    {size}: {ing_str}")

    # ─── Foam Recipes ───
    lines.append("\nFOAM RECIPES:")
    for name, data in FOAM_RECIPES.items():
        ing_str = ", ".join(f"{k}: {v}" for k, v in data["ingredients"].items())
        lines.append(f"  {name}: {ing_str}")
        lines.append(f"    Method: {data['method']}")

    # ─── Topping Prep ───
    lines.append("\nTOPPING PREP RECIPES:")
    for name, data in TOPPING_RECIPES.items():
        lines.append(f"\n  {name}:")
        if "ingredients" in data:
            lines.append(f"    Ingredients: {data['ingredients']}")
        for i, step in enumerate(data["method"], 1):
            lines.append(f"    {i}. {step}")

    # ─── Drinks ───
    lines.append("\nDRINKS RECIPES:")
    for category, drinks in DRINKS_RECIPES.items():
        lines.append(f"\n  [{category}]")
        for drink_name, data in drinks.items():
            ing_str = ", ".join(f"{k}: {v}" for k, v in data["ingredients"].items())
            lines.append(f"  {drink_name}: {ing_str}")
            lines.append(f"    Method: {data['method']}")

    # ─── Operations ───
    lines.append("\n\nOPERATIONS CHECKLISTS:")
    for checklist, items in OPS_CHECKLISTS.items():
        lines.append(f"\n  {checklist.upper()}:")
        for i, item in enumerate(items, 1):
            lines.append(f"    {i}. {item}")

    # ─── Stock Minimums ───
    lines.append("\n\nSTOCK MINIMUM LEVELS (alert if below):")
    for item, info in STOCK_MINIMUMS.items():
        unit = info.get("unit", "")
        loc = info.get("location", "")
        extra = f" ({unit})" if unit else ""
        loc_str = f" [{loc}]" if loc else ""
        lines.append(f"  {item}: min {info['min']}{extra}{loc_str}")

    # ─── Inspection ───
    lines.append("\n\nINSPECTION CHECKLIST:")
    for zone, items in INSPECTION_CHECKLIST.items():
        lines.append(f"\n  [{zone}]")
        for item in items:
            lines.append(f"    {item}")

    return "\n".join(lines)
