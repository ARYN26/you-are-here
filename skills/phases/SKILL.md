---
name: phases
description: Track a multi-phase plan in STATE.md or beads. Use right after a plan is approved.
argument-hint: "[plan path]"
---

# Phases

Input: the plan file in `$ARGUMENTS`. Otherwise use the plan approved in this session, or else the newest file in `~/.claude/plans` that matches the current work. Read only its phase list and ground rules.

`PY` is `python` on Windows and `python3` elsewhere; use `py -3` only if both fail.

Run `PY "${CLAUDE_SKILL_DIR}/../../scripts/where.py" --json --no-gh` and read its `bd` field. STATE.md is the default. Use **beads** only when the repo already uses it: `.beads/` exists at the repo root and `bd` is a path; run beads commands as that path, quoted. Never set, export or follow `BEADS_DIR`. If `.beads/` exists but `bd` is null, tell the user `bd_note`, or that the bd CLI was not found, before using **STATE.md**.

## STATE.md

Edit STATE.md at the repo root (create it if missing; keep any dated entries). If a `## Plan:` section already names this plan path, replace it; otherwise add one at the top:

```markdown
## Plan: <plan name> (<plan path>)
- [x] P1 <title> | branch <branch> | PR #<N>
- [~] P2 <title> | branch <branch> | base <PR base branch>
- [ ] P3 <title>

## <today, YYYY-MM-DD>
- Next: <the first concrete action of the current phase>
- At: <git rev-parse --short HEAD>
```

`[x]` done, `[~]` current (exactly one in the file), `[ ]` open. Fields are optional and separated by `|`. `/yah:where` follows the plan with a `[~]` phase, else the first with an open phase, so a finished plan can stay below.

A step only the user can do (a review, a merge, a console step) is its own line that ends with `(you)`, as a phase or under `## Follow-ups`. `/yah:where` lists the open ones as waiting on the user.

`- At:` stamps NEXT so `/yah:where` can flag it once commits land after it; leave it out if the repo tracks STATE.md. Tell the user STATE.md is not committed unless they want it tracked; offer to add it to `.gitignore`.

## Beads (a repo that already uses it)

1. **Look for an existing epic:** `bd list --type epic --all --json --limit 0`. Match `spec_id` against the plan path. If one exists, update it (add missing phases, fix refs) instead of creating a duplicate.
2. **Create the epic:**
   `bd create "<plan name>" -t epic -l plan --spec-id "<plan path>" --metadata '{"short":"<tag of 8 chars or fewer, e.g. CHECKOUT>"}' --design "<the plan's ground rules and traps, 12 lines or fewer>" --silent`
   If the repo names epics by slug, look at existing ids and pass `--id <prefix>-<slug>`.
3. **One child per phase, in order:**
   `bd create "<phase title>" --parent <epic> -l phase --metadata '{"phase":<n>,"branch":"<branch>","base":"<PR base branch>","next_sha":"<HEAD>"}' --description "<goal; done when ...; 4 lines or fewer>" --notes "<NEXT: the first concrete action>" --silent`
   - `<HEAD>` is `git rev-parse HEAD`, run once. It stamps NEXT so `/yah:where` can flag it once the phase branch has commits NEXT predates.
   - Already shipped as a PR: add `--external-ref gh-<N>`, then `bd close <id> --reason "PR #<N> open"`.
   - Order: `bd dep add <phase n+1> <phase n>`.
   - A step only the user can do (a review, a merge, a console step) is its own child with `-l human` and a title that starts with a verb.
   - Reparent existing beads with `bd update <old> --parent <phase-id>` only when the match is unambiguous.
4. **Claim the current phase:** `bd update <id> --claim`.

## Check the result

Run `PY "${CLAUDE_SKILL_DIR}/../../scripts/where.py"` and show its output. The PLAN, PHASE and NEXT lines must be right. Fix STATE.md or the beads if they are not.

Keep phase text short. The plan holds the detail; STATE.md or beads hold state and pointers.
