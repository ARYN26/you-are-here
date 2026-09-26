"""run.py: `yah run`, which chains fresh headless sessions until the phase's PR is open and green.

    yah run [TARGET] [--plan] [--cwd DIR | --project NAME] [--iterations N] [--budget USD] [--dry-run]
            [--model M] [--plugin-dir DIR] [--detach]
    yah run --stop [--cwd DIR | --project NAME]

--plan goes on past exit 0: once a phase's PR is open and green (or merged) and the phase is closed, it
pins the next open phase and runs that, until none is left ("plan done", exit 0). The iteration cap is per
phase; run_total_hours and the weekly pace hold for the whole run. A phase whose plan base is a phase
branch that has since merged builds on where it merged, and a phase with no base stacks on the phase this
run just finished while that PR is open; either way /yah:resume is told `base=<branch>`.

Auto-merge is off unless config.json has "auto_merge": true. Then the driver, never a session, merges an
open, green PR of a closed plan phase when every rule holds (a check ran and all passed, MERGEABLE, merge
state CLEAN or HAS_HOOKS, not a draft, no newer CHANGES_REQUESTED, authored by the gh user): a merge commit
pinned to the head it saw, then the open PRs on its head retargeted to its base, then the head branch
deleted unless protected. --plan then goes on, and the next phase builds on that base.

TARGET is P<n> (a plan phase), #<pr> (a bare PR number works too, since shells treat # as a
comment) or empty for the current phase, which is pinned at start and passed as P<n>. Each iteration
is one `claude -p "/yah:resume TARGET MODE"` in auto permission mode, with prompts off and a DENY
list for force pushes, pushes to protected branches, merges and deletes, backed by the push_guard.py
PreToolUse hook. Protected is where.py's set (trunks, PROD, the plan's phase bases, where PRs land,
origin/HEAD, the branch HEAD was cut from) plus main and master. A P<n> that is not in the plan is
refused, and so is a run that cannot name the branch its PR targets. Run from a yah checkout rather
than an installed plugin, it passes --plugin-dir <checkout> so each session has /yah:resume. Before
and after each iteration it reads the PR (gh) and the phase (where.py), and the first stop rule that
matches sets the exit code:

    0  PR open, green, phase closed: the merge is yours, or auto-merge skipped it and says why (--plan:
       no open phase left, "plan done")
    2  needs you: error, denial, needs-human, blocked, or a session that ended without a YAH-RESULT line
    3  stalled: HEAD and NEXT unchanged twice in a row
    4  iteration or wall-clock cap
    5  refused to start
    6  PR merged or closed (--plan goes on past a merged PR of a closed phase)
    7  weekly usage at run_week_stop_pct or over pace + pace_slack
    8  PR merged by yah (auto_merge on; --plan goes on to the next phase instead)
    1  run.py itself failed
  130  Ctrl-C, or ended by --stop

Logs go to <data dir>/runs/. The driver writes nothing inside the repo. Each run holds a lock on
runs/<project>-<hash>.pid (<project>-<hash>@<worktree>.pid in a linked worktree), a JSON file with its pid, target, log and,
once it stops, its exit code and reason; a second run in the same checkout is refused (exit 5). The OS drops the
lock when the driver dies, so a pid file whose lock is free is a run that ended. where.py and the statusline show
it as the RUN line (live: target and the log's last line; ended: exit code and reason) for 3 days.

--detach starts the same command in the background (Windows: CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, so
closing the terminal cannot end it; elsewhere: its own session, so SIGHUP never reaches it). Its output goes to
runs/<name>.out beside its .log. The caller returns 0 once the run holds the lock, or the run's own exit code if
it stopped first, like a refusal.

--stop ends the checkout's run: the driver, the session it is in and every process under them (taskkill /T on
Windows; elsewhere the driver is frozen first, so it starts nothing while its tree is found). It then writes exit
130 to the pid file, so the RUN line says the run ended. A session cut off mid-edit can leave uncommitted work.

What a real `claude -p --output-format stream-json --verbose` stream carries (checked Sep 2026):
  events: system (subtypes hook_started, hook_response, init, thinking_tokens), assistant
  (message.content holds the tool_use blocks), rate_limit_event, result.
  result: is_error, subtype, result, total_cost_usd, num_turns, permission_denials, usage,
  modelUsage, duration_ms, stop_reason, terminal_reason, session_id.
  rate_limit_event.rate_limit_info: status, resetsAt, rateLimitType, overageStatus, isUsingOverage,
  unifiedWindows {five_hour, seven_day: {utilization (0-1), resetsAt (epoch s)}}. seven_day is
  weekly usage, so the pace check prefers it over limits.json.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import where  # noqa: E402
from statusline import week_pace  # noqa: E402
from yahlib import (NO_WINDOW, config, data_dir, find_git, find_tool, end_run, hold_run, num,  # noqa: E402
                    project_key, read_branch, read_json, run, run_pid_path, run_state, utf8_stdout, write_run)

POLL_S = num(os.environ.get("YAH_RUN_POLL_S"), 30)  # tests shorten the pending-checks poll
KEEP_RUNS = 20
DETACH_WAIT_S = 60  # how long --detach waits for the background run to take its lock
STOP_WAIT_S = 15  # how long --stop waits for the ended run to let go of its lock
STOPPED = "ended by yah run --stop."
TAG = re.compile(r"^\s*YAH-RESULT:\s*(.+?)\s*$", re.M)
DENY_BASE = ["PowerShell", "Bash(git push --force*)", "Bash(git push -f*)", "Bash(git push *--force*)", "Bash(git push * -f*)",
             "Bash(git push *+*)", "Bash(gh pr merge*)", "Bash(gh api *merge*)", "Bash(gh repo delete*)",
             "Bash(git push *--delete*)", "Bash(git push *--mirror*)", "Bash(git push *--all*)",
             "Bash(git push *--prune*)", "Bash(git push -d*)", "Bash(git push * -d *)"]
DENY_BRANCH = ["Bash(git push * {})", "Bash(git push * {} *)", "Bash(git push * HEAD:{}*)", "Bash(git push * *:{}*)",
               "Bash(git push *refs/heads/{}*)"]
PR_JSON = ("state,url,reviewDecision,reviews,commits,headRefName,baseRefName,mergeable,mergeStateStatus,isDraft,"
           "author,headRefOid")
NO_TAG = "session ended without a YAH-RESULT line; is the yah plugin loaded? (--plugin-dir)"
NO_BASE = ("cannot tell which branch {} targets: no base in the plan, no open PR for {}, no origin/HEAD and no "
           "prod in config.json, so it cannot be protected. Set base in the plan (`| base <branch>` on the STATE.md "
           "phase line, or beads metadata if you already use beads) or prod in config.json.")


def say(text=""):
    print(text, flush=True)


def brief(value, n=60):
    """A tool input in one short line: its main field when it has one."""
    if isinstance(value, dict):
        for k in ("command", "file_path", "path", "pattern", "url", "skill", "description", "prompt"):
            if value.get(k):
                value = value[k]
                break
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return " ".join(str(text).split())[:n]


# ---------------------------------------------------------------- setup

prod_branch = where.prod_branch


def protected(branches, prod=""):
    """The branches no iteration may push: where.py's protected set, the PROD branch, main and master."""
    return sorted({str(b) for b in branches if b} | {"main", "master"} | ({prod} if prod else set()))


