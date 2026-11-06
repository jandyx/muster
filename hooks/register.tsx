import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Run, Task, Usage } from '../types'

const PANE = 'muster'
const TITLE = 'Muster'
const POLL_MS = 5000

// Change a tag when its value's fields change, so a reload declines old values.
const runsAtom = atom({ plugin: 'muster', key: 'runs' } as const, [], { shape: 'runs-7' })
// Fold the person chose, per run (`<run>`) and per status section (`<run>/<label>`). A run without an entry
// folds once nothing in it runs; a section without one stays open.
const foldsAtom = atom({ plugin: 'muster', key: 'folds' } as const, {}, { shape: 'folds-1' })
const notifiedAtom = atom({ plugin: 'muster', key: 'notified' } as const, [], { shape: 'notified-1' })

const isRunning = (t: Task) => t.status === 'running'
const isFailed = (t: Task) => t.status.startsWith('failed')
const isCanceled = (t: Task) => t.status.startsWith('canceled')
const isFinished = (t: Task) => t.status === 'done' || isFailed(t) || isCanceled(t)
// Queued by the runner (waiting for a slot or a dependency), or `pending`: the folder exists, no runner yet.
const isQueued = (t: Task) => !isRunning(t) && !isFinished(t)
const isActive = (t: Task) => isRunning(t) || isQueued(t)
// Queued tasks in the order they will start: ready before waiting on a dependency, then higher priority,
// then first queued; tasks without a runner yet go last.
const startOrder = (a: Task, b: Task) => {
  const rank = (t: Task) => [t.status === 'pending' ? 2 : t.status.includes('waiting for') ? 1 : 0, -t.priority, t.queuedMs]
  const [x, y] = [rank(a), rank(b)]
  return x[0]! - y[0]! || x[1]! - y[1]! || x[2]! - y[2]!
}
// Pane sections inside a run, in display order; a task falls into the first whose test it passes.
const SECTIONS: [string, string | undefined, (t: Task) => boolean][] = [
  ['Running', 'yellow', isRunning],
  ['Queued', 'cyan', isQueued],
  ['Failed', 'red', isFailed],
  ['Canceled', undefined, isCanceled],
  ['Done', 'green', () => true],
]
const mark = (t: Task) => (t.status === 'done' ? '✓' : isRunning(t) ? '…' : isFailed(t) ? '✗' : isCanceled(t) ? '⊘' : '○')
const markColor = (t: Task) => (t.status === 'done' ? 'green' : isRunning(t) ? 'yellow' : isFailed(t) ? 'red' : isQueued(t) ? 'cyan' : undefined)

// meta holds local "YYYY-MM-DD HH:MM:SS" from run-worker.sh
const parseTime = (s: string | undefined) => {
  if (!s) return 0
  const ms = new Date(s.trim().replace(' ', 'T')).getTime()
  return Number.isNaN(ms) ? 0 : ms
}

// Run dir is YYYYMMDD-HHMM-<slug>; the slug alone labels it on the status line.
const slug = (name: string) => name.replace(/^\d{8}-\d{4}-/, '')

// Worker colors are theme keys, so they follow the person's light/dark theme.
// Each worker name hashes to one theme key, so a worker keeps its color across runs and sessions.
const WORKER_COLORS = ['merged', 'suggestion', 'claude', 'planMode', 'autoAccept', 'ide', 'remember', 'permission']
const workerColor = (w: string) => {
  let h = 0
  for (const ch of w) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  return WORKER_COLORS[h % WORKER_COLORS.length]!
}
const usdColor = (billing: string) => (billing === 'metered' ? 'success' : 'permission')

const fmtTokens = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : `${n}`)
// "$" is billed money (DeepSeek); "≈$" is the API-equivalent of a ChatGPT-subscription Codex run.
const fmtUsd = (usd: number, billing: string) => `${billing === 'metered' ? '$' : '≈$'}${usd < 0.01 ? usd.toFixed(4) : usd.toFixed(2)}`
const sumUsage = (tasks: Task[]) => {
  const us = tasks.map(t => t.usage).filter((u): u is Usage => u !== null)
  const tokens = us.reduce((a, u) => a + u.input + u.output, 0)
  const billed = us.filter(u => u.billing === 'metered').reduce((a, u) => a + (u.usd ?? 0), 0)
  const equiv = us.filter(u => u.billing !== 'metered').reduce((a, u) => a + (u.usd ?? 0), 0)
  return { count: us.length, tokens, billed, equiv }
}

