---
name: muster
description: Run tasks in parallel on Codex and other CLI workers, then verify and report. Use for muster, dispatch, fan out, run in parallel.
---

# Muster

Claude plans and verifies; workers do the hands-on work. `$MUSTER` below is `muster.py` in this skill's folder.

## Config

Read `~/.muster/config.json` (or `MUSTER_CONFIG`) at the start of every run; `python3 $MUSTER workers` lists workers. No config, or `python3 $MUSTER doctor` reports problems: run the `muster-setup` skill first; never guess workers.

- `workers.<name>.type`: `codex` (`codex exec` with its `codexHome`) or `claude` (a Claude Code compatible command run with `-p`).
- `routing`: `{ match, workers, preamble? }` list. The first `match` glob (`~` allowed) that fits the cwd gives its workers, first choice first; `*` is the fallback.
- `only.<worker>`: the only globs that worker may work in; never route elsewhere, not even as a quota fallback. The runner refuses outside them (exit 3).
- `caps.<worker>` or `caps.<type>`: tasks at once per worker (default codex 3, claude 5). The runner queues over the cap.
- `preamble` and the matching `routing` entry's `preamble`: lines added to every prompt's rules block.
- The user's explicit choice always wins over the config.

Pass each task the whole worker list for its cwd (`run codex-a,codex-b …`), minus workers the user ruled out, and launch every task at once: the runner starts each on the first listed worker with a free slot. A task failed on quota or rate limit (`last_error` says so): rerun it without that worker.

Metered workers on a peak-priced model (`prices.json`; DeepSeek costs double weekdays 01:00–04:00 and 06:00–10:00 UTC): if a large, non-urgent batch would run at peak, say so in the plan and offer to run it later.

## Loop

1. **Plan.** Split the request into independent tasks, each with a goal, scope and done check. Show a table (id, worker, cwd, goal) and start without waiting unless the request is ambiguous. Size each task so one run can finish it; work that needs design judgment gets the design written into the brief, or stays with Claude.
2. **Isolate.** Parallel writers to one repo each get a worktree: `git -C <repo> worktree add ../<repo>-<task-id> -b muster/<task-id>`. One writer, or read-only tasks, may share the repo.
3. **Prepare.** Task dir: `<runsDir>/<YYYYMMDD-HHMM>-<slug>/<task-id>/` (`runsDir` default `~/.muster/runs`). `<task-id>` is a kebab-case slug (`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`, max 63 chars) from the title's English core, e.g. `fix-pr-association-wait`; a retry appends `-r2`, `-r3`. It labels the task in the pane, names it in `--after` and ends its `muster/<task-id>` branch, so keep it stable.
   Write `prompt.md`. Workers start cold and run once, so it is a full brief:
   - **Goal and why**: the outcome and where it fits in the larger change.
   - **Decisions made**: settle approach, names and data shapes now; a worker left to choose often picks what you would reject.
   - **Where to look**: cwd, files expected to change, interfaces to call, one existing file that shows the pattern to follow.
   - **Not this task**: what sibling tasks own and what to leave alone.
   - **Done check**: a command you write that proves the outcome. Prefer end to end (the real program driven as a user would: CLI, API, browser, simulator); for a migration or refactor, compare old and new outputs on the same inputs; for an invariant such as encode-then-decode, a property test. Use the repo's verify skill or script if it has one. Confirm it can run in this cwd before launch.

   First line: `# <short task title>` in the user's language, at most 60 characters (the pane's task name). Then:

   ```
   Rules for this task:
   - Do not commit, push, rebase, or open a pull request unless this task says so.
   - Never write CI skip markers ([skip ci], [ci skip], [no ci], [skip actions], [actions skip], skip-checks: true) in a commit message unless this task says the commit is meant to skip CI.
   - No new dependencies unless this task says so.
   - When a planned step finishes, print a line of its own: `Progress: <steps done>/<steps planned>`.
   - Change only what this task needs, plus files the change strictly requires (callers, tests, types); list each extra file with its reason. Never edit what "Not this task" lists. Report BLOCKED instead of guessing when the task needs a design choice it does not settle, or more than a few files beyond the expected ones.
   - Follow the codebase's patterns and reuse its helpers. No refactors, renames or cleanups outside the task; no options, abstractions or fallbacks "for later".
   - Tests check real behavior, not mocks, and fail if the change is reverted. Never weaken, skip or delete a test to make it pass. A flaky test is not fixed by rerunning, sleeps or retries: find the cause, or leave it failing and say why.
   - Before reporting, read your whole diff once for missing or unasked-for work, debug leftovers, duplicated logic and swallowed errors; fix them.
   - Run the done check yourself and report its real output. If it fails, say so.
   - You run once: finish the task or state what is left. Saying the task is too hard or under-specified is fine; handing in work you are unsure of without saying so is not.
   - End with a report whose first line is exactly one of `Status: DONE`, `Status: DONE_WITH_CONCERNS`, `Status: BLOCKED`, `Status: NEEDS_CONTEXT`, then the files changed (extra files with their reason), the done check's real output tail, and concerns or what is left.
   ```

   Then the config's `preamble` lines, the matching `routing` entry's, and any repo rule from the user's own instructions for this cwd (workers do not see Claude's CLAUDE.md or memory).
