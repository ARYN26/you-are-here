---
title: yah run's codex critic gets its brief from run.py; only a codex.py failure turns codex off
tags: [codex, run, critic]
paths: ["scripts/run.py"]
status: active
source: scripts/run.py:854
---
GPT has no gh or network in review: run.py writes the plan section or PR diff to runs/*-brief.md. No brief: that review goes to roles.review_fallback; a codex.py error: codex off for the run.
