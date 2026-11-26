-- Idempotent; applied by `python -m app.migrate`. Column changes need a real migration tool.

-- One-time replacement of the original telemetry table (source/metric/value/threshold).
-- It held only demo data. Scoped to the current schema, so tests in their own schema are safe.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = 'events'
                 AND column_name = 'source') THEN
        DROP TABLE events;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS events (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type   text NOT NULL,
    payload      jsonb NOT NULL DEFAULT '{}',
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'processed', 'failed')),
    attempts     integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    last_error   text,
    duration_ms  double precision, -- the last attempt's delivery time
    created_at   timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);

-- Partial, so claims stay cheap as processed rows accumulate.
CREATE INDEX IF NOT EXISTS events_pending_idx
    ON events (available_at, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS events_created_idx ON events (created_at);

-- One row per delivery attempt: error rates and latency percentiles come from here, because
-- an event's own row only keeps its last attempt.
CREATE TABLE IF NOT EXISTS attempts (
    event_id    bigint NOT NULL,
    event_type  text NOT NULL,
    attempt     integer NOT NULL,
    ok          boolean NOT NULL,
    duration_ms double precision NOT NULL,
    finished_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS attempts_finished_idx ON attempts (finished_at);

-- The demo scenario: one row, changed from the UI.
CREATE TABLE IF NOT EXISTS scenario (
    id         boolean PRIMARY KEY DEFAULT true CHECK (id),
    profile    text NOT NULL DEFAULT 'healthy',
    changed_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO scenario DEFAULT VALUES ON CONFLICT DO NOTHING;

-- Alerts the worker evaluates for the UI. `key` is the event type, or '' for queue-wide rules.
CREATE TABLE IF NOT EXISTS alerts (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule          text NOT NULL,
    key           text NOT NULL DEFAULT '',
    severity      text NOT NULL,
    summary       text NOT NULL,
    value         double precision NOT NULL,
    pending_since timestamptz NOT NULL,
    fired_at      timestamptz,
    resolved_at   timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS alerts_open_idx ON alerts (rule, key) WHERE resolved_at IS NULL;
