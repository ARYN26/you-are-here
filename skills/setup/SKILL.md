---
name: setup
description: Set up yah - statusline, plan tier, optional launcher and rules. Run once after install.
disable-model-invocation: true
---

# Setup

Every step changes a file outside this repo, so each one needs the user's explicit yes. If they say no, skip that step and go on.

Run setup with Bash as `PY "${CLAUDE_SKILL_DIR}/../../scripts/setup.py" <flags>`. `PY` is `python` on Windows and `python3` elsewhere; use `py -3` only if both fail.

## 1. Tier

Ask which plan the user is on. It sets when the statusline turns amber and red and when yah says to wrap:
- `pro`: amber 100k, wrap 120k, wrap now 160k
- `max5` (default): 120k / 150k / 200k
- `max20`: 150k / 200k / 260k
- `api` (API key, pay per token): 80k / 100k / 150k

## 2. Preview

Run `setup.py --tier <tier> --dry-run` and show the output verbatim. It writes nothing. Point out any existing statusLine it would replace (it is backed up and `--uninstall` restores it).

## 3. Apply

On yes, run `setup.py --tier <tier> --yes` and show the output. The statusline shows from the next render.

## 3b. Ultracode by default (max20 only, optional)

Only when the tier is `max20`, offer: "Turn ultracode on for every session? It runs xhigh effort with workflows, which costs more per turn. yah keeps it lean: workflows capped at medium size (<10 agents), and a once-per-session rule to use workflows only for genuinely parallel work, with low or medium effort for mechanical stages." On yes, run `setup.py --ultracode --yes` and show the output. `--uninstall` restores the previous values.

## 3c. Auto-update (optional)

Offer: "Turn on auto-update for yah? Claude Code leaves it off for plugins from third-party marketplaces, so yah stays at the version you installed until you run `claude plugin update yah@you-are-here`. With it on, Claude Code fetches new versions in the background once a session starts, and the next session loads them." On yes, run `setup.py --auto-update` and show the output. It changes only that setting. `--uninstall` restores the previous value.

## 3d. Auto-merge (optional)

Offer: "Let `yah run` merge its own PRs? It is off by default, and you merge every PR. With it on, only the `yah run` driver merges, never the model, and only a green PR you authored for a plan phase: at least one check ran and all passed, GitHub says it is mergeable with a clean merge state, it is not a draft, and no change request is newer than its last commit. It makes a merge commit pinned to the commit it checked (never a squash), then retargets any PR stacked on it, then deletes the merged branch, never a protected one. A run that merges ends with exit 8; with `--plan` it goes on to the next phase." On yes, run `setup.py --auto-merge` and show the output. It sets only `auto_merge` in config.json. `--uninstall` restores the previous value.

## 4. Launcher (optional)

Explain: `yah <project>` opens Claude Code in that project from any folder, so project hooks and memory load; `yah` alone lists projects. On yes, find the shell's rc file (`~/.zshrc`, `~/.bashrc`, `~/.config/fish/config.fish`, or on Windows the path printed by `powershell -NoProfile -Command '$PROFILE'`) and run `setup.py --install-launcher <rcfile>`. On Windows also run it with the `yah.cmd` path that setup printed, so `yah` works in cmd.exe too. Tell the user to open a new terminal.

## 5. Working rules (optional)

Show the user the contents of `${CLAUDE_SKILL_DIR}/../../RULES.md`. On yes, run `setup.py --install-rules` to append them to their user CLAUDE.md inside a marked block. Re-running does not duplicate it.

## 6. Done

Say: to undo all of this, run `/yah:setup` again and ask to uninstall, which runs `setup.py --uninstall` (restores the previous statusLine, removes the launcher and rules blocks, keeps the data folder).
