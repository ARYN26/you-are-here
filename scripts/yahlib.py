"""Shared helpers for the yah scripts. Stdlib only, Python 3.9+.

Everything yah writes on its own lives in the data dir: ~/.claude/you-are-here/,
or $CLAUDE_CONFIG_DIR/you-are-here/ when that is set. Nothing here writes inside a repo.
"""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# tier -> (amber, wrap_soon, wrap_now). Starting points, not official numbers.
TIERS = {"pro": (100_000, 120_000, 160_000), "max5": (120_000, 150_000, 200_000),
         "max20": (150_000, 200_000, 260_000), "api": (80_000, 100_000, 150_000)}
# role -> "model effort". config.json "roles" overrides one role at a time: {"roles": {"critic": "opus high"}}.
ROLES = {"main": "opus high", "scout": "sonnet low", "mechanical": "opus medium", "judge": "opus high",
         "critic": "fable high"}
EFFORTS = ("low", "medium", "high", "xhigh")  # max is refused everywhere
DEFAULTS = {"tier": "max5", "pace_slack": 15, "premium_models": ["fable", "mythos"], "recent_days": 14,
            "brain_dir": "docs/brain", "recall_max_chars": 10000,
            "run_iterations": 8, "run_iteration_minutes": 45, "run_total_hours": 6,
            "run_checks_wait_minutes": 30, "run_week_stop_pct": 80, "ultracode": False, "auto_merge": False,
            "roles": ROLES, "critic_week_skip_pct": 50}
# tier -> --max-budget-usd per `yah run` iteration, unless run_budget_usd is set.
RUN_BUDGET = {"pro": 5, "max5": 10, "max20": 15, "api": 5}
_config = None


def utf8_stdout():
    """Windows pipes default to cp1252; Claude Code reads UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def claude_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def data_dir():
    """The data folder. Not created here: write_json creates it on first write."""
    return claude_dir() / "you-are-here"


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def write_json(path, data):
    """Atomic enough for a cache: write a temp file, then replace."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def config():
    """config.json over the defaults. The tier preset fills amber, wrap_soon and wrap_now
    unless those keys are set explicitly."""
    global _config
    if _config is None:
        user = read_json(data_dir() / "config.json", {})
        cfg = dict(DEFAULTS, **(user if isinstance(user, dict) else {}))
        cfg["tier"] = str(cfg["tier"]).lower()
        for key, val in zip(("amber", "wrap_soon", "wrap_now"), TIERS.get(cfg["tier"], TIERS["max5"])):
            cfg[key] = num(cfg.get(key), val)
        cfg["pace_slack"] = num(cfg["pace_slack"], 15)
        cfg["recent_days"] = num(cfg["recent_days"], 14)
        for key in ("recall_max_chars", "run_iterations", "run_iteration_minutes", "run_total_hours",
                    "run_checks_wait_minutes", "run_week_stop_pct", "critic_week_skip_pct"):
            cfg[key] = num(cfg[key], DEFAULTS[key])
        cfg["run_budget_usd"] = num(cfg.get("run_budget_usd"), RUN_BUDGET.get(cfg["tier"], RUN_BUDGET["max5"]))
        cfg["brain_dir"] = str(cfg["brain_dir"] or DEFAULTS["brain_dir"]).strip("/\\")
        cfg["auto_merge"] = cfg["auto_merge"] is True  # merging is opt-in: only a JSON true turns it on
        cfg["ultracode"] = cfg["ultracode"] is True
        roles = cfg["roles"] if isinstance(cfg["roles"], dict) else {}
        cfg["roles"] = {name: role_text(roles.get(name), default) for name, default in ROLES.items()}
        pm = cfg["premium_models"]
        cfg["premium_models"] = [str(m).lower() for m in ([pm] if isinstance(pm, str) else pm or []) if m]
        if not isinstance(cfg.get("projects"), dict):
            cfg["projects"] = {}
        _config = cfg
    return _config


def role_text(value, default):
    """A clean "model effort" string. A missing model, or an effort outside EFFORTS (max, junk), comes from default."""
    model, effort = default.split()
    words = value.lower().split() if isinstance(value, str) else []
    if words and words[0] in EFFORTS + ("max",):  # an effort alone ("xhigh", "max") names no model
        words = [model] + words
    if words:
        model = words[0]
    if len(words) > 1 and words[1] in EFFORTS:
        effort = words[1]
    return f"{model} {effort}"


def role(name):
    """(model, effort) for a role in config.json "roles"; an unknown role gets main's."""
    roles = config()["roles"]
    return tuple(roles.get(name, roles["main"]).split())


def norm(p):
    try:
        p = Path(p).resolve()
    except Exception:
        pass
    return os.path.normcase(os.path.normpath(str(p)))


