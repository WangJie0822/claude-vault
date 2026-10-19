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
  expect(await ui.find({ type: 'Text', text: /📚 粗排 1 · alpha/ })).toBeDefined()
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
  expect(await ui.find({ type: 'Text', text: /^（本轮未召回） 📚 粗排 1 · alpha/ })).toBeDefined()
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
  await act(pane, 'inj', '笔记/alpha.md', 'ex')
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
  expect(await ui.find({ type: 'Text', text: /📚 粗排 1 · abc/ })).toBeDefined()
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

function qharness(on, opts: { store?: Record<string, unknown>; env?: Record<string, string> } = {}) {
  const files = new Map<string, string>()
  const raw: string[] = []
  const store = new Map<string, unknown>(Object.entries(opts.store ?? {}))
  const toasts: string[] = []
  const queue: (string | null)[] = []
  // 每一项在对应那一轮里、请求已写出而快照还没写出之前执行：模拟这一轮处理期间发生的事。
  // 用例里写成闭包、用测试自己的 `$`：测试 hook 收到的 `$` 不能调 tool.call（宿主规则）
  const during: (() => Promise<void>)[] = []
  mock.env(on, opts.env ?? { OS: 'Linux', HOME })
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
    await during.shift()?.()
    const s = queue.shift()
    if (s) files.set(`${LIVE}/${await hashOf(e.session_id)}.json`, s)
    return {}
  })
  return { files, store, raw, toasts, queue, during }
}

const item = (name: string, kind = 'admitted_list', path = `笔记/${name}.md`) =>
  ({ path, title: name, confidence: '中', fulltext: kind === 'admitted_fulltext', kind })
const ups = ($, pid: string, sid = SID) => $.classic.UserPromptSubmit({ session_id: sid, prompt_id: pid, prompt: 'x' })
// 与 mod 的 pathKey 同一算法（FNV-1a 32 位）：key 由 path 派生，不用序号
function pk(p: string): string {
  let h = 0x811c9dc5
  for (const ch of p) {
    h ^= ch.codePointAt(0) as number
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h.toString(16).padStart(8, '0')
}
const rowKey = (sec: string, p: string) => `row:${sec}:${pk(p)}`
// 展开该篇（未展开时）后返回它的操作按钮；该篇不在面板里时返回 undefined
async function shown(pane, sec: string, p: string, name: string) {
  if (await pane.find({ key: `${name}:${pk(p)}` })) return pane.find({ key: `${name}:${pk(p)}` })
  if (!(await pane.find({ key: rowKey(sec, p) }))) return undefined
  await pane.press({ key: rowKey(sec, p) })
  return pane.find({ key: `${name}:${pk(p)}` })
}
async function act(pane, sec: string, p: string, name: string) {
  expect(await shown(pane, sec, p, name)).toBeDefined()
  return pane.press({ key: `${name}:${pk(p)}` })
}
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
  expect(await shown(pane, 'inj', '笔记/alpha.md', 'relevant')).toBeDefined()
})

test('digest mismatch warns on the band (Q-M1)', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap())
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await act(pane, 'inj', '笔记/alpha.md', 'ex')
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
  for (let i = 0; i < 9; i++) await act(pane, 'inj', `笔记/n${i}.md`, 'pin')
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
  await act(pane, 'inj', '笔记/alpha.md', 'ex')
  expect(await shown(pane, 'exc', '笔记/alpha.md', 'restore')).toBeDefined()
  await pane.unmount()
  // 只看 SessionStart 之后、下一次提问之前：UPS 一侧另有同样的重置，混在一起测不出这一侧
  await $.classic.SessionStart({ session_id: SID2, source: 'clear' })
  pane = await $.ui.mount(PANE)
  expect(await shown(pane, 'exc', '笔记/alpha.md', 'restore')).toBeUndefined()
})

test('a new session id on prompt submit starts with no ops (D-M3)', async ($, on) => {
  const { files, queue } = qharness(on)
  queue.push(snap())
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await act(pane, 'inj', '笔记/alpha.md', 'ex')
  await pane.unmount()
  await ups($, PID2, SID2)
  expect((await req(files, PID2, SID2)).exclude).toEqual([])
})

test('scored turn with metrics shows annotation buttons', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ metrics_on: true, prompt_h: PH }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect(await shown(pane, 'inj', '笔记/alpha.md', 'relevant')).toBeDefined()
})

test('annotation button queues a well-formed entry with the snapshot kind', async ($, on) => {
  const { store, queue } = qharness(on)
  queue.push(snap({ metrics_on: true, prompt_h: PH, injected: [item('alpha', 'admitted_fulltext')] }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await act(pane, 'inj', '笔记/alpha.md', 'relevant')
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
  await act(pane, 'inj', '笔记/alpha.md', 'relevant')
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
  expect(await band.find({ type: 'Text', text: /^\[DRY-RUN\] 📚 粗排 1/ })).toBeDefined()
  const pane = await $.ui.mount(PANE)
  expect(await shown(pane, 'inj', '笔记/alpha.md', 'relevant')).toBeUndefined()
  // 先证明确实展开了：否则「没有按钮」只是因为没展开（节点 1 T4）
  expect(await pane.find({ type: 'Text', text: /来源  脚本粗排/ })).toBeDefined()
  expect(await pane.find({ key: `ex:${pk('笔记/alpha.md')}` })).toBeDefined()
})

test('pane buttons keep exclude / pin / unpin mutually exclusive, restore clears both', async ($, on) => {
  const { files, queue } = qharness(on)
  const view = { injected: [item('alpha')], pins: { llm: [{ path: '笔记/beta.md', title: 'beta', rendered: true }], user: [] } }
  queue.push(snap(view))
  await ups($, PID)
  let pane = await $.ui.mount(PANE)
  await act(pane, 'inj', '笔记/alpha.md', 'ex')
  await act(pane, 'inj', '笔记/alpha.md', 'pin')
  await act(pane, 'llm', '笔记/beta.md', 'unpin')
  await pane.unmount()
  queue.push(snap({ ...view, prompt_id: PID2 }))
  await ups($, PID2)
  const r2 = await req(files, PID2)
  expect([r2.exclude, r2.pin, r2.unpin_llm]).toEqual([[], ['笔记/alpha.md'], ['笔记/beta.md']])
  pane = await $.ui.mount(PANE)
  await act(pane, 'exc', '笔记/beta.md', 'restore')
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
  await act(pane, 'inj', '笔记/alpha.md', 'pin')
  await act(pane, 'inj', '笔记/alpha.md', 'ex')
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
  await act(pane, 'inj', '笔记/alpha.md', 'ex')
  await pane.unmount()
  queue.push(snap({ prompt_id: PID2, rejected: [{ what: 'exclude', key: '笔记/alpha.md', reason: 'not_in_index' }] }))
  await ups($, PID2)
  queue.push(snap({ prompt_id: PID3 }))
  await ups($, PID3)
  expect((await req(files, PID3)).exclude).toEqual([])
})

const VROOT = '/home/u/vault'
const EFF = { coarse_inject: true, fine_rank: true, fine_supplement: true, auto_fulltext: false,
  'user_prompt_submit.max_notes': 3, fine_pin_max: 1, fine_max_notes: 3, fine_model: 'haiku' }
const SRC = Object.fromEntries(Object.keys(EFF).map((k) => [k, 'default']))
const SETTINGS_SNAP = (over: Record<string, unknown> = {}) =>
  ({ effective: { ...EFF }, session: {}, locked: [], source: { ...SRC }, ...over })

test('matching digest: request equals the cross-language fixture and band does not warn', async ($, on) => {
  const { files, queue, store } = qharness(on)
  tools(on)
  // 第一轮无操作（不消耗每会话一次的告警额度），标注在第一轮之后才入队
  queue.push(snap({ injected: [item('a', 'admitted_list', '项目笔记/demo/a.md'), item('b', 'admitted_list', '项目笔记/demo/b.md')],
    settings: SETTINGS_SNAP(), vault_root: VROOT }))
  await ups($, PID)
  store.set('ann:ann-00000001', { id: 'ann-00000001', path: '项目笔记/demo/c.md', kind: 'admitted_list',
    verdict: 'relevant', session: SID, prompt_h: PH, ts: NOW_S - 60 })
  const pane = await $.ui.mount(PANE)
  await act(pane, 'inj', '项目笔记/demo/a.md', 'ex')
  await act(pane, 'inj', '项目笔记/demo/b.md', 'pin')
  await pane.press({ key: 'dec:fine_max_notes' })
  await pane.press({ key: 'persist' })
  await pane.unmount()
  await readFile($, `${VROOT}/项目笔记/demo/c.md`)
  // _session_ops.request_digest(tests/fixtures/mod_contract/request_v1.json) 的输出（Python 侧同一固定值）
  queue.push(snap({ prompt_id: PID2, request_digest: 'a0aa76f4f72077b40c8676494f9f2ca2', acked: ['ann-00000001'] }))
  await ups($, PID2)
  const r = await req(files, PID2)
  expect([r.v, r.exclude, r.pin, r.unpin_llm, r.annotations.map((a) => a.id), r.settings, r.persist, r.reads])
    .toEqual([1, ['项目笔记/demo/a.md'], ['项目笔记/demo/b.md'], [], ['ann-00000001'], { fine_max_notes: 2 },
      ['fine_max_notes'], ['项目笔记/demo/c.md']])
  const band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /⚠/ })).toBeUndefined()
})

