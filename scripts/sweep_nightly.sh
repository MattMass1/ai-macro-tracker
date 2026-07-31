#!/bin/bash
# Nightly max-weight sweep: Fitness Tracker -> Exercise Max Reps.
# Silence = no changes (cron watchdog: nothing delivered). Output = changes.
cd /opt/data/ai-macro-tracker/server
exec .venv/bin/python src/sweep_max_weights.py
