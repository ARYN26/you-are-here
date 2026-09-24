---
name: where
description: Show where you are in this project - branch, plan, phase, NEXT, open PRs, what waits on you. Use for "where am I", "what's next", and before reading any docs to orient.
allowed-tools: Bash(python3 *scripts/where.py*), Bash(python *scripts/where.py*), Bash(py -3 *scripts/where.py*)
---

Run this with the Bash tool, exactly as written:

PY "${CLAUDE_SKILL_DIR}/../../scripts/where.py"

`PY` is `python` on Windows and `python3` elsewhere. If it is not found or fails, try the other, then `py -3`, and keep whichever works.

Show the output verbatim in a code block.

After it, add at most one sentence, and only when the output shows something to act on: red checks, a PR to retarget, a phase branch that differs from the current branch, or items waiting on the user. Do not read docs or run other commands to embellish it.
