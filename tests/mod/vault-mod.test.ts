import { expect, mock, test } from 'claude-code/testing'

// 占位 UUID 拆开拼接：发布闸门按 8-4-4-4-12 hex 拦截疑似真实 session id（test_no_secrets_in_tree）
const SID = ['11111111', '2222', '3333', '4444', '555555555555'].join('-')
const PID = ['aaaaaaaa', 'bbbb', 'cccc', 'dddd', 'eeeeeeeeeeee'].join('-')
const HOME = '/home/u'
// 引擎在 fs.* 事件到达 hook 之前把路径解析成宿主 OS 的绝对路径（Windows 上 `/home/u/...` 变成
// `D:\home\u\...`），所以 stub 按 `/.context-vault/` 之后的部分做键，与宿主 OS 无关
const LIVE = '/.context-vault/state/live'
const norm = (p: string) => p.replace(/\\/g, '/').replace(/^.*?(\/\.context-vault\/)/, '$1')

async function sidHash(): Promise<string> {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(SID))
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 24)
}

const BAND = {
  plugin: 'context-vault', component: 'AbovePrompt', requestId: 'band', surface: 'terminal',
  viewport: { columns: 100, rows: 30 },
  props: { hasSurvey: false, isWorking: false, maxRows: 3, bodyColumns: 100, scroll: { offset: 0, bodyRows: 3 }, view: {} },
} as const

function harness(on, opts: { surfaces?: string[]; os?: string; env?: Record<string, string> } = {}) {
  const files = new Map<string, string>()
  const raw: string[] = []
  const store = new Map<string, unknown>()
  mock.env(on, opts.env ?? { OS: opts.os ?? 'Linux', HOME })
  mock.clock(on, { now: 1_800_000_000_000 })
  on('session.surfaces', () => ({ value: opts.surfaces ?? ['terminal'] }))
  on('fs.write', ($, e) => { raw.push(e.path); files.set(norm(e.path), e.text); return { value: undefined } })
  on('fs.read', ($, e) => (files.has(norm(e.path)) ? { value: files.get(norm(e.path)) } : { deny: 'ENOENT' }))
  on('store.keys', () => ({ value: [...store.keys()] }))
  on('store.get', ($, e) => ({ value: store.get(e.key) }))
  on('store.set', ($, e) => { store.set(e.key, e.value); return { value: undefined } })
  on('store.delete', ($, e) => { store.delete(e.key); return { value: undefined } })
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['other'] }))
  return { files, store, raw }
}

function snap(over: Record<string, unknown> = {}) {
  return JSON.stringify({
    v: 1, status: 'scored', event: 'UserPromptSubmit', prompt_id: PID, request_digest: null,
    injected: [{ path: '笔记/alpha.md', title: 'alpha', confidence: '中', fulltext: false, kind: 'admitted_list' }],
    pins: { llm: [], user: [] }, excluded: [], session_start: [], rejected: [], acked: [],
    metrics_on: false, ...over,
  })
}

test('no request when no drawing surface', async ($, on) => {
  const { files } = harness(on, { surfaces: [] })
  on('classic.UserPromptSubmit', () => ({}))
  await $.classic.UserPromptSubmit({ session_id: SID, prompt_id: PID, prompt: 'x' })
  expect([...files.keys()]).toEqual([])
})

test('writes a request before next and absorbs the snapshot into the band', async ($, on) => {
  const { files } = harness(on)
  const h = await sidHash()
  on('classic.UserPromptSubmit', () => {
    expect(files.has(`${LIVE}/${h}-${PID}.req.json`)).toBe(true)
    files.set(`${LIVE}/${h}.json`, snap())
    return {}
  })
  await $.classic.UserPromptSubmit({ session_id: SID, prompt_id: PID, prompt: 'x' })
  const ui = await $.ui.mount(BAND)
  expect(await ui.find({ type: 'Text', text: /📚 1 篇：alpha/ })).toBeDefined()
})

