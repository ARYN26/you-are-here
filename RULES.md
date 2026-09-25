# Working rules (you-are-here)

## Orient
- Orient with /yah:where or the state injected at session start. Never read long docs just to find out where things stand.

## Brain
- For project facts, use the recalled brain notes, brain.py recall or the brain's INDEX.md. Never read the whole brain folder.
- At /yah:wrap, write at most one brain note, with a source. A fact without evidence goes to _pending.md.

## Sessions
- One task per session. When the statusline says wrap, run /yah:wrap, then /clear.
- Prefer /clear to /compact. /compact is itself a large request.
- End each task with 2-3 plain lines: what changed, what's next, what needs you.
- Never switch model mid-session: it rewrites the whole prompt cache. Changing effort does not.

## Models and agents
- Use Fable only through /yah:deep, for a single hard, self-contained question.
- Send rote lookups (where X is, what a file, PR or log says) to the scout agent.
- Run browser and screenshot work in a subagent, never in the main thread.
- Use parallel agent workflows only for genuinely parallel work. Size them to the work: one agent per independent unit. Keep reports short.

## Safety
- Never push a trunk or production branch. Open a PR into it instead.
- Never commit .env files or secrets.
