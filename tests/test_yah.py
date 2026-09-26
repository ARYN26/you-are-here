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
from unittest import mock

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
     "spec_id": "docs/plans/checkout.md", "metadata": {"short": "CO"}},  # ignored: the tag is the title's first word
    {"id": "yah-2", "title": "P1 Cart API", "issue_type": "task", "status": "closed", "labels": ["phase"],
     "parent": "yah-1", "metadata": {"phase": 1, "branch": "checkout/cart", "base": "main"}, "external_ref": "gh-12"},
    {"id": "yah-3", "title": "P2 Payment form", "issue_type": "task", "status": "in_progress", "labels": ["phase"],
     "assignee": "Sam", "parent": "yah-1", "metadata": {"phase": 2, "branch": "checkout/payment", "base": "checkout/cart"},
     "notes": "Wire the Stripe element into PaymentForm.tsx, then run the e2e test.\nCart API is merged."},
    {"id": "yah-4", "title": "P3 Emails", "issue_type": "task", "status": "open", "labels": ["phase"],
     "parent": "yah-1", "metadata": {"phase": 3}, "notes": "Send the receipt email."},  # not shown: not current
    {"id": "yah-5", "title": "Approve the payment copy", "issue_type": "task", "status": "open", "labels": ["human"]},
    {"id": "yah-6", "title": "Rotate the Stripe test keys", "issue_type": "task", "status": "open", "labels": ["human"]},
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
STATE_YOU = STATE_PLAN.replace("- [ ] P3 Emails", "- [ ] P3 Emails\n\n## Follow-ups\n"  # the same plan as BEADS
                               "- [ ] Approve the payment copy (you)\n- [ ] Rotate the Stripe test keys (you)")
STATE_DATED = "# Notes\n\n## 2026-09-20\n- Next: Old.\n\n## 2026-09-23\n- Next: Ship the login fix,\n  then tag v1.2.\n"


