# Root variables — exact names per .memory/azure-build-spec.md "Root variables (exact names)".
#
# NO secret variables here (issues #321/#322): every credential is random_password- or
# Azure-issued, generated in modules/security and modules/data, and stored in Key Vault —
# never a tfvar. That still means every secret lands in Terraform state (see backend.tf /
# README.md), the same caveat docs/getting-started/production.md:76-82 documents for the
# Hetzner root — the state backend must be private + RBAC'd.

variable "location" {
  description = "Primary Azure region."
  type        = string
  default     = "eastus2"
}

variable "location_dr" {
  description = "Paired DR region (modules/dr) — only used when enable_dr = true."
  type        = string
  default     = "centralus"
}

variable "resource_prefix" {
  description = "Prefix applied to every resource name."
  type        = string
  default     = "inherent"
}

variable "environment" {
  description = "Environment label, applied to resource names and tags."
  type        = string
  default     = "prod"
}

variable "tags" {
  description = "Tags applied to every resource this root creates."
  type        = map(string)
  default     = {}
}

# --- Profile knobs ---------------------------------------------------------------------


variable "embedding_profile" {
  description = "\"azure_openai\" (default, depends on #311/PR #314) = azurerm_cognitive_account OpenAI deployment. \"tei\" = TEI CPU Deployment on AKS, self-hosted fallback."
  type        = string
  default     = "azure_openai"

  validation {
    condition     = contains(["azure_openai", "tei"], var.embedding_profile)
    error_message = "embedding_profile must be \"azure_openai\" or \"tei\"."
  }
}

# Reserved knob: only "minio" is implementable until the native Azure Blob backend lands
# (#329) — the validation below is the whole point of declaring it today, so operators get
# a clear rejection instead of a silent no-op if they set "azure_blob" early.
# tflint-ignore: terraform_unused_declarations
variable "storage_profile" {
  description = "Object storage backend. Only \"minio\" (MinIO StatefulSet on AKS + hourly mirror to Blob) is implemented today — the app's storage abstraction is s3-compatible + local only. Native Azure Blob backend is tracked as a separate roadmap item, issue #329."
  type        = string
  default     = "minio"

  validation {
    condition     = var.storage_profile == "minio"
    error_message = "storage_profile only supports \"minio\" today — native Azure Blob backend is not yet implemented in the app's storage abstraction (issue #329)."
  }
}

variable "ingress_profile" {
  description = "\"nginx\" (default) = ingress-nginx + cert-manager (Let's Encrypt) on a Standard LB. \"appgw_waf\" = Application Gateway WAF_v2 (higher cost, WAF/DDoS posture) on the appgw subnet."
  type        = string
  default     = "nginx"

  validation {
    condition     = contains(["nginx", "appgw_waf"], var.ingress_profile)
    error_message = "ingress_profile must be \"nginx\" or \"appgw_waf\"."
  }
}

# --- HA / DR -----------------------------------------------------------------------------

variable "enable_ha" {
  description = "Zone-redundant PG HA standby, zone-redundant Cosmos Mongo vCore, multi-zone AKS user pool."
  type        = bool
  default     = true
}

variable "enable_dr" {
  description = "GRS storage replication, geo-redundant PG backups, and stands up modules/dr (secondary-region storage + runbook outputs)."
  type        = bool
  default     = true
}

variable "pg_geo_replica" {
  description = "Also create a cross-region PG read replica (modules/dr) for faster failover than geo-restore alone. Off by default — geo-redundant backup (enable_dr) already meets the RPO/RTO targets; this adds standing compute cost."
  type        = bool
  default     = false
}

# --- Enterprise BYO-VNet ------------------------------------------------------------------

variable "existing_vnet_id" {
  description = "Existing VNet resource ID to deploy into. Empty string (default) means modules/network creates the VNet."
  type        = string
  default     = ""
}

variable "existing_subnet_ids" {
  description = "Existing subnet IDs, keyed \"aks\"/\"data\"/\"appgw\". Required when existing_vnet_id is set."
  type        = map(string)
  default     = {}
}

variable "private_cluster_enabled" {
  description = "Make the AKS API server private (no public endpoint). Requires network line-of-sight (VPN/ExpressRoute/peering) from wherever kubectl/terraform runs."
  type        = bool
  default     = false
}

variable "authorized_ip_ranges" {
  description = "CIDRs allowed to reach the AKS API server when it is public (private_cluster_enabled = false). Empty list = open to the internet — set this in production."
  type        = list(string)
  default     = []
}

