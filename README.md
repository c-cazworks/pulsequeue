# pulsequeue
a small React web dashboard backed by a Python/Go API + background worker and PostgreSQL, deployed on AWS EKS via Terraform and GitHub Actions + GitOps/Helm.

## Running a demo

Run commands in PowerShell from the repo root.

### Option A: local (free)

First time only:

```powershell
Copy-Item .env.example .env      # then set POSTGRES_PASSWORD in .env
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r apps/api/requirements-dev.txt
npm.cmd ci --prefix apps/web
```

| | Command |
|---|---|
| Start | `make up`, then open http://localhost:8080 |
| Stop, keep data | `docker compose stop` |
| Stop, **delete data** | `make down` |

### Option B: AWS (uses Free plan credits)

Expect roughly $0.23–0.28 of credits per hour from the moment `make tf-apply` starts until
`make tf-destroy` finishes. Allow about 30 minutes to start and 20–30 to shut down. Never upgrade
the plan or activate "advanced features": neither can be reversed, and the Free plan is what
prevents real charges.

#### First time only

```powershell
Copy-Item infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
make tf-init
```

#### Start up

1. Sign in, and use the project's profile in this window:

   ```powershell
   $env:AWS_PROFILE = "pulsequeue"
   aws login
   aws freetier get-account-plan-state     # plan type and remaining credits
   ```

2. Find your public IP and set it in `infra/terraform/terraform.tfvars` as
   `api_allowed_cidrs = ["<your ip>/32"]`. It can change between sessions.

   ```powershell
   (Invoke-WebRequest -UseBasicParsing https://checkip.amazonaws.com).Content.Trim()
   ```

3. Review the plan, then create everything:

   ```powershell
   make tf-plan
   make tf-apply
   ```

4. Connect `kubectl` and check that both nodes are `Ready`:

   ```powershell
   aws eks update-kubeconfig --region us-east-2 --name pulsequeue
   kubectl get nodes
   ```

5. Deploy the app. This needs a commit that CI has published from `main`, and Docker running. The
   first time, let `gh` read private packages with `gh auth refresh -s read:packages`.

   ```powershell
   make deploy                  # latest origin/main; or: make deploy TAG=<full commit sha>
   ```

   It prints the dashboard's URL. Only the addresses in `api_allowed_cidrs` can open it. It is plain
   HTTP, because TLS needs a domain.

#### Shut down

Do this after every demo. Stopping pods is not enough: the cluster, NAT gateway, nodes and database
use credits until they are destroyed. **All data and pushed images are deleted.**

1. Destroy everything. This does not ask for confirmation:

   ```powershell
   $env:AWS_PROFILE = "pulsequeue"
   aws login
   make tf-destroy
   ```

   If it refuses because of the `kubectl` context, run the command it prints and try again. If your
   IP has changed since start-up, update `terraform.tfvars` and run `make tf-plan` and `make tf-apply`
   first so that `kubectl` can reach the cluster.

   If it stops while deleting the load balancer, fix that first: removing the cluster would leave
   the load balancer behind and block deleting the VPC. If the app was never deployed, you can skip
   that step with `terraform -chdir=infra/terraform destroy`.

2. Confirm nothing that uses credits is left. Each command should print `[]`:

   ```powershell
   aws eks list-clusters --query clusters
   aws ec2 describe-instances --filters Name=instance-state-name,Values=pending,running,stopping,stopped --query "Reservations[].Instances[].InstanceId"
   aws ec2 describe-nat-gateways --filter Name=state,Values=pending,available,deleting --query "NatGateways[].NatGatewayId"
   aws ec2 describe-addresses --query "Addresses[].AllocationId"
   aws ec2 describe-volumes --query "Volumes[].VolumeId"
   aws elbv2 describe-load-balancers --query "LoadBalancers[].LoadBalancerName"
   aws rds describe-db-instances --query "DBInstances[].DBInstanceIdentifier"
   aws rds describe-db-snapshots --query "DBSnapshots[].DBSnapshotIdentifier"
   ```

Terraform state is stored only on this machine (`infra/terraform/terraform.tfstate`). Do not delete
it while AWS resources exist, because `make tf-destroy` needs it to find them.

## Tests

With the local stack running and the virtual environment active:

```powershell
$env:PULSEQUEUE_TEST_DB = "1"; $env:PGHOST = "127.0.0.1"
$env:PGUSER = "<POSTGRES_USER>"; $env:PGDATABASE = "<POSTGRES_DB>"; $env:PGPASSWORD = '<POSTGRES_PASSWORD>'
make test
terraform -chdir=infra/terraform test     # mocked; needs no AWS account
```

Use the values from `.env`. Without them, the database tests are skipped.
