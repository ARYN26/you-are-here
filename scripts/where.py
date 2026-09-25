"""where.py: the "you are here" view of a repo, or the project list in the home dir.

    where.py              full view, at most 15 lines
    where.py --brief      at most 6 lines, for the SessionStart hook
    where.py --json       the collected state, for /yah:wrap and /yah:phases
    where.py --cwd DIR    run as if started in DIR
    where.py --path NAME  print a project's path (for the `yah` launcher)
    --no-gh, --no-bd      skip `gh pr list` / the bd CLI (.beads/issues.jsonl is still read)

Sources, in order:
  1. beads: the open epic labelled `plan` (made by /yah:phases). Its children labelled
     `phase` are the phases, the in_progress one is current, its first notes line is NEXT.
  2. STATE.md / NOW.md at the repo root: a `## Plan:` section with one checkbox line per
     phase, and dated `## YYYY-MM-DD` entries whose `- Next:` line is NEXT.
  3. git (branch, dirty, ahead/behind) and `gh pr list` (open PRs, checks, stacking).

It writes where-<project>.json in the data dir for the statusline and the home view. For a repo
not in config.json the name also carries 8 hex of its path's sha1, so same-named repos never collide.
Never writes inside a repo. Stdlib only.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yahlib import (claude_dir, config, data_dir, find_git, norm, project_key, read_branch,  # noqa: E402
                    read_json, utf8_stdout, where_cache_path, write_json)

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
PR_FIELDS = "number,title,headRefName,baseRefName,isDraft,reviewDecision,statusCheckRollup,updatedAt"
FOOTER = ("This is the current state. Do not read docs to orient; /yah:where shows the full view. "
          "Reply first with one line (phase, NEXT, what waits on the user), before any tool call or branch change.")
SKILLS = ("where", "wrap", "start", "phases", "deep")
ALIAS = re.compile(r"(?<![\w/:])/([a-z][\w-]*):(" + "|".join(SKILLS) + r")(?![\w-])")
SHA = re.compile(r"[0-9a-fA-F]{7,40}")


# ---------------------------------------------------------------- subprocess

def run(cmd, cwd, timeout=10, env=None):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout, env=env,
                           creationflags=NO_WINDOW)
        if p.returncode != 0:
            return None
        return p.stdout.decode("utf-8", "replace")
    except Exception:
        return None


def find_tool(name):
    """PATH first, then where Homebrew, pipx/uv and the Windows bd installer put binaries."""
    extra = [Path.home() / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        extra.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "bd")
    return shutil.which(name) or shutil.which(name, path=os.pathsep.join(str(p) for p in extra))


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


# ---------------------------------------------------------------- beads

def as_dict(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip().startswith("{"):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return {}


def find_beads(top, main_root):
    """This repo's .beads dir (the worktree's, else the main checkout's), or None."""
    return next((d / ".beads" for d in (Path(top), Path(main_root)) if (d / ".beads").is_dir()), None)


def load_issues(top, main_root, use_bd):
    """All issues (closed included). bd first, then the committed JSONL export. bd runs with BEADS_DIR
    pinned to this repo's .beads, so an inherited BEADS_DIR or a walk up to a parent dir never reads
    another repo's database."""
    beads_dir = find_beads(top, main_root)
    if beads_dir is None:
        return None, None
    if use_bd:
        bd = find_tool("bd")
        if bd:
            out = run([bd, "list", "--all", "--json", "--limit", "0"], str(top), timeout=10,
                      env=dict(os.environ, BEADS_DIR=str(beads_dir)))
            if out:
                try:
                    data = json.loads(out)
                    if isinstance(data, list):
                        return data, "bd"
                except Exception:
                    pass
    jsonl = beads_dir / "issues.jsonl"
    if not jsonl.exists():
        return [], "none"
    issues = []
    for line in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict) and rec.get("_type", "issue") == "issue" and rec.get("id"):
            issues.append(rec)
    return issues, "jsonl"


def parent_of(issue):
    if issue.get("parent"):
        return issue["parent"]
    for dep in issue.get("dependencies") or []:
        if dep.get("type") == "parent-child" and dep.get("issue_id") == issue.get("id"):
            return dep.get("depends_on_id")
    return None


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]


def phase_order(issue):
    meta = as_dict(issue.get("metadata"))
    try:
        return (0, float(meta.get("phase", "")), "")
    except (TypeError, ValueError):
        return (1, 0.0, natural_key(issue.get("title", "")))


def first_line(text):
    for ln in (text or "").splitlines():
        ln = ln.strip().lstrip("-* ").strip()
        if ln:
            return re.sub(r"^(NEXT|Next)\s*[:\-]\s*", "", ln)
    return ""


