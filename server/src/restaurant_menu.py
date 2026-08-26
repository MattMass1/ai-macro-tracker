"""Curated restaurant nutrition panels from official US chain sources.

Only fixed menu items and explicitly described serving configurations belong
here.  Values are per ``serving_size`` and use grams for all macronutrients.
"""
from __future__ import annotations

import re
from typing import Any


def _item(name: str, aliases: list[str], calories: float, protein: float,
          carbs: float, fat: float, fiber: float, serving_size: str,
          source: str) -> dict[str, Any]:
    return {"name": name, "aliases": aliases, "calories": calories,
            "protein": protein, "carbs": carbs, "fat": fat, "fiber": fiber,
            "serving_size": serving_size, "source": source}


CHIPOTLE_SOURCE = "https://www.chipotle.com/nutrition-calculator"
CFA_SOURCE = "https://www.chick-fil-a.com/nutrition-allergens"
TACO_BELL_SOURCE = "https://www.tacobell.com/nutrition/info"
MCD_SOURCE = "https://www.mcdonalds.com/us/en-us/about-our-food/nutrition-calculator.html"
CANES_SOURCE = "https://www.raisingcanes.com/allergen-and-nutritional-information/"

# Chipotle entries are individual standard portions. Custom entrees are matched
# by their components instead of pretending that every "bowl" is identical.
RESTAURANT_MENU: dict[str, list[dict[str, Any]]] = {
    "chipotle": [
        # Fixed build: chicken, brown rice, black beans, roasted chili-corn
        # salsa, and cheese. Values are the sum of the official components.
        _item("Chicken Burrito Bowl", ["burrito bowl", "chicken bowl"], 710, 53, 75, 23.5, 12, "1 bowl: chicken, brown rice, black beans, corn salsa, cheese", CHIPOTLE_SOURCE),
        _item("Double Chicken Burrito Bowl", ["double chicken bowl", "double chicken burrito bowl"], 890, 85, 75, 30.5, 12, "1 bowl: double chicken, brown rice, black beans, corn salsa, cheese", CHIPOTLE_SOURCE),
        _item("Chicken Burrito", ["chicken burrito"], 1030, 61, 125, 32.5, 15, "1 burrito: chicken, brown rice, black beans, corn salsa, cheese", CHIPOTLE_SOURCE),
        _item("Adobo Chicken", ["chicken", "chicken protein cup"], 180, 32, 0, 7, 0, "4 oz", CHIPOTLE_SOURCE),
        _item("Double Adobo Chicken", ["double chicken", "double chicken protein cup"], 360, 64, 0, 14, 0, "8 oz", CHIPOTLE_SOURCE),
        _item("Honey Chicken", ["honey chipotle chicken", "chipotle honey chicken"], 210, 27, 16, 7, 1, "4 oz", CHIPOTLE_SOURCE),
        _item("Steak", [], 150, 21, 1, 6, 0, "4 oz", CHIPOTLE_SOURCE),
        _item("Barbacoa", [], 170, 24, 2, 7, 0, "4 oz", CHIPOTLE_SOURCE),
        _item("Carnitas", [], 210, 23, 0, 12, 0, "4 oz", CHIPOTLE_SOURCE),
        _item("Sofritas", [], 150, 8, 9, 10, 3, "4 oz", CHIPOTLE_SOURCE),
        _item("White Rice", ["cilantro lime white rice"], 210, 4, 40, 4, 1, "4 oz", CHIPOTLE_SOURCE),
        _item("Brown Rice", ["cilantro lime brown rice"], 210, 4, 36, 6, 2, "4 oz", CHIPOTLE_SOURCE),
        _item("Black Beans", [], 130, 8, 22, 1.5, 7, "4 oz", CHIPOTLE_SOURCE),
        _item("Pinto Beans", [], 130, 8, 21, 1, 8, "4 oz", CHIPOTLE_SOURCE),
        _item("Fajita Vegetables", ["fajita veggies"], 20, 1, 5, 0, 1, "3.5 oz", CHIPOTLE_SOURCE),
        _item("Fresh Tomato Salsa", ["mild salsa", "pico de gallo"], 25, 0, 4, 0, 1, "3.5 oz", CHIPOTLE_SOURCE),
        _item("Roasted Chili-Corn Salsa", ["corn salsa", "chili corn salsa"], 80, 3, 16, 1, 3, "3.5 oz", CHIPOTLE_SOURCE),
        _item("Tomatillo-Green Chili Salsa", ["green salsa"], 15, 0, 4, 0, 0, "2 oz", CHIPOTLE_SOURCE),
        _item("Tomatillo-Red Chili Salsa", ["red salsa", "hot salsa"], 30, 0, 4, 0, 0, "2 oz", CHIPOTLE_SOURCE),
        _item("Cheese", ["shredded cheese"], 110, 6, 1, 8, 0, "1 oz", CHIPOTLE_SOURCE),
        _item("Sour Cream", [], 110, 2, 2, 9, 0, "2 oz", CHIPOTLE_SOURCE),
        _item("Guacamole", ["guac"], 230, 2, 8, 22, 6, "4 oz", CHIPOTLE_SOURCE),
        _item("Romaine Lettuce", ["lettuce"], 5, 0, 1, 0, 1, "1 oz", CHIPOTLE_SOURCE),
        _item("Flour Tortilla", ["burrito tortilla"], 320, 8, 50, 9, 3, "1 tortilla", CHIPOTLE_SOURCE),
        _item("Crispy Corn Taco Shell", ["crispy taco shell"], 70, 1, 13, 2.5, 1, "1 shell", CHIPOTLE_SOURCE),
        _item("Soft Corn Tortilla", ["corn tortilla"], 70, 1, 15, 1, 1, "1 tortilla", CHIPOTLE_SOURCE),
        _item("Chips", ["tortilla chips"], 540, 7, 73, 25, 9, "4 oz bag", CHIPOTLE_SOURCE),
    ],
    "chick-fil-a": [
        _item("Egg White Grill", ["egg white grill sandwich"], 300, 27, 29, 8, 1, "1 sandwich", "https://www.chick-fil-a.com/menu/breakfast/egg-white-grill"),
        _item("Chick-n-Minis 4 Count", ["4ct chicken minis", "4 count chick n minis", "chicken minis 4ct"], 360, 20, 41, 13, 2, "4 minis", CFA_SOURCE),
        _item("Grilled Nuggets 8 Count", ["8ct grilled nuggets", "8 count grilled nuggets"], 130, 25, 1, 3, 0, "8 nuggets", CFA_SOURCE),
        _item("Grilled Nuggets 12 Count", ["12ct grilled nuggets", "12 count grilled nuggets"], 200, 38, 2, 4.5, 0, "12 nuggets", CFA_SOURCE),
        _item("Chick-fil-A Nuggets 8 Count", ["8ct nuggets", "8 count nuggets"], 250, 27, 11, 11, 0, "8 nuggets", CFA_SOURCE),
        _item("Chick-fil-A Nuggets 12 Count", ["12ct nuggets", "12 count nuggets"], 380, 40, 16, 17, 0, "12 nuggets", CFA_SOURCE),
        _item("Grilled Chicken Club Sandwich", ["grilled club", "grilled chicken club"], 520, 38, 45, 22, 3, "1 sandwich", CFA_SOURCE),
        _item("Grilled Chicken Sandwich", ["grilled sandwich"], 390, 28, 44, 12, 3, "1 sandwich", CFA_SOURCE),
        _item("Chick-fil-A Chicken Sandwich", ["original chicken sandwich", "cfa sandwich"], 420, 29, 41, 18, 1, "1 sandwich", CFA_SOURCE),
        _item("Spicy Chicken Sandwich", ["spicy sandwich"], 450, 28, 45, 19, 1, "1 sandwich", CFA_SOURCE),
        _item("Chicken Biscuit", ["chick fil a chicken biscuit"], 460, 19, 45, 23, 2, "1 biscuit", CFA_SOURCE),
        _item("Hash Browns", [], 270, 3, 23, 18, 3, "1 order", CFA_SOURCE),
        _item("Medium Waffle Potato Fries", ["medium fries", "waffle fries"], 420, 5, 45, 24, 5, "1 medium order", CFA_SOURCE),
        _item("Mac & Cheese Medium", ["medium mac and cheese", "mac n cheese"], 450, 20, 28, 29, 0, "1 medium order", CFA_SOURCE),
        _item("Honey Mustard Sauce", ["honey mustard sauce packet", "honey mustard packet"], 50, 0, 11, 0, 0, "1 packet", CFA_SOURCE),
        _item("Honey Roasted BBQ Sauce", ["honey bbq sauce", "honey roasted bbq packet"], 60, 0, 3, 5, 0, "1 packet", CFA_SOURCE),
        _item("Chick-fil-A Sauce", ["cfa sauce"], 140, 0, 7, 13, 0, "1 packet", CFA_SOURCE),
        _item("Zesty Buffalo Sauce", ["buffalo sauce"], 25, 0, 1, 2.5, 0, "1 packet", CFA_SOURCE),
        _item("Polynesian Sauce", [], 110, 0, 28, 0, 0, "1 packet", CFA_SOURCE),
        _item("Garden Herb Ranch Sauce", ["ranch sauce"], 140, 1, 1, 15, 0, "1 packet", CFA_SOURCE),
    ],
    "taco-bell": [
        _item("Crunchy Taco", [], 170, 8, 13, 10, 3, "1 taco", "https://www.tacobell.com/food/tacos/crunchy-taco"),
        _item("Crunchy Taco Supreme", ["supreme crunchy taco"], 190, 8, 15, 11, 3, "1 taco", TACO_BELL_SOURCE),
        _item("Soft Taco", [], 180, 9, 18, 9, 3, "1 taco", TACO_BELL_SOURCE),
        _item("Soft Taco Supreme", ["supreme soft taco"], 200, 9, 20, 10, 3, "1 taco", TACO_BELL_SOURCE),
        _item("Doritos Locos Taco", ["nacho cheese doritos locos taco", "dlt"], 170, 8, 13, 9, 3, "1 taco", TACO_BELL_SOURCE),
        _item("Burrito Supreme", [], 390, 16, 51, 14, 8, "1 burrito", TACO_BELL_SOURCE),
        _item("Bean Burrito", [], 360, 13, 54, 10, 9, "1 burrito", TACO_BELL_SOURCE),
        _item("Beefy 5-Layer Burrito", ["5 layer burrito"], 490, 18, 64, 18, 9, "1 burrito", TACO_BELL_SOURCE),
        _item("Crunchwrap Supreme", ["crunchwrap"], 530, 16, 71, 21, 6, "1 crunchwrap", TACO_BELL_SOURCE),
        _item("Chicken Quesadilla", [], 510, 27, 38, 27, 4, "1 quesadilla", TACO_BELL_SOURCE),
        _item("Cheese Quesadilla", [], 470, 19, 37, 25, 3, "1 quesadilla", TACO_BELL_SOURCE),
        _item("Nachos BellGrande", ["nachos bell grande"], 730, 17, 82, 38, 13, "1 order", TACO_BELL_SOURCE),
        _item("Cheesy Gordita Crunch", [], 490, 20, 41, 28, 5, "1 item", TACO_BELL_SOURCE),
        _item("Mexican Pizza", [], 540, 19, 48, 29, 8, "1 pizza", TACO_BELL_SOURCE),
        _item("Cheesy Fiesta Potatoes", [], 240, 3, 28, 13, 2, "1 order", TACO_BELL_SOURCE),
        _item("Black Beans and Rice", [], 160, 4, 31, 2, 5, "1 order", TACO_BELL_SOURCE),
        _item("Cinnamon Twists", [], 170, 1, 27, 6, 1, "1 order", TACO_BELL_SOURCE),
        _item("Cinnabon Delights 2 Pack", ["2 cinnabon delights"], 170, 2, 17, 11, 1, "2 pieces", TACO_BELL_SOURCE),
    ],
    "mcdonalds": [
        _item("Chicken McNuggets 10 Piece", ["10pc chicken mcnuggets", "10 piece mcnuggets", "10pc nuggets"], 410, 23, 26, 24, 1, "10 nuggets", MCD_SOURCE),
        _item("Chicken McNuggets 6 Piece", ["6pc mcnuggets", "6 piece nuggets"], 250, 14, 15, 15, 1, "6 nuggets", MCD_SOURCE),
        _item("Chicken McNuggets 4 Piece", ["4pc mcnuggets", "4 piece nuggets"], 170, 9, 10, 10, 0, "4 nuggets", "https://www.mcdonalds.com/us/en-us/product/chicken-mcnuggets-4-piece.html"),
        _item("Big Mac", [], 590, 25, 46, 34, 3, "1 sandwich", MCD_SOURCE),
        _item("Quarter Pounder with Cheese", ["quarter pounder", "qpc"], 520, 30, 42, 26, 2, "1 sandwich", MCD_SOURCE),
        _item("Double Quarter Pounder with Cheese", ["double quarter pounder"], 740, 48, 43, 42, 2, "1 sandwich", MCD_SOURCE),
        _item("McDouble", [], 400, 22, 33, 20, 2, "1 sandwich", MCD_SOURCE),
        _item("Double Cheeseburger", [], 450, 24, 34, 24, 2, "1 sandwich", MCD_SOURCE),
        _item("Cheeseburger", [], 300, 15, 32, 13, 2, "1 sandwich", MCD_SOURCE),
        _item("Hamburger", [], 250, 13, 31, 9, 1, "1 sandwich", MCD_SOURCE),
        _item("McChicken", [], 400, 14, 39, 21, 1, "1 sandwich", MCD_SOURCE),
        _item("Filet-O-Fish", ["filet o fish"], 380, 16, 38, 19, 1, "1 sandwich", MCD_SOURCE),
        _item("Medium World Famous Fries", ["medium fries"], 320, 5, 43, 15, 4, "1 medium order", MCD_SOURCE),
        _item("Small World Famous Fries", ["small fries"], 230, 3, 31, 11, 3, "1 small order", MCD_SOURCE),
        _item("Large World Famous Fries", ["large fries"], 480, 7, 65, 23, 6, "1 large order", MCD_SOURCE),
        _item("Egg McMuffin", [], 310, 17, 30, 13, 2, "1 sandwich", MCD_SOURCE),
        _item("Sausage McMuffin with Egg", [], 480, 20, 30, 31, 2, "1 sandwich", MCD_SOURCE),
        _item("Hash Browns", [], 140, 2, 18, 8, 2, "1 order", MCD_SOURCE),
        _item("Baked Apple Pie", ["apple pie"], 230, 2, 33, 11, 1, "1 pie", MCD_SOURCE),
        _item("Blueberry & Creme Pie", ["blueberry creme pie", "blueberry and creme pie"], 260, 3, 36, 12, 1, "1 pie", MCD_SOURCE),
    ],
    "raising-canes": [
        _item("Chicken Finger", ["chicken tender", "cane's tender"], 130, 13, 5, 6, 0, "1 finger", CANES_SOURCE),
        _item("Three Chicken Fingers", ["3 chicken tenders", "3 fingers"], 390, 39, 15, 18, 0, "3 fingers", CANES_SOURCE),
        _item("Four Chicken Fingers", ["4 chicken tenders", "4 fingers"], 520, 52, 20, 24, 0, "4 fingers", CANES_SOURCE),
        _item("Six Chicken Fingers", ["6 chicken tenders", "6 fingers"], 780, 78, 30, 36, 0, "6 fingers", CANES_SOURCE),
        _item("Cane's Sauce", ["canes sauce", "raising cane's sauce"], 190, 0, 6, 19, 0, "1.5 oz cup", CANES_SOURCE),
        _item("Crinkle-Cut Fries", ["canes fries", "fries"], 390, 5, 49, 19, 7, "1 order", CANES_SOURCE),
        _item("Texas Toast", ["toast"], 150, 4, 24, 4, 1, "1 slice", CANES_SOURCE),
        _item("Coleslaw", ["cole slaw"], 100, 1, 11, 6, 2, "1 serving", CANES_SOURCE),
        _item("Chicken Sandwich", ["canes sandwich"], 780, 48, 66, 39, 5, "1 sandwich", CANES_SOURCE),
        _item("3 Finger Combo Without Drink", ["3 finger combo", "three finger combo"], 1020, 47, 81, 56, 8, "1 combo, excluding drink", CANES_SOURCE),
        _item("Box Combo Without Drink", ["box combo"], 1250, 60, 97, 65, 9, "1 combo, excluding drink", CANES_SOURCE),
        _item("Caniac Combo Without Drink", ["caniac combo"], 1790, 89, 129, 96, 9, "1 combo, excluding drink", CANES_SOURCE),
    ],
}

