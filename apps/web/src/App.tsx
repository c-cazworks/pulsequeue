import { useState, type ReactNode } from 'react'
import {
  putScenario,
  type Alert,
  type DeliveryEvent,
  type EventFilter,
  type Profile,
  type Summary,
} from './api'
import { Sparkline } from './Sparkline'
import { useSnapshot, type Point } from './useSnapshot'

const clock = new Intl.DateTimeFormat(undefined, { timeStyle: 'medium' })
const pct = (v: number) => `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%`
const ms = (v: number) => `${Math.round(v)} ms`
const secs = (v: number) => `${v.toFixed(v < 10 ? 1 : 0)} s`
const rate = (v: number) => `${v.toFixed(1)}/s`

// The same thresholds the alert rules use (apps/api/app/alerts.py).
const ERROR_RATE_THRESHOLD = 0.05
const QUEUE_LAG_THRESHOLD = 30

type Tone = 'good' | 'warning' | 'critical' | 'neutral'
const ICONS: Record<Tone, string> = {
  good: '✓',
  warning: '▲',
  critical: '✕',
  neutral: '○',
}

// Icon + label, so state is never conveyed by color alone.
function Status({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className="status">
      <span className={`icon ${tone}`} aria-hidden="true">
        {ICONS[tone]}
      </span>{' '}
      {children}
    </span>
  )
}

const PROFILE_TONE: Record<Profile, Tone> = {
  healthy: 'good',
  downstream_degradation: 'warning',
  retry_storm_backlog: 'critical',
  recover: 'neutral',
}

const SCENARIOS: { profile: Profile; label: string; hint: string }[] = [
  {
    profile: 'healthy',
    label: 'Healthy',
    hint: '10 events/s, everything fast',
  },
  {
    profile: 'downstream_degradation',
    label: 'Webhook 5xx',
    hint: 'One partner fails 30%',
  },
  {
    profile: 'retry_storm_backlog',
    label: 'Queue backlog',
    hint: 'Slow dependencies, retries',
  },
  {
    profile: 'recover',
    label: 'Recover',
    hint: 'Clear faults, drain the queue',
  },
]

function ScenarioControls({ summary, onSwitched }: { summary: Summary; onSwitched: () => void }) {
  const [busy, setBusy] = useState<Profile | null>(null)
  const [error, setError] = useState<string | null>(null)
  const { scenario } = summary

  async function choose(profile: Profile) {
    setBusy(profile)
    setError(null)
    try {
      await putScenario(profile)
      onSwitched()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'switching failed')
    } finally {
      setBusy(null)
    }
  }

  if (!scenario.switchable) {
    return <p className="muted">Scenario switching is disabled on this deployment.</p>
  }
  return (
    <div className="scenarios" role="group" aria-label="Scenario">
      {SCENARIOS.map((s) => (
        <button
          key={s.profile}
          type="button"
          aria-pressed={scenario.profile === s.profile}
          disabled={busy !== null}
          onClick={() => void choose(s.profile)}
          title={s.hint}
        >
          {busy === s.profile ? 'Switching…' : s.label}
        </button>
      ))}
      {error && (
        <span role="alert">
          <Status tone="critical">{error}</Status>
        </span>
      )}
    </div>
  )
}

function ago(from: string, now: string): string {
  const s = Math.max(0, Math.round((Date.parse(now) - Date.parse(from)) / 1000))
  return s < 90 ? `${s}s ago` : `${Math.round(s / 60)}m ago`
}

function AlertCard({ alert, now }: { alert: Alert; now: string }) {
  const resolved = alert.resolved_at !== null
  return (
    <li className={`alert ${resolved ? 'resolved' : alert.severity}`}>
      <div className="alert-head">
        <Status tone={resolved ? 'good' : alert.severity}>
          <strong>
            {resolved ? 'Resolved' : alert.severity === 'critical' ? 'Critical' : 'Warning'}
          </strong>{' '}
          {alert.rule}
        </Status>
        {alert.key && <code>{alert.key}</code>}
      </div>
      <p>{alert.summary}</p>
      <p className="muted">
        Fired {clock.format(new Date(alert.fired_at))} ({ago(alert.fired_at, now)})
        {resolved && `, resolved ${ago(alert.resolved_at!, now)}`}
      </p>
    </li>
  )
}