test('warning shows once per session', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap())
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await act(pane, 'inj', '笔记/alpha.md', 'ex')
  await pane.unmount()
  await ups($, PID2)                          // 无新快照 → 告警
  let band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /⚠/ })).toBeDefined()
  await band.unmount()
  // 摘要为 exclude=['笔记/alpha.md']、settings {}、persist []、reads [] 那份请求的摘要（Python 侧复算一致）
  queue.push(snap({ prompt_id: PID3, request_digest: '74499db0ee2aaa076f8e8954de1e6775' }))
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
  expect(await band.find({ type: 'Text', text: /手动📌1/ })).toBeDefined()
  expect(await band.find({ type: 'Text', text: /精排📌/ })).toBeUndefined()
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

// ── spec 2026-10-12：快照新键、band 来源计数与精排简标 ────────────────────────────────
const LLM = (over: Record<string, unknown> = {}) => ({ state: 'ok', fail: null, words: ['设计'],
  in_scoring: 1, n_pinned: 1, age_s: 30, pin_on: true, stale_pins: false, ...over })
// 同一用例里只建一个 harness、换 prompt_id 跑多轮：重复调用 qharness 会叠加 fs / store 的 hook，
// 先注册的那组会用它自己的（旧）文件表应答
const PIDS = [PID, PID2, PID3, PID4]

test('band marks the llm state and never shows llm words', async ($, on) => {
  const { queue } = qharness(on)
  const cases = [['ok', '精排✓'], ['pending', '精排…'], ['failed', '精排✗'], ['unavailable', '精排✗']]
  for (let i = 0; i < cases.length; i++) {
    const [state, mark] = cases[i]
    queue.push(snap({ prompt_id: PIDS[i], llm: LLM({ state, words: ['秘密词'] }) }))
    await ups($, PIDS[i])
    const band = await $.ui.mount(BAND)
    const t = (await band.find({ type: 'Text', text: /📚/ }))!.text
    expect(t.includes(mark)).toBe(true)
    expect(t.includes('秘密词')).toBe(false)
    await band.unmount()
  }
})

test('band shows no llm mark when unknown, off or idle', async ($, on) => {
  const { queue } = qharness(on)
  const cases = [null, LLM({ state: 'off' }), LLM({ state: 'idle' })]   // null 放第一轮：此时还没有上一次的值可保留
  for (let i = 0; i < cases.length; i++) {
    queue.push(snap({ prompt_id: PIDS[i], llm: cases[i] }))
    await ups($, PIDS[i])
    const band = await $.ui.mount(BAND)
    expect(await band.find({ type: 'Text', text: /精排[✓…✗]/ })).toBeUndefined()
    await band.unmount()
  }
})

test('band counts sources separately and keeps the excluded count', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ pins: { llm: [{ path: '笔记/d.md', title: 'd', rendered: true }],
    user: [{ path: '笔记/g.md', title: 'g', rendered: true }] },
  excluded: [{ path: '笔记/x.md', title: 'x' }] }))
  await ups($, PID)
  const band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /^📚 粗排 1 · 精排📌1 · 手动📌1 · 排除 1 · alpha · \/vault-injected$/ })).toBeDefined()
})

test('band keeps a blank row above itself only when the host gives 3 rows or more', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap())
  await ups($, PID)
  let band = await $.ui.mount(BAND)
  expect((await band.drawn()).props.marginTop).toBe(1)
  await band.unmount()
  band = await $.ui.mount({ ...BAND, props: { ...BAND.props, maxRows: 2 } })
  expect((await band.drawn()).props.marginTop).toBe(0)
})

test('gate snapshot refreshes llm but keeps the list', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ llm: LLM({ state: 'pending', age_s: 3 }) }))
  queue.push(snap({ status: 'gate:deictic_only', prompt_id: PID2, injected: [], llm: LLM() }))
  await ups($, PID)
  await ups($, PID2)
  const band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /^（本轮未召回） 📚 粗排 1 · 精排✓ · alpha/ })).toBeDefined()
})

test('old snapshot without new keys still draws the band', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap())                       // snap() 只有 2.1.0 的键
  await ups($, PID)
  const band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /^📚 粗排 1 · alpha · \/vault-injected$/ })).toBeDefined()
})

// ── spec 2026-10-12：面板折叠行、排版与焦点 ──────────────────────────────────────────

test('only one entry is expanded and a second press collapses it', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ injected: [item('alpha'), item('beta')] }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: rowKey('inj', '笔记/alpha.md') })
  expect(await pane.find({ key: `ex:${pk('笔记/alpha.md')}` })).toBeDefined()
  await pane.press({ key: rowKey('inj', '笔记/beta.md') })
  expect(await pane.find({ key: `ex:${pk('笔记/alpha.md')}` })).toBeUndefined()
  expect(await pane.find({ key: `ex:${pk('笔记/beta.md')}` })).toBeDefined()
  await pane.press({ key: rowKey('inj', '笔记/beta.md') })
  expect(await pane.find({ key: `ex:${pk('笔记/beta.md')}` })).toBeUndefined()
})

