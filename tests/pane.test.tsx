import { expect, test } from 'claude-code/testing'

const R = '/h/.muster/runs'
const files: Record<string, string> = {
  [`${R}/20261105-1130-x/A/status`]: 'done\n',
  [`${R}/20261105-1130-x/A/meta`]: 'worker=codex-a\ntype=codex\ncwd=/x\nsession=S1\nstarted=2026-11-05 11:30:00\nfinished=2026-11-05 11:31:40\nexit=0\n',
  [`${R}/20261105-1130-x/A/result.md`]: '## Summary\nretry logic added, see [report.md](/h/.muster/runs/x/A/report.md)\n',
  [`${R}/20261101-0900-old/Z/status`]: 'done\n',
  [`${R}/20261101-0900-old/Z/meta`]: 'worker=codex-b\ntype=codex\nsession=S1\nstarted=2026-11-05 11:00:00\n',
  [`${R}/20261105-1130-x/A/prompt.md`]: '# 任务：实现 `retry` 重试\n\nRules...\n',
  ['/x/.git']: 'gitdir: /repo/.git/worktrees/x\n',
  ['/repo/.git/worktrees/x/HEAD']: 'ref: refs/heads/feat/retry\n',
  [`${R}/20261104-0800-foreign/F/status`]: 'running\n',
  [`${R}/20261104-0800-foreign/F/meta`]: 'worker=codex-c\ntype=codex\nsession=OTHER\nstarted=2026-11-05 11:00:00\n',
  [`${R}/20261105-1130-x/A/usage`]: '{"model":"gpt-6-astra","input":1508687,"cached":1431680,"output":14136,"usd":2.9085,"billing":"subscription"}',
  [`${R}/20261105-1130-x/B/usage`]: '{"model":"deepseek-flash","input":28449,"cached":0,"output":3,"usd":0.0085,"billing":"metered","peak":true}',
  [`${R}/20261105-1130-x/G/status`]: 'running\n',
  [`${R}/20261105-1130-x/G/meta`]: 'worker=codex-b\ntype=codex\ncwd=/x\nsession=S1\nstarted=2026-11-05 11:30:00\n',
  [`${R}/20261105-1130-x/G/log.txt`]: "exec\n/bin/zsh -lc 'npm test -- retry' in /x\n succeeded in 1s:\nok\n\ncodex\n重试测试已通过，正在补 `README`。\n\nhook: PreToolUse\n",
  [`${R}/20261105-1130-x/B/stream.jsonl`]: "{\"type\": \"assistant\", \"message\": {\"content\": [{\"type\": \"tool_use\", \"name\": \"Read\", \"input\": {\"file_path\": \"/x/README.md\"}}]}}\n{\"type\": \"assistant\", \"message\": {\"content\": [{\"type\": \"text\", \"text\": \"README 已读完，开始写评审。\"}]}}\n",
  [`${R}/20261105-1130-x/B/status`]: 'running\n',
  [`${R}/20261105-1130-x/B/meta`]: 'worker=deepseek\ntype=claude\ncwd=/x\nsession=S1\nstarted=2026-11-05 11:30:00\n',
}
const dir = (name: string) => ({ name, kind: 'dir', size: 0, mtimeMs: 0, isLink: false })
const PANE = {
  plugin: 'muster', component: 'Pane', requestId: 'muster',
  props: { title: 'Muster', isFocused: false, bodyColumns: 50, placement: 'dock', scroll: { offset: 0, bodyRows: 20 }, view: {} },
} as const

