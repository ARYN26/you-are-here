"""setup.py: point Claude Code's statusLine at yah, write config.json, offer the `yah` launcher.

    setup.py [--tier pro|max5|max20|api] [--python CMD] [--dry-run] [--yes]
    setup.py --launcher bash|zsh|fish|powershell   print the launcher snippet
    setup.py --install-launcher RCFILE             append it to RCFILE as a marked block
    setup.py --install-rules [FILE]                append RULES.md to FILE (default: CLAUDE.md)
    setup.py --ultracode                           opt-in: ultracode on by default, workflows medium
    setup.py --uninstall                           undo all of the above

Merge-only: settings.json is backed up first and only its statusLine key changes (never model,
effort, permissions, hooks or env), plus ultracode and workflowSizeGuideline with --ultracode. What it changed goes into setup-state.json in the data dir,
so --uninstall can put things back. --dry-run writes nothing. Re-running is safe.
"""
import argparse
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yahlib import TIERS, claude_dir, data_dir, read_json, utf8_stdout  # noqa: E402

PROBE = "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
SHELLS = ("bash", "zsh", "fish", "powershell")
RC = {"bash": "~/.bashrc", "zsh": "~/.zshrc", "fish": "~/.config/fish/config.fish", "powershell": "$PROFILE"}
LAUNCH = ("# >>> you-are-here >>>", "# <<< you-are-here <<<")
RULES = ("<!-- >>> you-are-here rules >>> -->", "<!-- <<< you-are-here rules <<< -->")


# ---------------------------------------------------------------- python + paths

def pick_python(given):
    """The first command that really runs Python 3.9+. The timeout covers macOS's
    /usr/bin/python3 stub, which opens an install dialog when the Command Line Tools are missing."""
    if given:
        cands = [[given]] if os.path.isfile(given) else [given.split()]
    elif os.name == "nt":  # python3 here is often the Microsoft Store stub
        cands = [["python"], ["py", "-3"], ["python3"]]
    else:
        cands = [["python3"], ["python"]]
    for c in cands:
        try:
            if subprocess.run(c + ["-c", PROBE], capture_output=True, timeout=20).returncode == 0:
                return [x.replace("\\", "/") for x in c]
        except Exception:
            pass
    return None


def scripts_dir():
    """The stable marketplace clone when running from the versioned plugin cache, else our own dir."""
    here = Path(__file__).resolve().parent
    p = here.parent.parents  # <plugins>/cache/<mkt>/<plugin>/<ver>/scripts
    if len(p) > 3 and p[2].name == "cache" and p[3].name == "plugins":
        stable = p[3] / "marketplaces" / p[1].name / "scripts"
        if (stable / "statusline.py").is_file():
            return stable.as_posix()
    return here.as_posix()


def command(py, script):
    return " ".join(f'"{x}"' if " " in x else x for x in py) + f' "{script}"'


# ---------------------------------------------------------------- files

