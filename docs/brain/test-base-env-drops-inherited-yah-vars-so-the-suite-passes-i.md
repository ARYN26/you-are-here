---
title: Test Base.env drops inherited YAH_ vars, so the suite passes inside yah run
tags: [tests, run, env]
paths: ["tests/test_yah.py", "tests/test_run.py", "scripts/run.py"]
status: active
source: tests/test_yah.py:82
---
run.py sets YAH_PROTECTED for child sessions and refuses a start or --stop when it is set (exit 5). Base.setUp and RunTests.setUp leave YAH_* vars out of their env copy, so the suite passes under yah run.