test('keys follow the path when a new snapshot reorders the list', async ($, on) => {
  const { files, queue } = qharness(on)
  queue.push(snap({ injected: [item('alpha'), item('beta')] }))
  await ups($, PID)
  let pane = await $.ui.mount(PANE)
  await act(pane, 'inj', '笔记/beta.md', 'ex')
  await pane.unmount()
  queue.push(snap({ prompt_id: PID2, injected: [item('beta'), item('alpha')] }))
  await ups($, PID2)
  pane = await $.ui.mount(PANE)
  await act(pane, 'inj', '笔记/alpha.md', 'pin')
  await pane.unmount()
  await ups($, PID3)
  const r = await req(files, PID3)
  expect([r.exclude, r.pin]).toEqual([['笔记/beta.md'], ['笔记/alpha.md']])
})

test('expanded entry collapses when a new snapshot drops it', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap())
  queue.push(snap({ prompt_id: PID2, injected: [item('beta')] }))
  queue.push(snap({ prompt_id: PID3 }))
  await ups($, PID)
  let pane = await $.ui.mount(PANE)
  await pane.press({ key: rowKey('inj', '笔记/alpha.md') })
  await pane.unmount()
  await ups($, PID2)
  await ups($, PID3)
  pane = await $.ui.mount(PANE)
  expect(await pane.find({ key: `ex:${pk('笔记/alpha.md')}` })).toBeUndefined()
})

test('actions carry single-letter hotkeys and live only in the expanded area', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ metrics_on: true, prompt_h: PH }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect((await pane.findAll({ type: 'Button' })).filter((b) => b.props.hotkey)).toHaveLength(0)
  await pane.press({ key: rowKey('inj', '笔记/alpha.md') })
  const hk = (await pane.findAll({ type: 'Button' })).filter((b) => b.props.hotkey).map((b) => b.props.hotkey)
  expect(hk).toEqual(['x', 'p', 'y', 'n'])
})

test('pane has side padding, bold section titles and hides empty sections', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap())
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect((await pane.drawn()).props.paddingX).toBe(1)
  expect((await pane.find({ type: 'Text', text: /^脚本粗排 · 1 篇$/ }))!.props.bold).toBe(true)
  expect(await pane.find({ type: 'Text', text: /^手动钉住/ })).toBeUndefined()
  expect(await pane.find({ type: 'Text', text: /^会话开始注入/ })).toBeUndefined()
  expect(await pane.find({ type: 'Text', text: /^LLM 精排/ })).toBeDefined()
})

test('narrow pane truncates by cell width and never splits a surrogate pair', async ($, on) => {
  const isWideCh = (ch: string) => ch.length > 1 || (ch >= '　' && ch <= '鿿') || (ch >= '＀' && ch <= '￯')
  const wide = (s: string) => [...s].reduce((n, ch) => n + (isWideCh(ch) ? 2 : 1), 0)
  const loneSurrogate = (ch: string) => ch.length === 1 && ch.charCodeAt(0) >= 0xd800 && ch.charCodeAt(0) <= 0xdfff
  const { queue } = qharness(on)
  const titles = ['很长的中文标题'.repeat(6), String.fromCodePoint(0x1f600).repeat(30)]
  for (let i = 0; i < titles.length; i++) {
    queue.push(snap({ prompt_id: PIDS[i], injected: [{ ...item('t'), title: titles[i] }] }))
    await ups($, PIDS[i])
    const pane = await $.ui.mount({ ...PANE, props: { ...PANE.props, bodyColumns: 24 } })
    const label = (await pane.find({ key: rowKey('inj', '笔记/t.md') }))!.props.label as string
    expect(label.endsWith('…')).toBe(true)
    expect(wide(label)).toBeLessThanOrEqual(22)
    expect([...label].some(loneSurrogate)).toBe(false)
    await pane.unmount()
  }
})

test('old snapshot shows unknown provenance instead of guessing', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap())
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ type: 'Text', text: /^LLM 精排 · 未知/ })).toBeDefined()
  await pane.press({ key: rowKey('inj', '笔记/alpha.md') })
  expect(await pane.find({ type: 'Text', text: /脚本粗排 · 未知 · topical 未知/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /提问词：未知/ })).toBeDefined()
})

test('expanded entry explains script scoring and llm topic words', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ injected: [{ ...item('alpha'), topical: 9, admit_arm: 'topical', hits: ['flavor'],
    topic_hits: ['配置化'], llm_also_pinned: true }],
  thresholds: { min_topical: 4, ft_topical: 6, topic_hit_weight: 2 }, llm: LLM() }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ type: 'Text', text: /\+主题词/ })).toBeDefined()
  await pane.press({ key: rowKey('inj', '笔记/alpha.md') })
  expect(await pane.find({ type: 'Text', text: /脚本粗排 · topical 达标 · topical 9（注入 ≥4，全文 ≥6 且强证据）/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /提问词：flavor ｜ LLM 主题词：配置化（\+2）/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /LLM 也点名了这篇/ })).toBeDefined()
})

test('llm section shows state, cleaned words and pin rank', async ($, on) => {
  const { queue } = qharness(on)
  const rlo = String.fromCharCode(0x202e)
  queue.push(snap({ llm: LLM({ words: ['a' + rlo + 'b'], age_s: 125 }),
    pins: { llm: [{ path: '笔记/d.md', title: 'd', rendered: true, rank: 2, dedup: null },
      { path: '笔记/e.md', title: 'e', rendered: false, rank: null, dedup: 'candidate_injected' }], user: [] },
    thresholds: { min_topical: 4, ft_topical: 6, topic_hit_weight: 2 } }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ type: 'Text', text: /^LLM 精排 · 已完成 · 2 分钟前完成$/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /主题词 1 个（本轮打分已用 1）：ab$/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /LLM 点名 · 粗排第 2/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /LLM 点名 · 已注入过/ })).toBeDefined()
})

test('llm failure states name the reason', async ($, on) => {
  const { queue } = qharness(on)
  const cases = [
    [LLM({ state: 'failed', fail: 'lost', words: [] }), /^LLM 精排 · 失败：结果丢失$/],
    [LLM({ state: 'unavailable', fail: 'no_claude', words: [] }), /^LLM 精排 · 无法发起：找不到 claude 命令/],
    [LLM({ state: 'pending', words: [], age_s: 12 }), /^LLM 精排 · 提炼中（已 12 秒，截至上次提问）/],
  ] as const
  for (let i = 0; i < cases.length; i++) {
    const [llm, re] = cases[i]
    queue.push(snap({ prompt_id: PIDS[i], llm }))
    await ups($, PIDS[i])
    const pane = await $.ui.mount(PANE)
    expect(await pane.find({ type: 'Text', text: re })).toBeDefined()
    await pane.unmount()
  }
})

test('session-start rows show why they were picked', async ($, on) => {
  const { files } = qharness(on)
  const h = await hashOf(SID)
  on('classic.SessionStart', () => {
    files.set(`${LIVE}/${h}.json`, JSON.stringify({ v: 1, status: 'scored', event: 'SessionStart', prompt_id: null,
      request_digest: null, injected: [], pins: { llm: [], user: [] }, excluded: [],
      session_start: [{ path: 'p/design.md', title: 'design', why: ['project_dir', 'tag'], tags: ['设计'] }],
      rejected: [], acked: [], metrics_on: false, llm: null, thresholds: null, ts: NOW_S }))
    return {}
  })
  await $.classic.SessionStart({ session_id: SID, source: 'startup' })
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ type: 'Text', text: /项目目录 \/ 标签 设计/ })).toBeDefined()
})

