"""Idempotently seed trusted curated menus and hardcoded known foods."""
from __future__ import annotations

import argparse, asyncio, json, os, sys
from pathlib import Path
from uuid import UUID, uuid5
import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from food_catalog import evidence_hash, normalize_food_name  # noqa: E402
from restaurant_menu import RESTAURANT_MENU  # noqa: E402

NAMESPACE = UUID("850239a6-e2a2-4ff2-af2b-dbd29e95e683")
KNOWN_REF = "server/src/server.py#KNOWN_CHAT_FOODS@2026-09-01"
KNOWN = [
    ("Moe's cookie",170,2,23,8,0,"1 cookie"),("Fairlife 30g shake",150,30,3,2.5,0,"1 shake"),
    ("Barebells",200,20,21,7,0,"1 bar"),("Banana",105,1,27,0,0,"1 medium"),
    ("Rice Krispies Treat",90,1,16,3,0,"1 bar"),("TJ olive oil butter",80,0,0,9,0,"1 serving"),
    ("TJ artisan roll",200,8,38,2,0,"1 roll"),("3 large eggs",216,19,1,14,0,"3 eggs"),
    ("McNuggets 10pc",410,24,25,25,0,"10 pieces"),("Michelob Ultra",95,0,2.6,0,0,"1 serving"),
    ("Chipotle bowl",625,75,38,22,0,"1 bowl"),("Chipotle burrito",945,83,88,31,0,"1 burrito"),
    ("CFA 8ct grilled nuggets + grilled club + sauce",710,62,61,25,0,"1 combination"),
]


def records():
    for chain, items in RESTAURANT_MENU.items():
        for item in items:
            yield {**item, "provider":"curated_restaurant", "external_id":f"restaurant:{chain}:{normalize_food_name(item['name']).replace(' ','-')}",
                "source_reference":item["source"], "source_url":item["source"]}
    for name, calories, protein, carbs, fat, fiber, serving in KNOWN:
        yield {"name":name,"aliases":[],"calories":calories,"protein":protein,"carbs":carbs,"fat":fat,"fiber":fiber,
            "serving_size":serving,"provider":"hardcoded_known_food","external_id":f"known:{normalize_food_name(name).replace(' ','-')}",
            "source_reference":KNOWN_REF,"source_url":None}


async def seed(conn) -> int:
    changed = 0
    for item in records():
        stable = item["external_id"]; item_id = uuid5(NAMESPACE, "item:" + stable)
        source_id = uuid5(NAMESPACE, "source:" + stable); observation_id = uuid5(NAMESPACE, "observation:" + stable)
        digest = evidence_hash({key:item[key] for key in ("external_id","source_reference","serving_size","calories","protein","carbs","fat","fiber")})
        result = await conn.execute("INSERT INTO food_items(id,normalized_name,display_name,product_identity) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
            item_id, normalize_food_name(item["name"]), item["name"], stable)
        changed += result.endswith(" 1")
        for alias in [item["name"], *item["aliases"]]:
            await conn.execute("INSERT INTO food_aliases(food_item_id,normalized_alias,alias_text,alias_kind) VALUES($1,$2,$3,'curated') ON CONFLICT DO NOTHING",
                item_id, normalize_food_name(alias), alias)
        await conn.execute("INSERT INTO food_identifiers(food_item_id,identifier_type,provider,external_id) VALUES($1,'provider_id',$2,$3) ON CONFLICT DO NOTHING",
            item_id, item["provider"], stable)
        await conn.execute("INSERT INTO food_source_records(id,provider,external_id,source_reference,source_url,retrieved_at,confidence,evidence_hash,metadata,trusted,cache_allowed) "
            "VALUES($1,$2,$3,$4,$5,TIMESTAMPTZ '2026-09-01 00:00:00+00',1,$6,$7::jsonb,true,true) ON CONFLICT DO NOTHING",
            source_id,item["provider"],stable,item["source_reference"],item["source_url"],digest,json.dumps({"serving_size":item["serving_size"]}))
        verification_state = "official_curated" if item["provider"] == "curated_restaurant" else "internal_curated"
        await conn.execute("INSERT INTO food_nutrition_observations(id,observation_key,food_item_id,source_record_id,verification_state,serving_basis,serving_text,"
            "calories,protein,carbs,fat,fiber,observed_at) VALUES($1,$2,$3,$4,$5,'per_serving',$6,$7,$8,$9,$10,$11,TIMESTAMPTZ '2026-09-01 00:00:00+00') ON CONFLICT DO NOTHING",
            observation_id,"curated:"+stable,item_id,source_id,verification_state,item["serving_size"],*(item[k] for k in ("calories","protein","carbs","fat","fiber")))
    return changed


async def run(url: str, apply: bool):
    items = list(records()); result = {"mode":"apply" if apply else "dry-run", "projected_items":len(items)}
    if apply:
        conn = await asyncpg.connect(url)
        try:
            async with conn.transaction(): result["new_items"] = await seed(conn)
        finally: await conn.close()
    print(json.dumps(result, sort_keys=True)); return result


def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--apply",action="store_true")
    parser.add_argument("--database-url",default=os.environ.get("DATABASE_URL","")); args=parser.parse_args()
    if args.apply and not args.database_url: parser.error("DATABASE_URL is required with --apply")
    asyncio.run(run(args.database_url,args.apply)); return 0


if __name__ == "__main__": raise SystemExit(main())
