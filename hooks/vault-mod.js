// context-vault mod：band 上常驻显示最近一次 vault 注入；/vault-injected 打开的 pane 里本会话
// 排除、钉住与（metrics 开启时）标注。召回仍在 Python。
// 通道：每轮在 next() 之前写按轮请求文件，next() 之后读 Python 写的会话快照；事件 e 原样传递。
// 本模块不起子进程、不联网（守卫：tests/test_mod_static.py 的 calls 白名单）。
import { atom, read, update } from 'claude-code'

const PANE = 'vault-injected'
const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/
// C0、DEL、C1、零宽与 bidi 控制字符：快照已由 Python 净化，这里是纵深防御
const STRIP_RE = /[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩﻿]/g
const MAX_TEXT = 120
const MAX_LIST = 50
const ANN_PREFIX = 'ann:'
const ANN_TTL_SECONDS = 7 * 24 * 3600
const ANN_MAX = 200
const LIMITS = { exclude: 50, pin: 8, unpin: 8, annotations: 50 }
const DRAWN_STATUSES = new Set(['scored', 'dry_run'])

const OPS_INIT = { exclude: [], pin: [], unpin: [], pending: [] }
const VIEW_INIT = { status: '', last: null, gateNow: false, sessionStart: [], excluded: [],
  pins: { llm: [], user: [] }, metricsOn: false, promptH: '', session: '', note: '', queued: 0 }
const WARN_INIT = { shown: false, active: false }
const OPS = atom({ plugin: 'context-vault', key: 'ops' }, OPS_INIT)
const VIEW = atom({ plugin: 'context-vault', key: 'view' }, VIEW_INIT)
const WARN = atom({ plugin: 'context-vault', key: 'warn' }, WARN_INIT)
const LIMIT_TEXT = { exclude: '排除', pin: '手动钉住', unpin: '取消自动点名' }

const clean = (s, limit = MAX_TEXT) => (typeof s === 'string' ? s.replace(STRIP_RE, '').slice(0, limit) : '')
const uniq = (a) => [...new Set(a)]
const strings = (v, limit) => (Array.isArray(v) ? v.filter((x) => typeof x === 'string').slice(0, limit) : [])

function items(v, withRendered) {
  if (!Array.isArray(v)) return []
  return v.filter((x) => x && typeof x.path === 'string').slice(0, MAX_LIST).map((x) => {
    const o = { path: x.path, title: clean(x.title) }
    if (withRendered) o.rendered = x.rendered === true
    return o
  })
}

function injectedItems(v) {
  if (!Array.isArray(v)) return []
  return v.filter((x) => x && typeof x.path === 'string').slice(0, MAX_LIST).map((x) => ({
    path: x.path, title: clean(x.title), confidence: clean(x.confidence, 4),
    fulltext: x.fulltext === true, kind: x.kind === 'admitted_fulltext' ? 'admitted_fulltext' : 'admitted_list',
  }))
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
      // 标注目标是最近一次打分轮（promptH 同理）：闸门轮不改变它，也不收掉按钮
      metricsOn: scored ? snap.metrics_on === true : v.metricsOn,
      promptH: scored && typeof snap.prompt_h === 'string' ? snap.prompt_h : v.promptH,
      session: e.session_id,
      note: note || v.note,
      queued: Math.max(0, v.queued - removed),
    }
  })
  await update($, WARN, (w) => ({ ...w, active: false }))
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