test('header tells how old the snapshot is', async ($, on) => {
  const { queue } = qharness(on)
  const cases = [[NOW_S - 180, /截至上次提问（3 分钟前）/], [NOW_S + 100, /截至上次提问（刚刚）/]] as const
  for (let i = 0; i < cases.length; i++) {
    const [ts, re] = cases[i]
    queue.push(snap({ prompt_id: PIDS[i], ts }))
    await ups($, PIDS[i])
    const pane = await $.ui.mount(PANE)
    expect(await pane.find({ type: 'Text', text: re })).toBeDefined()
    await pane.unmount()
  }
})

test('opening the pane asks for rows that fit its content', async ($, on) => {
  const { queue } = qharness(on)
  const opened: Record<string, unknown>[] = []
  on('ui.open', ($, e) => { opened.push(e); return { value: { isPlaced: true } } })
  queue.push(snap())
  await ups($, PID)
  await $.command.run({ command: 'vault-injected', args: '' })
  expect(opened).toHaveLength(1)
  expect(Number.isInteger(opened[0].rows)).toBe(true)
  expect(opened[0].rows as number).toBeGreaterThanOrEqual(23)
})

// ── full-review 修复（.full-review/05-final-report.md）────────────────────────────────

// 旧版 mod 写进 $.state 的 view：条目只有 2.1.0 的五个键，没有 llm / thresholds / ts。
// 热重载后新代码读到的就是它（d.ts：$.state 是「plain data that survives a hot reload」）。
const OLD_ITEM = { path: '笔记/alpha.md', title: 'alpha', confidence: '中', fulltext: false, kind: 'admitted_list' }
const OLD_VIEW = { status: 'scored', last: { injected: [OLD_ITEM] }, gateNow: false, sessionStart: [], excluded: [],
  pins: { llm: [{ path: '笔记/d.md', title: 'd', rendered: true }], user: [] },
  metricsOn: false, promptH: '', session: SID, note: '', queued: 0 }
const NEW_VIEW = { ...OLD_VIEW, llm: null, thresholds: null, ts: 0, last: { injected: [{ ...OLD_ITEM,
  topical: null, arm: '', hits: null, topicHits: null, alsoPinned: false }] } }

for (const [name, view] of [['old', OLD_VIEW], ['new', NEW_VIEW]] as const) {
  test(`pane draws a view left by ${name} mod code and shows unknown provenance (BP-M1)`, async ($, on) => {
    mock.clock(on, { now: NOW_S * 1000 })
    on('state.get', ($, e, next) => (e.plugin === 'context-vault' && e.key === 'view'
      ? { value: { value: view, version: 1 } } : next(e)))
    on('ui.render', () => ({ type: 'Text', props: {}, children: ['other'] }))
    const pane = await $.ui.mount(PANE)
    expect(await pane.find({ key: rowKey('inj', '笔记/alpha.md') })).toBeDefined()
    await pane.press({ key: rowKey('inj', '笔记/alpha.md') })
    expect(await pane.find({ type: 'Text', text: /脚本粗排 · 未知 · topical 未知/ })).toBeDefined()
    expect(await pane.find({ type: 'Text', text: /提问词：未知/ })).toBeDefined()
    expect(await pane.find({ type: 'Text', text: /undefined/ })).toBeUndefined()
  })
}

test('gate snapshot without llm / thresholds / ts keeps the last known ones (T-M3)', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ llm: LLM({ state: 'pending', words: [], age_s: 3 }), ts: NOW_S - 30,
    thresholds: { min_topical: 4, ft_topical: 6, topic_hit_weight: 2 } }))
  queue.push(snap({ status: 'gate:deictic_only', prompt_id: PID2, injected: [], llm: null, thresholds: null, ts: null }))
  await ups($, PID)
  await ups($, PID2)
  const band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /精排…/ })).toBeDefined()
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ type: 'Text', text: /截至上次提问（刚刚）/ })).toBeDefined()
  await pane.press({ key: rowKey('inj', '笔记/alpha.md') })
  expect(await pane.find({ type: 'Text', text: /（注入 ≥4，全文 ≥6 且强证据）/ })).toBeDefined()
})

test('an llm state this version does not know shows unknown instead of the previous state (A2)', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ llm: LLM({ state: 'ok' }) }))
  queue.push(snap({ prompt_id: PID2, llm: LLM({ state: 'retrying' }) }))
  await ups($, PID)
  await ups($, PID2)
  const band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /精排[✓…✗]/ })).toBeUndefined()
  const pane = await $.ui.mount(PANE)
  // 钉原因而不只是「未知」：把它当 null 时也会显示未知，但原因会被说成「快照来自旧版本或读取失败」
  expect(await pane.find({ type: 'Text', text: /^LLM 精排 · 未知（本版不认识的状态）$/ })).toBeDefined()
})

test('an unknown dedup value shows unknown, not a definite "not ranked" (A2)', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ pins: { llm: [{ path: '笔记/d.md', title: 'd', rendered: true, rank: null, dedup: 'later_kind' },
    { path: '笔记/e.md', title: 'e', rendered: true, rank: null, dedup: null }], user: [] } }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ type: 'Text', text: /LLM 点名 · 未知/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /LLM 点名 · 未进粗排/ })).toBeDefined()
})

test('dry_run says extraction is never started instead of waiting for a recall (A1)', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ status: 'dry_run', llm: LLM({ state: 'idle', fail: 'dry_run', words: [], age_s: null }) }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ type: 'Text', text: /^LLM 精排 · dry_run 下不发起提炼$/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /尚未发起/ })).toBeUndefined()
})

test('an upgrade candidate is not described as already full text (D-M5)', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ injected: [{ ...item('alpha'), topical: 7, admit_arm: 'upgrade_candidate', hits: ['hook'] }] }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: rowKey('inj', '笔记/alpha.md') })
  expect(await pane.find({ type: 'Text', text: /脚本粗排 · 升级候选（曾以清单注入） · topical 7/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /升级为全文/ })).toBeUndefined()
})

test('hits, topic hits and session-start tags are stripped before drawing (T-M5)', async ($, on) => {
  const { files, queue } = qharness(on)
  const rlo = String.fromCharCode(0x202e)
  const h = await hashOf(SID)
  on('classic.SessionStart', () => {
    files.set(`${LIVE}/${h}.json`, JSON.stringify({ v: 1, status: 'scored', event: 'SessionStart', prompt_id: null,
      request_digest: null, injected: [], pins: { llm: [], user: [] }, excluded: [],
      session_start: [{ path: 'p/design.md', title: 'design', why: ['tag'], tags: ['设' + rlo + '计'] }],
      rejected: [], acked: [], metrics_on: false, llm: null, thresholds: null, ts: NOW_S }))
    return {}
  })
  await $.classic.SessionStart({ session_id: SID, source: 'startup' })
  queue.push(snap({ injected: [{ ...item('alpha'), hits: ['h' + rlo + 'x'], topic_hits: ['t' + rlo + 'y'] }],
    thresholds: { min_topical: 4, ft_topical: 6, topic_hit_weight: 2 } }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ type: 'Text', text: /标签 设计/ })).toBeDefined()
  await pane.press({ key: rowKey('inj', '笔记/alpha.md') })
  expect(await pane.find({ type: 'Text', text: /提问词：hx ｜ LLM 主题词：ty/ })).toBeDefined()
})