def plugins_dir():
    """Claude Code's plugins folder: installed_plugins.json, known_marketplaces.json and cache/."""
    return Path(os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR") or claude_dir() / "plugins")


def plugin_outdated():
    """True when this copy of yah is an old version that a running Claude Code still holds.

    An update installs the new version beside the old one in the plugin cache and points
    installed_plugins.json at it. A session keeps the copy it loaded, even across /clear, until
    /reload-plugins or a restart. A copy outside the cache (--plugin-dir, the marketplace clone) never counts."""
    root = Path(__file__).resolve().parent.parent  # cache/<marketplace>/<plugin>/<version>
    try:
        if norm(root.parents[2]) != norm(plugins_dir() / "cache"):
            return False
        installed = read_json(plugins_dir() / "installed_plugins.json")["plugins"]
        paths = {norm(e["installPath"]) for es in installed.values() for e in es}
    except Exception:
        return False  # never cost the other nudges
    here = norm(root)
    return here not in paths and any(os.path.dirname(p) == os.path.dirname(here) for p in paths)


def find_git(start):
    """Walk up from `start` without a subprocess.

    Returns (toplevel, main_root, git_dir). For a linked worktree, main_root is
    the main checkout, so a worktree maps to its project.
    Returns (None, None, None) outside a repo.
    """
    try:
        p = Path(start).resolve()
    except Exception:
        return None, None, None
    for d in [p, *p.parents]:
        g = d / ".git"
        if g.is_dir():
            return d, d, g
        if g.is_file():
            try:
                txt = g.read_text(encoding="utf-8").strip()
                if txt.startswith("gitdir:"):
                    gd = Path(txt[7:].strip())
                    if not gd.is_absolute():
                        gd = (d / gd).resolve()
                    common = gd.parent.parent if gd.parent.name == "worktrees" else gd
                    main = common.parent if common.name == ".git" else d
                    return d, main, gd
            except Exception:
                pass
            return d, d, None
    return None, None, None


def read_branch(git_dir):
    try:
        head = (Path(git_dir) / "HEAD").read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/"):]
    return head[:8] + " (detached)"


def registered_key(main_root):
    """The config.json project whose path is main_root, else None."""
    for key, cfg in config()["projects"].items():
        if isinstance(cfg, dict) and cfg.get("path") and norm(cfg["path"]) == norm(main_root):
            return key
    return None


def project_key(main_root):
    """The config.json project whose path is main_root, else the folder name."""
    return registered_key(main_root) or Path(main_root).name.lower()


def root_tag(root):
    """8 hex of sha1 of the root: tells apart repos that share a folder name."""
    return hashlib.sha1(norm(root).encode("utf-8")).hexdigest()[:8]


def where_cache_path(main_root):
    """where-<key>.json for a config.json project, else where-<name>-<root_tag>.json,
    so two repos with the same folder name never share a cache."""
    name = registered_key(main_root) or "{}-{}".format(Path(main_root).name.lower(), root_tag(main_root))
    return data_dir() / "where-{}.json".format(re.sub(r"[^\w.-]", "_", name))


# ---------------------------------------------------------------- the `yah run` pid file

LOCK_AT = 1 << 30  # Windows byte locks are mandatory, so the lock sits past the JSON and readers can still read it


def run_pid_path(key, top, main_root):
    """<data dir>/runs/<key>-<root_tag>.pid, so two repos with the same folder name never share a lock,
    or <key>-<root_tag>@<worktree folder>.pid in a linked worktree: one `yah run` per checkout."""
    root = main_root or top
    name = f"{key}-{root_tag(root)}"
    name = name if norm(top) == norm(root) else f"{name}@{Path(top).name}"
    return data_dir() / "runs" / (re.sub(r"[^\w.@-]", "_", name) + ".pid")


def _lock(fd, unlock=False):
    """Try once to lock fd (or unlock it); True on success. The OS drops the lock when its process dies."""
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, LOCK_AT, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release(fd):
    _lock(fd, unlock=True)  # at once: Windows may take a while to drop a lock on close
    os.close(fd)


def write_run(fd, data):
    """Rewrite the pid file in place: it cannot be replaced while its run holds it open on Windows."""
    try:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        os.lseek(fd, 0, 0)
        os.write(fd, raw)
        os.ftruncate(fd, len(raw))
    except OSError:
        pass


def hold_run(path, data):
    """Take the checkout's run lock and write data to its pid file. The fd, to keep open for the life of the run,
    or None while another live run holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    for _ in range(5):  # a few tries: run_state (statusline, where.py, --detach's wait) holds the lock for a moment
        if _lock(fd):
            break
        time.sleep(0.02)
    else:
        os.close(fd)
        return None
    write_run(fd, data)
    return fd


def end_run(path, data):
    """Write an ended run's data to its pid file, holding the lock only while it writes. False while a live run
    holds it."""
    fd = hold_run(path, data)
    if fd is None:
        return False
    _release(fd)
    return True


def run_held(path):
    """Whether a live run holds the pid file's lock; None when the file cannot be opened."""
    try:
        fd = os.open(str(path), os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    if _lock(fd):
        _release(fd)
        return False
    os.close(fd)
    return True


def run_state(path):
    """The pid file's data plus alive, whether its run still holds the lock. None with no pid file, or one caught
    mid-write."""
    data = read_json(path)
    held = run_held(path) if isinstance(data, dict) else None
    if held is None:
        return None
    data["alive"] = held
    return data


def run_status(st):
    """live, ended or died. A run writes its code a moment before it lets go of the lock, so a code wins."""
    return "ended" if "code" in st else "live" if st.get("alive") else "died"


RUN_SHOW_S = 3 * 86400  # how long an ended run keeps its RUN line
LOG_STAMP = re.compile(r"^\d{4}-\d\d-\d\d (\d\d:\d\d):\d\d ")


def log_tail(path, size=4096):
    """The log's last non-empty line, read from its end only: the statusline reads it on every refresh."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - size))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    return next((ln.strip() for ln in reversed(lines) if ln.strip()), "")


def run_info(key, top, main_root, now=None):
    """The checkout's `yah run` for the RUN line: run_state plus `last`, its log's last line, and for a run that
    ended, `at`: when (for one that died without saying, its log's last write). None with no run, or one that
    ended over RUN_SHOW_S ago."""
    path, now = run_pid_path(key, top, main_root), now or time.time()
    st = read_json(path)
    if not isinstance(st, dict) or now - num(st.get("ended"), now) > RUN_SHOW_S:
        return None  # long over: the statusline skips the lock probe and the log read on every refresh
    held = run_held(path)
    if held is None:
        return None
    st["alive"] = held
    log = str(st.get("log") or "")
    st["last"] = log_tail(log) if log else ""
    if run_status(st) != "live":
        at = num(st.get("ended"), None)
        if at is None:
            try:
                at = os.path.getmtime(log)
            except OSError:
                at = num(st.get("started"), 0)
        st["at"] = at
        if now - at > RUN_SHOW_S:
            return None
    return st


def ago(secs):
    secs = max(0, int(secs))
    return f"{max(1, secs // 60)}m" if secs < 3600 else f"{secs // 3600}h" if secs < 86400 else f"{secs // 86400}d"


def run_text(st, now=None):
    """The RUN line from run_info: a live run's target and last log line; an ended one's exit code and reason."""
    now = now or time.time()
    what = (st.get("target") or "current phase") + (" (--plan)" if st.get("plan") else "")
    last = LOG_STAMP.sub(r"\1 ", st.get("last") or "")
    last = f", last: {last}" if last else ""
    status = run_status(st)
    if status == "ended":
        return f"{what} ended {ago(now - st['at'])} ago, exit {st['code']}: {st.get('reason') or '-'}"
    if status == "live":
        return f"{what} running for {ago(now - num(st.get('started'), now))}, pid {st.get('pid', '?')}{last}"
    return f"{what} died {ago(now - st['at'])} ago without an exit code, pid {st.get('pid', '?')}{last}"


# ---------------------------------------------------------------- subprocess

def run(cmd, cwd, timeout=10, env=None):
    import subprocess  # here, not at the top: the statusline and prompt hooks import yahlib and never run one
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout, env=env,
                           creationflags=NO_WINDOW)
        if p.returncode != 0:
            return None
        return p.stdout.decode("utf-8", "replace")
    except Exception:
        return None


def find_tool(name, extra=()):
    """PATH first, then where Homebrew and pipx/uv put binaries, then the `extra` dirs."""
    import shutil
    dirs = [Path.home() / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"), *extra]
    return shutil.which(name) or shutil.which(name, path=os.pathsep.join(str(p) for p in dirs))


# ---------------------------------------------------------------- plan state, the shape every source fills

def repo_dirs(top, main_root):
    """The worktree, then the main checkout: gitignored state (.beads, STATE.md) exists only where it was made."""
    return list(dict.fromkeys((Path(top), Path(main_root or top))))


def first_line(text):
    for ln in (text or "").splitlines():
        ln = ln.strip().lstrip("-* ").strip()
        if ln:
            return re.sub(r"^(NEXT|Next)\s*[:\-]\s*", "", ln)
    return ""


def short_of(title):
    """A plan's statusline tag: its title's first word, upper case."""
    return ((title or "").split() or ["PLAN"])[0].upper()[:8]


def empty_state():
    """The plan state with nothing in it, that each source fills."""
    return {"human": [], "in_progress": [], "open_count": 0, "plan": None, "phase": None, "next_phase": None,
            "phases": []}


def pick_phase(state, views):
    """The in_progress phase is current; with none running, the first unclosed one is next."""
    running = [v for v in views if v["status"] == "in_progress"]
    pending = [v for v in views if v["status"] != "closed"]
    state.update(phases=views, phase=running[0] if running else None,
                 next_phase=None if running else pending[0] if pending else None)
    return state
