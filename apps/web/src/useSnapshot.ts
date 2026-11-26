import { useCallback, useEffect, useState } from 'react'
import { fetchSnapshot, type EventFilter, type Snapshot } from './api'

export const POLL_MS = 2000
const REQUEST_TIMEOUT_MS = 8000
export const HISTORY_POINTS = 60 // two minutes at one poll every 2 s

export interface Point {
  at: number // server time, ms
  throughput: number
  errorRate: number
  p95: number | null
  lag: number
}

function toPoint({ summary: s }: Snapshot): Point {
  return {
    at: Date.parse(s.at),
    throughput: s.throughput_per_second,
    errorRate: s.error_rate,
    p95: s.p95_ms,
    lag: s.queue.lag_seconds,
  }
}

/**
 * Polls the API, keeping the last good snapshot when a refresh fails, and a rolling history
 * for the sparklines. `refresh()` polls now, e.g. right after switching the scenario.
 */
export function useSnapshot(filter: EventFilter) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [history, setHistory] = useState<Point[]>([])
  const [failed, setFailed] = useState(false)
  const [nonce, setNonce] = useState(0)
  const refresh = useCallback(() => setNonce((n) => n + 1), [])

  useEffect(() => {
    const unmount = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined

    async function poll() {
      try {
        const timeout = AbortSignal.timeout(REQUEST_TIMEOUT_MS)
        const next = await fetchSnapshot(filter, AbortSignal.any([unmount.signal, timeout]))
        if (unmount.signal.aborted) return
        setSnapshot(next)
        setFailed(false)
        const point = toPoint(next)
        setHistory((h) =>
          (h.at(-1)?.at ?? 0) >= point.at ? h : [...h, point].slice(-HISTORY_POINTS),
        )
      } catch {
        if (unmount.signal.aborted) return
        setFailed(true)
      }
      // Chained rather than setInterval, so slow responses never overlap.
      timer = setTimeout(poll, POLL_MS)
    }

    void poll()
    return () => {
      unmount.abort()
      clearTimeout(timer)
    }
  }, [filter, nonce])

  return { snapshot, history, failed, refresh }
}
