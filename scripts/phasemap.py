"""phasemap.py: the phase map, what a phase or plan does to the repo.

    plan_sections(path)            the plan file's `### P<n>` sections: title, branch, base, after line, files
    actual_changes(base, branch)   what the branch changed since it left its base, as the same file entries
    render_tree(entries, files)    the file tree those entries make of the repo, folded to fit about 60 lines
    render_stack(phases)           the phases stacked on their bases, with status, PR and after line, then merge order

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
BRANCH = re.compile(r"(?i)\bbranch[:*]*\s+`?([^`|·\s]+)`?")  # `branch x`, `Branch: x`, `**Branch:** x`
BASE = re.compile(r"(?i)\bbase[:*]*\s+`?([^`|·\s]+)`?")
DONE = re.compile(r"\s*(?:[-*]\s+)?(?:\*\*)?Done when(?:\*\*)?[:\s]\s*(.*?)\s*$")
AFTER = re.compile(r"\s*(?:\*\*)?After it merges:(?:\*\*)?\s*(.*?)\s*$")
FILES = re.compile(r"\s*(?:\*\*)?Files:(?:\*\*)?\s*$")
ENTRY = re.compile(r"\s*([+~>-])\s+(\S.*?)\s*$")
NOTE = re.compile(r"\s*—\s*|\s+--?\s+")  # the em dash, or `--`/` - ` in a plan typed in ASCII
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
    ph = {"label": label, "title": title, "branch": "", "base": "", "after": "", "done": "", "files": [],
          "planned": False}
    first = next((ln for ln in body if ln.strip()), "")
    if not BULLET.match(first) and not FILES.match(first):
        bm, sm = BRANCH.search(first), BASE.search(first)
        ph["branch"], ph["base"] = (bm.group(1) if bm else ""), (sm.group(1) if sm else "")
    prose, fence, block = [], False, False  # block: True while the `Files:` lines run
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
        elif FILES.match(ln) and not ph["planned"]:
            block, ph["planned"] = True, True
        else:
            dm = DONE.match(ln)
            if dm and not ph["done"]:
                ph["done"] = dm.group(1)
            prose.append(ln)
    if not ph["planned"]:
        ph["files"] = bullet_paths(prose)
    return ph


def plan_sections(path):
    """The plan's phases in order, each {label, title, branch, base, after, done, files, planned}. `done` is the
    done-when, for a plan without an after line. `planned` is True
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


LEGEND = (("+", "new"), ("~", "changed"), ("-", "removed"), (">", "renamed"), ("*", "named in the plan"))