def jsonl(items):
    return "\n".join(json.dumps(i) for i in items) + "\n"


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

    def beads_repo(self, branch="checkout/payment", name="shop"):
        return self.repo({".beads/issues.jsonl": jsonl(BEADS)}, branch, name)

    def state_repo(self, text=STATE_YOU, branch="checkout/payment", name="shop"):
        return self.repo({"STATE.md": text}, branch, name)

    def where(self, repo, *args):
        out, err, rc = self.py("where.py", "--no-gh", "--no-bd", *args, cwd=repo)
        self.assertEqual(rc, 0, err)
        return out

    def plugin_copy(self, where, rules=None):
        shutil.copytree(SCRIPTS, where / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        if rules is not None:
            (where / "RULES.md").write_text(rules, encoding="utf-8")
        return where / "scripts"

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
        repo = self.state_repo()  # the cache is the same for beads: test_state_md_matches_beads
        self.where(repo)
        out = self.line(cwd=repo, pr={"number": 8})
        for want in ("checkout/payment", "PR#8", "CHECKOUT P2/3", "2 for you"):
            self.assertIn(want, out)

    def test_timing(self):
        repo = self.state_repo()
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
    def guard(self, tokens=0, prompt="go on", sid="g1", scripts=SCRIPTS):
        d = {"session_id": sid, "prompt": prompt, "transcript_path": self.transcript(tokens) if tokens else ""}
        out, err, rc = self.py("context_guard.py", stdin=json.dumps(d), scripts=scripts)
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

    def cached(self, name):
        """A copy of yah in the plugin cache, laid out as Claude Code installs it. Returns its scripts folder."""
        return self.plugin_copy(self.cfg / "plugins" / "cache" / "you-are-here" / "yah" / name)

    def installed(self, scripts):
        (self.cfg / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {"yah@you-are-here": [{"scope": "user", "installPath": str(scripts.parent)}]}}),
            encoding="utf-8")

    def test_outdated_copy_tells_the_user_once_per_session(self):
        old, new = self.cached("aaa"), self.cached("bbb")
        self.installed(old)
        self.assertIsNone(self.guard(sid="o1", scripts=old))  # the installed copy
        self.assertIsNone(self.guard(sid="o1"))  # --plugin-dir checkout: nothing installed beside it
        self.installed(new)  # an update lands mid-session
        want = {"systemMessage": "yah was updated: run /reload-plugins (or /exit and restart) to load it."}
        self.assertEqual(self.guard(sid="o1", scripts=old), want)  # the user only; nothing for the model
        self.assertIsNone(self.guard(sid="o1", scripts=old))
        self.assertIsNone(self.guard(sid="o2", prompt="/reload-plugins", scripts=old))
        self.assertEqual(self.guard(sid="o2", scripts=old), want)  # again after /clear
        self.assertIsNone(self.guard(sid="o3", scripts=new))
        (self.cfg / "plugins" / "installed_plugins.json").write_text("[]", encoding="utf-8")
        r = self.guard(210_000, sid="o4", scripts=old)  # unreadable: no notice, other nudges intact
        self.assertEqual(r["systemMessage"], "Context 210k: wrap now (/yah:wrap, then /clear).")


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
        self.assertNotIn("beads", s)
        self.assertEqual((s["state"]["plan"]["source"], s["state"]["phase"]["base"]), ("beads", "checkout/cart"))
        self.assertEqual([h["id"] for h in s["state"]["human"]], ["yah-5", "yah-6"])
        self.assertEqual([p["next"] for p in s["state"]["phases"]],  # each bead keeps its own notes
                         ["", "Wire the Stripe element into PaymentForm.tsx, then run the e2e test.",
                          "Send the receipt email."])
        self.assertEqual((s["beads_source"], s["ignored_plan"]), ("jsonl", None))
        self.assertIsNone(s["next_stale"])  # no stamp, no flag
        self.assertNotIn("predates", "\n".join(brief + full))

    def stamp_beads(self, repo, sha):
        beads = [dict(b, metadata=dict(b["metadata"], next_sha=sha)) if b["id"] == "yah-3" else b for b in BEADS]
        (repo / ".beads" / "issues.jsonl").write_text(jsonl(beads), "utf-8")

    def check_only_the_phase_branch_counts(self, repo):
        """NEXT was stamped on checkout/cart, the base: base commits after it never count, while commits on the
        phase branch do, even from off it. Leaves the repo on the phase branch, 2 commits past the stamp."""
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

    def test_stale_next_from_beads_counts_only_the_phase_branch(self):
        repo = self.beads_repo(branch="checkout/cart")
        self.stamp_beads(repo, self.head(repo))
        self.git(repo, "commit", "-qam", "plan")  # a base commit after the stamp
        self.check_only_the_phase_branch_counts(repo)
        self.stamp_beads(repo, self.head(repo)[:7])  # wrap re-stamps after its WIP commit
        self.assertNotIn("predates", self.where(repo, "--brief"))
        for bad in ("--output=x", "HEAD", "zzzzzzz", ""):  # never passed to git as an option or a ref
            self.stamp_beads(repo, bad)
            self.assertIsNone(json.loads(self.where(repo, "--json"))["next_stale"], bad)

    def test_stale_next_from_state_md_counts_only_the_phase_branch(self):
        repo = self.state_repo(STATE_PLAN, branch="checkout/cart")  # tracked: its last commit is the stamp
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "base work")  # a base commit after the stamp
        self.check_only_the_phase_branch_counts(repo)
        (repo / "STATE.md").write_text(STATE_PLAN.replace("An older", "An old"), encoding="utf-8")
        self.git(repo, "commit", "-qam", "wrap")  # wrap commits the new NEXT
        self.assertNotIn("predates", self.where(repo, "--brief"))

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
        for src in ("beads", "STATE.md"):
            with self.subTest(src):
                if src == "beads":
                    repo = self.beads_repo(branch="checkout/cart", name="bd")
                    init = self.head(repo)
                    self.stamp_beads(repo, init)  # /yah:phases stamps every phase at plan time
                    self.git(repo, "commit", "-qam", "plan")
                else:
                    repo = self.state_repo(STATE_PLAN, branch="checkout/cart", name="md")
                    init = self.head(repo)  # STATE.md's last commit: the stamp
                self.git(repo, "commit", "-q", "--allow-empty", "-m", "cart work")
                self.git(repo, "checkout", "-q", "-b", "main", init)
                self.git(repo, "merge", "-q", "--no-ff", "-m", "merge P1", "checkout/cart")
                self.git(repo, "checkout", "-q", "-b", "checkout/payment", "checkout/cart")
                self.git(repo, "branch", "-q", "-D", "checkout/cart")  # merged and deleted: P1's work is main's now
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

    def test_only_the_human_label_waits_on_you_in_beads(self):
        w = load_where()
        issues = [{"id": "x-1", "title": "Fix the flaky test", "issue_type": "task", "status": "in_progress",
                   "assignee": "Sam"},
                  {"id": "x-2", "title": "Rotate the keys", "issue_type": "task", "status": "open", "assignee": "Sam"},
                  {"id": "x-3", "title": "Approve the copy", "issue_type": "task", "status": "in_progress",
                   "labels": ["human"]}]
        self.assertEqual([i["id"] for i in w.beads.beads_state(issues)["human"]], ["x-3"])

    def test_next_per_source(self):
        w = load_where()
        epic = {"id": "x-0", "title": "Plan", "issue_type": "epic", "status": "open", "labels": ["plan"]}
        kids = [{"id": f"x-{n}", "title": f"P{n} Step", "issue_type": "task", "status": st, "labels": ["phase"],
                 "parent": "x-0", "metadata": {"phase": n}, "notes": f"Do step {n}."}
                for n, st in ((1, "closed"), (2, "open"), (3, "open"))]
        s = w.beads.beads_state([epic] + kids)  # each bead keeps the notes wrap wrote, so `yah run P3` resumes from its own
        self.assertEqual((s["next_phase"]["id"], [p["next"] for p in s["phases"]]),
                         ("x-2", ["Do step 1.", "Do step 2.", "Do step 3."]))
        text = "## Plan: Plan\n- [x] P1 Step\n- [ ] P2 Step\n- [ ] P3 Step\n\n## 2026-09-23\n- Next: Do step 2.\n"
        s = json.loads(self.where(self.state_repo(text), "--json"))["state"]
        self.assertEqual((s["next_phase"]["id"], [p["next"] for p in s["phases"]]),
                         ("STATE.md:3", ["", "Do step 2.", ""]))

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

    def test_brief_worst_case_is_six_lines(self):
        prod = {"prod": "main deploys on push. PRs only."}
        self.config(projects={"shop": prod})
        repo = self.state_repo(branch="main")  # the views are the same for beads: test_state_md_matches_beads
        brief = self.where(repo, "--brief").splitlines()
        self.assertEqual(len(brief), 6)
        self.assertTrue(brief[0].endswith("! phase branch is checkout/payment"))
        self.assertEqual(brief[3], "2 waiting on you")
        self.assertEqual(brief[4], "PROD main deploys on push. PRs only.")
        full = self.where(repo)
        self.assertIn("!       phase branch is checkout/payment, you are on main", full)
        self.assertIn("PROD    main deploys on push. PRs only.", full)

    def test_state_md_plan(self):
        repo = self.state_repo()
        brief = self.where(repo, "--brief")
        self.assertLessEqual(len(brief.splitlines()), 6)
        self.assertIn("PLAN CHECKOUT 1/3 done  PHASE P2 Payment form (STATE.md:5)", brief)
        self.assertIn("NEXT Wire the Stripe element into PaymentForm.tsx, then run the e2e test.", brief)
        self.assertNotIn("older", brief)
        full = self.where(repo).splitlines()
        self.assertEqual(full[1:6], [
            "PLAN    Checkout rewrite  [1/3 done]  STATE.md  checkout.md",
            "PHASE   P2 Payment form  in_progress  STATE.md:5  branch checkout/payment",
            "NEXT    Wire the Stripe element into PaymentForm.tsx, then run the e2e test.",
            "YOU     STATE.md:9  Approve the payment copy",
            "        STATE.md:10  Rotate the Stripe test keys"])
        b = json.loads(self.where(repo, "--json"))["state"]
        self.assertEqual([(p["id"], p["label"], p["status"]) for p in b["phases"]],  # a phase's id is its line
                         [("STATE.md:4", "P1", "closed"), ("STATE.md:5", "P2", "in_progress"), ("STATE.md:6", "P3", "open")])
        self.assertEqual((b["phases"][0]["pr"], b["phase"]["branch"], b["phase"]["base"], b["plan"]["source"]),
                         ("#12", "checkout/payment", "checkout/cart", "STATE.md"))
        self.assertEqual((b["plan"]["id"], b["in_progress"], b["open_count"]), ("STATE.md", [], 0))

    def test_state_md_fenced_plan_and_indented_phases(self):
        w = load_where()
        fenced = "## Follow-ups\n```\n## Plan: Example\n- [ ] P1 fake\n```\n- [ ] real task\n"
        (self.tmp / "STATE.md").write_text(fenced, encoding="utf-8")
        sm = w.state_md(self.tmp)
        self.assertEqual((sm["plan"], sm["open_count"], sm["next"]), (None, 1, ""))  # a fence is an example, not NEXT
        (self.tmp / "STATE.md").write_text("## Plan: X\n  - [~] P1 A\n    - [ ] sub\n  - [ ] P2 B\n", encoding="utf-8")
        sm = w.state_md(self.tmp)
        self.assertEqual([(p["label"], p["status"]) for p in sm["phases"]],
                         [("P1", "in_progress"), ("P2", "open")])
        self.assertEqual(sm["open_count"], 1)

    def test_state_md_plan_without_running_phase(self):
        text = "## Plan: Emails\n- [x] P1 Templates\n- [ ] P2 Sending\n\n## 2026-09-23\n- Next: Pick a mail provider.\n"
        out = self.where(self.repo({"STATE.md": text}))
        self.assertIn("PHASE   none in progress. Next: P2 Sending  (mark it [~] in STATE.md)", out)
        self.assertIn("NEXT    Pick a mail provider.", out)

    def test_state_md_sub_tasks_are_not_phases(self):
        text = ("## Plan: Launch\n- [x] P1 Build\n  - [x] Write the build script\n- [~] P2 Ship | branch launch/ship\n"
                "  - [ ] Tag the release\n  - [~] Upload the assets\n- [ ] P3 Announce\n\n"
                "## Follow-ups\n- [~] Fix the flaky e2e test\n- [ ] Rotate the keys (you)\n- [ ] Update the README\n"
                "- [x] Old thing\n")
        s = json.loads(self.where(self.state_repo(text, branch="launch/ship"), "--json"))["state"]
        self.assertEqual([(p["id"], p["label"], p["title"]) for p in s["phases"]],
                         [("STATE.md:2", "P1", "Build"), ("STATE.md:4", "P2", "Ship"), ("STATE.md:7", "P3", "Announce")])
        self.assertEqual((s["plan"]["done"], s["plan"]["total"], s["phase"]["id"]), (1, 3, "STATE.md:4"))
        self.assertEqual(s["in_progress"], [{"id": "STATE.md:6", "title": "Upload the assets"},
                                            {"id": "STATE.md:10", "title": "Fix the flaky e2e test"}])
        self.assertEqual((s["open_count"], s["human"]), (2, [{"id": "STATE.md:11", "title": "Rotate the keys"}]))

    def test_state_md_follow_up_in_progress_shows_as_doing(self):
        repo = self.state_repo(STATE_DATED + "\n## Follow-ups\n- [~] Fix the flaky e2e test\n- [ ] Update the README\n"
                               "- [ ] Bump the deps\n- [ ] Rotate the keys (you)\n", branch="main")
        full = self.where(repo).splitlines()
        self.assertEqual(full[1:], [
            "STATE   STATE.md: 2026-09-23",
            "NEXT    Ship the login fix, then tag v1.2.",
            "DOING   STATE.md:11  Fix the flaky e2e test",
            "TASKS   2 open, no plan (/yah:phases after a plan is approved)",
            "YOU     STATE.md:14  Rotate the keys"])
        brief = self.where(repo, "--brief").splitlines()
        self.assertLessEqual(len(brief), 6)
        self.assertEqual(brief[1:4], ["STATE.md 2026-09-23  NEXT Ship the login fix, then tag v1.2.",
                                      "IN PROGRESS STATE.md:11 Fix the flaky e2e test", "1 waiting on you"])
        home = self.py("where.py")[0].splitlines()  # the last wrap's NEXT beats the [~] follow-up
        self.assertTrue(any(ln.startswith("shop") and "Ship the login fix" in ln for ln in home), home)
        (repo / "STATE.md").write_text("## Follow-ups\n- [~] Fix the flaky e2e test\n", encoding="utf-8")
        self.where(repo)
        home = self.py("where.py")[0].splitlines()  # no NEXT: the [~] follow-up shows
        self.assertTrue(any(ln.startswith("shop") and "doing Fix the flaky e2e test" in ln for ln in home), home)

    def test_tasks_row_for_open_work_without_a_plan(self):
        issues = [{"id": "x-1", "title": "Fix the flaky test", "issue_type": "task", "status": "in_progress"},
                  {"id": "x-2", "title": "Bump the deps", "issue_type": "task", "status": "open"},
                  {"id": "x-3", "title": "Rotate the keys", "issue_type": "task", "status": "open", "labels": ["human"]}]
        repo = self.repo({".beads/issues.jsonl": jsonl(issues)})
        full = self.where(repo)
        self.assertIn("DOING   x-1  Fix the flaky test", full)
        self.assertIn("TASKS   1 open, no plan (/yah:phases after a plan is approved)", full)  # a human bead is not
        self.assertIn("YOU     x-3  Rotate the keys", full)  # an open task, like a (you) line
        self.assertNotIn("BEADS", full)
        (repo / "STATE.md").write_text("## Follow-ups\n- [ ] Update the README\n", encoding="utf-8")
        self.assertIn("TASKS   2 open, no plan", self.where(repo))  # both sources count
        (repo / ".beads" / "issues.jsonl").write_text(jsonl(issues[:1]), encoding="utf-8")
        (repo / "STATE.md").write_text("## Follow-ups\n- [x] Update the README\n", encoding="utf-8")
        self.assertNotIn("TASKS", self.where(repo))  # nothing open: no row
        planned = self.state_repo(STATE_PLAN + "\n## Follow-ups\n- [ ] Update the README\n", name="md")
        self.assertEqual(json.loads(self.where(planned, "--json"))["state"]["open_count"], 1)
        self.assertNotIn("TASKS", self.where(planned))  # a plan: no row

    def test_a_state_md_plan_wins_over_a_beads_epic(self):
        repo = self.beads_repo()
        (repo / "STATE.md").write_text("## Plan: Emails (docs/plans/emails.md)\n- [~] P1 Templates | branch "
                                       "checkout/payment\n\n## Follow-ups\n- [ ] Rotate the prod keys (you)\n",
                                       encoding="utf-8")
        s = json.loads(self.where(repo, "--json"))
        self.assertEqual((s["state"]["plan"]["title"], s["state"]["plan"]["source"], s["state"]["phase"]["id"]),
                         ("Emails", "STATE.md", "STATE.md:2"))
        self.assertEqual(s["ignored_plan"], {"id": "yah-1", "title": "Checkout rewrite", "source": "beads"})
        self.assertEqual((s["store"]["kind"], s["store"]["path"]), ("STATE.md", str(repo / "STATE.md")))
        self.assertIn("beads epic yah-1 ignored: STATE.md has a plan", s["store"]["note"])
        self.assertEqual([h["id"] for h in s["state"]["human"]], ["STATE.md:5", "yah-5", "yah-6"])  # both count, STATE.md first
        full = self.where(repo).splitlines()
        self.assertEqual(full[1:4], ["PLAN    Emails  [0/1 done]  STATE.md  emails.md",
                                     "!       beads epic yah-1 ignored: STATE.md has a plan",
                                     "PHASE   P1 Templates  in_progress  STATE.md:2  branch checkout/payment"])
        self.assertIn("PLAN EMAILS 0/1 done  PHASE P1 Templates (STATE.md:2)", self.where(repo, "--brief"))

    def test_state_md_dated_only(self):
        repo = self.repo({"STATE.md": STATE_DATED})
        self.assertIn("STATE.md 2026-09-23  NEXT Ship the login fix, then tag v1.2.", self.where(repo, "--brief"))
        full = self.where(repo)
        self.assertIn("STATE   STATE.md: 2026-09-23", full)
        self.assertIn("NEXT    Ship the login fix, then tag v1.2.", full)
        self.assertNotIn("PLAN", full)

    def test_a_worktree_reads_the_main_checkouts_state_md(self):
        """STATE.md is gitignored, so a linked worktree has none: it reads the main checkout's, as beads does."""
        repo = self.repo({".gitignore": "STATE.md\n"}, branch="checkout/cart")
        (repo / "STATE.md").write_text(STATE_YOU, encoding="utf-8")
        wt = self.tmp / "shop-wt"
        self.git(repo, "worktree", "add", "-q", "-b", "checkout/payment", str(wt))
        s = json.loads(self.where(wt, "--json"))
        self.assertTrue(os.path.samefile(s["state_md"]["path"], repo / "STATE.md"), s["state_md"])
        self.assertEqual((s["state"]["phase"]["label"], s["state"]["phase"]["id"]), ("P2", "STATE.md:5"))
        self.assertIn("PHASE   P2 Payment form  in_progress  STATE.md:5  branch checkout/payment", self.where(wt))
        (wt / "STATE.md").write_text("## 2026-09-24\n- Next: Only here.\n", encoding="utf-8")  # its own wins
        s = json.loads(self.where(wt, "--json"))
        self.assertEqual((s["state_md"]["next"], s["state"]["plan"]), ("Only here.", None))

    def test_state_md_matches_beads(self):
        """The same plan in STATE.md and in beads gives the same plan, phase, NEXT, waiting-on-you, work in
        progress and open tasks: phases are none of the last two, in either source. Only the phases' own NEXT
        differs: beads keeps each bead's notes, STATE.md has one NEXT."""
        md = json.loads(self.where(self.state_repo(name="md"), "--json"))
        bd = json.loads(self.where(self.beads_repo(), "--json"))

        def view(s):
            b = s["state"]
            return ({k: b["plan"][k] for k in ("title", "short", "spec", "done", "total")},
                    [(p["label"], p["title"], p["status"], p["branch"]) for p in b["phases"]],
                    {k: b["phase"][k] for k in ("label", "branch", "base", "next")},
                    [h["title"] for h in b["human"]], [i["title"] for i in b["in_progress"]], b["open_count"],
                    s["protected"])
        self.assertEqual(view(md)[:6], view(bd)[:6])
        self.assertEqual(view(bd)[4:6], ([], 0))
        self.assertEqual(set(view(md)[6]), set(view(bd)[6]))

    def test_state_md_you_marker(self):
        text = ("## Plan: Launch\n- [x] P1 Build | branch launch/build\n- [ ] P2 Merge PR #12 (you) | PR #12\n\n"
                "## Follow-ups\n- [ ] Rotate the test keys (you)\n- [x] Old console step (you)\n- [ ] Flaky e2e test\n"
                "- [ ] Ask (you) about it later\n\n"
                "## 2026-09-23\n- Next: Wait for review.\n")
        repo = self.repo({"STATE.md": text})
        s = json.loads(self.where(repo, "--json"))
        self.assertEqual(s["state"]["human"], [{"id": "STATE.md:3", "title": "P2 Merge PR #12"},
                                               {"id": "STATE.md:6", "title": "Rotate the test keys"}])
        self.assertEqual((s["state"]["plan"]["total"], s["state"]["next_phase"]["label"]), (2, "P2"))
        for key in ("human", "in_progress", "open_count", "plan"):
            self.assertNotIn(key, s["state_md"])
        full = self.where(repo)
        self.assertIn("YOU     STATE.md:3  P2 Merge PR #12", full)
        self.assertIn("        STATE.md:6  Rotate the test keys", full)
        self.assertIn("2 waiting on you", self.where(repo, "--brief"))
        cache = next(self.data.glob("where-*.json"))  # the statusline's "for you" count
        self.assertEqual(json.loads(cache.read_text(encoding="utf-8"))["human"], 2)

    def test_state_md_you_line_with_its_command(self):
        text = "## Follow-ups\n- [ ] Merge PR #9: gh pr merge 9 --merge (you)\n"
        repo = self.repo({"STATE.md": text})
        s = json.loads(self.where(repo, "--json"))
        self.assertEqual(s["state"]["human"], [{"id": "STATE.md:2", "title": "Merge PR #9: gh pr merge 9 --merge"}])
        self.assertNotIn("(you)", self.where(repo))

    def test_state_md_you_after_fields_and_fences(self):
        text = ("## Plan: Launch\n- [ ] P1 Ship | branch launch/ship | PR #13 (you)\n\n"
                "```markdown\n- [ ] An example line (you)\n```\n")
        s = json.loads(self.where(self.repo({"STATE.md": text}), "--json"))
        self.assertEqual(s["state"]["human"], [{"id": "STATE.md:2", "title": "P1 Ship"}])
        p = s["state"]["phases"][0]
        self.assertEqual((p["title"], p["branch"], p["pr"]), ("Ship", "launch/ship", "#13"))

    def test_state_md_you_with_a_beads_plan(self):
        repo = self.beads_repo()
        (repo / "STATE.md").write_text("## Follow-ups\n- [ ] Rotate the prod keys (you)\n", encoding="utf-8")
        s = json.loads(self.where(repo, "--json"))
        self.assertEqual((s["state"]["plan"]["source"], s["ignored_plan"]), ("beads", None))  # no STATE.md plan
        self.assertEqual([h["id"] for h in s["state"]["human"]], ["STATE.md:2", "yah-5", "yah-6"])
        self.assertEqual(s["store"], {"kind": "STATE.md", "path": str(repo / "STATE.md"), "bd": None,
                                      "note": "bd was skipped (--no-bd)"})  # a beads plan, but no bd
        w = load_where()
        with mock.patch.dict(os.environ), mock.patch.object(w.beads, "find_tool", return_value=None):
            os.environ.pop("BEADS_DIR", None)
            bz = w.beads.read(repo, repo)
        self.assertEqual((bz["source"], bz["bd"], bz["note"]), ("jsonl", None, "the bd CLI was not found"))
        self.assertEqual(bz["state"]["plan"]["id"], "yah-1")
        with mock.patch.dict(os.environ), mock.patch.object(w.beads, "find_tool", return_value=str(repo / "no-bd")):
            os.environ.pop("BEADS_DIR", None)
            bz = w.beads.read(repo, repo)  # a bd whose `bd list` fails here is not one the model may run
        self.assertEqual((bz["source"], bz["bd"], bz["note"]), ("jsonl", None, "`bd list` failed here"))

    def test_state_md_you_without_plan(self):
        repo = self.repo({"STATE.md": STATE_DATED + "\n## Follow-ups\n- [ ] Rotate the test keys (you)\n"})
        full = self.where(repo)
        self.assertIn("NEXT    Ship the login fix, then tag v1.2.", full)
        self.assertIn("YOU     STATE.md:11  Rotate the test keys", full)
        self.assertNotIn("TASKS", full)  # a (you) line is not an open task
        self.assertNotIn("PLAN", full)

    def test_state_md_picks_the_running_plan(self):
        done = "## Plan: Old (plans/old.md)\n- [x] P1 Shipped\n\n"
        cases = {"running": done + "## Plan: New (plans/new.md)\n- [x] P1 A\n- [~] P2 B\n",
                 "open": done + "## Plan: New (plans/new.md)\n- [ ] P1 A\n",
                 "running over open": "## Plan: Old\n- [ ] P1 Later\n\n## Plan: New\n- [~] P1 Now\n"}
        for i, (case, text) in enumerate(cases.items()):
            with self.subTest(case=case):
                s = json.loads(self.where(self.repo({"STATE.md": text}, name=f"r{i}"), "--json"))
                self.assertEqual(s["state"]["plan"]["title"], "New")
        s = json.loads(self.where(self.repo({"STATE.md": done}, name="all-done"), "--json"))
        self.assertEqual((s["state"]["plan"]["title"], s["state"]["phase"], s["state"]["next_phase"]), ("Old", None, None))

    def test_path(self):
        repo = self.state_repo()
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
        self.where(self.state_repo())
        self.where(self.state_repo(STATE_PLAN.replace("- [~] P2", "- [ ] P2"), name="blog"))
        full = self.py("where.py")[0].splitlines()
        self.assertTrue(full[0].startswith("PROJECTS  (start one with: yah <name>)"))
        self.assertTrue(any(ln.startswith("shop") and "CHECKOUT P2/3 Payment form" in ln for ln in full), full)
        self.assertTrue(any(ln.startswith("blog") and "CHECKOUT P2/3 Payment form (not started)" in ln
                            for ln in full), full)
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
        self.assertEqual((json.loads(out)["store"], json.loads(out)["beads_source"]),
                         ({"kind": "STATE.md", "path": str(plain / "STATE.md"), "bd": None, "note": None}, None))
        repo = self.beads_repo()
        out, err, rc = self.py("where.py", "--no-gh", "--json", cwd=repo)
        s = json.loads(out)
        self.assertEqual(s["beads_source"], "bd", err)
        self.assertEqual(Path(s["state"]["in_progress"][0]["title"]), repo / ".beads")
        self.assertEqual((s["store"]["kind"], s["store"]["bd"]), ("STATE.md", None))  # the model's own bd calls would inherit the foreign BEADS_DIR
        self.assertIn("BEADS_DIR", s["store"]["note"])
        del self.env["BEADS_DIR"]
        s = json.loads(self.py("where.py", "--no-gh", "--json", cwd=repo)[0])
        self.assertEqual(s["store"]["kind"], "beads")
        self.assertTrue(Path(s["store"]["bd"]).name.startswith("bd"))
        self.assertIsNone(s["store"]["note"])

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
        self.assertIn("stacked on #1: retarget after it merges, `gh pr edit 2 --base main`", by[2])
        self.assertNotIn("gh pr merge", by[2])  # a stacked PR is retargeted first, never merged into its base
        self.assertIn("<- this branch", by[2])
        self.assertIn("P1", by[1])
        self.assertIn("merge: you, `gh pr merge 1 --merge`", by[1])
        self.assertNotIn("--delete-branch", by[1])  # deleting a stack's base branch closes the PR on it
        self.assertNotIn("stacked", by[6])
        self.assertEqual(index["feature/b"], {"number": 2, "base": "feature/a", "checks": "green"})
        lines, _ = w.pr_lines(prs, "feature/b", None, limit=10)
        self.assertIn("stacked on #5", [ln for ln in lines if ln.startswith("#6")][0])
        lines, _ = w.pr_lines(prs, "main", None, limit=2)
        self.assertEqual(lines[-1], "+2 more (gh pr list)")

    def test_auto_merge_marks_only_a_green_phase_pr(self):
        w = load_where()
        green = [{"conclusion": "SUCCESS"}]
        prs = [{"number": 1, "headRefName": "p/1", "baseRefName": "main", "statusCheckRollup": green},
               {"number": 2, "headRefName": "docs", "baseRefName": "main", "statusCheckRollup": green},
               {"number": 3, "headRefName": "p/3", "baseRefName": "main", "statusCheckRollup": green, "isDraft": True},
               {"number": 4, "headRefName": "p/4", "baseRefName": "main", "statusCheckRollup": [{"conclusion": "FAILURE"}]}]
        phases = {"phases": [{"label": f"P{n}", "pr": "", "branch": f"p/{n}"} for n in (1, 3, 4)]}

        def by(auto):
            lines, _ = w.pr_lines(prs, "main", phases, limit=10, auto=auto)
            return {int(re.match(r"#(\d+)", ln).group(1)): ln for ln in lines}

        on, off = by(True), by(False)
        self.assertIn("auto-merge: on", on[1])
        self.assertNotIn("merge: you", on[1])
        self.assertNotIn("gh pr merge", on[1])  # yah run merges it: no command for the user
        self.assertIn("merge: you, `gh pr merge 2 --merge`", on[2])  # not a phase PR: yah run never merges it
        for n in (3, 4):  # a draft or a red PR: nobody merges it yet
            self.assertNotIn("merge", on[n])
        self.assertIn("merge: you", off[1])
        self.assertFalse(any("auto-merge" in ln for ln in off.values()), off)

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


