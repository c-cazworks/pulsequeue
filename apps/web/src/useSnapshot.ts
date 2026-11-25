import { useEffect, useState } from 'react'
import { fetchSnapshot, type Snapshot } from './api'

const POLL_MS = 5000
const REQUEST_TIMEOUT_MS = 8000

/** Polls the API, keeping the last good snapshot when a refresh fails. */
export function useSnapshot() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    const unmount = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined

    async function poll() {
      try {
        const timeout = AbortSignal.timeout(REQUEST_TIMEOUT_MS)
        const next = await fetchSnapshot(AbortSignal.any([unmount.signal, timeout]))
        if (unmount.signal.aborted) return
        setSnapshot(next)
        setFailed(false)
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
  }, [])

  return { snapshot, failed }
}