variable "enable_private_endpoints" {
  description = "Private-endpoint (or VNet-delegate, for PG) every data service and Key Vault, denying public network access. False only for disconnected/local evaluation."
  type        = bool
  default     = true
}

# --- Sizing --------------------------------------------------------------------------------

variable "aks_system_vm_size" {
  description = "VM size for the AKS system node pool."
  type        = string
  default     = "Standard_D2s_v5"
}

variable "aks_user_vm_size" {
  description = "VM size for the AKS user (workload) node pool."
  type        = string
  default     = "Standard_D4s_v5"
}

variable "aks_user_min_count" {
  description = "Cluster autoscaler minimum node count, user pool."
  type        = number
  default     = 3
}

variable "aks_user_max_count" {
  description = "Cluster autoscaler maximum node count, user pool. Primary knob for raising the AKS-layer QPS ceiling."
  type        = number
  default     = 6

  validation {
    condition     = var.aks_user_max_count >= var.aks_user_min_count
    error_message = "aks_user_max_count must be >= aks_user_min_count."
  }
}

variable "api_replicas_min" {
  description = "public-api HPA minimum replica count."
  type        = number
  default     = 2
}

variable "api_replicas_max" {
  description = "public-api HPA maximum replica count."
  type        = number
  default     = 6

  validation {
    condition     = var.api_replicas_max >= var.api_replicas_min
    error_message = "api_replicas_max must be >= api_replicas_min."
  }
}

variable "worker_replicas" {
  description = "Ingestion worker replica count. Safe to scale out (Temporal task-queue + consumer-group semantics, not a singleton like the migrate hook Job)."
  type        = number
  default     = 2
}

variable "pg_sku" {
  description = "PG Flexible Server compute SKU."
  type        = string
  default     = "GP_Standard_D2ds_v5"
}

variable "pg_storage_mb" {
  description = "PG Flexible Server storage size, in MB."
  type        = number
  default     = 65536
}

variable "cosmos_mongo_sku" {
  description = "Cosmos DB for MongoDB vCore compute tier."
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

variable "weaviate_disk_gb" {
  description = "Weaviate StatefulSet PVC size (Premium_ZRS), GB."
  type        = number
  default     = 64
}

variable "minio_disk_gb" {
  description = "MinIO StatefulSet PVC size (Premium_ZRS), GB."
  type        = number
  default     = 128
}

# --- App ------------------------------------------------------------------------------------

variable "inherent_version" {
  description = "public-api-svc / ingestion-svc image tag. Pin a real release — never \"latest\" in prod (see envs/prod.tfvars.example)."
  type        = string
  default     = "0.6.0"
}

variable "dns_zone_name" {
  description = "Existing Azure DNS zone to create dns_record in. Leave empty and set api_hostname instead if DNS is managed outside Azure DNS."
  type        = string
  default     = ""
}

variable "dns_record" {
  description = "Record name created in dns_zone_name (e.g. \"api\" -> api.<dns_zone_name>). Ignored when api_hostname is set."
  type        = string
  default     = ""
}

variable "api_hostname" {
  description = "Fully-qualified public API hostname when DNS is managed outside Azure DNS (dns_zone_name left empty). One of dns_zone_name+dns_record or api_hostname must resolve to the ingress IP/FQDN out of band."
  type        = string
  default     = ""
}

variable "letsencrypt_email" {
  description = "Contact email for cert-manager's Let's Encrypt ACME registration (ingress_profile = nginx)."
  type        = string
  default     = ""
}

# Deviation from the spec's enumerated root variable list: modules/monitoring requires an
# explicit alert contact (alert_email_address, no default — alerts must never be silently
# unroutable) separate from letsencrypt_email's ACME registration purpose. Defaults to
# letsencrypt_email in main.tf when left empty so most operators only set one address.
variable "alert_email_address" {
  description = "Email receiver for the monitoring action group's alerts (API 5xx rate, PG CPU, Redis memory, node health). Empty string falls back to letsencrypt_email."
  type        = string
  default     = ""
}

variable "openai_embedding_model" {
  description = "Azure OpenAI embedding deployment name (embedding_profile = azure_openai)."
  type        = string
  default     = "text-embedding-3-small"
}

variable "openai_embedding_dim" {
  description = "Embedding vector dimension — must match openai_embedding_model (1536 for text-embedding-3-small)."
  type        = number
  default     = 1536
}

variable "openai_sku" {
  description = "Azure OpenAI (Cognitive Services) account SKU."
  type        = string
  default     = "S0"
}

variable "openai_capacity" {
  description = "Azure OpenAI deployment capacity, in thousands of tokens/minute (TPM units)."
  type        = number
  default     = 50
}
