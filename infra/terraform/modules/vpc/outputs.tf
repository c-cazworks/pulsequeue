output "vpc_id" {
  value = aws_vpc.this.id
}

# Waits for egress routes, so nodes never launch into a subnet with no way out.
output "private_subnet_ids" {
  value = [for s in aws_subnet.private : s.id]

  depends_on = [
    aws_route_table_association.private,
    aws_route_table_association.public,
  ]
}
