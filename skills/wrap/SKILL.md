---
name: wrap
description: End-of-task routine - save the phase's NEXT (beads or STATE.md), close finished work, commit WIP, open the PR when a phase is done. Use when a task ends, when yah says wrap, or the user says wrap.
argument-hint: "[optional: what just got done]"
---

# Wrap

Goal: after this, a fresh session (`/clear`) resumes from `/yah:where` alone. State lives in beads or STATE.md. Docs, memory and chat never hold progress.

## 1. Read the state

Run with Bash, exactly:

PY "${CLAUDE_SKILL_DIR}/../../scripts/where.py" --json --no-gh

`PY` is `python` on Windows and `python3` elsewhere. If it is not found or fails, try the other, then `py -3`, and keep whichever works.

Note `beads.plan` (its `source` is `beads`, `STATE.md` or `NOW.md`), `beads.phase` (label, branch, id), `git.branch`, `git.dirty`, `state_md`, `protected` and `bd`. Run beads commands as the `bd` path, quoted; `bd` null means the CLI is missing: say so and use **2b**.
- Plan from beads, or no plan but an in_progress bead: use **2a** (treat that bead as the phase).
- Plan from STATE.md/NOW.md, or no beads at all: use **2b**.

## 2a. Beads: rewrite the phase bead's notes

Replace the notes. Never append; they are a pointer, not a diary.

bd update <phase-id> --notes "<NEXT line>
<up to 3 short lines: what is done, what is half-done and where, any trap>"

- The first line is what `/yah:where` prints as NEXT: one concrete action someone could start cold, at most 140 characters. Example: "Run make eval, then paste the table into PR #9".
- Close finished task beads: `bd close <id> --reason "<one line>"`.
- A new follow-up becomes a bead (`bd create "<title>" --parent <epic> --silent`), never prose.

## 2b. STATE.md: write today's entry

Edit STATE.md (or NOW.md) at the repo root; create STATE.md if neither exists.
- Add a `## YYYY-MM-DD` entry for today (or rewrite today's), with `- Next: <NEXT line>` first and at most 3 short lines after it. The NEXT rules above apply.
- In the `## Plan:` section, keep the checkboxes true: `[x]` done, `[~]` current, `[ ]` open, with `| branch X | base Y | PR #N` fields.
- Keep only the 5 newest dated entries; delete older ones. Leave anything else in the file alone.

## 3. Brain note (only if the brain folder exists)

The folder is `docs/brain` at the repo root, unless yah's config sets `brain_dir`. No folder: skip this step.

Ask: did this session establish a durable project fact or decision a future session would otherwise rediscover (a constraint, convention, gotcha, or decision with its reason)? Progress belongs in NEXT; personal preferences belong in memory. If no, skip. If yes, write at most one note.

Run `PY "${CLAUDE_SKILL_DIR}/../../scripts/brain.py" find <key words>`, then:
- A listed note already holds the fact: edit it in place only if a detail changed, then run `brain.py index`.
- The fact replaces a listed note: `brain.py new ... --supersedes <old-slug>`. Never delete a note.
- Otherwise: `brain.py new --title "<fact>" --tldr "<one line, 200 chars or fewer>" --tags a,b --paths "<globs>" --source "<evidence>"`.

The source must be evidence: a PR, commit sha, issue, URL, `path/to/file:line`, or `user, YYYY-MM-DD` when the user stated it. A fact you inferred gets no `--source`; it goes to `_pending.md` for the user to review.

Stage what brain.py wrote (the note and INDEX.md, or `_pending.md`) with the WIP commit.

## 4. Commit work in progress

Only if `git.dirty` > 0 or step 3 wrote a note.
- Never commit on a branch in `protected` (trunks, the PROD branch, and where open PRs land). If you are on one, stop and tell the user.
- Run `git status`, then stage files by name. Never stage `.env*`, credentials, or large generated files the repo does not already track. Stage STATE.md only if the repo already tracks it.
- `git commit -m "wip(<phase label>): <what>"`. Do not push unless step 6 applies.

## 5. Memory

Save only durable, non-obvious lessons: a trap, a user preference, a fact that cost time to learn. Progress and status never go into memory. Update an existing memory rather than add a duplicate.

If this project's auto-memory index (`MEMORY.md` in its memory folder under `$CLAUDE_CONFIG_DIR` or `~/.claude`) is over 100 lines, say so and offer to prune it.

## 6. Only if the phase is complete

The phase's done-when is met and the tests pass.
1. Review the branch diff: run `/code-review --fix`, then `/simplify`, if available. Re-run the tests.
2. Push the feature branch. Never push a branch in `protected`.
3. Open the PR into the phase's base (`base`, else the plan's target branch), following the project's title convention. If the base is another phase's branch, say so in the body.
4. Record it. Beads: `bd update <phase-id> --external-ref gh-<N>`, then `bd close <phase-id> --reason "PR #<N> open, checks <state>"`. STATE.md: mark the phase `[x]` and add `| PR #<N>`.
5. Claim the next phase and give it a NEXT line. Beads: `bd update <next-id> --claim`. STATE.md: mark it `[~]`.

Merging is always the user's.

## 7. Finish with exactly this

```
Saved   <phase label> NEXT = <the NEXT line>
Commit  <short sha or none>   PR <#N or none>
Safe to /clear. The next session starts from /yah:where; just say "continue".
```
