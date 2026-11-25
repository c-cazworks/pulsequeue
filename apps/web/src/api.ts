export interface QueueStatus {
  pending: number
  processed: number
  failed: number
  breached: number
}

export interface TelemetryEvent {
  id: number
  source: string
  metric: string
  value: number
  threshold: number | null
  status: 'pending' | 'processed' | 'failed'
  breached: boolean
  created_at: string
}

export interface Snapshot {
  status: QueueStatus
  events: TelemetryEvent[]
}

async function getJson<T>(path: string, signal: AbortSignal): Promise<T> {
  const res = await fetch(path, { signal })
  if (!res.ok) throw new Error(`${path} returned ${res.status}`)
  return (await res.json()) as T
}

export async function fetchSnapshot(signal: AbortSignal): Promise<Snapshot> {
  const [status, events] = await Promise.all([
    getJson<QueueStatus>('/api/status', signal),
    getJson<TelemetryEvent[]>('/api/events?limit=20', signal),
  ])
  return { status, events }
}
