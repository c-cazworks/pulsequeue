-- Idempotent; applied by `python -m app.migrate`. Column changes need a real migration tool.
CREATE TABLE IF NOT EXISTS events (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source       text NOT NULL,
    metric       text NOT NULL,
    value        double precision NOT NULL,
    threshold    double precision,
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'processed', 'failed')),
    breached     boolean NOT NULL DEFAULT false,
    attempts     integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    last_error   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);

-- Partial, so claims stay cheap as processed rows accumulate.
CREATE INDEX IF NOT EXISTS events_pending_idx
    ON events (available_at, id) WHERE status = 'pending';
