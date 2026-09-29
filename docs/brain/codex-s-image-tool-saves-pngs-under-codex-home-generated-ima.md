---
title: codex's image tool saves PNGs under CODEX_HOME/generated_images whatever the sandbox
tags: [codex, mockup, image]
paths: ["scripts/codex.py"]
status: active
source: smoke run 2026-09-29, codex-cli 0.159.1, scripts/codex.py mockup()
---
codex exec's image_generation writes $CODEX_HOME/generated_images/<thread>/exec-*.png even read-only; the model could not copy it out on Windows, so codex.py mockup copies new PNGs itself.
