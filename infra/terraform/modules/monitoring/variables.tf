variable "name" {
  type = string
}

variable "db_instance_id" {
  type = string
}

variable "alert_email" {
  description = "Receives alarm emails after confirming the link AWS sends. Null means no email."
  type        = string
  default     = null
}
