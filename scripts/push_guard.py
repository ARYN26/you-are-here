"""push_guard.py: PreToolUse hook for Bash and PowerShell, active only inside `yah run` iterations. run.py adds it
through --settings and sets YAH_PROTECTED to the protected branches (trunks, PROD, main, master).

It denies, in any simple command of the Bash line (split on && || ; | & and newlines outside quotes,
after a leading `cd DIR`, `git [-C DIR] [-c k=v] [--git-dir=..] [--work-tree=..] push`):
  - a force push: -f, --force, --force-with-lease, --force-if-includes, a short cluster with f, a +refspec
  - --mirror, --all, --delete, -d, --prune, and :branch deletes
  - a refspec whose destination is protected
  - no refspec, or HEAD, while the repo's current branch is protected
  - gh pr merge, gh api with "merge" in its args, gh repo delete
A git push it cannot parse is denied. Anything else, or any error, exits 0 silently.
"""
import json
import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yahlib import run, utf8_stdout  # noqa: E402

PUNCT = "();<>|&\n`"
WRAPPERS = {"env", "command", "builtin", "exec", "nohup", "time", "sudo", "xargs", "{", "!", "if", "then", "else",
            "elif", "do", "while", "until"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "pwsh", "powershell"}
GIT_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
PUSH_ARG = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
SHELL_TOOLS = {"Bash", "PowerShell"}  # PowerShell splits on ; | & too, and `& git push` is a separator + git
DANGER = ("force", "force-with-lease", "force-if-includes", "mirror", "all", "delete", "prune")


def split(cmd):
    """Simple commands as token lists. Redirections are dropped. Raises ValueError on bad quoting."""
    lex = shlex.shlex(re.sub(r"\\\r?\n", " ", cmd), posix=True, punctuation_chars=PUNCT)
    lex.whitespace, lex.commenters, lex.whitespace_split = " \t\r", "", True
    segs, cur, skip = [], [], False
    for tok in lex:
        if skip:
            skip = False
        elif tok and set(tok) <= set(PUNCT):
            if "<" in tok or ">" in tok:  # a redirection: drop its fd and its target
                if cur and cur[-1].isdigit():
                    cur.pop()
                skip = True
            else:
                segs.append(cur)
                cur = []
        else:
            cur.append(tok)
    return [s for s in segs + [cur] if s]


def segments(cmd):
    """split(cmd); when the whole line will not parse, each naive piece, as a raw string if it fails too."""
    try:
        return split(cmd)
    except ValueError:
        out = []
        for piece in re.split(r"&&|\|\||[;|&\n]", cmd):
            try:
                out += split(piece)
            except ValueError:
                out.append(piece)
        return out


def to_path(base, d):
    d = os.path.expanduser(d)
    if os.name == "nt":  # Git Bash spells C:\ as /c/
        d = re.sub(r"^/([a-zA-Z])(?=/|$)", r"\1:", d)
    return os.path.join(base, d)


def push_problem(args, branch, protected):
    """What is wrong with `git push ARGS`, or None. branch() reads the repo's current branch."""
    pos, it, dash = [], iter(args), False
    for tok in it:
        if dash or not tok.startswith("-") or tok == "-":
            pos.append(tok)
        elif tok == "--":
            dash = True
        elif tok.startswith("--"):
            name = tok[2:].split("=", 1)[0]
            hit = next((o for o in DANGER if name and o.startswith(name)), None)  # git accepts prefixes
            if hit:
                return "a force push (--{})".format(hit) if hit.startswith("force") else "git push --" + hit
            if name in {o[2:] for o in PUSH_ARG if o.startswith("--")} and "=" not in tok:
                next(it, None)
        else:
            for i, ch in enumerate(tok[1:]):
                if ch in "fd":
                    return "a force push ({})".format(tok) if ch == "f" else "a branch delete ({})".format(tok)
                if ch == "o":
                    if i == len(tok) - 2:
                        next(it, None)
                    break
    head = len(pos) < 2
    for ref in pos[1:]:
        if ref.startswith("+"):
            return "a force push (+refspec {})".format(ref)
        src, colon, dst = ref.partition(":")
        if colon and not src:
            return "a delete or matching push (refspec '{}')".format(ref)
        dst = dst if colon else src
        dst = dst[len("refs/heads/"):] if dst.startswith("refs/heads/") else dst
        if "*" in dst:
            return "a wildcard push ({})".format(ref)
        if dst in ("HEAD", "@") or "$" in dst or "`" in dst:
            head = True
        elif dst in protected:
            return "a push to {}".format(dst)
    b = branch() if head else None
    return "a push from {}, which is protected".format(b) if b in protected else None