def deny_list(branches):
    return DENY_BASE + [p.format(b) for b in branches for p in DENY_BRANCH]


def guard(r, branches):
    """Grow the protected set (it never shrinks mid-run) and rebuild the DENY list from it."""
    r.protected = protected(set(r.protected) | set(branches))
    r.deny = deny_list(r.protected)


def plugin_source(given, here=None):
    """(--plugin-dir for each session or None, what the dry run says). An installed plugin, in
    <config>/plugins/cache/... or <config>/plugins/marketplaces/..., is already loaded; a checkout is not."""
    if given:
        path = str(Path(given).resolve())
        return path, f"--plugin-dir {path} (given)"
    here = Path(here or __file__).resolve()
    names = [p.name for p in here.parents]
    if any(a in ("cache", "marketplaces") and b == "plugins" for a, b in zip(names, names[1:])):
        return None, f"the installed yah plugin ({here.parents[1]})"
    root = str(here.parents[1])
    return root, f"--plugin-dir {root} (this checkout: run.py is not an installed plugin)"


def parse_target(t):
    """'' | 'P<n>' | '#<n>'; None when TARGET is neither."""
    t = (t or "").strip()
    if not t:
        return ""
    if re.fullmatch(r"[Pp]\d+", t):
        return t.upper()
    m = re.fullmatch(r"#?(\d+)", t)
    return "#" + m.group(1) if m else None


def command(path, args):
    """The Popen command. Windows .cmd/.bat shims run through cmd /c."""
    if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return '"{}" /d /s /c "{}"'.format(comspec, subprocess.list2cmdline([path, *args]))
    return [path, *args]


def claude_argv(r):
    prompt = " ".join(x for x in ("/yah:resume", r.target or r.label, r.mode, r.base_arg and "base=" + r.base_arg)
                      if x)
    argv = [r.claude, "-p", prompt, "--permission-mode", "auto", "--permission-prompts", "none",
            "--disallowedTools", *r.deny, "--max-budget-usd", "{:g}".format(r.budget),
            "--output-format", "stream-json", "--verbose", "--settings", guard_settings()]
    return argv + (["--model", r.model] if r.model else []) + (["--plugin-dir", r.plugin_dir] if r.plugin_dir else [])


def guard_settings():
    """Hooks for run iterations only; interactive sessions never pay for them. Headless sessions get one
    prompt, so the UserPromptSubmit guard fires once: the context guard also runs as PostToolUse. The push
    guard (PreToolUse on Bash and PowerShell) denies pushes the DENY patterns can miss; YAH_PROTECTED names the
    branches. DENY_BASE also turns the PowerShell tool off in runs: its commands would bypass the Bash patterns."""
    here, py = Path(__file__).resolve().parent, Path(sys.executable).as_posix()

    def hook(name):
        return {"type": "command", "command": f'"{py}" "{(here / name).as_posix()}"', "timeout": 10}
    return json.dumps({"hooks": {"PostToolUse": [{"hooks": [hook("context_guard.py")]}],
                                 "PreToolUse": [{"matcher": t, "hooks": [hook("push_guard.py")]}
                                                for t in ("Bash", "PowerShell")]}})  # no "|": cmd /c reads it as a pipe


# ---------------------------------------------------------------- PR and phase

