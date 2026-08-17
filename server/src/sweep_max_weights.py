#!/usr/bin/env python3
"""Synchronize absolute workout PRs into exercise_max_reps."""
from __future__ import annotations
import argparse, asyncio, sys
from datetime import date
from uuid import uuid4
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import get_config
from domain import MUSCLE_TO_WORKOUT_TYPE
from store import Store

async def run(dry_run: bool) -> int:
    store=Store(get_config().database_url); changes=[]
    try:
        workouts=await store.fetch_workouts(); existing={r['exercise'].strip().casefold():r for r in await store.fetch_prs()}
        best={}
        for row in workouts:
            if not row['exercise'] or not row['sets']: continue
            weight=max(float(s['weight']) for s in row['sets']); key=row['exercise'].strip().casefold()
            if weight > float(best.get(key,{}).get('weight',0)): best[key]={**row,'weight':weight}
        pool=await store.connect()
        for key,row in sorted(best.items()):
            old=existing.get(key); types=row['workout_type'] or sorted({MUSCLE_TO_WORKOUT_TYPE[m] for m in row['muscle_group'] if m in MUSCLE_TO_WORKOUT_TYPE})
            if old is None or row['weight'] > float(old['max_weight']):
                changes.append(f"{'NEW' if old is None else 'PR'}: {row['exercise']} -> {row['weight']:g}")
                if not dry_run:
                    if old is None:
                        await pool.execute("INSERT INTO exercise_max_reps(id,exercise,max_weight,date_achieved,workout_type,source_entry) VALUES($1,$2,$3,$4,$5,$6)",str(uuid4()),row['exercise'],row['weight'],date.fromisoformat(row['date']) if row['date'] else None,types,row['id'])
                    else:
                        await pool.execute("UPDATE exercise_max_reps SET max_weight=$2,date_achieved=$3,workout_type=$4,source_entry=$5 WHERE id=$1",old['id'],row['weight'],date.fromisoformat(row['date']),types,row['id'])
            elif not old['workout_type'] and types:
                changes.append(f"TAG: {row['exercise']} += {', '.join(types)}")
                if not dry_run: await pool.execute("UPDATE exercise_max_reps SET workout_type=$2 WHERE id=$1",old['id'],types)
    finally: await store.aclose()
    if changes:
        print(f"{'[dry-run] ' if dry_run else ''}Max weight sweep — {len(changes)} change(s):")
        print(*[f"  {x}" for x in changes],sep='\n')
    return 0

def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--dry-run',action='store_true'); return asyncio.run(run(p.parse_args().dry_run))
if __name__ == '__main__': sys.exit(main())
