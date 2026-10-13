// context-vault mod：band 上常驻显示最近一次 vault 注入；/vault-injected 打开的 pane 里本会话
// 排除、钉住与（metrics 开启时）标注。召回仍在 Python。
// 通道：每轮在 next() 之前写按轮请求文件，next() 之后读 Python 写的会话快照；事件 e 原样传递。
// 本模块不起子进程、不联网（守卫：tests/test_mod_static.py 的 calls 白名单）。
import { atom, read, update } from 'claude-code'

const PANE = 'vault-injected'
const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/
// C0、DEL、C1、零宽、bidi（含 U+061C）、不可见运算符与 U+2028/2029：快照已由 Python 净化，这里是纵深防御；字符集与 Python `_session_ops._INVISIBLE_RE` 由 parity 用例钉住
const STRIP_RE = /[\u0000-\u001f\u007f-\u009f\u061c\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u2069\ufeff]/g
const MAX_TEXT = 120
const MAX_LIST = 50
const ANN_PREFIX = 'ann:'
const ANN_TTL_SECONDS = 7 * 24 * 3600
const ANN_MAX = 200
const LIMITS = { exclude: 50, pin: 8, unpin: 8, annotations: 50 }
const DRAWN_STATUSES = new Set(['scored', 'dry_run'])
// 快照新键的取值白名单（spec 2026-10-12 §4.6），键集合与 Python 侧逐一相同（parity 用例：
// skills/vault-loader/tests/test_session_ops_snapshot.py）。集合外的值：arm、dedup、llm.state 显示「未知」，
// why 里的丢弃，llm.fail 当作没有原因。upgrade_candidate 只说明 topical 升到了全文阈值，本轮全文仍只给一篇
const ARMS = { topical: 'topical 达标', keyword_bypass: '关键词直通', upgrade_candidate: '升级候选（曾以清单注入）' }
const DEDUPS = { fulltext_injected: '已全文注入过', candidate_injected: '已注入过', user_excluded: '已排除' }
const REASONS = { project_dir: '项目目录', tag: '标签' }
const LLM_MARK = { ok: '精排✓', pending: '精排…', failed: '精排✗', unavailable: '精排✗' }

const OPS_INIT = { exclude: [], pin: [], unpin: [], pending: [] }
const VIEW_INIT = { status: '', last: null, gateNow: false, sessionStart: [], excluded: [],
  pins: { llm: [], user: [] }, llm: null, thresholds: null, ts: 0,
  metricsOn: false, promptH: '', session: '', note: '', queued: 0 }
const WARN_INIT = { shown: false, active: false }
const OPS = atom({ plugin: 'context-vault', key: 'ops' }, OPS_INIT)
const VIEW = atom({ plugin: 'context-vault', key: 'view' }, VIEW_INIT)
const WARN = atom({ plugin: 'context-vault', key: 'warn' }, WARN_INIT)
const UI_INIT = { expanded: '' }
const UI = atom({ plugin: 'context-vault', key: 'ui' }, UI_INIT)
const FOOTER = 'Enter 展开/收起 · ↑↓/Tab 移动 · Esc 关闭'
const MAX_PANE_ROWS = 40
const LLM_TEXT = { off: '未开启（relevance.session_topic）', idle: '尚未发起（等第一个有召回的提问）',
  unavailable: '无法发起', pending: '提炼中', ok: '已完成', failed: '失败' }
const FAIL_TEXT = { no_claude: '找不到 claude 命令', spawn_error: '拉起子进程失败', timeout: '超时',
  exit_nonzero: '退出码非 0', lost: '结果丢失', error: '出错', dry_run: 'dry_run 下不发起提炼' }
// 白名单从文案表派生：只往一边加值时，另一边不会悄悄显示 undefined
const LLM_STATES = new Set(Object.keys(LLM_TEXT))
const LLM_FAILS = new Set(Object.keys(FAIL_TEXT))
const LIMIT_TEXT = { exclude: '排除', pin: '手动钉住', unpin: '取消自动点名' }

