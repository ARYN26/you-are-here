---
title: Stacked PRs into main merge with a merge commit, not squash
tags: [git, pr, merge]
paths: ["scripts/where.py"]
status: active
source: PR #3, #6, #7 merged clean 2026-09-25; #5 closed when #4's branch was deleted
---
main takes squash merges, but a stack's base needs a merge commit or the next PR conflicts on retarget. Retarget each PR to main before deleting its base branch, or GitHub closes it.
