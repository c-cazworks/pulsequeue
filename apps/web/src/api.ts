export type Profile = 'healthy' | 'downstream_degradation' | 'retry_storm_backlog' | 'recover'
export type EventFilter = 'all' | 'problems'

export interface Scenario {
  profile: Profile
  label: string
  rate_per_second: number
  changed_at: string
  switchable: boolean
}

export interface TypeSummary {
  event_type: string
  throughput_per_second: number
  error_rate: number
  p95_ms: number | null
  slo_ms: number
}

export interface Alert {
  rule: string
  key: string
  severity: 'warning' | 'critical'
  summary: string
  fired_at: string
  resolved_at: string | null
}

export interface Summary {
  at: string
  window_seconds: number
  scenario: Scenario
  throughput_per_second: number
  error_rate: number
  p95_ms: number | null
  queue: {
    pending: number
    retrying: number
    failed: number
    lag_seconds: number
  }
  types: TypeSummary[]
  alerts: Alert[]
}

export interface DeliveryEvent {
  id: number
  event_type: string
  reference: string | null
  state: 'pending' | 'retrying' | 'processed' | 'failed'
  attempts: number
  duration_ms: number | null
  last_error: string | null
  created_at: string
  processed_at: string | null
}

export interface Snapshot {
  summary: Summary
  events: DeliveryEvent[]
}

async function getJson<T>(path: string, signal: AbortSignal): Promise<T> {
  const res = await fetch(path, { signal })
  if (!res.ok) throw new Error(`${path} returned ${res.status}`)
  return (await res.json()) as T
}

export async function fetchSnapshot(filter: EventFilter, signal: AbortSignal): Promise<Snapshot> {
  const [summary, events] = await Promise.all([
    getJson<Summary>('/api/summary', signal),
    getJson<DeliveryEvent[]>(`/api/events?limit=25&filter=${filter}`, signal),
  ])
  return { summary, events }
}

export async function putScenario(profile: Profile): Promise<Scenario> {
  const res = await fetch('/api/scenario', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ profile }),
  })
  if (!res.ok) throw new Error(`switching scenario returned ${res.status}`)
  return (await res.json()) as Scenario
}