def gh(r, *args, timeout=20):
    """(returncode, stdout, stderr); returncode None when gh could not run in `timeout` s."""
    try:
        p = subprocess.run(command(r.gh, list(args)), cwd=r.top, capture_output=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except Exception:
        return None, "", ""


def pr_view(r, ref=None, fields=PR_JSON):
    """`gh pr view` of ref (a number or a branch; default the run's PR) as a dict, or None when gh cannot say."""
    rc, out, _ = gh(r, "pr", "view", str(ref or r.pr), "--json", fields)
    try:
        view = json.loads(out) if rc == 0 else None
    except ValueError:
        return None
    return view if isinstance(view, dict) else None


def checks(r, wait):
    """pass, none (gh reports no checks), fail, pending or unknown. Pending (gh exit 8) is polled up to
    run_checks_wait_minutes. The stop rules treat none as green; auto-merge wants pass."""
    deadline = time.monotonic() + r.cfg["run_checks_wait_minutes"] * 60
    while True:
        rc, out, err = gh(r, "pr", "checks", str(r.pr), "--required")
        if rc not in (0, 8) and "no required checks" in (out + err).lower():
            rc, out, err = gh(r, "pr", "checks", str(r.pr))
        if rc is not None and "no checks reported" in (out + err).lower():
            return "none"
        if rc == 0:
            return "pass"
        if rc != 8:
            return "unknown" if rc is None else "fail"
        if not wait or time.monotonic() >= deadline:
            return "pending"
        if not r.waiting:
            say(f"[yah] PR #{r.pr}: checks pending, waiting up to {r.cfg['run_checks_wait_minutes']:g} min")
            r.waiting = True
        time.sleep(POLL_S)


def changes_requested(view):
    """A CHANGES_REQUESTED review, still each reviewer's latest verdict, newer than the head commit."""
    commits = view.get("commits") or []
    head = str(commits[-1].get("committedDate") or "") if commits else ""
    latest = {}
    for rv in sorted(view.get("reviews") or [], key=lambda x: str(x.get("submittedAt") or "")):
        if rv.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[(rv.get("author") or {}).get("login")] = rv
    return any(rv["state"] == "CHANGES_REQUESTED" and str(rv.get("submittedAt") or "") > head
               for rv in latest.values())


def pr_number(ph):
    """A phase's PR number from its `pr` field (#12 or gh-12), else None."""
    m = re.search(r"(\d+)\s*$", (ph or {}).get("pr") or "")
    return int(m.group(1)) if m else None


def pr_from_where(s, ph, skip=()):
    """The phase's PR: its `pr` field, else the open PR from the phase branch (or this branch). skip: the PRs of
    phases this run finished, since their branch can still be checked out when the next phase starts."""
    n = pr_number(ph)
    if n is not None:
        return n
    heads = {p.get("headRefName"): p.get("number") for p in s.get("prs") or []
             if isinstance(p, dict) and p.get("number") not in skip}
    return heads.get((ph or {}).get("branch") or (s.get("git") or {}).get("branch"))


def phase_pr(r, ph):
    """gh's number, state and baseRefName for a phase's PR (by number, else by branch), or {} when gh cannot say."""
    n = pr_number(ph)
    ref = str(n) if n is not None else (ph or {}).get("branch") or ""
    if not ref or not r.gh:
        return {}
    view = pr_view(r, ref, "number,state,baseRefName") or {}
    if n is not None:
        view.setdefault("number", n)
    return view


def settle(r, base, why):
    """(base, why) with a stacked base followed down while its phase's PR has merged: that branch may be
    gone, and what it merged into holds its work."""
    owner = {p.get("branch"): p for p in r.phases if p.get("branch")}
    first, seen = base, set()
    while base in owner and base not in seen:
        seen.add(base)
        v = phase_pr(r, owner[base])
        if str(v.get("state") or "").upper() != "MERGED" or not v.get("baseRefName"):
            break
        hop = f"{owner[base]['label']}'s PR #{v.get('number')} merged into it"
        base = v["baseRefName"]
    return (base, why) if base == first else (base, f"{hop}; {why} is {first}")


def pr_base(r, s):
    """(branch, source): what the run's PR targets. The phase's base (settled), else the open PR's base for
    the phase branch (or this branch), else in --plan the phase this run just finished, else origin/HEAD,
    else PROD. ("", "") when nothing names it. Sets r.base_arg when /yah:resume cannot read the base from
    the plan: a settled stacked base, or a stack on the previous phase."""
    r.base_arg = ""
    plan_base = (r.phase or {}).get("base")
    if plan_base:
        base, why = settle(r, plan_base, "the plan's base for " + r.label)
        r.base_arg = "" if base == plan_base else base
        return base, why
    mine = (r.phase or {}).get("branch") or (s.get("git") or {}).get("branch")
    done = {d[1] for d in r.done}  # a finished phase's branch can still be checked out: its PR is not this one's
    for p in s.get("prs") or []:
        if isinstance(p, dict) and p.get("baseRefName") and p.get("number") not in done \
                and (p.get("number") == r.pr or p.get("headRefName") == mine):
            return p["baseRefName"], f"the base of PR #{p.get('number')}"
    if r.prev:  # what evaluate last read of the phase this run just finished
        name = f"{r.prev['label']}'s PR {r.prev['pr']}"
        own = (r.phase or {}).get("branch")
        if r.prev["pr_state"] == "OPEN" and r.prev.get("branch") and r.prev["branch"] != own:
            r.base_arg = r.prev["branch"]
            return r.base_arg, f"stacked on {name}, still open"
        if r.prev["pr_state"] == "MERGED" and r.prev.get("merged_into"):
            r.base_arg = r.prev["merged_into"]
            return r.base_arg, f"where {name} merged"
    ref = (run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], r.top, timeout=3) or "").strip()
    if ref.startswith("origin/"):
        return ref[7:], "origin/HEAD"
    return (r.prod, "prod in config.json") if r.prod else ("", "")


def refresh(r, s=None):
    """Re-read the target phase, NEXT and HEAD, the protected branches, and the PR while none is known."""
    s = s or where.collect(r.top, use_gh=bool(r.gh) and not r.pr) or {}
    guard(r, s.get("protected") or [])
    b = s.get("state") or {}
    r.phases = plan_phases(r, b)
    if r.label is None and not r.target and r.phases:  # empty TARGET: pin the phase that is current now
        r.label = (b.get("phase") or b.get("next_phase") or {}).get("label")
    r.phase = next((p for p in r.phases if p.get("label") == r.label), None) if r.label else None
    r.next = (r.phase or {}).get("next") or (s.get("state_md") or {}).get("next") or ""
    r.head = (run(["git", "rev-parse", "--short", "HEAD"], r.top, timeout=5) or "").strip() or "?"
    if not r.pr:
        r.pr = pr_from_where(s, r.phase, {d[1] for d in r.done})


def plan_phases(r, b):
    """The phases of the plan this run started on (pinned at the first read). Once that plan has no open phase,
    where.py shows the next `## Plan:` with one, and its P<n> labels are not this run's."""
    plan = b.get("plan") or {}
    key = (plan.get("id"), plan.get("spec") or plan.get("title"))
    if r.plan_key is None:
        r.plan_key = key
    return [p for p in b.get("phases") or [] if isinstance(p, dict)] if key == r.plan_key else []


def week_usage(r):
    """(used %, pace %) from the stream's rate_limit_event, else from a limits.json under 6 h old."""
    now = time.time()
    if r.week:
        return r.week[0], week_pace(r.week[1], now)
    lim = read_json(data_dir() / "limits.json", {})
    wk = (lim.get("seven_day") or {}) if isinstance(lim, dict) else {}
    if wk.get("used_pct") is None or now - num(lim.get("ts"), 0) >= 6 * 3600:
        return None
    pace = week_pace(wk.get("resets_at"), now)
    return num(wk["used_pct"], 0), lim.get("pace_pct") if pace is None else pace


def needs_you(r):
    it = r.last
    if it["denials"]:
        d = it["denials"][0] if isinstance(it["denials"][0], dict) else {}
        return "permission denied: {} {}".format(d.get("tool_name", "?"), brief(d.get("tool_input"), 120))
    if it["is_error"]:
        return "iteration {} ended with an error: {}".format(it["n"], it["error"])
    if it["word"] == "needs-human":
        q = r.next if r.next.startswith("NEEDS-HUMAN:") else "the session asked for you; see NEXT"
        return "needs you: " + q
    return "blocked: " + (it["tag"][len("blocked"):].strip() or "no reason given")


def stopped(it, errors=True):
    """The session ended on a denial, needs-human or blocked, or (with errors) an error: needs_you says which."""
    it = it or {}
    return bool((errors and it.get("is_error")) or it.get("denials") or it.get("word") in ("needs-human", "blocked"))


