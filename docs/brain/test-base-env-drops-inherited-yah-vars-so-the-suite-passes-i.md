---
title: Test Base.env drops inherited YAH_ vars, so the suite passes inside yah run
tags: [tests, run, env]
paths: ["tests/test_yah.py", "scripts/run.py"]
status: active
source: tests/test_yah.py:82
---
run.py sets YAH_PROTECTED for child sessions; Base.setUp leaves YAH_* vars out of its env copy, so the suite passes under yah run.