def clip(text, n=170):
    """Cut at the last sentence end before n chars, else hard-cut with an ellipsis."""
    text = " ".join((text or "").split())
    if len(text) <= n:
        return text
    cut = max(text.rfind(". ", 0, n), text.rfind("; ", 0, n))
    return text[:cut + 1] if cut > 50 else text[:n - 3].rstrip() + "..."


def short_of(title):
    """A plan's statusline tag when none is given: its first word, upper case."""
    return ((title or "").split() or ["PLAN"])[0].upper()[:8]


def labels(issue):
    return set(issue.get("labels") or [])


def pick_phase(state, views):
    """The in_progress phase is current; with none running, the first unclosed one is next."""
    running = [v for v in views if v["status"] == "in_progress"]
    pending = [v for v in views if v["status"] != "closed"]
    state.update(phases=views, phase=running[0] if running else None,
                 next_phase=None if running or not pending else pending[0])
    return state


def empty_state():
    """The plan state with nothing in it: what beads_state fills, and what STATE.md merges into."""
    return {"human": [], "in_progress": [], "open_count": 0, "plan": None, "phase": None, "next_phase": None}


def beads_state(issues, you=()):
    """Pick the plan epic and its phases. Returns a dict or None.

    "Waiting on you" = open or in-progress beads labelled `human`, and open (unclaimed) ones
    assigned to a name in `you` (config.json, else git user.name). `bd update --claim` assigns
    git user.name, so a claimed bead is the work itself, not a wait; so is a phase bead."""
    if not issues:
        return None
    by_parent = {}
    for it in issues:
        p = parent_of(it)
        if p:
            by_parent.setdefault(p, []).append(it)

    def phases_of(epic_id):
        kids = by_parent.get(epic_id, [])
        tagged = [k for k in kids if "phase" in labels(k)]
        return sorted(tagged or kids, key=phase_order)

    open_epics = [i for i in issues if i.get("issue_type") == "epic" and i.get("status") != "closed"]
    plans = [e for e in open_epics if "plan" in labels(e)] or \
            [e for e in open_epics if any("phase" in labels(k) for k in by_parent.get(e["id"], []))]

    you = {y for y in you if y}
    human = [i for i in issues if i.get("status") in ("open", "in_progress") and i.get("issue_type") != "epic"
             and ("human" in labels(i) or (i.get("status") == "open" and (i.get("assignee") or "") in you
                                           and "phase" not in labels(i)))]
    in_prog = [i for i in issues if i.get("status") == "in_progress" and i.get("issue_type") != "epic"]
    ready = [i for i in issues if i.get("status") == "open" and i.get("issue_type") != "epic"]

    state = {**empty_state(), "human": [{"id": h["id"], "title": h.get("title", "")} for h in human],
             "in_progress": [{"id": i["id"], "title": i.get("title", "")} for i in in_prog], "open_count": len(ready)}
    if not plans:
        return state

    def active_score(e):
        kids = phases_of(e["id"])
        running = [k for k in kids if k.get("status") == "in_progress"]
        latest = max([k.get("updated_at", "") for k in kids] + [e.get("updated_at", "")])
        return (1 if running else 0, latest)

    epic = max(plans, key=active_score)
    kids = phases_of(epic["id"])
    meta = as_dict(epic.get("metadata"))
    done = sum(1 for k in kids if k.get("status") == "closed")
    state["plan"] = {"id": epic["id"], "title": epic.get("title", ""), "source": "beads",
                     "short": meta.get("short") or short_of(epic.get("title")),
                     "spec": epic.get("spec_id") or "", "done": done, "total": len(kids)}

    def phase_view(k, idx):
        m = as_dict(k.get("metadata"))
        num = m.get("phase")
        label = f"P{int(num) if isinstance(num, (int, float)) or str(num).isdigit() else num}" if num not in (None, "") else f"P{idx + 1}"
        title = re.sub(rf"^{re.escape(label)}\b[\s:.-]*", "", k.get("title", "")) or k.get("title", "")
        return {"id": k["id"], "label": label, "title": title, "status": k.get("status"),
                "branch": m.get("branch") or "", "base": m.get("base") or "",
                "pr": (k.get("external_ref") or ""), "next": first_line(k.get("notes")),
                "next_sha": str(m.get("next_sha") or "")}

    return pick_phase(state, [phase_view(k, i) for i, k in enumerate(kids)])


# ---------------------------------------------------------------- STATE.md / NOW.md

PHASE_LINE = re.compile(r"^\s*[-*]\s*\[([ xX~>])\]\s*(.+)$")
STATUS = {"x": "closed", "~": "in_progress", ">": "in_progress", " ": "open"}
YOU_MARK = re.compile(r"\s*\(you\)\s*$", re.I)


