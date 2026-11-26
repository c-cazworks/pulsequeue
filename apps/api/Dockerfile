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
# RDS CAs, for PGSSLMODE=verify-full on AWS; unused locally.
ADD --chmod=644 https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /etc/ssl/rds-ca.pem
COPY --from=builder /opt/venv /opt/venv
COPY app ./app
# Numeric UID so Kubernetes runAsNonRoot can verify it.
USER 10001
EXPOSE 8000
# Exec form: uvicorn is PID 1 and receives SIGTERM directly. The 20s drain plus 5s pool close
# must stay under the orchestrator grace period (Compose 30s; Kubernetes 45s, less its preStop).
# The log config makes uvicorn's own logs JSON; app.main logs requests, so its access log is off.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "20", "--log-config", "app/log_config.json", "--no-access-log"]
