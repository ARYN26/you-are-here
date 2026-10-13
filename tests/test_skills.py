"""Tests for the skill and agent files. Stdlib unittest only:

    python -m unittest tests.test_skills

Every skill and agent description is in the model's context on every turn, so these tests keep
the listings short and the start, wrap and resume bodies to the rules the real-run test found.
They also keep beads frozen: STATE.md is the plan state, and beads only a repo that already uses it.
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = sorted(ROOT.glob("skills/*/SKILL.md"))
AGENTS = sorted(ROOT.glob("agents/*.md"))
MAX_DESC = 100
MAX_VISIBLE = 740  # 588 before P1 of yah + GPT added the gpt skill and agent
START_BYTES = 2500  # the restate-first rewrite came in under 2394; P2's phase-memory read adds about 100


def parse(path):
    """(frontmatter dict, body) from a --- delimited file; values lose one layer of quotes."""
    text = path.read_text("utf-8")
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n(.*)", text, re.S)
    if not m:
        raise AssertionError(f"no frontmatter in {path}")
    meta = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith(" "):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            meta[key.strip()] = value
    return meta, m.group(2)


def model_visible(path):
    meta, _ = parse(path)
    return path in AGENTS or meta.get("disable-model-invocation", "").lower() != "true"


def body(name):
    return parse(ROOT / "skills" / name / "SKILL.md")[1]


class DescriptionTests(unittest.TestCase):
    def test_files_found(self):
        self.assertEqual({p.parent.name for p in SKILLS},
                         {"auto", "deep", "gpt", "phases", "resume", "setup", "start", "tree", "where", "wrap"})
        self.assertEqual({p.stem for p in AGENTS}, {"deep", "gpt", "scout"})

    def test_each_description_short(self):
        for f in SKILLS + AGENTS:
            with self.subTest(f=str(f.relative_to(ROOT))):
                desc = parse(f)[0].get("description", "")
                self.assertTrue(desc)
                self.assertLessEqual(len(desc), MAX_DESC)

    def test_model_visible_total(self):
        visible = [f for f in SKILLS + AGENTS if model_visible(f)]
        self.assertEqual(len(visible), 9)  # 6 skills + 3 agents; auto, resume, setup and tree are user-only
        total = sum(len(parse(f)[0]["description"]) for f in visible)
        self.assertLessEqual(total, MAX_VISIBLE)

    def test_no_yaml_breaking_colon(self):
        # ": " inside a plain scalar ends the key and breaks the frontmatter
        for f in SKILLS + AGENTS:
            with self.subTest(f=str(f.relative_to(ROOT))):
                self.assertNotIn(": ", parse(f)[0]["description"])

    def test_trigger_words_kept(self):
        want = {
            ROOT / "skills/where/SKILL.md": ["where am I", "what's next"],
            ROOT / "skills/wrap/SKILL.md": ["wrap", "when a task ends"],
            ROOT / "skills/deep/SKILL.md": ['"deep"', "two attempts"],
            ROOT / "skills/phases/SKILL.md": ["approved"],
            ROOT / "skills/start/SKILL.md": ["/yah:start"],
            ROOT / "agents/deep.md": ["/yah:deep", "two attempts"],
            ROOT / "agents/scout.md": ["proactively", "read-only", "lookups"],
            ROOT / "skills/gpt/SKILL.md": ["web research", "review", "/yah:gpt research"],
            ROOT / "agents/gpt.md": ["/yah:gpt", "codex.py"],
        }
        for f, words in want.items():
            desc = parse(f)[0]["description"]
            for w in words:
                self.assertIn(w, desc, f"{w!r} missing from {f.relative_to(ROOT)}")

    def test_no_listing_offers_beads(self):
        # the listing is in context every turn; beads is only "if you already use it"
        for f in SKILLS + AGENTS:
            with self.subTest(f=str(f.relative_to(ROOT))):
                self.assertNotIn("beads", parse(f)[0]["description"].lower())


class StartTests(unittest.TestCase):
    def setUp(self):
        self.body = body("start")
        self.block = self.body.find("\nTask   <")

    def test_restate_block_before_any_command(self):
        self.assertGreater(self.block, 0)
        recall = self.body.find("brain.py\" recall")
        self.assertGreater(recall, self.block)
        self.assertGreater(self.body.find("scripts/"), self.block)  # no tool call before the restate
        self.assertIn("zero tool calls before it", self.body)
        self.assertIn("Phase  unknown", self.body)

    def test_recall_query_is_short(self):
        self.assertIn("12 words or fewer", self.body)
        self.assertNotIn('recall --json "<task>"', self.body)  # the whole task text was the query

    def test_no_state_lookup_when_the_block_is_given(self):
        self.assertIn("The `[yah]` block and any task text already in context are enough", self.body)
        self.assertIn("never re-read STATE.md or run `bd show` or `which bd`", self.body)
        self.assertIn("where.py --json", self.body)
        self.assertNotIn("Bead or task text", self.body)  # the old bd-only wording

    def test_negative_regression_guard(self):
        self.assertIn("negative regression guard", self.body)
        self.assertIn("every test suite", self.body)

    def test_kept_parts_and_size(self):
        meta, _ = parse(ROOT / "skills/start/SKILL.md")
        self.assertIn("scripts/brain.py* recall", meta["allowed-tools"])
        self.assertIn("scripts/where.py", meta["allowed-tools"])
        self.assertIn("no brain folder", self.body)
        self.assertIn("/yah:phases", self.body)
        body = (ROOT / "skills/start/SKILL.md").read_bytes().replace(b"\r\n", b"\n")  # autocrlf-proof
        self.assertLess(len(body), START_BYTES)


class RunnerTests(unittest.TestCase):
    def test_no_python_fallback_dance(self):
        for f in SKILLS:
            with self.subTest(f=f.parent.name):
                text = f.read_text("utf-8")
                self.assertNotIn("keep whichever works", text)
                if "`PY` is" in text:
                    self.assertIn("`py -3` only if both fail", text)

    def test_push_runs_alone_since_deny_globs_match_the_whole_line(self):
        # E2E 2026-09-26: `git push ... && gh pr create --body "$(cat <<'EOF' ...)"` with a `+` in the body hit
        # `Bash(git push *+*)`, since a line Claude Code cannot split is matched whole
        self.assertIn('"Bash(git push *+*)"', (ROOT / "scripts/run.py").read_text("utf-8"))
        self.assertIn("as a Bash call of its own: nothing chained before or after it, not even `&& gh pr create`",
                      body("resume"))
        self.assertIn("Push the feature branch in a Bash call of its own, with nothing chained to it", body("wrap"))


class StampTests(unittest.TestCase):
    def test_wrap_stamps_next_and_reads_the_stale_flag(self):
        text = body("wrap")
        self.assertIn("--set-metadata next_sha=$(git rev-parse HEAD)", text)
        self.assertIn("- At: <git rev-parse --short HEAD>", text)
        self.assertIn("Always stamp NEXT", text)  # not under the commit-only-if-dirty condition
        self.assertIn("next_stale", text)
        self.assertIn("stamped as in step 4", text)  # the next phase's NEXT too

    def test_wrap_finish_line_points_an_open_phase_at_autopilot(self):
        text = body("wrap")
        finish = text.split("## 7. Finish with exactly this", 1)[1]
        self.assertIn("when a plan phase is still open", finish)
        self.assertIn("hands <phase label> to autopilot (a detached `yah run --plan`)", finish)
        self.assertIn("`/yah:auto here` works it by hand", finish)
        self.assertIn("With no plan phase open", finish)
        self.assertEqual(finish.count("Safe to /clear."), 2)  # one line per case, both still say it
        # every line of a fenced block is literal: no nested backticks around a whole finish line
        self.assertNotIn("`Safe to /clear", finish)

    def test_wrap_starts_the_run_only_when_it_is_safe(self):
        text = body("wrap")
        step = text.split("## 6.5 Hand the open phase to a run", 1)[1].split("## 7.", 1)[0]
        self.assertIn("Skip this step under `/yah:resume`", step)  # a run's session never starts a run
        self.assertIn("does not end with `(you)`", step)
        self.assertIn("NEXT does not start with `NEEDS-HUMAN:`", step)
        self.assertIn("`run.alive` is not true", step)
        self.assertIn("`git.dirty` is 0", step)
        self.assertIn("not counting a STATE.md the repo does not track", step)  # wrap never stages an untracked one
        self.assertIn("never restart a run unasked", step)  # a run the user ended stays ended
        self.assertIn('run `PY "${CLAUDE_SKILL_DIR}/../../scripts/run.py" --plan --detach` in a Bash call of its own', step)
        self.assertIn("Any other exit: show its output", step)
        finish = text.split("## 7. Finish with exactly this", 1)[1]
        self.assertIn("The run has <phase label>; this session can close.", finish)

    def test_wrap_hands_a_phase_pr_to_the_run_when_auto_merge_is_on(self):
        text = body("wrap")
        self.assertIn("`next_stale` and `auto_merge`", text)  # step 1 reads the repo's effective setting
        step = text.split("## 6.5 Hand the open phase to a run", 1)[1].split("## 7.", 1)[0]
        # the run merges the PR this wrap opened, even with no phase left; the other rules still hold
        hand = "Or `auto_merge` is true and this wrap opened or updated a plan phase's PR, even with no phase left open"
        self.assertIn(hand, step)
        self.assertIn("the run merges that PR, then goes on to any next phase", step)
        self.assertLess(step.index("never restart a run unasked"), step.index(hand))
        self.assertLess(step.index(hand), step.index("NEXT does not start with `NEEDS-HUMAN:`"))
        finish = text.split("## 7. Finish with exactly this", 1)[1]
        self.assertIn("It merges PR #<N> when it is green.", finish)
        self.assertIn("With `auto_merge` true and a phase PR open that has no `(you)` merge follow-up, keep the "
                      "middle sentence", finish)

    def test_wrap_never_commits_a_lone_brief_on_a_protected_branch(self):
        text = body("wrap")
        step = text.split("## 4. Commit work in progress", 1)[1].split("## 5.", 1)[0]
        stop = "Never commit on a branch in `protected` (trunks, the PROD branch, and where open PRs land). " \
               "If you are on one, stop and tell the user."
        self.assertIn(stop, step)  # the rule holds for everything but a lone BRIEF.md
        lone = "But a new or changed BRIEF.md alone is no reason to commit or to stop there: leave it in the tree, " \
               "and it rides on the next phase's branch."
        self.assertIn(lone, step)
        self.assertLess(step.index(stop), step.index(lone))
        self.assertIn("On a feature branch, stage a new or changed BRIEF.md with the rest, by name", step)
        hand = text.split("## 6.5 Hand the open phase to a run", 1)[1].split("## 7.", 1)[0]
        self.assertIn("not counting a STATE.md the repo does not track or a new or changed BRIEF.md (it rides on the "
                      "phase branch)", hand)
        # the plan's last phase ticks its Roadmap line before the push, so the PR carries it
        done = text.split("## 6. Only if the phase is complete", 1)[1].split("## 6.5", 1)[0]
        tick = done.index("No phase left in this plan after this one, and the plan served a BRIEF.md Roadmap line")
        self.assertLess(tick, done.index("Push the feature branch"))
        self.assertIn("mark that line `[x]` in the BRIEF.md at `top` (in a linked worktree with none, copy "
                      "`brief.path` there first) and commit it with this phase", done)
        self.assertIn("named in its `## Bet`", done)
        resume = body("resume").split("## 4. Test, then wrap", 1)[1].split("## 5.", 1)[0]
        self.assertIn("A new or changed BRIEF.md in the tree is the project's brief: wrap stages it with this slice's "
                      "commit (the phase branch is never protected).", resume)

    def test_a_branch_switch_carries_brief_md(self):
        """BRIEF.md is uncommitted, or on one phase branch only, until a phase PR lands it on the trunk: a switch to a
        branch without it must neither stop on it (blocked dirty tree) nor drop it (the brief runs again)."""
        get = body("resume").split("## 2. Get on the phase branch", 1)[1].split("**judge**", 1)[0]
        carry = get.index("- BRIEF.md crosses the switch")
        self.assertLess(get.index("`git switch -c <branch> origin/<base>`"), carry)
        self.assertLess(carry, get.index("`YAH-RESULT: blocked dirty tree`"))
        for s in ("copy it outside the repo first", "if it alone blocks the switch, `git restore BRIEF.md` (delete an "
                  "untracked one), since the copy keeps it", "then copy it back"):
            self.assertIn(s, get[carry:], s)
        auto = body("auto")
        row = next(ln for ln in auto.splitlines() if ln.startswith("| You are not on the phase's branch"))
        self.assertIn("Switch to it if the tree is clean or only BRIEF.md changed, carrying BRIEF.md over: copy it "
                      "outside the repo, `git restore` it (delete an untracked one) only if it blocks the switch, "
                      "switch, then copy it back. Otherwise say so and ask.", row)

    def test_wrap_never_tells_the_session_to_merge(self):
        text = body("wrap")
        self.assertNotIn("Merging is always the user's", text)  # true only with auto_merge off
        self.assertIn("Never merge a PR yourself.", text)
        self.assertIn("With `auto_merge` true, the `yah run` driver merges", text)
        self.assertIn("a merge while `auto_merge` is false", text)  # (you) merge lines only when it is off
        self.assertIn("With `auto_merge` true, write no merge line", text)
        self.assertNotIn("`/yah:where` shows `auto-merge: on`", text)  # wrap reads auto_merge itself

    def test_resume_never_starts_or_stops_a_run(self):
        step = body("resume").split("## 4. Test, then wrap", 1)[1].split("## 5.", 1)[0]
        self.assertIn("Never start or stop a run (`run.py`), and skip wrap's step 6.5", step)
        self.assertNotIn("run.py", parse(ROOT / "skills/resume/SKILL.md")[0].get("allowed-tools", ""))

    def test_wrap_allowed_tools_only_start_a_detached_plan_run(self):
        tools = parse(ROOT / "skills/wrap/SKILL.md")[0]["allowed-tools"]
        for py in ("python3", "python", "py -3"):  # the skill quotes the path: `PY ".../run.py" --plan --detach`
            self.assertIn(f"Bash({py} *scripts/run.py* --plan --detach)", tools)
        self.assertNotIn("scripts/run.py*)", tools)  # never a bare run.py: a foreground run holds the session
        self.assertNotIn("--stop", tools)

    def test_wrap_returns_to_the_plan_and_claims_a_parked_phase(self):
        text = body("wrap")
        self.assertIn("After a tiny task done while a plan phase is open, NEXT goes back to that phase's step", text)
        self.assertIn("`  - Parked: NEXT was <NEXT>`", text)
        self.assertIn("take NEXT from its Parked line", text)
        self.assertIn("delete the Parked line", text)
        # the run step 6.5 starts follows the claimed plan, so only a run on this plan merges this PR
        self.assertIn("The merge of this PR becomes a `(you)` follow-up with its command, unless `auto_merge` is true "
                      "and this is a run's session (`/yah:resume`)", text)
        self.assertIn("A run started from step 6.5 follows the claimed phase's plan, so it never merges this PR.", text)
        self.assertNotIn("With `auto_merge` false, the merge of this PR becomes a `(you)` follow-up", text)

    def test_phases_stamps_next(self):
        text = body("phases")
        self.assertIn('"next_sha":"<HEAD>"', text)
        self.assertIn("- At: <git rev-parse --short HEAD>", text)


class PhaseMemoryTests(unittest.TestCase):
    """A session reads its plan section, the Decisions and the phase's handoff log; wrap writes the log."""

    def test_resume_and_start_read_the_plan_section_decisions_and_log_before_recall(self):
        for name, recall in (("resume", "brain.py recall --phase"), ("start", "brain.py\" recall")):
            with self.subTest(skill=name):
                text = body(name)
                for want in ("`### <label>`", "`## Decisions`", "`state.plan.spec`", "log`", "say so in one line"):
                    self.assertIn(want, text, want)
                    self.assertLess(text.index(want), text.index(recall), want)  # read first, then recall
        self.assertIn("do not redo a tried approach or reopen a decision", body("resume"))
        for name in ("resume", "start"):
            self.assertIn('`grep -n "^#"`', body(name))  # find the two sections, never read the whole plan

    def test_wrap_writes_the_log_and_moves_it_to_the_pr(self):
        text = body("wrap")
        for want in ("`  - Done: <what>`", "`  - Tried: <what> failed because <why>`",
                     "`  - Decided: <what> because <why>`", "plain, not checkboxes", "Keep only the newest 6"):
            self.assertIn(want, text, want)
        finish = text.split("## 6. Only if the phase is complete", 1)[1]
        self.assertIn("Move the phase's log lines into the body", finish)
        self.assertIn("delete its log lines", finish)
        # beads rewrites NEXT and the log together; appending would pile up stale NEXT lines
        self.assertIn("never `--append-notes`", text)
        self.assertNotIn("bd update <phase-id> --append-notes", text)
        self.assertNotIn("not a diary", text)

    def test_phases_documents_the_log_and_the_plan_headings(self):
        text = body("phases")
        self.assertIn("handoff log (`log` in `where.py --json`)", text)
        self.assertIn("`  - Tried: <what> failed because <why>`", text)
        self.assertIn("a heading that starts with `### P<n>`", text)
        self.assertIn("`## Decisions`", text)
        self.assertIn("`### P<n> <title>` section", body("auto"))  # /yah:auto drafts plans that way


