#!/usr/bin/env python3
"""Print the daily macro and workout summary from PostgreSQL."""
from __future__ import annotations
import asyncio, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import get_config
from store import Store
from auth import bind_user, reset_user

ROTATION = ["Push", "Pull", "Legs"]

async def run() -> int:
    store = Store(get_config().database_url)
    user_context = bind_user(get_config().matt_user_id)
    try:
        today = datetime.now().astimezone().date()
        meals = await store.fetch_meals(today)
        target = await store.fetch_targets(today) or {k: 0 for k in ('calories','protein','carbs','fat')}
        totals = {k: sum(float(m.get(k) or 0) for m in meals) for k in ('calories','protein','carbs','fat')}
        prs = await store.fetch_prs(); last = None
        if prs:
            last = next((t for t in ROTATION if t in (prs[0].get('workout_type') or [])), None)
        nxt = ROTATION[(ROTATION.index(last)+1) % 3] if last else None
        lines=[f"📊 *Macros — {today.isoformat()}*"]
        for icon,key,label,unit in [('🔥','calories','kcal',''),('🥩','protein','protein','g'),('🌾','carbs','carbs','g'),('🧈','fat','fat','g')]:
            lines.append(f"  {icon} {totals[key]:g}/{float(target.get(key) or 0):g}{unit} {label} ({float(target.get(key) or 0)-totals[key]:+.0f})")
        lines += [f"\n_Meals ({len(meals)}):_", *[f"  🍽 {m['name']} ({m['meal']}) — {m['calories']:g} kcal" for m in meals]] if meals else ["\n_No meals logged yet today._"]
        lines.append(f"\n💪 *Next workout: {nxt} day*" if nxt else "\n💪 No workout data yet — log one to start PR tracking.")
        print("\n".join(lines)); return 0
    finally:
        reset_user(user_context)
        await store.aclose()

if __name__ == '__main__': sys.exit(asyncio.run(run()))
