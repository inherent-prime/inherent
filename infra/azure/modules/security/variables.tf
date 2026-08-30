variable "resource_group_name" {
  description = "Resource group for Key Vault and the workload identity."
  type        = string
}

variable "location" {
  description = "Azure region."
  type        = string
}

variable "resource_prefix" {
  description = "Prefix applied to all resource names."
  type        = string
}

variable "environment" {
  description = "Environment label, used in resource names and tags."
  type        = string
}

variable "tags" {
  description = "Tags applied to all resources this module creates."
  type        = map(string)
  default     = {}
}

variable "enable_private_endpoints" {
  description = "Put Key Vault behind a private endpoint and deny public network access."
  type        = bool
  default     = true
}

variable "subnet_id" {
  description = "Data subnet ID for the Key Vault private endpoint (network module's subnet_ids[\"data\"]). Ignored when enable_private_endpoints = false."
  type        = string
  default     = ""
}

variable "private_dns_zone_id" {
  description = "privatelink.vaultcore.azure.net zone ID (network module's private_dns_zone_ids[\"vault\"]). Ignored when enable_private_endpoints = false."
  type        = string
  default     = ""
}