test('first row takes the focus; the close button takes it when the pane is empty (T-M6)', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ injected: [item('alpha'), item('beta')] }))
  queue.push(snap({ prompt_id: PID2, injected: [] }))
  await ups($, PID)
  let pane = await $.ui.mount(PANE)
  let focused = (await pane.findAll({ type: 'Button' })).filter((b) => b.props.autoFocus)
  expect(focused.map((b) => b.props.key)).toEqual([rowKey('inj', '笔记/alpha.md')])
  await pane.unmount()
  await ups($, PID2)
  pane = await $.ui.mount(PANE)
  focused = (await pane.findAll({ type: 'Button' })).filter((b) => b.props.autoFocus)
  expect(focused.map((b) => b.props.key)).toEqual(['close'])
})

// ── 2.4.0：精排补充、面板设置与 Read 观测（spec 2026-10-13 §10）──────────────────────────
const ok = { result: {}, text: '1\tx', isReadOnly: true }
// 测试自己的 tool.call hook 在插件之下、代表引擎：按队列答复，队列空时答成功
function tools(on) {
  const answers: Record<string, unknown>[] = []
  const seen: Record<string, unknown>[] = []
  on('tool.call', ($, e) => { seen.push(e); return answers.shift() ?? ok })
  return { answers, seen }
}
const readFile = ($, file_path: string, extra: Record<string, unknown> = {}) =>
  $.tool.call({ tool: 'Read', file_path, ...extra } as never)

test('kit hands agentId to tool.call hooks (probe for the subagent case)', async ($, on) => {
  const { seen } = tools(on)
  await readFile($, '/x/a.md', { agentId: 'a1' })
  expect((seen[0] as { agentId?: string }).agentId).toBe('a1')
})

const supp = (name: string, over: Record<string, unknown> = {}) =>
  ({ path: `笔记/${name}.md`, title: name, src_seq: 3, recheck: 'passed', rendered: true, ...over })

// 与 mod 的请求摘要同一算法（八项数组的 JSON 取 sha256 前 32 位）。跨语言的固定值另有用例钉着
async function digestOf(r: { exclude?: string[]; pin?: string[]; unpin_llm?: string[]; ids?: string[];
  settings?: Record<string, unknown>; persist?: string[]; reads?: string[] } = {}) {
  const payload = [1, r.exclude ?? [], r.pin ?? [], r.unpin_llm ?? [], r.ids ?? [], r.settings ?? {},
    r.persist ?? [], r.reads ?? []]
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(payload)))
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 32)
}

test('request carries session settings every turn and a queued persist until the snapshot confirms it', async ($, on) => {
  const { files, queue } = qharness(on)
  queue.push(snap({ settings: SETTINGS_SNAP() }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: 'dec:fine_max_notes' })
  await pane.press({ key: 'set:fine_supplement' })
  await pane.press({ key: 'persist' })
  await pane.unmount()
  queue.push(snap({ prompt_id: PID2, settings: SETTINGS_SNAP(), persisted: ['fine_supplement', 'fine_max_notes'] }))
  await ups($, PID2)
  const r2 = await req(files, PID2)
  expect(r2.settings).toEqual({ fine_supplement: false, fine_max_notes: 2 })
  expect(r2.persist).toEqual(['fine_supplement', 'fine_max_notes'])
  await ups($, PID3)
  const r3 = await req(files, PID3)
  expect(r3.settings).toEqual({ fine_supplement: false, fine_max_notes: 2 })
  expect(r3.persist).toEqual([])
})

// Python 在闸门轮、dry_run、配置回落时既不处理 persist 也不回执：mod 不能在送出时就当它已完成
test('a gate turn keeps the queued persist and the observed Reads for the next requests', async ($, on) => {
  const { files, queue } = qharness(on)
  tools(on)
  queue.push(snap({ settings: SETTINGS_SNAP(), vault_root: VROOT }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: 'dec:fine_max_notes' })
  await pane.press({ key: 'persist' })
  await pane.unmount()
  await readFile($, `${VROOT}/a.md`)
  queue.push(snap({ status: 'gate:skipped_source', prompt_id: PID2, injected: [] }))
  queue.push(snap({ status: 'gate:deictic_only', prompt_id: PID3, injected: [] }))
  await ups($, PID2)
  await ups($, PID3)
  await ups($, PID4)
  for (const pid of [PID2, PID3, PID4]) {
    const r = await req(files, pid)
    expect([r.persist, r.reads, r.settings]).toEqual([['fine_max_notes'], ['a.md'], { fine_max_notes: 2 }])
  }
})

test('a snapshot that lists a persisted key takes only that key off the queue', async ($, on) => {
  const { files, queue } = qharness(on)
  queue.push(snap({ settings: SETTINGS_SNAP() }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: 'set:fine_supplement' })
  await pane.press({ key: 'dec:fine_max_notes' })
  await pane.press({ key: 'persist' })
  await pane.unmount()
  queue.push(snap({ prompt_id: PID2, settings: SETTINGS_SNAP(), persisted: ['fine_max_notes'] }))
  await ups($, PID2)
  expect((await req(files, PID2)).persist).toEqual(['fine_supplement', 'fine_max_notes'])
  await ups($, PID3)
  expect((await req(files, PID3)).persist).toEqual(['fine_supplement'])
})

test('a snapshot that rejected a persist key takes it off the queue', async ($, on) => {
  const { files, queue } = qharness(on)
  queue.push(snap({ settings: SETTINGS_SNAP() }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: 'set:fine_supplement' })
  await pane.press({ key: 'dec:fine_max_notes' })
  await pane.press({ key: 'persist' })
  await pane.unmount()
  queue.push(snap({ prompt_id: PID2, settings: SETTINGS_SNAP(),
    rejected: [{ what: 'persist', key: 'fine_max_notes', reason: 'widen' }] }))
  await ups($, PID2)
  await ups($, PID3)
  expect((await req(files, PID3)).persist).toEqual(['fine_supplement'])
})

test('records main-thread Reads under vault_root regardless of separator and case on Windows', async ($, on) => {
  const { files, queue } = qharness(on, { env: { OS: 'Windows_NT', USERPROFILE: 'C:\\Users\\u' } })
  tools(on)
  queue.push(snap({ vault_root: 'D:\\KB' }))
  await ups($, PID)
  await readFile($, 'D:\\KB\\笔记\\a.md')
  await readFile($, 'D:/KB/b.md')
  await readFile($, 'd:/kb/Sub/C.md')
  await readFile($, 'D:/KB/笔记/a.md')                    // 同一篇换个写法：只记一次
  await ups($, PID2)
  expect((await req(files, PID2)).reads).toEqual(['笔记/a.md', 'b.md', 'Sub/C.md'])
})

test('ignores Reads outside vault_root, relative, non-md, a sibling prefix or another case on POSIX', async ($, on) => {
  const { files, queue } = qharness(on)
  tools(on)
  queue.push(snap({ vault_root: VROOT }))
  await ups($, PID)
  for (const p of ['/home/u/other/a.md', 'vault/a.md', `${VROOT}/a.txt`, '/home/u/vault2/a.md',
    '/home/u/Vault/a.md', `${VROOT}/../x.md`, `${VROOT}/a.md`]) await readFile($, p)
  await ups($, PID2)
  expect((await req(files, PID2)).reads).toEqual(['a.md'])   // 最后一条在 vault 内：阳性对照
})

