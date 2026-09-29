---
name: gpt
description: Send multi-source web research or a read-only code review to GPT. Use as /yah:gpt research <q>.
argument-hint: "research <self-contained question> | review <self-contained brief>"
context: fork
agent: yah:gpt
allowed-tools: Bash(python3 *scripts/codex.py* research *), Bash(python *scripts/codex.py* research *), Bash(py -3 *scripts/codex.py* research *), Bash(python3 *scripts/codex.py* review *), Bash(python *scripts/codex.py* review *), Bash(py -3 *scripts/codex.py* review *)
---

Request:

$ARGUMENTS

The forked agent cannot see the conversation that sent this, so the request above must stand alone: the question or what to review, the files or links involved, and what a good answer holds.

The first word is the mode: `review` reviews this repo, anything else is `research` (drop a leading `research`). The rest is the brief.

Run codex.py with Bash as `PY "${CLAUDE_SKILL_DIR}/../../scripts/codex.py"`. `PY` is `python` on Windows and `python3` elsewhere; use `py -3` only if both fail. Pass the brief through a quoted heredoc, so quotes and `$` in it stay as written:

- research: `PY "<codex.py>" research --timeout 570 "$(cat <<'YAH_BRIEF'` then the brief, then `YAH_BRIEF` and `)"` on lines of their own.
- review: write the brief to a temp file (`f=$(mktemp)`, then `cat > "$f" <<'YAH_BRIEF'` ... `YAH_BRIEF`), then `PY "<codex.py>" review --timeout 570 "$(git rev-parse --show-toplevel)" "$f"`.

If codex.py says yah:gpt is off, reply that `/yah:setup` (or `setup.py --codex`) turns it on, and stop.
