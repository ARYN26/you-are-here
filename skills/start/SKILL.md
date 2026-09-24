---
name: start
description: Start a task with its context - recall the project brain notes that apply, restate the phase and NEXT, then begin. Use as /yah:start <task> at the start of a task.
argument-hint: "<the task>"
allowed-tools: Bash(python3 *scripts/brain.py*), Bash(python *scripts/brain.py*), Bash(py -3 *scripts/brain.py*), Bash(python3 *scripts/where.py*), Bash(python *scripts/where.py*), Bash(py -3 *scripts/where.py*)
---

# Start

The task: $ARGUMENTS

## 1. Recall

Run with Bash, with the task in place of `<task>` (drop any double quotes from it):

python3 "${CLAUDE_SKILL_DIR}/../../scripts/brain.py" recall --json "<task>"

If `python3` is not found or fails (as on Windows), run the same command with `python`, then `py -3`, and use whichever works for every later command.

- **JSON printed:** the brain exists. `matches` are the notes that fit, best first; `also` lists related slugs. Open a note (`<brain_dir>/<slug>.md`) only when its TL;DR is not enough for the task.
- **Nothing printed:** this repo has no brain folder. Say so once: "This repo has no brain folder for durable project facts. Create one (`docs/brain` unless config.json sets `brain_dir`)?" Run `brain.py init` only if the user says yes, then go on. Never create it unasked, and do not ask again this session.

## 2. Orient

Take the current phase and NEXT from the `[yah]` SessionStart block already in context. If it is missing, run `python3 "${CLAUDE_SKILL_DIR}/../../scripts/where.py" --brief` (same fallback). Do not read docs to orient.

## 3. Restate, then start

```
Task   <the task in one line>
Phase  <label and title, or "no plan">   NEXT <the NEXT line, or none>
Notes  <slug>: <why it matters here>, one per line, or "none apply"
First  <the first concrete step>
```

Then do that first step without waiting.

If the task clearly takes several steps or sessions and there is no plan, suggest `/yah:phases` once, in one line.
