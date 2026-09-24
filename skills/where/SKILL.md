---
name: where
description: Show where you are in this project - branch, plan, phase, NEXT, open PRs, what waits on you. Use for "where am I", "what's next", and before reading any docs to orient.
allowed-tools: Bash(python3 *scripts/where.py*), Bash(python *scripts/where.py*), Bash(py -3 *scripts/where.py*)
---

Run this with the Bash tool, exactly as written:

python3 "${CLAUDE_SKILL_DIR}/../../scripts/where.py"

If `python3` is not found or fails (as on Windows), run the same command with `python` instead.

Show the output verbatim in a code block.

After it, add at most one sentence, and only when the output shows something to act on: red checks, a PR to retarget, a phase branch that differs from the current branch, or items waiting on the user. Do not read docs or run other commands to embellish it.
