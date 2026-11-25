data "aws_partition" "current" {}

locals {
  policy_prefix = "arn:${data.aws_partition.current.partition}:iam::aws:policy"
  oidc_issuer   = trimprefix(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://")
}

# --- Control plane
resource "aws_iam_role" "cluster" {
  name = "${var.name}-cluster"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "eks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "${local.policy_prefix}/AmazonEKSClusterPolicy"
}

# EKS would otherwise create this itself, with no expiry, and leave it behind on destroy.
resource "aws_cloudwatch_log_group" "cluster" {
  name              = "/aws/eks/${var.name}/cluster"
  retention_in_days = 7
}

resource "aws_eks_cluster" "this" {
  name     = var.name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  enabled_cluster_log_types     = ["api", "audit", "authenticator"]
  bootstrap_self_managed_addons = false

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  # Auto-upgrade at the end of standard support instead of paying for extended support.
  upgrade_policy {
    support_type = "STANDARD"
  }

  vpc_config {
    subnet_ids              = var.subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.api_allowed_cidrs
  }

  depends_on = [
    aws_iam_role_policy_attachment.cluster,
    aws_cloudwatch_log_group.cluster,
  ]
}

resource "aws_iam_openid_connect_provider" "this" {
  url            = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list = ["sts.amazonaws.com"]
}

# --- Networking add-ons: nodes cannot become Ready without these
module "vpc_cni_irsa" {
  source = "../irsa"

  role_name         = "${var.name}-vpc-cni"
  oidc_provider_arn = aws_iam_openid_connect_provider.this.arn
  oidc_issuer       = local.oidc_issuer
  namespace         = "kube-system"
  service_account   = "aws-node"
  policy_arns       = { cni = "${local.policy_prefix}/AmazonEKS_CNI_Policy" }
}

resource "aws_eks_addon" "vpc_cni" {
  cluster_name             = aws_eks_cluster.this.name
  addon_name               = "vpc-cni"
  service_account_role_arn = module.vpc_cni_irsa.role_arn
}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name = aws_eks_cluster.this.name
  addon_name   = "kube-proxy"
}

# --- Nodes
resource "aws_iam_role" "node" {
  name = "${var.name}-node"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset(["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryPullOnly"])

  role       = aws_iam_role.node.name
  policy_arn = "${local.policy_prefix}/${each.key}"
}

resource "aws_launch_template" "node" {
  name_prefix = "${var.name}-node-"

  # IMDSv2 with one hop: pods cannot borrow the node role and must use IRSA.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      volume_size           = 20
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  # default_tags does not reach resources launched by the autoscaling group.
  dynamic "tag_specifications" {
    for_each = toset(["instance", "volume"])

    content {
      resource_type = tag_specifications.value
      tags          = { Name = "${var.name}-node", Project = var.name }
    }
  }
}

resource "aws_eks_node_group" "this" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "default"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.subnet_ids
  ami_type        = "AL2023_x86_64_STANDARD"
  capacity_type   = var.node_capacity_type
  instance_types  = var.node_instance_types

  launch_template {
    id      = aws_launch_template.node.id
    version = aws_launch_template.node.latest_version
  }

  # Two nodes so the PodDisruptionBudget has somewhere to move pods during a drain.
  scaling_config {
    min_size     = 2
    desired_size = 2
    max_size     = 3
  }

  update_config {
    max_unavailable = 1
  }

  # Also orders destroy: nodes go first, while the CNI can still release its ENIs.
  depends_on = [
    aws_iam_role_policy_attachment.node,
    aws_eks_addon.vpc_cni,
    aws_eks_addon.kube_proxy,
  ]
}

# --- Add-ons that need nodes to run on; metrics-server feeds the HPA
resource "aws_eks_addon" "workload" {
  for_each = toset(["coredns", "metrics-server"])

  cluster_name = aws_eks_cluster.this.name
  addon_name   = each.key

  depends_on = [aws_eks_node_group.this]
}
