variable "name" {
  description = "Prefix for all resources; also the EKS cluster name."
  type        = string
  default     = "pulsequeue"
}

variable "region" {
  description = "Must match the AWS project's assigned region; all others are blocked by policy."
  type        = string
  default     = "us-east-2"
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "kubernetes_version" {
  description = "Keep on a version in EKS standard support; extended support costs about 6x."
  type        = string
  default     = "1.36"
}

variable "api_allowed_cidrs" {
  description = "CIDRs allowed to reach the public EKS API endpoint, e.g. [\"203.0.113.4/32\"]."
  type        = list(string)

  validation {
    condition = length(var.api_allowed_cidrs) > 0 && alltrue([
      for c in var.api_allowed_cidrs : can(cidrhost(c, 0)) && !endswith(c, "/0")
    ])
    error_message = "Provide at least one valid CIDR; /0 is not allowed."
  }
}

variable "node_instance_types" {
  description = "x86 types the AWS Free plan can launch. t3.small fits at most 11 pods per node."
  type        = list(string)
  default     = ["t3.small"]

  validation {
    condition = alltrue([
      for t in var.node_instance_types :
      contains(["t3.small", "c7i-flex.large", "m7i-flex.large"], t)
    ])
    error_message = "Use t3.small, c7i-flex.large or m7i-flex.large; the Free plan rejects other x86 types."
  }
}

variable "node_capacity_type" {
  type    = string
  default = "ON_DEMAND"

  validation {
    condition     = contains(["ON_DEMAND", "SPOT"], var.node_capacity_type)
    error_message = "Use ON_DEMAND or SPOT."
  }
}
