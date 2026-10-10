declare module 'claude-code' {
  interface PluginState {
    'context-vault': {
      ops: { exclude: string[]; pin: string[]; unpin: string[]; pending: string[] }
      view: {
        status: string
        last: {
          injected: { path: string; title: string; confidence: string; fulltext: boolean; kind: string }[]
        } | null
        gateNow: boolean
        sessionStart: { path: string; title: string }[]
        excluded: { path: string; title: string }[]
        pins: {
          llm: { path: string; title: string; rendered: boolean }[]
          user: { path: string; title: string; rendered: boolean }[]
        }
        metricsOn: boolean
        promptH: string
        session: string
        note: string
        queued: number
      }
      warn: { shown: boolean; active: boolean }
    }
  }
}
