# How Muster works

Diagrams of the moving parts. The [README](../README.md) covers install, config and use.

## Architecture

```mermaid
flowchart LR
    you([You]) <--> claude["Claude Code<br/>plans · verifies · reports"]
    claude -- "muster skill:<br/>python3 muster.py run" --> runner[muster.py]
    cfg[(~/.muster/config.json<br/>workers · routing · caps)] -.-> claude
    cfg -.-> runner
    runner -- "codex exec<br/>CODEX_HOME=…" --> cx1[Codex worker]
    runner -- "codex exec" --> cx2[Codex worker 2]
    runner -- "claude -p<br/>stream-json" --> ds[Claude-compatible worker<br/>e.g. DeepSeek]
    cx1 & cx2 & ds --> runs[("runs/&lt;run&gt;/&lt;task&gt;/<br/>prompt · status · meta<br/>log · result · usage")]
    prices[(prices.json)] -.-> runner
    runs --> pane["/muster pane<br/>+ band above prompt"]
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
        C->>R: run codex-work A
        R->>W: codex exec (prompt on stdin)
    and
        C->>R: run deepseek B
        R->>W: claude -p --output-format stream-json
    end
    loop every 5 s
        P->>P: read status · log / stream · usage
    end
    W-->>R: exit
    R->>R: usage from rollout / result event, priced
    R-->>C: background task finished
    C->>C: read result.md, check the diff and tests
    C->>U: report: results · usage · verification · next steps
```

A task's life, as the pane shows it:

```mermaid
stateDiagram-v2
    [*] --> running: muster.py run
    running --> running: ↻ new note / $ new command
    running --> quiet: no output for 2 min
    quiet --> running: output again
    running --> done: exit 0
    running --> failed: exit ≠ 0
    done --> [*]
    failed --> running: Claude reruns it (same or next worker)
```

What the pane reads for each task:

```mermaid
flowchart TB
    subgraph task["runs/&lt;run&gt;/&lt;task&gt;/"]
        prompt["prompt.md<br/># title"]
        meta["meta<br/>worker · type · cwd · session · started · finished"]
        status[status]
        live["log.txt (codex)<br/>stream.jsonl (claude)"]
        usage["usage<br/>tokens · usd · billing · peak"]
        result[result.md]
    end
    prompt --> row1["✓ / … / ✗  title"]
    status --> row1
    meta --> row2["id · worker · status · elapsed · ⎇ branch"]
    usage --> row3["in 3.65M (97% cached) · out 12k · ≈$5.17"]
    live --> row4["↻ latest note<br/>$ latest command<br/>no output for … (stall)"]
    result --> row5["↳ one-line result"]
    meta -- "session = this session?" --> filter{shown}
```

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
