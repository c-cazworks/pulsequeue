# Runbook

Start-up and shutdown steps are in the [README](../README.md). Design reasons are in
[architecture.md](architecture.md).

Each alert links to a section here. Local alerts are Prometheus rules in
`deploy/observability/alerts.yml`, shown at http://localhost:9090/alerts. AWS alarms are CloudWatch
alarms in `infra/terraform/modules/monitoring`, sent to the SNS topic `pulsequeue-alerts`.

**Where to look**

| | Local (Docker Compose) | EKS |
|---|---|---|
| Console (scenario, alerts, SLOs per event type) | http://localhost:8080 | The URL `make deploy` prints |
| Dashboard | Grafana: http://localhost:3000 | CloudWatch: `terraform -chdir=infra/terraform output dashboard_url` |
| Logs (JSON) | `docker compose logs -f api worker` | `kubectl -n pulsequeue logs -l app.kubernetes.io/component=api --tail=100` |
| App metrics | Prometheus: http://localhost:9090 | `kubectl -n pulsequeue port-forward deploy/pulsequeue-worker 9100`, then http://localhost:9100 |
| Pods and probes | `docker compose ps` | `kubectl -n pulsequeue get pods,hpa` and `kubectl -n pulsequeue get events --sort-by=.lastTimestamp` |

Filter JSON logs with PowerShell, for example to show only failures:

```powershell
docker compose logs --no-log-prefix api | ConvertFrom-Json | Where-Object { $_.status -ge 500 }
```

## API errors or API down

Alerts: `ApiDown`, `ApiHighErrorRate` (local); `pulsequeue-alb-target-5xx` (AWS).

1. **Is the database reachable?** While it is not, the API answers `503 database unavailable` and
   `/readyz` fails, so pods leave the load balancer instead of restarting.
   - Local: `docker compose ps db`, then `curl http://localhost:8000/readyz`.
   - EKS: `kubectl -n pulsequeue get pods`. Ready `0/1` on every API pod points at the database.
2. **On EKS, can pods read the password?** Look for `cannot read the database secret` in the API
   logs.
   - `AccessDenied`: the Pod Identity association or the `pulsequeue-app` policy is missing. Check
     that `terraform -chdir=infra/terraform plan` shows no changes.
   - Timeouts: the NAT gateway or Secrets Manager is unreachable.
3. **Is it the code?** Errors on one route only, and logs with an `exception` field, point at a bad
   release. Roll back with `helm -n pulsequeue rollback pulsequeue`, then fix forward.
4. **Crash loops?** Run `kubectl -n pulsequeue describe pod <pod>`. `OOMKilled` means raising
   `api.resources.limits.memory`.

## API slow

Alerts: `ApiSlow` (p95 above 500 ms, local); `pulsequeue-alb-slow` (average above 500 ms, AWS).

1. Find the slow route on the Grafana latency panel, or in the logs' `duration_ms` field.
2. Check the database first: `pulsequeue-rds-cpu`, and CPU and connections on the CloudWatch
   dashboard. `GET /api/events` and `/api/status` scan the `events` table, which grows without limit.
3. On EKS, check CPU with `kubectl -n pulsequeue top pods`. If the API HPA is at its maximum (4),
   the nodes may be full: `kubectl get pods -A --field-selector=status.phase=Pending`. Two
   `t3.small` nodes fit 22 pods in total.

## Is it a demo scenario?

The console's badge shows the active scenario. Anything other than **HEALTHY** means the failures
are injected on purpose: `PARTIAL DEGRADATION` fails partner webhooks, and
`BACKLOG / SLO BREACH` slows every delivery. Click **Recover** to end it. The sections below are
written as if the failure were real, which is how to practise them.

## Queue lag or worker down

Alerts: `WorkerDown`; `QueueLag` (ready events have waited over 30 s for a worker, for 30 s).

Queue lag counts only events that are ready to run. Events waiting out a retry backoff are not
late, and appear under "retrying" instead.

1. **Is a worker running?**
   - Local: `docker compose ps worker`.
   - EKS: `kubectl -n pulsequeue get pods -l app.kubernetes.io/component=worker`. A restart loop
     with a failing liveness probe means the poll loop is stuck: the heartbeat goes stale after 60 s.
