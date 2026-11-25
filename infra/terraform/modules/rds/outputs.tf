output "address" {
  value = aws_db_instance.this.address
}

output "secret_arn" {
  description = "Secrets Manager secret holding the username and password."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}