const clean = (s, limit = MAX_TEXT) => (typeof s === 'string' ? s.replace(STRIP_RE, '').slice(0, limit) : '')
const uniq = (a) => [...new Set(a)]
const strings = (v, limit) => (Array.isArray(v) ? v.filter((x) => typeof x === 'string').slice(0, limit) : [])
const own = (o, k) => typeof k === 'string' && Object.prototype.hasOwnProperty.call(o, k)
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
// 要画出来的字符串列表一律逐项 clean；不是数组时返回 null（= 未知），不得改用 strings()（节点 1 T11）
const cleanList = (v, n, c) => (Array.isArray(v)
  ? v.filter((x) => typeof x === 'string').slice(0, n).map((x) => clean(x, c)).filter((x) => x)
  : null)

function items(v, withRendered) {
  if (!Array.isArray(v)) return []
  return v.filter((x) => x && typeof x.path === 'string').slice(0, MAX_LIST).map((x) => {
    const o = { path: x.path, title: clean(x.title) }
    if (withRendered) {
      o.rendered = x.rendered === true
      o.rankKnown = 'rank' in x
      o.rank = num(x.rank)
      // '' = 没有去重裁决（未进粗排）；'?' = 有值但本版不认识，显示「未知」而不是一个确定的说法
      o.dedup = x.dedup == null || x.dedup === '' ? '' : own(DEDUPS, x.dedup) ? x.dedup : '?'
    }
    return o
  })
}

function injectedItems(v) {
  if (!Array.isArray(v)) return []
  return v.filter((x) => x && typeof x.path === 'string').slice(0, MAX_LIST).map((x) => ({
    path: x.path, title: clean(x.title), confidence: clean(x.confidence, 4),
    fulltext: x.fulltext === true, kind: x.kind === 'admitted_fulltext' ? 'admitted_fulltext' : 'admitted_list',
    topical: num(x.topical), arm: own(ARMS, x.admit_arm) ? x.admit_arm : '',
    hits: cleanList(x.hits, 10, 40), topicHits: cleanList(x.topic_hits, 10, 40),
    alsoPinned: x.llm_also_pinned === true,
  }))
}

function ssItems(v) {
  if (!Array.isArray(v)) return []
  return v.filter((x) => x && typeof x.path === 'string').slice(0, MAX_LIST).map((x) => ({
    path: x.path, title: clean(x.title),
    why: Array.isArray(x.why) ? x.why.filter((r) => own(REASONS, r)) : null,
    tags: cleanList(x.tags, 5, 40),
  }))
}

// 不是对象 → null（未知）。是对象但 state 本版不认识 → state 记 ''、显示「未知」，不能当 null：
// absorb 遇 null 保留上一次，Python 新增一个状态值时 band 会一直停在旧简标上（full-review A2）
function parseLlm(l) {
  if (!l || typeof l !== 'object') return null
  return { state: LLM_STATES.has(l.state) ? l.state : '', fail: LLM_FAILS.has(l.fail) ? l.fail : '',
    words: cleanList(l.words, 8, 40) || [], inScoring: num(l.in_scoring), nPinned: num(l.n_pinned),
    ageS: num(l.age_s), pinOn: l.pin_on === true, stalePins: l.stale_pins === true }
}

function parseThresholds(t) {
  if (!t || typeof t !== 'object') return null
  return { min: num(t.min_topical), ft: num(t.ft_topical), weight: num(t.topic_hit_weight) }
}

async function homeDir($) {
  const os = await $.env.get('OS')
  const raw = os === 'Windows_NT' ? await $.env.get('USERPROFILE') : await $.env.get('HOME')
  if (typeof raw !== 'string' || raw.startsWith('\\\\')) return null
  if (!/^[A-Za-z]:[\\/]/.test(raw) && !raw.startsWith('/')) return null
  return raw.replace(/[\\/]+$/, '')
}

async function sha256Hex(text) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('')
}

async function livePaths($, sessionId, promptId) {
  if (!UUID_RE.test(sessionId || '')) return null
  const home = await homeDir($)
  if (!home) return null
  const dir = home + '/.context-vault/state/live'
  const h = (await sha256Hex(sessionId)).slice(0, 24)
  let req = null
  if (promptId === null) req = `${dir}/${h}-ss.req.json`
  else if (UUID_RE.test(promptId || '')) req = `${dir}/${h}-${promptId}.req.json`
  return req ? { req, snap: `${dir}/${h}.json` } : null
}

async function hasSurface($) {
  try {
    const s = await $.session.surfaces()
    return Array.isArray(s) && s.length > 0
  } catch {
    return false
  }
}