def evaluate(r, wait=True):
    """The stop rules, first match wins: (exit code, reason), or (None, "") to run again.
    Also sets r.mode for the next iteration."""
    r.mode, r.checks, r.view = "build", "", None
    if r.pr and r.gh:
        view = r.view = pr_view(r)
        state = r.pr_state = str((view or {}).get("state") or "").upper()
        r.url = (view or {}).get("url") or r.url
        if (view or {}).get("baseRefName"):  # the PR's own base: protect it, and it is what the PR targets
            guard(r, [view["baseRefName"]])
            r.base, r.base_from, r.base_arg = view["baseRefName"], f"the base of PR #{r.pr}", ""
        if state in ("MERGED", "CLOSED"):
            return 6, f"PR #{r.pr} is {state.lower()}."
        if state == "OPEN":
            r.checks, cr = checks(r, wait), changes_requested(view)
            phase_done = r.target.startswith("#") or r.phase is None or r.phase.get("status") == "closed"
            if r.checks in ("pass", "none") and not cr and phase_done:
                return 0, f"PR #{r.pr} is open and green. The merge is yours."
            r.mode = "fix-checks" if r.checks == "fail" else "address-review" if cr else "build"
    if r.last:
        if stopped(r.last):
            return 2, needs_you(r)
        if not r.last["tag"]:
            return 2, NO_TAG
        if r.stalls >= 2:
            return 3, "HEAD and NEXT unchanged for 2 iterations in a row."
    if r.n >= r.cap:
        return 4, f"reached the cap of {r.cap} iterations."
    if time.monotonic() - r.t0 > r.cfg["run_total_hours"] * 3600:
        return 4, f"passed run_total_hours ({r.cfg['run_total_hours']:g} h)."
    usage = week_usage(r)
    if usage is None:
        if not r.pace_logged:
            say("[yah] pace unknown: no rate-limit event yet and no fresh limits.json")
            log(r, "pace unknown")
            r.pace_logged = True
        return None, ""
    used, pace = usage
    stop, slack = r.cfg["run_week_stop_pct"], r.cfg["pace_slack"]
    if used >= stop:
        return 7, f"weekly usage {used:.0f}% is at or over run_week_stop_pct ({stop:g}%)."
    if pace is not None and used > num(pace, 0) + slack:
        return 7, f"weekly usage {used:.0f}% is over pace {num(pace, 0):.0f}% + {slack:g}."
    return None, ""


# ---------------------------------------------------------------- auto-merge (config auto_merge, off by default)

def tell(r, line):
    say("[yah] " + line)
    log(r, line)


def fresh_view(r):
    """The PR re-read once its checks passed: evaluate's view can predate a checks wait of up to
    run_checks_wait_minutes, and GitHub says mergeable UNKNOWN until it has worked it out."""
    view = pr_view(r)
    for wait in (2, 4, 8):  # GitHub settles in seconds
        if "UNKNOWN" not in ((view or {}).get("mergeable"), (view or {}).get("mergeStateStatus")):
            break
        time.sleep(min(wait, POLL_S))
        view = pr_view(r)
    return view


def mergeable(r, view):
    """(ok, reason): may the driver merge the run's PR? Every rule must hold; view is a gh pr view of it."""
    ph = r.phase
    if str(view.get("state") or "").upper() != "OPEN":
        return False, f"PR #{r.pr} is {str(view.get('state') or 'unknown').lower()}, not open"
    if ph is None:
        return False, f"PR #{r.pr} is not a plan phase's PR"
    if pr_number(ph) != r.pr and not (ph.get("branch") and view.get("headRefName") == ph["branch"]):
        return False, f"PR #{r.pr} is not {r.label}'s PR in the plan"
    if r.checks != "pass":
        return False, "no checks ran" if r.checks == "none" else f"checks {r.checks or 'unknown'}"
    if view.get("isDraft") is not False:
        return False, "it is a draft"
    if view.get("mergeable") != "MERGEABLE":
        return False, f"mergeable is {view.get('mergeable') or 'unknown'}"
    if view.get("mergeStateStatus") not in ("CLEAN", "HAS_HOOKS"):
        return False, f"merge state {view.get('mergeStateStatus') or 'unknown'}"
    if changes_requested(view):
        return False, "changes requested"
    if not (view.get("headRefName") and view.get("baseRefName") and view.get("headRefOid")):
        return False, "gh did not name its head, base and head commit"
    # A stacked PR merges into the phase below it, open or merged, and so never reaches the plan's base.
    stack = ({p.get("branch") for p in r.phases if p.get("label") != r.label} | {(r.prev or {}).get("branch")}) - {"", None}
    if view["baseRefName"] in stack:
        return False, f"it targets {view['baseRefName']}, another phase's branch"
    if not r.login and r.gh:  # cached once known
        rc, out, _ = gh(r, "api", "user", "--jq", ".login")
        r.login = out.strip() if rc == 0 else ""
    author = (view.get("author") or {}).get("login") or "unknown"
    if not r.login:
        return False, "gh api user did not say who you are"
    if author != r.login:
        return False, f"its author {author} is not you ({r.login})"
    return True, ""


def merge(r, view):
    """Merge commit pinned to the head gh showed, then retarget the open PRs on the head to the base, then
    delete the head branch unless protected. (None, "") once merged, else (2, reason). Never squash, rebase,
    --admin, --auto or --delete-branch: GitHub closes a PR whose base branch is deleted, so retarget comes first."""
    head, base, oid = view["headRefName"], view["baseRefName"], view["headRefOid"]
    rc, out, err = gh(r, "pr", "merge", str(r.pr), "--merge", "--match-head-commit", oid, timeout=60)
    if rc is None:
        return 2, f"gh pr merge {r.pr} did not answer in 60 s; check whether PR #{r.pr} merged."
    if rc != 0:
        return 2, f"auto-merge of PR #{r.pr} failed: {brief(err or out, 300) or f'gh exit {rc}'}"
    tell(r, f"merged PR #{r.pr} into {base} (merge commit of {oid[:7]})")
    rc, out, err = gh(r, "pr", "list", "--base", head, "--state", "open", "--json", "number")
    try:
        stacked = [int(p["number"]) for p in json.loads(out)] if rc == 0 else None
    except (ValueError, TypeError, KeyError):
        stacked = None
    if stacked is None:
        return 2, (f"PR #{r.pr} merged, but the open PRs based on {head} could not be listed "
                   f"({brief(err, 200) or 'gh failed'}). Head branch {head} kept.")
    for m in stacked:
        rc, out, err = gh(r, "pr", "edit", str(m), "--base", base)
        if rc != 0:
            return 2, (f"PR #{r.pr} merged, but #{m} could not be retargeted from {head} to {base} "
                       f"({brief(err or out, 200) or 'gh failed'}). Head branch {head} kept.")
        tell(r, f"retargeted PR #{m} from {head} to {base}")
    if head in r.protected:  # protected() always holds main, master and PROD
        tell(r, f"kept {head}: a protected branch is never deleted")
        return None, ""
    rc, out, err = gh(r, "api", "-X", "DELETE", "repos/{owner}/{repo}/git/refs/heads/" + quote(head, safe="/"))
    tell(r, f"deleted branch {head} on origin" if rc == 0 else
         f"warning: could not delete {head} on origin ({brief(err or out, 200) or 'gh failed'})")
    return None, ""


