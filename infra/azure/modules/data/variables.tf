variable "resource_group_name" {
  description = "Resource group for all data-layer resources."
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

variable "subnet_id" {
  description = "network module's subnet_ids[\"data\"] — delegated for PG Flexible, and the private-endpoint subnet for Redis/Cosmos/Blob."
  type        = string
}

variable "private_dns_zone_ids" {
  description = "network module's private_dns_zone_ids map. Must contain \"postgres\", \"redis\", \"cosmos\", \"blob\" when enable_private_endpoints = true."
  type        = map(string)
  default     = {}
}

variable "key_vault_id" {
  description = "security module's key_vault_id — this module writes its connection secrets there."
  type        = string
}

variable "postgres_admin_password" {
  description = "security module's generated postgres_admin_password (sensitive)."
  type        = string
  sensitive   = true
}

variable "postgres_admin_password_kv_secret" {
  description = "security module's postgres_admin_password_kv_secret name, re-exposed on this module's output so downstream (apps) only depends on modules/data for the data-layer secret namespace."
  type        = string
}

variable "enable_ha" {
  description = "Zone-redundant PG HA, zone-redundant Cosmos Mongo vCore."
  type        = bool
  default     = true
}

variable "enable_dr" {
  description = "GRS storage replication + geo-redundant PG backups."
  type        = bool
  default     = true
}

variable "enable_private_endpoints" {
  description = "Private-endpoint (or VNet-delegated, for PG) every data service and deny public network access."
  type        = bool
  default     = true
}

variable "pg_sku" {
  description = "PG Flexible Server compute SKU."
  type        = string
  default     = "GP_Standard_D2ds_v5"
}

variable "pg_storage_mb" {
  description = "PG Flexible Server storage size (MB)."
  type        = number
  default     = 65536
}

variable "cosmos_mongo_sku" {
  description = "Cosmos DB for MongoDB vCore compute tier (e.g. M30)."
  type        = string
  default     = "M30"
}

variable "redis_sku" {
  description = "Azure Cache for Redis SKU tier name (Basic/Standard/Premium)."
  type        = string
  default     = "Standard"
}

variable "redis_family" {
  description = "Azure Cache for Redis SKU family (C = Basic/Standard, P = Premium)."
  type        = string
  default     = "C"
}

variable "redis_capacity" {
  description = "Azure Cache for Redis SKU capacity (size within the family)."
  type        = number
  default     = 1
}
