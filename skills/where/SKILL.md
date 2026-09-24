---
name: where
description: Branch, phase, NEXT, PRs, what waits on you. Use for "where am I", "what's next".
allowed-tools: Bash(python3 *scripts/where.py*), Bash(python *scripts/where.py*), Bash(py -3 *scripts/where.py*)
---

Run this with the Bash tool, exactly as written:

PY "${CLAUDE_SKILL_DIR}/../../scripts/where.py"

`PY` is `python` on Windows and `python3` elsewhere; use `py -3` only if both fail.

Show the output verbatim in a code block.

After it, add at most one sentence, and only when the output shows something to act on: red checks, a PR to retarget, a phase branch that differs from the current branch, or items waiting on the user. Do not read docs or run other commands to embellish it.
