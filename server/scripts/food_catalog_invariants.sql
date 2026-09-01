-- Post-migration aggregate-only checks. Run after migrations/backfill.
SELECT 'nutrition_entries' AS invariant, count(*)::text AS value FROM nutrition_entries
UNION ALL SELECT 'meal_presets', count(*)::text FROM meal_presets
UNION ALL SELECT 'tenant_nulls', count(*)::text FROM nutrition_entries WHERE user_id IS NULL
UNION ALL SELECT 'cross_tenant_meals', count(*)::text FROM nutrition_entries e JOIN meals m ON m.id=e.meal_id WHERE e.user_id IS DISTINCT FROM m.user_id
UNION ALL SELECT 'day_rollup_delta', count(*)::text FROM days d FULL JOIN (SELECT user_id,day,sum(calories) calories,sum(protein) protein,sum(carbs) carbs,sum(fat) fat,sum(fiber) fiber FROM nutrition_entries GROUP BY user_id,day) x ON x.user_id=d.user_id AND x.day=d.date WHERE (d.calories,d.protein,d.carbs,d.fat,d.fiber) IS DISTINCT FROM (x.calories,x.protein,x.carbs,x.fat,x.fiber)
UNION ALL SELECT 'meal_rollup_delta', count(*)::text FROM meals m FULL JOIN (SELECT user_id,meal_id,sum(calories) calories,sum(protein) protein,sum(carbs) carbs,sum(fat) fat,sum(fiber) fiber FROM nutrition_entries GROUP BY user_id,meal_id) x ON x.user_id=m.user_id AND x.meal_id=m.id WHERE (m.calories,m.protein,m.carbs,m.fat,m.fiber) IS DISTINCT FROM (x.calories,x.protein,x.carbs,x.fat,x.fiber)
UNION ALL SELECT 'unlinked_entries', count(*)::text FROM nutrition_entries WHERE food_item_id IS NULL OR food_observation_id IS NULL
UNION ALL SELECT 'unlinked_presets', count(*)::text FROM meal_presets WHERE food_item_id IS NULL OR food_observation_id IS NULL
UNION ALL SELECT 'verified_observations', count(*)::text FROM food_nutrition_observations WHERE verification_state IN ('official_curated','internal_curated','exact_identifier')
UNION ALL SELECT 'unverified_observations', count(*)::text FROM food_nutrition_observations WHERE verification_state IN ('community_observed','unknown')
UNION ALL SELECT 'estimate_observations', count(*)::text FROM food_nutrition_observations WHERE verification_state='estimate'
UNION ALL SELECT 'duplicate_observation_keys', count(*)::text FROM (SELECT observation_key FROM food_nutrition_observations GROUP BY observation_key HAVING count(*) > 1) d
UNION ALL SELECT 'catalog_cross_item_observations', count(*)::text FROM nutrition_entries e JOIN food_nutrition_observations o ON o.id=e.food_observation_id WHERE o.food_item_id IS DISTINCT FROM e.food_item_id;
