"""Tests for scripts/push_guard.py, the PreToolUse hook `yah run` adds for Bash. Stdlib unittest only:

    python -m unittest tests.test_push_guard -v

Two temp repos, one on a protected branch (main) and one on a feature branch, each with a `sub` folder.
The table runs in-process; the hook's stdin/stdout contract runs as a subprocess. CLAUDE_CONFIG_DIR, HOME
and USERPROFILE point at a temp dir, so the real ~/.claude is never touched.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "scripts" / "push_guard.py"
PROTECTED = {"main", "master", "release"}

# (command, repo it runs in, a phrase the deny reason must contain)
DENIED = [
    ("git push -f origin feat/x", "feat", "force"),
    ("git push --force origin feat/x", "feat", "force"),
    ("git push --force-with-lease origin feat/x", "feat", "force-with-lease"),
    ("git push --force-with-lease=feat/x:abc origin feat/x", "feat", "force-with-lease"),
    ("git push --force-if-includes origin feat/x", "feat", "force-if-includes"),
    ("git push -uf origin feat/x", "feat", "force"),
    ("git push -fu origin feat/x", "feat", "force"),
    ("git push --forc origin feat/x", "feat", "force"),
    ("git push origin +feat/x", "feat", "+refspec"),
    ("git push --mirror origin", "feat", "--mirror"),
    ("git push --all origin", "feat", "--all"),
    ("git push --delete origin feat/y", "feat", "--delete"),
    ("git push -d origin feat/y", "feat", "delete"),
    ("git push -ud origin feat/y", "feat", "delete"),
    ("git push --prune origin feat/x", "feat", "--prune"),
    ("git push origin :feat/y", "feat", "delete"),
    ("git push origin main", "feat", "push to main"),
    ("git push origin HEAD:main", "feat", "push to main"),
    ("git push origin feat/x:refs/heads/main", "feat", "push to main"),
    ("git push origin refs/heads/release", "feat", "push to release"),
    ("git push origin 'refs/heads/*:refs/heads/*'", "feat", "wildcard"),
    ("git push", "main", "from main"),
    ("git push origin", "main", "from main"),
    ("git push -u origin HEAD", "main", "from main"),
    ("git push origin 2>&1", "main", "from main"),
    ('git push -u origin "$(git branch --show-current)"', "main", "from main"),
    ("cd ../main-repo && git push", "feat", "from main"),
    ("git -C ../main-repo push", "feat", "from main"),
    ("git --git-dir=../main-repo/.git push origin", "feat", "from main"),
    ("git -c push.default=current push origin main", "feat", "push to main"),
    ("/usr/bin/git push origin main", "feat", "push to main"),
    ("FOO=1 git push origin main", "feat", "push to main"),
    ("git status && git push origin main", "feat", "push to main"),
    ("echo ok; git push -f origin feat/x", "feat", "force"),
    ("true || git push -f origin feat/x", "feat", "force"),
    ("git add -A\ngit push origin main", "feat", "push to main"),
    ("git push origin \\\n  --force feat/x", "feat", "force"),
    ("bash -c 'git push --force origin feat/x'", "feat", "force"),
    ('bash -lc "git push origin main"', "feat", "push to main"),
    ("eval git push -f origin feat/x", "feat", "force"),
    ("git push origin 'feat/x", "feat", "could not parse"),
    ("gh pr merge 12 --squash", "feat", "gh pr merge"),
    ("gh api -X PUT repos/o/r/pulls/12/merge", "feat", "merge through gh api"),
    ("gh repo delete o/r --yes", "feat", "gh repo delete"),
]
ALLOWED = [
    ("git push -u origin feat/x", "feat"),
    ("git push -u origin feat/x", "main"),
    ("git -C . push origin feat/x", "feat"),
    ("git push --follow-tags origin feat/x", "feat"),
    ("cd sub && git push origin feat/x", "main"),
    ("cd sub && git push", "feat"),
    ("git push", "feat"),
    ("git push origin HEAD", "feat"),
    ("git push origin feat/x 2>&1 | tail -5", "main"),
    ('git commit -m "never git push --force; merge is yours" && git push -u origin feat/x', "feat"),
    ("echo 'git push --force origin main'", "feat"),
    ("git status", "main"),
    ("ls -la && npm test", "main"),
    ("gh pr view 12 --comments", "feat"),
    ("gh pr create --fill --base main", "feat"),
]


def load_guard():
    spec = importlib.util.spec_from_file_location("yah_push_guard", GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PushGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="yah-guard-")).resolve()
        home, cfg = cls.tmp / "home", cls.tmp / "claude"
        home.mkdir()
        cfg.mkdir()
        fake = {"CLAUDE_CONFIG_DIR": str(cfg), "HOME": str(home), "USERPROFILE": str(home),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}
        cls.env = dict(os.environ, **fake)
        cls.patcher = mock.patch.dict(os.environ, fake)  # for in-process calls
        cls.patcher.start()
        cls.repos = {name: cls.repo(f"{name}-repo", branch) for name, branch in (("main", "main"), ("feat", "feat/x"))}
        cls.guard = load_guard()

    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()
        shutil.rmtree(cls.tmp, True)

    @classmethod
    def repo(cls, name, branch):
        r = cls.tmp / name
        (r / "sub").mkdir(parents=True)
        (r / "sub" / "a.txt").write_text("a\n", encoding="utf-8")
        for args in (["init", "-q"], ["symbolic-ref", "HEAD", f"refs/heads/{branch}"], ["add", "-A"],
                     ["commit", "-q", "-m", "init"]):
            subprocess.run(["git", *args], cwd=str(r), env=cls.env, check=True, capture_output=True)
        return r

    def hook(self, payload, protected=",".join(sorted(PROTECTED))):
        env = dict(self.env, YAH_PROTECTED=protected)
        p = subprocess.run([sys.executable, str(GUARD)], input=payload.encode("utf-8"), capture_output=True,
                           env=env, timeout=60)
        self.assertEqual((p.returncode, p.stderr), (0, b""))
        return p.stdout.decode("utf-8")

    def test_denied(self):
        for cmd, where, phrase in DENIED:
            with self.subTest(cmd=cmd, repo=where):
                why = self.guard.problem(cmd, str(self.repos[where]), PROTECTED)
                self.assertIsNotNone(why)
                self.assertIn(phrase, why)

    def test_allowed(self):
        for cmd, where in ALLOWED:
            with self.subTest(cmd=cmd, repo=where):
                self.assertIsNone(self.guard.problem(cmd, str(self.repos[where]), PROTECTED))

    def test_hook_contract(self):
        def payload(cmd, tool="Bash"):
            return json.dumps({"tool_name": tool, "tool_input": {"command": cmd}, "cwd": str(self.repos["main"])})
        out = json.loads(self.hook(payload("git push")))
        hso = out["hookSpecificOutput"]
        self.assertEqual((set(out), hso["hookEventName"], hso["permissionDecision"]),
                         ({"hookSpecificOutput"}, "PreToolUse", "deny"))
        self.assertTrue(hso["permissionDecisionReason"].startswith("[yah] blocked a push from main"))
        self.assertIn("The merge is the user's.", hso["permissionDecisionReason"])
        self.assertEqual(self.hook(payload("git push -u origin feat/x")), "")
        self.assertEqual(self.hook(payload("git push --force"), protected=""), "")  # not inside `yah run`
        self.assertEqual(self.hook(payload("git push --force", tool="Read")), "")
        # The PowerShell tool is guarded too: `;` and `& git` split like Bash, so the same rules apply.
        for cmd in ("git push origin main", "cd x; git push --force", "& git push -u origin main", "gh pr merge 12"):
            with self.subTest(powershell=cmd):
                out = json.loads(self.hook(payload(cmd, tool="PowerShell")))
                self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.hook(payload("git push -u origin feat/x", tool="PowerShell")), "")
        self.assertEqual(self.hook("not json"), "")


if __name__ == "__main__":
    unittest.main()
