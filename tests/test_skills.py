"""Tests for the skill and agent files. Stdlib unittest only:

    python -m unittest tests.test_skills

Every skill and agent description is in the model's context on every turn, so these tests keep
the listings short and the start, wrap and resume bodies to the rules the real-run test found.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = sorted(ROOT.glob("skills/*/SKILL.md"))
AGENTS = sorted(ROOT.glob("agents/*.md"))
MAX_DESC = 100
MAX_VISIBLE = 600
START_BYTES = 2394  # start/SKILL.md before the restate-first rewrite


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
                         {"deep", "phases", "resume", "setup", "start", "where", "wrap"})
        self.assertEqual({p.stem for p in AGENTS}, {"deep", "scout"})

    def test_each_description_short(self):
        for f in SKILLS + AGENTS:
            with self.subTest(f=str(f.relative_to(ROOT))):
                desc = parse(f)[0].get("description", "")
                self.assertTrue(desc)
                self.assertLessEqual(len(desc), MAX_DESC)

    def test_model_visible_total(self):
        visible = [f for f in SKILLS + AGENTS if model_visible(f)]
        self.assertEqual(len(visible), 7)  # 5 skills + 2 agents; resume and setup are user-only
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

    def test_no_bd_lookup_when_text_is_given(self):
        self.assertIn("never run `bd show` or `which bd`", self.body)
        self.assertIn("where.py --json", self.body)

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

    def test_phases_stamps_next(self):
        text = body("phases")
        self.assertIn('"next_sha":"<HEAD>"', text)
        self.assertIn("- At: <git rev-parse --short HEAD>", text)


class BeadsTests(unittest.TestCase):
    def test_wrap_and_resume_stay_in_this_repo(self):
        for name in ("wrap", "resume"):
            with self.subTest(skill=name):
                text = body(name)
                self.assertIn("no beads DB here", text)
                self.assertIn("`beads` is non-null", text)
                self.assertIn("Never set, export or follow `BEADS_DIR`", text)
                self.assertIn("never run bd against a database outside this repo", text)
                self.assertNotRegex(text, r"(export|set)\s+BEADS_DIR\s*=")


if __name__ == "__main__":
    unittest.main()
