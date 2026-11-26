data "aws_availability_zones" "available" {
  state = "available"

  # Excludes Local Zones, which cannot host the EKS control plane.
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

module "vpc" {
  source = "./modules/vpc"

  name = var.name
  cidr = var.vpc_cidr
  azs  = slice(data.aws_availability_zones.available.names, 0, 2)
}

module "ecr" {
  source = "./modules/ecr"

  repositories = [for app in ["api", "web"] : "${var.name}/${app}"]
}

module "eks" {
  source = "./modules/eks"

  name                = var.name
  kubernetes_version  = var.kubernetes_version
  subnet_ids          = module.vpc.private_subnet_ids
  api_allowed_cidrs   = var.api_allowed_cidrs
  node_instance_types = var.node_instance_types
}

module "rds" {
  source = "./modules/rds"

  name                     = var.name
  vpc_id                   = module.vpc.vpc_id
  subnet_ids               = module.vpc.private_subnet_ids
  client_security_group_id = module.eks.cluster_security_group_id
}

module "monitoring" {
  source = "./modules/monitoring"

  name           = var.name
  db_instance_id = module.rds.identifier
  alert_email    = var.alert_email
}
