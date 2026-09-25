"""beads.py: where.py's beads adapter, for a repo that already uses beads. yah never needs beads.

It reads this repo's .beads (bd first, then the committed issues.jsonl) and returns the plan state in the shape
a STATE.md plan has: the open epic labelled `plan`, its children labelled `phase` (the in_progress one is current,
its first notes line is NEXT) and beads labelled `human` as waiting on you. It is frozen: it shows nothing STATE.md
does not. Stdlib only.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yahlib import empty_state, find_tool, first_line, pick_phase, repo_dirs, run, short_of  # noqa: E402

CLAIM = "bd update {} --claim"  # starts a phase, like marking a STATE.md phase [~]


def read(top, main_root, use_bd=True):
    """What where.py needs from beads, or all None with no .beads here. state: the plan state, or None. source:
    bd, jsonl or none. dir: the .beads dir. bd: the CLI path the model may run, set only when `bd list` worked here
    and there is no note. note: why bd is off although .beads exists."""
    beads_dir = find_beads(top, main_root)
    if beads_dir is None:
        return {"state": None, "source": None, "dir": None, "bd": None, "note": None}
    local = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    bd = find_tool("bd", [Path(local) / "Programs" / "bd"] if local else []) if use_bd else None
    issues, source = load_issues(beads_dir, top, bd)
    env_dir = os.environ.get("BEADS_DIR")
    # An inherited BEADS_DIR would send the model's own bd calls to another repo's DB.
    note = ("BEADS_DIR points outside this repo; unset it to use beads here" if env_dir
            and os.path.normcase(os.path.realpath(env_dir)) != os.path.normcase(os.path.realpath(beads_dir))
            else "bd was skipped (--no-bd)" if not use_bd
            else "the bd CLI was not found" if not bd
            else "`bd list` failed here" if source != "bd" else None)
    return {"state": beads_state(issues), "source": source, "dir": str(beads_dir), "bd": None if note else bd,
            "note": note}


def find_beads(top, main_root):
    """This repo's .beads dir (the worktree's, else the main checkout's), or None."""
    return next((d / ".beads" for d in repo_dirs(top, main_root) if (d / ".beads").is_dir()), None)


def load_issues(beads_dir, top, bd):
    """All issues (closed included). bd first, then the committed JSONL export. bd runs with BEADS_DIR
    pinned to this repo's .beads, so an inherited BEADS_DIR or a walk up to a parent dir never reads
    another repo's database."""
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


def as_dict(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip().startswith("{"):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return {}


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


def labels(issue):
    return set(issue.get("labels") or [])


def beads_state(issues):
    """The plan epic and its phases, in the shape a STATE.md plan has. Returns a dict or None.

    "Waiting on you" = open or in-progress beads labelled `human`, like a STATE.md `(you)` line. As in STATE.md,
    a plan epic's phases are not loose work: in progress and open tasks leave them out, and open tasks leave
    out `human` beads."""
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

    phase_ids = {k["id"] for e in plans for k in phases_of(e["id"])}
    loose = [i for i in issues if i.get("issue_type") != "epic" and i["id"] not in phase_ids]
    human = [i for i in issues if i.get("status") in ("open", "in_progress") and i.get("issue_type") != "epic"
             and "human" in labels(i)]
    in_prog = [i for i in loose if i.get("status") == "in_progress"]
    ready = [i for i in loose if i.get("status") == "open" and "human" not in labels(i)]

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
    done = sum(1 for k in kids if k.get("status") == "closed")
    state["plan"] = {"id": epic["id"], "title": epic.get("title", ""), "source": "beads",
                     "short": short_of(epic.get("title")),
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
