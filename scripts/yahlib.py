"""Shared helpers for the yah scripts. Stdlib only, Python 3.9+.

Everything yah writes on its own lives in the data dir: ~/.claude/you-are-here/,
or $CLAUDE_CONFIG_DIR/you-are-here/ when that is set. Nothing here writes inside a repo.
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# tier -> (amber, wrap_soon, wrap_now). Starting points, not official numbers.
TIERS = {"pro": (100_000, 120_000, 160_000), "max5": (120_000, 150_000, 200_000),
         "max20": (150_000, 200_000, 260_000), "api": (80_000, 100_000, 150_000)}
DEFAULTS = {"tier": "max5", "pace_slack": 15, "premium_models": ["fable", "mythos"], "recent_days": 14,
            "brain_dir": "docs/brain", "recall_max_chars": 10000,
            "run_iterations": 8, "run_iteration_minutes": 45, "run_total_hours": 6,
            "run_checks_wait_minutes": 30, "run_week_stop_pct": 80}
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
                    "run_checks_wait_minutes", "run_week_stop_pct"):
            cfg[key] = num(cfg[key], DEFAULTS[key])
        cfg["run_budget_usd"] = num(cfg.get("run_budget_usd"), RUN_BUDGET.get(cfg["tier"], RUN_BUDGET["max5"]))
        cfg["brain_dir"] = str(cfg["brain_dir"] or DEFAULTS["brain_dir"]).strip("/\\")
        pm = cfg["premium_models"]
        cfg["premium_models"] = [str(m).lower() for m in ([pm] if isinstance(pm, str) else pm or []) if m]
        if not isinstance(cfg.get("projects"), dict):
            cfg["projects"] = {}
        _config = cfg
    return _config


def norm(p):
    try:
        p = Path(p).resolve()
    except Exception:
        pass
    return os.path.normcase(os.path.normpath(str(p)))


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


def where_cache_path(main_root):
    """where-<key>.json for a config.json project, else where-<name>-<8 hex of sha1(root)>.json,
    so two repos with the same folder name never share a cache."""
    name = registered_key(main_root) or "{}-{}".format(
        Path(main_root).name.lower(), hashlib.sha1(norm(main_root).encode("utf-8")).hexdigest()[:8])
    return data_dir() / "where-{}.json".format(re.sub(r"[^\w.-]", "_", name))
