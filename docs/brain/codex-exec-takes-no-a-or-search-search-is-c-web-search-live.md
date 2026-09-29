---
title: codex exec takes no -a or --search; search is -c web_search=live
tags: [codex, gpt]
paths: ["scripts/codex.py"]
status: active
source: https://github.com/openai/codex/blob/main/codex-rs/exec/src/cli.rs
---
codex exec always runs approval never and rejects -a; web search is -c web_search=live. Prompt goes on stdin with a trailing -, the answer comes from -o FILE, not stdout.