def git_push(args, cur):
    """(repo dir, git options for rev-parse, push args) when `git ARGS` is a push, else None."""
    opts, i = [], 0
    while i < len(args):
        a = args[i]
        if a in GIT_ARG:
            v = args[i + 1] if i + 1 < len(args) else ""
            if a == "-C":
                cur = to_path(cur, v)
            elif a in ("--git-dir", "--work-tree"):
                opts.append("{}={}".format(a, to_path(cur, v)))
            i += 2
            continue
        if a.startswith(("--git-dir=", "--work-tree=")):
            k, v = a.split("=", 1)
            opts.append("{}={}".format(k, to_path(cur, v)))
        elif not a.startswith("-"):
            return (cur, opts, args[i + 1:]) if a == "push" else None
        i += 1
    return None


def problem(cmd, cwd, protected):
    """What in the Bash line must be denied, or None."""
    for seg in segments(cmd):
        if isinstance(seg, str):
            if "git" in seg and "push" in seg:
                return "a git push the guard could not parse"
            continue
        while seg and (seg[0] in WRAPPERS or re.match(r"^\w+=", seg[0])):
            seg = seg[1:]
        if not seg:
            continue
        name, args = re.sub(r"\.exe$", "", os.path.basename(seg[0]).lower()), seg[1:]
        why = None
        if name == "cd":  # later commands run there; `cd -` is not followed
            arg = next((a for a in args if a[:1] != "-"), None if args else "~")
            cwd = to_path(cwd, arg) if arg else cwd
        elif name in SHELLS:  # bash -c "...", pwsh -Command "...": check the script text
            why = problem(" ".join(a for a in args if not a.startswith("-")), cwd, protected)
        elif name == "eval":
            why = problem(" ".join(args), cwd, protected)
        elif name == "git":
            push = git_push(args, cwd)
            if push:
                d, opts, rest = push
                why = push_problem(rest, lambda: (run(["git", "-C", d, *opts, "rev-parse", "--abbrev-ref", "HEAD"],
                                                      None, timeout=3) or "").strip(), protected)
        elif name == "gh":
            pairs = set(zip(args, args[1:]))
            if ("pr", "merge") in pairs or ("repo", "delete") in pairs:
                why = "gh pr merge" if ("pr", "merge") in pairs else "gh repo delete"
            elif args[:1] == ["api"] and any("merge" in a.lower() for a in args):
                why = "a merge through gh api"
        if why:
            return why
    return None


def main():
    utf8_stdout()
    protected = {b.strip() for b in os.environ.get("YAH_PROTECTED", "").split(",") if b.strip()}
    if not protected:
        return  # not inside `yah run`
    d = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    cmd = (d.get("tool_input") or {}).get("command") if d.get("tool_name", "Bash") in SHELL_TOOLS else None
    why = problem(cmd, d.get("cwd") or os.getcwd(), protected) if isinstance(cmd, str) else None
    if why:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "[yah] blocked {}. yah run pushes only feature branches, with "
            "`git push -u origin <branch>`, and never force-pushes, deletes or merges ({} are protected). "
            "The merge is the user's.".format(why, ", ".join(sorted(protected)))}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # only an unparseable git push fails closed; any other error allows