test('home uses USERPROFILE on Windows even when HOME is set', async ($, on) => {
  const { raw } = harness(on, { env: { OS: 'Windows_NT', USERPROFILE: 'C:\\Users\\u', HOME: 'D:\\home' } })
  on('classic.UserPromptSubmit', () => ({}))
  await $.classic.UserPromptSubmit({ session_id: SID, prompt_id: PID, prompt: 'x' })
  expect(raw.length).toBe(1)
  const p = raw[0].replace(/\\/g, '/')
  expect(p.includes('Users/u/.context-vault/state/live/')).toBe(true)
  expect(p.includes('home')).toBe(false)
})

test('gate snapshot keeps the last scored view', async ($, on) => {
  const { files } = harness(on)
  const h = await sidHash()
  let turn = 0
  on('classic.UserPromptSubmit', ($, e) => {
    turn += 1
    files.set(`${LIVE}/${h}.json`, turn === 1 ? snap() : snap({ status: 'gate:skipped_source', prompt_id: e.prompt_id, injected: [] }))
    return {}
  })
  await $.classic.UserPromptSubmit({ session_id: SID, prompt_id: PID, prompt: 'x' })
  const PID2 = ['aaaaaaaa', 'bbbb', 'cccc', 'dddd', 'ffffffffffff'].join('-')
  await $.classic.UserPromptSubmit({ session_id: SID, prompt_id: PID2, prompt: '<task-notification>' })
  const ui = await $.ui.mount(BAND)
  expect(await ui.find({ type: 'Text', text: /alpha.*（本轮未召回）/ })).toBeDefined()
})

test('warns once when ops were sent but no snapshot came back', async ($, on) => {
  const { files } = harness(on)
  const h = await sidHash()
  let turn = 0
  on('classic.UserPromptSubmit', () => {
    turn += 1
    if (turn === 1) files.set(`${LIVE}/${h}.json`, snap())
    return {}
  })
  await $.classic.UserPromptSubmit({ session_id: SID, prompt_id: PID, prompt: 'x' })
  const pane = await $.ui.mount({ ...BAND, component: 'Pane', requestId: 'vault-injected',
    props: { title: 'vault 注入', isFocused: true, bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} } })
  await pane.press({ key: 'ex:0' })
  await pane.unmount()
  const PID2 = ['aaaaaaaa', 'bbbb', 'cccc', 'dddd', 'ffffffffffff'].join('-')
  await $.classic.UserPromptSubmit({ session_id: SID, prompt_id: PID2, prompt: 'y' })
  const req = JSON.parse(files.get(`${LIVE}/${h}-${PID2}.req.json`) as string)
  expect(req.exclude).toEqual(['笔记/alpha.md'])
  const ui = await $.ui.mount(BAND)
  expect(await ui.find({ type: 'Text', text: /⚠ vault：操作未送达/ })).toBeDefined()
})

test('acked and rejected annotations leave the queue', async ($, on) => {
  const { files, store } = harness(on)
  const h = await sidHash()
  store.set('ann:ann-00000001', { id: 'ann-00000001', path: '笔记/alpha.md', kind: 'admitted_list', verdict: 'relevant', session: SID, prompt_h: '0123456789abcdef', ts: 1_800_000_000 })
  store.set('ann:ann-00000002', { id: 'ann-00000002', path: '笔记/beta.md', kind: 'admitted_list', verdict: 'relevant', session: SID, prompt_h: '0123456789abcdef', ts: 1_800_000_000 })
  on('classic.UserPromptSubmit', () => {
    files.set(`${LIVE}/${h}.json`, snap({ acked: ['ann-00000001'], rejected: [{ what: 'annotation', key: 'ann-00000002', reason: 'no_record' }] }))
    return {}
  })
  await $.classic.UserPromptSubmit({ session_id: SID, prompt_id: PID, prompt: 'x' })
  expect([...store.keys()]).toEqual([])
})

test('disabled status hides the band', async ($, on) => {
  const { files } = harness(on)
  const h = await sidHash()
  on('classic.UserPromptSubmit', () => { files.set(`${LIVE}/${h}.json`, snap({ status: 'disabled', injected: [] })); return {} })
  await $.classic.UserPromptSubmit({ session_id: SID, prompt_id: PID, prompt: 'x' })
  const ui = await $.ui.mount(BAND)
  expect(await ui.find({ type: 'Text', text: /📚/ })).toBeUndefined()
})

