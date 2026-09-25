---
name: wrap
description: Save NEXT, commit WIP, open the PR when a phase is done. Use when a task ends or on "wrap".
argument-hint: "[what got done]"
---

# Wrap

Goal: after this, a fresh session (`/clear`) resumes from `/yah:where` alone. State lives in STATE.md, or in beads if the repo already uses it. Docs, memory and chat never hold progress.

## 1. Read the state

Run with Bash, exactly:

PY "${CLAUDE_SKILL_DIR}/../../scripts/where.py" --json --no-gh

`PY` is `python` on Windows and `python3` elsewhere; use `py -3` only if both fail.

Note `beads.plan` (its `source` is `beads`, `STATE.md` or `NOW.md`), `beads.phase` (label, branch, id), `beads_source`, `git.branch`, `git.dirty`, `state_md`, `protected`, `bd` and `next_stale`.

`next_stale` set means commits landed after NEXT was written, so it may already be done. Run `git log --oneline <next_stale.sha>..<next_stale.branch>` and write the new NEXT from where those commits leave off.

STATE.md is the default. Use beads only when the repo already uses it: `beads` is non-null, `beads_source` is set (a `.beads/` at this repo's root) and `bd` is a path; run beads commands as that path, quoted. Never set, export or follow `BEADS_DIR`, and never run bd against a database outside this repo.
- `bd` is a path, and the plan comes from beads or there is no plan but an in_progress bead: use **2b** (treat that bead as the phase).
- Anything else (a plan from STATE.md/NOW.md, no beads DB here, or `bd` null): use **2a**. If `.beads/` exists but `bd` is null, say why first: print `bd_note`, or say the bd CLI is missing.

## 2a. STATE.md: write today's entry

Edit STATE.md (or NOW.md) at the repo root; create STATE.md if neither exists.
- Add a `## YYYY-MM-DD` entry for today (or rewrite today's), with `- Next: <NEXT line>` first and at most 3 short lines after it: what is done, what is half-done and where, any trap.
- The NEXT line is what `/yah:where` prints as NEXT: one concrete action someone could start cold, at most 140 characters. Example: "Run make eval, then paste the table into PR #9".
- In the `## Plan:` section, keep the checkboxes true: `[x]` done, `[~]` current, `[ ]` open, with `| branch X | base Y | PR #N` fields.
- A new follow-up becomes a `- [ ] <title>` line under `## Follow-ups` (add the section if missing), never prose. Tick finished ones `[x]` or delete them.
- A step only the user can do (a review, a merge, a console step) ends with `(you)`. `/yah:where` lists the open ones as waiting on the user.
- Keep only the 5 newest dated entries; delete older ones. Leave anything else in the file alone.

## 2b. Beads: rewrite the phase bead's notes

Replace the notes. Never append; they are a pointer, not a diary.

bd update <phase-id> --notes "<NEXT line>
<up to 3 short lines: what is done, what is half-done and where, any trap>"

- The NEXT rules in 2a apply to the first line.
- Close finished task beads: `bd close <id> --reason "<one line>"`.
- A new follow-up becomes a bead (`bd create "<title>" --parent <epic> --silent`), never prose. A step only the user can do gets `-l human`.

## 3. Brain note (only if the brain folder exists)

The folder is `docs/brain` at the repo root, unless yah's config sets `brain_dir`. No folder: skip this step.

Ask: did this session establish a durable project fact or decision a future session would otherwise rediscover (a constraint, convention, gotcha, or decision with its reason)? Progress belongs in NEXT; personal preferences belong in memory. If no, skip. If yes, write at most one note.

Run `PY "${CLAUDE_SKILL_DIR}/../../scripts/brain.py" find <key words>`, then:
- A listed note already holds the fact: edit it in place only if a detail changed, then run `brain.py index`.
- The fact replaces a listed note: `brain.py new ... --supersedes <old-slug>`. Never delete a note.
- Otherwise: `brain.py new --title "<fact>" --tldr "<one line, 200 chars or fewer>" --tags a,b --paths "<globs>" --source "<evidence>"`.

The source must be evidence: a PR, commit sha, issue, URL, `path/to/file:line`, or `user, YYYY-MM-DD` when the user stated it. A fact you inferred gets no `--source`; it goes to `_pending.md` for the user to review.

Stage what brain.py wrote (the note and INDEX.md, or `_pending.md`) with the WIP commit.

## 4. Commit work in progress, then stamp NEXT

Commit only if `git.dirty` > 0, step 3 wrote a note, or step 2a changed a STATE.md the repo tracks.
- Never commit on a branch in `protected` (trunks, the PROD branch, and where open PRs land). If you are on one, stop and tell the user.
- Run `git status`, then stage files by name. Never stage `.env*`, credentials, or large generated files the repo does not already track. Stage STATE.md only if the repo already tracks it.
- `git commit -m "wip(<phase label>): <what>"`. Do not push unless step 6 applies.

Always stamp NEXT with the commit it was written at, so `/yah:where` flags it once later commits make it stale:
- STATE.md or NOW.md the repo does not track: put `- At: <git rev-parse --short HEAD>` right under `- Next:`. A tracked one needs no stamp; its own commit is the stamp.
- Beads: `bd update <phase-id> --set-metadata next_sha=$(git rev-parse HEAD)`.

## 5. Memory

Save only durable, non-obvious lessons: a trap, a user preference, a fact that cost time to learn. Progress and status never go into memory. Update an existing memory rather than add a duplicate.

If this project's auto-memory index (`MEMORY.md` in its memory folder under `$CLAUDE_CONFIG_DIR` or `~/.claude`) is over 100 lines, say so and offer to prune it.

## 6. Only if the phase is complete

The phase's done-when is met and the tests pass.
1. Review the branch diff: run `/code-review --fix`, then `/simplify`, if available. Re-run the tests.
2. Push the feature branch. Never push a branch in `protected`.
3. Open the PR into the phase's base (`base`, else the plan's target branch), following the project's title convention. If the base is another phase's branch, say so in the body.
4. Record it. STATE.md: mark the phase `[x]` and add `| PR #<N>`. Beads: `bd update <phase-id> --external-ref gh-<N>`, then `bd close <phase-id> --reason "PR #<N> open, checks <state>"`.
5. Claim the next phase and give it a NEXT line, stamped as in step 4. STATE.md: mark it `[~]`. Beads: `bd update <next-id> --claim`.

Merging is always the user's.

## 7. Finish with exactly this

```
Saved   <phase label> NEXT = <the NEXT line>
Commit  <short sha or none>   PR <#N or none>
Safe to /clear. Then /yah:auto, or `yah <project>` from a terminal, picks up from NEXT.
```
