# Proposal: what Muster can take from agy swarm and magpie

Status: proposal, nothing built yet.

## The three tools

- **agy `/swarm`** (preview in the Antigravity CLI 1.5.18). The user types `/swarm <task>`; a coordinator agent plans, agy workers carry out the tasks and post to a shared board, and a `@verifier` checks the work of every 2 to 4 workers. Workers share the workspace unless the coordinator gives one its own copy. A worker that runs out of quota pauses until someone acts. `/swarm board` shows progress; there is no per-worker cost report. Source: `agy --help`, `agy changelog`, <https://antigravity.google/docs/swarm.md>.
- **magpie, [yetone/magpie](https://github.com/yetone/magpie).** A local gateway that pools several AI subscriptions behind one endpoint for any coding agent, moving a request to the next account or model when one runs out, with cost and quota shown in a desktop app, terminal UI or web UI. It plans and reviews nothing.
- **magpie, [liliu-z/magpie](https://github.com/liliu-z/magpie).** A multi-model pull request reviewer: several models review, debate, and a verifier checks each claim against the code by searching it. It shows an estimated cost per role.

## Comparison

| | Muster | agy swarm | yetone/magpie | liliu-z/magpie |
|---|---|---|---|---|
| Who plans | Claude, in the user's session | a coordinator agent | nobody | fixed review roles |
| Who works | Codex, Claude-compatible and agy workers, mixed vendors | agy workers only | any agent, through the gateway | the review models |
| Isolation | a git worktree per parallel writer | shared workspace unless the coordinator gives a copy | n/a | not verified |
| Verification | a done check run by the runner, then Claude reviews the full diff | a `@verifier` per 2 to 4 workers | none | a verifier checks findings against the code |
| Out of quota | the worker is skipped, the task moves on | the worker pauses | the request moves to the next account | not verified |
| Cost | per task, worker and run, in the pane and `stats` | none found | per request, in its own UI | estimated per role |
| How it starts | the user asks for muster, then it stays on | `/swarm <task>` | always on, as the endpoint | per pull request |

## Ideas, best value for effort first

1. **A second model family reviews high-tier work.** For `high` tasks, launch an independent review task on a worker of another vendor than the one that wrote the change; Claude confirms each finding against the code before acting on it, as liliu-z/magpie's verifier does. Small: `skills/muster/SKILL.md` (step 6).
2. **Reviews say what they did not check.** Ask every review brief to end with one line naming what it did not examine. It keeps a clean review from reading as full coverage. Tiny: `skills/muster/SKILL.md`.
3. **magpie as a backend for `claude` workers.** Document pointing a Claude-compatible worker at a yetone/magpie endpoint, so one worker draws on several subscriptions. Its pooled model names are not in `prices.json`, so such tasks show as partly priced. Docs only: `README.md`.
4. **Prefer the quota that resets soonest.** Among workers with quota left, start a task on the one whose window resets first, so quota that would expire unused is spent first. Medium: `skills/muster/muster.py` plus a test.
5. **Keep briefs from starting a swarm.** Pass `--disable-slash-commands` to agy workers, as an option, so text in a brief can never start `/swarm` inside a worker. Small: worker `args` in the README's agy example.

## What not to copy

- A shared board between workers: briefs and the review keep Claude the single place where decisions are made.
- A shared workspace by default: worktrees are what make parallel writers safe.
- Pausing on quota: skipping to the next worker keeps the batch moving.
- Debate rounds between reviewers: costly, and Claude's own review already settles disagreements.
- Posting to pull requests automatically: opening and commenting stay with the user.

## Not verified

Swarm's cost reporting, whether swarm's copies are git worktrees, isolation in liliu-z/magpie, and the details of yetone/magpie's routing (taken from its README only).
