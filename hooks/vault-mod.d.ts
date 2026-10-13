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
        // state：'' = 本版不认识的状态（显示「未知」）；fail：'' = 没有原因或本版不认识；null = 从未收到
        llm: {
          state: string; fail: string; words: string[]; inScoring: number | null; nPinned: number | null
          ageS: number | null; pinOn: boolean; stalePins: boolean
        } | null
        thresholds: { min: number | null; ft: number | null; weight: number | null } | null
        ts: number
        metricsOn: boolean
        promptH: string
        session: string
        note: string
        queued: number
      }
      warn: { shown: boolean; active: boolean }
      ui: { expanded: string }
    }
  }
}
