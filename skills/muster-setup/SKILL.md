---
name: muster-setup
description: Set up or change Muster's workers and routing (~/.muster/config.json). Use for set up muster, add a worker, muster doctor.
---

# Muster setup

Goal: a working `~/.muster/config.json` (or the path in `MUSTER_CONFIG`) that `muster.py doctor` passes, written with as few questions as possible.

The runner is `../muster/muster.py` relative to this skill's base directory. Below, `$MUSTER` means that path.

## 1. Detect, don't ask

```bash
python3 $MUSTER detect
```

It prints JSON:
- `codex_homes`: each folder with a Codex login (`codexHome`, `billing`: `subscription` for a ChatGPT login, `metered` for an API key, `logged_in`).
- `commands`: `codex`, `claude`, and `claude-wrapper` scripts (they set `ANTHROPIC_BASE_URL` and run Claude Code on another backend, such as DeepSeek).
- `config_exists`: when true, read the current config first. You are editing it, not starting over: keep what the user set unless they asked to change it.

Also worth a look, read-only: shell aliases that set `CODEX_HOME` (`grep -n CODEX_HOME ~/.zshrc ~/.bashrc 2>/dev/null`) give each Codex home the name the user already calls it by. Use those names as worker names.

## 2. Propose a config

From what you found, draft workers:
- one `codex` worker per logged-in Codex home: `{ "type": "codex", "codexHome": "<path>" }`
- one `claude` worker per wrapper: `{ "type": "claude", "command": "<path>", "args": ["--dangerously-skip-permissions"], "billing": "metered" }` (drop the flag if the wrapper already passes it)
- a plain `claude` worker only if the user asks for one; it bills the same account this session uses

Then draft `routing` with a `*` fallback, `caps` (`codex` 3, `claude` 5) and `runsDir` (`~/.muster/runs`, or keep the existing one).

## 3. Ask only what detection cannot answer

Use AskUserQuestion, at most one round of up to four questions, each with a recommended default first. Typical:
- Which repos or folders should each worker prefer? (Offer the folders under the user's usual workspace, such as `~/workspace/*`, grouped by prefix.)
- Should any worker be limited to certain repos only (`only`)? Mention that the runner enforces it.
- Parallel caps per account: keep 3 / 5, or change?
- Any rule every worker prompt should carry (`preamble`)? For example, how pull requests are opened in their repos.

If the user already said these things in this conversation, do not ask again.

## 4. Write and check

1. Show the final JSON in one block and say where it goes. If a config exists, show what changes.
2. Write it. Back up an existing file first: `cp ~/.muster/config.json ~/.muster/config.json.bak-$(date +%Y%m%d-%H%M%S)`.
3. Run `python3 $MUSTER doctor`. Fix every line it prints, then run it again until it says `ok`.
4. Optional smoke test, only if the user agrees (it spends a little quota): one task per worker that replies `PONG`, run with `python3 $MUSTER run <worker> <task-dir> /tmp`; report each `status` and `usage`.

## 5. Hand off

Tell the user, in their language:
- the workers and which repos each one takes;
- which limits the runner enforces (`only`, `caps`) and which Claude follows when planning (`routing`, `preamble`);
- how to start: "muster: …" (the pane opens by itself), and `/muster-panel` to reopen it.

## Rules

- Never print the contents of `auth.json`, API key files or environment variables that hold keys. Paths and the `billing` value are enough.
- Do not log in, log out or edit any Codex or Claude profile; if a home is not logged in, tell the user the login command (`CODEX_HOME=<home> codex login`).
- Write only the Muster config (and its backup).
