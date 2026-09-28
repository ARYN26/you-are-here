"""phasemap.py: the phase map, what a phase or plan does to the repo.

    plan_sections(path)            the plan file's `### P<n>` sections: title, branch, base, after line, files
    actual_changes(base, branch)   what the branch changed since it left its base, as the same file entries

A plan section may carry an `After it merges: <plain words>` line and a `Files:` block, one line per path:
`+ path — note` (new), `~ path — note` (changed), `- path — note` (removed), `> old -> new — note` (renamed).
Both are optional; without a `Files:` block the paths come from bolded bullets (**`path`**, (`path`)).
Nothing here raises on a bad plan: a headless step must not die on it. Stdlib only.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yahlib import run  # noqa: E402

HEAD = re.compile(r"#{1,3} ")
PHASE = re.compile(r"### +(P\d+)\b[ \t:.-]*(.*?)\s*$")
BRANCH = re.compile(r"\bbranch\s+`?([^`|·\s]+)`?")
BASE = re.compile(r"\bbase\s+`?([^`|·\s]+)`?")
AFTER = re.compile(r"\s*(?:\*\*)?After it merges:(?:\*\*)?\s*(.*?)\s*$")
FILES = re.compile(r"\s*(?:\*\*)?Files:(?:\*\*)?\s*$")
ENTRY = re.compile(r"\s*([+~>-])\s+(\S.*?)\s*$")
NOTE = re.compile(r"\s+(?:—|--|-)\s+|\s*—\s*")  # the em dash, or `--`/` - ` in a plan typed in ASCII
BULLET = re.compile(r"\s*[-*]\s+(.*)$")
STATUS = {"A": "+", "C": "+", "M": "~", "T": "~", "D": "-", "R": ">"}  # git's --name-status letters as marks
BOLD = re.compile(r"\*\*`([^`]+)`\*\*")
PAREN = re.compile(r"\(([^()]*)\)")
WORD = re.compile(r"`([^`]+)`|([^\s,;`]+)")  # a quoted token, or a bare one
LINES = re.compile(r":\d+(?:-\d+)?$")  # where.py:715-760


def is_fence(ln):
    return ln.lstrip().startswith(("```", "~~~"))


def pathlike(tok, bare=False):
    """A token that names a file or directory: a `/` or a file extension, no spaces, calls or `:`.
    A bare (unquoted) token needs both, so plain words in parentheses never count."""
    tok = LINES.sub("", tok.strip().strip("`").rstrip(".,;"))
    if not tok or tok.startswith(("-", "/")) or not re.fullmatch(r"[\w.\-/]+", tok):
        return ""
    slash, ext = "/" in tok, bool(re.search(r"\.[A-Za-z]\w{0,5}$", tok))
    return tok if ((slash and ext) if bare else (slash or ext)) else ""


def entry(mark, rest):
    """One `Files:` line as {mark, path, old, note}; a rename splits `old -> new`."""
    parts = NOTE.split(rest, 1)
    target, note = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
    old = ""
    if mark == ">" and "->" in target:
        old, target = (s.strip().strip("`") for s in target.split("->", 1))
    return {"mark": mark, "path": target.strip("`"), "old": old, "note": note}


def bullet_paths(lines):
    """Paths named in bullets as **`path`** or inside parentheses, for a plan without a `Files:` block.
    The mark is "" because the plan does not say whether the file is new."""
    seen, out = set(), []
    for ln in lines:
        bm = BULLET.match(ln)
        if not bm:
            continue
        found = [pathlike(t) for t in BOLD.findall(bm.group(1))]
        for group in PAREN.findall(bm.group(1)):
            found += [pathlike(t) if t else pathlike(b, bare=True) for t, b in WORD.findall(group)]
        for p in found:
            if p and p not in seen:
                seen.add(p)
                out.append({"mark": "", "path": p, "old": "", "note": ""})
    return out


def section(label, title, body):
    """One phase from its heading and the lines under it."""
    ph = {"label": label, "title": title, "branch": "", "base": "", "after": "", "files": [], "planned": False}
    first = next((ln for ln in body if ln.strip()), "")
    if not BULLET.match(first) and not FILES.match(first):
        bm, sm = BRANCH.search(first), BASE.search(first)
        ph["branch"], ph["base"] = (bm.group(1) if bm else ""), (sm.group(1) if sm else "")
    prose, fence, block = [], False, None  # block: None before `Files:`, then True while its lines run
    for ln in body:
        if is_fence(ln):
            fence = not fence
            continue
        if block:
            em = ENTRY.match(ln)
            e = entry(em.group(1), em.group(2)) if em else None
            if e and e["path"] and not re.search(r"\s", e["path"]):  # `- **Tests** ...` is a bullet, not a removal
                ph["files"].append(e)
                continue
            if not ln.strip() and (fence or not ph["files"]):
                continue
            block = False
        if fence:
            continue
        am = AFTER.match(ln)
        if am and not ph["after"]:
            ph["after"] = am.group(1)
        elif FILES.match(ln) and block is None:
            block, ph["planned"] = True, True
        else:
            prose.append(ln)
    if not ph["planned"]:
        ph["files"] = bullet_paths(prose)
    return ph


def plan_sections(path):
    """The plan's phases in order, each {label, title, branch, base, after, files, planned}. `planned` is True
    when the section has a `Files:` block. A missing or unreadable plan gives []; a `### P<n>` in a fence is not
    a phase."""
    try:
        lines = Path(path).read_text("utf-8", errors="replace").splitlines() if path else []
    except (OSError, ValueError):
        return []
    out, cur, body, fence = [], None, [], False
    try:
        for ln in lines:
            if is_fence(ln):
                fence = not fence
            elif not fence and HEAD.match(ln):
                if cur:
                    out.append(section(cur[0], cur[1], body))
                pm = PHASE.match(ln)
                cur, body = ((pm.group(1), pm.group(2)) if pm else None), []
                continue
            if cur:
                body.append(ln)
        if cur:
            out.append(section(cur[0], cur[1], body))
    except Exception:  # noqa: BLE001 - a bad plan gives what parsed so far, never a traceback
        pass
    return out


def actual_changes(base, branch, cwd=None):
    """What `branch` changed since it left `base`, from `git diff -M --name-status base...branch`, as
    {mark, path, old, note} entries with an empty note. Always diff a phase against its own base, so a stacked
    phase shows only its own files. No commits, a missing branch or no git gives []."""
    if not base or not branch:
        return []
    out = run(["git", "diff", "-M", "--name-status", "-z", "--no-color", f"{base}...{branch}", "--"], cwd, timeout=15)
    fields, entries = iter((out or "").split("\0")), []  # -z: status, path (R/C: status, old, new), NUL-separated
    for status in fields:
        mark = STATUS.get(status[:1])
        if not mark:
            continue
        old = next(fields, "") if status[:1] in "RC" else ""
        path = next(fields, "")
        if path:
            entries.append({"mark": mark, "path": path, "old": old if mark == ">" else "", "note": ""})
    return entries
