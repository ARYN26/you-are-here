---
title: Auto-merge is opt-in: yah merges only green PRs it opened
tags: [merge, auto, setup]
paths: ["skills/auto/SKILL.md", "scripts/setup.py", "scripts/run.py"]
status: active
source: user, 2026-09-25
---
Off unless /yah:setup opts in. Only the run driver merges its own phase PR, judged on a read taken after checks pass: a check ran, CLEAN, not draft, not stacked. Merge commit, retarget, then delete.