class UserCommandTests(unittest.TestCase):
    """A step left to the user comes with the command that does it, ready to paste in a terminal."""

    def test_user_steps_name_their_command(self):
        for name in ("wrap", "phases", "where", "auto"):
            with self.subTest(skill=name):
                self.assertIn("gh pr merge", body(name))
                self.assertNotIn("--delete-branch", body(name))  # deleting a stack's base closes the PR on it
        self.assertIn("Merge PR #13: gh pr merge 13 --merge (you)", body("wrap"))  # `(you)` still ends the line
        self.assertIn("- [ ] Merge PR #13: gh pr merge 13 --merge (you)", body("phases"))
        # with auto-merge on (the default) the run merges, and --plan would stop at a `(you)` merge phase
        self.assertIn("a merge while `auto_merge` is false in the JSON above) is its own line", body("phases"))
        self.assertIn("With `auto_merge` true, write no merge step", body("phases"))
        self.assertIn("a merge while `auto_merge` is false) is its own child with `-l human`", body("phases"))
        self.assertNotIn("(a review, a merge, a console step)", body("phases"))
        self.assertIn("paste in a terminal, e.g. `gh pr merge 13 --merge`", (ROOT / "RULES.md").read_text("utf-8"))

    def test_wrap_finish_gives_the_merge_command(self):
        finish = body("wrap").split("## 7. Finish with exactly this", 1)[1]
        self.assertIn("Merge   <the merge command>", finish)
        self.assertIn("`gh pr edit <N> --base <that PR's base>` once that PR merges", finish)
        self.assertIn("Leave the Merge line out when no PR is open, or when `auto_merge` is true and the PR has no "
                      "`(you)` merge follow-up: the run merges it", finish)
        self.assertNotIn("or when `auto_merge` is true: the run merges it", finish)  # not a PR step 6 left to the user

    def test_rules_say_only_the_run_driver_merges(self):
        safety = (ROOT / "RULES.md").read_text("utf-8").split("## Safety", 1)[1]
        self.assertIn("- Never merge a PR yourself. With auto-merge on (the default), yah run's driver merges the "
                      "green PRs it opened; with it off, give the user the merge command.", safety)