def load_obj(path):
    """A JSON object from path ({} if missing or empty). Raises on anything else, so a broken
    file is never overwritten."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def save_json(path, data, dry):
    if dry:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".yah-tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def backup(path, dry, say):
    if path.exists():
        b = path.with_name(f"{path.name}.bak-yah-{time.strftime('%Y%m%d-%H%M%S')}")
        n = 1
        while b.exists():
            b, n = b.with_name(f"{path.name}.bak-yah-{time.strftime('%Y%m%d-%H%M%S')}-{n}"), n + 1
        say(f"backup     {b}")
        if not dry:
            shutil.copy2(path, b)


def edit_block(path, block, marks, dry, say, what):
    """Put `block` into a text file between its marker lines (block=None removes it). Idempotent.
    Bytes outside the block stay as they were, BOM and line endings included; the block takes the
    file's usual line ending. A file that is not UTF-8 is never edited: the block is printed to paste
    by hand. Returns True if the file changed, False if not, None if it is not UTF-8."""
    raw = path.read_bytes() if path.exists() else b""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        say(f"{what:<10} {path} is not UTF-8, so it was left as is. " + (
            f"Paste this into it by hand:\n{block}" if block is not None
            else f"Remove the lines from {marks[0]} to {marks[1]} by hand."))
        return None
    nl = "\r\n" if text.count("\r\n") * 2 > text.count("\n") else "\n"
    if block is not None:
        block = block.replace("\n", nl)
        if block in text:
            say(f"{what:<10} unchanged: {path}")
            return False
    pat = re.compile(r"(?:\r?\n)?" + re.escape(marks[0]) + r".*?" + re.escape(marks[1]) + r"[^\r\n]*(?:\r?\n)?",
                     re.S)
    new = pat.sub("", text)
    if block is not None:
        new += ("" if not new or new.endswith("\n") else nl) + (nl if new else "") + block + nl
    if new == text:
        say(f"{what:<10} not found in {path}")
        return False
    say(f"{what:<10} {'removed from' if block is None else 'updated in' if pat.search(text) else 'added to'} {path}")
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b"") + new.encode("utf-8"))
    return True


def record(state, key, path, state_file, dry):
    if str(path) not in state.setdefault(key, []):
        state[key].append(str(path))
        save_json(state_file, state, dry)


def ask(question):
    try:
        return sys.stdin.isatty() and input(question).strip().lower() in ("y", "yes")
    except Exception:
        return False


# ---------------------------------------------------------------- launcher

def default_shell():
    if os.name == "nt":
        return "powershell"
    sh = Path(os.environ.get("SHELL", "")).name
    return sh if sh in SHELLS else "zsh" if sys.platform == "darwin" else "bash"


def shell_for(rc):
    n = Path(rc).name.lower()
    return "powershell" if n.endswith(".ps1") else "fish" if n.endswith(".fish") else "zsh" if "zsh" in n else "bash"


def snippet(shell, py, sdir):
    """`yah <project> [claude args]` cds into the project and runs claude; `yah` alone lists projects;
    `yah run <args>` hands off to scripts/run.py."""
    pyc = " ".join(f'"{x}"' if " " in x else x for x in py)
    where, run = f'"{sdir}/where.py"', f'"{sdir}/run.py"'
    if shell == "powershell":
        body = ["function yah {",
                "  $rest = @($args | Select-Object -Skip 1)",
                f"  if ($args[0] -eq 'run') {{ & {pyc} {run} @rest; return }}",
                f"  $d = & {pyc} {where} --path \"$($args[0])\"",
                "  if ($LASTEXITCODE -ne 0 -or -not $d) { return }",
                "  Set-Location $d",
                "  claude @rest",
                "}"]
    elif shell == "fish":
        body = ["function yah",
                "    if test \"$argv[1]\" = run",
                f"        {pyc} {run} $argv[2..-1]",
                "        return",
                "    end",
                f"    set -l d ({pyc} {where} --path \"$argv[1]\"); or return 1",
                "    cd $d; and claude $argv[2..-1]",
                "end"]
    else:
        body = ["yah() {",
                f"  if [ \"$1\" = run ]; then shift; {pyc} {run} \"$@\"; return; fi",
                "  local d",
                f"  d=\"$({pyc} {where} --path \"$1\")\" || return 1",
                "  shift",
                "  cd \"$d\" && claude \"$@\"",
                "}"]
    return "\n".join([LAUNCH[0], *body, LAUNCH[1]])


# ---------------------------------------------------------------- actions

def git_bash():
    """Where Claude Code would find Git Bash on Windows, or None. Without it hooks run in PowerShell,
    which cannot parse the hook commands."""
    env = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if env and Path(env).exists():
        return env
    git = shutil.which("git")
    for cand in ([Path(git).resolve().parents[1] / "bin" / "bash.exe"] if git else []) +             [Path(os.environ.get(v, "")) / "Git" / "bin" / "bash.exe" for v in ("ProgramFiles", "LOCALAPPDATA")]:
        if cand.exists():
            return str(cand)
    return None


def install(a, py, sdir, state, state_file, say):
    dry = a.dry_run
    sp, cp = claude_dir() / "settings.json", data_dir() / "config.json"
    settings, cfg = load_obj(sp), load_obj(cp)
    say(f"python     {' '.join(py)}")
    if os.name == "nt" and not git_bash():
        say("WARNING    Git Bash not found. Claude Code then runs hooks in PowerShell, which cannot run yah's "
            "hooks (the statusline still works). Install Git for Windows: https://git-scm.com/download/win")

    ours = {"type": "command", "command": command(py, f"{sdir}/statusline.py"), "padding": 0}
    cur, mine = settings.get("statusLine"), state.get("statusLine") or {}
    if cur == ours:
        say("statusLine unchanged")
    else:
        was_ours = bool(mine) and cur == mine.get("ours")
        ok = True
        if cur and not was_ours:
            say(f"statusLine another one is set: {json.dumps(cur, ensure_ascii=False)}")
            ok = a.yes or (not dry and ask("Replace it? --uninstall puts it back. [y/N] "))
            if not ok:
                say("           left as is; re-run with --yes to replace it")
        if ok:
            backup(sp, dry, say)
            state["statusLine"] = {"prev": mine.get("prev") if was_ours else cur, "ours": ours}
            settings["statusLine"] = ours
            save_json(sp, settings, dry)
            save_json(state_file, state, dry)
            say(f"statusLine {ours['command']}  ({sp})")

    if a.ultracode:
        ultracode(settings, sp, cfg, state, state_file, dry, say)

    tier = a.tier or cfg.get("tier") or "max5"
    if cfg.get("tier") == tier:
        say(f"config     unchanged (tier {tier})")
    else:
        cfg["tier"] = tier
        save_json(cp, cfg, dry)
        say(f"config     tier {tier}  ({cp})")

    if state.get("launchers"):
        say(f"launcher   installed in {', '.join(state['launchers'])}")
    else:
        shell = default_shell()
        say(f"launcher   optional. `yah <project>` opens Claude Code in a project. Add it with "
            f"--install-launcher {RC[shell]}, or paste this into {RC[shell]}:")
        print(snippet(shell, py, sdir))
    say(f"data       {data_dir()}")
    return 0


ULTRA = {"ultracode": True, "workflowSizeGuideline": "medium"}


def ultracode(settings, sp, cfg, state, state_file, dry, say):
    """Opt-in for Max 20x: ultracode on in every session, workflows sized medium (<10 agents). The old values
    go into setup-state.json so --uninstall can put them back; config.json's ultracode turns on the guard's rules."""
    if all(settings.get(k) == v for k, v in ULTRA.items()):
        say("ultracode  unchanged (on, workflows medium)")
    else:
        backup(sp, dry, say)
        state.setdefault("ultracode", {k: settings.get(k) for k in ULTRA})
        settings.update(ULTRA)
        save_json(sp, settings, dry)
        save_json(state_file, state, dry)
        say(f"ultracode  on, workflowSizeGuideline medium  ({sp})")
    if cfg.get("ultracode") is not True:
        cfg["ultracode"] = True
        save_json(data_dir() / "config.json", cfg, dry)


def uninstall(sdir, state, state_file, dry, say):
    sp = claude_dir() / "settings.json"
    settings = load_obj(sp)
    prev = state.get("ultracode")
    if isinstance(prev, dict):
        backup(sp, dry, say)
        for k, v in prev.items():
            if v is None:
                settings.pop(k, None)
            else:
                settings[k] = v
        save_json(sp, settings, dry)
        say("ultracode  restored: " + ", ".join(f"{k}={v}" for k, v in prev.items()))
    cur, mine = settings.get("statusLine"), state.get("statusLine")
    ours_now = isinstance(cur, dict) and f'{sdir}/statusline.py"' in str(cur.get("command", ""))
    if cur is not None and (cur == (mine or {}).get("ours") or (not mine and ours_now)):
        backup(sp, dry, say)
        prev = (mine or {}).get("prev")
        if prev:
            settings["statusLine"] = prev
        else:
            settings.pop("statusLine", None)
        save_json(sp, settings, dry)
        say("statusLine " + (f"restored: {json.dumps(prev, ensure_ascii=False)}" if prev else "removed"))
    else:
        say("statusLine not set by yah; left as is")
    for key, marks, what in (("launchers", LAUNCH, "launcher"), ("rules", RULES, "rules")):
        for f in state.get(key, []):
            edit_block(Path(f), None, marks, dry, say, what)
    if state_file.exists() and not dry:
        state_file.unlink()
    say(f"data       left in place: {data_dir()}")
    return 0


def main():
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Set up yah: the statusLine, config.json, the launcher and the rules.")
    ap.add_argument("--tier", choices=list(TIERS), help="plan tier for the context thresholds (default: keep, else max5)")
    ap.add_argument("--dry-run", action="store_true", help="show what would change and write nothing")
    ap.add_argument("--uninstall", action="store_true", help="undo what setup changed")
    ap.add_argument("--python", metavar="CMD", help="the Python command to use (default: probe)")
    ap.add_argument("--launcher", choices=SHELLS, help="print the launcher snippet for this shell")
    ap.add_argument("--install-launcher", metavar="RCFILE", help="append the launcher to RCFILE as a marked block")
    ap.add_argument("--install-rules", nargs="?", const="", metavar="FILE",
                    help="append RULES.md to FILE as a marked block (default: CLAUDE.md in the Claude config dir)")
    ap.add_argument("--ultracode", action="store_true",
                    help="opt-in (for max20): ultracode on by default, workflowSizeGuideline medium")
    ap.add_argument("--yes", action="store_true", help="no prompts")
    a = ap.parse_args()
    say = (lambda s: print("[dry-run] " + s)) if a.dry_run else print
    sdir = scripts_dir()
    state_file = data_dir() / "setup-state.json"
    state = load_obj(state_file)

    if a.uninstall:
        return uninstall(sdir, state, state_file, a.dry_run, say)
    if a.install_rules is not None:
        src = Path(__file__).resolve().parent.parent / "RULES.md"
        if not src.is_file():
            print(f"[yah] {src} not found", file=sys.stderr)
            return 1
        dest = Path(a.install_rules).expanduser() if a.install_rules else claude_dir() / "CLAUDE.md"
        rules = src.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
        if edit_block(dest, f"{RULES[0]}\n{rules}\n{RULES[1]}", RULES, a.dry_run, say, "rules") is not None:
            record(state, "rules", dest, state_file, a.dry_run)
        return 0

    py = pick_python(a.python)
    if not py:
        tried = a.python or ("python, py -3, python3" if os.name == "nt" else "python3, python")
        print(f"[yah] No working Python 3.9+ found (tried {tried}). On macOS run `xcode-select --install` "
              "or `brew install python`; elsewhere install Python 3.9+ or pass --python CMD.", file=sys.stderr)
        return 1
    if a.install_launcher:
        rc = Path(a.install_launcher).expanduser()
        text = snippet(a.launcher or shell_for(rc), py, sdir)
        if edit_block(rc, text, LAUNCH, a.dry_run, say, "launcher") is not None:
            record(state, "launchers", rc, state_file, a.dry_run)
        return 0
    if a.launcher:
        print(snippet(a.launcher, py, sdir))
        return 0
    return install(a, py, sdir, state, state_file, say)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[yah] setup stopped: {e}", file=sys.stderr)
        sys.exit(1)