def clean(path):
    """A repo path with forward slashes and no leading `./` (a leading dot, as in `.github/`, stays)."""
    path = (path or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return "" if path in (".", "/") else path.lstrip("/")


def tree_of(entries, ls_files):
    """A nested {dirs, files, entry} tree of every path, each touched file or directory holding its entry."""
    root = {"dirs": {}, "files": {}, "entry": None}

    def node(parts):
        cur = root
        for part in parts:
            cur = cur["dirs"].setdefault(part, {"dirs": {}, "files": {}, "entry": None})
        return cur

    moved = {clean(e.get("old")) for e in entries if e.get("mark") == ">"}
    for p in ls_files:
        p = clean(p)
        if p and p not in moved:
            *dirs, name = p.split("/")
            node(dirs)["files"].setdefault(name, None)
    for e in entries:
        p = clean(e.get("path"))
        if not p:
            continue
        e = dict(e, mark=e.get("mark") or "*")
        if p.endswith("/"):
            node(p.rstrip("/").split("/"))["entry"] = e
        else:
            *dirs, name = p.split("/")
            node(dirs)["files"][name] = e
    return root


def count(n):
    return len(n["files"]) + sum(count(d) for d in n["dirs"].values())


def touched(n):
    return any(n["files"].values()) or any(d["entry"] or touched(d) for d in n["dirs"].values())


def tail(e):
    """What follows a name: the rename's old path, then the note."""
    old = f" (from {e['old']})" if e.get("old") else ""
    return old + (f" -- {e['note']}" if e.get("note") else "")


LINKS = {True: ("`-- ", "    "), False: ("|-- ", "|   ")}  # (connector, child prefix) for a last or other child


def tree_lines(n, prefix, at):
    """(line, touched path or "") pairs for the children of `n`, which sits at path `at`."""
    kids = [("d", k, n["dirs"][k]) for k in sorted(n["dirs"], key=str.lower)]
    kids += [("f", k, e) for k, e in sorted(n["files"].items(), key=lambda kv: kv[0].lower()) if e]
    rest = sum(1 for e in n["files"].values() if not e)
    if rest:
        kids.append(("r", "", rest))
    out = []
    for i, (kind, name, val) in enumerate(kids):
        last = i == len(kids) - 1
        conn, more = LINKS[last]
        path = f"{at}{name}"
        if kind == "r":
            out.append((f"{prefix}{conn}({val} other file{'s' * (val != 1)}, untouched)", ""))
        elif kind == "f":
            out.append((f"{prefix}{conn}{val['mark']} {name}{tail(val)}", path))
        else:
            e = val["entry"]
            mark = f"{e['mark']} " if e else ""
            text, tag = (tail(e), path + "/") if e else ("", "")
            if touched(val):
                out.append((f"{prefix}{conn}{mark}{name}/{text}", tag))
                out += tree_lines(val, prefix + more, path + "/")
            else:
                size = count(val)
                files = f"{size} file{'s' * (size != 1)}"
                fold = f" ({files})" if e and size else ("" if e else f" ({files}, untouched)")
                out.append((f"{prefix}{conn}{mark}{name}/{fold}{text}", tag))
    return out


def render_tree(entries, ls_files, cap=60):
    """The phase's file tree as lines: every path from `git ls-files` plus the entries' new ones. A directory on
    a touched path expands; any other folds to `dir/ (N files, untouched)`, and a directory's untouched files
    fold to one line. Touched files carry their mark and note. A legend line ends it, and past `cap` lines it
    stops and names the touched paths it cut. No entries gives []."""
    entries = [e for e in entries or [] if clean(e.get("path"))]
    if not entries:
        return []
    rows = [(".", "")] + tree_lines(tree_of(entries, ls_files or []), "", "")
    marks = {e.get("mark") or "*" for e in entries}
    legend = "marks: " + "  ".join(f"{m} {word}" for m, word in LEGEND if m in marks)
    cap = max(cap, 3)
    if len(rows) + 1 > cap:
        keep = cap - 2
        cut = [tag for _, tag in rows[keep:] if tag]
        line = f"... {len(rows) - keep} more lines cut"
        if cut:
            line += f", {len(cut)} touched: " + ", ".join(cut[:3]) + (f", +{len(cut) - 3} more" if len(cut) > 3 else "")
        rows = rows[:keep] + [(line, "")]
    return [r for r, _ in rows] + [legend]


BOX = {"closed": "[x]", "in_progress": "[~]"}  # where.py's phase status; anything else is [ ]


def pr_tag(pr):
    """`#12` from a phase's `#12`, `gh-12` or `12`; "" for none."""
    m = re.fullmatch(r"(?:#|gh-)?(\d+)", str(pr or "").strip())
    return f"#{m.group(1)}" if m else ""


def render_stack(phases, trunk="", brief=False):
    """The phases as lines: each under the branch it starts from, as `[x]`/`[~]`/`[ ]`, label, title, branch and
    PR, with its after line (else `done when:`) below, then a merge-order line. A phase stacks on the phase whose
    branch is its base; one with no base starts from `trunk` (default: the first base given that is no phase's
    branch, else `main`).
    `brief` gives only the phase lines, one per phase: no after lines, base lines or merge-order line. No phases
    gives []."""
    phases = [p for p in phases or [] if p.get("label")]
    if not phases:
        return []
    at = {p["branch"]: i for i, p in reversed(list(enumerate(phases))) if p.get("branch")}
    trunk = trunk or next((p["base"] for p in phases if p.get("base") and p["base"] not in at), "") or "main"
    up = [at.get(p.get("base") or trunk) for p in phases]
    for i in range(len(phases)):  # a phase based on itself, or a loop of bases, starts from its base instead
        seen, j = {i}, up[i]
        while j is not None and j not in seen:
            seen.add(j)
            j = up[j]
        if j == i:  # only a phase in the loop; one that merely stacks on a loop stays under it
            up[i] = None
    kids = {i: [k for k in range(len(phases)) if up[k] == i] for i in range(len(phases))}
    roots = {}
    for i, p in enumerate(phases):
        if up[i] is None:
            roots.setdefault(p.get("base") or trunk, []).append(i)
    out, order = [], []

    def walk(ids, prefix):
        for n, i in enumerate(ids):
            p, last = phases[i], n == len(ids) - 1
            conn, more = LINKS[last]
            head = " ".join(x for x in (BOX.get(p.get("status"), "[ ]"), p["label"], p.get("title") or "") if x)
            out.append(prefix + conn + "  ".join(x for x in (head, p.get("branch") or "", pr_tag(p.get("pr"))) if x))
            order.append(p["label"])
            after = f"after: {p['after']}" if p.get("after") else f"done when: {p['done']}" if p.get("done") else ""
            if after and not brief:
                out.append(prefix + more + ("|   " if kids[i] else "    ") + after)
            walk(kids[i], prefix + more)

    for base, ids in roots.items():
        out += [] if brief else [base]
        walk(ids, "")
    if brief:
        return out
    into = next(iter(roots)) if len(roots) == 1 else ""
    line = "merge order: " + " -> ".join(order) + (f" into {into}" if into else ", each into its base")
    if any(u is not None for u in up):
        line += f"; retarget a stacked PR onto {into or 'its base'} once the one below it merges"
    return out + [line]


# ---------------------------------------------------------------- CLI

MARKER = "<!-- yah:map -->"


def phases_of(state, plan):
    """where.py's phases with the plan's after line, done-when and files merged in by label; a phase only the plan
    names comes after them, as not started. Branch and base come from the state first, then the plan."""
    by = {p["label"]: p for p in plan}
    out = []
    for p in (state or {}).get("phases") or []:
        sec = by.pop(p.get("label"), {})
        out.append(dict(sec, **{k: v for k, v in p.items() if v or k not in sec}))
    return out + [dict(p, status="open") for p in plan if p["label"] in by]


def phase_refs(top, base, branch):
    """(fork, branch ref) for a phase: the branch (local first, else origin's) and where it left its base, the
    newest common ancestor with the local base or origin's, so a stale local base does not pull in commits the
    branch took from origin. ("", "") when either is missing."""
    from where import existing
    refs = existing(top, [base, branch]) if base and branch else {}
    ref = (refs.get(branch) or [""])[0]
    fork = run(["git", "merge-base", ref, *refs[base]], top) if ref and refs.get(base) else None
    return (fork or "").strip(), ref


def covers(planned, path):
    """True when a planned path names `path` itself, or a directory it sits in."""
    return any(p == path or (p.endswith("/") and path.startswith(p)) for p in planned)


def phase_entries(ph, top):
    """(entries, gaps, ref) for a phase's tree. Before it has commits: the plan's files and ref "". After: what the
    branch changed since its base, with the plan's notes kept, gap lines naming planned paths not touched yet and
    touched paths the plan's `Files:` block does not name, and the branch ref whose files the tree lists."""
    plan = ph.get("files") or []
    fork, ref = phase_refs(top, ph.get("base"), ph.get("branch"))
    got = actual_changes(fork, ref, top) if fork else []
    if not got:
        return plan, [], ""
    notes = {}
    for e in plan:
        for p in (e["path"], e.get("old")):
            if p and e.get("note"):
                notes.setdefault(clean(p), e["note"])
    got = [dict(e, note=notes.get(clean(e["path"]), notes.get(clean(e["old"]), ""))) for e in got]
    if not ph.get("planned"):
        return got, [], ref
    planned = {clean(e["path"]) for e in plan} | {clean(e.get("old")) for e in plan if e.get("old")}
    done = {clean(e["path"]) for e in got} | {clean(e["old"]) for e in got if e["old"]}
    gaps = []
    todo = [clean(e["path"]) for e in plan if not any(covers([clean(e["path"])], d) for d in done)]
    extra = [clean(e["path"]) for e in got if not covers(planned, clean(e["path"]))
             and not (e["old"] and covers(planned, clean(e["old"])))]
    for words, paths in (("planned, not touched yet", todo), ("touched, not in the plan", extra)):
        if paths:
            gaps.append(f"{words}: " + ", ".join(paths[:6]) + (f", +{len(paths) - 6} more" if len(paths) > 6 else ""))
    return got, gaps, ref


def map_lines(s, target="", brief=False):
    """The map for where.py's collected state `s`: the stack, then for a `P<n>` target that phase's tree. No
    target means the current phase, else `plan`. A plan with no phases gives []."""
    state = s.get("state") or {}
    spec = (state.get("plan") or {}).get("spec") or ""
    spec = Path(s["top"]) / Path(spec).expanduser() if spec else ""  # joining keeps an absolute spec as is
    phases = phases_of(state, plan_sections(spec) if spec else [])
    if not phases:
        return []
    heads = {p.get("headRefName"): p.get("number") for p in s.get("prs") or [] if isinstance(p, dict)}
    for p in phases:  # a phase whose STATE.md line names no PR takes the open one from its branch
        if not p.get("pr") and heads.get(p.get("branch")):
            p["pr"] = f"#{heads[p['branch']]}"
    target = (target or (state.get("phase") or {}).get("label") or "plan").strip()
    lines = render_stack(phases, brief=brief)
    if target.lower() == "plan":
        return lines
    ph = next((p for p in phases if p["label"].lower() == target.lower()), None)
    if not ph:
        return lines + ["", f"no phase {target} in this plan"]
    entries, gaps, ref = phase_entries(ph, s["top"])  # the tree lists the phase branch's files, not the checkout's
    ls_cmd = ["git", "ls-tree", "-r", "--name-only", "-z", ref] if ref else ["git", "ls-files", "-z"]
    ls = (run(ls_cmd, s["top"], timeout=15) or "").split("\0")
    tree = render_tree(entries, ls)
    head = f"{ph['label']} files" + ("" if tree else ": none named in the plan yet")
    return lines + ["", head] + tree + gaps


def main(argv=None):
    import argparse
    from yahlib import utf8_stdout
    from where import collect
    utf8_stdout()
    ap = argparse.ArgumentParser(description="The phase map: the plan's stack, and a phase's file tree.")
    ap.add_argument("target", nargs="?", default="", help="P<n> for that phase's tree, or plan (default: the phase)")
    ap.add_argument("--brief", action="store_true", help="one line per phase, no after lines")
    ap.add_argument("--pr", action="store_true", help="markdown for a PR description, between yah:map markers")
    ap.add_argument("--no-gh", action="store_true", help="skip gh pr list")
    ap.add_argument("--cwd", help="run as if started in this directory")
    a = ap.parse_args(argv)
    s = collect(a.cwd or ".", use_gh=not a.no_gh, infer=False)  # the map needs no branch-ancestry walk
    lines = map_lines(s, a.target, a.brief) if s else []
    if lines and a.pr:
        lines = [MARKER, "```text", *lines, "```", MARKER.replace("yah:", "/yah:")]
    if lines:
        print("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 - a headless step must not die on the map
        print(f"[yah] phasemap.py failed: {e}")