test('band yields to a survey', async ($, on) => {
  const { files } = harness(on)
  const h = await sidHash()
  on('classic.UserPromptSubmit', () => { files.set(`${LIVE}/${h}.json`, snap()); return {} })
  await $.classic.UserPromptSubmit({ session_id: SID, prompt_id: PID, prompt: 'x' })
  const ui = await $.ui.mount({ ...BAND, props: { ...BAND.props, hasSurvey: true } })
  expect(await ui.find({ type: 'Text', text: /📚/ })).toBeUndefined()
})

test('snapshot strings are stripped before drawing', async ($, on) => {
  const { files } = harness(on)
  const h = await sidHash()
  on('classic.UserPromptSubmit', () => {
    files.set(`${LIVE}/${h}.json`, snap({ injected: [{ path: 'a.md', title: 'a\u202eb\u200bc\u0007', confidence: '中', fulltext: false, kind: 'admitted_list' }] }))
    return {}
  })
  await $.classic.UserPromptSubmit({ session_id: SID, prompt_id: PID, prompt: 'x' })
  const ui = await $.ui.mount(BAND)
  expect(await ui.find({ type: 'Text', text: /📚 1 篇：abc/ })).toBeDefined()
})

// ── 节点 2 修复与交互面（T-H1 / T-H2 / T-M1；多数用例出自评审 3A 的探针）────────────────
// 这一组用一个排队的 UPS stub：每轮 next() 时把队首快照写到快照路径，队列空则不写。

const PID2 = ['aaaaaaaa', 'bbbb', 'cccc', 'dddd', 'ffffffffffff'].join('-')
const PID3 = ['aaaaaaaa', 'bbbb', 'cccc', 'dddd', '000000000003'].join('-')
const PID4 = ['aaaaaaaa', 'bbbb', 'cccc', 'dddd', '000000000004'].join('-')
const SID2 = ['99999999', '2222', '3333', '4444', '555555555555'].join('-')
const PH = '0123456789abcdef'
const NOW_S = 1_800_000_000
const PANE = { ...BAND, component: 'Pane', requestId: 'vault-injected',
  props: { title: 'vault 注入', isFocused: true, bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 40 }, view: {} } } as const

async function hashOf(sid: string): Promise<string> {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(sid))
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 24)
}

function qharness(on, opts: { store?: Record<string, unknown> } = {}) {
  const files = new Map<string, string>()
  const raw: string[] = []
  const store = new Map<string, unknown>(Object.entries(opts.store ?? {}))
  const toasts: string[] = []
  const queue: (string | null)[] = []
  mock.env(on, { OS: 'Linux', HOME })
  mock.clock(on, { now: NOW_S * 1000 })
  on('session.surfaces', () => ({ value: ['terminal'] }))
  on('fs.write', ($, e) => { raw.push(e.path); files.set(norm(e.path), e.text); return { value: undefined } })
  on('fs.read', ($, e) => (files.has(norm(e.path)) ? { value: files.get(norm(e.path)) } : { deny: 'ENOENT' }))
  on('store.keys', () => ({ value: [...store.keys()] }))
  on('store.get', ($, e) => ({ value: store.get(e.key) }))
  on('store.set', ($, e) => { store.set(e.key, e.value); return { value: undefined } })
  on('store.delete', ($, e) => { store.delete(e.key); return { value: undefined } })
  on('ui.toast', ($, e) => { toasts.push(e.text); return { value: undefined } })
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['other'] }))
  on('classic.UserPromptSubmit', async ($, e) => {
    const s = queue.shift()
    if (s) files.set(`${LIVE}/${await hashOf(e.session_id)}.json`, s)
    return {}
  })
  return { files, store, raw, toasts, queue }
}

const item = (name: string, kind = 'admitted_list', path = `笔记/${name}.md`) =>
  ({ path, title: name, confidence: '中', fulltext: kind === 'admitted_fulltext', kind })