test('ignores Reads the engine refused', async ($, on) => {
  const { files, queue } = qharness(on)
  const { answers } = tools(on)
  queue.push(snap({ vault_root: VROOT }))
  await ups($, PID)
  answers.push({ result: {}, text: 'Claude requested permissions to read from it', isError: true })
  await readFile($, `${VROOT}/denied.md`)
  await readFile($, `${VROOT}/ok.md`)
  await ups($, PID2)
  expect((await req(files, PID2)).reads).toEqual(['ok.md'])
})

test('ignores Reads made inside a subagent', async ($, on) => {
  const { files, queue } = qharness(on)
  tools(on)
  queue.push(snap({ vault_root: VROOT }))
  await ups($, PID)
  await readFile($, `${VROOT}/sub.md`, { agentId: 'a1' })
  await readFile($, `${VROOT}/main.md`)
  await ups($, PID2)
  expect((await req(files, PID2)).reads).toEqual(['main.md'])
})

test('records at most 50 Reads, keeping the newest, and sends each confirmed batch once', async ($, on) => {
  const { files, queue } = qharness(on)
  tools(on)
  queue.push(snap({ vault_root: VROOT }))
  await ups($, PID)
  for (let i = 0; i < 55; i++) await readFile($, `${VROOT}/n${i}.md`)
  const kept = Array.from({ length: 50 }, (_, i) => `n${i + 5}.md`)      // 超限时丢最旧的 5 篇
  queue.push(snap({ prompt_id: PID2, request_digest: await digestOf({ reads: kept }) }))
  await ups($, PID2)
  expect((await req(files, PID2)).reads).toEqual(kept)
  await ups($, PID3)
  expect((await req(files, PID3)).reads).toEqual([])
})

// 只有摘要对得上的 scored 快照才说明 Python 读到的是我们送出的那份 reads；之后只移除送出的那几条
test('a matching scored snapshot removes only the Reads that were sent', async ($, on) => {
  const { files, queue, during } = qharness(on)
  tools(on)
  queue.push(snap({ vault_root: VROOT }))
  await ups($, PID)
  await readFile($, `${VROOT}/a.md`)
  await readFile($, `${VROOT}/b.md`)
  during.push(async () => { await readFile($, `${VROOT}/c.md`) })        // 第二轮处理期间主模型又读了一篇
  queue.push(snap({ prompt_id: PID2, request_digest: await digestOf({ reads: ['a.md', 'b.md'] }) }))
  await ups($, PID2)
  expect((await req(files, PID2)).reads).toEqual(['a.md', 'b.md'])
  await ups($, PID3)
  expect((await req(files, PID3)).reads).toEqual(['c.md'])
})

test('Reads stay queued when the snapshot answers another request or is not a scored turn', async ($, on) => {
  const { files, queue } = qharness(on)
  tools(on)
  queue.push(snap({ vault_root: VROOT }))
  await ups($, PID)
  await readFile($, `${VROOT}/a.md`)
  const mine = await digestOf({ reads: ['a.md'] })
  queue.push(snap({ prompt_id: PID2, request_digest: 'f'.repeat(32) }))                  // scored，但答的不是这份请求
  queue.push(snap({ status: 'dry_run', prompt_id: PID3, request_digest: mine }))         // 摘要对得上，但不是 scored
  await ups($, PID2)
  await ups($, PID3)
  await ups($, PID4)
  for (const pid of [PID3, PID4]) expect((await req(files, pid)).reads).toEqual(['a.md'])
})

test('session-start snapshot supplies vault_root before the first prompt', async ($, on) => {
  const { files } = qharness(on)
  tools(on)
  const h = await hashOf(SID)
  on('classic.SessionStart', () => {
    files.set(`${LIVE}/${h}.json`, JSON.stringify({ v: 1, status: 'scored', event: 'SessionStart', prompt_id: null,
      request_digest: null, injected: [], pins: { llm: [], user: [] }, excluded: [],
      session_start: [{ path: 'p/design.md', title: 'design' }], rejected: [], acked: [], metrics_on: false,
      vault_root: VROOT }))
    return {}
  })
  await $.classic.SessionStart({ session_id: SID, source: 'startup' })
  await readFile($, `${VROOT}/p/design.md`)
  await ups($, PID)
  expect((await req(files, PID)).reads).toEqual(['p/design.md'])
})

test('a new session id resets session settings and observed Reads', async ($, on) => {
  const { files, queue } = qharness(on)
  tools(on)
  queue.push(snap({ settings: SETTINGS_SNAP(), vault_root: VROOT }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: 'set:auto_fulltext' })
  await pane.unmount()
  await readFile($, `${VROOT}/a.md`)
  await ups($, PID2, SID2)
  const r = await req(files, PID2, SID2)
  expect([r.settings, r.persist, r.reads]).toEqual([{}, [], []])
})

test('band counts delivered supplements; a supplement-only turn keeps the coarse list', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap())
  queue.push(snap({ status: 'supp_only', prompt_id: PID2, injected: [],
    supps: [supp('s1'), supp('s2', { rendered: false, recheck: 'failed' })] }))
  queue.push(snap({ prompt_id: PID3 }))
  await ups($, PID)
  await ups($, PID2)
  let band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /^（本轮仅精排补充） 📚 粗排 1 · 精排补充 1 · alpha · \/vault-injected$/ })).toBeDefined()
  await band.unmount()
  await ups($, PID3)                                       // 补充只出现一次：下一份快照没有就不再计
  band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /^📚 粗排 1 · alpha · \/vault-injected$/ })).toBeDefined()
})

