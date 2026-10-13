---
title: Auto-merge is on by default: the run driver merges green PRs yah opened
tags: [merge, auto, setup]
paths: ["skills/auto/SKILL.md", "scripts/setup.py", "scripts/run.py", "scripts/yahlib.py"]
status: active
source: user, 2026-10-12
---
On unless auto_merge is false in config.json or projects.<key>. Only the run driver merges its own green phase PR: checks passed, or none and a judge pass on its head. No stacking; sweeps leftovers.
