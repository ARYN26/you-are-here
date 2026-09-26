"""where.py: the "you are here" view of a repo, or the project list in the home dir.

    where.py              full view, at most 15 lines
    where.py --brief      at most 6 lines, for the SessionStart hook
    where.py --json       the collected state, for /yah:wrap and /yah:phases
    where.py --cwd DIR    run as if started in DIR
    where.py --path NAME  print a project's path (for the `yah` launcher)
    --no-gh, --no-bd      skip `gh pr list` / the bd CLI (.beads/issues.jsonl is still read)

Sources:
  1. STATE.md / NOW.md at the repo root: a `## Plan:` section whose outermost checkbox lines are
     the phases (the [~] one is current; indented lines are sub-tasks), and dated `## YYYY-MM-DD`
     entries whose `- Next:` line is NEXT. `(you)` marks an open line as waiting on you.
  2. beads, if you already use beads, read by beads.py in the same shape. A STATE.md plan wins over a beads one.
  3. git (branch, dirty, ahead/behind) and `gh pr list` (open PRs, checks, stacking).

It writes where-<project>.json in the data dir for the statusline and the home view. For a repo
not in config.json the name also carries 8 hex of its path's sha1, so same-named repos never collide.
Never writes inside a repo. Stdlib only.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import beads  # noqa: E402
from yahlib import (NO_WINDOW, claude_dir, config, data_dir, empty_state, find_git, find_tool,  # noqa: E402
                    first_line, log_line, norm, pick_phase, project_key, read_branch, read_json, repo_dirs, run, run_info,
                    run_status, run_text, short_of, utf8_stdout, where_cache_path, write_json)

PR_FIELDS = "number,title,headRefName,baseRefName,isDraft,reviewDecision,statusCheckRollup,updatedAt"
FOOTER = ("This is the current state. Do not read docs to orient; /yah:where shows the full view. "
          "Reply first with one line (phase, NEXT, what waits on the user), before any tool call or branch change.")
SKILLS = ("where", "wrap", "start", "phases", "deep")
ALIAS = re.compile(r"(?<![\w/:])/([a-z][\w-]*):(" + "|".join(SKILLS) + r")(?![\w-])")
SHA = re.compile(r"[0-9a-fA-F]{7,40}")


# ---------------------------------------------------------------- subprocess

def start_gh(top):
    gh = find_tool("gh")
    if not gh:
        return None
    try:
        return subprocess.Popen([gh, "pr", "list", "--state", "open", "--limit", "30", "--json", PR_FIELDS],
                                cwd=top, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=NO_WINDOW)
    except Exception:
        return None


def finish_gh(proc):
    if proc is None:
        return None
    try:
        out, _ = proc.communicate(timeout=8)
        return json.loads(out.decode("utf-8", "replace")) if proc.returncode == 0 else None
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return None


# ---------------------------------------------------------------- git

def git_state(top):
    out = run(["git", "status", "--porcelain=v1", "-b"], top, timeout=5)
    st = {"branch": None, "dirty": 0, "ahead": 0, "behind": 0, "upstream": None, "base": None, "base_ahead": None}
    if out is None:
        return st
    lines = out.splitlines()
    if lines and lines[0].startswith("## "):
        head = lines[0][3:]
        m = re.search(r"\[(.*)\]$", head)
        if m:
            a = re.search(r"ahead (\d+)", m.group(1))
            b = re.search(r"behind (\d+)", m.group(1))
            st["ahead"] = int(a.group(1)) if a else 0
            st["behind"] = int(b.group(1)) if b else 0
            head = head[:m.start()].strip()
        if "..." in head:
            head, st["upstream"] = head.split("...", 1)
        if head.startswith("No commits yet on "):
            head = head[len("No commits yet on "):]
        st["branch"] = head.strip()
        lines = lines[1:]
    st["dirty"] = sum(1 for ln in lines if ln.strip())
    return st


# ---------------------------------------------------------------- text

def clip(text, n=170):
    """Cut at the last sentence end before n chars, else hard-cut with an ellipsis."""
    text = " ".join((text or "").split())
    if len(text) <= n:
        return text
    cut = max(text.rfind(". ", 0, n), text.rfind("; ", 0, n))
    return text[:cut + 1] if cut > 50 else text[:n - 3].rstrip() + "..."


# ---------------------------------------------------------------- STATE.md / NOW.md

CHECKBOX = re.compile(r"^(\s*)[-*]\s*\[([ xX~>])\]\s*(.+)$")
STATUS = {"x": "closed", "~": "in_progress", ">": "in_progress", " ": "open"}
YOU_MARK = re.compile(r"\s*\(you\)\s*$", re.I)


def md_line(ln):
    """A checkbox line as (status, title, fields, you, indent), or None. `(you)` may end the title or the line."""
    pm = CHECKBOX.match(ln)
    if not pm:
        return None
    rest = pm.group(3)
    you = bool(YOU_MARK.search(rest))
    parts = [p.strip() for p in re.split(r"[|·]", YOU_MARK.sub("", rest))]
    you = you or bool(YOU_MARK.search(parts[0]))
    return (STATUS[pm.group(2).lower()], YOU_MARK.sub("", parts[0]).strip(), parts[1:], you,
            len(pm.group(1).expandtabs(4)))


def md_scan(lines):
    """The `## ` headings, checkbox lines and other text lines outside code fences, so an example in a fence is
    never a plan or a NEXT. heads: [(line number, heading)]. items: [(line number, the index in heads of its section
    or None, md_line)]. prose: the stripped lines that are neither, nor any other heading."""
    heads, items, prose, fence = [], [], [], False
    for n, ln in enumerate(lines, 1):
        if ln.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        hm = re.match(r"## +(.+?)\s*$", ln)
        if hm:
            heads.append((n, hm.group(1)))
            continue
        ml = md_line(ln)
        if ml:
            items.append((n, len(heads) - 1 if heads else None, ml))
        elif ln.strip() and not ln.startswith("#"):
            prose.append(ln.strip())
    return heads, items, prose


def md_log(lines, n):
    """The plain lines indented under the phase line at line n, bullets dropped: the handoff log wrap keeps for the
    next session. Checkbox lines there are sub-tasks, skipped but not an end; a blank line is not an end either."""
    def indent(ln):
        t = ln.expandtabs(4)
        return len(t) - len(t.lstrip())
    top, log = indent(lines[n - 1]), []
    for ln in lines[n:]:
        s = ln.strip()
        if not s:
            continue
        if indent(ln) <= top or s.startswith(("```", "~~~")):
            break
        if not md_line(ln):
            log.append(log_line(s))
    return log


def md_plan(file, head, items, nxt, lines):
    """A `## Plan: Title (spec)` section as a plan: its phase lines (the least indented checkbox lines, normally
    unindented) with id file:line. Lines indented under a phase are its sub-tasks, not phases, or, when they are
    not checkboxes, its handoff `log`."""
    m = re.match(r"Plan:\s*(.*?)\s*(?:\(([^)]*)\))?$", head, re.I)
    title, spec = (m.group(1), m.group(2) or "") if m else (head, "")
    views = []
    for n, (status, text, fields, _, _) in items:
        lm = re.match(r"(P\d+)\b[\s:.-]*(.*)", text)
        label, name = (lm.group(1), lm.group(2)) if lm else (f"P{len(views) + 1}", text)
        v = {"id": f"{file}:{n}", "label": label, "title": name or text, "status": status,
             "branch": "", "base": "", "pr": "", "next": "", "log": md_log(lines, n)}
        for p in fields:
            fm = re.match(r"(branch|base)\s+(\S+)$", p, re.I)
            pr = re.match(r"PR\s*#?(\d+)$", p, re.I)
            if fm:
                v[fm.group(1).lower()] = fm.group(2)
            elif pr:
                v["pr"] = "#" + pr.group(1)
        views.append(v)
    if not views:
        return None
    s = pick_phase({"plan": {"id": file, "title": title, "source": file, "short": short_of(title), "spec": spec,
                             "done": sum(1 for v in views if v["status"] == "closed"), "total": len(views)}}, views)
    shown = s["phase"] or s["next_phase"]
    if shown:  # the file's one NEXT goes on the phase it shows; a bead keeps its own notes
        shown["next"] = nxt
    return s


def state_md(top, main_root=None):
    """STATE.md (else NOW.md): its NEXT, its plan, and its other checkbox lines, anywhere outside code fences.
    An open or in-progress line marked `(you)` waits on you. A [~] line that is not a phase is work in progress,
    `(you)` or not. An open line that is neither a phase nor `(you)` counts as an open task. Their id is
    file:line, so the model can go straight to it. A linked worktree with neither file reads the main checkout's,
    as beads does: STATE.md is gitignored, so a new worktree never has one. `path` is the file wrap must edit."""
    for f in (d / name for d in repo_dirs(top, main_root) for name in ("STATE.md", "NOW.md")):
        if not f.is_file():
            continue
        name = f.name
        text = f.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        heads, items, prose = md_scan(lines)

        def body(i):
            return "\n".join(lines[heads[i][0]:heads[i + 1][0] - 1 if i + 1 < len(heads) else len(lines)])

        dated = [i for i, (_, h) in enumerate(heads) if re.match(r"\d{4}-\d{2}-\d{2}", h)]
        plans = [i for i, (_, h) in enumerate(heads) if re.match(r"Plan:", h, re.I)]
        lead = {}  # a plan section's phase indent: its least indented checkbox lines are the phases
        for _, sec, ml in items:
            if sec in plans:
                lead[sec] = min(lead.get(sec, ml[4]), ml[4])
        phase_lines = {n for n, sec, ml in items if sec in lead and ml[4] == lead[sec]}
        rest = [(n, ml) for n, _, ml in items if n not in phase_lines]
        out: dict = {**empty_state(), "file": name, "path": str(f), "head": "", "next": "", "at": "",
                     "human": [{"id": f"{name}:{n}", "title": ml[1]} for n, _, ml in items
                               if ml[0] != "closed" and ml[3]],
                     "in_progress": [{"id": f"{name}:{n}", "title": ml[1]} for n, ml in rest
                                     if ml[0] == "in_progress"],
                     "open_count": sum(1 for _, ml in rest if ml[0] == "open" and not ml[3])}
        if dated:
            i = max(dated, key=lambda j: heads[j][1][:10])  # the newest date, whatever the order
            nxt = re.search(r"^- Next:\s*(.+(?:\n(?!- )\s+.+)*)", body(i), re.M)
            at = re.search(r"^- At:\s*(" + SHA.pattern + r")\b", body(i), re.M)  # the commit NEXT was written at
            out.update(head=heads[i][1], next=" ".join(nxt.group(1).split()) if nxt else first_line(body(i)),
                       at=at.group(1) if at else "")
        elif not plans:
            out["next"] = prose[0] if prose else ""
        # The plan with a phase in progress, else one with an open phase, else the first. A finished plan
        # left above the next one does not hide it.
        parsed = [p for p in (md_plan(name, heads[i][1], [(n, ml) for n, sec, ml in items
                                                           if sec == i and n in phase_lines], out["next"], lines)
                              for i in plans) if p]
        out.update(next((p for p in parsed if p["phase"]), None) or
                   next((p for p in parsed if p["next_phase"]), None) or (parsed[0] if parsed else {}))
        return out
    return None


def merge(md, bz):
    """STATE.md's plan state and beads' as one, either may be None: a STATE.md plan wins over a beads epic, and
    (you) lines, work in progress and open tasks add up, STATE.md's first."""
    both = [s for s in (md, bz) if s]
    if not both:
        return None
    won = md if md and (md["plan"] or not bz or not bz["plan"]) else bz
    return dict(won, human=[h for s in both for h in s["human"]],
                in_progress=[i for s in both for i in s["in_progress"]], open_count=sum(s["open_count"] for s in both))


