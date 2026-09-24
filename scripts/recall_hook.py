"""recall_hook.py: UserPromptSubmit hook. On a session's first prompt it injects the project
brain notes that match the prompt (brain.py recall, run in-process). Never blocks.

  - once per session: recall-<session>.flag in the data dir marks that recall ran
  - /yah:start and /yah:resume recall themselves: they use up the session's recall and inject nothing
  - any other prompt starting with "/" injects nothing and leaves recall for the first real prompt
  - no brain folder in the repo: nothing, and no git calls
  - recall-*.flag files older than 3 days are deleted
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yahlib import config, data_dir, utf8_stdout  # noqa: E402

MAX_AGE = 3 * 86400
OWN_RECALL = ("/yah:start", "/yah:resume")


def first_prompt(sid):
    """True the first time a session is seen: write its flag and sweep old flags."""
    d = data_dir()
    flag = d / "recall-{}.flag".format(re.sub(r"[^\w.-]", "_", sid))
    if flag.exists():
        return False
    d.mkdir(parents=True, exist_ok=True)
    flag.write_bytes(b"")
    cutoff = time.time() - MAX_AGE
    for f in d.glob("recall-*.flag"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass
    return True


def main():
    utf8_stdout()
    try:
        d = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return
    prompt = str(d.get("prompt") or "").strip()
    slash = prompt.split()[0] if prompt.startswith("/") else ""
    if slash and slash not in OWN_RECALL:
        return
    if not first_prompt(str(d.get("session_id") or "nosession")) or slash:
        return
    import brain
    r = brain.recall(d.get("cwd") or os.getcwd(), prompt)
    block = brain.render(r, int(config()["recall_max_chars"]))
    if block:
        n = len(r["matches"])
        print(json.dumps({
            "systemMessage": "[yah] brain: {} note{} recalled".format(n, "" if n == 1 else "s"),
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": block},
        }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # recall must never block a prompt
