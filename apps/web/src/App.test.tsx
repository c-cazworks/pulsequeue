import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { App } from './App'

const status = { pending: 3, processed: 1284, failed: 0, breached: 7 }
const event = {
  id: 1,
  source: 'web-1',
  metric: 'cpu.load',
  value: 0.9,
  threshold: 0.8,
  status: 'processed',
  breached: true,
  created_at: '2026-01-01T00:00:00Z',
}

function stubApi(events: unknown[]) {
  const fetchMock = vi.fn(async (path: string) =>
    Response.json(path === '/api/status' ? status : events),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

test('shows queue counts and events', async () => {
  stubApi([event])
  render(<App />)
  expect(await screen.findByText('1.3K')).toBeTruthy()
  expect(screen.getByText('cpu.load')).toBeTruthy()
  expect(screen.getByText('Breached')).toBeTruthy()
})

test('shows an empty state', async () => {
  stubApi([])
  render(<App />)
  expect(await screen.findByText('No events yet.')).toBeTruthy()
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
  const fetchMock = stubApi([event])
  render(<App />)
  await screen.findByText('cpu.load')
  fetchMock.mockRejectedValue(new TypeError('network'))
  await vi.advanceTimersByTimeAsync(5000)
  expect((await screen.findByRole('alert')).textContent).toContain('last known data')
  expect(screen.getByText('cpu.load')).toBeTruthy()
})

test('stops polling after unmount', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  const fetchMock = stubApi([])
  const { unmount } = render(<App />)
  await screen.findByText('No events yet.')
  unmount()
  const calls = fetchMock.mock.calls.length
  await vi.advanceTimersByTimeAsync(20_000)
  expect(fetchMock.mock.calls.length).toBe(calls)
})
