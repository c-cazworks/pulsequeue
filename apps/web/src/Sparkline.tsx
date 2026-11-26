import { useState, type PointerEvent } from 'react'

const W = 240
const H = 48
const PAD = 3 // keeps the 2px line and the end dot inside the box

export interface SparkPoint {
  at: number // ms
  value: number | null
}

interface Props {
  label: string
  points: SparkPoint[]
  format: (v: number) => string
  threshold?: number
}

const clock = new Intl.DateTimeFormat(undefined, { timeStyle: 'medium' })

/** Rolling trend for one KPI, with a dashed threshold line and a crosshair readout. */
export function Sparkline({ label, points, format, threshold }: Props) {
  const [hover, setHover] = useState<number | null>(null)
  const known = points.filter((p): p is { at: number; value: number } => p.value !== null)
  const first = known[0]
  const latest = known.at(-1)
  if (!first || !latest || known.length < 2) {
    return <div className="spark-empty">Collecting data…</div>
  }

  // Zero-based, so a small wobble never looks like a spike; the threshold always fits.
  const max = Math.max(...known.map((p) => p.value), threshold ?? 0) * 1.1 || 1
  const last = points.length - 1
  const x = (i: number) => PAD + (i / last) * (W - 2 * PAD)
  const y = (v: number) => H - PAD - (v / max) * (H - 2 * PAD)

  // A gap (null) breaks the line instead of bridging it.
  let path = ''
  let drawing = false
  points.forEach((p, i) => {
    if (p.value === null) {
      drawing = false
      return
    }
    path += `${drawing ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`
    drawing = true
  })

  function track(e: PointerEvent<SVGSVGElement>) {
    const box = e.currentTarget.getBoundingClientRect()
    const i = Math.round(((e.clientX - box.left) / box.width) * last)
    setHover(Math.max(0, Math.min(last, i)))
  }

  const hovered = hover === null ? undefined : points[hover]
  const end = points[last]
  const seconds = Math.round((latest.at - first.at) / 1000)

  return (
    <div className="spark">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`${label} over the last ${seconds} seconds, from ${format(first.value)} to ${format(latest.value)}`}
        onPointerMove={track}
        onPointerLeave={() => setHover(null)}
      >
        {threshold !== undefined && (
          <line className="spark-threshold" x1={0} x2={W} y1={y(threshold)} y2={y(threshold)} />
        )}
        <path className="spark-line" d={path} />
        {hover === null && end?.value != null && (
          <circle className="spark-dot" cx={x(last)} cy={y(end.value)} r={3} />
        )}
        {hover !== null && (
          <>
            <line className="spark-cross" x1={x(hover)} x2={x(hover)} y1={0} y2={H} />
            {hovered?.value != null && (
              <circle className="spark-dot" cx={x(hover)} cy={y(hovered.value)} r={3} />
            )}
          </>
        )}
      </svg>
      {hover !== null && hovered && (
        <div className="spark-tip" style={{ left: `${(x(hover) / W) * 100}%` }} role="status">
          <span>{clock.format(hovered.at)}</span>{' '}
          <strong>{hovered.value === null ? 'no data' : format(hovered.value)}</strong>
        </div>
      )}
    </div>
  )
}
