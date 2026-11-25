variable "role_name" {
  type = string
}

variable "oidc_provider_arn" {
  type = string
}

variable "oidc_issuer" {
  description = "OIDC issuer without the https:// prefix."
  type        = string
}

variable "namespace" {
  type = string
}

variable "service_account" {
  type = string
}

variable "policy_arns" {
  description = "Policies to attach, keyed by a static label."
  type        = map(string)
}
