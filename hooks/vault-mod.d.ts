declare module 'claude-code' {
  interface PluginState {
    'context-vault': {
      ops: { exclude: string[]; pin: string[]; unpin: string[]; pending: string[] }
      view: {
        status: string
        last: {
          injected: {
            path: string; title: string; confidence: string; fulltext: boolean; kind: string
            topical: number | null; arm: string; hits: string[] | null; topicHits: string[] | null
            alsoPinned: boolean
          }[]
        } | null
        gateNow: boolean
        sessionStart: { path: string; title: string; why: string[] | null; tags: string[] | null }[]
        excluded: { path: string; title: string }[]
        // dedup：'' = 没有去重裁决，'?' = 有值但本版不认识；rankKnown = 快照条目带 rank 键
        pins: {
          llm: { path: string; title: string; rendered: boolean; rankKnown: boolean; rank: number | null; dedup: string }[]
          user: { path: string; title: string; rendered: boolean; rankKnown: boolean; rank: number | null; dedup: string }[]
        }
        // state：'' = 本版不认识的状态（显示「未知」）；fail / mode / disabled：'' = 没有或本版不认识；null = 从未收到
        llm: {
          state: string; fail: string; words: string[]; inScoring: number | null; nPinned: number | null
          ageS: number | null; pinOn: boolean; stalePins: boolean
          mode: string; failStreak: number | null; calls: number | null; disabled: string
        } | null
        thresholds: { min: number | null; ft: number | null; weight: number | null } | null
        ts: number
        metricsOn: boolean
        promptH: string
        session: string
        note: string
        queued: number
        // 2.4.0：本轮投递 / 未投递的精排补充（recheck：'' = 本版不认识）、面板设置快照、vault 根路径、config 的模型
        supps: { path: string; title: string; srcSeq: number | null; recheck: string; rendered: boolean }[]
        settings: {
          effective: Record<string, boolean | number | string>
          source: Record<string, string>
          locked: string[]
        } | null
        vaultRoot: string
        cfgModel: string
      }
      warn: { shown: boolean; active: boolean }
      ui: { expanded: string }
      // 本会话改过的面板设置与「设为默认」队列（队列里的键等快照回执才出队，没有回执每轮重发）
      settings: { values: Record<string, boolean | number | string>; persist: string[] }
      // 主模型读过、尚未被摘要对得上的 scored 快照确认的 vault 笔记（vault 内相对路径，最多 50 条，超限丢最旧的）
      reads: { items: string[] }
    }
  }
}
