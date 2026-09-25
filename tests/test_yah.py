"""Tests for the yah scripts. Stdlib unittest only:

    python -m unittest discover -s tests -v

Every script runs as a subprocess with sys.executable and a temp CLAUDE_CONFIG_DIR, HOME and
USERPROFILE, so the real ~/.claude is never touched.
"""
import ast
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
GREEN, AMBER, RED, RST = "\033[32m", "\033[33m", "\033[31m", "\033[0m"
WEEK = 7 * 86400
FOOTER = ("This is the current state. Do not read docs to orient; /yah:where shows the full view. "
          "Reply first with one line (phase, NEXT, what waits on the user), before any tool call or branch change.")
PREMIUM = ("On Max plans Fable can use up to half of the weekly cap (it has its own usage bar); "
           "on Pro it needs extra-usage credits.")

BEADS = [
    {"id": "yah-1", "title": "Checkout rewrite", "issue_type": "epic", "status": "open", "labels": ["plan"],
     "spec_id": "docs/plans/checkout.md", "metadata": {"short": "CHECKOUT"}},
    {"id": "yah-2", "title": "P1 Cart API", "issue_type": "task", "status": "closed", "labels": ["phase"],
     "parent": "yah-1", "metadata": {"phase": 1, "branch": "checkout/cart", "base": "main"}, "external_ref": "gh-12"},
    {"id": "yah-3", "title": "P2 Payment form", "issue_type": "task", "status": "in_progress", "labels": ["phase"],
     "assignee": "Sam",  # a claimed phase is the work itself, never "waiting on you"
     "parent": "yah-1", "metadata": {"phase": 2, "branch": "checkout/payment", "base": "checkout/cart"},
     "notes": "Wire the Stripe element into PaymentForm.tsx, then run the e2e test.\nCart API is merged."},
    {"id": "yah-4", "title": "P3 Emails", "issue_type": "task", "status": "open", "labels": ["phase"],
     "parent": "yah-1", "metadata": {"phase": 3}},
    {"id": "yah-5", "title": "Approve the payment copy", "issue_type": "task", "status": "open", "labels": ["human"]},
    {"id": "yah-6", "title": "Rotate the Stripe test keys", "issue_type": "task", "status": "open", "assignee": "Sam"},
]
STATE_PLAN = """# Shop

## Plan: Checkout rewrite (docs/plans/checkout.md)
- [x] P1 Cart API | branch checkout/cart | PR #12
- [~] P2 Payment form · branch checkout/payment · base checkout/cart
- [ ] P3 Emails

## 2026-09-22
- Next: An older entry.

## 2026-09-23
- Next: Wire the Stripe element into PaymentForm.tsx, then run the e2e test.
"""
STATE_DATED = "# Notes\n\n## 2026-09-20\n- Next: Old.\n\n## 2026-09-23\n- Next: Ship the login fix,\n  then tag v1.2.\n"