def auto_merge(r, reason):
    """evaluate said open and green (exit 0) and auto_merge is on: merge when mergeable() allows. (code, reason):
    8 once merged (--plan goes on from its base; /yah:resume fetches it), 2 when a merge step failed, else 0
    with the skip reason."""
    if stopped(r.last):
        ok, why = False, "the last session stopped: " + needs_you(r)
    elif r.phase is None or r.checks != "pass":  # a fresh read changes neither: skip its wait
        ok, why = mergeable(r, r.view or {})
    else:
        seen = (r.view or {}).get("headRefOid")
        r.view = fresh_view(r) or {}
        ok, why = mergeable(r, r.view)
        if ok and r.view.get("headRefOid") != seen:
            ok, why = False, "a new commit landed after its checks passed"
    if not ok:
        if r.chain:  # advance's own reason replaces this one
            tell(r, f"PR #{r.pr}: auto-merge skipped: {why}.")
        return 0, f"{reason} Auto-merge skipped: {why}."
    code, why = merge(r, r.view)
    if code is not None:
        return code, why
    r.pr_state, r.base = "MERGED", r.view["baseRefName"]
    return 8, f"PR #{r.pr} merged by yah (merge commit)."


# ---------------------------------------------------------------- one iteration

def descendants(pid):
    """Every descendant of pid, from `ps`. It finds children that started their own session (the
    Bash tool's shells do), which killpg misses."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, timeout=10).stdout
    except Exception:
        return []
    kids = {}
    for line in out.decode("ascii", "replace").splitlines():
        f = line.split()
        if len(f) == 2 and f[0].isdigit() and f[1].isdigit():
            kids.setdefault(int(f[1]), []).append(int(f[0]))
    found, todo = [], [pid]
    while todo:
        for kid in kids.get(todo.pop(), []):
            if kid not in found:
                found.append(kid)
                todo.append(kid)
    return found


def end_tree(pid, freeze=False):
    """Kill pid and every process under it (taskkill /T on Windows; elsewhere its descendants from `ps`, then its
    process group). freeze stops pid first, so it starts nothing new while its tree is found."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=15,
                           creationflags=NO_WINDOW)
            return
        if freeze:
            os.kill(pid, signal.SIGSTOP)
        leader = os.getpgid(pid) == pid  # read before the kill: its group holds children reparented away from it
        for p in descendants(pid) + [pid]:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        if leader:
            os.killpg(pid, signal.SIGKILL)
    except Exception:
        pass


def kill_tree(proc):
    end_tree(proc.pid)
    try:
        proc.kill()
    except Exception:
        pass


def on_event(ev, it, r):
    kind = ev.get("type")
    if kind == "system" and ev.get("subtype") == "init" and ev.get("permissionMode") not in (None, "auto"):
        it["bad_mode"] = ev.get("permissionMode")
    elif kind == "assistant":
        for c in (ev.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                say(f"  {c.get('name')} {brief(c.get('input'))}")
    elif kind == "rate_limit_event":
        wk = ((ev.get("rate_limit_info") or {}).get("unifiedWindows") or {}).get("seven_day") or {}
        u = wk.get("utilization")
        if isinstance(u, (int, float)):
            r.week = (u * 100 if u <= 1 else u, wk.get("resetsAt"))
    elif kind == "result":
        text = ev.get("result") if isinstance(ev.get("result"), str) else ""
        tags = TAG.findall(text)
        it.update(done=True, is_error=bool(ev.get("is_error")), cost=num(ev.get("total_cost_usd"), 0),
                  turns=int(num(ev.get("num_turns"), 0)), denials=ev.get("permission_denials") or [],
                  tag=tags[-1] if tags else "",
                  error="{}: {}".format(ev.get("subtype") or "error", brief(text, 200)))


def read_stream(proc, raw, it, r):
    try:
        for line in iter(proc.stdout.readline, b""):
            raw.write(line)
            text = line.decode("utf-8", "replace").strip()
            try:
                ev = json.loads(text)
            except ValueError:
                if text:
                    say("  ! " + text[:200])
                continue
            if isinstance(ev, dict):
                on_event(ev, it, r)
                if it.get("bad_mode"):
                    kill_tree(proc)
                    break
    except Exception:
        pass


def iterate(r):
    """One headless session. Returns its summary dict. r.n counts the phase's sessions, r.total the run's."""
    r.n += 1
    r.total += 1
    argv = claude_argv(r)
    say(f"[yah] iteration {r.n}/{r.cap}: {argv[2]}")
    it = {"n": r.n, "done": False, "is_error": True, "cost": 0.0, "turns": 0, "denials": [], "tag": "", "error": ""}
    left = r.cfg["run_total_hours"] * 60 - (time.monotonic() - r.t0) / 60
    limit = min(r.cfg["run_iteration_minutes"], max(left, 0.1))
    kw = {} if os.name == "nt" else {"start_new_session": True}
    timed_out = False
    with open(r.log.with_name(f"{r.log.stem}-{r.total}.jsonl"), "wb") as raw:
        try:
            proc = subprocess.Popen(command(argv[0], argv[1:]), cwd=r.top, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    env=dict(os.environ, YAH_PROTECTED=",".join(r.protected)), **kw)
        except OSError as e:
            return dict(it, error=f"could not start claude: {e}", word="")
        reader = threading.Thread(target=read_stream, args=(proc, raw, it, r), daemon=True)
        reader.start()
        try:
            proc.wait(timeout=limit * 60)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc)
        except KeyboardInterrupt:
            kill_tree(proc)
            raise
        try:
            proc.wait(timeout=15)
        except Exception:
            pass
        reader.join(10)
    if it.get("bad_mode"):
        it.update(done=False, is_error=True, error=f"the session started in permission mode '{it['bad_mode']}', not "
                  "auto, so every edit would be denied. Auto mode is not available for this model or account; "
                  "try without --model, or run the phase interactively.")
    elif not it["done"]:
        it["error"] = f"timed out after {limit:g} min; the process tree was killed" if timed_out else \
            f"no result event (claude exit code {proc.returncode})"
    it["word"] = it["tag"].split()[0].lower() if it["tag"] else ""
    m = re.match(r"pr-open\s+#?(\d+)", it["tag"], re.I)
    if m:
        r.pr = int(m.group(1))
    r.cost += it["cost"]
    return it