class TreeTests(unittest.TestCase):
    """/yah:tree runs phasemap.py and shows its output untouched."""

    def test_tree_runs_the_phase_map_as_is(self):
        meta, text = parse(ROOT / "skills/tree/SKILL.md")
        self.assertEqual(meta.get("disable-model-invocation"), "true")  # the listing budget has no room for it
        self.assertEqual(meta.get("argument-hint"), "[P<n>|plan]")
        for py in ("python3", "python", "py -3"):
            self.assertIn(f"Bash({py} *scripts/phasemap.py*)", meta["allowed-tools"])
        self.assertIn('PY "${CLAUDE_SKILL_DIR}/../../scripts/phasemap.py" $ARGUMENTS', text)
        self.assertIn("Show the output as is, in a code block", text)
        self.assertTrue((ROOT / "scripts/phasemap.py").is_file())

    def test_phase_prs_carry_the_map_through_a_body_file(self):
        step = body("wrap").split("## 6. Only if the phase is complete", 1)[1].split("## 6.5", 1)[0]
        self.assertIn('PY "${CLAUDE_SKILL_DIR}/../../scripts/phasemap.py" P<n> --into <file>', step)
        self.assertIn("`--body-file <file>`, never `--body`", step)  # an inline body can trip the deny rules
        self.assertIn("gh pr edit <N> --body-file <file>", step)
        record = step.split("4. Record it.", 1)[1].split("\n5.", 1)[0]  # the map redrawn once the phase is [x]
        self.assertIn("refresh the PR's map as in step 3", record)
        wrap_step = body("resume").split("## 4. Test, then wrap", 1)[1].split("## 5.", 1)[0]
        refresh = wrap_step.split("After a fix-findings push", 1)[1]
        for want in ("gh pr view <n> --json body -q .body", "scripts/phasemap.py\" <label> --into <file>",
                     "`<!-- yah:map -->` section", "gh pr edit <n> --body-file <file>"):
            self.assertIn(want, refresh, want)


