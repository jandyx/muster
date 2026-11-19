# How Muster works

Diagrams of the moving parts. The [README](../README.md) covers install, config and use; [Design](design.md) explains why it is built this way.

## Architecture

```mermaid
flowchart LR
    you([You]) <--> claude["Claude Code<br/>plans · verifies · reports"]
    claude -- "muster skill:<br/>python3 muster.py run" --> runner[muster.py]
    cfg[(~/.muster/config.json<br/>workers · routing · caps · tiers)] -.-> claude
    cfg -.-> runner
    runner -- "codex exec<br/>CODEX_HOME=…" --> cx1[Codex worker]
    runner -- "codex exec" --> cx2[Codex worker 2]
    runner -- "claude -p<br/>stream-json" --> ds[Claude-compatible worker<br/>e.g. DeepSeek]
    cx1 & cx2 & ds --> runs[("runs/&lt;run&gt;/&lt;task&gt;/<br/>prompt · status · meta<br/>log · result · usage<br/>changes.diff · check.txt · review.md")]
    prices[(prices.json)] -.-> runner
    runs --> pane["Muster pane (/muster-panel)<br/>+ band above prompt"]
    pane --> you
```

One run, start to finish:

```mermaid
sequenceDiagram
    actor U as You
    participant C as Claude Code
    participant R as muster.py
    participant W as Worker (codex / claude)
    participant P as Pane
    U->>C: "muster: do A, B and C"
    C->>C: plan table, worktrees, prompt.md per task
    par one background Bash per task
        C->>R: run codex-work A --check "npm test …"
        R->>R: snapshot the repo
        R->>W: codex exec (prompt on stdin)
    and
        C->>R: run deepseek B
        R->>W: claude -p --output-format stream-json
    end
    loop every 5 s
        P->>P: read status · log / stream · usage
    end
    W-->>R: exit
    R->>R: snapshot again → changes.diff · model · usage, priced
    R->>R: run the done check → check.txt (a failure fails the task)
    R-->>C: background task finished
    C->>C: read the whole changes.diff (or a fresh reviewer does), review against the brief
    C->>C: write review.md, findings → follow-up task -r2
    C->>U: report: results · usage · verified · next steps
```

A task's life, as the pane shows it:

```mermaid
stateDiagram-v2
    [*] --> running: muster.py run
    running --> running: ↻ new note / $ new command
    running --> quiet: no output for 2 min
    quiet --> running: output again
    running --> checking: worker exits, done check runs
    checking --> needs_review: check passes
    running --> needs_review: exit 0, no check
    running --> failed: exit ≠ 0, or the worker reports BLOCKED / NEEDS_CONTEXT
    checking --> failed: check fails
    needs_review --> needs_fixes: review.md asks for fixes on this changes.diff
    needs_review --> done: review.md approves this changes.diff
    needs_fixes --> done: a later review approves it
    done --> [*]
    failed --> running: Claude reruns it (same or next worker)
```

What the pane reads for each task:

```mermaid
flowchart TB
    subgraph task["runs/&lt;run&gt;/&lt;task&gt;/"]
        prompt["prompt.md<br/># title"]
        meta["meta<br/>worker · model · cwd · session · started · finished<br/>changes · changes_sha256"]
        status[status]
        live["log.txt (codex)<br/>stream.jsonl (claude)"]
        usage["usage<br/>tokens · usd · billing · peak"]
        result[result.md]
        review[review.md]
    end
    prompt --> row1["✓ / … / ✗  title"]
    status --> row1
    meta --> row2["id · worker · model · status · elapsed · ⎇ branch"]
    review --> row1b["section and mark: Needs review, Needs fixes, or Done once it approves this diff"]
    usage --> row3["in 3.65M (97% cached) · out 12k · ≈$5.17"]
    live --> row4["↻ latest note<br/>$ latest command<br/>no output for … (stall)<br/>running done check · 3m08s (from meta)"]
    result --> row5["↳ one-line result"]
    meta -- "session = this session?" --> filter{shown}
```

## How a task gets its worker, and what happens when it fails

```mermaid
flowchart TB
    plan["Claude plans a task"] --> tier{"tiers in config?"}
    tier -- no --> list
    tier -- yes --> pick["pick low / standard / high<br/>(routing when, tiers when, or the skill's rubric)"]
    pick --> list["workers from routing<br/>(user's choice wins)"]
    list --> fence["only: drop fenced workers<br/>none left → refused, exit 3"]
    fence --> prefer["tier prefer: move to front"]
    prefer --> queue["queue: first listed worker<br/>under its cap takes it"]
    queue --> run["run with worker args + tier args"]
    run --> outcome{outcome}
    outcome -- "check fails / Needs fixes" --> task["failure=task<br/>fix round, same tier;<br/>up one tier if the tier proved too weak (max 2)"]
    outcome -- "BLOCKED / NEEDS_CONTEXT" --> blocked["failure=blocked<br/>answer or split, same tier"]
    outcome -- "quota, rate limit, auth" --> provider["failure=provider<br/>another worker, same tier"]
    outcome -- "disk, network, crash" --> infra["failure=infra<br/>rerun, same tier"]
    outcome -- "signal" --> interrupted["failure=interrupted<br/>rerun, same tier"]
    outcome -- "approved" --> done([Done])
```

Provider, infra and interrupted failures do not count toward the three-round limit (the first run and two fixes); after three failed rounds, Claude switches to a stronger worker, does it itself or asks. Two failures outside the task in a row on one piece of work also stop the reruns and go to the user, and a context or turn limit counts as a task failure.

## How a task's cost is worked out

```mermaid
flowchart LR
    A[task usage] --> B{worker billing}
    B -- "metered<br/>(API key, DeepSeek)" --> C["$ billed<br/>green"]
    B -- "subscription<br/>(Codex on ChatGPT login)" --> D["≈$ API-equivalent<br/>not charged per token"]
    C --> E{peak-priced model<br/>and started in peak hours?}
    E -- yes --> F["× peak rate · marked peak"]
```

Prices come from [`prices.json`](../prices.json), overridden by `~/.muster/prices.json`.
