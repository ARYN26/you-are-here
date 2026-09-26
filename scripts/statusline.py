"""statusline.py: one line from Claude Code's statusline JSON + the where.py cache + the `yah run` pid file.

  Opus 5.5 high | 113k | 5h 22% | wk 41% (pace 35%) | feature/login | PR#8 | CHECKOUT P2/4 | 1 for you | run P2: iter…

Rules it makes visible:
  context amber at `amber`, red with "wrap" at `wrap_soon` (config.json, tier presets);
  a red tag when the main model matches `premium_models`; weekly % against the share of the
  week already gone ("pace"); "cache cold" when a big session has been idle over an hour
  (resuming it rewrites the whole cache). 5h/wk appear only when rate_limits exist
  (Pro/Max subscribers, never API keys).

Side effects, all in the data dir:
  state-<session>.json  latest numbers, read by context_guard.py
  limits.json           the latest rate limits, rewritten only when they change
  usage-log.csv         one row per day (latest value wins), the weekly trend
Never calls bd, gh or git. Must stay well under 300 ms.
"""
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yahlib import (LOG_STAMP, config, data_dir, find_git, project_key, read_branch, read_json,  # noqa: E402
                    run_info, utf8_stdout, where_cache_path, write_json)

COLD_AFTER_S, COLD_MIN_TOKENS = 3600, 30_000
WEEK_S = 7 * 86400

RST, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
GREEN, AMBER, RED = "\033[32m", "\033[33m", "\033[31m"


def color(text, c):
    return f"{c}{text}{RST}" if c else text