class AutoTests(unittest.TestCase):
    """/yah:auto is the launcher's first prompt: it routes to the other skills and never makes up work."""

    def setUp(self):
        self.meta, self.body = parse(ROOT / "skills/auto/SKILL.md")

    def test_user_only(self):
        self.assertEqual(self.meta.get("disable-model-invocation"), "true")  # the launcher sends it

    def test_never_invents_a_task(self):
        self.assertIn("Ask one question", self.body)
        self.assertIn("Never invent a task", self.body)

    def test_hands_off_to_the_skills(self):
        for name in ("yah:start", "yah:phases", "yah:wrap", "yah:deep"):
            self.assertIn(f"`{name}`", self.body)

    def rows(self):
        return [ln for ln in self.body.splitlines() if ln.startswith("| ") and "---" not in ln]

    def row(self, start):
        return next(i for i, ln in enumerate(self.rows()) if ln.startswith("| " + start))

    def test_a_live_run_blocks_work_here_before_any_task(self):
        rows, live, task = self.rows(), self.row("A live run in this checkout"), self.row("A task was given")
        self.assertLess(live, task)  # a given task waits too: the run's sessions edit this tree
        self.assertIn("`running for`", rows[live])
        self.assertIn("wait for it, or end it", rows[live])
        self.assertIn('scripts/run.py" --stop`', rows[live])
        # after --stop the table is not read again: its open-phase row would start the run just ended
        self.assertIn("never start the run you just ended", rows[live])
        self.assertNotIn("read the state again", rows[live])

    def test_allowed_tools_match_the_quoted_script_path(self):
        tools = self.meta["allowed-tools"]
        for py in ("python3", "python", "py -3"):  # the skill quotes the path: `PY ".../run.py" --stop`
            self.assertIn(f"Bash({py} *scripts/run.py* --stop)", tools)
            self.assertIn(f"Bash({py} *scripts/run.py* --detach)", tools)
        self.assertNotIn("run.py --stop*", tools)  # never matched `run.py" --stop`
        self.assertNotIn("Bash(python *scripts/run.py*)", tools)  # never a foreground run: it holds the session

    def test_an_ended_run_is_asked_about_before_the_run_starts_again(self):
        rows, ended, stopped = self.rows(), self.row("The RUN line says the run `ended` with exit 1"), \
            self.row("The RUN line says the run `ended` with exit 130")
        start = self.row("A plan with an open phase")
        self.assertLess(self.row("NEXT starts with `NEEDS-HUMAN:`"), ended)  # exit 2 needs-human: answered first
        self.assertLess(ended, start)
        self.assertLess(stopped, start)
        for code in ("1, 2, 3, 4 or 7", "`died`"):
            self.assertIn(code, rows[ended])
        self.assertIn("rerun it, `yah:deep` on why it stopped, or go on here by hand", rows[ended])
        self.assertIn("offer waiting in place of deep", rows[ended])  # exit 7: a rerun stops again at once
        self.assertIn("never restart it unasked", rows[stopped])
        self.assertNotIn("**Start the run", rows[stopped])

    def test_an_appetite_stop_asks_continue_shrink_or_drop(self):
        rows, spent = self.rows(), self.row("The RUN line says the run `ended` with exit 9")
        self.assertLess(spent, self.row("A plan with an open phase"))  # else the open phase reruns it unasked
        row = rows[spent]
        self.assertIn("never rerun it unasked", row)
        self.assertIn("ask: continue, shrink or drop", row)
        self.assertIn('scripts/run.py" --plan --dry-run`', row)  # its bet line holds the spend so far
        self.assertIn("--appetite-hint <open phases>", row)
        self.assertIn("  - Decided: appetite raised to", row)
        self.assertIn("under `## Follow-ups`", row)
        self.assertIn("the plan file stays", row)
        tools = self.meta["allowed-tools"]
        for py in ("python3", "python", "py -3"):
            self.assertIn(f"Bash({py} *scripts/run.py* --plan --dry-run)", tools)

    def test_needs_human_writes_the_answer_into_next_then_starts_the_run(self):
        row = self.rows()[self.row("NEXT starts with `NEEDS-HUMAN:`")]
        self.assertIn("Ask that question as it is", row)
        self.assertIn("`yah:wrap` with the answer", row)
        self.assertIn("without `NEEDS-HUMAN:`", row)  # else the run's first session stops needs-human again
        self.assertIn("**start the run**", row)
        self.assertIn("unless wrap's finish line says it already did", row)  # wrap's 6.5 may have started it

    def test_an_open_phase_starts_the_detached_plan_run(self):
        rows = self.rows()
        row = rows[self.row("A plan with an open phase")]
        self.assertIn("**Start the run.**", row)
        self.assertNotIn("yah:start", row)  # autopilot: no foreground session work for an open phase
        self.assertIn('scripts/run.py" --plan --detach`', self.body)
        self.assertIn("`run.py <run.target> --detach`", self.body)
        self.assertIn("never start a run in the foreground", self.body)
        self.assertIn("If the tree is dirty", self.body)
        self.assertIn("The task `here` opts out", self.body)
        self.assertIn("other than `here`", rows[self.row("A task was given")])

    def test_a_multi_pr_task_is_deep_checked_and_every_decision_asked_before_the_run(self):
        rows = self.rows()
        self.assertIn("**plan it** (section 3)", rows[self.row("A task was given")])
        self.assertIn("Route the answer as a given task", rows[self.row("No plan and no NEXT")])
        plan = self.body.split("## 3. Plan it", 1)[1].split("## 4. Start the run", 1)[0]
        explore = plan.index("Explore only what the task touches")
        self.assertLess(plan.index("Enter plan mode"), explore)
        steps = ("`yah:deep`", "AskUserQuestion", "`## Decisions`", "Then ExitPlanMode", "`yah:phases`",
                 "**start the run**")
        at = [plan.index(s, explore) for s in steps]  # the task rounds before it ask and decide too
        self.assertEqual(at, sorted(at))  # deep-check, then ask everything, then exit, then track, then run
        self.assertIn("before ExitPlanMode", plan)
        self.assertIn("a `branch` and a `base`", plan)  # yah run --plan stops at a phase with no branch
        self.assertIn("`NEEDS-HUMAN:` is only for what nobody could foresee", plan)
        # never the old hands-on ending: an approved plan goes to the run, not a foreground first phase
        self.assertNotIn("start its first phase", self.body)
        self.assertNotIn("`yah:start`", plan)

    def test_the_plan_rule_example_parses_as_the_phase_map_reads_it(self):
        plan = self.body.split("## 3. Plan it", 1)[1].split("## 4. Start the run", 1)[0]
        for field in ("`After it merges:`", "`Files:`", "End the block with a blank line"):
            self.assertIn(field, plan)
        example = plan.split("```", 2)[1].strip("\n")
        sys.path.insert(0, str(ROOT / "scripts"))
        import phasemap
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "plan.md"
            lines = [ln.strip() for ln in example.splitlines()]
            spec.write_text("### P1 Pay\nbranch `x/pay` | base `main`\n" + "\n".join(lines)
                            + "\n\n- **Tests** in tests/pay.test.ts\n", "utf-8")
            [ph] = phasemap.plan_sections(spec)
        self.assertEqual(ph["after"], "checkout takes Apple Pay.")
        self.assertEqual([(e["mark"], e["path"]) for e in ph["files"]],
                         [("+", "src/pay/applepay.ts"), ("~", "src/pay/PaymentForm.tsx"), (">", "src/pay/card.ts")])
        self.assertIn("`After it merges:`", body("phases"))
        self.assertIn("`Files:`", body("phases"))

    def test_plan_mode_draws_the_map_into_the_plan_before_approval(self):
        plan = self.body.split("## 3. Plan it", 1)[1].split("## 4. Start the run", 1)[0]
        cmd = 'PY "${CLAUDE_SKILL_DIR}/../../scripts/phasemap.py" all --no-gh --spec "<plan file>"'
        self.assertIn(cmd, plan)
        self.assertLess(plan.index(cmd), plan.index("Then ExitPlanMode"))
        self.assertIn("under `## Map`", plan)
        self.assertIn("replacing any `## Map` already there", plan)
        self.assertIn("draw the map again, and exit plan mode again", plan)
        for py in ("python3", "python", "py -3"):
            self.assertIn(f"Bash({py} *scripts/phasemap.py* all --no-gh --spec *)", self.meta["allowed-tools"])

    def test_only_a_tiny_task_stays_in_session_everything_else_is_planned(self):
        rows = self.rows()
        for start in ("A task was given", "A NEXT with no plan"):
            row = rows[self.row(start)]
            self.assertIn("Tiny", row)
            self.assertIn("`yah:start`", row)
            self.assertIn("**plan it** (section 3)", row)  # a one-PR task is planned too, then run
            self.assertLess(row.index("Tiny"), row.index("**plan it**"))
        self.assertIn("one PR or several", rows[self.row("A task was given")])
        self.assertNotIn("If it needs several PRs", self.body)  # the old split sent one-PR tasks to the session
        self.assertNotIn("this one stays hands-on", self.body)

    def test_a_merged_next_with_no_step_after_it_asks_what_to_build(self):
        rows, waits, empty = self.rows(), self.row("NEXT waits on the user"), self.row("No plan and no NEXT")
        self.assertIn('Merged, and NEXT names nothing after it: use the "No plan and no NEXT" row', rows[waits])
        self.assertIn("a merge that already happened", rows[empty])
        self.assertIn("ask: wait, or stack", rows[waits])
        self.assertIn("the waiting PR's branch as its base", rows[waits])
        self.assertIn("Tiny: cut a new branch from that base", rows[waits])  # never commit onto the waiting PR
        self.assertIn("**plan it** (section 3)", rows[waits])

    def test_a_waiting_phase_pr_goes_to_the_run_with_auto_merge_on(self):
        row = self.rows()[self.row("NEXT waits on the user")]
        hand = ("Not merged, `auto_merge` true in `where.py --json --no-gh`, the PR is a phase's of the current plan "
                "(its PR line names the phase) and the task is not `here`: **start the run**")
        self.assertIn(hand, row)
        # run.py sweeps only the plan it pins: another plan's PR stays the user's
        self.assertIn("its sweep merges that plan's green phase PRs left open, bottom-up, then goes on", row)
        self.assertNotIn("the PR is a plan phase's and", row)
        # the old question stays for auto_merge off, a PR that is no phase's, or `here`
        self.assertLess(row.index(hand), row.index("Otherwise not merged: say in one line what waits on the user"))
        self.assertIn("`gh pr merge <N> --merge`", row)
        self.assertLess(self.row("The RUN line says the run `ended` with exit 1"), self.row("NEXT waits on the user"))
        self.assertNotIn("gh pr merge", self.meta["allowed-tools"])  # only the run driver merges

    def test_a_closed_phases_open_pr_goes_to_the_run_not_to_what_next(self):
        """wrap marks a phase [x] once its PR opens, so a finished stack still has PRs open: with auto-merge on,
        "every phase is done" must not ask what to build while the sweep has them to merge."""
        rows, empty = self.rows(), self.row("No plan and no NEXT")
        hand = self.row("A closed phase's PR is still open")
        self.assertIn("It does not count while a closed phase of the current plan still has an open PR, "
                      "`auto_merge` is true in `where.py --json --no-gh` and the task is not `here`: read on",
                      rows[empty])
        self.assertIn("**Start the run**: its sweep merges that plan's green phase PRs left open, bottom-up", rows[hand])
        self.assertIn("`auto_merge` is true", rows[hand])
        # an ended run (exit 2 on a skipped merge) is told first; the hand-off comes before the open-phase rows
        for row in ("NEXT starts with `NEEDS-HUMAN:`", "The RUN line says the run `ended` with exit 1",
                    "The RUN line says the run `ended` with exit 9", "The RUN line says the run `ended` with exit 130"):
            self.assertLess(self.row(row), hand, row)
        for row in ("NEXT waits on the user", "You are not on the phase's branch", "A plan with an open phase"):
            self.assertLess(hand, self.row(row), row)

    def test_an_open_plan_is_parked_before_a_new_one_is_tracked(self):
        plan = self.body.split("## 3. Plan it", 1)[1].split("## 4. Start the run", 1)[0]
        park = plan.index("park it first")
        self.assertLess(park, plan.index("`yah:phases`"))  # parked first, so the new plan is the one with `[~]`
        self.assertIn("no run is live", plan)  # a live run's plan is never parked under it
        self.assertIn("`  - Parked: NEXT was <NEXT>`", plan)  # wrap's step 6 item 6 claims this exact line
        self.assertIn("goes back to `[ ]`", plan)
        self.assertIn("with no `[~]`, its first `[ ]` phase stays `[ ]`", plan)  # a not-yet-started phase parks too

    def test_deep_on_every_tier_and_only_the_run_driver_merges(self):
        self.assertIn("every plan tier", self.body)
        self.assertIn("Never merge a PR yourself, and never push a protected branch.", self.body)
        self.assertIn("Only the run driver merges, with `auto_merge` on (the default).", self.body)
        self.assertNotIn("only when `auto_merge` is on", self.body)  # it is on unless config turns it off

    def test_the_brief_interview_runs_once_outside_plan_mode(self):
        rows, none, empty = self.rows(), self.row("No brief"), self.row("No plan and no NEXT")
        task, brief = self.row("A task was given"), self.row("The task is `brief`")
        self.assertEqual(none + 1, empty)  # only where nothing says what to do: never before a task or a NEXT
        self.assertLess(task, none)
        # it writes only BRIEF.md, which a live run's sessions commit, so a run does not block it
        self.assertLess(self.row("No project"), brief)
        self.assertLess(brief, self.row("A live run in this checkout"))
        self.assertLess(brief, task)
        self.assertIn("other than `here` or `brief`", rows[task])
        for s in ("`${CLAUDE_SKILL_DIR}/brief.md`", "outside plan mode", "read the table again with no task",
                  "even beside a live run, since it writes only BRIEF.md, which the run's sessions commit"):
            self.assertIn(s, rows[brief], s)
        # the block shows no decline and no brief field, so --json decides
        for s in ("`where.py --json --no-gh`", "`brief` is null", "`state_md.brief_declined` is not true",
                  "`BRIEF: none`, or the old `AIM: none`", "`${CLAUDE_SKILL_DIR}/brief.md`", "outside plan mode",
                  "read the table again"):
            self.assertIn(s, rows[none], s)
        self.assertNotIn("`state_md.aim` is not `none`", self.body)  # declining the brief no longer hides the AIM
        self.assertTrue((ROOT / "skills/auto/brief.md").is_file())
        plan = self.body.split("## 3. Plan it", 1)[1].split("## 4. Start the run", 1)[0]
        step0 = plan.index("\n0. No brief")
        self.assertLess(step0, plan.index("Enter plan mode"))
        self.assertIn("outside plan mode, then come back to step 1", plan[step0:plan.index("Enter plan mode")])
        the_brief = self.body[self.body.index("**The brief**"):self.body.index("**Q2, the pick**")]
        for s in ("BRIEF.md at the repo root", "`${CLAUDE_SKILL_DIR}/brief.md`", "outside plan mode", "`BRIEF: none`",
                  "file at `store.path`", "never offered again unasked", "the task `brief`",
                  # a worktree's edit rides on its own branch, never on the main checkout's file
                  "Every BRIEF.md edit goes to the one at this checkout's root (`top`)",
                  "copy the main checkout's (`brief.path`) there first"):
            self.assertIn(s, the_brief, s)
        self.assertNotIn("`AIM: none` in the file", the_brief)
        # Q1 drafted a one-line aim from a peek at the README; the interview replaced it
        for gone in ("README's first 40 lines", "never explore further", "**Q1, the aim**", "Q1 and Q2", "No AIM"):
            self.assertNotIn(gone, self.body, gone)

    def test_q2_offers_a_b_c_from_the_follow_ups_and_the_roadmap_and_keeps_the_old_question_without_them(self):
        row = self.rows()[self.row("No plan and no NEXT")]
        self.assertIn("`state_md.followups`", row)
        self.assertIn("`brief.roadmap`", row)
        self.assertLess(row.index("**Q2**"), row.index("nothing to rank. Ask one question"))
        self.assertIn("With neither, there is nothing to rank", row)
        q2 = self.body[self.body.index("**Q2, the pick**"):self.body.index("## 3. Plan it")]
        for s in ("own `text`", "impact H/M/L", "effort S/M/L", "words you quote", "`absent`", "never an estimate",
                  "discovery task", "`brief.roadmap`", "BRIEF.md's AIM and `## Success` section",
                  "the AIM alone with no brief", "a Roadmap line's `(size S/M/L)` is its effort",
                  "Never read code or other docs to rank: BRIEF.md is the one document Q2 reads",
                  "`A/B/C | id title | outcome | impact/effort | evidence | done-when`",
                  "I recommend A; 'auto' lets me choose", "Other for a new task", "Route the pick",
                  "A Roadmap line picked and done as a tiny task: mark it `[x]` before `yah:wrap`, which commits it"):
            self.assertIn(s, q2, s)
        self.assertNotIn("Never read code or docs to rank", q2)  # BRIEF.md is a doc it reads

    def test_task_rounds_come_before_exploring(self):
        plan = self.body.split("## 3. Plan it", 1)[1].split("## 4. Start the run", 1)[0]
        enter, explore = plan.index("Enter plan mode"), plan.index("Explore only what the task touches")
        rounds = plan[enter:explore]
        for s in ("1-2 AskUserQuestion rounds of at most 4 questions", "from BRIEF.md and the task", "never generic",
                  "recommended one first with a one-line why", "why now", "which brief goal it serves",
                  "whether it changes the brief", "done-when, as the user would see it", "what to leave out",
                  "prototype first, or build it right", "already answers, and say so in one line",
                  "`auto` takes the recommended option", "under `## Decisions` in the plan file"):
            self.assertIn(s, rounds, s)
        self.assertIn("after the rounds", plan[explore:])
        # the Bet, deep's prompt and the decision rounds all lean on the brief
        for s in ("tied to BRIEF.md's Success signal and the AIM when there is one", "naming the Roadmap line it serves",
                  "BRIEF.md's AIM, Success and Won't lines (or the AIM alone with no brief)",
                  "the trade-off each one commits to, and a recommendation tied to BRIEF.md (or the AIM)"):
            self.assertIn(s, plan, s)
        self.assertNotIn("with a brief that stands alone", plan)  # deep gets a prompt; the brief is BRIEF.md
        # approved: the Roadmap line goes [~] and brief changes are written, but nothing is committed yet
        approved = plan[plan.index("6. Approved"):]
        for s in ("mark it `[~]`", "a new non-goal, a changed success signal", "dated line under its `## Decisions`",
                  "Do not commit BRIEF.md here: it rides on the first phase's branch"):
            self.assertIn(s, approved, s)
        self.assertNotIn("the plan's `## Brief`", plan)  # written out before the plan, so a Q3 skip keeps it
        self.assertLess(approved.index("mark it `[~]`"), approved.index("`yah:phases`"))
        start = self.body.split("## 4. Start the run", 1)[1]
        self.assertIn("A new or changed BRIEF.md alone does not count", start)  # the dirty-tree question
        drop = self.rows()[self.row("The RUN line says the run `ended` with exit 9")]
        self.assertIn("A `[~]` Roadmap line in BRIEF.md that the dropped plan served goes back to `[ ]`", drop)
        # no phase left to wrap after the shrink, so nothing else would close the Roadmap line
        shrink = drop[drop.index("Shrink:"):drop.index("Drop:")]
        self.assertIn("A shrink that leaves no open phase first marks the plan's `[~]` Roadmap line `[x]` (`[ ]` if "
                      "the user says its outcome was not reached)", shrink)

    def test_brief_md_has_every_round_and_section(self):
        text = (ROOT / "skills/auto/brief.md").read_text("utf-8")
        self.assertFalse(text.startswith("---"))  # a supporting file the skill reads, not a skill
        heads = ["**Round 1: person and problem**", "**Round 2: success and evidence**",
                 "**Round 3: trade-offs and scope**", "**Round 4: constraints and taste**", "**Round 5: pre-mortem**",
                 "**Round 6: the mirror**"]
        at = [text.index(h) for h in heads]
        self.assertEqual(at, sorted(at))
        self.assertLess(text.index("## 1. Read first, ask second"), at[0])  # the scout read comes first
        for s in ("Explore agent", "300 words or fewer", "`git log --oneline -20`", "any `AIM:` line",
                  "at most 4 questions, at most 6 rounds", "outside plan mode", "Never offer generic options",
                  "(Recommended)", "`(assumed)`", "`(not asked)`", "\"auto the rest\"", "\"skip\"",
                  # the technique rules
                  "Ask about past behavior, never opinions", "until it bottoms out", "A vague word",
                  "names a feature instead of a problem", "never smoothed over", "every tension",
                  # the decline path
                  "\"no brief\", at any point: write `BRIEF: none`", "file at `store.path`",
                  "never offered again unasked", "Never create a second STATE.md",
                  "Leave any `AIM:` line as it is: it stays the project's aim",
                  "\"no brief\" only ends the update: change nothing",
                  # plan mode can write only the plan file: write the brief out before any plan, so a skip keeps it
                  "under `## Brief` in the plan file (\"no brief\" puts `BRIEF: none` there)",
                  "Then ExitPlanMode with the `## Brief` alone, and once it is approved, write it out as below at once",
                  "a plan that is skipped or never approved must not lose it",
                  "the table must not send you back here", "its step 1 then enters plan mode again",
                  # an empty repo has nothing to draft from; a given task is a source
                  "No product in the summary (no README, no code) and no task given: before Round 1, ask one open "
                  "question", "what do they want to build, and for whom?",
                  "the Explore summary, the task when there is one, and every earlier answer",
                  # a probe has a slot; the why chain is one
                  "Each takes the place of that round's least-needed question, which moves to a later round with room",
                  "Then, as a probe, ask why of that answer",
                  # an update keeps what it did not re-ask
                  "Read BRIEF.md yourself first", "edit only the sections those rounds cover, in place",
                  "keep every other section, each Roadmap line's mark and the `## Decisions` lines",
                  "Platform and stack: web, mobile, desktop, CLI or API",
                  "Write BRIEF.md at the root of this checkout (`top`)",
                  "copies the main checkout's (`brief.path`) there first"):
            self.assertIn(s, text, s)
        for gone in ("by its step 6", "write `AIM: none`", "above any `AIM:` line already there"):
            self.assertNotIn(gone, text, gone)
        for n in range(1, 6):  # AskUserQuestion takes at most 4 questions a round
            part = text[at[n - 1]:at[n]]
            self.assertLessEqual(sum(1 for ln in part.splitlines() if ln.startswith("- ")), 4, f"round {n}")
        template = text[text.index("```markdown"):]
        template = template[:template.index("\n```", 3)]
        lines = template.splitlines()
        self.assertIn("AIM: Help <who> <outcome> when <situation>", lines)
        self.assertIn("- [ ] <bet>: serves <success signal> (size S/M/L)", lines)  # where.py reads it as roadmap
        sections = ["# Brief: <project>", "## Who and problem", "## Why it matters", "## Success", "## Scope",
                    "## Constraints", "## Taste", "## Risks and assumptions", "## Decisions", "## Roadmap"]
        at = [lines.index(s) for s in sections]
        self.assertEqual(at, sorted(at))
        self.assertLess(lines.index("AIM: Help <who> <outcome> when <situation>"), lines.index("## Who and problem"))
        self.assertIn("Do not commit BRIEF.md here", text)
        self.assertIn("BRIEF.md's AIM wins", text)

    def test_every_plan_gets_a_bet_and_ends_with_q3(self):
        plan = self.body.split("## 3. Plan it", 1)[1].split("## 4. Start the run", 1)[0]
        for s in ("`## Bet`", "`appetite: $<X> and <H>h`", "`stop if <signal> by <checkpoint>`",
                  'scripts/run.py" --appetite-hint <phases>', "$7 and 0.6h a phase", "pre-mortem",
                  "does not serve the AIM", "run / shrink / skip", "ask Q3 again", "Skip: drop the plan"):
            self.assertIn(s, plan)
        self.assertLess(plan.index("`## Bet`"), plan.index("pre-mortem"))  # deep sees the Bet it critiques
        self.assertLess(plan.index("**Q3**"), plan.index("Then ExitPlanMode"))
        for py in ("python3", "python", "py -3"):
            self.assertIn(f"Bash({py} *scripts/run.py* --appetite-hint *)", self.meta["allowed-tools"])

    def test_resume_reads_the_bet_and_stops_on_a_stop_if(self):
        text = body("resume")
        self.assertIn("`## Decisions` and `## Bet` sections", text)
        self.assertIn("`NEEDS-HUMAN: stop-if hit: <which>, <evidence>`", text)

    def test_wrap_and_phases_keep_the_aim_line(self):
        self.assertIn("above all the `AIM:` and `BRIEF: none` lines under the `#` title: never move, reword or delete "
                      "them", body("wrap"))
        self.assertIn("keep the `AIM:` and `BRIEF: none` lines under the `#` title as they are: never move or rewrite "
                      "them", body("phases"))


