"""codex.py: hand web research or a read-only code review to GPT through OpenAI's codex CLI.

    codex.py research BRIEF              multi-source web research with cited URLs
    codex.py review REPO BRIEF_FILE      read-only review of REPO; BRIEF_FILE says what to look for
    Both take --timeout SECONDS (default: config codex_timeout_minutes, 15). A BRIEF or BRIEF_FILE of -
    reads the brief from stdin, so a skill can pass it through a quoted heredoc.

Refuses unless config.json has "codex": true (setup.py --codex). codex is $YAH_CODEX, else codex on PATH;
a YAH_CODEX that ends in .py runs with this Python (the tests' fake). Every call is `codex exec` in a
read-only sandbox, ephemeral, with the prompt on stdin (a long brief would pass Windows' command-line cap)
and the answer read from the -o file. Research runs in the report folder, never in a repo, with live web
search. The full report goes to <data dir>/codex/<stamp>-<mode>.md (the newest 20 are kept); stdout is
its path, then the answer: the first 1,500 characters for research, all of it for review. A usage limit
is one line on stderr and exit 3; any other failure is one line and exit 1. Stdlib only.
"""
import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yahlib import NO_WINDOW, config, data_dir, first_line, utf8_stdout  # noqa: E402

KEEP = 20
SUMMARY_CHARS = 1500
# Only read on a failed exec, and only as whole words, so "generate" or "accurate" is not a rate.
LIMIT = re.compile(r"\b(?:usage limit|rate[ _-]?limit(?:ed)?|quota|429|too many requests)\b", re.I)
SANDBOX = re.compile(r"\bsandbox", re.I)
SIGNED_OUT = re.compile(r"\b(?:not logged in|codex login|log ?in again|sign(?:ed)? in|401|unauthori[sz]ed)\b", re.I)
ERROR = re.compile(r"\berror\b", re.I)
# codex exec streams its banner (which names the sandbox), the echoed prompt and the model's own reads to
# stderr; its error comes last. Only these last lines are read, and of them only the error lines when there
# are any, so a brief or a file about rate limits or sign-in is never taken for a usage limit or a sign-out.
TAIL = 3
RESEARCH = """Research this question on the web. Use several independent sources and read the pages you cite.

QUESTION:
{brief}

Reply in plain markdown with exactly these sections:
ANSWER: at most 10 lines; tie each claim to a source number like [2].
SOURCES: numbered, one URL per line, with a few words on what it says.
UNSURE: what the sources disagree on or leave open, or "none".
Do not run commands that change anything and do not edit files."""
REVIEW = """{brief}

You are in the repository under review, in a read-only sandbox. Read any file you need; do not edit files
or run commands that change anything."""


class Fail(Exception):
    """One line for stderr; main exits 1."""
    code = 1


class Limit(Fail):
    """codex hit its usage limit; main exits 3 so a caller can fall back."""
    code = 3


def command(path):
    return [sys.executable, str(path)] if str(path).lower().endswith(".py") else [str(path)]


def find_codex():
    if os.environ.get("YAH_CODEX"):
        return os.environ["YAH_CODEX"]
    found = shutil.which("codex")
    if found:
        return found
    raise Fail("codex not found: install it with `npm i -g @openai/codex`, or set YAH_CODEX.")