def load_where():
    spec = importlib.util.spec_from_file_location("yah_where", SCRIPTS / "where.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="yah-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.cfg = self.tmp / "home", self.tmp / "claude"
        self.home.mkdir()
        self.cfg.mkdir()
        self.data = self.cfg / "you-are-here"
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.cfg), HOME=str(self.home), USERPROFILE=str(self.home),
                        GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com",
                        PYTHONDONTWRITEBYTECODE="1")  # Apple's python3 caches bytecode under $HOME/Library

    def py(self, script, *args, stdin="", cwd=None, scripts=SCRIPTS):
        p = subprocess.run([sys.executable, str(scripts / script), *args], input=stdin.encode("utf-8"),
                           capture_output=True, cwd=str(cwd or self.home), env=self.env, timeout=60)
        return p.stdout.decode("utf-8"), p.stderr.decode("utf-8"), p.returncode

    def config(self, **cfg):
        self.data.mkdir(exist_ok=True)
        (self.data / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def git(self, repo, *args):
        subprocess.run(["git", *args], cwd=str(repo), env=self.env, check=True, capture_output=True)

    def head(self, repo):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), env=self.env, check=True,
                              capture_output=True).stdout.decode().strip()

    def repo(self, files=None, branch="main", name="shop"):
        r = self.tmp / name
        r.mkdir()
        for rel, text in (files or {}).items():
            (r / rel).parent.mkdir(parents=True, exist_ok=True)
            (r / rel).write_text(text, encoding="utf-8")
        self.git(r, "init", "-q")
        self.git(r, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        self.git(r, "config", "user.name", "Sam")
        self.git(r, "add", "-A")
        self.git(r, "commit", "-q", "--allow-empty", "-m", "init")
        return r

    def beads_repo(self, branch="checkout/payment"):
        return self.repo({".beads/issues.jsonl": "\n".join(json.dumps(b) for b in BEADS) + "\n"}, branch)

    def where(self, repo, *args):
        out, err, rc = self.py("where.py", "--no-gh", "--no-bd", *args, cwd=repo)
        self.assertEqual(rc, 0, err)
        return out

    def transcript(self, tokens):
        f = self.tmp / f"t{tokens}.jsonl"
        rec = {"type": "assistant", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": tokens - 10}}}
        f.write_text(json.dumps({"type": "user"}) + "\n" + json.dumps(rec) + "\n", encoding="utf-8")
        return str(f)


# ---------------------------------------------------------------- statusline

class StatuslineTests(Base):
    def line(self, tokens=50_000, model=("claude-opus-5-5", "Opus 5.5"), cwd=None, **extra):
        d = dict({"session_id": "s1", "model": {"id": model[0], "display_name": model[1]}, "effort": "high",
                  "context_window": {"current_usage": {"input_tokens": tokens}},
                  "workspace": {"current_dir": str(cwd or self.home)}}, **extra)
        out, err, rc = self.py("statusline.py", stdin=json.dumps(d))
        self.assertEqual((rc, err), (0, ""))
        return out

    def test_api_user_gets_no_limit_fields(self):
        out = self.line()
        self.assertIn("Opus 5.5 high", out)
        self.assertIn(f"{GREEN}50k{RST}", out)
        for bad in ("5h ", "wk ", "error", "FABLE"):
            self.assertNotIn(bad, out)
        self.assertFalse((self.data / "limits.json").exists())
        self.assertEqual(json.loads((self.data / "state-s1.json").read_text("utf-8"))["premium"], "")

    def test_rate_limits_pace_and_limits_json(self):
        now = time.time()
        rl = {"five_hour": {"used_percentage": 22, "resets_at": now + 3600},
              "seven_day": {"used_percentage": 41, "resets_at": now + 0.65 * WEEK},
              "seven_day_opus": {"used_percentage": 60}}
        out = self.line(rate_limits=rl)
        self.assertIn("5h 22%", out)
        self.assertIn("wk 41% (pace 35%)", out)
        self.assertNotIn(f"{AMBER}wk 41", out)
        self.assertIn(f"{AMBER}wk opus 60%", out)
        lim = json.loads((self.data / "limits.json").read_text("utf-8"))
        self.assertEqual((lim["five_hour"]["used_pct"], lim["seven_day"]["used_pct"], lim["pace_pct"]), (22, 41, 35))
        self.assertEqual(lim["five_hour"]["resets_at"], round(now + 3600))
        (self.data / "limits.json").write_text(json.dumps(dict(lim, ts=1)), encoding="utf-8")
        self.line(rate_limits=rl)  # same numbers: not rewritten
        self.assertEqual(json.loads((self.data / "limits.json").read_text("utf-8"))["ts"], 1)
        rl["seven_day"]["used_percentage"] = 50
        self.assertIn(f"{AMBER}wk 50%", self.line(rate_limits=rl))
        self.assertNotEqual(json.loads((self.data / "limits.json").read_text("utf-8"))["ts"], 1)
        rl["seven_day"]["used_percentage"] = 60
        self.assertIn(f"{RED}wk 60%", self.line(rate_limits=rl))

    def test_premium_tag(self):
        out = self.line(model=("claude-fable-5-1", "Fable 5.1"))
        self.assertIn(" FABLE ", out)
        self.assertEqual(json.loads((self.data / "state-s1.json").read_text("utf-8"))["premium"], "FABLE")
        self.config(premium_models=["opus"])
        self.assertIn(" OPUS ", self.line())

    def test_colour_thresholds_per_tier(self):
        cases = [(None, 110_000, GREEN, ""), (None, 130_000, AMBER, ""), (None, 150_000, RED, " wrap"),
                 ("pro", 90_000, GREEN, ""), ("pro", 110_000, AMBER, ""), ("pro", 125_000, RED, " wrap"),
                 ("max20", 140_000, GREEN, ""), ("max20", 160_000, AMBER, ""), ("max20", 210_000, RED, " wrap"),
                 ("api", 85_000, AMBER, ""), ("api", 100_000, RED, " wrap")]
        for tier, tokens, colour, wrap in cases:
            with self.subTest(tier=tier, tokens=tokens):
                if tier:
                    self.config(tier=tier)
                self.assertIn(f"{colour}{round(tokens / 1000)}k{wrap}{RST}", self.line(tokens))
        self.config(tier="max20", amber=50_000)  # explicit keys beat the preset
        self.assertIn(f"{AMBER}60k{RST}", self.line(60_000))

    def test_where_cache_shows_plan_phase_and_for_you(self):
        repo = self.beads_repo()
        self.where(repo)
        out = self.line(cwd=repo, pr={"number": 8})
        for want in ("checkout/payment", "PR#8", "CHECKOUT P2/3", "2 for you"):
            self.assertIn(want, out)

    def test_timing(self):
        repo = self.beads_repo()
        self.where(repo)
        best = 99.0
        for _ in range(3):
            t = time.perf_counter()
            self.line(cwd=repo)
            best = min(best, time.perf_counter() - t)
        if best > 0.3:
            sys.stderr.write(f"\nWARNING: statusline took {best * 1000:.0f} ms (budget 300 ms)\n")
        self.assertLess(best, 1.5)


# ---------------------------------------------------------------- context guard

class GuardTests(Base):
    def guard(self, tokens=0, prompt="go on", sid="g1"):
        d = {"session_id": sid, "prompt": prompt, "transcript_path": self.transcript(tokens) if tokens else ""}
        out, err, rc = self.py("context_guard.py", stdin=json.dumps(d))
        self.assertEqual((rc, err), (0, ""))
        return json.loads(out) if out.strip() else None

    def state(self, sid, **kv):
        self.data.mkdir(exist_ok=True)
        (self.data / f"state-{sid}.json").write_text(json.dumps(kv), encoding="utf-8")

    def test_fires_once_per_threshold_and_rearms(self):
        r = self.guard(160_000)
        self.assertIn("wrap after this step", r["systemMessage"])
        self.assertEqual(r["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertTrue(r["hookSpecificOutput"]["additionalContext"].startswith("[yah] Context is 160k"))
        self.assertIn("/yah:wrap", r["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.guard(160_000))
        self.assertIn("wrap now", self.guard(210_000)["systemMessage"])
        self.assertIsNone(self.guard(210_000))
        self.assertIsNone(self.guard(100_000))  # below amber: re-arms
        self.assertIn("wrap after this step", self.guard(160_000)["systemMessage"])

    def test_tier_sets_thresholds(self):
        self.config(tier="pro")
        self.assertIn("wrap after this step", self.guard(125_000)["systemMessage"])
        self.assertIn("wrap now", self.guard(165_000)["systemMessage"])

    def test_skips_wrap_and_session_commands(self):
        for prompt in ("/yah:wrap", "/yah:wrap phase done", "/clear", "/compact", "/exit"):
            self.assertIsNone(self.guard(210_000, prompt=prompt))
        self.assertIn("wrap now", self.guard(210_000)["systemMessage"])

    def test_premium_nudge_once(self):
        self.state("g2", premium="FABLE", tokens=1000)
        r = self.guard(sid="g2")
        ctx = r["hookSpecificOutput"]["additionalContext"]
        self.assertIn(PREMIUM, ctx)
        for bad in ("separate pool", "4x"):
            self.assertNotIn(bad, ctx + r["systemMessage"])
        self.assertIn("premium", r["systemMessage"])
        self.assertIsNone(self.guard(sid="g2"))

    def test_daily_pace_nudge(self):
        self.state("g3", week=40, pace=30, tokens=1000)  # within pace_slack 15
        self.assertIsNone(self.guard(sid="g3"))
        self.state("g4", week=60, pace=30, tokens=1000)
        self.assertIn("Weekly 60% vs 30%", self.guard(sid="g4")["systemMessage"])
        self.assertIsNone(self.guard(sid="g4"))
        self.state("g5", week=60, pace=30, tokens=1000)
        self.assertIsNone(self.guard(sid="g5"))  # once a day, across sessions

    def test_ultracode_rules_once_per_session(self):
        self.config(ultracode=True)
        r = self.guard(sid="u1")
        self.assertIn("Ultracode is on", r["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("systemMessage", r)  # model-only: nothing shown to the user
        self.assertIsNone(self.guard(sid="u1"))
        self.state("u2", week=60, pace=30, tokens=1000)
        ctx = self.guard(sid="u2")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("under 5 agents", ctx)
        self.assertNotIn("high over xhigh", ctx)


# ---------------------------------------------------------------- where.py

class WhereTests(Base):
    def test_beads_brief_and_full(self):
        repo = self.beads_repo()
        brief = self.where(repo, "--brief").splitlines()
        self.assertLessEqual(len(brief), 6)
        self.assertEqual(brief, [
            "[yah] shop  branch checkout/payment (clean, no upstream)",
            "PLAN CHECKOUT 1/3 done  PHASE P2 Payment form (yah-3)",
            "NEXT Wire the Stripe element into PaymentForm.tsx, then run the e2e test.",
            "2 waiting on you",
            FOOTER])
        full = self.where(repo).splitlines()
        self.assertLessEqual(len(full), 15)
        for want in ("PLAN    Checkout rewrite  [1/3 done]  yah-1  checkout.md",
                     "PHASE   P2 Payment form  in_progress  yah-3  branch checkout/payment",
                     "NEXT    Wire the Stripe element", "YOU     yah-5  Approve the payment copy",
                     "        yah-6  Rotate the Stripe test keys"):
            self.assertTrue(any(ln.startswith(want) for ln in full), want)
        self.assertFalse(any("yah-3" in ln for ln in full if not ln.startswith("PHASE")), full)  # not in YOU
        s = json.loads(self.where(repo, "--json"))
        self.assertEqual((s["beads"]["plan"]["source"], s["beads"]["phase"]["base"]), ("beads", "checkout/cart"))
        self.assertEqual([h["id"] for h in s["beads"]["human"]], ["yah-5", "yah-6"])
        self.assertIsNone(s["next_stale"])  # no stamp, no flag
        self.assertNotIn("predates", "\n".join(brief + full))

    def stamp_beads(self, repo, sha):
        beads = [dict(b, metadata=dict(b["metadata"], next_sha=sha)) if b["id"] == "yah-3" else b for b in BEADS]
        (repo / ".beads" / "issues.jsonl").write_text("\n".join(json.dumps(b) for b in beads) + "\n", "utf-8")

    def test_stale_next_from_beads_counts_only_the_phase_branch(self):
        repo = self.beads_repo(branch="checkout/cart")  # NEXT written on the base, before the phase branch
        self.stamp_beads(repo, self.head(repo))
        self.git(repo, "commit", "-qam", "plan")  # base commits after the stamp never count
        self.git(repo, "checkout", "-q", "-b", "checkout/payment")
        brief = self.where(repo, "--brief")
        self.assertNotIn("predates", brief)
        self.assertIn("branch checkout/payment (clean, 0 ahead of checkout/cart, no upstream)", brief)
        for i in (1, 2):
            self.git(repo, "commit", "-q", "--allow-empty", "-m", f"work {i}")
        warn = "NEXT predates 2 commits on checkout/payment: /yah:wrap first"
        brief = self.where(repo, "--brief").splitlines()
        self.assertLessEqual(len(brief), 6)
        self.assertEqual(brief[2], f"NEXT Wire the Stripe element into PaymentForm.tsx, then run the e2e test.  ! {warn}")
        self.assertIn("(clean, 2 ahead of checkout/cart, no upstream)", brief[0])
        full = self.where(repo).splitlines()
        self.assertEqual(full[full.index("NEXT    Wire the Stripe element into PaymentForm.tsx, then run the e2e test.") + 1],
                         f"!       {warn}")
        s = json.loads(self.where(repo, "--json"))
        self.assertEqual((s["next_stale"]["commits"], s["next_stale"]["branch"]), (2, "checkout/payment"))
        self.git(repo, "checkout", "-q", "checkout/cart")  # off the phase branch: still counts the phase branch
        self.assertIn(warn, self.where(repo, "--brief"))
        self.git(repo, "checkout", "-q", "checkout/payment")
        self.stamp_beads(repo, self.head(repo)[:7])  # wrap re-stamps after its WIP commit
        self.assertNotIn("predates", self.where(repo, "--brief"))
        for bad in ("--output=x", "HEAD", "zzzzzzz", ""):  # never passed to git as an option or a ref
            self.stamp_beads(repo, bad)
            self.assertIsNone(json.loads(self.where(repo, "--json"))["next_stale"], bad)

    def test_stale_next_from_state_md(self):
        repo = self.repo({"STATE.md": STATE_DATED})  # tracked: its own last commit is the stamp
        self.assertNotIn("predates", self.where(repo, "--brief"))
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "work")
        brief = self.where(repo, "--brief")
        self.assertIn("NEXT Ship the login fix, then tag v1.2.  ! NEXT predates 1 commit on main: /yah:wrap first", brief)
        self.assertIn("!       NEXT predates 1 commit on main", self.where(repo))
        self.git(repo, "checkout", "-q", "--detach")  # no branch: wrap's `git log <sha>..HEAD` still works
        self.assertEqual(json.loads(self.where(repo, "--json"))["next_stale"]["branch"], "HEAD")
        self.git(repo, "checkout", "-q", "main")
        (repo / "STATE.md").write_text(STATE_DATED.replace("Ship", "Now ship"), encoding="utf-8")
        self.assertNotIn("predates", self.where(repo, "--brief"))  # being rewritten: not stale
        other = self.repo(name="blog", branch="checkout/payment")  # untracked: the `- At:` line wrap writes
        sha = self.head(other)
        text = STATE_PLAN.replace("then run the e2e test.", "then run the e2e test.\n- At: " + sha[:9])
        (other / "STATE.md").write_text(text, encoding="utf-8")
        self.assertNotIn("predates", self.where(other, "--brief"))
        self.git(other, "commit", "-q", "--allow-empty", "-m", "work")
        self.assertIn("! NEXT predates 1 commit on checkout/payment: /yah:wrap first", self.where(other, "--brief"))
        self.assertIn("PaymentForm.tsx, then run the e2e test.  !", self.where(other, "--brief"))  # At: is not NEXT
        (other / "STATE.md").write_text(text.replace("- [~] P2", "- [ ] P2"), encoding="utf-8")
        self.assertNotIn("predates", self.where(other, "--brief"))  # no running phase: nothing to be stale

    def test_stale_next_skips_a_base_merge_that_touched_state_md(self):
        repo = self.repo({"STATE.md": STATE_DATED})  # tracked and plan-less
        self.git(repo, "checkout", "-q", "-b", "feat/a")
        (repo / "STATE.md").write_text(STATE_DATED.replace("Ship", "Now ship"), encoding="utf-8")
        self.git(repo, "commit", "-qam", "wrap")  # NEXT is written here
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "work")
        self.git(repo, "checkout", "-q", "main")
        (repo / "STATE.md").write_text(STATE_DATED.replace("Old.", "Older."), encoding="utf-8")
        self.git(repo, "commit", "-qam", "base notes")
        self.git(repo, "checkout", "-q", "feat/a")
        self.git(repo, "merge", "-q", "--no-edit", "main")  # touches STATE.md, but is not a wrap
        s = json.loads(self.where(repo, "--json"))
        self.assertEqual((s["next_stale"]["commits"], s["next_stale"]["branch"]), (1, "feat/a"))  # the work only

    def test_stale_next_when_the_phase_base_is_gone(self):
        repo = self.beads_repo(branch="checkout/cart")
        init = self.head(repo)
        self.stamp_beads(repo, init)  # /yah:phases stamps every phase at plan time
        self.git(repo, "commit", "-qam", "plan")
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "cart work")
        self.git(repo, "checkout", "-q", "-b", "main", init)
        self.git(repo, "merge", "-q", "--no-ff", "-m", "merge P1", "checkout/cart")
        self.git(repo, "checkout", "-q", "-b", "checkout/payment", "checkout/cart")
        self.git(repo, "branch", "-q", "-D", "checkout/cart")  # merged and deleted: P1's commits are main's now
        self.assertIsNone(json.loads(self.where(repo, "--json"))["next_stale"])
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "pay work")
        self.assertEqual(json.loads(self.where(repo, "--json"))["next_stale"]["commits"], 1)

    def test_base_ahead_counts_past_a_stale_local_base(self):
        repo = self.repo(branch="main")
        old = self.head(repo)
        self.git(repo, "checkout", "-q", "-b", "feat/x")
        for i in (1, 2, 3):
            self.git(repo, "commit", "-q", "--allow-empty", "-m", f"dev {i}")
        self.git(repo, "update-ref", "refs/remotes/origin/aryan_dev", "HEAD")  # cut from origin's aryan_dev
        self.git(repo, "branch", "-q", "aryan_dev", old)  # the local copy was never pulled
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "work")
        s = json.loads(self.where(repo, "--json"))
        self.assertEqual((s["git"]["base"], s["git"]["base_ahead"]), ("aryan_dev", 1))

    def test_protects_a_base_synced_in_with_merge(self):
        repo = self.repo(branch="main")
        self.git(repo, "checkout", "-q", "-b", "feat/old")
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "old")
        self.git(repo, "checkout", "-q", "main")
        self.git(repo, "merge", "-q", "--no-ff", "-m", "merge feat/old", "feat/old")
        self.git(repo, "checkout", "-q", "-b", "feat/y")
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "y")
        s = json.loads(self.where(repo, "--json"))  # feat/old is off the chain, but main has it: not synced in
        self.assertEqual((s["ancestors"], s["merged_in"]), (["main"], []))
        self.git(repo, "checkout", "-q", "main")
        self.git(repo, "checkout", "-q", "-b", "aryan_dev")
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "dev")
        self.git(repo, "checkout", "-q", "-b", "feat/x")
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "work")
        self.git(repo, "checkout", "-q", "aryan_dev")
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "dev 2")
        self.git(repo, "checkout", "-q", "feat/x")
        self.git(repo, "merge", "-q", "--no-ff", "-m", "sync aryan_dev", "aryan_dev")  # its tip is off the chain now
        s = json.loads(self.where(repo, "--json"))
        self.assertEqual((s["ancestors"], s["merged_in"]), (["main"], ["aryan_dev"]))
        self.assertIn("aryan_dev", s["protected"])
        self.assertNotIn("PROD", self.where(repo))  # protected, but never inferred as PROD

    def test_a_claimed_bead_is_not_waiting_on_you(self):
        w = load_where()
        issues = [{"id": "x-1", "title": "Fix the flaky test", "issue_type": "task", "status": "in_progress",
                   "assignee": "Sam"},  # `bd update --claim` assigns git user.name
                  {"id": "x-2", "title": "Rotate the keys", "issue_type": "task", "status": "open", "assignee": "Sam"},
                  {"id": "x-3", "title": "Approve the copy", "issue_type": "task", "status": "in_progress",
                   "labels": ["human"]}]
        self.assertEqual([i["id"] for i in w.beads_state(issues, ["Sam"])["human"]], ["x-2", "x-3"])

    def test_footer_maps_other_plugins_skill_names(self):
        repo = self.repo({"CLAUDE.md": "End each step with /acme:wrap; start with `/acme:where`.\n"
                                       "Not these: https://x.io/acme:start, /yah:wrap, /acme:build, "
                                       "/acme:wrap-up, /acme:deep-dive.\n",
                          "plans/p.md": "Then run /acme:phases.\n",
                          "STATE.md": "## Plan: P (plans/p.md)\n- [~] P1 Go\n"}, branch="feat/a")
        self.assertEqual(self.where(repo, "--brief").splitlines()[-1],
                         FOOTER + " The plan or CLAUDE.md says /acme:where, /acme:wrap, /acme:phases; unless those "
                                  "skills are listed, use /yah:where, /yah:wrap, /yah:phases.")
        (self.cfg / "CLAUDE.md").write_text("Hard bugs: /zed:deep.\n", encoding="utf-8")  # the user's own
        self.assertIn("/acme:phases, /zed:deep; unless", self.where(repo, "--brief"))
        self.assertNotIn("The plan or CLAUDE.md", self.where(repo))  # the full view has no footer
        self.assertEqual(self.where(self.repo(name="plain"), "--brief").splitlines()[-1],
                         FOOTER + " The plan or CLAUDE.md says /zed:deep; unless those skills are listed, "
                                  "use /yah:deep.")

    def test_you_from_config_beats_git_user_name(self):
        self.config(projects={"shop": {"you": ["Nobody"]}})
        self.assertIn("1 waiting on you", self.where(self.beads_repo(), "--brief"))

    def test_brief_worst_case_is_six_lines(self):
        self.config(projects={"shop": {"prod": "main deploys on push. PRs only."}})
        repo = self.beads_repo(branch="main")
        brief = self.where(repo, "--brief").splitlines()
        self.assertEqual(len(brief), 6)
        self.assertTrue(brief[0].endswith("! phase branch is checkout/payment"))
        self.assertEqual(brief[4], "PROD main deploys on push. PRs only.")
        full = self.where(repo)
        self.assertIn("!       phase branch is checkout/payment, you are on main", full)
        self.assertIn("PROD    main deploys on push. PRs only.", full)

    def test_state_md_plan(self):
        repo = self.repo({"STATE.md": STATE_PLAN}, branch="checkout/payment")
        brief = self.where(repo, "--brief")
        self.assertLessEqual(len(brief.splitlines()), 6)
        self.assertIn("PLAN CHECKOUT 1/3 done  PHASE P2 Payment form (STATE.md)", brief)
        self.assertIn("NEXT Wire the Stripe element into PaymentForm.tsx, then run the e2e test.", brief)
        self.assertNotIn("older", brief)
        self.assertIn("PLAN    Checkout rewrite  [1/3 done]  STATE.md  checkout.md", self.where(repo))
        b = json.loads(self.where(repo, "--json"))["beads"]
        self.assertEqual([(p["label"], p["status"]) for p in b["phases"]],
                         [("P1", "closed"), ("P2", "in_progress"), ("P3", "open")])
        self.assertEqual((b["phases"][0]["pr"], b["phase"]["branch"], b["phase"]["base"], b["plan"]["source"]),
                         ("#12", "checkout/payment", "checkout/cart", "STATE.md"))

    def test_state_md_plan_without_running_phase(self):
        text = "## Plan: Emails\n- [x] P1 Templates\n- [ ] P2 Sending\n\n## 2026-09-23\n- Next: Pick a mail provider.\n"
        out = self.where(self.repo({"STATE.md": text}))
        self.assertIn("PHASE   none in progress. Next: P2 Sending  (mark it [~] in STATE.md)", out)
        self.assertIn("NEXT    Pick a mail provider.", out)

    def test_state_md_dated_only(self):
        repo = self.repo({"STATE.md": STATE_DATED})
        self.assertIn("STATE.md 2026-09-23  NEXT Ship the login fix, then tag v1.2.", self.where(repo, "--brief"))
        full = self.where(repo)
        self.assertIn("STATE   STATE.md: 2026-09-23", full)
        self.assertIn("NEXT    Ship the login fix, then tag v1.2.", full)
        self.assertNotIn("PLAN", full)

    def test_state_md_matches_beads(self):
        """The same plan in STATE.md and in beads gives the same plan, phase, NEXT and waiting-on-you."""
        text = STATE_PLAN.replace("- [ ] P3 Emails", "- [ ] P3 Emails\n\n## Follow-ups\n"
                                  "- [ ] Approve the payment copy (you)\n- [ ] Rotate the Stripe test keys (you)")
        md = json.loads(self.where(self.repo({"STATE.md": text}, branch="checkout/payment", name="md"), "--json"))
        bd = json.loads(self.where(self.beads_repo(), "--json"))

        def view(s):
            b = s["beads"]
            return ({k: b["plan"][k] for k in ("title", "short", "spec", "done", "total")},
                    [(p["label"], p["title"], p["status"], p["branch"]) for p in b["phases"]],
                    {k: b["phase"][k] for k in ("label", "branch", "base", "next")},
                    [h["title"] for h in b["human"]], s["protected"])
        self.assertEqual(view(md)[:4], view(bd)[:4])
        self.assertEqual(set(view(md)[4]), set(view(bd)[4]))

    def test_state_md_you_marker(self):
        text = ("## Plan: Launch\n- [x] P1 Build | branch launch/build\n- [ ] P2 Merge PR #12 (you) | PR #12\n\n"
                "## Follow-ups\n- [ ] Rotate the test keys (you)\n- [x] Old console step (you)\n- [ ] Flaky e2e test\n"
                "- [ ] Ask (you) about it later\n\n"
                "## 2026-09-23\n- Next: Wait for review.\n")
        repo = self.repo({"STATE.md": text})
        s = json.loads(self.where(repo, "--json"))
        self.assertEqual(s["beads"]["human"], [{"id": "STATE.md:3", "title": "P2 Merge PR #12"},
                                               {"id": "STATE.md:6", "title": "Rotate the test keys"}])
        self.assertEqual((s["beads"]["plan"]["total"], s["beads"]["next_phase"]["label"]), (2, "P2"))
        self.assertNotIn("human", s["state_md"])
        full = self.where(repo)
        self.assertIn("YOU     STATE.md:3  P2 Merge PR #12", full)
        self.assertIn("        STATE.md:6  Rotate the test keys", full)
        self.assertIn("2 waiting on you", self.where(repo, "--brief"))
        cache = next(self.data.glob("where-*.json"))  # the statusline's "for you" count
        self.assertEqual(json.loads(cache.read_text(encoding="utf-8"))["human"], 2)

    def test_state_md_you_after_fields_and_fences(self):
        text = ("## Plan: Launch\n- [ ] P1 Ship | branch launch/ship | PR #13 (you)\n\n"
                "```markdown\n- [ ] An example line (you)\n```\n")
        s = json.loads(self.where(self.repo({"STATE.md": text}), "--json"))
        self.assertEqual(s["beads"]["human"], [{"id": "STATE.md:2", "title": "P1 Ship"}])
        p = s["beads"]["phases"][0]
        self.assertEqual((p["title"], p["branch"], p["pr"]), ("Ship", "launch/ship", "#13"))

    def test_state_md_you_with_a_beads_plan(self):
        repo = self.beads_repo()
        (repo / "STATE.md").write_text("## Follow-ups\n- [ ] Rotate the prod keys (you)\n", encoding="utf-8")
        s = json.loads(self.where(repo, "--json"))
        self.assertEqual(s["beads"]["plan"]["source"], "beads")
        self.assertEqual([h["id"] for h in s["beads"]["human"]], ["yah-5", "yah-6", "STATE.md:2"])

    def test_state_md_you_without_plan(self):
        repo = self.repo({"STATE.md": STATE_DATED + "\n## Follow-ups\n- [ ] Rotate the test keys (you)\n"})
        full = self.where(repo)
        self.assertIn("NEXT    Ship the login fix, then tag v1.2.", full)
        self.assertIn("YOU     STATE.md:11  Rotate the test keys", full)
        self.assertNotIn("BEADS", full)
        self.assertNotIn("PLAN", full)

    def test_state_md_picks_the_running_plan(self):
        done = "## Plan: Old (plans/old.md)\n- [x] P1 Shipped\n\n"
        cases = {"running": done + "## Plan: New (plans/new.md)\n- [x] P1 A\n- [~] P2 B\n",
                 "open": done + "## Plan: New (plans/new.md)\n- [ ] P1 A\n",
                 "running over open": "## Plan: Old\n- [ ] P1 Later\n\n## Plan: New\n- [~] P1 Now\n"}
        for i, (case, text) in enumerate(cases.items()):
            with self.subTest(case=case):
                s = json.loads(self.where(self.repo({"STATE.md": text}, name=f"r{i}"), "--json"))
                self.assertEqual(s["beads"]["plan"]["title"], "New")
        s = json.loads(self.where(self.repo({"STATE.md": done}, name="all-done"), "--json"))
        self.assertEqual((s["beads"]["plan"]["title"], s["beads"]["phase"], s["beads"]["next_phase"]), ("Old", None, None))

    def test_path(self):
        repo = self.beads_repo()
        self.where(repo)
        for name in ("shop", "SHOP"):
            out, err, rc = self.py("where.py", "--path", name)
            self.assertEqual(rc, 0, err)
            self.assertEqual(Path(out.strip()).resolve(), repo)
        out, err, rc = self.py("where.py", "--path", "nope")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("no project named 'nope'", err)
        self.assertIn("shop", err)
        out, err, rc = self.py("where.py", "--path")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("usage: yah <project>", err)
        other = self.repo(name="blog")
        self.config(projects={"blog": {"path": str(other)}})
        out, _, rc = self.py("where.py", "--path", "blog")
        self.assertEqual((rc, Path(out.strip()).resolve()), (0, other))

    def test_home_view(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.assertEqual(self.py("where.py", "--brief")[0], "")  # home, no projects yet
        self.assertEqual(self.py("where.py", "--brief", cwd=elsewhere)[0], "")  # not a repo, not home
        self.where(self.beads_repo())
        full = self.py("where.py")[0].splitlines()
        self.assertTrue(full[0].startswith("PROJECTS  (start one with: yah <name>)"))
        self.assertTrue(any(ln.startswith("shop") and "CHECKOUT P2/3 Payment form" in ln for ln in full), full)
        brief = self.py("where.py", "--brief")[0].splitlines()
        self.assertLessEqual(len(brief), 6)
        self.assertTrue(brief[0].startswith("[yah] Session is in the home dir"))
        self.assertIn("yah <name>", brief[0])
        self.config(recent_days=0)  # the cache is now too old to count
        self.assertEqual(self.py("where.py", "--brief")[0], "")

    def test_protects_the_phase_base_and_the_branch_head_was_cut_from(self):
        repo = self.repo(branch="main")  # no plan, no remote, no gh
        self.git(repo, "checkout", "-q", "-b", "aryan_dev")
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "dev")
        self.git(repo, "checkout", "-q", "-b", "feat/x")  # tip is HEAD: 0 commits away, main is 1
        s = json.loads(self.where(repo, "--json"))
        self.assertEqual((s["ancestors"], s["bases"]), (["aryan_dev"], []))
        self.assertIn("aryan_dev", s["protected"])
        self.assertIn("PROD    `aryan_dev` inferred: this branch was cut from it. Never push or commit it; "
                      "set prod in config.json", self.where(repo))
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "work")
        self.assertEqual(json.loads(self.where(repo, "--json"))["ancestors"], ["aryan_dev"])
        self.git(repo, "checkout", "-q", "-b", "docs/y", "aryan_dev")  # a docs PR branch, merged into this one
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "docs")
        self.git(repo, "checkout", "-q", "feat/x")
        self.git(repo, "merge", "-q", "--no-ff", "-m", "merge docs/y", "docs/y")
        s = json.loads(self.where(repo, "--json"))
        self.assertEqual(s["ancestors"], ["aryan_dev"])  # docs/y is only a second parent, and nearer by B..HEAD
        self.assertNotIn("docs/y", s["protected"])
        self.assertIn("aryan_dev", s["protected"])
        self.assertEqual((s["git"]["base"], s["git"]["base_ahead"]), ("aryan_dev", 2))  # work + the merge
        self.assertIn("BRANCH  feat/x  clean, 2 ahead of aryan_dev, no upstream", self.where(repo))
        (repo / "STATE.md").write_text("## Plan: X\n- [~] P1 Fix | branch feat/x | base staging\n", encoding="utf-8")
        s = json.loads(self.where(repo, "--json"))
        self.assertEqual(s["bases"], ["staging"])
        self.assertTrue({"aryan_dev", "staging"} <= set(s["protected"]), s["protected"])
        self.assertIn("PROD    `staging` inferred: phase PRs target it.", self.where(repo))
        cache = next(self.data.glob("where-*.json"))  # where open PRs landed when gh last saw any
        cache.write_text(json.dumps(dict(json.loads(cache.read_text("utf-8")), landing=["feat/x", "live"])), "utf-8")
        s = json.loads(self.where(repo, "--json"))
        self.assertTrue({"live", "aryan_dev"} <= set(s["protected"]), s["protected"])
        self.assertNotIn("feat/x", s["protected"])  # the phase's own branch never is
        (repo / "STATE.md").unlink()
        cache.unlink()
        self.git(repo, "checkout", "-q", "main")  # nothing main descends from: only trunks, so no PROD line
        self.assertNotIn("PROD", self.where(repo))
        self.git(repo, "checkout", "-q", "-b", "feat/old")
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "old")
        self.git(repo, "checkout", "-q", "main")
        self.git(repo, "merge", "-q", "--no-ff", "-m", "merge feat/old", "feat/old")  # an ancestor of main now
        s = json.loads(self.where(repo, "--json"))
        self.assertEqual(s["ancestors"], [])  # on a trunk, "cut from" means nothing
        self.assertNotIn("feat/old", s["protected"])
        self.assertNotIn("PROD", self.where(repo))

    def test_bd_is_null_without_beads_and_pinned_to_this_repos_beads(self):
        bin_dir, fake = self.tmp / "bin", self.tmp / "fake_bd.py"
        bin_dir.mkdir()
        fake.write_text("import json, os\nprint(json.dumps([{'id': 'x-1', 'issue_type': 'task', 'status': "
                        "'in_progress', 'title': os.environ.get('BEADS_DIR', '-')}]))\n", encoding="utf-8")
        if os.name == "nt":
            (bin_dir / "bd.cmd").write_text(f'@"{sys.executable}" "{fake}" %*\r\n', encoding="utf-8")
        else:
            (bin_dir / "bd").write_text(f"#!{sys.executable}\n" + fake.read_text("utf-8"), encoding="utf-8")
            (bin_dir / "bd").chmod(0o755)
        self.env.update(PATH=str(bin_dir) + os.pathsep + self.env.get("PATH", ""),
                        BEADS_DIR=str(self.tmp / "other" / ".beads"))  # another repo's database
        plain = self.repo(name="plain")
        out, err, rc = self.py("where.py", "--no-gh", "--json", cwd=plain)
        self.assertEqual(rc, 0, err)
        self.assertEqual((json.loads(out)["bd"], json.loads(out)["beads_dir"]), (None, None))
        repo = self.beads_repo()
        out, err, rc = self.py("where.py", "--no-gh", "--json", cwd=repo)
        s = json.loads(out)
        self.assertEqual(s["beads_source"], "bd", err)
        self.assertEqual(Path(s["beads"]["in_progress"][0]["title"]), repo / ".beads")
        self.assertIsNone(s["bd"])  # the model's own bd calls would inherit the foreign BEADS_DIR
        self.assertIn("BEADS_DIR", s["bd_note"])
        del self.env["BEADS_DIR"]
        s = json.loads(self.py("where.py", "--no-gh", "--json", cwd=repo)[0])
        self.assertTrue(Path(s["bd"]).name.startswith("bd"))
        self.assertIsNone(s["bd_note"])

    def test_same_named_repos_keep_separate_caches(self):
        (self.tmp / "a").mkdir()
        (self.tmp / "b").mkdir()
        a = self.repo({"STATE.md": STATE_PLAN}, branch="checkout/payment", name="a/api")
        b = self.repo({"STATE.md": STATE_DATED}, name="b/api")
        for r in (a, b):
            self.where(r)
        caches = sorted(p.name for p in self.data.glob("where-*.json"))
        self.assertEqual(len(caches), 2)
        for name in caches:
            self.assertRegex(name, r"^where-api-[0-9a-f]{8}\.json$")
        full = self.py("where.py")[0].splitlines()  # the home view shows both, under the same name
        self.assertEqual(len([ln for ln in full if ln.startswith("api ")]), 2, full)
        self.assertTrue(any("CHECKOUT P2/3" in ln for ln in full), full)
        out, err, rc = self.py("where.py", "--path", "api")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("'api' matches 2 repos", err)
        self.assertIn(str(a), err)
        self.assertIn(str(b), err)
        self.config(projects={"api-b": {"path": str(b)}})  # a registered repo keeps a plain name
        self.where(b)
        self.assertTrue((self.data / "where-api-b.json").is_file())
        self.assertEqual(Path(self.py("where.py", "--path", "api")[0].strip()).resolve(), a)