class BeadsTests(unittest.TestCase):
    def test_wrap_and_resume_stay_in_this_repo(self):
        for name in ("wrap", "resume"):
            with self.subTest(skill=name):
                text = body(name)
                self.assertIn("`store`", text)
                self.assertIn("`store.bd`, quoted", text)
                self.assertIn("Never set, export or follow `BEADS_DIR`", text)
                self.assertIn("never run bd against a database outside this repo", text)
                self.assertNotRegex(text, r"(export|set)\s+BEADS_DIR\s*=")

    def test_skills_read_store_instead_of_restating_the_beads_rule(self):
        # where.py's `store` names where the plan is written; no skill re-derives it from the beads fields
        for name in ("wrap", "resume", "phases", "start"):
            with self.subTest(skill=name):
                text = body(name)
                self.assertIn("`store", text)
                for gone in ("`beads_source`", "`bd_note`", "`bd` is a path", "`state.plan.source` is `beads`",
                             "`ignored_plan`", "`state_md.path`", "no beads DB here"):
                    self.assertNotIn(gone, text, gone)

    def test_resume_takes_the_base_yah_run_worked_out(self):
        meta, text = parse(ROOT / "skills" / "resume" / "SKILL.md")
        self.assertIn("[base=<branch>]", meta["argument-hint"])
        self.assertIn("replaces the phase's `base` everywhere below", text)
        # written back in both stores, so the next session reads it from the plan (beads adds nothing)
        self.assertIn("`| base <branch>` on the STATE.md phase line", text)
        self.assertIn("--set-metadata base=<branch>", text)

    def test_resume_critique_is_read_only_and_the_build_folds_it_in(self):
        meta, text = parse(ROOT / "skills" / "resume" / "SKILL.md")
        self.assertIn("|critique|judge]", meta["argument-hint"])
        self.assertIn("[critique=<path>]", meta["argument-hint"])
        for s in ("ends here and changes nothing", "at most 300 words", "`YAH-RESULT: critique-done`",
                  "the path runs to the end", "a `Decided:` log line", "never write NEEDS-HUMAN because of it"):
            self.assertIn(s, text)

    def test_resume_judge_is_read_only_and_fix_findings_fixes_what_it_proved(self):
        meta, text = parse(ROOT / "skills" / "resume" / "SKILL.md")
        self.assertIn("|fix-findings|", meta["argument-hint"])
        self.assertIn("[findings=<path>]", meta["argument-hint"])
        judge = text[text.index("**judge**"):text.index("## 3.")]
        for s in ("changes nothing", "`gh pr diff <n>`", "should block the merge", "a broken or weakened test",
                  "Prove each one", "report only the proven ones", "file:line", "`YAH-RESULT: judge pass`",
                  "`YAH-RESULT: judge block <count>`"):
            self.assertIn(s, judge)
        fix = text[text.index("- **fix-findings:**"):text.index("`<n>` is TARGET")]
        for s in ("`findings=<path>`", "regression test", "`Done:` or `Decided:` log line", "NEEDS-HUMAN"):
            self.assertIn(s, fix)
        self.assertIn("In fix-checks, address-review and fix-findings, the PR exists", text)
        self.assertIn("`YAH-RESULT: judge pass` or `YAH-RESULT: judge block <count>` in judge mode", text)

    def test_skills_read_the_state_key(self):
        for name in ("wrap", "resume"):
            with self.subTest(skill=name):
                text = body(name)
                self.assertIn("`state.phase`", text)
                self.assertNotRegex(text, r"\bbeads\.(plan|phase|phases|next_phase)\b")

    def test_wrap_writes_beads_only_for_a_beads_plan(self):
        text = body("wrap")
        self.assertIn("`store.kind` is `beads` and `state.plan` is set: use **2b**", text)
        self.assertIn("Otherwise use **2a**", text)  # no plan goes to STATE.md, not an in_progress bead
        self.assertNotIn("in_progress bead", text)
        self.assertIn("If `store.note` is set, tell the user first", text)  # why bd is off, or an ignored epic
        self.assertIn("Run every bd command in this skill as `store.bd`, quoted", text)  # steps 4 and 6 too

    def test_wrap_beads_matches_state_md(self):
        text = body("wrap")
        self.assertIn('--reason "PR #<N> open"`', text)
        self.assertNotIn("checks <state>", text)  # no CI state in the close reason
        self.assertNotIn("--parent", text)  # follow-ups are plain beads, like STATE.md's file-global list
        self.assertIn('`bd create "<title>" --silent`', text)
        self.assertIn("`-l human`", text)
        self.assertIn("done-when in the plan file", text)  # both sources, not a bead description
        self.assertIn("`spec_id`", text)
        self.assertIn("Indented checkbox lines under a phase are its sub-tasks", text)

    def test_phases_beads_is_frozen(self):
        text = body("phases")
        beads = text[text.index("## Beads"):]
        for gone in ("--design", '"short"', "--id ", "--description", "bd dep", "Reparent", "--parent <phase-id>"):
            self.assertNotIn(gone, text, gone)
        for kept in ("--external-ref gh-<N>", 'bd close <id> --reason "PR #<N> open"', "`-l human`",
                     "bd update <id> --claim", "--spec-id"):
            self.assertIn(kept, beads, kept)
        self.assertIn("Only the current phase gets `--notes", beads)
        self.assertLess(text.index("## STATE.md"), text.index("## Beads (only if the repo already uses it)"))

    def test_phases_documents_state_md_syntax(self):
        text = body("phases")
        self.assertIn("Phases are the outermost checkbox lines", text)
        self.assertIn("exactly one outermost `[~]`", text)
        self.assertIn("  - [ ] <sub-task>", text)
        self.assertIn("not counted in done/total", text)
        self.assertIn("shows it as DOING", text)
        self.assertIn("a STATE.md plan wins over a beads epic", text)
        self.assertIn("If `store.note` is set, tell the user first", text)
        self.assertIn("Run every `bd` below as `store.bd`, quoted", text)
        self.assertIn("follow only the section `store.kind` names", text)  # not Beads because .beads/ exists

    def test_wrap_writes_the_file_where_read_and_trims_it(self):
        text = body("wrap")
        for want in ("`store.path`", "create it if it does not exist", "Delete finished ones",
                     "Delete a `## Plan:` section whose phases are all `[x]` once none of their PRs is open"):
            self.assertIn(want, text, want)
        self.assertIn("`store.path`", body("phases"))

    def test_scout_names_state_md(self):
        text = (ROOT / "agents/scout.md").read_text("utf-8")
        self.assertIn("Plan state is in STATE.md", text)
        self.assertLess(text.index("STATE.md"), text.index("bd show/list"))


