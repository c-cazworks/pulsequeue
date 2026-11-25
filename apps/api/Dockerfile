FROM python:3.14-slim AS builder
RUN python -m venv /opt/venv
COPY requirements.txt .
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.14-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /srv
COPY --from=builder /opt/venv /opt/venv
COPY app ./app
# Numeric UID so Kubernetes runAsNonRoot can verify it.
USER 10001
EXPOSE 8000
# Exec form: uvicorn is PID 1 and receives SIGTERM directly. The 20s drain plus 5s pool close
# must stay under the orchestrator grace period (30s).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "20"]
