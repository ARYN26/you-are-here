---
title: Beads is a frozen adapter: every plan-state feature ships in STATE.md first
tags: [beads, state-md, where]
paths: ["scripts/where.py", "scripts/beads.py", "skills/phases/SKILL.md", "skills/wrap/SKILL.md"]
status: active
source: PR #6; user, 2026-09-24
---
STATE.md is the default and wins over a beads epic. Beads may not offer anything STATE.md lacks; worktrees read the main checkout's STATE.md like .beads.

Only scripts/beads.py knows beads (where.py calls beads.read()); where.py's `store` field says where the plan is written, and skills read it instead of restating the beads rule.
