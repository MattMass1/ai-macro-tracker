-- REVIEWED ROLLBACK for 003_calorie_only_unknown_macros.sql. NOT a forward
-- migration: it lives outside server/migrations/ so the deploy runner can
-- never apply it. Run it only through
--   python scripts/nutrient_rollback.py --restore-not-null --confirm-checksum <sha256>
-- after separate owner approval. The script runs this whole file in ONE
-- transaction and records it in schema_rollbacks; schema_migrations history
-- (including the 003 row and its checksum) is never edited or deleted.
--
-- Safety invariant: NOT NULL may be restored only while zero unknown-nutrient
-- rows exist. The table lock blocks concurrent writers between the check and
-- the ALTER. If any NULL exists this file aborts and changes nothing; use the
-- forward-disable rollback instead (CALORIE_ONLY_WRITES_ENABLED=false). Never
-- replace NULL with 0 to make this pass.
LOCK TABLE nutrition_entries IN ACCESS EXCLUSIVE MODE;

DO $$
DECLARE unknown_rows bigint;
BEGIN
    SELECT count(*) INTO unknown_rows FROM nutrition_entries
    WHERE protein IS NULL OR carbs IS NULL OR fat IS NULL OR fiber IS NULL;
    IF unknown_rows <> 0 THEN
        RAISE EXCEPTION 'refusing rollback: % nutrition_entries row(s) have unknown nutrients; use forward-disable', unknown_rows;
    END IF;
END
$$;

ALTER TABLE nutrition_entries
    ALTER COLUMN protein SET NOT NULL,
    ALTER COLUMN carbs SET NOT NULL,
    ALTER COLUMN fat SET NOT NULL,
    ALTER COLUMN fiber SET NOT NULL;