function AlertStrip({ summary }: { summary: Summary }) {
  const firing = summary.alerts.filter((a) => a.resolved_at === null)
  const resolved = summary.alerts.filter((a) => a.resolved_at !== null).slice(0, 3)
  return (
    <section aria-labelledby="alerts-h">
      <h2 id="alerts-h">Alerts</h2>
      {firing.length === 0 && (
        <p className="all-clear">
          <Status tone="good">No active alerts</Status>
        </p>
      )}
      {(firing.length > 0 || resolved.length > 0) && (
        <ul className="alerts">
          {[...firing, ...resolved].map((a) => (
            <AlertCard key={`${a.rule}:${a.key}:${a.fired_at}`} alert={a} now={summary.at} />
          ))}
        </ul>
      )}
    </section>
  )
}

interface KpiProps {
  label: string
  value: string
  detail?: string
  tone?: Tone
  history: Point[]
  pick: (p: Point) => number | null
  format: (v: number) => string
  threshold?: number
}

function Kpi({ label, value, detail, tone, history, pick, format, threshold }: KpiProps) {
  return (
    <div className="tile">
      <dt>{label}</dt>
      <dd>
        <span className="value">{value}</span>
        {tone && tone !== 'good' && (
          <span className="tile-status">
            <Status tone={tone}>over threshold</Status>
          </span>
        )}
        {detail && <span className="detail">{detail}</span>}
        <Sparkline
          label={label}
          points={history.map((p) => ({ at: p.at, value: pick(p) }))}
          format={format}
          threshold={threshold}
        />
      </dd>
    </div>
  )
}

function Kpis({ summary: s, history }: { summary: Summary; history: Point[] }) {
  return (
    <dl className="tiles">
      <Kpi
        label="Throughput"
        value={rate(s.throughput_per_second)}
        detail="events delivered"
        history={history}
        pick={(p) => p.throughput}
        format={rate}
      />
      <Kpi
        label="Error rate"
        value={pct(s.error_rate)}
        detail="of delivery attempts"
        tone={s.error_rate > ERROR_RATE_THRESHOLD ? 'warning' : 'good'}
        history={history}
        pick={(p) => p.errorRate}
        format={pct}
        threshold={ERROR_RATE_THRESHOLD}
      />
      <Kpi
        label="p95 delivery latency"
        value={s.p95_ms === null ? '–' : ms(s.p95_ms)}
        detail="all event types"
        history={history}
        pick={(p) => p.p95}
        format={ms}
      />
      <Kpi
        label="Queue lag"
        value={secs(s.queue.lag_seconds)}
        detail={`${s.queue.pending.toLocaleString()} pending, ${s.queue.retrying.toLocaleString()} retrying`}
        tone={s.queue.lag_seconds > QUEUE_LAG_THRESHOLD ? 'critical' : 'good'}
        history={history}
        pick={(p) => p.lag}
        format={secs}
        threshold={QUEUE_LAG_THRESHOLD}
      />
    </dl>
  )
}