test('supplement section explains the source and annotates as llm_supplement', async ($, on) => {
  const { store, queue } = qharness(on)
  queue.push(snap({ metrics_on: true, prompt_h: PH, supps: [supp('s1'), supp('s2', { rendered: false, recheck: 'failed' })] }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect((await pane.find({ type: 'Text', text: /^精排补充 · 1 篇$/ }))!.props.bold).toBe(true)
  await pane.press({ key: rowKey('sup', '笔记/s1.md') })
  expect(await pane.find({ type: 'Text', text: /来源  LLM 精排补充（上一轮判断，只投递一次）/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /复核  通过（本轮提问词命中）/ })).toBeDefined()
  await pane.press({ key: `relevant:${pk('笔记/s1.md')}` })
  const v = store.get([...store.keys()][0]) as Record<string, unknown>
  expect(v).toMatchObject({ path: '笔记/s1.md', kind: 'llm_supplement', verdict: 'relevant', prompt_h: PH })
  await pane.press({ key: rowKey('sup', '笔记/s2.md') })   // 未投递的那篇：可排除，不可标注
  expect(await pane.find({ type: 'Text', text: /复核  未通过，未投递/ })).toBeDefined()
  expect(await pane.find({ key: `ex:${pk('笔记/s2.md')}` })).toBeDefined()
  expect(await pane.find({ key: `relevant:${pk('笔记/s2.md')}` })).toBeUndefined()
})

test('settings section toggles, clamps, cycles the model and honours config locks', async ($, on) => {
  const { files, queue } = qharness(on)
  queue.push(snap({ settings: SETTINGS_SNAP({ effective: { ...EFF, fine_pin_max: 8, fine_model: 'claude-x' },
    locked: ['fine_supplement'], source: { ...SRC, fine_model: 'config' } }) }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ type: 'Text', text: /^逐轮补充：已在 config 关闭$/ })).toBeDefined()
  expect(await pane.find({ key: 'set:fine_supplement' })).toBeUndefined()
  expect((await pane.findAll({ type: 'Button' })).filter((b) => b.props.hotkey)).toHaveLength(0)
  await pane.press({ key: 'set:coarse_inject' })                        // 开 → 关
  await pane.press({ key: 'inc:fine_pin_max' })                         // 已到 8，不再加
  await pane.press({ key: 'dec:user_prompt_submit.max_notes' })
  for (let i = 0; i < 4; i++) await pane.press({ key: 'set:fine_model' }) // claude-x → haiku → sonnet → opus → claude-x
  expect((await pane.find({ key: 'set:fine_model' }))!.props.label).toBe('精排模型：claude-x（下一轮生效）')
  await pane.unmount()
  await ups($, PID2)
  expect((await req(files, PID2)).settings).toEqual({ coarse_inject: false, 'user_prompt_submit.max_notes': 2,
    fine_pin_max: 8, fine_model: 'claude-x' })
})

test('a setting Python rejected leaves the session values', async ($, on) => {
  const { files, queue } = qharness(on)
  queue.push(snap({ settings: SETTINGS_SNAP() }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: 'set:fine_rank' })
  await pane.unmount()
  queue.push(snap({ prompt_id: PID2, settings: SETTINGS_SNAP(),
    rejected: [{ what: 'settings', key: 'fine_rank', reason: 'locked' }] }))
  await ups($, PID2)
  expect((await req(files, PID2)).settings).toEqual({ fine_rank: false })
  await ups($, PID3)
  expect((await req(files, PID3)).settings).toEqual({})
})

test('persist results and a widen rejection show a note; persist with nothing changed only toasts', async ($, on) => {
  const { files, queue, toasts } = qharness(on)
  queue.push(snap({ settings: SETTINGS_SNAP(), persisted: ['fine_max_notes'],
    rejected: [{ what: 'persist', key: 'fine_rank', reason: 'widen' }] }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ type: 'Text', text: /已设为默认：补充上限；「设为默认」只能收窄，放宽请用 \/config/ })).toBeDefined()
  await pane.press({ key: 'persist' })
  await pane.unmount()
  expect(toasts).toEqual(['vault：本会话没有改过设置'])
  await ups($, PID2)
  expect((await req(files, PID2)).persist).toEqual([])
})

test('llm head shows the failure streak and why fine-rank launches stopped', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ llm: LLM({ state: 'failed', fail: 'timeout', words: [], fail_streak: 2, disabled: 'streak',
    calls: 4, mode: 'supp' }) }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ type: 'Text', text: /^LLM 精排 · 失败：超时（连续失败 2 次） · 本会话已停用精排发起：连续失败$/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /^本会话已调用 4 次$/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /^当前阶段：逐轮补充/ })).toBeDefined()
})

// 停用对首轮点名同样生效（Python `_fine_flow.spawn_blocked` 不分模式），所以文案不能只说逐轮补充
test('llm head names the stopped launches while the first round is still the current phase', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ llm: LLM({ state: 'failed', fail: 'timeout', words: [], fail_streak: 2, disabled: 'streak',
    calls: 2, mode: 'pin' }) }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ type: 'Text', text: /本会话已停用精排发起：连续失败$/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /^当前阶段：首轮点名/ })).toBeDefined()
  await pane.unmount()
})

// llm.mode 是「当前阶段」（Python：首轮点名尚未成功为 pin，已成功且逐轮补充开启为 supp），不是最近一次调用的种类
test('llm lines name the current phase and never call the mode the latest call', async ($, on) => {
  const { queue } = qharness(on)
  const cases = [['supp', /^当前阶段：逐轮补充/], ['pin', /^当前阶段：首轮点名/]] as const
  for (let i = 0; i < cases.length; i++) {
    const [mode, phase] = cases[i]
    queue.push(snap({ prompt_id: PIDS[i], llm: LLM({ state: 'ok', calls: 1, mode }) }))
    await ups($, PIDS[i])
    const pane = await $.ui.mount(PANE)
    expect(await pane.find({ type: 'Text', text: /^本会话已调用 1 次$/ })).toBeDefined()
    expect(await pane.find({ type: 'Text', text: phase })).toBeDefined()
    expect(await pane.find({ type: 'Text', text: /最近一次/ })).toBeUndefined()
    await pane.unmount()
  }
})

// 面板上标「下一轮生效」要等快照确认：快照里该键来源是 session 且有效值等于本会话值，才算已生效
test('a changed setting reads next-turn until the snapshot confirms it, then this-session', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ settings: SETTINGS_SNAP() }))
  await ups($, PID)
  let pane = await $.ui.mount(PANE)
  await pane.press({ key: 'dec:fine_max_notes' })
  await pane.press({ key: 'set:fine_supplement' })
  expect(await pane.find({ type: 'Text', text: /^补充上限：2（下一轮生效）$/ })).toBeDefined()
  expect((await pane.find({ key: 'set:fine_supplement' }))!.props.label).toBe('逐轮补充：关（下一轮生效）')
  // 按两次回到原值：值与快照相同，但快照里它的来源还不是 session，仍算没确认
  await pane.press({ key: 'set:fine_rank' })
  await pane.press({ key: 'set:fine_rank' })
  expect((await pane.find({ key: 'set:fine_rank' }))!.props.label).toBe('LLM 精排：开（下一轮生效）')
  await pane.unmount()
  queue.push(snap({ prompt_id: PID2, settings: SETTINGS_SNAP({
    effective: { ...EFF, fine_max_notes: 2, fine_supplement: false },
    session: { fine_max_notes: 2, fine_supplement: false, fine_rank: true },
    source: { ...SRC, fine_max_notes: 'session', fine_supplement: 'session', fine_rank: 'session' } }) }))
  await ups($, PID2)
  pane = await $.ui.mount(PANE)
  expect(await pane.find({ type: 'Text', text: /^补充上限：2（本会话）$/ })).toBeDefined()
  expect((await pane.find({ key: 'set:fine_supplement' }))!.props.label).toBe('逐轮补充：关（本会话）')
  expect(await pane.find({ type: 'Text', text: /下一轮生效/ })).toBeUndefined()
})

// Python 真实形状：persisted 是面板键名字符串（Task 13 接口）
test('persisted key names from Python show in the pane footer', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ settings: SETTINGS_SNAP(), persisted: ['fine_max_notes', 'coarse_inject'] }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ type: 'Text', text: /^已设为默认：补充上限、粗排注入$/ })).toBeDefined()
})

// 前面带 settings / persist / reads 的用例摘要里都不是空值；这条钉「只有 exclude 时 settings 记 {}、persist 与 reads 记 []」
test('an exclude-only request matches the fixed digest and the band stays quiet', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap())
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await act(pane, 'inj', '笔记/alpha.md', 'ex')
  await pane.unmount()
  // 摘要为 exclude=['笔记/alpha.md']、其余各项为空那份请求的摘要（Python 侧 `request_digest` 复算同值）
  queue.push(snap({ prompt_id: PID2, request_digest: '74499db0ee2aaa076f8e8954de1e6775' }))
  await ups($, PID2)
  const band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /⚠/ })).toBeUndefined()
})