// Live progress of a running codex-type task, from its log: the agent's last "codex" note and last "exec" command.
const parseProgress = (log: string) => {
  const lines = log.split('\n')
  let progress = ''
  let lastCmd = ''
  for (let i = lines.length - 1; i >= 0 && (progress === '' || lastCmd === ''); i--) {
    const line = lines[i]!.trim()
    if (progress === '' && line === 'codex') {
      const note: string[] = []
      for (let j = i + 1; j < lines.length && lines[j]!.trim() !== '' && !/^(hook|exec|codex)\b/.test(lines[j]!.trim()); j++) note.push(lines[j]!.trim())
      progress = note.join(' ')
    }
    if (lastCmd === '' && line === 'exec' && lines[i + 1]) {
      lastCmd = lines[i + 1]!
        .replace(/ in \/\S+$/, '')
        .replace(/^\/bin\/(z|ba)?sh -lc (['"])(.*?)\2?$/, '$3')
        .trim()
    }
  }
  return { progress: progress.replace(/[`*]/g, '').replace(/\s+/g, ' ').slice(0, 300), lastCmd: lastCmd.slice(0, 200) }
}

// Live progress of a running claude-type task, from its stream-json events: last assistant text and last tool call.
const parseStream = (stream: string) => {
  let progress = ''
  let lastCmd = ''
  const lines = stream.split('\n')
  for (let i = lines.length - 1; i >= 0 && (progress === '' || lastCmd === ''); i--) {
    if (!/^\{\s*"type"\s*:\s*"assistant"/.test(lines[i]!)) continue
    let ev: { message?: { content?: { type: string; text?: string; name?: string; input?: Record<string, unknown> }[] } }
    try {
      ev = JSON.parse(lines[i]!)
    } catch {
      continue
    }
    for (const c of [...(ev.message?.content ?? [])].reverse()) {
      if (progress === '' && c.type === 'text' && c.text) progress = c.text
      if (lastCmd === '' && c.type === 'tool_use') {
        const input = c.input ?? {}
        const arg = input.command ?? input.file_path ?? input.pattern ?? input.description ?? input.prompt ?? ''
        lastCmd = c.name === 'Bash' ? String(arg) : `${c.name} ${String(arg)}`.trim()
      }
    }
  }
  return { progress: progress.replace(/[`*#]/g, '').replace(/\s+/g, ' ').slice(0, 300), lastCmd: lastCmd.replace(/\s+/g, ' ').slice(0, 200) }
}

// Detail rows wrap to a few lines instead of one: enough to read, not enough for one task to fill the pane.
const MAX_LINES = { progress: 3, command: 2, summary: 2 }
// Terminal cells a character takes: East Asian wide and full-width forms take two.
const cells = (ch: string) => {
  const c = ch.codePointAt(0) ?? 0
  return (c >= 0x1100 && c <= 0x115f) || (c >= 0x2e80 && c <= 0xa4cf) || (c >= 0xac00 && c <= 0xd7a3) ||
    (c >= 0xf900 && c <= 0xfaff) || (c >= 0xfe30 && c <= 0xfe4f) || (c >= 0xff00 && c <= 0xff60) ||
    (c >= 0xffe0 && c <= 0xffe6) || (c >= 0x1f300 && c <= 0x1faff) || (c >= 0x20000 && c <= 0x3fffd)
    ? 2
    : 1
}
const width = (text: string) => [...text].reduce((a, c) => a + cells(c), 0)
// Splits text into lines of `width` cells, breaking at the last space when there is one; past `max` lines it
// keeps `max` and ends the last with "…".
const wrapCells = (text: string, cols: number, max: number) => {
  const w = Math.max(10, cols)
  const lines: string[] = []
  let line = ''
  for (const ch of text.trim()) {
    if (width(line) + cells(ch) > w) {
      // Break at a space only near the line's end; CJK text has few spaces, and an early one wastes the row.
      const space = line.lastIndexOf(' ')
      if (space > 0 && ch !== ' ' && width(line.slice(0, space)) >= w * 0.6) {
        lines.push(line.slice(0, space))
        line = line.slice(space + 1)
      } else {
        lines.push(line)
        line = ''
      }
      if (ch === ' ') continue
    }
    line += ch
  }
  if (line !== '') lines.push(line)
  if (lines.length <= max) return lines
  const kept = lines.slice(0, max)
  let last = kept[max - 1]!
  while (last !== '' && width(last) + 1 > w) last = [...last].slice(0, -1).join('')
  kept[max - 1] = `${last}…`
  return kept
}

const fmtElapsed = (ms: number) => {
  if (ms <= 0) return ''
  const s = Math.floor(ms / 1000)
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`
}

export const register: Register = on => {
  let primed = false
  const opened = new Set<string>()

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'muster-panel', description: 'Open the Muster pane' })
    const home = (await $.env.get('HOME')) ?? ''
    // runsDir comes from ~/.muster/config.json (MUSTER_CONFIG overrides the path); default ~/.muster/runs.
    const expand = (p: string) => p.replace(/^~(?=\/|$)/, home)
    const configPath = expand((await $.env.get('MUSTER_CONFIG')) ?? '~/.muster/config.json')
    const config = await $.fs
      .read(configPath)
      .then(t => JSON.parse(typeof t === 'string' ? t : '{}') as { runsDir?: string }, () => ({}) as { runsDir?: string })
      .catch(() => ({}) as { runsDir?: string })
    const root = expand(config.runsDir ?? '~/.muster/runs')
    // Runs are bound to the session that dispatched them (muster.py records it).
    // Unknown session id: show everything, as before.
    let sessionId = ''
    const isMine = (run: Run) => !sessionId || run.tasks.some(t => t.session === sessionId)

    const readText = (path: string) => $.fs.read(path).then(t => (typeof t === 'string' ? t : ''), () => '')

    // Branch of a worktree or repo, read from .git without running git.
    const readBranch = async (cwd: string | undefined) => {
      if (!cwd) return ''
      const dotGit = await readText(`${cwd}/.git`)
      const gitDir = dotGit.startsWith('gitdir:') ? dotGit.slice(7).trim() : `${cwd}/.git`
      const head = (await readText(`${gitDir}/HEAD`)).trim()
      return head.startsWith('ref: refs/heads/') ? head.slice(16) : head.slice(0, 7)
    }

    const loadTasks = async (name: string): Promise<Task[]> => {
      const dirs = (await $.fs.list(`${root}/${name}`)).filter(d => d.kind === 'dir').map(d => d.name).sort()
      return Promise.all(
        dirs.map(async (id): Promise<Task> => {
          const dir = `${root}/${name}/${id}`
          const [status, meta, result, prompt, usageText] = await Promise.all([
            readText(`${dir}/status`),
            readText(`${dir}/meta`),
            readText(`${dir}/result.md`),
            readText(`${dir}/prompt.md`),
            readText(`${dir}/usage`),
          ])
          let usage: Usage | null = null
          try {
            usage = usageText ? (JSON.parse(usageText) as Usage) : null
          } catch {
            usage = null
          }
          // Title: the prompt's first heading, minus a leading "Task:" label (full-width colon too).
          const heading = prompt.split('\n').find(l => /^#\s/.test(l)) ?? ''
          const title = heading.replace(/^#+\s*/, '').replace(/^Task\s*[:\uFF1A]\s*/i, '').replace(/`/g, '').trim()
          const kv = Object.fromEntries(
            meta.split('\n').filter(l => l.includes('=')).map(l => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)]),
          )
          const first = result.split('\n').filter(l => !/^\s*#/.test(l)).map(l => l.replace(/^[>*\-\s]+/, '').trim()).find(Boolean) ?? ''
          const tidy = first
            .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1') // [report.md](/long/path) -> report.md
            .replace(/`?(\/[^\s`]+\/)([^\s`/]+)`?/g, '$2') // /abs/path/report.md -> report.md
            .replace(/[`*_]/g, '')
            .replace(/\s+/g, ' ')
            .trim()
          const branch = await readBranch(kv.cwd)
          // A runner that died without writing its final status (killed, crashed, machine slept) leaves `running`
          // behind; muster.py records its pid, so a dead pid means the task is lost, not running.
          const pid = Number(kv.pid ?? 0)
          const isOrphan =
            /^(running|queued)/.test(status.trim()) &&
            pid > 0 &&
            (await $.process.run(['kill', '-0', String(pid)], { timeoutMs: 2000 }).then(r => r.exitCode !== 0, () => false))
          const isLive = status.trim() === 'running' && !isOrphan
          // shortcut: reads the whole log each poll while running; switch to a tail read if logs grow past a few MB.
          // meta's `type` is the worker kind muster.py ran: claude writes stream.jsonl, codex a plain log.
          const isStream = kv.type === 'claude'
          const liveFile = `${dir}/${isStream ? 'stream.jsonl' : 'log.txt'}`
          const live = !isLive
            ? { progress: '', lastCmd: '' }
            : isStream
              ? parseStream(await readText(liveFile))
              : parseProgress(await readText(liveFile))
          const logMs = isLive ? ((await $.fs.stat(liveFile).catch(() => undefined))?.mtimeMs ?? 0) : 0
          return {
            id,
            branch,
            session: kv.session ?? '',
            title: title.slice(0, 60) || id,
            // A queued task has no `worker` yet, only the `workers` it may start on.
            worker: kv.worker ?? (kv.workers ? kv.workers.split(',').join('|') : '?'),
            status: isOrphan ? 'failed: lost (runner exited without a final status)' : status.trim() || 'pending',
            startedMs: parseTime(kv.started),
            queuedMs: parseTime(kv.queued),
            after: (kv.after ?? '').split(',').filter(Boolean),
            priority: Number(kv.priority ?? 0) || 0,
            finishedMs: parseTime(kv.finished),
            summary: tidy.slice(0, 400),
            usage,
            progress: live.progress,
            lastCmd: live.lastCmd,
            logMs,
          }
        }),
      )
    }

    // A run's session never changes, so its owner is read once per run and kept.
    const ownerOf = new Map<string, string>()
    const runOwner = async (name: string) => {
      const cached = ownerOf.get(name)
      if (cached !== undefined) return cached
      const dirs = (await $.fs.list(`${root}/${name}`).catch(() => [])).filter(d => d.kind === 'dir').map(d => d.name).sort()
      // The runner makes a task dir before it writes meta; cache only once a meta has been read.
      for (const dir of dirs) {
        const meta = await readText(`${root}/${name}/${dir}/meta`)
        if (!meta) continue
        const owner = /^session=(.*)$/m.exec(meta)?.[1] ?? ''
        ownerOf.set(name, owner)
        return owner
      }
      return ''
    }
    // The pane keeps every run of this session, newest first, up to KEEP.
    const KEEP = 30
    const loadRuns = async (): Promise<Run[]> => {
      if (!(await $.fs.exists(root))) return []
      const names = (await $.fs.list(root)).filter(r => r.kind === 'dir').map(r => r.name).sort().reverse()
      const mine: string[] = []
      for (const name of names) {
        if (mine.length >= KEEP) break
        if (!sessionId || (await runOwner(name)) === sessionId) mine.push(name)
      }
      return Promise.all(mine.reverse().map(async name => ({ name, tasks: await loadTasks(name).catch(() => []) })))
    }

    const poll = async () => {
      // Re-read each poll: /clear and resume move the session to a new id.
      sessionId = await $.session.id().catch(() => '')
      const all = (await loadRuns().catch(() => [] as Run[])).filter(isMine)
      const shown = [...all].reverse()
      await update($, runsAtom, () => shown)

      const key = (r: Run, t: Task) => `${r.name}/${t.id}`
      const finished = all.flatMap(r => r.tasks.filter(isFinished).map(t => key(r, t)))
      if (!primed) {
        // Tasks already finished before this session started do not toast.
        primed = true
        await update($, notifiedAtom, old => [...new Set([...old, ...finished])].slice(-500))
      } else {
        const seen = new Set(await read($, notifiedAtom))
        const fresh = all.flatMap(r => r.tasks.filter(t => isFinished(t) && !seen.has(key(r, t))).map(t => ({ r, t })))
        for (const { r, t } of fresh) $.ui.toast(`muster ${t.title} (${t.worker}): ${t.status}`)
        if (fresh.length) await update($, notifiedAtom, old => [...old, ...fresh.map(({ r, t }) => key(r, t))].slice(-500))
      }

      const active = shown.filter(r => r.tasks.some(isActive))
      const unseen = active.filter(r => !opened.has(r.name))
      if (unseen.length) {
        for (const r of unseen) opened.add(r.name)
        void $.ui.open({ id: PANE, title: TITLE })
      }
    }

    // The band above the prompt replaced the status line; clear what an older version pinned.
    $.ui.status(undefined)
    $.clock.every(POLL_MS, () => void poll())
    void poll()

    return next(e)
  })

  // Starting a batch ("/muster …", "muster: …") opens the pane too. Opened from the person's own prompt,
  // it is placed at any terminal width; without focus, so typing goes on in the prompt.
  on('prompt.submit', async ($, e, next) => {
    if (/^\s*(\/muster(:muster)?\b|muster\s*:)/i.test(e.text ?? '')) void $.ui.open({ id: PANE, title: TITLE })
    return next(e)
  })

  on('command.run', { command: 'muster-panel' }, async $ => {
    // The command hands the pane the keyboard: ↑↓ scroll, Enter folds, Esc back to the prompt.
    await $.ui.open({ id: PANE, title: TITLE, focus: true })
    return { text: 'Muster pane opened and focused (Esc returns to the prompt).' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const runs = await read($, runsAtom)
    if (runs.length === 0) {
      return (
        <Box flexDirection="column">
          <Text dimColor>No Muster runs yet.</Text>
          <Text dimColor>Tasks appear here when Claude dispatches work to your workers.</Text>
        </Box>
      )
    }
    const folds = await read($, foldsAtom)
    const isFolded = (run: Run) => folds[run.name] ?? !run.tasks.some(isActive)
    const keys = runs.flatMap(r => [r.name, ...SECTIONS.map(([label]) => `${r.name}/${label}`)])
    const setAll = (folded: boolean) => () => void update($, foldsAtom, old => ({ ...old, ...Object.fromEntries(keys.map(k => [k, folded])) }))
    const toggle = (run: Run) => () => void update($, foldsAtom, old => ({ ...old, [run.name]: !isFolded(run) }))
    const now = await $.clock.now()
    // Room for a detail line: the body less its 4-cell indent and the 2-cell marker.
    const detailWidth = (e.props.bodyColumns ?? 80) - 6
    const Worker = (w: string) => <Text bold color={workerColor(w)}>{w}</Text>
    const Tok = (n: number) => <Text bold>{fmtTokens(n)}</Text>
    const Usd = (usd: number, billing: string) => <Text bold color={usdColor(billing)}>{fmtUsd(usd, billing)}</Text>
    const SumLine = (tasks: Task[], lead: string) => {
      const s = sumUsage(tasks)
      if (s.count === 0) return null
      return (
        <Text dimColor>
          {lead}{Tok(s.tokens)} tokens
          {s.billed > 0 && <Text> · {Usd(s.billed, 'metered')} billed</Text>}
          {s.equiv > 0 && <Text> · {Usd(s.equiv, 'subscription')} sub</Text>}
        </Text>
      )
    }
    const count = (run: Run) => {
      const n = (test: (t: Task) => boolean) => run.tasks.filter(test).length
      const extra = [[n(isQueued), 'queued'], [n(isCanceled), 'canceled']].filter(([k]) => k).map(([k, l]) => ` · ${k} ${l}`).join('')
      return `${n(isRunning)} running · ${n(t => t.status === 'done')} done · ${n(isFailed)} failed${extra}`
    }
    return (
      <Box flexDirection="column">
        <Box flexDirection="row" gap={2}>
          <Button key="expand-all" hotkey="e" plain onPress={setAll(false)}>Expand all</Button>
          <Button key="collapse-all" hotkey="c" plain onPress={setAll(true)}>Collapse all</Button>
        </Box>
        <Text dimColor>{e.props.isFocused ? '↑↓ scroll · Enter on a run or section folds it' : 'Click or ctrl+x tab here to scroll and fold'}</Text>
        <Text> </Text>
        {runs.map(run => (
          <Box flexDirection="column" marginBottom={1}>
            <Button key={`run:${run.name}`} plain onPress={toggle(run)}>
              {isFolded(run) ? '▸' : '▾'} {run.name} <Text dimColor>{count(run)}</Text>
            </Button>
            {SumLine(run.tasks, '  Σ ')}
            {!isFolded(run) &&
              [...new Set(run.tasks.map(t => t.worker))].length > 1 &&
              [...new Set(run.tasks.map(t => t.worker))].map(w => {
                const line = SumLine(run.tasks.filter(t => t.worker === w), '')
                return line === null ? null : <Text dimColor>{'    '}{Worker(w)}: {line}</Text>
              })}
            {!isFolded(run) &&
              SECTIONS.flatMap(([label, color], i) => {
                const tasks = run.tasks.filter(t => SECTIONS.findIndex(([, , test]) => test(t)) === i)
                if (label === 'Queued') tasks.sort(startOrder)
                // Place in its worker's line, for tasks only a slot holds back (not a dependency, not unlaunched).
                const place = new Map<Task, number>()
                const perWorker = new Map<string, number>()
                for (const t of tasks) {
                  if (!/^queued(?!, waiting for)/.test(t.status)) continue
                  const n = (perWorker.get(t.worker) ?? 0) + 1
                  perWorker.set(t.worker, n)
                  place.set(t, n)
                }
                if (tasks.length === 0) return []
                const key = `${run.name}/${label}`
                const header = (
                  <Box marginTop={1}>
                    <Button key={`section:${key}`} plain onPress={() => void update($, foldsAtom, old => ({ ...old, [key]: !old[key] }))}>
                      <Text bold color={color}>{'  '}{folds[key] ? '▸' : '▾'} {label} · {tasks.length}</Text>
                    </Button>
                  </Box>
                )
                if (folds[key]) return [header]
                return [header, ...tasks.map(t => (
                <Box flexDirection="column" marginTop={1}>
                  <Text color={markColor(t)}>
                    {'  '}{mark(t)} {t.title}
                  </Text>
                  <Text dimColor>
                    {'    '}{t.id} · {Worker(t.worker)} · {place.has(t) ? t.status.replace(/^queued/, `queued #${place.get(t)}`) : t.status}{t.after.length > 0 ? ` · after ${t.after.join(', ')}` : ''}{t.priority !== 0 ? ` · priority ${t.priority}` : ''}{t.startedMs > 0 ? ` · ${fmtElapsed((t.finishedMs || now) - t.startedMs)}` : ''}{t.branch ? ` · ⎇ ${t.branch}` : ''}
                  </Text>
                  {t.usage !== null && (
                    <Text dimColor>
                      {'    '}in {Tok(t.usage.input)}
                      {t.usage.input > 0 ? ` (${Math.round((t.usage.cached / t.usage.input) * 100)}% cached)` : ''} · out {Tok(t.usage.output)}
                      {t.usage.usd === null ? ' · no price' : <Text> · {Usd(t.usage.usd, t.usage.billing)}</Text>}
                      {t.usage.peak && <Text bold color="error"> peak</Text>}
                    </Text>
                  )}
                  {isRunning(t) &&
                    t.progress !== '' &&
                    wrapCells(t.progress, detailWidth, MAX_LINES.progress).map((line, i) => (
                      <Text>
                        {'    '}
                        {i === 0 ? <Text color="warning">↻</Text> : ' '} {line}
                      </Text>
                    ))}
                  {isRunning(t) &&
                    t.lastCmd !== '' &&
                    wrapCells(t.lastCmd, detailWidth, MAX_LINES.command).map((line, i) => (
                      <Text dimColor>
                        {'    '}
                        {i === 0 ? '$' : ' '} {line}
                      </Text>
                    ))}
                  {isRunning(t) && t.logMs > 0 && now - t.logMs > 120000 && (
                    <Text color="error">{'    '}no output for {fmtElapsed(now - t.logMs)}</Text>
                  )}
                  {t.summary !== '' &&
                    isFinished(t) &&
                    wrapCells(t.summary, detailWidth, MAX_LINES.summary).map((line, i) => (
                      <Text dimColor>
                        {'    '}
                        {i === 0 ? '↳' : ' '} {line}
                      </Text>
                    ))}
                </Box>
              ))]
              })}
          </Box>
        ))}
      </Box>
    )
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const active = (await read($, runsAtom)).filter(r => r.tasks.some(isActive))
    if (e.props.hasSurvey || active.length === 0) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    const now = await $.clock.now()
    return (
      <Box flexDirection="row" flexWrap="wrap">
        <Text color="cyan" bold>⇉ muster </Text>
        {active.map((r, i) => {
          // Only running tasks get a row; finished ones collapse to counts.
          const done = r.tasks.filter(t => t.status === 'done').length
          const failed = r.tasks.filter(isFailed).length
          const queued = r.tasks.filter(isQueued).length
          const canceled = r.tasks.filter(isCanceled).length
          return (
            <Text>
              {i > 0 && <Text dimColor>| </Text>}
              {r.tasks.filter(isRunning).map(t => (
                <Text>
                  <Text color="yellow">… {t.title.length > 30 ? `${t.title.slice(0, 29)}…` : t.title}</Text>
                  <Text> </Text>
                  <Text bold color={workerColor(t.worker)}>{t.worker}</Text>
                  <Text dimColor> {fmtElapsed(now - t.startedMs)}  </Text>
                </Text>
              ))}
              {queued > 0 && <Text color="cyan">○{queued} </Text>}
              {done > 0 && <Text color="green">✓{done} </Text>}
              {failed > 0 && <Text color="red">✗{failed} </Text>}
              {canceled > 0 && <Text dimColor>⊘{canceled} </Text>}
            </Text>
          )
        })}
        <Text dimColor>· /muster-panel</Text>
      </Box>
    )
  })
}
