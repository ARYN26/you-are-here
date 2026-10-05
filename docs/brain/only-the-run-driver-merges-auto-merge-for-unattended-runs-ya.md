---
title: Only the run driver merges: auto_merge for unattended runs, /yah:merge on request
tags: [merge, auto, setup]
paths: ["skills/merge/SKILL.md", "skills/auto/SKILL.md", "scripts/setup.py", "scripts/run.py"]
status: active
source: C:/Users/Aryan/.claude/plans/imperative-purring-dragonfly.md:49 (plan Decisions); scripts/run.py merge_now
---
auto_merge (setup opt-in) lets a run merge its phase PR and sweep the plan's green stack bottom-up. /yah:merge (run.py --merge) merges on request, auto_merge on or off. Never the model.
Rules per PR (mergeable): a check ran and all passed, MERGEABLE with CLEAN or HAS_HOOKS, not draft, no newer change request, your gh user authored it, it is that phase's PR, not stacked on another phase's branch; with the quality profile, a judge pass logged on its current head. Merge commit pinned to the head, retarget the PR stacked on it, then delete the branch. setup --auto-merge also adds Bash(<py> *scripts/run.py* --merge) allow rules so auto mode lets /yah:merge run.