CHAIN_ALIASES = {
    "chipotle": ("chipotle",),
    "chick-fil-a": ("chick fil a", "chickfila", "cfa"),
    "taco-bell": ("taco bell",),
    "mcdonalds": ("mcdonalds", "mcdonald"),
    "raising-canes": ("raising canes", "raising cane", "canes", "cane"),
}


def normalize_restaurant_query(value: str) -> str:
    """Return lowercase alphanumeric words with possessives/punctuation removed."""
    text = str(value or "").casefold().replace("&", " and ")
    text = re.sub(r"['’]s\b", "s", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def match_restaurant_items(query: str) -> list[dict[str, Any]]:
    """Find non-overlapping curated items for a chain-qualified query."""
    normalized = normalize_restaurant_query(query)
    chain = next((key for key, aliases in CHAIN_ALIASES.items()
                  if any(re.search(rf"\b{re.escape(alias)}\b", normalized)
                         for alias in aliases)), None)
    if chain is None:
        return []
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for item in RESTAURANT_MENU[chain]:
        phrases = [item["name"], *item["aliases"]]
        for phrase in phrases:
            needle = normalize_restaurant_query(phrase)
            match = re.search(rf"\b{re.escape(needle)}\b", normalized)
            if match:
                candidates.append((match.start(), len(needle), item))
                break
    # Prefer longer phrases at the same position ("double chicken" over chicken).
    selected: list[tuple[int, int, dict[str, Any]]] = []
    for start, length, item in sorted(candidates, key=lambda row: (row[0], -row[1])):
        end = start + length
        if any(start < other_start + other_len and end > other_start
               for other_start, other_len, _ in selected):
            continue
        selected.append((start, length, item))
    return [dict(item) for _, _, item in sorted(selected)]


def restaurant_lookup(query: str) -> dict[str, Any] | None:
    """Return one compatible serving result, aggregating explicit multi-orders."""
    matches = match_restaurant_items(query)
    if not matches:
        return None
    macros = {key: round(sum(float(item[key]) for item in matches), 2)
              for key in ("calories", "protein", "carbs", "fat", "fiber")}
    sources = list(dict.fromkeys(str(item["source"]) for item in matches))
    result: dict[str, Any] = {
        "name": " + ".join(str(item["name"]) for item in matches),
        "serving_size": " + ".join(str(item["serving_size"]) for item in matches),
        "macros_per_serving": macros,
        "source": "Restaurant menu: " + ", ".join(sources),
    }
    if len(matches) > 1:
        result["matched_items"] = matches
    return result