def kill_tree(p):
    """codex from npm is a node shim over the native binary: kill the whole tree, not just the shim."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, creationflags=NO_WINDOW)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)  # call() starts it as its own group's leader
        except OSError:
            pass
    try:
        p.kill()
    except OSError:
        pass
    try:
        p.wait(5)
    except subprocess.TimeoutExpired:
        pass


def call(cmd, cwd, timeout, stdin=""):
    """(rc, stdout, stderr), or None on a timeout. stdin and the output go through temp files, not pipes,
    so a grandchild that keeps a pipe open cannot hang the wait."""
    with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        inp.write(stdin.encode("utf-8"))
        inp.seek(0)
        try:
            p = subprocess.Popen(cmd, cwd=str(cwd), stdin=inp, stdout=out, stderr=err, creationflags=NO_WINDOW,
                                 start_new_session=os.name != "nt")
        except OSError as e:
            raise Fail(f"could not start codex ({cmd[0]}): {e}") from None
        try:
            rc = p.wait(timeout)
        except subprocess.TimeoutExpired:
            kill_tree(p)
            return None
        except BaseException:  # Ctrl-C: its own group gets no signal from the terminal
            kill_tree(p)
            raise
        out.seek(0)
        err.seek(0)
        return rc, out.read().decode("utf-8", "replace"), err.read().decode("utf-8", "replace")


def timeout_seconds(flag, minutes):
    """--timeout SECONDS if given, else config codex_timeout_minutes; at least 1s."""
    return max(1, int(flag if flag is not None else minutes * 60))


def tail(text):
    """The last TAIL non-empty lines of text."""
    return [ln for ln in text.splitlines() if ln.strip()][-TAIL:]


def limit_line(text):
    """The first output line that names the limit, for the one-line error."""
    return next((ln for ln in text.splitlines() if LIMIT.search(ln)), "")


def ask(mode, prompt, cwd, timeout, repo=None):
    """codex's final answer text; with no repo it runs outside git (research, setup's smoke call).
    Raises Limit on a usage limit, Fail on a timeout, an error or no answer."""
    cfg = config()
    cmd = command(find_codex()) + ["exec", "--ephemeral", "-s", "read-only", "-m", cfg["codex_model"],
                                   "-c", f"model_reasoning_effort={cfg['codex_effort']}"]
    cmd += ["--skip-git-repo-check"] if repo is None else ["-C", str(repo)]
    if mode == "research":
        cmd += ["-c", "web_search=live"]
    scratch = Path(tempfile.mkdtemp(prefix="yah-codex-"))
    answer = scratch / "answer.md"
    try:
        got = call(cmd + ["-o", str(answer), "-"], cwd, timeout, prompt)
        text = answer.read_text("utf-8", "replace").strip() if answer.is_file() else ""
    finally:
        shutil.rmtree(scratch, True)
    if got is None:
        raise Fail(f"codex timed out after {timeout}s; try a narrower brief or a larger --timeout.")
    rc, out, err = got
    lines = tail(err.replace(prompt, "")) + tail(out)
    errors = [ln for ln in lines if ERROR.search(ln)]  # codex's own ERROR: line, when it printed one
    both = "\n".join(errors or lines)
    said = first_line(errors[-1] if errors else both) or "no output"
    if rc != 0:
        if LIMIT.search(both):
            raise Limit(f"codex hit its usage limit (exit {rc}): {first_line(limit_line(both)) or said}")
        if SIGNED_OUT.search(both):
            raise Fail(f"codex is not signed in (exit {rc}): run `codex login`, then try again.")
        if SANDBOX.search(both) and os.name == "nt":
            raise Fail(f"codex's sandbox failed (exit {rc}): put `[windows]` and `sandbox = \"unelevated\"` "
                       "in ~/.codex/config.toml, then try again.")
        raise Fail(f"codex failed (exit {rc}): {said}")
    if not text:
        raise Fail(f"codex gave no answer (exit 0): {said}")
    return text


def prune(folder):
    """Keep the newest KEEP reports. Names sort by time; a file another run already removed is fine."""
    for old in sorted(folder.glob("*.md"))[:-KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


def main():
    utf8_stdout()
    ap = argparse.ArgumentParser(prog="codex.py", description="Web research or a read-only review through GPT.")
    sub = ap.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("research", help="multi-source web research, cited URLs back")
    p.add_argument("brief", help="the question, or - to read it from stdin")
    p.add_argument("--timeout", type=int)
    p = sub.add_parser("review", help="read-only review of a repo, led by a brief file")
    p.add_argument("repo")
    p.add_argument("brief_file", help="the brief's file, or - to read it from stdin")
    p.add_argument("--timeout", type=int)
    a = ap.parse_args()
    try:
        return run(a)
    except Fail as e:
        print(f"codex: {e}", file=sys.stderr)
        return e.code


def stdin_text():
    """The brief from stdin as UTF-8 (a Windows pipe would otherwise decode it as cp1252)."""
    return sys.stdin.buffer.read().decode("utf-8-sig", "replace")


def run(a):
    cfg = config()
    if not cfg["codex"]:
        raise Fail("yah:gpt is off; turn it on with /yah:setup or setup.py --codex.")
    repo = None
    if a.mode == "review":
        repo = Path(a.repo).expanduser().resolve()
        if not repo.is_dir():
            raise Fail(f"no repo folder at {a.repo}.")
        if a.brief_file == "-":
            brief = stdin_text()
        else:
            brief_file = Path(a.brief_file).expanduser()
            if not brief_file.is_file():
                raise Fail(f"no brief file at {a.brief_file}.")
            brief = brief_file.read_text("utf-8", "replace")
    else:
        brief = stdin_text() if a.brief == "-" else a.brief
    brief = brief.strip()
    if not brief:
        raise Fail("the brief is empty; pass a self-contained question or review brief.")
    timeout = timeout_seconds(a.timeout, cfg["codex_timeout_minutes"])
    folder = data_dir() / "codex"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    started = time.time()
    if a.mode == "research":
        text = ask("research", RESEARCH.format(brief=brief), folder, timeout)
        head = ["Brief: " + brief]
    else:
        text = ask("review", REVIEW.format(brief=brief), repo, timeout, repo)
        head = [f"Repo: {repo}"] + ([] if a.brief_file == "-" else
                                    [f"Brief file: {Path(a.brief_file).expanduser().resolve()}"])
    report = folder / f"{stamp}-{a.mode}.md"
    head = [f"# yah:gpt {a.mode}, {time.strftime('%Y-%m-%d %H:%M')}", "",
            f"Model {cfg['codex_model']} at {cfg['codex_effort']} effort, {round(time.time() - started)}s.", ""] + head
    report.write_text("\n".join(head) + "\n\n---\n\n" + text + "\n", encoding="utf-8")
    prune(folder)
    if a.mode == "research" and len(text) > SUMMARY_CHARS:
        text = text[:SUMMARY_CHARS].rstrip() + " ... (the rest is in the report)"
    print(f"REPORT {report}\n\n{text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