4. **Launch.** Each task in its own Bash call with `run_in_background: true`, all in one message, dependent ones too:
   `python3 $MUSTER run <worker[,worker…]> <task-dir> <cwd> [--after A,B] [--priority N] [--check "<done check>"] [--retry-of <task-dir>]`
   - `--retry-of <task-dir>`: give it on every rerun of earlier work, whatever its id or run: the task dir of the attempt it replaces. Progress counts both as one piece of work and the newest attempt decides.
   - `--after A,B`: wait until sibling tasks A and B are `done`; if one fails or is canceled, this one is canceled (exit 5).
   - `--priority N` (default 0): on a full worker, higher starts first.
   - `--check`: give it for every done check that is a command. After the worker exits successfully, the runner runs it in the cwd (`sh -c`, one check at a time per `runsDir`), writes `check.txt` and `check=pass|fail (exit N)` to `meta`, and a failure makes the task `failed: done check failed …`; a failed worker gets `check=skipped` and no `check.txt`. A worker that exits 0 but whose report opens with `Status: BLOCKED` or `Status: NEEDS_CONTEXT` gets the same skip and ends `failed: worker reported BLOCKED` (or `NEEDS_CONTEXT`), so tasks `--after` it are canceled. That exit code is the test evidence, not the worker's report. Keep it narrow (the touched modules' tests, a typecheck), since the worker usually ran it too.
   - The runner writes `status`, `meta`, `log.txt` or `stream.jsonl`, `result.md`, `usage`, `check.txt`, and `changes.diff` when the cwd is in a git repo.
   - `python3 $MUSTER cancel <task-dir> "<reason>"` stops a task that is no longer needed; never write `status` by hand.
