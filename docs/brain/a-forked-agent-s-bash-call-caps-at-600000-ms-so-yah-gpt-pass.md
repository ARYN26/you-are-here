---
title: A forked agent's Bash call caps at 600000 ms, so /yah:gpt passes codex.py --timeout 570
tags: [codex, gpt, timeout]
paths: ["skills/gpt/SKILL.md", "agents/gpt.md", "scripts/codex.py"]
status: active
source: skills/gpt/SKILL.md:20
---
The Bash tool stops a foreground call at 10 minutes; codex_timeout_minutes (15) cannot bind there, so the gpt skill and agent pass --timeout 570 and codex.py fails with its own line first.