class GptTests(unittest.TestCase):
    """/yah:gpt forks to the gpt agent, which makes one codex.py call and relays a short answer."""

    def setUp(self):
        self.meta, self.body = parse(ROOT / "skills/gpt/SKILL.md")
        self.agent_meta, self.agent = parse(ROOT / "agents/gpt.md")

    def test_forks_to_a_cheap_agent(self):
        self.assertEqual(self.meta["context"], "fork")
        self.assertEqual(self.meta["agent"], "yah:gpt")
        self.assertEqual(self.agent_meta["model"], "sonnet")
        self.assertEqual(self.agent_meta["effort"], "low")
        self.assertEqual(self.agent_meta["tools"], "Bash, Read")  # no Edit or Write: GPT does the work

    def test_allowed_tools_match_the_quoted_script_path(self):
        tools = self.meta["allowed-tools"]
        for py in ("python3", "python", "py -3"):  # the skill quotes the path: `PY ".../codex.py" research`
            for mode in ("research", "review", "mockup", "critique"):
                self.assertIn(f"Bash({py} *scripts/codex.py* {mode} *)", tools)
        self.assertNotIn("codex.py research", tools)
        self.assertIn('`PY "${CLAUDE_SKILL_DIR}/../../scripts/codex.py"`', self.body)

    def test_brief_goes_through_a_quoted_heredoc(self):
        # quotes, backticks and $ in a brief would break or run in a double-quoted argument
        self.assertIn("<<'YAH_BRIEF'", self.body)
        self.assertIn("must stand alone", self.body)

    def test_the_call_ends_before_the_bash_tool_cap(self):
        # Bash tops out at 600000 ms, and codex_timeout_minutes defaults to 15
        self.assertEqual(self.body.count("--timeout 570"), 5)  # research, review, mockup, critique by URL or PNGs
        self.assertIn("600000 ms timeout", self.agent)
        self.assertIn("--timeout 570", self.agent)

    def test_agent_makes_one_call_and_relays_failures(self):
        self.assertIn("exactly one codex.py call", self.agent)
        self.assertIn("never retry", self.agent)
        self.assertIn("Exit 3 means GPT hit its usage limit", self.agent)
        self.assertIn("at most 1,500 characters", self.agent)
        for field in ("ANSWER:", "SOURCES:", "PNGS:", "REPORT:", "UNSURE:"):
            self.assertIn(field, self.agent)

    def test_setup_offers_codex_only_when_installed(self):
        setup = body("setup")
        section = setup[setup.index("## 3e. GPT"):setup.index("## 4.")]
        self.assertIn("Only when `codex` is on PATH", section)
        self.assertIn("run `setup.py --codex`", section)
        self.assertIn("`codex login`", section)
        self.assertIn("`--uninstall` restores", section)


