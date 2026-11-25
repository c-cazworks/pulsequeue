output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "cluster_security_group_id" {
  description = "Attached to every node by EKS."
  value       = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}
