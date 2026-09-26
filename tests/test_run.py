"""Tests for scripts/run.py (`yah run`). Stdlib unittest only:

    python -m unittest tests.test_run -v

Fake `claude`, `gh` and notifier executables go first on PATH: POSIX scripts with a sys.executable
shebang, .cmd shims on Windows. script.json in $YAH_FAKE_DIR scripts their output per call (the
last entry repeats), and each fake appends its argv to <name>.log there. CLAUDE_CONFIG_DIR, HOME and
USERPROFILE point at a temp dir, so the real ~/.claude is never touched.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "scripts" / "run.py"
sys.path.insert(0, str(ROOT / "scripts"))
import yahlib  # noqa: E402
IS_WIN = os.name == "nt"
NOTIFIER = "osascript" if sys.platform == "darwin" else "powershell" if IS_WIN else "notify-send"
URL = "https://example.com/pr/12"
GREEN = "PR #12 is open and green. The merge is yours."
# The SPEC's DENY patterns, spelled out here so the test does not trust run.py's own list.
DENY = ["PowerShell", "Bash(git push --force*)", "Bash(git push -f*)", "Bash(git push *--force*)", "Bash(git push * -f*)",
        "Bash(git push *+*)", "Bash(gh pr merge*)", "Bash(gh api *merge*)", "Bash(gh repo delete*)",
        "Bash(git push *--delete*)", "Bash(git push *--mirror*)", "Bash(git push *--all*)", "Bash(git push *--prune*)"]
PER_BRANCH = ["Bash(git push * {0})", "Bash(git push * {0} *)", "Bash(git push * HEAD:{0}*)", "Bash(git push * *:{0}*)",
              "Bash(git push *refs/heads/{0}*)"]
STATE = """# Shop

## Plan: Checkout rewrite (docs/plans/checkout.md)
- [x] P1 Cart API | branch checkout/cart | PR #11
- [~] P2 Payment form | branch checkout/payment | base main
- [ ] P3 Emails

