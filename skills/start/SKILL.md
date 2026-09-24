---
name: start
description: Start a task with its context - recall the project brain notes that apply, restate the phase and NEXT, then begin. Use as /yah:start <task> at the start of a task.
argument-hint: "<the task>"
allowed-tools: Bash(python3 *scripts/brain.py* recall *), Bash(python *scripts/brain.py* recall *), Bash(py -3 *scripts/brain.py* recall *), Bash(python3 *scripts/brain.py* find *), Bash(python *scripts/brain.py* find *), Bash(py -3 *scripts/brain.py* find *), Bash(python3 *scripts/where.py*), Bash(python *scripts/where.py*), Bash(py -3 *scripts/where.py*)
---

# Start

The task: $ARGUMENTS

Budget: at most 2 tool calls before the restate block (recall, and where.py only if needed). The restate block is your first output. Before it, do not read `docs/`, `documents/`, plans, or any file over 200 lines, and do not search the codebase.

## 1. Recall

`PY` is `python` on Windows and `python3` elsewhere. If it is not found or fails, try the other, then `py -3`, and keep whichever works.

Run with Bash, with the task in place of `<task>` (drop any double quotes from it):

PY "${CLAUDE_SKILL_DIR}/../../scripts/brain.py" recall --json "<task>"

- **JSON printed:** the brain exists. `matches` are the notes that fit, best first; `also` lists related slugs. Open a note (`<brain_dir>/<slug>.md`) only when its TL;DR is not enough for the task, and only after the restate block.
- **Nothing printed:** this repo has no brain folder. Remember that for step 3.

## 2. Orient

Take the current phase and NEXT from the `[yah]` SessionStart block already in context. If it is missing, run `PY "${CLAUDE_SKILL_DIR}/../../scripts/where.py" --brief`. Do not read docs to orient.

## 3. Restate, then start

```
Task   <the task in one line>
Phase  <label and title, or "no plan">   NEXT <the NEXT line, or none>
Notes  <slug>: <why it matters here>, one per line, or "none apply"
First  <the first concrete step: a few tool calls, not a survey>
```

If recall printed nothing, add this line after the block, once per session: "This repo has no brain folder for durable project facts. Create one (`docs/brain` unless config.json sets `brain_dir`)?" Run `brain.py init` only if the user says yes. Never create it unasked.

Then do the first step without waiting.

If the task clearly takes several steps or sessions and there is no plan, suggest `/yah:phases` once, in one line.
