.PHONY: help up down logs test lint validate-infra helm-lint tf-init tf-plan tf-apply check-cluster deploy k8s-cleanup tf-destroy

TF_DIR   := infra/terraform
HELM_DIR := deploy/helm
RELEASE  := pulsequeue
NS       := pulsequeue

# On Windows, sh would pick npm's bash script, whose shebang can resolve to WSL's bash.exe.
NPM := npm
ifeq ($(OS),Windows_NT)
NPM := npm.cmd
endif

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Local Development (Slice 1) ---
up: ## Start local stack (React, FastAPI, Worker, Postgres)
	docker compose up --build -d

down: ## Stop local stack and remove volumes
	docker compose down -v

logs: ## Tail local container logs
	docker compose logs -f --tail=100

# --- Verification & Quality Gates ---
test: ## Run backend and frontend unit tests
	cd apps/api && pytest -q
	cd apps/web && $(NPM) test -- --run

lint: ## Lint API and build-check frontend
	cd apps/api && ruff check .
	cd apps/web && $(NPM) run lint && $(NPM) run build

validate-infra: ## Validate Terraform formatting/syntax and lint Helm chart
	terraform -chdir=$(TF_DIR) fmt -check -recursive
	terraform -chdir=$(TF_DIR) validate
	helm lint $(HELM_DIR) -f $(HELM_DIR)/values.yaml

# --- AWS & Kubernetes Lifecycle (Slices 2–4) ---
tf-init: ## Initialize Terraform backend and providers
	terraform -chdir=$(TF_DIR) init

tf-plan: ## Preview AWS infrastructure changes
	terraform -chdir=$(TF_DIR) plan -out=tfplan

tf-apply: ## Apply planned AWS infrastructure
	terraform -chdir=$(TF_DIR) apply tfplan && rm -f $(TF_DIR)/tfplan

check-cluster: ## Fail unless kubectl points at the PulseQueue EKS cluster
	@ctx=$$(kubectl config current-context 2>/dev/null); \
	case "$$ctx" in \
		arn:aws:eks:*:cluster/$(RELEASE)) ;; \
		*) echo "Refusing: kubectl context is '$$ctx', not the $(RELEASE) EKS cluster."; \
		   echo "Run: aws eks update-kubeconfig --region us-east-2 --name $(RELEASE)"; \
		   exit 1 ;; \
	esac

deploy: check-cluster ## Deploy a CI-published commit to EKS (TAG=<sha>, default origin/main)
	sh scripts/deploy.sh $(TAG)

# Deleting an ingress waits until the controller has deleted its ALB. A failure stops tf-destroy,
# because removing the controller first would orphan the ALB and block deleting the VPC.
k8s-cleanup: check-cluster ## Remove the load balancer, then the release
	kubectl delete ingress --all -n $(NS) --ignore-not-found=true --timeout=5m
	-helm uninstall $(RELEASE) -n $(NS) --ignore-not-found
	-helm uninstall aws-load-balancer-controller -n kube-system --ignore-not-found

# Skips the Kubernetes step when there is no cluster, so a failed apply can still be destroyed.
tf-destroy: ## Cleanly tear down K8s ingress/load balancers before destroying AWS infra
	@if terraform -chdir=$(TF_DIR) state list 2>/dev/null | grep -qx 'module.eks.aws_eks_cluster.this'; then \
		$(MAKE) --no-print-directory k8s-cleanup; \
	fi
	terraform -chdir=$(TF_DIR) destroy -auto-approve