function TypeTable({ summary }: { summary: Summary }) {
  return (
    <section aria-labelledby="types-h">
      <h2 id="types-h">By event type (last {summary.window_seconds} s)</h2>
      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Event type</th>
              <th scope="col" className="num">
                Delivered
              </th>
              <th scope="col" className="num">
                Errors
              </th>
              <th scope="col" className="num">
                p95
              </th>
              <th scope="col" className="num">
                Objective
              </th>
              <th scope="col">SLO</th>
            </tr>
          </thead>
          <tbody>
            {summary.types.map((t) => {
              const slow = t.p95_ms !== null && t.p95_ms > t.slo_ms
              const failing = t.error_rate > ERROR_RATE_THRESHOLD
              return (
                <tr key={t.event_type}>
                  <td>
                    <code>{t.event_type}</code>
                  </td>
                  <td className="num">{rate(t.throughput_per_second)}</td>
                  <td className="num">{pct(t.error_rate)}</td>
                  <td className="num">{t.p95_ms === null ? '–' : ms(t.p95_ms)}</td>
                  <td className="num">{ms(t.slo_ms)}</td>
                  <td>
                    {slow || failing ? (
                      <Status tone="warning">{slow ? 'Too slow' : 'Failing'}</Status>
                    ) : (
                      <Status tone="good">Met</Status>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}

const STATE: Record<DeliveryEvent['state'], { tone: Tone; label: string }> = {
  processed: { tone: 'good', label: 'Delivered' },
  pending: { tone: 'neutral', label: 'Queued' },
  retrying: { tone: 'warning', label: 'Retrying' },
  failed: { tone: 'critical', label: 'Dead-lettered' },
}

function EventsTable({
  events,
  filter,
  onFilter,
}: {
  events: DeliveryEvent[]
  filter: EventFilter
  onFilter: (f: EventFilter) => void
}) {
  return (
    <section aria-labelledby="events-h">
      <div className="section-head">
        <div>
          <h2 id="events-h">Recent events</h2>
          <p className="muted">Events still waiting for a first attempt are counted in Queue lag.</p>
        </div>
        <div className="segmented" role="group" aria-label="Show">
          <button type="button" aria-pressed={filter === 'all'} onClick={() => onFilter('all')}>
            All
          </button>
          <button
            type="button"
            aria-pressed={filter === 'problems'}
            onClick={() => onFilter('problems')}
          >
            Errors and retries
          </button>
        </div>
      </div>
      {events.length === 0 ? (
        <p>{filter === 'all' ? 'No events delivered yet.' : 'No errors or retries.'}</p>
      ) : (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">State</th>
                <th scope="col">Event type</th>
                <th scope="col">Reference</th>
                <th scope="col" className="num">
                  Attempts
                </th>
                <th scope="col" className="num">
                  Last attempt
                </th>
                <th scope="col">Last error</th>
                <th scope="col">Received</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id}>
                  <td>
                    <Status tone={STATE[e.state].tone}>{STATE[e.state].label}</Status>
                  </td>
                  <td>
                    <code>{e.event_type}</code>
                  </td>
                  <td>
                    <code>{e.reference ?? '–'}</code>
                  </td>
                  <td className="num">{e.attempts}</td>
                  <td className="num">{e.duration_ms === null ? '–' : ms(e.duration_ms)}</td>
                  <td className="error-cell" title={e.last_error ?? undefined}>
                    {e.last_error ?? ''}
                  </td>
                  <td>{clock.format(new Date(e.created_at))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

export function App() {
  const [filter, setFilter] = useState<EventFilter>('all')
  const { snapshot, history, failed, refresh } = useSnapshot(filter)
  const summary = snapshot?.summary

  return (
    <main>
      <header className="top">
        <div>
          <h1>PulseQueue</h1>
          <p className="muted">Payment and webhook delivery console</p>
        </div>
        {summary && (
          <span className={`badge ${PROFILE_TONE[summary.scenario.profile]}`}>
            <Status tone={PROFILE_TONE[summary.scenario.profile]}>{summary.scenario.label}</Status>
          </span>
        )}
      </header>
      {failed && (
        <p role="alert" className="banner">
          <Status tone="critical">
            API unavailable. {snapshot ? 'Showing the last known data.' : 'Retrying…'}
          </Status>
        </p>
      )}
      {!snapshot && !failed && <p>Loading…</p>}
      {summary && snapshot && (
        <>
          <ScenarioControls summary={summary} onSwitched={refresh} />
          <AlertStrip summary={summary} />
          <Kpis summary={summary} history={history} />
          <TypeTable summary={summary} />
          <EventsTable events={snapshot.events} filter={filter} onFilter={setFilter} />
        </>
      )}
    </main>
  )
}
