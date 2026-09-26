---
title: Skill allowed-tools patterns must allow for the quoted script path
tags: [skills, permissions, allowed-tools]
paths: ["skills/*/SKILL.md", "tests/test_skills.py"]
status: active
source: skills/auto/SKILL.md:6
---
Skills run PY "${CLAUDE_SKILL_DIR}/../../scripts/x.py" --flag, so the path ends in a quote: write Bash(python *scripts/x.py* --flag), never *scripts/x.py --flag*, which cannot match.
