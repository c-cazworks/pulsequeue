# Waits for the policies, so a workload given this role never starts without permissions.
output "role_arn" {
  value = aws_iam_role.this.arn

  depends_on = [aws_iam_role_policy_attachment.this]
}
