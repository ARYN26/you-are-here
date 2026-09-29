---
title: Run logs are named to the second, so tests that run yah twice share a log on fast CI
tags: [tests, run]
paths: ["tests/test_run.py", "scripts/run.py"]
status: active
source: scripts/run.py:1259, CI run 36638517363
---
run.py names a run's log key-YYYYmmdd-HHMMSS; runs in one second append to one file. Tests reading run_logs()[-1] across runs call clear_run_logs() first.