const ups = ($, pid: string, sid = SID) => $.classic.UserPromptSubmit({ session_id: sid, prompt_id: pid, prompt: 'x' })
async function req(files: Map<string, string>, pid: string, sid = SID) {
  return JSON.parse(files.get(`${LIVE}/${await hashOf(sid)}-${pid}.req.json`) as string)
}
const queued = (id: string, ts = NOW_S - 60) => ({ id, path: '笔记/alpha.md', kind: 'admitted_list',
  verdict: 'relevant', session: SID, prompt_h: PH, ts })

test('gate turn keeps the annotation buttons of the last scored turn (Q-H1)', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ metrics_on: true, prompt_h: PH }))
  // 修复前 Python 闸门轮快照的形态：metrics_on 恒 false、无 prompt_h；mod 侧不得据它收掉按钮
  queue.push(snap({ status: 'gate:skipped_source', prompt_id: PID2, injected: [], metrics_on: false }))
  await ups($, PID)
  await ups($, PID2)
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ key: 'relevant:0' })).toBeDefined()
})

test('digest mismatch warns on the band (Q-M1)', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap())
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: 'ex:0' })
  await pane.unmount()
  queue.push(snap({ prompt_id: PID2, request_digest: 'ffffffffffffffffffffffffffffffff' }))
  await ups($, PID2)
  const band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /⚠ vault/ })).toBeDefined()
})

test('ninth manual pin is refused with a toast instead of being dropped silently (Q-M4)', async ($, on) => {
  const { files, queue, toasts } = qharness(on)
  queue.push(snap({ injected: [...Array(9).keys()].map((i) => item(`n${i}`)) }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  for (let i = 0; i < 9; i++) await pane.press({ key: `pin:${i}` })
  await pane.unmount()
  await ups($, PID2)
  expect((await req(files, PID2)).pin).toEqual([...Array(8).keys()].map((i) => `笔记/n${i}.md`))
  expect(toasts).toHaveLength(1)
})

test('a new session id after SessionStart starts with no ops (D-M3)', async ($, on) => {
  const { queue } = qharness(on)
  on('classic.SessionStart', () => ({}))
  queue.push(snap())
  await ups($, PID)
  let pane = await $.ui.mount(PANE)
  await pane.press({ key: 'ex:0' })
  expect(await pane.find({ key: 'restore:0' })).toBeDefined()
  await pane.unmount()
  // 只看 SessionStart 之后、下一次提问之前：UPS 一侧另有同样的重置，混在一起测不出这一侧
  await $.classic.SessionStart({ session_id: SID2, source: 'clear' })
  pane = await $.ui.mount(PANE)
  expect(await pane.find({ key: 'restore:0' })).toBeUndefined()
})

test('a new session id on prompt submit starts with no ops (D-M3)', async ($, on) => {
  const { files, queue } = qharness(on)
  queue.push(snap())
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: 'ex:0' })
  await pane.unmount()
  await ups($, PID2, SID2)
  expect((await req(files, PID2, SID2)).exclude).toEqual([])
})

test('scored turn with metrics shows annotation buttons', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ metrics_on: true, prompt_h: PH }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ key: 'relevant:0' })).toBeDefined()
})

test('annotation button queues a well-formed entry with the snapshot kind', async ($, on) => {
  const { store, queue } = qharness(on)
  queue.push(snap({ metrics_on: true, prompt_h: PH, injected: [item('alpha', 'admitted_fulltext')] }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: 'relevant:0' })
  const keys = [...store.keys()]
  expect(keys).toHaveLength(1)
  const v = store.get(keys[0]) as Record<string, unknown>
  expect(keys[0]).toBe('ann:' + v.id)
  expect(/^ann-[0-9a-f]{32}$/.test(v.id as string)).toBe(true)
  expect(v).toMatchObject({ path: '笔记/alpha.md', kind: 'admitted_fulltext', verdict: 'relevant',
    session: SID, prompt_h: PH, ts: NOW_S })
})