test('the Read hook hands back exactly what the engine answered', async ($, on) => {
  const { queue } = qharness(on)
  const { answers } = tools(on)
  queue.push(snap({ vault_root: VROOT }))
  await ups($, PID)
  answers.push({ result: { lines: 3 }, text: '1\tabc', isReadOnly: true })
  const out = await readFile($, `${VROOT}/a.md`)
  expect([out.text, out.result]).toEqual(['1\tabc', { lines: 3 }])
  answers.push({ result: {}, text: 'Claude requested permissions to read from it', isError: true })
  const refused = await readFile($, `${VROOT}/b.md`)
  expect([refused.text, refused.isError]).toEqual(['Claude requested permissions to read from it', true])
})

// 钉的是「观测出错时 Read 结果不变、之后的观测照常」。拿掉 hook 里的 try/catch 这条仍绿（变异 SURVIVED）：
// 宿主对抛错的 hook 本身 fail-open（跳过该 hook、沿用下层答复），所以 try/catch 在 kit 里不可观测；
// 它的判别力来自「改写结果」类变异（见 `the Read hook hands back exactly` 那条的同批变异）
test('a failing observation still returns the Read result untouched', async ($, on) => {
  const { files, queue } = qharness(on)
  tools(on)
  let armed = false
  let thrown = 0
  // 测试 hook 要在第一次用 `$` 之前注册。hook 自己抛错会被引擎跳过（fail-open），到不了插件；
  // 所以改成答一个空值，让插件里 `read($, VIEW)` 之后的取字段抛 TypeError
  on('state.get', ($, e, next) => {
    if (armed && e.plugin === 'context-vault' && e.key === 'view') {
      thrown += 1
      return { value: { value: null, version: 1 } }
    }
    return next(e)
  })
  queue.push(snap({ vault_root: VROOT }))
  await ups($, PID)
  armed = true
  const out = await readFile($, `${VROOT}/a.md`)
  armed = false
  expect(thrown).toBeGreaterThan(0)                         // 观测段确实抛过，下面的断言不是空转
  expect([out.text, out.result]).toEqual(['1\tx', {}])
  await readFile($, `${VROOT}/b.md`)
  await ups($, PID2)
  expect((await req(files, PID2)).reads).toEqual(['b.md'])  // 抛的那一条没记，之后恢复正常
})

// 以下三条不在计划原文里：变异验证发现 mod 的三处新行为没有用例钉住（见 task-19-report）
test('a count setting stops at zero', async ($, on) => {
  const { files, queue } = qharness(on)
  queue.push(snap({ settings: SETTINGS_SNAP({ effective: { ...EFF, fine_max_notes: 0 } }) }))
  await ups($, PID)
  const pane = await $.ui.mount(PANE)
  await pane.press({ key: 'dec:fine_max_notes' })
  await pane.unmount()
  await ups($, PID2)
  expect((await req(files, PID2)).settings).toEqual({ fine_max_notes: 0 })
})

test('an expanded supplement stays open when the next snapshot supplies it again', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ supps: [supp('s1')] }))
  queue.push(snap({ prompt_id: PID2, supps: [supp('s1')] }))
  await ups($, PID)
  let pane = await $.ui.mount(PANE)
  await pane.press({ key: rowKey('sup', '笔记/s1.md') })
  expect(await pane.find({ key: `ex:${pk('笔记/s1.md')}` })).toBeDefined()
  await pane.unmount()
  await ups($, PID2)
  pane = await $.ui.mount(PANE)
  expect(await pane.find({ key: `ex:${pk('笔记/s1.md')}` })).toBeDefined()
})

test('a supplement-only first turn still draws the band', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap({ status: 'supp_only', injected: [], supps: [supp('s1')] }))
  await ups($, PID)
  const band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /^（本轮仅精排补充） .*精排补充 1/ })).toBeDefined()
})

// ── spec 2026-10-13 D7：band 末尾的「面板」按钮 ────────────────────────────────────────
test('band ends with a 面板 button that opens the pane and asks for the keyboard', async ($, on) => {
  const { queue } = qharness(on)
  const opened: Record<string, unknown>[] = []
  on('ui.open', ($, e) => { opened.push(e); return { value: { isPlaced: true } } })
  queue.push(snap())
  await ups($, PID)
  const band = await $.ui.mount(BAND)
  const btn = await band.find({ key: 'open' })
  expect(btn!.props.label).toBe('面板')
  expect(await band.find({ type: 'Text', text: /^📚 粗排 1 · alpha · \/vault-injected$/ })).toBeDefined()
  await band.press({ key: 'open' })
  expect(opened).toHaveLength(1)
  expect(opened[0]).toMatchObject({ id: 'vault-injected', title: 'vault 注入', closeOnEscape: true })
  expect(Number.isInteger(opened[0].rows)).toBe(true)
  expect(opened[0].focus).toBe(true)
})

test('the command still opens the pane with the keyboard', async ($, on) => {
  const { queue } = qharness(on)
  const opened: Record<string, unknown>[] = []
  on('ui.open', ($, e) => { opened.push(e); return { value: { isPlaced: true } } })
  queue.push(snap())
  await ups($, PID)
  await $.command.run({ command: 'vault-injected', args: '' })
  expect(opened).toHaveLength(1)
  expect(opened[0].focus).toBe(true)
})

test('no 面板 button when a survey is up or the loader is disabled', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap())
  queue.push(snap({ prompt_id: PID2, status: 'disabled', injected: [] }))
  await ups($, PID)
  let band = await $.ui.mount({ ...BAND, props: { ...BAND.props, hasSurvey: true } })
  expect(await band.find({ key: 'open' })).toBeUndefined()
  await band.unmount()
  await ups($, PID2)
  band = await $.ui.mount(BAND)
  expect(await band.find({ key: 'open' })).toBeUndefined()
})

// 以下一条不在计划原文里：kit 没有布局引擎，「窄终端里按钮不被挤掉」观测不到，只能钉结构（Task 20 的设计）——
// 文字在可收缩的格子里；按钮自己没有布局 prop（d.ts：flexShrink 只在 BoxProps 上），所以包在一层 flexShrink 为 0 的 Box 里，
// 这一层排在文字格子后面，不在文字格子里面
test('the band text sits in a shrinkable cell and the button sits in a non-shrinking cell after it', async ($, on) => {
  const { queue } = qharness(on)
  queue.push(snap())
  await ups($, PID)
  const band = await $.ui.mount(BAND)
  const row = (await band.drawn()).children[0]
  expect(row.props.flexDirection).toBe('row')
  expect(row.children.map((c) => c.type)).toEqual(['Box', 'Box'])
  expect(row.children[0].props).toMatchObject({ flexGrow: 1, flexShrink: 1, minWidth: 0 })
  expect(row.children[0].children.map((c) => c.type)).toEqual(['Text'])
  expect(row.children[1].props).toMatchObject({ flexShrink: 0 })
  expect(row.children[1].children.map((c) => c.type)).toEqual(['Button'])
  expect(row.children[1].children[0].props.key).toBe('open')
})
