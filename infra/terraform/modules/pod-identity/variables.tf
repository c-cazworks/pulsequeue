variable "role_name" {
  type = string
}

variable "policy_arns" {
  description = "Policies to attach, keyed by a static label."
  type        = map(string)
}
