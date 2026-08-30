variable "resource_group_name" {
  description = "Resource group the network resources are created in (or looked up in, for BYO-VNet)."
  type        = string
}

variable "location" {
  description = "Azure region for network resources."
  type        = string
}

variable "resource_prefix" {
  description = "Prefix applied to all resource names (see root variable of the same name)."
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

# Issue #321: default address space sized for AKS CNI overlay pod IPs + headroom;
# override only if it collides with an existing VNet the caller is peering to.
variable "vnet_cidr" {
  description = "VNet address space, used only when existing_vnet_id is not set."
  type        = string
  default     = "10.20.0.0/16"
}

variable "aks_subnet_cidr" {
  description = "AKS node subnet CIDR (must fit inside vnet_cidr)."
  type        = string
  default     = "10.20.0.0/20"
}

variable "data_subnet_cidr" {
  description = "Subnet CIDR for private endpoints (PG delegation, Redis, Cosmos, Key Vault, Blob)."
  type        = string
  default     = "10.20.16.0/24"
}

variable "appgw_subnet_cidr" {
  description = "Application Gateway subnet CIDR, dedicated per Azure App Gateway v2 requirements."
  type        = string
  default     = "10.20.17.0/24"
}

# Issue #321 enterprise BYO-VNet: when set, no VNet/subnets/NSGs/NAT gateway are created here —
# the caller's existing_subnet_ids map is passed straight through as this module's subnet_ids
# output so downstream modules (aks/data/security) see an identical interface either way.
variable "existing_vnet_id" {
  description = "Existing VNet resource ID. Empty string (default) means this module creates the VNet."
  type        = string
  default     = ""
}

variable "existing_subnet_ids" {
  description = "Existing subnet IDs, keyed \"aks\"/\"data\"/\"appgw\". Required when existing_vnet_id is set."
  type        = map(string)
  default     = {}
}

variable "enable_private_endpoints" {
  description = "Create private DNS zones for the data-layer privatelink names. False only for disconnected/dev evaluation."
  type        = bool
  default     = true
}
