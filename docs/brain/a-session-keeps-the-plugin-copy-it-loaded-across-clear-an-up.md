---
title: A session keeps the plugin copy it loaded across /clear; an update marks it .orphaned_at
tags: [plugin, update, claude-code]
paths: ["scripts/yahlib.py", "scripts/context_guard.py"]
status: active
source: ~/.claude/plugins/cache/you-are-here/yah/87bce901fa0a/.orphaned_at after claude plugin update (Claude Code 2.1.283, 2026-09-26)
---
Claude Code loads yah once per process. /clear keeps it; /reload-plugins or a restart loads a new install. An update points installed_plugins.json at the new cache dir and marks the old one .orphaned_at; .in_use/<pid> lists holders. yahlib.plugin_outdated compares against installed_plugins.json only (an uninstall may also orphan a dir).
