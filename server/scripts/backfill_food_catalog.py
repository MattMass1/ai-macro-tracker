"""Idempotently link legacy logs/presets to deduplicated unverified evidence."""
from __future__ import annotations

import argparse, asyncio, json, os, sys
from pathlib import Path
import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from food_catalog import evidence_hash, legacy_food_identity, provenance_state  # noqa: E402

TABLES = ("nutrition_entries", "meal_presets")
MACROS = ("calories", "protein", "carbs", "fat", "fiber")


def stable_signature(row) -> str:
    """Stable food + source + serving + macro signature shared across rows."""
    normalized, _display, product_identity = legacy_food_identity(row["name"])
    return evidence_hash({"food": normalized, "product_identity": product_identity,
        "source": " ".join(str(row["macro_source"] or "community_observed").split()).casefold(),
        "serving": str(row.get("serving_text") or ""),
        "macros": [str(row[key]) for key in MACROS]})


async def counts(conn) -> dict[str, int]:
    row = await conn.fetchrow("SELECT (SELECT count(*) FROM nutrition_entries) entries,"
        "(SELECT count(*) FROM nutrition_entries WHERE food_item_id IS NULL OR food_observation_id IS NULL) unlinked_entries,"
        "(SELECT count(*) FROM meal_presets) presets,"
        "(SELECT count(*) FROM meal_presets WHERE food_item_id IS NULL OR food_observation_id IS NULL) unlinked_presets,"
        "(SELECT count(*) FROM food_items) food_items,(SELECT count(*) FROM food_aliases) aliases,"
        "(SELECT count(*) FROM food_nutrition_observations) observations")
    return {key: int(value) for key, value in row.items()}


async def pending_rows(conn):
    result = []
    for table in TABLES:
        rows = await conn.fetch(f"SELECT id,name,macro_source,calories,protein,carbs,fat,fiber,created_at,"
            f"NULL::text serving_text FROM {table} WHERE food_item_id IS NULL OR food_observation_id IS NULL ORDER BY id")
        result.extend((table, row) for row in rows)
    return result


def projected(rows) -> dict[str, int]:
    identities = {legacy_food_identity(row["name"])[::2] for _, row in rows}
    signatures = {stable_signature(row) for _, row in rows}
    return {"links": len(rows), "food_items": len(identities), "aliases": len(identities),
            "observations": len(signatures), "source_records": len(signatures)}


async def link_row(conn, table: str, row) -> bool:
    normalized, display_name, product_identity = legacy_food_identity(row["name"])
    item_id = await conn.fetchval("INSERT INTO food_items(normalized_name,display_name,product_identity) VALUES($1,$2,$3) "
        "ON CONFLICT(normalized_name,brand,product_identity) DO UPDATE SET display_name=food_items.display_name RETURNING id",
        normalized, display_name, product_identity)
    await conn.execute("INSERT INTO food_aliases(food_item_id,normalized_alias,alias_text,alias_kind) "
        "VALUES($1,$2,$3,'legacy_name') ON CONFLICT(food_item_id,normalized_alias) DO NOTHING",
        item_id, normalized, display_name)
    source = str(row["macro_source"] or "community_observed")
    state = "estimate" if provenance_state(source)[0] == "estimate" else "community_observed"
    signature = stable_signature(row)
    source_id = await conn.fetchval("WITH i AS (INSERT INTO food_source_records(provider,source_reference,retrieved_at,evidence_hash,metadata,trusted,cache_allowed) "
        "VALUES('legacy_backfill',$1,COALESCE($2,now()),$3,$4::jsonb,false,true) ON CONFLICT(provider,evidence_hash) DO NOTHING RETURNING id) "
        "SELECT id FROM i UNION ALL SELECT id FROM food_source_records WHERE provider='legacy_backfill' AND evidence_hash=$3 LIMIT 1",
        source, row["created_at"], signature, json.dumps({"macro_source": source, "signature": signature}))
    observation_key = "legacy-signature:" + signature
    observation_id = await conn.fetchval("WITH i AS (INSERT INTO food_nutrition_observations(observation_key,food_item_id,source_record_id,verification_state,serving_basis,serving_text,"
        "calories,protein,carbs,fat,fiber,observed_at) VALUES($1,$2,$3,$4,'logged_snapshot',$5,$6,$7,$8,$9,$10,COALESCE($11,now())) "
        "ON CONFLICT(observation_key) DO NOTHING RETURNING id) SELECT id FROM i UNION ALL SELECT id FROM food_nutrition_observations WHERE observation_key=$1 LIMIT 1",
        observation_key, item_id, source_id, state, row.get("serving_text"), *(row[key] for key in MACROS), row["created_at"])
    result = await conn.execute(f"UPDATE {table} SET food_item_id=$1,food_observation_id=$2 WHERE id=$3 "
        "AND (food_item_id IS NULL OR food_observation_id IS NULL)", item_id, observation_id, row["id"])
    return result.endswith(" 1")


async def run(database_url: str, apply: bool) -> dict[str, object]:
    conn = await asyncpg.connect(database_url)
    try:
        before, rows = await counts(conn), await pending_rows(conn)
        changes = 0
        if apply:
            async with conn.transaction():
                for table, row in rows:
                    changes += await link_row(conn, table, row)
        result = {"mode": "apply" if apply else "dry-run", "before": before,
            "projected": projected(rows), "after": await counts(conn), "logical_rows_changed": changes}
        print(json.dumps(result, sort_keys=True)); return result
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--apply", action="store_true")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", "")); args = parser.parse_args()
    if not args.database_url: parser.error("DATABASE_URL or --database-url is required")
    asyncio.run(run(args.database_url, args.apply)); return 0


if __name__ == "__main__": raise SystemExit(main())