class AutoMergeWhereTests(Base):
    def test_only_a_json_true_turns_auto_merge_on(self):
        w = load_where()
        repo = self.beads_repo()
        prs = [{"number": 7, "headRefName": "checkout/payment", "baseRefName": "checkout/cart",
                "statusCheckRollup": [{"conclusion": "SUCCESS"}]}]
        for value, on in ((True, True), ("true", False), (1, False), (None, False)):
            with self.subTest(auto_merge=value):
                self.config(**({} if value is None else {"auto_merge": value}))
                s = json.loads(self.where(repo, "--json"))
                self.assertIs(s["auto_merge"], on)
                s["prs"] = prs  # the same view gh would give, through render: --brief and the full view agree
                for brief in (False, True):
                    text = "\n".join(w.render(s, brief=brief))
                    self.assertIn("#7  P2  checkout/payment -> checkout/cart  green", text)
                    if on:
                        self.assertIn("auto-merge: on", text)
                        self.assertNotIn("merge: you", text)
                    else:
                        self.assertNotIn("auto-merge", text)
                        self.assertIn("merge: you", text)


class RunLineTests(Base):
    """The RUN line: where.py and the statusline read the checkout's `yah run` pid file."""
    line = StatuslineTests.line

    def test_run_line_live_ended_died_and_too_old(self):
        sys.path.insert(0, str(SCRIPTS))
        import yahlib
        repo, now = self.state_repo(), time.time()
        pidf = self.data / "runs" / yahlib.run_pid_path("shop", str(repo), str(repo)).name
        log = self.data / "runs" / "shop-1.log"
        log.parent.mkdir(parents=True)
        log.write_text("2026-09-25 14:02:00 started\n2026-09-25 14:35:10 iteration 2: $1.20, 30 turns, CONTINUE\n\n",
                       encoding="utf-8")
        for out in (self.where(repo), self.where(repo, "--brief"), self.line(cwd=repo)):  # no run yet
            self.assertNotIn("RUN", out)
            self.assertNotIn("run P", out)
        info = {"pid": 4242, "started": int(now) - 7200, "target": "P2", "plan": False, "log": str(log)}
        fd = yahlib.hold_run(pidf, info)
        self.assertIsNotNone(fd)
        try:
            self.assertIn("RUN     P2 running for 2h, pid 4242, last: 14:35 iteration 2: $1.20, 30 turns, CONTINUE",
                          self.where(repo))
            brief = self.where(repo, "--brief")
            self.assertLessEqual(len(brief.splitlines()), 6)
            self.assertIn("RUN P2 running for 2h, pid 4242", brief)
            self.assertIs(json.loads(self.where(repo, "--json"))["run"]["alive"], True)
            self.assertIn(f"{GREEN}run P2: iteration 2: $1.20, 30 turns, CONTINUE{RST}", self.line(cwd=repo))
            self.assertIn("RUN live", self.py("where.py")[0])  # the home view
            yahlib.write_run(fd, dict(info, ended=int(now) - 600, code=2, reason="needs you: CI is red."))
        finally:
            os.close(fd)
        full = self.where(repo)
        self.assertIn("RUN     P2 ended 10m ago, exit 2: needs you: CI is red.", full)
        self.assertNotIn("running", full)
        self.assertIn(f"{AMBER}run P2 exit 2{RST}", self.line(cwd=repo))
        pidf.write_text(json.dumps(info), encoding="utf-8")  # no code and no lock: the driver was killed
        self.assertIn("RUN     P2 died 1m ago without an exit code, pid 4242, last: 14:35 iteration 2",
                      self.where(repo))
        self.assertIn(f"{RED}run P2 died{RST}", self.line(cwd=repo))
        pidf.write_text(json.dumps(dict(info, ended=int(now) - 4 * 86400, code=0, reason="plan done.")), "utf-8")
        for out in (self.where(repo), self.where(repo, "--brief"), self.line(cwd=repo)):  # over 3 days ago
            self.assertNotIn("RUN", out)
            self.assertNotIn("run P2", out)


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

    def test_dry_run_writes_nothing(self):
        self.settings(self.SETTINGS)
        (self.home / ".bashrc").write_text("export A=1\n", encoding="utf-8")
        scripts = self.plugin_copy(self.tmp / "plugin", rules="# Rules\n")
        (self.cfg / "plugins").mkdir()
        (self.cfg / "plugins" / "known_marketplaces.json").write_text(
            json.dumps({"you-are-here": {"source": {"source": "github", "repo": "ARYN26/you-are-here"}}}), "utf-8")
        before = self.snapshot()
        for args in (["--tier", "max20", "--yes"], ["--auto-update"], ["--auto-merge"],
                     ["--install-launcher", str(self.home / ".bashrc")],
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

    GH = {"source": "github", "repo": "ARYN26/you-are-here"}

    def known(self, **entries):
        p = self.cfg / "plugins" / "known_marketplaces.json"
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps(entries), encoding="utf-8")
        return p

    def no_update_env(self):
        for v in ("DISABLE_UPDATES", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                  "FORCE_AUTOUPDATE_PLUGINS", "CLAUDE_CODE_PLUGIN_CACHE_DIR"):
            self.env.pop(v, None)

    def test_auto_update_opt_in_and_uninstall(self):
        self.no_update_env()
        other = {"source": {"source": "github", "repo": "someone/else"}}
        original = dict(self.SETTINGS, statusLine={"type": "command", "command": "echo mine"},
                        extraKnownMarketplaces={"other": other, "you-are-here": {"source": self.GH}})
        self.settings(original)
        known = self.known(**{"you-are-here": {"source": self.GH, "installLocation": "x"}})
        known_before = known.read_bytes()
        out, err, rc = self.setup_py("--auto-update", "--yes")  # --yes must not reach the statusline
        self.assertEqual(rc, 0, err)
        self.assertIn("autoupdate on for you-are-here", out)
        self.assertNotIn("auto-update off", out)
        s = self.settings()
        self.assertEqual(s["extraKnownMarketplaces"], {"other": other,
                                                       "you-are-here": {"source": self.GH, "autoUpdate": True}})
        self.assertEqual({k: v for k, v in s.items() if k != "extraKnownMarketplaces"},
                         {k: v for k, v in original.items() if k != "extraKnownMarketplaces"})
        self.assertEqual(known.read_bytes(), known_before)  # Claude Code's own record is never written
        self.assertIn("autoupdate unchanged (on for you-are-here)", self.setup_py("--auto-update")[0])
        self.setup_py("--uninstall")
        self.assertEqual(self.settings(), original)

    def test_auto_update_adds_a_missing_entry_and_uninstall_removes_it(self):
        self.no_update_env()
        self.known(**{"you-are-here": {"source": self.GH, "autoUpdate": False}})
        out, err, rc = self.setup_py("--auto-update", "--yes")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.settings(), {"extraKnownMarketplaces":  # no statusLine, no config.json
                                           {"you-are-here": {"source": self.GH, "autoUpdate": True}}})
        self.assertFalse((self.data / "config.json").exists())
        self.setup_py("--uninstall")
        self.assertEqual(self.settings(), {})

    def test_auto_update_leaves_settings_alone_when_it_cannot_or_need_not_apply(self):
        self.no_update_env()
        for why, known, expect in (
                ("not installed", {}, "not an installed marketplace"),
                ("local", {"you-are-here": {"source": {"source": "directory", "path": "/src/yah"}}}, "loads in place"),
                ("already on", {"you-are-here": {"source": self.GH, "autoUpdate": True}}, "autoupdate unchanged")):
            with self.subTest(why):
                self.settings(self.SETTINGS)
                self.known(**known)
                out, err, rc = self.setup_py("--auto-update", "--yes")
                self.assertEqual(rc, 0, err)
                self.assertIn(expect, out)
                self.assertNotIn("extraKnownMarketplaces", self.settings())

    def test_auto_update_warns_when_an_env_var_turns_updates_off(self):
        self.no_update_env()
        self.known(**{"you-are-here": {"source": self.GH}})
        self.env["DISABLE_AUTOUPDATER"] = "0"  # a 0 turns nothing off
        self.assertNotIn("auto-update off", self.setup_py("--auto-update", "--yes")[0])
        self.env["DISABLE_AUTOUPDATER"] = "1"
        self.assertIn("WARNING    DISABLE_AUTOUPDATER", self.setup_py("--auto-update", "--yes")[0])
        self.env["FORCE_AUTOUPDATE_PLUGINS"] = "0"
        self.assertIn("WARNING    DISABLE_AUTOUPDATER", self.setup_py("--auto-update", "--yes")[0])
        self.env["FORCE_AUTOUPDATE_PLUGINS"] = "1"
        self.assertNotIn("auto-update off", self.setup_py("--auto-update", "--yes")[0])

    def test_auto_update_rerun_after_a_hand_edit_restores_the_latest_choice(self):
        self.no_update_env()
        self.known(**{"you-are-here": {"source": self.GH}})
        self.settings({"extraKnownMarketplaces": {"you-are-here": {"source": self.GH}}})
        self.setup_py("--auto-update")
        self.settings({})  # the user deletes the entry by hand, then runs setup again
        self.setup_py("--auto-update")
        self.setup_py("--uninstall")
        self.assertEqual(self.settings(), {})  # not a leftover {"source": ...} entry

    def read_config(self):
        return json.loads((self.data / "config.json").read_text("utf-8"))

    def test_auto_merge_opt_in_and_uninstall(self):
        self.settings(self.SETTINGS)
        for prior in (None, False, "true"):  # absent, off, and a string that never counted as on
            with self.subTest(prior=prior):
                cfg = {"tier": "max20", **({} if prior is None else {"auto_merge": prior})}
                self.config(**cfg)
                out, err, rc = self.setup_py("--auto-merge", "--yes")  # --yes must not reach the statusline
                self.assertEqual(rc, 0, err)
                self.assertIn("automerge  on:", out)
                self.assertNotIn("statusLine", out)
                self.assertEqual(self.read_config(), {**cfg, "auto_merge": True})
                self.assertEqual(self.settings(), self.SETTINGS)
                out = self.setup_py("--auto-merge", "--yes")[0]
                self.assertIn("automerge  unchanged (on)", out)
                out, err, rc = self.setup_py("--uninstall")
                self.assertEqual(rc, 0, err)
                self.assertIn("automerge  removed" if prior is None else "automerge  restored", out)
                self.assertEqual(self.read_config(), cfg)
                self.assertEqual(self.settings(), self.SETTINGS)

    def test_auto_merge_uninstall_keeps_a_later_hand_edit(self):
        self.setup_py("--auto-merge")
        self.config(auto_merge=False)  # the user turns it off by hand
        self.assertNotIn("automerge", self.setup_py("--uninstall")[0])
        self.assertEqual(self.read_config(), {"auto_merge": False})

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
        repo = self.state_repo()
        self.where(repo)
        scripts = self.plugin_copy(self.tmp / "plugin")
        (scripts / "run.py").write_text("import sys\nprint('RUN', sys.argv[1:])\n", encoding="utf-8")
        rc_file = self.home / rc_name
        self.assertEqual(self.setup_py("--install-launcher", str(rc_file), scripts=scripts)[2], 0)
        return repo, rc_file

    def check_launcher(self, argv, repo):
        p = subprocess.run(argv, capture_output=True, env=self.env, timeout=120)
        out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        out = out.replace('"', "")  # cmd's stand-in claude echoes the quotes around the prompt
        self.assertIn(f"claude --resume in {repo}", out, err)  # flags go to claude, with no prompt
        self.assertIn(f"claude /yah:auto in {repo}", out, err)  # no words: the session starts on /yah:auto
        self.assertIn(f"claude /yah:auto add pay in {repo}", out, err)  # words: /yah:auto gets them as the task
        self.assertIn("RUN ['a', 'b c']", out, err)
        self.assertIn("rc=1", out, err)
        self.assertIn("usage: yah <project>", err)
        return out

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"), "needs POSIX bash")
    def test_launcher_runs_in_bash(self):
        repo, rc_file = self.launcher_fixture(".bashrc")
        script = (f'claude() {{ echo "claude $* in $PWD"; }}; . "{rc_file}"; '
                  'yah shop --resume; yah shop; yah shop add pay; yah run a "b c"; yah; echo "rc=$?"')
        self.check_launcher(["bash", "-c", script], repo)

    @unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "needs PowerShell")
    def test_launcher_runs_in_powershell(self):
        repo, rc_file = self.launcher_fixture("profile.ps1")
        script = (f"function claude {{ \"claude $args in $((Get-Location).Path)\" }}; . '{rc_file}'; "
                  "yah shop --resume; yah shop; yah shop add pay; yah run a 'b c'; yah; \"rc=$LASTEXITCODE\"")
        shell = shutil.which("pwsh") or shutil.which("powershell")
        self.check_launcher([shell, "-NoProfile", "-NonInteractive", "-Command", script], repo)

    def test_cmd_launcher_file(self):
        shim = self.home / "bin" / "yah.cmd"
        for _ in range(2):
            out, err, rc = self.setup_py("--install-launcher", str(shim))
            self.assertEqual(rc, 0, err)
        self.assertIn("unchanged", out)
        raw = shim.read_bytes()
        self.assertTrue(raw.startswith(b"@rem >>> you-are-here >>>\r\n@echo off\r\n"), raw[:60])
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))  # CRLF only: cmd's goto can miss labels in LF files
        self.assertIn(b'endlocal & cd /d "%YAH_D%" && claude %YAH_REST%\r\n', raw)
        self.assertIn(b'endlocal & cd /d "%YAH_D%" && claude "/yah:auto%YAH_REST%"\r\n', raw)
        self.assertIn(b"\\where.py\" --path \"%~1\"", raw)
        printed = self.setup_py("--launcher", "cmd")[0].replace("\r\n", "\n").strip()
        self.assertEqual(printed, raw.decode("utf-8").replace("\r\n", "\n").strip())
        foreign = self.home / "other" / "yah.cmd"
        foreign.parent.mkdir()
        foreign.write_bytes(b"@echo mine\r\n")
        out, err, rc = self.setup_py("--install-launcher", str(foreign))
        self.assertEqual(rc, 1)
        self.assertIn("did not write it", err)
        self.assertEqual(foreign.read_bytes(), b"@echo mine\r\n")
        self.assertEqual(self.setup_py("--launcher", "bash", "--install-launcher", str(foreign))[2], 1)
        self.assertEqual(self.setup_py("--launcher", "cmd", "--install-launcher", str(self.home / "yah"))[2], 1)
        self.setup_py("--uninstall")
        self.assertFalse(shim.exists())
        self.assertEqual(foreign.read_bytes(), b"@echo mine\r\n")

    @unittest.skipUnless(os.name == "nt", "needs cmd.exe")
    def test_launcher_runs_in_cmd(self):
        repo, shim = self.launcher_fixture("bin/yah.cmd")
        (shim.parent / "claude.cmd").write_text("@echo claude %* in %CD%\r\n", encoding="utf-8")
        driver = self.tmp / "drive.cmd"  # call, so each yah returns here; typed at a prompt it needs none
        driver.write_text('@echo off\r\ncall yah shop --resume\r\necho after=%CD%\r\ncall yah shop\r\n'
                          'call yah shop add pay\r\ncall yah run a "b c"\r\ncall yah\r\necho rc=%errorlevel%\r\n'
                          'set YAH_\r\n', encoding="utf-8")
        self.env["PATH"] = str(shim.parent) + os.pathsep + self.env.get("PATH", "")
        out = self.check_launcher([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(driver)], repo)
        self.assertIn(f"after={repo}", out)  # the cd outlives yah, as in the other shells
        self.assertNotIn("YAH_", out)  # setlocal kept yah's variables out of the window

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
        repo = self.state_repo()
        out, rc = self.sh(start, json.dumps({"hook_event_name": "SessionStart", "source": "startup",
                                             "cwd": str(repo)}), repo, path)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("[yah] shop  branch checkout/payment"), out)
        self.assertIn("PLAN CHECKOUT 1/3 done  PHASE P2 Payment form", out)
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
    def test_where_core_is_bd_free(self):
        # beads.py is the only file that knows beads: where.py reads it through beads.read()
        src = (SCRIPTS / "where.py").read_text("utf-8")
        for gone in ("BEADS_DIR", 'find_tool("bd"', "def load_issues", "def beads_state", "parent-child",
                     "bd update", "import shutil"):
            self.assertNotIn(gone, src, gone)
        self.assertIn("beads.read(", src)

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
