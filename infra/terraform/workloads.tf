# AWS access for pods. Each association names one namespace and service account.
# Names must match deploy/helm and scripts/deploy.sh.
resource "aws_iam_policy" "app" {
  name = "${var.name}-app"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = module.rds.secret_arn
    }]
  })
}

module "app_role" {
  source = "./modules/pod-identity"

  role_name   = "${var.name}-app"
  policy_arns = { db_secret = aws_iam_policy.app.arn }
}

# The migration hook runs before Helm creates the app's service account, so it has its own.
resource "aws_eks_pod_identity_association" "app" {
  for_each = toset([var.name, "${var.name}-migrate"])

  cluster_name    = module.eks.cluster_name
  namespace       = var.name
  service_account = each.key
  role_arn        = module.app_role.role_arn
}

# Upstream policy, unmodified so upgrades can be diffed. Keep in step with LBC_VERSION in
# scripts/deploy.sh.
resource "aws_iam_policy" "load_balancer_controller" {
  name   = "${var.name}-load-balancer-controller"
  policy = file("${path.module}/policies/aws-load-balancer-controller-v3.7.0.json")
}

module "load_balancer_controller_role" {
  source = "./modules/pod-identity"

  role_name   = "${var.name}-load-balancer-controller"
  policy_arns = { controller = aws_iam_policy.load_balancer_controller.arn }
}

resource "aws_eks_pod_identity_association" "load_balancer_controller" {
  cluster_name    = module.eks.cluster_name
  namespace       = "kube-system"
  service_account = "aws-load-balancer-controller"
  role_arn        = module.load_balancer_controller_role.role_arn
}
