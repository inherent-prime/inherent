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
  description = "Private-endpoint subnet ID for the Key Vault private endpoint (network module's subnet_ids[\"pe\"] — Key Vault's PE cannot live in the PG-delegated \"data\" subnet). Ignored when enable_private_endpoints = false."
  type        = string
  default     = ""
}

variable "private_dns_zone_id" {
  description = "privatelink.vaultcore.azure.net zone ID (network module's private_dns_zone_ids[\"vault\"]). Ignored when enable_private_endpoints = false."
  type        = string
  default     = ""
}

variable "deployer_ip_ranges" {
  description = <<-EOT
    CIDRs allowed through Key Vault's network ACLs in addition to the VNet, when
    enable_private_endpoints = true. Terraform itself runs the KV secret writes in this
    module (azurerm_key_vault_secret.generated) as a data-plane call — with
    enable_private_endpoints = true the vault's default_action is "Deny" and no ip_rules,
    so a deployer running outside the VNet (a laptop, most CI runners) gets 403 on every
    secret write. Set this to the deployer's egress IP/CIDR (e.g. ["203.0.113.4/32"]) when
    that's the case. Empty list = only VNet/private-endpoint traffic and Azure services
    (bypass = AzureServices) can reach the vault's data plane, which is correct only when
    Terraform itself runs from inside the VNet (a jumpbox, a self-hosted CI runner peered
    into it, or a VPN/ExpressRoute session).
  EOT
  type        = list(string)
  default     = []
}
