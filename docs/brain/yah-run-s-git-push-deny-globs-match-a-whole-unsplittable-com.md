---
title: yah run's git push deny globs match a whole unsplittable command line
tags: [run, deny, push]
paths: ["scripts/run.py", "skills/resume/SKILL.md", "skills/wrap/SKILL.md"]
status: active
source: yah-e2e run e2e-20260926-154207 P2 denial; yah/push-alone branch
---
A Bash line Claude Code cannot split (heredoc in $()) is matched whole against --disallowedTools, so text after git push (a + or main in a PR body) trips run.py's globs; push alone.
