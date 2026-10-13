# The brief

/yah:auto reads this file when the project has no brief (`brief` is null in `where.py --json --no-gh` and `state_md.brief_declined` is not true), or for the task `brief`. It finds out what the user actually wants before anything is built, and writes it to BRIEF.md so every later session and run builds toward it. The user answers once, here; after this, yah decides from BRIEF.md and asks only the per-task rounds.

Run it outside plan mode: plan mode can write only the plan file. If plan mode is already on, ask the rounds anyway and put the draft under `## Brief` in the plan file ("no brief" puts `BRIEF: none` there). Then ExitPlanMode with the `## Brief` alone, and once it is approved, write it out as below at once: a plan that is skipped or never approved must not lose it, and the table must not send you back here. From /yah:auto's "Plan it" step 0, its step 1 then enters plan mode again.

`PY` is `python` on Windows and `python3` elsewhere; use `py -3` only if both fail.

## 1. Read first, ask second

Send one Explore agent this task: read README.md, the top two levels of the tree, the package manifests (package.json, pyproject.toml, Cargo.toml, go.mod and the like), `git log --oneline -20`, and BRIEF.md and the file at `store.path` (from `where.py --json --no-gh`) if they exist. Ask for 300 words or fewer: what the project is, who it seems to be for, what already works, the stack, any `AIM:` line, and what looks unfinished. Read nothing further yourself.

Then say in one line that the interview starts: up to 6 short rounds, `auto` takes the recommended answer, "skip" skips a round, "auto the rest" lets yah fill in every answer left, and "no brief" ends it.

No product in the summary (no README, no code) and no task given: before Round 1, ask one open question in plain text: in a sentence, what do they want to build, and for whom? Draft Round 1 from the answer.

The task `brief` with a BRIEF.md already there: say its AIM in one line, ask which sections changed, run only the rounds that cover them, then the mirror round. Read BRIEF.md yourself first. In section 3, edit only the sections those rounds cover, in place: keep every other section, each Roadmap line's mark and the `## Decisions` lines, and add a dated Decisions line for each change.

## 2. The rounds

Ask in AskUserQuestion rounds of at most 4 questions, at most 6 rounds, a second mirror round aside. Skip a question the Explore summary or an earlier answer already settles, and say so in one line.

Every question:
- Draft its options from what you know so far: the Explore summary, the task when there is one, and every earlier answer. Never offer generic options such as "Developers" or "Better UX". Name the person, the moment, the number.
- Put the option you would pick first, marked (Recommended). Its description gives the one-line why, resting on an earlier answer or the summary.
- Each option says what it commits the project to, so the trade-off shows.
- Other is always there for the user's own words. `auto` (or "you pick") takes the recommended option, and the brief marks it `(assumed)`. "auto the rest" does that for every question left, then goes to the mirror round. "skip" skips the round, and its sections say `(not asked)`.

**Round 1: person and problem**
- Who exactly is it for? A person the user could name, or one role in one situation ("a solo dev shipping a side project on weekends"), not "developers".
- What do they do today instead, and what is the worst part of it?
- When did that last happen, and what did they do then? Ask about past behavior, never opinions about the future.
- Why does this matter to the user: learning, income, their own itch, a portfolio, or a team need? Then, as a probe, ask why of that answer, at most twice, until it bottoms out.

**Round 2: success and evidence**
- Four weeks after v1 ships, what can they see that says it worked? A number, a behavior, or a person using it.
- What would make them stop or change course?
- How do they know the problem is real: they have it, people asked for it, they watched it happen, or a guess?
- What one thing must it do brilliantly, and what can be merely fine?

**Round 3: trade-offs and scope**
- Force a ranking of ship fast, polish, learn something and scale: if they could have only one, which?
- The smallest v1 that proves the success signal: offer 2-3 cuts drafted from the answers, smallest first.
- What is it explicitly not? Offer the tempting extensions you can see as non-goals.

**Round 4: constraints and taste**
- Time: hours a week, and a date that matters, if any.
- Money: may it use paid hosting, APIs or services, and up to what?
- Platform and stack: web, mobile, desktop, CLI or API; what it must use or avoid; and where it deploys.
- Taste: a product whose feel they want, and one they would hate it to become.

**Round 5: pre-mortem**
- "It is three months from now and this failed. What is the likeliest reason?" Offer three reasons drafted from the answers.
- Which belief, if wrong, sinks it? That is the riskiest assumption.
- What is the cheapest test of it, before or during v1? Offer drafted tests: five users, a landing page, a spike, a prototype phase.

**Round 6: the mirror**
Show the draft BRIEF.md in full in a code block. Then name every tension between answers, one question each, with resolutions as the options. Examples: "polish matters most" against "ship in a week", or "for strangers" against "evidence: I have it myself". Ask until none is left, in at most two mirror rounds. End with "Is this what you want to build?": yes, or change something.

Probes go in the next round (after round 5, the mirror), never in an extra one. Each takes the place of that round's least-needed question, which moves to a later round with room (rounds 3 and 5 ask three):
- A vague word ("simple", "fast", "users", "clean", "powerful", "scalable") gets a question that makes it concrete: how fast, which users, simple compared to what.
- An answer that names a feature instead of a problem gets "what would you do with it?"
- An answer that contradicts an earlier one is never smoothed over: it goes to the mirror round.

"no brief", at any point: write `BRIEF: none` on its own line right under the `#` title of the file at `store.path` in `where.py --json --no-gh`. That file may be NOW.md or the main checkout's STATE.md; create it with a `#` title if it is missing, and a file with no `#` title gets the line at its top. Never create a second STATE.md beside it: it would shadow the project's state. If a `BRIEF: none` line is already there, leave it. Leave any `AIM:` line as it is: it stays the project's aim. The interview is then never offered again unasked. Write no BRIEF.md, and go back to /yah:auto (the end of this file). With a BRIEF.md already there (the task `brief`), "no brief" only ends the update: change nothing, and go back.

## 3. Write BRIEF.md

Write BRIEF.md at the root of this checkout (`top`), in this shape; an update in a linked worktree with none copies the main checkout's (`brief.path`) there first. Mark guesses `(assumed)` and unasked parts `(not asked)`.

```markdown
# Brief: <project>

AIM: Help <who> <outcome> when <situation>

## Who and problem
<who, what they do today, the worst part, the last time it happened>

## Why it matters
<the user's own reason, after the whys>

## Success
- <observable signal> by <when>
- Stop if: <what would make them stop>
- Evidence the problem is real: <which kind>

## Scope
- v1 must: <the smallest cut>
- Brilliant at: <the one thing>
- Won't: <non-goals>

## Constraints
<time, money, stack, platform, deploy>

## Taste
<products to feel like, and not like>

## Risks and assumptions
- <riskiest assumption>: test with <cheapest test>

## Decisions
- <YYYY-MM-DD> <decision> because <why>

## Roadmap
- [ ] <bet>: serves <success signal> (size S/M/L)
```

Rank the Roadmap by how much each bet moves the Success signal. Put the riskiest assumption's test first when it is cheap.

Do not commit BRIEF.md here: it rides on the first plan phase's branch, where /yah:wrap stages it. Leave any `AIM:` line in the file at `store.path` as it is; BRIEF.md's AIM wins.

Then say in one line that the brief is written, and go back to /yah:auto: from "Plan it" step 0, go on with its step 1 and the task; otherwise read the routing table again with no task (after the task `brief` too), where Q2 can rank the Roadmap.
