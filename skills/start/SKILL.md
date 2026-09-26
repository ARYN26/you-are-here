---
name: start
description: Start a task - restate phase and NEXT, recall notes, begin. Use as /yah:start <task>.
argument-hint: "<task>"
allowed-tools: Bash(python3 *scripts/brain.py* recall *), Bash(python *scripts/brain.py* recall *), Bash(py -3 *scripts/brain.py* recall *), Bash(python3 *scripts/brain.py* find *), Bash(python *scripts/brain.py* find *), Bash(py -3 *scripts/brain.py* find *), Bash(python3 *scripts/where.py*), Bash(python *scripts/where.py*), Bash(py -3 *scripts/where.py*)
---

# Start

The task: $ARGUMENTS

`PY` is `python` on Windows and `python3` elsewhere; use `py -3` only if both fail.

## 1. Restate first

Your first output is this block, with zero tool calls before it. Take Phase and NEXT from the `[yah]` SessionStart block already in context.

```
Task   <the task in one line>
Phase  <label and title, or "no plan">   NEXT <the NEXT line, or none>
First  <at most one targeted search, then the edit>
```

No `[yah]` block: write `Phase  unknown`.

The `[yah]` block and any task text already in context are enough: never re-read STATE.md or run `bd show` or `which bd` for them. If bd is truly needed, run `store.bd` from `where.py --json`, quoted (null: skip bd). Do not read `docs/`, `documents/`, whole plans or files over 200 lines to orient.

## 2. Recall

With a plan phase (or `unknown`), run `PY "${CLAUDE_SKILL_DIR}/../../scripts/where.py" --json --no-gh`. Read the phase's `log`, then only the `## Decisions` and `### <label>` sections of `state.plan.spec` (`grep -n "^#"` finds them; none: say so in one line).

Query with the task's key nouns, 12 words or fewer, no double quotes:

PY "${CLAUDE_SKILL_DIR}/../../scripts/brain.py" recall --json "<key nouns>"

- **JSON printed:** `matches` are the notes that fit, best first. Add `Notes  <slug>: <why it matters here>` per note that applies. Open `<brain_dir>/<slug>.md` only when its TL;DR is not enough.
- **Nothing printed:** add this line once per session: "This repo has no brain folder for durable project facts. Create one (`docs/brain` unless config.json sets `brain_dir`)?" Run `brain.py init` only if the user says yes.

## 3. Begin

Do the First step without waiting.

A bug fix adds a negative regression guard in every test suite that covers the changed code: assert the bad output cannot appear (e.g. `assertNotIn("**", out)`), not only that the good output does.

If the task clearly takes several steps or sessions and there is no plan, suggest `/yah:phases` once, in one line.
