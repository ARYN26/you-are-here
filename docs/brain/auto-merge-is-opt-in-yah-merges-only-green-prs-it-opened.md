---
title: Auto-merge is opt-in: yah merges only green PRs it opened
tags: [merge, auto, setup]
paths: ["skills/auto/SKILL.md", "scripts/setup.py", "scripts/run.py"]
status: active
source: user, 2026-09-25
---
Off unless the user opts in via /yah:setup. When on: every check passed, MERGEABLE, no unanswered change request; merge commit, retarget the stacked PR, then delete the branch.
