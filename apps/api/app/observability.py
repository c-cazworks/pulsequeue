"""Prometheus metrics and JSON logs shared by the API, worker and migration."""

import json
import logging
import os
import sys
from datetime import UTC, datetime

from prometheus_client import Counter, Gauge, Histogram

# --- API (RED). Probes and /metrics are excluded: they are not user traffic.
HTTP_REQUESTS = Counter(
    "pulsequeue_http_requests_total", "HTTP requests handled", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "pulsequeue_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

# --- Worker
WORKER_EVENTS = Counter(
    "pulsequeue_worker_events_total", "Events finished by the worker", ["outcome"]
)
WORKER_EVENT_DURATION = Histogram(
    "pulsequeue_worker_event_duration_seconds", "Time spent evaluating one event"
)
WORKER_LAST_POLL = Gauge(
    "pulsequeue_worker_last_poll_timestamp_seconds", "When the worker last polled the queue"
)
# Every worker replica reports the same queue, so query these with max().
QUEUE_PENDING = Gauge("pulsequeue_queue_pending", "Events waiting to be processed")
QUEUE_OLDEST_AGE = Gauge(
    "pulsequeue_queue_oldest_pending_age_seconds", "Age of the oldest pending event (0 if none)"
)
QUEUE_FAILED = Gauge("pulsequeue_queue_failed", "Events that exhausted their retries")

# Attributes every LogRecord has; anything else came from `extra=` and is logged as a field.
# uvicorn adds color_message: the same text with terminal colour codes.
_STANDARD = set(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, so log tooling can filter on fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update({k: v for k, v in vars(record).items() if k not in _STANDARD})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), handlers=[handler], force=True)