async function queuedAnnotations($, nowMs) {
  let keys = []
  try {
    keys = (await $.store.keys()).filter((k) => typeof k === 'string' && k.startsWith(ANN_PREFIX))
  } catch {
    return []
  }
  const out = []
  for (const k of keys) {
    const a = await $.store.get(k)
    if (!a || typeof a !== 'object' || typeof a.ts !== 'number' || nowMs / 1000 - a.ts > ANN_TTL_SECONDS) {
      await $.store.delete(k)
      continue
    }
    if (out.length < LIMITS.annotations) out.push(a)
  }
  return out
}

async function writeRequest($, e) {
  if (!(await hasSurface($))) return null
  const paths = await livePaths($, e.session_id, e.prompt_id)
  if (!paths) return null
  const ops = await read($, OPS)
  const anns = await queuedAnnotations($, Number(await $.clock.now()))
  const req = {
    v: 1,
    exclude: ops.exclude.slice(0, LIMITS.exclude),
    pin: ops.pin.slice(0, LIMITS.pin),
    unpin_llm: ops.unpin.slice(0, LIMITS.unpin),
    annotations: anns,
  }
  await $.fs.write(paths.req, JSON.stringify(req))
  const digest = (await sha256Hex(JSON.stringify(
    [1, req.exclude, req.pin, req.unpin_llm, anns.map((a) => a.id)]))).slice(0, 32)
  const hasOps = req.exclude.length + req.pin.length + req.unpin_llm.length + anns.length > 0
  return { paths, digest, hasOps }
}

async function flagWarn($) {
  await update($, WARN, (w) => (w.shown ? w : { shown: true, active: true }))
}

// 「本会话」的操作与告警额度按会话 id 归属，不依赖 $.state 是否随 /clear、/resume 重置
// （类型文档与 spec 记载不一致，未实测）：会话 id 变了就从空开始
async function enterSession($, sid) {
  if (!UUID_RE.test(sid || '')) return
  const v = await read($, VIEW)
  if (v.session === sid) return
  await update($, OPS, () => ({ ...OPS_INIT }))
  await update($, WARN, () => ({ ...WARN_INIT }))
  await update($, UI, () => ({ ...UI_INIT }))
  await update($, VIEW, () => ({ ...VIEW_INIT, session: sid }))
}

async function dequeue($, snap) {
  const ids = new Set([
    ...strings(snap.acked, LIMITS.annotations),
    ...(Array.isArray(snap.rejected) ? snap.rejected : [])
      .filter((r) => r && r.what === 'annotation' && typeof r.key === 'string').map((r) => r.key),
  ])
  for (const id of ids) await $.store.delete(ANN_PREFIX + id)
  const note = (Array.isArray(snap.rejected) ? snap.rejected : [])
    .some((r) => r && r.what === 'annotation' && r.reason === 'metrics_off') ? '标注未写入：metrics 未开启' : ''
  return { removed: ids.size, note }
}

async function absorb($, e, snap) {
  const { removed, note } = await dequeue($, snap)
  const dropped = new Set((Array.isArray(snap.rejected) ? snap.rejected : [])
    .filter((r) => r && r.reason === 'not_in_index' && typeof r.key === 'string').map((r) => r.key))
  await update($, OPS, (o) => ({
    exclude: o.exclude.filter((p) => !dropped.has(p)),
    pin: o.pin.filter((p) => !dropped.has(p)),
    unpin: o.unpin.filter((p) => !dropped.has(p)),
    pending: [],
  }))
  await update($, VIEW, (v) => {
    const status = typeof snap.status === 'string' ? snap.status : ''
    const scored = DRAWN_STATUSES.has(status)
    const pins = snap.pins && typeof snap.pins === 'object' ? snap.pins : {}
    return {
      ...v,
      status,
      last: scored ? { injected: injectedItems(snap.injected) } : v.last,
      gateNow: status.startsWith('gate:'),
      excluded: scored ? items(snap.excluded, false) : v.excluded,
      pins: scored ? { llm: items(pins.llm, true), user: items(pins.user, true) } : v.pins,
      thresholds: scored ? parseThresholds(snap.thresholds) : v.thresholds,
      // llm 与 ts 对任何有效 UPS 快照都吸收（含闸门轮，spec §4.3）；null 或缺键 = 本轮没算出来，保留上一次
      llm: snap.llm == null ? v.llm : parseLlm(snap.llm),
      ts: num(snap.ts) || v.ts,
      // 标注目标是最近一次打分轮（promptH 同理）：闸门轮不改变它，也不收掉按钮
      metricsOn: scored ? snap.metrics_on === true : v.metricsOn,
      promptH: scored && typeof snap.prompt_h === 'string' ? snap.prompt_h : v.promptH,
      session: e.session_id,
      note: note || v.note,
      queued: Math.max(0, v.queued - removed),
    }
  })
  await update($, WARN, (w) => ({ ...w, active: false }))
  await collapseGone($)
}

