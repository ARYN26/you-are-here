"""Tests for the skill and agent files. Stdlib unittest only:

    python -m unittest tests.test_skills

Every skill and agent description is in the model's context on every turn, so these tests keep
the listings short and the start, wrap and resume bodies to the rules the real-run test found.
They also keep beads frozen: STATE.md is the plan state, and beads only a repo that already uses it.
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = sorted(ROOT.glob("skills/*/SKILL.md"))
AGENTS = sorted(ROOT.glob("agents/*.md"))
MAX_DESC = 100
MAX_VISIBLE = 600
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
                         {"auto", "deep", "phases", "resume", "setup", "start", "where", "wrap"})
        self.assertEqual({p.stem for p in AGENTS}, {"deep", "scout"})

    def test_each_description_short(self):
        for f in SKILLS + AGENTS:
            with self.subTest(f=str(f.relative_to(ROOT))):
                desc = parse(f)[0].get("description", "")
                self.assertTrue(desc)
                self.assertLessEqual(len(desc), MAX_DESC)

    def test_model_visible_total(self):
        visible = [f for f in SKILLS + AGENTS if model_visible(f)]
        self.assertEqual(len(visible), 7)  # 5 skills + 2 agents; auto, resume and setup are user-only
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
        self.assertIn("paste in a terminal, e.g. `gh pr merge 13 --merge`", (ROOT / "RULES.md").read_text("utf-8"))

    def test_wrap_finish_gives_the_merge_command(self):
        finish = body("wrap").split("## 7. Finish with exactly this", 1)[1]
        self.assertIn("Merge   <the merge command>", finish)
        self.assertIn("`gh pr edit <N> --base <that PR's base>` once that PR merges", finish)
        self.assertIn("Leave the Merge line out when no PR is open", finish)


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

    def test_needs_human_writes_the_answer_into_next_then_starts_the_run(self):
        row = self.rows()[self.row("NEXT starts with `NEEDS-HUMAN:`")]
        self.assertIn("Ask that question as it is", row)
        self.assertIn("`yah:wrap` with the answer", row)
        self.assertIn("without `NEEDS-HUMAN:`", row)  # else the run's first session stops needs-human again
        self.assertIn("**start the run**", row)

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
        steps = ("Enter plan mode", "`yah:deep`", "AskUserQuestion", "`## Decisions`", "Then ExitPlanMode",
                 "`yah:phases`", "**start the run**")
        at = [plan.index(s) for s in steps]
        self.assertEqual(at, sorted(at))  # deep-check, then ask everything, then exit, then track, then run
        self.assertIn("before ExitPlanMode", plan)
        self.assertIn("a `branch` and a `base`", plan)  # yah run --plan stops at a phase with no branch
        self.assertIn("`NEEDS-HUMAN:` is only for what nobody could foresee", plan)
        # never the old hands-on ending: an approved plan goes to the run, not a foreground first phase
        self.assertNotIn("start its first phase", self.body)
        self.assertNotIn("`yah:start`", plan)

    def test_deep_on_every_tier_and_merges_stay_the_users(self):
        self.assertIn("every plan tier", self.body)
        self.assertIn("Never merge a PR yourself", self.body)
        self.assertIn("Only the run driver merges, and only when `auto_merge` is on", self.body)


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
        self.assertFalse(market["metadata"]["description"].lower().startswith("beads"))


if __name__ == "__main__":
    unittest.main()
