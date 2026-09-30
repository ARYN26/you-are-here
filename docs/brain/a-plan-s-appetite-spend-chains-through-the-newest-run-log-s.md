---
title: A plan's appetite spend chains through the newest run log's plan line
tags: [run, appetite, logs]
paths: ["scripts/run.py"]
status: active
source: scripts/run.py:plan_spend
---
Run logs start 'plan <file> | before $X and Hh'. Spend = newest log on the plan (before + session lines + stamp span) + this run; prune keeps that log (newest_by_plan). The chain only holds with one run per plan: run.py takes runs/plan-<hash>.lock (plan_lock) across checkouts and rereads the spend under it; hours are written with 6 decimals, since 2 dropped short runs. Editing iteration/review log lines means editing SESSION.
