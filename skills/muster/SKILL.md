---
name: muster
description: Run tasks in parallel on Codex and other CLI workers, then verify and report. Use for muster, dispatch, fan out, run in parallel.
---

# Muster

Claude is the planner and verifier. Workers do the hands-on work. Every run follows the same loop and the same report format, so the user never has to explain it again.

The runner is `muster.py` in this skill's base directory (the folder this file is in). Below, `$MUSTER` means that path.

## Workers and routing come from the config

Read `~/.muster/config.json` (or the path in `MUSTER_CONFIG`) at the start of every run. `python3 $MUSTER workers` lists the workers. If there is no config, or `python3 $MUSTER doctor` reports problems, run the `muster-setup` skill first; do not guess workers.

- `workers.<name>.type`: `codex` (runs `codex exec` with that worker's `codexHome`) or `claude` (runs a Claude Code compatible command with `-p`).
- `routing`: a list of `{ match, workers }`. The first `match` glob (`~` allowed) that fits the task's cwd gives the worker priority, first choice first. `*` is the fallback.
- `only.<worker>`: globs that worker may work in. Never route any other project to it, not even as a quota fallback. `muster.py` enforces it: outside those globs it refuses (exit 3, status `failed: refused, …`).
- `caps.<worker>` or `caps.<type>`: the most tasks one worker runs at once (default codex 3, claude 5). `muster.py` enforces it: over the cap the task waits with status `queued` and starts by itself when a slot frees.
- `preamble`: extra lines appended to the rules block of every prompt.
- The user's explicit choice in the request always wins over the config.

Assigning parallel tasks: pass each task the whole worker list `routing` gives its cwd, first choice first (`run codex-a,codex-b …`), minus workers the user ruled out. The runner starts it on the first listed worker with a free slot, and while every one is full it waits on all of them, so a task never sits behind one busy worker while another is idle. Launch every task at once; do not pick one worker per task to spread the load. When a worker fails on quota or rate limit (see `log.txt`), rerun that task without that worker in its list.

Metered workers on a peak-priced model (see `prices.json`, e.g. DeepSeek: weekdays 01:00–04:00 and 06:00–10:00 UTC cost double): when a large, non-urgent batch would run in peak hours, say so in the plan and offer to run it later. The user decides.

## Loop

1. **Plan.** Split the request into independent tasks, each with a clear goal, scope (files or directories) and a done check. Show the plan as a table (id, worker, cwd, goal) and start without waiting, unless the request is ambiguous.
2. **Isolate.** Tasks that write to the same repo in parallel (on one worker or several) each get their own worktree: `git -C <repo> worktree add ../<repo>-<task-id> -b muster/<task-id>`. A single writer, or read-only tasks, can share the repo.
3. **Prepare.** Run dir: `<runsDir>/<YYYYMMDD-HHMM>-<slug>/<task-id>/` (`runsDir` from the config, default `~/.muster/runs`). Write `prompt.md` there. Workers start cold, so the prompt holds everything: goal, cwd, files in scope, constraints, the done check, and "end with a short summary: what changed, how it was verified, open issues". The first line is `# <short task title>` in the user's language (the pane shows it), then the rules block:

   ```
   Rules for this task:
   - Do not commit, push, rebase, or open a pull request unless this task says so.
   - Never write CI skip markers ([skip ci], [ci skip], [no ci], [skip actions], [actions skip], skip-checks: true) in a commit message unless this task says the commit is meant to skip CI.
   - No new dependencies unless this task says so.
   - Stay inside the files in scope. If the task needs more, stop and say so in the summary.
   - Run the done check yourself before finishing and report its real output. If it fails, say so; do not claim success.
   - You run once, with no follow-up turn: finish the whole task, or state clearly what is left.
   ```

   Then each line of the config's `preamble`, and any repo rule from the user's own instructions that applies to this cwd (workers do not see Claude's CLAUDE.md or memory).
4. **Launch.** For each task, call Bash with `run_in_background: true`:
   `python3 $MUSTER run <worker[,worker…]> <task-dir> <cwd> [--after A,B] [--priority N]`
   Launch all tasks in the same message, dependent ones too. `--after A,B`: wait until sibling tasks A and B (same run dir) are `done`; if one fails or is canceled, this task is canceled (exit 5) without running. `--priority N` (default 0): on a full worker, higher N starts first; equal N start in launch order. The runner writes `status` (queued, … / running / done / failed: … / canceled: …), `meta`, `log.txt` (codex) or `stream.jsonl` (claude), `result.md` and `usage`.
   **Cancel** a task that is no longer needed (superseded, out of scope) with `python3 $MUSTER cancel <task-dir> "<reason>"`; never write `status` by hand. It stops the worker if it runs and records `canceled: <reason>`, which the pane and report do not count as a failure.
5. **Wait.** Background tasks notify on exit, so do not poll. If the user asks in the meantime, read each `status` and report; the Muster pane (`/muster-panel`) shows live progress.
6. **Verify.** Read each `result.md`, then check the work yourself: the diff (`git -C <cwd> diff`), the build or tests the task names. A worker's claim is not evidence. Failed or wrong: write a follow-up prompt with the concrete problem and rerun on the same or another worker.
7. **Report** in the format below. Merging worktrees, committing and pushing happen only when the user asks; once a `muster/*` branch is merged, offer to remove its worktree.

## Report format

```
| id | worker | task | status | tokens | cost | result |
|----|--------|------|--------|--------|------|--------|
| A  | codex  | ...  | done / running / queued / failed / canceled / needs review | in 1.5M (95% cached) · out 14k | ≈$2.91 | one line |
```

Then:
- **Results**: what each task produced, with file paths or diff stats.
- **Usage**: from each task's `usage` file. Total tokens for the run, then metered money as `$` (say when it ran at peak price) and subscription work as `≈$` (API-equivalent; not billed per token).
- **Verification**: what Claude checked and the result (commands run, pass or fail).
- **Open / risks**: anything skipped, failed, or needing the user's decision.
- **Next**: concrete options (merge worktree X, rerun B, ...).

Write the report in the user's language.

## Notes

- Codex workers run `codex exec --dangerously-bypass-approvals-and-sandbox`; claude workers run whatever `command` and `args` the config gives (usually with permissions skipped). Workers can run any command: keep them in repos the user trusts, and never put secrets in prompts.
- Codex workers still load their `codexHome`'s `AGENTS.md`, config and skills; wrappers such as oh-my-codex add runtime hooks that plain `codex exec` does not run.
