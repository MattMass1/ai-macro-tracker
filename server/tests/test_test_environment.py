"""Offline collection must not inherit credentials from a live smoke shell."""
import os
from pathlib import Path
import subprocess
import sys


def test_collection_removes_inherited_provider_settings_before_app_imports():
    conftest = Path(__file__).with_name("conftest.py")
    script = """
import os, runpy, sys
keys = ('OPENAI_API_KEY', 'OPENAI_ACCESS_TOKEN', 'FATSECRET_CLIENT_SECRET',
        'EXA_API_KEY', 'USDA_API_KEY', 'WEB_LOOKUP_PROVIDER', 'COACH_MODEL',
        'FOOD_AGENT_ENABLED', 'DATABASE_URL', 'APP_SHARED_TOKEN')
for key in keys:
    os.environ[key] = 'synthetic-inherited-value'
os.environ['MMMACROS_TEST_PG_ADMIN_URL'] = 'postgresql://localhost/disposable'
os.environ['FOOD_AGENT_BENCH_DIR'] = 'explicit-fixture-path'
runpy.run_path(sys.argv[1])
assert not any(key in os.environ for key in keys)
assert os.environ['MMMACROS_TEST_PG_ADMIN_URL'] == 'postgresql://localhost/disposable'
assert os.environ['FOOD_AGENT_BENCH_DIR'] == 'explicit-fixture-path'
import config
assert config._ENV_LOADED is True
print('isolated')
"""
    # Only OS execution necessities, never the caller's provider configuration.
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "HOME", "TMPDIR", "SYSTEMROOT", "LANG"}}
    result = subprocess.run([sys.executable, "-c", script, str(conftest)],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated"