def to_epoch(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e12 else float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def pct(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def context_tokens(d):
    cw = d.get("context_window") or {}
    cu = cw.get("current_usage") or {}
    if isinstance(cu, dict) and cu:
        n = sum(int(cu.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        if n:
            return n
    p, size = pct(cw.get("used_percentage")), pct(cw.get("context_window_size"))
    if p is not None and size:
        return int(p * size / 100)
    return None


def effort_of(d):
    e = d.get("effort")
    if isinstance(e, dict):
        e = e.get("level") or e.get("value")
    return str(e) if e else ""


def model_bits(d, premium):
    """(short name, premium tag or "", model id). The tag is the matched premium_models word."""
    m = d.get("model") or {}
    name = str(m.get("display_name") or m.get("id") or "?")
    ident = f"{m.get('id', '')} {name}".lower()
    tag = next((p for p in premium if p in ident), "").upper()
    short = re.sub(r"\s*\(.*?\)", "", name).replace("Claude ", "").strip()
    return short, tag, str(m.get("id") or name)


def run_part(ri):
    """The checkout's `yah run`: its target and last log line while it is live, else its exit code."""
    what = f"run {ri.get('target') or 'phase'}"
    if "code" in ri:
        code = ri["code"]
        return color(f"{what} exit {code}", GREEN if code in (0, 8) else RED if code == 1 else AMBER)
    if not ri.get("alive"):
        return color(f"{what} died", RED)
    last = LOG_STAMP.sub("", ri.get("last") or "")
    return color(what + (f": {last[:40]}{'…' if len(last) > 40 else ''}" if last else ""), GREEN)


def week_pace(resets_at, now):
    r = to_epoch(resets_at)
    if not r:
        return None
    left = max(0.0, min(WEEK_S, r - now))
    return round(100 * (1 - left / WEEK_S))


def save_limits(now, rl, five, week, pace):
    """limits.json for other tools; rewritten only when a number changes."""
    def pool(key, used):
        r = to_epoch((rl.get(key) or {}).get("resets_at"))
        return {"used_pct": used, "resets_at": None if r is None else round(r)}
    new = {"five_hour": pool("five_hour", five), "seven_day": pool("seven_day", week), "pace_pct": pace}
    path = data_dir() / "limits.json"
    old = read_json(path, {}) or {}
    if {k: old.get(k) for k in new} != new:
        write_json(path, dict(new, ts=round(now)))


def log_usage(now, five, week, pace, model):
    """One row per day in usage-log.csv; the row for today is overwritten as it changes."""
    if five is None and week is None:
        return
    f = data_dir() / "usage-log.csv"
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    row = f"{today},{datetime.fromtimestamp(now).strftime('%H:%M')},{'' if five is None else round(five)}," \
          f"{'' if week is None else round(week)},{'' if pace is None else pace},{model}"
    try:
        lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else ["date,time,five_hour_pct,week_pct,week_pace_pct,model"]
        last = lines[-1] if len(lines) > 1 else ""
        if last.startswith(today + ","):
            if last.split(",")[2:5] == row.split(",")[2:5]:
                return
            lines[-1] = row
        else:
            lines.append(row)
            cleanup(now)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def cleanup(now):
    for p in data_dir().glob("*.json"):
        if p.name.startswith(("state-", "guard-")):
            try:
                if now - p.stat().st_mtime > 3 * 86400:
                    p.unlink()
            except Exception:
                pass


def main():
    utf8_stdout()
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        d = json.loads(raw) if raw.strip() else {}
    except Exception:
        d = {}
    cfg = config()
    now = time.time()
    parts = []

    short, tag, model_id = model_bits(d, cfg["premium_models"])
    eff = effort_of(d)
    parts.append((f"\033[1;97;41m {tag} {RST} " if tag else "") + color(f"{short}{(' ' + eff) if eff else ''}", BOLD))

    tokens = context_tokens(d)
    if tokens is not None:
        k = f"{round(tokens / 1000)}k"
        if tokens >= cfg["wrap_soon"]:
            parts.append(color(f"{k} wrap", RED))
        elif tokens >= cfg["amber"]:
            parts.append(color(k, AMBER))
        else:
            parts.append(color(k, GREEN))

    rl = d.get("rate_limits") or {}
    rl = rl if isinstance(rl, dict) else {}
    five = pct((rl.get("five_hour") or {}).get("used_percentage"))
    week = pct((rl.get("seven_day") or {}).get("used_percentage"))
    pace = week_pace((rl.get("seven_day") or {}).get("resets_at"), now)
    if five is not None:
        parts.append(color(f"5h {five:.0f}%", RED if five >= 90 else AMBER if five >= 70 else ""))
    if week is not None:
        c = ""
        if pace is not None:
            c = RED if week > pace + 20 else AMBER if week > pace + 10 else ""
        parts.append(color(f"wk {week:.0f}%" + (f" (pace {pace}%)" if pace is not None else ""), c))
    for key, val in rl.items():  # any extra pool (e.g. a per-model weekly bar), shown once it matters
        if key in ("five_hour", "seven_day") or not isinstance(val, dict):
            continue
        p = pct(val.get("used_percentage"))
        if p is not None and p >= 50:
            parts.append(color(f"{key.replace('seven_day_', 'wk ').replace('_', ' ')} {p:.0f}%", AMBER if p < 85 else RED))

    ws = d.get("workspace") or {}
    cwd = ws.get("current_dir") or d.get("cwd") or os.getcwd()
    top, main_root, git_dir = find_git(cwd)
    branch = read_branch(git_dir) if git_dir else None
    cache = read_json(where_cache_path(main_root), {}) if main_root else {}
    cache = cache if isinstance(cache, dict) else {}
    if branch:
        parts.append(branch)
    pr = d.get("pr") or {}
    pr_num = pr.get("number") if isinstance(pr, dict) else None
    if not pr_num and branch:
        pr_num = ((cache.get("prs") or {}).get(branch) or {}).get("number")
    if pr_num:
        rs = str(pr.get("review_state") or "") if isinstance(pr, dict) else ""
        parts.append(f"PR#{pr_num}" + (f" {rs.lower().replace('_', ' ')}" if rs and rs.upper() not in ("NONE", "") else ""))
    plan, phase = cache.get("plan"), cache.get("phase")
    if plan:
        label = phase.get("label", "?") if phase else "done"
        mark = "" if cache.get("phase_running") or not phase else " (not started)"
        parts.append(f"{plan.get('short', '')} {label}/{plan.get('total', '?')}{mark}")
    if cache.get("human"):
        parts.append(color(f"{cache['human']} for you", AMBER))
    ri = run_info(project_key(main_root), str(top), str(main_root), now) if main_root else None
    if ri:
        parts.append(run_part(ri))

    tp = d.get("transcript_path")
    try:
        if tp and tokens and tokens >= COLD_MIN_TOKENS and now - os.path.getmtime(tp) > COLD_AFTER_S:
            parts.append(color("cache cold: /clear beats resuming", AMBER))
    except Exception:
        pass

    print(color(" | ", DIM).join(parts))

    sid = d.get("session_id")
    if sid:
        write_json(data_dir() / f"state-{sid}.json", {
            "ts": now, "tokens": tokens, "model": model_id, "premium": tag,
            "five_hour": five, "week": week, "pace": pace})
    if five is not None or week is not None:
        save_limits(now, rl, five, week, pace)
    log_usage(now, five, week, pace, model_id)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[yah] statusline error: {e}")