5. **Wait.** Tasks notify on exit; do not poll. If asked meanwhile, read each `status`; `/muster-panel` shows live progress. After launching, also start `python3 $MUSTER watch <run-dir> [--stall-min N]` (default 10) in its own background Bash call. It exits when a running worker's log has gone silent, printing the task, its log path and last lines, or when every task has finished. On a stall, read the log tail and decide: leave it if a long command is legitimately running, otherwise `cancel` and relaunch or ask the user. Then start `watch` again, until it reports all tasks finished. Each silence is reported once, and `watch` itself stops nothing.
6. **Verify.** The failure to avoid, seen in past runs: merging after reading only `result.md`, `git log`, `--stat` or a diff piped to `head`, on the worker's word. A worker's report is a claim. No task is merged, deployed or reported done until its whole diff is read and `review.md` records it; the pane lists a task under Done only once its review approves the current diff; until then it sits under `Needs review`, or `Needs fixes` when `review.md` says so. The `status` file still says `done`: the worker and its check succeeded, which is what `--after` waits for. Merge only when the user asks, and only reviewed tasks.
   - **The runner's evidence.** `changes.diff` is everything that changed in the repo between the worker's start and exit: its commits, edits and new files, without edits already uncommitted before it started. It cannot tell who made a change: if `meta` has `shared_with` (other tasks ran in that repo meanwhile), check each file against this task's brief before calling it extra, and never edit a repo yourself while a worker runs in it. `meta` also has `changes` (`N files +A -B`), `changes_sha256`, `branch`, `commits`, `check`, `tests_touched`, and `branch_start` if the worker switched branches. `changes_missing` names what the diff lacks (edits inside submodules). `changes` ending in `(cut)`: the diff stops at 5 MB and its last line is the command for the rest; read that too, or hand it to a reviewer. `changes=unavailable: <reason>`: diff from `base_head` yourself (`git -C <repo> diff <base_head>` plus `status` for new files), or mark the task `needs review`.
   - **Read** `result.md` (no `Status:` line: treat as `DONE_WITH_CONCERNS`), `check.txt` if there is one, then all of `changes.diff` with the Read tool (page with `offset`/`limit`), never through `head`, `tail` or `grep`.
   - **Review against `prompt.md`**, in two passes:
     - **Spec**: every requirement maps to a hunk. Flag **missing**, **extra** (not asked for) and **misread** (wrong problem or approach). What the diff cannot show goes under **Cannot verify**.
     - **Quality**: codebase patterns, no duplicated logic or swallowed errors, edge cases handled, tests that fail without the change, no file grown far past the task. Style is Minor.
     Rate each finding Critical, Important (task cannot be trusted until fixed) or Minor. A problem in code the task did not change is Pre-existing: report it, do not send it back.
   - **The change must not grade itself.** Read the hunks in `tests_touched` (changed tests and files the `--check` runs) first: a weakened assertion, a skipped or deleted test or a loosened check script is Critical unless the brief asked for it. Never rerun a failed check hoping for green; a flaky check is a finding.
   - **A fresh reviewer** for a diff over about 300 lines or 8 files, or when your context is long: an Agent subagent (`general-purpose`) given the paths of `prompt.md`, `result.md`, `check.txt` and `changes.diff`, told to read the whole diff, treat `result.md` and the worker's reasons as unverified claims, review against the brief rather than taste, cite `file:line`, rate each finding, list what it cannot verify, and end with `Verdict: Approved` or `Verdict: Needs fixes`. Do not tell it what you expect to find. Confirm its Critical and Important findings in the code yourself.
   - **Record** `review.md` in the task dir:
     ```
     Reviewer: <Claude, or reviewer subagent>
     Verdict: <Approved, or Needs fixes>
     changes-sha256: <changes_sha256 from meta>
     Findings: <severity, file:line, problem; or none>
     Cannot verify: <items; or none>
     ```
     `Approved` needs no open Critical or Important finding and every Cannot verify item settled (run it, check it, or ask the user). `Verdict: User override` only when the user explicitly said to merge without review.
   - Tasks in one repo: compare the files in their `changes.diff`; an overlap is a merge conflict to plan.
   **Fix loop.** Critical or Important findings, or `failed: done check failed`: write `<task-id>-r2` (launched with `--retry-of` the task it fixes) repeating the original brief plus each finding at `file:line` (or the end of `check.txt`) and what is wanted, and run it in the same worktree. Its diff holds only the fix; review it against the brief and the findings. Once approved, set the original's `review.md` to `Verdict: Approved` with `Fixed in: <task-id>-r2`, keeping its own `changes-sha256`. Tasks canceled because they ran `--after` the failed one do not restart by themselves: launch them again with `--after <task-id>-r2`. `failed: worker reported BLOCKED` or `NEEDS_CONTEXT`: `result.md` says why; answer in the follow-up, or split the task. After three failed rounds (the first run and two fixes), stop: use a stronger worker, do it yourself, or ask the user.
   **Empty diff.** The pane treats a reviewed task with no changes as a finished read-only review. If the task was meant to change code, run `<task-id>-r2` instead of recording it as reviewed. For a read-only task the report is the result: confirm its Critical and Important findings in the code, then write `review.md` with meta's `changes_sha256`, the confirmed findings under `Findings`, and `Verdict: Needs fixes` if any stay open or `Verdict: Approved` if none do. Either verdict finishes the task; open findings become follow-up work.

   **Record before moving on.** Confirming findings in the conversation does not finish a task: until `review.md` exists the pane shows it as waiting. Write it before you report the run or start other work. When a turn ends with a finished run still waiting, the pane sends this session back once per task.
   **Learn from misses.** The same kind of miss caught twice in one project: propose one line for that project's `routing[].preamble` and add it once the user agrees. Drop preamble lines that no longer fire.
7. **Report** as below. Merge, commit and push only when asked; after a `muster/*` branch is merged, offer to remove its worktree. Asked what can be cleaned up: show `python3 $MUSTER clean` (it deletes nothing) and remove only what the user picks.

## Report format

```
| id | worker | task | status | verified | tokens | cost | result |
|----|--------|------|--------|----------|--------|------|--------|
| A  | codex  | ...  | done / running / queued / failed / canceled / needs review / needs fixes | check pass · review subagent / none | in 1.5M (95% cached) · out 14k | ≈$2.91 | one line |
```

Then, in the user's language:
- **Results**: what each task produced, with paths or diff stats.
- **Usage**: from each `usage` file. Total tokens; metered money as `$` (say if at peak price), subscription work as `≈$` (API-equivalent).
- **Verification**: per task, who read the whole diff and the verdict in `review.md`, plus the commands you ran and their result; `none` when nothing was verified. "The worker says tests pass" is not verification.
- **Open / risks**: anything skipped, failed or needing a decision; for each failure, meta's `last_error` and `log_path` (on `disk full (ENOSPC)`, `result.md` ends with the rerun command).
- **Next**: concrete options (merge worktree X, rerun B, …).

## Notes

- Codex workers run `codex exec --dangerously-bypass-approvals-and-sandbox`; claude workers run their configured `command` and `args`, usually with permissions skipped. Workers can run any command: keep them in repos the user trusts, and never put secrets in prompts.
- Codex workers still load their `codexHome`'s `AGENTS.md`, config and skills; wrappers such as oh-my-codex add hooks that plain `codex exec` does not run.
