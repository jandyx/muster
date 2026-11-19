# Muster docs

Start with the [README](../README.md) to install and use Muster. These pages go deeper.

## Understand it

| Page | What it covers |
|---|---|
| [Design](design.md) | Principles, who enforces what, and why each major choice was made |
| [How it works](how-it-works.md) | Diagrams: architecture, one run's sequence, a task's states, what the pane reads, worker and tier selection with failure handling, cost |

## Use and configure it

| Topic | Where |
|---|---|
| Install and first run | [README: Quick start](../README.md#quick-start) |
| Config keys: workers, routing, `only`, `caps`, tiers, preamble | [README: Configure](../README.md#configure), [example config](../examples/config.example.json) |
| Set up by asking Claude | the [`muster-setup` skill](../skills/muster-setup/SKILL.md) |
| The dispatch loop Claude follows | the [`muster` skill](../skills/muster/SKILL.md) |
| The pane and band | [README: The pane](../README.md#the-pane) |
| Tokens, cost and prices | [README: Tokens and cost](../README.md#tokens-and-cost), [`prices.json`](../prices.json) |
| How tiers perform (`stats`) | [README: How the tiers perform](../README.md#how-the-tiers-perform) |
| Safety, FAQ, limits | [Safety](../README.md#safety), [FAQ](../README.md#faq), [Limits](../README.md#limits) |

## Work on it

| Topic | Where |
|---|---|
| Layout, checks, rules, releases | [AGENTS.md](../AGENTS.md) |
| Runner command reference | the docstring at the top of [`muster.py`](../skills/muster/muster.py) |
| Tests | [`tests/`](../tests) |
