# CLAUDE.md — PulseQueue

## Project Overview
PulseQueue is a compact telemetry & alerting control-plane portfolio project built to demonstrate senior-level architecture, reliability, and DevOps practices:
- **Frontend (`apps/web`)**: React + TypeScript + Vite served via Nginx in a multi-stage container.
- **Backend & Worker (`apps/api`)**: Python (FastAPI) API + background worker + PostgreSQL, with `/healthz` and `/readyz` endpoints and `SIGTERM` graceful shutdown.
- **IaC (`infra/terraform`)**: Modular AWS provisioning (VPC, EKS, ECR, IRSA) with safe teardown support.
- **Kubernetes (`deploy/helm`)**: Helm chart with probes, HPA, PDB, and environment value overrides.
- **CI/CD (`.github/workflows`)**: PR checks, immutable SHA image tags, OIDC auth, and deploy workflows.
- **Docs (`docs/`)**: Architecture trade-offs (`architecture.md`) and failure runbooks (`runbook.md`).

## Token & Context Efficiency Rules
- **Read narrowly**: Open only the specific files needed for the current task. Never read generated artifacts, lockfiles (`package-lock.json`, `poetry.lock`, `*.tfstate*`), `.venv`, `node_modules`, or full Terraform/Helm dependency trees unless explicitly asked.
- **Summarize over dump**: When inspecting logs, diffs, or test output, summarize root causes and exact changes rather than pasting large raw outputs.
- **Scoped changes only**: Touch only files relevant to the requested slice. Do not refactor unrelated modules or add unrequested abstractions.
- **Prefer diffs & targeted edits**: Make small, verifiable increments per step; check status/tests before moving to the next slice.

## Architectural & Production Standards
- **Security first**: No hardcoded secrets, static AWS access keys in CI, or overly broad IAM policies. Use IRSA and GitHub OIDC where applicable.
- **Resilience by default**: Keep liveness (`/healthz`) and readiness (`/readyz`) distinct; handle `SIGTERM` cleanly so in-flight work drains; include HPA/PDB and sensible resource requests/limits in Helm.
- **Cost & teardown awareness**: Keep infrastructure modular and easy to `terraform destroy` cleanly without leaving orphaned cloud resources.
- **Explain trade-offs briefly**: When making an architectural or tooling choice, state *why* in 1–2 sentences and note the main trade-off.

## Working Cadence & Commands
Work in small, vertical slices rather than scaffolding everything at once:
1. Local stack (`docker compose` + unit tests + health checks)
2. Core Terraform modules (`vpc`, `eks`, `ecr`, `irsa`) + teardown check
3. Container builds + GitHub Actions CI/CD + Helm deployment
4. Observability (RED metrics/logs/alerts) + `docs/architecture.md` & `docs/runbook.md`

Use `make` targets or standard local commands (`pytest`, `npm test` / `npm run build`, `terraform validate`, `helm lint`) to verify changes before finishing a task.

## When to Ask Before Acting
Pause and ask a brief question only when a choice has lasting architectural or cost impact (e.g., AWS-native vs. self-hosted observability stack, managed RDS vs. in-cluster/local DB for dev, or breaking schema/API changes). Otherwise, make a pragmatic default and state it clearly.
