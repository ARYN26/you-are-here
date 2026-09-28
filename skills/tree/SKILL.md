---
name: tree
description: The phase map - the plan's stack of phases, and what one phase does to the repo as a file tree.
argument-hint: "[P<n>|plan]"
disable-model-invocation: true
allowed-tools: Bash(python3 *scripts/phasemap.py*), Bash(python *scripts/phasemap.py*), Bash(py -3 *scripts/phasemap.py*)
---

Arguments: $ARGUMENTS

Run this with the Bash tool, with the arguments above (`P<n>`, `plan`, or nothing for the current phase):

PY "${CLAUDE_SKILL_DIR}/../../scripts/phasemap.py" $ARGUMENTS

`PY` is `python` on Windows and `python3` elsewhere; use `py -3` only if both fail.

Show the output as is, in a code block. Add nothing after it, and do not read the plan, docs or code to embellish it. Empty output means there is no plan to map; say so in one line.