class PrLinesTests(unittest.TestCase):
    def test_stacking_respects_trunks(self):
        w = load_where()
        green = [{"conclusion": "SUCCESS"}]
        prs = [{"number": 1, "headRefName": "feature/a", "baseRefName": "main", "statusCheckRollup": green},
               {"number": 2, "headRefName": "feature/b", "baseRefName": "feature/a", "statusCheckRollup": green},
               {"number": 5, "headRefName": "release", "baseRefName": "main"},
               {"number": 6, "headRefName": "hotfix", "baseRefName": "release"}]
        phases = {"phases": [{"label": "P1", "pr": "gh-1", "branch": ""}, {"label": "P2", "pr": "", "branch": "feature/b"}]}
        lines, index = w.pr_lines(prs, "feature/b", phases, limit=10, trunks=["release"])
        by = {int(re.match(r"#(\d+)", ln).group(1)): ln for ln in lines}
        self.assertTrue(lines[0].startswith("#2  P2  feature/b -> feature/a"), lines[0])
        self.assertIn("stacked on #1: retarget after it merges", by[2])
        self.assertIn("<- this branch", by[2])
        self.assertIn("P1", by[1])
        self.assertIn("merge: you", by[1])
        self.assertNotIn("stacked", by[6])
        self.assertEqual(index["feature/b"], {"number": 2, "base": "feature/a", "checks": "green"})
        lines, _ = w.pr_lines(prs, "feature/b", None, limit=10)
        self.assertIn("stacked on #5", [ln for ln in lines if ln.startswith("#6")][0])
        lines, _ = w.pr_lines(prs, "main", None, limit=2)
        self.assertEqual(lines[-1], "+2 more (gh pr list)")

    def test_prod_is_inferred_from_where_prs_land(self):
        w = load_where()
        prs = [{"headRefName": "feat/a", "baseRefName": "live"}, {"headRefName": "feat/b", "baseRefName": "feat/a"},
               {"headRefName": "feat/c", "baseRefName": "live"}, {"headRefName": "fix", "baseRefName": "main"}]
        with tempfile.TemporaryDirectory() as d:  # no remote: no origin/HEAD
            subprocess.run(["git", "init", "-q", d], check=True)
            self.assertEqual(w.landing(d, prs), ["live", "main"])
            self.assertEqual(w.landing(d, None), [])
        s = {"prod": "", "trunks": "release", "landing": ["main", "live"]}
        self.assertTrue(w.prod_line(s).startswith("`live` inferred"))
        self.assertEqual(w.prod_line({**s, "landing": ["main", "release"]}), "")  # ordinary trunks say nothing
        self.assertEqual(w.prod_line({**s, "prod": "main deploys"}), "main deploys")
        phases = [{"branch": "p/1", "base": "aryan_dev"}, {"branch": "p/2", "base": "p/1"},  # p/2 is stacked
                  {"branch": "p/3", "base": "aryan_dev"}, {"branch": "p/4", "base": "main"}, {"branch": "", "base": ""}]
        self.assertEqual(w.phase_bases(phases), ["aryan_dev", "main"])
        s2 = {**s, "bases": ["main", "aryan_dev"], "ancestors": ["feat/q"]}
        self.assertTrue(w.prod_line(s2).startswith("`aryan_dev` inferred: phase PRs target it."))  # bases first
        self.assertEqual(w.protected(s2), sorted(w.DEFAULT_TRUNKS | {"release", "live", "aryan_dev", "feat/q"}))
        near = {**s, "landing": ["main"], "ancestors": ["feat/q"]}
        self.assertTrue(w.prod_line(near).startswith("`feat/q` inferred: this branch was cut from it."))
        self.assertEqual(w.prod_line({**near, "ancestors": ["feat/q", "main"]}), "")  # as near a trunk: say nothing


