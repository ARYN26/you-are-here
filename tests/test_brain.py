"""Tests for brain.py and recall_hook.py. Stdlib unittest only:

    python -m unittest tests.test_brain -v

Scripts run as subprocesses with sys.executable and a temp CLAUDE_CONFIG_DIR, HOME and USERPROFILE.
In-process calls patch the same variables and reset yahlib._config, so the real ~/.claude is never touched.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def load_brain():
    spec = importlib.util.spec_from_file_location("yah_brain", SCRIPTS / "brain.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def note(title, tldr, tags=(), paths=(), source="PR #1", status="", superseded_by="", body=""):
    lines = ["---", "title: " + title]
    if tags:
        lines.append("tags: [{}]".format(", ".join(tags)))
    if paths:
        lines.append("paths: [{}]".format(", ".join('"{}"'.format(p) for p in paths)))
    if status:
        lines.append("status: " + status)
    if superseded_by:
        lines.append("superseded_by: " + superseded_by)
    if source:
        lines.append("source: " + source)
    lines += ["---", tldr] + ([body] if body else [])
    return "\n".join(lines) + "\n"


VAULT = {
    "vercel-deploys-main": note(
        "Vercel deploys only from main", "Vercel builds production only from main; previews come from PR branches.",
        ("deploy", "vercel"), ("vercel.json", ".github/**"), "PR #41, 2026-09-20",
        body="Links: [[preview-env-vars]], [[e2e-seed-user]]."),
    "preview-env-vars": note(
        "Preview env vars live in the dashboard", "Preview builds read env vars from the hosting dashboard.",
        ("env",), source="PR #44"),
    "release-branch-deploys": note(
        "Deploys run from the release branch", "Production deploys run from the release branch.", ("deploy",),
        source="PR #12", status="superseded", superseded_by="vercel-deploys-main"),
    "api-rate-limit": note(
        "Partner API rate-limits at 10 rps", "Batch writes; the partner API rejects bursts over 10 requests a second.",
        ("api", "rate-limit"), ("src/api/**",), "docs/partner.md:12"),
    "stripe-webhooks": note(
        "Stripe webhooks need the raw body", "Verify Stripe signatures against the raw request body.",
        ("stripe", "payments"), ("src/payments/*.ts",), "https://example.com/stripe"),
    "db-migrations": note(
        "Migrations run only in CI", "Never run database migrations against the shared staging database.",
        ("database", "migration"), source="user, 2026-09-18"),
    "e2e-seed-user": note(
        "End-to-end tests need the seeded user", "Run the seed script before the end-to-end suite.",
        ("testing", "e2e"), source="commit 1a2b3c4"),
    "log-format": note(
        "Logs are JSON lines", "Every service logs one JSON object per line.", ("logging",), source="PR #30",
        body="Links: [[api-rate-limit]]."),
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="yah-brain-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.cfg = self.tmp / "home", self.tmp / "claude"
        self.home.mkdir()
        self.cfg.mkdir()
        self.data = self.cfg / "you-are-here"
        fake = {"CLAUDE_CONFIG_DIR": str(self.cfg), "HOME": str(self.home), "USERPROFILE": str(self.home),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}
        self.env = dict(os.environ, **fake)
        patcher = mock.patch.dict(os.environ, fake)  # for in-process calls
        patcher.start()
        self.addCleanup(patcher.stop)
        self.brain = load_brain()
        self.yahlib = sys.modules["yahlib"]
        self.yahlib._config = None
        self.addCleanup(setattr, self.yahlib, "_config", None)

    def py(self, script, *args, stdin="", cwd=None):
        p = subprocess.run([sys.executable, str(SCRIPTS / script), *args], input=stdin.encode("utf-8"),
                           capture_output=True, cwd=str(cwd or self.home), env=self.env, timeout=60)
        return (p.stdout.decode("utf-8").replace("\r\n", "\n"), p.stderr.decode("utf-8").replace("\r\n", "\n"),
                p.returncode)

    def bp(self, repo, *args):
        return self.py("brain.py", *args, cwd=repo)

    def git(self, repo, *args):
        subprocess.run(["git", *args], cwd=str(repo), env=self.env, check=True, capture_output=True)

    def repo(self, notes=None, name="shop"):
        """A git repo on main with the notes in docs/brain, then a feature branch that changed
        src/payments/charge.ts (committed) and src/api/client.py (uncommitted)."""
        r = self.tmp / name
        files = {"README.md": "shop\n", "src/api/client.py": "x = 1\n"}
        files.update({"docs/brain/{}.md".format(k): v for k, v in (notes or {}).items()})
        for rel, text in files.items():
            (r / rel).parent.mkdir(parents=True, exist_ok=True)
            (r / rel).write_bytes(text.encode("utf-8"))
        self.git(r, "init", "-q")
        self.git(r, "symbolic-ref", "HEAD", "refs/heads/main")
        self.git(r, "add", "-A")
        self.git(r, "commit", "-q", "-m", "init")
        self.git(r, "checkout", "-q", "-b", "feature")
        (r / "src" / "payments").mkdir(parents=True)
        (r / "src" / "payments" / "charge.ts").write_text("export {}\n", encoding="utf-8")
        self.git(r, "add", "-A")
        self.git(r, "commit", "-q", "-m", "charge")
        (r / "src" / "api" / "client.py").write_text("x = 2\n", encoding="utf-8")
        return r

    def recall_json(self, repo, *args):
        out, err, rc = self.bp(repo, "recall", "--json", *args)
        self.assertEqual((rc, err), (0, ""))
        return json.loads(out)

    def scores(self, repo, *args):
        return {m["slug"]: m["score"] for m in self.recall_json(repo, *args)["matches"]}

    def note_file(self, repo, slug):
        return repo / "docs" / "brain" / (slug + ".md")


class RecallTests(Base):
    def test_right_note_ranks_first(self):
        repo = self.repo(VAULT)
        out, err, rc = self.bp(repo, "recall", "how", "do", "we", "deploy", "to", "production", "on", "vercel")
        self.assertEqual((rc, err), (0, ""))
        lines = out.splitlines()
        self.assertRegex(lines[0], r'^\[yah\] brain: \d+ of 7 notes match "how do we deploy to production on vercel" '
                                   r'\(docs/brain, full list in INDEX\.md\)$')
        self.assertEqual(lines[1], "- vercel-deploys-main: Vercel builds production only from main; previews come "
                                   "from PR branches. (PR #41, 2026-09-20)")
        self.assertEqual(self.recall_json(repo, "stripe", "signature")["matches"][0]["slug"], "stripe-webhooks")

    def test_superseded_notes_are_excluded(self):
        repo = self.repo(VAULT)
        r = self.recall_json(repo, "deploys run from the release branch")
        slugs = [m["slug"] for m in r["matches"]] + r["also"]
        self.assertIn("vercel-deploys-main", slugs)
        self.assertNotIn("release-branch-deploys", slugs)
        self.assertEqual(r["total"], 7)
        out, _, _ = self.bp(repo, "find", "release", "branch", "deploys")
        self.assertNotIn("release-branch-deploys", out)
        out, _, _ = self.bp(repo, "find", "release", "branch", "deploys", "--all")
        self.assertEqual(out.splitlines()[0].split(" | ")[:2], ["release-branch-deploys", "superseded"])

    def test_paths_globs_match_diff_and_status(self):
        repo = self.repo(VAULT)
        # nothing in the text matches: only the +4 for globs over the changed files scores
        self.assertEqual(self.scores(repo, "zzz"), {"api-rate-limit": 4, "stripe-webhooks": 4})
        (repo / ".github" / "workflows").mkdir(parents=True)
        (repo / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")  # untracked
        self.assertEqual(set(self.scores(repo, "zzz")), {"api-rate-limit", "stripe-webhooks", "vercel-deploys-main"})
        # the where cache's phase base wins over main: feature...HEAD is empty, so only status paths count
        self.data.mkdir(exist_ok=True)
        self.yahlib.where_cache_path(repo).write_text(json.dumps({"phase": {"base": "feature"}}), encoding="utf-8")
        self.assertEqual(set(self.scores(repo, "zzz")), {"api-rate-limit", "vercel-deploys-main"})

    def test_glob_rules(self):
        hit = self.brain.glob_hit
        self.assertTrue(hit(".github/**", {".github/workflows/ci.yml"}))
        self.assertTrue(hit("vercel.json", {"apps/web/vercel.json"}))
        self.assertTrue(hit("src/**/x.py", {"src/x.py"}))
        self.assertTrue(hit("src/**/x.py", {"src/a/b/x.py"}))
        self.assertFalse(hit("vercel.json", {"notvercel.json"}))
        self.assertFalse(hit("src/*.ts", {"lib/src.ts"}))

    def test_phase_text_comes_from_where_cache(self):
        repo = self.repo(VAULT)
        self.data.mkdir(exist_ok=True)
        self.yahlib.where_cache_path(repo).write_text(json.dumps(
            {"phase": {"title": "P2 Payment form", "next": "Wire the Stripe webhook handler", "base": "main"}}),
            encoding="utf-8")
        # 1.5 x {stripe, webhook, payment} + 4 for src/payments/charge.ts
        self.assertEqual(self.scores(repo, "zzz")["stripe-webhooks"], 8.5)
        self.assertEqual(self.scores(repo, "zzz", "--phase", "")["stripe-webhooks"], 4)

    def test_one_hop_link_bonus(self):
        repo = self.repo(VAULT)
        query = ("vercel", "deploy", "preview")
        with_link = self.recall_json(repo, *query)
        slugs = [m["slug"] for m in with_link["matches"]] + with_link["also"]
        self.assertNotIn("e2e-seed-user", slugs)  # linked, but scores 0 on its own
        path = self.note_file(repo, "vercel-deploys-main")
        path.write_text(path.read_text("utf-8").replace("Links: [[preview-env-vars]], [[e2e-seed-user]].", ""),
                        encoding="utf-8")
        before = {m["slug"]: m["score"] for m in with_link["matches"]}
        after = self.scores(repo, *query)
        self.assertEqual(before["preview-env-vars"] - after["preview-env-vars"], 1)  # linked from the top note
        self.assertEqual(before["vercel-deploys-main"] - after["vercel-deploys-main"], 1)  # links to a top note

    def test_no_brain_dir_prints_nothing(self):
        repo = self.repo()
        for args in (("recall", "deploy"), ("recall", "--json", "deploy"), ("find", "deploy"), ("index",),
                     ("pending",), ("new", "--title", "T", "--tldr", "S", "--source", "PR #1")):
            with self.subTest(args=args):
                out, _, rc = self.bp(repo, *args)
                self.assertEqual((out, rc), ("", 0))
        self.assertFalse((repo / "docs").exists())
        self.assertEqual(self.bp(self.home, "recall", "deploy")[:3:2], ("", 0))  # outside git
        self.assertIsNone(self.brain.recall(str(repo), "deploy"))

    def test_output_fits_max_chars(self):
        repo = self.repo(widget_vault(20))
        (repo / "docs" / "brain" / "_pending.md").write_text("## 2026-09-23 A\n- tldr: a\n\n## 2026-09-23 B\n",
                                                            encoding="utf-8")
        full, _, _ = self.bp(repo, "recall", "widget")
        full = full.rstrip("\n")
        self.assertRegex(full, r"Also related: (widget-\d+, ){9}widget-\d+\n2 unreviewed notes in "
                               r"docs/brain/_pending\.md$")
        self.assertEqual(full.count("\n- widget-"), 5)
        cap = len(full) - 12  # the also list is trimmed first; every TL;DR stays
        out = self.bp(repo, "recall", "widget", "--max-chars", str(cap))[0].rstrip("\n")
        self.assertLessEqual(len(out), cap)
        self.assertEqual(out.count("pre-rendered widget cache"), 5)
        self.assertLess(out.count("widget-"), full.count("widget-"))
        for cap in (400, 250, 60):
            out = self.bp(repo, "recall", "widget", "--max-chars", str(cap))[0].rstrip("\n")
            self.assertLessEqual(len(out), cap)
            self.assertTrue(out.startswith("[yah] brain: 20 of 20 notes match"), out)
        self.assertNotIn("Also related", self.bp(repo, "recall", "widget", "--max-chars", "400")[0])

    def test_recall_speed_on_100_notes(self):
        repo = self.repo(widget_vault(100))
        best, best_cli = 99.0, 99.0
        for _ in range(3):
            t = time.perf_counter()
            block = self.brain.render(self.brain.recall(str(repo), "widget deploy cache"), 10000)
            best = min(best, time.perf_counter() - t)
            t = time.perf_counter()
            self.bp(repo, "recall", "widget", "deploy", "cache")
            best_cli = min(best_cli, time.perf_counter() - t)
        self.assertTrue(block.startswith("[yah] brain: 100 of 100 notes match"))
        sys.stderr.write("\nrecall on 100 notes: {:.0f} ms in-process, {:.0f} ms as a script (budget 300 ms)\n"
                         .format(best * 1000, best_cli * 1000))
        self.assertLess(best, 0.3)


def widget_vault(n):
    return {"widget-{:03d}".format(i): note(
        "Widget rule {}".format(i), "Widget {} serves the pre-rendered widget cache for region {}.".format(i, i % 7),
        ("widget",), ("src/widgets/w{}/**".format(i),) if i % 2 else (), "PR #{}".format(i),
        body="Links: [[widget-{:03d}]].".format((i + 1) % n)) for i in range(n)}


class WriteTests(Base):
    def test_new_without_source_goes_to_pending(self):
        repo = self.repo(VAULT)
        out, err, rc = self.bp(repo, "new", "--title", "Cache keys include the locale",
                               "--tldr", "Every cache key carries the locale.", "--tags", "cache,i18n")
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith("pending: Cache keys include the locale"), out)
        self.assertFalse(self.note_file(repo, "cache-keys-include-the-locale").exists())
        pending = (repo / "docs" / "brain" / "_pending.md").read_text("utf-8")
        self.assertRegex(pending, r"^## \d{4}-\d\d-\d\d Cache keys include the locale\n- tldr: Every cache key")
        self.assertIn("- tags: cache, i18n", pending)
        self.assertIn("1 pending in docs/brain/_pending.md", self.bp(repo, "pending")[0])
        out = self.bp(repo, "recall", "vercel")[0]
        self.assertTrue(out.rstrip().endswith("1 unreviewed note in docs/brain/_pending.md"), out)

    def test_new_on_existing_slug_exits_1(self):
        repo = self.repo(VAULT)
        before = self.note_file(repo, "log-format").read_bytes()
        out, err, rc = self.bp(repo, "new", "--title", "Log format!", "--tldr", "x", "--source", "PR #50")
        self.assertEqual(rc, 1)
        self.assertIn("exists: log-format; update it or pass --supersedes", err)
        self.assertEqual(self.note_file(repo, "log-format").read_bytes(), before)

    def test_supersedes_rejects_paths_and_reserved_names(self):
        repo = self.repo(VAULT)
        for bad in ("../../README", "INDEX", "_pending"):
            out, err, rc = self.bp(repo, "new", "--title", "Some new fact " + bad, "--tldr", "x",
                                   "--source", "PR #70", "--supersedes", bad)
            self.assertEqual(rc, 1, bad)
        self.assertEqual(self.brain.slugify("Index"), "index-note")

    def test_supersedes_marks_the_old_note(self):
        repo = self.repo(VAULT)
        out, err, rc = self.bp(repo, "new", "--title", "Deploys go through the release bot",
                               "--tldr", "The release bot promotes main to production.", "--tags", "deploy,vercel",
                               "--paths", "vercel.json", "--source", "PR #60", "--links", "preview-env-vars",
                               "--supersedes", "vercel-deploys-main")
        self.assertEqual(rc, 0, err)
        self.assertIn("wrote docs/brain/deploys-go-through-the-release-bot.md", out)
        new = self.brain.read_note(self.note_file(repo, "deploys-go-through-the-release-bot"))
        self.assertEqual((new["tags"], new["paths"], new["source"], new["links"], new["status"]),
                         (["deploy", "vercel"], ["vercel.json"], "PR #60", ["preview-env-vars"], "active"))
        self.assertEqual(new["tldr"], "The release bot promotes main to production.")
        old = self.brain.read_note(self.note_file(repo, "vercel-deploys-main"))
        self.assertEqual((old["status"], old["superseded_by"]), ("superseded", "deploys-go-through-the-release-bot"))
        self.assertEqual((old["title"], old["source"]), ("Vercel deploys only from main", "PR #41, 2026-09-20"))
        slugs = [m["slug"] for m in self.recall_json(repo, "vercel", "deploy")["matches"]]
        self.assertEqual(slugs[0], "deploys-go-through-the-release-bot")
        self.assertNotIn("vercel-deploys-main", slugs)
        index = (repo / "docs" / "brain" / "INDEX.md").read_text("utf-8")
        superseded = index.split("## Superseded")[1]
        self.assertIn("- [[vercel-deploys-main]] Vercel deploys only from main (superseded by "
                      "[[deploys-go-through-the-release-bot]])", superseded)

    def test_index_is_deterministic(self):
        repo = self.repo(dict(VAULT, **{"orphan": note("Orphan", "Links to [[nowhere]].", source="")}))
        index = repo / "docs" / "brain" / "INDEX.md"
        out, err, rc = self.bp(repo, "index")
        self.assertEqual((rc, out), (0, "wrote docs/brain/INDEX.md (9 notes)\n"))
        self.assertIn("orphan: no source", err)
        self.assertIn("orphan: broken link [[nowhere]]", err)
        first = index.read_bytes()
        index.unlink()
        self.bp(repo, "index")
        self.assertEqual(index.read_bytes(), first)
        self.assertNotIn(b"\r", first)
        entries = [ln for ln in first.decode("utf-8").split("## Superseded")[0].splitlines() if ln.startswith("- ")]
        self.assertEqual(entries, sorted(entries))
        self.assertIn("- [[api-rate-limit]] Partner API rate-limits at 10 rps: Batch writes; the partner API rejects "
                      "bursts over 10 requests a second. (docs/partner.md:12)", entries)
        self.assertEqual(len(entries), 8)

    def test_init_is_idempotent(self):
        repo = self.repo()
        out, err, rc = self.bp(repo, "init")
        self.assertEqual((rc, out), (0, "created docs/brain with README.md, _pending.md and INDEX.md\n"), err)
        brain = repo / "docs" / "brain"

        def snapshot():
            return {p.name: p.read_bytes() for p in sorted(brain.iterdir())}

        first = snapshot()
        self.assertEqual(sorted(first), ["INDEX.md", "README.md", "_pending.md"])
        self.assertEqual(first["_pending.md"], b"")
        self.assertLessEqual(len(first["README.md"].decode("utf-8").splitlines()), 15)
        out, _, rc = self.bp(repo, "init")
        self.assertEqual((rc, out), (0, "docs/brain already exists; INDEX.md is up to date\n"))
        self.assertEqual(snapshot(), first)
        self.assertEqual(self.bp(repo, "recall", "anything")[0], "")  # an empty brain matches nothing
        self.assertEqual(self.bp(self.home, "init")[:3:2], ("", 0))  # outside git: nothing created

    def test_frontmatter_subset(self):
        meta = self.brain.parse_front(['title: A: b', "tags: [a, 'b', \"c\"]", "paths: []", "status: superseded",
                                       "no colon here", "source: \"PR #1\""])
        self.assertEqual(meta, {"title": "A: b", "tags": ["a", "b", "c"], "paths": [], "status": "superseded",
                                "source": "PR #1"})
        self.assertEqual(self.brain.slugify("Vercel: deploys ONLY from `main`!"), "vercel-deploys-only-from-main")
        self.assertEqual(len(self.brain.slugify("x" * 80)), 60)


class HookTests(Base):
    def hook(self, sid, prompt, cwd):
        out, err, rc = self.py("recall_hook.py", stdin=json.dumps({"session_id": sid, "prompt": prompt,
                                                                    "cwd": str(cwd)}))
        self.assertEqual((rc, err), (0, ""))
        return out

    def test_output_shape_and_once_per_session(self):
        repo = self.repo(VAULT)
        out = json.loads(self.hook("s1", "how do we deploy to vercel?", repo))
        self.assertEqual(set(out), {"systemMessage", "hookSpecificOutput"})
        self.assertRegex(out["systemMessage"], r"^\[yah\] brain: [1-5] notes? recalled$")
        hso = out["hookSpecificOutput"]
        self.assertEqual(set(hso), {"hookEventName", "additionalContext"})
        self.assertEqual(hso["hookEventName"], "UserPromptSubmit")
        self.assertTrue(hso["additionalContext"].startswith("[yah] brain: "))
        self.assertIn("\n- vercel-deploys-main: ", hso["additionalContext"])
        self.assertTrue((self.data / "recall-s1.flag").exists())
        self.assertEqual(self.hook("s1", "how do we deploy to vercel?", repo), "")
        self.assertTrue(self.hook("s2", "how do we deploy to vercel?", repo))

    def test_slash_prompts(self):
        repo = self.repo(VAULT)
        for skill in ("/yah:start deploy to vercel", "/yah:resume P2 build"):  # they recall themselves
            with self.subTest(skill):
                sid = skill.split()[0][5:]
                self.assertEqual(self.hook(sid, skill, repo), "")
                self.assertTrue((self.data / "recall-{}.flag".format(sid)).exists())
                self.assertEqual(self.hook(sid, "deploy to vercel", repo), "")  # recall is for the first prompt only
        self.assertEqual(self.hook("s3", "/clear", repo), "")  # other commands leave recall for the first prompt
        self.assertEqual(self.hook("s3", "/model opus", repo), "")
        self.assertFalse((self.data / "recall-s3.flag").exists())
        self.assertIn("vercel-deploys-main", self.hook("s3", "deploy to vercel", repo))

    def test_no_brain_and_old_flags(self):
        repo = self.repo()
        self.data.mkdir()
        old = self.data / "recall-old.flag"
        old.write_bytes(b"")
        os.utime(old, (time.time() - 4 * 86400,) * 2)
        self.assertEqual(self.hook("s1", "deploy to vercel", repo), "")
        self.assertFalse(old.exists())
        self.assertEqual(self.hook("s2", "deploy", self.home), "")


if __name__ == "__main__":
    unittest.main()
