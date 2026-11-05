import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Run, Task, Usage } from '../types'

const PANE = 'muster'
const TITLE = 'Muster'
const POLL_MS = 5000

// Change a tag when its value's fields change, so a reload declines old values.
const runsAtom = atom({ plugin: 'muster', key: 'runs' } as const, [], { shape: 'runs-6' })
// Per-run fold the person chose; a run without an entry folds once nothing in it runs.
const foldsAtom = atom({ plugin: 'muster', key: 'folds' } as const, {}, { shape: 'folds-1' })
const notifiedAtom = atom({ plugin: 'muster', key: 'notified' } as const, [], { shape: 'notified-1' })

const isRunning = (t: Task) => t.status === 'running'
const isFinished = (t: Task) => t.status === 'done' || t.status.startsWith('failed')
const mark = (t: Task) => (t.status === 'done' ? '✓' : isRunning(t) ? '…' : t.status.startsWith('failed') ? '✗' : '?')

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

const fmtElapsed = (ms: number) => {
  if (ms <= 0) return ''
  const s = Math.floor(ms / 1000)
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`
}

export const register: Register = on => {
  let primed = false
  const opened = new Set<string>()

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'muster', description: 'Show Muster worker tasks in a pane' })
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
          // Title: the prompt's first heading, minus a leading "任务：" / "Task:".
          const heading = prompt.split('\n').find(l => /^#\s/.test(l)) ?? ''
          const title = heading.replace(/^#+\s*/, '').replace(/^(任务|Task)\s*[:：]\s*/i, '').replace(/`/g, '').trim()
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
          const isLive = status.trim() === 'running'
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
            worker: kv.worker ?? '?',
            status: status.trim() || 'pending',
            startedMs: parseTime(kv.started),
            finishedMs: parseTime(kv.finished),
            summary: tidy.slice(0, 120),
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
      const meta = dirs[0] ? await readText(`${root}/${name}/${dirs[0]}/meta`) : ''
      const owner = /^session=(.*)$/m.exec(meta)?.[1] ?? ''
      if (owner !== '' || dirs.length > 0) ownerOf.set(name, owner)
      return owner
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

      const active = shown.filter(r => r.tasks.some(isRunning))
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

  on('command.run', { command: 'muster' }, async $ => {
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
    const isFolded = (run: Run) => folds[run.name] ?? !run.tasks.some(isRunning)
    const setAll = (folded: boolean) => () =>
      void update($, foldsAtom, old => ({ ...old, ...Object.fromEntries(runs.map(r => [r.name, folded])) }))
    const toggle = (run: Run) => () => void update($, foldsAtom, old => ({ ...old, [run.name]: !isFolded(run) }))
    const now = await $.clock.now()
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
    const count = (run: Run) =>
      `${run.tasks.filter(isRunning).length} running · ${run.tasks.filter(t => t.status === 'done').length} done · ${run.tasks.filter(t => t.status.startsWith('failed')).length} failed`
    return (
      <Box flexDirection="column">
        <Box flexDirection="row" gap={2}>
          <Button key="expand-all" hotkey="e" plain onPress={setAll(false)}>Expand all</Button>
          <Button key="collapse-all" hotkey="c" plain onPress={setAll(true)}>Collapse all</Button>
        </Box>
        <Text dimColor>{e.props.isFocused ? '↑↓ scroll · Enter on a run folds it' : 'Click or ctrl+x tab here to scroll and fold'}</Text>
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
              run.tasks.map(t => (
                <Box flexDirection="column" marginTop={1}>
                  <Text color={t.status === 'done' ? 'green' : isRunning(t) ? 'yellow' : t.status.startsWith('failed') ? 'red' : undefined}>
                    {'  '}{mark(t)} {t.title}
                  </Text>
                  <Text dimColor>
                    {'    '}{t.id} · {Worker(t.worker)} · {t.status} · {fmtElapsed((t.finishedMs || now) - t.startedMs)}{t.branch ? ` · ⎇ ${t.branch}` : ''}
                  </Text>
                  {t.usage !== null && (
                    <Text dimColor>
                      {'    '}in {Tok(t.usage.input)}
                      {t.usage.input > 0 ? ` (${Math.round((t.usage.cached / t.usage.input) * 100)}% cached)` : ''} · out {Tok(t.usage.output)}
                      {t.usage.usd === null ? ' · no price' : <Text> · {Usd(t.usage.usd, t.usage.billing)}</Text>}
                      {t.usage.peak && <Text bold color="error"> peak</Text>}
                    </Text>
                  )}
                  {isRunning(t) && t.progress !== '' && (
                    <Text wrap="truncate-end">
                      {'    '}<Text color="warning">↻</Text> {t.progress}
                    </Text>
                  )}
                  {isRunning(t) && t.lastCmd !== '' && (
                    <Text dimColor wrap="truncate-end">
                      {'    '}$ {t.lastCmd}
                    </Text>
                  )}
                  {isRunning(t) && t.logMs > 0 && now - t.logMs > 120000 && (
                    <Text color="error">{'    '}no output for {fmtElapsed(now - t.logMs)}</Text>
                  )}
                  {t.summary !== '' && isFinished(t) && (
                    <Text dimColor wrap="truncate-end">
                      {'    '}↳ {t.summary}
                    </Text>
                  )}
                </Box>
              ))}
          </Box>
        ))}
      </Box>
    )
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const active = (await read($, runsAtom)).filter(r => r.tasks.some(isRunning))
    if (e.props.hasSurvey || active.length === 0) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    const now = await $.clock.now()
    return (
      <Box flexDirection="row" flexWrap="wrap">
        <Text color="cyan" bold>⇉ muster </Text>
        {active.map((r, i) => {
          // Only running tasks get a row; finished ones collapse to counts.
          const done = r.tasks.filter(t => t.status === 'done').length
          const failed = r.tasks.filter(t => t.status.startsWith('failed')).length
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
              {done > 0 && <Text color="green">✓{done} </Text>}
              {failed > 0 && <Text color="red">✗{failed} </Text>}
            </Text>
          )
        })}
        <Text dimColor>· /muster</Text>
      </Box>
    )
  })
}