async function readSnapshot($, e, sent) {
  if (!sent) return
  let snap = null
  try {
    snap = JSON.parse(await $.fs.read(sent.paths.snap))
  } catch {
    snap = null
  }
  const valid = snap && typeof snap === 'object' && snap.v === 1
    && snap.event === 'UserPromptSubmit' && snap.prompt_id === e.prompt_id
  if (!valid) {
    if (sent.hasOps) await flagWarn($)
    return
  }
  if (snap.status === 'disabled') {
    await update($, VIEW, (v) => ({ ...v, status: 'disabled' }))
    return
  }
  // 先吸收再告警：absorb 末尾清掉告警，反过来会让摘要不一致的告警当场消失并耗掉每会话一次的额度
  const mismatch = sent.hasOps && snap.request_digest !== sent.digest
  await absorb($, e, snap)
  if (mismatch) await flagWarn($)
}

// 来源计数与精排简标放在标题之前：这一行是 truncate-end，标题多时截掉的是末尾（spec §4.5）。
// 不渲染任何 LLM 主题词或命中词（只在面板展开区出现）。
function bandLine(view, warn) {
  if (warn.active) return '⚠ vault：操作未送达（快照路径不一致或请求未被读取） · /vault-injected'
  const head = []
  if (view.status === 'dry_run') head.push('[DRY-RUN]')
  if (view.gateNow) head.push('（本轮未召回）')
  const parts = []
  if (view.last) {
    const inj = view.last.injected
    parts.push(inj.length ? `📚 粗排 ${inj.length}` : '📚 无注入')
  } else {
    parts.push(`📚 会话开始注入 ${view.sessionStart.length} 篇`)
  }
  if (view.pins.llm.length) parts.push(`精排📌${view.pins.llm.length}`)
  if (view.pins.user.length) parts.push(`手动📌${view.pins.user.length}`)
  if (view.excluded.length) parts.push(`排除 ${view.excluded.length}`)
  if (view.llm && own(LLM_MARK, view.llm.state)) parts.push(LLM_MARK[view.llm.state])
  if (view.last) parts.push(...view.last.injected.map((x) => x.title))
  return [...head, parts.join(' · ')].join(' ') + ' · /vault-injected'
}