def md_line(ln):
    """A checkbox line as (status, title, fields, you), or None. `(you)` may end the title or the line."""
    pm = PHASE_LINE.match(ln)
    if not pm:
        return None
    rest = pm.group(2)
    you = bool(YOU_MARK.search(rest))
    parts = [p.strip() for p in re.split(r"[|·]", YOU_MARK.sub("", rest))]
    you = you or bool(YOU_MARK.search(parts[0]))
    return STATUS[pm.group(1).lower()], YOU_MARK.sub("", parts[0]).strip(), parts[1:], you


def md_human(file, text):
    """Open or in-progress checkbox lines marked `(you)`, anywhere in the file outside code fences:
    STATE.md's `human` label. The id is file:line, so the model can go straight to it."""
    out, fence = [], False
    for n, ln in enumerate(text.splitlines(), 1):
        if ln.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        ml = None if fence else md_line(ln)
        if ml and ml[0] != "closed" and ml[3]:
            out.append({"id": f"{file}:{n}", "title": ml[1]})
    return out


def md_plan(file, head, body, nxt):
    """A `## Plan: Title (spec)` section as the same plan/phase/phases shape beads gives."""
    m = re.match(r"Plan:\s*(.*?)\s*(?:\(([^)]*)\))?$", head, re.I)
    title, spec = (m.group(1), m.group(2) or "") if m else (head, "")
    views = []
    for ln in body.splitlines():
        ml = md_line(ln)
        if not ml:
            continue
        status, text, fields, _ = ml
        lm = re.match(r"(P\d+)\b[\s:.-]*(.*)", text)
        label, name = (lm.group(1), lm.group(2)) if lm else (f"P{len(views) + 1}", text)
        v = {"id": file, "label": label, "title": name or text, "status": status,
             "branch": "", "base": "", "pr": "", "next": ""}
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
    state = pick_phase({"plan": {"id": file, "title": title, "source": file, "short": short_of(title), "spec": spec,
                                 "done": sum(1 for v in views if v["status"] == "closed"), "total": len(views)}},
                       views)
    cur = state["phase"] or state["next_phase"]
    if cur:
        cur["next"] = nxt
    return state


