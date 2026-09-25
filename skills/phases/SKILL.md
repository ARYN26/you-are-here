---
name: phases
description: Track a multi-phase plan in STATE.md or beads. Use right after a plan is approved.
argument-hint: "[plan path]"
---

# Phases

Input: the plan file in `$ARGUMENTS`. Otherwise use the plan approved in this session, or else the newest file in `~/.claude/plans` that matches the current work. Read only its phase list.

`PY` is `python` on Windows and `python3` elsewhere; use `py -3` only if both fail.

Run `PY "${CLAUDE_SKILL_DIR}/../../scripts/where.py" --json --no-gh` and read its `beads_source` and `bd` fields. STATE.md is the default. Use **beads** only if the repo already uses it: `beads_source` is set (a `.beads/` at this repo's root), `bd` is a path, and neither STATE.md nor NOW.md has a `## Plan:` section (a STATE.md plan wins over a beads epic). Run beads commands as that path, quoted. Never set, export or follow `BEADS_DIR`. If `.beads/` exists but `bd` is null, tell the user `bd_note`, or that the bd CLI was not found, before using **STATE.md**.

## STATE.md

Edit STATE.md at the repo root (create it if missing; keep any dated entries). If a `## Plan:` section already names this plan path, replace it; otherwise add one at the top:

```markdown
## Plan: <plan name> (<plan path>)
- [x] P1 <title> | branch <branch> | PR #<N>
- [~] P2 <title> | branch <branch> | base <PR base branch>
  - [ ] <sub-task>
- [ ] P3 <title>

## <today, YYYY-MM-DD>
- Next: <the first concrete action of the current phase>
- At: <git rev-parse --short HEAD>
```

Phases are the unindented checkbox lines in `## Plan:`: `[x]` done, `[~]` current (exactly one top-level `[~]`), `[ ]` open. Fields are optional and separated by `|`. `/yah:where` follows the plan with a `[~]` phase, else the first with an open phase, so a finished plan can stay below.

An indented checkbox line under a phase is a sub-task: not a phase, and not counted in done/total. A `[~]` line anywhere else (a sub-task, or under `## Follow-ups`) is work in progress, not a phase; with no plan, `/yah:where` shows it as DOING.

A step only the user can do (a review, a merge, a console step) is its own line that ends with `(you)`, as a phase, a sub-task or under `## Follow-ups`. `/yah:where` lists the open ones as waiting on the user.

`- At:` stamps NEXT so `/yah:where` can flag it once commits land after it; leave it out if the repo tracks STATE.md. Tell the user STATE.md is not committed unless they want it tracked; offer to add it to `.gitignore`.

## Beads (only if the repo already uses it)

1. **Look for an existing epic:** `bd list --type epic --all --json --limit 0`. Match `spec_id` against the plan path. If one exists, update it (add missing phases, fix refs) instead of creating a duplicate.
2. **Create the epic:** `bd create "<plan name>" -t epic -l plan --spec-id "<plan path>" --silent`
3. **One child per phase, in order:**
   `bd create "<phase title>" --parent <epic> -l phase --metadata '{"phase":<n>,"branch":"<branch>","base":"<PR base branch>","next_sha":"<HEAD>"}' --silent`
   - `<HEAD>` is `git rev-parse HEAD`, run once. It stamps NEXT so `/yah:where` can flag it once the phase branch has commits NEXT predates.
   - Only the current phase gets `--notes "<NEXT: the first concrete action>"`.
   - Already shipped as a PR: add `--external-ref gh-<N>`, then `bd close <id> --reason "PR #<N> open"`.
   - A step only the user can do (a review, a merge, a console step) is its own child with `-l human` and a title that starts with a verb.
4. **Claim the current phase:** `bd update <id> --claim`.

## Check the result

Run `PY "${CLAUDE_SKILL_DIR}/../../scripts/where.py"` and show its output. The PLAN, PHASE and NEXT lines must be right, with no `!` line about an ignored beads epic. Fix STATE.md or the beads if they are not.

Keep phase text short. The plan file holds the detail (goals, done-when, ground rules); STATE.md or beads hold only state and pointers.