2. **Is it polling?** The Grafana panel "Seconds since each worker polled" should stay near 1 s.
3. **Is it keeping up?** On the console, compare Throughput with the arrival rate (10/s when
   healthy). Slow deliveries cut throughput: check the p95 column per event type. If workers stay
   behind, add a replica: `--set worker.replicas=3` on EKS, or `docker compose up -d --scale
   worker=3` locally. Claims use `SKIP LOCKED`, so replicas never process the same event.
4. **Is one dependency slow?** If one event type's p95 is far above its objective, that dependency
   is holding every worker. Lowering `HANDLER_TIMEOUT_SECONDS` sheds it faster, at the cost of more
   retries.
5. **Is the database slow?** A slow claim query slows every worker. See
   [Database under pressure](#database-under-pressure).

While the backlog lasts, the simulator stops sending at 5,000 pending events.

## Delivery errors

Alert: `DeliveryErrorRate` (more than 5% of one event type's delivery attempts failed over the
last minute, for 30 s). The alert names the event type.

1. **One type or all of them?** The console's "By event type" table shows it. One type failing
   points at its downstream (a partner's endpoint for `webhook.partner_notify`); all types failing
   points at something shared, such as the network or NAT gateway.
2. **What does the downstream say?** Filter the console's events to "Errors and retries" and read
   **Last error**, or the worker logs' `delivery failed` lines and their `error` field.
3. **Is anything being lost?** No, while retries succeed: events retry with backoff (2, 4, 8,
   16 s), and only after 5 failed attempts are they dead-lettered. Watch for `EventsDeadLettered`.
4. A partner that is down for long only retries its own events. Other event types keep their
   latency unless the queue lag rises too.

## Latency SLO breach

Alert: `LatencySLOBreach` (one event type's p95 delivery time is above its objective: 250 ms for
payments, 300 ms for inventory, 500 ms for invoices, 1 s for partner webhooks).

1. **Which dependency?** The alert names the event type. Payments breaching first matters most:
   a customer is waiting at checkout.
2. **Is it slow, or queued?** This measures delivery time only. Time spent waiting in the queue is
   `QueueLag`. Both together mean slow deliveries are also starving the workers.
3. Slow deliveries hold workers longer, so throughput drops: see
   [Queue lag or worker down](#queue-lag-or-worker-down).

## Events dead-lettered

Alert: `EventsDeadLettered` (an event failed all 5 attempts in the last 5 minutes).

1. Find them and the reason. The console's "Errors and retries" filter shows them as
   **Dead-lettered**. Or in SQL (locally, open a shell with
   `docker compose exec db psql -U pulsequeue`):

   ```sql
   SELECT id, event_type, payload->>'id' AS reference, attempts, last_error FROM events
   WHERE status = 'failed' ORDER BY id DESC LIMIT 20;
   ```

2. Worker logs show each failed attempt, with a stack trace in `exception` for unexpected errors.
3. After fixing the cause, replay them. Dead-lettered events are kept for a day:

   ```sql
   UPDATE events SET status = 'pending', attempts = 0, available_at = now() WHERE status = 'failed';
   ```

## ALB has no healthy targets

Alarms: `pulsequeue-alb-alb-5xx`, `pulsequeue-alb-unhealthy-targets` (AWS).

The ALB answers `502` or `503` itself when no `web` pod passes its health check (`/readyz` on port
8080). `web` readiness does not depend on the API, so the cause is the web pods or the controller.

1. `kubectl -n pulsequeue get pods -l app.kubernetes.io/component=web`, and their events.
2. Controller errors: `kubectl -n kube-system logs deploy/aws-load-balancer-controller --tail=50`.
   `AccessDenied` there means its Pod Identity association or policy is missing.
3. The page does not load at all, with no 5xx? Your IP has probably changed: see
   [Your IP changed](#your-ip-changed).

## Database under pressure

Alarms: `pulsequeue-rds-cpu`, `pulsequeue-rds-connections`, `pulsequeue-rds-storage` (AWS).

- **Connections:** the app needs at most about 44 (4 API pods × 10, plus 2 workers × 2). More
  points at leaked connections, or another client.
- **Storage:** 20 GiB holds a lot of events. Delete old processed ones:
  `DELETE FROM events WHERE status = 'processed' AND processed_at < now() - interval '7 days';`
- **CPU:** usually the unbounded `events` scans. Check `pg_stat_statements` for slow queries.

## A deploy failed

`make deploy` uses `--rollback-on-failure`, so a failed release rolls itself back.

1. `helm -n pulsequeue history pulsequeue` shows what happened.
2. **Migration failed:** the pre-upgrade hook stops the rollout before any pod changes, so the old
   version keeps serving. The rollback removes the failed Job; to read its logs, run the
   `helm upgrade` from `scripts/deploy.sh` without `--rollback-on-failure`, then
   `kubectl -n pulsequeue logs job/pulsequeue-migrate`.
3. **Pods never became ready:** `kubectl -n pulsequeue get events --sort-by=.lastTimestamp`.
   `ImagePullBackOff` means the commit was never published: check that its `main` CI run finished.
4. **Image copy failed:** run `gh auth refresh -s read:packages`, and check `aws login` is current.

## Teardown is stuck or failed

- **"Refusing: kubectl context is ...":** run the `aws eks update-kubeconfig` command it prints.
- **Stopped while deleting the ingress:** the controller could not delete the ALB. Do not skip this
  step: the ALB would be orphaned and the VPC could not be deleted. Find out why with
  `kubectl -n kube-system logs deploy/aws-load-balancer-controller`. If the controller is gone,
  delete the ALB and its target groups in the EC2 console, remove the ingress finalizer, and run
  `make tf-destroy` again:

  ```powershell
  kubectl -n pulsequeue patch ingress pulsequeue --type=merge -p '{\"metadata\":{\"finalizers\":null}}'
  ```

- **`DependencyViolation` deleting the VPC or a subnet:** something outside Terraform still has a
  network interface there, usually a leftover load balancer. Look in the EC2 console under Network
  Interfaces, filtered by the VPC.
- Afterwards, confirm nothing is left with the commands in the README.

## Your IP changed

The EKS API and the ALB only accept `api_allowed_cidrs`, so `kubectl` times out and the dashboard
does not load.

1. Put the new IP in `infra/terraform/terraform.tfvars`.
2. Run `make tf-plan` and `make tf-apply`. This updates the EKS endpoint.
3. Run `make deploy`, which updates the ALB's allowlist.

## AWS sign-in expired

Terraform, `kubectl` and `make deploy` fail with expired or missing credentials. Run
`aws login --profile pulsequeue` and retry. The cluster keeps running meanwhile; nothing is lost.

## Access denied on something that worked before

Check spend status in AWS Settings > Billing: a spend limit pauses the project when it is reached.
Run `aws freetier get-account-plan-state` to confirm the plan is still `FREE` with credits left. Do
not upgrade the plan or activate advanced features to work around it.

## AWS rules

Read this section before any AWS task. It is AWS's rules file for the new AWS experience, kept here
rather than in `CLAUDE.md` to save context. `CLAUDE.md` takes precedence where they conflict.

Source: <https://raw.githubusercontent.com/aws/agent-toolkit-for-aws/refs/heads/main/rules/aws-starter-rules.md>

<!-- BEGIN AWS Agent Toolkit rules -->
### AWS Guidance for the new AWS experience

This user has signed up for the new AWS experience. This experience lets you sign into AWS using a social provider and requires the following additional context.

Where this guidance conflicts with the project's own instructions, the project's instructions take precedence.

#### Context

##### Terminology:

- Say "project" instead of "account" — a project contains an AWS account and settings for sharing with other collaborators
- Say "team member" instead of "IAM user" — users are invited by email, not created or federated in IAM
- Say "AWS Settings" when referring to management tasks at [settings.aws.com](https://settings.aws.com/) (project management, billing, team members, spend limits). Users view their actual AWS resources in the AWS Management Console.
- Say "selected Region" when referring to the user's Region — not "home Region"
- The user has a managed IAM experience. This includes a managed service control policies (SCP) and resource control policies (RCP) that govern the use of AWS. They will still need to use IAM to create policies to let services work with each other. If there are questions about the SCPs or RCPs, go to the documentation at https://docs.aws.amazon.com/accounts/latest/reference/scps-and-rcps-for-projects.html

##### Constraints:

- All projects share a single AWS Region determined by the user's contact address. Resources cannot be created in other Regions
- When developing:
  - MUST create all Regional resources in the project's assigned Region
  - You CAN create AWS WAF and Cloudwatch Logs resources in us-east-1 when there are global resources (like a global WAF instance) that require a connection to dependencies in us-east-1. You should not use these for any other reason, because resources in the selected Region will provide lower cost (due to no cross-Region traffic), increased availability (due to no cross-Region traffic), and easier manageability (due to not needing to look in another Region). When you need to do an inventory of resources, you need to look in both the selected Region and us-east-1 for Cloudwatch Logs or WAF resources.
  - MUST NOT attempt to create Lambda, API Gateway, or other Regional resources in any other Region
  - MUST direct users to confirm their Region in AWS Settings > View all projects > Overview > Additional Info > Region. If the user cannot confirm their Region, check in ~/.aws/config
  - MUST NOT use Lambda@Edge — excluded from both Lambda and CloudFront
  - MUST NOT use CloudFormation StackSets — no multi-account or multi-Region deployments
  - MUST NOT attempt cross-Region actions — no cross-Region replication for DynamoDB/S3/RDS, no multi-Region KMS keys
  - MUST NOT use Route 53 cross-Region routing — geolocation, latency-based, and failover routing policies are not available
  - CloudFront is a global service and its actions ARE allowed in `us-east-1`. A user can create a CloudFront distribution pointing to their project-region Lambda function URL or API Gateway. However, Lambda and API Gateway themselves MUST NOT be created in `us-east-1` — they must be in the project Region.
  - Reduced availability in `eu-north-1` specifically: Amazon Rekognition, Amazon Textract, Amazon Personalize, AWS App Runner are not available in that Region.
- IAM permissions for human access are managed by AWS. Don't assign roles to team members unless absolutely necessary
- The user may have a spend limit if they are on the paid plan. The limit that pauses their project if it's exceeded. If resources suddenly become inaccessible, ask if they have a spend limit configured. Only project owners can modify a spend limit.
- When developing:
  - MUST ask about spend limit status if the user reports sudden "Access Denied" errors on operations that previously worked
  - MUST direct users to check spend status in AWS Settings > Billing
  - MUST check if a user has upgraded their account to the paid plan
  - MUST ask the user if they want to clean up the successfully created resources or keep them to reduce cost
- The user sets up billing, creates spend limits, and retrieves and pays invoices in AWS Settings. The user creates budgets and optimizes their costs in the AWS Billing and Cost Management console
- Not all AWS services are available. If a service isn't working, do the following:
  1. Run the command `aws freetier get-account-plan-state`
  2. If accountPlanType": "FREE", check the [Free Tier supported services list](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.html#supported-services-free-tier) next,
  3. If accountPlanType": "PAID", check the [Paid Tier supported services list](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.html#supported-services-paid-plan).
  4. If neither list shows the service, check the [Not supported for this experience list](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.html#unsupported-services). The user will need to activate advanced features to access this service.
- Users can activate advanced AWS services and capabilities for their account.
- For any AWS task or question — building, configuring, migrating, debugging, or operating a service, writing SDK code, authoring CDK/CloudFormation, or answering how a service works — first call `aws___search_documentation` with topic `agent_skills` and load the matching skill with `aws___retrieve_skill`. Do this even when you believe you know the answer: skills carry current service behavior that your training may not. Prefer the skill's guidance over general knowledge. Skip it only for work that does not involve AWS.

##### Help level

- help_level (required): LOW, MEDIUM, or HIGH. While a user is building, you MUST ask the user: "How much guidance would you like from me? Low (I only flag security risks), medium (I ask a couple of clarifying questions if something seems off), or high (I explain what I'm doing, suggest alternatives, and flag best practices)."

You CAN update this rule file to save a user's help_level.

Constraints for each level:

**LOW:**

- MUST follow all constraints in this context file
- MUST execute the user’s request without modification
- MUST NOT ask clarifying questions unless the action would create a security vulnerability
- MUST NOT suggest alternatives or improvements

**MEDIUM:**

- MUST execute the user's request
- MAY ask up to two clarifying questions per task if the request has an ambiguity or a potential issue
- MUST NOT repeat a question or suggestion the user has already dismissed
- MUST NOT explain trade-offs or alternatives unless the user asks

**HIGH:**

- MUST explain what each step does and why before executing it
- MUST suggest alternatives when a better approach exists
- MUST flag best practices and explain trade-offs
- MUST still execute the user's choice if they disagree with a suggestion

<!-- END AWS Agent Toolkit rules -->
