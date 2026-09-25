"""run.py: `yah run`, which chains fresh headless sessions until the phase's PR is open and green.

    yah run [TARGET] [--cwd DIR | --project NAME] [--iterations N] [--budget USD] [--dry-run] [--model M]
            [--plugin-dir DIR]

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

    0  PR open, green, phase closed: the merge is yours      4  iteration or wall-clock cap
    2  needs you: error, denial, needs-human, blocked, or    5  refused to start
       a session that ended without a YAH-RESULT line        6  PR merged or closed
    3  stalled: HEAD and NEXT unchanged twice in a row
    7  weekly usage at run_week_stop_pct or over pace + pace_slack
    1  run.py itself failed

Logs go to <data dir>/runs/. The driver writes nothing inside the repo.

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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import where  # noqa: E402
from statusline import week_pace  # noqa: E402
from yahlib import (config, data_dir, find_git, num, project_key, read_branch, read_json,  # noqa: E402
                    utf8_stdout)

NO_WINDOW = where.NO_WINDOW
POLL_S = num(os.environ.get("YAH_RUN_POLL_S"), 30)  # tests shorten the pending-checks poll
KEEP_RUNS = 20
TAG = re.compile(r"^\s*YAH-RESULT:\s*(.+?)\s*$", re.M)
DENY_BASE = ["PowerShell", "Bash(git push --force*)", "Bash(git push -f*)", "Bash(git push *--force*)", "Bash(git push * -f*)",
             "Bash(git push *+*)", "Bash(gh pr merge*)", "Bash(gh api *merge*)", "Bash(gh repo delete*)",
             "Bash(git push *--delete*)", "Bash(git push *--mirror*)", "Bash(git push *--all*)",
             "Bash(git push *--prune*)", "Bash(git push -d*)", "Bash(git push * -d *)"]
DENY_BRANCH = ["Bash(git push * {})", "Bash(git push * {} *)", "Bash(git push * HEAD:{}*)", "Bash(git push * *:{}*)",
               "Bash(git push *refs/heads/{}*)"]
PR_JSON = "state,url,reviewDecision,reviews,commits,headRefName,baseRefName"
NO_TAG = "session ended without a YAH-RESULT line; is the yah plugin loaded? (--plugin-dir)"
NO_BASE = ("cannot tell which branch {} targets: no base in the plan, no open PR for {}, no origin/HEAD and no "
           "prod in config.json, so it cannot be protected. Set base in the plan (beads metadata, or "
           "`| base <branch>` on the STATE.md phase line) or prod in config.json.")


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
    prompt = " ".join(x for x in ("/yah:resume", r.target or r.label, r.mode) if x)
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

def gh(r, *args):
    """(returncode, stdout, stderr); returncode None when gh could not run in 20 s."""
    try:
        p = subprocess.run(command(r.gh, list(args)), cwd=r.top, capture_output=True, timeout=20,
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except Exception:
        return None, "", ""


def pr_view(r):
    rc, out, _ = gh(r, "pr", "view", str(r.pr), "--json", PR_JSON)
    try:
        view = json.loads(out) if rc == 0 else None
    except ValueError:
        return None
    return view if isinstance(view, dict) else None


def checks(r, wait):
    """pass, fail, pending or unknown. Pending (gh exit 8) is polled up to run_checks_wait_minutes."""
    deadline = time.monotonic() + r.cfg["run_checks_wait_minutes"] * 60
    while True:
        rc, out, err = gh(r, "pr", "checks", str(r.pr), "--required")
        if rc not in (0, 8) and "no required checks" in (out + err).lower():
            rc, out, err = gh(r, "pr", "checks", str(r.pr))
        if rc == 0 or (rc is not None and "no checks reported" in (out + err).lower()):
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


def pr_from_where(s, ph):
    m = re.search(r"(\d+)\s*$", (ph or {}).get("pr") or "")
    if m:
        return int(m.group(1))
    heads = {p.get("headRefName"): p.get("number") for p in s.get("prs") or [] if isinstance(p, dict)}
    return heads.get((ph or {}).get("branch") or (s.get("git") or {}).get("branch"))


def pr_base(r, s):
    """(branch, source): what the run's PR targets. The phase's base, else the open PR's base for the
    phase branch (or this branch), else origin/HEAD, else PROD. ("", "") when nothing names it."""
    if (r.phase or {}).get("base"):
        return r.phase["base"], "the plan's base for " + r.label
    mine = (r.phase or {}).get("branch") or (s.get("git") or {}).get("branch")
    for p in s.get("prs") or []:
        if isinstance(p, dict) and p.get("baseRefName") and (p.get("number") == r.pr or p.get("headRefName") == mine):
            return p["baseRefName"], f"the base of PR #{p.get('number')}"
    ref = (where.run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], r.top, timeout=3) or "").strip()
    if ref.startswith("origin/"):
        return ref[7:], "origin/HEAD"
    return (r.prod, "prod in config.json") if r.prod else ("", "")


def refresh(r, s=None):
    """Re-read the target phase, NEXT and HEAD, the protected branches, and the PR while none is known."""
    s = s or where.collect(r.top, use_gh=bool(r.gh) and not r.pr) or {}
    guard(r, s.get("protected") or [])
    b = s.get("beads") or {}
    r.labels = [p.get("label") for p in b.get("phases") or []]
    if r.label is None and not r.target:  # empty TARGET: pin the phase that is current now
        r.label = (b.get("phase") or b.get("next_phase") or {}).get("label")
    r.phase = next((p for p in b.get("phases") or [] if p.get("label") == r.label), None) if r.label else None
    r.next = (r.phase or {}).get("next") or (s.get("state_md") or {}).get("next") or ""
    r.head = (where.run(["git", "rev-parse", "--short", "HEAD"], r.top, timeout=5) or "").strip() or "?"
    if not r.pr:
        r.pr = pr_from_where(s, r.phase)


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


def evaluate(r, wait=True):
    """The stop rules, first match wins: (exit code, reason), or (None, "") to run again.
    Also sets r.mode for the next iteration."""
    r.mode, r.checks = "build", ""
    if r.pr and r.gh:
        view = pr_view(r)
        state = str((view or {}).get("state") or "").upper()
        r.url = (view or {}).get("url") or r.url
        if (view or {}).get("baseRefName"):  # the PR's own base: protect it, and it is what the PR targets
            guard(r, [view["baseRefName"]])
            r.base, r.base_from = view["baseRefName"], f"the base of PR #{r.pr}"
        if state in ("MERGED", "CLOSED"):
            return 6, f"PR #{r.pr} is {state.lower()}."
        if state == "OPEN":
            r.checks, cr = checks(r, wait), changes_requested(view)
            phase_done = r.target.startswith("#") or r.phase is None or r.phase.get("status") == "closed"
            if r.checks == "pass" and not cr and phase_done:
                return 0, f"PR #{r.pr} is open and green. The merge is yours."
            r.mode = "fix-checks" if r.checks == "fail" else "address-review" if cr else "build"
    if r.last:
        if r.last["is_error"] or r.last["denials"] or r.last["word"] in ("needs-human", "blocked"):
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


def kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15,
                           creationflags=NO_WINDOW)
        else:
            for pid in descendants(proc.pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
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
    """One headless session. Returns its summary dict."""
    r.n += 1
    argv = claude_argv(r)
    say(f"[yah] iteration {r.n}/{r.cap}: {argv[2]}")
    it = {"n": r.n, "done": False, "is_error": True, "cost": 0.0, "turns": 0, "denials": [], "tag": "", "error": ""}
    left = r.cfg["run_total_hours"] * 60 - (time.monotonic() - r.t0) / 60
    limit = min(r.cfg["run_iteration_minutes"], max(left, 0.1))
    kw = {} if os.name == "nt" else {"start_new_session": True}
    timed_out = False
    with open(r.log.with_name(f"{r.log.stem}-{r.n}.jsonl"), "wb") as raw:
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
    """Keep the KEEP_RUNS newest runs: each .log and its per-iteration .jsonl files."""
    try:
        logs = sorted(runs.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in logs[KEEP_RUNS:]:
            for f in [old, *runs.glob(old.stem + "-*.jsonl")]:
                f.unlink()
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


def finish(r, code, reason):
    log(r, f"stop, exit {code}: {reason} Cost ${r.cost:.2f} over {r.n} iterations.")
    notify(f"yah run {r.key}", reason)
    say(f"[yah] stop (exit {code}): {reason}")
    say(f"[yah] PR {r.url or (f'#{r.pr}' if r.pr else 'none')} | cost ${r.cost:.2f} | "
        f"{r.n} iterations | log {r.log}")


# ---------------------------------------------------------------- main

def refuse(reason):
    say(f"[yah] run refused: {reason}")
    return 5


def no_base(r):
    who = f"phase {r.label}'s PR" if r.label else f"PR #{r.pr}" if r.pr else "this run's PR"
    return NO_BASE.format(who, (r.phase or {}).get("branch") or r.branch or "this branch")


def dry_run(r):
    r.pace_logged = True  # the week line says it instead
    code, reason = evaluate(r, wait=False)
    if code is None and not r.base:
        code, reason = 5, "run refused: " + no_base(r)
    c, usage = r.cfg, week_usage(r)
    phase = f"phase {r.label}" if r.label else "no plan phase"
    say("[yah] dry run: nothing is spawned")
    say(f"repo    {r.top}  branch {r.branch}  project {r.key}")
    say(f"target  {r.target or 'current phase'} ({phase})  PR {f'#{r.pr}' if r.pr else 'none yet'}"
        + (f"  {r.url}" if r.url else ""))
    say(f"base    {r.base} ({r.base_from})" if r.base else "base    unknown")
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
    say(f"now     would stop, exit {code}: {reason}" if code is not None else
        f"now     would run iteration 1 in MODE {r.mode}" + (f" (checks {r.checks})" if r.checks else ""))
    return 0


def main():
    utf8_stdout()
    ap = argparse.ArgumentParser(prog="yah run", description="Chain headless /yah:resume sessions until the "
                                 "phase's PR is open and green. It never merges.")
    ap.add_argument("target", nargs="?", default="", help="P<n>, #<pr> (or the bare number), or empty")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--cwd", help="run in this repo")
    grp.add_argument("--project", help="run in this project (config.json or seen by where.py)")
    ap.add_argument("--iterations", type=int, help="max sessions (config run_iterations)")
    ap.add_argument("--budget", type=float, help="--max-budget-usd per session (config run_budget_usd)")
    ap.add_argument("--dry-run", action="store_true", help="show the argv, DENY list, caps and evaluation")
    ap.add_argument("--model", help="passed to claude as --model")
    ap.add_argument("--plugin-dir", help="passed to claude as --plugin-dir (testing an uninstalled yah checkout)")
    a = ap.parse_args()
    cfg = config()
    target = parse_target(a.target)
    if target is None:
        return refuse(f"TARGET must be P<n>, #<pr> or empty, not '{a.target}'.")
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
    pcfg = cfg["projects"].get(key) if isinstance(cfg["projects"].get(key), dict) else {}
    trunks = pcfg.get("trunks") or []
    trunks = where.DEFAULT_TRUNKS | set([trunks] if isinstance(trunks, str) else trunks)
    prod = prod_branch(str(pcfg.get("prod") or ""))
    branch = read_branch(git_dir) if git_dir else None
    gh_path = where.find_tool("gh")
    # where.py's protected set covers a repo with no config.json: the plan's phase bases, where open PRs land
    # (or landed when gh last saw any), origin/HEAD and the branch HEAD was cut from, beside the trunks.
    s = where.collect(str(top), use_gh=bool(gh_path)) or {}
    fence = protected(trunks | set(s.get("protected") or []), prod)
    if not target and branch in fence:
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
        phase=None, labels=[], next="", head="", mode="build", checks="", url="", waiting=False, week=None,
        pace_logged=False, n=0, cost=0.0, last=None, stalls=0, t0=time.monotonic(), log=None)
    refresh(r, s)
    if target[:1] == "P" and r.phase is None:
        return refuse(f"no phase {target} in this repo's plan. "
                      + (f"Phases: {', '.join(r.labels)}." if r.labels else "There is no plan (beads or STATE.md)."))
    r.base, r.base_from = pr_base(r, s)
    if not r.base and not r.pr:  # a known PR names its base once evaluate reads it
        return refuse(no_base(r))
    if a.dry_run:
        return dry_run(r)
    runs = data_dir() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    r.log = runs / "{}-{}.log".format(re.sub(r"[^\w.-]", "_", key), time.strftime("%Y%m%d-%H%M%S"))
    r.log.touch()
    prune(runs)
    try:
        while True:
            code, reason = evaluate(r)
            if code is not None:
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
            say("[yah] " + line)
            log(r, line)
    except KeyboardInterrupt:
        code, reason = 130, "interrupted."
    finish(r, code, reason)
    return code


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:  # never 0, which means an open, green PR
        print(f"[yah] run.py failed: {e}")
        code = 1
    sys.exit(code or 0)
