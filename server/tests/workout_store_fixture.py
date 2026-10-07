"""Transaction-capable pool double for workout-store SQL unit tests."""
from auth import current_user_id


class WorkoutTransactionPool:
    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def execute(self, sql, *args):
        assert "pg_advisory_xact_lock" in sql
        assert "workout:" in sql
        assert args == (str(current_user_id()),)