function bandLine(view, warn) {
  if (warn.active) return '⚠ vault：操作未送达（快照路径不一致或请求未被读取） · /vault-injected'
  const parts = []
  if (view.last) {
    const inj = view.last.injected
    parts.push(inj.length ? `📚 ${inj.length} 篇：${inj.map((x) => x.title).join(' · ')}` : '📚 无注入')
  } else {
    parts.push(`📚 会话开始注入 ${view.sessionStart.length} 篇`)
  }
  const nPin = view.pins.llm.length + view.pins.user.length
  if (nPin) parts.push(`📌${nPin}`)
  if (view.excluded.length) parts.push(`已排除${view.excluded.length}`)
  let line = (view.status === 'dry_run' ? '[DRY-RUN] ' : '') + parts.join(' · ')
  if (view.gateNow) line += '（本轮未召回）'
  return line + ' · /vault-injected'
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
    await $.ui.open({ id: PANE, title: 'vault 注入', focus: true, closeOnEscape: true })
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
          await update($, VIEW, (v) => ({ ...v, sessionStart: items(snap.session_start, false),
            status: typeof snap.status === 'string' ? snap.status : v.status, session: e.session_id }))
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
    return Box({ flexDirection: 'column', children: [
      Text({ wrap: 'truncate-end', children: [bandLine(view, warn)] }),
      theirs,
    ] })
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const view = await read($, VIEW)
    const ops = await read($, OPS)
    const pend = (p) => (ops.pending.includes(p) ? '（下一轮生效）' : '')
    const rows = []
    const row = (children) => rows.push(Box({ flexDirection: 'row', columnGap: 1, children }))
    const heading = (t) => rows.push(Text({ bold: true, children: [t] }))

    rows.push(Box({ flexDirection: 'row', children: [
      Button({ key: 'close', label: '关闭', autoFocus: true, onPress: () => $.ui.close({ id: PANE }) }),
    ] }))

    heading('最近一次注入')
    const inj = view.last ? view.last.injected : []
    if (!inj.length) rows.push(Text({ dimColor: true, children: ['（无）'] }))
    const canAnnotate = view.metricsOn && view.status !== 'dry_run' && /^[0-9a-f]{16}$/.test(view.promptH)
    inj.forEach((it, i) => {
      const ctrl = [
        Text({ children: [`${it.title}（${it.confidence}置信 · ${it.fulltext ? '全文' : '清单'}）${pend(it.path)}`] }),
        Button({ key: `ex:${i}`, label: '排除', onPress: () => addOp($, 'exclude', it.path, ['pin']) }),
        Button({ key: `pin:${i}`, label: '钉住', onPress: () => addOp($, 'pin', it.path, ['exclude', 'unpin']) }),
      ]
      if (canAnnotate) {
        for (const [verdict, label] of [['relevant', '有用'], ['irrelevant', '无用']]) {
          ctrl.push(Button({ key: `${verdict}:${i}`, label, onPress: async () => {
            let n = 0
            try { n = (await $.store.keys()).filter((k) => k.startsWith(ANN_PREFIX)).length } catch {}
            if (n >= ANN_MAX) { await $.ui.toast('vault：待写入标注已达 200 条，请先提交一轮提问'); return }
            const id = await randomId()
            const ts = Math.floor(Number(await $.clock.now()) / 1000)
            await $.store.set(ANN_PREFIX + id, { id, path: it.path, kind: it.kind, verdict,
              session: view.session, prompt_h: view.promptH, ts })
            await update($, VIEW, (v) => ({ ...v, queued: v.queued + 1 }))
          } }))
        }
      }
      row(ctrl)
    })

    heading('📌 自动点名')
    view.pins.llm.forEach((it, i) => row([
      Text({ children: [`${it.title}${pend(it.path)}`] }),
      Button({ key: `unpin:${i}`, label: '取消', onPress: () => addOp($, 'unpin', it.path, ['pin']) }),
    ]))
    heading('📌 手动钉住')
    ops.pin.forEach((p, i) => {
      const shown = view.pins.user.find((x) => x.path === p)
      const tail = shown && !shown.rendered ? '（本轮未渲染）' : ''
      row([
        Text({ children: [`${clean(shown ? shown.title : p.split('/').pop())}${tail}${pend(p)}`] }),
        Button({ key: `unpinuser:${i}`, label: '取消', onPress: () => update($, OPS, removeFrom(['pin'], p)) }),
      ])
    })
    heading('本会话已排除（已注入过的不会撤回；之后不再升级为全文、不再被自动点名或注入）')
    uniq([...ops.exclude, ...ops.unpin]).forEach((p, i) => row([
      Text({ children: [`${clean(p.split('/').pop())}${pend(p)}`] }),
      Button({ key: `restore:${i}`, label: '恢复', onPress: () => update($, OPS, removeFrom(['exclude', 'unpin'], p)) }),
    ]))
    heading('会话开始注入')
    view.sessionStart.forEach((it, i) => row([
      Text({ children: [it.title] }),
      Button({ key: `sspin:${i}`, label: '钉住', onPress: () => addOp($, 'pin', it.path, ['exclude', 'unpin']) }),
    ]))
    if (!view.metricsOn) rows.push(Text({ dimColor: true, children: ['开启 metrics 后可标注'] }))
    if (view.queued) rows.push(Text({ dimColor: true, children: [`${view.queued} 条标注待写入（随下一次提问）`] }))
    if (view.note) rows.push(Text({ dimColor: true, children: [view.note] }))
    return Box({ flexDirection: 'column', children: rows })
  })
}
