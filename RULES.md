# Working rules (you-are-here)

## Orient
- Orient with /yah:where or the state injected at session start. Never read long docs just to find out where things stand.
- For what a phase changes, read the phase map (/yah:where, /yah:tree P<n>), not its diff. In a plan, give each phase an `After it merges:` line and a `Files:` block.

## Brain
- For project facts, use the recalled brain notes, brain.py recall or the brain's INDEX.md. Never read the whole brain folder.
- At /yah:wrap, write at most one brain note, with a source. A fact without evidence goes to _pending.md.

## Sessions
- One task per session. When the statusline says wrap, run /yah:wrap, then /clear, or close the session when wrap started the run.
- Prefer /clear to /compact. /compact is itself a large request.
- End each task with 2-3 plain lines: what changed, what's next, what needs you.
- Give each step the user must take as the exact command to paste in a terminal, e.g. `gh pr merge 13 --merge`.
- Never switch model mid-session: it rewrites the whole prompt cache. Changing effort does not.

## Models and agents
- Use Fable only through /yah:deep, for a single hard, self-contained question. With the quality profile on, yah run also uses it once per phase as critic and judge, unless codex is on. Never put it in a workflow.
- Send rote lookups (where X is, what a file, PR or log says) to the scout agent.
- Run browser and screenshot work in a subagent, never in the main thread.
- Use parallel agent workflows only for genuinely parallel work. Size them to the work: one agent per independent unit. Keep reports short.

## GPT (codex on, setup.py --codex)
- Send multi-source web research to /yah:gpt research, as a self-contained question. A single lookup stays in Claude.
- With the quality profile on, GPT critiques and judges yah run's phases, unless config roles.critic names a Claude model. If codex fails, that review reruns on roles.review_fallback.
- A GPT finding is a claim: prove it by reading the code or running a test before fixing it.
- Never enable the Codex plugin's review gate (`--enable-review-gate`): it loops and burns both plans.

## Safety
- Never push a trunk or production branch. Open a PR into it instead.
- Never commit .env files or secrets.