# ---------------------------------------------------------------- stop

def log(r, line):
    if r.log is None:
        return
    try:
        with open(r.log, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except Exception:
        pass


def prune(runs):
    """Keep the KEEP_RUNS newest runs: each .log, its --detach .out and its per-iteration .jsonl files."""
    try:
        logs = sorted(runs.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in logs[KEEP_RUNS:]:
            for f in [old, old.with_suffix(".out"), *runs.glob(old.stem + "-*.jsonl")]:
                f.unlink(missing_ok=True)
    except Exception:
        pass


def notify(title, msg):
    """A desktop notification plus a terminal bell. Failures are ignored."""
    print("\a", end="", flush=True)
    try:
        if sys.platform == "darwin":
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
            cmd = [shutil.which("osascript") or "osascript", "-e",
                   f'display notification "{esc(msg)}" with title "{esc(title)}"']
        elif os.name == "nt":
            ps = shutil.which("powershell")
            if not ps:
                return
            q = lambda s: s.replace('"', "'").replace("'", "''")  # noqa: E731
            script = ("Add-Type -AssemblyName System.Windows.Forms, System.Drawing; "
                      "$n = New-Object System.Windows.Forms.NotifyIcon; "
                      "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
                      f"$n.ShowBalloonTip(5000, '{q(title)}', '{q(msg)}', 'Info'); "
                      "Start-Sleep -Seconds 6; $n.Dispose()")
            cmd = command(ps, ["-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script])
        else:
            ns = shutil.which("notify-send")
            if not ns:
                return
            cmd = [ns, title, msg]
        subprocess.run(cmd, capture_output=True, timeout=10, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except Exception:
        pass


def goes_on(r, code):
    """--plan: the stop rule says the phase is done, so the run goes on: its PR open and green or merged by yah,
    or merged and the phase closed. A closed PR, or a merged one whose phase is still open, stops the run."""
    return bool(r.chain) and (code in (0, 8) or (code == 6 and r.pr_state == "MERGED"
                                                 and (r.phase or {}).get("status") == "closed"))


def advance(r, code, reason):
    """--plan: the stop rule said the phase is done (its PR open and green, or merged, and the phase closed),
    so pin the next open phase and go on. (None, "") to run again, else the (code, reason) to stop with."""
    if not goes_on(r, code):
        return code, reason
    merged = r.pr_state == "MERGED"
    if stopped(r.last, errors=False):  # evaluate reads the PR before these; an error after the phase closed is not one
        return 2, f"{needs_you(r)} ({r.label}'s PR #{r.pr} is {'merged' if merged else 'open and green'}.)"
    r.done.append((r.label, r.pr, "merged by yah" if code == 8 else "merged" if merged else "green"))
    s = where.collect(r.top, use_gh=bool(r.gh)) or {}
    done = [d[0] for d in r.done]
    nxt = next((p for p in plan_phases(r, s.get("state") or {})
                if p.get("status") != "closed" and p.get("label") not in done), None)
    parts = ", ".join(f"{label} PR #{pr} {how}" for label, pr, how in r.done)
    if nxt is None:
        return 0, f"plan done: {parts}." + (" The merges are yours." if any(d[2] == "green" for d in r.done) else "")
    human = {h.get("id") for h in (s.get("state") or {}).get("human") or [] if isinstance(h, dict)}
    if nxt.get("id") in human:
        return 2, f"needs you: {nxt['label']} ({nxt.get('title')}) is marked (you). So far: {parts}."
    # A phase line without `| branch` still has a PR head: stack on it and protect it, never reuse it for the next.
    head = (r.phase or {}).get("branch") or (r.view or {}).get("headRefName") or ""
    r.prev = dict(r.phase or {}, label=r.label, branch=head, pr=f"#{r.pr}", pr_state=r.pr_state, merged_into=r.base)
    r.label = r.target = nxt["label"]
    r.pr, r.url, r.pr_state, r.n, r.stalls, r.last, r.waiting = None, "", "", 0, 0, None, False
    refresh(r, s)
    r.base, r.base_from = pr_base(r, s)
    # Nothing in this run pushes the finished phase's branch again, nor the new phase's base.
    guard(r, [r.base_arg] + ([r.prev.get("branch")] if r.prev.get("branch") != (r.phase or {}).get("branch") else []))
    tell(r, "{} done: PR {} {}. Next: {} on {}".format(
        r.prev["label"], r.prev["pr"], r.done[-1][2].replace("green", "open and green"), r.label,
        f"{r.base} ({r.base_from})" if r.base else "no base"))
    return None, ""


def finish(r, code, reason):
    log(r, f"stop, exit {code}: {reason} Cost ${r.cost:.2f} over {r.total} iterations.")
    notify(f"yah run {r.key}", reason)
    say(f"[yah] stop (exit {code}): {reason}")
    say(f"[yah] PR {r.url or (f'#{r.pr}' if r.pr else 'none')} | cost ${r.cost:.2f} | "
        f"{r.total} iterations | log {r.log}")


# ---------------------------------------------------------------- main

def refuse(reason):
    say(f"[yah] run refused: {reason}")
    return 5


def busy(st):
    st = st or {}
    since = time.strftime("%H:%M", time.localtime(st["started"])) if st.get("started") else "?"
    return (f"a run is already going in this checkout: pid {st.get('pid', '?')}, {st.get('target') or 'current phase'}"
            f", since {since}, log {st.get('log', '?')}. Let it finish, or end it with `yah run --stop`.")


def stop_run(pidf):
    """--stop: end the checkout's run and everything under it, then write its exit code for the RUN line."""
    st = run_state(pidf)
    if not st or not st.get("alive"):
        say("[yah] no run is going in this checkout.")
        return 0
    pid, what = int(num(st.get("pid"), 0)), st.get("target") or "current phase"
    if pid > 0:
        end_tree(pid, freeze=True)
    t = time.monotonic()
    while st.get("alive") and time.monotonic() - t < STOP_WAIT_S:
        time.sleep(0.1)
        st = run_state(pidf) or st  # None: caught mid-write
    if st.get("alive"):
        say(f"[yah] the run (pid {pid}, {what}) still holds its lock after {STOP_WAIT_S} s. End that pid by hand.")
        return 1
    if "code" not in st:  # it may have ended on its own just now: then its own code stands
        st.pop("alive", None)
        end_run(pidf, dict(st, ended=int(time.time()), code=130, reason=STOPPED))
        log(SimpleNamespace(log=st.get("log") or None), f"stop, exit 130: {STOPPED}")
    say(f"[yah] run ended: pid {pid}, {what}, log {st.get('log', '?')}")
    return 0


def detach(r, pidf, stem):
    """--detach: start this same command in the background, where closing the terminal or ending the session does not
    reach it, and return once it holds the run lock. The child knows itself by YAH_RUN_DETACHED, its log stem."""
    st = run_state(pidf)
    if st and st.get("alive"):
        return refuse(busy(st))
    out = Path(f"{stem}.out")
    # DETACHED_PROCESS would leave it no console, so every claude, gh and git it starts would open a window.
    kw = ({"creationflags": NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
          else {"start_new_session": True})
    with open(out, "wb") as f:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
                                stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                                env=dict(os.environ, YAH_RUN_DETACHED=str(stem)), **kw)
    t = time.monotonic()
    while time.monotonic() - t < DETACH_WAIT_S:
        st = run_state(pidf)
        if st and st.get("alive") and st.get("stem") == str(stem):  # not proc.pid: a venv python.exe is a launcher
            say(f"[yah] run started in the background: {r.label or r.target or 'current phase'}, pid {st.get('pid')}")
            say(f"[yah] log {st.get('log')} | output {out}")
            return 0
        code = proc.poll()
        if code is not None:  # it stopped before the caller saw it hold the lock: a refusal, or a run already done
            say(out.read_text("utf-8", "replace").strip())
            return code
        time.sleep(0.1)
    say(f"[yah] the background run (pid {proc.pid}) has not taken its lock after {DETACH_WAIT_S} s. Output: {out}")
    return 1


def no_base(r):
    who = f"phase {r.label}'s PR" if r.label else f"PR #{r.pr}" if r.pr else "this run's PR"
    return NO_BASE.format(who, (r.phase or {}).get("branch") or r.branch or "this branch")


def dry_run(r):
    r.pace_logged = True  # the week line says it instead
    code, reason = evaluate(r, wait=False)
    if code is None and not r.base:
        code, reason = 5, "run refused: " + no_base(r)
    if goes_on(r, code):
        reason += " With --plan, a done phase goes on to the next open one."
    c, usage = r.cfg, week_usage(r)
    phase = f"phase {r.label}" if r.label else "no plan phase"
    say("[yah] dry run: nothing is spawned")
    say(f"repo    {r.top}  branch {r.branch}  project {r.key}")
    say(f"target  {r.target or 'current phase'} ({phase})  PR {f'#{r.pr}' if r.pr else 'none yet'}"
        + (f"  {r.url}" if r.url else ""))
    say(f"base    {r.base} ({r.base_from})" if r.base else "base    unknown")
    if r.chain:
        todo, prev, steps = [p for p in r.phases if p.get("status") != "closed"], "", []
        for p in todo:
            base = r.base if p.get("label") == r.label else p.get("base") or (f"{prev}'s branch" if prev else "?")
            steps.append(f"{p.get('label')} {p.get('branch') or '(new branch)'} <- {base}")
            prev = p.get("label")
        say(f"plan    {' | '.join(steps)}; each phase until its PR is green, then the next")
    say(f"plugin  {r.plugin_src}")
    say(f"protect {', '.join(r.protected)}")
    say(f"argv    {shlex.join(claude_argv(r))}")
    say(f"deny    {len(r.deny)} patterns")
    for p in r.deny:
        say(f"          {p}")
    say(f"caps    {r.cap} iterations, ${r.budget:g} per iteration, {c['run_iteration_minutes']:g} min per "
        f"iteration, {c['run_total_hours']:g} h total, checks wait {c['run_checks_wait_minutes']:g} min, "
        f"week stop {c['run_week_stop_pct']:g}% or pace + {c['pace_slack']:g}")
    say("week    pace unknown" if usage is None else f"week    {usage[0]:.0f}% used"
        + ("" if usage[1] is None else f", pace {num(usage[1], 0):.0f}%"))
    am = "on" if c["auto_merge"] else "off"
    if c["auto_merge"] and code == 0 and r.pr_state == "OPEN":  # open and green: what it would do, never doing it
        ok, why = mergeable(r, r.view or {})
        am += f": would merge PR #{r.pr} (merge commit)" if ok else f": would not merge PR #{r.pr}: {why}"
    say(f"merge   auto-merge {am}")
    say(f"now     would stop, exit {code}: {reason}" if code is not None else
        f"now     would run iteration 1 in MODE {r.mode}" + (f" (checks {r.checks})" if r.checks else ""))
    return 0


def main():
    utf8_stdout()
    ap = argparse.ArgumentParser(prog="yah run", description="Chain headless /yah:resume sessions until the "
                                 "phase's PR is open and green. It merges only with auto_merge on in config.json.")
    ap.add_argument("target", nargs="?", default="", help="P<n>, #<pr> (or the bare number), or empty")
    ap.add_argument("--plan", action="store_true", help="after the phase's PR is green, go on to the next open "
                    "phase until the plan is done")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--cwd", help="run in this repo")
    grp.add_argument("--project", help="run in this project (config.json or seen by where.py)")
    ap.add_argument("--iterations", type=int, help="max sessions per phase (config run_iterations)")
    ap.add_argument("--budget", type=float, help="--max-budget-usd per session (config run_budget_usd)")
    ap.add_argument("--dry-run", action="store_true", help="show the argv, DENY list, caps and evaluation")
    ap.add_argument("--model", help="passed to claude as --model")
    ap.add_argument("--plugin-dir", help="passed to claude as --plugin-dir (testing an uninstalled yah checkout)")
    ap.add_argument("--detach", action="store_true", help="run in the background, output to <data dir>/runs/, and "
                    "return once it has started")
    ap.add_argument("--stop", action="store_true", help="end the run going in this checkout, and every process "
                    "under it")
    a = ap.parse_args()
    if a.stop and (a.target or a.plan or a.detach or a.dry_run or a.iterations is not None or a.budget is not None
                   or a.model or a.plugin_dir):
        return refuse("--stop takes only --cwd or --project.")
    cfg = config()
    target = parse_target(a.target)
    if target is None:
        return refuse(f"TARGET must be P<n>, #<pr> or empty, not '{a.target}'.")
    if a.plan and target[:1] == "#":
        return refuse("--plan follows the plan's phases, so TARGET must be P<n> or empty, not a PR.")
    cwd = a.cwd or os.getcwd()
    if a.project:
        projects = where.known_projects()
        hits = where.named(projects, a.project)
        if len(hits) > 1:
            return refuse(f"'{a.project}' matches {len(hits)} repos: {', '.join(hits)}. Pass --cwd DIR instead.")
        if not hits:
            known = ", ".join(sorted({k for k, _ in projects})) or "none"
            return refuse(f"no project named '{a.project}'. Known: {known}.")
        cwd = hits[0]
    top, main_root, git_dir = find_git(cwd)
    if top is None:
        return refuse(f"not inside a git repo: {cwd}")
    key = project_key(main_root)
    pidf = run_pid_path(key, str(top), main_root)
    if a.stop:
        return stop_run(pidf)
    pcfg = cfg["projects"].get(key) if isinstance(cfg["projects"].get(key), dict) else {}
    trunks = pcfg.get("trunks") or []
    trunks = where.DEFAULT_TRUNKS | set([trunks] if isinstance(trunks, str) else trunks)
    prod = prod_branch(str(pcfg.get("prod") or ""))
    branch = read_branch(git_dir) if git_dir else None
    gh_path = find_tool("gh")
    # where.py's protected set covers a repo with no config.json: the plan's phase bases, where open PRs land
    # (or landed when gh last saw any), origin/HEAD and the branch HEAD was cut from, beside the trunks.
    s = where.collect(str(top), use_gh=bool(gh_path)) or {}
    fence = protected(trunks | set(s.get("protected") or []), prod)
    if not target and not a.plan and branch in fence:  # --plan pins its phase from the plan, like P<n>
        return refuse(f"you are on {branch}, a trunk or the PROD branch. Check out the phase branch first, "
                      "or pass P<n> and resume creates it from base.")
    claude = shutil.which("claude")
    if not claude:
        return refuse("claude not found on PATH.")
    if not gh_path:
        return refuse("gh (the GitHub CLI) not found. yah run needs it to open the PR and follow its checks.")
    plugin_dir, plugin_src = plugin_source(a.plugin_dir)
    r = SimpleNamespace(
        cfg=cfg, top=str(top), key=key, branch=branch, target=target, claude=claude, gh=gh_path, model=a.model,
        plugin_dir=plugin_dir, plugin_src=plugin_src, prod=prod, base="", base_from="",
        deny=deny_list(fence), protected=fence,
        cap=int(cfg["run_iterations"] if a.iterations is None else a.iterations),
        budget=cfg["run_budget_usd"] if a.budget is None else a.budget,
        pr=int(target[1:]) if target[:1] == "#" else None, label=target if target[:1] == "P" else None,
        phase=None, phases=[], plan_key=None, next="", head="", mode="build", checks="", url="", waiting=False,
        week=None, pace_logged=False, n=0, total=0, cost=0.0, last=None, stalls=0, t0=time.monotonic(), log=None,
        chain=a.plan, prev=None, done=[], pr_state="", base_arg="", view=None, login="")
    refresh(r, s)
    if (target[:1] == "P" or a.plan) and r.phase is None:
        what = f"no phase {target} in this repo's plan. " if target else "--plan found no open phase in this repo's plan. "
        labels = [p.get("label") for p in r.phases]
        return refuse(what + (f"Phases: {', '.join(labels)}." if labels
                              else "There is no plan in STATE.md (or beads, if you already use it)."))
    r.base, r.base_from = pr_base(r, s)
    guard(r, [r.base_arg])  # a base the plan does not name is protected like one it does
    if not r.base and not r.pr:  # a known PR names its base once evaluate reads it
        return refuse(no_base(r))
    if a.dry_run:
        return dry_run(r)
    runs = data_dir() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    child = os.environ.pop("YAH_RUN_DETACHED", "")  # popped, so no claude session it starts inherits it
    stem = Path(child) if child else runs / "{}-{}".format(re.sub(r"[^\w.-]", "_", key), time.strftime("%Y%m%d-%H%M%S"))
    if a.detach and not child:
        return detach(r, pidf, stem)
    r.log = Path(f"{stem}.log")
    info = {"pid": os.getpid(), "stem": str(stem), "started": int(time.time()), "top": r.top, "branch": branch,
            "target": r.label or target, "plan": a.plan, "log": str(r.log), "out": f"{stem}.out" if child else ""}
    hold = hold_run(pidf, info)
    if hold is None:
        return refuse(busy(run_state(pidf)))
    r.log.touch()
    prune(runs)
    try:
        while True:
            code, reason = evaluate(r)
            if code == 0 and r.cfg["auto_merge"]:
                code, reason = auto_merge(r, reason)
            if code is not None:
                code, reason = advance(r, code, reason)
                if code is None:  # a new phase: its own stop rules (hours, week, its PR) before its first session
                    info["target"] = r.label  # the RUN line names the phase it is on
                    write_run(hold, info)
                    continue
                break
            if not r.base:  # the PR gh could not read names no base either
                code, reason = 5, "run refused: " + no_base(r)
                break
            before = (r.head, r.next)
            r.last = iterate(r)
            refresh(r)
            r.stalls = r.stalls + 1 if (r.head, r.next) == before else 0
            it = r.last
            line = "iteration {}: ${:.2f}, {} turns, {}, HEAD {}, NEXT {}".format(
                it["n"], it["cost"], it["turns"], it["tag"] or "no YAH-RESULT", r.head, r.next[:80] or "-")
            tell(r, line)
    except KeyboardInterrupt:
        code, reason = 130, "interrupted."
    except Exception as e:  # a detached run has no terminal: the log, the notification and the pid file say it
        code, reason = 1, f"run.py failed: {e}"
    write_run(hold, dict(info, ended=int(time.time()), code=code, reason=reason))  # first: finish can fail
    finish(r, code, reason)
    return code


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:  # never 0, which means an open, green PR
        print(f"[yah] run.py failed: {e}")
        code = 1
    sys.exit(code or 0)
