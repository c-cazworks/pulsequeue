variable "name" {
  type = string
}

variable "kubernetes_version" {
  type = string
}

variable "subnet_ids" {
  description = "Private subnets for the nodes and control-plane network interfaces."
  type        = list(string)
}

variable "api_allowed_cidrs" {
  type = list(string)
}

variable "node_instance_types" {
  type = list(string)
}

variable "node_capacity_type" {
  type = string
}
