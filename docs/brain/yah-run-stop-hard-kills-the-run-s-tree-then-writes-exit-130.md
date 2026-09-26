---
title: yah run --stop hard-kills the run's tree, then writes exit 130 itself
tags: [run, lock, autopilot]
paths: ["scripts/run.py", "scripts/yahlib.py", "skills/auto/SKILL.md"]
status: active
source: scripts/run.py:813
---
No graceful stop: --stop kills the driver and everything under it (taskkill /T; elsewhere SIGSTOP, then SIGKILL the tree), waits for the lock to free, then writes the pid file's exit 130.