# ---------------------------------------------------------------- setup.py

class SetupTests(Base):
    SETTINGS = {"model": "opus", "effortLevel": "high", "env": {"A": "1"}, "permissions": {"allow": ["Bash(ls)"]},
                "hooks": {"Stop": []}}

    def setup_py(self, *args, scripts=SCRIPTS):
        return self.py("setup.py", *args, scripts=scripts)

    def settings(self, data=None, raw=None):
        p = self.cfg / "settings.json"
        if data is not None or raw is not None:
            p.write_bytes(raw if raw is not None else json.dumps(data, indent=4).encode("utf-8"))
        return json.loads(p.read_text("utf-8-sig")) if p.exists() else None

    def snapshot(self):
        return {str(p): p.read_bytes() for p in self.tmp.rglob("*") if p.is_file() and "__pycache__" not in p.parts}

    def backups(self):
        return list(self.cfg.glob("settings.json.bak-yah-*"))

    def plugin_copy(self, where, rules=None):
        shutil.copytree(SCRIPTS, where / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        if rules is not None:
            (where / "RULES.md").write_text(rules, encoding="utf-8")
        return where / "scripts"

    def test_dry_run_writes_nothing(self):
        self.settings(self.SETTINGS)
        (self.home / ".bashrc").write_text("export A=1\n", encoding="utf-8")
        scripts = self.plugin_copy(self.tmp / "plugin", rules="# Rules\n")
        before = self.snapshot()
        for args in (["--tier", "max20", "--yes"], ["--install-launcher", str(self.home / ".bashrc")],
                     ["--install-rules"], ["--uninstall"]):
            out, err, rc = self.setup_py("--dry-run", *args, scripts=scripts)
            self.assertEqual(rc, 0, err)
            self.assertTrue(out.startswith("[dry-run] "), out)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.data.exists())

    def test_install_merges_backs_up_and_is_idempotent(self):
        original = b"\xef\xbb\xbf" + json.dumps(self.SETTINGS, indent=4).encode("utf-8")  # with a BOM
        self.settings(raw=original)
        out, err, rc = self.setup_py("--tier", "max20", "--yes")
        self.assertEqual(rc, 0, err)
        s = self.settings()
        self.assertEqual({k: v for k, v in s.items() if k != "statusLine"}, self.SETTINGS)
        sl = s["statusLine"]
        self.assertEqual((sl["type"], sl["padding"]), ("command", 0))
        self.assertTrue(sl["command"].endswith(f' "{SCRIPTS.as_posix()}/statusline.py"'), sl["command"])
        self.assertNotIn("\\", sl["command"])
        self.assertEqual([b.read_bytes() for b in self.backups()], [original])
        self.assertEqual(json.loads((self.data / "config.json").read_text("utf-8"))["tier"], "max20")
        self.assertIn("# >>> you-are-here >>>", out)  # the launcher snippet for this OS's shell
        # the command it wrote really runs the statusline
        p = subprocess.run(sl["command"], shell=True, input=b'{"model":{"display_name":"Opus 5.5"}}',
                           capture_output=True, env=self.env, timeout=60)
        self.assertIn("Opus 5.5", p.stdout.decode("utf-8"))
        out, err, rc = self.setup_py("--yes")
        self.assertEqual(rc, 0, err)
        self.assertIn("statusLine unchanged", out)
        self.assertIn("config     unchanged (tier max20)", out)
        self.assertEqual(len(self.backups()), 1)

    def test_foreign_statusline_needs_yes_and_uninstall_restores(self):
        mine = {"type": "command", "command": "echo mine"}
        self.settings(dict(self.SETTINGS, statusLine=mine))
        before = (self.cfg / "settings.json").read_bytes()
        out, err, rc = self.setup_py()
        self.assertEqual(rc, 0, err)
        self.assertIn("another one is set", out)
        self.assertIn("--yes", out)
        self.assertEqual((self.cfg / "settings.json").read_bytes(), before)
        self.assertEqual(self.backups(), [])
        self.setup_py("--yes")
        self.assertNotEqual(self.settings()["statusLine"], mine)
        self.setup_py("--yes")  # a re-run must not forget what was there first
        out, err, rc = self.setup_py("--uninstall")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.settings(), dict(self.SETTINGS, statusLine=mine))
        self.assertIn("left in place", out)
        self.assertFalse((self.data / "setup-state.json").exists())
        self.assertTrue(self.data.is_dir())

    def test_uninstall_removes_ours_when_nothing_was_there(self):
        self.setup_py("--yes")
        self.assertIn("statusLine", self.settings())
        self.setup_py("--uninstall")
        self.assertEqual(self.settings(), {})

    def test_ultracode_opt_in_and_uninstall(self):
        self.settings({**self.SETTINGS, "workflowSizeGuideline": "small"})
        out, err, rc = self.setup_py("--tier", "max20", "--ultracode", "--yes")
        self.assertEqual(rc, 0, err)
        s = self.settings()
        self.assertEqual((s["ultracode"], s["workflowSizeGuideline"]), (True, "medium"))
        self.assertEqual({k: s[k] for k in self.SETTINGS}, self.SETTINGS)  # model, effort, env untouched
        self.assertTrue(json.loads((self.data / "config.json").read_text("utf-8"))["ultracode"])
        self.assertIn("ultracode  unchanged", self.setup_py("--ultracode", "--yes")[0])
        self.setup_py("--uninstall")
        s = self.settings()
        self.assertNotIn("ultracode", s)
        self.assertEqual(s["workflowSizeGuideline"], "small")

    def test_launcher_block(self):
        rc_file = self.home / ".bashrc"
        rc_file.write_text("export A=1\n", encoding="utf-8")
        for _ in range(2):
            out, err, rc = self.setup_py("--install-launcher", str(rc_file))
            self.assertEqual(rc, 0, err)
        self.assertIn("unchanged", out)
        text = rc_file.read_text("utf-8")
        self.assertEqual(text.count("# >>> you-are-here >>>"), 1)
        self.assertIn('where.py" --path "$1")" || return 1', text)
        self.assertIn('run.py" "$@"', text)
        ps = self.setup_py("--launcher", "powershell")[0]
        self.assertIn("function yah {", ps)
        self.assertIn("run.py\" @rest", ps)
        self.assertIn("run.py\" $argv[2..-1]", self.setup_py("--launcher", "fish")[0])
        profile = self.home / "profile.ps1"
        profile.write_bytes(b"Set-Alias x y\r\n")
        self.setup_py("--install-launcher", str(profile))
        ptext = profile.read_bytes()
        self.assertIn(b"function yah {\r\n", ptext)
        self.assertEqual(ptext.count(b"\n"), ptext.count(b"\r\n"))
        self.setup_py("--uninstall")
        self.assertEqual(rc_file.read_text("utf-8"), "export A=1\n")
        self.assertEqual(profile.read_bytes(), b"Set-Alias x y\r\n")

    def test_launcher_block_leaves_foreign_bytes_alone(self):
        latin = self.home / ".bashrc"
        latin.write_bytes(b"export CITY='caf\xe9'\n")  # Latin-1, not UTF-8
        out, err, rc = self.setup_py("--install-launcher", str(latin))
        self.assertEqual(rc, 0, err)
        self.assertEqual(latin.read_bytes(), b"export CITY='caf\xe9'\n")
        self.assertIn("not UTF-8", out)
        self.assertIn("Paste this into it by hand:\n# >>> you-are-here >>>", out.replace("\r\n", "\n"))
        mixed = self.home / ".zshrc"
        mixed.write_bytes(b"\xef\xbb\xbfa=1\r\nb=2\n")  # a BOM and mixed line endings
        self.setup_py("--install-launcher", str(mixed))
        self.assertTrue(mixed.read_bytes().startswith(b"\xef\xbb\xbfa=1\r\nb=2\n\n# >>> you-are-here >>>\n"))
        self.setup_py("--uninstall")
        self.assertEqual(mixed.read_bytes(), b"\xef\xbb\xbfa=1\r\nb=2\n")
        self.assertEqual(latin.read_bytes(), b"export CITY='caf\xe9'\n")

    def launcher_fixture(self, rc_name):
        """A repo where.py has seen, and a plugin copy whose run.py just echoes its args."""
        repo = self.beads_repo()
        self.where(repo)
        scripts = self.plugin_copy(self.tmp / "plugin")
        (scripts / "run.py").write_text("import sys\nprint('RUN', sys.argv[1:])\n", encoding="utf-8")
        rc_file = self.home / rc_name
        self.assertEqual(self.setup_py("--install-launcher", str(rc_file), scripts=scripts)[2], 0)
        return repo, rc_file

    def check_launcher(self, argv, repo):
        p = subprocess.run(argv, capture_output=True, env=self.env, timeout=120)
        out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        self.assertIn(f"claude --resume in {repo}", out, err)
        self.assertIn("RUN ['a', 'b c']", out, err)
        self.assertIn("rc=1", out, err)
        self.assertIn("usage: yah <project>", err)

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"), "needs POSIX bash")
    def test_launcher_runs_in_bash(self):
        repo, rc_file = self.launcher_fixture(".bashrc")
        script = (f'claude() {{ echo "claude $* in $PWD"; }}; . "{rc_file}"; '
                  'yah shop --resume; yah run a "b c"; yah; echo "rc=$?"')
        self.check_launcher(["bash", "-c", script], repo)

    @unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "needs PowerShell")
    def test_launcher_runs_in_powershell(self):
        repo, rc_file = self.launcher_fixture("profile.ps1")
        script = (f"function claude {{ \"claude $args in $((Get-Location).Path)\" }}; . '{rc_file}'; "
                  "yah shop --resume; yah run a 'b c'; yah; \"rc=$LASTEXITCODE\"")
        shell = shutil.which("pwsh") or shutil.which("powershell")
        self.check_launcher([shell, "-NoProfile", "-NonInteractive", "-Command", script], repo)

    def test_rules_block(self):
        scripts = self.plugin_copy(self.tmp / "plugin", rules="# Rules\n- One task per session.\n")
        claude_md = self.cfg / "CLAUDE.md"
        claude_md.write_text("# Mine\n", encoding="utf-8")
        for _ in range(2):
            out, err, rc = self.setup_py("--install-rules", scripts=scripts)
            self.assertEqual(rc, 0, err)
        self.assertIn("unchanged", out)
        text = claude_md.read_text("utf-8")
        self.assertTrue(text.startswith("# Mine\n"))
        self.assertEqual(text.count("<!-- >>> you-are-here rules >>> -->"), 1)
        self.assertIn("- One task per session.\n<!-- <<< you-are-here rules <<< -->", text)
        fresh = self.tmp / "new" / "CLAUDE.md"
        self.setup_py("--install-rules", str(fresh), scripts=scripts)
        self.assertIn("One task per session", fresh.read_text("utf-8"))
        self.setup_py("--uninstall", scripts=scripts)
        self.assertEqual(claude_md.read_text("utf-8"), "# Mine\n")
        self.assertNotIn("you-are-here", fresh.read_text("utf-8"))
        bare = self.plugin_copy(self.tmp / "bare")
        self.assertEqual(self.setup_py("--install-rules", scripts=bare)[2], 1)

    def test_uses_stable_marketplace_path(self):
        plugins = self.cfg / "plugins"
        cached = self.plugin_copy(plugins / "cache" / "you-are-here" / "yah" / "0.1.0")
        self.plugin_copy(plugins / "marketplaces" / "you-are-here")
        out, err, rc = self.setup_py("--yes", scripts=cached)
        self.assertEqual(rc, 0, err)
        self.assertIn(f'"{plugins.as_posix()}/marketplaces/you-are-here/scripts/statusline.py"',
                      self.settings()["statusLine"]["command"])


