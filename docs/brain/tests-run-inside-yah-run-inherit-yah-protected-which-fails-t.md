---
title: Tests run inside yah run inherit YAH_PROTECTED, which fails test_launcher_runs_in_cmd
tags: [tests, run, env]
paths: ["tests/test_yah.py", "scripts/run.py"]
status: active
source: scripts/run.py:657, tests/test_yah.py:1420
---
run.py sets YAH_PROTECTED for child sessions; the suite copies os.environ, so under yah run the launcher test fails. Run it with env -u YAH_PROTECTED.
