---
name: gpt
description: Send web research, a code review, UI mockups or a UI critique to GPT. Use as /yah:gpt research <q>.
argument-hint: "research <question> | review <brief> | mockup <brief> | critique <brief> (--url <page> | <png>...)"
context: fork
agent: yah:gpt
allowed-tools: Bash(python3 *scripts/codex.py* research *), Bash(python *scripts/codex.py* research *), Bash(py -3 *scripts/codex.py* research *), Bash(python3 *scripts/codex.py* review *), Bash(python *scripts/codex.py* review *), Bash(py -3 *scripts/codex.py* review *), Bash(python3 *scripts/codex.py* mockup *), Bash(python *scripts/codex.py* mockup *), Bash(py -3 *scripts/codex.py* mockup *), Bash(python3 *scripts/codex.py* critique *), Bash(python *scripts/codex.py* critique *), Bash(py -3 *scripts/codex.py* critique *)
---

Request:

$ARGUMENTS

The forked agent cannot see the conversation that sent this, so the request above must stand alone: the question or what to review, the files or links involved, and what a good answer holds.

The first word is the mode: `review` reviews this repo, `mockup` draws UI mockups, `critique` judges UI screenshots, anything else is `research` (drop a leading `research`). The rest is the brief. For critique, take `--url <page>` or the `.png` paths out of the brief; they go on the command line, not in the heredoc. A `--url` must already be reachable (start the dev server first); it is shot at 375x812 and 1440x900.

Run codex.py with Bash as `PY "${CLAUDE_SKILL_DIR}/../../scripts/codex.py"`. `PY` is `python` on Windows and `python3` elsewhere; use `py -3` only if both fail. The brief goes on stdin (`-`) through a quoted heredoc, so quotes and `$` in it stay as written and no temp file is needed. After the command line comes the brief, then `YAH_BRIEF` on a line of its own:

- research: `PY "<codex.py>" research --timeout 570 - <<'YAH_BRIEF'`
- review: `PY "<codex.py>" review --timeout 570 "$(git rev-parse --show-toplevel)" - <<'YAH_BRIEF'`
- mockup: `PY "<codex.py>" mockup --timeout 570 - <<'YAH_BRIEF'`
- critique: `PY "<codex.py>" critique --timeout 570 --url "<page>" - <<'YAH_BRIEF'`, or with PNGs `PY "<codex.py>" critique --timeout 570 - "<a.png>" "<b.png>" <<'YAH_BRIEF'`

If codex.py says yah:gpt is off, reply that `/yah:setup` (or `setup.py --codex`) turns it on, and stop.
