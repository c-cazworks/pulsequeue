import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { App } from './App'

const summary = {
  at: '2026-01-01T00:01:00Z',
  window_seconds: 60,
  scenario: {
    profile: 'downstream_degradation',
    label: 'PARTIAL DEGRADATION',
    rate_per_second: 10,
    changed_at: '2026-01-01T00:00:00Z',
    switchable: true,
  },
  throughput_per_second: 9.4,
  error_rate: 0.08,
  p95_ms: 212,
  queue: { pending: 14, retrying: 6, failed: 0, lag_seconds: 1.2 },
  types: [
    {
      event_type: 'payment.authorized',
      throughput_per_second: 4,
      error_rate: 0,
      p95_ms: 110,
      slo_ms: 250,
    },
    {
      event_type: 'webhook.partner_notify',
      throughput_per_second: 1.7,
      error_rate: 0.3,
      p95_ms: 380,
      slo_ms: 1000,
    },
  ],
  alerts: [
    {
      rule: 'DeliveryErrorRate',
      key: 'webhook.partner_notify',
      severity: 'warning',
      summary: '30% of webhook.partner_notify deliveries failing (threshold 5%)',
      fired_at: '2026-01-01T00:00:15Z',
      resolved_at: null,
    },
  ],
}
const event = {
  id: 1,
  event_type: 'webhook.partner_notify',
  reference: 'whk_abc123',
  state: 'retrying',
  attempts: 2,
  duration_ms: 310,
  last_error: 'partner endpoint returned 503 (upstream timeout)',
  created_at: '2026-01-01T00:00:00Z',
  processed_at: null,
}

function stubApi(events: unknown[] = [event], overrides: object = {}) {
  const fetchMock = vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === 'PUT') return Response.json(summary.scenario)
    if (path === '/api/summary') return Response.json({ ...summary, ...overrides })
    return Response.json(events)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

test('shows the scenario, the firing alert and the KPIs', async () => {
  stubApi()
  render(<App />)
  expect(await screen.findByText('PARTIAL DEGRADATION')).toBeTruthy()
  expect(screen.getByText('DeliveryErrorRate')).toBeTruthy()
  expect(screen.getByText(/30% of webhook.partner_notify deliveries failing/)).toBeTruthy()
  expect(screen.getByText('9.4/s')).toBeTruthy()
  expect(screen.getByText('8.0%')).toBeTruthy()
  expect(screen.getByText('14 pending, 6 retrying')).toBeTruthy()
})

test('shows partial failure per event type', async () => {
  stubApi()
  render(<App />)
  const table = within((await screen.findByText(/By event type/)).closest('section')!)
  const webhook = table.getByText('webhook.partner_notify').closest('tr')!
  expect(within(webhook).getByText('Failing')).toBeTruthy()
  const payment = table.getByText('payment.authorized').closest('tr')!
  expect(within(payment).getByText('Met')).toBeTruthy()
})

test('switches the scenario in one click', async () => {
  const fetchMock = stubApi()
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: 'Queue backlog' }))
  await vi.waitFor(() =>
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/scenario',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ profile: 'retry_storm_backlog' }),
      }),
    ),
  )
  expect(screen.getByRole('button', { name: 'Webhook 5xx' }).getAttribute('aria-pressed')).toBe(
    'true',
  )
})

test('hides the controls when switching is disabled', async () => {
  stubApi([event], { scenario: { ...summary.scenario, switchable: false } })
  render(<App />)
  expect(await screen.findByText(/switching is disabled/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Recover' })).toBeNull()
})

test('filters to errors and retries', async () => {
  const fetchMock = stubApi()
  render(<App />)
  expect(await screen.findByText('Retrying')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Errors and retries' }))
  await vi.waitFor(() =>
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/events?limit=25&filter=problems',
      expect.anything(),
    ),
  )
})

test('shows an empty state', async () => {
  stubApi([])
  render(<App />)
  expect(await screen.findByText('No events delivered yet.')).toBeTruthy()
})

test('reports an API outage', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(null, { status: 503 })),
  )
  render(<App />)
  expect((await screen.findByRole('alert')).textContent).toContain('API unavailable')
})

test('keeps the last data when a refresh fails', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  const fetchMock = stubApi()
  render(<App />)
  await screen.findByText('whk_abc123')
  fetchMock.mockRejectedValue(new TypeError('network'))
  await vi.advanceTimersByTimeAsync(2000)
  expect((await screen.findByRole('alert')).textContent).toContain('last known data')
  expect(screen.getByText('whk_abc123')).toBeTruthy()
})

test('stops polling after unmount', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  const fetchMock = stubApi([])
  const { unmount } = render(<App />)
  await screen.findByText('No events delivered yet.')
  unmount()
  const calls = fetchMock.mock.calls.length
  await vi.advanceTimersByTimeAsync(20_000)
  expect(fetchMock.mock.calls.length).toBe(calls)
})
