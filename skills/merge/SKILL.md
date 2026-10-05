---
name: merge
description: Merge the plan's green phase PRs now, bottom-up, through the yah run driver.
disable-model-invocation: true
allowed-tools: Bash(python3 *scripts/run.py* --merge), Bash(python *scripts/run.py* --merge), Bash(py -3 *scripts/run.py* --merge)
---

Run this with the Bash tool, exactly as written, in a Bash call of its own:

PY "${CLAUDE_SKILL_DIR}/../../scripts/run.py" --merge

`PY` is `python` on Windows and `python3` elsewhere; use `py -3` only if both fail. Add nothing after `--merge`, not even `--cwd` or `--project`: the allow rule that `setup.py --auto-merge` installs ends there, so auto mode denies any other form.

It walks the plan's phase PRs from the bottom of the stack up. It merges each one whose phase is closed, whose checks passed on its head and that GitHub calls mergeable, with a judge pass on that head when the quality profile is on, and retargets the PR stacked on it. It stops at the first that cannot merge. Typing `/yah:merge` is the consent, so it works with `auto_merge` off.

Show the output verbatim in a code block. After it, add at most one sentence:
- Exit 8: what merged, and what waits where it stopped.
- Exit 0: what holds the bottom PR back, with the step that clears it as a command to paste in a terminal when there is one (`yah run P<n>` judges a head with no judge pass).
- Exit 5 (`run refused`) or 2: what it printed, then stop.

Never run `gh pr merge` yourself, never retry in another form, and never start a run or a judge to get a PR merged. If the call is denied, give the user `yah run --merge` to paste in their own terminal.
