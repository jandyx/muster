# AGENTS.md

Guidance for coding agents (Claude Code, Codex, others) working **on** this repository. To **use** Muster, see the README.

## What this is

A Claude Code plugin with three parts:

| Part | Path | Language |
|---|---|---|
| `muster` skill (the dispatch loop Claude follows) | `skills/muster/SKILL.md` | Markdown |
| `muster-setup` skill (writes the user's config) | `skills/muster-setup/SKILL.md` | Markdown |
| Runner: run a task, enforce `only` / `caps`, price usage, `detect`, `doctor` | `skills/muster/muster.py` | Python 3.10+, standard library only |
| Pane and band (Claude Code mod) | `hooks/register.tsx`, `types/index.d.ts` | TypeScript, Claude Code hooks API |
| Prices | `prices.json` | JSON, USD per 1M tokens |

The runner and the pane talk only through files in a task folder: `prompt.md`, `meta` (key=value), `status`, `log.txt` or `stream.jsonl`, `result.md`, `usage` (JSON). A change to any of these is a change to both sides.

## Checks

Run all three before you finish; each must pass. CI (`.github/workflows/ci.yml`) runs the same on every push and pull request, plus an English-only and JSON check.

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests   # runner: usage math, peak, fences, caps, detect, doctor, a full run
claude plugin test .                      # pane and band
claude plugin validate .                  # manifest, hooks module, marketplace
```

## Rules

- English only in code, comments, docs, tests and commit messages.
- `muster.py` stays dependency-free. No pip packages.
- New behaviour in `muster.py` gets a test in `tests/test_muster.py`; new pane rows get an assertion in `tests/pane.test.tsx`.
- When a stored value's shape changes in `hooks/register.tsx`, bump that atom's `shape` tag (`runs-6` → `runs-7`) so old stored values are declined.
- Colors in the pane are theme keys (`success`, `merged`, …), never hex, so both terminal themes work.
- Prices in `prices.json` carry their source and check date in `_note`; update both together.
- Keep user data out of the repo: no real account names, paths, repos or branches in docs, tests or screenshots. `assets/screenshot.html` is the source of `assets/screenshot.png`.
- Workers run with approvals off. Do not widen what the runner does without saying so in the README's Safety section.
