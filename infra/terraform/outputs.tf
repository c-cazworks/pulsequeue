output "cluster_name" {
  value = module.eks.cluster_name
}

output "configure_kubectl" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

output "ecr_repository_urls" {
  value = module.ecr.repository_urls
}

output "db_address" {
  value = module.rds.address
}

output "db_secret_arn" {
  value = module.rds.secret_arn
}

output "vpc_id" {
  value = module.vpc.vpc_id
}

output "oidc_provider_arn" {
  description = "Input for modules/irsa when granting workloads AWS access."
  value       = module.eks.oidc_provider_arn
}

output "oidc_issuer" {
  value = module.eks.oidc_issuer
}