test('the pane shows each task of the newest run', async ($, on) => {
  on('command.register', () => ({ value: { command: 'muster' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('env.get', (_, e: any) => ({ value: e.name === 'HOME' ? '/h' : 'S1' }))
  on('fs.exists', () => ({ value: true }))
  on('fs.list', (_, e: any) => ({
    value: e.path === R ? [dir('20261101-0900-old'), dir('20261104-0800-foreign'), dir('20261105-1130-x')] : e.path.endsWith('-x') ? [dir('A'), dir('B'), dir('G')] : e.path.endsWith('-old') ? [dir('Z')] : e.path.endsWith('-foreign') ? [dir('F')] : [],
  }))
  on('fs.read', (_, e: any) => (e.path in files ? { value: files[e.path] } : { deny: 'ENOENT' }))
  on('clock.every', () => ({ value: undefined }))
  on('clock.now', () => ({ value: new Date('2026-11-05T11:32:00').getTime() }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('session.id', () => ({ value: 'S1' }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Button', text: /20261105-1130-x/ }))?.text).toMatch(/▾ 20261105-1130-x/)
    expect((await ui.find({ type: 'Text', text: /实现 retry/ }))?.text).toMatch(/✓ 实现 retry 重试/)
    expect((await ui.find({ type: 'Text', text: /A · codex-a/ }))?.text).toMatch(/A · codex-a · done · 1m40s · ⎇ feat\/retry/)
    expect((await ui.find({ type: 'Text', text: /retry logic/ }))?.text).toMatch(/↳ retry logic added, see report\.md$/)
    expect((await ui.find({ type: 'Text', text: /B · deepseek/ }))?.text).toMatch(/B · deepseek · running/)
    expect((await ui.find({ type: 'Button', text: /20261101-0900-old/ }))?.text).toMatch(/[▸▾] 20261101-0900-old 0 running · 1 done/)
    expect(await ui.find({ type: 'Button', text: /foreign/ })).toBeUndefined()
    expect((await ui.find({ type: 'Text', text: /in 1\.51M/ }))?.text).toMatch(/in 1\.51M \(95% cached\) · out 14k · ≈\$2\.91/)
    expect((await ui.find({ type: 'Text', text: /in 28k/ }))?.text).toMatch(/out 3 · \$0\.0085 peak/)
    expect((await ui.find({ type: 'Text', text: /Σ/ }))?.text).toMatch(/Σ 1\.55M tokens · \$0\.0085 billed · ≈\$2\.91 sub/)
    expect((await ui.find({ type: 'Text', text: /codex-a: / }))?.text).toMatch(/codex-a: 1\.52M tokens · ≈\$2\.91 sub/)
    // No empty subtotal row is drawn for a worker without usage.
    expect(await ui.find({ type: 'Text', text: /^\s*codex-b: $/ })).toBeUndefined()
    expect((await ui.find({ type: 'Text', text: /重试测试/ }))?.text).toMatch(/↻ 重试测试已通过，正在补 README。/)
    expect((await ui.find({ type: 'Text', text: /npm test/ }))?.text).toMatch(/\$ npm test -- retry$/)
    expect((await ui.find({ type: 'Text', text: /README 已读完/ }))?.text).toMatch(/↻ README 已读完，开始写评审。/)
    expect((await ui.find({ type: 'Text', text: /Read \/x\/README/ }))?.text).toMatch(/\$ Read \/x\/README\.md/)
    // Folding the newest run hides its rows; expanding all brings them back.
    await ui.press({ key: 'run:20261105-1130-x' })
    expect(await ui.find({ type: 'Text', text: /retry logic/ })).toBeUndefined()
    await ui.press({ key: 'expand-all' })
    expect((await ui.find({ type: 'Text', text: /retry logic/ }))?.text).toMatch(/↳ retry logic added, see report\.md$/)
    await ui.unmount()
  }
})

test('the band above the prompt colors each running run', async ($, on) => {
  on('command.register', () => ({ value: { command: 'muster' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('env.get', (_, e: any) => ({ value: e.name === 'HOME' ? '/h' : 'S1' }))
  on('fs.exists', () => ({ value: true }))
  on('fs.list', (_, e: any) => ({ value: e.path === R ? [dir('20261105-1130-x')] : e.path.endsWith('-x') ? [dir('A'), dir('B'), dir('G')] : [] }))
  on('fs.read', (_, e: any) => (e.path in files ? { value: files[e.path] } : { deny: 'ENOENT' }))
  on('clock.every', () => ({ value: undefined }))
  on('clock.now', () => ({ value: new Date('2026-11-05T11:32:00').getTime() }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('session.id', () => ({ value: 'S1' }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount({ plugin: 'muster', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false } as any })

  expect(await ui.find({ type: 'Text', text: /codex-a/ })).toBeUndefined()
  expect((await ui.find({ type: 'Text', text: /^✓1/ }))?.text).toMatch(/✓1/)
  expect((await ui.find({ type: 'Text', text: /^… B/ }))?.text).toMatch(/^… B deepseek/)
  await ui.unmount()
})