# ---------------------------------------------------------------- hooks.json

def hook_commands():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text("utf-8"))["hooks"]
    return hooks["SessionStart"][0], hooks["UserPromptSubmit"][0]


class HooksJsonTests(unittest.TestCase):
    def test_shape(self):
        start, prompt = hook_commands()
        self.assertEqual(start["matcher"], "startup|clear|compact|resume")
        self.assertEqual((start["hooks"][0]["timeout"], prompt["hooks"][0]["timeout"]), (20, 10))
        for entry, script in ((start, "where.py\" --brief"), (prompt, "context_guard.py\"")):
            cmd = entry["hooks"][0]["command"]
            runners = [part.strip().split(" \"")[0] for part in cmd.split("||")]
            self.assertEqual(runners, ["python", "python3", "py -3"])
            self.assertEqual(cmd.count(f'"${{CLAUDE_PLUGIN_ROOT}}/scripts/{script}'), 3)


@unittest.skipUnless(shutil.which("sh"), "no sh on PATH")
class HookShellTests(Base):
    def sh(self, cmd, stdin, cwd, path=None):
        env = dict(self.env, CLAUDE_PLUGIN_ROOT=ROOT.as_posix())
        if path:
            env["PATH"] = path
        p = subprocess.run([shutil.which("sh"), "-c", cmd], input=stdin.encode("utf-8"), capture_output=True,
                           cwd=str(cwd), env=env, timeout=60)
        return p.stdout.decode("utf-8", "replace"), p.returncode

    def check(self, path=None):
        start, prompt = (e["hooks"][0]["command"] for e in hook_commands())
        repo = self.beads_repo()
        out, rc = self.sh(start, json.dumps({"hook_event_name": "SessionStart", "source": "startup", "cwd": str(repo)}),
                          repo, path)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("[yah] shop  branch checkout/payment"), out)
        out, rc = self.sh(prompt, json.dumps({"session_id": "h1", "prompt": "hi",
                                              "transcript_path": self.transcript(210_000)}), repo, path)
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["hookSpecificOutput"]["additionalContext"].startswith("[yah]"), out)

    def test_hook_commands_run(self):
        self.check()

    @unittest.skipIf(os.name == "nt" or not shutil.which("python3"), "POSIX fallback only")
    def test_falls_back_to_python3(self):
        fake = self.tmp / "fakebin"
        fake.mkdir()
        (fake / "python").write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
        (fake / "python").chmod(0o755 | stat.S_IEXEC)
        path = str(fake) + os.pathsep + os.environ.get("PATH", "")
        self.assertEqual(shutil.which("python", path=path), str(fake / "python"))
        self.check(path)


# ---------------------------------------------------------------- repo hygiene

class RepoTests(unittest.TestCase):
    def test_python39_syntax(self):
        for f in list(SCRIPTS.glob("*.py")) + [Path(__file__)]:
            with self.subTest(f=f.name):
                ast.parse(f.read_text("utf-8"), filename=str(f), feature_version=(3, 9))

    def test_short_descriptions_and_no_private_names(self):
        for f in list(ROOT.glob("skills/*/SKILL.md")) + list(ROOT.glob("agents/*.md")):
            text = f.read_text("utf-8")
            desc = re.search(r"^description: (.+)$", text, re.M)
            with self.subTest(f=str(f.relative_to(ROOT))):
                self.assertIsNotNone(desc)
                self.assertLessEqual(len(desc.group(1)), 220)
        for f in list(SCRIPTS.glob("*.py")) + list(ROOT.glob("skills/*/SKILL.md")) + list(ROOT.glob("agents/*.md")) \
                + [ROOT / "hooks" / "hooks.json"]:
            text = f.read_text("utf-8")
            for bad in ("/kit:", "[kit]", "kitlib", "claude-kit", "cc <"):
                self.assertNotIn(bad, text, f"{bad} in {f.name}")


if __name__ == "__main__":
    unittest.main()