class SetupSkillTests(unittest.TestCase):
    def test_auto_merge_is_an_opt_out(self):
        setup = body("setup")
        section = setup[setup.index("## 3d. Auto-merge (on by default)"):setup.index("## 3e. GPT")]
        for want in ("It is on by default. Keep it on?", "Only the `yah run` driver merges, never the model",
                     "at least one check ran and all passed, or it has no checks and yah's judge passed its exact "
                     "head commit", "merges green phase PRs left open, bottom-up",
                     "it stops with exit 2 instead of stacking the next phase on that PR",
                     "On no, run `setup.py --no-auto-merge` and show the output", "`--uninstall` restores",
                     "`\"auto_merge\": false` in that repo's entry under `projects`"):
            self.assertIn(want, section, want)
        self.assertNotIn("It is off by default", section)
        # a yes writes true, so a re-run turns back on what an earlier no turned off
        self.assertIn("On yes, run `setup.py --auto-merge` and show the output", section)
        self.assertNotIn("On yes, change nothing", section)
        self.assertIn("Step 3d is the one opt-out: auto-merge is on by default, so there a no turns it off.",
                      setup)  # the yes-to-change rule's exception


class DocsTests(unittest.TestCase):
    def setUp(self):
        self.readme = (ROOT / "README.md").read_text("utf-8")

    def section(self, head):
        """From the `## ` heading to the next one, skipping headings inside code blocks."""
        lines = self.readme[self.readme.index(head):].splitlines()
        out, fence = lines[:1], False
        for ln in lines[1:]:
            if ln.startswith("```"):
                fence = not fence
            elif ln.startswith("## ") and not fence:
                break
            out.append(ln)
        return "\n".join(out)

    def test_beads_detail_lives_in_one_section(self):
        beads = self.section("## If you already use beads")
        self.assertIn("`human`", beads)
        self.assertIn("STATE.md wins", beads)
        self.assertIn("ignored: STATE.md has a plan", beads)
        self.assertIn("only in a repo with `.beads/`", beads)
        self.assertNotIn("beads", self.section("## Plan state: STATE.md"))
        self.assertNotIn("an open plan epic wins over STATE.md", self.readme)
        self.assertNotIn("(or beads)", self.readme)  # unqualified: always "if you already use it"
        self.assertNotIn("or in beads if the repo already uses it", self.readme)

    def test_state_md_syntax_and_tasks_row(self):
        state = self.section("## Plan state: STATE.md")
        self.assertIn("  - [ ] Apple Pay button", state)
        self.assertIn("Phases are the outermost checkbox lines", state)
        self.assertIn("not counted in done/total", state)
        self.assertIn("DOING", state)
        self.assertIn("TASKS   <N> open, no plan (/yah:phases after a plan is approved)", state)
        self.assertNotIn("no plan epic (/yah:phases", self.readme)  # the old beads-only BEADS row

    def test_bd_runs_only_with_a_beads_dir(self):
        self.assertNotIn("`bd list` when installed", self.readme)
        self.assertNotIn("`bd` if installed", self.readme)
        self.assertNotIn("`gh` and `bd` when installed", self.readme)
        self.assertGreaterEqual(self.readme.count("only in a repo with `.beads/`"), 3)

    def test_no_you_config_key(self):
        config = self.section("## Config")
        self.assertNotIn('"you"', config)
        self.assertNotIn("| `you` |", config)
        self.assertNotIn("assigned to you", self.readme)

    def test_critic_and_judge_section_matches_run_py(self):
        runs = self.section("## Hands-free runs")
        crit = runs[runs.index("#### Critic and judge: quality profile"):]
        run_py = (ROOT / "scripts/run.py").read_text("utf-8")
        for words in ("critic_week_skip_pct", "`roles.judge`", "fix-findings", "critique=<path>", "`judge block <n>`"):
            self.assertIn(words, crit)
        # the log lines and the stop line it quotes are the ones run.py writes
        for words in ("critic/judge sessions", "at or over", ": roles.judge", "bar unknown", "skipped: ",
                      "it ended with YAH-RESULT: "):
            with self.subTest(w=words):
                self.assertIn(words, crit)
                self.assertIn(words, run_py)
        self.assertIn("(#critic-and-judge-quality-profile)", self.section("## Quality profile"))

    def test_manifests(self):
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text("utf-8"))
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text("utf-8"))
        for where, entry in (("plugin", plugin), ("marketplace", market["plugins"][0])):
            with self.subTest(f=where):
                kw = entry["keywords"]
                self.assertIn("beads", kw)
                self.assertIn("state-md", kw)
                self.assertLess(kw.index("state-md"), kw.index("beads"))
                self.assertFalse(entry["description"].lower().startswith("beads"))
                self.assertNotIn("never merges", entry["description"].lower())  # auto-merge is on by default
                self.assertIn("until a PR is open and green, then merges it (auto-merge is on by default)",
                              entry["description"])
        self.assertFalse(market["metadata"]["description"].lower().startswith("beads"))
        self.assertNotIn("never merges", market["metadata"]["description"].lower())

    def test_the_brief_section_replaced_aim_and_bets(self):
        self.assertNotIn("## Aim and bets", self.readme)
        self.assertNotIn("#aim-and-bets", self.readme)
        brief = self.section("## The brief, the questions and bets")
        self.assertGreaterEqual(self.readme.count("(#the-brief-the-questions-and-bets)"), 4)
        for want in ("once per project", "outside plan mode", "Explore agent", "the last 20 commits",
                     "300 words or fewer", "up to 6 rounds", "**Person and problem:**", "**Success and evidence:**",
                     "**Trade-offs and scope:**", "**Constraints and taste:**", "**Pre-mortem:**", "**The mirror:**",
                     "never generic", "what you did, not what you would do", "A vague word",
                     "what would you do with it?", "instead of being smoothed over",
                     "`auto` takes the recommended answer", "`(assumed)`", '"auto the rest"', '"skip"',
                     '"no brief"', "`BRIEF: none`", "an AIM already there stays",
                     "An empty repo with no task first gets one open question",
                     "the platform, stack and deploy target", "`AIM: Help <who> <outcome> when <situation>`",
                     "`## Who and problem`", "`## Why it matters`", "`## Success`", "`## Scope`", "`## Constraints`",
                     "`## Taste`", "`## Risks and assumptions`", "`## Decisions`", "`## Roadmap`",
                     "`- [ ] <bet>: serves <success signal> (size S/M/L)`", "never on a protected branch",
                     "Its AIM wins over STATE.md's in `/yah:where`", "`yah <project> brief`",
                     "**The task rounds**", "before Claude explores", "Why now", "what to leave out",
                     "prototype first, or build it right", "the brief's open `## Roadmap` lines",
                     "BRIEF.md is the only document it reads", "tied to BRIEF.md's Success signal and the AIM"):
            self.assertIn(want, brief, want)
        for gone in ("README's first 40 lines", "in at most three questions", "1. **The aim**"):
            self.assertNotIn(gone, self.readme, gone)
        self.assertIn("`yah shop brief`", self.readme)  # the task word next to `here`
        self.assertIn("`/yah:auto brief`", self.readme)
        writes = self.section("## What it reads, writes and never does")
        self.assertIn("`BRIEF.md` at the repo root", writes)
        self.assertIn("BRIEF.md is committed on the first plan phase's branch with that phase's work, never on a "
                      "protected branch.", writes)
        self.assertIn("| `skills/auto/brief.md` |", writes)
        state = self.section("## Plan state")
        self.assertIn("BRIEF.md's AIM, when it has one other than `none`, wins over it", state)
        self.assertIn("A `BRIEF: none` line here means you declined the [brief]", state)
        self.assertIn("A switch of branch carries the newest copy over", writes)
        self.assertIn("`yah shop brief` still runs the interview, since it writes only BRIEF.md", self.readme)

    def test_auto_merge_section_is_on_by_default(self):
        runs = self.section("## Hands-free runs")
        merge = runs[runs.index("#### Auto-merge: on by default"):runs.index("#### Critic and judge")]
        for want in ("`auto_merge: false` in config.json turns it off", "`setup.py --no-auto-merge`",
                     "`\"auto_merge\": false` in that repo's entry under `projects`",
                     "a check ran and every check passed, or no checks exist and yah's judge passed the PR's exact "
                     "head commit", "on `roles.judge` (Opus at high effort), or on GPT when [codex](#claude--gpt) is on",
                     "one `fix-findings` slice, then one re-judge", "**No stacking.**", "stops with exit 2",
                     "**The sweep.**", "the lowest closed phase of its plan whose PR is still open", "bottom-up"):
            self.assertIn(want, merge, want)
        self.assertNotIn("Turned on after a stacked plan finished", self.readme)  # the sweep replaced it
        self.assertNotIn("#auto-merge-opt-in", self.readme)
        self.assertNotIn("Auto-merge: opt-in", self.readme)
        self.assertGreaterEqual(self.readme.count("(#auto-merge-on-by-default)"), 8)
        self.assertIn("| `auto_merge` | Default `true`", self.section("## Config"))
        for want in ("any value but `true` turns it off", "a config.json that does not load as JSON"):
            self.assertIn(want, merge, want)
        # --plan goes on only after a merge while auto-merge is on: never "past exit 0" on a green PR
        plan_row = next(ln for ln in self.readme.splitlines() if ln.startswith("| `--plan` |"))
        self.assertNotIn("Go on past exit 0", plan_row)
        self.assertIn("a merge it skips stops the run with exit 2", plan_row)
        self.assertIn("or open and green (auto-merge off)", plan_row)
        self.assertIn("on the plan's last phase there is none, but the merge still waits on you", merge)
        self.assertIn("`auto_merge` once you answer whether to keep auto-merge", self.readme)
        self.assertNotIn("and `auto_merge` if you opt out", self.readme)  # a yes writes true too
        run_py = (ROOT / "scripts/run.py").read_text("utf-8")
        self.assertNotIn('help="after the phase\'s PR is green, go on to the next open', run_py)
        self.assertIn("a skipped merge stops", run_py)
        for words in ('"merged by yah"', '"plan done"'):  # the stop lines it quotes are the ones run.py writes
            self.assertIn(words.strip('"'), run_py)
            self.assertIn(words, merge if words == '"merged by yah"' else runs)


if __name__ == "__main__":
    unittest.main()