// path → 8 位十六进制（FNV-1a）。Button 的 key 由 path 派生，不用序号、不由文案派生：新快照换了
// 顺序时，按序号的 key 会把操作打到另一篇上（节点 1 T3）
function pathKey(p) {
  let h = 0x811c9dc5
  for (const ch of p) {
    h ^= ch.codePointAt(0)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h.toString(16).padStart(8, '0')
}

function isWide(cp) {
  return (cp >= 0x1100 && cp <= 0x115f) || (cp >= 0x2e80 && cp <= 0xa4cf) || (cp >= 0xac00 && cp <= 0xd7a3)
    || (cp >= 0xf900 && cp <= 0xfaff) || (cp >= 0xfe30 && cp <= 0xfe4f) || (cp >= 0xff00 && cp <= 0xff60)
    || (cp >= 0xffe0 && cp <= 0xffe6) || (cp >= 0x1f300 && cp <= 0x1faff) || (cp >= 0x20000 && cp <= 0x3fffd)
}

function cellWidth(s) {
  let n = 0
  for (const ch of s) n += isWide(ch.codePointAt(0)) ? 2 : 1
  return n
}

// 按显示列宽截断（CJK 与 emoji 记 2 列）；按码点迭代，不会切开代理对
function fitText(s, cols) {
  if (cols <= 1) return '…'
  if (cellWidth(s) <= cols) return s
  let out = ''
  let n = 0
  for (const ch of s) {
    const w = isWide(ch.codePointAt(0)) ? 2 : 1
    if (n + w > cols - 1) break
    out += ch
    n += w
  }
  return out + '…'
}

function injMeta(it) {
  return `${it.confidence}置信 · ${it.fulltext ? '全文' : '清单'}${it.topicHits && it.topicHits.length ? ' · +主题词' : ''}`
}

// 条目字段按「是不是数组 / 数字」判断，不按 === null：热重载后读到的可能是旧版 mod 写进 $.state 的
// 条目，新字段是 undefined（full-review BP-M1：`undefined.join` 曾让整个面板画不出来）
function injDetails(it, th) {
  const arm = own(ARMS, it.arm) ? ARMS[it.arm] : '未知'
  const score = typeof it.topical === 'number' ? `topical ${it.topical}` : 'topical 未知'
  const gate = th && th.min !== null && th.ft !== null ? `（注入 ≥${th.min}，全文 ≥${th.ft} 且强证据）` : ''
  const lines = [`来源  脚本粗排 · ${arm} · ${score}${gate}${it.fulltext ? ' · 已升级为全文' : ''}`]
  let hit = `命中  提问词：${Array.isArray(it.hits) ? it.hits.join(', ') || '（无）' : '未知'}`
  if (Array.isArray(it.topicHits) && it.topicHits.length) {
    hit += ` ｜ LLM 主题词：${it.topicHits.join(', ')}（+${th && th.weight !== null ? th.weight : '?'}）`
  }
  lines.push(hit)
  if (it.alsoPinned) lines.push('LLM 也点名了这篇（不重复注入）')
  return lines
}

function pinMeta(it, l) {
  let where = ''
  if (it.rankKnown) {
    where = typeof it.rank === 'number' ? ` · 粗排第 ${it.rank}`
      : ` · ${!it.dedup ? '未进粗排' : own(DEDUPS, it.dedup) ? DEDUPS[it.dedup] : '未知'}`
  }
  return `LLM 点名${where}${l && l.stalePins ? ' · 沿用上一窗口' : ''}`
}

function ssMeta(it) {
  if (!it.why) return ''
  return it.why.map((r) => (r === 'tag' && it.tags && it.tags.length ? `标签 ${it.tags.join(', ')}` : REASONS[r])).join(' / ')
}

function llmHead(l) {
  if (!l) return 'LLM 精排 · 未知（快照来自旧版本或读取失败）'
  if (!own(LLM_TEXT, l.state)) return 'LLM 精排 · 未知（本版不认识的状态）'
  if (l.state === 'idle' && l.fail === 'dry_run') return `LLM 精排 · ${FAIL_TEXT.dry_run}`
  let s = `LLM 精排 · ${LLM_TEXT[l.state]}`
  if (l.fail && own(FAIL_TEXT, l.fail)) s += `：${FAIL_TEXT[l.fail]}`
  if (l.state === 'pending' && l.ageS !== null) s += `（已 ${l.ageS} 秒，截至上次提问）`
  if (l.state === 'failed') s += '；本窗口内不再重试（user_prompt_submit.state_ttl_hours）'
  if (l.state === 'ok' && l.ageS !== null) s += ` · ${l.ageS < 60 ? '刚刚' : `${Math.floor(l.ageS / 60)} 分钟前`}完成`
  return s
}

function llmLines(l, th) {
  if (!l || !['pending', 'ok', 'failed'].includes(l.state)) return []
  const out = []
  if (l.words.length) {
    out.push(`主题词 ${l.words.length} 个（本轮打分已用 ${l.inScoring === null ? '?' : l.inScoring}）：${l.words.join(', ')}`)
  }
  if (l.state === 'ok') out.push(`点名 ${l.nPinned === null ? '?' : l.nPinned} 篇${l.pinOn ? '' : '（点名已关闭）'}`)
  out.push(`作用：主题词给命中的笔记加 ${th && th.weight !== null ? th.weight : '?'} 分（下一轮起生效）；点名的篇以摘要固定注入`)
  if (l.stalePins) out.push('注入中的点名沿用上一窗口')
  return out
}

// 打开面板时按内容请求高度：不传时宿主给三分之一，内容溢出后 ↑↓ 变成滚动（节点 1 T18）
function paneRows(view, ops) {
  const n = (view.last ? view.last.injected.length : 0) + view.pins.llm.length + ops.pin.length
    + uniq([...ops.exclude, ...ops.unpin]).length + view.sessionStart.length
  // 表头 1 + 五节标题与节间空行 10 + 精排说明 4 + 页脚 4 + 展开一篇的余量 4
  return Math.min(MAX_PANE_ROWS, n + 23)
}

// 新快照里展开的那篇已不在时收起（spec §4.3）
async function collapseGone($) {
  const u = await read($, UI)
  if (!u.expanded) return
  const v = await read($, VIEW)
  const o = await read($, OPS)
  const ids = new Set([
    ...(v.last ? v.last.injected.map((x) => `inj:${x.path}`) : []),
    ...v.pins.llm.map((x) => `llm:${x.path}`),
    ...o.pin.map((p) => `user:${p}`),
    ...uniq([...o.exclude, ...o.unpin]).map((p) => `exc:${p}`),
    ...v.sessionStart.map((x) => `ss:${x.path}`),
  ])
  if (!ids.has(u.expanded)) await update($, UI, () => ({ ...UI_INIT }))
}

const addTo = (key, path, also = []) => (o) => {
  const n = { ...o, pending: uniq([...o.pending, path]) }
  n[key] = uniq([...o[key], path])
  for (const k of also) n[k] = o[k].filter((x) => x !== path)
  return n
}
// 达到上限时拒绝并提示：请求按上限截断，超出的那条不会被发出，pane 却会显示为已生效
async function addOp($, key, path, also) {
  const o = await read($, OPS)
  if (!o[key].includes(path) && o[key].length >= LIMITS[key]) {
    await $.ui.toast(`vault：本会话最多${LIMIT_TEXT[key]} ${LIMITS[key]} 篇`)
    return
  }
  await update($, OPS, addTo(key, path, also))
}
const removeFrom = (keys, path) => (o) => {
  const n = { ...o, pending: uniq([...o.pending, path]) }
  for (const k of keys) n[k] = o[k].filter((x) => x !== path)
  return n
}

async function randomId() {
  const b = new Uint8Array(16)
  crypto.getRandomValues(b)
  return 'ann-' + [...b].map((x) => x.toString(16).padStart(2, '0')).join('')
}

export function register(on, options) {
  if (options && options.mod_enabled === false) return

  on('session.start', async ($, e, next) => {
    try {
      await $.command.register({ name: 'vault-injected', immediate: true,
        description: '查看并调整本会话 vault 注入（排除 / 钉住 / 标注）' })
    } catch {
      // 命令名被占用时会抛错；band 照常工作
    }
    return next(e)
  })

  on('command.run', { command: 'vault-injected' }, async ($) => {
    const rows = paneRows(await read($, VIEW), await read($, OPS))
    await $.ui.open({ id: PANE, title: 'vault 注入', focus: true, closeOnEscape: true, rows })
    return {}
  })

  on('classic.SessionStart', async ($, e, next) => {
    const fresh = ['startup', 'clear', 'compact'].includes(e.source)
    let paths = null
    try {
      await enterSession($, e.session_id)
    } catch {
      // 只影响「本会话」操作的归属
    }
    if (fresh) {
      try {
        if (await hasSurface($)) {
          paths = await livePaths($, e.session_id, null)
          if (paths) await $.fs.write(paths.req, '{}')
        }
      } catch {
        paths = null
      }
    }
    const r = await next(e)
    try {
      if (!fresh) {
        // /resume、/branch 不运行 Python 的 SessionStart（matcher 只有 startup|clear|compact）：清空展示，不读旧快照
        await update($, VIEW, (v) => ({ ...v, last: null, gateNow: false, sessionStart: [] }))
      } else if (paths) {
        const snap = JSON.parse(await $.fs.read(paths.snap))
        if (snap && snap.v === 1 && snap.event === 'SessionStart') {
          await update($, VIEW, (v) => ({ ...v, sessionStart: ssItems(snap.session_start), ts: num(snap.ts) || v.ts,
            status: typeof snap.status === 'string' ? snap.status : v.status, session: e.session_id }))
          await collapseGone($)
        }
      }
    } catch {
      // 读不到会话开始快照只影响首轮前的 band
    }
    return r
  })

  on('classic.UserPromptSubmit', async ($, e, next) => {
    let sent = null
    try {
      await enterSession($, e.session_id)
      sent = await writeRequest($, e)
    } catch {
      sent = null
    }
    const r = await next(e)
    try {
      await readSnapshot($, e, sent)
    } catch {
      // 快照只服务展示
    }
    return r
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    const view = await read($, VIEW)
    const warn = await read($, WARN)
    const theirs = await next(e)
    const nothing = !view.last && !view.sessionStart.length && !warn.active
    if (view.status === 'disabled' || nothing) return theirs
    const { Box, Text } = $.ui.resolve(e)
    // 与上方会话内容隔一行（D4）；宿主给的高度不足 3 行时不加，免得把内容挤进滚动
    const marginTop = typeof e.props.maxRows === 'number' && e.props.maxRows >= 3 ? 1 : 0
    return Box({ flexDirection: 'column', marginTop, children: [
      Text({ wrap: 'truncate-end', children: [bandLine(view, warn)] }),
      theirs,
    ] })
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const view = await read($, VIEW)
    const ops = await read($, OPS)
    const ui = await read($, UI)
    const nowS = Math.floor(Number(await $.clock.now()) / 1000)
    const bodyCols = typeof e.props.bodyColumns === 'number' ? e.props.bodyColumns : 80
    const cols = Math.max(20, bodyCols - 2)              // 根 Box 左右各 1 格内边距（D4）
    const pend = (p) => (ops.pending.includes(p) ? '（下一轮生效）' : '')
    const dim = (t) => Text({ dimColor: true, wrap: 'truncate-end', children: [t] })
    let firstRow = true

    const toggle = (id, key) => async () => {
      await update($, UI, (u) => ({ expanded: u.expanded === id ? '' : id }))
      try {
        await $.ui.focus({ requestId: PANE, key })        // 重绘后把焦点放回这一行（节点 1 T3）
      } catch {
        // 焦点落位只影响体验（claude plugin test 2.1.296 不实现插件发起的 ui.focus，在此抛 HooksError）
      }
    }
    // 一篇 = 一行一个焦点停靠点；展开区放来源详情与该篇的操作（D1）
    const entry = (sec, path, title, meta, details, actions) => {
      const id = `${sec}:${path}`
      const k = pathKey(path)
      const key = `row:${sec}:${k}`
      const open = ui.expanded === id
      const metaW = meta ? cellWidth(meta) + 2 : 0
      const showMeta = metaW > 0 && cols - metaW >= 16
      const btn = { key, plain: true, onPress: toggle(id, key),
        label: (open ? '▾ ' : '▸ ') + fitText(title, cols - 2 - (showMeta ? metaW : 0)) }
      if (firstRow) {
        btn.autoFocus = true
        firstRow = false
      }
      const out = [Box({ flexDirection: 'row', children: [
        Button(btn), Box({ flexGrow: 1 }), ...(showMeta ? [dim(meta)] : []),
      ] })]
      if (open) {
        out.push(Box({ flexDirection: 'column', paddingLeft: 4, children: [
          ...details.map(dim),
          Box({ flexDirection: 'row', columnGap: 2, children: actions.map(([name, hotkey, label, fn]) =>
            Button({ key: `${name}:${k}`, plain: true, hotkey, label, onPress: fn })) }),
        ] }))
      }
      return out
    }
    const sections = []
    const section = (title, rows, lead = [], always = false) => {
      if (!rows.length && !lead.length && !always) return
      const body = rows.length || lead.length ? [...lead, ...rows.flat()] : [dim('（无）')]
      sections.push(Box({ flexDirection: 'column', marginTop: 1, children: [
        Text({ bold: true, children: [title] }), ...body,
      ] }))
    }

    const canAnnotate = view.metricsOn && view.status !== 'dry_run' && /^[0-9a-f]{16}$/.test(view.promptH)
    const annotate = (it, verdict) => async () => {
      let n = 0
      try { n = (await $.store.keys()).filter((k) => k.startsWith(ANN_PREFIX)).length } catch {}
      if (n >= ANN_MAX) { await $.ui.toast('vault：待写入标注已达 200 条，请先提交一轮提问'); return }
      const id = await randomId()
      const ts = Math.floor(Number(await $.clock.now()) / 1000)
      await $.store.set(ANN_PREFIX + id, { id, path: it.path, kind: it.kind, verdict,
        session: view.session, prompt_h: view.promptH, ts })
      await update($, VIEW, (v) => ({ ...v, queued: v.queued + 1 }))
    }

    const inj = view.last ? view.last.injected : []
    section(`脚本粗排 · ${inj.length} 篇`, inj.map((it) => {
      const acts = [['ex', 'x', '排除', () => addOp($, 'exclude', it.path, ['pin'])],
        ['pin', 'p', '钉住', () => addOp($, 'pin', it.path, ['exclude', 'unpin'])]]
      if (canAnnotate) {
        acts.push(['relevant', 'y', '有用', annotate(it, 'relevant')],
          ['irrelevant', 'n', '无用', annotate(it, 'irrelevant')])
      }
      return entry('inj', it.path, it.title, injMeta(it) + pend(it.path), injDetails(it, view.thresholds), acts)
    }), [], true)

    const llmTitle = view.llm || view.last ? llmHead(view.llm) : 'LLM 精排 · 等第一次提问'
    section(llmTitle, view.pins.llm.map((it) => entry('llm', it.path, it.title, pinMeta(it, view.llm) + pend(it.path),
      ['来源  LLM 从候选池点名（会话首轮提炼）',
        it.rendered ? '本轮以摘要固定注入' : '本轮未单独注入（已在粗排里，或已不在索引）'],
      [['unpin', 'u', '取消点名', () => addOp($, 'unpin', it.path, ['pin'])]])),
    llmLines(view.llm, view.thresholds).map(dim), true)

    section(`手动钉住 · ${ops.pin.length} 篇`, ops.pin.map((p) => {
      const got = view.pins.user.find((x) => x.path === p)
      return entry('user', p, clean(got ? got.title : p.split('/').pop()),
        (got && !got.rendered ? '本轮未单独注入' : '') + pend(p),
        ['来源  你在面板里钉住：之后每次打分的提问都以摘要注入'],
        [['unpinuser', 'u', '取消钉住', () => update($, OPS, removeFrom(['pin'], p))]])
    }))

    const exc = uniq([...ops.exclude, ...ops.unpin])
    section(`本会话已排除 · ${exc.length} 篇`, exc.map((p) => {
      const got = view.excluded.find((x) => x.path === p)
      return entry('exc', p, clean(got ? got.title : p.split('/').pop()), pend(p),
        ['已注入过的不会撤回；之后不再升级为全文、不再被自动点名或注入'],
        [['restore', 'r', '恢复', () => update($, OPS, removeFrom(['exclude', 'unpin'], p))]])
    }))

    section(`会话开始注入 · ${view.sessionStart.length} 篇`, view.sessionStart.map((it) => entry('ss', it.path, it.title,
      ssMeta(it), [`来源  会话开始的项目固定上下文，不打分：${ssMeta(it) || '未知'}`],
      [['sspin', 'p', '钉住', () => addOp($, 'pin', it.path, ['exclude', 'unpin'])]])))

    const ago = view.ts ? Math.max(0, nowS - view.ts) : null
    const when = ago === null ? '' : ago < 60 ? '刚刚' : `${Math.floor(ago / 60)} 分钟前`
    const close = { key: 'close', label: '关闭', onPress: () => $.ui.close({ id: PANE }) }
    if (firstRow) close.autoFocus = true
    const header = Box({ flexDirection: 'row', children: [
      Text({ bold: true, children: ['vault 注入'] }),
      ...(when ? [dim(` · 截至上次提问（${when}）`)] : []),
      Box({ flexGrow: 1 }), Button(close),
    ] })
    const foot = [dim(FOOTER)]
    if (!view.metricsOn) foot.push(dim('开启 metrics 后可标注'))
    if (view.queued) foot.push(dim(`${view.queued} 条标注待写入（随下一次提问）`))
    if (view.note) foot.push(dim(view.note))
    return Box({ flexDirection: 'column', paddingX: 1, children: [
      header, ...sections, Box({ flexDirection: 'column', marginTop: 1, children: foot }),
    ] })
  })
}