## 2026-09-23
- Next: Wire the payment form.
"""
CLOSED = STATE.replace("- [~] P2 Payment form | branch checkout/payment | base main",
                       "- [x] P2 Payment form | branch checkout/payment | base main | PR #12")
MERGE_OK = "PR #12 merged by yah (merge commit)."

FAKE = r'''
import json, os, re, subprocess, sys, time
args = sys.argv[1:]
role = os.path.basename(sys.argv[0]).split(".")[0]
if role == "fake":
    role, args = args[0], args[1:]
d = os.environ["YAH_FAKE_DIR"]
with open(os.path.join(d, role + ".log"), "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
with open(os.path.join(d, "script.json"), encoding="utf-8") as f:
    script = json.load(f)


def step(key):
    seq = script.get(key) or [{}]
    counter = os.path.join(d, re.sub(r"[^\w.-]", "_", key) + ".count")  # view-<branch> may hold a /
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    with open(counter, "w") as f:
        f.write(str(n + 1))
    return seq[min(n, len(seq) - 1)]


def emit(ev):
    print(json.dumps(ev), flush=True)


if role == "gh":
    if args[:2] == ["pr", "list"]:  # --base: the PRs stacked on a merged head
        print(json.dumps(script.get("stacked" if "--base" in args else "list", [])))
    elif args[:2] in (["pr", "merge"], ["pr", "edit"]) or args[:1] == ["api"]:
        c = step("user" if "user" in args else args[1] if args[0] == "pr" else "delete")
        sys.stdout.write(c.get("out", ""))
        sys.stderr.write(c.get("err", ""))
        sys.exit(c.get("rc", 0))
    elif args[:2] == ["pr", "view"]:  # view-<ref> scripts one PR; view scripts the rest
        ref = "view-" + (args[2] if len(args) > 2 else "")
        print(json.dumps(step(ref if ref in script else "view")))
    elif args[:2] == ["pr", "checks"]:
        c = step("required" if "--required" in args else "checks")
        sys.stdout.write(c.get("out", ""))
        sys.stderr.write(c.get("err", ""))
        sys.exit(c.get("rc", 0))
    else:
        sys.exit(1)
elif role == "claude":
    c = step("claude")
    with open(os.path.join(d, "claude.env"), "w", encoding="utf-8") as f:
        f.write(os.environ.get("YAH_PROTECTED", "-"))
    with open(os.path.join(d, "claude.detached"), "w", encoding="utf-8") as f:
        f.write(os.environ.get("YAH_RUN_DETACHED", "-"))
    emit({"type": "system", "subtype": "init", "permissionMode": c.get("mode", "auto")})
    for name, inp in c.get("tools", []):
        emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})
    if "rate" in c:
        emit({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {"seven_day": c["rate"]}}})
    if c.get("orphan"):  # a child in its own session, like the Bash tool's shells
        beat, t = os.path.join(d, "orphan.txt"), time.time()
        subprocess.Popen([sys.executable, "-c", "import sys, time\nfor _ in range(600):\n"
                          "    open(sys.argv[1], 'a').write('x')\n    time.sleep(0.05)", beat],
                         start_new_session=os.name != "nt", stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while not os.path.exists(beat) and time.time() - t < 10:
            time.sleep(0.05)
    time.sleep(c.get("sleep", 0))
    if os.path.exists("STATE.md"):
        with open("STATE.md", encoding="utf-8") as f:
            text = f.read()
        if c.get("close"):
            text = text.replace("- [~] P2", "- [x] P2")
        for old, new in c.get("replace", []):
            text = text.replace(old, new)
        if c.get("next"):
            text = re.sub(r"(?m)^- Next:.*$", "- Next: " + c["next"], text)
        with open("STATE.md", "w", encoding="utf-8", newline="") as f:
            f.write(text)
    if c.get("commit"):
        with open("work.txt", "a") as f:
            f.write("x\n")
        subprocess.run(["git", "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "wip"], check=True, capture_output=True)
    result = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.25, "num_turns": 3,
              "permission_denials": [], "result": "Done.\nYAH-RESULT: " + c.get("tag", "progress")}
    result.update(c.get("result", {}))
    emit(result)
'''


def view(state="OPEN", commit="2026-09-20T10:00:00Z", reviews=(), **kw):
    return dict({"state": state, "url": URL, "reviewDecision": "", "reviews": list(reviews),
                 "commits": [{"oid": "abc", "committedDate": commit}], "headRefName": "checkout/payment",
                 "baseRefName": "main", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "isDraft": False,
                 "author": {"login": "aryan"}, "headRefOid": "abc1234def"}, **kw)


def review(login, state, at):
    return {"author": {"login": login}, "state": state, "submittedAt": at}


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="yah-run-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.cfg, self.fake = self.tmp / "home", self.tmp / "claude", self.tmp / "fake"
        for d in (self.home, self.cfg, self.fake):
            d.mkdir()
        self.data = self.cfg / "you-are-here"
        (self.tmp / "fake.py").write_text(FAKE, encoding="utf-8")
        self.bin = self.make_bin("bin", ("claude", "gh", NOTIFIER))
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.cfg), HOME=str(self.home), USERPROFILE=str(self.home),
                        YAH_FAKE_DIR=str(self.fake), YAH_RUN_POLL_S="0.05", GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com",
                        PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""))

    # ------------------------------------------------------------ fixtures

    def make_bin(self, name, roles):
        b = self.tmp / name
        b.mkdir()
        for role in roles:
            if IS_WIN:
                (b / f"{role}.cmd").write_text(f'@"{sys.executable}" "{self.tmp / "fake.py"}" {role} %*\r\n',
                                               encoding="utf-8")
            else:
                exe = b / role
                exe.write_text(f"#!{sys.executable}\n" + FAKE, encoding="utf-8")
                exe.chmod(0o755)
        return b

    def config(self, **cfg):
        self.data.mkdir(exist_ok=True)
        (self.data / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def git(self, repo, *args):
        subprocess.run(["git", *args], cwd=str(repo), env=self.env, check=True, capture_output=True)

    def repo(self, branch="checkout/payment", name="shop", state=STATE):
        r = self.tmp / name
        r.mkdir()
        if state:
            (r / "STATE.md").write_text(state, encoding="utf-8")
        self.git(r, "init", "-q")
        self.git(r, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        self.git(r, "add", "-A")
        self.git(r, "commit", "-q", "--allow-empty", "-m", "init")
        return r

    def script(self, **data):
        for f in self.fake.iterdir():
            f.unlink()
        (self.fake / "script.json").write_text(json.dumps(data), encoding="utf-8")

    def calls(self, role):
        f = self.fake / f"{role}.log"
        return [json.loads(ln) for ln in f.read_text("utf-8").splitlines()] if f.exists() else []

    def prompts(self):
        return [argv[argv.index("-p") + 1] for argv in self.calls("claude")]

    def run_yah(self, cwd, *args, env=None, code=None):
        t = time.monotonic()
        p = subprocess.run([sys.executable, str(RUN), *args], cwd=str(cwd), env=env or self.env,
                           capture_output=True, timeout=90)
        self.elapsed = time.monotonic() - t
        out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
        if code is not None:
            self.assertEqual(p.returncode, code, out)
        for argv in self.calls("claude"):
            self.assert_safe(argv)
        self.assert_merge_safe(out)
        return out

    def assert_merge_safe(self, out):
        """Every run: a merge is a merge commit pinned to a head, nothing squashes, rebases, forces or deletes the
        branch with the merge, the ref DELETE comes after the merge and every retarget, and auto_merge off asks gh
        nothing new and prints nothing new."""
        calls = self.calls("gh")
        for a in calls:
            for bad in ("--squash", "--rebase", "--delete-branch", "--admin", "--auto"):
                self.assertNotIn(bad, a, a)
            if a[:2] == ["pr", "merge"]:
                self.assertEqual(a[3:5], ["--merge", "--match-head-commit"], a)
                self.assertEqual(len(a), 6, a)
        kinds = [("delete" if a[:3] == ["api", "-X", "DELETE"] else "merge" if a[:2] == ["pr", "merge"] else
                  "retarget" if a[:2] == ["pr", "edit"] or (a[:2] == ["pr", "list"] and "--base" in a) else "")
                 for a in calls]
        for i, k in enumerate(kinds):
            if k == "delete":  # after its merge, and no retarget for that merge after it
                nxt = kinds.index("merge", i) if "merge" in kinds[i:] else len(kinds)
                self.assertIn("merge", kinds[:i], calls)
                self.assertNotIn("retarget", kinds[i + 1:nxt], calls)
        cfg = self.data / "config.json"
        if not (cfg.exists() and json.loads(cfg.read_text("utf-8")).get("auto_merge") is True):
            self.assertFalse(self.merge_calls(), calls)
            self.assertNotIn("Auto-merge", out)
            self.assertNotIn("merged by yah", out)

    def assert_safe(self, argv, branches=("main", "master")):
        i = argv.index("--disallowedTools")
        for pat in DENY + [p.format(b) for b in branches for p in PER_BRANCH]:
            self.assertIn(pat, argv[i + 1:])
        for bad in ("--bare", "bypassPermissions", "--dangerously-skip-permissions"):
            self.assertFalse([a for a in argv if bad in a], bad)
        for flag, val in (("--permission-mode", "auto"), ("--permission-prompts", "none"),
                          ("--output-format", "stream-json")):
            self.assertEqual(argv[argv.index(flag) + 1], val)
        self.assertIn("--verbose", argv)
        self.assertIn("--max-budget-usd", argv)

    def run_logs(self):
        return sorted((self.data / "runs").glob("*.log"))

    # ------------------------------------------------------------ exit 5

    def test_refuses_to_start(self):
        self.script()
        (self.tmp / "plain").mkdir()
        cases = [("outside a repo", self.tmp / "plain", [], None, "not inside a git repo"),
                 ("on a trunk", self.repo("main", "trunk"), [], None, "a trunk or the PROD branch"),
                 ("bad target", self.repo(name="shop"), ["later"], None, "TARGET must be")]
        shop = self.tmp / "shop"
        cases.append(("unknown phase", shop, ["P9"], None, "no phase P9 in this repo's plan. Phases: P1, P2, P3."))
        cases.append(("no claude", shop, [], self.make_bin("nc", ("gh",)), "claude not found"))
        homebrew = [Path("/opt/homebrew/bin/gh"), Path("/usr/local/bin/gh")]
        if not any(p.exists() for p in homebrew):
            cases.append(("no gh", shop, ["P2"], self.make_bin("ngh", ("claude",)), "GitHub CLI) not found"))
        for name, cwd, args, path, text in cases:
            with self.subTest(name):
                env = dict(self.env, PATH=str(path)) if path else None
                self.assertIn(text, self.run_yah(cwd, *args, env=env, code=5))
        self.assertEqual(self.calls("claude"), [])
        self.assertFalse((self.data / "runs").exists())

    # ------------------------------------------------------------ --detach and the pid file

    def test_detach_returns_while_the_run_goes_on_and_holds_the_lock(self):
        self.script(list=[], view=[view()], required=[{"rc": 0}],
                    claude=[{"sleep": 6, "commit": True, "close": True, "next": "Start P3.", "tag": "pr-open #12"}])
        repo = self.repo()
        out = self.run_yah(repo, "--detach", code=0)
        pidf = self.data / "runs" / "shop.pid"
        st = yahlib.run_state(pidf)
        self.assertTrue(st and st["alive"], (st, out))
        self.assertIn(f"run started in the background: P2, pid {st['pid']}", out)
        self.assertNotIn("stop (exit", out)
        self.assertEqual((st["target"], st["branch"], st["plan"]), ("P2", "checkout/payment", False))
        busy = f"a run is already going in this checkout: pid {st['pid']}, P2"
        self.assertIn(busy, self.run_yah(repo, code=5))
        self.assertIn(busy, self.run_yah(repo, "--detach", code=5))
        t = time.monotonic()
        while st["alive"] and time.monotonic() - t < 60:
            time.sleep(0.2)
            st = yahlib.run_state(pidf) or st
        self.assertFalse(st["alive"], "the background run did not end")
        self.assertEqual((st["code"], st["reason"]), (0, GREEN))
        self.assertIn("[yah] stop (exit 0): " + GREEN, Path(st["out"]).read_text("utf-8"))
        self.assertIn("stop, exit 0: " + GREEN, Path(st["log"]).read_text("utf-8"))
        self.assertEqual(Path(st["out"]).with_suffix(".log"), Path(st["log"]))
        self.assertEqual(self.prompts(), ["/yah:resume P2 build"])
        self.assertEqual((self.fake / "claude.detached").read_text("utf-8"), "-")  # sessions never see the marker

    def test_a_held_pid_file_refuses_and_a_stale_one_does_not(self):
        self.script(view=[view()], required=[{"rc": 0}])
        repo = self.repo()
        pidf = self.data / "runs" / "shop.pid"
        fd = yahlib.hold_run(pidf, {"pid": 4242, "target": "P2", "log": "old.log"})
        try:
            for args in ([], ["--detach"]):
                with self.subTest(args=args):
                    out = self.run_yah(repo, *args, code=5)
                    self.assertIn("already going in this checkout: pid 4242, P2, since ?, log old.log", out)
        finally:
            os.close(fd)
        self.assertEqual(self.calls("claude"), [])
        self.assertEqual(self.run_logs(), [])
        self.assertIn(GREEN, self.run_yah(repo, "#12", code=0))  # its holder is gone, so the lock is free
        st = yahlib.run_state(pidf)
        self.assertEqual((st["alive"], st["target"], st["code"], st["out"]), (False, "#12", 0, ""))
        self.assertNotEqual(st["pid"], 4242)

    # ------------------------------------------------------------ exit 0 and MODE

    def test_green_pr_stops_before_any_session(self):
        self.script(view=[view()], required=[{"rc": 0}])
        out = self.run_yah(self.repo(), "#12", code=0)
        self.assertIn(GREEN, out)
        self.assertIn(URL, out)
        self.assertEqual(self.calls("claude"), [])
        self.assertEqual(len(self.calls(NOTIFIER)), 1)
        log = self.run_logs()[0].read_text("utf-8")
        self.assertIn("stop, exit 0: " + GREEN, log)

    def test_phase_runs_until_its_pr_is_green_and_closed(self):
        self.script(list=[], view=[view()], required=[{"rc": 0}],
                    claude=[{"tools": [["Bash", {"command": "npm test -- --run"}]], "commit": True,
                             "close": True, "next": "Start P3.", "tag": "pr-open #12"}])
        out = self.run_yah(self.repo(), code=0)
        self.assertEqual(self.prompts(), ["/yah:resume P2 build"])
        self.assertIn("  Bash npm test -- --run", out)
        self.assertRegex(out, r"iteration 1: \$0\.25, 3 turns, pr-open #12, HEAD [0-9a-f]{7}, NEXT Start P3\.")
        self.assertIn(GREEN, out)
        log = self.run_logs()[0]
        self.assertIn("iteration 1: $0.25", log.read_text("utf-8"))
        raw = log.with_name(log.stem + "-1.jsonl").read_text("utf-8")
        self.assertIn('"type": "result"', raw)

    def test_green_pr_waits_for_the_phase_to_close(self):
        self.script(list=[{"number": 12, "headRefName": "checkout/payment", "baseRefName": "main"}],
                    view=[view()], required=[{"rc": 0}],
                    claude=[{"commit": True, "next": "Close P2."}, {"commit": True, "close": True}])
        self.run_yah(self.repo(), code=0)
        self.assertEqual(self.prompts(), ["/yah:resume P2 build"] * 2)

    def test_pending_checks_are_polled(self):
        self.script(view=[view()], required=[{"rc": 8}, {"rc": 8}, {"rc": 0}])
        out = self.run_yah(self.repo(), "#12", code=0)
        self.assertIn("checks pending", out)
        self.assertEqual(len([a for a in self.calls("gh") if "--required" in a]), 3)
        self.assertEqual(self.calls("claude"), [])

    def test_no_required_checks_falls_back_and_no_checks_pass(self):
        self.script(view=[view()], required=[{"rc": 1, "err": "no required checks reported on the 'x' branch\n"}],
                    checks=[{"rc": 1, "err": "no checks reported on the 'x' branch\n"}])
        self.run_yah(self.repo(), "12", code=0)
        self.assertIn(["pr", "checks", "12"], self.calls("gh"))

    def test_failing_checks_switch_to_fix_checks(self):
        self.script(view=[view()], required=[{"rc": 1, "out": "ci\tfail\t1m\turl\n"}, {"rc": 0}],
                    claude=[{"commit": True}])
        self.run_yah(self.repo(), "#12", code=0)
        self.assertEqual(self.prompts(), ["/yah:resume #12 fix-checks"])

    def test_changes_requested_switch_to_address_review(self):
        reviews = [review("b", "CHANGES_REQUESTED", "2026-09-21T09:00:00Z"),
                   review("b", "APPROVED", "2026-09-21T11:00:00Z"),
                   review("a", "CHANGES_REQUESTED", "2026-09-21T10:00:00Z")]
        self.script(view=[view(reviews=reviews), view(commit="2026-09-22T10:00:00Z", reviews=reviews)],
                    required=[{"rc": 0}], claude=[{"commit": True}])
        self.run_yah(self.repo(), "#12", code=0)
        self.assertEqual(self.prompts(), ["/yah:resume #12 address-review"])

    # ------------------------------------------------------------ exit 2, 3, 4, 6, 7

    def test_needs_you_stops_with_exit_2(self):
        repo = self.repo()
        denial = {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "git push origin main"}}
        cases = [("needs-human", {"tag": "needs-human", "next": "NEEDS-HUMAN: Which card processor?"},
                  "needs you: NEEDS-HUMAN: Which card processor?"),
                 ("blocked", {"tag": "blocked tests need a database"}, "blocked: tests need a database"),
                 ("denial", {"result": {"permission_denials": [denial]}},
                  "permission denied: Bash git push origin main"),
                 ("error", {"result": {"is_error": True, "subtype": "error_max_budget_usd"}}, "error_max_budget_usd"),
                 ("no YAH-RESULT", {"result": {"result": "Done."}},  # the plugin was not loaded, say
                  "session ended without a YAH-RESULT line; is the yah plugin loaded? (--plugin-dir)")]
        for name, step, text in cases:
            with self.subTest(name):
                self.script(claude=[dict(step, commit=True)])
                self.assertIn(text, self.run_yah(repo, code=2))
                self.assertEqual(len(self.calls("claude")), 1)

    def test_timeout_kills_the_iteration_and_its_detached_children(self):
        self.config(run_iteration_minutes=0.05)
        self.script(claude=[{"sleep": 30, "orphan": True}])
        out = self.run_yah(self.repo(), code=2)
        self.assertIn("timed out after 0.05 min", out)
        self.assertLess(self.elapsed, 25)
        beat = self.fake / "orphan.txt"
        time.sleep(0.5)
        size = beat.stat().st_size
        time.sleep(1)
        self.assertEqual(beat.stat().st_size, size, "the detached child outlived the kill")

    def test_stall_stops_with_exit_3(self):
        self.script(claude=[{"tag": "progress"}])
        self.assertIn("unchanged for 2 iterations", self.run_yah(self.repo(), code=3))
        self.assertEqual(len(self.calls("claude")), 2)

    def test_caps_stop_with_exit_4(self):
        repo = self.repo()
        self.script(claude=[{"commit": True}])
        self.assertIn("cap of 2 iterations", self.run_yah(repo, "--iterations", "2", code=4))
        self.assertEqual(len(self.calls("claude")), 2)
        self.config(run_total_hours=0.0006)
        self.script(claude=[{"commit": True, "sleep": 3}])
        self.assertIn("run_total_hours", self.run_yah(repo, code=4))
        self.assertEqual(len(self.calls("claude")), 1)

    def test_merged_or_closed_pr_stops_with_exit_6(self):
        repo = self.repo()
        for state in ("MERGED", "CLOSED"):
            with self.subTest(state):
                self.script(view=[view(state)])
                self.assertIn(f"PR #12 is {state.lower()}", self.run_yah(repo, "#12", code=6))
                self.assertEqual(self.calls("claude"), [])

    # ------------------------------------------------------------ --plan

    def test_plan_chains_phases_until_the_plan_is_done(self):
        state = STATE.replace("- [ ] P3 Emails", "- [ ] P3 Emails | branch checkout/emails")
        p2 = "- [~] P2 Payment form | branch checkout/payment | base main"
        p3 = "- [~] P3 Emails | branch checkout/emails"
        self.script(list=[], required=[{"rc": 0}], **{
            "view-12": [view()],
            "view-13": [view(headRefName="checkout/emails", baseRefName="checkout/payment")],
            "claude": [{"commit": True, "tag": "pr-open #12", "next": "Write the emails.",
                        "replace": [[p2, p2.replace("[~]", "[x]") + " | PR #12"], ["- [ ] P3", "- [~] P3"]]},
                       {"commit": True, "tag": "pr-open #13", "replace": [[p3, p3.replace("[~]", "[x]") + " | PR #13"]]}]})
        out = self.run_yah(self.repo(state=state), "--plan", "--iterations", "1", code=0)  # the cap is per phase
        self.assertEqual(self.prompts(), ["/yah:resume P2 build", "/yah:resume P3 build base=checkout/payment"])
        self.assertIn("P2 done: PR #12 open and green. Next: P3 on checkout/payment "
                      "(stacked on P2's PR #12, still open)", out)
        self.assertIn("plan done: P2 PR #12 green, P3 PR #13 green. The merges are yours.", out)
        self.assertIn("2 iterations", out)
        self.assertEqual(len(list((self.data / "runs").glob("*-[12].jsonl"))), 2)  # per-run numbering, no clobber

    def test_plan_builds_on_where_a_stacked_parent_merged(self):
        repo = self.repo(state=STATE.replace("| branch checkout/payment | base main",
                                             "| branch checkout/payment | base checkout/cart"))
        for state, base, arg in (("MERGED", "base    main (P1's PR #11 merged into it; the plan's base for P2 is "
                                  "checkout/cart)", " base=main"),
                                 ("OPEN", "base    checkout/cart (the plan's base for P2)", "")):
            with self.subTest(state):
                self.script(list=[], **{"view-11": [{"number": 11, "state": state, "baseRefName": "main"}]})
                out = self.run_yah(repo, "--plan", "--dry-run", code=0)
                self.assertIn(base, out)
                self.assertIn(f"/yah:resume P2 build{arg}'", out)
                self.assertIn("plan    P2 checkout/payment <- " + base.split()[1] + " | P3 (new branch) <- P2's branch;", out)
                self.assertEqual(self.calls("claude"), [])

    def test_plan_goes_on_past_a_merged_phase_but_not_a_closed_pr(self):
        state = STATE.replace("- [~] P2 Payment form | branch checkout/payment | base main",
                              "- [x] P2 Payment form | branch checkout/payment | base main | PR #12")
        repo = self.repo(state=state)
        self.script(list=[], required=[{"rc": 0}], **{
            "view-12": [view("MERGED")], "view-13": [view(headRefName="checkout/emails")],
            "claude": [{"commit": True, "tag": "pr-open #13", "replace": [["- [ ] P3", "- [x] P3"]]}]})
        out = self.run_yah(repo, "--plan", "P2", code=0)
        self.assertEqual(self.prompts(), ["/yah:resume P3 build base=main"])
        self.assertIn("plan done: P2 PR #12 merged, P3 PR #13 green. The merges are yours.", out)
        self.script(list=[], **{"view-12": [view("CLOSED")]})
        self.assertIn("PR #12 is closed.", self.run_yah(repo, "--plan", "P2", code=6))
        self.assertEqual(self.calls("claude"), [])

    def test_plan_refusals_and_trunk_start(self):
        self.script(list=[])
        repo = self.repo()
        self.assertIn("TARGET must be P<n> or empty, not a PR", self.run_yah(repo, "--plan", "#12", code=5))
        done = self.repo(name="done", state=STATE.replace("[~] P2", "[x] P2").replace("[ ] P3", "[x] P3"))
        self.assertIn("--plan found no open phase", self.run_yah(done, "--plan", code=5))
        trunk = self.repo("main", "trunk")  # the plan pins the phase, so a trunk checkout is fine
        self.assertIn("target  current phase (phase P2)", self.run_yah(trunk, "--plan", "--dry-run", code=0))
        self.assertIn("a trunk or the PROD branch", self.run_yah(trunk, code=5))
        self.assertEqual(self.calls("claude"), [])

    def test_plan_stacks_a_branchless_phase_on_the_open_pr_below(self):
        p2 = "- [~] P2 Payment form | branch checkout/payment | base main"
        pr12 = {"number": 12, "title": "P2", "headRefName": "checkout/payment", "baseRefName": "main", "isDraft": False,
                "reviewDecision": "", "statusCheckRollup": [], "updatedAt": "2026-09-24T10:00:00Z"}
        self.script(list=[pr12], required=[{"rc": 0}], **{
            "view-12": [view()], "view-13": [view(headRefName="p3", baseRefName="checkout/payment")],
            "claude": [{"commit": True, "tag": "pr-open #12",
                        "replace": [[p2, p2.replace("[~]", "[x]") + " | PR #12"], ["- [ ] P3", "- [~] P3"]]},
                       {"commit": True, "tag": "pr-open #13", "replace": [["- [~] P3", "- [x] P3"]]}]})
        out = self.run_yah(self.repo(), "--plan", code=0)
        # P2's branch is still checked out and gh lists its open PR: neither is P3's
        self.assertEqual(self.prompts(), ["/yah:resume P2 build", "/yah:resume P3 build base=checkout/payment"])
        self.assertIn("Next: P3 on checkout/payment (stacked on P2's PR #12, still open)", out)
        self.assertIn("checkout/payment", (self.fake / "claude.env").read_text("utf-8").split(","))

    def test_plan_checks_the_stop_rules_before_the_next_phase(self):
        p2 = "- [~] P2 Payment form | branch checkout/payment | base main"
        self.script(list=[], required=[{"rc": 0}], **{"view-12": [view()], "claude": [
            {"commit": True, "tag": "pr-open #12", "rate": {"utilization": 0.95, "resetsAt": time.time() + 86400},
             "replace": [[p2, p2.replace("[~]", "[x]") + " | PR #12"], ["- [ ] P3", "- [~] P3"]]}]})
        out = self.run_yah(self.repo(), "--plan", code=7)
        self.assertIn("P2 done: PR #12 open and green", out)
        self.assertEqual(self.prompts(), ["/yah:resume P2 build"])

    def test_plan_stops_at_a_you_phase_a_question_or_the_plan_end(self):
        p2 = "- [~] P2 Payment form | branch checkout/payment | base main"
        other = "\n## Plan: Admin (docs/plans/admin.md)\n- [ ] P1 Roles | base main\n- [ ] P3 Audit | base main\n"
        cases = [("(you) phase", STATE.replace("- [ ] P3 Emails", "- [ ] P3 Emails: sign off the copy (you)"),
                  {"tag": "pr-open #12"}, 2, "needs you: P3 (Emails: sign off the copy) is marked (you). "
                  "So far: P2 PR #12 green."),
                 ("a question", STATE, {"tag": "needs-human", "next": "NEEDS-HUMAN: SES or SendGrid?"}, 2,
                  "needs you: NEEDS-HUMAN: SES or SendGrid? (P2's PR #12 is open and green.)"),
                 ("another plan below", STATE.replace("- [ ] P3 Emails", "- [x] P3 Emails") + other,
                  {"tag": "pr-open #12"}, 0, "plan done: P2 PR #12 green.")]
        for i, (name, state, claude, code, text) in enumerate(cases):
            with self.subTest(name):
                self.script(list=[], required=[{"rc": 0}], **{"view-12": [view()], "claude": [
                    dict(claude, commit=True, replace=[[p2, p2.replace("[~]", "[x]") + " | PR #12"]])]})
                self.assertIn(text, self.run_yah(self.repo(name=f"shop{i}", state=state), "--plan", code=code))
                self.assertEqual(len(self.calls("claude")), 1)

    def test_weekly_usage_stops_with_exit_7(self):
        repo, now = self.repo(), time.time()
        self.data.mkdir()
        lim = {"ts": now, "five_hour": {"used_pct": 10, "resets_at": now}, "pace_pct": 86,
               "seven_day": {"used_pct": 85, "resets_at": now + 86400}}
        (self.data / "limits.json").write_text(json.dumps(lim), encoding="utf-8")
        self.script()
        self.assertIn("weekly usage 85% is at or over", self.run_yah(repo, code=7))
        self.assertEqual(self.calls("claude"), [])
        (self.data / "limits.json").unlink()
        self.script(claude=[{"commit": True, "rate": {"utilization": 0.5, "resetsAt": now + 6 * 86400}}])
        out = self.run_yah(repo, code=7)
        self.assertIn("weekly usage 50% is over pace 14% + 15", out)
        self.assertEqual(len(self.calls("claude")), 1)
        self.assertIn("pace unknown", self.run_logs()[-1].read_text("utf-8"))

    # ------------------------------------------------------------ auto-merge (exit 8)

    def auto(self, cfg=None, **script):
        """auto_merge on, gh user aryan, required checks passing, no open PRs."""
        self.config(auto_merge=True, **(cfg or {}))
        self.script(**dict({"list": [], "required": [{"rc": 0}], "user": [{"out": "aryan\n"}]}, **script))

    def merges(self):
        return [a for a in self.calls("gh") if a[:2] == ["pr", "merge"]]

    def merge_calls(self):
        """The gh calls only auto-merge makes, in order."""
        return [a for a in self.calls("gh") if a[:2] in (["pr", "merge"], ["pr", "edit"]) or a[:1] == ["api"]
                or (a[:2] == ["pr", "list"] and "--base" in a)]

    def test_auto_merge_merges_then_retargets_then_deletes(self):
        self.auto(**{"view-12": [view()], "stacked": [{"number": 14}, {"number": 15}]})
        out = self.run_yah(self.repo(state=CLOSED), "P2", code=8)
        self.assertEqual(self.merge_calls(), [
            ["api", "user", "--jq", ".login"],
            ["pr", "merge", "12", "--merge", "--match-head-commit", "abc1234def"],
            ["pr", "list", "--base", "checkout/payment", "--state", "open", "--json", "number"],
            ["pr", "edit", "14", "--base", "main"], ["pr", "edit", "15", "--base", "main"],
            ["api", "-X", "DELETE", "repos/{owner}/{repo}/git/refs/heads/checkout/payment"]])
        self.assertIn("stop (exit 8): " + MERGE_OK, out)
        log = self.run_logs()[0].read_text("utf-8")
        self.assertIn("stop, exit 8: " + MERGE_OK, log)
        for line in ("merged PR #12 into main (merge commit of abc1234)", "retargeted PR #15 from checkout/payment "
                     "to main", "deleted branch checkout/payment on origin"):
            self.assertIn("[yah] " + line, out)
            self.assertIn(line, log)
        self.assertEqual(self.calls("claude"), [])

    def test_auto_merge_plan_builds_the_next_phase_on_the_merged_base(self):
        state = STATE.replace("- [x] P1 Cart API | branch checkout/cart | PR #11",
                              "- [x] P1 Cart API | branch checkout/cart | base staging | PR #11") \
            .replace("- [~] P2 Payment form | branch checkout/payment | base main", "- [ ] P2 Payment form") \
            .replace("- [ ] P3 Emails\n", "")
        repo = self.repo("checkout/cart", state=state)
        self.auto(**{"view-11": [view(headRefName="checkout/cart", baseRefName="staging", headRefOid="1111111aa")],
                     "view-13": [view(headRefName="p2", baseRefName="staging", headRefOid="1313131bb")],
                     "claude": [{"commit": True, "tag": "pr-open #13",
                                 "replace": [["- [ ] P2 Payment form",
                                              "- [x] P2 Payment form | branch p2 | PR #13"]]}]})
        out = self.run_yah(repo, "--plan", "P1", code=0)
        self.assertEqual(self.prompts(), ["/yah:resume P2 build base=staging"])
        self.assertIn("P1 done: PR #11 merged by yah. Next: P2 on staging (where P1's PR #11 merged)", out)
        self.assertIn("plan done: P1 PR #11 merged by yah, P2 PR #13 merged by yah.", out)
        self.assertNotIn("The merges are yours", out)
        self.assertEqual([a for a in self.merge_calls() if a[:2] == ["pr", "merge"] or a[0] == "api"], [
            ["api", "user", "--jq", ".login"],
            ["pr", "merge", "11", "--merge", "--match-head-commit", "1111111aa"],
            ["api", "-X", "DELETE", "repos/{owner}/{repo}/git/refs/heads/checkout/cart"],
            ["pr", "merge", "13", "--merge", "--match-head-commit", "1313131bb"],
            ["api", "-X", "DELETE", "repos/{owner}/{repo}/git/refs/heads/p2"]])

    def test_auto_merge_skips_an_ineligible_pr(self):
        repo = self.repo(state=CLOSED)
        cr = [review("b", "CHANGES_REQUESTED", "2026-09-21T09:00:00Z")]
        cases = [("no checks", "P2", {"view-12": [view()], "checks": [{"rc": 1, "err": "no checks reported\n"}],
                                      "required": [{"rc": 1, "err": "no required checks reported\n"}]},
                  "no checks ran"),
                 ("#N target", "#12", {"view-12": [view()]}, "PR #12 is not a plan phase's PR")]
        # The other rules one by one: test_mergeable_needs_every_rule, in-process.
        for name, target, script, why in cases:
            with self.subTest(name):
                self.auto(**script)
                self.assertIn(f"stop (exit 0): {GREEN} Auto-merge skipped: {why}.", self.run_yah(repo, target, code=0))
                self.assertFalse(self.merges())
        self.assertNotIn(["api", "user", "--jq", ".login"], self.calls("gh"))  # #N: no phase, so no need to ask
        # Not green, so evaluate never offers a merge: a session runs instead, and this one asks a question.
        for name, script in (("pending", {"required": [{"rc": 8}]}),
                             ("changes requested", {"view-12": [view(reviews=cr)]})):
            with self.subTest(name):
                self.auto({"run_checks_wait_minutes": 0}, **dict({"view-12": [view()]}, **script,
                                                                  claude=[{"tag": "needs-human"}]))
                self.run_yah(repo, "P2", code=2)
                self.assertEqual(self.merge_calls(), [])
        # Green and closed, but the session stopped: evaluate says green before it reads r.last.
        for name, session, why in (("open", {"tag": "needs-human"}, "needs you: the session asked for you"),
                                   ("erred", {"result": {"is_error": True, "subtype": "error_max_budget_usd"}},
                                    "iteration 1 ended with an error")):
            with self.subTest(name):
                self.auto(**{"view-12": [view()], "list": [{"number": 12, "headRefName": "checkout/payment",
                                                            "baseRefName": "main"}],
                             "claude": [dict(session, close=True, commit=True)]})
                out = self.run_yah(self.repo(name=name), "P2", code=0)
                self.assertIn(f"{GREEN} Auto-merge skipped: the last session stopped: {why}", out)
                self.assertEqual(self.merge_calls(), [])
        # evaluate's view can predate a long checks wait: the merge reads the PR again and trusts only that read.
        for name, views, why in (("changes requested during the wait", [view(), view(reviews=cr)], "changes requested"),
                                 ("back to draft during the wait", [view(), view(isDraft=True)], "it is a draft"),
                                 ("merged by hand during the wait", [view(), view("MERGED")],
                                  "PR #12 is merged, not open"),
                                 ("a new commit after the checks", [view(), view(headRefOid="fff9999aaa")],
                                  "a new commit landed after its checks passed")):
            with self.subTest(name):
                self.auto(**{"view-12": views})
                self.assertIn(f"stop (exit 0): {GREEN} Auto-merge skipped: {why}.", self.run_yah(repo, "P2", code=0))
                self.assertFalse(self.merges())

    def test_auto_merge_trusts_the_fresh_view(self):
        """Right after a push GitHub says BLOCKED or UNKNOWN; once checks pass the fresh read says CLEAN."""
        repo = self.repo(state=CLOSED)
        for name, views in (("blocked, then clean", [view(mergeStateStatus="BLOCKED"), view()]),
                            ("unknown until computed", [view(mergeable="UNKNOWN"), view(mergeable="UNKNOWN"), view()])):
            with self.subTest(name):
                self.auto(**{"view-12": views})
                self.assertIn("stop (exit 8): " + MERGE_OK, self.run_yah(repo, "P2", code=8))
                self.assertIn(["pr", "merge", "12", "--merge", "--match-head-commit", "abc1234def"], self.calls("gh"))
        with self.subTest("still unknown after the retries"):
            self.auto(**{"view-12": [view(mergeable="UNKNOWN")]})
            self.assertIn("Auto-merge skipped: mergeable is UNKNOWN.", self.run_yah(repo, "P2", code=0))
            self.assertFalse(self.merges())

    def test_auto_merge_failures_and_a_protected_head(self):
        repo = self.repo(state=CLOSED)
        self.auto(**{"view-12": [view()], "merge": [{"rc": 1, "err": "GraphQL: Head branch was modified. Review "
                                                                     "and try the merge again. (mergePullRequest)\n"}]})
        out = self.run_yah(repo, "P2", code=2)
        self.assertIn("auto-merge of PR #12 failed: GraphQL: Head branch was modified.", out)
        self.assertEqual([a[:2] for a in self.merge_calls()], [["api", "user"], ["pr", "merge"]])
        self.auto(**{"view-12": [view()], "stacked": [{"number": 14}, {"number": 15}],
                     "edit": [{"rc": 1, "err": "GraphQL: Could not update base\n"}]})
        out = self.run_yah(repo, "P2", code=2)
        self.assertIn("PR #12 merged, but #14 could not be retargeted from checkout/payment to main (GraphQL: Could "
                      "not update base). Head branch checkout/payment kept.", out)
        self.assertEqual([a[:2] for a in self.merge_calls()], [["api", "user"], ["pr", "merge"], ["pr", "list"],
                                                               ["pr", "edit"]])
        self.auto(**{"view-12": [view()], "delete": [{"rc": 1, "err": "gh: Reference does not exist (HTTP 422)\n"}]})
        out = self.run_yah(repo, "P2", code=8)  # a failed delete is a warning
        self.assertIn("warning: could not delete checkout/payment on origin (gh: Reference does not exist", out)
        for head, cfg in (("checkout/payment", {"projects": {"shop": {"trunks": ["checkout/payment"]}}}),
                          ("master", {})):
            with self.subTest(head):
                self.auto(cfg, **{"view-12": [view(headRefName=head)]})
                out = self.run_yah(repo, "P2", code=8)
                self.assertIn(f"kept {head}: a protected branch is never deleted", out)
                self.assertEqual([a[:2] for a in self.merge_calls()],
                                 [["api", "user"], ["pr", "merge"], ["pr", "list"]])

    # ------------------------------------------------------------ argv and --dry-run

    def test_deny_covers_trunks_and_prod_and_passes_model_and_budget(self):
        self.config(projects={"shop": {"prod": "`release` deploys on push. PRs only.", "trunks": ["stable"]}})
        repo = self.repo()
        self.script(claude=[{"tag": "needs-human"}])
        self.run_yah(repo, "P2", "--model", "haiku", "--budget", "3", code=2)
        argv = self.calls("claude")[0]
        self.assert_safe(argv, ("main", "master", "develop", "dev", "stable", "release"))
        self.assertEqual(argv[argv.index("-p") + 1], "/yah:resume P2 build")
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "3")
        self.assertEqual(argv[argv.index("--plugin-dir") + 1], str(ROOT))  # a checkout, not an installed plugin
        self.assertIn("Bash(git push -d*)", argv)
        hooks = json.loads(argv[argv.index("--settings") + 1])["hooks"]
        self.assertIn("context_guard.py", hooks["PostToolUse"][0]["hooks"][0]["command"])
        self.assertEqual([h["matcher"] for h in hooks["PreToolUse"]], ["Bash", "PowerShell"])
        self.assertIn("push_guard.py", hooks["PreToolUse"][0]["hooks"][0]["command"])
        self.assertEqual((self.fake / "claude.env").read_text("utf-8"), "dev,develop,main,master,release,stable")
        self.git(repo, "symbolic-ref", "HEAD", "refs/heads/release")
        self.assertIn("PROD branch", self.run_yah(repo, code=5))

    def test_no_config_still_protects_where_open_prs_land(self):
        repo = self.repo()
        self.script(claude=[{"tag": "needs-human"}],
                    list=[{"number": 7, "headRefName": "feat/a", "baseRefName": "live"},
                          {"number": 8, "headRefName": "feat/b", "baseRefName": "feat/a"}])
        self.run_yah(repo, "P2", code=2)
        self.assert_safe(self.calls("claude")[0], ("main", "master", "live"))
        self.assertNotIn("feat/a", (self.fake / "claude.env").read_text("utf-8").split(","))
        self.git(repo, "symbolic-ref", "HEAD", "refs/heads/live")
        self.assertIn("PROD branch", self.run_yah(repo, code=5))

    def test_phase_base_is_protected_when_gh_sees_no_prs(self):
        state = STATE.replace("branch checkout/payment | base main", "branch feat/x | base aryan_dev")
        repo = self.repo("aryan_dev", state=state)
        self.git(repo, "checkout", "-q", "-b", "feat/x")
        self.script(list=[], claude=[{"tag": "needs-human"}])
        self.run_yah(repo, "P2", code=2)
        self.assert_safe(self.calls("claude")[0], ("main", "master", "aryan_dev"))
        self.assertIn("aryan_dev", (self.fake / "claude.env").read_text("utf-8").split(","))
        out = self.run_yah(repo, "--dry-run", code=0)
        self.assertIn("base    aryan_dev (the plan's base for P2)", out)
        self.git(repo, "checkout", "-q", "aryan_dev")
        self.assertIn("a trunk or the PROD branch", self.run_yah(repo, code=5))

    def test_cached_landing_survives_an_empty_pr_list(self):
        repo = self.repo()
        self.script(list=[{"number": 7, "headRefName": "feat/a", "baseRefName": "live"}])
        subprocess.run([sys.executable, str(ROOT / "scripts" / "where.py")], cwd=str(repo), env=self.env,
                       check=True, capture_output=True)
        self.script(list=[], claude=[{"tag": "needs-human"}])
        self.run_yah(repo, "P2", code=2)
        self.assert_safe(self.calls("claude")[0], ("main", "master", "live"))

    def test_refuses_without_a_pr_base_and_protects_the_branch_head_was_cut_from(self):
        repo = self.repo("main", state=None)  # no plan, no remote, no config.json
        self.git(repo, "checkout", "-q", "-b", "aryan_dev")
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "dev")
        self.git(repo, "checkout", "-q", "-b", "feat/x")
        self.script()
        out = self.run_yah(repo, code=5)
        self.assertIn("run refused: cannot tell which branch this run's PR targets", out)
        self.assertIn("Set base in the plan", out)
        self.assertIn("or prod in config.json", out)
        self.assertEqual(self.calls("claude"), [])
        self.git(repo, "update-ref", "refs/remotes/origin/main", "main")
        self.git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        out = self.run_yah(repo, "--dry-run", code=0)
        self.assertIn("base    main (origin/HEAD)", out)
        self.assertIn("protect aryan_dev, dev, develop, main, master", out)
        self.assertIn("Bash(git push * aryan_dev)", out)
        self.script(view=[view(baseRefName="staging")])
        out = self.run_yah(repo, "#12", "--dry-run", code=0)  # the PR's own base beats origin/HEAD
        self.assertIn("base    staging (the base of PR #12)", out)
        self.assertIn("Bash(git push * staging)", out)
        self.script(view=[view(baseRefName="")], required=[{"rc": 1, "out": "ci\tfail\n"}])
        self.git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        out = self.run_yah(repo, "#12", code=5)  # gh names no base for the PR either
        self.assertIn("run refused: cannot tell which branch PR #12 targets", out)
        self.assertEqual(self.calls("claude"), [])

    def test_project_name_shared_by_two_repos_is_refused(self):
        self.script()
        (self.tmp / "a").mkdir()
        (self.tmp / "b").mkdir()
        repos = [self.repo(name="a/api"), self.repo(name="b/api")]
        for r in repos:
            subprocess.run([sys.executable, str(ROOT / "scripts" / "where.py"), "--no-gh", "--no-bd"], cwd=str(r),
                           env=self.env, check=True, capture_output=True)
        self.assertEqual(len(list(self.data.glob("where-api-*.json"))), 2)
        out = self.run_yah(self.tmp, "--project", "API", "--dry-run", code=5)
        self.assertIn("'API' matches 2 repos", out)
        for r in repos:
            self.assertIn(str(r), out)
        self.config(projects={"api-b": {"path": str(repos[1])}})  # naming one in config.json settles it
        self.assertIn(f"repo    {repos[0]}  ", self.run_yah(self.tmp, "--project", "api", "--dry-run", code=0))

    def test_stops_when_auto_mode_is_unavailable(self):
        self.script(claude=[{"mode": "default", "sleep": 30}])
        t = time.time()
        out = self.run_yah(self.repo(), "P2", code=2)
        self.assertIn("not auto", out)
        self.assertLess(time.time() - t, 25)
        self.assertEqual(len(self.calls("claude")), 1)

    def test_dry_run_spawns_no_claude(self):
        self.script(view=[view()], required=[{"rc": 1, "out": "ci\tfail\n"}])
        out = self.run_yah(self.repo(), "#12", "--dry-run", code=0)
        self.assertIn("dry run", out)
        self.assertIn("/yah:resume #12 fix-checks", out)
        self.assertIn("Bash(gh pr merge*)", out)
        self.assertIn("caps    8 iterations, $10 per iteration, 45 min per iteration, 6 h total", out)
        self.assertIn("would run iteration 1 in MODE fix-checks", out)
        self.assertIn("base    main (the base of PR #12)", out)
        self.assertIn(f"plugin  --plugin-dir {ROOT} (this checkout", out)
        self.assertIn("week    pace unknown", out)
        self.assertIn("merge   auto-merge off", out)
        self.assertEqual(self.calls("claude"), [])
        self.assertTrue(self.calls("gh"))
        self.assertFalse((self.data / "runs").exists())

    def test_dry_run_says_what_auto_merge_would_do(self):
        repo = self.repo(state=CLOSED)
        self.script(list=[], required=[{"rc": 0}], **{"view-12": [view()]})
        self.assertRegex(self.run_yah(repo, "P2", "--dry-run", code=0), r"(?m)^merge   auto-merge off\s*$")
        cases = [({"view-12": [view()]}, "merge   auto-merge on: would merge PR #12 (merge commit)"),
                 ({"view-12": [view(mergeStateStatus="BEHIND")]},
                  "merge   auto-merge on: would not merge PR #12: merge state BEHIND"),
                 ({"view-12": [view()], "required": [{"rc": 1, "out": "ci\tfail\n"}]}, "merge   auto-merge on\n")]
        for script, line in cases:
            with self.subTest(line):
                self.auto(**script)
                self.assertIn(line, self.run_yah(repo, "P2", "--dry-run", code=0).replace("\r\n", "\n"))
                self.assertFalse(self.merges())


class HelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("yah_run", RUN)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_run_lock_and_pid_file(self):
        tmp = Path(tempfile.mkdtemp(prefix="yah-lock-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        pidf = tmp / "runs" / "shop.pid"
        self.assertIsNone(yahlib.run_state(pidf))
        fd = yahlib.hold_run(pidf, {"pid": 1, "reason": "long " * 50})
        self.assertIsNotNone(fd)
        self.assertIsNone(yahlib.hold_run(pidf, {"pid": 2}))  # a second fd conflicts even in the same process
        self.assertEqual(yahlib.run_state(pidf), {"pid": 1, "reason": "long " * 50, "alive": True})
        yahlib.write_run(fd, {"pid": 1, "code": 0})  # shorter than before: the old tail is cut
        self.assertEqual(yahlib.run_state(pidf), {"pid": 1, "code": 0, "alive": True})
        os.close(fd)
        self.assertEqual(yahlib.run_state(pidf), {"pid": 1, "code": 0, "alive": False})
        fd = yahlib.hold_run(pidf, {"pid": 3})
        self.assertIsNotNone(fd)
        os.close(fd)
        self.assertEqual(yahlib.run_pid_path("shop", str(tmp / "shop"), str(tmp / "shop")).name, "shop.pid")
        self.assertEqual(yahlib.run_pid_path("shop", str(tmp / "wt 2"), str(tmp / "shop")).name, "shop@wt_2.pid")

    def test_targets_and_prod_branch(self):
        pt = self.mod.parse_target
        self.assertEqual([pt(""), pt("p2"), pt("#12"), pt("12"), pt("x")], ["", "P2", "#12", "#12", None])
        pb = self.mod.prod_branch
        self.assertEqual([pb("main deploys on push."), pb("Vercel builds `release` only"), pb("")],
                         ["main", "release", ""])

    def test_plugin_source(self):
        ps = self.mod.plugin_source
        cache = Path("/c/claude/plugins/cache/mkt/yah/0.1.0/scripts/run.py")
        self.assertIsNone(ps(None, cache)[0])
        self.assertIsNone(ps(None, Path("/c/claude/plugins/marketplaces/mkt/scripts/run.py"))[0])
        self.assertEqual(ps(None, RUN)[0], str(ROOT))
        self.assertEqual(ps(str(ROOT / "scripts"), cache)[0], str(ROOT / "scripts"))

    def test_changes_requested_uses_each_reviewers_latest_verdict(self):
        cr = self.mod.changes_requested
        old = view(reviews=[review("b", "CHANGES_REQUESTED", "2026-09-21T09:00:00Z"),
                            review("b", "APPROVED", "2026-09-21T11:00:00Z")])
        self.assertFalse(cr(old))
        self.assertTrue(cr(view(reviews=[review("a", "CHANGES_REQUESTED", "2026-09-21T09:00:00Z")])))
        self.assertFalse(cr(view(commit="2026-09-22T00:00:00Z",
                                 reviews=[review("a", "CHANGES_REQUESTED", "2026-09-21T09:00:00Z")])))

    def test_mergeable_needs_every_rule(self):
        def r(**kw):
            return SimpleNamespace(**dict({"phase": {"label": "P2", "branch": "checkout/payment", "pr": ""},
                                           "label": "P2", "pr": 12, "checks": "pass", "login": "aryan", "gh": None,
                                           "top": ".", "phases": [], "prev": None}, **kw))
        cr = [review("b", "CHANGES_REQUESTED", "2026-09-21T09:00:00Z")]
        cases = [(r(), view(), ""),
                 (r(phase={"label": "P2", "branch": "other", "pr": "#12"}), view(), ""),  # the phase's PR by number
                 (r(), view(mergeStateStatus="HAS_HOOKS"), ""),
                 (r(phase=None), view(), "PR #12 is not a plan phase's PR"),
                 (r(phase={"label": "P2", "branch": "other", "pr": "#9"}), view(), "PR #12 is not P2's PR"),
                 (r(phase={"label": "P2"}), view(), "PR #12 is not P2's PR"),
                 (r(checks="none"), view(), "no checks ran"),
                 (r(checks="pending"), view(), "checks pending"),
                 (r(checks="fail"), view(), "checks fail"),
                 (r(), view(isDraft=True), "it is a draft"),
                 (r(), view(mergeable="UNKNOWN"), "mergeable is UNKNOWN"),
                 (r(), view(mergeStateStatus="UNSTABLE"), "merge state UNSTABLE"),
                 (r(), view(mergeStateStatus="DIRTY"), "merge state DIRTY"),
                 (r(), view(mergeStateStatus="BLOCKED"), "merge state BLOCKED"),
                 (r(), view(mergeable="CONFLICTING"), "mergeable is CONFLICTING"),
                 (r(), view(reviews=cr), "changes requested"),
                 (r(), view(headRefOid=""), "gh did not name"),
                 (r(phases=[{"label": "P1", "branch": "checkout/cart"}]), view(baseRefName="checkout/cart"),
                  "it targets checkout/cart, another phase's branch"),
                 (r(prev={"label": "P1", "branch": "p1"}), view(baseRefName="p1"), "another phase's branch"),
                 (r(login=""), view(), "gh api user did not say who you are"),
                 (r(), view(author={"login": "mallory"}), "its author mallory is not you (aryan)")]
        for i, (run, v, why) in enumerate(cases):
            with self.subTest(i=i, why=why):
                ok, reason = self.mod.mergeable(run, v)
                self.assertEqual(ok, not why, reason)
                self.assertIn(why, reason)


if __name__ == "__main__":
    unittest.main()