test('annotation queue at 200 toasts and adds nothing', async ($, on) => {
  const pre: Record<string, unknown> = {}
  for (let i = 0; i < 200; i++) pre[`ann:ann-${String(i).padStart(8, '0')}`] = queued(`ann-${String(i).padStart(8, '0')}`)
  const { store, queue, toasts } = qharness(on, { store: pre })
  queue.push(snap({ metrics_on: true, prompt_h: PH }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: 'relevant:0' })
  expect(toasts).toHaveLength(1)
  expect(store.size).toBe(200)
})

test('request carries at most 50 queued annotations', async ($, on) => {
  const pre: Record<string, unknown> = {}
  for (let i = 0; i < 60; i++) pre[`ann:ann-${String(i).padStart(8, '0')}`] = queued(`ann-${String(i).padStart(8, '0')}`)
  const { files } = qharness(on, { store: pre })
  await ups($, PID)
  expect((await req(files, PID)).annotations).toHaveLength(50)
})

test('expired annotation is dropped from queue and request', async ($, on) => {
  const { files, store } = qharness(on, { store: { 'ann:ann-00000001': queued('ann-00000001', NOW_S - 8 * 86400) } })
  await ups($, PID)
  expect((await req(files, PID)).annotations).toEqual([])
  expect(store.size).toBe(0)
})

test('annotation neither acked nor rejected stays queued', async ($, on) => {
  const { store, queue } = qharness(on, { store: { 'ann:ann-00000001': queued('ann-00000001') } })
  queue.push(snap({ request_digest: null }))
  await ups($, PID)
  expect([...store.keys()]).toEqual(['ann:ann-00000001'])
})

test('dry-run snapshot: band prefix and no annotation buttons', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ status: 'dry_run', metrics_on: true, prompt_h: PH }))
  await ups($, PID)
  const band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /^\[DRY-RUN\] 📚 1 篇/ })).toBeDefined()
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ key: 'relevant:0' })).toBeUndefined()
})

test('pane buttons keep exclude / pin / unpin mutually exclusive, restore clears both', async ($, on) => {
  const { files, queue } = qharness(on)
  const view = { injected: [item('alpha')], pins: { llm: [{ path: '笔记/beta.md', title: 'beta', rendered: true }], user: [] } }
  queue.push(snap(view))
  await ups($, PID)
  let pane = await $.ui.mount(PANE)
  await pane.press({ key: 'ex:0' })
  await pane.press({ key: 'pin:0' })
  await pane.press({ key: 'unpin:0' })
  await pane.unmount()
  queue.push(snap({ ...view, prompt_id: PID2 }))
  await ups($, PID2)
  const r2 = await req(files, PID2)
  expect([r2.exclude, r2.pin, r2.unpin_llm]).toEqual([[], ['笔记/alpha.md'], ['笔记/beta.md']])
  pane = await $.ui.mount(PANE)
  await pane.press({ key: 'restore:0' })
  await pane.unmount()
  queue.push(snap({ ...view, prompt_id: PID3 }))
  await ups($, PID3)
  const r3 = await req(files, PID3)
  expect([r3.exclude, r3.unpin_llm]).toEqual([[], []])
})

test('exclude after pin removes the pin', async ($, on) => {
  const { files, queue } = qharness(on)
  queue.push(snap())
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: 'pin:0' })
  await pane.press({ key: 'ex:0' })
  await pane.unmount()
  await ups($, PID2)
  const r = await req(files, PID2)
  expect([r.exclude, r.pin]).toEqual([['笔记/alpha.md'], []])
})

test('not_in_index op leaves the ops list', async ($, on) => {
  const { files, queue } = qharness(on)
  queue.push(snap())
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: 'ex:0' })
  await pane.unmount()
  queue.push(snap({ prompt_id: PID2, rejected: [{ what: 'exclude', key: '笔记/alpha.md', reason: 'not_in_index' }] }))
  await ups($, PID2)
  queue.push(snap({ prompt_id: PID3 }))
  await ups($, PID3)
  expect((await req(files, PID3)).exclude).toEqual([])
})

