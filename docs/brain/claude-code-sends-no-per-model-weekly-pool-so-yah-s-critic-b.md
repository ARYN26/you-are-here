---
title: Claude Code sends no per-model weekly pool, so yah's critic bar falls back to the weekly bar
tags: [run, critic, pace]
paths: ["scripts/run.py"]
status: active
source: scripts/run.py review_role; runs/*.log 26-29 Sep 2026 (44 reviews, all fable high (bar unknown))
---
rate_limits and unifiedWindows carry five_hour and seven_day only; pools is {} everywhere, so run.py review_role reads r.used (week) when pool_used finds no seven_day_<model>.
