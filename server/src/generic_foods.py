"""Curated, source-pinned nutrition for exact generic whole-food identities.

All values are per 100 g edible food and copied from the cited row. Meat
defaults raw; produce defaults to its as-sold raw state. A default serving is
only a silent logging fallback, never a claim that the user stated that size.
"""
from __future__ import annotations

from typing import Any

COFID = (
    "UK CoFID 2021 https://www.gov.uk/government/publications/"
    "composition-of-foods-integrated-dataset-cofid"
)

FOODS: dict[str, dict[str, Any]] = {
    "93 7 ground beef": {"name":"93/7 ground beef","basis":"raw","grams":100,
        "macros":(152,20.54,0,7.14,0),"source":"OpenFoodFacts 0710178008028 (unbranded generic record)"},
    "steak": {"name":"beef fillet steak","basis":"raw","grams":100,
        "macros":(155,20.9,0,7.9,0),"source":f"{COFID}, food 18-017"},
    "chicken breast": {"name":"chicken breast","basis":"raw, meat only","grams":100,
        "macros":(106,24,0,1.1,0),"source":f"{COFID}, food 18-290"},
    "chicken thigh": {"name":"chicken thigh","basis":"raw, meat only","grams":100,
        "macros":(109,20.9,0,2.8,0),"source":f"{COFID}, food 18-289"},
    "salmon": {"name":"farmed salmon","basis":"raw, flesh only","grams":100,
        "macros":(217,20.4,0,15,.2),"source":f"{COFID}, food 16-356"},
    "eggs": {"name":"chicken egg","basis":"raw","grams":52.7,
        "count_grams":{"small":41.6,"medium":46.4,"large":52.7},
        "macros":(131,12.6,0,9,0),"source":f"{COFID}, food 12-937; Health Canada CNF food 125 serving weights"},
    "egg": {"alias_of":"eggs"},
    "sweet potato": {"name":"sweet potato","basis":"as-sold raw","grams":100,
        "macros":(87,1.2,21.3,.3,2.4),"source":f"{COFID}, food 13-463 (NSP fibre)"},
    "white potato": {"name":"white potato","basis":"as-sold raw, flesh only","grams":100,
        "macros":(82,1.9,19.6,.1,2),"source":f"{COFID}, food 13-489"},
    "white rice": {"name":"white long-grain rice","basis":"as-sold raw","grams":100,
        "macros":(355,6.7,85.1,1,1.1),"source":f"{COFID}, food 11-861"},
    "brown rice": {"name":"brown wholegrain rice","basis":"as-sold raw","grams":100,
        "macros":(333,7.7,77,1.5,3),"source":f"{COFID}, food 11-868"},
    "banana": {"name":"banana","basis":"as-sold flesh","grams":100,
        "count_grams":{"small":101,"medium":118,"large":136},
        "macros":(81,1.2,20.3,.1,1.4),"source":f"{COFID}, food 14-318; Health Canada CNF food 1704 serving weights"},
    "apple": {"name":"apple","basis":"as-sold raw, skin on","grams":182,
        "count_grams":{"small":149,"medium":182,"large":223},
        "macros":(51,.6,11.6,.5,1.2),"source":f"{COFID}, food 14-319; Health Canada CNF food 1696 serving weights"},
    "strawberries": {"name":"strawberries","basis":"as-sold raw","grams":100,
        "macros":(30,.6,6.1,.5,3.8),"source":f"{COFID}, food 14-324"},
    "blueberries": {"name":"blueberries","basis":"as-sold raw","grams":100,
        "macros":(40,.9,9.1,.2,1.5),"source":f"{COFID}, food 14-325"},
    "oats": {"name":"porridge oats","basis":"as-sold dry","grams":100,
        "macros":(381,10.9,70.7,8.1,7.8),"source":f"{COFID}, food 11-788"},
    "broccoli": {"name":"broccoli","basis":"as-sold raw","grams":100,
        "macros":(34,4.3,3.2,.6,4),"source":f"{COFID}, food 13-502"},
    "spinach": {"name":"baby spinach","basis":"as-sold raw","grams":100,
        "macros":(16,2.6,.2,.6,1),"source":f"{COFID}, food 13-521"},
    "avocado": {"name":"Hass avocado","basis":"as-sold flesh","grams":100,
        "macros":(171,1.8,1.8,17.4,3.1),"source":f"{COFID}, food 14-386"},
    "olive oil": {"name":"olive oil","basis":"as-sold","grams":100,
        "macros":(899,0,0,99.9,0),"source":f"{COFID}, food 17-038"},
    "butter": {"name":"butter","basis":"as-sold unsalted","grams":100,
        "macros":(744,.6,.6,82.2,0),"source":f"{COFID}, food 17-661"},
    "whole milk": {"name":"whole milk","basis":"as-sold pasteurised","grams":100,
        "macros":(63,3.4,4.6,3.6,0),"source":f"{COFID}, food 12-596"},
    "greek yogurt": {"name":"Greek yogurt","basis":"as-sold plain, whole milk","grams":100,
        "macros":(133,5.7,4.8,10.2,0),"source":f"{COFID}, food 12-555"},
    "peanut butter": {"name":"peanut butter","basis":"as-sold smooth","grams":100,
        "macros":(607,22.8,13.1,51.8,6.6),"source":f"{COFID}, food 14-892"},
}