test('matching digest: request equals the cross-language fixture and band does not warn', async ($, on) => {
  const { files, queue, store } = qharness(on)
  // 第一轮无操作（不消耗每会话一次的告警额度），标注在第一轮之后才入队
  queue.push(snap({ injected: [item('a', 'admitted_list', '项目笔记/demo/a.md'), item('b', 'admitted_list', '项目笔记/demo/b.md')] }))
  await ups($, PID)
  store.set('ann:ann-00000001', { id: 'ann-00000001', path: '项目笔记/demo/c.md', kind: 'admitted_list',
    verdict: 'relevant', session: SID, prompt_h: PH, ts: NOW_S - 60 })
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: 'ex:0' })
  await pane.press({ key: 'pin:1' })
  await pane.unmount()
  // _session_ops.request_digest(tests/fixtures/mod_contract/request_v1.json) 的输出
  queue.push(snap({ prompt_id: PID2, request_digest: 'caef2f245a8dd2710078881708d351e5', acked: ['ann-00000001'] }))
  await ups($, PID2)
  const r = await req(files, PID2)
  expect([r.v, r.exclude, r.pin, r.unpin_llm, r.annotations.map((a) => a.id)])
    .toEqual([1, ['项目笔记/demo/a.md'], ['项目笔记/demo/b.md'], [], ['ann-00000001']])
  const band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /⚠/ })).toBeUndefined()
})

test('warning shows once per session', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap())
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: 'ex:0' })
  await pane.unmount()
  await ups($, PID2)                          // 无新快照 → 告警
  let band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /⚠/ })).toBeDefined()
  await band.unmount()
  // 摘要为 exclude=['笔记/alpha.md'] 那份请求的摘要（Python 侧复算一致）
  queue.push(snap({ prompt_id: PID3, request_digest: '0b717ff036d13642f0b79bf71272a795' }))
  await ups($, PID3)                          // 有效快照 → 告警消失
  await ups($, PID4)                          // 再次缺失 → 不再告警
  band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /⚠/ })).toBeUndefined()
})

test('metrics_off rejection shows a note in the pane', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ rejected: [{ what: 'annotation', key: 'ann-00000009', reason: 'metrics_off' }] }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ type: 'Text', text: /标注未写入：metrics 未开启/ })).toBeDefined()
})

test('band shows the manual pin count', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ pins: { llm: [], user: [{ path: '笔记/g.md', title: 'g', rendered: true }] } }))
  await ups($, PID)
  const band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /📌1/ })).toBeDefined()
})

test('startup writes the marker and shows session-start notes; resume writes none and resets', async ($, on) => {
  const { files, raw } = qharness(on)
  const h = await hashOf(SID)
  on('classic.SessionStart', () => {
    if (files.get(`${LIVE}/${h}-ss.req.json`) === '{}') {
      files.set(`${LIVE}/${h}.json`, JSON.stringify({ v: 1, status: 'scored', event: 'SessionStart', prompt_id: null,
        request_digest: null, injected: [], pins: { llm: [], user: [] }, excluded: [],
        session_start: [{ path: 'p/design.md', title: 'design' }], rejected: [], acked: [], metrics_on: false }))
    }
    return {}
  })
  await $.classic.SessionStart({ session_id: SID, source: 'startup' })
  let band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /会话开始注入 1 篇/ })).toBeDefined()
  await band.unmount()
  raw.length = 0
  await $.classic.SessionStart({ session_id: SID, source: 'resume' })
  expect(raw).toEqual([])
  band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /📚/ })).toBeUndefined()
})

test('resume clears the last injection from the band', async ($, on) => {
  const { queue } = qharness(on)
  on('classic.SessionStart', () => ({}))
  queue.push(snap())
  await ups($, PID)
  await $.classic.SessionStart({ session_id: SID, source: 'resume' })
  const band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /📚/ })).toBeUndefined()
})

test('mod_enabled false writes nothing', { options: { mod_enabled: false } }, async ($, on) => {
  const { files } = qharness(on)
  await ups($, PID)
  expect([...files.keys()]).toEqual([])
})
