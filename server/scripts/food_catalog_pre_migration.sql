-- Pre-migration aggregate baseline. References legacy schema only.
SELECT 'nutrition_entries' AS invariant, count(*)::text AS value FROM nutrition_entries
UNION ALL SELECT 'meal_presets', count(*)::text FROM meal_presets
UNION ALL SELECT 'tenant_nulls', count(*)::text FROM nutrition_entries WHERE user_id IS NULL
UNION ALL SELECT 'cross_tenant_meals', count(*)::text FROM nutrition_entries e JOIN meals m ON m.id=e.meal_id WHERE e.user_id IS DISTINCT FROM m.user_id
UNION ALL SELECT 'day_rollup_delta', count(*)::text FROM days d FULL JOIN (SELECT user_id,day,sum(calories) calories,sum(protein) protein,sum(carbs) carbs,sum(fat) fat,sum(fiber) fiber FROM nutrition_entries GROUP BY user_id,day) x ON x.user_id=d.user_id AND x.day=d.date WHERE (d.calories,d.protein,d.carbs,d.fat,d.fiber) IS DISTINCT FROM (x.calories,x.protein,x.carbs,x.fat,x.fiber)
UNION ALL SELECT 'meal_rollup_delta', count(*)::text FROM meals m FULL JOIN (SELECT user_id,meal_id,sum(calories) calories,sum(protein) protein,sum(carbs) carbs,sum(fat) fat,sum(fiber) fiber FROM nutrition_entries GROUP BY user_id,meal_id) x ON x.user_id=m.user_id AND x.meal_id=m.id WHERE (m.calories,m.protein,m.carbs,m.fat,m.fiber) IS DISTINCT FROM (x.calories,x.protein,x.carbs,x.fat,x.fiber);
