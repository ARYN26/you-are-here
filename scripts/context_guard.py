"""context_guard.py: UserPromptSubmit hook (and PostToolUse inside `yah run`). Nudges, never blocks. Each nudge fires once:

  context >= wrap_soon   "finish this step, then /yah:wrap"
  context >= wrap_now    "wrap now"
  premium main model     once per session (config.json premium_models)
  weekly % more than pace_slack points ahead of the week's elapsed share: once a day

Thresholds come from config.json and its tier preset. Context size comes from the
transcript's last main-thread assistant turn (fresh), falling back to the statusline's
state file, which also supplies the rate limits and the premium tag.
Flags re-arm when context drops below `amber` (after /clear or a compaction).
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yahlib import config, data_dir, read_json, utf8_stdout, write_json  # noqa: E402

TAIL_BYTES = 600_000
SKIP = ("/yah:wrap", "/clear", "/compact", "/exit")


def transcript_tokens(path):
    """Context size at the last main-thread assistant turn, read from the file's tail."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            chunk = f.read().decode("utf-8", "replace")
    except Exception:
        return None
    for line in reversed(chunk.splitlines()):
        if '"usage"' not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "assistant" or rec.get("isSidechain"):
            continue
        u = (rec.get("message") or {}).get("usage") or {}
        n = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        if n:
            return n
    return None


def main():
    utf8_stdout()
    try:
        d = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return
    sid = d.get("session_id") or "nosession"
    event = "PostToolUse" if d.get("hook_event_name") == "PostToolUse" else "UserPromptSubmit"
    if (d.get("prompt") or "").strip().startswith(SKIP):
        return

    cfg = config()
    now = time.time()
    state = read_json(data_dir() / f"state-{sid}.json", {}) or {}
    tokens = transcript_tokens(d.get("transcript_path") or "") or state.get("tokens") or 0
    flags_path = data_dir() / f"guard-{sid}.json"
    flags = read_json(flags_path, {}) or {}
    daily_path = data_dir() / "guard-daily.json"
    daily = read_json(daily_path, {}) or {}
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    to_claude, to_user = [], []
    k = round(tokens / 1000)

    if tokens < cfg["amber"]:
        flags.pop("soon", None)
        flags.pop("now", None)
    if tokens >= cfg["wrap_now"] and not flags.get("now"):
        flags["now"] = flags["soon"] = True
        to_claude.append(f"[yah] Context is {k}k tokens. Wrap now: answer this prompt briefly, then run /yah:wrap "
                         "and tell the user to /clear. Every further turn re-reads all of this context.")
        to_user.append(f"Context {k}k: wrap now (/yah:wrap, then /clear).")
    elif tokens >= cfg["wrap_soon"] and not flags.get("soon"):
        flags["soon"] = True
        to_claude.append(f"[yah] Context is {k}k tokens. Finish the current step, then run /yah:wrap and tell the "
                         "user it is safe to /clear. Do not start a new task in this session.")
        to_user.append(f"Context {k}k: wrap after this step.")

    tag = state.get("premium")
    if tag and not flags.get("premium"):
        flags["premium"] = True
        name = str(tag).title()
        to_claude.append(f"[yah] The main thread is {name}, a premium model. On Max plans Fable can use up to half "
                         "of the weekly cap (it has its own usage bar); on Pro it needs extra-usage credits. Keep "
                         "this session short. Do not switch model mid-session (it rewrites the cache); for routine "
                         "work suggest a fresh session on a standard model, and use /yah:deep for single hard questions.")
        to_user.append(f"Main thread is {name} (premium). Keep it short.")

    week, pace = state.get("week"), state.get("pace")
    if week is not None and pace is not None and week > pace + cfg["pace_slack"] and daily.get("pace") != today:
        daily["pace"] = today
        write_json(daily_path, daily)
        to_claude.append(f"[yah] Weekly usage is {week:.0f}% with {pace}% of the week gone. Prefer effort high over "
                         "xhigh, avoid large parallel agent fan-outs unless the work is genuinely parallel, and keep "
                         "sessions short.")
        to_user.append(f"Weekly {week:.0f}% vs {pace}% of the week gone: run lean today.")

    write_json(flags_path, flags)
    if to_claude:
        print(json.dumps({
            "systemMessage": " ".join(to_user),
            "hookSpecificOutput": {"hookEventName": event, "additionalContext": "\n".join(to_claude)},
        }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a nudge must never block a prompt
