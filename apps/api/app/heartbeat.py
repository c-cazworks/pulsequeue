"""Worker liveness: the loop touches a file; an exec probe runs `python -m app.heartbeat`."""

import os
import sys
import tempfile
import time
from pathlib import Path

# Must exceed the worker's longest iteration (see app.worker.ITERATION_TIMEOUT_SECONDS).
MAX_AGE_SECONDS = 60.0


def path() -> Path:
    default = Path(tempfile.gettempdir()) / "pulsequeue-worker-heartbeat"
    return Path(os.environ.get("WORKER_HEARTBEAT_FILE", default))


def touch() -> None:
    path().touch()


def is_fresh() -> bool:
    try:
        return time.time() - path().stat().st_mtime < MAX_AGE_SECONDS
    except FileNotFoundError:
        return False


if __name__ == "__main__":
    sys.exit(0 if is_fresh() else 1)
