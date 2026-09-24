---
name: deep
description: Send one hard, self-contained question to Fable in a forked context, without switching this session's model. Use when the user says "deep", or for a bug that resisted two attempts.
argument-hint: "<self-contained question naming files and what was tried>"
context: fork
agent: yah:deep
---

Question:

$ARGUMENTS

The forked agent cannot see the conversation that sent this, so the question above must stand alone: the goal, the files involved, what was tried and what happened.

Read only the files the question needs. Answer in at most 300 words: the verdict first, then the reasoning that decides it, then the one check to run before acting. If the question cannot be answered from what you can read, say what is missing.
