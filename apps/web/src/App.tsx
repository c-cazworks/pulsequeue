import type { QueueStatus, TelemetryEvent } from './api'
import { useSnapshot } from './useSnapshot'

const compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 })
const time = new Intl.DateTimeFormat(undefined, { dateStyle: 'short', timeStyle: 'medium' })

const TILES: { key: keyof QueueStatus; label: string }[] = [
  { key: 'pending', label: 'Pending' },
  { key: 'processed', label: 'Processed' },
  { key: 'breached', label: 'Breached threshold' },
  { key: 'failed', label: 'Failed' },
]

// Icon + label, so state is never conveyed by color alone.
function eventState(event: TelemetryEvent): { icon: string; label: string; tone: string } {
  if (event.status === 'failed') return { icon: '✕', label: 'Failed', tone: 'critical' }
  if (event.status === 'pending') return { icon: '○', label: 'Pending', tone: 'neutral' }
  if (event.breached) return { icon: '▲', label: 'Breached', tone: 'warning' }
  return { icon: '✓', label: 'OK', tone: 'good' }
}

function EventRow({ event }: { event: TelemetryEvent }) {
  const state = eventState(event)
  return (
    <tr>
      <td>
        <span className={`icon ${state.tone}`} aria-hidden="true">
          {state.icon}
        </span>{' '}
        <span>{state.label}</span>
      </td>
      <td>{event.source}</td>
      <td>{event.metric}</td>
      <td className="num">{event.value}</td>
      <td className="num">{event.threshold ?? '–'}</td>
      <td>{time.format(new Date(event.created_at))}</td>
    </tr>
  )
}

export function App() {
  const { snapshot, failed } = useSnapshot()

  return (
    <main>
      <h1>PulseQueue</h1>
      {failed && (
        <p role="alert" className="banner">
          <span className="icon critical" aria-hidden="true">
            ✕
          </span>{' '}
          API unavailable. {snapshot ? 'Showing the last known data.' : 'Retrying…'}
        </p>
      )}
      {!snapshot && !failed && <p>Loading…</p>}
      {snapshot && (
        <>
          <dl className="tiles">
            {TILES.map(({ key, label }) => (
              <div key={key} className="tile">
                <dt>{label}</dt>
                <dd>{compact.format(snapshot.status[key])}</dd>
              </div>
            ))}
          </dl>
          <h2>Recent events</h2>
          {snapshot.events.length === 0 ? (
            <p>No events yet.</p>
          ) : (
            <div className="scroll">
              <table>
                <thead>
                  <tr>
                    <th scope="col">State</th>
                    <th scope="col">Source</th>
                    <th scope="col">Metric</th>
                    <th scope="col" className="num">
                      Value
                    </th>
                    <th scope="col" className="num">
                      Threshold
                    </th>
                    <th scope="col">Received</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshot.events.map((event) => (
                    <EventRow key={event.id} event={event} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </main>
  )
}