# ---------------------------------------------------------------- PRs

def checks_state(pr):
    roll = pr.get("statusCheckRollup") or []
    vals = [(c.get("conclusion") or c.get("state") or c.get("status") or "").upper() for c in roll]
    if any(v in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE") for v in vals):
        return "checks RED"
    if any(v in ("PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "WAITING", "") for v in vals):
        return "checks running"
    return "green" if vals else "no checks"


def age_days(iso):
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds() / 86400
    except Exception:
        return 0


DEFAULT_TRUNKS = {"main", "master", "develop", "dev"}


def landing(top, prs):
    """Where work lands, for protection when config.json names no PROD: the bases at the bottom of the open
    PR stacks (most PRs first), then the remote's default branch. Read from local refs and the PR list."""
    heads = {p.get("headRefName") for p in prs or []}
    count = {}
    for p in prs or []:
        b = p.get("baseRefName")
        if b and b not in heads:
            count[b] = count.get(b, 0) + 1
    out = sorted(count, key=lambda b: (-count[b], b))
    ref = (run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], top, timeout=3) or "").strip()
    if ref.startswith("origin/") and ref[7:] not in out:
        out.append(ref[7:])
    return out


def phase_bases(phases):
    """The branches the plan's phase PRs target that are not themselves a phase's branch (a stacked phase
    targets the phase below it), most used first. Read from the STATE.md lines or beads metadata."""
    branches = {v.get("branch") for v in phases or []}
    count = {}
    for v in phases or []:
        b = v.get("base")
        if b and b not in branches:
            count[b] = count.get(b, 0) + 1
    return sorted(count, key=lambda b: (-count[b], b))


def branch_name(ref):
    """refs/heads/x and refs/remotes/origin/x are both x; any other ref is None."""
    return ref[11:] if ref.startswith("refs/heads/") else \
        ref[20:] if ref.startswith("refs/remotes/origin/") else None


def nearest(dist, shared):
    """The names at the smallest distance, those on origin first."""
    best = [k for k, v in dist.items() if v == min(dist.values())] if dist else []
    return sorted([k for k in best if k in shared] or best)


def nearest_bases(top, branch, skip=(), trunks=()):
    """(cut, merged) from git alone. [] for either on any failure.

    cut: the branches HEAD was cut from. Of the local and origin branches whose tip is on HEAD's first-parent
    chain (not HEAD's own branch or a phase branch), the nearest. A branch merged in as a second parent, like a
    docs PR merged into this branch, was never cut from, so it is not one. A branch whose tip is HEAD counts 0,
    like a phase branch just cut from its base. At the same distance a branch on origin beats a local-only one.

    merged: when cut names only trunks, the branches merged into HEAD off that chain with the fewest commits
    B..HEAD. A base synced in with `git merge` has moved its tip off the chain, and git cannot tell it from a
    merged docs PR, so these are protected (fail closed) but never called cut from."""
    skip = set(skip) | {branch, "HEAD", "", None}
    chain = run(["git", "rev-list", "--first-parent", "--max-count=5000", "HEAD"], top, timeout=3)
    pos = {sha: i for i, sha in enumerate((chain or "").split())}  # steps back from HEAD
    out = run(["git", "for-each-ref", "--format", "%(refname)%09%(objectname)", "refs/heads", "refs/remotes/origin"],
              top, timeout=3) if pos else None
    dist, shared, tips = {}, set(), {}
    for ln in (out or "").splitlines():
        ref, _, tip = ln.partition("\t")
        name = branch_name(ref)
        tips.setdefault(name, set()).add(tip)
        if name in skip or tip not in pos:
            continue
        if ref.startswith("refs/remotes/"):
            shared.add(name)
        dist[name] = min(dist.get(name, pos[tip]), pos[tip])
    cut = nearest(dist, shared)
    if not pos or set(cut) - set(trunks):
        return cut, []
    done = {t for n in set(trunks) | set(cut) for t in tips.get(n, ())}  # old feature branches merged into main
    return cut, merged_in(top, skip | set(dist), pos, done)


def merged_in(top, skip, pos, done=()):
    """The branches merged into HEAD, but not into any commit in done, whose tips are off HEAD's first-parent
    chain (not in pos), nearest by B..HEAD, those on origin first. Tips past the chain walk's 5000 commits count
    as off it. A few seconds at most."""
    refs, fmt = ["refs/heads", "refs/remotes/origin"], "%(refname)%09%(objectname)"
    filt = ["--merged", "HEAD", *[a for t in sorted(done) for a in ("--no-merged", t)]]
    out = run(["git", "for-each-ref", *filt, "--format", fmt + "%09%(ahead-behind:HEAD)", *refs], top, timeout=3)
    if out is None:  # git before 2.41 has no ahead-behind atom: count each tip with rev-list below
        out = run(["git", "for-each-ref", *filt, "--format", fmt, *refs], top, timeout=3)
    dist, shared, counts, deadline = {}, set(), {}, time.monotonic() + 3
    for ln in (out or "").splitlines():
        f = ln.split("\t")
        name = branch_name(f[0])
        if len(f) < 2 or name in skip or f[1] in pos:
            continue
        ab = f[2].split() if len(f) > 2 else []  # "ahead behind"; behind is B..HEAD
        if len(ab) == 2 and ab[1].isdigit():
            counts[f[1]] = int(ab[1])
        elif f[1] not in counts and time.monotonic() < deadline:
            c = run(["git", "rev-list", "--count", f"{f[1]}..HEAD"], top, timeout=2) or ""
            counts[f[1]] = int(c) if c.strip().isdigit() else None
        if counts.get(f[1]) is not None:
            if f[0].startswith("refs/remotes/"):
                shared.add(name)
            dist[name] = min(dist.get(name, counts[f[1]]), counts[f[1]])
    return nearest(dist, shared)


def existing(top, names):
    """{name: [its refs]}: the local branch and the one on origin, for each name that is either. One git call."""
    names = [n for n in dict.fromkeys(names) if n]
    cands = {n: (f"refs/heads/{n}", f"refs/remotes/origin/{n}") for n in names}
    out = run(["git", "for-each-ref", "--format=%(refname)", *[r for rs in cands.values() for r in rs]],
              top, timeout=3) if names else None
    have = set((out or "").split())
    return {n: [r for r in rs if r in have] for n, rs in cands.items() if have & set(rs)}


def count(top, revs, merges=True):
    """`git rev-list --count --first-parent` over revs (merges=False adds --no-merges), or None."""
    out = run(["git", "rev-list", "--count", "--first-parent", *([] if merges else ["--no-merges"]), *revs],
              top, timeout=3)
    return int(out) if out and out.strip().isdigit() else None


def md_stamp(top, sm):
    """The commit a STATE.md/NOW.md NEXT was written at: a tracked file's last own commit on this branch (none
    while it has uncommitted edits: NEXT was just written), else the `- At: <sha>` line wrap puts under `- Next:`.
    Merges are skipped: a merge of the base that touched the file was not a wrap."""
    if Path(sm["path"]).parent != Path(top):  # the main checkout's, read from a worktree: never tracked here
        return sm.get("at") or ""
    f = sm["file"]
    st = run(["git", "status", "--porcelain=v1", "--ignored", "--", f], top, timeout=3)
    if st is None:
        return ""
    if st[:2] in ("??", "!!"):
        return sm.get("at") or ""
    if st.strip():
        return ""
    return (run(["git", "log", "-1", "--first-parent", "--no-merges", "--format=%H", "--", f], top, timeout=3)
            or "").strip() or sm.get("at") or ""


def stale_next(top, sha, branch, base, refs):
    """Non-merge commits on the phase branch, local or on origin (HEAD when it names none), that the commit NEXT
    was written at lacks, leaving out the base's: the work NEXT does not know about. When the base is gone (merged
    and deleted, say), every other branch's commits are left out instead. None when there is no stamp, or the
    stamp or the branch is not in this clone. Never compares timestamps: wrap writes NEXT before its WIP commit."""
    if not SHA.fullmatch(sha or "") or (branch and branch not in refs):
        return None
    if base in refs:
        away = ["^" + r for r in refs[base]]
    elif base:
        away = ["--not", *([f"--exclude={branch}"] if branch else []), "--branches",
                *([f"--exclude=origin/{branch}"] if branch else []), "--remotes=origin"]
    else:
        away = []
    return count(top, (refs.get(branch) or ["HEAD"]) + ["^" + sha] + away, merges=False)


def aliases(top, spec):
    """[(other plugin's command, yah's)] for /x:wrap-style names of yah's skills in the plan, the project's
    CLAUDE.md files or the user's own, so a plan written for another plugin still maps when that one is off."""
    files = [Path(top) / "CLAUDE.md", Path(top) / ".claude" / "CLAUDE.md", Path(top) / "CLAUDE.local.md",
             claude_dir() / "CLAUDE.md"]
    if spec:
        p = Path(spec).expanduser()
        files.insert(0, p if p.is_absolute() else Path(top) / p)
    found = set()
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read(200_000)
        except (OSError, ValueError):
            continue
        found |= {(m.group(1), m.group(2)) for m in ALIAS.finditer(text) if m.group(1) != "yah"}
    pairs = sorted(found, key=lambda pn: (SKILLS.index(pn[1]), pn[0]))[:5]
    return [(f"/{p}:{n}", f"/yah:{n}") for p, n in pairs]


def prod_branch(prod):
    """The branch a PROD line names: a `backticked` word, else its first word."""
    m = re.search(r"`([^`\s]+)`", prod or "") or re.match(r"\s*([\w./-]+)", prod or "")
    return m.group(1) if m else ""


def trunk_set(s):
    return DEFAULT_TRUNKS | set([s["trunks"]] if isinstance(s["trunks"], str) else s["trunks"] or [])


def protected(s):
    """Branches nothing may commit on or push: trunks, the PROD branch, the plan's phase bases, where open PRs
    land (the last gh run's too when gh sees none), origin/HEAD, the branch HEAD was cut from, and when that is
    only a trunk, the nearest branch merged in (a synced base). Fails closed: each source only adds."""
    return sorted(trunk_set(s) | set(s.get("landing") or []) | set(s.get("bases") or [])
                  | set(s.get("ancestors") or []) | set(s.get("merged_in") or []) | ({prod_branch(s["prod"])} - {""}))


def prod_line(s):
    """The PROD text: config.json's, else the first inferred branch that is not an ordinary trunk, from the
    plan's phase bases, then where open PRs land, then the branch HEAD was cut from, saying which."""
    if s["prod"]:
        return s["prod"]
    trunks, near = trunk_set(s), s.get("ancestors") or []
    for why, cands in (("phase PRs target it", s.get("bases")), ("open PRs land there", s.get("landing")),
                       ("this branch was cut from it", [] if set(near) & trunks else near)):
        guess = next((b for b in cands or [] if b not in trunks), "")
        if guess:
            return f"`{guess}` inferred: {why}. Never push or commit it; set prod in config.json"
    return ""


def pr_lines(prs, branch, state, limit=4, trunks=(), auto=False):
    """auto: config.json's auto_merge, so a green phase PR says `yah run` merges it, not you."""
    if not prs:
        return [], {}
    trunks = DEFAULT_TRUNKS | set(trunks)
    # A PR is "stacked" only when its base is another PR's feature branch, not a trunk.
    heads = {p["headRefName"]: p["number"] for p in prs if p["headRefName"] not in trunks}
    base_of = {p["headRefName"]: p["baseRefName"] for p in prs}
    chain, cur = set(), branch
    while cur in base_of and cur not in chain:
        chain.add(cur)
        cur = base_of[cur]
    phase_by_pr = {}
    for v in (state or {}).get("phases", []) or []:
        m = re.match(r"(?:gh-|#)?(\d+)$", v.get("pr") or "")
        if m:
            phase_by_pr[int(m.group(1))] = v["label"]
        if v.get("branch") in heads:
            phase_by_pr.setdefault(heads[v["branch"]], v["label"])

    def rank(p):
        tier = 0 if p["headRefName"] == branch else 1 if p["headRefName"] in chain else \
            2 if p["number"] in phase_by_pr else 3
        return (tier, age_days(p.get("updatedAt", "")))

    ordered = sorted(prs, key=rank)
    out, index = [], {}
    for p in ordered:
        index[p["headRefName"]] = {"number": p["number"], "base": p["baseRefName"], "checks": checks_state(p)}
    for p in ordered[:limit]:
        bits = [f"#{p['number']}"]
        if p["number"] in phase_by_pr:
            bits.append(phase_by_pr[p["number"]])
        bits.append(f"{p['headRefName']} -> {p['baseRefName']}")
        bits.append(checks_state(p))
        if p.get("isDraft"):
            bits.append("draft")
        rd = p.get("reviewDecision")
        if rd == "CHANGES_REQUESTED":
            bits.append("changes requested")
        elif rd == "APPROVED":
            bits.append("approved")
        # A step left to the user comes with the command that does it, ready to paste in a terminal.
        if p["baseRefName"] in heads:
            bits.append(f"stacked on #{heads[p['baseRefName']]}: retarget after it merges, "
                        f"`gh pr edit {p['number']} --base {base_of[p['baseRefName']]}`")
        elif not p.get("isDraft") and checks_state(p) == "green":
            bits.append("auto-merge: on" if auto and p["number"] in phase_by_pr
                        else f"merge: you, `gh pr merge {p['number']} --merge`")
        d = age_days(p.get("updatedAt", ""))
        if d >= 2:
            bits.append(f"idle {int(d)}d")
        if p["headRefName"] == branch:
            bits.append("<- this branch")
        out.append("  ".join(bits))
    if len(prs) > limit:
        out.append(f"+{len(prs) - limit} more (gh pr list)")
    return out, index


# ---------------------------------------------------------------- collect + render

def collect(cwd, use_bd=True, use_gh=True, infer=True):
    """The project's state. infer=False skips the git walk for the branch HEAD was cut from (the home view)."""
    top, main_root, git_dir = find_git(cwd)
    if top is None:
        return None
    key = project_key(main_root)
    cfg = config()["projects"].get(key)
    cfg = cfg if isinstance(cfg, dict) else {}
    proc = start_gh(str(top)) if use_gh else None
    g = git_state(str(top))
    if not g["branch"]:
        g["branch"] = read_branch(git_dir) if git_dir else None
    bz = beads.read(top, main_root, use_bd)
    sm = state_md(top, main_root)
    md = {k: sm.pop(k) for k in empty_state()} if sm else None
    state = merge(md, bz["state"])
    lost = bz["state"] if md and md["plan"] and bz["state"] and bz["state"]["plan"] else None  # a hidden beads epic
    ignored = {k: lost["plan"][k] for k in ("id", "title", "source")} if lost else None
    hidden = lost["phases"] if lost else []
    phases = (state or {}).get("phases") or []
    mine = {v.get("branch") for v in phases} - {"", None}
    # A phase's own branch (a merged lower phase's, say) is never a landing, even when a PR still targets it.
    # The hidden epic's bases stay protected too: protection fails closed.
    bases = phase_bases(phases)
    bases += [b for b in phase_bases(hidden) if b not in bases and b not in mine]
    cached = [b for b in (read_json(where_cache_path(main_root), {}) or {}).get("landing") or [] if b not in mine]
    # "Cut from" only means something on a work branch: on main, a branch merged with --no-ff is an ancestor too.
    # Checked against the cached landing here so the git walk overlaps gh, and against the live one below.
    trunks = trunk_set({"trunks": cfg.get("trunks")})
    trunkish = trunks | set(bases) | set(cached) | {prod_branch(cfg.get("prod"))}
    near, merged = nearest_bases(str(top), g["branch"], mine, trunks) \
        if infer and g["branch"] and g["branch"] not in trunkish else ([], [])
    plan, phase = (state or {}).get("plan") or {}, (state or {}).get("phase") or {}
    stamp = ""  # the commit NEXT was written at: the running phase's (a bead's own), or a plan-less STATE.md's
    if infer and phase and plan.get("source") == "beads":
        stamp = phase.get("next_sha") or ""
    elif infer and sm and (phase or not plan):
        stamp = md_stamp(str(top), sm)
    cands = []  # with no upstream, BRANCH counts commits ahead of the phase base, else of the branch cut from
    if infer and g["branch"] and not g["upstream"]:
        on_phase = phase.get("branch") in ("", None, g["branch"])
        cands = [c for c in (phase.get("base") if on_phase else "", *near[:1]) if c and c != g["branch"]]
    refs = existing(str(top), ([phase.get("branch"), phase.get("base")] if SHA.fullmatch(stamp) else []) + cands)
    base = next((c for c in cands if c in refs), "")
    if base:  # local and origin both: a stale local base must not count origin's newer commits as ahead
        g["base"], g["base_ahead"] = base, count(str(top), ["HEAD", *["^" + r for r in refs[base]]])
    n = stale_next(str(top), stamp, phase.get("branch"), phase.get("base"), refs)
    on = g["branch"] if g["branch"] and not g["branch"].startswith("HEAD (") else "HEAD"  # detached: "HEAD (no branch)"
    stale = {"commits": n, "branch": phase.get("branch") or on, "sha": stamp} if n else None
    prs = finish_gh(proc)  # the git calls above overlap it
    land = [b for b in landing(str(top), prs) if b not in mine]
    if not prs:  # no gh, or gh sees no open PR: keep the PR bases the last gh run saw (fail closed)
        land += [b for b in cached if b not in land]
    if g["branch"] in land:
        if g["base"] in near and g["base"] != phase.get("base"):
            g["base"] = g["base_ahead"] = None
        near, merged = [], []
    s = {"key": key, "top": str(top), "main_root": str(main_root), "git": g, "state": state,
         "store": store(sm, main_root, plan, ignored, bz),
         "ignored_plan": ignored, "beads_source": bz["source"], "state_md": sm, "prs": prs,
         "gh_tried": proc is not None, "prod": cfg.get("prod", ""), "trunks": cfg.get("trunks", []), "landing": land,
         "auto_merge": config()["auto_merge"],
         "bases": bases, "ancestors": near, "merged_in": merged, "next_stale": stale,
         "aliases": aliases(str(top), plan.get("spec")) if infer else [],
         "run": run_info(key, str(top), str(main_root))}
    s["protected"] = protected(s)
    return s


def store(sm, main_root, plan, ignored, bz):
    """Where /yah:phases and /yah:wrap write the plan, so they never re-derive it. kind `beads` only in a repo that
    already uses beads, with bd usable and no STATE.md plan; else `STATE.md`. `path` is the STATE.md/NOW.md file to
    edit (in a linked worktree, the main checkout's) or to create at the repo root. `bd` is the only bd the model
    may run, null when bd is off. `note` is what to tell the user first: why bd is off, or that STATE.md's plan
    hides a beads epic."""
    kind = "beads" if bz["bd"] and plan.get("source") in (None, "beads") else "STATE.md"
    notes = [bz["note"], hides(ignored, plan) if ignored else None]
    return {"kind": kind, "path": sm["path"] if sm else str(Path(main_root) / "STATE.md"), "bd": bz["bd"],
            "note": "; ".join(n for n in notes if n) or None}


def hides(ignored, plan):
    return f"beads epic {ignored['id']} ignored: {plan['id']} has a plan"


def render(s, brief=False):
    g, b = s["git"], s["state"] or {}
    plan, phase, nxt_phase = b.get("plan"), b.get("phase"), b.get("next_phase")
    branch = g["branch"] or "?"
    tree = f"{g['dirty']} uncommitted" if g["dirty"] else "clean"
    sync = []
    if g["ahead"]:
        sync.append(f"{g['ahead']} unpushed")
    if g["behind"]:
        sync.append(f"{g['behind']} behind")
    if not g["upstream"] and branch not in ("?",):
        if g.get("base") and g.get("base_ahead") is not None:
            sync.append(f"{g['base_ahead']} ahead of {g['base']}")
        sync.append("no upstream")
    prl, _ = pr_lines(s["prs"], branch, b, limit=2 if brief else 4, trunks=s["trunks"], auto=s.get("auto_merge"))
    off_branch = phase["branch"] if phase and phase.get("branch") and phase["branch"] != branch else ""
    st = s.get("next_stale")
    stale = f"NEXT predates {st['commits']} commit{'' if st['commits'] == 1 else 's'} on {st['branch']}: " \
            "/yah:wrap first" if st else ""
    lines = []

    if brief:  # at most 6 lines
        warn = f"  ! phase branch is {off_branch}" if off_branch else ""
        lines.append(f"[yah] {s['key']}  branch {branch} ({', '.join([tree] + sync)}){warn}")
        swarn = f"  ! {stale}" if stale else ""
        if plan:
            p = phase or nxt_phase
            head = f"PLAN {plan['short']} {plan['done']}/{plan['total']} done"
            if phase:
                head += f"  PHASE {phase['label']} {phase['title']} ({phase['id']})"
            elif nxt_phase:
                head += f"  no phase in progress; next {nxt_phase['label']} ({nxt_phase['id']})"
            lines.append(head)
            if p and p.get("next"):
                lines.append(f"NEXT {p['next']}{swarn}")
        elif s["state_md"]:
            sm = s["state_md"]
            lines.append(f"{sm['file']} {sm['head']}  NEXT {clip(sm['next'])}{swarn}")
        if not plan and b.get("in_progress"):  # no plan, so no NEXT line: still 6 lines at most
            lines.append("IN PROGRESS " + "; ".join(f"{i['id']} {i['title'][:60]}" for i in b["in_progress"][:2]))
        tail = [f"RUN {clip(run_text(s['run']), 120)}"] if s.get("run") else []  # in the tail: still 6 lines
        if prl:
            tail.append("PR " + prl[0])
        if b.get("human"):
            tail.append(f"{len(b['human'])} waiting on you")
        if tail:
            lines.append(" | ".join(tail))
        if prod_line(s):
            lines.append(f"PROD {prod_line(s)}")
        al = s.get("aliases") or []
        lines.append(FOOTER + (f" The plan or CLAUDE.md says {', '.join(a for a, _ in al)}; unless those skills are "
                               f"listed, use {', '.join(y for _, y in al)}." if al else ""))
        return lines

    lines.append(f"BRANCH  {branch}  {', '.join([tree] + sync)}")
    if plan:
        spec = f"  {Path(plan['spec']).name}" if plan["spec"] else ""
        lines.append(f"PLAN    {clip(plan['title'], 58)}  [{plan['done']}/{plan['total']} done]  {plan['id']}{spec}")
        if s.get("ignored_plan"):
            lines.append(f"!       {hides(s['ignored_plan'], plan)}")
        if phase:
            br = f"  branch {phase['branch']}" if phase.get("branch") else ""
            lines.append(f"PHASE   {phase['label']} {clip(phase['title'], 64)}  in_progress  {phase['id']}{br}")
            if off_branch:
                lines.append(f"!       phase branch is {off_branch}, you are on {branch}")
            lines.append(f"NEXT    {phase['next'] or '(none yet; /yah:wrap writes one)'}")
            if stale:
                lines.append(f"!       {stale}")
        elif nxt_phase:
            claim = beads.CLAIM.format(nxt_phase["id"]) if plan.get("source") == "beads" else \
                f"mark it [~] in {plan['id']}"
            lines.append(f"PHASE   none in progress. Next: {nxt_phase['label']} {nxt_phase['title'][:50]}  ({claim})")
            if nxt_phase.get("next"):
                lines.append(f"NEXT    {nxt_phase['next']}")
        else:
            lines.append("PHASE   all phases closed. Close the plan when the last PR merges.")
    else:
        if s["state_md"]:
            sm = s["state_md"]
            lines.append(f"STATE   {sm['file']}: {sm['head']}")
            lines.append(f"NEXT    {clip(sm['next'])}")
            if stale:
                lines.append(f"!       {stale}")
        if b.get("in_progress"):
            for i in b["in_progress"][:3]:
                lines.append(f"DOING   {i['id']}  {i['title'][:70]}")
        if b.get("open_count"):
            lines.append(f"TASKS   {b['open_count']} open, no plan (/yah:phases after a plan is approved)")
    if s.get("run"):
        lines.append(f"RUN     {clip(run_text(s['run']))}")
    for i, h in enumerate((b.get("human") or [])[:2]):
        more = f"  (+{len(b['human']) - 2} more)" if i == 1 and len(b["human"]) > 2 else ""
        lines.append(("YOU     " if i == 0 else "        ") + f"{h['id']}  {h['title'][:64]}{more}")
    for i, ln in enumerate(prl):
        lines.append(("PRs     " if i == 0 else "        ") + ln)
    if s["prs"] is None and s.get("gh_tried"):
        lines.append("PRs     (gh unavailable or offline)")
    if prod_line(s):
        lines.append(f"PROD    {prod_line(s)}")
    return lines[:15]


def write_cache(s):
    g, b = s["git"], s["state"] or {}
    _, index = pr_lines(s["prs"], g["branch"], b, trunks=s["trunks"])
    path = where_cache_path(s["main_root"])
    old = read_json(path, {}) or {}
    phase = b.get("phase") or b.get("next_phase")
    write_json(path, {
        "ts": time.time(), "key": s["key"], "top": s["top"], "root": s["main_root"], "branch": g["branch"],
        "plan": b.get("plan"), "phase": phase, "phase_running": bool(b.get("phase")),
        "human": len(b.get("human") or []),
        "prs": index if s["prs"] is not None else old.get("prs", {}),
        "state_md": s["state_md"], "prod": prod_line(s), "landing": s["landing"]})


# ---------------------------------------------------------------- projects (home view, --path)

def known_projects():
    """[(name, path)]: the config.json projects, then repos where.py saw in the last recent_days,
    newest first. Each path appears once; two repos can share a name."""
    cfg = config()
    out = [(k, v["path"]) for k, v in cfg["projects"].items() if isinstance(v, dict) and v.get("path")]
    cutoff = time.time() - cfg["recent_days"] * 86400
    caches = [c for c in (read_json(f) for f in data_dir().glob("where-*.json")) if isinstance(c, dict)]
    out += [(c["key"], c["root"]) for c in sorted(caches, key=lambda c: -(c.get("ts") or 0))
            if c.get("key") and c.get("root") and (c.get("ts") or 0) >= cutoff]
    seen = set()
    return [(k, p) for k, p in out if not (norm(p) in seen or seen.add(norm(p)))]


def named(projects, name):
    """The paths of the known projects called `name`, ignoring case."""
    return [p for k, p in projects if name and k.lower() == name.lower()]


def home_view(brief):
    projects = known_projects()
    if brief and not projects:
        return []
    lines = ["PROJECTS  (start one with: yah <name>)"] if not brief else \
            ["[yah] Session is in the home dir, so project hooks, memory and state are not loaded. "
             "For project work, exit and run: yah <name>"]
    for key, path in projects:
        s = collect(path, use_bd=False, use_gh=False, infer=False) if Path(path).is_dir() else None
        if s is None:
            continue
        cache = read_json(where_cache_path(s["main_root"]), {}) or {}
        b = s["state"] or {}
        branch = s["git"]["branch"] or "?"
        if b.get("plan"):
            p = b.get("phase") or b.get("next_phase") or {}
            mark = "" if b.get("phase") else " (not started)"
            what = f"{b['plan']['short']} {p.get('label', '')}/{b['plan']['total']} {p.get('title', '')[:40]}{mark}"
        elif b.get("in_progress") and not (s["state_md"] or {}).get("next"):  # the last wrap's NEXT beats a [~]
            what = f"doing {b['in_progress'][0]['title'][:60]}"
        elif s["state_md"]:
            what = f"{s['state_md']['head'][:10]}: {clip(s['state_md']['next'], 60)}"
        else:
            what = "no plan"
        pr = (cache.get("prs") or {}).get(branch)
        prs = f"  PR #{pr['number']} {pr['checks']}" if pr else ""
        ri = s.get("run")
        status = run_status(ri) if ri else ""
        run_ = f"  RUN exit {ri['code']}" if status == "ended" else f"  RUN {status}" if status else ""
        dirty = f" +{s['git']['dirty']}" if s["git"]["dirty"] else ""
        lines.append(f"{key:<10} {clip(branch + dirty, 26):<26} {what}{prs}{run_}")
    if len(lines) == 1:
        lines.append("(none yet: start Claude Code in a git repo once, or add projects to config.json)")
    return lines[:6] if brief else lines


def print_path(name):
    """For the launcher: the project's path on stdout (exit 0), else the project list on stderr (exit 1)."""
    projects = known_projects()
    name = name.strip()
    hits = named(projects, name)
    if len(hits) == 1 and Path(hits[0]).is_dir():
        print(hits[0])
        return
    msg = [f"yah: '{name}' matches {len(hits)} repos; cd into one, or name them in config.json:" if len(hits) > 1
           else f"yah: no project named '{name}'. Projects:" if name else "usage: yah <project> [task words | -claude flags]. Projects:"]
    msg += [f"  {k:<14} {p}" for k, p in projects] or \
        ["  none yet: start Claude Code in a git repo once, or add projects to config.json"]
    print("\n".join(msg), file=sys.stderr)
    sys.exit(1)


def main():
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Where you are in this repo, or the project list in the home dir.")
    ap.add_argument("--brief", action="store_true", help="at most 6 lines, for the SessionStart hook")
    ap.add_argument("--json", action="store_true", help="the collected state as JSON")
    ap.add_argument("--cwd", help="run as if started in this directory")
    ap.add_argument("--no-gh", action="store_true", help="skip gh pr list")
    ap.add_argument("--no-bd", action="store_true", help="skip the bd CLI; read .beads/issues.jsonl")
    ap.add_argument("--path", nargs="?", const="", metavar="NAME", help="print a project's path and exit")
    a = ap.parse_args()
    if a.path is not None:
        return print_path(a.path)
    cwd = a.cwd or os.getcwd()
    top, _, _ = find_git(cwd)
    home = norm(Path.home())
    if top is None or norm(top) == home:
        if top is None and a.brief and norm(cwd) != home:
            return  # outside any repo: nothing to say at session start
        lines = home_view(a.brief)
        if lines:
            print("\n".join(lines))
        return
    s = collect(cwd, use_bd=not a.no_bd, use_gh=not a.no_gh)
    write_cache(s)
    if a.json:
        print(json.dumps(s, indent=1, ensure_ascii=False, default=str))
    else:
        print("\n".join(render(s, brief=a.brief)))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # a hook must never break a session
        print(f"[yah] where.py failed: {e}")
