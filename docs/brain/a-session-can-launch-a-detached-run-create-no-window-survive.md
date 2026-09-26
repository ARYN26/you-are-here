---
title: A session can launch a detached run; CREATE_NO_WINDOW survives the terminal closing
tags: [run, detach, autopilot, windows]
paths: ["scripts/run.py", "skills/auto/SKILL.md"]
status: active
source: spike on yah/autopilot-auto, Windows 11, 2026-09-25
---
Claude Code on Windows puts tool and claude -p children in no job and kills no tree, so they outlive the call and the session. Closing the terminal kills plain children; CREATE_NO_WINDOW ones live.
Spike: a launcher started sleeper children with four creation-flag sets (plain, CREATE_NO_WINDOW, DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP, the same plus CREATE_BREAKAWAY_FROM_JOB) from a Bash tool call, a PowerShell tool call, a headless claude -p session, and a Windows Terminal window closed with WM_CLOSE.

- IsProcessInJob is false in all three Claude Code contexts, and every child outlived the tool call and the claude -p session, plain ones included.
- Closing the terminal window sends CTRL_CLOSE_EVENT to every process on that console: the plain child died; the other three lived.
- Use CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP for the detached driver. It gets its own hidden console, which claude, gh and git inherit, so no window flashes. DETACHED_PROCESS leaves the driver with no console, so each console child it starts (iterate() passes no creationflags) would open a visible window.
- Skip CREATE_BREAKAWAY_FROM_JOB: CreateProcess fails with access denied inside a job that lacks JOB_OBJECT_LIMIT_BREAKAWAY_OK. If it is ever added, retry without it.
- POSIX (not tested here): start_new_session=True with stdin from /dev/null and stdout/stderr to a file, so the terminal's SIGHUP never reaches it.
- Still open: whether auto mode's classifier lets a session start the detached run, which itself starts claude -p sessions in auto mode. Test once --detach exists.