def state_md(top):
    for name in ("STATE.md", "NOW.md"):
        f = Path(top) / name
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        heads = list(re.finditer(r"^## +(.+?)\s*$", text, re.M))

        def body(i):
            return text[heads[i].end():heads[i + 1].start() if i + 1 < len(heads) else len(text)]

        dated = [i for i, m in enumerate(heads) if re.match(r"\d{4}-\d{2}-\d{2}", m.group(1))]
        plans = [i for i, m in enumerate(heads) if re.match(r"Plan:", m.group(1), re.I)]
        out: dict = {"file": name, "head": "", "next": "", "at": "", "plan": None, "human": md_human(name, text)}
        if dated:
            i = max(dated, key=lambda j: heads[j].group(1)[:10])  # the newest date, whatever the order
            nxt = re.search(r"^- Next:\s*(.+(?:\n(?!- )\s+.+)*)", body(i), re.M)
            at = re.search(r"^- At:\s*(" + SHA.pattern + r")\b", body(i), re.M)  # the commit NEXT was written at
            out.update(head=heads[i].group(1), next=" ".join(nxt.group(1).split()) if nxt else first_line(body(i)),
                       at=at.group(1) if at else "")
        elif not plans:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
            out["next"] = lines[0] if lines else ""
        # Like beads' running epic: the plan with a phase in progress, else one with an open phase, else the first.
        # A finished plan left above the next one does not hide it.
        parsed = [p for p in (md_plan(name, heads[i].group(1), body(i), out["next"]) for i in plans) if p]
        out["plan"] = next((p for p in parsed if p["phase"]), None) or \
            next((p for p in parsed if p["next_phase"]), None) or (parsed[0] if parsed else None)
        return out
    return None


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
    targets the phase below it), most used first. Read from beads metadata or the STATE.md lines."""
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


def pr_lines(prs, branch, bstate, limit=4, trunks=()):
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
    for v in (bstate or {}).get("phases", []) or []:
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
        if p["baseRefName"] in heads:
            bits.append(f"stacked on #{heads[p['baseRefName']]}: retarget after it merges")
        elif not p.get("isDraft") and checks_state(p) == "green":
            bits.append("merge: you")
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
    issues, source = load_issues(top, main_root, use_bd)
    you = cfg.get("you") or []
    you = [you] if isinstance(you, str) else you
    if issues and not you:
        you = [(run(["git", "config", "user.name"], str(top), timeout=5) or "").strip()]
    bstate = beads_state(issues, you) if issues is not None else None
    sm = state_md(top)
    mdplan = sm.pop("plan", None) if sm else None
    mdhuman = sm.pop("human", []) if sm else []
    if mdplan and not (bstate or {}).get("plan"):  # beads wins when it has a plan epic
        bstate = {**(bstate or empty_state()), **mdplan}
    if mdhuman:  # a `(you)` line waits on you whichever source holds the plan
        bstate = bstate or empty_state()
        bstate["human"] = bstate["human"] + mdhuman
    phases = (bstate or {}).get("phases") or []
    mine = {v.get("branch") for v in phases} - {"", None}
    # A phase's own branch (a merged lower phase's, say) is never a landing, even when a PR still targets it.
    bases = phase_bases(phases)
    cached = [b for b in (read_json(where_cache_path(main_root), {}) or {}).get("landing") or [] if b not in mine]
    # "Cut from" only means something on a work branch: on main, a branch merged with --no-ff is an ancestor too.
    # Checked against the cached landing here so the git walk overlaps gh, and against the live one below.
    trunks = trunk_set({"trunks": cfg.get("trunks")})
    trunkish = trunks | set(bases) | set(cached) | {prod_branch(cfg.get("prod"))}
    near, merged = nearest_bases(str(top), g["branch"], mine, trunks) \
        if infer and g["branch"] and g["branch"] not in trunkish else ([], [])
    plan, phase = (bstate or {}).get("plan") or {}, (bstate or {}).get("phase") or {}
    stamp = ""  # the commit NEXT was written at: the running phase's, or a plan-less STATE.md's
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
    beads_dir = find_beads(top, main_root)
    env_dir = os.environ.get("BEADS_DIR")
    # An inherited BEADS_DIR would send the model's own bd calls to another repo's DB.
    bd_note = ("BEADS_DIR points outside this repo; unset it to use beads here" if beads_dir and env_dir
               and os.path.normcase(os.path.abspath(env_dir)) != os.path.normcase(str(beads_dir)) else None)
    s = {"key": key, "top": str(top), "main_root": str(main_root), "git": g, "beads": bstate,
            "beads_source": source, "state_md": sm, "prs": prs, "gh_tried": proc is not None,
            "prod": cfg.get("prod", ""), "trunks": cfg.get("trunks", []), "landing": land,
            "bases": bases, "ancestors": near, "merged_in": merged, "next_stale": stale,
            "aliases": aliases(str(top), plan.get("spec")) if infer else [],
            "beads_dir": str(beads_dir) if beads_dir else None,
            "bd": find_tool("bd") if use_bd and beads_dir and not bd_note else None,  # null: no CLI, no .beads here, or bd_note
            "bd_note": bd_note}
    s["protected"] = protected(s)
    return s


def render(s, brief=False):
    g, b = s["git"], s["beads"] or {}
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
    prl, _ = pr_lines(s["prs"], branch, b, limit=2 if brief else 4, trunks=s["trunks"])
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
        elif b.get("in_progress"):
            lines.append("IN PROGRESS " + "; ".join(f"{i['id']} {i['title'][:60]}" for i in b["in_progress"][:2]))
        tail = []
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
        if phase:
            br = f"  branch {phase['branch']}" if phase.get("branch") else ""
            lines.append(f"PHASE   {phase['label']} {clip(phase['title'], 64)}  in_progress  {phase['id']}{br}")
            if off_branch:
                lines.append(f"!       phase branch is {off_branch}, you are on {branch}")
            lines.append(f"NEXT    {phase['next'] or '(none yet; /yah:wrap writes one)'}")
            if stale:
                lines.append(f"!       {stale}")
        elif nxt_phase:
            claim = f"bd update {nxt_phase['id']} --claim" if plan.get("source") == "beads" else \
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
        if s["beads_source"] and s["beads"] is not None and not s["beads"].get("plan"):
            lines.append(f"BEADS   {b.get('open_count', 0)} open, no plan epic (/yah:phases after a plan is approved)")
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
    g, b = s["git"], s["beads"] or {}
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
        b = s["beads"] or {}
        branch = s["git"]["branch"] or "?"
        if b.get("plan"):
            p = b.get("phase") or b.get("next_phase") or {}
            mark = "" if b.get("phase") else " (not started)"
            what = f"{b['plan']['short']} {p.get('label', '')}/{b['plan']['total']} {p.get('title', '')[:40]}{mark}"
        elif s["state_md"]:
            what = f"{s['state_md']['head'][:10]}: {clip(s['state_md']['next'], 60)}"
        elif b.get("in_progress"):
            what = f"doing {b['in_progress'][0]['title'][:60]}"
        else:
            what = "no plan"
        pr = (cache.get("prs") or {}).get(branch)
        prs = f"  PR #{pr['number']} {pr['checks']}" if pr else ""
        dirty = f" +{s['git']['dirty']}" if s["git"]["dirty"] else ""
        lines.append(f"{key:<10} {clip(branch + dirty, 26):<26} {what}{prs}")
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
           else f"yah: no project named '{name}'. Projects:" if name else "usage: yah <project> [claude args]. Projects:"]
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
